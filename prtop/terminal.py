"""The terminal behind the live view: the screen taken over the way `htop`
takes it, and keys read as they are pressed, on macOS, Linux and Windows.

Only the standard library: Python on Windows ships without `curses`, so the
screen is drawn with escape sequences, which Windows Terminal and the Windows
console both read once virtual terminal processing is on. The modules that
exist on one platform only (`termios`, `tty`, `msvcrt`) are imported where
they are used.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import time
from typing import Self

ENTER = "\033[?1049h\033[?25l"  # the alternate screen, with the cursor hidden
LEAVE = "\033[?25h\033[?1049l"
# Mouse reporting, as `htop` asks for it: the terminal hands the wheel to prtop,
# which drops it, instead of scrolling its history behind the screen. SGR
# encoding makes each report one escape sequence.
MOUSE_ON, MOUSE_OFF = "\033[?1000h\033[?1006h", "\033[?1006l\033[?1000l"
HOME, CLEAR_TO_LINE_END, CLEAR_BELOW, CLEAR = "\033[H", "\033[K", "\033[J", "\033[2J"

# A function key reaches a POSIX terminal as an escape sequence.
NAMED_KEYS = {
    "\033OP": "F1",
    "\033[11~": "F1",
    "\033[21~": "F10",
    "\x0c": "Ctrl+L",
    "\x03": "Ctrl+C",
}
# A terminal without SGR encoding reports the mouse as `CSI M` and then three
# characters, the button and the position, that would otherwise read as keys.
_UNNAMED_SEQUENCE = re.compile("\033(\\[M...|\\[[0-?]*[ -/]*[@-~]|O.)")
# The Windows console reads it as a NUL, then the key's scan code.
SCAN_CODES = {";": "F1", "D": "F10"}


def decode_keys(text: str) -> list[str]:
    """Name each key in what the terminal sent: a printable key as itself, a
    known sequence by its name, a lone escape as `Esc`. Sequences prtop has no
    use for, the arrows and the mouse among them, are dropped."""
    keys: list[str] = []
    i = 0
    while i < len(text):
        sequence = next((s for s in NAMED_KEYS if text.startswith(s, i)), None)
        if sequence is not None:
            keys.append(NAMED_KEYS[sequence])
            i += len(sequence)
        elif match := _UNNAMED_SEQUENCE.match(text, i):
            i = match.end()
        else:
            if text[i] == "\033":
                keys.append("Esc")
            elif text[i].isprintable():
                keys.append(text[i])
            i += 1
    return keys


class _Screen:
    def __init__(self) -> None:
        self._out = sys.stdout
        self._shown: list[str] | None = None

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size()
        return size.columns, size.lines

    def draw(self, lines: list[str]) -> None:
        if lines == self._shown:
            return
        self._shown = lines
        self._write(HOME + f"{CLEAR_TO_LINE_END}\r\n".join(lines) + CLEAR_TO_LINE_END + CLEAR_BELOW)

    def redraw(self) -> None:
        self._shown = None
        self._write(CLEAR)

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()


def _exit_on(signum: int, frame: object) -> None:
    # Unwinds through the `with` that holds the screen, which gives it back.
    sys.exit(128 + signum)


class _PosixTerminal(_Screen):
    def __enter__(self) -> Self:
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._modes = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._on_term = signal.signal(signal.SIGTERM, _exit_on)
        self._write(ENTER + MOUSE_ON)
        return self

    def __exit__(self, *exc: object) -> None:
        import termios

        self._write(MOUSE_OFF + LEAVE)
        signal.signal(signal.SIGTERM, self._on_term)
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._modes)

    def read_keys(self, timeout: float) -> list[str]:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return []
        return decode_keys(os.read(self._fd, 64).decode("utf-8", "replace"))


ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11


class _WindowsTerminal(_Screen):
    def __enter__(self) -> Self:
        import ctypes

        self._kernel32 = ctypes.windll.kernel32
        self._console = self._kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        self._mode = mode.value if self._kernel32.GetConsoleMode(self._console, ctypes.byref(mode)) else None
        if self._mode is not None:
            self._kernel32.SetConsoleMode(self._console, self._mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        self._write(ENTER)
        return self

    def __exit__(self, *exc: object) -> None:
        self._write(LEAVE)
        if self._mode is not None:
            self._kernel32.SetConsoleMode(self._console, self._mode)

    def read_keys(self, timeout: float) -> list[str]:
        import msvcrt

        deadline = time.monotonic() + timeout
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.02)
        keys: list[str] = []
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in "\x00\xe0":
                scan_code = msvcrt.getwch()
                if char == "\x00" and scan_code in SCAN_CODES:
                    keys.append(SCAN_CODES[scan_code])
            else:
                keys += decode_keys(char)
        return keys


def fullscreen(platform: str = sys.platform) -> _PosixTerminal | _WindowsTerminal:
    """The terminal, to hold in a `with` block: the screen is given back, and
    the keyboard's usual modes restored, however the block ends."""
    return _WindowsTerminal() if platform == "win32" else _PosixTerminal()
