"""Turn a question into a cited answer: retrieve passages, build the prompt, stream.

The model sees only passages the asker's role may see (filtered in search.py), so the
prompt never has to be trusted to keep secrets.
"""
import json
import re
import time
from typing import Iterator

from . import config, db, llm, search

MAX_HISTORY_TURNS = 3       # previous Q/A pairs sent for follow-up questions

SYSTEM = """You are the table assistant for a Dungeons & Dragons 5e campaign set in \
Strixhaven, a university of magic. You are talking to {who}.
{party}
Passages come from the rulebook, the adventure book, the group's campaign notes (what \
actually happened at our table - prefer these over the book when they differ) and the \
party's character sheets.

How to answer:
- FACTS (rules, numbers, spells, stats, what happened in the campaign) must come from the \
numbered passages. Cite each one with its passage number in square brackets, like [2] or \
[1][3]. Never invent rules, numbers or page numbers. If a fact isn't in the passages, say \
so in one sentence and suggest what to look up.
- JUDGMENT AND IDEAS (tactics, roleplay, who would get along, NPC voices, plot hooks, \
"what should we do") are welcome: give a real, specific suggestion built from the \
passages - especially the character sheets - and say it's your suggestion. Don't refuse \
just because no rule covers it.
- You CAN see the party: the list above and the character sheet passages are real. Never \
say you can't see the party's characters.
- If the user questions or pushes back on your previous answer, reconsider it and answer \
differently - never repeat a previous answer word for word.
- Be concise and practical: lead with the direct answer, then the details that matter at \
the table. Quote exact rules text when precision matters. Short paragraphs or bullet \
lists, no preamble."""

WHO = {
    "dm": "the Dungeon Master, who may see everything, including adventure secrets",
    "player": "a player. Only rules and player-safe information is available to you",
}


PARTY_WORDS = re.compile(
    r"\b(party|our|we|us|group|team|characters?|members?|everyone|anyone|who (?:in|of|has|is|can)"
    r"|pcs?|adventurers?|players?)\b", re.I)


def _party(conn) -> list[dict]:
    rows = conn.execute("SELECT id, sheet_json FROM characters ORDER BY name COLLATE NOCASE")
    return [{"id": r[0], **json.loads(r[1])} for r in rows]


def party_roster(party: list[dict]) -> str:
    if not party:
        return ""
    lines = "\n".join(f"- {c['name']}: {c['summary']}"
                      f"{', ' + c['background'] + ' background' if c.get('background') else ''}"
                      f" (player: {c['player']})" for c in party)
    return f"\nThe party (their full character sheets are in the app):\n{lines}\n"


MY_WORDS = re.compile(r"\b(my|me|mine|i|i'm|i've|myself)\b", re.I)
NOTE_WORDS = re.compile(r"\b(notes?|wrote|written|writ\w*|theor\w*|jot\w*|remember|remind|"
                        r"journal|suspect\w*|clues?)\b", re.I)


def own_note_hits(conn, user, text: str, limit: int = 4) -> list[search.Hit]:
    """"What are my theories / what did I write?" - include the asker's latest own notes,
    even when the words don't match. Only ever this user's own `user:<id>` chunks."""
    if user is None or user.is_dm or not (MY_WORDS.search(text) and NOTE_WORDS.search(text)):
        return []
    rows = conn.execute("""SELECT c.*, s.key AS source_key, s.title AS source_title
                           FROM chunks c JOIN sources s ON s.id = c.source_id
                           JOIN entries e ON e.id = c.ref_id AND c.ref_type = 'entry'
                           WHERE c.visibility = ? ORDER BY e.updated_at DESC LIMIT ?""",
                        (f"user:{user.id}", limit)).fetchall()
    return [search.Hit(r["id"], r["source_key"], r["source_title"], r["heading_path"], r["title"],
                       0, 0, r["text"], 1.0, None, None, r["ref_type"], r["ref_id"], r["visibility"])
            for r in rows]


def system_prompt(role: str, party: list[dict], user=None) -> str:
    who = WHO[role]
    if user is not None and role != "dm":
        char = next((c for c in party if c["id"] == user.character_id), None)
        who = (f"a player named {user.name}"
               + (f", who plays {char['name']} ({char['summary']}). When they say \"I\", "
                  f"\"me\" or \"my character\" they mean {char['name']}" if char else "")
               + ". Only rules, player-safe information and their own notes are available to you")
    return SYSTEM.format(who=who, party=party_roster(party))


def party_hits(conn, role: str, text: str, party: list[dict], user=None) -> list[search.Hit]:
    """Party passages to always include: the overview for questions about the group,
    the asker's own sheet for "my/I" questions, and any character mentioned by name."""
    if not party:
        return []
    named = [c["id"] for c in party
             if any(len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", text, re.I)
                    for part in re.split(r"[\s.]+", c["name"]))]
    if user is not None and user.character_id and MY_WORDS.search(text) \
            and user.character_id not in named:
        named.insert(0, user.character_id)
    hits = search.ref_hits(conn, role, "character", named)
    if PARTY_WORDS.search(text) or len(named) > 1:
        hits = search.ref_hits(conn, role, "party") + hits
    return hits


def _retrieval_query(question: str, history: list[dict]) -> str:
    """Short follow-ups ("what about at higher levels?") need the previous question."""
    if history and len(question.split()) < 8:
        prev = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        return f"{prev} {question}"
    return question


