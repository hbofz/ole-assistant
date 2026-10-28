"""Chat server. Run: uv run uvicorn ole.web:app --port 8000"""
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ole.answer import stream_answer
from ole.db import connect
from ole.search import warmup

STATIC = Path(__file__).parent / "static"
MAX_QUESTION = 2000



@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=warmup, daemon=True).start()
    yield


app = FastAPI(title="Lefse", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    question: str
    history: list[Turn] = Field(default_factory=list, max_length=40)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with connect() as con:
        updated, pages = con.execute("SELECT max(checked_at), count(*) FROM pages WHERE status='live'").fetchone()
    return {"updated": updated, "pages": pages}


@app.post("/api/chat")
def chat(req: ChatRequest):
    question = req.question.strip()
    if not question or len(question) > MAX_QUESTION:
        raise HTTPException(400, f"Ask a question between 1 and {MAX_QUESTION} characters.")
    history = [t.model_dump() for t in req.history if t.content.strip()]

    def events():
        for ev in stream_answer(history, question):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
