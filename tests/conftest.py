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
        row["temp_mean_c"] = float(i)             # perfectly increasing -> rho = 1
        row["precip_mm"] = 5.0 if i % 2 else 0.0  # odd days rainy
        if i >= 14:                               # last 14 days: watch era
            row["sleep_minutes"] = 400.0 if i % 2 else 440.0
            row["azm_total"] = 30.0
        con.execute(f"INSERT INTO daily VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
                    [d, d.strftime("%A"), d.weekday() >= 5, *row.values()])
    con.execute("CREATE TABLE sleep_sessions (id VARCHAR, date DATE, start TIMESTAMPTZ, \"end\" TIMESTAMPTZ, "
                "is_nap BOOLEAN, minutes_asleep DOUBLE, minutes_awake DOUBLE, total_minutes DOUBLE, deep DOUBLE, "
                "rem DOUBLE, light DOUBLE, efficiency DOUBLE, source VARCHAR)")
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