def _passages_block(hits: list[search.Hit]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] {h.source_title}, {h.location} — {h.heading_path}\n{h.text}")
    return "\n\n---\n\n".join(parts)


def est_tokens(text: str) -> int:
    """Cheap, deliberately pessimistic token estimate. Character count covers prose;
    word count covers dense text (tables, dice notation, short tokens)."""
    return max(len(text) // 3, len(text.split()) * 13 // 10) + 4


def fit_budget(system: str, question: str, hits: list, history: list[dict]):
    """Trim history (oldest first), then passages (lowest-ranked first) until the prompt
    fits in CHAT_CTX minus room for the answer. Ollama would otherwise silently drop the
    *start* of the prompt - the instructions."""
    budget = config.CHAT_CTX - config.ANSWER_RESERVE
    hits, history = list(hits), list(history)

    def size():
        return (est_tokens(system) + est_tokens(question) + 40
                + sum(est_tokens(h["content"]) for h in history)
                + sum(est_tokens(h.heading_path + h.text) + 20 for h in hits))

    while size() > budget and history:
        history = history[2:] if len(history) >= 2 else []
    while size() > budget and len(hits) > 2:
        hits.pop()
    return hits, history


def clean_history(history: list[dict]) -> list[dict]:
    return [{"role": h["role"], "content": h["content"][:4000]}
            for h in history[-MAX_HISTORY_TURNS * 2:]
            if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str)]


def build_messages(question: str, hits: list[search.Hit], role: str,
                   history: list[dict], system: str | None = None) -> list[dict]:
    system = system or system_prompt(role, _party(db.connect()))
    hits, history = fit_budget(system, question, hits, clean_history(history))
    msgs = [{"role": "system", "content": system}, *history]
    if hits:
        user = f"Passages:\n\n{_passages_block(hits)}\n\n===\n\nQuestion: {question}"
    else:
        user = (f"Passages: (none found)\n\n===\n\nQuestion: {question}\n\n"
                "No passages matched. Say that you couldn't find this in the books.")
    msgs.append({"role": "user", "content": user})
    return msgs


def hit_payload(i: int, h: search.Hit) -> dict:
    link = {"entry": f"/notes/{h.ref_id}", "character": f"/party#c{h.ref_id}",
            "party": "/party", "session": f"/sessions/{h.ref_id}"}.get(h.ref_type or "")
    return {"n": i, "id": h.chunk_id, "source": h.source_key, "source_title": h.source_title,
            "citation": h.citation, "heading_path": h.heading_path, "title": h.title,
            "text": h.text, "dm_only": h.visibility == "dm", "link": link}


def stream_events(messages: list[dict], sources: list[search.Hit],
                  think: bool | None = None, model: str | None = None) -> Iterator[str]:
    """Generic NDJSON stream (sources, thinking, token..., done/error) for any prompt."""
    t0 = time.time()
    try:
        yield json.dumps({"type": "sources",
                          "sources": [hit_payload(i, h) for i, h in enumerate(sources, 1)]}) + "\n"
        thinking_sent = False
        for kind, piece in llm.chat_events(messages, model=model, think=think):
            if kind == "thinking":
                if not thinking_sent:
                    yield json.dumps({"type": "thinking"}) + "\n"
                    thinking_sent = True
                continue
            yield json.dumps({"type": "token", "text": piece}) + "\n"
        yield json.dumps({"type": "done", "model": model or config.CHAT_MODEL,
                          "total_s": round(time.time() - t0, 2)}) + "\n"
    except Exception as e:
        yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n"


def answer_stream(question: str, role: str, history: list[dict] | None = None,
                  kinds: tuple[str, ...] | None = None, k: int = 6,
                  model: str | None = None, think: bool | None = None,
                  user=None) -> Iterator[str]:
    """Yield NDJSON events: sources, token..., done (or error)."""
    history = history or []
    t0 = time.time()
    try:
        conn = db.connect()
        rq = _retrieval_query(question, history)
        party = _party(conn) if kinds in (None, ("party",)) else []
        system = system_prompt(role, party, user)
        hits = search.search(rq, role=role, k=k, kinds=kinds, conn=conn,
                             user_id=user.id if user is not None else None)
        # Party passages first (they're what "our"/"we"/"my" questions are about), no duplicates.
        pinned = own_note_hits(conn, user, question) + party_hits(conn, role, rq, party, user)
        seen = {h.chunk_id for h in pinned}
        hits = pinned + [h for h in hits if h.chunk_id not in seen]
        # Only show the passages the model will actually see.
        hits, _ = fit_budget(system, question, hits, clean_history(history))
        yield json.dumps({"type": "sources",
                          "sources": [hit_payload(i, h) for i, h in enumerate(hits, 1)]}) + "\n"
        t1 = time.time()
        first = None
        thinking_sent = False
        for kind, piece in llm.chat_events(build_messages(question, hits, role, history, system),
                                           model=model, think=think):
            if kind == "thinking":
                if not thinking_sent:
                    yield json.dumps({"type": "thinking"}) + "\n"
                    thinking_sent = True
                continue
            first = first or time.time()
            yield json.dumps({"type": "token", "text": piece}) + "\n"
        yield json.dumps({"type": "done", "model": model or config.CHAT_MODEL,
                          "search_s": round(t1 - t0, 2),
                          "first_token_s": round((first or time.time()) - t1, 2),
                          "total_s": round(time.time() - t0, 2)}) + "\n"
    except Exception as e:  # surface errors in the UI instead of a dead stream
        yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n"
