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


def test_workouts_summary_visits_and_types():
    led = new_ledger()
    r = tools.workouts("2026-08-03", "2026-08-30")
    s = r["summary"]
    assert (s["workouts"], s["total_minutes"], s["light_min"], s["moderate_min"], s["vigorous_min"], s["peak_min"]) \
        == (4, 80.0, 40.0, 10.0, 15.0, 15.0)
    assert (s["moderate_or_higher_min"], s["started_on_watch"], s["detected_automatically"]) == (40.0, 2, 2)
    assert r["visits"] == [{"visit": 1, "start": "2026-08-29 18:00", "end": "19:00", "minutes": 60.0, "workouts": 3,
                            "types": ["Walk", "Run", "Elliptical"]}]
    assert r["by_type"]["Walk"]["workouts"] == 2 and r["by_type"]["Walk"]["minutes"] == 50.0
    assert "recording_note" in r and "zone_note" in r  # range starts before the first workout
    ev = led.get(r["evidence_id"])
    assert ev.values["peak_min[2026-08-29 18:50 Elliptical]"] == 10.0 and ev.footnote().endswith("n=4 workouts")


def test_workouts_filter_and_errors():
    assert tools.workouts("2026-08-29", "2026-08-30", "running")["summary"]["workouts"] == 1
    assert tools.workouts("2026-08-29", "2026-08-30", "RUNNING")["summary"]["workouts"] == 1
    assert "Types recorded" in tools.workouts("2026-08-29", "2026-08-30", "swimming")["error"]
    assert "records workouts from 2026-08-29" in tools.workouts("2026-08-03", "2026-08-10")["error"]


def test_workout_heart_rate_covers_the_whole_visit():
    led = new_ledger()
    h = tools.workout_heart_rate("2026-08-29", "18:26")  # any workout of the visit, start time within 5 minutes
    assert (h["visit_start"], h["visit_end"], h["visit_minutes"], h["samples"]) == ("18:00", "19:00", 60.0, 360)
    walk, run, ell = h["workouts"]
    assert (walk["median_hr"], run["median_hr"], run["min_hr"], run["max_hr"], ell["median_hr"]) == \
        (110.0, 165.0, 160.0, 170.0, 180.0)
    assert run["fitbit_avg_hr"] == 165.0 and "hr_note" in h
    assert led.get(h["evidence_id"]).values["median_hr[2026-08-29 18:25 Run]"] == 165.0
    assert "Workouts that day: 18:00 Walk" in tools.workout_heart_rate("2026-08-29", "12:00")["error"]
    assert "No heart-rate readings" in tools.workout_heart_rate("2026-08-30", "07:00")["error"]


def test_visits_and_heart_rate_steps_in_the_build():
    from datetime import datetime
    from build.normalize import assign_visits, heart_rate_steps
    ws = [{"start": f"2026-11-01T{a}:00-05:00", "end": f"2026-11-01T{b}:00-05:00"}
          for a, b in (("18:00", "18:20"), ("18:29", "18:40"), ("18:55", "19:00"))]  # gaps 9 and 15 minutes
    assign_visits(ws)
    assert [w["visit"] for w in ws] == [1, 1, 2]
    t0 = datetime.fromisoformat("2026-11-01T18:00:00-05:00")
    readings = [(t0.replace(second=s), b) for s, b in ((1, 100), (5, 110), (12, 120), (59, 90))]
    assert heart_rate_steps(readings, t0, t0.replace(second=30)) == [(0, 105.0), (10, 120.0)]


def test_weekly_guideline_evidence_names_the_judged_days():
    ledger = new_ledger()
    r = tools.check_guideline("azm_total", "2026-08-15", "2026-08-30")  # 16 days: only the last 14 form full weeks
    ev = ledger.get(r["evidence_id"])
    assert r["judged_range"] == ["2026-08-17", "2026-08-30"] and ev.date_range == ("2026-08-17", "2026-08-30")
    assert ev.values["total[2026-08-24..2026-08-30]"] == 210.0 and ev.values["days_with_data_in_judged_weeks"] == 14


def test_correlate_accepts_a_negative_lag():
    r = tools.correlate("steps", "temp_mean_c", "2026-08-03", "2026-08-30", lag_days=-1)
    assert "error" not in r and r["n_pairs"] == 27


def test_zero_days_are_filled_only_where_the_device_was_recording():
    from build.normalize import fill_zero_days
    rows = {"2026-08-01": {"steps": 900.0},                                   # before floors exist: stays empty
            "2026-08-02": {"steps": 500.0, "floors": 3.0},
            "2026-08-03": {"steps": 100.0},                                   # phone carried, no floors -> 0
            "2026-11-06": {"steps": 800.0, "hr_avg": 71.5, "sleep_minutes": 392.0},  # watch worn, nothing in a zone
            "2026-11-07": {"steps": 700.0, "hr_avg": 70.8, "sleep_minutes": 430.0, "azm_total": 3.0, "nap_minutes": 70.0}}
    fill_zero_days(rows)
    assert "floors" not in rows["2026-08-01"] and rows["2026-08-03"]["floors"] == 0.0
    assert "azm_total" not in rows["2026-08-03"] and "nap_minutes" not in rows["2026-08-03"]
    assert rows["2026-11-06"]["azm_total"] == 0.0 and rows["2026-11-06"]["nap_minutes"] == 0.0
    assert rows["2026-11-06"]["exercise_sessions"] == 0.0
    assert rows["2026-11-07"]["azm_total"] == 3.0 and rows["2026-11-07"]["nap_minutes"] == 70.0


def test_refresh_refetches_files_fetched_before_their_day_ended(tmp_path):
    import os
    from datetime import date, datetime
    from build import refresh
    assert refresh.is_stale(tmp_path / "2026-11-08.json", datetime(2026, 11, 8, 23, 0))        # fetched at 23:00 that day
    assert not refresh.is_stale(tmp_path / "2026-11-08.json", datetime(2026, 11, 9, 11, 0))    # fetched the next morning
    assert refresh.is_stale(tmp_path / "steps__2026-10-30__2026-11-08.json", datetime(2026, 11, 8, 4, 40))
    steps = tmp_path / "intraday" / "steps"; steps.mkdir(parents=True); (tmp_path / "daily").mkdir()
    for name, when in (("intraday/steps/2026-11-07.json", datetime(2026, 11, 8, 4, 40)),
                       ("intraday/steps/2026-11-08.json", datetime(2026, 11, 8, 4, 40)),
                       ("daily/steps__2026-10-30__2026-11-08.json", datetime(2026, 11, 8, 4, 40))):
        f = tmp_path / name; f.write_text("{}"); os.utime(f, (when.timestamp(), when.timestamp()))
    dropped = refresh.drop_stale(tmp_path)
    assert sorted(f.name for f in dropped) == ["2026-11-08.json", "steps__2026-10-30__2026-11-08.json"]
    # the dropped rollup covered 10-30..11-08, so the export has to start again at 10-30
    assert refresh.export_from(tmp_path, date(2026, 11, 11), dropped) == date(2026, 10, 30)
