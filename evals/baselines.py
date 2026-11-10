"""README baseline: the same model with NO tools and NO system prompt, on the 3 sample questions.

Usage: uv run evals/baselines.py   -> evals/results/no_tools.json
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import litellm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import SAMPLE_QUESTIONS  # noqa: E402
from pulse import config  # noqa: E402

litellm.suppress_debug_info = True
OUT = Path(__file__).resolve().parent / "results" / "no_tools.json"


async def ask(q: str) -> str:
    r = await litellm.acompletion(model=f"vertex_ai/{config.MODEL_LEAD}", messages=[{"role": "user", "content": q}],
                                  vertex_project=os.environ.get("GOOGLE_CLOUD_PROJECT"), vertex_location="global",
                                  timeout=120)
    return r.choices[0].message.content


async def main() -> None:
    answers = await asyncio.gather(*(ask(q) for q in SAMPLE_QUESTIONS))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps([{"question": q, "model": config.MODEL_LEAD, "no_tools_answer": a}
                               for q, a in zip(SAMPLE_QUESTIONS, answers)], indent=1))
    for q, a in zip(SAMPLE_QUESTIONS, answers):
        print(f"### {q}\n{a[:500]}\n")


if __name__ == "__main__":
    asyncio.run(main())
