---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 03
subsystem: logging
tags: [logging, 12-factor, docker, serve, cli, observability]
requires:
  - "31-01 (packaging foundation; base commit b776a0f)"
provides:
  - "`saneless serve` streams log records to stderr and writes no file, so `docker logs` and journald see them"
  - "Tracebacks render on the serve stream with and without `-v` (D-36 amended)"
  - "`configure_logging(log_file=None)` as the service logging shape"
  - "`_load_cli_settings(ctx, *, stream_logs=False)`, with `serve` the only caller that opts in"
  - "A regression guard proving one-shot CLI logging is unchanged (D-34)"
  - "Mode scope documented on the three rotating-file config keys"
affects:
  - "Plan 02 (docs rename) — no file overlap; `tests/test_deployment_config.py` untouched here"
  - "Plan 04 (workflows) — no file overlap"
  - "The Dockerfile — deliberately unchanged: `CMD [\"serve\"]` already selects the streaming mode (D-24)"
tech-stack:
  added: []
  patterns:
    - "Mode selection by absence of a sink (`log_file=None`) rather than a boolean flag"
    - "`logging.StreamHandler(sys.stderr)` with a plain `Formatter`, beside the traceback-free fallback"
    - "Handler counting by set-difference against a pre-invoke snapshot, so pytest's own capture handlers do not pollute the assertion"
key-files:
  created: []
  modified:
    - src/saneless/logging_config.py
    - src/saneless/cli.py
    - src/saneless/config.py
    - tests/test_logging.py
    - tests/test_cli.py
    - docs/reference/configuration.md
    - docs/reference/cli-commands.md
    - docs/how-to/troubleshoot-a-failed-scan.md
decisions:
  - "The service mode is selected by `log_file=None`, not a sixth `stream=True` parameter: ruff's PLR0913 caps the signature at five and CLAUDE.md forbids both a suppression and raising the limit"
  - "The serve stream uses a plain Formatter, the inverse of the OSError fallback's `_TracebackFreeFormatter`, because the stream *is* the log and no file carries the traceback instead (D-36 amended)"
  - "The `-v` mirror handler is skipped when there is no log file, so the stream stays exactly one handler instead of printing every record to stderr twice"
  - "An exit-5 line under `serve` drops the `Run again with -v` hint: the traceback is already printed directly above it, and the hint would send an operator to restart a running service"
metrics:
  duration: "~50 min"
  completed: 2026-09-18
  tasks: 3
  commits: 5
  files_changed: 8
---

# Phase 31 Plan 03: Serve Logging Mode Split Summary

`saneless serve` now behaves as a 12-factor service — one stderr `StreamHandler`, no log file, tracebacks always rendered — while every one-shot CLI command keeps Phase 28's rotating file handler byte-for-byte.

## What Was Built

**Task 1 — the service shape in `configure_logging`** (`8ba1ede` RED, `e93509d` GREEN)

`src/saneless/logging_config.py`'s `log_file` parameter became `str | None`. Given `None`, the
function attaches a single `logging.StreamHandler(sys.stderr)` with a **plain**
`logging.Formatter(_FORMAT)`, creates no directory, emits no "Cannot write to …" warning, and
returns `False`. Given a path, every byte of Phase 28's behaviour is unchanged.

Three things were deliberately left alone:

- `_TracebackFreeFormatter` is still defined once and used once, by the `OSError` fallback only.
- The `if verbose:` mirror gained an `and log_file is not None` guard — without it the mirror
  would print every record to the same stderr twice. The "exactly one handler" assertion is the
  contract that pins this.
- `logging.getLogger("saneless").setLevel(logging.DEBUG if verbose else logging.NOTSET)` runs
  once, in both modes, exactly as written (D-38, T-27-23).

A comment above the branch records why the plain formatter is right here and wrong in the
fallback below it, in the house form: what the reader will assume, why it is wrong, and what
breaks if it is changed back.

`TestConfigureLoggingStreamMode` in `tests/test_logging.py` carries six tests: no file handler,
exactly one stderr handler at the configured level, the `False` return, a traceback without
`-v`, a traceback with `-v` (exactly one), and the credential-leak guard.

**Task 2 — the seam** (`eda19f4` RED, `73ef3ab` GREEN)

`_load_cli_settings(ctx: click.Context, *, stream_logs: bool = False)` passes
`None if stream_logs else settings.output.log_file` into `configure_logging`. `serve` is the
only caller that opts in; `scan`, `devices`, `jobs`, `doctor` and `auto-profiles` are textually
identical (`grep -c '_load_cli_settings(ctx)$'` = 5).

