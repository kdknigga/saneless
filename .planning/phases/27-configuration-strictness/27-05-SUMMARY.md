---
phase: 27-configuration-strictness
plan: 05
subsystem: deployment-docs
tags: [docker-compose, bind-mount, ebusy, docs, auto-profiles, title, static-test]
requires:
  - "27-02 (replace_file_atomically EBUSY ConfigError message)"
provides:
  - "docker-compose.yml read-write directory mount ./config:/etc/saneless"
  - "tests/test_deployment_config.py static text contract over compose and docs/**/*.md"
affects:
  - "27-04 (the atomic rewrite path these docs describe)"
  - "Phase 31 DOCS-06 (quick-start relative mount path left for it)"
tech-stack:
  added: []
  patterns:
    - "plain-text doc/compose assertions with file:line failure messages"
key-files:
  created:
    - tests/test_deployment_config.py
  modified:
    - docker-compose.yml
    - docs/how-to/deploy-docker-compose.md
    - docs/reference/docker.md
    - docs/getting-started/quick-start.md
    - docs/how-to/configure-scan-profiles.md
decisions:
  - "The migration section describes the old mount in words (host ./config.toml onto /etc/saneless/config.toml, :ro) rather than the literal mount string, so the static test can forbid the literal everywhere under docs/"
  - "Docker reference marks /etc/saneless as Recommended, not Yes, since a missing config.toml is valid"
  - "Deploy note states the config directory should hold only config.toml and not be writable by untrusted users (T-27-19)"
metrics:
  duration: "~15 min"
  completed: 2026-09-15
  tasks: 2
  files: 6
requirements: [CFG-09, CFG-07, CFG-06, CFG-03]
---

# Phase 27 Plan 05: Config directory mount and profile how-to Summary

The compose example and every Docker doc now mount the config directory read-write (`./config:/etc/saneless`). They explain that a missing `config.toml` means defaults plus environment variables, that a single-file bind mount makes the atomic rename fail with EBUSY, and how to migrate. The profile how-to now describes `auto-profiles --force` as a merge and `title` as a literal default. An 8-test static text suite locks all of this in.

## What was built

- **tests/test_deployment_config.py** (8 tests, plain text, failure messages give file:line):
  - compose has a `- ./config:/etc/saneless` volume line
  - `config.toml:/etc/saneless/config.toml` appears nowhere in compose or `docs/**/*.md`, and the docs glob is not empty
  - no line mentions both `fail to start` and `config`
  - the deploy how-to, Docker reference and quick start show the directory mount
  - the deploy how-to mentions defaults, environment variables, EBUSY, and a `mv config.toml config/` migration step
  - the profile how-to describes the merge, calls `title` a literal default with a `Scan <` fallback, and no longer blames the compose examples for a read-only mount
- **docker-compose.yml**:
  - header bullet and volume comments explain the directory mount, why it must be writable (temp file plus rename; EBUSY on a single-file mount), and that a missing `config.toml` means defaults plus env with no file created
  - the `See:` URL is kept verbatim
  - image, environment and data volume are untouched
- **docs/how-to/deploy-docker-compose.md**:
  - Step 1 runs `mkdir config` and creates `config/config.toml`
  - the warning admonition becomes a note: missing file means defaults plus env and the container still starts; the directory must be writable; keep it private
  - the compose snippet and the Config mount bullet use the directory mount and explain EBUSY
  - new section "Moving from a single-file config mount": quotes the 27-02 EBUSY message, says the CLI exits 2 and the server keeps profiles for that run only, then lists 4 migration steps
- **docs/reference/docker.md**:
  - the volumes row becomes `/etc/saneless` (read-write, missing file allowed, Recommended), followed by an EBUSY paragraph that links the migration section
  - all four compose snippets use `./config:/etc/saneless`
- **docs/getting-started/quick-start.md**:
  - `docker run -v ./config:/etc/saneless` plus a sentence that `config.toml` goes in `./config`
  - the search order names `$XDG_CONFIG_HOME/saneless/config.toml` (default `~/.config/...`)
- **docs/how-to/configure-scan-profiles.md**:
  - `title` row uses the literal-title rule
  - explains the `--force` merge: owned keys, stale-key removal, what is kept, hand edits to owned keys are overwritten, unflagged profiles skipped and reported
  - shows the grouped output lines in a text block
  - new files are created 0600
  - the unwritable-config bullet names read-only and single-file (EBUSY) mounts and links the deploy guide

## Commits

| Task | Gate | Commit | Message |
|------|------|--------|---------|
| 1 | RED | ce8afda | test(27-05): add failing static tests for config directory mount |
| 1 | GREEN | a15c523 | docs(27-05): mount the config directory read-write in compose and Docker docs |
| 2 | RED+GREEN | 21deeee | docs(27-05): describe auto-profiles --force merge and literal profile title |

For Task 2 the plan allows committing the tests together with the prose. The 3 new tests were run first and failed (3 failed, 5 passed) before the doc was edited.

## Verification

- `uv run pytest tests/test_deployment_config.py -q`: 8 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1486 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: clean (pyrefly 0 errors)
- `uv run mkdocs build --strict`: builds with no warnings. The built page has the anchor `moving-from-a-single-file-config-mount`.
- Acceptance greps:
  - no `config.toml:/etc/saneless/config.toml` and no `fail to start` in compose or docs
  - `- ./config:/etc/saneless$` appears exactly once in compose
  - the image, `SANELESS_PAPERLESS__TOKEN=changeme`, and `saneless-data:/var/lib/saneless` lines each appear once
  - `title template` and `as in the Docker Compose examples` are gone
  - `Skipped (not auto-generated)` is present

## Deviations from Plan

**1. [Rule 1 - Bug] Migration section wording adjusted to satisfy the plan's own static test**
- **Found during:** Task 1 GREEN
- **Issue:** The first draft of the migration section quoted the old mount literally (`./config.toml:/etc/saneless/config.toml:ro`). The test forbids that substring anywhere under docs/.
- **Fix:** The section now describes the old mount in words (host `./config.toml` bind-mounted by itself, read-only, onto `/etc/saneless/config.toml`).
- **Files modified:** docs/how-to/deploy-docker-compose.md
- **Commit:** a15c523

The migration text says the server's generated profiles "do not survive a restart". That matches the `worker.py` warning text and is less specific than a first draft that said they would be regenerated at every start.

## Known Stubs

None.

## Threat Flags

None. T-27-18 is mitigated by the directory mount plus the static test. T-27-19 is addressed by the deploy note: keep only `config.toml` in the directory, and do not let untrusted users write to it. T-27-20 is addressed by the 0600 note in the profile how-to.

## Self-Check: PASSED

- FOUND: tests/test_deployment_config.py
- FOUND: docker-compose.yml, docs/how-to/deploy-docker-compose.md, docs/reference/docker.md, docs/getting-started/quick-start.md, docs/how-to/configure-scan-profiles.md
- FOUND commits: ce8afda, a15c523, 21deeee
