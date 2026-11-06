"""Shared bits for HTML pages: templates and login dependencies."""
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from . import auth

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=HERE / "templates")


def _asset_version() -> str:
    """Changes whenever a static file changes, so browsers never use stale CSS/JS."""
    return str(int(max(p.stat().st_mtime for p in (HERE / "static").iterdir())))


templates.env.globals["asset_v"] = _asset_version


class LoginRequired(Exception):
    """Raised by page dependencies; main.py turns it into a redirect to /login."""


def page_user(request: Request) -> auth.User:
    u = auth.current_user(request)
    if not u:
        raise LoginRequired()
    request.state.user = u          # templates show the name in the top bar
    return u


def page_role(request: Request) -> str:
    return page_user(request).role


def dm_only(request: Request) -> str:
    u = page_user(request)
    if not u.is_dm:
        raise HTTPException(status_code=403, detail="DM only")
    return u.role
