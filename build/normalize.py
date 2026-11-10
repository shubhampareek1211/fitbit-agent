"""Offline build step: raw Google Health API JSON (+ weather) -> one DuckDB snapshot.

Usage: uv run build/normalize.py [--raw data/raw] [--asof YYYY-MM-DD] [--out data/snapshots]

Produces data/snapshots/<asof>/pulse.duckdb with tables:
  daily           one row per date (activity, heart, sleep summary, weather)
  sleep_sessions  one row per sleep session (stages, nap flag)
  exercise        one row per exercise session
  coverage        per metric: source, first/last date, days with data
  meta            asof, built_at, user, location
`asof` defaults to yesterday (the last complete day). Nothing after asof is kept.
"""
import argparse
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

DAILY_COLUMNS = {  # column -> (unit, source description)
    "steps": ("steps", "Google Health API daily total (watch + phone merged by the API)"),
    "steps_watch": ("steps", "Fitbit Edge intraday steps, summed"),
    "steps_phone": ("steps", "iPhone (Apple Health) intraday steps, summed"),
    "distance_km": ("km", "Google Health API daily distance"),
    "floors": ("floors", "Google Health API daily floors"),
    "active_kcal": ("kcal", "Active energy burned"),
    "total_kcal": ("kcal", "Total calories burned (Fitbit)"),
    "azm_total": ("minutes", "Active Zone Minutes (fat burn + cardio + peak)"),
    "active_minutes": ("minutes", "Active minutes, all activity levels"),
    "sedentary_minutes": ("minutes", "Sedentary time"),
    "hr_avg": ("bpm", "Average heart rate over the day"),
    "hr_max": ("bpm", "Max heart rate"),
    "resting_hr": ("bpm", "Daily resting heart rate"),
    "hrv_ms": ("ms", "Average heart rate variability (RMSSD) during sleep"),
    "spo2_avg": ("%", "Average blood oxygen during sleep"),
    "resp_rate": ("breaths/min", "Respiratory rate during sleep"),
    "skin_temp_dev_c": ("°C", "Nightly skin temperature minus personal baseline"),
    "vo2max": ("ml/kg/min", "Estimated VO2 max (cardio fitness)"),
    "sleep_minutes": ("minutes", "Minutes asleep in main (non-nap) sleep ending this date"),
    "sleep_deep": ("minutes", "Deep sleep minutes"),
    "sleep_rem": ("minutes", "REM sleep minutes"),
    "sleep_light": ("minutes", "Light sleep minutes"),
    "sleep_awake": ("minutes", "Minutes awake during main sleep"),
    "sleep_efficiency": ("%", "Minutes asleep / time in bed for main sleep"),
    "nap_minutes": ("minutes", "Minutes asleep in naps"),
    "exercise_sessions": ("sessions", "Number of logged exercise sessions"),
    "temp_mean_c": ("°C", "Open-Meteo daily mean temperature, NYC"),
    "temp_max_c": ("°C", "Open-Meteo daily max temperature, NYC"),
    "precip_mm": ("mm", "Open-Meteo daily precipitation, NYC"),
    "daylight_hours": ("hours", "Open-Meteo daylight duration, NYC"),
}


def num(x):
    if x is None:
        return None
    if isinstance(x, str):
        m = re.fullmatch(r"(-?[\d.]+)s?", x)
        return float(m.group(1)) if m else None
    return float(x)


def load_points(folder: Path, type_name: str) -> list[dict]:
    pts = []
    for f in sorted(folder.glob(f"{type_name}__*.json")):
        pts += json.loads(f.read_text()).get("dataPoints", [])
    return pts


def by_date(points: list[dict]) -> dict[str, dict]:
    out = {}
    for p in points:  # later chunk files win on overlap
        out[p["date"]] = p
    return out


def dedupe_by_id(points: list[dict]) -> list[dict]:
    return list({p["id"]: p for p in points}.values())


