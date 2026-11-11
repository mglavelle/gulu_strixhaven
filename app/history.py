"""Ask-page history: every finished answer is saved so people can reopen it instead of
asking (and waiting) again.

Who sees what:
  * Your own history ("Mine") - always, including answers built from your private notes.
  * The "Group" view - a player's answers that used only material every player may see
    (rules, revealed notes, the party). Never the DM's questions (they may contain
    adventure secrets) and never an answer that drew on someone's private notes.
"""
import json
import re
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException

from . import db, indexing
from .auth import User, require_user

router = APIRouter()

KEEP_PER_USER = 150        # oldest entries beyond this are pruned
LIST_LIMIT = 60


def _shareable(user: User, sources: list[dict]) -> bool:
    if user.is_dm:
        return False
    for s in sources:
        if s.get("dm_only"):
            return False
        if (s.get("heading_path") or "").startswith("Personal notes"):
            return False
    return True


def save(user: User, question: str, answer: str, sources: list[dict], scope: str) -> None:
    if not answer.strip():
        return
    conn = db.connect()
    conn.execute("""INSERT INTO ask_history(user_id, role, question, answer, sources_json, scope,
                                            shared, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                 (user.id, user.role, question.strip(), answer.strip(), json.dumps(sources), scope,
                  1 if _shareable(user, sources) else 0, indexing.now()))
    conn.execute("""DELETE FROM ask_history WHERE user_id = ? AND id NOT IN
                    (SELECT id FROM ask_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)""",
                 (user.id, user.id, KEEP_PER_USER))
    conn.commit()


def record(stream: Iterator[str], user: User, question: str, scope: str) -> Iterator[str]:
    """Pass the NDJSON answer stream through unchanged, and save it once it finishes
    cleanly (stopped or failed answers aren't saved)."""
    sources, text, ok = [], [], False
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
            ok = True
    if ok:
        save(user, question, "".join(text), sources, scope)


def _visible(conn, entry_id: int, user: User):
    row = conn.execute("SELECT h.*, u.name AS asker FROM ask_history h JOIN users u ON u.id = h.user_id "
                       "WHERE h.id = ?", (entry_id,)).fetchone()
    if not row or not (row["user_id"] == user.id or row["shared"]):
        raise HTTPException(status_code=404, detail="Not found")
    return row


def _snippet(answer: str) -> str:
    plain = re.sub(r"\[\d+\]|[#*_`>|]", "", answer)
    return re.sub(r"\s+", " ", plain).strip()[:140]


@router.get("/api/history")
def list_history(view: str = "mine", user: User = Depends(require_user)):
    conn = db.connect()
    if view == "group":
        rows = conn.execute("""SELECT h.id, h.question, h.answer, h.created_at, h.user_id, u.name AS asker
                               FROM ask_history h JOIN users u ON u.id = h.user_id
                               WHERE h.shared = 1 ORDER BY h.id DESC LIMIT ?""", (LIST_LIMIT * 2,)).fetchall()
        # Same question asked by several people: show it once (the newest answer).
        seen, unique = set(), []
        for r in rows:
            key = re.sub(r"[^a-z0-9 ]", "", r["question"].lower()).strip()
            if key not in seen:
                seen.add(key); unique.append(r)
        rows = unique[:LIST_LIMIT]
    else:
        rows = conn.execute("""SELECT h.id, h.question, h.answer, h.created_at, h.user_id, u.name AS asker
                               FROM ask_history h JOIN users u ON u.id = h.user_id
                               WHERE h.user_id = ? ORDER BY h.id DESC LIMIT ?""",
                            (user.id, LIST_LIMIT)).fetchall()
    return [{"id": r["id"], "question": r["question"], "snippet": _snippet(r["answer"]),
             "asker": r["asker"], "mine": r["user_id"] == user.id, "created_at": r["created_at"],
             "can_delete": r["user_id"] == user.id or user.is_dm} for r in rows]


@router.get("/api/history/{entry_id}")
def get_history(entry_id: int, user: User = Depends(require_user)):
    r = _visible(db.connect(), entry_id, user)
    return {"id": r["id"], "question": r["question"], "answer": r["answer"],
            "sources": json.loads(r["sources_json"]), "asker": r["asker"],
            "mine": r["user_id"] == user.id, "created_at": r["created_at"], "scope": r["scope"]}


@router.delete("/api/history/{entry_id}")
def delete_history(entry_id: int, user: User = Depends(require_user)):
    conn = db.connect()
    r = _visible(conn, entry_id, user)
    if not (r["user_id"] == user.id or user.is_dm):
        raise HTTPException(status_code=403, detail="Not yours")
    conn.execute("DELETE FROM ask_history WHERE id = ?", (entry_id,))
    conn.commit()
    return {"ok": True}
