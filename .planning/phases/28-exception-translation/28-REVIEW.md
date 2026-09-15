---
phase: 28-exception-translation
reviewed: 2026-09-15T19:47:25Z
depth: deep
files_reviewed: 43
files_reviewed_list:
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/exceptions.py
  - src/saneless/job.py
  - src/saneless/logging_config.py
  - src/saneless/paperless.py
  - src/saneless/pdf.py
  - src/saneless/pipeline.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/vocabulary.py
  - src/saneless/web/static/app.css
  - src/saneless/web/templates/partials/history.html
  - src/saneless/web/templates/partials/status.html
  - src/saneless/worker.py
  - tests/fake_sane.py
  - tests/test_auto_profiles.py
  - tests/test_browser.py
  - tests/test_cli.py
  - tests/test_config.py
  - tests/test_deployment_config.py
  - tests/test_exceptions.py
  - tests/test_job.py
  - tests/test_logging.py
  - tests/test_outcomes_e2e.py
  - tests/test_paperless.py
  - tests/test_pdf.py
  - tests/test_pipeline.py
  - tests/test_scanner.py
  - tests/test_vocabulary.py
  - tests/test_web_state_rendering.py
  - tests/test_worker.py
  - docs/explanation/architecture.md
  - docs/explanation/consume-directory-fallback.md
  - docs/explanation/empty-page-detection.md
  - docs/how-to/cli-scripting.md
  - docs/how-to/install-bare-metal.md
  - docs/how-to/scanner-host-discovery.md
  - docs/how-to/set-up-adf-duplex.md
  - docs/how-to/troubleshoot-a-failed-scan.md
  - docs/reference/cli-commands.md
  - docs/reference/web-api.md
  - mkdocs.yml
findings:
  critical: 1
  warning: 8
  info: 8
  total: 17
status: issues_found
---

# Phase 28: Code Review Report

**Reviewed:** 2026-09-15T19:47:25Z
**Depth:** deep
**Files Reviewed:** 43
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

I reviewed the phase diff `45d1aaf..HEAD` against CONTEXT D-01..D-13 and their amendments. I
traced the CLI guard, the worker's three endings, the Paperless retry, fallback and poll paths,
the SANE, img2pdf and TOML boundaries, and every doc sentence the phase changed. The non-browser
suite passes (1887 tests). Where a finding's behaviour could be tested, I checked it with a probe
script against the real code rather than inferring it.

What holds up:
- **Guard order.** click's own exceptions are re-raised first, then KeyboardInterrupt and
  ScanCancelledError, then StorageError, then SanelessError, then Exception. The order is right.
- **Flip prompt thread safety.** `ClickFlipCoordinator.abort_cause` is safe. The claim and the
  cause are set under one lock, and the reader takes the same lock, so it cannot see the claim
  without the cause.
- **Degraded health.** A CANCELLED job never counts toward it.
- **Config errors.** The TOML and UTF-8 renderers never echo `doc`/`object` content.

What fails:
- **CR-01: tracebacks still reach the user.** When the log file cannot be written, every failure
  prints a full traceback on stderr without `-v`. This breaks EXC-02 and the guard's own
  docstring.
- **One-line guarantee.** It is not enforced for unknown exceptions (WR-01).
- **Worker shutdown.** A shutdown during pass A hides a real job failure (WR-02).
- **Paperless client.** Three problems: redirects are retried and fall back as if they were
  transient (WR-03), a malformed URL falls back silently with no logged cause (WR-04), and one
  httpx error type escapes `poll_task` (WR-05).
- **Exit codes.** The documentation and the doc-truth test pin claims the code contradicts
  (WR-06, WR-07).

## Critical Issues

### CR-01: A traceback is printed to stderr without `-v` whenever the log file could not be opened

**File:** `src/saneless/cli.py:292-334` (with `src/saneless/logging_config.py:70-75`)
**Issue:** `configure_logging` falls back to a root `StreamHandler(sys.stderr)` when the log file
cannot be opened. This is a documented, supported path: "An unwritable log_file is not a
failure... the command runs". `_logging_ready(ctx)` is True in that case too. The guard then
calls `logger.error(..., exc_info=exc)` from `_log_failure` for every classified failure
(ScanError, PaperlessError, PdfError, StorageError, post-load ConfigError), and from
`_report_unexpected` for exit 5. Both reach the stderr fallback handler, so the user sees the
full traceback before the "one line".

