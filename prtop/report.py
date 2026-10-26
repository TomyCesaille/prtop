"""The report: one table, where every state is encoded four times over.

1. **Severity mark**: plain ASCII, one cell wide, legible regardless of color,
   glyph rendering or font size. The channel that must never regress.
2. **Position**: rows sort by mark, and each severity block is ruled off.
3. **Text label**: the state in plain words, unambiguous on its own.
4. **Glyph**: redundant garnish, never the only carrier of a distinction.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from itertools import accumulate
from typing import NamedTuple

from prtop import model
from prtop.model import DeployState

# The four marks read as a weight gradient — `!` is the heaviest shape, `.` the
# lightest — so the eye lands on the top of the table without resolving them.
ACT = "!"      # on you now
ANSWER = "?"   # on you to look at or reply to
PEOPLE = "~"   # on someone else, and it will sit there until a person moves
RUNNING = "."  # on a machine, and it resolves by itself

MARK_ORDER = {ACT: 0, ANSWER: 1, PEOPLE: 2, RUNNING: 3}

# Not a forge state: `fmt_status` substitutes it when every unresolved thread is
# one you opened and nobody answered.
OWN_THREADS_UNRESOLVED = "own_threads_unresolved"

# A glyph never carries two different meanings across the open-MR and
# awaiting-deploy halves of the shared Status column — ⏳ spans both only because
# a pipeline in flight is one state — and silhouettes are picked to stay
# distinguishable when a terminal drops the color and renders monochrome outlines.
# Each is one code point that every terminal draws two cells wide: one built on
# a variation selector (`⚠️`) is one cell in some terminals, two in others, and
# no width count can keep the right border straight in both.
#
# Labels stay short enough to leave the Task column the width it needs; keep a
# new one inside `STATUS_WIDTH`.
REVIEW_SIGNAL = {
    model.DRAFT:               (PEOPLE, "📝", "draft"),
    model.AWAITING_REVIEW:     (PEOPLE, "👀", "awaiting review"),
    model.REBASE_NEEDED:       (ACT, "🔁", "rebase needed"),
    model.CONFLICTS:           (ACT, "💥", "conflicts"),
    model.CI_RUNNING:          (RUNNING, "⏳", "CI running"),
    model.CI_MUST_PASS:        (RUNNING, "⏳", "CI must pass"),
    model.MERGE_CHECK_RUNNING: (RUNNING, "🔍", "merge check running"),
    model.MERGE_CHECK_PENDING: (RUNNING, "🔍", "merge check pending"),
    model.UNRESOLVED_THREADS:  (ANSWER, "💬", "unresolved threads"),
    OWN_THREADS_UNRESOLVED:    (ACT, "🧵", "resolve own threads"),
    model.BLOCKED:             (PEOPLE, "🚧", "blocked"),
    model.CHANGES_REQUESTED:   (ANSWER, "⛔", "changes requested"),
}

# These two already name the approval state; the suffix would repeat it.
_APPROVAL_IMPLIED = (model.DRAFT, model.AWAITING_REVIEW)

DEPLOY_SIGNAL = {
    model.PIPELINE_RUNNING: (RUNNING, "⏳ pipeline {detail}"),
    model.PIPELINE_FAILED:  (ACT, "❌ pipeline failed"),
    model.PROD_FAILED:      (ACT, "❌ prod deploy failed"),
    model.PROD_DEPLOYING:   (RUNNING, "📦 prod deploying"),
    model.AWAITING_TRIGGER: (ACT, "🚀 awaiting prod trigger"),
    model.PROD_OTHER:       (ANSWER, "➖ prod {detail}"),
    model.CHECK_FAILED:     (ANSWER, "🔌 check failed: {detail}"),
}


def status_signal(status: str, fully_approved: bool) -> tuple[str, str, str]:
    # An unmapped status is a gap in this table, not a quiet state: surface it at
    # `?`, under its raw name, so a state the forge adds later can't sit
    # unnoticed at the bottom.
    if status == model.MERGEABLE:
        return (ACT, "✅", "mergeable") if fully_approved else (PEOPLE, "👀", "mergeable")
    return REVIEW_SIGNAL.get(status, (ANSWER, "❔", status))


def fmt_status(
    status: str,
    fully_approved: bool,
    has_conflicts: bool,
    threads_await_user: bool = True,
) -> tuple[str, str]:
    """(severity mark, Status cell) for an open MR."""
    # A forge can report conflicts independently of the review state, which may
    # still read "awaiting review". Conflicts win: they are on you whatever the
    # review state says.
    if has_conflicts:
        status = model.CONFLICTS
    elif status == model.UNRESOLVED_THREADS and not threads_await_user:
        # A thread you opened and nobody answered is a hint you left for the
        # reviewer. It blocks the merge once the MR is approved; before that,
        # the review is what the MR actually waits on.
        status = OWN_THREADS_UNRESOLVED if fully_approved else model.AWAITING_REVIEW
    mark, glyph, label = status_signal(status, fully_approved)
    if status not in _APPROVAL_IMPLIED:
        label += ", approved" if fully_approved else ", unapproved"
    return mark, f"{glyph} {label}"


def deploy_signal(state: DeployState) -> tuple[str, str]:
    """(severity mark, Status cell) for an MR that is merged but not yet in production."""
    mark, template = DEPLOY_SIGNAL[state.kind]
    cell = template.format(detail=state.detail)
    return mark, hyperlink(cell, state.url) if state.url else cell


# ---------- Cells ----------

_OSC_RE = re.compile("\033\\][^\a]*\a")


def hyperlink(text: str, url: str) -> str:
    """OSC 8 terminal hyperlink: renders as clickable `text` with the URL
    invisible — unlike a markdown link, which the terminal shows raw."""
    return f"\033]8;;{url}\a{text}\033]8;;\a"


def plain(text: str) -> str:
    """A cell stripped of its hyperlink escapes — the text the terminal shows."""
    return _OSC_RE.sub("", text)


def _cells(char: str) -> int:
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Terminal cell width: OSC escape sequences (hyperlinks) occupy no cells,
    and East-Asian-wide chars (incl. emoji) count as 2 so the emoji-bearing
    Status column stays aligned. `str.ljust` can't do either — it counts a
    single-codepoint emoji as 1 while the terminal renders it 2 cells wide."""
    return sum(map(_cells, plain(text)))


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


