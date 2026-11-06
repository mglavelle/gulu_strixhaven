"""Score retrieval against eval/questions.yaml.

    python -m eval.run_eval                       # hybrid, current EMBED_MODEL
    python -m eval.run_eval --compare             # fts vs vec vs hybrid
    python -m eval.run_eval --models nomic-embed-text,qwen3-embedding:0.6b

Adventure questions (DM-only content) report pass/fail + page only, never text.
"""
import argparse
from pathlib import Path

import yaml

from app import config, db, search
from app.indexing import embed_missing

HERE = Path(__file__).resolve().parent
K = 5


def rank_of(hits, item) -> int | None:
    exp = [e.lower() for e in item.get("expect", [])]
    exp_text = [e.lower() for e in item.get("expect_text", [])]
    for i, h in enumerate(hits):
        if any(e in h.heading_path.lower() for e in exp) or \
                any(e in h.text.lower() for e in exp_text):
            return i
    return None


def score(conn, items, role, mode, label, show_text) -> dict:
    r1 = r5 = mrr = 0.0
    misses = []
    for it in items:
        hits = search.search(it["q"], role=role, k=K, conn=conn, mode=mode)
        r = rank_of(hits, it)
        if r is not None:
            r1 += r == 0
            r5 += 1
            mrr += 1 / (r + 1)
        else:
            misses.append((it["q"], hits))
    n = len(items) or 1
    res = {"label": label, "hit@1": r1 / n, "hit@5": r5 / n, "mrr": mrr / n, "n": len(items)}
    print(f"  {label:<38} hit@1 {res['hit@1']:.0%}  hit@5 {res['hit@5']:.0%}  "
          f"MRR {res['mrr']:.2f}  (n={len(items)})")
    for q, hits in misses:
        print(f"      MISS: {q}")
        for h in hits[:3]:
            where = h.heading_path if show_text else f"{h.source_key} p.{h.page_start}"
            print(f"            got [{h.citation}] {where}")
    return res


def spoiler_check(conn, queries) -> bool:
    ok = True
    leaked = 0
    for q in queries:
        hits = search.search(q, role="player", k=20, conn=conn)
        bad = [h for h in hits if conn.execute(
            "SELECT visibility FROM chunks WHERE id=?", (h.chunk_id,)).fetchone()[0] == "dm"]
        leaked += len(bad)
        ok &= not bad
    dm_hits = sum(1 for q in queries for h in search.search(q, role="dm", k=5, conn=conn)
                  if h.source_key != "SRD")
    print(f"  spoiler check: {len(queries)} player searches, {leaked} DM-only results leaked "
          f"-> {'PASS' if ok else 'FAIL'}")
    print(f"  (same searches as DM found {dm_hits} adventure-book results - shows the book is indexed)")
    return ok


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="fts vs vec vs hybrid")
    ap.add_argument("--models", help="comma-separated embedding models to compare")
    args = ap.parse_args(argv)

    data = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))
    conn = db.connect()
    models = args.models.split(",") if args.models else [config.EMBED_MODEL]
    modes = ("fts", "vec", "hybrid") if args.compare else ("hybrid",)

    for model in models:
        config.EMBED_MODEL = model
        embed_missing(conn, model)
        print(f"\n== embedding model: {model}")
        for mode in modes:
            score(conn, data.get("rules") or [], "player", mode, f"rules [{mode}]", True)
            if data.get("adventure"):
                score(conn, data["adventure"], "dm", mode, f"adventure [{mode}]", False)
    print()
    spoiler_check(conn, data.get("spoiler_checks") or [])


if __name__ == "__main__":
    main()
