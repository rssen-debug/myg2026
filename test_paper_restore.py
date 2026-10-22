"""Adversarial archive-restoration checks. No network and no real trading."""
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
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

    def test_corrupt_db_rejected(self):
        with self.assertRaisesRegex(ValueError,"Invalid paper SQLite"):
            module.safe_extract_zip(archive([("paper_forward.db",b"not sqlite")]),self.path)


if __name__=="__main__":
    unittest.main()
