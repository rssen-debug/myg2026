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

    def test_restore_verifies_manifest_and_rejects_tampering(self):
        import hashlib
        import io
        import json
        import zipfile
        restore_path = Path(__file__).parent / ".github" / "scripts" / "restore_paper_state.py"
        restore_spec = importlib.util.spec_from_file_location("paper_restore_manifest", restore_path)
        restore = importlib.util.module_from_spec(restore_spec)
        restore_spec.loader.exec_module(restore)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE t (value TEXT)")
                db.execute("INSERT INTO t VALUES ('PAPER')")
            content = source.read_bytes()
            manifest = {"format": 1, "database": "paper_forward.db", "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()}
            def archive(meta):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("paper_forward.db", content)
                    z.writestr("snapshot_manifest.json", json.dumps(meta))
                return stream.getvalue()
            restored = root / "restored"
            restore.safe_extract_zip(archive(manifest), restored)
            self.assertFalse((restored / "snapshot_manifest.json").exists())
            bad = dict(manifest, sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                restore.safe_extract_zip(archive(bad), restored)
            with sqlite3.connect(restored / "paper_forward.db") as db:
                self.assertEqual(db.execute("SELECT value FROM t").fetchone(), ("PAPER",))

    def test_snapshot_full_integrity_check_failure_preserves_destination(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / "source.db", Path(root) / "snapshot.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE evidence (value TEXT)")
                db.execute("INSERT INTO evidence VALUES ('PAPER')")
            original_connect = module.sqlite3.connect

            class RejectIntegrity(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql == "PRAGMA integrity_check":
                        return super().execute("SELECT 'corrupt'")
                    return super().execute(sql, *args)

            def connect(path, *args, **kwargs):
                if Path(str(path)).name.startswith(".paper-snapshot-"):
                    kwargs["factory"] = RejectIntegrity
                return original_connect(path, *args, **kwargs)

            with mock.patch.object(module.sqlite3, "connect", side_effect=connect):
                with self.assertRaisesRegex(ValueError, "integrity_check"):
                    module.snapshot_paper_db(source, dest)
            self.assertFalse(dest.exists())
            self.assertEqual(list(Path(root).glob(".paper-snapshot-*")), [])

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
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("compression-level: 1", workflow)
        self.assertIn("overwrite: false", workflow)
        self.assertNotIn("overwrite: true", workflow)
        self.assertIn("timeout-minutes: 25", workflow)
        self.assertIn("snapshot_manifest.json", workflow)
        self.assertIn("id: paper_runtime", workflow)
        self.assertIn("id: paper_verify", workflow)
        self.assertIn('conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"', workflow)
        self.assertNotIn('conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"', workflow)
        self.assertIn("if: steps.paper_runtime.outcome == 'success' && steps.paper_verify.outcome == 'success'", workflow)
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
