"""Offline tests for PAPER SQLite snapshots; no trading and no network."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPT = Path(__file__).parent / ".github" / "scripts" / "snapshot_paper_state.py"
spec = importlib.util.spec_from_file_location("snapshot_paper_state", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestPaperSnapshot(unittest.TestCase):
    def test_backup_contains_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / "source.db", Path(root) / "snapshot.db"
            with sqlite3.connect(source) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE trades (id INTEGER)")
                writer.execute("INSERT INTO trades VALUES (42)")
                writer.commit()
                module.snapshot_paper_db(source, dest)
                with sqlite3.connect(dest) as snapshot:
                    self.assertEqual(snapshot.execute("SELECT id FROM trades").fetchall(), [(42,)])
                    self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone(), ("ok",))
                    self.assertEqual(snapshot.execute("PRAGMA journal_mode").fetchone(), ("delete",))

    def test_special_characters_in_source_path(self):
        for name in ("paper #1.db", "paper %23.db", "paper ?x.db"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                source, dest = Path(root) / name, Path(root) / "snapshot.db"
                with sqlite3.connect(source) as db:
                    db.execute("CREATE TABLE t (id INTEGER)")
                    db.execute("INSERT INTO t VALUES (7)")
                module.snapshot_paper_db(source, dest)
                with sqlite3.connect(dest) as copied:
                    self.assertEqual(copied.execute("SELECT id FROM t").fetchone(), (7,))

    def test_existing_destination_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / "source.db", Path(root) / "snapshot.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE x (id INTEGER)")
            dest.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                module.snapshot_paper_db(source, dest)
            self.assertEqual(dest.read_bytes(), b"original")

    def test_missing_source_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / "missing.db", Path(root) / "snapshot.db"
            with self.assertRaises(FileNotFoundError):
                module.snapshot_paper_db(source, dest)
            self.assertFalse(dest.exists())
