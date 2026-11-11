"""Read-only access to the snapshot and the metric catalog."""
from datetime import date
from functools import lru_cache
from pathlib import Path

import duckdb
import yaml

from pulse import config


@lru_cache(maxsize=4)
def _connect(path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path, read_only=True,
                          config={"enable_external_access": False})


def db() -> duckdb.DuckDBPyConnection:
    """A cursor on the current snapshot (cursors are safe to use from several threads)."""
    return _connect(str(config.latest_snapshot())).cursor()


@lru_cache(maxsize=1)
def catalog() -> dict[str, dict]:
    return yaml.safe_load(Path(config.METRICS_YAML).read_text())["metrics"]


def meta() -> dict[str, str]:
    return dict(db().execute("SELECT key, value FROM meta").fetchall())


def asof() -> str:
    return meta()["asof"]


def through() -> str:
    """The day of the last fetch: sleep sessions and workouts exist up to it; daily metrics stop at asof()."""
    return meta().get("data_through") or asof()


def first_date() -> str:
    return meta()["first_date"]


def daily_columns() -> set[str]:
    cols = {r[0] for r in db().execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'daily'").fetchall()}
    return cols - {"date", "weekday", "is_weekend"}


def parse_day(s: str) -> date:
    return date.fromisoformat(str(s).strip()[:10])
