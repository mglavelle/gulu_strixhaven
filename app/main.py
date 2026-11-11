"""FastAPI app: login, Ask page, streaming answer API."""
import secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import accounts, answer, auth, config, db, history, notes, party, sessions, status, users
from .web import LoginRequired, page_user, templates

HERE = Path(__file__).resolve().parent
app = FastAPI(title="D&D Companion", docs_url=None, redoc_url=None)
app.include_router(notes.router)
app.include_router(sessions.router)
app.include_router(party.router)
app.include_router(accounts.router)
app.include_router(status.router)
app.include_router(history.router)
users.ensure_dm(db.connect())          # DM account follows DM_PASSWORD in .env


@app.exception_handler(LoginRequired)
def _to_login(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or secrets.token_urlsafe(32),  # random = logout on restart
    session_cookie="dndc_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

SCOPES = {"all": None, "rules": ("rules",), "adventure": ("adventure",), "notes": ("notes",)}


# --------------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: auth.User = Depends(page_user)):
    return templates.TemplateResponse(request, "ask.html", {
        "role": user.role, "model": config.CHAT_MODEL})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "configured": auth.any_logins()})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(...)):
    user = auth.check_password(password)
    if not user:
        return templates.TemplateResponse(request, "login.html", {
            "error": "That password didn't work.",
            "configured": auth.any_logins()}, status_code=401)
    request.session.clear()
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- API
class Turn(BaseModel):
    role: str
    content: str = Field(max_length=8000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=12)
    scope: str = "all"
    careful: bool = False


@app.post("/api/ask")
def ask(req: AskRequest, user: auth.User = Depends(auth.require_user)):
    kinds = SCOPES.get(req.scope)
    if not user.is_dm and req.scope == "adventure":
        kinds = SCOPES["rules"]          # players never search the adventure book
    stream = answer.answer_stream(
        req.question.strip(), role=user.role, kinds=kinds, user=user,
        history=[t.model_dump() for t in req.history], think=req.careful)
    stream = history.record(stream, user, req.question, req.scope)   # saved for the Recent panel
    return StreamingResponse(stream, media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})  # nginx/SWAG: don't buffer


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><path d="M16 3 29 10v12L16 29 3 22V10z" '
           'fill="#b8862b"/><path d="M16 3v26M3 10l13 7 13-7" stroke="#fff" stroke-width="1.5" fill="none"/></svg>')


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/health")
def health():
    try:
        httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5).raise_for_status()
        return {"ok": True}
    except httpx.HTTPError:
        return {"ok": False, "error": "Ollama not reachable"}
