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

Your `uv` must be a 0.12.x release, 0.12.18 or newer. `pyproject.toml` declares that
range as `required-version` under `[tool.uv]`, and uv refuses to run `uv sync`,
`uv run` or `uv lock` outside it. The git hooks run through `uv run`, so an older uv
blocks commits too. `uv --version` shows what you have. If it is outside the range,
run `uv self update 0.12.18`, or upgrade through whatever installed uv. Name the
version: a bare `uv self update` installs the newest uv, which can already be past
the 0.12 series.

## Setting up

```bash
# Install the SANE headers first (Debian/Ubuntu shown; see README.md for others)
sudo apt-get install libsane-dev

# Build the environment from the committed lockfile
uv sync --locked

# Install the git hooks
uv run prek install
```

Use `uv sync --locked` rather than a bare `uv sync`. `--locked` fails if `uv.lock`
does not match `pyproject.toml` instead of quietly re-resolving the dependency graph.
CI runs the same command, so a stale lockfile turns the pull request red rather than
letting your machine and the runner drift apart.

The last command installs three hooks, `pre-commit`, `pre-merge-commit` and
`pre-push`, which are listed under `default_install_hook_types` in
`.pre-commit-config.yaml`. It writes them where git looks for hooks, so they also run
in any git worktree of the clone.

**If you installed the hooks before `pre-merge-commit` and `pre-push` were added to
`default_install_hook_types`, re-run that install command.** `prek install` writes
the shims that exist when it runs; adding a hook type to the config afterwards does
not reach back into a clone that already has one. Such a checkout still gets the
commit-stage `src/` checks, but nothing at merge or push, and nothing tells you the
other two stages exist. To confirm, list the directory git runs hooks from -- all
three names should be there:

```bash
ls "$(git rev-parse --git-path hooks)"
```

To add a dependency, use `uv add <package>` (or `uv add --dev <package>`) and commit
the resulting `uv.lock` change alongside the `pyproject.toml` change.

## The checks CI runs

Every push to `master` and every pull request runs `.github/workflows/ci.yml`, whose
four parallel jobs run the commands below, and `.github/workflows/docs.yml`, which
builds the documentation site:

| Workflow | Job | Command |
|----------|-----|---------|
| `ci.yml` | `lint` | `uv run prek run --all-files --show-diff-on-failure` |
| `ci.yml` | `lint` | `uv run prek run --stage pre-push --all-files --show-diff-on-failure` |
| `ci.yml` | `lint` | `uv audit --preview-features audit-command` |
| `ci.yml` | `test` | `uv run env HOME="$(mktemp -d)" pytest -m "not browser and not sane_hardware"` |
| `ci.yml` | `test` | `uv run env HOME="$(mktemp -d)" pytest -m sane_hardware` |
| `ci.yml` | `browser` | `uv run env HOME="$(mktemp -d)" PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" pytest -m browser` |
| `ci.yml` | `docker` | builds the image as `saneless:ci`, then `uv run --no-project python scripts/smoke_image.py saneless:ci` |
| `docs.yml` | `build` | `uv run --no-sync mkdocs build --strict` |

The `lint` job runs the hook file rather than a list of its own:
`.pre-commit-config.yaml` is the one definition of the lint gate, so what the hooks
check on your machine and what CI checks are the same thing. The first prek command
runs the commit stage over the whole tree. That includes the file-modifying fixers
(the ruff fixer and formatter, the end-of-file and trailing-whitespace fixers and the
rest); on a clean tree they change nothing, and if any of them would change a file
the job fails and prints the diff. The same stage runs ty and pyrefly over `src/`,
the workflow audit (`zizmor`), the read-only file checks and the planning-citation
check. The second command runs the push stage: ty over the whole project, pyrefly
over `src tests scripts`, `ruff check --no-fix .`, `ruff format --check .` and
`pytest tests/test_deployment_config.py`. One hook does nothing in CI:
`check-added-large-files` looks only at files staged as new, and a CI checkout stages
nothing, so it guards your local commits and not the pull request. `uv audit` is its
own step because it queries an advisory database over the network, which no hook
should need.

You can reproduce the gate, in the same order, with:

