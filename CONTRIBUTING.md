# Contributing

Bug reports and pull requests are welcome. [AGENTS.md](AGENTS.md) describes the
layout and the rules the code holds to; read it before a change of any size.

## Set up

prtop needs Python 3.14 or later and nothing else to run its tests.

```bash
git clone https://github.com/TomyCesaille/prtop
cd prtop
uv tool install --editable .   # `prtop` now runs this checkout
```

Without uv: `python3 -m venv .venv`, then `.venv/bin/pip install --editable .`.

## Check a change

```bash
python -m unittest
ruff check
```

CI runs the tests on Linux, macOS and Windows with Python 3.14 and 3.15, and lints
with ruff 0.17.2.

To see a change on screen, `prtop --demo` needs no GitLab. Against a real instance,
use a scratch state file, so the run does not move your own merge-detection
baseline:

```bash
PRTOP_STATE_FILE=/tmp/prtop-scratch.json prtop
```

## Pull requests

- One change per pull request, with tests for the behaviour it adds or fixes.
- Name tests after what the user sees: `test_a_rebase_is_on_you`, not
  `test_status_signal_returns_act`.
- Title commits and pull requests with Conventional Commits: `feat:`, `fix:`,
  `refactor:`, `docs:`, `chore:`.
- Note user-facing changes under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).

## Common changes

**A new status.** Choose its mark first: whose move is it? Then a label that stands
on its own, then a glyph that is one code point, two cells wide and not used
elsewhere. Map it in `prtop/report.py`, and in the forge that produces it. The
accessibility tests fail until all three channels agree.

**A new task source.** Add it to `prtop/tasks.py`, either as a branch preset in
`_PRESETS` or as a class with `shows_column` and `resolve`, and to the allowed
values of `PRTOP_TASK_SOURCE` in `prtop/config.py`.

**A new forge.** Implement the `Forge` protocol of `prtop/forges/__init__.py` in a
module of that package, map the service's states onto `prtop.model`, register it
in `make_forge` and in the allowed values of `PRTOP_FORGE`. A forge stays
read-only.

## Releasing

1. Set the version in `prtop/__init__.py`, and move the `Unreleased` entries of
   CHANGELOG.md under a heading for it.
2. Once CI passes on that commit, tag it `v<version>`, push the tag, and publish
   a GitHub release with the changelog section as its notes.
3. The tag runs the Chocolatey workflow: it builds the package around its own
   Python, installs and runs it on Windows, and pushes it to
   community.chocolatey.org.
4. In [TomyCesaille/homebrew-tap](https://github.com/TomyCesaille/homebrew-tap),
   point the formula at the new tag's archive and its SHA256 in a pull request;
   the tap's CI builds it on macOS and Linux, and its `brew pr-pull` workflow
   merges it with the bottles.

The Python inside the Chocolatey package is pinned, version and SHA256, in
`packaging/chocolatey/build.py`. A security release of Python means raising it
there and releasing a new package.
