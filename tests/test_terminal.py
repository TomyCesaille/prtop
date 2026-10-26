"""Keys as terminals send them, and the screen taken and given back."""

import io
import os
import select
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from prtop.terminal import (
    CLEAR,
    ENABLE_VIRTUAL_TERMINAL_PROCESSING,
    ENTER,
    HOME,
    LEAVE,
    _exit_on,
    _PosixTerminal,
    _WindowsTerminal,
    decode_keys,
    fullscreen,
)

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

    def test_control_characters_without_a_name_are_dropped(self):
        self.assertEqual(decode_keys("\x01q"), ["q"])


class FakeTermios:
    TCSADRAIN = 1

    def __init__(self):
        self.restored = []

    def tcgetattr(self, fd):
        return ["the keyboard's modes"]

    def tcsetattr(self, fd, when, modes):
        self.restored.append((fd, when, modes))


class FakeKernel32:
    def __init__(self, mode):
        self.mode, self.modes_set = mode, []

    def GetStdHandle(self, which):
        return "console"

    def GetConsoleMode(self, console, mode):
        if self.mode is None:
            return 0
        mode.value = self.mode
        return 1

    def SetConsoleMode(self, console, mode):
        self.modes_set.append(mode)


def fake_ctypes(kernel32):
    return SimpleNamespace(
        windll=SimpleNamespace(kernel32=kernel32),
        c_uint32=lambda: SimpleNamespace(value=0),
        byref=lambda value: value,
    )


class FakeMsvcrt:
    """A console whose keyboard buffer fills after `misses` empty looks."""

    def __init__(self, chars, misses=0):
        self.chars, self.misses = list(chars), misses

    def kbhit(self):
        if self.misses:
            self.misses -= 1
            return False
        return bool(self.chars)

    def getwch(self):
        return self.chars.pop(0)


class Screen(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        with redirect_stdout(self.out):
            self.screen = _PosixTerminal()

    def test_it_takes_the_window_size_from_the_terminal(self):
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 40))):
            self.assertEqual(self.screen.size(), (120, 40))

    def test_a_frame_already_on_screen_is_not_drawn_again(self):
        self.screen.draw(["a", "b"])
        self.screen.draw(["a", "b"])
        self.assertEqual(self.out.getvalue().count(HOME), 1)

    def test_a_repaint_clears_the_screen_and_draws_the_next_frame_whole(self):
        self.screen.draw(["a"])
        self.screen.redraw()
        self.screen.draw(["a"])
        self.assertEqual(self.out.getvalue().count(HOME), 2)
        self.assertIn(CLEAR, self.out.getvalue())

    def test_a_terminate_signal_exits_through_the_screens_cleanup(self):
        with self.assertRaises(SystemExit) as caught:
            _exit_on(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)

    def test_each_platform_gets_its_own_terminal(self):
        with redirect_stdout(io.StringIO()):
            self.assertIsInstance(fullscreen("win32"), _WindowsTerminal)
            self.assertIsInstance(fullscreen("linux"), _PosixTerminal)


class PosixTerminal(unittest.TestCase):
    def test_the_screen_and_the_keyboard_modes_are_given_back(self):
        termios, cbreak, out = FakeTermios(), [], io.StringIO()
        tty = SimpleNamespace(setcbreak=cbreak.append)
        before = signal.getsignal(signal.SIGTERM)
        modules = mock.patch.dict(sys.modules, {"termios": termios, "tty": tty})
        stdin = mock.patch("sys.stdin", SimpleNamespace(fileno=lambda: 5))
        with modules, stdin, redirect_stdout(out), fullscreen("linux"):
            during = signal.getsignal(signal.SIGTERM)
        self.assertEqual((cbreak, during), ([5], _exit_on))
        self.assertEqual(termios.restored, [(5, FakeTermios.TCSADRAIN, ["the keyboard's modes"])])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(out.getvalue(), ENTER + LEAVE)

    def test_keys_are_read_as_they_are_pressed(self):
        with redirect_stdout(io.StringIO()):
            terminal = _PosixTerminal()
        terminal._fd = 5
        with mock.patch("select.select", return_value=([5], [], [])), mock.patch("os.read", return_value=b"q\x0c"):
            self.assertEqual(terminal.read_keys(0.2), ["q", "Ctrl+L"])
        with mock.patch("select.select", return_value=([], [], [])):
            self.assertEqual(terminal.read_keys(0.2), [])


class WindowsTerminal(unittest.TestCase):
    def enter_and_leave(self, mode):
        kernel32, out = FakeKernel32(mode), io.StringIO()
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes(kernel32)}), redirect_stdout(out), fullscreen("win32"):
            pass
        return kernel32.modes_set, out.getvalue()

    def test_the_console_reads_escape_sequences_until_it_is_given_back(self):
        modes, out = self.enter_and_leave(7)
        self.assertEqual(modes, [7 | ENABLE_VIRTUAL_TERMINAL_PROCESSING, 7])
        self.assertEqual(out, ENTER + LEAVE)

    def test_a_console_without_a_mode_is_left_alone(self):
        self.assertEqual(self.enter_and_leave(None)[0], [])

    def read_keys(self, msvcrt, timeout):
        with redirect_stdout(io.StringIO()):
            terminal = _WindowsTerminal()
        with mock.patch.dict(sys.modules, {"msvcrt": msvcrt}):
            return terminal.read_keys(timeout)

    def test_no_key_within_the_timeout_reads_as_none(self):
        self.assertEqual(self.read_keys(FakeMsvcrt([]), 0), [])

    def test_function_keys_arrive_as_scan_codes_and_others_are_dropped(self):
        console = FakeMsvcrt(["\x00", ";", "\xe0", "H", "\x00", "X", "q"], misses=1)
        self.assertEqual(self.read_keys(console, 5), ["F1", "q"])


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
