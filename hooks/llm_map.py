#!/usr/bin/env python3
"""llm_map: label each item of a large list with a small model (F60).

Usage: llm_map.py --in FILE --prompt TEXT --out FILE [--model haiku] [--batch 40]
                 [--jobs 4] [--max-usd 2.00] [--json-out]

Each batch runs one `claude -p` call. The call has no tools, no hooks, no MCP
servers and no saved session. Set LLM_MAP_CLAUDE to replace the claude command.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

CALL_TIMEOUT = 600


def die(msg):
    print("llm_map: " + msg, file=sys.stderr)
    sys.exit(2)


def read_items(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        die("cannot read %s: %s" % (path, e))
    items = []
    jsonl = path.endswith(".jsonl")
    for line in lines:
        if not line.strip():
            continue
        if jsonl:
            try:
                obj = json.loads(line)
            except ValueError:
                die("bad JSON line in %s" % path)
            if not isinstance(obj, dict) or "text" not in obj:
                die("each .jsonl object needs a text field")
            it = {"text": str(obj["text"])}
            if "id" in obj:
                it["id"] = obj["id"]
        else:
            it = {"text": line}
        items.append(it)
    return items


def one_line(text):
    return " ".join(str(text).split())


def build_prompt(task, batch, items, json_out):
    kind = "any JSON value" if json_out else "a string"
    lines = [task, "", "Items:"]
    lines += ["%d: %s" % (i, one_line(items[i - 1]["text"])) for i in batch]
    lines += ["",
              "Answer with exactly one JSON line per item and nothing else, in this form:",
              '{"i": <item number>, "out": <answer>}',
              'where "out" is %s. Do not skip an item. Do not add other text.' % kind]
    return "\n".join(lines)


def claude_cmd(model, prompt):
    base = shlex.split(os.environ.get("LLM_MAP_CLAUDE") or "claude")
    return base + [
        "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--settings", '{"disableAllHooks": true}',
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def parse_reply(text, json_out):
    """Return {i: out} for each valid JSON line in the reply (fences allowed)."""
    found = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or "out" not in obj:
            continue
        i = obj.get("i")
        if isinstance(i, str) and i.strip().isdigit():
            i = int(i)
        if isinstance(i, bool) or not isinstance(i, int):
            continue
        out = obj["out"]
        if not json_out and out is not None and not isinstance(out, str):
            out = json.dumps(out) if isinstance(out, (dict, list)) else str(out)
        found[i] = out
    return found


def run_call(model, prompt):
    """Run one claude call in a new temp cwd. Return (result_text, cost, error)."""
    tmp = tempfile.mkdtemp(prefix="llm-map-")
    try:
        try:
            p = subprocess.run(claude_cmd(model, prompt), cwd=tmp, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=CALL_TIMEOUT)
        except FileNotFoundError:
            return None, 0.0, "claude command not found"
        except subprocess.TimeoutExpired:
            return None, 0.0, "timeout"
        except OSError as e:
            return None, 0.0, "cannot run claude: %s" % e
        try:
            obj = json.loads(p.stdout)
        except ValueError:
            return None, 0.0, "bad call output (exit %d)" % p.returncode
        if not isinstance(obj, dict):
            return None, 0.0, "bad call output"
        cost = obj.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) else 0.0
        if obj.get("is_error") or p.returncode != 0:
            return None, cost, ("call failed: " + one_line(obj.get("result") or "")[:80]).strip()
        res = obj.get("result")
        return (res if isinstance(res, str) else ""), cost, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Runner:
    def __init__(self, args, items):
        self.args = args
        self.items = items
        self.lock = threading.Lock()
        self.spent = 0.0
        self.max_cost = 0.0
        self.batches = 0
        self.results = {}   # i -> out
        self.errors = {}    # i -> reason

    def do_batch(self, batch):
        a = self.args
        with self.lock:
            if a.max_usd is not None and self.spent + self.max_cost > a.max_usd:
                for i in batch:
                    self.errors[i] = "budget"
                return
            self.batches += 1
        text, cost, err = run_call(a.model, build_prompt(a.prompt, batch, self.items, a.json_out))
        with self.lock:
            self.spent += cost
            self.max_cost = max(self.max_cost, cost)
            if err:
                for i in batch:
                    self.errors[i] = err
                return
            got = parse_reply(text, a.json_out)
            for i in batch:
                if i in got:
                    self.results[i] = got[i]
                    self.errors.pop(i, None)
                else:
                    self.errors[i] = "missing from reply"

    def run_round(self, indexes):
        size = self.args.batch
        batches = [indexes[k:k + size] for k in range(0, len(indexes), size)]
        with ThreadPoolExecutor(max_workers=self.args.jobs) as ex:
            list(ex.map(self.do_batch, batches))


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".llm-map-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(description="Label each item of a list with a small model.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--max-usd", type=float, default=2.00)
    ap.add_argument("--json-out", action="store_true")
    args = ap.parse_args()  # argparse exits with code 2 on a usage error
    if args.batch < 1 or args.jobs < 1:
        die("--batch and --jobs must be 1 or more")
    if not os.path.isfile(args.inp):
        die("input file not found: %s" % args.inp)
    items = read_items(args.inp)
    if not items:
        die("no items in %s" % args.inp)

    r = Runner(args, items)
    r.run_round(list(range(1, len(items) + 1)))
    retry = [i for i in range(1, len(items) + 1) if i not in r.results and r.errors.get(i) != "budget"]
    if retry:
        r.run_round(retry)

    failed = 0
    lines = []
    for n, it in enumerate(items, 1):
        row = {"i": n}
        if "id" in it:
            row["id"] = it["id"]
        row["text"] = it["text"]
        if n in r.results:
            row["out"] = r.results[n]
        else:
            row["out"] = None
            row["error"] = r.errors.get(n, "failed")
            failed += 1
        lines.append(json.dumps(row, ensure_ascii=False))
    try:
        write_atomic(args.out, "\n".join(lines) + "\n")
    except OSError as e:
        die("cannot write %s: %s" % (args.out, e))
    total = len(items)
    print("llm_map: %d items, %d ok, %d failed, %d batches, cost $%.4f"
          % (total, total - failed, failed, r.batches, r.spent))
    sys.exit(3 if failed else 0)


if __name__ == "__main__":
    main()
