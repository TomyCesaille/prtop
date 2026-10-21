"""Keys as terminals send them, and the screen taken and given back."""

import os
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

from prtop.terminal import ENTER, LEAVE, decode_keys

ROOT = Path(__file__).parents[1]


class Keys(unittest.TestCase):
    def test_function_keys_read_the_same_whichever_sequence_the_terminal_sends(self):
        self.assertEqual(decode_keys("\033OP\033[11~\033[21~"), ["F1", "F1", "F10"])

    def test_control_keys_are_named(self):
        self.assertEqual(decode_keys("\x0c\x03"), ["Ctrl+L", "Ctrl+C"])

    def test_keys_prtop_has_no_use_for_are_dropped(self):
        self.assertEqual(decode_keys("\033[A\033[1;5Cq"), ["q"])

    def test_a_lone_escape_reads_as_esc(self):
        self.assertEqual(decode_keys("\033"), ["Esc"])


@unittest.skipUnless(os.name == "posix", "needs a pseudo-terminal")
class FullScreen(unittest.TestCase):
    def run_demo(self, *args, keys=b""):
        main, child = os.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-m", "prtop", "--demo", *args],
            stdin=child, stdout=child, stderr=child, cwd=ROOT,
            env={**os.environ, "COLUMNS": "180", "LINES": "40"},
        )
        os.close(child)
        # The terminal holds little, so it is read the whole time the view runs.
        output = self.read(main, lambda out: b"PR title" in out)
        os.write(main, keys)
        output += self.read(main, lambda out: proc.poll() is not None)
        output += self.read(main, lambda out: False, timeout=0.5)
        os.close(main)
        proc.wait(timeout=5)
        return proc.returncode, output.decode("utf-8", "replace")

    def read(self, fd, done, timeout=10.0):
        output, deadline = b"", time.monotonic() + timeout
        while not done(output) and time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.05)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        return output

    def test_q_gives_the_screen_back(self):
        code, output = self.run_demo(keys=b"q")
        self.assertEqual(code, 0)
        self.assertIn(ENTER, output)
        self.assertTrue(output.rstrip().endswith(LEAVE), output[-200:])

    def test_with_n_the_last_table_stays_on_the_screen(self):
        code, output = self.run_demo("-n", "1")
        self.assertEqual(code, 0)
        self.assertIn("PR title", output.partition(LEAVE)[2])


if __name__ == "__main__":
    unittest.main()
