# D&D Companion

Local AI helper for our Strixhaven campaign: rules lookup, adventure/NPC details, and
(later) a DM + player tracker. Runs entirely on local models via Ollama.
Design and milestones: [docs/PLAN.md](docs/PLAN.md).

## Setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env        # then edit passwords
ollama pull nomic-embed-text
```

`OLLAMA_URL` can stay blank: the app reuses Ollama's own `OLLAMA_HOST` setting.

Put the PDFs in `data/sources/` with the file names from `sources.yaml`.
`data/` is git-ignored — the books and the database are never committed.

## Ingest the books

```powershell
.\.venv\Scripts\python.exe -m ingest.pdf_ingest              # all sources
.\.venv\Scripts\python.exe -m ingest.pdf_ingest SRD --show 40 # one source, sample headings
```

The first run extracts each PDF to Markdown (slow: ~9 min for the SRD). The result is
cached in `data/extracted/`, so re-chunking after a code change takes seconds.
Add `--re-extract` to redo the extraction.

DM-only sources (`visibility: dm`) print counts only — never their text.

## Search from the command line

```powershell
.\.venv\Scripts\python.exe -m app.search "can I opportunity attack a grappled creature"
.\.venv\Scripts\python.exe -m app.search --role dm "..."     # includes the adventure book
```

## Test retrieval quality

```powershell
.\.venv\Scripts\python.exe -m eval.run_eval              # score + spoiler check
.\.venv\Scripts\python.exe -m eval.run_eval --compare    # keyword vs vector vs hybrid
```

Add real questions from sessions to `eval/questions.yaml`. The `adventure:` section is
for the DM to fill in (it contains spoilers).

## Run the web app

1. In `.env`, set `DM_PASSWORD` (the login page stays locked until it is set). Players
   get their own logins on the DM's Players page — see **Logins** below.
2. Start it:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. Open http://127.0.0.1:8000 and sign in. The password you enter picks the view:
   - **Dungeon Master** — rules + adventure book, with an "All / Rules / Adventure / Notes" selector.
   - **A player** — rules, revealed campaign notes, the party and their own notes.
     DM-only passages are filtered out in the database query.

On the Ask page, answers stream in with numbered citations; click one to read the exact
passage. Tick **Careful** for questions with calculations — the model reasons first
(more accurate, ~30–60 s instead of ~5 s).

To let others on your home network use it while it runs on this PC, start it with
`--host 0.0.0.0` and browse to `http://<this PC's IP>:8000` (allow it through Windows
Firewall when asked). Internet access via SWAG comes with the Unraid move (M5).

## Recent questions

Every finished answer is saved. The **Recent** panel on the Ask page (left side on wide
screens; the **Recent** button on smaller ones) has two views:

- **Mine** — your own questions.
- **Group** — questions anyone in the group asked (same question asked twice shows once).

Click one to see the saved answer instantly, sources included; **Ask again** gets a fresh
one. While you type, matching past questions appear as **Asked before** suggestions.

Privacy: the DM's questions are never shared (they may involve the adventure), and a
player's answer that used their private notes stays in their own list. You can delete your
own entries (✕, click twice); the DM can remove any shared one. Each person keeps their
latest 150.

## Usage page (admin)

Logins listed in `ADMIN_USERS` (e.g. `ADMIN_USERS=Aldric`) get a **Usage** tab. The DM is
not an admin unless listed. It shows:

- **Summary** — people active this week, questions, average answer time, 👍/👎, failed
  answers and failed sign-ins.
- **People** — last active, last sign-in, days active and sign-ins (30 days), questions
  (7 / 30 days / all time), 👍/👎.
- **Questions** — everyone's questions with the full answer, time taken, Careful, sources
  and feedback; filter by person, 👎 only, or include stopped/failed answers. The DM's
  questions are hidden unless you tick *Show DM questions* (adventure spoilers).
- **Recent sign-ins** — time, person, IP address and device, including failed attempts.

People rate answers with 👍/👎 under each answer. Removing a question from *Recent* hides
it there but keeps it in the usage log. The login page tells everyone that sign-ins and
questions are visible to the site admin.

## Logins

The login page has one password box; the password decides who you are.

