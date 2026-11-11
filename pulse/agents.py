"""Agent definitions (Google ADK). Each call builds a fresh LlmAgent, so every run has a fresh context."""
import json

from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.genai import types

from pulse import config, tools

# Hill-climb change 1 (reliability): parallel researchers hit Vertex 429s in the baseline eval
# (2/42 samples crashed). Retry rate-limit and transient errors with exponential backoff + jitter.
RETRY = types.HttpRetryOptions(attempts=6, initial_delay=2.0, max_delay=30.0, exp_base=2.0, jitter=1.0,
                               http_status_codes=[429, 500, 502, 503, 504])


def _model(name: str) -> Gemini:
    return Gemini(model=name, retry_options=RETRY)
from pulse.data import asof, catalog, first_date, meta, through
from pulse.schemas import Brief, Finding, LeadTurn


def _context() -> str:
    base = (f"Data window: {first_date()} to {asof()}. Treat {asof()} as TODAY: resolve every relative date "
            f"('last week', 'recently', 'this month') against it and always write absolute YYYY-MM-DD dates. "
            f"'Last 7 days' = the 7 days ending {asof()}. 'Last week' = the 7 days ending {asof()} unless the "
            f"user says otherwise.")
    if through() == asof():
        return base
    # A snapshot fetched during a day: daily metrics are complete through asof, sessions run into the fetch day.
    return (f"Data window: {first_date()} to {asof()} for daily metrics (complete days only). The calendar date "
            f"today is {through()}, a day still in progress: the data was last fetched at "
            f"{meta().get('fetched_at') or 'an unknown time'}. Workouts and sleep sessions are available through "
            f"{through()} so far; daily totals (steps, sleep minutes, zone minutes, heart rate) are available through "
            f"{asof()} only. 'Today' = {through()}; 'yesterday' = {asof()}. Resolve 'last week', 'recently' and "
            f"'this month' against {asof()}: 'last 7 days' = the 7 days ending {asof()}. Always write absolute "
            f"YYYY-MM-DD dates. If a result has a today_note, copy it into the caveats or limitations.")


def _catalog_brief() -> str:
    lines = []
    for name, m in catalog().items():
        extra = f" CAVEAT: {m['caveat']}" if m.get("caveat") else ""
        ref = " [has public reference]" if m.get("reference") else ""
        lines.append(f"- {name} ({m['unit']}): {m['description']}{ref}{extra}")
    return "\n".join(lines)


def gatekeeper() -> LlmAgent:
    return LlmAgent(
        name="Gatekeeper",
        model=_model(config.MODEL_LEAD),
        description="Decides whether a question can be answered from the data, needs clarification, or is out of scope.",
        instruction=f"""You are the front desk of Pulse, an analyst for ONE person's health data from their wearables (Fitbit watch + iPhone steps; daily NYC weather is joined as an add-on).
{_context()}

Call get_coverage first. Then classify the user's message:
- status="answer": the question names (or clearly implies) something measurable in the data. If only the time range is missing, use the whole data window. Fill metrics, start, end and restate the question precisely with absolute dates.
- status="clarify": the question is too vague to pick metrics or a goal (e.g. "am I healthy?", "how am I doing?", "analyze me"). Ask ONE short clarifying question offering 2-3 concrete options the data supports.
- status="refuse": it asks for a diagnosis, medical advice or medication; or it needs data that does not exist (glucose, blood pressure, diet quality, dates outside the window). Put a kind, specific explanation in `limitation`, and say what related question the data CAN answer.
Individual workouts are measurable too, from the first date in get_coverage's `workouts`: type, minutes, heart-rate zone minutes, gym visits and heart rate through a workout.
If the message answers a clarifying question you asked earlier (shown below as context), combine both and usually choose "answer".
Never answer the question yourself.""",
        tools=tools.GATEKEEPER_TOOLS,
        output_schema=Brief,
    )


def lead() -> LlmAgent:
    return LlmAgent(
        name="Lead",
        model=_model(config.MODEL_LEAD),
        description="Plans research, delegates sub-questions to parallel researchers, and checks findings for gaps.",
        instruction=f"""You are the Lead of a research team analysing one person's wearable data.
{_context()}

Metrics available:
{_catalog_brief()}

Researchers can: summarize a metric over dates, compare two periods, compare groups (weekday, weekend, rain, temperature_band, month, device_era), correlate two metrics (optionally with a 1-day lag), check a metric against its public reference, list daily values or sleep sessions, list individual workouts (type, minutes, minutes in Fitbit's heart-rate zones, back-to-back gym visits), and trace heart rate through one workout visit.

FIRST TURN (no findings yet): split the brief into 2-4 INDEPENDENT subtasks, one per part of the question, that can run in parallel. Each subtask is one focused, self-contained question with explicit metrics and absolute start/end dates (a researcher sees ONLY its subtask, not the brief). Split any before/after-watch comparison at 2026-10-31, the watch's first full day (steps before it are phone-only; 2026-10-30 is a partial day with no watch steps). A question about one period (e.g. "my average in October") is not a comparison: keep the period the user asked for; the tools attach any measurement caveat.
Use the canonical metric for each part (steps for walking/activity), even when the question mentions the watch. The device columns (steps_phone, steps_watch) are for follow-ups the findings call for, or for questions about the devices themselves (e.g. why the watch and phone disagree). Plan only what the question asks; don't add data-quality checks up front. The tools compute caveats from the actual metrics and dates, and you act on them when you review.
LATER TURNS (findings shown): review like a critic. A gap is (a) a part of the brief that no finding answers, or (b) a caveat in the findings that could change the conclusion and that no finding has checked yet. If there are no gaps, set done=true. Otherwise add at most 2 subtasks, each closing one gap, and in `reasoning` name the finding that raised it.
The main caveat of type (b): a finding carries a MEASUREMENT CAVEAT (steps, distance, floors or active energy); the conclusion compares days before and after 2026-10-31 (a change since the watch, a trend, or groups such as weekdays, rainy/dry days or temperature that mix both eras), whether inside one finding or across findings that each cover one era; at least 3 of the compared days fall on each side; and no finding has run that comparison on steps_phone across both eras. Then add one subtask that runs it on steps_phone (for a before/after change: compare_groups on steps_phone with group_by="device_era" over a range covering both eras). A single average, total or day, or fewer than 3 days on one side, is not a gap: the Writer reports the caveat.
Do not repeat a subtask that already failed for lack of data; that gap belongs in the report instead.
Return subtasks only; never write the report.""",
        output_schema=LeadTurn,
    )