The line `ctx.obj["log_file"] = settings.output.log_file if attached else None` needed **no
edit** — a `False` return makes it `None` on its own, which is what stops anything printing
`Full details in <log_file>` for a service that writes none.

`uvicorn.run(...)` is unchanged and now carries a comment saying why changing it would break
D-37: with no log config uvicorn runs no `dictConfig`, attaches no handlers, and its
`uvicorn.error` / `uvicorn.access` / `uvicorn.asgi` loggers propagate to saneless's root
handlers, so the access log follows the stream for free.

The startup-failure message's tail changed from `; the cause is in the log` to
`; the cause is in the preceding log lines`, because in serve mode "the log" is the very stream
that line is printed on.

`TestServeLogging` in `tests/test_cli.py` carries seven tests, each patching in the **real**
`configure_logging`: no rotating handler and no `logs/` directory created, exactly one added
stream handler, `ctx.obj["log_file"] is None`, a parametrised traceback test over
`["serve"]` and `["-v", "serve"]`, the exit-5 hint suppression, and
`test_one_shot_command_still_attaches_the_file_handler` — the D-34 regression guard, which
invokes `jobs` and asserts the rotating handler's `baseFilename`, the recorded `log_file` and
that the log directory *was* created.

**Task 3 — the documentation the change falsified** (`097b601`)

| Surface | Change |
|---------|--------|
| `docs/reference/configuration.md` | `log_file`, `log_max_bytes`, `log_backup_count` marked **one-shot CLI commands only**, each with one sentence of consequence; `log_level` marked as applying to **both** modes |
| `docs/reference/cli-commands.md` | the `-v` row made mode-aware; the global and `serve` exit-5 rows no longer promise a log file |
| `src/saneless/config.py` | the same mode scope as a comment over `OutputConfig`'s three log keys, naming D-39 as the reason no warning fires |
| `src/saneless/cli.py` | the `-v` help text no longer claims "the log file and stderr" |
| `docs/how-to/troubleshoot-a-failed-scan.md` | the two sentences this behaviour change made false |

The `-v` row keeps its closing clause verbatim in meaning — *other libraries and the web server
keep the configured `log_level`* — because that clause is T-27-23: httpx at DEBUG prints the
Paperless `Authorization` header. No runtime warning was added for a `log_file` set under
`serve` (D-39).

## Verification Results

| Check | Result |
|-------|--------|
| `uv run pytest -m "not browser and not sane_hardware" -q` | 3087 passed (3074 before, +13) |
| `uv run pytest tests/test_logging.py -q` | 22 passed (16 before, +6) |
| `uv run pytest tests/test_cli.py -q` | 188 passed (181 before, +7) |
| `uv run mkdocs build --strict` | exit 0 |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks passed |
| `uv run prek run --stage pre-push --all-files` | all hooks passed |
| `uv run saneless --help` | renders the new `-v` text, exit 0 |

Acceptance greps: `_load_cli_settings(ctx, stream_logs=True)` 1, `_load_cli_settings(ctx)` 5,
`log_config=None` 1, `access_log=True` 1, `the cause is in the log"` 0,
`log file and mirror it to stderr` in `cli-commands.md` 0, `keep the configured` + `log_level`
in the `-v` row 1, `log file and stderr` in `cli.py` 0, `getLogger("saneless").setLevel` 1,
`_TracebackFreeFormatter` 1 definition + 1 use.

**`git diff --numstat b776a0f..HEAD` shows zero deleted lines in both test files**
(`tests/test_cli.py` 239/0, `tests/test_logging.py` 135/0), so **no pre-existing test was
modified or weakened** — see deviation 2. No `# noqa`, `# type: ignore` or rule-disabling was
added anywhere.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] The planned `stream: bool = False` parameter is impossible; the mode is selected by `log_file=None` instead**

- **Found during:** Task 1
- **Issue:** adding a sixth parameter to `configure_logging` raised
  `PLR0913 Too many arguments in function definition (6 > 5)`. CLAUDE.md forbids suppressions and
  forbids disabling rules, and this repository has a documented house precedent for exactly this
  situation (`src/saneless/job.py:519-531`, `:686`): five non-`self` parameters is the ceiling,
  keyword-only ones count, and the answer is to restructure. Bundling `max_bytes` /
  `backup_count` was ruled out because `tests/test_logging.py:230,257` call
  `configure_logging(log_file, "INFO", 1024, 1)` positionally and are must-not-change tests.
  Splitting into a second public `configure_stream_logging` was ruled out because `_patch_cli`
  stubs `configure_logging` by name, so all seven `TestServeCommand` tests would have begun
  running the real configurator and attaching live stderr handlers — the plan's own reason for
  preferring a defaulted keyword.
