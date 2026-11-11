"""Deterministic tools shared by the agents, the MCP server and the eval gold.

Every tool records the numbers it returns in the per-request EvidenceLedger and returns
the evidence id, so the Writer can cite it and the Verifier can check it.
Dates are YYYY-MM-DD strings, clamped to the snapshot's coverage (first_date .. asof).
"""
from datetime import datetime, timedelta
from functools import wraps

from pulse import stats
from pulse.data import asof, catalog, daily_columns, db, first_date, meta, parse_day, through
from pulse.evidence import ledger

RAIN_MM = 1.0
WATCH_START = "2026-10-31"  # first full day of Fitbit Edge data
WATCH_FIRST_DAY = "2026-10-30"  # first (partial) day with any watch data: watch-only metrics can't exist before it
# Metrics the phone measured alone before WATCH_START and the watch measures too from then on (all jump at that date:
# mean before vs after = steps 4,161 vs 10,197, distance 2.9 vs 7.1 km, floors 6.1 vs 17.8, active energy 146 vs 1,051 kcal).
WATCH_MEASURED = ("steps", "distance_km", "floors", "active_kcal")
GROUPINGS = ("weekday", "weekend", "rain", "temperature_band", "month", "device_era")


class ToolError(ValueError):
    pass


def _r(x, nd=1):
    return None if x is None else round(float(x), nd)


def _range(start: str, end: str, sessions: bool = False) -> tuple[str, str, str | None]:
    """Clamp a requested range to coverage. Returns (start, end, note). Daily metrics end at asof (the last complete
    day); sessions=True (sleep sessions, workouts) extends to the day of the last fetch."""
    try:
        s, e = parse_day(start), parse_day(end)
    except ValueError:
        raise ToolError(f"Dates must be YYYY-MM-DD, got {start!r}..{end!r}")
    if s > e:
        s, e = e, s
    lo, hi = parse_day(first_date()), parse_day(through() if sessions else asof())
    if e < lo or s > hi:
        raise ToolError(f"No data for {s}..{e}. Data covers {lo}..{hi} (as of {hi}).")
    note = None
    if s < lo or e > hi:
        note = f"Requested {s}..{e}; clamped to available data {max(s, lo)}..{min(e, hi)}."
    return max(s, lo).isoformat(), min(e, hi).isoformat(), note


def _metric(name: str) -> str:
    name = name.strip()
    if name not in daily_columns():
        raise ToolError(f"Unknown metric {name!r}. Use list_metrics to see valid names.")
    return name


def _series(metric: str, start: str, end: str) -> list[tuple[str, float]]:
    rows = db().execute(
        f"SELECT date::VARCHAR, {metric} FROM daily WHERE date BETWEEN ? AND ? AND {metric} IS NOT NULL ORDER BY date",
        [start, end]).fetchall()
    return [(d, float(v)) for d, v in rows]


def _measurement_note(metric: str, start: str, end: str, group_by: str | None = None) -> str | None:
    """Hill-climb change 2: steps before the watch arrived are phone-only, so raw steps jump on 2026-10-31.
    Change 5: the note counts the days on each side, so the Lead can judge on review whether the like-for-like
    check is a gap worth a follow-up round."""
    # Any range touching the watch era gets the note, including one that lies wholly after WATCH_START: a trace
    # showed the Lead planning one subtask per era, so no single result crossed the date and no caveat fired.
    if metric not in WATCH_MEASURED or end < WATCH_START:
        return None
    watch = parse_day(WATCH_START)
    before = max(0, (min(parse_day(end) + timedelta(days=1), watch) - parse_day(start)).days)
    after = (parse_day(end) - max(parse_day(start), watch)).days + 1
    span = (f"this range mixes {before} phone-only day(s) (before {WATCH_START}) with {after} day(s) when the watch "
            f"also measured {metric}" if before else
            f"all {after} day(s) in this range are from {WATCH_START} on, when the watch also measured {metric}, so "
            f"they can't be compared directly with the phone-only days before {WATCH_START}")
    if metric == "steps":
        return (f"MEASUREMENT CAVEAT: {span}. Raw 'steps' rises at {WATCH_START} partly because more steps are "
                f"measured, not because behaviour changed. To compare days before and after {WATCH_START}, repeat "
                f"the analysis with metric='steps_phone' (the phone counted steps on every day); for a before/after "
                f"change use compare_groups(metric='steps_phone', group_by='device_era') over both eras.")
    return (f"MEASUREMENT CAVEAT: {span}. {metric} jumps at {WATCH_START} for measurement reasons, not behaviour "
            f"(the watch measures it differently from the phone), and there is no phone-only version: never present "
            f"its change across {WATCH_START} as real. For a like-for-like activity comparison use steps_phone; "
            f"otherwise compare within one era.")


def _recorded_from(metric: str) -> str | None:
    row = db().execute("SELECT first_date::VARCHAR FROM coverage WHERE metric = ?", [metric]).fetchone()
    return row[0] if row else None


