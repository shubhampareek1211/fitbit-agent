"""Charts built by code from verified evidence, never by a model.

For the evidence ids the final report cites, this module turns the ledger's numbers into Apache ECharts option
objects (plain JSON) that the frontend only renders: the backend/frontend split from the Text2SQL lecture's "chart
JSON schemas" slide. Bar heights, target lines, means and correlations are the ledger's own values, the same ones
the Verifier checked, so a chart cannot disagree with the text. Bars start at zero.

Colours are tokens such as "@accent" or "@c-res": the frontend replaces each with the CSS variable of the same name,
so charts follow the page's light and dark themes.
"""
import json
import logging
from datetime import timedelta

from pulse import tools
from pulse.data import asof, catalog, daily_columns, db, first_date
from pulse.evidence import Evidence, EvidenceLedger
from pulse.verifier import CITE, SOURCES_HEADING

log = logging.getLogger("pulse")

MAX_CHARTS = 3
GRID = {"left": 8, "right": 16, "top": 16, "bottom": 8, "containLabel": True}
NAMES = {"steps": "steps", "steps_phone": "phone-counted steps", "steps_watch": "watch-counted steps",
         "sleep_minutes": "sleep", "azm_total": "Active Zone Minutes", "resting_hr": "resting heart rate",
         "hrv_ms": "HRV", "temp_mean_c": "mean temperature", "temp_max_c": "max temperature",
         "precip_mm": "precipitation", "distance_km": "distance", "active_kcal": "active energy",
         "spo2_avg": "blood oxygen", "active_minutes": "active minutes"}
GROUPS = {"weekday": "by day of the week", "weekend": "on weekdays vs weekends", "rain": "on rainy vs dry days",
          "temperature_band": "by temperature", "month": "by month", "device_era": "before vs after the watch"}
SHORT = {"phone only (before watch)": "before the watch", "watch + phone": "with the watch",
         "rainy (>= 1.0 mm)": "rainy (≥1 mm)"}
VERDICT = {"higher": "Clearly higher", "lower": "Clearly lower", "no_clear_difference": "No clear difference",
           "insufficient_data": "Too few days to tell", "positive": "Positive relationship",
           "negative": "Negative relationship", "inconclusive": "No clear relationship"}


# ---------------------------------------------------------------------------------------------- helpers
def _name(metric: str) -> str:
    return NAMES.get(metric, metric.replace("_", " "))


