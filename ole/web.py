"""Chat server. Run: uv run uvicorn ole.web:app --port 8000"""
import asyncio
import hmac
import json
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ole.answer import stream_answer
from ole.config import LEFSE_PASSWORD
from ole.db import connect
from ole.search import warmup

STATIC = Path(__file__).parent / "static"
MAX_QUESTION = 2000
PASSWORD = LEFSE_PASSWORD  # None = no login (local use)
FAIL_DELAY = 1.0  # seconds per wrong password, so guessing is slow
COOKIE = "lefse_session"



@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=warmup, daemon=True).start()
    yield


app = FastAPI(title="Lefse", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def session_token(password: str) -> str:
    # stateless: the cookie proves you knew the password; changing it logs everyone out
    return hmac.new(password.encode(), b"lefse-session-v1", "sha256").hexdigest()


@app.middleware("http")
async def require_login(request: Request, call_next):
    if PASSWORD is None or request.url.path == "/login":
        return await call_next(request)
    if hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), session_token(PASSWORD).encode()):
        return await call_next(request)
    if request.url.path.startswith(("/api/", "/static/")):
        return JSONResponse({"detail": "Log in first."}, 401)
    return RedirectResponse("/login", 303)


def login_page(error: str = "") -> str:
    html = (STATIC / "login.html").read_text()
    lefse = re.search(r'<symbol id="lefse".*?</symbol>', (STATIC / "index.html").read_text(), re.S).group()
    return html.replace("<!--LEFSE-->", lefse).replace("<!--ERROR-->", f'<p class="error">{error}</p>' if error else "")


@app.get("/login")
def login_form():
    return HTMLResponse(login_page())


@app.post("/login")
async def login(request: Request):
    password = parse_qs((await request.body()).decode()).get("password", [""])[0]
    if PASSWORD is not None and hmac.compare_digest(password.encode(), PASSWORD.encode()):
        response = RedirectResponse("/", 303)
        response.set_cookie(COOKIE, session_token(PASSWORD), max_age=30 * 86400, httponly=True, secure=True,
                            samesite="lax")
        return response
    await asyncio.sleep(FAIL_DELAY)
    return HTMLResponse(login_page("That's not it. Try again?"), 401)


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
