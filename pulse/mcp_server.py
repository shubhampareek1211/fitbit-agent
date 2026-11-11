"""MCP endpoint: the deterministic tools, so any chatbot (Claude, Gemini, ...) can query the data.

Tools only (no agent loop), so calls are fast and every number carries an evidence id.
"""
from fastmcp import FastMCP

from pulse import tools

mcp = FastMCP(
    name="Pulse",
    instructions=("Pulse exposes one person's wearable data (Fitbit + iPhone steps + NYC weather) as deterministic "
                  "tools. Call get_coverage first to see which metrics and dates exist. Every result includes an "
                  "evidence_id; cite it. Never diagnose medical conditions."),
)

for fn in (tools.get_coverage, tools.list_metrics, tools.get_daily, tools.summarize_metric, tools.compare_periods,
           tools.compare_groups, tools.correlate, tools.check_guideline, tools.sleep_sessions, tools.workouts,
           tools.workout_heart_rate):
    mcp.tool(fn)

mcp_app = mcp.http_app(path="/", stateless_http=True)
