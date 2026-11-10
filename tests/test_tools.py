from pulse import stats, tools
from pulse.evidence import new_ledger


def test_summarize_exact_values():
    ledger = new_ledger()
    r = tools.summarize_metric("steps", "2026-08-03", "2026-08-09")  # first week: 1000..7000
    assert r["mean"] == 4000.0 and r["total"] == 28000.0
    assert (r["min"], r["min_date"]) == (1000.0, "2026-08-03")
    assert (r["max"], r["max_date"]) == (7000.0, "2026-08-09")
    assert ledger.get(r["evidence_id"]).values["mean"] == 4000.0


def test_dates_clamped_and_out_of_range_is_admitted():
    r = tools.summarize_metric("steps", "2026-07-01", "2026-08-04")
    assert r["start"] == "2026-08-03" and "clamped" in r["note"]
    r = tools.summarize_metric("steps", "2025-01-01", "2025-01-31")
    assert "error" in r and "Data covers" in r["error"]


def test_unknown_metric_is_an_error_not_an_exception():
    assert "Unknown metric" in tools.summarize_metric("blood_glucose", "2026-08-03", "2026-08-30")["error"]


def test_compare_periods_direction_and_ci():
    r = tools.compare_periods("steps", "2026-08-03", "2026-08-09", "2026-08-24", "2026-08-30")
    assert r["mean_a"] == 4000.0 and r["mean_b"] == 25000.0 and r["difference"] == 21000.0
    assert r["verdict"] == "higher" and r["ci95_difference"][0] > 0


def test_compare_groups_rain():
    r = tools.compare_groups("steps", "rain", "2026-08-03", "2026-08-30")
    groups = {g["group"]: g for g in r["groups"]}
    assert groups["dry"]["n_days"] == 14 and groups["rainy (>= 1.0 mm)"]["n_days"] == 14
    assert groups["rainy (>= 1.0 mm)"]["mean"] - groups["dry"]["mean"] == 1000.0


def test_correlate_perfect_and_insufficient():
    r = tools.correlate("temp_mean_c", "steps", "2026-08-03", "2026-08-30")
    assert r["rho"] == 1.0 and r["verdict"] == "positive" and r["n_pairs"] == 28
    r = tools.correlate("steps", "sleep_minutes", "2026-08-25", "2026-08-30")
    assert r["verdict"] == "insufficient_data" and r["rho"] is None


def test_correlate_lag_pairs_next_day():
    r = tools.correlate("steps", "sleep_minutes", "2026-08-17", "2026-08-30", lag_days=1)
    assert r["n_pairs"] == 13  # sleep exists from day 14; the last day has no next day


def test_guideline_daily_and_weekly():
    r = tools.check_guideline("sleep_minutes", "2026-08-17", "2026-08-30")
    assert r["periods_judged"] == 14 and r["periods_meeting"] == 7 and r["share_meeting_pct"] == 50.0
    r = tools.check_guideline("azm_total", "2026-08-17", "2026-08-30")  # two full weeks of 7*30 = 210
    assert r["periods_judged"] == 2 and r["periods_meeting"] == 2 and r["average_per_period"] == 210.0
    assert "error" in tools.check_guideline("floors", "2026-08-03", "2026-08-30")


def test_days_before_the_watch_are_not_missing(monkeypatch):
    monkeypatch.setattr(tools, "WATCH_FIRST_DAY", "2026-08-17")  # fixture: watch metrics start on day 14
    ledger = new_ledger()
    r = tools.summarize_metric("azm_total", "2026-08-03", "2026-08-30")
    assert (r["days_before_watch"], r["days_missing"], r["days_with_data"]) == (14, 0, 14)
    assert "not missing data" in r["recording_note"]
    assert ledger.get(r["evidence_id"]).values["days_before_watch"] == 14
    assert "days_before_watch" not in tools.summarize_metric("steps", "2026-08-03", "2026-08-30")
    assert "before the watch" in tools.summarize_metric("azm_total", "2026-08-03", "2026-08-10")["error"]
    assert "not missing data" in tools.check_guideline("azm_total", "2026-08-03", "2026-08-30")["recording_note"]


def test_measurement_note_counts_days_on_each_side(monkeypatch):
    monkeypatch.setattr(tools, "WATCH_START", "2026-08-24")
    note = tools.compare_groups("steps", "rain", "2026-08-03", "2026-08-30")["measurement_note"]
    assert "21 phone-only day(s)" in note and "with 7 day(s)" in note
    assert tools.compare_groups("steps", "rain", "2026-08-03", "2026-08-20")["measurement_note"] is None
    assert "all 7 day(s)" in tools.summarize_metric("steps", "2026-08-24", "2026-08-30")["measurement_note"]
    note = tools.correlate("temp_mean_c", "floors", "2026-08-03", "2026-08-30")["measurement_note"]
    assert "floors jumps at 2026-08-24" in note and "no phone-only version" in note


def test_stats_verdicts():
    assert stats.compare([1, 2], [3, 4])["verdict"] == "insufficient_data"
    assert stats.spearman(list(range(9)), list(range(9)))["verdict"] == "insufficient_data"
    assert stats.spearman(list(range(30)), [(-1) ** i for i in range(30)])["verdict"] == "inconclusive"
