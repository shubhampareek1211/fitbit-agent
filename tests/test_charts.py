"""Charts are built by code from cited evidence: every plotted aggregate must be the ledger's own number."""
import json

from pulse import charts, tools
from pulse.evidence import new_ledger


def _cite(*results) -> str:
    return " ".join(f"A claim [{r['evidence_id']}]." for r in results) + "\n\n## Sources\n- [E99] ignored"


def _no_functions(obj) -> bool:
    return json.loads(json.dumps(obj)) == obj  # plain JSON only: nothing a page would have to execute


def test_group_bars_are_the_ledger_means():
    led = new_ledger()
    r = tools.compare_groups("steps", "weekday", "2026-08-03", "2026-08-30")
    [c] = charts.build_charts(_cite(r), led)
    bars = [d["value"] for d in c["option"]["series"][0]["data"]]
    ev = led.get(r["evidence_id"])
    assert bars == [v for k, v in ev.values.items() if k.startswith("mean[")]
    assert c["option"]["yAxis"]["min"] == 0 and c["evidence"] == [r["evidence_id"]]
    assert "highest Sun" in c["subtitle"] and _no_functions(c)


def test_same_grouping_metrics_merge_into_one_chart(monkeypatch):
    monkeypatch.setattr(tools, "WATCH_START", "2026-08-24")
    led = new_ledger()
    a = tools.compare_groups("steps", "device_era", "2026-08-03", "2026-08-30")
    b = tools.compare_groups("steps_phone", "device_era", "2026-08-03", "2026-08-30")
    [c] = charts.build_charts(_cite(a, b), led)
    assert [s["name"] for s in c["option"]["series"]] == ["steps", "phone-counted steps"]
    assert c["evidence"] == [a["evidence_id"], b["evidence_id"]]
    assert "Clearly higher" in c["subtitle"]


def test_identical_series_is_noted_not_drawn_twice():
    led = new_ledger()
    vals = {"mean[dry]": 4000.0, "mean[rainy (>= 1.0 mm)]": 4100.0, "n[dry]": 60, "n[rainy (>= 1.0 mm)]": 39}
    for m in ("steps", "steps_phone"):  # before the watch, both counts are the same numbers
        led.add(tool="compare_groups", args={"metric": m, "group_by": "rain", "start": "2026-08-03",
                                             "end": "2026-08-30"}, table="daily",
                date_range=("2026-08-03", "2026-08-30"), n=99, values=dict(vals))
    [c] = charts.build_charts("Claims [E1] and [E2].", led)
    assert len(c["option"]["series"]) == 1 and c["evidence"] == ["E1", "E2"]
    assert "phone-counted steps is identical to steps" in c["subtitle"]


def test_guideline_target_line_and_colours_match_the_ledger():
    led = new_ledger()
    r = tools.check_guideline("sleep_minutes", "2026-08-17", "2026-08-30")
    [c] = charts.build_charts(_cite(r), led)
    s = c["option"]["series"][0]
    ev = led.get(r["evidence_id"])
    assert s["markLine"]["data"][0]["yAxis"] == ev.values["target"]
    met = sum(1 for d in s["data"] if d["itemStyle"]["color"] == "@ok")
    assert met == ev.values["periods_meeting"] and len(s["data"]) == ev.values["periods_judged"]


def test_scatter_plots_exactly_the_correlated_pairs():
    led = new_ledger()
    r = tools.correlate("temp_mean_c", "steps", "2026-08-03", "2026-08-30")
    [c] = charts.build_charts(_cite(r), led)
    assert sum(len(s["data"]) for s in c["option"]["series"]) == r["n_pairs"]
    assert "ρ = +1.00" in c["subtitle"]


def test_summary_mean_line_and_watch_shading(monkeypatch):
    monkeypatch.setattr(tools, "WATCH_START", "2026-08-24")
    led = new_ledger()
    r = tools.summarize_metric("steps", "2026-08-03", "2026-08-30")
    [c] = charts.build_charts(_cite(r), led)
    s = c["option"]["series"][0]
    assert s["markLine"]["data"][0]["yAxis"] == r["mean"]
    assert s["markArea"]["data"][0][0]["xAxis"] == "08-24"


