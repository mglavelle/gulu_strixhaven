"""Hybrid retrieval: FTS5 keyword search + embedding similarity, merged with
Reciprocal Rank Fusion. Visibility is filtered in SQL, so a player-role search can
never return DM-only text.

CLI:  python -m app.search "can I take an opportunity attack on a grappled creature"
      python -m app.search --role player "who runs Lorehold college"
"""
import argparse
import re
import sqlite3
from dataclasses import dataclass

import numpy as np

from . import config, db, llm

# public: everyone (rules). dm: DM only (adventure book, full notes).
# player: player-facing copy of revealed notes (the DM has the full version instead).
ROLE_VISIBILITY = {"dm": ("public", "dm"), "player": ("public", "player")}
RRF_K = 60
STOPWORDS = set("""a an and are as at be but by can do does for from how i if in is it its
me my of on or so that the their them then there this to was what when where which who
why will with you your we our""".split())


@dataclass
class Hit:
    chunk_id: int
    source_key: str
    source_title: str
    heading_path: str
    title: str
    page_start: int
    page_end: int
    text: str
    score: float
    fts_rank: int | None = None
    vec_rank: int | None = None
    ref_type: str | None = None     # "entry" / "character" for app-made chunks
    ref_id: int | None = None
    visibility: str = "public"

    @property
    def location(self) -> str:
        if not self.page_start:                      # notes / characters have no pages
            return self.title
        return f"p.{self.page_start}" if self.page_start == self.page_end \
            else f"pp.{self.page_start}-{self.page_end}"

    @property
    def citation(self) -> str:
        return f"{self.source_key} {self.location}" if self.page_start \
            else f"{self.source_key}: {self.title}"


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9']+", q.lower()) if w not in STOPWORDS]
    words = [w.replace("'", "") for w in words if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words)


def _filter(vis: tuple, kinds: tuple | None) -> tuple[str, tuple]:
    """SQL condition on alias `c` (chunks): visibility, and optionally source kind."""
    sql = f"c.visibility IN ({','.join('?' * len(vis))})"
    params: tuple = vis
    if kinds:
        sql += (f" AND c.source_id IN (SELECT id FROM sources WHERE kind IN "
                f"({','.join('?' * len(kinds))}))")
        params += kinds
    return sql, params


def _fts_ids(conn, q: str, vis: tuple, kinds, limit: int) -> list[int]:
    expr = _fts_query(q)
    if not expr:
        return []
    where, params = _filter(vis, kinds)
    # bm25 weights: title 8x, heading path 3x, body 1x -> "Grappled" finds the condition
    rows = conn.execute(f"""
        SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid
        WHERE chunks_fts MATCH ? AND {where}
        ORDER BY bm25(chunks_fts, 8.0, 3.0, 1.0) LIMIT ?""", (expr, *params, limit)).fetchall()
    return [r[0] for r in rows]


_VEC_CACHE: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}


def _vectors(conn, vis: tuple, kinds) -> tuple[np.ndarray, np.ndarray]:
    stamp = conn.execute("SELECT COUNT(*), MAX(rowid) FROM chunk_vectors").fetchone()
    key = (config.EMBED_MODEL, vis, kinds, tuple(stamp))
    if key not in _VEC_CACHE:
        where, params = _filter(vis, kinds)
        rows = conn.execute(f"""
            SELECT v.chunk_id, v.vec FROM chunk_vectors v JOIN chunks c ON c.id = v.chunk_id
            WHERE v.model = ? AND {where}""", (config.EMBED_MODEL, *params)).fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows \
            else np.zeros((0, 1), dtype=np.float32)
        if len(_VEC_CACHE) > 8:
            _VEC_CACHE.clear()
        _VEC_CACHE[key] = (ids, mat)
    return _VEC_CACHE[key]


def _vec_ids(conn, q: str, vis: tuple, kinds, limit: int) -> list[int]:
    ids, mat = _vectors(conn, vis, kinds)
    if len(ids) == 0:
        return []
    qv = llm.embed([q], kind="query")[0]
    scores = mat @ qv
    top = np.argsort(-scores)[:limit]
    return [int(ids[i]) for i in top]


