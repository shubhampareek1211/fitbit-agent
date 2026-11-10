"""Typed contracts between agents. Each agent's output is validated against one of these."""
from typing import Literal

from pydantic import BaseModel, Field


class Brief(BaseModel):
    """Gatekeeper output: should we research this, ask the user, or explain a limit?"""
    status: Literal["answer", "clarify", "refuse"]
    question: str = Field(description="The question restated precisely, with absolute dates")
    metrics: list[str] = Field(default_factory=list, description="Metric names from list_metrics")
    start: str | None = Field(default=None, description="YYYY-MM-DD")
    end: str | None = Field(default=None, description="YYYY-MM-DD")
    clarification: str | None = Field(default=None, description="If status=clarify: the question to ask the user")
    limitation: str | None = Field(default=None, description="If status=refuse: what the data cannot answer and why")


class Subtask(BaseModel):
    id: str = Field(description="Short id like S1")
    question: str = Field(description="One self-contained sub-question a researcher can answer with the tools")
    metrics: list[str] = Field(default_factory=list)
    start: str = Field(description="YYYY-MM-DD")
    end: str = Field(description="YYYY-MM-DD")


class LeadTurn(BaseModel):
    done: bool = Field(description="True when the findings fully cover the brief")
    reasoning: str = Field(description="One or two sentences: why these subtasks, or why done")
    subtasks: list[Subtask] = Field(default_factory=list)


class Claim(BaseModel):
    text: str = Field(description="One factual sentence")
    value: float | None = Field(default=None, description="The key number, copied from the tool result")
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, description="evidence_id values from tool results, e.g. E3")


class Finding(BaseModel):
    subtask_id: str
    summary: str = Field(description="Two-sentence answer to the sub-question")
    claims: list[Claim] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list, description="What the data could not answer")
    error: str | None = Field(default=None, description="Set by the orchestrator if the researcher failed")


class VerifyResult(BaseModel):
    passed: bool
    problems: list[str] = Field(default_factory=list)
    cited_ids: list[str] = Field(default_factory=list)
    numbers_checked: int = 0