- **Fix:** `log_file: str | None`, where `None` *is* the service mode. This adds no parameter,
  leaves all 16 `TestConfigureLogging` tests and all 7 `TestServeCommand` tests green unmodified,
  and is arguably more honest: a service genuinely has no log file, so the two modes cannot be
  asked for contradictorily. `serve` still opts in through the planned
  `_load_cli_settings(ctx, stream_logs=True)` keyword, so the plan's key link is intact; only the
  inner spelling changed.
- **Files modified:** `src/saneless/logging_config.py`, `src/saneless/cli.py`,
  `tests/test_logging.py`
- **Commit:** `e93509d`

**2. [Rule 2 — Missing correctness] An exit-5 line under `serve` no longer offers the `-v` hint**

- **Found during:** Task 3
- **Issue:** `_report_unexpected` appends `Run again with -v to see the traceback` whenever
  `ctx.obj["log_file"]` is falsy and `-v` was not given. With the mode split that branch now
  fires for `serve` — where the traceback has *already* been rendered on the stream, directly
  above the line. The hint told an operator to restart a running service to see something they
  were already looking at, which directly contradicts D-36's amended rationale.
- **Fix:** `_load_cli_settings` records `ctx.obj["log_stream"] = stream_logs`, and the hint is
  skipped when it is set. The CLI stderr-fallback path (`log_file` None, `log_stream` falsy) is
  untouched, so `test_stderr_log_fallback_prints_no_traceback_without_verbose` still asserts the
  hint and still passes unmodified. Pinned by
  `test_serve_never_offers_the_verbose_hint_on_an_unexpected_error`.
- **Files modified:** `src/saneless/cli.py`, `tests/test_cli.py`
- **Commit:** `097b601`

**3. [Rule 2 — Missing correctness] `docs/how-to/troubleshoot-a-failed-scan.md` was falsified by this change**

