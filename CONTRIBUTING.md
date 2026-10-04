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
| `ci.yml` | `test` | `uv run env HOME="$(mktemp -d)" pytest -o timeout_method=thread -m "not browser and not sane_hardware"` |
| `ci.yml` | `test` | `uv run env HOME="$(mktemp -d)" pytest -o timeout_method=thread -m sane_hardware` |
| `ci.yml` | `browser` | `uv run env HOME="$(mktemp -d)" PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" pytest -o timeout_method=thread -m browser` |
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
uv run env HOME="$(mktemp -d)" pytest -o timeout_method=thread -m "not browser and not sane_hardware"
uv run env HOME="$(mktemp -d)" pytest -o timeout_method=thread -m sane_hardware
uv run playwright install chromium firefox   # once, to fetch the browsers
uv run env HOME="$(mktemp -d)" PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright" pytest -o timeout_method=thread -m browser
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

The suite keeps off the network and out of your environment too. Paperless
requests are refused by default: every test URL names `paperless.invalid`, and the
app's Paperless client is built over a transport that fails every request without
opening a socket. A test that needs the real HTTP transport says so with
`@pytest.mark.real_paperless_transport`. A socket guard watches every connect the
test process makes, from any thread, and fails the test that connected anywhere but
a loopback port the process itself bound. Ports 8000 and 6566, where a local
Paperless or saned usually listens, are refused unless the process holds the
listener at that address. A server started in a child process binds its port where
the guard cannot see it, so its test declares the port with
`socket_guard.allow_port(port)`. libsane's own sockets are opened in C and are
invisible to the guard. Before the first test runs, ambient `SANELESS_*` (in any
case), `SANE_*`, `SSL_CERT_FILE`, `SSL_CERT_DIR` and proxy variables are cleared,
and the system config file `/etc/saneless/saneless.toml` is replaced by an empty
location, so nothing exported in your shell changes a verdict.

The `docker` job builds the image from the `Dockerfile` and never pushes it, then
starts containers from it and runs the smoke checks in `scripts/smoke_image.py`
against them. The script drives the local `docker` CLI, and Podman's `docker` shim
works too, provided the image is built in Docker format: Podman's default OCI format
drops the `HEALTHCHECK`, and the smoke test then refuses the image. Under Podman, build
with `docker build --format docker -t saneless:ci .` instead. The docs build is strict: a broken nav entry, a dead link or a link to an
anchor that does not exist fails it. Never add `-q` to that command. Quiet mode hides
the warnings that strict mode counts, so a quiet strict build exits 0 over a dead
anchor.

Every command must exit 0. Fix what they report -- do not silence them. `# noqa` and
`# type: ignore` are not accepted. A lint rule is relaxed only by an entry under
`[tool.ruff.lint.per-file-ignores]` in `pyproject.toml` that carries a one-line
reason, and never by an inline marker. Both type checkers must be clean because they
do not always report the same issues for the same code. The
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
name. A browser test does the same through `browser_quiet_window`, which waits through
the page so Playwright keeps delivering its events. The `no-inline-fixed-waits` hook
fails on the inline `Event().wait(...)` and `wait_for_timeout(...)` spellings of a pause
anywhere under `tests/` except `tests/conftest.py`, which holds the two helpers; a pause
written any other way is left to review.

Comments and docstrings in `src/`, `scripts/`, `tests/`, the workflows under `.github/`
and `.dockerignore` state their reasons in words and never cite planning IDs (decision,
finding, review or requirement numbers, phase or plan numbers, planning file names),
because the planning records do not ship with the product. For the same reason a
comment cites a symbol, such as `paperless._without_userinfo`, and never a line number,
a file that does not ship (the assistant-instructions file or the planning records) or
a phase of work it does not number, such as an earlier or a later one.

The `no-planning-citations` hook is a net for the common shapes of those references,
not a proof. It fails a commit, merge or push that adds, to the shipped sources, the
tests, the workflows, `.dockerignore`, the decision records under
`docs/explanation/decisions/`, this file, the `Dockerfile` or `docker-compose.yml`,
any of these: an identifier of a shape the pattern in `tests/citation_samples.py`
spells out; a numbered phase or plan; a Python, HTML, CSS or JavaScript file name
followed by a colon and a line number; the planning directory or the
assistant-instructions file by name; or a phase named only by a word such as this,
earlier or next. A test in `tests/test_deployment_config.py` runs the same pattern
over the same files in CI. Anything the pattern misses, such as a line number written
out in words, is left to review. The hook skips one file, `tests/citation_samples.py`,
which holds the pattern and the sample identifiers the guard's own tests need; a test
pins that it stays the only exclusion.

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

The `slow` marker labels a test that takes tens of seconds: the loop in
`tests/test_libsane_reader_exit.py` that runs a hundred whole saneless processes to
show a failed libsane read never leaves one hung. It is also `sane_hardware`, so CI
runs it in that step; nothing deselects `slow` by default. Add `-m "not slow"` for a
quicker local run.

A test that hangs is not allowed to hang the run: `pytest-timeout` is configured in
`pyproject.toml` with `timeout = 60` and `timeout_method = "signal"` for local runs, so
a stuck test fails on its own with a traceback while the rest of the suite keeps going.
CI passes `-o timeout_method=thread` to every pytest command in `ci.yml`, as the table
and the block above show. On a timeout the thread method prints every thread's stack
and then terminates the whole pytest process, so a hang in CI says where each thread
was stuck, at the cost of the rest of the run: later tests never run, session fixtures
are not torn down, and child processes are left behind. The signal method fails only
the stuck test, but it interrupts only the main thread and prints only its stack. Add
the thread flag locally when you are chasing a hang.

## Comments and docstrings

