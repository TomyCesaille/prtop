"""Draw docs/public/how-to-read-prtop.png: prtop's live view, annotated.

The output in the picture is prtop's own: the demo's live view just after a
fetch that changed two rows, so the picture follows the code. The annotations are
laid out in HTML over that output, and headless Chrome renders it.

    uv run --no-project --with pillow python docs/infographic/how_to_read_prtop.py

Chrome is found at its macOS location unless CHROME names another binary.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from prtop.demo import demo_story
from prtop.live import NOT_REVERSE, REVERSE, Outcome, View
from prtop.report import DEPLOY_SIGNAL, display_width, plain

OUT = ROOT / "docs" / "public" / "how-to-read-prtop.png"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LINE_HEIGHT = 20  # px per line of output
PAD_TOP = 14  # the terminal's padding above its first line
GUTTER = 52  # room left of the output for the block brackets

LINK = re.compile("\033\\]8;;[^\a]*\a(.*?)\033\\]8;;\a")
DEPLOY_PREFIXES = tuple(template.split("{")[0] for _, template in DEPLOY_SIGNAL.values())


def live_view() -> list[str]:
    """The demo's live view after its first change, with the top line's times fixed."""
    fetch = demo_story(pause=lambda seconds: None)
    first, second = fetch(), fetch()
    view = View("gitlab.example.com")
    view.take(Outcome(first, []), datetime(2026, 10, 23, 14, 4, 10))
    view.take(Outcome(second, []), datetime(2026, 10, 23, 14, 4, 40))
    view.take(Outcome(second, []), datetime(2026, 10, 23, 14, 5, 12))
    table = view.table(1000)
    width = max(display_width(unhighlighted(line)) for line in table)
    # The top line and a blank, the table, a blank and the key bar.
    return view.frame(width, 2 + len(table) + 2, False, 23.0)


def unhighlighted(line: str) -> str:
    return line.replace(REVERSE, "").replace(NOT_REVERSE, "")


def cells(text: str) -> str:
    # An emoji takes two terminal cells; the browser is held to the same.
    return "".join(
        f'<span class="emo">{html.escape(c)}</span>' if unicodedata.east_asian_width(c) in ("W", "F") else html.escape(c)
        for c in text
    )


def line_html(line: str) -> str:
    text, out, pos = unhighlighted(line), [], 0
    for m in LINK.finditer(text):
        out += [cells(text[pos:m.start()]), f'<span class="link">{cells(m.group(1))}</span>']
        pos = m.end()
    out.append(cells(text[pos:]))
    return "".join(out) or "&nbsp;"


def top(line: int) -> int:
    return PAD_TOP + line * LINE_HEIGHT


def badge(n: int, line: int, column: float) -> str:
    """A numbered callout centred on a character column of the output."""
    return f'<span class="badge" style="top:{top(line) - 1}px;left:calc({GUTTER}px + {column}ch - 11px)">{n}</span>'


def bracket(first: int, last: int, n: int) -> str:
    height = (last - first + 1) * LINE_HEIGHT - 4
    return (f'<div class="bracket" style="top:{top(first) + 2}px;height:{height}px">'
            f'<span class="badge side">{n}</span></div>')


NOTES = [
    ("Mark", "whose move it is"),
    ("Links", "the MR, its task, the prod trigger"),
    ("Highlighted", "changed at the last fetch"),
    ("Open", "sorted by mark"),
    ("Merged", "until production runs them"),
]
MARKS = [
    ("!", "On you", ["mergeable", "rebase", "conflicts", "own threads", "failed pipeline", "prod trigger"]),
    ("?", "To look at", ["changes requested", "open threads", "unknown status"]),
    ("~", "Someone else", ["awaiting review", "draft", "blocked"]),
    (".", "Running", ["CI", "merge check", "deploying"]),
]

CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root { --mono: "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace;
          --sans: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  body { font-family: var(--sans); padding: 40px 44px 44px; background: #0f1115; color: #e8eaed; }
  /* prtop's output sets the width; everything else wraps under it. */
  .sheet { width: min-content; }
  .term { position: relative; width: max-content; background: #000; color: #f2f2f2; border: 1px solid #3a3f47;
          border-radius: 10px; padding: 14px 44px 14px 52px; font-family: var(--mono); font-size: 13px; }
  .ln { height: 20px; line-height: 20px; white-space: pre; width: max-content; }
  .ln.hl { background: #d9d9d9; color: #000; }
  .emo { display: inline-block; width: 2ch; font-size: 12px; }
  .link { text-decoration: underline dashed #8a8a8a; text-underline-offset: 3px; }
  .ln.hl .link { text-decoration-color: #666; }
  /* The annotations are yellow, a colour prtop's black-and-white output never uses. */
  .badge { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 50%;
           background: #F0E442; color: #111; font: 800 13px/1 var(--sans); flex: none; box-shadow: 0 0 0 2px #000; }
  /* In the terminal's font, so that a callout's ch offsets land on the output's columns. */
  .term > .badge { position: absolute; font-family: var(--mono); }
  .bracket { position: absolute; left: 18px; width: 12px; border: 2px solid #F0E442; border-right: none; border-radius: 6px 0 0 6px; }
  .bracket .side { position: absolute; left: -18px; top: 50%; transform: translateY(-50%); }
  .notes { list-style: none; display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: repeat(3, auto);
           grid-auto-flow: column; gap: 10px 40px; margin-top: 22px; }
  .notes li { display: grid; grid-template-columns: 22px 104px 1fr; gap: 12px; align-items: center; font-size: 15.5px; }
  .notes span:last-child { color: #9aa0a6; }
  .marks { margin-top: 26px; }
  .marks-title { display: flex; align-items: center; gap: 12px; font-size: 18px; margin-bottom: 12px; }
  .mark-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .mark { display: grid; grid-template-columns: 44px 1fr; grid-template-rows: auto 1fr; column-gap: 12px; align-items: start;
          background: var(--fill); color: var(--ink); border: 2px solid var(--edge); border-radius: 8px; padding: 12px 16px 12px 10px; }
  .mark .glyph { grid-row: span 2; align-self: center; font-family: var(--mono); font-size: 36px; font-weight: 800; line-height: 1; text-align: center; }
  .mark b { font-size: 17px; }
  .mark .ex { color: var(--soft); font-size: 15px; line-height: 1.45; margin-top: 2px; }
  .mark .item { white-space: nowrap; }
  /* Black and white like prtop: brightness falls with the marks' weight, `!` heaviest, `.` lightest. */
  .k0 { --fill: #f2f2f2; --edge: #f2f2f2; --ink: #0f1115; --soft: #3a3d42; }
  .k1 { --fill: #a3a7ad; --edge: #a3a7ad; --ink: #0f1115; --soft: #1f2226; }
  .k2 { --fill: #3a3f47; --edge: #3a3f47; --ink: #f0f0f0; --soft: #c8ccd2; }
  .k3 { --fill: transparent; --edge: #5a5f67; --ink: #e8eaed; --soft: #9aa0a6; border-style: dashed; }
"""


