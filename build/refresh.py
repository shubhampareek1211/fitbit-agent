"""Scheduled refresh: fetch the newest data, rebuild the snapshot, check it, publish it to Cloud Storage.

Usage: uv run build/refresh.py [--bucket NAME] [--raw data/raw] [--out data/snapshots] [--no-export]

Runs at 11:00 and 23:00. Each run:
  1. deletes raw files that were fetched before their day (or date range) had ended, so they are fetched again;
  2. exports from the first day that isn't fully fetched up to today (scripts/export_fitbit.sh), and the weather;
  3. builds data/snapshots/<today>/pulse.duckdb: daily metrics through yesterday (the last complete day), sleep
     sessions and workouts through today so far;
  4. checks the new snapshot against the previous one (no lost days, no date gaps, nothing shrinking);
  5. uploads it as snapshots/<today>/pulse.duckdb and then points latest.json at it.
A failed export or check stops before step 5: the app keeps serving the previous snapshot.
"""
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from build import fetch_weather, normalize  # noqa: E402

FIRST_DAY = "2026-08-01"
GRACE = timedelta(hours=1)  # a file counts as complete if it was fetched at least this long after its last day ended


def covered_until(path: Path) -> date | None:
    """The last day a raw file covers: <type>__<from>__<to>.json or intraday/<type>/<day>.json."""
    try:
        return date.fromisoformat(path.stem.split("__")[-1])
    except ValueError:
        return None


def is_stale(path: Path, fetched: datetime) -> bool:
    """True if the file was fetched before the last day it covers was over: more data may have arrived since."""
    last = covered_until(path)
    return last is not None and fetched < datetime.combine(last + timedelta(days=1), time()) + GRACE


def drop_stale(raw: Path) -> list[Path]:
    stale = [f for f in raw.glob("**/*.json") if f.name != "weather.json"
             and is_stale(f, datetime.fromtimestamp(f.stat().st_mtime))]
    for f in stale:
        f.unlink()
    return stale


def export_from(raw: Path, today: date, dropped: list[Path]) -> date:
    """The first day that still needs fetching: the day after the last complete intraday steps file, or the first
    day of any multi-day file that was just dropped (its whole range has to be fetched again), whichever is earlier."""
    days = [d for f in (raw / "intraday" / "steps").glob("*.json") if (d := covered_until(f))]
    start = max(days) + timedelta(days=1) if days else date.fromisoformat(FIRST_DAY)
    for f in dropped:
        parts = f.stem.split("__")
        if len(parts) == 3:
            start = min(start, date.fromisoformat(parts[1]))
    return min(start, today - timedelta(days=1))


def check(new: Path, old: Path | None) -> list[str]:
    """Reasons not to publish the new snapshot (empty = fine)."""
    con = duckdb.connect(str(new), read_only=True)
    q = lambda sql: con.execute(sql).fetchone()
    problems = []
    days, first, last, with_steps = q("SELECT count(*), min(date), max(date), count(steps) FROM daily")
    asof = q("SELECT value FROM meta WHERE key = 'asof'")[0]
    if str(last) != asof:
        problems.append(f"daily ends on {last}, not on asof {asof}")
    if days != (last - first).days + 1:
        problems.append(f"daily has {days} rows for {first}..{last}: a date is missing or repeated")
    if q(f"SELECT steps FROM daily WHERE date = DATE '{asof}'")[0] is None:
        problems.append(f"no steps on the last complete day ({asof}): the export probably failed")
    counts = {t: q(f"SELECT count(*) FROM {t}")[0] for t in ("daily", "sleep_sessions", "exercise", "workout_hr")}
    con.close()
    if old and old.exists() and old != new:
        prev = duckdb.connect(str(old), read_only=True)
        for t, n in counts.items():
            before = prev.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            if n < before:
                problems.append(f"{t} shrank from {before} to {n} rows")
        if (was := prev.execute("SELECT value FROM meta WHERE key = 'asof'").fetchone()[0]) > asof:
            problems.append(f"asof went backwards: {was} -> {asof}")
        prev.close()
    return problems


def publish(db: Path, bucket_name: str, day: str, asof: str, fetched_at: str) -> str:
    from google.cloud import storage
    bucket = storage.Client().bucket(bucket_name)
    name = f"snapshots/{day}/pulse.duckdb"
    bucket.blob(name).upload_from_filename(str(db))
    pointer = bucket.blob("latest.json")  # written last: readers switch only once the snapshot is fully uploaded
    pointer.cache_control = "no-store"
    pointer.upload_from_string(json.dumps({"day": day, "asof": asof, "fetched_at": fetched_at, "object": name}),
                               content_type="application/json")
    return f"gs://{bucket_name}/{name}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=None, help="Cloud Storage bucket to publish to (omit to build locally only)")
    ap.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out", default=str(ROOT / "data" / "snapshots"))
    ap.add_argument("--no-export", action="store_true", help="rebuild from the raw files already on disk")
    a = ap.parse_args()
    raw, out = Path(a.raw), Path(a.out)
    now = datetime.now()
    today, asof = now.date(), now.date() - timedelta(days=1)
    fetched_at = now.strftime("%Y-%m-%d %H:%M")
    print(f"[refresh] {fetched_at}: daily metrics through {asof}, sessions through {today}")

    previous = sorted(p for p in out.glob("*/pulse.duckdb"))
    previous = previous[-1] if previous else None
    if not a.no_export:
        stale = drop_stale(raw)
        start = export_from(raw, today, stale)
        print(f"[refresh] {len(stale)} incomplete raw file(s) dropped; exporting {start}..{today}")
        r = subprocess.run([str(ROOT / "scripts" / "export_fitbit.sh"), start.isoformat(), today.isoformat(), str(raw)],
                           cwd=ROOT, capture_output=True, text=True)
        print("\n".join(r.stdout.splitlines()[-6:]))
        if r.returncode != 0:
            print(f"[refresh] export failed ({r.returncode}): {r.stderr.strip()[-300:]}\n[refresh] nothing published")
            return 1
        fetch_weather.main(FIRST_DAY, today.isoformat(), str(raw / "weather.json"))

    tmp_out = out / ".building"
    db = normalize.build(raw, asof.isoformat(), tmp_out, through=today.isoformat(), fetched_at=fetched_at)
    problems = check(db, previous)
    if problems:
        print("[refresh] snapshot NOT published:\n  - " + "\n  - ".join(problems))
        return 1
    final = out / today.isoformat() / "pulse.duckdb"
    final.parent.mkdir(parents=True, exist_ok=True)
    db.replace(final)
    db.parent.rmdir()
    tmp_out.rmdir()
    print(f"[refresh] built {final} ({final.stat().st_size / 1e6:.1f} MB)")
    if a.bucket:
        print(f"[refresh] published {publish(final, a.bucket, today.isoformat(), asof.isoformat(), fetched_at)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
