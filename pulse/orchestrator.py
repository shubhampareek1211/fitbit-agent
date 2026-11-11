"""Deep-research orchestration in plain asyncio. Every cap is enforced here, in code.

Gatekeeper -> Lead (plan) -> parallel Researchers (fresh sessions) -> Lead (gap review, <= MAX_ROUNDS)
-> Writer -> Verifier (-> Writer retry) -> report with sources.
"""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from pulse import agents, config
from pulse.charts import build_charts
from pulse.data import asof
from pulse.evidence import EvidenceLedger, new_ledger
from pulse.schemas import Brief, Finding, LeadTurn, VerifyResult
from pulse.verifier import verify

Emit = Callable[[dict], Awaitable[None]] | None
PENDING: dict[str, dict] = {}  # session_id -> pending clarification (single instance on Cloud Run)


@dataclass
class Result:
    response: str
    session_id: str
    tool_calls: list[dict] = field(default_factory=list)
    status: str = "answer"
    brief: dict | None = None
    findings: list[dict] = field(default_factory=list)
    verify: dict | None = None
    rounds: int = 0
    researchers: int = 0
    seconds: float = 0.0
    charts: list[dict] = field(default_factory=list)


def _short(x, limit=600) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


async def run_agent(agent, prompt: str, kind: str, trace: list, emit: Emit, timeout: float):
    """Run one ADK agent in its own session; record tool calls into `trace` with the agent's name."""
    svc = InMemorySessionService()
    runner = Runner(agent=agent, app_name="pulse", session_service=svc)
    session = await svc.create_session(app_name="pulse", user_id="pulse")
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    final, pending = None, {}

    async def _loop():
        nonlocal final
        async for ev in runner.run_async(user_id="pulse", session_id=session.id, new_message=msg,
                                         run_config=RunConfig(max_llm_calls=config.MAX_LLM_CALLS[kind])):
            for fc in ev.get_function_calls() or []:
                pending[fc.id] = {"agent": ev.author, "name": fc.name, "args": dict(fc.args or {})}
                if emit:
                    await emit({"type": "activity", "agent": ev.author,
                                "text": f"{fc.name}({_short(dict(fc.args or {}), 120)})"})
            for fr in ev.get_function_responses() or []:
                call = pending.pop(fr.id, {"agent": ev.author, "name": fr.name, "args": {}})
                trace.append({**call, "result": _short(fr.response)})
            if ev.is_final_response() and ev.content and ev.content.parts:
                final = "".join(p.text or "" for p in ev.content.parts if not getattr(p, "thought", False))

    await asyncio.wait_for(_loop(), timeout=timeout)
    return final or ""


