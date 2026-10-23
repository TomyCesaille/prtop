# prtop with Claude Code

Two pieces, for [Claude Code](https://github.com/anthropics/claude-code) with
prtop installed and pointed at your instance:

- [`commands/prs.md`](commands/prs.md): a `/prs` command. Claude runs `prtop` once,
  reads the marks, and offers to act on what is on you: merge, rebase, answer a
  review. It asks before anything that changes GitLab or GitHub.
- [`CLAUDE.md`](CLAUDE.md): instructions for watching the merge requests or pull
  requests of the task at hand in the background with `prtop watch`, so Claude
  hears about an approval, a needed rebase or a deploy while it works on
  something else.

To use them, copy `commands/prs.md` into `~/.claude/commands/` (or a project's
`.claude/commands/`), and paste the section of `CLAUDE.md` into your own
`~/.claude/CLAUDE.md`. Or let Claude do it all, prtop's installation included:
[`SETUP.md`](SETUP.md) walks it through, and the README gives the one line to
paste.
