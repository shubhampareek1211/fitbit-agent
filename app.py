"""Pulse web app: POST /chat (Project 1 shape + agent field), GET /run (live trace), /mcp (tools), / (frontend)."""
import asyncio
import json
import logging
import os
import sys
import uuid

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pulse import charts
from pulse.data import asof, db, first_date, meta
from pulse.mcp_server import mcp_app
from pulse.orchestrator import PENDING, answer

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("pulse")

app = FastAPI(title="Pulse", lifespan=mcp_app.lifespan)
app.mount("/mcp", mcp_app)
app.mount("/static", StaticFiles(directory="static"), name="static")

SAMPLE_QUESTIONS = [
    "How hard have my workouts been since I got the watch, and which were the most intense?",
    "What's my weekly activity rhythm, and did it change after I started wearing the Fitbit?",
    "Am I meeting CDC/AHA targets for activity and sleep, and where can't my data tell?",
]
ADDON_QUESTION = "How do temperature and rain change how much I walk?"  # weather is an add-on, not a core question


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]
    status: str = "answer"
    verified: bool | None = None
    seconds: float | None = None
    charts: list[dict] = []  # ECharts options built by code from the cited evidence (optional extra)


def _log(result, message: str) -> None:
    """One JSON line per request -> Cloud Logging (the online-eval record)."""
    log.info(json.dumps({"event": "chat", "session_id": result.session_id, "question": message[:300],
                         "status": result.status, "seconds": result.seconds, "rounds": result.rounds,
                         "researchers": result.researchers,
                         "verified": (result.verify or {}).get("passed"),
                         "problems": (result.verify or {}).get("problems", [])[:3]}))


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    session_id = request.session_id or str(uuid.uuid4())
    try:
        r = await answer(request.message, session_id)
        _log(r, request.message)
        return ChatResponse(response=r.response, session_id=r.session_id, tool_calls=r.tool_calls, status=r.status,
                            verified=(r.verify or {}).get("passed"), seconds=r.seconds, charts=r.charts)
    except Exception as e:  # same contract as Project 1: errors come back as a message, not a 500
        log.exception("chat failed")
        return ChatResponse(response=f"Model call failed: {type(e).__name__}: {str(e)[:300]}",
                            session_id=session_id, tool_calls=[], status="error")


@app.post("/clear")
async def clear(session_id: str) -> dict:
    PENDING.pop(session_id, None)
    return {"status": "ok"}


@app.get("/run")
async def run(question: str = Query(...), session_id: str | None = None) -> StreamingResponse:
    """Server-sent events: live agent activity, then the final report."""
    session_id = session_id or str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def work():
        try:
            r = await answer(question, session_id, emit=emit)
            _log(r, question)
            await queue.put({"type": "done", "markdown": r.response, "session_id": r.session_id,
                             "status": r.status, "tool_calls": r.tool_calls, "verify": r.verify,
                             "seconds": r.seconds, "charts": r.charts})
        except Exception as e:
            log.exception("run failed")
            await queue.put({"type": "error", "message": f"{type(e).__name__}: {str(e)[:300]}"})

    async def stream():
        task = asyncio.create_task(work())
        yield f"data: {json.dumps({'type': 'start', 'session_id': session_id})}\n\n"
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/overview")
async def overview() -> dict:
    cov = db().execute("SELECT metric, unit, first_date::VARCHAR, last_date::VARCHAR, days_with_data FROM coverage "
                       "WHERE days_with_data > 0 ORDER BY days_with_data DESC, metric").fetchall()
    workouts, nights, step_days = db().execute(
        "SELECT (SELECT count(*) FROM exercise), (SELECT count(*) FROM sleep_sessions WHERE NOT is_nap), "
        "(SELECT count(steps) FROM daily)").fetchone()
    return {"asof": asof(), "first_date": first_date(), "location": meta().get("location"),
            "fetched_at": meta().get("fetched_at") or None,
            "stats": [["Workouts", workouts], ["Nights of sleep", nights], ["Days of steps", step_days]],
            "samples": SAMPLE_QUESTIONS, "addon": ADDON_QUESTION,
            "coverage": [dict(zip(["metric", "unit", "first_date", "last_date", "days"], r)) for r in cov],
            "charts": charts.overview()}


@app.get("/health")  # Cloud Run reserves paths ending in "z", so /healthz only answers locally
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "asof": asof()}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
