"""Notes pages.

Two kinds of entries share the `entries` table:
  * Campaign notes (owner_id NULL): the DM writes them; players see revealed ones'
    player text only.
  * Personal notes (owner_id = a player): only that player can see or change them. The DM
    can read (not edit) one if the player ticks "Share with DM".
Every save re-indexes the entry so the Ask page can find it (see indexing.py).
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db, indexing
from .auth import User
from .web import page_user, templates

router = APIRouter()

TYPES = indexing.TYPE_LABELS
PERSONAL_TYPES = {k: v for k, v in TYPES.items() if k != "session"}
PLURAL = {"session": "Sessions", "npc": "NPCs", "location": "Locations", "faction": "Factions",
          "quest": "Quests", "item": "Items", "lore": "Lore"}
BLANK = {"id": None, "type": "npc", "name": "", "player_text": "", "dm_text": "",
         "revealed": 0, "session_date": None, "tags": "", "owner_id": None, "shared_with_dm": 0}


def _ctx(user: User, **kw):
    return {"role": user.role, "user": user, "types": TYPES, "personal_types": PERSONAL_TYPES,
            "label_plural": PLURAL, **kw}


def can_view(e, user: User) -> bool:
    if e["owner_id"] is None:
        return user.is_dm or bool(e["revealed"])
    return e["owner_id"] == user.id or (user.is_dm and bool(e["shared_with_dm"]))


def can_edit(e, user: User) -> bool:
    return (user.is_dm and e["owner_id"] is None) or e["owner_id"] == user.id


def _get(conn, entry_id: int, user: User, edit: bool = False):
    e = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if not e or not can_view(e, user):
        raise HTTPException(status_code=404, detail="Not found")   # don't reveal it exists
    if edit and not can_edit(e, user):
        raise HTTPException(status_code=403, detail="Not yours to change")
    return e


def _owner_names(conn) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, name FROM users")}


@router.get("/notes")
def list_notes(request: Request, type: str | None = None, q: str | None = None,
               mine: int = 0, shared: int = 0, user: User = Depends(page_user)):
    conn = db.connect()
    if user.is_dm:
        scope_sql = "(owner_id IS NULL)" if not shared else "(owner_id IS NOT NULL AND shared_with_dm = 1)"
        scope_params: list = []
    else:
        scope_sql = "(owner_id = ?)" if mine else "(owner_id IS NULL AND revealed = 1)"
        scope_params = [user.id] if mine else []
    counts = {r[0]: r[1] for r in conn.execute(
        f"SELECT type, COUNT(*) FROM entries WHERE {scope_sql} GROUP BY type", scope_params)}
    mine_count = conn.execute("SELECT COUNT(*) FROM entries WHERE owner_id=?", (user.id,)).fetchone()[0]
    shared_count = conn.execute("SELECT COUNT(*) FROM entries WHERE owner_id IS NOT NULL "
                                "AND shared_with_dm=1").fetchone()[0] if user.is_dm else 0
    where, params = [scope_sql], list(scope_params)
    if type in TYPES:
        where.append("type = ?"); params.append(type)
    else:
        type = None
    if q:
        fields = "name || ' ' || player_text || ' ' || tags" + (
            " || ' ' || dm_text" if user.is_dm and not shared else "")
        where.append(f"({fields}) LIKE ?"); params.append(f"%{q}%")
    entries = conn.execute(f"""
        SELECT * FROM entries WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN type='session' THEN 0 ELSE 1 END,
                 COALESCE(session_date, '') DESC, name COLLATE NOCASE""", params).fetchall()
    return templates.TemplateResponse(request, "notes_list.html", _ctx(
        user, entries=entries, counts=counts, type=type, q=q, mine=mine, shared=shared,
        mine_count=mine_count, shared_count=shared_count, owners=_owner_names(conn)))


@router.get("/notes/new")
def new_note(request: Request, type: str = "npc", name: str = "", player_text: str = "",
             dm_text: str = "", tags: str = "", user: User = Depends(page_user)):
    """Query parameters pre-fill the form (used by a session log entry's "→ Note").
    The DM creates campaign notes; players create their own personal notes."""
    allowed = TYPES if user.is_dm else PERSONAL_TYPES
    e = {**BLANK, "type": type if type in allowed else "npc", "name": name[:120],
         "player_text": player_text, "dm_text": dm_text if user.is_dm else "", "tags": tags,
         "owner_id": None if user.is_dm else user.id}
    return templates.TemplateResponse(request, "note_edit.html", _ctx(user, e=e))


def _form_values(type, name, player_text, dm_text, revealed, session_date, tags, personal: bool):
    if type not in (PERSONAL_TYPES if personal else TYPES):
        raise HTTPException(status_code=400, detail="Unknown type")
    name = name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    return (type, name, player_text.strip(), "" if personal else dm_text.strip(),
            0 if personal else (1 if revealed else 0),
            (session_date or None) if type == "session" else None,
            ", ".join(t.strip() for t in tags.split(",") if t.strip()))


@router.post("/notes/new")
def create_note(type: str = Form(...), name: str = Form(...), player_text: str = Form(""),
                dm_text: str = Form(""), revealed: str | None = Form(None),
                session_date: str | None = Form(None), tags: str = Form(""),
                shared_with_dm: str | None = Form(None), user: User = Depends(page_user)):
    conn = db.connect()
    personal = not user.is_dm
    vals = _form_values(type, name, player_text, dm_text, revealed, session_date, tags, personal)
    ts = indexing.now()
    cur = conn.execute("""INSERT INTO entries(type, name, player_text, dm_text, revealed,
                          session_date, tags, owner_id, shared_with_dm, created_at, updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (*vals, user.id if personal else None,
                        1 if (personal and shared_with_dm) else 0, ts, ts))
    conn.commit()
    indexing.index_entry(conn, conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone())
    return RedirectResponse(f"/notes/{cur.lastrowid}", status_code=303)


@router.get("/notes/{entry_id}")
def view_note(request: Request, entry_id: int, user: User = Depends(page_user)):
    conn = db.connect()
    e = _get(conn, entry_id, user)
    return templates.TemplateResponse(request, "note_view.html", _ctx(
        user, e=e, editable=can_edit(e, user), owner=_owner_names(conn).get(e["owner_id"])))


@router.get("/notes/{entry_id}/edit")
def edit_note(request: Request, entry_id: int, user: User = Depends(page_user)):
    e = _get(db.connect(), entry_id, user, edit=True)
    return templates.TemplateResponse(request, "note_edit.html", _ctx(user, e=e))


@router.post("/notes/{entry_id}/edit")
def save_note(entry_id: int, type: str = Form(...), name: str = Form(...),
              player_text: str = Form(""), dm_text: str = Form(""),
              revealed: str | None = Form(None), session_date: str | None = Form(None),
              tags: str = Form(""), shared_with_dm: str | None = Form(None),
              user: User = Depends(page_user)):
    conn = db.connect()
    e = _get(conn, entry_id, user, edit=True)
    personal = e["owner_id"] is not None
    vals = _form_values(type, name, player_text, dm_text, revealed, session_date, tags, personal)
    conn.execute("""UPDATE entries SET type=?, name=?, player_text=?, dm_text=?, revealed=?,
                    session_date=?, tags=?, shared_with_dm=?, updated_at=? WHERE id=?""",
                 (*vals, 1 if (personal and shared_with_dm) else 0, indexing.now(), entry_id))
    conn.commit()
    indexing.index_entry(conn, conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone())
    return RedirectResponse(f"/notes/{entry_id}", status_code=303)


@router.post("/notes/{entry_id}/reveal")
def toggle_reveal(entry_id: int, back: str = Form("/notes"), user: User = Depends(page_user)):
    conn = db.connect()
    e = _get(conn, entry_id, user, edit=True)
    if not user.is_dm or e["owner_id"] is not None:
        raise HTTPException(status_code=403, detail="DM only")
    conn.execute("UPDATE entries SET revealed=?, updated_at=? WHERE id=?",
                 (0 if e["revealed"] else 1, indexing.now(), entry_id))
    conn.commit()
    indexing.index_entry(conn, conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone())
    ok = back.startswith(("/notes", "/sessions")) and not back.startswith("//")
    return RedirectResponse(back if ok else "/notes", status_code=303)   # no open redirects


@router.post("/notes/{entry_id}/delete")
def delete_note(entry_id: int, user: User = Depends(page_user)):
    conn = db.connect()
    e = _get(conn, entry_id, user, edit=True)
    indexing.remove_entry(conn, entry_id)
    conn.execute("DELETE FROM session_events WHERE session_id=?", (entry_id,))
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    conn.commit()
    return RedirectResponse("/notes?mine=1" if e["owner_id"] else "/notes", status_code=303)