I reproduced it with `log_file="/proc/nope/saneless.log"` and a command that raises:

```
2026-09-15 ... ERROR    saneless.cli Unexpected error in saneless devices
Traceback (most recent call last):
  File ".../cli.py", line 376, in invoke
  ...
RuntimeError: kaboom
Unexpected error (RuntimeError): kaboom
```

This breaks EXC-02 ("no user ever sees a traceback"), D-06 (the traceback goes to stderr only
with `-v`) and the `_GuardedGroup` docstring. The existing test
`test_unexpected_error_exits_5_without_log_file_when_not_attached` patches `configure_logging` to
a lambda that attaches no handler, so it never exercises the real fallback handler and cannot
catch this.

**Fix:** Log the traceback only when it goes to a file, or when the user asked for it:

```python
def _log_failure(ctx: click.Context, exc: Exception) -> None:
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    if not _logging_ready(ctx):
        return
    if obj.get("log_file") or obj.get("verbose"):
        logger.error("saneless %s failed", ctx.invoked_subcommand, exc_info=exc)
    else:
        # stderr fallback: the record would print the traceback to the user
        logger.error("saneless %s failed: %s", ctx.invoked_subcommand, describe(exc))
```

Apply the same rule in `_report_unexpected`. When no file is attached and `-v` was not given,
append "Run again with -v to see the traceback". Add a test that uses the real
`configure_logging` with an unwritable `log_file` and asserts `"Traceback" not in result.stderr`.

## Warnings

### WR-01: The "one line" guarantee is not enforced for third-party or unknown messages

**File:** `src/saneless/exceptions.py:76-92`, `src/saneless/cli.py:245-256`
**Issue:** `describe()` returns `str(exc)` verbatim, and `_unexpected_line` embeds it verbatim. An
exception whose message contains a newline therefore prints several lines on the D-06 path. A
probe with `RuntimeError("kaboom\nsecond line")` printed `Unexpected error (RuntimeError): kaboom`
followed by `second line`.

Exit 5 exists precisely for exceptions whose text saneless does not control. For example,
pydantic's `ValidationError.__str__` is multi-line and embeds `input_value=...`, which Phase 27
Pitfall 1 flagged as a possible token carrier. Every wrapped message (SANE, img2pdf, Pillow,
OSError) also flows through `describe()` into `job.error` and CLI lines, with no whitespace
collapse.

`paperless._one_line_reason` already does the right thing, but only inside `paperless.py`.
**Fix:** Collapse whitespace once, in `describe`, so every boundary inherits it:

```python
def describe(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__
```

Then `_one_line_reason`'s own `" ".join(describe(exc).split())` becomes redundant. Consider also
truncating `_unexpected_line`'s message, for example to 200 characters like
`_MAX_BODY_LINE_CHARS`.

### WR-02: A shutdown during pass A turns a real scan failure into a "restart", logged at INFO with no traceback