def _before_watch(metric: str, start: str, end: str) -> tuple[int, str | None]:
    """Change 6: days in start..end before the watch came into use, for metrics only the watch records.
    A trace called these days "missing" and guessed the watch was off; in fact the watch wasn't in use yet."""
    first = _recorded_from(metric)
    if not first or first < WATCH_FIRST_DAY or start >= WATCH_FIRST_DAY:
        return 0, None
    last = min(parse_day(WATCH_FIRST_DAY) - timedelta(days=1), parse_day(end))
    days = (last - parse_day(start)).days + 1
    return days, (f"{metric} comes from the Fitbit watch, whose data starts on {WATCH_FIRST_DAY} (first {metric} "
                  f"value: {first}). The {days} day(s) from {start} to {last} are before the watch: not recorded "
                  f"yet. That is not missing data, and not a watch that was off or unworn.")


def _no_data(metric: str, start: str, end: str) -> str:
    return " ".join(x for x in (f"No {metric} data between {start} and {end}.", _before_watch(metric, start, end)[1])
                    if x)


def _today_note(end: str) -> str | None:
    """Sessions on a day after asof come from a day still in progress when the data was fetched."""
    if end <= asof():
        return None
    at = meta().get("fetched_at") or "the last fetch"
    return (f"{through()} was still in progress when the data was fetched ({at}): sessions later that day are not in "
            f"yet, and daily totals (steps, sleep minutes, zone minutes) exist only through {asof()}.")


def _wrap(fn):
    """Turn ToolErrors into a structured error the model can read (and admit), not an exception."""
    @wraps(fn)
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except ToolError as e:
            return {"error": str(e), "as_of": asof()}
    return inner


@_wrap
def get_coverage() -> dict:
    """Which metrics exist, for which dates, and how many days have data. Call this first to know what the data can and cannot answer."""
    rows = db().execute("SELECT metric, unit, source, first_date::VARCHAR, last_date::VARCHAR, days_with_data "
                        "FROM coverage ORDER BY days_with_data DESC").fetchall()
    m = meta()
    n_workouts, w_first, w_last = db().execute(
        "SELECT count(*), min(date)::VARCHAR, max(date)::VARCHAR FROM exercise").fetchone()
    n_sleeps, s_first, s_last = db().execute(
        "SELECT count(*), min(date)::VARCHAR, max(date)::VARCHAR FROM sleep_sessions").fetchone()
    eid = ledger().add(tool="get_coverage", args={}, table="coverage", date_range=(m["first_date"], m["asof"]),
                       n=None, values={f"{r[0]}_days": float(r[5]) for r in rows} | {"workouts": float(n_workouts),
                                                                               "sleep_sessions": float(n_sleeps)})
    return {"evidence_id": eid, "as_of": m["asof"], "first_date": m["first_date"], "location": m["location"],
            "metrics": [{"metric": r[0], "unit": r[1], "source": r[2], "first_date": r[3], "last_date": r[4],
                         "days_with_data": r[5]} for r in rows],
            "workouts": {"count": n_workouts, "first_date": w_first, "last_date": w_last,
                         "detail": "per workout: type, minutes, heart-rate zone minutes, average heart rate, calories, "
                                   "gym visits, and heart rate through a workout (tools: workouts, workout_heart_rate)"},
            # A live answer refused "what time did I go to bed?": nothing here said the sessions carry clock times.
            "sleep_sessions": {"count": n_sleeps, "first_date": s_first, "last_date": s_last,
                               "detail": "per sleep session: local bedtime (start) and wake time (end), minutes asleep "
                                         "and awake, deep / REM / light minutes, efficiency, naps (tool: sleep_sessions)"},
            "watch_note": (f"Metrics whose first_date is {WATCH_FIRST_DAY} or later come from the Fitbit watch. Its data "
                           f"starts on {WATCH_FIRST_DAY}, a partial first day (heart rate, no watch steps); its first "
                           f"full day is {WATCH_START}. Days before {WATCH_FIRST_DAY} are 'before the watch' (not "
                           f"recorded yet), not missing data. Split before/after-watch comparisons at {WATCH_START}: "
                           f"steps before that date are phone-only."),
            "not_available": ["blood glucose", "blood pressure", "diet quality/macros (only a few food logs)",
                              "medical diagnoses", "any data before " + m["first_date"]]}


@_wrap
def list_metrics() -> dict:
    """Describe every metric: unit, meaning, caveats and any public reference range."""
    return {"as_of": asof(), "metrics": catalog()}


