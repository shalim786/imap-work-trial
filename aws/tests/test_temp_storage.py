import os
import tempfile
import unittest
from pathlib import Path
from agentmail_imap.temp_storage import run_directory

class TempStorageTests(unittest.TestCase):
    def test_owned_cleanup_and_live_preservation(self):
        with tempfile.TemporaryDirectory() as root:
            live=Path(root)/'run-live';live.mkdir();(live/'owner.pid').write_text(str(os.getpid()))
            unrelated=Path(root)/'run-unmarked';unrelated.mkdir()
            with run_directory(root) as current:
                self.assertTrue(current.exists())
                self.assertEqual(current.stat().st_mode & 0o777,0o700)
                self.assertTrue(live.exists())
            self.assertFalse(current.exists())
            self.assertTrue(unrelated.exists())
