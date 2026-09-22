# Git hooks

## Why these exist

This is graded academic work. Commits must carry your authorship only.

Claude Code disables commit attribution through a settings flag, but that flag
has proven unreliable in practice: it has been reported as bypassed when a
commit message is composed directly through the shell, and a newer
`Claude-Session:` trailer ignored the attribution config entirely. The flag is
also mid-migration — `includeCoAuthoredBy` was deprecated in v2.0.62 in favour
of `attribution`.

A `commit-msg` hook runs on every commit no matter how it was created, so it is
the layer that actually holds. `.claude/settings.json` is the polite request;
this hook is the enforcement.

## Install

```bash
git config core.hooksPath githooks
```

Run this once per clone. `make hooks` does it for you.

## What it strips

- `Co-Authored-By: Claude ...` and any `@anthropic.com` co-author
- `Generated with [Claude Code]` lines
- `Claude-Session:` trailers
- The robot emoji line
- Blank lines the removals leave behind

Human co-authors are preserved. A commit co-authored with a teammate keeps that
trailer.

## Verify it works

```bash
make check-attribution
```

Scans the whole history. If you have already pushed tainted commits, see below.

## Already committed with attribution?

Rewriting published history is disruptive; do it before anyone else clones.

```bash
git filter-branch -f --msg-filter githooks/commit-msg-filter -- --all
```

Or, if the repo is small and unpushed, squash and recommit. Check with
`make check-attribution` afterwards either way.