- **DM** — `DM_PASSWORD` in `.env` (the DM account is re-synced from it at startup, so
  editing `.env` is always a way back in).
- **Players** — one login per character, named after the character's first name
  (Aldric, Donovan, George, Igon, Kaine, Tyrion, Yasna). The starting password is that
  same name; passwords aren't case-sensitive.

Each player gets **My notes** (private; tick "Share with DM" to let the DM read one), and
the Ask page knows their character: "what's my AC?" answers for them, and "what are my
theories?" pulls in their own notes. No one else's private notes are ever searched.

The DM manages logins on **Players** (add, link to a character, set a new password,
remove — which also deletes that player's notes). Everyone can change their own password
under **Account** (click your name in the top bar).

> Character names are visible to everyone on the Party page, so as passwords they only
> keep honest people out. Fine at home; before the app is on the internet, have each
> player set a real password under Account (or put Cloudflare Access in front).

## Campaign status (DM)

At the top of **Sessions**, the DM keeps a running bookmark: **Where we are** and
**What's next**. Update it at the end of a session or right before "Start next session";
earlier versions are kept under **Earlier versions** on the Status page.

**Catch me up with AI** (Status page, ~15 s) reads every session's recap, DM notes and log,
the status note, open quests, the adventure book and the party, and writes: the story so
far, where we are now, open threads, and what's coming next. "Copy into my status" fills
the status form from it for review — nothing is saved until you click Save.

Session prep starts from the status note, and the DM's Ask page can answer "where are we
in the campaign?". Players never see the status.

## Sessions (DM session tracker)

**Sessions → Start next session** creates a session dated today. On its page the DM can:

- **Prep with AI** — drafts a plan for the session (where we left off, goals, scenes,
  NPCs, encounters, a spotlight hook per character, loose threads) from the last three
  sessions' logs and notes, open quests, the adventure book and the party's sheets.
  ~45 s; tick Careful for a better plan (~1–2 min). Save it into the session's DM notes.
- **Log what happened** — quick entries during play, small or large: pick a type
  (combat, discovery, roleplay, question/lore, loot, other), tap the characters involved,
  type, Ctrl+Enter. Untick "Players know this" for secrets. Each entry can be edited,
  hidden/revealed, or turned into a Note ("→ Note").
- **Recap with AI** — drafts the read-aloud "previously on…" from the session's
  *revealed* entries only, so it can't leak secrets. Save it as the player recap.

Players see a session (its recap and revealed entries) once the DM reveals it. The Ask
page can answer from session logs ("what did the librarian tell Aldric?") with the same
visibility rules. AI drafts are never saved without the DM clicking Save.

## Notes (campaign tracker)

The **Notes** tab holds sessions, NPCs, locations, factions, quests, items and lore.
Each entry has two texts and a switch:

- **What the players know** — shown to players once the entry is **Revealed**.
- **DM only** — never shown to players, in pages or in AI answers.

Every save updates the Ask page's search. The DM's searches see everything; players'
searches only see the player text of revealed entries (enforced in the database query).

## Party (D&D Beyond characters)

The **Party** tab shows each character's AC, HP, speed, abilities, saves, skills, spells,
features and gear, and the Ask page can answer questions about them ("who has the best
passive Perception?", "what's Aldric's spell save DC?").

Our characters are private on D&D Beyond, so the app can't fetch them itself. To sync,
the DM uses a bookmark that borrows their own signed-in D&D Beyond session for one click:

1. Sign in as DM, open **Party**, and drag **⟳ Sync to Companion** to the bookmarks bar.
2. Open the campaign page on dndbeyond.com and click the bookmark.
3. The browser lands on the Party page with "Synced N characters". Repeat after level-ups.

No D&D Beyond password or session is stored in the app. The bookmark carries a private
import key (reset it on the Party page if it leaks). Fallback: upload character JSON files.

Numbers are computed from D&D Beyond's raw data and were checked against D&D Beyond's own
sheets for all 7 characters (HP, AC, speed, passive Perception, saves, skills). AC doesn't
include spell effects such as Mage Armor. Backstories and personal notes are DM-search only.

## Docker / Unraid

Every push to `main` builds an image with GitHub Actions and publishes it as
`ghcr.io/mglavelle/gulu_strixhaven:latest`. The image contains only the code — books,
database and passwords stay in the `/data` volume and environment variables.

**Docker Compose** (app + Ollama):

```bash
cp .env.example .env                 # set DM_PASSWORD and a long random SESSION_SECRET
docker compose up -d
docker compose exec ollama ollama pull qwen3.5:latest
docker compose exec ollama ollama pull nomic-embed-text
# put the PDFs in ./data/sources (names from sources.yaml), then build the index:
docker compose exec companion python -m ingest.pdf_ingest
```

**Unraid:** full step-by-step guide (data move, Ollama on the Arc, SWAG, Cloudflare,
security) in [docs/UNRAID.md](docs/UNRAID.md), with a ready-made Unraid template and SWAG
config in [docs/unraid/](docs/unraid/). Quick reference (Docker → Add Container):

| Field | Value |
|---|---|
| Repository | `ghcr.io/mglavelle/gulu_strixhaven:latest` |
| Port | container `8000` → host port of your choice |
| Path | container `/data` → `/mnt/user/appdata/strixhaven-companion` |
| Extra Parameters | `--user 99:100` (so it can write to appdata) |
| Variables | `DM_PASSWORD`, `SESSION_SECRET` (long random string), `OLLAMA_URL` (e.g. `http://<server-ip>:11434`), optionally `CHAT_MODEL`, `CHAT_CTX` |

The appdata folder needs `sources/` with the PDFs; run the ingest once from the
container console: `python -m ingest.pdf_ingest`. You can also copy an existing
`companion.db` + `extracted/` from your PC's `data/` folder to skip re-ingesting.

If the GitHub package is private, either make it public (Packages → package settings) or
log Unraid in to ghcr.io with a GitHub token that has `read:packages`.

Notes:
- `SESSION_SECRET` must stay the same across restarts or everyone gets logged out.
- Behind SWAG, proxy to the container's port 8000; answers stream, so keep proxy
  buffering off (the app already sends `X-Accel-Buffering: no`).
- Ollama on the Intel Arc A580 uses the Vulkan backend (`OLLAMA_VULKAN=1`, `/dev/dri`
  passed through) — not yet tested on the server.

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `CHAT_MODEL` | `qwen3.5:latest` | Answer model |
| `EMBED_MODEL` | `nomic-embed-text` | Search embeddings (re-ingest after changing) |
| `CHAT_CTX` | `6144` | Context window for answers. Model + context must fit in graphics memory: on the 8 GB card, 8192 spilled into system RAM (3x slower, stalls mid-answer) |
| `THINK` | `false` | Default for Careful mode on API calls |
| `KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded |
| `EMBED_NUM_GPU` | `0` | `0` runs the search-embedding model on the CPU so the chat model keeps the graphics memory (questions embed in ~0.2 s). `-1` lets Ollama use the GPU (faster bulk ingestion) |

## Compare chat models

```powershell
.\.venv\Scripts\python.exe -m eval.answer_eval --models "qwen3.5:latest,qwen3:4b-instruct"
```

## Layout

| Path | What |
|---|---|
| `app/config.py` | settings from `.env` |
| `app/llm.py` | Ollama embeddings + chat |
| `app/db.py` | SQLite schema |
| `app/search.py` | hybrid keyword + vector search, role-filtered |
| `app/answer.py` | prompt building + streamed, cited answers |
| `app/auth.py`, `app/users.py` | per-user login, password hashing |
| `app/accounts.py` | Players page (DM) and Account page (change password) |
| `app/main.py` | web routes and `/api/ask` |
| `app/notes.py` | Notes pages (create / edit / reveal / delete) |
| `app/sessions.py` | Session log, AI prep and recap drafts |
| `app/status.py` | Campaign status note + AI "catch me up" |
| `app/party.py`, `app/characters.py` | D&D Beyond import + sheet calculations, Party page |
| `app/indexing.py` | keeps notes and characters in the search index |
| `app/templates/`, `app/static/` | pages, CSS, page script (no build step) |
| `ingest/pdf_ingest.py` | PDF → Markdown → sections → index |
| `eval/` | test questions and scorer |
| `sources.yaml` | which books to ingest and who can see them |
