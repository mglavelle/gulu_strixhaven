"""Campaign status (DM only): "where we are / what's next", plus an AI catch-up recap.

The status note is the DM's running bookmark in the story. Each save adds a row to
`campaign_status` (latest = current, older rows = history). It's indexed as a DM-only
search chunk and is the starting point for session prep.

"Catch me up" reads the whole campaign record - every session's recap, DM notes and log,
the status note, open quests, the adventure book and the party - and writes the story so
far, where we are, open threads and what comes next. It never saves anything itself.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from . import answer, config, db, indexing, search
from .answer import est_tokens
from .indexing import event_line
from .web import dm_only, templates

router = APIRouter()

CATCHUP_SYSTEM = """You are helping the Dungeon Master of a D&D 5e campaign set in Strixhaven \
(a university of magic) catch up on the campaign. You may see everything, including \
adventure secrets.
{party}
Using the session records, the DM's status note, open quests and the adventure-book \
passages, write a catch-up briefing in Markdown with exactly these sections:
## The story so far
(chronological, one short paragraph or a few bullets per session; name who did what)
## Where we are now
(where the party is in the story and in the adventure book - chapter/section if the \
passages show it - and the situation they're in right now)
## Open threads
(bullets: unresolved mysteries, promises, NPCs waiting on the party, loot not yet used)
## What's coming next
(what the adventure book has next from this point, plus 2-3 directions the DM could take)

Cite adventure passages as [1], [2]... Only state campaign events that appear in the \
records; if the records are thin, say so. Refer to player characters by name or "they". \
Be concise and skimmable; no preamble."""


# --------------------------------------------------------------------------- data
def current(conn):
    return conn.execute("SELECT * FROM campaign_status ORDER BY id DESC LIMIT 1").fetchone()


def index_status(conn) -> None:
    s = current(conn)
    chunks = []
    if s and (s["where_text"] or s["next_text"]):
        chunks.append({
            "heading_path": "Campaign notes > Campaign status", "title": "Campaign status",
            "visibility": "dm",
            "text": (f"## Campaign status (DM's note, updated {s['created_at'][:10]})\n\n"
                     f"**Where we are:**\n{s['where_text'] or '(not written)'}\n\n"
                     f"**What's next:**\n{s['next_text'] or '(not written)'}")})
    indexing.replace_chunks(conn, "Notes", "status", 0, chunks)


def status_block(conn) -> str:
    """The status note as prompt text (for session prep)."""
    s = current(conn)
    if not s or not (s["where_text"] or s["next_text"]):
        return ""
    return (f"Where we are: {s['where_text'] or '(not written)'}\n"
            f"What's next: {s['next_text'] or '(not written)'}\n(DM's note from {s['created_at'][:10]})")


# --------------------------------------------------------------------------- pages
@router.get("/status")
def status_page(request: Request, role: str = Depends(dm_only)):
    conn = db.connect()
    history = conn.execute("SELECT * FROM campaign_status ORDER BY id DESC LIMIT 11").fetchall()
    sessions = conn.execute("SELECT COUNT(*) FROM entries WHERE type='session'").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
    return templates.TemplateResponse(request, "status.html", {
        "role": role, "status": history[0] if history else None, "history": history[1:],
        "sessions": sessions, "events": events, "saved": request.query_params.get("saved")})


@router.post("/status")
def save_status(where_text: str = Form(""), next_text: str = Form(""), back: str = Form("/status"),
                role: str = Depends(dm_only)):
    conn = db.connect()
    cur = current(conn)
    where_text, next_text = where_text.strip(), next_text.strip()
    if not cur or (cur["where_text"], cur["next_text"]) != (where_text, next_text):
        conn.execute("INSERT INTO campaign_status(where_text, next_text, created_at) VALUES (?,?,?)",
                     (where_text, next_text, indexing.now()))
        conn.commit()
        index_status(conn)
    target = back if back in ("/status", "/sessions") else "/status"
    return RedirectResponse(f"{target}?saved=1#status", status_code=303)


# --------------------------------------------------------------------------- AI
def _sessions_text(conn, budget: int) -> tuple[str, int, int]:
    """All sessions oldest -> newest, compressed to fit `budget` tokens. The most recent
    session keeps the most detail; the oldest are dropped first if needed."""
    rows = conn.execute("""SELECT * FROM entries WHERE type='session'
                           ORDER BY COALESCE(session_date, created_at), id""").fetchall()
    blocks = []
    for i, s in enumerate(rows):
        latest = i == len(rows) - 1
        events = conn.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id",
                              (s["id"],)).fetchall()
        log = "\n".join(event_line(e) for e in events)
        parts = [f"### {s['name']}{' (' + s['session_date'] + ')' if s['session_date'] else ''}"]
        if s["player_text"]:
            parts.append("Recap: " + s["player_text"][: 1500 if latest else 600])
        if s["dm_text"]:
            parts.append("DM notes: " + s["dm_text"][: 1500 if latest else 400])
        if log:
            parts.append("Log:\n" + log[: 2500 if latest else 900])
        blocks.append("\n".join(parts))
    dropped = 0
    while blocks and sum(est_tokens(b) for b in blocks) > budget:
        blocks.pop(0); dropped += 1
    text = "\n\n".join(blocks)
    if dropped:
        text = f"({dropped} earliest session(s) left out for length.)\n\n" + text
    return text, len(rows), dropped


@router.post("/api/status/catchup")
def catchup(careful: bool = False, role: str = Depends(dm_only)):
    conn = db.connect()
    party = answer._party(conn)
    system = CATCHUP_SYSTEM.format(party=answer.party_roster(party))
    status = status_block(conn)
    quests = conn.execute("SELECT name, player_text, dm_text FROM entries WHERE type='quest' "
                          "AND owner_id IS NULL ORDER BY updated_at DESC LIMIT 8").fetchall()
    quest_text = "\n".join(f"- {q['name']}: {(q['dm_text'] or q['player_text'])[:250]}" for q in quests)

    budget = config.CHAT_CTX - 1700 - est_tokens(system) - est_tokens(status) - est_tokens(quest_text)
    sessions, n_sessions, _ = _sessions_text(conn, int(budget * 0.6))
    used = est_tokens(sessions)
    # Locate the party in the adventure from the status note and the latest session.
    query = (status + "\n" + sessions[-1000:]).strip() or "start of the adventure orientation"
    kept = []
    for h in search.search(query, role="dm", k=5, conn=conn, kinds=("adventure",)):
        cost = est_tokens(h.heading_path + h.text) + 20
        if used + cost > budget:
            break
        kept.append(h); used += cost
    passages = "\n\n---\n\n".join(f"[{i}] {h.heading_path} ({h.location})\n{h.text}"
                                  for i, h in enumerate(kept, 1))
    user = (f"# Sessions so far ({n_sessions}), oldest first\n{sessions or '(No sessions logged yet.)'}\n"
            + (f"\n# DM's current status note\n{status}\n" if status else "")
            + (f"\n# Open quests\n{quest_text}\n" if quest_text else "")
            + f"\n# Adventure book passages\n{passages or '(none found)'}\n\nWrite the briefing now.")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    from .sessions import _stream
    return _stream(answer.stream_events(msgs, kept, think=careful))