def search(q: str, role: str = "dm", k: int = 6, conn: sqlite3.Connection | None = None,
           mode: str = "hybrid", kinds: tuple[str, ...] | None = None,
           user_id: int | None = None) -> list[Hit]:
    """mode: hybrid | fts | vec (the last two are for comparing in the eval).
    kinds: restrict to source kinds, e.g. ("rules",) or ("adventure",).
    user_id: a player's id also unlocks their own personal notes."""
    vis = ROLE_VISIBILITY[role]
    if user_id and role != "dm":
        vis = vis + (f"user:{user_id}",)
    kinds = tuple(kinds) if kinds else None
    conn = conn or db.connect()
    pool = max(30, k * 5)
    fts = _fts_ids(conn, q, vis, kinds, pool) if mode in ("hybrid", "fts") else []
    vec = _vec_ids(conn, q, vis, kinds, pool) if mode in ("hybrid", "vec") else []

    scores: dict[int, float] = {}
    for rank, cid in enumerate(fts):
        scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank)
    for rank, cid in enumerate(vec):
        scores[cid] = scores.get(cid, 0) + 1 / (RRF_K + rank)
    best = sorted(scores, key=scores.get, reverse=True)[:k]
    if not best:
        return []

    marks = ",".join("?" * len(best))
    rows = {r["id"]: r for r in conn.execute(f"""
        SELECT c.*, s.key AS source_key, s.title AS source_title, s.page_offset
        FROM chunks c JOIN sources s ON s.id = c.source_id WHERE c.id IN ({marks})""", best)}
    fts_rank = {cid: i for i, cid in enumerate(fts)}
    vec_rank = {cid: i for i, cid in enumerate(vec)}
    def page(p):
        return p + r["page_offset"] if p else 0

    return [Hit(cid, r["source_key"], r["source_title"], r["heading_path"], r["title"],
                page(r["page_start"]), page(r["page_end"]),
                r["text"], scores[cid], fts_rank.get(cid), vec_rank.get(cid),
                r["ref_type"], r["ref_id"], r["visibility"])
            for cid in best if (r := rows.get(cid))]


def ref_hits(conn, role: str, ref_type: str, ref_ids: list[int] | None = None) -> list[Hit]:
    """Fetch app-made chunks (party overview, character sheets) directly - still
    filtered by the role's visibility like every other search."""
    vis = ROLE_VISIBILITY[role]
    sql = (f"SELECT c.*, s.key AS source_key, s.title AS source_title FROM chunks c "
           f"JOIN sources s ON s.id = c.source_id WHERE c.ref_type = ? "
           f"AND c.visibility IN ({','.join('?' * len(vis))})")
    params: list = [ref_type, *vis]
    if ref_ids is not None:
        if not ref_ids:
            return []
        sql += f" AND c.ref_id IN ({','.join('?' * len(ref_ids))})"
        params += ref_ids
    sql += " ORDER BY c.ref_id, c.visibility DESC"     # public sheet before DM backstory
    return [Hit(r["id"], r["source_key"], r["source_title"], r["heading_path"], r["title"],
                0, 0, r["text"], 1.0, None, None, r["ref_type"], r["ref_id"], r["visibility"])
            for r in conn.execute(sql, params)]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--role", choices=ROLE_VISIBILITY, default="player")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--mode", choices=("hybrid", "fts", "vec"), default="hybrid")
    ap.add_argument("--full", action="store_true", help="print whole chunk text")
    args = ap.parse_args(argv)
    for i, h in enumerate(search(args.query, args.role, args.k, mode=args.mode), 1):
        print(f"{i}. [{h.citation}] {h.heading_path}   "
              f"(fts #{h.fts_rank}, vec #{h.vec_rank})")
        body = h.text if args.full else h.text[:240].replace("\n", " ") + "..."
        print("   " + body + "\n")


if __name__ == "__main__":
    main()