def test_order_dedupe_cap_and_uncharted_tools():
    led = new_ledger()
    cov = tools.get_coverage()
    r1 = tools.summarize_metric("steps", "2026-08-03", "2026-08-09")
    r1b = tools.summarize_metric("steps", "2026-08-03", "2026-08-09")  # same call twice -> one chart
    r2 = tools.correlate("temp_mean_c", "steps", "2026-08-03", "2026-08-30")
    r3 = tools.compare_groups("steps", "rain", "2026-08-03", "2026-08-30")
    r4 = tools.check_guideline("sleep_minutes", "2026-08-17", "2026-08-30")
    out = charts.build_charts(_cite(r2, cov, r1, r1b, r3, r4), led)
    assert [c["evidence"][0] for c in out] == [r2["evidence_id"], r1["evidence_id"], r3["evidence_id"]]


def test_overview_charts_are_plain_json():
    out = charts.overview()
    assert [c["kind"] for c in out] == ["series", "calendar"] and all(_no_functions(c) for c in out)


def test_workout_effort_bars_are_the_ledger_zone_minutes():
    led = new_ledger()
    r = tools.workouts("2026-08-29", "2026-08-30")
    effort, timeline = charts.build_charts(_cite(r), led)
    ev = led.get(r["evidence_id"])
    keys = [w["key"] for w in ev.context["workouts"]]
    for s in effort["option"]["series"]:
        assert s["data"] == [ev.values[f"{s['name']}_min[{k}]"] for k in keys]
    assert len(effort["table"]["rows"]) == 4 and "n = 4 workouts" in effort["caption"] and _no_functions(effort)


def test_workout_timeline_blocks_match_the_workouts():
    led = new_ledger()
    r = tools.workouts("2026-08-29", "2026-08-30")
    _, tl = charts.build_charts(_cite(r), led)
    blocks = {d["name"]: d for s in tl["option"]["series"] for d in s["data"]}
    assert sum(len(s["data"]) for s in tl["option"]["series"]) == 4
    assert tl["option"]["yAxis"]["data"] == ["08-29 Sat", "08-30 Sun"]
    assert blocks["Run"]["value"][:3] == [0, 18.417, 18.75] and blocks["Run"]["itemStyle"]["opacity"] == 1
    assert {s["name"] for s in tl["option"]["series"]} == {"Walk", "Run", "Elliptical"}
    assert all(s["renderItem"] == "timeline" for s in tl["option"]["series"])


def test_heart_rate_trace_plots_every_step_and_the_medians():
    led = new_ledger()
    h = tools.workout_heart_rate("2026-08-29", "18:50")
    [c] = charts.build_charts(_cite(h), led)
    s = c["option"]["series"][0]
    ev = led.get(h["evidence_id"])
    assert len(s["data"]) == ev.values["samples"] == 360
    assert [m[0]["coord"][1] for m in s["markLine"]["data"]] == [110.0, 165.0, 180.0]
    assert [a[0]["xAxis"] for a in s["markArea"]["data"]] == [0.0, 25.0, 50.0]


def test_no_guideline_chart_when_no_week_could_be_judged():
    led = new_ledger()
    r = tools.check_guideline("azm_total", "2026-08-26", "2026-08-30")  # 5 days: no full 7-day block
    assert r["periods_judged"] == 0 and charts.build_charts(_cite(r), led) == []


def test_raw_change_across_the_watch_start_is_not_called_clearly_higher(monkeypatch):
    monkeypatch.setattr(tools, "WATCH_START", "2026-08-17")
    led = new_ledger()
    a = tools.compare_periods("steps", "2026-08-03", "2026-08-16", "2026-08-17", "2026-08-30")
    b = tools.compare_groups("steps", "device_era", "2026-08-03", "2026-08-30")
    c = tools.compare_periods("steps_phone", "2026-08-03", "2026-08-16", "2026-08-17", "2026-08-30")
    d = tools.compare_periods("steps", "2026-08-03", "2026-08-09", "2026-08-10", "2026-08-16")  # both before the watch
    pa, pb, pc, pd = charts.build_charts(_cite(a, b, c, d), led, limit=4)
    for chart in (pa, pb):
        assert "Clearly" not in chart["subtitle"] and "partly measurement" in chart["subtitle"]
    for chart in (pc, pd):
        assert "Clearly higher" in chart["subtitle"] and "measurement" not in chart["subtitle"]
