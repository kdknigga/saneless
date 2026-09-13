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

# Install the git hooks (run this from the main checkout, not from a worktree)
uv run prek install --git-dir "$(git rev-parse --path-format=absolute --git-common-dir)"
```

Use `uv sync --locked` rather than a bare `uv sync`. `--locked` fails if `uv.lock`
does not match `pyproject.toml` instead of quietly re-resolving the dependency graph.
CI runs the same command, so a stale lockfile turns the pull request red rather than
letting your machine and the runner drift apart.

The last command installs three hooks, `pre-commit`, `pre-merge-commit` and
`pre-push`, which are listed under `default_install_hook_types` in
`.pre-commit-config.yaml`. The `--git-dir` form is needed because prek 0.3.6 refuses
to install when `core.hooksPath` is set locally, which is the case in a clone that
shares its hooks with its git worktrees.

**If you installed the hooks before `pre-merge-commit` and `pre-push` were added to
`default_install_hook_types`, re-run that install command.** `prek install` writes
the shims that exist when it runs; adding a hook type to the config afterwards does
not reach back into a clone that already has one. Such a checkout still gets the
commit-stage `src/` checks, but nothing at merge or push, and nothing tells you the
other two stages exist. To confirm, list the hook directory -- all three names
should be there:

```bash
ls "$(git rev-parse --path-format=absolute --git-common-dir)/hooks"
```

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
| `lint` | `uv run pyrefly check src tests` |
| `test` | `uv run pytest -m "not browser and not sane_hardware"` |

You can reproduce the gate exactly, in the same order, with:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pyrefly check src tests
uv run pytest -m "not browser and not sane_hardware"
```

All five must exit 0. Fix what they report -- do not silence them. `# noqa`,
`# type: ignore` and rule-disabling are not accepted, and both type checkers must be
clean because they do not always report the same issues for the same code.

The `browser` marker deselects the Playwright tests, which need a real Chromium
install. Run those locally with `uv run pytest -m browser` when you touch the web UI.

The `sane_hardware` marker deselects the tests that drive the real SANE `test`
backend through a `SANE_CONFIG_DIR` pointed at a temporary `dll.conf`. Run those
locally with `uv run pytest -m sane_hardware` when you touch the scanner layer. No
extra apt package is needed: `libsane-dev` depends on `libsane1`, which ships
`libsane-test.so.1`, and both CI jobs already install it.

A test that hangs is not allowed to hang the run: `pytest-timeout` is configured in
`pyproject.toml` with `timeout = 60` and `timeout_method = "signal"`, so a stuck test
fails on its own with a traceback while the rest of the suite keeps going.

## Local pre-flight

The repository ships a hook configuration. Run it over the whole tree before you
push:

```bash
uv run prek run --all-files
```

`--all-files` is doing real work in that command. prek's default scope is the
*staged* set, and "before you push" is exactly the state in which nothing is staged:
your work is already committed. A bare `uv run prek run` in that state skips almost
every hook -- no ruff lint, no format check, no `debug-statements`, no
`detect-private-key` -- and prints a wall of `Skipped` that is easy to read as a
clean tree. Only the two type checkers, which are `always_run`, actually look at
anything. Use the bare `uv run prek run` only when the staged set is the point, such
as inspecting what a commit is about to run.

`prek` is a drop-in replacement for the older hook runner -- invoke it as
`uv run prek run`, never as `pre-commit run`. At its default stage it runs the
formatter, the linter and both type checkers, but the type checkers only cover `src/`
there. For the full type check over `src/` and `tests/`, run
`uv run prek run --stage pre-push --all-files`. prek does not run the test suite at
any stage, so run the five commands above as well.

## Where the type checkers run

The type checkers run at three points, and they do not check the same files at each
one.

At commit time, ty and pyrefly are limited to `src/`. That lets you commit a TDD RED
test -- a test that names a function or class that does not exist yet -- with every
hook enabled. A type error in `src/` is still rejected at commit.

At merge and push time the full type check runs: ty over the whole project, and
pyrefly over `src tests`. A `git merge --no-ff` that creates the merge commit by
itself runs it at `pre-merge-commit`, and `git push` runs it at `pre-push`. No
file-modifying hook runs at either of those stages, so a fixer can never abort a
merge or a push by rewriting a file.

`pre-push` checks the working tree you have checked out, not the commits being
pushed. Push from a checkout of the branch you are pushing. If you push some other
branch by name, the hook checks the wrong files.

A merge with conflicts is the one gap. When you resolve the conflicts and finish the
merge with `git commit`, git runs the commit-stage hooks rather than
`pre-merge-commit`, so only `src/` is type-checked. A type error in `tests/` that gets
through there is still caught at push, in CI and by the phase verifier.

If the hook rejects a merge:

1. Read the hook output to see what failed.
2. Run `git merge --abort`. A rejected merge leaves `MERGE_HEAD` and a staged merge
   result behind, and this clears them.
3. Fix the type error on the branch you were merging.
4. Merge again.

Never finish a rejected merge with `git commit`. That runs only the commit-stage
check, so the error you were just shown would get through.

### Why pyrefly always gets explicit paths

Every pyrefly command in this repository names its paths: `src` at commit time, and
`src tests` everywhere else. pyrefly's `use-ignore-files` is on by default and is
only bypassed when files are named on the command line. In a checkout inside a
gitignored directory (a `.claude/worktrees/` git worktree), running pyrefly without
paths filters out every source file, so it can report success having checked nothing.
Copy the commands as written, paths included.

## `--no-verify` no longer skips the gate

The hooks are installed as a convenience, not as the enforcement point. Committing
with `git commit --no-verify` still works, but it no longer gets you anything: the
same checks run again on the pull request, so skipping them locally only moves the
failure later and makes it slower to find.

The same is true of merges and pushes. `git merge --no-verify` skips the
`pre-merge-commit` hook, and `git push --no-verify` skips the `pre-push` hook. CI
still runs all five checks on the pull request, so skipping them locally only moves
the failure later.

## How `master` is protected

`master` is protected by a branch ruleset requiring both `lint` and `test` to be
green, and forbidding branch deletion and non-fast-forward pushes. Contributors reach
`master` through a pull request with both required checks green.

The repository administrator holds a bypass actor on this ruleset, so the maintainer
retains an emergency path for the cases where the gate must be overridden. The ruleset
requires green checks rather than requiring a pull request as such.

Dependabot is configured in `.github/dependabot.yml` to bump the SHA-pinned GitHub
Actions weekly. It watches all workflows in the repository, so it will also open pull
requests touching `release.yml` and `docs.yml`. That is expected behaviour rather
than scope creep, and those pull requests go through the same gate as any other.
