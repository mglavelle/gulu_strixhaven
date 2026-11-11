"""Admin usage tracking: logins, activity and questions, shown on the Usage page.

Kept deliberately light:
  * login_events  - every sign-in attempt (who, when, IP, device); failed ones too.
  * activity_days - per person per day, how many 5-minute windows they were active in,
                    plus users.last_seen. Updated at most once per 5 minutes per person.
  * ask_history   - every question and answer (see history.py).
Only the logins listed in ADMIN_USERS can open /usage (the DM is not an admin unless
listed). The DM's questions are hidden there unless "Show DM questions" is ticked, since
they can contain adventure spoilers and the admin is also a player.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db, indexing
from .auth import User
from .web import page_user, templates

router = APIRouter()

_last_touch: dict[int, float] = {}
TOUCH_EVERY = 300   # seconds


# --------------------------------------------------------------------------- recording
def client_ip(request: Request) -> str:
    """Real visitor IP behind Cloudflare + SWAG, else the direct connection."""
    for header in ("cf-connecting-ip", "x-real-ip"):
        v = request.headers.get(header)
        if v:
            return v.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def device(user_agent: str) -> str:
    ua = user_agent or ""
    kind = "Tablet" if "iPad" in ua or ("Android" in ua and "Mobile" not in ua) else \
        "Phone" if any(k in ua for k in ("iPhone", "Mobile", "Android")) else "Computer"
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                           ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "Browser")
    os_name = next((name for key, name in (("Windows", "Windows"), ("iPhone", "iOS"), ("iPad", "iPadOS"),
                                           ("Android", "Android"), ("Mac OS", "macOS"), ("Linux", "Linux")) if key in ua), "")
    return f"{kind} · {browser}" + (f" · {os_name}" if os_name else "")


def log_login(request: Request, user_id: int | None, ok: bool) -> None:
    conn = db.connect()
    conn.execute("INSERT INTO login_events(user_id, ok, ip, user_agent, at) VALUES (?,?,?,?,?)",
                 (user_id, 1 if ok else 0, client_ip(request),
                  (request.headers.get("user-agent") or "")[:300], indexing.now()))
    conn.commit()
    if ok and user_id:
        _last_touch.pop(user_id, None)
        touch(user_id)


def touch(user_id: int) -> None:
    """Mark someone active (cheap: at most one write per person per 5 minutes)."""
    now = time.time()
    if now - _last_touch.get(user_id, 0) < TOUCH_EVERY:
        return
    _last_touch[user_id] = now
    ts = indexing.now()
    conn = db.connect()
    conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (ts, user_id))
    conn.execute("""INSERT INTO activity_days(user_id, day, hits) VALUES (?, ?, 1)
                    ON CONFLICT(user_id, day) DO UPDATE SET hits = hits + 1""", (user_id, ts[:10]))
    conn.commit()


# --------------------------------------------------------------------------- page
def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def admin_only(user: User = Depends(page_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


@router.get("/usage")
def usage_page(request: Request, person: int | None = None, dm: int = 0, bad: int = 0,
               failed: int = 0, user: User = Depends(admin_only)):
    conn = db.connect()
    d7, d30 = _ago(7), _ago(30)
    one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]

    cards = {
        "active7": one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", d7),
        "people": one("SELECT COUNT(*) FROM users"),
        "questions7": one("SELECT COUNT(*) FROM ask_history WHERE created_at >= ?", d7),
        "avg7": one("SELECT AVG(total_s) FROM ask_history WHERE created_at >= ? AND status = 'ok'", d7),
        "down30": one("SELECT COUNT(*) FROM ask_history WHERE feedback = -1 AND created_at >= ?", d30),
        "up30": one("SELECT COUNT(*) FROM ask_history WHERE feedback = 1 AND created_at >= ?", d30),
        "failed_logins7": one("SELECT COUNT(*) FROM login_events WHERE ok = 0 AND at >= ?", d7),
        "errors7": one("SELECT COUNT(*) FROM ask_history WHERE status = 'error' AND created_at >= ?", d7),
    }

    people = conn.execute("""
        SELECT u.id, u.name, u.role, u.last_seen, c.name AS character,
          (SELECT MAX(at) FROM login_events l WHERE l.user_id = u.id AND l.ok = 1) AS last_login,
          (SELECT COUNT(*) FROM login_events l WHERE l.user_id = u.id AND l.ok = 1 AND l.at >= :d30) AS logins30,
          (SELECT COUNT(*) FROM activity_days a WHERE a.user_id = u.id AND a.day >= substr(:d30, 1, 10)) AS days30,
          (SELECT COUNT(*) FROM ask_history h WHERE h.user_id = u.id AND h.created_at >= :d7) AS q7,
          (SELECT COUNT(*) FROM ask_history h WHERE h.user_id = u.id AND h.created_at >= :d30) AS q30,
          (SELECT COUNT(*) FROM ask_history h WHERE h.user_id = u.id) AS qall,
          (SELECT COUNT(*) FROM ask_history h WHERE h.user_id = u.id AND h.feedback = 1) AS up,
          (SELECT COUNT(*) FROM ask_history h WHERE h.user_id = u.id AND h.feedback = -1) AS down
        FROM users u LEFT JOIN characters c ON c.id = u.character_id
        ORDER BY COALESCE(u.last_seen, '') DESC, u.name COLLATE NOCASE""", {"d7": d7, "d30": d30}).fetchall()

    per_day = dict(conn.execute("""SELECT substr(created_at, 1, 10) AS day, COUNT(*) FROM ask_history
                                   WHERE created_at >= ? GROUP BY day""", (_ago(14),)).fetchall())
    days = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

    where, params = ["1=1"], []
    if not dm:
        where.append("h.role != 'dm'")
    if person:
        where.append("h.user_id = ?"); params.append(person)
    if bad:
        where.append("h.feedback = -1")
    if not failed:
        where.append("h.status = 'ok'")
    questions = [dict(r) | {"sources": [s.get("citation") for s in json.loads(r["sources_json"] or "[]")]}
                 for r in conn.execute(f"""
        SELECT h.*, u.name AS asker FROM ask_history h JOIN users u ON u.id = h.user_id
        WHERE {' AND '.join(where)} ORDER BY h.id DESC LIMIT 200""", params).fetchall()]
    hidden_dm = 0 if dm else one("SELECT COUNT(*) FROM ask_history WHERE role = 'dm'")

    logins = [dict(r) | {"device": device(r["user_agent"])} for r in conn.execute("""
        SELECT l.*, u.name FROM login_events l LEFT JOIN users u ON u.id = l.user_id
        ORDER BY l.id DESC LIMIT 60""").fetchall()]

    return templates.TemplateResponse(request, "usage.html", {
        "role": user.role, "cards": cards, "people": people, "days": days, "per_day": per_day,
        "max_day": max(per_day.values(), default=0), "questions": questions, "logins": logins,
        "hidden_dm": hidden_dm,
        "f": {"person": person, "dm": dm, "bad": bad, "failed": failed}})
