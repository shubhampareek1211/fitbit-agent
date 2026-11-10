from pulse.evidence import EvidenceLedger
from pulse.verifier import verify

LIMITS = "\n\n## What your data can't tell you\nThis is not medical advice and covers only 7 nights."


def ledger():
    l = EvidenceLedger()
    l.add(tool="summarize_metric", args={}, table="daily", date_range=("2026-11-01", "2026-11-07"), n=7,
          values={"mean": 446.4, "total": 12382.0, "max": 557.0})
    l.add(tool="check_guideline", args={}, table="daily", date_range=("2026-11-01", "2026-11-07"), n=7,
          values={"periods_meeting": 5, "share_meeting_pct": 71.4, "target": 420.0})
    return l


def test_passes_with_rounding_unit_conversion_and_commas():
    report = ("You averaged 446 minutes (about 7.4 hours) of sleep per night [E1]. "
              "You met the 7-hour target on 5 of 7 nights (71%) [E2]. Your step total was 12,382 [E1]." + LIMITS)
    r = verify(report, ledger())
    assert r.passed, r.problems
    assert r.cited_ids == ["E1", "E2"]


def test_multi_id_citation_block():
    r = verify("You met the target on 5 nights and averaged 446 minutes [E1, E2]." + LIMITS, ledger())
    assert r.passed, r.problems
    assert r.cited_ids == ["E1", "E2"]


def test_flags_wrong_number_unknown_id_and_uncited_number():
    report = ("You averaged 480 minutes of sleep [E1]. Deep sleep was high [E9]. "
              "You slept 9 hours on Friday." + LIMITS)
    problems = " | ".join(verify(report, ledger()).problems)
    assert "480 is not in the cited evidence" in problems
    assert "Unknown citation(s) ['E9']" in problems
    assert "without a citation" in problems


def test_dates_are_not_numbers_and_limits_section_required():
    r = verify("Between 2026-11-01 and Nov 7, 2026 you averaged 446.4 minutes [E1].", ledger())
    assert r.problems == ['Missing a "What your data can\'t tell you" section.']


def test_month_year_and_iso_month_are_dates_not_numbers():
    report = "Data before November 2026 and in 2026-10 is phone-only; you averaged 446 minutes [E1]." + LIMITS
    assert verify(report, ledger()).passed


def test_blocks_diagnosis():
    r = verify("Your data suggests you have sleep apnea." + LIMITS, ledger())
    assert any("Diagnostic language" in p for p in r.problems)