Before writing a comment, ask what mistake it stops the next editor making. An
invariant, a race or a measured quirk of a library stays next to the line it guards,
in three sentences or fewer. How the code got here goes in the commit message, or, for
a choice a later contributor could undo by mistake, in a decision record. A comment
that restates the code is deleted. A docstring longer than its function is cut down,
unless the function is a public contract. Numbers the code already holds, such as the
size of an enum, the number of checks or an exit code, are not spelled out in prose:
name the enum or the constant instead, so the sentence cannot drift from the code.
Review enforces this rule; no hook measures it.

Decision records live under `docs/explanation/decisions/` as `NNNN-slug.md`, numbered
from `0001`. Each is short: Status, Context, Decision, Consequences and a one-line
list of the alternatives rejected. They are written in the present tense, with dates
only in Status, and each one is listed in `docs/explanation/decisions/README.md`. They
ship in the repository but stay out of the site navigation. Code that depends on a
decision keeps the invariant next to the line and ends that comment with the pointer:

```text
See docs/explanation/decisions/NNNN-slug.md.
```

A test in `tests/test_deployment_config.py` fails on a pointer that is not the
`NNNN-slug.md` shape, on a pointer to a record that does not exist and on a record
the index does not list. The placeholder above passes in this file only.

Two conventions the code follows without restating them at each site:

- The coordinator seams and the scanner backend are `abc.ABC` classes, not
  `typing.Protocol`. A `Protocol` describes a shape this project does not own, such as
  python-sane's device handle or pydantic's settings constructor; an `ABC` defines a
  seam the project implements itself. Where a seam is called a "protocol" in lower
  case, the word means contract, not `typing.Protocol`.
- Code that moves to another module leaves no re-export behind. Every importer and
  every `monkeypatch` target is updated to the new home, so a stale target fails
  loudly instead of patching a name nothing reads.

The lint notes below cover rules a contributor meets again and again. Each says what
the project does about the rule, so the code does not have to explain itself at every
site.

- `S105` and `S106` (ruff's hard-coded-credential rules) read a string literal
  assigned to a name, or passed to a keyword, containing "pass", "token", "secret" or
  "password" as a credential. A scan pass or a message about a missing token is not
  one. Give the literal a name the rule does not read, as `_REJECTED_WIRE_VALUE` in
  `vocabulary.py` does, and pass that constant where a keyword such as `pass_label=`
  takes the value.
- `PLR0913` caps a function at five parameters, `self` excluded and keyword-only ones
  included, and `PLR0912` caps its branches. Neither limit is raised. Over the
  argument limit, values that travel together become one frozen dataclass; over the
  branch limit, a helper takes the branches it owns. A parameter object is not
  introduced only to satisfy the count: if the values do not belong together, split
  the function instead. A FastAPI handler takes related form fields as one dataclass
  through `Depends`.
- `S101` bans `assert` in `src/`. Narrow an optional where the value is built, so the
  callee receives a record whose field is never `None`, or raise an explicit error.
- `S608` matches `select ... from` anywhere in the literal text of an interpolated
  string. SQL verbs are written plainly, never split into a constant to hide them from
  the rule. Where every interpolated fragment is a module constant and every value is
  a bound parameter, the module gets an `S608` entry under `per-file-ignores` that
  says so, as `src/saneless/job.py` does.
- `S603` accepts a subprocess argv only when every element is a literal or
  `sys.executable`. Variable parts reach the child through its environment, not
  through the argv.
- The `PTH` rules ask for the `pathlib` equivalent of an `os` or `open()` call; use it
  rather than suppress the rule. `PTH105` forbids `os.replace`: rename with
  `Path.replace`, which is the same atomic, overwriting `rename(2)`.
- The `FBT` rules reject a positional boolean parameter. Make it keyword-only, so a
  call site never passes a bare `True`.
- `PLW0603` rejects `global`. Module state that changes at run time lives in a small
  record the module mutates in place, never in a name it rebinds.
- `B008` rejects a call in a default argument, including FastAPI's `Query(...)` and
  `Form(...)`. Build the default once as a module-level constant and use that.
- `B027` flags an empty, non-abstract method on an `ABC` as a probable unfinished
  override. A deliberate no-op default gets a real body, such as a DEBUG log line.
- Python 3.14 accepts `except A, B:` without brackets. Use one clause only when the
  exceptions mean the same thing to the caller; when they do not, write two clauses.
- `E501` is off. The formatter keeps code at 88 columns, and a user-facing sentence
  that a test pins word for word stays one literal on one line, so a search finds it.

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

## Mutation testing

Mutation testing checks the tests rather than the code: each mutant is a small
deliberate bug, and a mutant that no test fails on marks a behaviour the suite does
not pin down. One command runs both kinds of mutant:

```bash
uv run env HOME="$(mktemp -d)" pytest --mutants -m mutant && uv run mutmut run && uv run mutmut results
```

The first part runs the hand-written mutants in `tests/mutants/`. Each one copies the
repository, applies a named edit to the copy and re-runs one test there, which must
fail. They are deselected unless pytest is given `--mutants`, so a plain test run
never pays for them, and a surviving one fails the command. `mutmut run` then
generates mutants over the modules listed under `[tool.mutmut]` in `pyproject.toml`
and exits 0 even when some survive. `mutmut results` lists the survivors, which are
findings to read rather than a failure.

mutmut has a blind spot: it never mutates a function that carries a decorator other
than `staticmethod` or `classmethod`. The job store's locked methods are all
decorated, so they get no generated mutants at all, and the hand-written mutants are
there to cover them.

The run takes far longer than the CI gate can afford, so it is not part of it.
`.github/workflows/mutation.yml` runs the same three steps weekly and whenever it is
started by hand from the Actions tab. No push or pull request starts it, and it never
blocks a merge.

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
