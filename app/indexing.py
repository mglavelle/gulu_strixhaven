"""Keep the search index in sync with app-made content (notes, characters).

Each note becomes search chunks under a synthetic source "Notes":
  * a `dm` chunk with everything (name, player text, DM text)    - DM searches see it
  * a `player` chunk with name + player text, only when revealed - player searches see it
The DM never sees the `player` copy (no duplicates); players never see the `dm` one.
"""
import time
from datetime import datetime, timezone

from . import config, llm

APP_SOURCES = {
    "Notes": ("Campaign notes", "notes"),
    "Party": ("Party characters", "party"),
}

TYPE_LABELS = {
    "session": "Session", "npc": "NPC", "location": "Location", "faction": "Faction",
    "quest": "Quest", "item": "Item", "lore": "Lore",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_id(conn, key: str) -> int:
    title, kind = APP_SOURCES[key]
    conn.execute("""INSERT INTO sources(key, title, kind, visibility, file, page_offset, ingested_at)
                    VALUES (?,?,?,?,?,0,?) ON CONFLICT(key) DO NOTHING""",
                 (key, title, kind, "dm", "", now()))
    return conn.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()[0]


def embed_missing(conn, model: str | None = None, quiet: bool = False) -> int:
    """Embed every chunk that has no vector for `model` yet. Returns the count."""
    model = model or config.EMBED_MODEL
    rows = conn.execute("""
        SELECT c.id, c.heading_path, c.text, s.title AS source_title
        FROM chunks c JOIN sources s ON s.id = c.source_id
        WHERE NOT EXISTS (SELECT 1 FROM chunk_vectors v
                          WHERE v.chunk_id = c.id AND v.model = ?)""", (model,)).fetchall()
    if not rows:
        return 0
    texts = [f"{r['source_title']} - {r['heading_path']}\n\n{r['text']}" for r in rows]
    t = time.time()
    vecs = llm.embed(texts, kind="doc", model=model)
    conn.executemany(
        "INSERT OR REPLACE INTO chunk_vectors(chunk_id, model, dim, vec) VALUES (?,?,?,?)",
        [(r["id"], model, vecs.shape[1], v.tobytes()) for r, v in zip(rows, vecs)])
    conn.commit()
    if not quiet:
        print(f"  embedded {len(rows)} chunks with {model} in {time.time() - t:.0f}s")
    return len(rows)


def replace_chunks(conn, source_key: str, ref_type: str, ref_id: int,
                   chunks: list[dict]) -> None:
    """Swap out every chunk for (ref_type, ref_id) and update both indexes.
    chunks: [{heading_path, title, text, visibility}]"""
    sid = source_id(conn, source_key)
    conn.execute("DELETE FROM chunks WHERE ref_type=? AND ref_id=?", (ref_type, ref_id))
    conn.executemany(
        """INSERT INTO chunks(source_id, seq, heading_path, title, page_start, page_end,
                              text, visibility, ref_type, ref_id)
           VALUES (?,?,?,?,0,0,?,?,?,?)""",
        [(sid, ref_id, c["heading_path"], c["title"], c["text"], c["visibility"],
          ref_type, ref_id) for c in chunks])
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    conn.commit()
    embed_missing(conn, quiet=True)


def _index_personal(conn, e) -> None:
    """A player's own note: searchable by that player only, plus a DM copy if shared."""
    owner = conn.execute("SELECT name FROM users WHERE id=?", (e["owner_id"],)).fetchone()
    owner_name = owner[0] if owner else "a player"
    label = TYPE_LABELS.get(e["type"], e["type"].title())
    tags = f"\nTags: {e['tags']}" if e["tags"] else ""
    body = e["player_text"] or "(empty)"
    path = f"Personal notes ({owner_name}) > {label} > {e['name']}"
    chunks = [{"heading_path": path, "title": e["name"], "visibility": f"user:{e['owner_id']}",
               "text": f"## A note you ({owner_name}) wrote yourself — private to you — {label}: "
                       f"{e['name']}{tags}\n\n{body}"}]
    if e["shared_with_dm"]:
        chunks.append({"heading_path": path, "title": e["name"], "visibility": "dm",
                       "text": f"## {owner_name}'s note (shared with the DM) — {label}: {e['name']}{tags}\n\n{body}"})
    replace_chunks(conn, "Notes", "entry", e["id"], chunks)


def index_entry(conn, e) -> None:
    if e["owner_id"] is not None:
        _index_personal(conn, e)
        return
    label = TYPE_LABELS.get(e["type"], e["type"].title())
    when = f" ({e['session_date']})" if e["session_date"] else ""
    head = f"{label}: {e['name']}{when}"
    tags = f"\nTags: {e['tags']}" if e["tags"] else ""
    chunks = [{
        "heading_path": f"Campaign notes > {label} > {e['name']}",
        "title": e["name"],
        "visibility": "dm",
        "text": (f"## {head}{tags}\n\n**What the players know:**\n{e['player_text'] or '(nothing yet)'}"
                 f"\n\n**DM only:**\n{e['dm_text'] or '(none)'}"
                 f"\n\nRevealed to players: {'yes' if e['revealed'] else 'no'}"),
    }]
    if e["revealed"] and e["player_text"].strip():
        chunks.append({
            "heading_path": f"Campaign notes > {label} > {e['name']}",
            "title": e["name"],
            "visibility": "player",
            "text": f"## {head}{tags}\n\n{e['player_text']}",
        })
    replace_chunks(conn, "Notes", "entry", e["id"], chunks)
    if e["type"] == "session":
        index_session(conn, e["id"])          # log visibility follows the session's reveal


def index_character(conn, char_id: int, sheet: dict) -> None:
    from .characters import private_text, sheet_text
    path = f"Party > {sheet['name']}"
    chunks = [{"heading_path": path, "title": sheet["name"], "visibility": "public",
               "text": sheet_text(sheet)}]
    private = private_text(sheet)
    if private:
        chunks.append({"heading_path": path + " > Backstory", "title": sheet["name"],
                       "visibility": "dm", "text": private})
    replace_chunks(conn, "Party", "character", char_id, chunks)


def remove_character(conn, char_id: int) -> None:
    conn.execute("DELETE FROM chunks WHERE ref_type='character' AND ref_id=?", (char_id,))
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    conn.commit()
    index_party_overview(conn)


def index_party_overview(conn) -> None:
    """One table comparing every character, so "who has the best X?" questions can be
    answered from a single passage instead of seven separate sheets."""
    import json
    from .characters import signed
    sheets = [json.loads(r[0]) for r in
              conn.execute("SELECT sheet_json FROM characters ORDER BY name COLLATE NOCASE")]
    if not sheets:
        conn.execute("DELETE FROM chunks WHERE ref_type='party'")
        conn.commit()
        return
    rows = ["| Character | Player | Race and class | Background | Alignment | AC | HP | Speed | Init | "
            "Passive Perception | STR | DEX | CON | INT | WIS | CHA | Save proficiencies | Spell DC | Languages |",
            "|" + "---|" * 19]
    for s in sheets:
        ab = [f"{v['score']} ({signed(v['mod'])})" for v in s["abilities"].values()]
        saves = ", ".join(k.upper() for k, v in s["saves"].items() if v["prof"])
        dc = ", ".join(str(c["save_dc"]) for c in s["spellcasting"]) or "—"
        rows.append(f"| {s['name']} | {s['player']} | {s['summary'].replace('Level ', 'Lvl ')} | "
                    f"{s.get('background') or '—'} | {s.get('alignment') or '—'} | {s['ac']} | "
                    f"{s['hp']['max']} | {s['speed']} ft. | {signed(s['initiative'])} | "
                    f"{s['passive_perception']} | {' | '.join(ab)} | {saves} | {dc} | "
                    f"{', '.join(s['languages'])} |")
    skill_lines = []
    for sk in sheets[0]["skills"]:
        best = sorted(sheets, key=lambda s: -s["skills"][sk]["bonus"])[:3]
        profs = [s["name"] for s in sheets if s["skills"][sk]["prof"]]
        skill_lines.append(
            f"- **{sk.replace('-', ' ').title()}** — proficient: {', '.join(profs) or 'nobody'}; "
            f"highest bonus: " + ", ".join(f"{s['name']} {signed(s['skills'][sk]['bonus'])}" for s in best))
    text = ("## Party overview — all characters compared\n\n" + "\n".join(rows) +
            "\n\n**Skills — who is proficient, and the three highest bonuses (proficient or not):**\n"
            + "\n".join(skill_lines))
    replace_chunks(conn, "Party", "party", 0, [{
        "heading_path": "Party > Party overview", "title": "Party overview",
        "visibility": "public", "text": text}])


EVENT_KINDS = {"combat": "Combat", "discovery": "Discovery", "roleplay": "Roleplay",
               "question": "Question / lore", "loot": "Loot", "other": "Other"}


def event_line(ev) -> str:
    who = f" ({ev['characters']})" if ev["characters"] else ""
    return f"- [{EVENT_KINDS.get(ev['kind'], ev['kind'])}]{who} {ev['text']}"


def index_session(conn, session_id: int) -> None:
    """The session log as search chunks: everything for the DM; revealed events only
    for players (and only once the session itself is revealed)."""
    s = conn.execute("SELECT * FROM entries WHERE id=?", (session_id,)).fetchone()
    if not s:
        return
    events = conn.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id",
                          (session_id,)).fetchall()
    when = f" ({s['session_date']})" if s["session_date"] else ""
    path = f"Campaign notes > Session log > {s['name']}"
    chunks = []
    if events:
        chunks.append({"heading_path": path, "title": s["name"], "visibility": "dm",
                       "text": f"## Session log: {s['name']}{when}\n\n" + "\n".join(event_line(e) for e in events)})
        shown = [e for e in events if e["revealed"]]
        if s["revealed"] and shown:
            chunks.append({"heading_path": path, "title": s["name"], "visibility": "player",
                           "text": f"## Session log: {s['name']}{when}\n\n" + "\n".join(event_line(e) for e in shown)})
    replace_chunks(conn, "Notes", "session", session_id, chunks)


def remove_entry(conn, entry_id: int) -> None:
    conn.execute("DELETE FROM chunks WHERE ref_type='session' AND ref_id=?", (entry_id,))
    conn.execute("DELETE FROM chunks WHERE ref_type='entry' AND ref_id=?", (entry_id,))
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    conn.commit()
