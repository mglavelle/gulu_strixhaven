"""Copy everything the app needs to another machine (e.g. the Unraid server).

    python -m ingest.export_data C:\\path\\to\\server-upload

Writes a consistent snapshot of the database (users, notes, sessions, characters,
search index) plus the book PDFs and their extracted text, so the server doesn't need to
re-process the books. Safe to run while the app is running.
"""
import shutil
import sqlite3
import sys
from pathlib import Path

from app import config


def export(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # SQLite's backup API gives a consistent copy even while the app is writing (WAL mode).
    src = sqlite3.connect(config.DB_PATH)
    out = sqlite3.connect(dest / "companion.db")
    with out:
        src.backup(out)
    out.execute("PRAGMA journal_mode = DELETE")   # single self-contained file
    out.close(); src.close()
    for sub in ("sources", "extracted"):
        if (config.DATA_DIR / sub).exists():
            shutil.copytree(config.DATA_DIR / sub, dest / sub, dirs_exist_ok=True)
    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"Exported to {dest} ({total / 1e6:.0f} MB):")
    for f in sorted(dest.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(dest)}  ({f.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    export(Path(sys.argv[1]).resolve())
