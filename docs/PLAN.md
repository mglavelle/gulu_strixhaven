# D&D Companion — Project Plan

A self-hosted helper for our Strixhaven campaign. Two password-protected views:

- **DM view** — rules lookup, the full adventure book, NPC/location/quest tracker with private notes, session log.
- **Player view** — rules lookup, party characters, and only the NPCs/locations/quests/recaps the DM has *revealed*.

Strictly local: models run in Ollama; no cloud APIs. Built on the PC first, moved to Unraid in Docker later.

---

## 1. Decisions

| Area | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | Best PDF + AI libraries; already installed |
| Web framework | **FastAPI** | Small, async, easy streaming responses |
| Front end | **Jinja2 templates + HTMX** (+ a little CSS) | Server-rendered pages, no JS build step, still feels interactive |
| Database | **SQLite** (one file) with **FTS5** (keyword search); embeddings stored as blobs and searched with **numpy** | No DB server or extensions; a few thousand chunks search in milliseconds; one file to back up |
| LLM runtime | **Ollama** (HTTP API) | Already installed on PC; runs on NVIDIA now and Intel Arc (Vulkan) on Unraid later |
| Chat model | Start with `qwen3.5` (already pulled); compare 1–2 other 7–12B models with the eval set | Must fit in 8 GB VRAM on both machines |
| Embedding model | **`nomic-embed-text`** — tied with `qwen3-embedding:0.6b` in the eval, and smaller | Runs alongside the chat model |
| PDF extraction | **pymupdf4llm** (PDF → Markdown with headings) | Keeps heading structure so we can chunk by section/stat block |
| Auth | Two roles (`dm`, `player`), each with its own password; hashed, cookie session | Matches "two password-protected views"; per-player logins can come later |
| Packaging | `Dockerfile` + `docker-compose.yml` (app + Ollama) | Easy Unraid install later |

## 2. The one rule that matters: spoiler safety

Everything stored has a **visibility**: `public` (rules, revealed tracker entries, player recaps) or `dm` (adventure book text, DM notes, unrevealed entries).

**Filtering happens in the database query, not in the prompt.** When a player asks a question, adventure-book chunks and DM notes are never retrieved, so the model can't leak what it never saw. Telling a model "don't reveal spoilers" is not a security boundary.

## 3. How a question is answered

1. User asks: *"Who is Professor Lorehold and what does she want?"*
2. **Hybrid search** over allowed content: FTS5 keyword match (great for exact names like "Grappled", "Lorehold") + vector similarity (great for paraphrased questions). Merge the results.
3. Add relevant tracker entries and party character summaries.
4. Prompt the model: answer **only** from the provided passages, cite each fact as `[SRD p.212]` / `[Strixhaven p.42]` / `[Tracker: NPC]`, and say "not found" rather than guess.
5. Stream the answer to the page; citations link to the source passage.

## 4. Data model (first cut)

```
sources     (id, title, kind[rules|adventure], visibility, file_path)
chunks      (id, source_id, heading_path, page_start, page_end, text, visibility)
chunks_fts  FTS5 over chunks.text + heading_path
chunks_vec  sqlite-vec embeddings keyed by chunk id
entities    (id, type[npc|location|faction|quest|item], name, summary,
             player_notes, dm_notes, revealed bool, source_refs, updated_at)
sessions    (id, date, title, player_recap, dm_notes)
characters  (id, ddb_id, name, player, class_level, data_json, synced_at)
```

## 5. Project layout

```
dnd-companion/
  app/
    main.py          FastAPI app, routes
    auth.py          login, roles, session cookie
    db.py            SQLite connection, schema/migrations
    search.py        hybrid FTS + vector retrieval with visibility filter
    llm.py           Ollama client (chat + embeddings, streaming)
    templates/       Jinja2 pages (ask, tracker, sessions, characters, login)
    static/          CSS, htmx.min.js
  ingest/
    pdf_ingest.py    PDF -> markdown -> section chunks -> embeddings
    ddb_import.py    D&D Beyond character JSON -> characters table
  eval/
    questions.yaml   real questions + expected source/page
    run_eval.py      scores retrieval/answers after each change
  data/              (git-ignored) PDFs, companion.db
  Dockerfile
  docker-compose.yml
  .env.example       DM_PASSWORD, PLAYER_PASSWORD, OLLAMA_URL, model names
```

**The PDFs and the database never go in git** — `data/` is git-ignored. They're copyrighted and contain the spoilers.

## 6. Milestones

Each milestone ends with something usable. No deadlines.

**Status (2026-11-05):** M0, M1 and M2 done (running locally on the PC) — code in `dnd-companion/`.
SRD: 2,041 sections; eval hit@5 97%, hit@1 83%. Strixhaven: 703 sections (DM-only),
split using the PDF bookmarks. Spoiler check passes. Chat model: `qwen3.5` (~5 s answers;
"Careful" thinking mode ~40 s, better at arithmetic). `qwen3:4b-instruct` was faster but got
rules wrong. Context is 6144 tokens: 8192 spilled out of the 8 GB of graphics memory and stalled.

