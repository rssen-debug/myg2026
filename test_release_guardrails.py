"""Minimal offline release checks for GODLIKE import."""
import ast
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parent
class TestReleaseGuardrails(unittest.TestCase):
    def test_launcher_present(self):
        self.assertTrue((ROOT / "START_PAPER.cmd").is_file())
    def test_launcher_paper_only(self):
        script = (ROOT / "START_PAPER.cmd").read_text(encoding="utf-8").lower()
        for marker in ("--realtime", "--execution quote", "--cash 100", "--trade-budget 20"):
            self.assertIn(marker, script)
        self.assertNotIn(" --live", script)
    def test_explicit_live_block(self):
        tree = ast.parse((ROOT / "godlike2026.py").read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        self.assertTrue(any(isinstance(n, ast.If) and isinstance(n.test, ast.Attribute) and n.test.attr == "live" for n in ast.walk(main)))
    def test_test_runner_fails_without_tests(self):
        script = (ROOT / "godlike2026.py").read_text(encoding="utf-8")
        self.assertIn("if count == 0:", script)
        self.assertIn("refusing to report green", script)
    def test_original_source_manifest_preserved(self):
        self.assertTrue((ROOT / "SOURCE_MANIFEST_SHA256.json").is_file())

if __name__ == "__main__": unittest.main()
