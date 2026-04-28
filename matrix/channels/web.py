"""Web channel — FastAPI app with chat UI and SSE streaming."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from matrix.core.envelope import Envelope
from matrix.core.harness import Harness
from matrix.transcripts import reader as transcripts

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


class SubmitBody(BaseModel):
    content: str
    user_id: str | None = None
    session_id: str | None = None


def build_app(harness: Harness) -> FastAPI:
    app = FastAPI(title="Matrix")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/api/agents")
    async def list_agents() -> list[dict[str, str]]:
        return [
            {"name": cfg.name, "description": cfg.description}
            for cfg in harness.list_agents()
        ]

    @app.get("/api/agents/{agent}/threads")
    async def list_threads(agent: str, user_id: str = "default") -> dict[str, Any]:
        try:
            cfg = harness.get_agent(agent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        default_id = harness.threads_for(agent).get(user_id)
        summaries = transcripts.list_sessions(cfg.cwd)
        return {
            "default_session_id": default_id,
            "threads": [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "updated_at": s.updated_at.isoformat(),
                    "message_count": s.message_count,
                    "is_default": s.session_id == default_id,
                }
                for s in summaries
            ],
        }

    @app.get("/api/agents/{agent}/threads/{session_id}")
    async def get_thread(agent: str, session_id: str) -> dict[str, Any]:
        try:
            cfg = harness.get_agent(agent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        items = transcripts.load_history(cfg.cwd, session_id)
        if items is None:
            raise HTTPException(status_code=404, detail="thread not found")
        return {
            "session_id": session_id,
            "items": [{"role": i.role, "blocks": i.blocks} for i in items],
        }

    @app.post("/api/agents/{agent}/threads")
    async def new_thread(agent: str, user_id: str = "default") -> dict[str, str]:
        try:
            harness.get_agent(agent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        session_id = harness.threads_for(agent).rotate(user_id)
        return {"session_id": session_id, "user_id": user_id}

    @app.post("/api/agents/{agent}/messages")
    async def submit(agent: str, body: SubmitBody) -> dict[str, str]:
        user_id = body.user_id or "default"
        reply_topic = str(uuid.uuid4())
        envelope = Envelope(
            agent=agent,
            user_id=user_id,
            session_id=body.session_id,
            content=body.content,
            reply_topic=reply_topic,
            source_channel="web",
        )
        await harness.session_manager.register(reply_topic)
        try:
            await harness.submit(envelope)
        except KeyError as exc:
            await harness.session_manager.close(reply_topic)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"reply_topic": reply_topic, "user_id": user_id}

    @app.get("/api/streams/{reply_topic}")
    async def stream(reply_topic: str, request: Request) -> StreamingResponse:
        async def event_source():
            generator = await harness.session_manager.subscribe(reply_topic)
            try:
                async for event in generator:
                    if await request.is_disconnected():
                        break
                    payload: dict[str, Any] = event.to_json()
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                await generator.aclose()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
