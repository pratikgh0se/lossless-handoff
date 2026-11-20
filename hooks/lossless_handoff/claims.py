# The claims register: --claims, --claims-lint and the evaluation.
# Part of hooks/precompact-handoff.py (the entry file). Loaded by lossless_handoff/__init__.py into the
# namespace of the entry file, so every name here is a global of precompact-handoff.py. The imports are in the entry file.

# ---- F48 claims register

def claims_path():
    q = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claims.json")  # F48 ruling: the running script's directory
    return q if os.path.isfile(q) else None


def _date_epoch(v):
    try:
        return datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return None


def eval_claims(now):
    """F48.b: list of {id, text, value, rate, as_of, status} for every claim in the register (None without a register)."""
    cp = claims_path()
    if not cp:
        return None
    try:
        reg = json.load(open(cp, encoding="utf-8")).get("claims") or []
    except Exception as e:
        log_error("unknown", e)
        return None
    proofs = _read_jsonl(os.path.join(HANDOFFS_DIR, "proofs.jsonl"))
    rj = None
    try:
        rj = json.load(open(os.path.join(HANDOFFS_DIR, "reconcile.json"), encoding="utf-8"))
    except Exception:
        rj = None
    out = []
    dsids = drift_sids()
    drift_on = bool(dsids)
    for c in reg:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        src, refresh = str(c.get("source") or ""), c.get("refresh_days") or 0
        win, minr = c.get("window_days") or 30, c.get("min_rate")
        p = q = 0
        dhit = False
        as_of_e, value = None, ""
        if src.startswith("proof:"):
            name = src[6:]
            for r in proofs:
                e = _iso_epoch(r.get("ts"))
                v = (r.get("checks") or {}).get(name)
                if e is None or e < now - win * 86400:
                    continue
                if str(r.get("sid"))[:8] in dsids and not r.get("agent_id"):
                    v, dhit = "fail", True  # F63: any stored verdict of a drift session fails; only rows inside the claim window count
                if v not in ("pass", "fail"):
                    continue
                q += 1
                p += 1 if v == "pass" else 0
                as_of_e = e if as_of_e is None or e > as_of_e else as_of_e
        elif src in ("reconcile:coverage", "reconcile:subagent_coverage") and isinstance(rj, dict):
            as_of_e = _iso_epoch(rj.get("ts"))
            if src == "reconcile:coverage":
                q = int(rj.get("compactions") or 0) - int(rj.get("missed_headless") or 0)
                p = q - int(rj.get("missed_interactive") or 0)
            else:
                q = int(rj.get("subagent_compactions") or 0)
                p = q - int(rj.get("subagent_missed") or 0)
            p = max(p, 0)
        elif src == "static":
            ev = c.get("evidence") or {}
            as_of_e = _date_epoch(ev.get("date"))
            stale = as_of_e is None or now - as_of_e > refresh * 86400
            out.append({"id": c["id"], "text": c.get("text", ""), "value": str(ev.get("value", "")), "rate": None,
                        "as_of": str(ev.get("date", ""))[:10], "status": "stale" if stale else "pass"})
            continue
        as_of = datetime.datetime.fromtimestamp(as_of_e, datetime.timezone.utc).strftime("%Y-%m-%d") if as_of_e else ""
        if drift_on and src.startswith("reconcile:"):
            status, rate = "fail", (p / q if q else None)  # F63: the counts cannot be trusted
        elif q == 0:
            status, rate = "no-data", None
        else:
            rate = p / q
            if dhit:
                status = "fail"  # F63: the counts cannot be trusted
            elif minr is not None and rate < minr:
                status = "fail"
            elif as_of_e is None or now - as_of_e > refresh * 86400:
                status = "stale"
            else:
                status = "pass"
        row = {"id": c["id"], "text": c.get("text", ""), "value": f"{p}/{q}", "rate": rate, "as_of": as_of, "status": status}
        if drift_on and status == "fail" and (dhit or src.startswith("reconcile:")):
            row["reason"] = "format_drift"
        out.append(row)
    return out


def claims_table(st):
    rows = ["| id | claim | value | as of | status |", "|---|---|---|---|---|"]
    for c in st:
        rows.append(f"| {c['id']} | {c['text']} | {c['value']} | {c['as_of'] or '-'} | {c['status']}{' (' + c['reason'] + ')' if c.get('reason') else ''} |")
    return "\n".join(rows) + "\n"


def claims_print():
    st = eval_claims(time.time())
    if st is None:
        _out("no claims register found")
        return
    sys.stdout.buffer.write(claims_table(st).encode("utf-8"))
    sys.stdout.buffer.flush()


def claims_default_doc():
    """F66.c: docs/evidence.md of the current folder, else of the tree around this script (repo checkout). None if neither exists."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (os.getcwd(), os.path.dirname(here), here):
        q = os.path.join(base, "docs", "evidence.md")
        if os.path.isfile(q):
            return q
    return None


def claims_lint(path):
    """F48.c: every [C<n>] in the file must exist and have status pass. Exit 1 on any problem.
    F66.c: with no file argument it lints docs/evidence.md of the repo tree."""
    if not path or path.startswith("--"):
        path = claims_default_doc()
        if not path:
            _out("no docs/evidence.md found; give a file path")
            raise SystemExit(1)
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        _out(f"cannot read {path}: {type(e).__name__}")
        raise SystemExit(1)
    ids = sorted(set(re.findall(r"\[(C\d+)\]", text)), key=lambda x: int(x[1:]))
    st = {c["id"]: c for c in (eval_claims(time.time()) or [])}
    problems = []
    for i in ids:
        c = st.get(i)
        if c is None:
            problems.append(f"unknown claim {i}")
        elif c["status"] == "fail":
            problems.append(f"{i} fail {c['value']}")
        elif c["status"] == "stale":
            problems.append(f"{i} stale {c['as_of']}")
        elif c["status"] == "no-data":
            problems.append(f"{i} no-data")
    if problems:
        for p in problems:
            _out(p)
        raise SystemExit(1)
    _out(f"claims ok ({len(ids)} cited)")
