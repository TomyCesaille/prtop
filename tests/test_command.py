"""The forge command-line tools prtop runs, `glab` and `gh`."""

import subprocess
import unittest
from unittest import mock

from prtop.forges.command import get_json
from prtop.model import ForgeError

URL = "https://cli.example/install"


def finished(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class Commands(unittest.TestCase):
    def test_a_tool_that_is_not_installed_says_where_to_get_it(self):
        missing = mock.patch("subprocess.run", side_effect=FileNotFoundError)
        with missing, self.assertRaisesRegex(ForgeError, f"gh is not installed: {URL}"):
            get_json(["gh", "api", "user"], URL)

    def test_a_failing_command_is_named_with_what_it_printed(self):
        refused = mock.patch("subprocess.run", return_value=finished(1, stderr="401 Unauthorized\n"))
        with refused, self.assertRaisesRegex(ForgeError, r"^glab api 'a b' failed\n401 Unauthorized$"):
            get_json(["glab", "api", "a b"], URL)

    def test_a_command_that_fails_silently_on_stderr_shows_its_output(self):
        quiet = mock.patch("subprocess.run", return_value=finished(1, stdout="not found"))
        with quiet, self.assertRaisesRegex(ForgeError, "not found"):
            get_json(["glab", "api", "x"], URL)

    def test_the_json_it_prints_is_read(self):
        with mock.patch("subprocess.run", return_value=finished(stdout='{"id": 7}')):
            self.assertEqual(get_json(["glab", "api", "x"], URL), {"id": 7})

    def test_a_command_that_prints_nothing_reads_as_none(self):
        with mock.patch("subprocess.run", return_value=finished(stdout="  \n")):
            self.assertIsNone(get_json(["glab", "api", "x"], URL))


if __name__ == "__main__":
    unittest.main()
