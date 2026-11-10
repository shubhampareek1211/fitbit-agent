"""Summarize the latest Inspect log for a task: scores with margin of error, and failing samples.

Usage: uv run evals/report.py [e2e|gatekeeper] [LOG_FILE]
"""
import math
import sys
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log


def latest(task: str) -> str:
    logs = [l for l in list_eval_logs("logs") if task in Path(l.name).name]
    return sorted(logs, key=lambda l: l.name)[-1].name


def main(task: str = "e2e", path: str | None = None) -> None:
    log = read_eval_log(path or latest(task))
    n = len(log.samples or [])
    print(f"{log.eval.task} | {log.eval.created[:19]} | samples={n} | status={log.status}")
    for s in log.results.scores:
        for name, m in s.metrics.items():
            if name == "mean":
                print(f"  {s.name:28} {m.value:6.3f}   (±{1 / math.sqrt(max(s.scored_samples or n, 1)):.3f} = 1/√n,"
                      f" n={s.scored_samples})")
    print("\nFailures:")
    for smp in log.samples or []:
        for name, sc in (smp.scores or {}).items():
            vals = sc.value if isinstance(sc.value, dict) else {name: sc.value}
            bad = [k for k, v in vals.items() if v in (0, 0.0, "I")]
            if bad:
                print(f"  {smp.id} [epoch {smp.epoch}] {name}:{','.join(bad)} | {(sc.explanation or '')[:160]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
