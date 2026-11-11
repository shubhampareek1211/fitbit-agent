"""Failure handling in the orchestrator, with the agents stubbed out (no model calls)."""
import asyncio
import json

import pytest

from pulse import orchestrator, tools

REPORT = "Mean was {mean} [{eid}].\n\n## What your data can't tell you\nNot medical advice."


def _stub(monkeypatch, fail):
    """fail(kind, n) -> True makes the n-th call of that agent kind time out."""
    seen = {"gatekeeper": 0, "lead": 0, "researcher": 0, "writer": 0}
    made = {}

    async def run_agent(agent, prompt, kind, trace, emit, timeout):
        seen[kind] += 1
        if fail(kind, seen[kind]):
            raise asyncio.TimeoutError()
        if kind == "gatekeeper":
            return json.dumps({"status": "answer", "question": "q", "metrics": ["steps"]})
        if kind == "lead":
            return json.dumps({"done": False, "reasoning": "r", "subtasks": [
                {"id": "S1", "question": "q", "metrics": ["steps"], "start": "2026-08-03", "end": "2026-08-09"}]})
        if kind == "researcher":
            r = made["r"] = tools.summarize_metric("steps", "2026-08-03", "2026-08-09")
            return json.dumps({"subtask_id": "S1", "summary": "s", "claims": [
                {"text": "mean", "value": r["mean"], "evidence_ids": [r["evidence_id"]]}]})
        return REPORT.format(mean=made["r"]["mean"], eid=made["r"]["evidence_id"])

    monkeypatch.setattr(orchestrator, "run_agent", run_agent)
    return seen


def test_a_failed_lead_review_still_writes_from_the_findings(monkeypatch):
    _stub(monkeypatch, lambda kind, n: kind == "lead" and n == 2)
    r = asyncio.run(orchestrator.answer("q"))
    assert r.status == "answer" and r.verify["passed"] and len(r.findings) == 1
    assert any(c["agent"] == "Lead" and str(c["result"]).startswith("failed") for c in r.tool_calls)


def test_a_failed_first_plan_is_retried_once(monkeypatch):
    seen = _stub(monkeypatch, lambda kind, n: kind == "lead" and n == 1)
    r = asyncio.run(orchestrator.answer("q"))
    assert r.status == "answer" and seen["researcher"] >= 1
    _stub(monkeypatch, lambda kind, n: kind == "lead")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(orchestrator.answer("q"))


def test_a_failed_first_draft_is_retried(monkeypatch):
    _stub(monkeypatch, lambda kind, n: kind == "writer" and n == 1)
    r = asyncio.run(orchestrator.answer("q"))
    assert r.status == "answer" and r.verify["passed"]
