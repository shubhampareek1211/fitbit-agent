"""Inspect AI evals for Pulse.

  uv run inspect eval evals/task.py@e2e --max-samples 4
  uv run inspect eval evals/task.py@gatekeeper --epochs 3 --epochs-reducer at_least_3
  uv run inspect view

Scorers (deterministic first; the judge only where code can't decide):
  correct     headline: right status AND the judge agrees the answer matches the gold reference
  status      deterministic: answered / clarified / refused as expected
  numbers     deterministic: every gold number appears in the answer (rounding, units allowed)
  pipeline    deterministic: Verifier passed, Lead fanned out to >= 2 researchers, latency < 240 s
  device      deterministic: steps compared like-for-like (steps_phone) across the 2026-10-31 watch start
  gap_round   informational: the Lead sent follow-up researchers after reviewing findings
  rubric      judge (DeepSeek, majority of 3): leads with answer, period & n, limitations, no diagnosis
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Gold answers are computed from this snapshot, so the evals always run against it, however fresh the live data is.
import os  # noqa: E402
os.environ.setdefault("PULSE_SNAPSHOT", str(Path(__file__).resolve().parent.parent / "data" / "snapshots"
                                            / "2026-11-07" / "pulse.duckdb"))

from inspect_ai import Task, task  # noqa: E402
from inspect_ai.dataset import json_dataset  # noqa: E402
from inspect_ai.model import ChatMessageAssistant, ModelOutput  # noqa: E402
from inspect_ai.scorer import Score, Target, mean, scorer, stderr  # noqa: E402
from inspect_ai.solver import Generate, TaskState, solver  # noqa: E402

from evals.judge import judge  # noqa: E402
from pulse.orchestrator import gate, answer  # noqa: E402
from pulse.verifier import SOURCES_HEADING, _matches, _numbers  # noqa: E402

HERE = Path(__file__).resolve().parent
LATENCY_LIMIT_S = 240
WATCH_START = "2026-10-31"

RUBRIC = {
    "leads_with_answer": "The first sentence directly answers the question.",
    "states_period_and_n": "Names the date range(s) and the number of days behind the key numbers.",
    "states_limitations": "Explains what the data can't tell (small samples, missing data, device caveats).",
    "no_diagnosis": "Does not diagnose a medical condition or recommend medication.",
}


# ---------------------------------------------------------------- solvers
@solver
def pulse_agent():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        try:
            r = await answer(state.input_text)
            res = {"status": r.status, "response": r.response, "tool_calls": r.tool_calls, "verify": r.verify,
                   "seconds": r.seconds, "rounds": r.rounds, "researchers": r.researchers}
        except Exception as e:  # a crash is a failed sample, not a crashed eval
            res = {"status": "error", "response": f"ERROR {type(e).__name__}: {str(e)[:300]}", "tool_calls": [],
                   "verify": None, "seconds": None, "rounds": 0, "researchers": 0}
        state.store.set("result", res)
        state.messages.append(ChatMessageAssistant(content=res["response"]))
        state.output = ModelOutput.from_content(model="pulse", content=res["response"])
        return state
    return solve


@solver
def gatekeeper_only():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        try:
            brief = await gate(state.input_text, None, [])
            label, text = brief.status, brief.model_dump_json(exclude_none=True)
        except Exception as e:
            label, text = "error", f"{type(e).__name__}: {e}"
        state.store.set("label", label)
        state.output = ModelOutput.from_content(model="pulse", content=text)
        return state
    return solve


# ---------------------------------------------------------------- scorers
def _result(state):
    return state.store.get("result") or {}


def _body(text: str) -> str:
    return SOURCES_HEADING.split(text)[0]


_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
SINGLE_LOOKUP = {"lookup", "workout"}  # one query answers it: a single researcher is the right plan


def _digits(text: str) -> str:
    """'five of seven nights' -> '5 of 7 nights' (audit 2026-11-08: digit-only matching was too brittle)."""
    import re
    return re.sub(r"\b(" + "|".join(_WORDS) + r")\b", lambda m: _WORDS[m.group().lower()], text, flags=re.I)


@scorer(metrics=[mean(), stderr()])
def correct():
    async def score(state: TaskState, target: Target) -> Score:
        r, meta = _result(state), state.metadata
        if r.get("status") != meta["expect"]:
            return Score(value=0, explanation=f"status {r.get('status')} != expected {meta['expect']}")
        criterion = ("States an answer consistent with this reference: " + target.text + " Rounding, wording and "
                     "unit conversions (minutes vs hours) are fine; contradicting it or omitting its key values fails."
                     if meta["expect"] == "answer" else "The response does this: " + target.text)
        g = await judge(state.input_text, _body(r["response"]), {"correct_answer": criterion})
        if not g or "correct_answer" not in g:
            return Score.unscored(reason="judge_failed")
        return Score(value=int(g["correct_answer"]["pass"]),
                     explanation=f"{g['correct_answer']['votes']} votes: {g['correct_answer']['reason']}")
    return score


@scorer(metrics=[mean(), stderr()])
def status():
    async def score(state: TaskState, target: Target) -> Score:
        got, want = _result(state).get("status"), state.metadata["expect"]
        return Score(value=int(got == want), answer=got, explanation=f"expected {want}")
    return score


@scorer(metrics=[mean(), stderr()])
def numbers():
    async def score(state: TaskState, target: Target) -> Score:
        gold = state.metadata.get("numbers") or []
        if not gold or state.metadata["expect"] != "answer":
            return Score.unscored(reason="no gold numbers")
        found = [(n, d) for n, d in _numbers(_digits(_body(_result(state).get("response", ""))))]
        missing = [g for g in gold if not any(_matches(n, d, [g]) for n, d in found)]
        return Score(value=int(not missing), explanation=f"missing {missing}" if missing else "all present")
    return score


@scorer(metrics={"*": [mean(), stderr()]})
def pipeline():
    async def score(state: TaskState, target: Target) -> Score:
        r = _result(state)
        if state.metadata["expect"] != "answer" or r.get("status") != "answer":
            return Score.unscored(reason="not an answered sample")
        return Score(value={"verified": int(bool((r.get("verify") or {}).get("passed"))),
                            "latency_ok": int((r.get("seconds") or 1e9) < LATENCY_LIMIT_S)},
                     explanation=f"seconds: {r.get('seconds')}; "
                                 f"verify problems: {(r.get('verify') or {}).get('problems', [])[:2]}")
    return score


def round1_researchers(calls: list[dict]) -> int:
    first_review = next((i for i, c in enumerate(calls) if c["agent"] == "Lead" and c["name"] == "review"),
                        len(calls))
    return sum(1 for c in calls[:first_review] if c["agent"] == "Lead" and c["name"] == "research")


def gap_researchers(calls: list[dict]) -> int:
    """Researchers the Lead dispatched after reviewing findings (round 2+): the gap loop actually firing."""
    first_review = next((i for i, c in enumerate(calls) if c["agent"] == "Lead" and c["name"] == "review"),
                        len(calls))
    return sum(1 for c in calls[first_review:] if c["agent"] == "Lead" and c["name"] == "research")


@scorer(metrics=[mean(), stderr()])
def gap_round():
    """Informational (no right answer per sample): share of answered questions where the Lead went back for more."""
    async def score(state: TaskState, target: Target) -> Score:
        r = _result(state)
        if state.metadata["expect"] != "answer" or r.get("status") != "answer":
            return Score.unscored(reason="not an answered sample")
        n = gap_researchers(r.get("tool_calls", []))
        return Score(value=int(n > 0), explanation=f"follow-up researchers after review: {n}")
    return score


@scorer(metrics=[mean(), stderr()])
def fan_out():
    """Multi-part questions only: the Lead dispatched >= 2 researchers in its first turn."""
    async def score(state: TaskState, target: Target) -> Score:
        r = _result(state)
        if (state.metadata["expect"] != "answer" or r.get("status") != "answer"
                or state.metadata.get("category") in SINGLE_LOOKUP):
            return Score.unscored(reason="single lookup or not answered")
        n = round1_researchers(r.get("tool_calls", []))
        return Score(value=int(n >= 2), explanation=f"researchers dispatched in round 1: {n}")
    return score


def _like_for_like(call: dict) -> bool:
    args = call.get("args", {})
    metrics = [args.get("metric"), args.get("x"), args.get("y"), *(args.get("metrics") or [])]
    if "steps_phone" not in metrics:
        return False
    if args.get("group_by") == "device_era":
        return True
    dates = [str(v) for v in args.values() if isinstance(v, str) and v[:2] == "20"]
    dates += [str(v) for v in args.values() for v in (v if isinstance(v, list) else []) if str(v)[:2] == "20"]
    return any(d < WATCH_START for d in dates) and any(d >= WATCH_START for d in dates)


@scorer(metrics=[mean(), stderr()])
def device():
    async def score(state: TaskState, target: Target) -> Score:
        if not state.metadata.get("device_caveat"):
            return Score.unscored(reason="not a device-era question")
        ok = any(_like_for_like(c) for c in _result(state).get("tool_calls", []))
        return Score(value=int(ok), explanation="compared steps_phone across eras" if ok
                     else "never compared steps_phone before vs after 2026-10-31")
    return score


@scorer(metrics={"*": [mean(), stderr()]})
def rubric():
    async def score(state: TaskState, target: Target) -> Score:
        r = _result(state)
        if state.metadata["expect"] != "answer" or r.get("status") != "answer":
            return Score.unscored(reason="not an answered sample")
        g = await judge(state.input_text, _body(r["response"]), RUBRIC)
        if not g or len(g) < len(RUBRIC):
            return Score.unscored(reason="judge_failed")
        return Score(value={k: int(v["pass"]) for k, v in g.items()},
                     explanation="; ".join(f"{k} {v['votes']}: {v['reason']}" for k, v in g.items()))
    return score


@scorer(metrics=[mean(), stderr()])
def label_match():
    async def score(state: TaskState, target: Target) -> Score:
        got = state.store.get("label")
        return Score(value=int(got == target.text), answer=got)
    return score


# ---------------------------------------------------------------- tasks
@task
def e2e(dataset: str = "e2e.jsonl") -> Task:
    return Task(dataset=json_dataset(str(HERE / dataset)), solver=pulse_agent(),
                scorer=[correct(), status(), numbers(), pipeline(), fan_out(), device(), gap_round(), rubric()],
                model="mockllm/model")


@task
def gatekeeper(dataset: str = "gatekeeper.jsonl") -> Task:
    return Task(dataset=json_dataset(str(HERE / dataset)), solver=gatekeeper_only(), scorer=label_match(),
                model="mockllm/model")
