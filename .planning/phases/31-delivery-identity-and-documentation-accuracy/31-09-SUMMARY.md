---
phase: 31
plan: 09
status: complete
completed: 2026-09-18
requirements: [DLVR-02]
---

# Plan 31-09 Summary — release rehearsal preparation and tag hand-over

## What happened

All five account-side prerequisites were confirmed in place before any tag existed. Four were
verified by API rather than taken on trust: the `pypi` environment carries one protection rule
(required reviewers), `testpypi` carries zero (it must run unattended), `github-pages` was
auto-created as predicted, and Pages reports `build_type=workflow`. The two pending trusted
publishers are not API-observable; a successful publish was their only possible proof, and they
subsequently provided it.

`pyproject.toml` was set to `0.2.0-rc.1`, then `0.2.0-rc.2` after the first rehearsal failed.
`test_pyproject_declares_the_0_2_0_series` accepted both **unmodified** — the
`^0\.2\.0(-rc\.\d+)?$` regex the plan-checker scrutinised is what made the retry possible without
touching a test.

## The filtered branch (D-55, D-56)

`/gsd-pr-branch` could not be used as written: it assumes `master` is an ancestor of the feature
branch, and here their merge-base is the *initial commit*. Investigation showed `master` is
`.planning`-free **and** `CLAUDE.md`-free — the original filter stripped both, which is why no
re-run reproduced its SHAs.

What worked: `git-filter-repo` removing all of `.planning/` and `CLAUDE.md`, then rebasing the
633-commit phase-21-to-31 range onto the real `master`. Conflicts were resolved toward autodev
(`-X theirs`), autodev being authoritative for the code, and the result was then **verified against
autodev's tree** rather than assumed correct.

That verification earned its place: it caught `-X theirs` having silently moved
`from saneless.spool import SpooledPageSink` into the third-party import block in
`tests/test_spool.py`, which would have failed `ruff check`. Fixed in `ef57a0b`. After the fix the
filtered tree differs from autodev by exactly one line — `.gitignore` ignoring `CLAUDE.md`, which
is correct for that line of development.

PR #9 was opened against `master` and **never merged**.

## Deviations

1. **Two tags, not one.** `v0.2.0-rc.1` failed at `publish-docker`; see 31-10's summary and the
   audit artifact. `0.2.0rc1` was already claimed on TestPyPI by the time it failed, so the retry
   had to be `rc.2`.
2. **Claude created and pushed both tags.** The standing project rule is that Claude never runs
   `git tag`; the user granted explicit one-time authorisation for each of the two tags, and the
   rule resumes. Every tag target was verified first as the filtered head, the exact SHA CI ran
   green on, and carrying the expected version.

## Verification

CI green on both filtered heads (`ef57a0b`, `0002cfc`): `lint`, `test`, `browser`. Locally at each:
3115 tests, ruff, ruff format, ty, pyrefly, zizmor, `mkdocs build --strict` all clean.
