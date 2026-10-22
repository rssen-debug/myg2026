"""Adversarial archive-restoration checks. No network and no real trading."""
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path
import importlib.util

SCRIPT = Path(__file__).parent / ".github" / "scripts" / "restore_paper_state.py"
spec = importlib.util.spec_from_file_location("restore_paper_state", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def archive(entries):
    mem = io.BytesIO()
    with zipfile.ZipFile(mem,"w") as z:
        for name, data in entries:
            z.writestr(name,data)
    return mem.getvalue()


def sqlite_bytes():
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/"paper.db"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE x (id INTEGER)")
            c.execute("INSERT INTO x VALUES (42)")
        return path.read_bytes()


class TestSafePaperRestore(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def test_truncated_sqlite_page_is_rejected_before_publish(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        with self.assertRaisesRegex(ValueError, "Truncated paper SQLite page data"):
            module.safe_extract_zip(
                archive([("paper_forward.db", original[:-1])]), self.path
            )
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_invalid_sqlite_page_size_is_rejected(self):
        original = sqlite_bytes()
        damaged = bytearray(original)
        damaged[16:18] = bytes((3, 0))
        with self.assertRaisesRegex(ValueError, "Invalid paper SQLite page size"):
            module.safe_extract_zip(
                archive([("paper_forward.db", bytes(damaged))]), self.path
            )

    def test_mixed_wal_header_flags_are_rejected(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        for flags in ((2, 1), (1, 2)):
            altered = bytearray(original)
            altered[18:20] = bytes(flags)
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(ValueError, "WAL-mode paper database"):
                    module.safe_extract_zip(
                        archive([("paper_forward.db", bytes(altered))]), self.path
                    )
                self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_wal_mode_database_is_rejected_before_state_overwrite(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        wal_header = bytearray(original)
        wal_header[18:20] = bytes((2, 2))
        with self.assertRaisesRegex(ValueError, "WAL-mode paper database"):
            module.safe_extract_zip(
                archive([("paper_forward.db", bytes(wal_header))]), self.path
            )
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_encrypted_member_rejected_before_state_writes(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        payload = bytearray(archive([("paper_forward.db", original)]))
        central = payload.find(bytes((80, 75, 1, 2)))
        self.assertGreaterEqual(central, 0)
        payload[central + 8] |= 0x01  # Central-directory encryption flag
        with self.assertRaisesRegex(ValueError, "Encrypted paper ZIP member"):
            module.safe_extract_zip(bytes(payload), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_zip_metadata_budget_rejects_before_state_writes(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        payload = archive([
            ("paper_forward.db", original),
            ("paper_forward.log", b"paper"),
        ])
        with mock.patch.object(module, "MAX_ZIP_METADATA_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "Oversized paper ZIP metadata"):
                module.safe_extract_zip(payload, self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_archive_member_count_limit_rejects_before_state_writes(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        payload = archive([
            ("paper_forward.db", original),
            ("paper_forward.log", b"paper"),
        ])
        with mock.patch.object(module, "MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "Too many paper archive members"):
                module.safe_extract_zip(payload, self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_streaming_restore_accepts_multi_chunk_sqlite(self):
        original = sqlite_bytes()
        payload = archive([
            ("paper_forward.db", original),
            ("paper_forward.log", b"paper"),
        ])
        with mock.patch.object(module, "MAX_SQLITE_BYTES", len(original) + 1024):
            with mock.patch.object(module, "MAX_FILE_BYTES", 1024):
                restored = module.safe_extract_zip(payload, self.path)
        self.assertIn("paper_forward.db", restored)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_declared_size_preflight_preserves_existing_db(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        with mock.patch.object(module, "MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "Oversized paper file"):
                module.safe_extract_zip(archive([
                    ("paper_forward.db", original),
                    ("paper_forward.log", b"12345"),
                ]), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_total_uncompressed_archive_limit_rejects_before_writes(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        with mock.patch.object(module, "MAX_TOTAL_EXTRACT_BYTES", len(original) + 4):
            with self.assertRaisesRegex(ValueError, "Oversized total paper extraction"):
                module.safe_extract_zip(archive([
                    ("paper_forward.db", original),
                    ("paper_forward.log", b"12345"),
                ]), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)

    def test_sqlite_has_separate_larger_limit(self):
        self.assertGreater(module.MAX_SQLITE_BYTES, module.MAX_FILE_BYTES)
        self.assertEqual(module.MAX_ARCHIVE_BYTES, 200 * 1024 * 1024)

    def test_large_sqlite_header_allowed_but_other_members_rejected(self):
        from unittest.mock import patch
        # Test boundary logic without allocating hundreds of megabytes.
        with patch.object(module, "MAX_FILE_BYTES", 32), patch.object(module, "MAX_SQLITE_BYTES", 65536):
            database = sqlite_bytes()
            module.safe_extract_zip(archive([("paper_forward.db", database)]), self.path)
            with self.assertRaisesRegex(ValueError, "Oversized paper file"):
                module.safe_extract_zip(archive([("paper_forward.db", database), ("paper_forward.log", b"x" * 33)]), self.path)

    def test_valid_archive_retains_database(self):
        data=sqlite_bytes()
        result=module.safe_extract_zip(archive([("paper_forward.db",data)]),self.path)
        self.assertEqual(result,{"paper_forward.db"})
        with sqlite3.connect(self.path/"paper_forward.db") as c:
            self.assertEqual(c.execute("SELECT id FROM x").fetchone()[0],42)

    def test_real_artifact_contains_reports_but_restores_only_state(self):
        database = sqlite_bytes()
        zipped = archive([
            ("paper_forward.db", database),
            ("forward_profit_gate.json", b'{"status":"NOT_READY"}'),
            ("forward_capacity_gate.json", b'{"live_approved":false}'),
            ("forward_reports/example.json", b'{"simulated":true}'),
        ])
        result = module.safe_extract_zip(zipped, self.path)
        self.assertIn("paper_forward.db", result)
        self.assertTrue((self.path/"paper_forward.db").exists())
        self.assertFalse((self.path/"forward_profit_gate.json").exists())
        self.assertFalse((self.path/"forward_capacity_gate.json").exists())

    def test_traversal_behind_report_folder_rejected(self):
        with self.assertRaisesRegex(ValueError,"unsafe"):
            module.safe_extract_zip(archive([
                ("paper_forward.db", sqlite_bytes()),
                ("forward_reports/../../.env", b"secret")]), self.path)

    def test_skip_failed_report_only_artifact_and_restore_prior_state(self):
        from unittest.mock import Mock, patch
        recent = archive([("forward_profit_gate.json", b'{"status":"NOT_READY"}')])
        previous = archive([("paper_forward.db", sqlite_bytes())])
        response_list = Mock()
        response_list.json.return_value = {"artifacts": [
            {"name":"godlike-forward-state","id":20,"expired":False,
             "created_at":"2026-10-14T19:00:00Z","archive_download_url":"https://example.test/20",
             "workflow_run":{"id":20}},
            {"name":"godlike-forward-state","id":10,"expired":False,
             "created_at":"2026-10-14T18:00:00Z","archive_download_url":"https://example.test/10",
             "workflow_run":{"id":10}},
        ]}
        response_recent = Mock(content=recent)
        response_previous = Mock(content=previous)
        cwd = Path.cwd()
        try:
            os.chdir(self.path)
            with patch.dict(os.environ, {
                "GITHUB_REPOSITORY":"rssen-debug/myg2026",
                "GITHUB_TOKEN":"synthetic-test-token",
                "GITHUB_RUN_ID":"30",
            }), patch("requests.get", side_effect=[response_list, response_recent, response_previous]) as getter:
                module.main()
                self.assertEqual(getter.call_count,3)
            with sqlite3.connect(self.path/"paper_forward.db") as c:
                self.assertEqual(c.execute("SELECT id FROM x").fetchone()[0],42)
            events = [json.loads(s) for s in (self.path/"forward_pulse_runs.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["artifact_id"],10)
            self.assertEqual(events[-1]["status"],"RESTORED")
        finally:
            os.chdir(cwd)

    def test_missing_database_rejected(self):
        with self.assertRaisesRegex(ValueError,"No paper_forward.db"):
            module.safe_extract_zip(archive([("paper_forward.log",b"log")]),self.path)

    def test_zip_slip_rejected(self):
        with self.assertRaisesRegex(ValueError,"unsafe"):
            module.safe_extract_zip(archive([("../.ssh/id_rsa",b"secret")]),self.path)

    def test_unknown_filename_rejected(self):
        with self.assertRaisesRegex(ValueError,"unsafe"):
            module.safe_extract_zip(archive([("paper_forward.db",sqlite_bytes()),(".env",b"secret")]),self.path)

    def test_duplicate_filename_rejected(self):
        with self.assertRaisesRegex(ValueError,"unsafe"):
            module.safe_extract_zip(archive([("paper_forward.db",sqlite_bytes()),("paper_forward.db",sqlite_bytes())]),self.path)

    def test_publish_failure_rolls_back_previous_files(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        (self.path / "paper_forward.settings.json").write_bytes(b"old")
        real_replace = os.replace
        failures = [1]
        def fail_publish(src, dst):
            if (failures and Path(src).name == "paper_forward.settings.json"
                    and ".paper-restore-stage-" in str(src)
                    and Path(dst).parent == self.path):
                failures.pop()
                raise OSError("simulated publish failure")
            return real_replace(src, dst)
        with mock.patch.object(module.os, "replace", side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                module.safe_extract_zip(archive([
                    ("paper_forward.db", original),
                    ("paper_forward.settings.json", b"new"),
                ]), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)
        self.assertEqual((self.path / "paper_forward.settings.json").read_bytes(), b"old")

    def test_staging_write_failure_preserves_existing_state(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        (self.path / "paper_forward.settings.json").write_bytes(b"original")
        real_write = Path.write_bytes
        def fail_settings(path, data):
            if path.name == "paper_forward.settings.json" and ".paper-restore-stage-" in str(path):
                raise OSError("simulated disk full")
            return real_write(path, data)
        with mock.patch.object(Path, "write_bytes", autospec=True, side_effect=fail_settings):
            with self.assertRaisesRegex(OSError, "simulated disk full"):
                module.safe_extract_zip(archive([
                    ("paper_forward.db", original),
                    ("paper_forward.settings.json", b"new"),
                ]), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)
        self.assertEqual((self.path / "paper_forward.settings.json").read_bytes(), b"original")

    def test_corrupt_db_never_overwrites_existing_state(self):
        original = sqlite_bytes()
        (self.path / "paper_forward.db").write_bytes(original)
        (self.path / "paper_forward.settings.json").write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "Invalid paper SQLite"):
            module.safe_extract_zip(archive([
                ("paper_forward.db", b"invalid sqlite"),
                ("paper_forward.settings.json", b"replacement"),
            ]), self.path)
        self.assertEqual((self.path / "paper_forward.db").read_bytes(), original)
        self.assertEqual((self.path / "paper_forward.settings.json").read_bytes(), b"existing")

    def test_corrupt_db_rejected(self):
        with self.assertRaisesRegex(ValueError,"Invalid paper SQLite"):
            module.safe_extract_zip(archive([("paper_forward.db",b"not sqlite")]),self.path)


if __name__=="__main__":
    unittest.main()