@_wrap
def get_daily(metrics: list[str], start: str, end: str) -> dict:
    """Daily values for up to 6 metrics between start and end (inclusive). Days without a value are null (for watch metrics, every day before the watch came into use)."""
    cols = [_metric(m) for m in metrics][:6]
    if not cols:
        raise ToolError("Pass at least one metric.")
    start, end, note = _range(start, end)
    rows = db().execute(f"SELECT date::VARCHAR, weekday, {', '.join(cols)} FROM daily "
                        f"WHERE date BETWEEN ? AND ? ORDER BY date", [start, end]).fetchall()
    rows = rows[:120]
    values = {f"{c}@{r[0]}": float(r[2 + i]) for r in rows for i, c in enumerate(cols) if r[2 + i] is not None}
    eid = ledger().add(tool="get_daily", args={"metrics": cols, "start": start, "end": end}, table="daily",
                       date_range=(start, end), n=len(rows), values=values)
    watch_cols = [c for c in cols if _before_watch(c, start, end)[0]]
    rnote = (f"{', '.join(watch_cols)} come(s) from the Fitbit watch, whose data starts on {WATCH_FIRST_DAY}: "
             f"nulls before that date are 'before the watch' (not recorded yet), not missing data.") if watch_cols else None
    return {"evidence_id": eid, "note": note, **({"recording_note": rnote} if rnote else {}),
            "columns": ["date", "weekday", *cols], "rows": [[r[0], r[1], *[_r(v) for v in r[2:]]] for r in rows]}


@_wrap
def summarize_metric(metric: str, start: str, end: str) -> dict:
    """Mean, median, min/max (with dates), total and day counts for one metric over a date range."""
    metric = _metric(metric)
    start, end, note = _range(start, end)
    s = _series(metric, start, end)
    span = (parse_day(end) - parse_day(start)).days + 1
    if not s:
        raise ToolError(_no_data(metric, start, end))
    pre, rnote = _before_watch(metric, start, end)
    vals = [v for _, v in s]
    lo, hi = min(s, key=lambda t: t[1]), max(s, key=lambda t: t[1])
    vals_sorted = sorted(vals)
    mid = len(vals_sorted) // 2
    median = vals_sorted[mid] if len(vals) % 2 else (vals_sorted[mid - 1] + vals_sorted[mid]) / 2
    out = {"metric": metric, "unit": catalog().get(metric, {}).get("unit"), "start": start, "end": end,
           "mean": _r(sum(vals) / len(vals)), "median": _r(median), "min": _r(lo[1]), "min_date": lo[0],
           "max": _r(hi[1]), "max_date": hi[0], "total": _r(sum(vals)), "days_with_data": len(vals),
           **({"days_before_watch": pre} if pre else {}), "days_missing": span - pre - len(vals)}
    eid = ledger().add(tool="summarize_metric", args={"metric": metric, "start": start, "end": end},
                       table="daily", date_range=(start, end), n=len(vals),
                       values={k: out[k] for k in ("mean", "median", "min", "max", "total", "days_with_data",
                                                   "days_before_watch", "days_missing") if k in out})
    return {"evidence_id": eid, "note": note, "measurement_note": _measurement_note(metric, start, end),
            **({"recording_note": rnote} if rnote else {}), **out}