def page() -> str:
    lines = live_view()
    texts = [plain(unhighlighted(line)) for line in lines]
    header = next(i for i, t in enumerate(texts) if t.startswith("|   | PR title"))
    # Each column starts after its "| ", searched left to right: "PR" would
    # otherwise match the start of "PR title".
    columns, start = {}, 0
    for name in ("PR title", "Task", "PR", "Status"):
        columns[name] = texts[header].index(f"| {name} ", start) + 2
        start = columns[name]
    rows = [i for i, t in enumerate(texts) if re.match(r"\| [!?~.] \|", t)]
    merged = [i for i in rows if texts[i].split("|")[-2].strip().startswith(DEPLOY_PREFIXES)]
    opened = [i for i in rows if i not in merged]
    first_highlighted = next(i for i, line in enumerate(lines) if line.startswith(REVERSE))
    width = display_width(texts[header])

    # Numbered in reading order, top to bottom.
    callouts = "".join([
        badge(1, header, 2.5),
        badge(2, header, columns["PR"] + 4),
        badge(3, first_highlighted, width + 1.4),
        bracket(min(opened), max(opened), 4),
        bracket(min(merged), max(merged), 5),
    ])
    output = "".join(
        f'<div class="ln{" hl" if line.startswith(REVERSE) else ""}">{line_html(line)}</div>' for line in lines
    )
    notes = "".join(
        f'<li><span class="badge">{n}</span><b>{label}</b><span>{text}</span></li>'
        for n, (label, text) in enumerate(NOTES, 1)
    )
    marks = "".join(
        f'<div class="mark k{i}"><span class="glyph">{glyph}</span><b>{name}</b>'
        f'<span class="ex">{" · ".join(f"<span class=item>{x}</span>" for x in examples)}</span></div>'
        for i, (glyph, name, examples) in enumerate(MARKS)
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>prtop, annotated</title><style>{CSS}</style></head>
<body><main class="sheet">
  <div class="term">{output}{callouts}</div>
  <ol class="notes">{notes}</ol>
  <section class="marks">
    <h2 class="marks-title"><span class="badge">1</span>The marks</h2>
    <div class="mark-grid">{marks}</div>
  </section>
</main></body></html>
"""


def render(source: Path, out: Path) -> None:
    """A 2x screenshot, cropped to the page and a margin as wide as its padding."""
    from PIL import Image

    shot = source.with_suffix(".shot.png")
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
         "--window-size=1500,1800", f"--screenshot={shot}", source.as_uri()],
        check=True, capture_output=True,
    )
    im = Image.open(shot).convert("RGB")
    background = im.getpixel((5, 5))
    w, h = im.size
    bottom = max(y for y in range(h) if any(im.getpixel((x, y)) != background for x in range(0, w, 4)))
    right = max(x for x in range(w) if any(im.getpixel((x, y)) != background for y in range(0, bottom, 4)))
    im.crop((0, 0, min(w, right + 88), min(h, bottom + 80))).save(out, optimize=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "how-to-read-prtop.html"
        source.write_text(page(), encoding="utf-8")
        render(source, OUT)
    print(OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