**File:** `src/saneless/worker.py:1337-1341`
**Issue:** `stop()` calls `coordinator.abort_for_shutdown()` whenever a manual-duplex job is
current, including while pass A is still scanning (`worker.py:492-497`, "one still in pass A
aborts the moment it asks"). That claim sets `aborted_by_shutdown=True` before the pipeline ever
reaches the flip. If pass A then raises a genuine failure, the first branch still matches because
it tests only `coordinator.aborted_by_shutdown`, not the exception type. Failures that match
include a jam `ScanError`, `FeederEmptyError`, "No pages were scanned", or a `ConfigError` from
`_resolve_device`. The job is recorded as `RESTART_REASON`, its real text and category are
thrown away, and it is logged with `logger.info(...)` and no `exc_info`.

That violates EXC-05 ("every job failure is logged with exc_info") and D-01's rule that the three
endings stay explicitly distinct. It also contradicts `_failure_record`'s docstring ("a jam...
inside the shutdown join window keeps its own text and category").
**Fix:** Treat the ending as a shutdown only when the pipeline actually came back through the
aborted flip:

```python
if (
    isinstance(exc, ScanCancelledError)
    and coordinator is not None
    and coordinator.aborted_by_shutdown
):
    ...  # RESTART_REASON, INFO
elif isinstance(exc, ScanCancelledError):
    ...  # CANCELLED
else:
    ...  # ERROR + logger.exception
```

Add a worker test that calls `stop()` during pass A, has pass A raise `ScanError`, and asserts
ERROR with the scanner's text plus an `exc_info` record.

### WR-03: 3xx (and 1xx) upload responses are retried as transient and then fall back to the consume directory

**File:** `src/saneless/paperless.py:452-461`
**Issue:** `response.raise_for_status()` raises `HTTPStatusError` for every non-2xx status,
redirects included (httpx 0.28.1, verified). The handler rejects only
`400 <= status < 500`, so everything else is treated as a retryable server error. A plain
`http://` URL behind a proxy that redirects to `https://` is a very common misconfiguration.
With it, the upload is attempted 3 times with 1 s and 2 s sleeps, then silently falls back to the
consume directory, or fails with `failed after 3 attempts: 302 Found: (empty response body)`.
Retrying a permanent redirect cannot help.

The class docstring, `consume-directory-fallback.md` and the troubleshooting page all say only
"a 5xx response" is retried. `_back_off` also logs `describe(exc)`, which for `HTTPStatusError`
is three lines (reproduced).
**Fix:** Retry only `status >= 500`. Fail fast on any other status, naming the redirect target
so the operator can fix `paperless.url`:

```python
status = exc.response.status_code
if status < 500:
    location = exc.response.headers.get("location")
    hint = f" (redirected to {location}; check paperless.url)" if location else ""
    msg = f"Paperless rejected the upload ({status} {exc.response.reason_phrase}){hint}: {_render_error_body(exc.response)}"
    raise PaperlessError(msg) from exc
```

Also log with `_one_line_reason(exc)` in `_back_off`.

### WR-04: A malformed Paperless URL with a consume directory falls back on every scan, and the cause is never logged

**File:** `src/saneless/paperless.py:462-465, 479-480, 613`
**Issue:** On `httpx.UnsupportedProtocol` the loop breaks without calling `_back_off`, which is
the only place a failed attempt is logged. `_fall_back_to_consume_dir` then logs only
"Upload failed; copied PDF to ...". Every scan ends `FALLBACK` ("Saved to folder") with a warning
about metadata, while the reason (no `http://` scheme) appears nowhere: not in the job, the CLI
output (exit 0) or the log. The troubleshooting page tells the user to "Fix the URL in the
config", but nothing ever tells them the URL is the problem.
**Fix:** Log the fast-fail cause before falling back:

```python
except httpx.UnsupportedProtocol as exc:
    logger.warning(
        "Paperless URL %s cannot be used (%s); not retrying",
        self._base_url, _one_line_reason(exc),
    )
    last_error = exc
    fast_fail = True
    break
```

Consider also carrying the reason into the `UploadResult` / FALLBACK warning.

### WR-05: `poll_task` lets `httpx.DecodingError` escape the boundary and fails an already-accepted upload

**File:** `src/saneless/paperless.py:725-741`
**Issue:** The poll catches only `httpx.TransportError`. `httpx.DecodingError` is a
`RequestError` and not a `TransportError`. It is raised from `client.get` when a response body
cannot be decoded, for example a proxy sending `Content-Encoding: gzip` with a corrupt body. It
escapes `poll_task` as a raw httpx type. A probe confirmed it:
`httpx.DecodingError: Error -3 while decompressing data`.

This breaks EXC-01 for the module. It also fails a job whose upload Paperless already accepted,
which is exactly the rescan-duplicate outcome D-11 exists to prevent. The pipeline's
`_preserving` guard happens to rewrap it as `PaperlessError`, but only because the PDF still
exists at that point.
**Fix:** Catch `httpx.RequestError` (the base of `TransportError`, `DecodingError` and
`TooManyRedirects`) in the poll loop, so any request-level failure keeps polling to the
deadline. Or add an explicit `except httpx.HTTPError` that wraps into `PaperlessError` naming the
task id. Add a test row for `DecodingError`.

### WR-06: "No scanner found" is exit 2 for `scan` but exit 1 for `auto-profiles`, and the docs contradict each other

**File:** `src/saneless/cli.py:832-836`, `docs/reference/cli-commands.md:177`,
`docs/how-to/troubleshoot-a-failed-scan.md:73`
**Issue:** D-07 files "No scanner found" under exit 2 (setup error) and requires the table to
apply "uniformly to every command where the failure can occur". The two commands disagree:

- **`scan`:** raises `ConfigError` from `_resolve_device`, so exit 2.
- **`auto-profiles`:** prints "No scanners found." and calls `ctx.exit(ExitCode.SCAN)`, so exit 1.

The troubleshooting page lists "No scanner found" only under exit 2, so a user of
`auto-profiles` who gets exit 1 is sent to the wrong section.

The `auto-profiles` exit table also lists 1 as only "No scanners found". It omits the more likely
exit-1 causes: `get_capabilities` / `_open_device` raising `ScanError` for a configured device
that cannot be opened.
**Fix:** Make `auto-profiles` raise
`ConfigError("No scanners found: ...")`, which exits 2 through the guard, and update its table.
Or keep exit 1 and move the troubleshooting bullet and D-07 wording to match. Either way, list
"SANE could not open or read the device" under `auto-profiles` exit 1.

### WR-07: `serve` can exit 1 and 130, but the reference and the doc-truth test pin that it cannot

**File:** `src/saneless/cli.py:732-738`, `docs/reference/cli-commands.md:150-156`,
`tests/test_deployment_config.py:427`
**Issue:** `serve` constructs `SaneBackend(host=...)`, which wraps a `sane.init()` failure as
`ScanError`, so exit 1 is reachable. A Ctrl-C before `uvicorn.run` (during settings load,
`SaneBackend`, `create_app` or the port probe) reaches the guard's `KeyboardInterrupt` clause and
exits 130. The reference table documents `serve` as `{0, 2, 3, 5}`. The doc-truth test asserts
exactly that set, with the justification "`serve` has no 1 (it scans nothing itself) and no 130".

The test therefore pins a false claim. The D-07 amendment also says "can't start" failures on
`serve` exit 2, and an init failure is one.
**Fix:** In `serve`, translate start-up `ScanError` into
`ConfigError` ("The web server could not start: <reason>"), so it exits 2 as D-07 intends.
Either document 130 for an interrupted start-up or catch `KeyboardInterrupt` before
`uvicorn.run` and exit 0. Then correct the test's expected set and docstring to match the code
rather than the intent.

### WR-08: Error messages and job records embed the full Paperless base URL, including any userinfo credentials

**File:** `src/saneless/paperless.py:391, 471, 488, 491, 554, 793, 909`
**Issue:** D-08 names "Paperless base URL" as an identifier and forbids only the token. A
`paperless.url` of the form `https://user:password@paperless.example/` is valid, and httpx sends
it as Basic auth, a common setup behind an authenticating reverse proxy. That password is
interpolated verbatim into:

- **`PaperlessError` messages:** "Could not reach Paperless at ...", "failed after N attempts",
  "is not valid".
- **The places those messages go:** `job.error` (persisted and rendered in the unauthenticated
  web status area), CLI stderr, and the log.

**Fix:** Render the URL with its userinfo stripped once, in `__init__`, and use that
for every message:

```python
parsed = httpx.URL(self._base_url)  # inside the InvalidURL try
self._display_url = str(parsed.copy_with(username=None, password=None))
```

Inside the `InvalidURL` branch, fall back to a regex that strips `//[^/@]*@`.

## Info

### IN-01: Pass B's "No pages were scanned" is false after pass A scanned pages, and it discards pass A

**File:** `src/saneless/pipeline.py:1038-1040`
**Issue:** `_require_pages(back_batch)` raises "No pages were scanned" even though pass A just
produced N pages, and those pages are dropped. With the real SANE backend this is mostly
pre-empted, because an empty feeder raises `FeederEmptyError` first. Any other backend reaches
it, though, and the message contradicts `_finish_duplex_mismatch`'s own rationale against turning
a recoverable duplex anomaly into data loss.
**Fix:** Use a pass-specific message, for example "Pass B scanned no pages (pass A scanned N)".
Consider routing an empty back pass to the mismatch recovery for fronts only, once Phase 29's
spooling exists.

### IN-02: The poll timeout names a stale transport error even when later polls succeeded

**File:** `src/saneless/paperless.py:731-753`
**Issue:** `last_transport_error` is never cleared after a successful response. A single blip in
the first second of a 300 s poll that otherwise just waited for a slow task still ends with
"...; last error: ConnectError", which suggests the network caused the timeout.
**Fix:** Set `last_transport_error = None` in the `else:` branch after a response is read.

### IN-03: `_read_config` catches only `ParseError`, but tomlkit raises other `TOMLKitError`s for invalid TOML

**File:** `src/saneless/auto_profiles.py:652-661`
**Issue:** `tomlkit.parse("[a]\nb=1\n[a.b]\nc=1\n")` raises `KeyAlreadyPresent`, which is a
`TOMLKitError` and not a `ParseError` (verified). The tomllib load in `_load_cli_settings`
normally rejects such a file first, so the leak is reachable only if the file changes between
load and write. The message also repeats the position: "at line 2, column 0 (Key "a" already
exists. at line 2 col 0)".
**Fix:** Catch `tomlkit.exceptions.TOMLKitError`. Render `exc.line`/`exc.col` only when present,
and use the message without tomlkit's own position suffix.

