"""Create a consistent PAPER-only SQLite artifact snapshot.

Run after the PAPER producer stops writing, before artifact upload.
Never alters the source database and never performs trading.
"""
from pathlib import Path
import os
import sqlite3
import tempfile


def snapshot_paper_db(source: Path, destination: Path) -> Path:
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.resolve() == destination.resolve():
        raise ValueError("Snapshot destination must differ from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".paper-snapshot-", suffix=".db",
                                         dir=destination.parent, delete=False) as tmp:
            temporary = Path(tmp.name)
        # mode=ro never creates a missing source; backup() includes committed WAL.
        with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True,
                             timeout=30) as src:
            with sqlite3.connect(temporary, timeout=30) as dst:
                src.backup(dst, pages=1024, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
                result = dst.execute("PRAGMA integrity_check").fetchone()
                if result != ("ok",):
                    raise ValueError("PAPER snapshot failed SQLite integrity_check")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
