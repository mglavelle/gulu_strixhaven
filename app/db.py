"""SQLite storage: schema and connection helper.

Search uses two indexes over `chunks`:
  * chunks_fts     - FTS5 keyword index (exact names: "Grappled", "Lorehold")
  * chunk_vectors  - embeddings stored as float32 blobs, searched with numpy
                     (a few thousand chunks -> brute force takes milliseconds)
"""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- rules | adventure
    visibility  TEXT NOT NULL,              -- public | dm
    file        TEXT NOT NULL,
    page_offset INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,          -- order within the source
    heading_path TEXT NOT NULL,             -- "Spells > Fireball"
    title        TEXT NOT NULL,             -- last heading: "Fireball"
    page_start   INTEGER NOT NULL,
    page_end     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    visibility   TEXT NOT NULL              -- copied from source; tracker rows later
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading_path, text,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
);

-- Campaign notes / tracker (M3). One table for every kind of entry.
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY,
    type         TEXT NOT NULL,            -- session | npc | location | faction | quest | item | lore
    name         TEXT NOT NULL,
    player_text  TEXT NOT NULL DEFAULT '', -- what the players know
    dm_text      TEXT NOT NULL DEFAULT '', -- DM only
    revealed     INTEGER NOT NULL DEFAULT 0,
    session_date TEXT,                     -- for type = session
    tags         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Party characters imported from D&D Beyond (M4).
CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY,
    ddb_id      INTEGER UNIQUE,
    name        TEXT NOT NULL,
    player      TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',  -- "Lvl 1 Half-Elf Wizard"
    sheet_json  TEXT NOT NULL,             -- our parsed sheet (see app/characters.py)
    raw_json    TEXT,                      -- the D&D Beyond export, kept for re-parsing
    synced_at   TEXT NOT NULL
);
"""

SCHEMA += """
-- What happened during a session (entries.type = 'session'), small or large.
CREATE TABLE IF NOT EXISTS session_events (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- combat | discovery | roleplay | question | loot | other
    characters  TEXT NOT NULL DEFAULT '', -- comma separated character names
    text        TEXT NOT NULL,
    revealed    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_events_session ON session_events(session_id, id);

-- Logins. Passwords are salted PBKDF2 hashes; the password alone identifies the user.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role          TEXT NOT NULL,                -- dm | player
    password_hash TEXT NOT NULL,
    character_id  INTEGER REFERENCES characters(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL
);

-- The DM's running "where we are / what's next" note. Latest row = current; older = history.
CREATE TABLE IF NOT EXISTS campaign_status (
    id          INTEGER PRIMARY KEY,
    where_text  TEXT NOT NULL DEFAULT '',
    next_text   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- Ask-page history. shared = 1: other logged-in people may see it in the "Group" view.
CREATE TABLE IF NOT EXISTS ask_history (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    sources_json  TEXT NOT NULL DEFAULT '[]',
    scope         TEXT NOT NULL DEFAULT 'all',
    shared        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ask_history_user ON ask_history(user_id, id);
CREATE INDEX IF NOT EXISTS ask_history_shared ON ask_history(shared, id);

-- Admin usage tracking.
CREATE TABLE IF NOT EXISTS login_events (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,   -- NULL = failed attempt
    ok          INTEGER NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_events_at ON login_events(at);
CREATE TABLE IF NOT EXISTS activity_days (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day      TEXT NOT NULL,              -- YYYY-MM-DD (UTC)
    hits     INTEGER NOT NULL DEFAULT 0, -- 5-minute windows with activity
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("chunks", "ref_type", "TEXT"),       # 'entry' | 'character' for app-made chunks
    ("chunks", "ref_id", "INTEGER"),
    ("entries", "owner_id", "INTEGER"),            # NULL = campaign note; else a player's own note
    ("entries", "shared_with_dm", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_seen", "TEXT"),
    ("ask_history", "hidden", "INTEGER NOT NULL DEFAULT 0"),      # removed from Recent by its owner
    ("ask_history", "status", "TEXT NOT NULL DEFAULT 'ok'"),      # ok | stopped | error
    ("ask_history", "total_s", "REAL"),
    ("ask_history", "careful", "INTEGER NOT NULL DEFAULT 0"),
    ("ask_history", "feedback", "INTEGER"),                       # 1 = 👍, -1 = 👎
    ("ask_history", "error", "TEXT"),
]


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_ref ON chunks(ref_type, ref_id)")
    conn.commit()
    return conn