```bash
uv run prek run --all-files --show-diff-on-failure
uv run prek run --stage pre-push --all-files --show-diff-on-failure
uv audit --preview-features audit-command
uv run env HOME="$(mktemp -d)" pytest -m "not browser and not sane_hardware"
uv run env HOME="$(mktemp -d)" pytest -m sane_hardware
uv run playwright install chromium firefox   # once, to fetch the browsers
uv run env HOME="$(mktemp -d)" PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" pytest -m browser
docker build -t saneless:ci .
uv run --no-project python scripts/smoke_image.py saneless:ci
uv run --no-sync mkdocs build --strict
```

The test commands point `HOME` at a fresh empty directory, which proves on every run
that the suite never reads or writes your real home: no `~/.config/saneless` config
(with its live Paperless URL and token), no `~/.local/state`. The suite also isolates
itself -- every test gets a fake `HOME` and XDG tree and runs in its own working
directory -- so a plain `uv run pytest` is safe too; the empty `HOME` is what checks
that isolation holds. The browser line names Playwright's browser directory because
Playwright looks for its browsers under `HOME`, and `$HOME` there is expanded by your
shell, before `env` changes it, so it is your real home, where `playwright install`
put them. If you set `PLAYWRIGHT_BROWSERS_PATH` or `XDG_CACHE_HOME` for the
install, use that directory instead.

The `docker` job builds the image from the `Dockerfile` and never pushes it, then
starts containers from it and runs the smoke checks in `scripts/smoke_image.py`
against them. The script drives the local `docker` CLI, and Podman's `docker` shim
works too, provided the image is built in Docker format: Podman's default OCI format
drops the `HEALTHCHECK`, and the smoke test then refuses the image. Under Podman, build
with `docker build --format docker -t saneless:ci .` instead. The docs build is strict: a broken nav entry, a dead link or a link to an
anchor that does not exist fails it. Never add `-q` to that command. Quiet mode hides
the warnings that strict mode counts, so a quiet strict build exits 0 over a dead
anchor.

Every command must exit 0. Fix what they report -- do not silence them. `# noqa`,
`# type: ignore` and rule-disabling are not accepted, and both type checkers must be
clean because they do not always report the same issues for the same code. The
workflows skip nothing either: no hook is skipped with `SKIP=`, and no step carries
`continue-on-error` or ends in `|| true`. A test in `tests/test_deployment_config.py`
fails the build on any of them.

Tests do not call `time.sleep`. A test that needs something to happen waits on a
`threading.Event` that the code under test sets, polls with `poll_until` from
`tests/conftest.py`, or advances a fake clock. `tests/ruff.toml` extends the project's
ruff settings with a `time.sleep` ban that covers `tests/` only, so `ruff check` fails
on a new sleep in a test while production code, such as the Paperless upload backoff,
may still sleep.

The ban covers `time.sleep` and nothing else, so it does not prove that no test pauses.
`poll_until` and `wait_for_state` pause briefly between polls, on an Event nobody sets,
until their condition holds or their budget runs out. A test that proves something does
*not* happen has nothing to poll for, so it may leave the code a short fixed window
through `quiet_window` in `tests/conftest.py`, which keeps every such pause findable by
name. The `no-inline-fixed-waits` hook fails on the inline `Event().wait(...)` spelling
of a pause; a pause written any other way is left to review.

Comments in `src/` state their reasons in words and never cite planning IDs (decision,
finding or requirement numbers, phase or plan numbers, planning file names), because the
planning records do not ship with the product. The `no-planning-citations` hook fails a
commit, merge or push that adds one, and a test in `tests/test_deployment_config.py`
fails CI.

The `test` job deselects the `browser` marker because the `browser` job runs those
Playwright tests, with Chromium and Firefox installed (`uv run playwright install
--with-deps chromium firefox`). Firefox is there for one test: it restores a ticked
checkbox on reload where Chromium does not, so only Firefox can show that the Multiple
pages box starts unticked on every page load. They need no internet: every page is routed through an egress gate that
fails the test if the UI tries to reach anything but the local test server, so they
pass the same way on a laptop with no network as in CI.

The `sane_hardware` marker deselects the tests that drive the real SANE `test`
backend through a `SANE_CONFIG_DIR` pointed at a temporary `dll.conf`. Run those
locally with `uv run pytest -m sane_hardware` when you touch the scanner layer. No
extra apt package is needed: `libsane-dev` depends on `libsane1`, which ships
`libsane-test.so.1`, and every CI job already installs it.

