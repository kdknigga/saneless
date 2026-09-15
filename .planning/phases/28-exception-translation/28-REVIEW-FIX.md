---
phase: 28-exception-translation
fixed_at: 2026-09-15T21:44:01Z
review_path: .planning/phases/28-exception-translation/28-REVIEW.md
iteration: 1
findings_in_scope: 17
fixed: 17
skipped: 0
status: all_fixed
---

# Phase 28: Code Review Fix Report

**Fixed at:** 2026-09-15T21:44:01Z
**Source review:** .planning/phases/28-exception-translation/28-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 17 (CR-01, WR-01..WR-08, IN-01..IN-08; fix scope `all`)
- Fixed: 17
- Skipped: 0

Every fix was made directly on `autodev` (no worktree, per the orchestrator), one commit per
finding, with normal git hooks. Each behaviour fix has a test that failed before the change (checked
by stashing the source change and running the new test). After every commit these all passed:
`uv run pytest -m "not browser and not sane_hardware"` (1891 tests at baseline, 1930 at the end),
`ruff check`, `ruff format --check`, `ty check` and `pyrefly check src tests`. After the last
commit, `prek run --stage pre-push --all-files` and `mkdocs build --strict` also passed. No
templates, CSS or SANE backend code were touched, so the browser and sane_hardware suites were not
run.

Findings marked **fixed: requires human verification** change a condition or the handling of a
state. The tests pin the new behaviour, but a person should confirm the logic is what was intended.

## Fixed Issues

### CR-01: A traceback is printed to stderr without `-v` whenever the log file could not be opened

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/logging_config.py`, `src/saneless/cli.py`, `tests/test_cli.py`, `tests/test_logging.py`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** 96bc6f2
**Applied fix:** I fixed this in `configure_logging` rather than only in the CLI guard. That way no
record reaches the terminal with a traceback, whoever logs it: the guard, the flip prompt's
`logger.exception`, or the SANE close warning.
- The stderr fallback handler, used when the log file cannot be opened, now uses a
  `_TracebackFreeFormatter`. It renders the message but drops `exc_info`, `exc_text` and
  `stack_info`. The record itself keeps `exc_info`.
- With `-v`, the existing mirror handler renders the traceback once.
- The guard's failure record now includes `describe(exc)`, so the stripped line still says what
  failed.
- `_report_unexpected` adds "Run again with -v to see the traceback" when logging is ready but no
  file is attached and `-v` was not given.
- New tests use the real `configure_logging` with an unwritable `log_file` and cover exit 5 and
  exit 1. They assert that no traceback appears without `-v`, and that one does appear with it.

This also applies to `serve`: if its log file cannot be opened, its stderr log has no tracebacks
unless `-v` is given.

### WR-01: The "one line" guarantee is not enforced for third-party or unknown messages

**Files modified:** `src/saneless/exceptions.py`, `src/saneless/paperless.py`, `tests/test_exceptions.py`, `tests/test_cli.py`
**Commit:** afec03d
**Applied fix:**
- `describe()` now returns `" ".join(str(exc).split()) or type(exc).__name__`, so every wrapped
  message and the exit-5 line are one line.
- A message that is only whitespace falls back to the class name.
- `_one_line_reason`'s own whitespace collapse was redundant and is removed.

I did not add the optional 200-character cut on `_unexpected_line`. The full text is in the log, and
no locked decision asks for the cut.

### WR-02: A shutdown during pass A turns a real scan failure into a "restart", logged at INFO with no traceback

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/worker.py`, `tests/test_worker.py`
**Commit:** c8003a5
**Applied fix:**
- New helper `_ended_by_shutdown(exc, coordinator)`. It is true only for a `ScanCancelledError`
  whose coordinator's answer was claimed by shutdown.
- Both `_scan_job`'s first branch and `_failure_record` use it. A jam or an empty feeder in pass A
  after `stop()` is now recorded as ERROR with its own text and category, and logged with
  `exc_info`.
- The new worker test calls the real `stop()` while pass A is held, then has pass A raise a
  `ScanError`.

