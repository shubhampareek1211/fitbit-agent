"""Offline build step: raw Google Health API JSON (+ weather) -> one DuckDB snapshot.

Usage: uv run build/normalize.py [--raw data/raw] [--asof YYYY-MM-DD] [--out data/snapshots]

Produces data/snapshots/<asof>/pulse.duckdb with tables:
  daily           one row per date (activity, heart, sleep summary, weather)
  sleep_sessions  one row per sleep session (stages, nap flag)
  exercise        one row per workout (type, heart-rate zone minutes, started on the watch or auto-detected, visit)
  workout_hr      heart rate through each workout visit, averaged into 10-second steps
  coverage        per metric: source, first/last date, days with data
  meta            asof, built_at, user, location
Times are stored twice: as TIMESTAMPTZ and as local wall-clock time (*_local). The app reads the local columns, because
its read-only DuckDB connection can't load the time-zone extension and would show TIMESTAMPTZ values in UTC.
`asof` defaults to yesterday (the last complete day). The daily table keeps nothing after asof; sessions (sleep, workouts) are kept up to --through, the fetch day.
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


VISIT_GAP_MIN = 10  # workouts less than this many minutes apart form one visit (e.g. one trip to the gym)
HR_STEP_S = 10      # intraday heart rate (a reading every ~2 s) is averaged into steps of this many seconds
ZONES = ("light", "moderate", "vigorous", "peak")

SLEEP_SQL = """CREATE TABLE sleep_sessions (id VARCHAR, date DATE, start TIMESTAMPTZ, "end" TIMESTAMPTZ,
    start_local TIMESTAMP, end_local TIMESTAMP, is_nap BOOLEAN, minutes_asleep DOUBLE, minutes_awake DOUBLE,
    total_minutes DOUBLE, deep DOUBLE, rem DOUBLE, light DOUBLE, efficiency DOUBLE, source VARCHAR)"""
EXERCISE_SQL = """CREATE TABLE exercise (id VARCHAR, date DATE, start TIMESTAMPTZ, "end" TIMESTAMPTZ,
    start_local TIMESTAMP, end_local TIMESTAMP, type VARCHAR, name VARCHAR, active_minutes DOUBLE, steps DOUBLE,
    distance_km DOUBLE, kcal DOUBLE, avg_hr DOUBLE, azm DOUBLE, light_min DOUBLE, moderate_min DOUBLE,
    vigorous_min DOUBLE, peak_min DOUBLE, started_on_watch BOOLEAN, visit INTEGER, source VARCHAR)"""
WORKOUT_HR_SQL = "CREATE TABLE workout_hr (visit INTEGER, offset_s INTEGER, bpm DOUBLE)"


def local(ts: str) -> str:
    """Wall-clock time of an ISO timestamp: the raw data carries each reading's local UTC offset."""
    return datetime.fromisoformat(ts).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def assign_visits(workouts: list[dict], gap_min: int = VISIT_GAP_MIN) -> None:
    """Number workouts by visit: one that starts within gap_min of the previous one's end joins its visit."""
    visit, last_end = 0, None
    for w in sorted(workouts, key=lambda w: datetime.fromisoformat(w["start"])):
        start, end = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        if last_end is None or (start - last_end).total_seconds() > gap_min * 60:
            visit, last_end = visit + 1, end
        else:
            last_end = max(last_end, end)
        w["visit"] = visit