_LINK_END = "\033]8;;\a"
_CELL_RE = re.compile(f"{_OSC_RE.pattern}|.", re.DOTALL)


def clip(text: str, width: int) -> str:
    """`text` cut to `width` terminal cells, ending on `…` when cut, with a
    hyperlink the cut falls inside closed again."""
    if display_width(text) <= width:
        return text
    tokens = _CELL_RE.findall(text)
    # Cells used up to and including each token, a hyperlink escape taking none.
    # The count only grows, so the tokens kept are the start of the line.
    used = accumulate(0 if token.startswith("\033]") else _cells(token) for token in tokens)
    kept = [token for token, cells in zip(tokens, used) if cells <= width - 1]
    links = [token for token in kept if token.startswith("\033]")]
    return "".join(kept) + "…" + (_LINK_END if links and links[-1] != _LINK_END else "")


def linked_mr_id(entry: dict, mr_id: str) -> str:
    """The `repo!iid` cell, clickable through to the MR page."""
    cell = f"`{mr_id}`"
    url = entry.get("web_url")
    return hyperlink(cell, url) if url else cell


# ---------- Rows ----------

class Row(NamedTuple):
    mark: str
    title: str
    task: str
    pr: str
    status: str
    task_url: str | None = None


def open_row(mr_id: str, entry: dict) -> Row:
    mark, status = fmt_status(
        entry["status"], entry["fully_approved"], entry["has_conflicts"],
        entry["threads_await_user"],
    )
    task = entry.get("task") or "—"
    return Row(mark, entry["title"], task, linked_mr_id(entry, mr_id), status, entry.get("task_url"))


def deploy_row(mr_id: str, entry: dict, state: DeployState) -> Row:
    mark, status = deploy_signal(state)
    task = entry.get("task") or "—"
    return Row(mark, entry.get("title", ""), task, linked_mr_id(entry, mr_id), status, entry.get("task_url"))


def unreachable_row(host: str) -> Row:
    """The row standing in for the open MRs when the forge did not answer.

    Marked `!` rather than `?`: the run has nothing to say about those MRs, and
    the mark feeds the tab color, where green would claim "all clear" — a claim
    the run cannot make about MRs it never saw.
    """
    # The PR cell links to the instance, which tells two forges' outages apart.
    return Row(ACT, host, "—", hyperlink("—", f"https://{host}"), "🔌 instance unreachable")