### IN-04: The "lost terminal" example is classified the opposite way from the docstrings

**File:** `src/saneless/cli.py:114-118`, `docs/reference/cli-commands.md:66`
**Issue:** The docstring gives "a lost terminal" as an example of a broken prompt that exits 1.
A terminal that goes away normally shows up as EOF on read, which `click.confirm` turns into
`click.Abort`. That is classified as an operator cancel and exits 130. Only a non-EOF exception
(for example a `UnicodeDecodeError`) takes the failure path.
**Fix:** Reword both to "a read error at the prompt (for example undecodable input)". Drop "lost
terminal", or state that EOF, including a closed terminal, counts as a cancel under D-02.

### IN-05: Task failure text from Paperless is collapsed to one line but not length-bounded

**File:** `src/saneless/paperless.py:806-810`
**Issue:** `_render_error_body` caps HTTP bodies at `_MAX_BODY_LINE_CHARS` (T-23-16), but the
task `result` / `error_message` text goes into `job.error` and the CLI line uncapped. Paperless
failure results can embed long OCR or consumer exception output.
**Fix:** Apply the same cut, with an ellipsis, to `failure` before building `msg`.

### IN-06: A failing `rollback()` in `JobStore.__init__` masks the migration error as a raw sqlite3 exception

**File:** `src/saneless/job.py:610-618`
**Issue:** If `self._conn.rollback()` raises `sqlite3.Error`, for example a disk I/O error on the
same broken file, the `StorageError` translation below it never runs. The raw sqlite3 error then
exits 5 ("a saneless bug") instead of exit 2.
**Fix:** Wrap the rollback in `contextlib.suppress(sqlite3.Error)` before `close()`.

