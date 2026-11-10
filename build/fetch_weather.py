"""Fetch daily weather for the data window from Open-Meteo (no API key).

Usage: uv run build/fetch_weather.py START END [OUT_JSON]
Uses the archive API for older days and the forecast API (past days) for the
last week, since the archive lags by a few days.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

# Morningside Heights, NYC (Columbia). Location is an assumption; see README.
LAT, LON, TZ = 40.8075, -73.9626, "America/New_York"
DAILY = [
    "temperature_2m_max", "temperature_2m_min", "temperature_2m_mean",
    "precipitation_sum", "rain_sum", "snowfall_sum", "daylight_duration",
]
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_LAG_DAYS = 6


def fetch(url: str, start: date, end: date) -> dict[str, dict]:
    if start > end:
        return {}
    params = {
        "latitude": LAT, "longitude": LON, "timezone": TZ,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": ",".join(DAILY),
    }
    r = httpx.get(url, params=params, timeout=60)
    r.raise_for_status()
    d = r.json()["daily"]
    return {
        day: {k: d[k][i] for k in DAILY}
        for i, day in enumerate(d["time"])
    }


def main(start: str, end: str, out: str = "data/raw/weather.json") -> None:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    split = min(e, date.today() - timedelta(days=ARCHIVE_LAG_DAYS))
    days = fetch(ARCHIVE, s, split)
    recent = fetch(FORECAST, max(s, split + timedelta(days=1)), e)
    days.update({k: v for k, v in recent.items() if v["temperature_2m_mean"] is not None})
    payload = {"location": {"lat": LAT, "lon": LON, "tz": TZ, "label": "New York, NY (Columbia)"},
               "source": "https://open-meteo.com/", "days": days}
    Path(out).write_text(json.dumps(payload, indent=1))
    missing = [d for d, v in days.items() if v["temperature_2m_mean"] is None]
    print(f"weather: {len(days)} days -> {out}; missing temp: {len(missing)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
