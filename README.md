# Pulse: agent-powered analytics for your health data

**Live:** https://pulse-405277086961.us-central1.run.app · **Path:** research · **Author:** sp4553

Pulse answers real questions about **one person's own health data from a Fitbit watch and an iPhone** (workouts,
heart rate, sleep and activity) with a team of AI agents that plan, research in parallel, write, and then **verify
every number before you see it**. When the data can't answer, it says so, and it never diagnoses. Daily New York
City weather is joined to the data as an add-on (see [Add-on: weather](#add-on-weather)).

**Who it's for:** anyone with a wearable who wants honest answers instead of another dashboard. Phone-and-watch
data is messy: devices disagree, sleep data starts late, and a new watch makes you look twice as active overnight.
Generic chatbots either can't see your data or confidently generalise from studies about other people (see the
no-tools comparison below). This demo runs on the author's data; the same tools are exposed over **MCP**, so any
chatbot can use them.

---

## How it works

```
Question
  │
  ▼
Gatekeeper ── vague? → asks one clarifying question (kept per session_id)
  │          ── diagnosis / data that doesn't exist? → explains the limit, offers what it CAN answer
  ▼ Brief (typed: metrics, absolute dates)
Lead ── splits the brief into 2-4 independent subtasks, dispatched in ONE turn ──┐
  ▲                                                                                │
  │ findings (typed claims + evidence ids)        Researcher_1 … Researcher_k  ◄──┘  parallel, fresh contexts
  └── reviews for gaps → sends follow-up researchers (MAX_ROUNDS=3, MAX_RESEARCHERS=8, enforced in code)
      e.g. a finding says "this range mixes 91 phone-only days with 8 watch days" → the Lead sends a
      follow-up that repeats the analysis on phone-only steps
  ▼
Writer ── report from findings only, every number cited [E3]
  ▼
Verifier (code, not an LLM) ── cited ids exist? every number matches the evidence? limits section? no diagnosis?
  │   fail → one rewrite with the exact problems → still failing → shipped with a visible note
  ▼
Answer + sources + full agent trace
```

| Deep Research requirement | Where |
|---|---|
| ≥ 3 agent shapes | Gatekeeper, Lead, Researcher, Writer (+ deterministic Verifier) in [pulse/agents.py](pulse/agents.py) |
| Lead fans out in one turn, parallel, fresh contexts | The Lead returns a typed plan; `asyncio.gather` runs one ADK Runner per subtask, each with its own session, in [pulse/orchestrator.py](pulse/orchestrator.py). Visible as `Lead → research` dispatch records in `tool_calls` (see note below) |
| Lead goes back for gaps, up to a cap | `LeadTurn{done, subtasks}` review rounds. The tools compute caveats from the actual dates; the Lead plans only what was asked, then treats an unchecked caveat as a gap (the weather add-on query sends a follow-up in every run we made). `MAX_ROUNDS`, `MAX_RESEARCHERS`, per-agent timeouts and a 200 s global deadline in [pulse/config.py](pulse/config.py) |
| Vague questions require clarification | Gatekeeper `status="clarify"`; the follow-up on the same `session_id` continues the question |
| Accurate citations | Every tool result is recorded in an **Evidence Ledger** ([pulse/evidence.py](pulse/evidence.py)); the Writer cites ids; the **Verifier** ([pulse/verifier.py](pulse/verifier.py)) checks each cited number against the ledger |

**Built with:** Google ADK 2.14 (`LlmAgent` + one `Runner` per agent call), Gemini 3.5 Flash (Gatekeeper, Lead,
Writer) and Flash-Lite (Researchers) on Vertex AI, DuckDB, FastAPI, FastMCP, Inspect AI, Apache ECharts. All
numbers come from deterministic, unit-tested tools ([pulse/tools.py](pulse/tools.py)); the models never do arithmetic.

### `/chat` contract (same shape as Project 1)

```bash
curl -X POST $URL/chat -H 'Content-Type: application/json' \
  -d '{"message": "Do I walk less on rainy days?"}'
# -> {"response": "...markdown with [E1] citations and a Sources list...",
#     "session_id": "...", "status": "answer|clarify|refuse", "verified": true, "seconds": 61.2,
#     "tool_calls": [{"agent": "Gatekeeper", "name": "get_coverage", "args": {}, "result": "..."},
#                    {"agent": "Lead", "name": "research", "args": {"subtask": "S1", ...}, "result": "dispatched ..."},
#                    {"agent": "Researcher_1", "name": "compare_groups", "args": {...}, "result": "..."},
#                    {"agent": "Verifier", "name": "verify", ...}],
#     "charts": [{"title": "...", "subtitle": "...", "caption": "E3: compare_groups on daily, ...",
#                 "evidence": ["E3"], "option": {...an Apache ECharts option object...}}]}
```
`charts` is an optional extra field; the Project 1 fields are unchanged.

**Which `tool_calls` entries are real tool calls.** `Gatekeeper` and `Researcher_N` entries are the agents' actual
tool calls, captured from ADK events. The `Lead` entries (`plan`, `review`, `research`) and the `Verifier → verify`
entries are **written by the orchestrator**, not called by a model. The Lead has no tools: it returns a typed plan
that code dispatches, and each `research` entry records one dispatch (`args.round` shows whether it was the first
fan-out or a follow-up after review, with the Lead's stated reason). The Verifier is code, not an agent. The Writer
has no tools, so it has no entries. These records are there so the fan-out, the follow-up rounds and the
verification show up in the trace.

Also: `GET /run?question=` (server-sent events that drive the live agent lanes in the UI), `GET /api/overview`,
`/mcp/` (MCP, tools only), `GET /health`.

### Charts drawn from the evidence, not by a model

Following the Text2SQL lecture's "chart JSON schemas" slide, the backend emits chart specs as JSON and the page only
renders them, with [Apache ECharts](https://echarts.apache.org) 5.5 (Apache-2.0). The specs are built by code
([pulse/charts.py](pulse/charts.py)) from the evidence items the final report cites, in the order it cites them, up
to three per answer. Bar heights, target lines, means and correlations are the ledger's own values, the ones the
Verifier checked, so a chart can't disagree with the text, and bars always start at zero (the same lecture's
slide 44 shows how a mis-scaled bar misleads). [tests/test_charts.py](tests/test_charts.py) checks this.

| Cited evidence | Chart |
|---|---|
| `compare_groups` (weekday, rain, temperature, era) | bars with days per group; the computed highest and lowest highlighted; the verdict as subtitle; metrics on the same grouping share one chart (an identical series is noted, not drawn twice) |
| `correlate` | scatter of the same daily pairs the tool used, coloured by era when they span the watch start; ρ, CI and verdict as subtitle |
| `check_guideline` | bars per night or week coloured by whether the target was met, with the target line (or the typical range shaded) |
| `compare_periods` | the two period means, e.g. phone-only steps before vs after the watch |
| `summarize_metric`, `get_daily` | the daily series with a 7-day average, the mean line and the watch era shaded |
| `sleep_sessions` | sleep stages stacked per night |
| `workouts` | (1) minutes in each of Fitbit's heart-rate zones, one stacked bar per workout (per type past 30 workouts); (2) a week timeline: each workout as a block on its day and time, coloured by type, faded if the watch detected it rather than being started, so back-to-back gym visits show as clusters |
| `workout_heart_rate` | heart rate through one gym visit in 10-second steps, each workout shaded, with its median as a dashed line |

Every chart's caption names its evidence id, linked to the Sources list. Colours come from the page's CSS variables,
so charts follow light and dark mode. The zone colours are one hue stepped light to dark (an ordered scale) and the
workout types use a fixed categorical order, both checked for colour-blind separation and contrast in both modes;
the workout charts also carry a **Data** table under the chart, so nothing depends on telling colours apart. The landing page shows the step trend with the watch era shaded (so the jump
reads as measurement) and a calendar heatmap of phone-counted steps, the one count comparable across the window.

---

## Data

| Source | What | Coverage | How it was loaded |
|---|---|---|---|
| **Google Health API v4** (Fitbit Edge watch) | sleep sessions & stages, resting HR, HRV, SpO2, respiratory rate, skin temperature, VO2 max, Active Zone Minutes, exercise sessions | 2026-10-30 → as-of date | official [`ghealth` CLI](https://github.com/Google-Health-API/google-health-cli) with my own OAuth client; [scripts/export_fitbit.sh](scripts/export_fitbit.sh) |
| **Apple Health → Google Health API** (iPhone) | steps, distance, floors, active energy | 2026-08-01 → as-of date | same export (synced into the same account) |
| **Open-Meteo** archive | NYC daily temperature, precipitation, daylight | same window | [build/fetch_weather.py](build/fetch_weather.py) |
| Public guidelines | CDC/AASM sleep (7+ h), CDC/AHA activity (150 min/week), AHA resting HR (60-100 bpm), steps research (Paluch 2022) | n/a | hand-written with URLs in [data/metrics.yaml](data/metrics.yaml) |

**Offline build step:** `uv run build/normalize.py` turns the raw JSON into one read-only DuckDB snapshot,
`data/snapshots/<asof>/pulse.duckdb` (~2 MB, committed), with tables `daily`, `sleep_sessions`, `exercise` (one
row per workout: type, minutes, minutes in each heart-rate zone, started on the watch or detected automatically, and
a `visit` number shared by workouts less than 10 minutes apart), `workout_hr` (heart rate through each visit, the
watch's ~2-second readings averaged into 10-second steps), `coverage` and `meta`. The snapshot's last day is
**"today"** for every question, so answers stay reproducible. Times are also stored as local wall-clock time, so a
server running on UTC (Cloud Run) reports a 03:45 bedtime as 03:45, not 08:45.

**Refresh.** [build/refresh.py](build/refresh.py) runs at 11:00 and 23:00 ([scripts/install_schedule.sh](scripts/install_schedule.sh)).
Each run re-fetches only the days that weren't complete at the last fetch, rebuilds the snapshot, checks it against
the previous one (no lost days, no date gaps, nothing shrinking) and publishes it to a private Cloud Storage bucket as
`snapshots/<day>/pulse.duckdb`, then points `latest.json` at it. The app (`PULSE_BUCKET`) looks for a newer snapshot
every ten minutes and falls back to the committed one. A snapshot fetched during a day holds daily metrics through
the last **complete** day and sleep sessions and workouts through the fetch day so far, so today's workout shows up
without half a day of steps skewing the averages. The evals and the expected answers below stay pinned to the
committed 2026-11-07 snapshot.

Things the data handles explicitly:
- **Watch vs phone.** The API's merged daily total is canonical; `steps_watch` and `steps_phone` are kept to explain disagreements. Before 2026-10-31 only the phone measured activity, so the daily means jump when the watch arrives: steps 4,161 → 10,197, distance 2.9 → 7.1 km, floors 6.1 → 17.8, active energy 146 → 1,051 kcal. That's measurement, not behaviour. Every tool result whose range touches the watch era carries a computed `measurement_note`; for steps the like-for-like check is `steps_phone`, and the other three have no phone-only version, so their change across that date is never reported as real.
- **Zero vs missing.** The API sends no record for a day whose value is zero, so the build fills 0 for Active Zone Minutes and workouts on days with heart-rate data, for naps on days with a main sleep, and for floors on days with steps (2 + 1 + 6 + 10 days). Days with no sign the device was recording stay empty.
- **Before the watch vs missing.** Watch-only metrics (sleep, heart rate, HRV, Active Zone Minutes…) start on 2026-10-30. The tools report the days before that as `days_before_watch` with a `recording_note` ("not recorded yet"), separately from genuinely missing days.
- **Workouts.** 29 workouts from 2026-10-31 (14 started on the watch, 15 detected by it). Zone minutes use Fitbit's own four zones, so a workout *named* "Zone 2" can show mostly vigorous or peak minutes; the tools say so (`zone_note`) instead of judging the workout. Heart rate during elliptical, rowing and weights comes from a wrist sensor that can be less accurate with strong arm movement (`hr_note`), and Fitbit's average comes from the same sensor, so neither confirms the other.
- **What it can't answer:** glucose, blood pressure, diet quality, anything before 2026-08-01, diagnoses, and trends that need more than ~1 week of watch data. Correlations with fewer than 10 paired days return `insufficient_data` instead of a number.

**Privacy & consent:** this is the author's own data, shared knowingly for the course. Weight, body fat and height
were excluded. Raw exports, OAuth secrets and tokens are not in the repo.

---

## Sample queries for the grader

Expected answers are computed by SQL from the committed snapshot (as of **2026-11-07**). The live app refreshes its data twice a day, so its
answers cover later days too: counts and totals will be higher than the values below (for example 33 workouts instead
of 29 by 2026-11-10), while the method and sources are the same.

Each query is shown with its expected answer and source, Pulse's actual answer, and the same model
(Gemini 3.5 Flash) with **no tools and no system prompt** (full texts are in
[evals/results/sample_answers.json](evals/results/sample_answers.json) and [evals/results/no_tools.json](evals/results/no_tools.json)).

**1. "How hard have my workouts been since I got the watch, and which were the most intense?"**
- **Expected:** **29** workouts from 2026-10-31 to 2026-11-07 totalling **527** minutes, of which **341** were in Fitbit's moderate zone or harder and **191** in vigorous or peak. Most vigorous + peak minutes: the weights session on 2026-11-03 at 19:10 (**35**), the run on 2026-11-01 at 19:19 (**22**), then two ellipticals tied at **21** (2026-11-02 19:01 and 2026-11-04 20:00).
  *Source:* `exercise` (one row per workout; zone minutes from the API's `heartRateZoneDurations`).
- **Pulse** (verified, 32 s, 12 tool calls, no follow-up needed): *"During the 8-day period from 2026-10-31 to 2026-11-07, you logged 29 exercise sessions totaling 527.2 minutes… The single most intense workout based on vigorous or peak heart-rate zones was a Weights session at 19:10 on 2026-11-03, which logged 35 vigorous or peak minutes [E2]. Other highly intense sessions included a Run at 19:19 on 2026-11-01 with 22 vigorous or peak minutes, and two separate Elliptical workouts at 19:01 on 2026-11-02 and 20:00 on 2026-11-04, which each recorded 21 vigorous or peak minutes [E2]."* Charts: zone minutes per workout, the week timeline, daily sessions.
  The ranking is computed by the `workouts` tool (most vigorous + peak minutes, ties kept), not by a model. "Most intense" here means most minutes at high heart rate, so a long session can outrank a short, harder one; the answer says which measure it used.
- **No tools:** *"I don't have access to your personal smartwatch, fitness accounts, or health data… if you can export your workout history… I can analyze them for you."*

**2. "What's my weekly activity rhythm, and did it change after I started wearing the Fitbit?"**
- **Expected:** most active Monday, least active Wednesday: **≈5,299** vs **≈3,787**/day over the whole window, **≈4,877** vs **≈3,126** before the watch (phone only, to 2026-10-30); weekday vs weekend not clearly different. Raw steps jump **4,161 → 10,197**/day at 2026-10-31, but like-for-like phone steps go **4,161 → 7,036** (+69%) over only 8 days, so part of the jump is measurement.
  *Source:* `daily.steps`, `daily.steps_phone`, `daily.weekday`.
- **Pulse** (verified, 45 s, 8 tool calls, **one follow-up round**): *"During the pre-watch period (2026-08-01 to 2026-10-30), your daily activity peaked on Mondays and was lowest on Wednesdays… 4,877.3 steps… 3,125.7 steps [E2, E4]… For a like-for-like comparison across eras, your daily steps_phone rose from a mean of 4,160.6 steps before the watch (91 days) to 7,036.0 steps after getting the watch (8 days)… [E7]. The raw jump… partly reflects this change in measurement device rather than pure behavior."*
  How it got there: round 1 analysed each era separately. The watch-era finding carried the measurement caveat, so on review the Lead sent a follow-up comparing phone-only steps across both eras. In 2 of the 5 runs we made, the Lead planned the phone-only comparison itself in round 1, so no follow-up was needed.
- **No tools:** *"I don't have access to your personal Fitbit account… if you can export some of your data…"*

**3. "Am I meeting CDC/AHA targets for activity and sleep, and where can't my data tell?"**
- **Expected:** activity: **747** Active Zone Minutes in the only judged week (2026-11-01..07), above 150/week, but one week can't show a habit. Sleep: **5 of 7** nights at 7+ h (71%), mean **446** min. Steps: 25 of 99 days at 7,000+, but phone-era counts undercount. Resting HR 62–65 bpm, inside the typical 60–100.
  *Source:* `daily.azm_total`, `daily.sleep_minutes`, `daily.steps`, `daily.resting_hr`; reference ranges in `data/metrics.yaml`.
- **Pulse** (verified, 80 s, 14 tool calls, no follow-up needed): *"…you met the CDC/AHA physical activity guideline for the evaluated week with 747 Active Zone Minutes and met the sleep guideline on 71.4% of tracked nights [E4, E6]… You slept 7 or more hours on 5 out of 7 nights [E4]… Because watch data before 2026-10-30 was not recorded yet, sleep and active minutes could only be evaluated for a single week… This sample is too small to show if you regularly meet these targets."*
  An earlier version called the 90 pre-watch days "missing" and guessed the watch had been off. The tools now report those days as `days_before_watch` ("not recorded yet"), separately from genuinely missing days.
- **No tools:** lists the CDC/AHA targets and asks the user to paste their stats.

### Add-on: weather

Daily New York City weather (Open-Meteo) is joined to every day, so Pulse can also relate activity to temperature
and rain. It isn't one of the three core questions; it shows the gap loop most clearly.

**"How do temperature and rain change how much I walk?"**
- **Expected:** no measurable effect. Dry days average **4,615** steps vs **4,702** on rainy days (≥ 1 mm), 61 vs 38 days; mean temperature vs steps Spearman **ρ = −0.02**, n = 99, inconclusive.
  *Source:* `daily.steps`, `daily.precip_mm`, `daily.temp_mean_c` (Open-Meteo), 2026-08-01..2026-11-07.
- **Pulse** (verified, 75 s, 23 tool calls, **two follow-up rounds**): *"Daily temperature and precipitation had no clear effect on your walking behavior from 2026-08-01 to 2026-11-07… phone-only steps had a correlation of 0.02 with mean temperature [E14]… Raw steps agree… phone-only steps averaged 4216.6 on dry days (61 days) and 4676.1 on rainy days (38 days) [E13], which agrees with raw steps averaging 4615.1 and 4701.9 steps respectively [E7]."*
  How it got there: in round 1 three Researchers analysed raw steps and distance. Every finding carried the tool's caveat that the range "mixes 91 phone-only days with 8 watch days". On review the Lead sent two follow-ups to repeat the analysis on phone-only steps, then two more to redo distance within the phone-only era (distance has no phone-only version). This query sent a follow-up in all 5 runs we made after the change.
- **No tools:** *"Both temperature and rain have a massive, scientifically proven impact on how much you walk… Even light rain can decrease average daily steps by 5% to 10%."* This is confident, generic and **wrong for this person**.

---

## Evaluations

Evals are part of the architecture, not an afterthought. The same deterministic checks run in three places:
1. **At runtime:** the Verifier checks every report before it's shown.
2. **Offline:** in [Inspect AI](https://inspect.aisi.org.uk), for hill-climbing ([evals/task.py](evals/task.py)).
3. **In unit tests:** the tool math is checked on a synthetic snapshot with known answers ([tests/](tests/)).

| Suite | Samples | Where they came from |
|---|---|---|
| `e2e` | 50 from 27 templates (≤ 3 per template); v1 = 42 frozen in `e2e_v1.jsonl` | **Gold**: hand-written templates in [evals/gold/templates.yaml](evals/gold/templates.yaml); answers computed by `gold_sql` on the snapshot ([evals/update_targets.py](evals/update_targets.py)), independent of the agent's tools. Categories: lookup, comparison, weather, device, guideline, trend, correlation, workout, refuse, clarify. |
| `gatekeeper` | 28 | **Gold**: hand-labelled answer / clarify / refuse ([evals/gatekeeper.jsonl](evals/gatekeeper.jsonl)) |

None of the eval questions appear in any prompt as few-shot examples (the prompts have none).

**Scorers (deterministic first; a judge only where code can't decide):**

| Scorer | Kind | Checks |
|---|---|---|
| `correct` (headline) | deterministic status + **judge vs golden reference** | answered/clarified/refused as expected AND the answer agrees with the gold reference |
| `status` | deterministic | answer / clarify / refuse matches |
| `numbers` | deterministic | every gold number appears in the answer (rounding, minutes↔hours, number words) |
| `pipeline` | deterministic | Verifier passed · Lead fanned out to ≥ 2 researchers on multi-part questions · latency < 240 s |
| `device` | deterministic (trajectory) | device-switch questions compared `steps_phone` before vs after 2026-10-31 |
| `gap_round` | deterministic, informational | the Lead sent follow-up researchers after reviewing findings (no right answer per question; tracks how often the gap loop fires) |
| `rubric` | **judge with rubric** | leads with the answer · names period & n · states limitations · no diagnosis |
| `label_match` (gatekeeper) | deterministic, **pass^3** | same correct label on 3 of 3 runs |

**Judge:** DeepSeek V4.1 Flash on Vertex AI (`vertex_ai/deepseek-ai/deepseek-v4.1-flash-maas`, via LiteLLM). It's a
different model family from the Gemini agents, to avoid self-preference bias. Each criterion is decided by **majority
of 3 votes**, because single judge runs flipped on a borderline case during calibration. The judge gives a reason
before its verdict, and unparseable judgments are marked unscored, not failed.

```bash
uv run pytest                                                             # tool math + Verifier
uv run evals/update_targets.py                                            # recompute gold answers from the snapshot
uv run inspect eval evals/task.py@e2e --max-samples 4
uv run inspect eval evals/task.py@gatekeeper --epochs 3 --epochs-reducer at_least_3
uv run evals/report.py e2e                                                # scores ± 1/√n and failing samples
uv run inspect view                                                       # full traces
```

### Hill-climbing log

Each run is one pass over the suite with the DeepSeek judge. The margin of error is ±1/√n (lecture rule of thumb for
95% confidence at p = 0.5): **±0.154 for n = 42**, ±0.151 for n = 44.

| Run | What changed | Suite | `correct` | `status` | `numbers` | `verified` | `fan_out`† | `device` | gap rounds‡ |
|---|---|---|---|---|---|---|---|---|---|
| v0 | Baseline | v1 (original) | 0.881 | 0.952 | 0.900 | 1.000 | (1.000) | 1.000 | 5/32 |
| v0a | **Suite audit, same agent**: gold for "150 min/week" no longer demands a Monday-Sunday week; number words ("five nights") count; fan-out judged only on multi-part questions | v1 | 0.929 | 0.976 | 1.000 | 1.000 | (1.000) | 0.500 | 8/34 |
| v1 | **Change 1, reliability:** Gemini retry with exponential backoff on 429/5xx (baseline had 2 crashed samples from Vertex rate limits under parallel researchers) | v1 | 0.976 | **1.000** | 1.000 | 1.000 | 0.947 | 1.000 | 5/34 |
| v2 | **Change 2, device switch:** tools attach a computed `measurement_note` to step results crossing 2026-10-31; Lead must plan a `steps_phone` like-for-like subtask; Writer leads with it; weekly guideline uses rolling 7-day blocks | v1 | 0.952 | 1.000 | 1.000 | 0.971 | 1.000 | 1.000 | 3/34 |
| v3 | Verifier fix: "November 2026" is a date, not an uncited number (false positive found in v2) | v1 | 0.929 | 1.000 | 1.000 | **1.000** | 0.947 | 1.000 | 5/34 |
| v4 | **Changes 3 + 4:** `compare_groups` returns computed `highest`/`lowest` (a real answer named the wrong least-active day); Writer must respect computed verdicts (`no_clear_difference`) and the new `regularity_note` | **v2** (+2 weekday samples from that trace) | **0.977** | 1.000 | 0.969 | 0.972 | 1.000 | 1.000 | 3/36 |
| v5a | **Changes 5 + 6:** (5) the Lead no longer has to plan the `steps_phone` check in round 1: it plans only what's asked and treats an unchecked caveat in the findings as a gap; Researchers report a caveat instead of quietly running the extra check; the caveat now counts days on each side and also covers distance, floors and active energy (all jump at the watch start) and ranges wholly inside the watch era. (6) Pre-watch days on watch-only metrics are reported as `days_before_watch` ("not recorded yet"), not `days_missing` | v2 | 0.977 | 1.000 | 0.969 | 1.000 | 0.810 | 1.000 | 6/36 |
| v5 | Date fix found in v5a's one failure: the new coverage note said the watch "came into use on 2026-10-30", so one plan split the eras there. Before/after comparisons now split at 2026-10-31 (the first full watch day; 2026-10-30 has no watch steps) | v2 | 0.932 | 1.000 | 0.969 | 1.000 | 0.762 | 1.000 | **8/36** |
| v6 | **Workout tools** (`workouts`, `workout_heart_rate`) added to every Researcher and named in the prompts; Verifier ignores clock times and "Zone N". The first run (0.864) caught a regression: the Lead split "my average in October" at 2026-10-31, primed by the watch date the new prompt lines repeated. Fix: those mentions removed, plus "a question about one period is not a comparison" | v2 | 0.932 | 0.977 | **1.000** | 0.944 | 0.905 | 1.000 | 8/36 |
| v7 | **Independent review fixes:** a failed Lead review or Writer draft no longer discards the findings; `check_guideline` evidence names the judged week (an answer had attributed the 2026-11-01..07 total to an 8-day range) and Researchers must use it for guideline questions; charts no longer call a raw change across the watch start "clearly higher"; no guideline chart when no week could be judged; `correlate` accepts a negative lag; ties kept in `most_intense`; stricter "was it really zone 2?" rule. **Suite v3** adds 6 workout samples (the workout tools had none) | **v3** (50) | **0.980** | 1.000 | 1.000 | 1.000 | 0.810 | 1.000 | 10/42 |

† `fan_out` = the Lead dispatched ≥ 2 researchers in its first turn, on multi-part questions. v0 and v0a values were
recomputed from the stored traces with the final scorer.
‡ Answered questions where the Lead sent follow-up researchers after reviewing findings (scorer `gap_round` from v5;
earlier runs recomputed from their stored traces).

**What's real and what's noise:**
- **No change in `correct` exceeds the ±0.15 margin.** v1 → v3 ran without any agent change in between and still moved 0.976 → 0.952 → 0.929, which is run-to-run noise. With 42 samples this suite can't tell a 5-point gain from noise (evals lecture, slide 27).
- **The gains we trust are mechanistic, confirmed by reading traces:**
  1. Crashed samples went from 2 to 0 after the retry change (`status` 0.952 → 1.000 in every later run).
  2. Device-switch answers now compare phone-to-phone in every run (`device` 1.000, trajectory-checked).
  3. Weekday superlatives are now computed by code: both new samples pass.
- **Gatekeeper:** pass^3 went from 0.96 → **1.00** (25/25 labels identical across 3 runs). Its prompt never changed, and the one baseline miss was a single label flip on "What's my average HRV?", so treat this as noise too.
- **Suite v2 vs v1:** v4 and v5 are on a different-size suite, so they aren't directly comparable to v0-v3. Suite v1 is frozen at [evals/e2e_v1.jsonl](evals/e2e_v1.jsonl).
- **Changes 5 + 6 (the gap loop):**
  - *How many:* the number of gap rounds across the suite (8/36) stays inside the range earlier runs already showed (3-8), so the count alone isn't the gain.
  - *Why they fire:* in v4, none of the 3 follow-ups came from a caveat (they explored a trend and checked watch wear). In v5, 5 of 8 are follow-ups the Lead sends because a finding carried the measurement caveat ("Findings S1 and S2 carry measurement caveats… repeat using steps_phone"). Those land on the questions whose groups mix both eras: rain vs dry, temperature, weekday extremes, weekend vs weekday.
  - *The sample queries:* before the change, none of the then three sample queries ever sent a follow-up. Since the change, the weather query (now the add-on) did in 5 of 5 runs, the weekly-rhythm query in 3 of 5, and the guideline query in 1 of 5.
  - *Wording:* answers using the word "missing" fell from 4-11 per run to 1-2.
  - *Device:* stayed 1.000.
  - *fan_out:* fell (1.000 → 0.762, inside its ±0.22 margin) because single-part questions such as "how many days in August did I reach 7,000 steps?" now get one Researcher instead of an extra caveat-check subtask. The scorer counts every non-lookup category as multi-part. All three sample queries still fan out to 2-4 Researchers in round 1.
- **v5a → v5 `correct` 0.977 → 0.932** is noise (±0.151). The two new failures give exactly the gold numbers (August ≈5,180 vs October ≈3,842 steps/day) but lead with the tool's verdict, "no clear difference" (bootstrap CI −3,071 to +473), which the judge reads as contradicting "you walked less". Both passed in v5a and v4.
- **Gatekeeper (v5):** still 25/25 labels identical across 3 runs after the coverage tool changed.
- **v6 (workout tools):**
  - *The regression and its fix:* the first full run after adding the workout tools scored 0.864. Reading the traces found one real regression. "What was my average daily step count in October?" passed in all 5 earlier runs, then failed in 2 of 2 because the Lead split October into Oct 1-30 and Oct 31, citing the watch date that the new prompt lines repeated. After the fix, the 8 split-sensitive samples (monthly averages, before/after-watch, weekend vs weekday) all passed, and the full run is back to 0.932. The first run's other failures passed when re-run, so they were noise.
  - *Errors:* `status` 0.977 is one Gatekeeper call that hung until its 45 s timeout before making any tool call. There were 4 such hangs on 2026-11-10 (in about 120 runs, gatekeeper suite included) and none in the ~200 runs before. Gatekeeper latency is otherwise unchanged (median 5.8 s vs 6.2 s). The gatekeeper suite's one miss (24/25) is the same hang, not a wrong label.
  - *Verifier:* `verified` 0.944 is two false positives of an already-known kind: a band label ("under 10°C") and a threshold ("at least 10 paired days") read as uncited numbers.

- **v7 (review fixes, suite v3):** 49 of 50 correct with no errors, but the suite grew by six samples and the margin is ±0.14, so this shows no regression rather than a gain. The "150 minutes per week" sample had failed in 7 of 11 earlier runs (the answer summed 8 days, or attributed the judged week's total to the wrong dates, and still passed the Verifier); after the fix it named the judged week in 3 of 3 runs. The six workout samples passed in 3 of 3 runs each. The one miss is the August-to-October trend question (right numbers, "no clear trend" where the reference says "down").
- **What "verified" does and doesn't mean:** the Verifier checks that each cited number appears in the cited evidence record (allowing rounding and unit conversions). It doesn't check that the number is attached to the right label, and it is weaker on wide records: against the all-workouts record about half of arbitrary integers would pass, against a small summary about 5%. Clock times aren't checked.

**Known remaining failures:**
- When a comparison's verdict is `no_clear_difference`, the Writer sometimes opens with "no clear difference" instead of "October was lower on average (3,842 vs 5,180/day), but the gap isn't statistically clear". The judge marks that wrong about half the time.
- The Lead sometimes plans the phone-only comparison in round 1 even though it's told not to (sample query 2, 2 of 5 runs), so that run has no follow-up round.
- Watch-vs-phone single-day questions sometimes omit the merged total.
- The Verifier occasionally flags harmless numbers such as "(1 day each)" or a band label like "under 10°C".
- A Gatekeeper call occasionally hangs. It now times out at 30 s and is tried once more; two hangs in a row still return an error (seen once in 84 Gatekeeper runs made in parallel).
- For "which workouts were most intense?", the first Researcher sometimes reports only totals, so the Lead spends a follow-up round asking for the individual workouts again.
- The judge itself flipped on borderline criteria during calibration (hence majority-of-3).

---

## Use it from your own chatbot (MCP)

The deterministic tools are served over MCP (streamable HTTP, stateless) at
`https://pulse-405277086961.us-central1.run.app/mcp/`:
`get_coverage`, `list_metrics`, `get_daily`, `summarize_metric`, `compare_periods`, `compare_groups`, `correlate`,
`check_guideline`, `sleep_sessions`, `workouts`, `workout_heart_rate`. Every result carries an `evidence_id`. In Claude: *Settings → Connectors → Add
custom connector →* the URL above. Test locally with `npx @modelcontextprotocol/inspector`.

## Deploy

Cloud Run service `pulse` (us-central1), continuously deployed from this repo's `main` branch by Cloud Build using the
[Dockerfile](Dockerfile). The image holds runtime dependencies only (no eval or test tools) plus the committed DuckDB
snapshot. Settings: request timeout **600 s**, **max 1 instance** (pending clarifications live in memory), 1 GiB
memory, public access. The runtime service account needs Vertex AI access for Gemini.

```bash
docker build -t pulse . && docker run -p 8080:8080 \
  -v ~/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json -e GOOGLE_CLOUD_PROJECT=<your-project> pulse
```

## Run it locally

```bash
gcloud auth application-default login          # Vertex AI (Gemini agents + DeepSeek judge)
uv sync
uv run app.py                                  # http://localhost:8080
# refresh data (needs your own Google Health API OAuth client + ghealth):
./scripts/export_fitbit.sh 2026-08-01 && uv run build/fetch_weather.py 2026-08-01 <asof> && uv run build/normalize.py
```
