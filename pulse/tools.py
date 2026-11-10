"""Deterministic tools shared by the agents, the MCP server and the eval gold.

Every tool records the numbers it returns in the per-request EvidenceLedger and returns
the evidence id, so the Writer can cite it and the Verifier can check it.
Dates are YYYY-MM-DD strings, clamped to the snapshot's coverage (first_date .. asof).
"""
from datetime import timedelta
from functools import wraps

from pulse import stats
from pulse.data import asof, catalog, daily_columns, db, first_date, meta, parse_day
from pulse.evidence import ledger

RAIN_MM = 1.0
WATCH_START = "2026-10-31"  # first full day of Fitbit Edge data
WATCH_FIRST_DAY = "2026-10-30"  # first (partial) day with any watch data: watch-only metrics can't exist before it
# Metrics the phone measured alone before WATCH_START and the watch measures too from then on (all jump at that date:
# mean before vs after = steps 4,161 vs 10,197, distance 2.9 vs 7.1 km, floors 6.9 vs 17.8, active energy 146 vs 1,051 kcal).
WATCH_MEASURED = ("steps", "distance_km", "floors", "active_kcal")
GROUPINGS = ("weekday", "weekend", "rain", "temperature_band", "month", "device_era")


class ToolError(ValueError):
    pass


def _r(x, nd=1):
    return None if x is None else round(float(x), nd)


def _range(start: str, end: str) -> tuple[str, str, str | None]:
    """Clamp a requested range to coverage. Returns (start, end, note)."""
    try:
        s, e = parse_day(start), parse_day(end)
    except ValueError:
        raise ToolError(f"Dates must be YYYY-MM-DD, got {start!r}..{end!r}")
    if s > e:
        s, e = e, s
    lo, hi = parse_day(first_date()), parse_day(asof())
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
    eid = ledger().add(tool="get_coverage", args={}, table="coverage", date_range=(m["first_date"], m["asof"]),
                       n=None, values={f"{r[0]}_days": float(r[5]) for r in rows})
    return {"evidence_id": eid, "as_of": m["asof"], "first_date": m["first_date"], "location": m["location"],
            "metrics": [{"metric": r[0], "unit": r[1], "source": r[2], "first_date": r[3], "last_date": r[4],
                         "days_with_data": r[5]} for r in rows],
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
                               if out[k] is not None})
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
                       table="daily", date_range=(start, end), n=len(rows), values=values)
    return {"evidence_id": eid, "note": note, "measurement_note": _measurement_note(metric, start, end, group_by),
            **out}


@_wrap
def correlate(x: str, y: str, start: str, end: str, lag_days: int = 0) -> dict:
    """Spearman correlation between two daily metrics. lag_days=1 pairs x on day d with y on day d+1 (e.g. activity vs that night's sleep, which is dated the next morning). Fewer than 10 paired days returns insufficient_data."""
    x, y = _metric(x), _metric(y)
    start, end, note = _range(start, end)
    rows = db().execute(
        f"SELECT a.{x}, b.{y} FROM daily a JOIN daily b ON b.date = a.date + INTERVAL {int(lag_days)} DAY "
        f"WHERE a.date BETWEEN ? AND ? AND a.{x} IS NOT NULL AND b.{y} IS NOT NULL", [start, end]).fetchall()
    s = stats.spearman([float(r[0]) for r in rows], [float(r[1]) for r in rows])
    out = {"x": x, "y": y, "lag_days": lag_days, "start": start, "end": end, "n_pairs": s["n"],
           "rho": _r(s["rho"], 2), "ci95": [_r(s["ci_low"], 2), _r(s["ci_high"], 2)],
           "p_value": _r(s["p_value"], 3), "strength": stats.strength(s["rho"]), "verdict": s["verdict"]}
    eid = ledger().add(tool="correlate", args={"x": x, "y": y, "start": start, "end": end, "lag_days": lag_days},
                       table="daily", date_range=(start, end), n=s["n"],
                       values={k: v for k, v in (("rho", out["rho"]), ("n_pairs", s["n"])) if v is not None})
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
    eid = ledger().add(tool="check_guideline", args={"metric": metric, "start": start, "end": end},
                       table="daily", date_range=(start, end), n=n, source_url=ref["source_url"],
                       values={k: out[k] for k in ("periods_judged", "periods_meeting", "share_meeting_pct",
                                                   "average_per_period") if out[k] is not None}
                       | ({"target": float(ref["target"])} if "target" in ref else
                          {"low": float(ref["low"]), "high": float(ref["high"])})
                       | ({"days_before_watch": float(pre)} if pre else {}))
    return {"evidence_id": eid, "note": note, **({"recording_note": rnote, "days_before_watch": pre} if rnote else {}),
            **out}


@_wrap
def sleep_sessions(start: str, end: str, include_naps: bool = True) -> dict:
    """Individual sleep sessions (dated by the morning they end) with stage minutes and efficiency."""
    start, end, note = _range(start, end)
    rows = db().execute(
        "SELECT date::VARCHAR, strftime(start, '%Y-%m-%d %H:%M'), strftime(\"end\", '%Y-%m-%d %H:%M'), is_nap, "
        "minutes_asleep, deep, rem, light, minutes_awake, efficiency FROM sleep_sessions "
        "WHERE date BETWEEN ? AND ? AND (? OR NOT is_nap) ORDER BY start", [start, end, include_naps]).fetchall()
    cols = ["date", "start", "end", "is_nap", "minutes_asleep", "deep", "rem", "light", "awake", "efficiency"]
    values = {f"{c}@{r[0]}{'(nap)' if r[3] else ''}": float(r[i]) for r in rows
              for i, c in enumerate(cols) if i >= 4 and r[i] is not None}
    eid = ledger().add(tool="sleep_sessions", args={"start": start, "end": end}, table="sleep_sessions",
                       date_range=(start, end), n=len(rows), values=values)
    return {"evidence_id": eid, "note": note, "columns": cols, "rows": [list(r) for r in rows]}


RESEARCH_TOOLS = [get_coverage, list_metrics, get_daily, summarize_metric, compare_periods,
                  compare_groups, correlate, check_guideline, sleep_sessions]
GATEKEEPER_TOOLS = [get_coverage, list_metrics]
