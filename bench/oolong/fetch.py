#!/usr/bin/env python3
"""Fetch OOLONG-synth rows for one data set and chosen context lengths, at the pinned revision.

This is the only part of the harness that uses the network. It runs on the harness side, never in a task session.
It reads the Parquet shards with HTTP range requests, so it does not download the whole 12 GB data set.
It needs pyarrow (pip install pyarrow). Output: <cache>/<dataset>_<len>.jsonl, one row per question.
The cache folder holds the gold answers. It is never inside a task folder.
"""
import argparse, json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = json.load(open(os.path.join(HERE, "dataset.lock.json"), encoding="utf-8"))
BASE = "https://huggingface.co/datasets/%s/resolve/%s/" % (LOCK["hf_dataset"], LOCK["hf_revision"])
TREE = "https://huggingface.co/api/datasets/%s/tree/%s/data" % (LOCK["hf_dataset"], LOCK["hf_revision"])
BLOCK = 8 * 1024 * 1024


class RangeFile:
    """A read-only file object over HTTP range requests, with a small block cache."""

    def __init__(self, url, size):
        self.url, self.size, self.pos, self.blocks = url, size, 0, {}

    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def close(self): pass
    closed = False

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def _block(self, i):
        if i not in self.blocks:
            lo, hi = i * BLOCK, min((i + 1) * BLOCK, self.size) - 1
            req = urllib.request.Request(self.url, headers={"Range": "bytes=%d-%d" % (lo, hi)})
            with urllib.request.urlopen(req, timeout=120) as r:
                self.blocks[i] = r.read()
        return self.blocks[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        end = min(self.pos + n, self.size)
        out = []
        p = self.pos
        while p < end:
            b = self._block(p // BLOCK)
            off = p % BLOCK
            take = b[off:off + (end - p)]
            out.append(take)
            p += len(take)
        self.pos = end
        return b"".join(out)

    def readinto(self, buf):
        d = self.read(len(buf))
        buf[:len(d)] = d
        return len(d)


def shards(split):
    with urllib.request.urlopen(TREE, timeout=60) as r:
        return [x for x in json.load(r) if "/%s-" % split in x["path"]]


def main():
    import pyarrow.parquet as pq
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=LOCK["dataset"])
    ap.add_argument("--split", default=LOCK["split"], help="trec_coarse is in the validation split")
    ap.add_argument("--lens", default="32768", help="comma list of context_len values")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    a = ap.parse_args()
    lens = {int(x) for x in a.lens.split(",")}
    os.makedirs(a.cache, exist_ok=True)
    rows = {n: [] for n in lens}
    for sh in shards(a.split):
        f = RangeFile(BASE + sh["path"], sh["size"])
        pf = pq.ParquetFile(f)
        names = pf.schema_arrow.names
        for g in range(pf.num_row_groups):
            rg = pf.metadata.row_group(g)
            col = {rg.column(i).path_in_schema: rg.column(i).statistics for i in range(rg.num_columns)}
            st = col.get("context_len")
            if st is not None and st.has_min_max and not any(st.min <= n <= st.max for n in lens):
                continue
            meta = pf.read_row_group(g, columns=["dataset", "context_len"]).to_pylist()
            hit = [i for i, r in enumerate(meta) if r["dataset"] == a.dataset and r["context_len"] in lens]
            if not hit:
                continue
            full = pf.read_row_group(g, columns=[c for c in names if c != "context_window_text_with_labels"]).to_pylist()
            for i in hit:
                rows[full[i]["context_len"]].append(full[i])
        print("shard", sh["path"], {n: len(v) for n, v in rows.items()}, file=sys.stderr)
        if all(len(v) >= 50 for v in rows.values()):
            break
    for n, v in rows.items():
        v.sort(key=lambda r: (r["context_window_id"], r["id"]))
        p = os.path.join(a.cache, "%s_%d.jsonl" % (a.dataset, n))
        with open(p, "w", encoding="utf-8") as fh:
            for r in v:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        print("wrote", p, len(v), "rows,", len({r["context_window_id"] for r in v}), "windows")


if __name__ == "__main__":
    main()