### IN-07: Pipeline start-up filesystem failures exit 5 as "a saneless bug"

**File:** `src/saneless/pipeline.py:1236-1239`
**Issue:** Three calls can raise a raw `OSError`: `Path(tmp_dir).mkdir`, `shutil.disk_usage` in
`_check_disk_space`, and `tempfile.TemporaryDirectory(dir=tmp_dir)`. Each would fail on a full
disk or a `tmp_dir` removed since start-up. The error classifies `UNKNOWN`, so the CLI exits 5
and the troubleshooting page calls it a bug, while the same disk-full condition one step later
exits 4. It is rare, because `validate_settings_dirs` ran at start-up.
**Fix:** Wrap these three calls and raise `ScanError` or `ConfigError` naming `tmp_dir`.

### IN-08: `upload_document` claims to be a module boundary, but `pdf_path.open` can raise a raw `OSError`

**File:** `src/saneless/paperless.py:328-332, 543`
**Issue:** The class docstring says "whatever goes wrong there leaves as a `PaperlessError`". An
`OSError` from opening the PDF propagates untranslated. `_preserving` rewraps it in practice,
except for `FileNotFoundError`, where no destination survives and the original is re-raised as
exit 5.
**Fix:** Catch `OSError` around `pdf_path.open` in `_post_document` and raise `PaperlessError`
naming the path, or narrow the docstring's claim.

---

_Reviewed: 2026-09-15T19:47:25Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