**Update (2026-11-06):** M3 (Notes) and M4 (Party) done. Notes: one entries table
with player text / DM text / Revealed; a third chunk visibility (`player`) keeps revealed
notes out of DM duplicates and DM text away from players. Characters: private on D&D Beyond,
so a DM-run bookmark posts them to the app (top-level form submission — browsers block
background requests from websites to localhost). Parsed values verified against D&D Beyond
for all 7 characters. A "party overview" passage answers whole-party questions.
Also added: a session tracker (log entries with type/characters/reveal, AI prep from
recent sessions + adventure book + party, AI recap from revealed entries only); the AI now
always knows the party roster, pins the party overview for group questions, and gives
real suggestions for judgment/fun questions while keeping facts cited. Embeddings run on
the CPU to leave graphics memory to the chat model.
Logins are now per person: the DM (DM_PASSWORD in .env) and one login per character
(first name, starting password = that name). Players have private notes (optional share
with DM) that only their own searches include (chunk visibility `user:<id>`), and the AI
knows which character is "me". The DM also has a campaign status note (where we are / what's next, with history) that
session prep starts from, and an AI "catch me up" briefing over the whole campaign record.
**Before M5:** character-name passwords are guessable —
have players change them under Account, or put Cloudflare Access in front.
Next: M5 (Docker + Unraid + SWAG). Note for M5: the bookmark works the same against the
hosted https address.

### M0 — Setup
- Create `dnd-companion/` with git, venv, `requirements.txt`, `.gitignore`.
- Pull an embedding model in Ollama.
- **Done when:** a hello-world FastAPI page loads and can get a reply from Ollama.

### M1 — Ingestion + search (the most important milestone)
- Convert both PDFs to Markdown; chunk by heading (chapter → section → stat block/spell/table), keep page numbers.
- Clean up known problem areas: two-column layout, stat blocks, tables, sidebars.
- Build the FTS5 + vector index. Command-line `search "query"` prints the top passages.
- Write `eval/questions.yaml` with ~25 real questions (rules + adventure + NPC) and where the answer lives.
- **Done when:** the right passage is in the top 5 for most eval questions.

### M2 — Ask page with roles
- Login page; DM and player passwords from `.env`.
- Ask page: question box, streamed answer, clickable citations.
- Visibility filter enforced in `search.py`; eval includes "player asks about a spoiler" cases that must return nothing.
- Pick the chat model by running the eval with 2–3 candidates.
- **Done when:** both views work on the PC and players can't surface adventure text.

### M3 — Tracker
- CRUD pages for NPCs, locations, factions, quests, items; DM sets `revealed`.
- Session log: DM notes (private) + player recap (public).
- Optional helper: "draft an NPC entry from the book" — DM reviews before saving.
- Tracker entries are included in search for both roles (respecting visibility).
- **Done when:** the DM can run a session using the tracker, and players see the revealed parts.

### M4 — Characters
- Import from D&D Beyond: try the (unofficial) character JSON endpoint for public characters; fall back to uploading the JSON file manually.
- Character cards (stats, AC, HP, spells, features) on a Party page; character summaries feed the AI context ("Can Aldric cast Shield as a reaction?").
- **Done when:** all 7 party characters show up and the AI can answer questions about them.

### M5 — Move to Unraid
- `Dockerfile` + `docker-compose.yml`; Ollama container with the Arc A580 passed through (`/dev/dri`, Vulkan backend).
- `data/` mapped to an appdata share; nightly backup of `companion.db`.
- SWAG `dmhelper.subdomain.conf`, Cloudflare proxied DNS; optionally Cloudflare Access as a second lock in front of the app's own login.
- **Done when:** the group uses it at `dmhelper.gulu.stream`.

### Later ideas
- Per-player logins (each player edits their own notes, sees only their own secret info).
- Initiative/HP tracker for combat.
- Session-recap drafting from the DM's bullet notes.
- Strixhaven-specific pages: relationships, Student Dice, extracurriculars, exam tracking.

## 7. Risks / open questions

- **PDF quality** is the biggest risk to answer quality — budget real time for M1 cleanup.
- **Small-model accuracy:** mitigated by retrieval + citations + "say not found". Use the eval set to keep track.
- **D&D Beyond endpoint** is unofficial and may change; manual JSON upload is the fallback.
- **Intel Arc on Unraid:** Ollama Vulkan support on the A580 needs a test before M5; the PC (NVIDIA) path needs no special setup.
- **Spoilers for the builder:** the person building this is also a player. Consider having the DM run the adventure ingestion and set the DM password themselves.
