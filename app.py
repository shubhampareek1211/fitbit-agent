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

from pulse.data import asof, db, first_date, meta
from pulse.mcp_server import mcp_app
from pulse.orchestrator import PENDING, answer

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("pulse")

app = FastAPI(title="Pulse", lifespan=mcp_app.lifespan)
app.mount("/mcp", mcp_app)
app.mount("/static", StaticFiles(directory="static"), name="static")

SAMPLE_QUESTIONS = [
    "How do temperature and rain change how much I walk?",
    "What's my weekly activity rhythm, and did it change after I started wearing the Fitbit?",
    "Am I meeting CDC/AHA targets for activity and sleep, and where can't my data tell?",
]


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
                            verified=(r.verify or {}).get("passed"), seconds=r.seconds)
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
                             "seconds": r.seconds, "chart": _chart_from(r)})
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


def _chart_from(result) -> dict | None:
    """A daily series for the report's main metric, drawn by the frontend."""
    brief = result.brief or {}
    metrics = [m for m in brief.get("metrics", []) if m][:2]
    if result.status != "answer" or not metrics or not brief.get("start"):
        return None
    cols = ", ".join(metrics)
    try:
        rows = db().execute(f"SELECT date::VARCHAR, {cols} FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
                            [brief["start"], brief["end"] or asof()]).fetchall()
    except Exception:
        return None
    return {"metrics": metrics, "dates": [r[0] for r in rows],
            "series": [[r[i + 1] for r in rows] for i in range(len(metrics))]}


@app.get("/api/overview")
async def overview() -> dict:
    cov = db().execute("SELECT metric, unit, first_date::VARCHAR, last_date::VARCHAR, days_with_data FROM coverage "
                       "WHERE days_with_data > 0 ORDER BY days_with_data DESC, metric").fetchall()
    steps = db().execute("SELECT date::VARCHAR, steps, steps_watch, steps_phone FROM daily ORDER BY date").fetchall()
    return {"asof": asof(), "first_date": first_date(), "location": meta().get("location"),
            "samples": SAMPLE_QUESTIONS,
            "coverage": [dict(zip(["metric", "unit", "first_date", "last_date", "days"], r)) for r in cov],
            "steps": {"dates": [r[0] for r in steps], "total": [r[1] for r in steps],
                      "watch": [r[2] for r in steps], "phone": [r[3] for r in steps]}}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "asof": asof()}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