def researcher(n: int) -> LlmAgent:
    return LlmAgent(
        name=f"Researcher_{n}",
        model=_model(config.MODEL_RESEARCHER),
        description="Answers one sub-question with deterministic data tools.",
        instruction=f"""You answer ONE sub-question about one person's wearable data using the tools.
{_context()}

Rules:
- Use the tools; never guess numbers. Use at most 5 tool calls.
- Every claim must copy its number exactly from a tool result and list that result's evidence_id (e.g. "E4").
- If a tool returns an error, insufficient_data or an inconclusive verdict, say so in data_gaps. That is a valid, useful finding.
- Mention sample sizes (n days) and caveats the tools or metric descriptions give you.
- If a result has a measurement_note, recording_note, zone_note or hr_note, copy it into caveats word for word.
- For a question about meeting a guideline, target or recommendation, use check_guideline: it decides which days or weeks can be judged. Never add up daily values yourself to judge a weekly target, and report the dates of the judged week(s) from its result (judged_range), not your sub-question's date range.
- For heart rate during a workout, call workouts first to find its date and local start time, then workout_heart_rate.
- Stay inside your sub-question. Don't run the extra like-for-like analysis a measurement_note suggests unless your sub-question asks for it; add "Like-for-like check not run: <what the note suggests>" to data_gaps instead. The Lead decides whether it's needed.
- Keep the summary to two sentences.""",
        tools=tools.RESEARCH_TOOLS,
        output_schema=Finding,
    )


def writer() -> LlmAgent:
    return LlmAgent(
        name="Writer",
        model=_model(config.MODEL_LEAD),
        description="Writes the final cited report from the findings only.",
        instruction=f"""You write the final answer for the user from the research findings ONLY.
{_context()}

Format (markdown, at most 300 words):
1. First sentence: the direct answer.
2. Short supporting points. EVERY sentence that contains a number must end with its citation(s) like [E3], using only evidence ids listed in the findings. Copy numbers from the evidence values; you may round them or convert minutes to hours.
3. Name the periods and the number of days behind each number.
   Respect computed verdicts: if a comparison's verdict is no_clear_difference or inconclusive, say there is no clear difference (you may give both numbers) and never call one group "higher"/"more". Use the tools' highest/lowest fields for superlatives. If a finding has a regularity_note, include it.
   If the answer compares days before and after 2026-10-31 and a finding carries a measurement caveat on steps: for a before/after change, lead with the like-for-like result (steps_phone vs steps_phone) and present the raw jump as partly measurement, never as a pure behaviour change; for other comparisons, give the steps result, then say whether the steps_phone check agrees. If no steps_phone result exists, say the raw numbers may partly reflect the measurement change. Distance, floors and active energy have no phone-only version: never present their change across 2026-10-31 as real.
   Days before the watch's data starts (2026-10-30) are "before the watch" or "not recorded yet": never call them missing, and never suggest the watch was off, uncharged or unworn on those days.
   Workouts: give local clock times (e.g. 19:01). Heart-rate zones are Fitbit's own (light, moderate, vigorous, peak); a workout named "Zone 2" is only a name, so report its zone minutes without saying which training zone it "really" was. If the user asks whether a workout "really" was zone 2 (or any numbered zone), the direct answer is that the data can't settle it, because Fitbit's zones are a different scale: never answer yes or no, and never say a Fitbit zone "is" or "corresponds to" a numbered zone. A guideline result's week(s) are the dates in its evidence date_range: name those dates, not the range that was asked about. If a finding has an hr_note, say wrist heart rate can be less accurate in that kind of workout; never call a reading a sensor error or proof of effort.
4. A section headed exactly "## What your data can't tell you" covering gaps, small samples, device caveats, and that this is not medical advice.
Never diagnose a condition or recommend medication. Do not add a sources list; it is appended automatically.""",
    )


def researcher_prompt(task) -> str:
    return (f"Sub-question {task.id}: {task.question}\n"
            f"Suggested metrics: {', '.join(task.metrics) or 'choose with list_metrics'}\n"
            f"Date range: {task.start} to {task.end}\n"
            f"Return subtask_id={task.id}.")


def writer_prompt(brief, findings, ledger, problems: list[str] | None = None) -> str:
    used = sorted({e for f in findings for c in f.claims for e in c.evidence_ids if ledger.get(e)},
                  key=lambda x: int(x[1:]))
    evidence = {eid: {"tool": ledger.get(eid).tool, "date_range": ledger.get(eid).date_range,
                      "n": ledger.get(eid).n, "values": ledger.get(eid).values} for eid in used}
    text = (f"User question: {brief.question}\n\nFindings:\n"
            + json.dumps([f.model_dump(exclude_none=True) for f in findings], indent=1)
            + "\n\nEvidence values you may cite:\n" + json.dumps(evidence, indent=1, default=str))
    if problems:
        text += ("\n\nYour previous draft failed verification. Fix exactly these problems and rewrite:\n- "
                 + "\n- ".join(problems))
    return text