def _sort_key(row: Row) -> tuple[int, str, str, int]:
    """Severity first, then task, repo and MR number — so the several MRs of one
    task stay adjacent inside a severity block, in repo then numeric order."""
    repo, _, iid = plain(row.pr).strip("`").partition("!")
    # An unreachable-instance row carries no MR number.
    return MARK_ORDER.get(row.mark, 9), row.task, repo, int(iid) if iid.isdigit() else 0


def by_severity(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=_sort_key)


def split_by_mark(rows: list[Row]) -> list[Row | None]:
    """Rule off each severity block so the eye can land on one block at a time.
    Expects `by_severity` order, where equal marks are already adjacent."""
    out: list[Row | None] = []
    for i, row in enumerate(rows):
        if i and row.mark != rows[i - 1].mark:
            out.append(None)
        out.append(row)
    return out


def assemble_rows(open_rows: list[Row], deploy_rows: list[Row]) -> list[Row | None]:
    """Open MRs on top, merged-but-not-yet-deployed below, with a rule between the
    two groups and another between each severity block inside them."""
    rows = split_by_mark(open_rows)
    if open_rows and deploy_rows:
        rows.append(None)
    return rows + split_by_mark(deploy_rows)


# ---------- Table ----------

TITLE_MAX = 42
TASK_MAX = 58
# Without a Task column the title takes its width, so the table stays as wide.
TITLE_MAX_ALONE = TITLE_MAX + len(" | ") + TASK_MAX

# The widest Status cell the vocabulary can produce
# (`🔍 merge check running, unapproved`). Flooring the column there holds the
# table at one width as MRs move between states, instead of reflowing the whole
# thing on every check. A cell carrying text no vocabulary bounds — an unmapped
# status, a `check failed:` message — still widens the column.
STATUS_WIDTH = 34

HEADERS = ("", "PR title", "Task", "PR", "Status")
MIN_WIDTHS = (0, 0, 0, 0, STATUS_WIDTH)
HEADERS_WITHOUT_TASK = ("", "PR title", "PR", "Status")
MIN_WIDTHS_WITHOUT_TASK = (0, 0, 0, STATUS_WIDTH)


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[str] | None], min_widths: Sequence[int] = ()
) -> str:
    """`None` in `rows` renders as a horizontal rule, separating row groups.
    `min_widths` floors a column so its width survives a change of content."""
    body = [r for r in rows if r is not None]

    def width(i: int, header: str) -> int:
        floor = min_widths[i] if i < len(min_widths) else 0
        return max([floor, display_width(header)] + [display_width(r[i]) for r in body])

    widths = [width(i, h) for i, h in enumerate(headers)]

    def pad(cell: str, w: int) -> str:
        return cell + " " * (w - display_width(cell))

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(pad(c, w) for c, w in zip(cells, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(headers), sep, *(sep if r is None else line(r) for r in rows)])


def _task_cell(row: Row, task_max: int) -> str:
    label = truncate(row.task, task_max)
    return hyperlink(label, row.task_url) if row.task_url else label


def render_report(
    rows: Sequence[Row | None],
    show_task: bool = True,
    *,
    title_max: int | None = None,
    task_max: int = TASK_MAX,
) -> str:
    """`title_max` and `task_max` cap the two free-text columns, which the other
    columns' fixed widths leave as the only ones to narrow."""
    if not show_task:
        title_max = title_max or TITLE_MAX_ALONE
        cells = [
            None if r is None else (r.mark, truncate(r.title, title_max), r.pr, r.status)
            for r in rows
        ]
        return render_table(HEADERS_WITHOUT_TASK, cells, MIN_WIDTHS_WITHOUT_TASK)
    title_max = title_max or TITLE_MAX
    cells = [
        None if r is None else (r.mark, truncate(r.title, title_max), _task_cell(r, task_max), r.pr, r.status)
        for r in rows
    ]
    return render_table(HEADERS, cells, MIN_WIDTHS)


NOTHING_OPEN = "No open merge requests or pending deploys."


class Report(NamedTuple):
    rows: list[Row | None]
    show_task: bool
    # Whether an open MR is on you now. Deployment progress never sets it.
    action_needed: bool

    @property
    def text(self) -> str:
        return render_report(self.rows, self.show_task) if self.rows else NOTHING_OPEN