### WR-03: 3xx (and 1xx) upload responses are retried as transient and then fall back to the consume directory

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`, `docs/explanation/consume-directory-fallback.md`, `docs/how-to/troubleshoot-a-failed-scan.md`, `docs/explanation/architecture.md`
**Commit:** 067ed5c
**Applied fix:**
- Only `response.is_server_error` is retried now. Any other `HTTPStatusError` raises a
  `PaperlessError` at once, with no retry and no fallback, as the orchestrator directed.
- The message comes from the new `_not_accepted_message`:
  - A 4xx keeps its message.
  - A redirect with a Location header reads `Paperless redirected the upload (<status>) to
    <location>; check paperless.url`.
  - Any other status reads `Paperless did not accept the upload (<status>): <body>`.
- A new `_bounded_line` helper collapses and cuts text. It is shared with `_render_error_body`.
- `_back_off` now logs `_one_line_reason(exc)`, one line and without httpx's request URL.
- The docs now say that a redirect fails fast.

### WR-04: A malformed Paperless URL with a consume directory falls back on every scan, and the cause is never logged

**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** 2a0a5da
**Applied fix:**
- The `UnsupportedProtocol` branch logs `Paperless URL <url> cannot be used (<reason>); not
  retrying` at WARNING before it breaks out to the fallback.
- The troubleshooting page says that with a consume directory the scan is saved to the folder and
  the log names the URL.

I did not carry the reason into `UploadResult`. That would mean changing the result contract, and
the finding only says to "consider" it.

### WR-05: `poll_task` lets `httpx.DecodingError` escape the boundary and fails an already-accepted upload

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`
**Commit:** 389c30d
**Applied fix:**
- The poll loop catches `httpx.RequestError`, the base of `TransportError` and `DecodingError`, and
  keeps polling to the deadline (D-11). `last_transport_error` is typed to match.
- A `DecodingError` row was added to the deadline cases, plus a test where polling continues past a
  `DecodingError` to SUCCESS.

### WR-08: Error messages and job records embed the full Paperless base URL, including any userinfo credentials

**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`
**Commit:** 1028e2e
**Applied fix:** A probe showed the leak was wider than the review said. httpx's own INFO request
log line (`HTTP Request: POST http://u:p@host/...`) and `str(HTTPStatusError)` both carried the
password too. The fix:
- When `paperless.url` has userinfo, `__init__` passes it as `httpx.BasicAuth(username, password)`
  and gives httpx a base URL without it.
  - This is the same Basic auth httpx derived from the URL before. A probe confirmed the
    Authorization header is identical.
  - httpx's logs and exceptions no longer see the credentials.
- Every saneless message and log line uses `self._display_url`, built by `_without_userinfo`.
  - It is a textual cut through the last `@`, so a raw `@` or `/` in a password leaves no fragment.
  - It also covers scheme-less and invalid URLs.
  - A redirect Location is redacted the same way.
- Tests cover:
  - the exhausted-retry message
  - that Basic auth is still sent
  - that no log record at DEBUG, httpx's included, carries the password
  - the InvalidURL message
  - a scheme-less URL
  - a parametrised check of the redaction helper
  - a redirect target that carries credentials

### WR-06: "No scanner found" is exit 2 for `scan` but exit 1 for `auto-profiles`, and the docs contradict each other

**Files modified:** `src/saneless/cli.py`, `tests/test_cli.py`, `tests/test_deployment_config.py`, `docs/reference/cli-commands.md`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** 93d2efe
**Applied fix:**
- `auto-profiles` raises `ConfigError("No scanner found: auto-detection found no devices. Check
  what SANE can see with `saneless devices`")`. It goes through the guard and exits 2, per D-07.
- The command's exit table:
  - Exit 1 now lists the real causes: SANE failed listing devices, or could not open or read the
    capabilities.
  - Exit 2 now includes "no scanner found".
- The troubleshooting bullet covers both commands.
- The doc-truth test pins "no scanner" under 2, and not under 1, for `scan` and `auto-profiles`.

### WR-07: `serve` can exit 1 and 130, but the reference and the doc-truth test pin that it cannot

**Files modified:** `src/saneless/cli.py`, `tests/test_cli.py`, `tests/test_deployment_config.py`, `docs/reference/cli-commands.md`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** 67e6de6
**Applied fix:**
- `serve` turns a `ScanError` from `SaneBackend(...)` into `ConfigError("The web server could not
  start: <reason>")`, so it exits 2.
- A Ctrl-C before `uvicorn.run` still exits 130 through the guard, and this is now documented. D-03
  exempts only uvicorn's own graceful stop, so I kept 130 rather than turning it into 0.
- The `serve` table gains 130 and "SANE could not be initialised" under 2. The global note and the
  troubleshooting page now say that Ctrl-C exits 0 only once the web server is running.
- The doc-truth test pins the exact set for every command, with a corrected docstring:
  - scan {0,1,2,3,4,5,130}
  - devices {0,1,2,5,130}
  - jobs {0,2,5,130}
  - serve {0,2,3,5,130}
  - auto-profiles {0,1,2,5,130}
- New CLI tests cover the SANE init failure (exit 2) and Ctrl-C during start-up (exit 130).

### IN-01: Pass B's "No pages were scanned" is false after pass A scanned pages, and it discards pass A

