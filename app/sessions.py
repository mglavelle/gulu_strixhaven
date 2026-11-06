"""Session tracker: a log of what happened at the table, plus AI prep and recap drafts.

A session is an `entries` row with type 'session' (so it also appears under Notes).
Its log lives in `session_events`. Players see a session only when it is revealed, and
then only its revealed events and its player recap.

AI drafts never save themselves: the DM reads them and clicks Save.
  * Prep  - from the previous sessions, open quests, the adventure book and the party.
  * Recap - from REVEALED events only, so the player recap can't leak DM secrets.
"""
import json
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from . import answer, db, indexing, search
from .answer import est_tokens
from .indexing import EVENT_KINDS, event_line
from .web import dm_only, page_role, templates
from . import config

router = APIRouter()

PREP_SYSTEM = """You are helping the Dungeon Master of a D&D 5e campaign set in Strixhaven \
(a university of magic) prepare the next session. You may see everything, including \
adventure secrets.
{party}
Write a practical, skimmable session plan in Markdown with these sections:
## Where we left off
## Goals for this session
## Scenes (3-5 likely scenes in order; for each: what happens, who is there, how it can go)
## NPCs to have ready (name, what they want, a voice/mannerism tip)
## Encounters and checks (with DCs or stat blocks from the passages)
## Spotlight hooks (one personal hook for each character, using their sheet and backstory)
## Loose threads to pick up

Ground the plan in the session history and the adventure-book passages. Cite adventure \
passages as [1], [2]... Where you invent something, it's fine - mark it "(idea)". Be \
specific and concise; no preamble. Refer to player characters by name or "they"; don't \
guess their gender."""

RECAP_SYSTEM = """You write the "previously on..." recap for a D&D 5e campaign set in \
Strixhaven, read aloud to the players at the start of the next session.
{party}
Use ONLY the events listed - they are what the players know. Write 120-250 words, past \
tense, second person plural or third person, vivid but factual, naming the characters \
who did things. Then add a short "Loose ends" bullet list of open questions the party \
has. Don't invent events, secrets or names that aren't in the list. Refer to characters \
by name or "they"; don't guess anyone's gender."""


# --------------------------------------------------------------------------- helpers
def _session(conn, session_id: int, role: str):
    s = conn.execute("SELECT * FROM entries WHERE id=? AND type='session'", (session_id,)).fetchone()
    if not s or (role != "dm" and not s["revealed"]):
        raise HTTPException(status_code=404, detail="Not found")
    return s


def _events(conn, session_id: int, role: str):
    sql = "SELECT * FROM session_events WHERE session_id=?"
    if role != "dm":
        sql += " AND revealed=1"
    return conn.execute(sql + " ORDER BY id DESC", (session_id,)).fetchall()


def _ordered_sessions(conn):
    return conn.execute("""SELECT * FROM entries WHERE type='session'
                           ORDER BY COALESCE(session_date, created_at) DESC, id DESC""").fetchall()


def _party_names(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM characters ORDER BY name COLLATE NOCASE")]


def _trim(text: str, max_tokens: int) -> str:
    if est_tokens(text) <= max_tokens:
        return text
    return text[: max_tokens * 3].rsplit("\n", 1)[0] + "\n…(trimmed)"


def _session_block(conn, s, full: bool) -> str:
    events = conn.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id",
                          (s["id"],)).fetchall()
    parts = [f"### {s['name']}{' (' + s['session_date'] + ')' if s['session_date'] else ''}"]
    if s["player_text"]:
        parts.append(f"Recap: {s['player_text']}")
    if full and s["dm_text"]:
        parts.append(f"DM notes: {s['dm_text']}")
    if events:
        parts.append("Log:\n" + "\n".join(event_line(e) for e in events))
    return "\n".join(parts)


