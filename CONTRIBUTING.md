# Contributing to saneless

Thanks for helping out. This document describes how to set up a development
environment and how to get a change through the automated gate on `master`.

## Prerequisites

`python-sane` is an sdist-only package that compiles against `sane/sane.h`, so the
SANE development headers must be installed before the environment can be built. The
package name for each distro is listed in the [System Requirements table in
`README.md`](README.md#system-requirements) -- on Debian and Ubuntu it is
`libsane-dev`.

The project uses `uv` for packaging and environments. Nothing here uses `pip`,
`poetry` or `conda`.

## Setting up

```bash
# Install the SANE headers first (Debian/Ubuntu shown; see README.md for others)
sudo apt-get install libsane-dev

# Build the environment from the committed lockfile
uv sync --locked
```

Use `uv sync --locked` rather than a bare `uv sync`. `--locked` fails if `uv.lock`
does not match `pyproject.toml` instead of quietly re-resolving the dependency graph.
CI runs the same command, so a stale lockfile turns the pull request red rather than
letting your machine and the runner drift apart.

To add a dependency, use `uv add <package>` (or `uv add --dev <package>`) and commit
the resulting `uv.lock` change alongside the `pyproject.toml` change.

## The five checks

Every push to `master` and every pull request runs these five commands, split across
two parallel jobs in `.github/workflows/ci.yml`:

| Job | Command |
|--------|---------|
| `lint` | `uv run ruff check .` |
| `lint` | `uv run ruff format --check .` |
| `lint` | `uv run ty check` |
| `lint` | `uv run pyrefly check` |
| `test` | `uv run pytest -m "not browser"` |

You can reproduce the gate exactly, in the same order, with:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pyrefly check
uv run pytest -m "not browser"
```

All five must exit 0. Fix what they report -- do not silence them. `# noqa`,
`# type: ignore` and rule-disabling are not accepted, and both type checkers must be
clean because they do not always report the same issues for the same code.

The `browser` marker deselects the Playwright tests, which need a real Chromium
install. Run those locally with `uv run pytest -m browser` when you touch the web UI.

A test that hangs is not allowed to hang the run: `pytest-timeout` is configured in
`pyproject.toml` with `timeout = 60` and `timeout_method = "signal"`, so a stuck test
fails on its own with a traceback while the rest of the suite keeps going.

## Local pre-flight

The repository ships a hook configuration. Run it before you push:

```bash
uv run prek run
```

`prek` is a drop-in replacement for the older hook runner -- always invoke it as
`uv run prek run`. It covers the formatter, the linter and both type checkers, but it
does not run the test suite, so run `uv run pytest -m "not browser"` too.

## `--no-verify` no longer skips the gate

The hooks are installed as a convenience, not as the enforcement point. Committing
with `git commit --no-verify` still works, but it no longer gets you anything: the
same checks run again on the pull request, so skipping them locally only moves the
failure later and makes it slower to find.

## Everything goes through a pull request

`master` is protected by a branch ruleset with no bypass actors. That means every
change reaches `master` through a pull request with both required checks green --
including the maintainer's own changes. There is no direct push path.

Dependabot is configured in `.github/dependabot.yml` to bump the SHA-pinned GitHub
Actions weekly. It watches all workflows in the repository, so it will also open pull
requests touching `release.yml` and `docs.yml`. That is expected behaviour rather
than scope creep, and those pull requests go through the same gate as any other.
