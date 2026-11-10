"""Expand evals/gold/templates.yaml into evals/e2e.jsonl, computing every answer from the snapshot.

Usage: uv run evals/update_targets.py [SNAPSHOT_DB]
Re-run after every new snapshot: targets are derived from data, never hand-typed.
"""
import json
import math
import sys
from pathlib import Path

import duckdb
import yaml
from scipy import stats

ROOT = Path(__file__).resolve().parent
MIN_N = 10


def spearman_verdict(rows):
    pairs = [(float(x), float(y)) for x, y in rows if x is not None and y is not None]
    n = len(pairs)
    if n < MIN_N:
        return ["insufficient_data", float("nan"), n]
    rho = float(stats.spearmanr(*zip(*pairs))[0])
    z, se = math.atanh(rho), 1 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    return ["positive" if lo > 0 else "negative" if hi < 0 else "inconclusive", rho, n]


def main(db_path: str | None = None) -> None:
    if db_path is None:
        snaps = sorted((ROOT.parent / "data" / "snapshots").glob("*/pulse.duckdb"))
        db_path = str(snaps[-1])
    con = duckdb.connect(db_path, read_only=True)
    asof = con.execute("SELECT value FROM meta WHERE key = 'asof'").fetchone()[0]
    templates = yaml.safe_load((ROOT / "gold" / "templates.yaml").read_text())
    samples = []
    for t in templates:
        for k, inst in enumerate(t["instances"], start=1):
            v = {}
            if sql := t.get("gold_sql"):
                query = sql.format(**inst)
                if t.get("post") == "spearman":
                    vals = spearman_verdict(con.execute(query).fetchall())
                else:
                    vals = list(con.execute(query).fetchone())
                v = {f"v{i}": x for i, x in enumerate(vals)}
            numbers = [float(v[f"v{i}"]) for i in t.get("numbers", [])]
            samples.append({
                "id": f"{t['id']}-{k}",
                "input": t["question"].format(**inst),
                "target": " ".join(t["target"].format(**inst, **v).split()),
                "metadata": {
                    "template_id": t["id"], "category": t["category"], "expect": t.get("expect", "answer"),
                    "numbers": numbers, "device_caveat": bool(t.get("device_caveat")),
                    "gold_sql": t.get("gold_sql", "").format(**inst) if t.get("gold_sql") else None,
                    "origin": "gold", "source": f"pulse.duckdb snapshot as of {asof}",
                },
            })
    out = ROOT / "e2e.jsonl"
    out.write_text("".join(json.dumps(s) + "\n" for s in samples))
    by_cat = {}
    for s in samples:
        by_cat[s["metadata"]["category"]] = by_cat.get(s["metadata"]["category"], 0) + 1
    print(f"{len(samples)} samples from {len(templates)} templates -> {out} (asof {asof})")
    print(by_cat)


if __name__ == "__main__":
    main(*sys.argv[1:])
