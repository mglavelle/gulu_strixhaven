"""Ask-page history: every answer is saved so people can reopen it instead of asking
(and waiting) again, and so the admin can review usage on the Usage page.

Who sees what in the Recent panel:
  * "Mine" - your own finished answers, including ones built from your private notes.
  * "Group" - a player's finished answers that used only material every player may see
    (rules, revealed notes, the party). Never the DM's questions (they may contain
    adventure secrets) and never an answer that drew on someone's private notes.
Removing an entry from Recent hides it there; it stays in the admin's usage log.
Stopped and failed answers are logged for the admin but never shown under Recent.
"""
import json
import re
import time
from typing import Iterator

from fastapi import APIRouter, Body, Depends, HTTPException

from . import db, indexing
from .auth import User, require_user

router = APIRouter()

KEEP_PER_USER = 1000       # oldest entries beyond this are pruned (admin log included)
LIST_LIMIT = 60
VISIBLE = "h.hidden = 0 AND h.status = 'ok'"


def _shareable(user: User, sources: list[dict]) -> bool:
    if user.is_dm:
        return False
    for s in sources:
        if s.get("dm_only"):
            return False
        if (s.get("heading_path") or "").startswith("Personal notes"):
            return False
    return True


def save(user: User, question: str, answer: str, sources: list[dict], scope: str,
         status: str = "ok", total_s: float | None = None, careful: bool = False,
         error: str | None = None) -> int:
    conn = db.connect()
    cur = conn.execute("""
        INSERT INTO ask_history(user_id, role, question, answer, sources_json, scope, shared,
                                status, total_s, careful, error, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user.id, user.role, question.strip(), answer.strip(), json.dumps(sources), scope,
         1 if (status == "ok" and _shareable(user, sources)) else 0,
         status, total_s, 1 if careful else 0, error, indexing.now()))
    conn.execute("""DELETE FROM ask_history WHERE user_id = ? AND id NOT IN
                    (SELECT id FROM ask_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)""",
                 (user.id, user.id, KEEP_PER_USER))
    conn.commit()
    return cur.lastrowid


def record(stream: Iterator[str], user: User, question: str, scope: str,
           careful: bool = False) -> Iterator[str]:
    """Pass the NDJSON answer stream through unchanged and log it. A finished answer ends
    with an extra {"type": "saved", "id": ...} event so the page can attach 👍/👎."""
    sources, text, t0 = [], [], time.time()
    status, error, total_s = "stopped", None, None
    try:
        for line in stream:
            yield line
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "sources":
                sources = ev.get("sources") or []
            elif ev.get("type") == "token":
                text.append(ev.get("text", ""))
            elif ev.get("type") == "done":
                status, total_s = "ok", ev.get("total_s")
            elif ev.get("type") == "error":
                status, error = "error", ev.get("message")
        if status == "ok":
            entry_id = save(user, question, "".join(text), sources, scope, "ok", total_s, careful)
            yield json.dumps({"type": "saved", "id": entry_id}) + "\n"
            return
    except GeneratorExit:            # the person pressed Stop / closed the page
        status = "stopped"
    if status != "ok":
        save(user, question, "".join(text), sources, scope, status,
             round(time.time() - t0, 2), careful, error)


def _visible(conn, entry_id: int, user: User):
    row = conn.execute(f"""SELECT h.*, u.name AS asker FROM ask_history h JOIN users u ON u.id = h.user_id
                           WHERE h.id = ? AND {VISIBLE}""", (entry_id,)).fetchone()
    if not row or not (row["user_id"] == user.id or row["shared"]):
        raise HTTPException(status_code=404, detail="Not found")
    return row


def _snippet(answer: str) -> str:
    plain = re.sub(r"\[\d+\]|[#*_`>|]", "", answer)
    return re.sub(r"\s+", " ", plain).strip()[:140]


@router.get("/api/history")
def list_history(view: str = "mine", user: User = Depends(require_user)):
    conn = db.connect()
    base = f"""SELECT h.id, h.question, h.answer, h.created_at, h.user_id, h.feedback, u.name AS asker
               FROM ask_history h JOIN users u ON u.id = h.user_id WHERE {VISIBLE}"""
    if view == "group":
        rows = conn.execute(base + " AND h.shared = 1 ORDER BY h.id DESC LIMIT ?", (LIST_LIMIT * 2,)).fetchall()
        # Same question asked by several people: show it once (the newest answer).
        seen, unique = set(), []
        for r in rows:
            key = re.sub(r"[^a-z0-9 ]", "", r["question"].lower()).strip()
            if key not in seen:
                seen.add(key); unique.append(r)
        rows = unique[:LIST_LIMIT]
    else:
        rows = conn.execute(base + " AND h.user_id = ? ORDER BY h.id DESC LIMIT ?",
                            (user.id, LIST_LIMIT)).fetchall()
    return [{"id": r["id"], "question": r["question"], "snippet": _snippet(r["answer"]),
             "asker": r["asker"], "mine": r["user_id"] == user.id, "created_at": r["created_at"],
             "can_delete": r["user_id"] == user.id or user.is_dm} for r in rows]


@router.get("/api/history/{entry_id}")
def get_history(entry_id: int, user: User = Depends(require_user)):
    r = _visible(db.connect(), entry_id, user)
    return {"id": r["id"], "question": r["question"], "answer": r["answer"],
            "sources": json.loads(r["sources_json"]), "asker": r["asker"],
            "mine": r["user_id"] == user.id, "created_at": r["created_at"], "scope": r["scope"],
            "feedback": r["feedback"]}


@router.delete("/api/history/{entry_id}")
def delete_history(entry_id: int, user: User = Depends(require_user)):
    """Removes it from Recent for everyone (kept in the admin's usage log)."""
    conn = db.connect()
    r = _visible(conn, entry_id, user)
    if not (r["user_id"] == user.id or user.is_dm):
        raise HTTPException(status_code=403, detail="Not yours")
    conn.execute("UPDATE ask_history SET hidden = 1, shared = 0 WHERE id = ?", (entry_id,))
    conn.commit()
    return {"ok": True}


@router.post("/api/history/{entry_id}/feedback")
def feedback(entry_id: int, value: int = Body(..., embed=True), user: User = Depends(require_user)):
    """👍 (1) / 👎 (-1) / clear (0) on your own answer."""
    if value not in (1, -1, 0):
        raise HTTPException(status_code=400, detail="value must be 1, -1 or 0")
    conn = db.connect()
    r = conn.execute("SELECT user_id FROM ask_history WHERE id = ?", (entry_id,)).fetchone()
    if not r or r["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    conn.execute("UPDATE ask_history SET feedback = ? WHERE id = ?", (value or None, entry_id))
    conn.commit()
    return {"ok": True, "feedback": value or None}
