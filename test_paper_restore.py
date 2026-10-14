"""Adversarial archive-restoration checks. No network and no real trading."""
import io
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
