"""The state file: what the previous fetch saw."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from prtop.state import load_state, save_state

FRESH = {"open_mrs": {}, "deployments": {}, "last_checked": None}


class StateFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "prtop" / "github.com.json"

    def test_the_first_fetch_starts_from_nothing(self):
        self.assertEqual(load_state(self.path), FRESH)

    def test_a_malformed_file_is_reported_and_started_afresh(self):
        self.path.parent.mkdir()
        self.path.write_text("{not json")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(load_state(self.path), FRESH)
        self.assertIn("is malformed; starting fresh", stderr.getvalue())

    def test_a_saved_state_reads_back_whole_and_leaves_no_temporary_file(self):
        save_state(self.path, {"open_mrs": {"web!1": {"title": "équipe"}}, "deployments": {}})
        state = load_state(self.path)
        self.assertEqual(state["open_mrs"], {"web!1": {"title": "équipe"}})
        self.assertIsNotNone(state["last_checked"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])


if __name__ == "__main__":
    unittest.main()