def _parse(model: type[BaseModel], text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return model.model_validate_json(text)


def _sources(report: str, ledger: EvidenceLedger, cited: list[str]) -> str:
    if not cited:
        return report
    return report.rstrip() + "\n\n## Sources\n" + "\n".join(f"- {ledger.get(i).footnote()}" for i in cited)


async def gate(message: str, prior: dict | None, trace: list, emit: Emit = None) -> Brief:
    """Run only the Gatekeeper (also used on its own by the gatekeeper eval)."""
    prompt = message if not prior else (
        f"Earlier the user asked: {prior['question']}\nYou asked them: {prior['clarification']}\n"
        f"Their reply: {message}")
    return _parse(Brief, await run_agent(agents.gatekeeper(), prompt, "gatekeeper", trace, emit,
                                         config.AGENT_TIMEOUT_S["gatekeeper"]))


async def answer(message: str, session_id: str | None = None, emit: Emit = None) -> Result:
    t0 = time.monotonic()
    session_id = session_id or str(uuid.uuid4())
    ledger = new_ledger()
    trace: list[dict] = []
    remaining = lambda: config.GLOBAL_DEADLINE_S - (time.monotonic() - t0)

    async def stage(agent_name: str, text: str):
        if emit:
            await emit({"type": "activity", "agent": agent_name, "text": text})

    # 1. Gatekeeper ---------------------------------------------------------------------------
    prior = PENDING.pop(session_id, None)
    await stage("Gatekeeper", "checking whether the data can answer this")
    brief = await gate(message, prior, trace, emit)
    if brief.status == "clarify":
        PENDING[session_id] = {"question": message if not prior else prior["question"] + " / " + message,
                               "clarification": brief.clarification}
        return Result(response=brief.clarification or "Could you say more about what you want to know?",
                      session_id=session_id, tool_calls=trace, status="clarify", brief=brief.model_dump(),
                      seconds=time.monotonic() - t0)
    if brief.status == "refuse":
        return Result(response=(brief.limitation or "Your data can't answer that question.")
                      + f"\n\n_Your data runs through {asof()}. Pulse describes data; it is not medical advice._",
                      session_id=session_id, tool_calls=trace, status="refuse", brief=brief.model_dump(),
                      seconds=time.monotonic() - t0)

    # 2. Lead <-> Researchers rounds ---------------------------------------------------------------
    findings: list[Finding] = []
    launched, rounds, lead_retried = 0, 0, False
    sem = asyncio.Semaphore(config.MAX_PARALLEL)
    brief_text = brief.model_dump_json(exclude_none=True)

    async def research(task, n):
        async with sem:
            budget = min(config.AGENT_TIMEOUT_S["researcher"], max(remaining() - 70, 15))
            text = await run_agent(agents.researcher(n), agents.researcher_prompt(task), "researcher", trace, emit,
                                   budget)
            f = _parse(Finding, text)
            f.subtask_id = task.id
            return f

    while rounds < config.MAX_ROUNDS and launched < config.MAX_RESEARCHERS and remaining() > 80:
        rounds += 1
        lead_prompt = f"Brief: {brief_text}\nRound {rounds} of at most {config.MAX_ROUNDS}. " \
                      f"Researchers left: {config.MAX_RESEARCHERS - launched}."
        if findings:
            lead_prompt += "\nFindings so far:\n" + json.dumps(
                [f.model_dump(exclude_none=True) for f in findings], indent=1)
        await stage("Lead", "planning" if rounds == 1 else f"reviewing findings (round {rounds})")
        try:
            turn = _parse(LeadTurn, await run_agent(agents.lead(), lead_prompt, "lead", trace, emit,
                                                    min(config.AGENT_TIMEOUT_S["lead"], max(remaining() - 40, 15))))
        except Exception as e:  # a hung or unparseable Lead turn must not throw away the findings already made
            trace.append({"agent": "Lead", "name": "plan" if rounds == 1 else "review", "args": {"round": rounds},
                          "result": f"failed: {type(e).__name__}: {str(e)[:200]}"})
            if findings:  # review failed: write the report from what the researchers found
                await stage("Lead", "review failed; writing from the findings so far")
                break
            if lead_retried or remaining() <= 80:  # planning failed twice, or no time to retry: nothing to write from
                raise
            lead_retried, rounds = True, rounds - 1
            continue
        trace.append({"agent": "Lead", "name": "plan" if rounds == 1 else "review",
                      "args": {"round": rounds}, "result": _short(turn.model_dump())})
        tasks = turn.subtasks[: config.MAX_RESEARCHERS - launched]
        if turn.done or not tasks:
            break
        # The Lead has no tools: it returns a plan (LeadTurn) and this code dispatches it. These "Lead -> research"
        # entries are dispatch records written here, so the fan-out and any follow-up round show up in tool_calls.
        for i, t in enumerate(tasks):
            trace.append({"agent": "Lead", "name": "research",
                          "args": {"round": rounds, "subtask": t.id, "question": t.question, "start": t.start,
                                   "end": t.end},
                          "result": f"dispatched to Researcher_{launched + 1 + i} " + (
                              "(parallel)" if rounds == 1 else f"(follow-up after review: {_short(turn.reasoning, 240)})")})
        await stage("Lead", f"dispatching {len(tasks)} researchers in parallel" if rounds == 1
                    else f"gap found on review; sending {len(tasks)} follow-up researcher(s)")
        results = await asyncio.gather(*(research(t, launched + 1 + i) for i, t in enumerate(tasks)),
                                       return_exceptions=True)
        for t, r in zip(tasks, results):
            if isinstance(r, BaseException):  # surface the failure; never let it silently disappear
                findings.append(Finding(subtask_id=t.id, summary=f"Research failed for: {t.question}",
                                        error=f"{type(r).__name__}: {str(r)[:200]}",
                                        data_gaps=[f"Could not answer: {t.question}"]))
            else:
                findings.append(r)
        launched += len(tasks)

    # 3. Writer <-> Verifier -------------------------------------------------------------------------
    problems, report, result = None, "", VerifyResult(passed=False)
    for attempt in range(config.WRITER_RETRIES + 1):
        await stage("Writer", "writing the report" if attempt == 0 else "fixing verification problems")
        try:
            draft = await run_agent(agents.writer(), agents.writer_prompt(brief, findings, ledger, problems),
                                    "writer", trace, emit, min(config.AGENT_TIMEOUT_S["writer"], max(remaining(), 30)))
        except Exception as e:
            trace.append({"agent": "Writer", "name": "write", "args": {"attempt": attempt + 1},
                          "result": f"failed: {type(e).__name__}: {str(e)[:200]}"})
            if report:  # the rewrite failed: keep the first draft, with its verification note
                break
            if attempt < config.WRITER_RETRIES and remaining() > 30:
                continue  # the first draft failed: one more try
            raise
        report = draft
        result = verify(report, ledger)
        trace.append({"agent": "Verifier", "name": "verify", "args": {"attempt": attempt + 1},
                      "result": _short(result.model_dump())})
        await stage("Verifier", "all citations and numbers check out" if result.passed
                    else f"{len(result.problems)} problem(s) found")
        if result.passed or remaining() < 30:
            break
        problems = result.problems
    if not result.passed:
        report += ("\n\n> **Verification note:** some statements could not be matched to the evidence: "
                   + "; ".join(result.problems[:3]))
    report = _sources(report, ledger, result.cited_ids)
    # Charts are drawn by code from the evidence the report cites, so they show the same verified numbers.
    try:
        charts = build_charts(report, ledger)
    except Exception:  # a chart must never cost the user their answer
        charts = []
    return Result(response=report, session_id=session_id, tool_calls=trace, status="answer",
                  brief=brief.model_dump(), findings=[f.model_dump() for f in findings],
                  verify=result.model_dump(), rounds=rounds, researchers=launched,
                  seconds=round(time.monotonic() - t0, 1), charts=charts)
