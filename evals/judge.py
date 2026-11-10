"""Rubric judge: DeepSeek V4.1 Flash on Vertex AI (serverless), called through LiteLLM.

A different model family from the Gemini agents (avoids self-preference bias). Each rubric is judged
`votes` times and every criterion is decided by majority, because single runs flipped on borderline
criteria. Unparseable votes are dropped; if all fail, the caller marks the sample unscored.
"""
import asyncio
import json
import os

import litellm

litellm.suppress_debug_info = True

JUDGE_MODEL = os.environ.get("PULSE_JUDGE_MODEL", "vertex_ai/deepseek-ai/deepseek-v4.1-flash-maas")
JUDGE_LOCATION = os.environ.get("PULSE_JUDGE_LOCATION", "global")  # DeepSeek MaaS is served from global only
VOTES = int(os.environ.get("PULSE_JUDGE_VOTES", "3"))
_sem = asyncio.Semaphore(8)

PROMPT = """You are an expert evaluator grading a response about ONE person's wearable data against a rubric.
For each criterion: decide on its own whether the response meets it (don't let one failure colour the others),
and don't reward length. Give a one-sentence reason BEFORE the verdict.

<question>{question}</question>
<response>
{response}
</response>
<rubric>
{rubric}
</rubric>

Reply with JSON only, one key per criterion name:
{{"<criterion>": {{"reason": "<one sentence>", "pass": true|false}}, ...}}"""


async def _vote(question: str, response: str, rubric: dict[str, str]) -> dict[str, dict] | None:
    text = PROMPT.format(question=question, response=response,
                         rubric="\n".join(f"- {k}: {v}" for k, v in rubric.items()))
    async with _sem:
        try:
            out = await litellm.acompletion(
                model=JUDGE_MODEL, vertex_location=JUDGE_LOCATION,
                vertex_project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                messages=[{"role": "user", "content": text}], response_format={"type": "json_object"},
                temperature=0.3, timeout=120, num_retries=2)
            data = json.loads(out.choices[0].message.content)
            return {k: data[k] for k in rubric if isinstance(data.get(k), dict) and "pass" in data[k]}
        except Exception:
            return None


async def judge(question: str, response: str, rubric: dict[str, str], votes: int = VOTES) -> dict | None:
    """Return {criterion: {"pass": bool, "votes": "2/3", "reason": str}} or None if every vote failed."""
    ballots = [b for b in await asyncio.gather(*(_vote(question, response, rubric) for _ in range(votes))) if b]
    if not ballots:
        return None
    result = {}
    for k in rubric:
        cast = [b[k] for b in ballots if k in b]
        if not cast:
            continue
        yes = sum(bool(c["pass"]) for c in cast)
        passed = yes * 2 > len(cast)
        reason = next((c.get("reason", "") for c in cast if bool(c["pass"]) == passed), "")
        result[k] = {"pass": passed, "votes": f"{yes}/{len(cast)}", "reason": reason}
    return result