def _stream(gen):
    return StreamingResponse(gen, media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- pages
@router.get("/sessions")
def list_sessions(request: Request, role: str = Depends(page_role)):
    conn = db.connect()
    rows = [s for s in _ordered_sessions(conn) if role == "dm" or s["revealed"]]
    counts = dict(conn.execute("SELECT session_id, COUNT(*) FROM session_events GROUP BY session_id"))
    from .status import current as current_status
    return templates.TemplateResponse(request, "sessions_list.html", {
        "role": role, "sessions": rows, "counts": counts,
        "status": current_status(conn) if role == "dm" else None})


@router.post("/sessions/new")
def new_session(role: str = Depends(dm_only)):
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM entries WHERE type='session'").fetchone()[0] + 1
    ts = indexing.now()
    cur = conn.execute("""INSERT INTO entries(type, name, player_text, dm_text, revealed,
                          session_date, tags, created_at, updated_at)
                          VALUES ('session', ?, '', '', 0, ?, '', ?, ?)""",
                       (f"Session {n}", date.today().isoformat(), ts, ts))
    conn.commit()
    indexing.index_entry(conn, conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone())
    return RedirectResponse(f"/sessions/{cur.lastrowid}", status_code=303)


@router.get("/sessions/{session_id}")
def view_session(request: Request, session_id: int, role: str = Depends(page_role)):
    conn = db.connect()
    s = _session(conn, session_id, role)
    return templates.TemplateResponse(request, "session_view.html", {
        "role": role, "s": s, "events": _events(conn, session_id, role),
        "kinds": EVENT_KINDS, "party": _party_names(conn)})


# --------------------------------------------------------------------------- log
def _event_values(kind: str, characters: list[str], text: str, revealed):
    if kind not in EVENT_KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind")
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    return kind, ", ".join(c.strip() for c in characters if c.strip()), text, 1 if revealed else 0


@router.post("/sessions/{session_id}/events")
async def add_event(request: Request, session_id: int, role: str = Depends(dm_only)):
    form = await request.form()
    conn = db.connect()
    _session(conn, session_id, role)
    vals = _event_values(form.get("kind", "other"), form.getlist("characters"),
                         form.get("text", ""), form.get("revealed"))
    conn.execute("""INSERT INTO session_events(session_id, kind, characters, text, revealed, created_at)
                    VALUES (?,?,?,?,?,?)""", (session_id, *vals, indexing.now()))
    conn.commit()
    indexing.index_session(conn, session_id)
    return RedirectResponse(f"/sessions/{session_id}#log", status_code=303)


@router.post("/sessions/{session_id}/events/{event_id}")
async def edit_event(request: Request, session_id: int, event_id: int, role: str = Depends(dm_only)):
    form = await request.form()
    conn = db.connect()
    action = form.get("action", "save")
    if action == "delete":
        conn.execute("DELETE FROM session_events WHERE id=? AND session_id=?", (event_id, session_id))
    elif action == "reveal":
        conn.execute("UPDATE session_events SET revealed = 1 - revealed WHERE id=? AND session_id=?",
                     (event_id, session_id))
    else:
        vals = _event_values(form.get("kind", "other"), form.getlist("characters"),
                             form.get("text", ""), form.get("revealed"))
        conn.execute("""UPDATE session_events SET kind=?, characters=?, text=?, revealed=?
                        WHERE id=? AND session_id=?""", (*vals, event_id, session_id))
    conn.commit()
    indexing.index_session(conn, session_id)
    return RedirectResponse(f"/sessions/{session_id}#ev{event_id}", status_code=303)


@router.post("/sessions/{session_id}/delete")
def delete_session(session_id: int, role: str = Depends(dm_only)):
    """Delete a session, its log and its search entries (for accidental sessions)."""
    conn = db.connect()
    _session(conn, session_id, role)
    indexing.remove_entry(conn, session_id)          # session + session-log chunks
    conn.execute("DELETE FROM session_events WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM entries WHERE id=?", (session_id,))
    conn.commit()
    return RedirectResponse("/sessions", status_code=303)


@router.post("/sessions/{session_id}/save-draft")
def save_draft(session_id: int, target: str = Form(...), text: str = Form(...),
               mode: str = Form("append"), role: str = Depends(dm_only)):
    """Save an AI draft into the session: prep -> DM notes, recap -> player recap."""
    if target not in ("dm_text", "player_text"):
        raise HTTPException(status_code=400, detail="Bad target")
    conn = db.connect()
    s = _session(conn, session_id, role)
    new = text.strip() if mode == "replace" or not s[target].strip() else f"{s[target].rstrip()}\n\n{text.strip()}"
    conn.execute(f"UPDATE entries SET {target}=?, updated_at=? WHERE id=?", (new, indexing.now(), session_id))
    conn.commit()
    indexing.index_entry(conn, _session(conn, session_id, role))
    return RedirectResponse(f"/sessions/{session_id}#{'prep' if target == 'dm_text' else 'recap'}",
                            status_code=303)


# --------------------------------------------------------------------------- AI drafts
@router.post("/api/sessions/{session_id}/prep")
def prep(session_id: int, careful: bool = False, role: str = Depends(dm_only)):
    conn = db.connect()
    current = _session(conn, session_id, role)
    party = answer._party(conn)
    system = PREP_SYSTEM.format(party=answer.party_roster(party))
    # Character sheets + backstories (DM) feed the spotlight hooks, if they fit.
    sheets = search.ref_hits(conn, "dm", "character")
    earlier = [s for s in _ordered_sessions(conn) if s["id"] != session_id
               and (s["session_date"] or "") <= (current["session_date"] or "9999")]
    history = "\n\n".join([_session_block(conn, s, full=(i == 0)) for i, s in enumerate(earlier[:3])])
    quests = conn.execute("SELECT name, player_text, dm_text FROM entries WHERE type='quest' "
                          "ORDER BY updated_at DESC LIMIT 8").fetchall()
    quest_text = "\n".join(f"- {q['name']}: {(q['dm_text'] or q['player_text'])[:300]}" for q in quests)

    # The DM's status note is the best "where we left off"; use it first.
    from .status import status_block
    status = status_block(conn)
    # Find where we are in the adventure from the status note + most recent session.
    query = ((status + "\n" + history[-1000:]).strip()
             or "start of the adventure first session orientation")
    hits = search.search(query, role="dm", k=5, conn=conn, kinds=("adventure",))
    budget = config.CHAT_CTX - 1600 - est_tokens(system) - est_tokens(status)
    history = _trim(history, budget // 2) if history else "(No earlier sessions logged yet - plan an opening session.)"
    used = est_tokens(history) + est_tokens(quest_text) + 300
    kept = []
    for h in hits:
        cost = est_tokens(h.heading_path + h.text) + 20
        if used + cost > budget:
            break
        kept.append(h); used += cost
    passages = "\n\n---\n\n".join(f"[{i}] {h.heading_path} ({h.location})\n{h.text}"
                                  for i, h in enumerate(kept, 1))
    # Backstories only (the roster already has class/level); short, and only if room is left.
    backstories = []
    for h in sheets:
        if h.visibility == "dm":
            t = _trim(h.text, 150)
            if used + est_tokens(t) > budget:
                break
            backstories.append(t); used += est_tokens(t)
    user = (f"# Session being prepared: {current['name']}"
            f"{' (' + current['session_date'] + ')' if current['session_date'] else ''}\n"
            + (f"DM's own notes for it so far:\n{current['dm_text']}\n" if current["dm_text"] else "")
            + (f"\n# DM's campaign status note (the most reliable 'where we left off')\n{status}\n"
               if status else "")
            + f"\n# Recent sessions (most recent first)\n{history}\n"
            + (f"\n# Open quests\n{quest_text}\n" if quest_text else "")
            + (f"\n# Character backstories (for spotlight hooks)\n" + "\n\n".join(backstories) + "\n"
               if backstories else "")
            + f"\n# Adventure book passages\n{passages or '(none found)'}\n\nWrite the plan now.")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return _stream(answer.stream_events(msgs, kept, think=careful))


@router.post("/api/sessions/{session_id}/recap")
def recap(session_id: int, role: str = Depends(dm_only)):
    conn = db.connect()
    s = _session(conn, session_id, role)
    events = [e for e in reversed(_events(conn, session_id, "dm")) if e["revealed"]]
    if not events:
        def none():
            yield json.dumps({"type": "error", "message":
                              "No revealed log entries yet - the recap only uses what players know."}) + "\n"
        return _stream(none())
    party = answer._party(conn)
    roster = "\nThe party: " + "; ".join(f"{c['name']} ({c['summary']})" for c in party) + "\n" if party else ""
    user = (f"Session: {s['name']}{' (' + s['session_date'] + ')' if s['session_date'] else ''}\n\n"
            "Events the players know about, in order:\n" + "\n".join(event_line(e) for e in events)
            + "\n\nWrite the recap now.")
    msgs = [{"role": "system", "content": RECAP_SYSTEM.format(party=roster)},
            {"role": "user", "content": _trim(user, config.CHAT_CTX - 1600)}]
    return _stream(answer.stream_events(msgs, []))