def heart_rate_steps(readings: list[tuple[datetime, float]], start: datetime, end: datetime,
                     step_s: int = HR_STEP_S) -> list[tuple[int, float]]:
    """Average the readings between start and end into step_s-second steps: [(offset_s, bpm)]."""
    bins: dict[int, list[float]] = defaultdict(list)
    for t, bpm in readings:
        if start <= t <= end:
            bins[int((t - start).total_seconds() // step_s) * step_s].append(bpm)
    return [(k, round(sum(v) / len(v), 1)) for k, v in sorted(bins.items())]


def fill_zero_days(rows: dict[str, dict]) -> None:
    """The API sends no record for a day whose value is zero (no Active Zone Minutes, no floors, no nap, no
    workout), so those days would read as missing. Fill 0 where another reading shows the device was recording:
    heart rate that day for zone minutes and workouts, a main sleep for naps, steps for floors (from the first
    day floors exist). Days with no such reading stay empty: they really are unknown."""
    first_floors = min((d for d, r in rows.items() if "floors" in r), default=None)
    for day, r in rows.items():
        if "hr_avg" in r:
            r.setdefault("azm_total", 0.0)
            r.setdefault("exercise_sessions", 0.0)
        if "sleep_minutes" in r:
            r.setdefault("nap_minutes", 0.0)
        if "steps" in r and first_floors and day >= first_floors:
            r.setdefault("floors", 0.0)


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


def build(raw: Path, asof: str, out_root: Path, through: str | None = None, fetched_at: str | None = None) -> Path:
    """asof = the last complete day: the daily table stops there. through = the day of the fetch (>= asof): sleep
    sessions and workouts are kept up to it, so today's workout shows without a half day skewing the daily averages.
    The snapshot is written to out_root/<through>/."""
    through = max(through or asof, asof)
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
        if day > through:
            continue
        total = p.get("totalMinutes") or 0
        sleep.append({
            "id": p["id"], "date": day, "start": p["start"], "end": p["end"],
            "start_local": local(p["start"]), "end_local": local(p["end"]),
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
        if day > through:
            continue
        m = p.get("metricsSummary", {})
        zones = m.get("heartRateZoneDurations") or {}
        dist = num(m.get("distanceMillimeters"))
        exercise.append({
            "id": p["id"], "date": day, "start": p["start"], "end": p["end"],
            "start_local": local(p["start"]), "end_local": local(p["end"]),
            "type": p.get("exerciseType"), "name": p.get("displayName"),
            "active_minutes": round((num(p.get("activeDuration")) or 0) / 60, 1),
            # Rowing and weights have no distance: keep it unknown, not 0 km.
            "steps": num(m.get("steps")), "distance_km": None if dist is None else round(dist / 1e6, 3),
            "kcal": m.get("caloriesKcal"), "avg_hr": num(m.get("averageHeartRateBeatsPerMinute")),
            "azm": num(m.get("activeZoneMinutes")),
            **{f"{z}_min": None if f"{z}Time" not in zones else round(num(zones[f"{z}Time"]) / 60, 1) for z in ZONES},
            # A START event means the workout was started on the watch; without one, the watch detected it itself.
            "started_on_watch": any(e.get("exerciseEventType") == "START" for e in p.get("exerciseEvents") or []),
            "visit": None, "source": p.get("source"),
        })
    for day in {e["date"] for e in exercise}:
        put(day, "exercise_sessions", sum(1 for e in exercise if e["date"] == day))
    assign_visits(exercise)

    # Heart rate through each visit (first workout's start to last one's end, breaks included), for the workout traces.
    hr_days: dict[str, list[tuple[datetime, float]]] = {}

    def readings(day: str) -> list[tuple[datetime, float]]:
        if day not in hr_days:
            f = raw / "intraday" / "heart-rate" / f"{day}.json"
            pts = json.loads(f.read_text()).get("dataPoints", []) if f.exists() else []
            hr_days[day] = [(datetime.fromisoformat(p["time"]), num(p["beatsPerMinute"])) for p in pts
                            if num(p.get("beatsPerMinute"))]
        return hr_days[day]

    workout_hr = []
    for v in sorted({e["visit"] for e in exercise}):
        ws = [e for e in exercise if e["visit"] == v]
        start = min(datetime.fromisoformat(e["start"]) for e in ws)
        end = max(datetime.fromisoformat(e["end"]) for e in ws)
        days = {local(e["start"])[:10] for e in ws} | {local(e["end"])[:10] for e in ws}
        workout_hr += [(v, off, bpm) for off, bpm in
                       heart_rate_steps([r for d in sorted(days) for r in readings(d)], start, end)]

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

    fill_zero_days(rows)

    # Dense date spine from first data day to asof, so missing days are explicit NULLs.
    first = min(rows)
    spine, cur = [], date.fromisoformat(first)
    while cur.isoformat() <= asof:
        spine.append(cur.isoformat())
        cur += timedelta(days=1)

    out_dir = out_root / through
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
    con.execute(SLEEP_SQL)
    if sleep:
        con.executemany(f"INSERT INTO sleep_sessions VALUES ({', '.join('?' * len(sleep[0]))})",
                        [list(s.values()) for s in sleep])
    con.execute(EXERCISE_SQL)
    if exercise:
        con.executemany(f"INSERT INTO exercise VALUES ({', '.join('?' * len(exercise[0]))})",
                        [list(e.values()) for e in exercise])
    con.execute(WORKOUT_HR_SQL)
    if workout_hr:
        con.executemany("INSERT INTO workout_hr VALUES (?, ?, ?)", workout_hr)

    con.execute("CREATE TABLE coverage (metric VARCHAR, unit VARCHAR, source VARCHAR, "
                "first_date DATE, last_date DATE, days_with_data INTEGER)")
    for c, (unit, src) in DAILY_COLUMNS.items():
        first_d, last_d, n = con.execute(
            f"SELECT min(date), max(date), count({c}) FROM daily WHERE {c} IS NOT NULL").fetchone()
        con.execute("INSERT INTO coverage VALUES (?,?,?,?,?,?)", [c, unit, src, first_d, last_d, n or 0])

    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ["asof", asof], ["first_date", first], ["data_through", through],
        ["fetched_at", fetched_at or ""],
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
    ap.add_argument("--through", default=None, help="day of the fetch; sessions are kept up to it")
    a = ap.parse_args()
    path = build(Path(a.raw), a.asof, Path(a.out), a.through)
    con = duckdb.connect(str(path), read_only=True)
    print(f"built {path} ({path.stat().st_size / 1e6:.1f} MB)")
    print(con.sql("SELECT metric, first_date, last_date, days_with_data FROM coverage ORDER BY days_with_data DESC"))
