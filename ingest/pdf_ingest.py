"""PDF -> Markdown -> heading-based chunks -> SQLite (FTS + embeddings).

Usage:
    python -m ingest.pdf_ingest                 # ingest every source in sources.yaml
    python -m ingest.pdf_ingest SRD             # one source by key
    python -m ingest.pdf_ingest SRD --re-extract   # redo the (slow) PDF extraction
    python -m ingest.pdf_ingest SRD --no-embed     # chunks + keyword index only

Content of DM-only sources is never printed - only counts.
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import config, db
from app.indexing import embed_missing

ROOT = Path(__file__).resolve().parent.parent
EXTRACT_DIR = config.DATA_DIR / "extracted"

MAX_CHARS = 2400          # split longer sections at paragraph breaks
MAX_HEADING_LEVEL = 5     # the extractor puts many small real headings at level 5
MAX_HEADING_CHARS = 70    # longer "headings" are mis-detected sentences

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
TAG_RE = re.compile(r"</?(mark|u|sup|sub|span)[^>]*>")
ABILITY_RE = re.compile(r"\*\*\s*([SDCIWsdciw])\s*\*?\*?\s*(tr|ex|on|nt|IS|is|ha)\s*\*\*")
ABILITY_FIX = {"str": "Str", "dex": "Dex", "con": "Con", "int": "Int", "wis": "Wis", "cha": "Cha"}


# --------------------------------------------------------------------------- extract
def load_sources() -> list[dict]:
    return yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))["sources"]


def extract(src: dict, force: bool = False) -> list[dict]:
    """Return [{page, text}] (1-based PDF pages). Cached as JSON in data/extracted/."""
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    cache = EXTRACT_DIR / f"{src['key']}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    import pymupdf4llm  # slow import; only needed here

    pdf = config.SOURCES_DIR / src["file"]
    print(f"  extracting {pdf.name} (slow, cached afterwards)...", flush=True)
    t = time.time()
    pages = pymupdf4llm.to_markdown(str(pdf), page_chunks=True, show_progress=False)
    out = [{"page": p["metadata"]["page_number"], "text": p["text"]} for p in pages]
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"  extracted {len(out)} pages in {time.time() - t:.0f}s")
    return out


# --------------------------------------------------------------------------- clean
def clean_text(text: str) -> str:
    text = TAG_RE.sub("", text)
    text = ABILITY_RE.sub(lambda m: "**" + ABILITY_FIX[(m[1] + m[2]).lower()] + "**", text)
    text = text.replace("­", "")                     # soft hyphens
    text = re.sub(r"(\d+)foot\b", r"\1-foot", text)       # "10foot" -> "10-foot"
    return text


def footer_key(line: str) -> str:
    """Normalise a line so "**124** Book Title" and "Book Title 125" compare equal."""
    return re.sub(r"[\d*_#\s]+", " ", line).strip().lower()


def repeated_lines(pages: list[dict]) -> set[str]:
    """Normalised lines that appear on many pages (running headers/footers)."""
    counts = Counter()
    for p in pages:
        counts.update({footer_key(ln) for ln in p["text"].splitlines()
                       if ln.strip() and not ln.lstrip().startswith("|")})
    limit = max(5, len(pages) * 0.15)
    return {k for k, n in counts.items() if n >= limit and len(k) >= 4}


def heading_title(raw: str) -> str:
    t = re.sub(r"[*_`]", "", raw)
    return re.sub(r"\s+", " ", t).strip(" :")


# --------------------------------------------------------------------------- chunk
# An "entry" is a spell, monster, magic item, NPC stat block...: a name followed by an
# italic type line, e.g. "**Fireball**" + "_Level 3 Evocation (Sorcerer, Wizard)_"
# or "## Goblin Warrior" + "_Small Fey (Goblinoid), Chaotic Neutral_".
BOLD_LINE_RE = re.compile(r"^\*\*([^*]{2,60}?)\s*\*\*\s*$")
TYPE_LINE_RE = re.compile(r"^_[^_\s][^_]{2,120}_")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick_heading_mode(pages: list[dict], bookmarks: list, configured: str) -> str:
    """'markdown': trust the extractor's heading levels.
    'bookmarks': take levels from the PDF bookmarks (for books where the extractor
    puts nearly every heading at the same deep level)."""
    if configured in ("markdown", "bookmarks"):
        return configured
    levels = Counter(len(m[1]) for p in pages for ln in p["text"].splitlines()
                     if (m := HEADING_RE.match(ln)))
    shallow = sum(n for lv, n in levels.items() if lv <= 4)
    total = sum(levels.values()) or 1
    return "bookmarks" if bookmarks and shallow / total < 0.10 else "markdown"


def chunk_pages(pages: list[dict], bookmarks: list | None = None) -> list[dict]:
    """Split pages into sections. With `bookmarks` ([level, title, page] from the PDF),
    heading levels come from the bookmarks; other short headings become sub-sections."""
    bm_by_page: dict[int, dict[str, int]] = {}
    sub_level = MAX_HEADING_LEVEL
    if bookmarks:
        for lvl, title, pg in bookmarks:
            for p in (pg - 1, pg, pg + 1):
                bm_by_page.setdefault(p, {}).setdefault(_norm(title), lvl)
        sub_level = min(max(b[0] for b in bookmarks) + 1, MAX_HEADING_LEVEL)

    def heading_info(page: int, line: str, m, bold) -> tuple[int, str] | None:
        """(level, title) if this line starts a section, else None."""
        if not (m or bold):
            return None
        title = heading_title(m[2] if m else bold[1])
        if not title or len(title) > MAX_HEADING_CHARS:
            return None
        if bookmarks is None:
            return (len(m[1]), title) if m and len(m[1]) <= MAX_HEADING_LEVEL else None
        t = _norm(title)
        for key, lvl in bm_by_page.get(page, {}).items():
            if t == key or (len(key) > 6 and t.startswith(key)):
                return lvl, title
        return (sub_level, title) if m else None

    noise = repeated_lines(pages)
    lines: list[tuple[int, str]] = []
    for p in pages:
        for line in clean_text(p["text"]).splitlines():
            s = line.strip()
            if (s and not s.startswith("|") and footer_key(s) in noise) \
                    or re.fullmatch(r"\**\d{1,3}\**", s):
                continue
            lines.append((p["page"], line))

    def next_text(i: int) -> str:
        for _, ln in lines[i + 1:i + 4]:
            if ln.strip():
                return ln.strip()
        return ""

    chunks: list[dict] = []
    stack: list[dict] = []     # {"level", "title", "entry"}
    cur = None

    def flush():
        nonlocal cur
        if cur:
            body = "\n".join(cur["lines"]).strip()
            heading_line = cur["lines"][0].strip() if cur["lines"] else ""
            if body and body != heading_line:      # skip heading-only sections
                cur["text"] = re.sub(r"\n{3,}", "\n\n", body)
                chunks.append(cur)
        cur = None

    def start(page: int):
        nonlocal cur
        path = [h["title"] for h in stack]
        cur = {"heading_path": " > ".join(path) or "(front matter)",
               "title": path[-1] if path else "(front matter)",
               "page_start": page, "page_end": page, "lines": []}

    for i, (page, line) in enumerate(lines):
        m = HEADING_RE.match(line)
        bold = BOLD_LINE_RE.match(line.strip())
        info = heading_info(page, line, m, bold)
        is_entry = bool((m or bold) and TYPE_LINE_RE.match(next_text(i)))
        in_entry = bool(stack and stack[-1]["entry"])

        if is_entry:
            title = heading_title(m[2] if m else bold[1])
            real_level = info[0] if info else (len(m[1]) if m and bookmarks is None else None)
            while stack and (stack[-1]["entry"] or
                             (real_level and stack[-1]["level"] >= real_level)):
                stack.pop()
            level = real_level or ((stack[-1]["level"] + 1) if stack else 1)
            flush()
            stack.append({"level": level, "title": title, "entry": True})
            start(page)
            cur["lines"].append(f"{'#' * min(level, 6)} {title}")
            continue

        if info:
            level, title = info
            # Sub-headings inside an entry ("Actions", "Reactions") stay in the entry.
            if not (in_entry and level > stack[-1]["level"]):
                flush()
                while stack and stack[-1]["level"] >= level:
                    stack.pop()
                stack.append({"level": level, "title": title, "entry": False})
                start(page)
                cur["lines"].append(f"{'#' * level} {title}")
                continue
        if m:
            title = heading_title(m[2])
            line = f"**{title}**" if title else ""   # demote to bold text

        if cur is None:
            start(page)
        cur["lines"].append(line)
        cur["page_end"] = page
    flush()
    return split_long(chunks)


SKIP_SECTIONS = ("contents", "table of contents", "index")


def drop_navigation(chunks: list[dict], extra: list[str]) -> list[dict]:
    """Remove table-of-contents / index sections: page-number lists pollute search."""
    skip = {s.lower() for s in (*SKIP_SECTIONS, *extra)}
    return [c for c in chunks
            if not any(c["heading_path"].lower().startswith(s) or
                       c["heading_path"].lower().startswith("index of") for s in skip)]


def split_long(chunks: list[dict]) -> list[dict]:
    out = []
    for c in chunks:
        if len(c["text"]) <= MAX_CHARS:
            out.append(c)
            continue
        paras, buf, part = [], [], 0
        for para in c["text"].split("\n\n"):     # very long tables: split by rows
            for piece in (para.split("\n") if len(para) > MAX_CHARS else [para]):
                # a single giant line: fall back to sentence boundaries
                paras.extend(re.split(r"(?<=[.!?])\s+(?=[A-Z])", piece)
                             if len(piece) > MAX_CHARS else [piece])
        for para in paras:
            if buf and len("\n\n".join(buf + [para])) > MAX_CHARS:
                out.append({**c, "text": _part_text(c, buf, part)})
                buf, part = [], part + 1
            buf.append(para)
        if buf:
            out.append({**c, "text": _part_text(c, buf, part)})
    return out


def _part_text(c: dict, paras: list[str], part: int) -> str:
    body = "\n\n".join(paras)
    return body if part == 0 else f"## {c['title']} (continued)\n\n{body}"


# --------------------------------------------------------------------------- store
def store(conn, src: dict, chunks: list[dict], embed: bool) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO sources(key, title, kind, visibility, file, page_offset, ingested_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET title=excluded.title, kind=excluded.kind,
             visibility=excluded.visibility, file=excluded.file,
             page_offset=excluded.page_offset, ingested_at=excluded.ingested_at""",
        (src["key"], src["title"], src["kind"], src["visibility"], src["file"],
         src.get("page_offset", 0), now))
    source_id = conn.execute("SELECT id FROM sources WHERE key=?", (src["key"],)).fetchone()[0]
    conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
    conn.executemany(
        """INSERT INTO chunks(source_id, seq, heading_path, title, page_start, page_end, text, visibility)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(source_id, i, c["heading_path"], c["title"], c["page_start"], c["page_end"],
          c["text"], src["visibility"]) for i, c in enumerate(chunks)])
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    conn.commit()

    if embed:
        embed_missing(conn)


def report(src: dict, chunks: list[dict]) -> None:
    sizes = sorted(len(c["text"]) for c in chunks)
    print(f"  {len(chunks)} chunks | chars median {sizes[len(sizes) // 2]}, "
          f"min {sizes[0]}, max {sizes[-1]} | under 150 chars: {sum(s < 150 for s in sizes)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", help="source keys (default: all)")
    ap.add_argument("--re-extract", action="store_true")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--show", type=int, default=0,
                    help="print N sample headings (public sources only)")
    args = ap.parse_args(argv)

    conn = db.connect()
    for src in load_sources():
        if args.keys and src["key"] not in args.keys:
            continue
        if not (config.SOURCES_DIR / src["file"]).exists():
            print(f"[{src['key']}] missing file data/sources/{src['file']} - skipped")
            continue
        print(f"[{src['key']}] {src['title']} ({src['visibility']})")
        import pymupdf
        pages = extract(src, args.re_extract)
        bookmarks = pymupdf.open(config.SOURCES_DIR / src["file"]).get_toc()
        mode = pick_heading_mode(pages, bookmarks, src.get("heading_mode", "auto"))
        print(f"  heading mode: {mode} ({len(bookmarks)} PDF bookmarks)")
        chunks = drop_navigation(
            chunk_pages(pages, bookmarks if mode == "bookmarks" else None),
            src.get("skip_sections", []))
        report(src, chunks)
        if args.show and src["visibility"] == "public":
            step = max(1, len(chunks) // args.show)
            for c in chunks[::step][:args.show]:
                print(f"    p{c['page_start']:>3}  {c['heading_path']}  ({len(c['text'])} chars)")
        store(conn, src, chunks, embed=not args.no_embed)


if __name__ == "__main__":
    sys.exit(main())
