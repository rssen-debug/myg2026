"""Ensure Token-2022 research flag cannot activate LIVE or legacy pool execution."""
import subprocess
import sys
from pathlib import Path
import unittest

SCRIPT=str(Path(__file__).with_name("godlike2026.py"))

class TestToken2022ResearchCLIGuard(unittest.TestCase):
    def run_cli(self,*args):
        return subprocess.run([sys.executable,SCRIPT,*args],text=True,capture_output=True,timeout=12)

    def test_live_with_flag_remains_hard_blocked(self):
        p=self.run_cli("--live","--token2022-metadata-paper-experiment")
        self.assertEqual(p.returncode,2)
        self.assertIn("Livehandel spärrad",p.stdout+p.stderr)

    def test_legacy_pool_model_cannot_use_token2022_flag(self):
        p=self.run_cli("--realtime","--execution","pool-model","--token2022-metadata-paper-experiment")
        self.assertEqual(p.returncode,2)
        self.assertIn("enbart",p.stderr)

    def test_non_realtime_mode_cannot_enable_token2022_flag(self):
        p=self.run_cli("--test","--token2022-metadata-paper-experiment")
        self.assertEqual(p.returncode,2)
        self.assertIn("enbart",p.stderr)


if __name__=="__main__":
    unittest.main()