CI runs this marker too, as its own step in the `test` job, so these are not
optional local extras: a red `sane_hardware` run blocks merge exactly like a red
lint or a red unit test.

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
clean tree. Only the hooks marked `always_run` -- ty, pyrefly and the `zizmor`
workflow audit -- actually look at anything. Use the bare `uv run prek run` only when the staged set is the point, such
as inspecting what a commit is about to run.

`prek` is a drop-in replacement for the older hook runner -- invoke it as
`uv run prek run`, never as `pre-commit run`. At its default stage it runs the
formatter, the linter and both type checkers, but the type checkers only cover `src/`
there. For the full type check over `src/` and `tests/`, run
`uv run prek run --stage pre-push --all-files`. That stage also runs
`tests/test_deployment_config.py`, but no stage runs the rest of the test suite, so
run the other commands above as well.

## Where the type checkers run

The type checkers run at three points, and they do not check the same files at each
one.

At commit time, ty and pyrefly are limited to `src/`. That lets you commit a TDD RED
test -- a test that names a function or class that does not exist yet -- with every
hook enabled. A type error in `src/` is still rejected at commit.

At merge and push time the full type check runs: ty over the whole project, and
pyrefly over `src tests scripts`. A `git merge --no-ff` that creates the merge commit by
itself runs it at `pre-merge-commit`, and `git push` runs it at `pre-push`. No
file-modifying hook runs at either of those stages, so a fixer can never abort a
merge or a push by rewriting a file.

`pre-push` checks the working tree you have checked out, not the commits being
pushed. Push from a checkout of the branch you are pushing. If you push some other
branch by name, the hook checks the wrong files.

A merge with conflicts is the one gap. When you resolve the conflicts and finish the
merge with `git commit`, git runs the commit-stage hooks rather than
`pre-merge-commit`, so only `src/` is type-checked. A type error in `tests/` that gets
through there is still caught at push and in CI.

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
`src tests scripts` everywhere else. pyrefly's `use-ignore-files` is on by default and
is only bypassed when files are named on the command line. In a checkout inside a
gitignored directory (a `.claude/worktrees/` git worktree), running pyrefly without
paths filters out every source file, so it can report success having checked nothing.
A directory left off the list is not checked either, which is why `scripts` is named
alongside `src` and `tests`. Copy the commands as written, paths included.

## `--no-verify` no longer skips the gate

The hooks are installed as a convenience, not as the enforcement point. Committing
with `git commit --no-verify` still works, but it no longer gets you anything: the
same checks run again on the pull request, so skipping them locally only moves the
failure later and makes it slower to find.

The same is true of merges and pushes. `git merge --no-verify` skips the
`pre-merge-commit` hook, and `git push --no-verify` skips the `pre-push` hook. CI
still runs every check on the pull request, both hook stages included, so skipping
them locally only moves the failure later.

## How `master` is protected

`master` is protected by a branch ruleset requiring both `lint` and `test` to be
green, and forbidding branch deletion and non-fast-forward pushes. Contributors reach
`master` through a pull request with both required checks green. The `browser` job
runs on the same pull requests but is not yet a required check in the ruleset.

The repository administrator holds a bypass actor on this ruleset, so the maintainer
retains an emergency path for the cases where the gate must be overridden. The ruleset
requires green checks rather than requiring a pull request as such.

Dependabot is configured in `.github/dependabot.yml` to bump the SHA-pinned GitHub
Actions weekly. It watches all workflows in the repository, so it will also open pull
requests touching `release.yml` and `docs.yml`. That is expected behaviour rather
than scope creep, and those pull requests go through the same gate as any other.
The same file has Dependabot move the Dockerfile's base-image digests, the locked
Python dependencies and the hook repositories in `.pre-commit-config.yaml`. Those
hook repositories are pinned to commit SHAs, each with a `# frozen: vX.Y.Z` comment
naming its release, and Dependabot updates the SHA and the comment together. Every
entry waits seven days after a release before proposing it. To move a hook pin by
hand, run `uv run prek autoupdate --freeze`, never a plain `autoupdate`, which would
write a tag back in place of the SHA.
