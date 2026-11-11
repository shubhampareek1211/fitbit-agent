"""A tiny synthetic snapshot with known answers, so tool math can be checked exactly."""
from datetime import date, timedelta

import duckdb
import pytest

START = date(2026, 8, 3)  # a Monday
DAYS = 28


@pytest.fixture(scope="session", autouse=True)
def fixture_snapshot(tmp_path_factory):
    path = tmp_path_factory.mktemp("snap") / "pulse.duckdb"
    con = duckdb.connect(str(path))
    from build.normalize import DAILY_COLUMNS
    cols = list(DAILY_COLUMNS)
    con.execute("CREATE TABLE daily (date DATE, weekday VARCHAR, is_weekend BOOLEAN, "
                + ", ".join(f"{c} DOUBLE" for c in cols) + ")")
    for i in range(DAYS):
        d = START + timedelta(days=i)
        row = {c: None for c in cols}
        row["steps"] = 1000.0 * (i + 1)          # 1000, 2000, ..., 28000 (perfectly increasing)
        row["steps_phone"] = 800.0 * (i + 1)      # the phone alone counts 80% of that
        row["temp_mean_c"] = float(i)             # perfectly increasing -> rho = 1
        row["precip_mm"] = 5.0 if i % 2 else 0.0  # odd days rainy
        if i >= 14:                               # last 14 days: watch era
            row["sleep_minutes"] = 400.0 if i % 2 else 440.0
            row["azm_total"] = 30.0
        con.execute(f"INSERT INTO daily VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
                    [d, d.strftime("%A"), d.weekday() >= 5, *row.values()])
    from build.normalize import EXERCISE_SQL, SLEEP_SQL, WORKOUT_HR_SQL
    con.execute(SLEEP_SQL)
    # Four workouts: a 3-workout visit on 08-29 (walk, run, elliptical, 5 minutes apart) and a walk on 08-30.
    con.execute(EXERCISE_SQL)
    for i, (day, t0, t1, code, minutes, hr, kcal, zones, started, visit) in enumerate([
            ("2026-08-29", "18:00", "18:20", "WALKING", 20, 110, 100, (10, 10, 0, 0), False, 1),
            ("2026-08-29", "18:25", "18:45", "RUNNING", 20, 165, 250, (0, 0, 15, 5), True, 1),
            ("2026-08-29", "18:50", "19:00", "ELLIPTICAL", 10, 180, 150, (0, 0, 0, 10), True, 1),
            ("2026-08-30", "07:00", "07:30", "WALKING", 30, 95, 120, (30, 0, 0, 0), False, 2)]):
        con.execute(f"INSERT INTO exercise VALUES ({', '.join('?' * 21)})",
                    [f"w{i}", day, f"{day}T{t0}:00-04:00", f"{day}T{t1}:00-04:00", f"{day} {t0}:00", f"{day} {t1}:00",
                     code, code.title(), minutes, None, None, kcal, hr, None, *zones, started, visit, "test"])
    # Visit 1 heart rate in 10-second steps: walk 110; run 160 then 170 (median 165); elliptical 180; breaks 130.
    con.execute(WORKOUT_HR_SQL)
    con.executemany("INSERT INTO workout_hr VALUES (1, ?, ?)",
                    [[o, 110.0 if o < 1200 else 160.0 if 1500 <= o < 2100 else 170.0 if 1500 <= o < 2700
                      else 180.0 if o >= 3000 else 130.0] for o in range(0, 3600, 10)])
    con.execute("CREATE TABLE coverage (metric VARCHAR, unit VARCHAR, source VARCHAR, first_date DATE, "
                "last_date DATE, days_with_data INTEGER)")
    end = START + timedelta(days=DAYS - 1)
    con.executemany("INSERT INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
                    [["steps", "steps", "test", START, end, DAYS],
                     ["sleep_minutes", "minutes", "test", START + timedelta(days=14), end, 14],
                     ["azm_total", "minutes", "test", START + timedelta(days=14), end, 14]])
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [["asof", end.isoformat()], ["first_date", START.isoformat()],
                                                       ["location", "test"], ["user", "test"]])
    con.close()
    mp = pytest.MonkeyPatch()
    mp.setenv("PULSE_SNAPSHOT", str(path))
    yield path
    mp.undo()