@_wrap
def compare_periods(metric: str, a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    """Compare a metric's daily mean in period B against period A (baseline): difference, % change, 95% CI and verdict."""
    metric = _metric(metric)
    a_start, a_end, note_a = _range(a_start, a_end)
    b_start, b_end, note_b = _range(b_start, b_end)
    a = [v for _, v in _series(metric, a_start, a_end)]
    b = [v for _, v in _series(metric, b_start, b_end)]
    if not a or not b:
        why = _before_watch(metric, min(a_start, b_start), max(a_end, b_end))[1]
        raise ToolError(f"No {metric} data in one of the periods ({len(a)} vs {len(b)} days)." + (f" {why}" if why else ""))
    c = stats.compare(a, b)
    out = {"metric": metric, "period_a": [a_start, a_end], "period_b": [b_start, b_end],
           "mean_a": _r(c["mean_a"]), "mean_b": _r(c["mean_b"]), "n_a": c["n_a"], "n_b": c["n_b"],
           "difference": _r(c["diff"]), "pct_change": _r(c["pct_change"]),
           "ci95_difference": [_r(c["ci_low"]), _r(c["ci_high"])], "verdict": c["verdict"]}
    eid = ledger().add(tool="compare_periods", args={"metric": metric, "a": [a_start, a_end], "b": [b_start, b_end]},
                       table="daily", date_range=(min(a_start, b_start), max(a_end, b_end)), n=c["n_a"] + c["n_b"],
                       values={k: out[k] for k in ("mean_a", "mean_b", "n_a", "n_b", "difference", "pct_change")
                               if out[k] is not None},
                       context={"verdict": c["verdict"], "ci95": out["ci95_difference"]})
    return {"evidence_id": eid, "note": " ".join(n for n in (note_a, note_b) if n) or None,
            "measurement_note": _measurement_note(metric, min(a_start, b_start), max(a_end, b_end)), **out}


def _group_key(group_by: str) -> str:
    return {
        "weekday": "weekday",
        "weekend": "CASE WHEN is_weekend THEN 'weekend' ELSE 'weekday' END",
        "rain": f"CASE WHEN precip_mm >= {RAIN_MM} THEN 'rainy (>= {RAIN_MM} mm)' ELSE 'dry' END",
        "temperature_band": "CASE WHEN temp_mean_c < 10 THEN 'cold (<10°C)' WHEN temp_mean_c < 20 "
                            "THEN 'mild (10-20°C)' ELSE 'warm (>=20°C)' END",
        "month": "strftime(date, '%Y-%m')",
        "device_era": f"CASE WHEN date < DATE '{WATCH_START}' THEN 'phone only (before watch)' "
                      f"ELSE 'watch + phone' END",
    }[group_by]


@_wrap
def compare_groups(metric: str, group_by: str, start: str, end: str) -> dict:
    """Mean of a metric per group. group_by: weekday | weekend | rain | temperature_band | month | device_era. With exactly two groups, also returns the difference, 95% CI and verdict."""
    metric = _metric(metric)
    if group_by not in GROUPINGS:
        raise ToolError(f"group_by must be one of {GROUPINGS}")
    start, end, note = _range(start, end)
    rows = db().execute(
        f"SELECT {_group_key(group_by)} AS g, {metric} FROM daily "
        f"WHERE date BETWEEN ? AND ? AND {metric} IS NOT NULL", [start, end]).fetchall()
    if not rows:
        raise ToolError(_no_data(metric, start, end))
    groups: dict[str, list[float]] = {}
    for g, v in rows:
        groups.setdefault(g, []).append(float(v))
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    keys = sorted(groups, key=lambda k: order.index(k) if k in order else k)
    table = [{"group": k, "mean": _r(sum(groups[k]) / len(groups[k])), "n_days": len(groups[k])} for k in keys]
    # Hill-climb change 3: superlatives are computed here, not inferred by the model (a trace showed the Writer
    # calling Friday the least active day for steps, when the evidence said Wednesday).
    hi, lo = max(table, key=lambda t: t["mean"]), min(table, key=lambda t: t["mean"])
    out = {"metric": metric, "group_by": group_by, "start": start, "end": end, "groups": table,
           "highest": {"group": hi["group"], "mean": hi["mean"]}, "lowest": {"group": lo["group"], "mean": lo["mean"]}}
    values = {f"mean[{t['group']}]": t["mean"] for t in table} | {f"n[{t['group']}]": t["n_days"] for t in table}
    if len(keys) == 2:
        c = stats.compare(groups[keys[0]], groups[keys[1]])
        out["comparison"] = {"baseline": keys[0], "other": keys[1], "difference": _r(c["diff"]),
                             "pct_change": _r(c["pct_change"]),
                             "ci95_difference": [_r(c["ci_low"]), _r(c["ci_high"])], "verdict": c["verdict"]}
        values |= {k: v for k, v in (("difference", _r(c["diff"])), ("pct_change", _r(c["pct_change"])))
                   if v is not None}
    eid = ledger().add(tool="compare_groups", args={"metric": metric, "group_by": group_by, "start": start,
                                                    "end": end},
                       table="daily", date_range=(start, end), n=len(rows), values=values,
                       context={"highest": hi["group"], "lowest": lo["group"],
                                **({"verdict": out["comparison"]["verdict"], "ci95": out["comparison"]["ci95_difference"],
                                    "baseline": keys[0], "other": keys[1]} if "comparison" in out else {})})
    return {"evidence_id": eid, "note": note, "measurement_note": _measurement_note(metric, start, end, group_by),
            **out}


@_wrap
def correlate(x: str, y: str, start: str, end: str, lag_days: int = 0) -> dict:
    """Spearman correlation between two daily metrics. lag_days=1 pairs x on day d with y on day d+1 (e.g. activity vs that night's sleep, which is dated the next morning). Fewer than 10 paired days returns insufficient_data."""
    x, y = _metric(x), _metric(y)
    start, end, note = _range(start, end)
    rows = db().execute(
        f"SELECT a.{x}, b.{y} FROM daily a JOIN daily b ON b.date = a.date + {int(lag_days)} "  # date + n days; n may be negative
        f"WHERE a.date BETWEEN ? AND ? AND a.{x} IS NOT NULL AND b.{y} IS NOT NULL", [start, end]).fetchall()
    s = stats.spearman([float(r[0]) for r in rows], [float(r[1]) for r in rows])
    out = {"x": x, "y": y, "lag_days": lag_days, "start": start, "end": end, "n_pairs": s["n"],
           "rho": _r(s["rho"], 2), "ci95": [_r(s["ci_low"], 2), _r(s["ci_high"], 2)],
           "p_value": _r(s["p_value"], 3), "strength": stats.strength(s["rho"]), "verdict": s["verdict"]}
    eid = ledger().add(tool="correlate", args={"x": x, "y": y, "start": start, "end": end, "lag_days": lag_days},
                       table="daily", date_range=(start, end), n=s["n"],
                       values={k: v for k, v in (("rho", out["rho"]), ("n_pairs", s["n"])) if v is not None},
                       context={"verdict": s["verdict"], "ci95": out["ci95"]})
    mnote = _measurement_note(x, start, end) or _measurement_note(y, start, end)
    rnote = _before_watch(x, start, end)[1] or _before_watch(y, start, end)[1]
    return {"evidence_id": eid, "note": note, "measurement_note": mnote,
            **({"recording_note": rnote} if rnote else {}), **out}


@_wrap
def check_guideline(metric: str, start: str, end: str) -> dict:
    """Compare a metric against its public reference (e.g. 7+ h sleep, 150 active minutes/week, 7,000 steps) and report how often it was met. Not a diagnosis."""
    metric = _metric(metric)
    ref = catalog().get(metric, {}).get("reference")
    if not ref:
        with_refs = [k for k, v in catalog().items() if v.get("reference")]
        raise ToolError(f"No public reference for {metric}. Metrics with references: {with_refs}")
    start, end, note = _range(start, end)
    s = _series(metric, start, end)
    if not s:
        raise ToolError(_no_data(metric, start, end))
    pre, rnote = _before_watch(metric, start, end)
    if ref["period"] == "week":
        # Hill-climb change 2b: "per week" = consecutive 7-day blocks counted back from the end date (the
        # guideline says per week, not per Monday-Sunday week). A block counts if it lies fully inside the range
        # and has data on at least 5 of its 7 days.
        end_d, start_d = parse_day(end), parse_day(start)
        blocks: dict[int, list[float]] = {}
        for d, v in s:
            blocks.setdefault((end_d - parse_day(d)).days // 7, []).append(v)
        judged, weeks, skipped = [], [], 0
        for k in sorted(blocks, reverse=True):
            b_end = end_d - timedelta(days=7 * k)
            b_start = b_end - timedelta(days=6)
            vals = blocks[k]
            if b_start >= start_d and len(vals) >= 5:
                label = f"{b_start}..{b_end}"
                judged.append((label, sum(vals)))
                weeks.append({"week": label, "total": _r(sum(vals)), "days_with_data": len(vals)})
            else:
                skipped += 1
        partial_note = f"{skipped} incomplete 7-day block(s) excluded" if skipped else None
    else:
        judged, partial_note, weeks = s, None, None
    if ref["op"] == "between":
        met = [v for _, v in judged if ref["low"] <= v <= ref["high"]]
        target = f"{ref['low']}-{ref['high']}"
    else:
        met = [v for _, v in judged if v >= ref["target"]]
        target = f">= {ref['target']} per {ref['period']}"
    n = len(judged)
    vals = [v for _, v in judged]
    out = {"metric": metric, "start": start, "end": end, "reference": ref["label"], "target": target,
           "period": ref["period"], "periods_judged": n, "periods_meeting": len(met),
           "share_meeting_pct": _r(100 * len(met) / n) if n else None,
           "average_per_period": _r(sum(vals) / n) if n else None, "partial_note": partial_note, "weeks": weeks,
           "source": ref["source_name"], "source_url": ref["source_url"]}
    if n == 0:
        out["verdict"] = "insufficient_data"
    elif n < 3:  # change 4: too few periods to call it a habit (computed, not left to the model)
        out["regularity_note"] = (f"Only {n} {ref['period']}(s) could be judged, too few to say whether this "
                                  f"target is met regularly.")
    # Weekly targets: the evidence names the days the judged blocks cover, not the requested range. A trace
    # attributed the 2026-11-01..07 total to "the week from 2026-10-31 to 2026-11-07" (8 days) because the range
    # was all the Writer saw.
    judged_range = (weeks[0]["week"][:10], weeks[-1]["week"][12:]) if weeks else (start, end)
    if weeks:
        out["judged_range"] = list(judged_range)
        out["days_with_data_in_judged_weeks"] = sum(w["days_with_data"] for w in weeks)
    eid = ledger().add(tool="check_guideline", args={"metric": metric, "start": start, "end": end},
                       table="daily", date_range=judged_range, n=n, source_url=ref["source_url"], n_unit=ref["period"],
                       values={k: out[k] for k in ("periods_judged", "periods_meeting", "share_meeting_pct",
                                                   "average_per_period", "days_with_data_in_judged_weeks")
                               if out.get(k) is not None}
                       | ({f"total[{w['week']}]": w["total"] for w in weeks} if weeks else {})
                       | ({"target": float(ref["target"])} if "target" in ref else
                          {"low": float(ref["low"]), "high": float(ref["high"])})
                       | ({"days_before_watch": float(pre)} if pre else {}),
                       context={"reference": ref["label"], "target_text": target, "period": ref["period"],
                                "op": ref["op"], "weeks": weeks, "source_name": ref["source_name"],
                                "regularity_note": out.get("regularity_note")})
    return {"evidence_id": eid, "note": note, **({"recording_note": rnote, "days_before_watch": pre} if rnote else {}),
            **out}


@_wrap
def sleep_sessions(start: str, end: str, include_naps: bool = True) -> dict:
    """Individual sleep sessions (dated by the morning they end) with stage minutes and efficiency."""
    start, end, note = _range(start, end, sessions=True)
    rows = db().execute(
        "SELECT date::VARCHAR, strftime(start_local, '%Y-%m-%d %H:%M'), strftime(end_local, '%Y-%m-%d %H:%M'), is_nap, "
        "minutes_asleep, deep, rem, light, minutes_awake, efficiency FROM sleep_sessions "
        "WHERE date BETWEEN ? AND ? AND (? OR NOT is_nap) ORDER BY start", [start, end, include_naps]).fetchall()
    cols = ["date", "start", "end", "is_nap", "minutes_asleep", "deep", "rem", "light", "awake", "efficiency"]
    values = {f"{c}@{r[0]}{'(nap)' if r[3] else ''}": float(r[i]) for r in rows
              for i, c in enumerate(cols) if i >= 4 and r[i] is not None}
    eid = ledger().add(tool="sleep_sessions", args={"start": start, "end": end}, table="sleep_sessions",
                       date_range=(start, end), n=len(rows), values=values, n_unit="session")
    tnote = _today_note(end)
    return {"evidence_id": eid, "note": note, **({"today_note": tnote} if tnote else {}), "columns": cols,
            "rows": [list(r) for r in rows]}


# ---------------------------------------------------------------------------------------------- workouts
ZONES = ("light", "moderate", "vigorous", "peak")
TYPE_LABELS = {"WALKING": "Walk", "RUNNING": "Run", "ELLIPTICAL": "Elliptical", "ROWING_MACHINE": "Rowing machine",
               "TREADMILL_WALK": "Treadmill walk", "TREADMILL": "Treadmill", "WEIGHTS": "Weights",
               "CARDIO_WORKOUT": "Cardio workout"}
MAX_WORKOUTS = 60
ZONE_NOTE = ("Zone minutes use Fitbit's own four heart-rate zones (light, moderate, vigorous, peak), which Fitbit "
             "personalises. Workout names such as 'Zone 2' come from a different zone scale, so a name and its zone "
             "minutes need not agree.")
HR_NOTE = ("Heart rate comes from the watch's wrist sensor, which can be less accurate during workouts with strong "
           "arm movement or gripping (elliptical, rowing, weights). Fitbit's average and these readings come from the "
           "same sensor, so one cannot confirm the other.")
_WORKOUT_COLS = ("date::VARCHAR, strftime(start_local, '%Y-%m-%d %H:%M'), strftime(end_local, '%Y-%m-%d %H:%M'), "
                 "type, name, active_minutes, avg_hr, kcal, steps, distance_km, light_min, moderate_min, "
                 "vigorous_min, peak_min, started_on_watch, visit")


def type_label(code: str | None) -> str:
    return TYPE_LABELS.get(code or "", (code or "workout").replace("_", " ").capitalize())


def _workout(r) -> dict:
    w = dict(zip(("date", "start", "end", "type_code", "name", "minutes", "avg_hr", "kcal", "steps", "distance_km",
                  *(f"{z}_min" for z in ZONES), "started_on_watch", "visit"), r))
    w["type"] = type_label(w["type_code"])
    w["key"] = f"{w['start']} {w['type']}"  # e.g. "2026-11-02 19:01 Elliptical": unique, readable, citable
    return w


def _workout_window() -> tuple[str | None, str | None]:
    return db().execute("SELECT min(date)::VARCHAR, max(date)::VARCHAR FROM exercise").fetchone()


@_wrap
def workouts(start: str, end: str, exercise_type: str | None = None) -> dict:
    """Individual workouts the watch recorded (from 2026-10-31 on): local start time, type, minutes, average heart rate, calories, minutes in each of Fitbit's heart-rate zones (light, moderate, vigorous, peak), and whether it was started on the watch or detected automatically. summary.most_intense ranks the hardest workouts (most vigorous + peak minutes): use it for "most intense" questions. Workouts less than 10 minutes apart share a `visit` (e.g. one gym visit). exercise_type filters by type, e.g. WALKING, RUNNING, ELLIPTICAL, ROWING_MACHINE, WEIGHTS."""
    start, end, note = _range(start, end, sessions=True)
    first, last = _workout_window()
    if not first or end < first:
        raise ToolError(f"No workouts between {start} and {end}: the watch records workouts from {first or 'its first day'}"
                        f" on (it was not in use before {WATCH_FIRST_DAY}).")
    sql, params = f"SELECT {_WORKOUT_COLS} FROM exercise WHERE date BETWEEN ? AND ?", [start, end]
    if exercise_type:
        wanted = exercise_type.strip()
        codes = [r[0] for r in db().execute("SELECT DISTINCT type FROM exercise").fetchall()]
        match = [c for c in codes if wanted.upper().replace(" ", "_") == c or wanted.lower() == type_label(c).lower()]
        if not match:
            raise ToolError(f"Unknown exercise_type {exercise_type!r}. Types recorded: {sorted(codes)}")
        sql, params = sql + " AND type = ?", params + [match[0]]
    ws = [_workout(r) for r in db().execute(sql + " ORDER BY start", params).fetchall()]
    if not ws:
        raise ToolError(f"No {exercise_type or ''} workouts between {start} and {end}.".replace("  ", " "))
    zone = {z: _r(sum(w[f"{z}_min"] or 0 for w in ws)) for z in ZONES}
    by_type: dict[str, list[dict]] = {}
    for w in ws:
        by_type.setdefault(w["type"], []).append(w)
    visits = []
    for v in sorted({w["visit"] for w in ws}):
        vw = [w for w in ws if w["visit"] == v]
        if len(vw) > 1:  # back-to-back workouts: one visit (a single workout is just itself)
            vs, ve = parse_dt(vw[0]["start"]), parse_dt(max(w["end"] for w in vw))
            visits.append({"visit": v, "start": vw[0]["start"], "end": max(w["end"] for w in vw)[11:],
                           "minutes": _r((ve - vs).total_seconds() / 60, 0), "workouts": len(vw),
                           "types": [w["type"] for w in vw]})
    started = sum(1 for w in ws if w["started_on_watch"])
    summary = {"workouts": len(ws), "total_minutes": _r(sum(w["minutes"] or 0 for w in ws)),
               **{f"{z}_min": zone[z] for z in ZONES},
               "moderate_or_higher_min": _r(zone["moderate"] + zone["vigorous"] + zone["peak"]),
               "vigorous_or_peak_min": _r(zone["vigorous"] + zone["peak"]),
               "started_on_watch": started, "detected_automatically": len(ws) - started,
               "multi_workout_visits": len(visits)}
    types = {t: {"workouts": len(g), "minutes": _r(sum(w["minutes"] or 0 for w in g)),
                 **{f"{z}_min": _r(sum(w[f"{z}_min"] or 0 for w in g)) for z in ZONES}} for t, g in by_type.items()}
    # Superlatives are computed here, not ranked by a model (as in compare_groups): hardest = most vigorous + peak minutes.
    def hard_min(w):
        return (w["vigorous_min"] or 0) + (w["peak_min"] or 0)
    ranked = sorted((w for w in ws if hard_min(w) > 0), key=lambda w: -hard_min(w))
    # Top three, plus any workout tied with the third: a tie must not be dropped silently.
    hard = [w for w in ranked if hard_min(w) >= hard_min(ranked[min(2, len(ranked) - 1)])][:6] if ranked else []
    summary["most_intense"] = [{"workout": w["key"], "vigorous_or_peak_min": _r((w["vigorous_min"] or 0) +
                                                                               (w["peak_min"] or 0))} for w in hard]
    shown = ws[:MAX_WORKOUTS]
    values = {k: float(v) for k, v in summary.items() if not isinstance(v, list)}
    values |= {f"vigorous_or_peak_min[{h['workout']}]": float(h["vigorous_or_peak_min"]) for h in summary["most_intense"]}
    values |= {f"{k}[{t}]": float(v) for t, d in types.items() for k, v in d.items()}
    values |= {f"{k}[{w['key']}]": float(w[f]) for w in shown
               for k, f in (("minutes", "minutes"), ("avg_hr", "avg_hr"), ("kcal", "kcal"),
                            *((f"{z}_min", f"{z}_min") for z in ZONES)) if w[f] is not None}
    values |= {f"visit_minutes[{v['start']}]": float(v["minutes"]) for v in visits}
    context = {"workouts": [{k: w[k] for k in ("key", "date", "start", "end", "type", "type_code", "name",
                                                "started_on_watch", "visit")} for w in shown],
               "visits": visits, "first_workout_date": first}
    eid = ledger().add(tool="workouts", args={"start": start, "end": end,
                                              **({"exercise_type": exercise_type} if exercise_type else {})},
                       table="exercise", date_range=(start, end), n=len(ws), values=values, context=context,
                       n_unit="workout")
    rnote = (f"Workouts are recorded from {first} on (the watch was not in use before {WATCH_FIRST_DAY}); days "
             f"before that are 'before the watch', not days without exercise.") if start < first else None
    tnote = _today_note(end)
    return {"evidence_id": eid, "note": note, **({"recording_note": rnote} if rnote else {}),
            **({"today_note": tnote} if tnote else {}), "zone_note": ZONE_NOTE, "summary": summary, "by_type": types, "visits": visits,
            "workouts": [{k: w[k] for k in ("key", "type", "name", "end", "minutes", "avg_hr", "kcal", "steps",
                                            "distance_km", *(f"{z}_min" for z in ZONES), "started_on_watch", "visit")}
                         for w in shown],
            **({"truncated": f"showing the first {MAX_WORKOUTS} of {len(ws)} workouts"} if len(ws) > MAX_WORKOUTS
               else {})}


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _median(xs: list[float]) -> float:
    s, m = sorted(xs), len(xs) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


@_wrap
def workout_heart_rate(date: str, time: str) -> dict:
    """Heart rate through one workout visit (back-to-back workouts less than 10 minutes apart, e.g. one gym visit), from the watch's readings every few seconds averaged into 10-second steps. Pass the date (YYYY-MM-DD) and local start time (HH:MM) of any workout from `workouts`. Returns each workout's median, lowest and highest heart rate next to Fitbit's own average."""
    try:
        day = parse_day(date).isoformat()
    except ValueError:
        raise ToolError(f"date must be YYYY-MM-DD, got {date!r}")
    rows = [_workout(r) for r in db().execute(f"SELECT {_WORKOUT_COLS} FROM exercise WHERE date = ? ORDER BY start",
                                              [day]).fetchall()]
    if not rows:
        first, _ = _workout_window()
        raise ToolError(f"No workouts on {day} (workouts are recorded from {first} on).")
    try:
        hh, mm = (int(x) for x in str(time).strip()[:5].split(":"))
    except ValueError:
        raise ToolError(f"time must be HH:MM (local), got {time!r}")
    want = hh * 60 + mm
    gap, hit = min((abs(int(w["start"][11:13]) * 60 + int(w["start"][14:16]) - want), i) for i, w in enumerate(rows))
    if gap > 5:
        raise ToolError(f"No workout starts near {time} on {day}. Workouts that day: "
                        + ", ".join(f"{w['start'][11:]} {w['type']}" for w in rows))
    visit = rows[hit]["visit"]
    found = db().execute(f"SELECT {_WORKOUT_COLS}, start_local, end_local FROM exercise WHERE visit = ? ORDER BY start",
                         [visit]).fetchall()
    ws, times = [_workout(r[:-2]) for r in found], [(r[-2], r[-1]) for r in found]
    vstart = times[0][0]  # offsets in workout_hr count seconds from the visit's first start
    steps = db().execute("SELECT offset_s, bpm FROM workout_hr WHERE visit = ? ORDER BY offset_s", [visit]).fetchall()
    if not steps:
        raise ToolError(f"No heart-rate readings were exported for the visit starting {str(vstart)[:16]}.")
    segs = []
    for w, (ws_start, ws_end) in zip(ws, times):
        s, e = (ws_start - vstart).total_seconds(), (ws_end - vstart).total_seconds()
        bpm = [b for o, b in steps if s <= o + 5 <= e]  # 10-second steps whose midpoint falls inside the workout
        segs.append({"key": w["key"], "type": w["type"], "start": w["start"][11:], "end": w["end"][11:],
                     "start_min": round(s / 60, 2), "end_min": round(e / 60, 2), "minutes": w["minutes"],
                     "median_hr": _r(_median(bpm), 0) if bpm else None, "min_hr": _r(min(bpm), 0) if bpm else None,
                     "max_hr": _r(max(bpm), 0) if bpm else None, "fitbit_avg_hr": w["avg_hr"],
                     "steps_with_hr": len(bpm)})
    span = _r((times[-1][1] - vstart).total_seconds() / 60, 0)
    values = {"samples": float(len(steps)), "step_seconds": 10.0, "visit_minutes": float(span),
              "visit_max_hr": float(max(b for _, b in steps)), "workouts": float(len(ws))}
    values |= {f"{k}[{g['key']}]": float(g[k]) for g in segs
               for k in ("median_hr", "min_hr", "max_hr", "fitbit_avg_hr", "minutes") if g[k] is not None}
    vs = str(vstart)
    eid = ledger().add(tool="workout_heart_rate", args={"date": day, "time": rows[hit]["start"][11:]},
                       table="workout_hr", date_range=(day, day), n=len(steps), values=values, n_unit="10-second step",
                       context={"visit": visit, "visit_start": vs[:19], "segments": segs})
    return {"evidence_id": eid, "visit_start": vs[11:16], "visit_end": max(w["end"] for w in ws)[11:],
            "visit_minutes": span, "visit_max_hr": values["visit_max_hr"], "samples": len(steps),
            "workouts": [{k: g[k] for k in ("key", "type", "start", "end", "minutes", "median_hr", "min_hr", "max_hr",
                                            "fitbit_avg_hr")} for g in segs],
            "zone_note": ZONE_NOTE, "hr_note": HR_NOTE}


RESEARCH_TOOLS = [get_coverage, list_metrics, get_daily, summarize_metric, compare_periods,
                  compare_groups, correlate, check_guideline, sleep_sessions, workouts, workout_heart_rate]
GATEKEEPER_TOOLS = [get_coverage, list_metrics]
