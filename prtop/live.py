"""The live view: the report on a full screen, fetched again every few seconds
and redrawn in place, the way `htop` shows processes.

The screen is a status line, the table and a key bar. A row that changed at the
latest fetch that changed anything stays in reverse video until the next such
fetch: it stands out by brightness, which survives any colour scheme.
"""

from __future__ import annotations

import io
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple, Protocol

from prtop.model import ForgeError
from prtop.report import (
    NOTHING_OPEN,
    TASK_MAX,
    TITLE_MAX,
    TITLE_MAX_ALONE,
    Report,
    clip,
    display_width,
    plain,
    render_report,
)

DEFAULT_DELAY = 30
# How long a key may wait to be read, and so how often the countdown and the
# window size are looked at.
TICK = 0.2
# The narrowest the title and task columns get; past that, lines are cut at the
# window edge.
NARROWEST = 16
MESSAGE_LINES = 3

REVERSE, NOT_REVERSE = "\033[7m", "\033[27m"

QUIT_KEYS = ("q", "F10", "Ctrl+C")
HELP_KEYS = ("h", "?", "F1")
PAUSE_KEYS = ("Z", "z")

HELP = """\
Keys
  q, F10, Ctrl+C   quit
  h, ?, F1         show or hide this help
  Ctrl+L           fetch now
  Z                pause or resume fetching

Marks
  !   on you now
  ?   on you to look at or reply to
  ~   on someone else, until a person moves
  .   on a machine, it resolves by itself

Highlighted rows changed at the latest fetch that changed anything.""".splitlines()


class Terminal(Protocol):
    def size(self) -> tuple[int, int]:
        """(columns, lines)"""
        ...

    def read_keys(self, timeout: float) -> list[str]:
        """The keys pressed within `timeout` seconds, named the way
        `prtop.terminal.decode_keys` names them."""
        ...

    def draw(self, lines: list[str]) -> None:
        """Show `lines` from the top of the screen in place of the last frame."""
        ...

    def redraw(self) -> None:
        """Clear the screen, so the next `draw` paints it whole."""
        ...