**Files modified:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** c7d7e0c
**Applied fix:**
- An empty pass B now raises `ScanError("No back pages were scanned in pass B (pass A scanned N
  front page(s))")`.
- Pass A and simplex scans keep EXC-03's "No pages were scanned".
- The pass A pages are still lost. Keeping them depends on Phase 29's spooling, and a code comment
  says so.

### IN-02: The poll timeout names a stale transport error even when later polls succeeded

**Status:** fixed: requires human verification
**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`, `docs/explanation/consume-directory-fallback.md`
**Commit:** caca540
**Applied fix:**
- `last_transport_error` is reset to `None` once a poll response is read.
- The new test has one `ConnectError`, then PENDING polls until the deadline. The timeout names no
  last error and has no `__cause__`.
- The docstring and the explanation doc were updated.

### IN-03: `_read_config` catches only `ParseError`, but tomlkit raises other `TOMLKitError`s for invalid TOML

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** 3627162
**Applied fix:**
- `_read_config` now catches `TOMLKitError`.
- For a `ParseError`, the message gives the position once, as "at line L, column C", and tomlkit's
  own ` at line N col M` suffix is stripped.
- Other `TOMLKitError`s claim no position. For example, `KeyAlreadyPresent` renders as `Cannot
  update <file>: it is not valid TOML (Key "b" already exists.)`.

### IN-04: The "lost terminal" example is classified the opposite way from the docstrings

**Files modified:** `src/saneless/cli.py`, `src/saneless/pipeline.py`, `tests/test_cli.py`, `docs/how-to/troubleshoot-a-failed-scan.md`, `docs/reference/cli-commands.md`, `docs/how-to/cli-scripting.md`
**Commit:** e8e4732
**Applied fix:** Wording only. Behaviour is unchanged, per D-02/D-03 and the orchestrator.
- The `ClickFlipCoordinator` and `FlipCoordinator.abort_cause` docstrings, two test docstrings and
  three docs now say:
  - End of input, including a terminal that closes, is a cancel (130).
  - Only a read error at the prompt, such as an I/O error or undecodable input, fails with exit 1.

### IN-05: Task failure text from Paperless is collapsed to one line but not length-bounded

**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`
**Commit:** ddffe47
**Applied fix:**
- The task failure text goes through `_bounded_line` (200 characters plus an ellipsis). If that
  leaves nothing, it falls back to `_NO_FAILURE_MESSAGE`.
- The duplicate check still reads the full collapsed text, so a "duplicate of" past the cut still
  gets the check-before-rescanning hint.
- Tests cover the length cut, a duplicate past the cut, and failure text that is only whitespace.

### IN-06: A failing `rollback()` in `JobStore.__init__` masks the migration error as a raw sqlite3 exception

**Files modified:** `src/saneless/job.py`, `tests/test_job.py`
**Commit:** 6d48606
**Applied fix:**
- The rollback after a failed migration is wrapped in `contextlib.suppress(sqlite3.Error)`, and the
  connection is still closed.
- The test replaces the connection with one whose rollback raises. It asserts that the migration's
  own error becomes the `StorageError` (and its `__cause__`), and that close ran.

### IN-07: Pipeline start-up filesystem failures exit 5 as "a saneless bug"

**Files modified:** `src/saneless/pipeline.py`, `tests/test_pipeline.py`, `docs/how-to/troubleshoot-a-failed-scan.md`
**Commit:** ed9df32
**Applied fix:**
- New `_open_workspace(tmp_dir, min_free_mb)` wraps the `tmp_dir` mkdir, the `disk_usage`
  measurement (inside `_check_disk_space`) and `TemporaryDirectory` creation. Any `OSError` from
  them becomes `ConfigError("Could not prepare the working directory <tmp_dir>: <reason>")`, exit 2.
  - That matches `validate_settings_dirs` at start-up.
  - Insufficient space is still the existing `ScanError`.
- Extracting the helper also keeps `run_pipeline` under PLR0915.
- A parametrised test covers all three failing calls.
- A troubleshooting bullet was added under configuration errors.

### IN-08: `upload_document` claims to be a module boundary, but `pdf_path.open` can raise a raw `OSError`

**Files modified:** `src/saneless/paperless.py`, `tests/test_paperless.py`
**Commit:** 324554c
**Applied fix:**
- `_post_document` guards only the open. An `OSError` there becomes `PaperlessError("Could not read
  the PDF <path> to upload it: <strerror>")`, chained to the `OSError`, with no request made. The
  request itself still raises httpx types for the retry logic.
- The docstrings were updated.

---

_Fixed: 2026-09-15T21:44:01Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