def _unit(metric: str) -> str:
    return (catalog().get(metric) or {}).get("unit", "")


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}" if abs(v) >= 10 else f"{v:,.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _signed(v) -> str:
    return ("+" if v is not None and v > 0 else "") + _fmt(v)


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _short(group: str) -> str:
    return group[:3] if group in WEEKDAYS else SHORT.get(group, group)


def _era(ev: Evidence) -> str:
    """Name the era when a range lies wholly on one side of the watch start (two such charts otherwise look alike)."""
    start, end = ev.date_range or ("", "")
    if ev.args.get("group_by") == "device_era" or not start:
        return ""
    return " (before the watch)" if end < tools.WATCH_START else " (with the watch)" if start >= tools.WATCH_START else ""


def _measurement(metric: str, start: str, end: str) -> str | None:
    """The caveat a chart must carry when it compares a watch-measured metric across the watch start: the text
    never presents that change as real, so the chart's subtitle can't call it 'clearly higher' either."""
    if metric not in tools.WATCH_MEASURED or not start < tools.WATCH_START <= end:
        return None
    tail = ("; phone-counted steps are the like-for-like comparison" if metric == "steps" else
            "; there is no phone-only version to compare like for like")
    return f"partly measurement, not behaviour: from {tools.WATCH_START} the watch also measures {_name(metric)}{tail}"


WEATHER = ("temp_mean_c", "temp_max_c", "precip_mm", "daylight_hours")


def _axis(metric: str, suffix: str = "") -> str:
    unit = _unit(metric)
    name = _name(metric) + suffix
    return name if not unit or unit in name else f"{name} ({unit})"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _plural(n, word: str) -> str:
    return f"{_fmt(n)} {word}{'' if n == 1 else 's'}"


def _caption(ev: Evidence) -> str:
    rng = f"{ev.date_range[0]} → {ev.date_range[1]}" if ev.date_range else "all dates"
    if ev.date_range and ev.date_range[0] == ev.date_range[1]:
        rng = ev.date_range[0]
    return f"{ev.id}: {ev.tool} on {ev.table}, {rng}" + (f", n = {_plural(ev.n, ev.n_unit)}" if ev.n is not None else "")


def _daily(metric: str, start: str, end: str) -> tuple[list[str], list[float | None]]:
    if metric not in daily_columns():
        raise ValueError(f"unknown metric {metric}")
    rows = db().execute(f"SELECT date::VARCHAR, {metric} FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
                        [start, end]).fetchall()
    return [r[0] for r in rows], [None if r[1] is None else round(float(r[1]), 1) for r in rows]


def _days(dates: list[str]) -> list[str]:
    """Date-axis labels as MM-DD: every date is in one year, and full ISO dates crowd a phone-width axis."""
    return [d[5:] for d in dates]


def _rolling(vals: list[float | None], k: int = 7, min_n: int = 4) -> list[float | None]:
    out = []
    for i in range(len(vals)):
        window = [v for v in vals[max(0, i - k + 1): i + 1] if v is not None]
        out.append(round(sum(window) / len(window), 1) if len(window) >= min_n else None)
    return out


def _watch_area(dates: list[str]) -> dict | None:
    """Shade the watch era on a date axis, so a jump at the watch start reads as measurement, not behaviour."""
    if not dates or not dates[0] < tools.WATCH_START <= dates[-1]:
        return None
    return {"silent": True, "itemStyle": {"color": "@c-gate", "opacity": 0.12},
            "label": {"show": True, "position": "insideTop", "formatter": "watch era", "color": "@muted",
                      "fontSize": 11},
            "data": [[{"xAxis": tools.WATCH_START[5:]}, {"xAxis": dates[-1][5:]}]]}


def _ref_line(value, text: str) -> dict:
    """A dashed reference line (target, mean) labelled past the plot's right edge, clear of bars and lines."""
    return {"silent": True, "symbol": "none", "lineStyle": {"color": "@ink", "type": "dashed"},
            "label": {"formatter": text, "color": "@ink", "position": "end", "fontSize": 11}, "data": [{"yAxis": value}]}


REF_GRID = {**GRID, "right": 80}  # leaves room for a reference line's label


def _bar_label(v) -> dict:
    return {"show": v is not None, "position": "top", "formatter": _fmt(v), "color": "@ink", "fontSize": 11}


def _chart(kind, title, subtitle, evs, option, height=260, note=None, table=None) -> dict:
    """A chart spec. `table` ({"columns", "rows"}) is the same data as a table, shown under the chart on request:
    the accessible view for readers who can't tell the colours apart."""
    caption = " · ".join(_caption(e) for e in evs) + (f". {note}" if note else "")
    return {"kind": kind, "title": title, "subtitle": subtitle, "caption": caption,
            "evidence": [e.id for e in evs], "height": height, "option": option, **({"table": table} if table else {})}


# ---------------------------------------------------------------------------------------------- builders
def groups_chart(evs: list[Evidence]) -> dict:
    """compare_groups: one bar per group (one series per metric when several share a grouping and range)."""
    ev0 = evs[0]
    gb = ev0.args["group_by"]
    groups = [k[5:-1] for k in ev0.values if k.startswith("mean[")]
    # A metric whose bars equal an earlier one's (e.g. steps == phone steps before the watch) is noted, not drawn.
    means = {e.id: [e.values.get(f"mean[{g}]") for g in groups] for e in evs}
    same_as = {e.id: next((_name(o.args["metric"]) for o in evs[:i] if means[o.id] == means[e.id]), None)
               for i, e in enumerate(evs)}
    shown = [e for e in evs if not same_as[e.id]]
    single = len(shown) == 1
    labelled = len(groups) * len(shown) <= 8  # value labels only when the bars have room for them
    series = []
    for e in shown:
        data = []
        for g, v in zip(groups, means[e.id]):
            item = {"value": v, "label": _bar_label(v) if labelled else {"show": False}}
            if single and len(groups) > 2 and g not in (e.context.get("highest"), e.context.get("lowest")):
                item["itemStyle"] = {"opacity": 0.45}  # highlight only the computed highest and lowest
            data.append(item)
        series.append({"type": "bar", "name": _name(e.args["metric"]), "data": data, "barMaxWidth": 44})
    ns = [int(ev0.values.get(f"n[{g}]", 0)) for g in groups]
    if len(groups) <= 4:  # few groups: the day count fits under each label
        labels = [f"{_short(g)}\n{n} days" for g, n in zip(groups, ns)]
    else:  # e.g. 7 weekdays on a phone: say it once instead
        labels = [_short(g) for g in groups]
    parts = [f"{_name(e.args['metric'])} is identical to {same_as[e.id]} here" for e in evs if same_as[e.id]]
    if len(groups) > 4 and ns:
        parts.append(f"{min(ns)}-{max(ns)} days per group" if min(ns) != max(ns) else f"{ns[0]} days per group")
    cited, evs = evs, shown  # every cited id stays in the caption; only distinct series are drawn
    for e in evs:
        c, unit = e.context, _unit(e.args["metric"])
        prefix = "" if single else f"{_name(e.args['metric'])}: "
        if c.get("verdict"):
            lo, hi = (c.get("ci95") or [None, None])[:2]
            caveat = _measurement(e.args["metric"], *e.date_range) if gb == "device_era" else None
            parts.append(f"{prefix}{'Raw difference' if caveat else VERDICT.get(c['verdict'], c['verdict'])}, "
                         f"{_short(c['other'])} vs "
                         f"{_short(c['baseline'])} {_signed(e.values.get('difference'))} {unit} "
                         f"(95% CI {_fmt(lo)} to {_fmt(hi)})" + (f", {caveat}" if caveat else ""))
        elif c.get("highest"):
            hi, lo = c["highest"], c["lowest"]
            parts.append(f"{prefix}highest {_short(hi)} ({_fmt(e.values.get(f'mean[{hi}]'))}), "
                         f"lowest {_short(lo)} ({_fmt(e.values.get(f'mean[{lo}]'))})")
    metrics = _and([_name(e.args["metric"]) for e in evs])
    option = {"grid": {**GRID, "top": 40 if not single else 24},
              "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
              "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0, "fontSize": 10}},
              "yAxis": {"type": "value", "min": 0}, "series": series}
    if not single:
        option["legend"] = {"top": 0, "left": 0}
    return _chart("bars", f"Average {metrics} per day, {GROUPS.get(gb, gb)}{_era(ev0)}", " · ".join(parts), cited, option)


def periods_chart(e: Evidence) -> dict:
    a, b = e.args["a"], e.args["b"]
    v, c = e.values, e.context
    labels = [f"{a[0][5:]} → {a[1][5:]}\n{int(v.get('n_a', 0))} days",  # MM-DD: fits two labels on a phone
              f"{b[0][5:]} → {b[1][5:]}\n{int(v.get('n_b', 0))} days"]
    lo, hi = (c.get("ci95") or [None, None])[:2]
    # Only when the two periods sit on opposite sides of the watch start is their difference a measurement change.
    caveat = _measurement(e.args["metric"], min(a[1], b[1]), max(a[0], b[0])) if min(a[1], b[1]) < max(a[0], b[0]) else None
    sub = (f"{'Raw difference' if caveat else VERDICT.get(c.get('verdict'), c.get('verdict'))}: "
           f"{_signed(v.get('difference'))} "
           f"{_unit(e.args['metric'])} ({_signed(v.get('pct_change'))}%), 95% CI {_fmt(lo)} to {_fmt(hi)}"
           + (f", {caveat}" if caveat else ""))
    option = {"grid": {**GRID, "top": 24}, "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
              "xAxis": {"type": "category", "data": labels, "axisLabel": {"interval": 0, "fontSize": 10}},
              "yAxis": {"type": "value", "min": 0},
              "series": [{"type": "bar", "name": _name(e.args["metric"]), "barMaxWidth": 56,
                          "data": [{"value": v.get("mean_a"), "label": _bar_label(v.get("mean_a")),
                                    "itemStyle": {"opacity": 0.55}},
                                   {"value": v.get("mean_b"), "label": _bar_label(v.get("mean_b"))}]}]}
    split = a[1] < tools.WATCH_START <= b[0]
    return _chart("bars", f"Average {_name(e.args['metric'])} per day: "
                          f"{'before vs after the watch' if split else 'two periods'}", sub, [e], option)


def correlate_chart(e: Evidence) -> dict:
    x, y, lag = e.args["x"], e.args["y"], int(e.args.get("lag_days") or 0)
    start, end = e.args["start"], e.args["end"]
    if x not in daily_columns() or y not in daily_columns():
        raise ValueError("unknown metric")
    rows = db().execute(  # the same pairs the tool correlated
        f"SELECT a.date::VARCHAR, a.{x}, b.{y} FROM daily a JOIN daily b ON b.date = a.date + {lag} "
        f"WHERE a.date BETWEEN ? AND ? AND a.{x} IS NOT NULL AND b.{y} IS NOT NULL ORDER BY a.date",
        [start, end]).fetchall()
    pts = [[round(float(r[1]), 1), round(float(r[2]), 1), r[0]] for r in rows]
    # Weather goes on the x-axis (the possible cause); ρ is symmetric, so swapping only changes the drawing.
    swap = not lag and y in WEATHER and x not in WEATHER
    if swap:
        x, y = y, x
        pts = [[p[1], p[0], p[2]] for p in pts]
    split = (x in tools.WATCH_MEASURED or y in tools.WATCH_MEASURED) and start < tools.WATCH_START <= end
    groups = ([("before the watch", [p for p in pts if p[2] < tools.WATCH_START]),
               ("with the watch", [p for p in pts if p[2] >= tools.WATCH_START])] if split else [("days", pts)])
    dims = [_axis(x), _axis(y, " next day" if lag else ""), "date"]
    series = [{"type": "scatter", "name": n, "data": d, "symbolSize": 7, "itemStyle": {"opacity": 0.75},
               "dimensions": dims, "encode": {"x": 0, "y": 1, "tooltip": [2, 0, 1]}} for n, d in groups if d]
    c = e.context
    lo, hi = (c.get("ci95") or [None, None])[:2]
    rho = e.values.get("rho")
    sub = (f"{VERDICT.get(c.get('verdict'), c.get('verdict'))}: Spearman ρ = "
           f"{'n/a' if rho is None else f'{rho:+.2f}'}"
           + (f" (95% CI {lo:+.2f} to {hi:+.2f})" if lo is not None and hi is not None else "")
           + f", {int(e.values.get('n_pairs', len(pts)))} paired days")
    option = {"grid": {**GRID, "top": 56 if split else 32, "bottom": 24},  # room for the legend, then the y name
              "tooltip": {"trigger": "item"},
              "xAxis": {"type": "value", "scale": True, "name": dims[0], "nameLocation": "middle", "nameGap": 26},
              "yAxis": {"type": "value", "scale": True, "name": dims[1], "nameTextStyle": {"align": "left"}},
              "series": series}
    if split:
        option["legend"] = {"top": 0, "left": 0}
    return _chart("scatter", f"{_cap(_name(y))}{' the next day' if lag else ''} vs {_name(x)}", sub, [e],
                  option, height=310)


def guideline_chart(e: Evidence) -> dict:
    c, v, metric = e.context, e.values, e.args["metric"]
    between = c.get("op") == "between"
    if not v.get("periods_judged"):
        return None  # nothing was judged (e.g. no full week): daily bars against a weekly target would mislead
    if c.get("period") == "week" and c.get("weeks"):
        cats = [f"{w['week'][5:10]} → {w['week'][17:]}" for w in c["weeks"]]
        vals = [w["total"] for w in c["weeks"]]
    else:  # daily target: the days the tool judged are the days with a value
        pairs = [(d, x) for d, x in zip(*_daily(metric, e.args["start"], e.args["end"])) if x is not None]
        cats, vals = _days([d for d, _ in pairs]), [x for _, x in pairs]

    def meets(x):
        return v["low"] <= x <= v["high"] if between else x >= v["target"]
    data = [{"value": x, "itemStyle": {"color": "@ok" if meets(x) else "@warn"}} for x in vals]
    if len(data) <= 14:
        for d in data:
            d["label"] = _bar_label(d["value"])
    mark = ({"markArea": {"silent": True, "itemStyle": {"color": "@ok", "opacity": 0.10},
                          "data": [[{"yAxis": v["low"]}, {"yAxis": v["high"]}]]}} if between else
            {"markLine": _ref_line(v["target"], f"target {_fmt(v['target'])}")})
    top = round(max([v["high"], *vals]) * 1.05) if between else None  # show the whole typical range
    option = {"grid": {**(GRID if between else REF_GRID), "top": 24},
              "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
              "xAxis": {"type": "category", "data": cats, "axisLabel": {"fontSize": 10, "hideOverlap": True}},
              "yAxis": {"type": "value", "min": 0, **({"max": top} if top else {})},
              "series": [{"type": "bar", "name": _name(metric), "data": data, "barMaxWidth": 44, **mark}]}
    unit = _unit(metric)
    goal = (f"stayed within {_fmt(v['low'])}-{_fmt(v['high'])} {unit}" if between else
            f"met the target of {_fmt(v['target'])}+ {unit} per {c.get('period')}")
    sub = (f"{_fmt(v.get('periods_meeting'))} of {_plural(v.get('periods_judged'), c.get('period', 'day'))} "
           f"{goal} · {c.get('source_name')}")
    return _chart("guideline", f"{_cap(_name(metric))} vs the guideline", sub, [e], option,
                  note=c.get("regularity_note"))


def summary_chart(e: Evidence) -> dict:
    metric, start, end = e.args["metric"], e.args["start"], e.args["end"]
    dates, vals = _daily(metric, start, end)
    mean = e.values.get("mean")
    mean_line = _ref_line(mean, f"mean {_fmt(mean)}")
    area = _watch_area(dates) if metric in tools.WATCH_MEASURED else None
    if len(dates) <= 31:
        series = [{"type": "bar", "name": _name(metric), "barMaxWidth": 36, "data": vals, "markLine": mean_line,
                   **({"markArea": area} if area else {})}]
    else:
        series = [{"type": "line", "name": "daily", "data": vals, "showSymbol": False,
                   "lineStyle": {"width": 1, "opacity": 0.45}, "itemStyle": {"opacity": 0.45},
                   **({"markArea": area} if area else {})},
                  {"type": "line", "name": "7-day avg", "data": _rolling(vals), "showSymbol": False,
                   "smooth": True, "lineStyle": {"width": 2.5}, "markLine": mean_line}]
    option = {"grid": {**REF_GRID, "top": 40 if len(series) > 1 else 24}, "tooltip": {"trigger": "axis"},
              "xAxis": {"type": "category", "data": _days(dates), "axisLabel": {"fontSize": 10, "hideOverlap": True}},
              "yAxis": {"type": "value", "min": 0}, "series": series}
    if len(series) > 1:
        option["legend"] = {"top": 0, "left": 0}
    sub = f"Mean {_fmt(mean)} {_unit(metric)} over {_fmt(e.values.get('days_with_data'))} days with data"
    if e.values.get("days_before_watch"):
        sub += f"; the {_fmt(e.values['days_before_watch'])} days before the watch are not recorded yet"
    return _chart("series", f"Daily {_name(metric)}", sub, [e], option)


def daily_chart(e: Evidence) -> dict:
    metrics = list(e.args.get("metrics") or [])[:2]
    dates = sorted({k.split("@", 1)[1] for k in e.values if "@" in k})
    units = [_unit(m) for m in metrics]
    dual = len(metrics) == 2 and units[0] != units[1]
    series = [{"type": "line", "name": _name(m), "yAxisIndex": i if dual else 0, "connectNulls": False,
               "showSymbol": len(dates) <= 31, "data": [e.values.get(f"{m}@{d}") for d in dates]}
              for i, m in enumerate(metrics)]
    area = _watch_area(dates) if any(m in tools.WATCH_MEASURED for m in metrics) else None
    if area:
        series[0]["markArea"] = area
    option = {"grid": {**GRID, "top": 60 if dual else 40}, "tooltip": {"trigger": "axis"},
              "legend": {"top": 0, "left": 0},
              "xAxis": {"type": "category", "data": _days(dates), "axisLabel": {"fontSize": 10, "hideOverlap": True}},
              "yAxis": ([{"type": "value", "name": u} for u in units]
                        if dual else {"type": "value"}),
              "series": series}
    sub = f"Left axis: {_axis(metrics[0])}; right axis: {_axis(metrics[1])}" if dual else ""
    return _chart("series", f"Daily {_and([_name(m) for m in metrics])}", sub, [e], option)


def sleep_chart(e: Evidence) -> dict:
    nights = sorted({k.split("@", 1)[1] for k in e.values if k.startswith("minutes_asleep@") and "(nap)" not in k})
    stages = [("deep", "deep"), ("rem", "REM"), ("light", "light"), ("awake", "awake")]
    series = [{"type": "bar", "stack": "night", "name": label, "barMaxWidth": 36,
               "data": [e.values.get(f"{key}@{d}") for d in nights],
               **({"itemStyle": {"color": "@line"}} if key == "awake" else {})} for key, label in stages]
    option = {"grid": {**GRID, "top": 40}, "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
              "legend": {"top": 0, "left": 0},
              "xAxis": {"type": "category", "data": _days(nights), "axisLabel": {"fontSize": 10, "hideOverlap": True}},
              "yAxis": {"type": "value", "min": 0},
              "series": series}
    return _chart("stack", "Sleep stages per night", "Minutes in each stage, main sleep only (naps excluded)", [e],
                  option)


# ---------------------------------------------------------------------------------------------- workouts
# Fitbit's zones are ordered intensities, so they get one hue stepped light -> dark (CSS --z1..--z4, validated as an
# ordinal ramp against both surfaces). Workout types are identities, so they get categorical slots in a fixed order
# (CSS --s1..--s6, validated for adjacent pairs); a type never changes colour when another is missing.
ZONE_COLORS = {"light": "@z1", "moderate": "@z2", "vigorous": "@z3", "peak": "@z4"}
FAMILIES = (("Walk", ("WALKING", "TREADMILL_WALK")), ("Run", ("RUNNING", "TREADMILL")), ("Elliptical", ("ELLIPTICAL",)),
            ("Rowing", ("ROWING_MACHINE",)), ("Strength", ("WEIGHTS",)), ("Other", ()))
PER_WORKOUT_MAX = 30  # more workouts than this: one bar per type instead of one per workout
LABEL_GRID = {**GRID, "left": 20}  # ECharts measures long row labels a few px short ("11-04 20:00 ..." lost its "1")
TIMELINE_MAX_DAYS = 31


def _family(code: str) -> int:
    return next((i for i, (_, codes) in enumerate(FAMILIES) if code in codes), len(FAMILIES) - 1)


def _hours(stamp: str) -> float:
    return round(int(stamp[11:13]) + int(stamp[14:16]) / 60, 3)


SHORT_TYPES = {"Rowing machine": "Rowing", "Treadmill walk": "Tread. walk", "Cardio workout": "Cardio"}


def _workout_label(w: dict) -> str:
    return f"{w['start'][5:]} {SHORT_TYPES.get(w['type'], w['type'])}"  # "11-02 19:01 Elliptical": fits a phone


def workout_effort_chart(e: Evidence) -> dict:
    """workouts: minutes in each heart-rate zone, one stacked bar per workout (or per type when there are many)."""
    ws, v = e.context["workouts"], e.values
    per_workout = len(ws) <= PER_WORKOUT_MAX and all(f"light_min[{w['key']}]" in v for w in ws)
    if per_workout:
        cats, keys = [_workout_label(w) for w in ws], [w["key"] for w in ws]
    else:
        types = [k[9:-1] for k in v if k.startswith("workouts[")]
        cats, keys = [f"{t} ({_fmt(v[f'workouts[{t}]'])})" for t in types], types
    series = [{"type": "bar", "name": z, "stack": "zones", "barMaxWidth": 14,
               "itemStyle": {"color": ZONE_COLORS[z], "borderColor": "@surface", "borderWidth": 1},
               "data": [v.get(f"{z}_min[{k}]") for k in keys]} for z in tools.ZONES]
    option = {"grid": {**LABEL_GRID, "top": 32, "bottom": 24}, "legend": {"top": 0, "left": 0},
              "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
              "xAxis": {"type": "value", "min": 0, "name": "minutes", "nameLocation": "middle", "nameGap": 22},
              # interval 0: every label is laid out (and measured for the grid), so the longest one isn't clipped
              "yAxis": {"type": "category", "data": cats, "inverse": True,
                        "axisLabel": {"fontSize": 10, "interval": 0}},
              "series": series}
    sub = (f"{_plural(v['workouts'], 'workout')}, {_fmt(v['total_minutes'])} minutes: {_fmt(v['moderate_or_higher_min'])}"
           f" moderate or harder, {_fmt(v['vigorous_or_peak_min'])} vigorous or peak · "
           f"{_fmt(v['started_on_watch'])} started on the watch, {_fmt(v['detected_automatically'])} detected automatically")
    if per_workout:
        rows = [[_workout_label(w), "started" if w["started_on_watch"] else "auto",
                 *[v.get(f"{k}[{w['key']}]") for k in ("minutes", "avg_hr", *(f"{z}_min" for z in tools.ZONES))]]
                for w in ws]
        table = {"columns": ["Workout", "How", "Minutes", "Avg HR", *[z.capitalize() for z in tools.ZONES]], "rows": rows}
    else:
        table = {"columns": ["Type", "Workouts", "Minutes", *[z.capitalize() for z in tools.ZONES]],
                 "rows": [[k, v.get(f"workouts[{k}]"), v.get(f"minutes[{k}]"),
                           *[v.get(f"{z}_min[{k}]") for z in tools.ZONES]] for k in keys]}
    title = "Minutes in each heart-rate zone, per " + ("workout" if per_workout else "workout type")
    return _chart("stack", title, sub, [e], option, height=max(200, 22 * len(cats) + 76),
                  note="Zones are Fitbit's own (light, moderate, vigorous, peak)", table=table)


def workout_timeline_chart(e: Evidence) -> dict | None:
    """workouts: when each workout happened, one row per day, coloured by type (back-to-back blocks = one visit)."""
    ws = e.context["workouts"]
    days = []
    if ws:
        d, last = tools.parse_day(ws[0]["date"]), tools.parse_day(ws[-1]["date"])
        while d <= last:
            days.append(d)
            d += timedelta(days=1)
    if len(ws) < 2 or len(days) > TIMELINE_MAX_DAYS:
        return None
    index = {d.isoformat(): i for i, d in enumerate(days)}
    lo = min(_hours(w["start"]) for w in ws)
    hi = max(_hours(w["end"]) if w["end"][:10] == w["date"] else 24 for w in ws)
    series = []
    for i, (fam, _) in enumerate(FAMILIES):
        data = [{"name": w["type"],
                 "value": [index[w["date"]], _hours(w["start"]),
                           _hours(w["end"]) if w["end"][:10] == w["date"] else 24,  # past midnight: stop at 24:00
                           f"{w['start'][11:]}–{w['end'][11:]}", e.values.get(f"minutes[{w['key']}]"),
                           "started on the watch" if w["started_on_watch"] else "detected automatically"],
                 "itemStyle": {"opacity": 1 if w["started_on_watch"] else 0.5}}
                for w in ws if _family(w["type_code"]) == i]
        if data:
            series.append({"type": "custom", "name": fam, "renderItem": "timeline",  # drawn by static/charts.js
                           "itemStyle": {"color": f"@s{i + 1}"},
                           "dimensions": ["day", "from", "to", "time", "minutes", "how"],
                           "encode": {"x": [1, 2], "y": 0, "tooltip": [3, 4, 5]}, "data": data})
    step = 2 if hi - lo <= 12 else 3  # hour ticks that still fit a phone-width axis
    option = {"grid": {**LABEL_GRID, "top": 52 if len(series) > 4 else 32, "bottom": 24},  # 5+ legend items wrap on a phone
              "legend": {"top": 0, "left": 0}, "tooltip": {"trigger": "item"},
              "xAxis": {"type": "value", "min": int(lo) - int(lo) % step,
                        "max": min(24, int(hi) + (hi % 1 > 0)), "interval": step,
                        "axisLabel": {"formatter": "{value}:00", "fontSize": 10, "hideOverlap": True},
                        "name": "time of day", "nameLocation": "middle", "nameGap": 22},
              "yAxis": {"type": "category", "inverse": True, "axisLabel": {"fontSize": 10, "interval": 0},
                        "data": [f"{d.isoformat()[5:]} {WEEKDAYS[d.weekday()][:3]}" for d in days]},
              "series": series}
    visits = e.context.get("visits") or []
    sub = "Solid blocks were started on the watch, faded ones detected automatically"
    if visits:
        mins = [int(x["minutes"]) for x in visits]
        sub += (f" · {_plural(len(visits), 'visit')} with back-to-back workouts, "
                + (f"{min(mins)}–{max(mins)} minutes" if len(mins) > 1 else f"{mins[0]} minutes"))
    table = {"columns": ["Day", "Time", "Workout", "Minutes", "How"],
             "rows": [[w["date"], f"{w['start'][11:]}–{w['end'][11:]}", w["type"],
                       e.values.get(f"minutes[{w['key']}]"), "started" if w["started_on_watch"] else "auto"]
                      for w in ws]}
    return _chart("timeline", "When each workout happened", sub, [e], option,
                  height=max(180, 26 * len(days) + 76), table=table)


def workouts_charts(e: Evidence) -> list[dict]:
    return [c for c in (workout_effort_chart(e), workout_timeline_chart(e)) if c]


def workout_hr_chart(e: Evidence) -> dict:
    """workout_heart_rate: the 10-second heart-rate steps through a visit, each workout shaded, with its median."""
    c, v = e.context, e.values
    rows = db().execute("SELECT offset_s, bpm FROM workout_hr WHERE visit = ? ORDER BY offset_s",
                        [c["visit"]]).fetchall()
    pts = [[round(o / 60, 2), b] for o, b in rows]
    segs = c["segments"]
    span = max(s["end_min"] for s in segs)
    labelled, areas, medians = 0, [], []
    for s in segs:
        show = (s["end_min"] - s["start_min"]) >= 0.07 * span  # label only blocks wide enough for their name
        labelled += show
        areas.append([{"xAxis": s["start_min"], "name": s["type"].split()[0] if show else "",
                       "label": {"position": "insideTop" if labelled % 2 else "insideBottom"}},
                      {"xAxis": s["end_min"]}])
        if s.get("median_hr") is not None:
            medians.append([{"coord": [s["start_min"], s["median_hr"]]}, {"coord": [s["end_min"], s["median_hr"]]}])
    single = len(segs) == 1
    series = [{"type": "line", "name": "heart rate", "data": pts, "showSymbol": False, "connectNulls": False,
               "lineStyle": {"width": 2, "color": "@ember"}, "itemStyle": {"color": "@ember"},
               "markLine": {"silent": True, "symbol": "none", "label": {"show": False},
                            "lineStyle": {"color": "@ink", "type": "dashed", "width": 1}, "data": medians}}]
    if not single:  # shade each workout of a visit; the breaks between them stay unshaded
        series[0]["markArea"] = {"silent": True, "itemStyle": {"color": "@muted", "opacity": 0.12},
                                 "label": {"color": "@muted", "fontSize": 10}, "data": areas}
    start = c["visit_start"][11:16]
    option = {"grid": {**GRID, "top": 28, "bottom": 24}, "tooltip": {"trigger": "axis"},
              "xAxis": {"type": "value", "min": 0, "max": round(span, 1), "name": f"minutes from {start}",
                        "nameLocation": "middle", "nameGap": 22, "axisLabel": {"showMaxLabel": False}},
              "yAxis": {"type": "value", "scale": True, "name": "bpm", "nameTextStyle": {"align": "right"}},
              "series": series}
    meds = ", ".join(f"{s['type'].lower()} {_fmt(s['median_hr'])}" for s in segs if s.get("median_hr") is not None)
    sub = (f"Dashed line{'' if single else 's'}: {'the' if single else 'each'} workout's median heart rate ({meds} bpm)"
           f" · peak {_fmt(v.get('visit_max_hr'))} bpm")
    title = (f"Heart rate during the {segs[0]['type'].lower()}, {c['visit_start'][:10]} {start}" if single else
             f"Heart rate through the visit, {c['visit_start'][:10]} {start}–{segs[-1]['end']}")
    table = {"columns": ["Workout", "Time", "Minutes", "Median", "Lowest", "Highest", "Fitbit avg"],
             "rows": [[s["type"], f"{s['start']}–{s['end']}", s["minutes"], s.get("median_hr"), s.get("min_hr"),
                       s.get("max_hr"), s.get("fitbit_avg_hr")] for s in segs]}
    return _chart("trace", title, sub, [e], option, height=280,
                  note="Readings every ~2 s from the wrist sensor, averaged per 10 s", table=table)


BUILDERS = {"compare_periods": periods_chart, "correlate": correlate_chart, "check_guideline": guideline_chart,
            "summarize_metric": summary_chart, "get_daily": daily_chart, "sleep_sessions": sleep_chart,
            "workouts": workouts_charts, "workout_heart_rate": workout_hr_chart}
CHARTABLE = set(BUILDERS) | {"compare_groups"}


# ---------------------------------------------------------------------------------------------- selection
def cited_in_order(report: str, ledger: EvidenceLedger) -> list[str]:
    """Evidence ids in the order the report first cites them (the answer's lead evidence comes first)."""
    body = SOURCES_HEADING.split(report)[0]
    seen: list[str] = []
    for block in CITE.findall(body):
        for eid in block.replace(";", ",").split(","):
            eid = eid.strip()
            if eid not in seen and ledger.get(eid):
                seen.append(eid)
    return seen


def build_charts(report: str, ledger: EvidenceLedger, limit: int = MAX_CHARTS) -> list[dict]:
    """Up to `limit` charts for the evidence the report cites. A chart that fails to build is skipped, never fatal."""
    evs, keys = [], set()
    for eid in cited_in_order(report, ledger):
        e = ledger.get(eid)
        key = (e.tool, json.dumps(e.args, sort_keys=True, default=str))
        if e.tool == "correlate":  # one scatter per pair of metric families (mean vs max temperature look alike)
            key = (e.tool, *sorted((e.args["x"].split("_")[0], e.args["y"].split("_")[0])))
        if e.tool in CHARTABLE and key not in keys:  # the same tool call cited twice gets one chart
            keys.add(key)
            evs.append(e)
    charts, used = [], set()
    for e in evs:
        if e.id in used:
            continue
        try:
            if e.tool == "compare_groups":  # metrics sharing a grouping, range and unit go in one grouped chart
                same = [o for o in evs if o.tool == "compare_groups" and o.id not in used
                        and o.args.get("group_by") == e.args.get("group_by") and o.date_range == e.date_range
                        and _unit(o.args["metric"]) == _unit(e.args["metric"])][:3]
                used.update(o.id for o in same)
                chart = groups_chart(same)
            else:
                used.add(e.id)
                chart = BUILDERS[e.tool](e)
        except Exception:
            log.exception("chart for %s failed", e.id)
            continue
        for c in chart if isinstance(chart, list) else [chart]:  # one evidence item may give several charts
            if c and len(charts) < limit:
                charts.append(c)
        if len(charts) >= limit:
            break
    return charts


# ---------------------------------------------------------------------------------------------- overview
def overview() -> list[dict]:
    """The landing page's two charts: the step trend with the watch era shaded, and a calendar of phone steps."""
    start, end = first_date(), asof()
    dates, total = _daily("steps", start, end)
    _, phone = _daily("steps_phone", start, end)
    area = _watch_area(dates)
    trend = {"grid": {**GRID, "top": 52}, "tooltip": {"trigger": "axis"}, "legend": {"top": 0, "left": 0},
             "xAxis": {"type": "category", "data": _days(dates), "axisLabel": {"fontSize": 10, "hideOverlap": True}},
             "yAxis": {"type": "value", "min": 0},
             "series": [{"type": "line", "name": "daily total", "data": total, "showSymbol": False,
                         "lineStyle": {"width": 1, "opacity": 0.4}, "itemStyle": {"opacity": 0.4},
                         **({"markArea": area} if area else {})},
                        {"type": "line", "name": "7-day avg", "data": _rolling(total),
                         "showSymbol": False, "smooth": True, "lineStyle": {"width": 2.5}},
                        {"type": "line", "name": "7-day avg, phone only", "data": _rolling(phone),
                         "showSymbol": False, "smooth": True, "lineStyle": {"width": 2.5, "type": "dashed"}}]}
    known = sorted(v for v in phone if v is not None)
    top = max(1000, round(known[int(0.95 * (len(known) - 1))] / 1000) * 1000) if known else 10000
    calendar = {"tooltip": {"position": "top"},
                "visualMap": {"min": 0, "max": top, "orient": "horizontal", "left": "center", "bottom": 0,
                              "itemHeight": 140, "itemWidth": 10, "text": [f"{_fmt(top)}+", "0"],
                              "textStyle": {"color": "@muted", "fontSize": 11},
                              "inRange": {"color": ["@lane", "@accent"]}},
                "calendar": {"range": [start, end], "top": 22, "left": 34, "right": 8, "cellSize": ["auto", 15],
                             "orient": "horizontal", "splitLine": {"show": False},
                             "itemStyle": {"color": "@surface", "borderColor": "@surface", "borderWidth": 2},
                             "yearLabel": {"show": False},
                             "dayLabel": {"firstDay": 1, "fontSize": 10, "color": "@muted",
                                          "nameMap": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]},
                             "monthLabel": {"fontSize": 11, "color": "@muted"}},
                "series": [{"type": "heatmap", "coordinateSystem": "calendar", "name": "phone-counted steps",
                            "data": [[d, v] for d, v in zip(dates, phone) if v is not None]}]}
    return [
        {"kind": "series", "title": f"Daily steps, {start} → {end}",
         "subtitle": f"From {tools.WATCH_START} the watch adds steps the phone missed (shaded), so the total jumps "
                     f"partly for measurement reasons. Before that the two averages are the same line; the dashed "
                     f"phone-only line is the one that's comparable across the whole window.",
         "caption": "Source: daily.steps (merged total) and daily.steps_phone.", "evidence": [], "height": 280,
         "option": trend},
        {"kind": "calendar", "title": "Phone-counted steps per day",
         "subtitle": "The one step count that's comparable across the whole window. Stronger colour = more steps.",
         "caption": "Source: daily.steps_phone.", "evidence": [], "height": 176, "option": calendar},
    ]
