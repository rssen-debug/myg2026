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
    def test_snapshot_artifact_roundtrip_through_restore(self):
        """Exercise real backup + ZIP restoration with committed WAL evidence."""
        import io
        import zipfile
        restore_path = Path(__file__).parent / ".github" / "scripts" / "restore_paper_state.py"
        restore_spec = importlib.util.spec_from_file_location("paper_restore_roundtrip", restore_path)
        restore = importlib.util.module_from_spec(restore_spec)
        restore_spec.loader.exec_module(restore)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, snapshot, recovered = root / "live.db", root / "artifact.db", root / "restored"
            with sqlite3.connect(source) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE evidence (seq INTEGER PRIMARY KEY, mode TEXT)")
                writer.execute("INSERT INTO evidence VALUES (1, 'PAPER')")
                writer.commit()
                module.snapshot_paper_db(source, snapshot)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.write(snapshot, arcname="paper_forward.db")
            restored = restore.safe_extract_zip(archive.getvalue(), recovered)
            self.assertIn("paper_forward.db", restored)
            with sqlite3.connect(recovered / "paper_forward.db") as conn:
                self.assertEqual(conn.execute("SELECT mode FROM evidence").fetchone(), ("PAPER",))
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone(), ("ok",))

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

    def test_forward_workflow_uploads_only_validated_snapshot(self):
        workflow = (Path(__file__).parent / ".github" / "workflows" /
                    "godlike-forward-pulse.yml").read_text(encoding="utf-8")
        self.assertIn("module.snapshot_paper_db(", workflow)
        self.assertIn("path: paper_artifact/", workflow)
        self.assertIn("timeout-minutes: 25", workflow)
        self.assertIn("snapshot_manifest.json", workflow)
        self.assertIn("hashlib.file_digest", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("if: steps.snapshot.outcome == 'success'", workflow)
        self.assertNotIn("            paper_forward.db\\n", workflow)

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