class Messages(io.TextIOBase):
    """Stands in for stderr while the view holds the screen: what a fetch
    prints there goes to the status area instead of scrolling the screen."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def take(self) -> list[str]:
        with self._lock:
            text, self._parts = "".join(self._parts), []
        return [line for line in text.splitlines() if line.strip()]


class Outcome(NamedTuple):
    # None when the forge did not answer; the messages then say why.
    report: Report | None
    messages: list[str]


class Fetcher:
    """Runs one fetch at a time off the main thread, so a key is answered while
    the forge takes its time. A failure other than the forge's surfaces in the
    main thread, from `poll`."""

    def __init__(
        self,
        fetch: Callable[[], Report],
        take_messages: Callable[[], list[str]] = list,
        submit: Callable[[Callable[[], Outcome]], Future[Outcome]] | None = None,
    ):
        self._fetch = fetch
        self._take_messages = take_messages
        self._submit = submit or ThreadPoolExecutor(max_workers=1).submit
        self._future: Future[Outcome] | None = None

    @property
    def busy(self) -> bool:
        return self._future is not None

    def start(self) -> None:
        self._future = self._submit(self._attempt)

    def _attempt(self) -> Outcome:
        try:
            report = self._fetch()
        except ForgeError as e:
            return Outcome(None, [*self._take_messages(), *f"prtop: {e}".splitlines()])
        return Outcome(report, self._take_messages())

    def poll(self) -> Outcome | None:
        if self._future is None or not self._future.done():
            return None
        future, self._future = self._future, None
        return future.result()


def fit(report: Report, columns: int) -> list[str]:
    """The table narrowed to `columns`: the task column gives way first, then
    the title, and lines are cut at the window edge once both are at their
    narrowest."""
    body = [row for row in report.rows if row is not None]
    title_max, task_max = TITLE_MAX if report.show_task else TITLE_MAX_ALONE, TASK_MAX
    while True:
        lines = render_report(
            report.rows, report.show_task, title_max=title_max, task_max=task_max
        ).splitlines()
        excess = max(map(display_width, lines)) - columns
        if excess <= 0:
            return lines
        if report.show_task and task_max > NARROWEST:
            task_max = max(NARROWEST, min(task_max, max(len(r.task) for r in body)) - excess)
        elif title_max > NARROWEST:
            title_max = max(NARROWEST, min(title_max, max(len(r.title) for r in body)) - excess)
        else:
            return [clip(line, columns) for line in lines]


def _signatures(report: Report) -> dict[str, tuple[str, ...]]:
    return {
        plain(row.pr): (row.mark, row.title, row.task, plain(row.status))
        for row in report.rows
        if row is not None
    }


def _duration(seconds: float) -> str:
    whole = max(0, math.ceil(seconds))
    return f"{whole}s" if whole < 60 else f"{whole // 60}m {whole % 60:02d}s"


@dataclass
class View:
    title: str
    report: Report | None = None
    messages: list[str] = field(default_factory=list)
    fetched_at: datetime | None = None
    # The PR cells of the rows that changed at the latest fetch that changed anything.
    changed: frozenset[str] = frozenset()
    changed_at: datetime | None = None
    help: bool = False
    paused: bool = False
    # Set when the fetch limit ended the run, rather than a key.
    finished: bool = False

    def take(self, outcome: Outcome, now: datetime) -> None:
        self.messages = outcome.messages
        if outcome.report is None:
            return
        if self.report is not None:
            changed = _signatures(outcome.report).items() - _signatures(self.report).items()
            if changed:
                self.changed = frozenset(key for key, _ in changed)
                self.changed_at = now
        self.report, self.fetched_at = outcome.report, now

    def status_line(self, fetching: bool, seconds_left: float) -> str:
        parts = [self.title]
        if self.fetched_at is not None:
            parts.append(f"fetched {self.fetched_at:%H:%M:%S}")
        if fetching:
            parts.append("fetching…")
        elif self.paused:
            parts.append("paused")
        else:
            parts.append(f"next in {_duration(seconds_left)}")
        if self.changed_at is not None:
            parts.append(f"last change {self.changed_at:%H:%M:%S}")
        return " · ".join(parts)

    def key_bar(self) -> str:
        if self.help:
            return "h or Esc  back to the table   q  quit"
        pause = "resume" if self.paused else "pause"
        return f"q  quit   h  help   Ctrl+L  fetch now   Z  {pause}"

    def table(self, columns: int) -> list[str]:
        if self.report is None:
            return []
        if not self.report.rows:
            return [clip(NOTHING_OPEN, columns)]
        # Two lines of header, then one line per row, a rule standing for `None`.
        rows = [None, None, *self.report.rows]
        return [
            f"{REVERSE}{line}{NOT_REVERSE}" if row is not None and plain(row.pr) in self.changed else line
            for row, line in zip(rows, fit(self.report, columns))
        ]

    def frame(self, columns: int, lines: int, fetching: bool, seconds_left: float) -> list[str]:
        top = [self.status_line(fetching, seconds_left), *self.messages[-MESSAGE_LINES:], ""]
        body = [clip(line, columns) for line in HELP] if self.help else self.table(columns)
        room = max(0, lines - len(top) - 1)
        if len(body) > room:
            hidden = len(body) - room + 1
            more = f"… {hidden} more lines below; enlarge the window to see them"
            body = [*body[: room - 1], more] if room else []
        filler = [""] * (room - len(body))
        return [*(clip(line, columns) for line in top), *body, *filler, clip(self.key_bar(), columns)]


def run(
    fetcher: Fetcher,
    terminal: Terminal,
    title: str,
    delay: float,
    max_iterations: int | None = None,
    *,
    on_report: Callable[[Report], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = datetime.now,
) -> View:
    """Fetch and redraw until `q`, or until `max_iterations` fetches have come
    back. A fetch the forge fails keeps the last table on screen, with the
    error in the status area."""
    view = View(title)
    next_fetch, fetches, fetch_now = clock(), 0, False
    while True:
        outcome = fetcher.poll()
        if outcome is not None:
            view.take(outcome, now())
            if outcome.report is not None and on_report is not None:
                on_report(outcome.report)
            fetches += 1
            if max_iterations is not None and fetches >= max_iterations:
                view.finished = True
                return view
            next_fetch = clock() + delay
        if not fetcher.busy and (fetch_now or (not view.paused and clock() >= next_fetch)):
            fetch_now = False
            fetcher.start()

        columns, lines = terminal.size()
        terminal.draw(view.frame(columns, lines, fetcher.busy, next_fetch - clock()))

        for key in terminal.read_keys(TICK):
            if key in QUIT_KEYS:
                return view
            if key in HELP_KEYS:
                view.help = not view.help
            elif key == "Esc":
                view.help = False
            elif key in PAUSE_KEYS:
                view.paused = not view.paused
            elif key == "Ctrl+L":
                terminal.redraw()
                fetch_now = not fetcher.busy