def build(raw: Path, asof: str, out_root: Path) -> Path:
    d, r = raw / "daily", raw / "records"
    rows: dict[str, dict] = defaultdict(dict)

    def put(day: str, col: str, val):
        if day <= asof and val is not None:
            rows[day][col] = val

    for day, p in by_date(load_points(d, "steps")).items():
        put(day, "steps", num(p.get("countSum")))
    for day, p in by_date(load_points(d, "distance")).items():
        put(day, "distance_km", round(num(p.get("millimetersSum")) / 1e6, 3))
    for day, p in by_date(load_points(d, "floors")).items():
        put(day, "floors", num(p.get("countSum")))
    for day, p in by_date(load_points(d, "active-energy-burned")).items():
        put(day, "active_kcal", round(num(p.get("kcalSum")), 1))
    for day, p in by_date(load_points(d, "total-calories")).items():
        put(day, "total_kcal", round(num(p.get("kcalSum")), 1))
    for day, p in by_date(load_points(d, "active-zone-minutes")).items():
        put(day, "azm_total", sum(num(p.get(k)) or 0 for k in
                                  ("sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone")))
    for day, p in by_date(load_points(d, "active-minutes")).items():
        put(day, "active_minutes", sum(num(x.get("activeMinutesSum")) or 0
                                       for x in p.get("activeMinutesRollupByActivityLevel", [])))
    for day, p in by_date(load_points(d, "sedentary-period")).items():
        put(day, "sedentary_minutes", round(num(p.get("durationSum")) / 60))
    for day, p in by_date(load_points(d, "heart-rate")).items():
        put(day, "hr_avg", round(num(p["beatsPerMinuteAvg"]), 1))
        put(day, "hr_max", num(p.get("beatsPerMinuteMax")))

    for day, p in by_date(load_points(r, "daily-resting-heart-rate")).items():
        put(day, "resting_hr", num(p.get("beatsPerMinute")))
    for day, p in by_date(load_points(r, "daily-heart-rate-variability")).items():
        put(day, "hrv_ms", round(num(p["averageHeartRateVariabilityMilliseconds"]), 1))
    for day, p in by_date(load_points(r, "daily-oxygen-saturation")).items():
        put(day, "spo2_avg", num(p.get("averagePercentage")))
    for day, p in by_date(load_points(r, "daily-respiratory-rate")).items():
        put(day, "resp_rate", num(p.get("breathsPerMinute")))
    for day, p in by_date(load_points(r, "daily-sleep-temperature-derivations")).items():
        night, base = num(p.get("nightlyTemperatureCelsius")), num(p.get("baselineTemperatureCelsius"))
        if night is not None and base is not None:
            put(day, "skin_temp_dev_c", round(night - base, 2))
    for day, p in by_date(load_points(r, "daily-vo2-max")).items():
        put(day, "vo2max", round(num(p["vo2Max"]), 1))

    # Per-source intraday steps -> watch vs phone, to explain disagreement with the merged API total.
    for f in sorted((raw / "intraday" / "steps").glob("*.json")):
        day = f.stem
        per = defaultdict(float)
        for p in json.loads(f.read_text()).get("dataPoints", []):
            per[p.get("source", "")] += num(p.get("count")) or 0
        put(day, "steps_watch", per.get("Fitbit Edge"))
        put(day, "steps_phone", per.get("com.apple.health"))

    # Sleep sessions: dated by the local date they END (Fitbit convention).
    sleep = []
    for p in dedupe_by_id(load_points(r, "sleep")):
        st = p.get("stageMinutes", {})
        day = p["end"][:10]
        if day > asof:
            continue
        total = p.get("totalMinutes") or 0
        sleep.append({
            "id": p["id"], "date": day, "start": p["start"], "end": p["end"],
            "is_nap": bool(p.get("isNap", False)), "minutes_asleep": p.get("minutesAsleep"),
            "minutes_awake": p.get("minutesAwake"), "total_minutes": total,
            "deep": st.get("DEEP"), "rem": st.get("REM"), "light": st.get("LIGHT"),
            "efficiency": round(100 * p["minutesAsleep"] / total, 1) if total else None,
            "source": p.get("source"),
        })
    for day in {s["date"] for s in sleep}:
        main = [s for s in sleep if s["date"] == day and not s["is_nap"]]
        naps = [s for s in sleep if s["date"] == day and s["is_nap"]]
        if main:
            asleep = sum(s["minutes_asleep"] or 0 for s in main)
            in_bed = sum(s["total_minutes"] or 0 for s in main)
            put(day, "sleep_minutes", asleep)
            for col, key in (("sleep_deep", "deep"), ("sleep_rem", "rem"), ("sleep_light", "light")):
                put(day, col, sum(s[key] or 0 for s in main))
            put(day, "sleep_awake", sum(s["minutes_awake"] or 0 for s in main))
            put(day, "sleep_efficiency", round(100 * asleep / in_bed, 1) if in_bed else None)
        if naps:
            put(day, "nap_minutes", sum(s["minutes_asleep"] or 0 for s in naps))

    exercise = []
    for p in dedupe_by_id(load_points(r, "exercise")):
        day = p["start"][:10]
        if day > asof:
            continue
        m = p.get("metricsSummary", {})
        exercise.append({
            "id": p["id"], "date": day, "start": p["start"], "end": p["end"],
            "type": p.get("exerciseType"), "name": p.get("displayName"),
            "active_minutes": round((num(p.get("activeDuration")) or 0) / 60, 1),
            "steps": num(m.get("steps")), "distance_km": round((m.get("distanceMillimeters") or 0) / 1e6, 3),
            "kcal": m.get("caloriesKcal"), "avg_hr": num(m.get("averageHeartRateBeatsPerMinute")),
            "azm": num(m.get("activeZoneMinutes")), "source": p.get("source"),
        })
    for day in {e["date"] for e in exercise}:
        put(day, "exercise_sessions", sum(1 for e in exercise if e["date"] == day))

    weather_file = raw / "weather.json"
    location = None
    if weather_file.exists():
        w = json.loads(weather_file.read_text())
        location = w["location"]["label"]
        for day, v in w["days"].items():
            put(day, "temp_mean_c", v["temperature_2m_mean"])
            put(day, "temp_max_c", v["temperature_2m_max"])
            put(day, "precip_mm", v["precipitation_sum"])
            put(day, "daylight_hours", round(v["daylight_duration"] / 3600, 2) if v["daylight_duration"] else None)

    # Dense date spine from first data day to asof, so missing days are explicit NULLs.
    first = min(rows)
    spine, cur = [], date.fromisoformat(first)
    while cur.isoformat() <= asof:
        spine.append(cur.isoformat())
        cur += timedelta(days=1)

    out_dir = out_root / asof
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "pulse.duckdb"
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    cols = list(DAILY_COLUMNS)
    con.execute("CREATE TABLE daily (date DATE PRIMARY KEY, weekday VARCHAR, is_weekend BOOLEAN, "
                + ", ".join(f"{c} DOUBLE" for c in cols) + ")")
    con.executemany(
        f"INSERT INTO daily VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
        [[day, date.fromisoformat(day).strftime("%A"), date.fromisoformat(day).weekday() >= 5]
         + [rows.get(day, {}).get(c) for c in cols] for day in spine],
    )
    con.execute("""CREATE TABLE sleep_sessions (id VARCHAR, date DATE, start TIMESTAMPTZ, "end" TIMESTAMPTZ,
        is_nap BOOLEAN, minutes_asleep DOUBLE, minutes_awake DOUBLE, total_minutes DOUBLE,
        deep DOUBLE, rem DOUBLE, light DOUBLE, efficiency DOUBLE, source VARCHAR)""")
    if sleep:
        con.executemany("INSERT INTO sleep_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [list(s.values()) for s in sleep])
    con.execute("""CREATE TABLE exercise (id VARCHAR, date DATE, start TIMESTAMPTZ, "end" TIMESTAMPTZ,
        type VARCHAR, name VARCHAR, active_minutes DOUBLE, steps DOUBLE, distance_km DOUBLE,
        kcal DOUBLE, avg_hr DOUBLE, azm DOUBLE, source VARCHAR)""")
    if exercise:
        con.executemany("INSERT INTO exercise VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [list(e.values()) for e in exercise])

    con.execute("CREATE TABLE coverage (metric VARCHAR, unit VARCHAR, source VARCHAR, "
                "first_date DATE, last_date DATE, days_with_data INTEGER)")
    for c, (unit, src) in DAILY_COLUMNS.items():
        first_d, last_d, n = con.execute(
            f"SELECT min(date), max(date), count({c}) FROM daily WHERE {c} IS NOT NULL").fetchone()
        con.execute("INSERT INTO coverage VALUES (?,?,?,?,?,?)", [c, unit, src, first_d, last_d, n or 0])

    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ["asof", asof], ["first_date", first],
        ["built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["user", "demo"], ["location", location or "unknown"],
    ])
    con.close()
    return db_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--asof", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--out", default="data/snapshots")
    a = ap.parse_args()
    path = build(Path(a.raw), a.asof, Path(a.out))
    con = duckdb.connect(str(path), read_only=True)
    print(f"built {path} ({path.stat().st_size / 1e6:.1f} MB)")
    print(con.sql("SELECT metric, first_date, last_date, days_with_data FROM coverage ORDER BY days_with_data DESC"))