- **Found during:** Task 3
- **Issue:** two sentences became untrue the moment `serve` stopped writing a file: the
  `serve` cannot start bullet ("when the web server itself failed, the cause is in the log
  file", line 114) and the unexpected-errors section's "-v hint" paragraph. The file is in no
  plan's `files_modified` in this phase — plan 02 owns other `docs/how-to/` pages, plan 04 owns
  only workflows — so there was no overlap risk and no other plan would have caught it.
- **Fix:** the bullet now points at the preceding log lines and says `serve` streams rather than
  writes; the unexpected-errors section gained one paragraph saying `serve` offers no hint and
  the traceback is collected from `docker logs` or `journalctl`. The doc-truth test
  `test_troubleshooting_page_is_linked_and_covers_every_exit_code` still passes: the section
  keeps `log file`, `bug` and `token`, and still never mentions the job database.
- **Files modified:** `docs/how-to/troubleshoot-a-failed-scan.md`
- **Commit:** `097b601`

### Notes, not deviations

- **No pre-existing test was modified — the one authorised modification turned out not to be
  needed.** The plan authorised updating
  `tests/test_cli.py::test_serve_uvicorn_startup_failure_exits_2_not_3`'s expected substring
  alongside the `cli.py:925` wording change. Reading it first (as instructed) showed it asserts
  only `"web server could not start"`, `"127.0.0.1:8080"`, a one-line failure and no traceback —
  never the tail of the message. `grep -rn "cause is in the log" tests/` returns nothing. The
  string was therefore changed with no test edit, and the numstat above proves zero deletions in
  either test file.
- **Two of the thirteen new tests passed on their RED run, by design.** The `-v` half of
  `test_serve_stream_renders_a_traceback` passed before the change (the old verbose mirror
  rendered tracebacks) and `test_one_shot_command_still_attaches_the_file_handler` passed
  (it pins behaviour that must *survive*). Both are regression guards, not behaviour-driving
  tests; the eleven genuinely new behaviours all failed first.
- **The worktree base needed correcting.** Claude Code spawned the agent branch at `ed2d620`
  (master), not at the plan base. The startup guard's `git reset --hard b776a0f` corrected it
  before any work began, as the orchestrator predicted.
- **`test_serve_attaches_one_stderr_stream_handler` counts handlers by set-difference.** The
  naive `h.stream is sys.stderr` filter returns zero under `CliRunner`, because click replaces
  `sys.stderr` during `invoke` and restores it before the assertion runs, while the handler keeps
  the replaced object. Snapshotting handler ids before the invoke also sidesteps pytest's own
  `LogCaptureHandler` and `_FileHandler(/dev/null)`, both of which sit on the root logger. That
  the one handler writes to stderr rather than stdout is pinned separately, by
  `assert marker in result.stderr` and `assert marker not in result.stdout`.
- **The `-v` help text wraps onto two rendered lines** at click's default 78-column width. It is
  one source line and one help string; no wording that keeps both the mode split and the log-file
  mention fits in the 61 columns click leaves after the option column.
- **No Dockerfile change, no new config key, no runtime warning** — D-24, D-39 and D-40 as
  written. `CMD ["serve"]` already selects the mode.

## Authentication Gates

None.

## Known Stubs

None.

## Threat Flags

None. The four threats in the plan's register were dispositioned as planned:

- **T-31-11** (`-v` leaking the Paperless `Authorization` header in serve mode) — *mitigated*.
  The `saneless`-logger `setLevel` line is untouched and the root logger keeps the configured
  level in both modes. Pinned by
  `test_stream_mode_leaves_non_saneless_loggers_at_the_configured_level` and by the surviving
  `test_serve_log_level_ignores_verbose`.
- **T-31-18** (uvicorn access lines now visible) — *accepted*, unchanged: `access_log=True` and
  `log_config=None` are byte-for-byte as they were.
- **T-31-19** (tracebacks discarded in serve mode) — *mitigated* by the plain formatter, pinned
  by two tests in `test_logging.py` and a parametrised pair in `test_cli.py`. Deviation 2
  strengthens the same mitigation: the operator is no longer told to restart to obtain a
  traceback they already have.
- **T-31-20** (CLI logging contract tampered with) — *mitigated*. Both new parameters have
  behaviour-preserving defaults, zero test lines were deleted, and
  `test_one_shot_command_still_attaches_the_file_handler` is the explicit guard.

No new network endpoint, auth path, file-access pattern or schema change was introduced. The one
trust-boundary movement — records leaving a `0600`-ish file for a process stream — is DLVR-04's
entire purpose and is dispositioned above.

## Commits

| Gate | Commit | Message |
|------|--------|---------|
| RED | `8ba1ede` | `test(31-03): add failing tests for the serve logging stream mode` |
| GREEN | `e93509d` | `feat(31-03): stream serve's logs to stderr with no log file` |
| RED | `eda19f4` | `test(31-03): add failing TestServeLogging and the D-34 regression guard` |
| GREEN | `73ef3ab` | `feat(31-03): serve streams its logs; one-shot commands keep the file handler` |
| — | `097b601` | `docs(31-03): state which logging keys apply in which mode` |

No REFACTOR commit: neither cycle left anything to clean up.

## TDD Gate Compliance

Both behaviour-adding tasks ran a full RED → GREEN cycle with the RED gate committed separately
and demonstrably failing first (Task 1: 16 passed / 6 failed on `TypeError: unexpected keyword
argument`; Task 2: 2 passed / 4 failed on the assertions the change makes true). Gate sequence in
`git log`: `test` → `feat` → `test` → `feat` → `docs`.

Task 1's RED commit was written against the planned `stream=True` spelling; the GREEN commit
reshaped those six tests to `log_file=None` along with the implementation, per deviation 1. The
RED gate itself is unaffected — the tests failed before the implementation existed and passed
after.

## Requirements Satisfied

- **DLVR-04** — `saneless serve` writes log records to stderr and creates no log file, so they
  appear in `docker logs`; the config keys that no longer apply say so in the reference.

## Self-Check

Files claimed created: none.

Files claimed modified — all present and changed in `b776a0f..HEAD`:
`src/saneless/logging_config.py`, `src/saneless/cli.py`, `src/saneless/config.py`,
`tests/test_logging.py`, `tests/test_cli.py`, `docs/reference/configuration.md`,
`docs/reference/cli-commands.md`, `docs/how-to/troubleshoot-a-failed-scan.md`.

Commits claimed: `8ba1ede`, `e93509d`, `eda19f4`, `73ef3ab`, `097b601` — all present in
`git log`.

## Self-Check: PASSED
