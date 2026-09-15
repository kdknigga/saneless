# Phase 28: Exception Translation - Research

**Researched:** 2026-09-15
**Domain:** Exception boundaries (python-sane, httpx, img2pdf/Pillow, tomllib/tomlkit), click CLI exit handling, job-state vocabulary (new terminal `CANCELLED`), docs
**Confidence:** HIGH (almost every claim below was executed in the project venv against the pinned versions)

## Summary

This phase is mechanical, but several of the "obvious" catch sites are wrong in ways only a probe
reveals. The most load-bearing measured facts: `httpx.InvalidURL` is raised by the
`httpx.Client(base_url=...)` **constructor** (inside `PaperlessClient.__init__`), not by a request;
an empty or scheme-less `paperless.url` raises `httpx.UnsupportedProtocol` on the first request;
img2pdf raises its seven named classes (all direct `Exception` subclasses, no common base) **and**
bare `Exception`, `TypeError` and `ValueError`; Pillow raises `SystemError` (not `OSError`) saving a
0x0 image; a missing `libsane.so` is a plain `ImportError`, while an absent package is
`ModuleNotFoundError`; `tomllib.TOMLDecodeError` escapes `Settings(...)` unchanged, and so do
`PermissionError` and `UnicodeDecodeError` for an unreadable or non-UTF-8 config; click's `Exit` and
`Abort` are `RuntimeError` subclasses, so a broad last-resort `except Exception` must re-raise click's
own control-flow exceptions first; and `uvicorn.run` already swallows `KeyboardInterrupt` (Ctrl-C on
`serve` exits 0 today) but exits **3** on a startup failure, which collides with saneless's exit 3.

EXC-03 and Phase 24 D-03 do **not** genuinely conflict. The SANE backend can never hand the pipeline
an empty batch: an ADF that feeds nothing raises `FeederEmptyError("No paper detected in feeder")`
inside the backend, an ADF whose fed pages were all unreadable raises its own distinct `ScanError`,
and the flatbed path returns exactly one page or raises. "No pages were scanned" is therefore a
pipeline-boundary precondition that only another `ScannerBackend` (or a test fake) can reach. It
replaces today's misleading "All pages were detected as empty" (detection on) and the leaked
img2pdf `ValueError` (detection off, or the duplex-mismatch path). The truthful feeder message stays
where it is.

**Primary recommendation:** Translate at each call site with `raise <SanelessType>(msg) from exc`,
one shared `describe(exc)` helper for the "`str(exc)` or class name" rule, a single `ExitCode`
`IntEnum` in `vocabulary.py`, and a `click.Group` subclass whose `invoke` is the one place every CLI
command's failures become one line plus an exit code. Make `ScanCancelledError` a direct
`SanelessError` subclass, and let the pipeline ask the flip coordinator whether an `ABORTED` came
from a broken prompt.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### Carried forward (already decided, do not re-litigate)
- **Phase 25 D-15/D-16:** abort at the flip prompt already *works* on both front ends; this phase
  only classifies it. A flip answer is final.
- **Phase 25 D-09:** `FlipOutcome` has exactly three members (`CONTINUED`, `ABORTED`,
  `TIMED_OUT`). Do not add a fourth (see D-02's WR-08 note).
- **Phase 24 D-03:** scanner messages are already truthful (`_FEEDER_EMPTY_MESSAGE`, the
  feeder-string → `FeederEmptyError` rule). Phase 28 makes them well-typed, not reworded.
- **Phase 26 D-01/D-04/D-10/D-15:** the web layer already renders every error through one
  renderer; the worker guard logs loop failures with `exc_info`; exception classes are *named,
  never interpreted*. `RESTART_REASON` for a shutdown-claimed abort (WR-06) stays an ERROR.
- **Phase 27 D-10/D-14:** config errors print under a `Configuration error in <file>:` header, exit
  2, and never echo values. The TOML syntax-error path was left for this phase (`cli.py:233-238`).
- **Phase 27 CFG-10:** `saneless <cmd> --help` works with no valid config. It must also work with no
  `python-sane` (D-05).
- **Exit codes 1 / 2 / 3 keep their documented meanings** (`docs/how-to/cli-scripting.md:69-78`,
  `docs/reference/cli-commands.md:31-38`). This phase only adds codes.

#### Cancelled jobs (EXC-04, N-08)
- **D-01: a new terminal `JobState.CANCELLED`.** Not `ERROR` with a category. It joins
  `TERMINAL_STATES`, gets `state_label` → `"Cancelled"`, and every `match`/`assert_never` consumer
  handles it (the type checkers find them). It renders with **neutral, muted styling** in the
  status partial and history table, not the error red, in both light and dark themes (following
  the 23.1 UI-SPEC status-token convention, proven with Playwright). The pipeline signals it with a
  dedicated exception (N-08's `ScanCancelledError`). The worker records the job `CANCELLED`, logs
  it at **INFO without a traceback** (a cancel is not a failure, so EXC-05 does not apply), and
  never counts it toward degraded health. `state` is stored as TEXT with no CHECK constraint, so no
  schema migration is expected; the planner confirms that.
  - The worker today uses `category is None` to mean "ended by shutdown"
    (`worker.py:1325-1334`). CANCELLED must not reuse that signal by accident. Keep the three
    endings (failure, cancel, shutdown) explicitly distinct.
  - The web JSON/status surface and `docs/reference/web-api.md` gain the `CANCELLED` state. Polling
    stops on it, and the Scan button re-enables on it, because it is terminal.
- **D-02: only an explicit operator abort is a cancellation.** CANCELLED covers the web Abort
  button, answering `n` at the CLI prompt, and Ctrl-C or Ctrl-D/EOF at the prompt. These stay
  **ERROR**:
  - a flip-wait **timeout** (`FlipOutcome.TIMED_OUT`), since nobody chose to stop;
  - a **terminal read failure** at the prompt (WR-08, `cli.py:148-157`). It settles `ABORTED`
    today, so it needs a way to be told apart from a real abort *without* a fourth `FlipOutcome`.
    Precedent: `WorkerFlipCoordinator.aborted_by_shutdown` (`worker.py:962`) marks *why* an
    `ABORTED` happened on the coordinator. The mechanism is the planner's.
  - a **shutdown-claimed** abort keeps Phase 26's `RESTART_REASON` ERROR.
- **D-03: CLI exit 130 for every cancel.** `n`, Ctrl-C and Ctrl-D at the flip prompt print one line
  (e.g. `Scan cancelled at the flip prompt`) and exit 130. A `KeyboardInterrupt` anywhere else in a
  one-shot CLI command (mid-scan, mid-upload, `devices`, `auto-profiles`) also prints one line and
  exits 130, with no traceback. **Exception:** Ctrl-C on `saneless serve` is uvicorn's normal
  graceful stop and keeps today's exit status; do not turn a normal server stop into 130.

#### PdfError and exit codes (EXC-02)
- **D-04: `PdfError(SanelessError)` is a sibling of `ScanError`, with its own `ErrorCategory` and
  exit code 4.** The job record stops saying the scanner failed when the disk was full.
  `classify_error` gains the new branch (ordered `isinstance` chain, `vocabulary.py:661`) and
  `user_message` gains the total-match entry. Everything `assemble_pdf` can raise becomes
  `PdfError` with the original message: all seven img2pdf classes (`AlphaChannelError`,
  `ExifOrientationError`, `ImageOpenError`, `JpegColorspaceError`, `NegativeDimensionError`,
  `PdfTooLargeError`, `UnsupportedColorspaceError`, verified against the installed img2pdf), its
  empty-list `ValueError`, Pillow `OSError` from the PNG save or PDF write, and today's bare
  `RuntimeError("img2pdf.convert returned None")` (`pdf.py:197-198`).
- **D-05: a missing `python-sane` makes every SANE command refuse with exit 2.** `scan`,
  `devices`, `auto-profiles` and `serve` check before doing anything else. They print one line with
  the import's own reason and an install hint (`libsane-dev` / `sane-backends-devel`, pointing at
  Install on Bare Metal) and exit 2 ("can't start, fix your setup"). `serve` does not come up
  half-working. The check covers both `ModuleNotFoundError` (package absent) and `ImportError` (the
  `_sane` extension can't load `libsane`). Commands that don't need SANE (`jobs`) are unaffected,
  and `--help` never triggers the import. python-sane is a mandatory dependency, so fail loudly at
  the start, not mid-scan.
- **D-06: an exception that is not a saneless type exits 5.** A last-resort handler around every
  CLI command prints `Unexpected error (<ExceptionType>): <message>. Full details in <log_file>`,
  logs the traceback with `exc_info`, and exits **5**. With `-v` the traceback is also mirrored to
  stderr, since `-v` already mirrors to stderr. If the failure happens before logging is configured,
  the line still prints; where the traceback goes then is the planner's call.
- **D-07: the complete exit-code table**, applied uniformly to every command where the failure can
  occur:

  | Code | Meaning |
  |------|---------|
  | 0 | Success |
  | 1 | Scan error: `ScanError` / `FeederEmptyError`, flip wait timed out, prompt read failure |
  | 2 | Configuration, profile or setup error: `ConfigError` (including "No scanner found" raised mid-scan, which today escapes `scan` as a traceback), TOML syntax error, python-sane not importable |
  | 3 | Paperless error: `PaperlessError` |
  | 4 | PDF assembly error: `PdfError` |
  | 5 | Unexpected error (a saneless bug) |
  | 130 | Cancelled by the operator |

  The codes live in one place in code (the house pattern is a total enum with `match` +
  `assert_never`), not as scattered literals.

#### Wrapped message shape and retries (EXC-01)
- **D-08: one-line messages read `<what saneless was doing, with identifiers>: <original message>`.**
  Examples: `Could not set mode to 'Lineart' on epson2:libusb:001:004: Invalid argument`,
  `Could not reach Paperless at http://paperless:8000: [Errno 111] Connection refused`. The
  original `str(exc)` appears verbatim, and `raise … from exc` keeps the chain for logs. When
  `str(exc)` is empty (httpx `ReadTimeout` can be), use the exception class name in its place.
  Identifiers are those known at the call site (device name, option name and value, Paperless base
  URL, file path). **Never the token** (CFG-05). Scanner feeder-string mapping to
  `FeederEmptyError` keeps Phase 24's rule.
- **D-09: Paperless error bodies are reduced to one line.** Prefer Paperless's JSON error text: DRF
  `detail`, else the first field error rendered as `field: message`. Otherwise collapse whitespace
  and cut to about 200 characters with an ellipsis. The full body is logged at DEBUG. Example:
  `Paperless rejected the upload (400 Bad Request): title: This field may not be blank.` The
  existing `_truncated_body` (`paperless.py:126-142`) produces a multi-line, length-marked form; the
  planner reconciles it with this rule so there is one body renderer. Today's
  `f"Paperless rejected upload: {status} {exc.response.text}"` (`paperless.py:300-306`) goes.
- **D-10: upload retries cover every transient `httpx.TransportError`**, with the existing backoff,
  then the consume-directory fallback, as `ReadTimeout` already does. Today a `ReadError`,
  `WriteError` or `RemoteProtocolError` (a reverse proxy closing the connection) escapes raw after
  one attempt and skips the fallback. **A malformed URL fails at once with no retry.**
  `httpx.UnsupportedProtocol` (which *is* a `TransportError` subclass) and `httpx.InvalidURL` (which
  is not) raise a `PaperlessError` immediately. Whether that path still takes the consume-dir
  fallback is the planner's call, leaning towards yes (never lose a scan; no retry ≠ no fallback).
  - Accepted risk: when Paperless received the upload but the response was lost, the retry's task
    fails as Paperless's checksum **duplicate**. That failure message must say the document may
    already be in Paperless, so the user checks before rescanning.
  - The exhausted-retry message counts attempts truthfully: `Upload failed after 3 attempts`, not
    "retries" (M-17's `max_retries` → `max_attempts` naming; whether the attribute is renamed is the
    planner's call).
- **D-11: a transport error while polling an accepted task keeps polling until the deadline**
  (`poll_task`, `paperless.py:385`). It does not fail the job, because the upload already succeeded
  and a rescan would be a duplicate (M-17). The deadline stays monotonic (OUTC-07). If the deadline
  expires, the timeout message names the last transport error. Non-200 poll responses keep OUTC-07's
  immediate raise. `get_tags` / `get_correspondents` wrap into `PaperlessError` too.
- **D-12: TOML syntax errors are `ConfigError`s under Phase 27's header**, with line and column:
  `Configuration error in /etc/saneless/config.toml:` then `  line 12, column 5: <tomllib message>`.
  Exit 2. The generic `except Exception` in `_load_cli_settings` (`cli.py:233-238`) then narrows
  or disappears. `tomllib` / `tomlkit` parse failures on the `auto-profiles` write path
  (`auto_profiles.py:640-720`) become `ConfigError` too.

#### Zero pages (EXC-03), locked by requirement
- "No pages were scanned" when the scanner returns zero pages. The precondition is checked at the
  boundary before `assemble_pdf`, so img2pdf's `ValueError: Unable to process empty list` can never
  be reached (N-06). "All pages were blank" is used only when empty-page detection removed every
  page; it replaces today's `"All pages were detected as empty"` (`pipeline.py:505`).
- **The planner must reconcile this with Phase 24 D-03:** the ADF path already raises
  `FeederEmptyError("No paper detected in feeder")` at zero pages (`sane_backend.py:776`), and that
  message was made truthful deliberately. Decide, with research, which paths reach "No pages were
  scanned" (flatbed, a batch whose pages all failed integrity, a manual-duplex pass) and keep the
  feeder-empty message where it is the true cause. If EXC-03's wording and D-03 genuinely conflict,
  raise it with the user rather than silently regressing either one.

#### Worker failure logging (EXC-05), locked by requirement
- Every job failure is logged with `exc_info` (`logger.exception` or `exc_info=True`), for
  classified categories as well as `UNKNOWN`. It replaces `logger.error("Job %s failed (%s): %s")`
  (`worker.py:1335-1337`). A cancel (D-01) logs at INFO without a traceback. A shutdown ending
  keeps its INFO line.

#### Troubleshooting docs
- **D-13: a new how-to, `docs/how-to/troubleshoot-a-failed-scan.md`, organised by symptom.** It is
  added to the How-To Guides nav in `mkdocs.yml`. Its structure: by exit code first, then by what
  you see (scanner errors, Paperless errors, PDF assembly errors, configuration, python-sane
  missing, cancelled, and unexpected error 5, which says to attach the log file when reporting a
  bug). The two exit-code tables (`docs/how-to/cli-scripting.md`,
  `docs/reference/cli-commands.md` for every command) gain rows 4, 5 and 130 and link to the page.
  The existing `## Troubleshooting` sections in `install-bare-metal.md` and
  `scanner-host-discovery.md` stay and cross-link. **A doc-truth test** (Phase 27 precedent) pins
  the documented exit-code table to the code's exit-code definition. The page is written by symptom
  rather than quoting exact message strings, because Phase 30 (APPL-04) will reword them.

### Claude's Discretion
The user accepted these defaults. The planner may refine them but should not reverse them without
cause.
- **`ScanCancelledError`'s place in the hierarchy.** N-08 suggests `ScanCancelledError(ScanError)`.
  If it subclasses `ScanError`, every `except ScanError` and `classify_error` must test the narrower
  class first (the `FeederEmptyError` precedent), with a test proving a cancel never exits 1 or
  records ERROR. A direct `SanelessError` subclass avoids that trap. Planner's call.
- **Name of the PDF `ErrorCategory`** (e.g. `ASSEMBLY` or `PDF`) and its `user_message` sentence.
- **Where the python-sane import check lives**: one helper shared by the four commands. It must not
  run on `--help` and must not break the lazy `_ensure_sane` (`sane_backend.py:50-56`).
- **CANCELLED styling tokens**: a `--saneless-status-cancelled` pair or an existing muted Pico
  variable, following the 23.1 dark-mode override convention, verified by computed colour in both
  schemes.
- **Exact wording** of every new line within D-03, D-05, D-06, D-08 and D-12.
- **`saneless jobs` on a fresh install** (M-17: `sqlite3.OperationalError` when `tmp_dir`/`data_dir`
  does not exist yet): either it prints an empty history, or it prints one line under the D-07
  table. No traceback either way.
- **Test shape**: success criterion 1's "parametrised test per third-party library" means SANE,
  httpx, img2pdf (all seven classes as separate rows) and tomllib/tomlkit, each asserting the
  saneless type, the original message substring, and `__cause__` is the third-party exception.
  Success criterion 2's four CLI cases run through `CliRunner` and assert exactly one stderr line
  and the D-07 code.

### Deferred Ideas (OUT OF SCOPE)
- **Keeping the scanned pages when PDF assembly fails** (`PdfError` currently loses the scan): needs
  HARD-01 page spooling, Phase 29.
- **Validating `paperless.url` at load time** (`AnyHttpUrl`, M-17 fix item 2): no v2.0 requirement
  maps it. Check at milestone audit whether it belongs with APPL-07 (Phase 30, placeholder token
  detection). D-10 covers the runtime symptom meanwhile.
- **A flip-wait timeout recorded as CANCELLED**: rejected (D-02); a timeout stays a failure.
- **Plain-language messages and "Technical details" disclosure**: APPL-04, Phase 30.
- **Safe device cancel on Ctrl-C mid-read**: HARD-03, Phase 29. This phase only gives Ctrl-C its
  exit code and one line.
- **Cause logging in the web metadata-fetch catch**: SWP-04, Phase 32.
- **A reference catalogue of every error message** (`docs/reference/errors.md`): rejected in favour
  of the symptom-organised how-to, because Phase 30 rewrites the wording.

**Also not in this phase (CONTEXT § Phase Boundary):** APPL-04 plain-language messages (Phase 30);
HARD-01..05 (Phase 29: page retention, safe cancel mid-read, flatbed timeout parity, `sane.init()`
guard); mid-pass abort (Phase 29); SWP-04 (Phase 32); `paperless.url` validation at load.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| EXC-01 | Every SANE, httpx, img2pdf (all seven error classes), and tomllib exception is caught at its call site and re-raised as a saneless exception type with the original message; no third-party exception type escapes a module boundary [M-17] | § Call-site inventory (SANE, httpx, img2pdf, TOML); measured exception types; `describe()` helper; Pitfalls 1-6 |
| EXC-02 | The CLI catches `ConfigError`, `ScanError`, `PaperlessError`, and `PdfError` and prints a one-line message with a non-zero exit code instead of a traceback; a missing `python-sane` import produces a clear install hint [M-17] | § Pattern 1 (Group.invoke guard, verified with CliRunner), Pattern 2 (`ExitCode`), Pattern 3 (`require_sane`), click/uvicorn measurements |
| EXC-03 | A scan that produces zero pages reports "No pages were scanned" (and "All pages were blank" only when detection removed them) rather than a misleading message or a bare `ValueError` [N-06] | § Zero-page reachability analysis (no genuine conflict with Phase 24 D-03) |
| EXC-04 | A user-initiated abort at the flip prompt is recorded as a cancelled job, not a scanner failure [N-08] | § CANCELLED ripple inventory; § Pattern 4 (prompt-failure cause on the coordinator); `ScanCancelledError` placement |
| EXC-05 | The worker logs every job failure with `exc_info` so the operator can find the cause [M-17] | § Worker three endings; caplog `record.exc_info` test pattern (precedent `tests/test_cli.py:725`) |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.14; `uv` only (`uv run`, `uv add`); pre-commit via `prek` (`uv run prek run --all-files`, never plain `prek run`).
- Zero errors from `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, `uv run pyrefly check src tests` (always name the paths). Both type checkers must pass; no mypy/pyright.
- No `# type: ignore`, no `# noqa`, no disabling rules. Fix properly.
- Ruff `D` rules: docstrings on all public modules/classes/functions; D203/D212 ignored (no blank line before class docstring; multi-line summary on second line).
- Active ruff families that bite this phase (from `pyproject.toml`): `EM` (build the message in a variable first), `PL` (PLR0911 too many returns, PLR0912 branches, PLR0913 args, PLR0915 statements), `S` (S101 no `assert` in `src/`, S110 no try-except-pass), `B`, `RET`, `SIM`, `LOG`, `G` (no f-strings in logging calls), `T20` (no `print`), `FBT` (no positional booleans). **`TRY` and `BLE` are not selected** (CONTEXT mentions `TRY`; it is not active).
- PEP 758 bracketless `except A, B:` is valid 3.14 syntax (only without `as`).
- Prefer external packages to reimplementation; favour Context7 libraries.
- Browser checks via Playwright; never mark them manual. CANCELLED styling must be proven by computed colour in both schemes.
- TDD RED commits are allowed (commit hooks type-check `src/` only); never `--no-verify` or `SKIP=`.
- pytest config (`pyproject.toml:150-162`): `filterwarnings = ["error"]`, `--strict-markers`, `timeout = 60` with `signal` method, markers `browser` and `sane_hardware`.
- Memory: never merge PRs; Serena edits in worktrees must target the worktree path.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Translate `_sane.error`/`ImportError`/`RuntimeError`/`AttributeError` | Scanner backend (`scanner/sane_backend.py`) | — | Device name, option name and value are only known here (D-08) |
| Translate httpx / JSON decode errors; body rendering; retry/fallback; poll continuation | Paperless client (`paperless.py`) | Pipeline `_preserving` (already wraps non-saneless as `PaperlessError`) | Base URL and status known here; token must never leave |
| Translate img2pdf / Pillow errors | PDF module (`pdf.py`) | — | Output path and page count known here |
| Zero-page precondition ("No pages were scanned", "All pages were blank") | Pipeline (`pipeline.py`) | — | The only layer that sees every backend's batch before `assemble_pdf` |
| Cancel vs failure vs prompt failure | Pipeline flip `match` + coordinator | Worker (shutdown), CLI coordinator (WR-08) | The coordinator knows *why* it answered `ABORTED`; the pipeline turns that into a type |
| TOML syntax / unreadable config → `ConfigError` | Config loader (`config.py::_build_settings`/`load_settings`) | `auto_profiles.py::_read_config` | Keeps "load_settings raises one type" (M-17 item 6) |
| Exception → exit code, one-line print, Ctrl-C → 130, last resort 5 | CLI (`cli.py` Group subclass) | `vocabulary.py` (`ExitCode` definition) | One handler for five commands; codes defined once |
| python-sane availability check | Scanner backend helper, called by CLI | — | Reuses `_ensure_sane`; `--help` never reaches command bodies |
| CANCELLED persistence, logging, degraded-health exclusion | Worker (`worker.py`) | Job store (TEXT column, no migration) | Worker owns job endings |
| CANCELLED rendering (muted) | Web templates + `app.css` | Playwright tests | Presentation only |

## Standard Stack

No new packages. Everything is already pinned; versions below were read from the project venv
(`importlib.metadata`) on 2026-09-15.

### Core (existing, touched by this phase)
| Library | Version | Purpose in this phase | Verified |
|---------|---------|-----------------------|----------|
| httpx | 0.28.1 | Paperless client exceptions | [VERIFIED: venv metadata + Context7 /encode/httpx hierarchy] |
| img2pdf | 0.6.3 | PDF assembly exceptions | [VERIFIED: venv, `dir(img2pdf)` + source grep] |
| pillow | 12.1.1 | PNG save errors | [VERIFIED: venv probe] |
| python-sane | 2.9.2 | `_sane.error`, import failure modes | [VERIFIED: venv probe, `sane.py` source] |
| click | 8.3.1 | Group.invoke guard, CliRunner | [VERIFIED: venv probe, `click/core.py:1319+`] |
| uvicorn | 0.42.0 | `serve` Ctrl-C and startup-failure exit status | [VERIFIED: `uvicorn/main.py:597-614` + subprocess SIGINT probe] |
| pydantic-settings | 2.13.1 | TOML source; lets parse errors escape | [VERIFIED: venv probe] |
| tomlkit | 0.14.0 | `auto-profiles` write path parse errors | [VERIFIED: venv probe] |
| tomllib | stdlib 3.14.2 | `TOMLDecodeError` attributes | [VERIFIED: venv probe + `_parser.py:63-108`] |
| pytest / pytest-playwright / playwright | 9.0.2 / 0.7.2 / 1.58.0 | Tests | [VERIFIED: venv metadata] |
| mkdocs / mkdocs-material | 1.6.1 / 9.7.6 | New how-to page nav | [VERIFIED: venv metadata] |

**Installation:** none.

## Package Legitimacy Audit

No external packages are installed by this phase, so slopcheck was not run.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| (none) | — | — | — | — | — | — |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Measured Exception Facts (the evidence the plan should rest on)

### httpx 0.28.1 [VERIFIED: venv probe; hierarchy matches Context7 /encode/httpx `_exceptions.py` docstring]

```
HTTPError
  RequestError
    TransportError
      TimeoutException: ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout
      NetworkError:     ReadError, WriteError, ConnectError, CloseError
      ProxyError
      UnsupportedProtocol
      ProtocolError:    LocalProtocolError, RemoteProtocolError
    DecodingError
    TooManyRedirects
  HTTPStatusError
InvalidURL      (Exception, NOT an HTTPError)
CookieConflict  (Exception)
StreamError     (RuntimeError)
```

| `paperless.url` | Where it fails | Type | `str(exc)` |
|---|---|---|---|
| `""` (default) | first request | `UnsupportedProtocol` | `Request URL is missing an 'http://' or 'https://' protocol.` |
| `paperless:8000`, `localhost:8000` | first request | `UnsupportedProtocol` | `Request URL has an unsupported protocol 'paperless://'.` |
| `ftp://x` | first request | `UnsupportedProtocol` | `...unsupported protocol 'ftp://'.` |
| `http://host:abc`, `http://[::1` | **`httpx.Client(...)` constructor** | `InvalidURL` | `Invalid port: 'abc'` |
| `http://ex\x00ample` | constructor | `InvalidURL` | `Invalid non-printable ASCII character in URL, '\x00' at position 9.` |
| `http://`, `http://a b` | first request | `ConnectError` | `[Errno -2] Name or service not known` |
| host label > 63 chars | first request | **`UnicodeEncodeError`** (not httpx) | `'idna' codec can't encode ... label too long` |
| unreachable port | first request | `ConnectError` | `[Errno 111] Connection refused` |
| black-hole address | first request | `ConnectTimeout` | `timed out` |
| `httpx.ReadTimeout("")` | — | — | `''` (empty; D-08 class-name fallback needed) |
| 200 with HTML body, `.json()` | on decode | `json.JSONDecodeError` (a `ValueError`) | `Expecting value: line 1 column 1 (char 0)` |

Consequences:
- `PaperlessClient.__init__` (`paperless.py:203-224`) must wrap `httpx.InvalidURL` → `PaperlessError`. It is constructed outside any `try` in `cli.py:287` (scan) and `web/app.py:60` (inside `create_app`, called by `serve` at `cli.py:497`), so today it is a raw traceback in both.
- The upload loop's retry branch should be `except httpx.UnsupportedProtocol` (immediate `PaperlessError`, no retry) **before** `except httpx.TransportError` (retry). Order matters because `UnsupportedProtocol` is a `TransportError`.
- `response.json()` (`paperless.py:280, 439, 522, 538`) raises `json.JSONDecodeError`; wrap as `PaperlessError` (a reverse-proxy login page returns 200 HTML).
- A final `except httpx.HTTPError` catch-all in each method closes the remaining `DecodingError`/`TooManyRedirects`/`ProxyError` holes. The IDNA `UnicodeEncodeError` is exotic; `_preserving` already converts non-saneless exceptions escaping the delivery window into `PaperlessError`, so it is covered at the pipeline boundary even if the client does not catch it [VERIFIED: `pipeline.py:469-471`].
- `test_connection` already maps `TransportError` → `UNREACHABLE` (`paperless.py:490-498`); leave it.

### img2pdf 0.6.3 and Pillow 12.1.1 [VERIFIED: venv probe; `img2pdf.py:412-439`]

- The seven classes are each `class X(Exception)`: `AlphaChannelError`, `ExifOrientationError`, `ImageOpenError`, `JpegColorspaceError`, `NegativeDimensionError`, `PdfTooLargeError`, `UnsupportedColorspaceError`. No shared base.
- img2pdf also raises **bare `Exception`** (e.g. `img2pdf.py:469, 476, 537-541, 1007, 1049`), `TypeError` (`:623, :630`) and `ValueError` (many).
- `img2pdf.convert([])` → `ValueError('Unable to process empty list')` (so is `convert()`).
- Garbage bytes → `ImageOpenError('cannot read input image (not jpeg2000). PIL: error reading image: ...')`.
- `convert(...)` returns `None` only when an `outputstream` is passed; `pdf.py:192` passes none, so the `RuntimeError` branch at `:197-198` is defensive. Keep the check, raise `PdfError`.
- Pillow PNG save: CMYK → `OSError('cannot write mode CMYK as PNG')`; **0x0 image → `SystemError('tile cannot extend outside image')`**; missing dir → `FileNotFoundError`.

Consequence: the only way to honour "everything `assemble_pdf` can raise becomes `PdfError`" is a
boundary catch of `Exception` around the assembly body (mkdir, temp dir, saves, convert, write),
narrow in span and broad in type, exactly the `_preserving` precedent (`pipeline.py:397-405`).
A tuple of the seven classes plus `ValueError`/`OSError` misses bare `Exception`, `TypeError` and
`SystemError`. The seven classes still get one parametrised test row each (success criterion 1).

### python-sane 2.9.2 [VERIFIED: venv probe]

| Situation | Type | Message |
|---|---|---|
| `python-sane` not installed | `ModuleNotFoundError` | `No module named 'sane'` |
| `libsane.so.1` missing (simulated by renaming the `.so`'s NEEDED entry) | plain `ImportError` (not `ModuleNotFoundError`) | `libsane.so.1: cannot open shared object file: No such file or directory` |
| `sys.modules["sane"] = None` (test seam) | `ModuleNotFoundError` | `import of sane halted; None in sys.modules` |
| `sane.open("nonexistent:device")` | `_sane.error` (direct `Exception` subclass, module `_sane`) | `Invalid argument` |
| option on inactive/unsettable option | `AttributeError` | `Inactive option: mode` / `Option can't be set by software: ...` (`sane.py:192-235`) |
| `SaneDev.__init__` unknown device | `RuntimeError` | `No such scan device '...'` (`sane.py:177`) |
| `snap()` empty read | `RuntimeError` | `Scanner returned no data` (`sane.py:302`) |
| `_SaneIterator.__next__` | converts **only** `str(e) == 'Document feeder out of documents'` into `StopIteration` (`sane.py:124-133`) | — |

`except ImportError` covers both import modes (`ModuleNotFoundError` subclasses it).
`sane_backend.py` never imports `sane` at module level (`:47-56`), and `cli.py:46` only imports
`SaneBackend`, so `--help` never triggers the import today. Keep it that way.

### tomllib / pydantic-settings / tomlkit [VERIFIED: venv probe]

- `tomllib.TOMLDecodeError` (3.14.2) is a `ValueError` with `msg`, `doc`, `pos`, `lineno`, `colno`.
  `tomllib.loads('a = = 1\n')` → `msg='Invalid value'`, `lineno=1`, `colno=5`, `str='Invalid value (at line 1, column 5)'`.
- **`exc.doc` is the entire TOML document, token included.** `str(exc)` does not contain it. Never render or log `exc.doc`.
- Constructing `TOMLDecodeError` with free-form args emits a `DeprecationWarning` and sets **no** `lineno`/`colno` (`_parser.py:81-102`). With `filterwarnings = ["error"]`, a test must build it as `TOMLDecodeError(msg, doc, pos)` with `str, str, int`, or produce it by actually parsing bad text.
- `load_settings(path)` on a bad file: the raw `TOMLDecodeError` escapes `Settings(_toml_file=...)` through `TomlConfigSettingsSource`; the `_build_settings` docstring already says so (`config.py:981-983`).
- Also escaping raw from `load_settings`: `PermissionError` (`[Errno 13] Permission denied: '<path>'`, unreadable file) and `UnicodeDecodeError` (`'utf-8' codec can't decode byte 0xff in position 27`, non-UTF-8 file). Both are handled today only by `cli.py:233-238`'s generic catch.
- tomllib messages interpolate at most a single character or a key path (`Found invalid character {c!r}`, `Cannot declare {key} twice`, `Duplicate inline table key {k!r}`), never a value [VERIFIED: `_parser.py` grep]. Key names can hold escapes, so render through the existing `_escape_name` style.
- `tomlkit.parse('a = = 1\n')` → `tomlkit.exceptions.UnexpectedCharError` (MRO `ParseError → ValueError, TOMLKitError`), `str="Unexpected character: '=' at line 1 col 4"`, attributes `.line`, `.col`. `auto_profiles.py:649` calls `tomlkit.parse` unguarded; `_render_checked` (`:719-730`) already converts its `tomllib` re-parse failure to `ConfigError`.

### click 8.3.1 [VERIFIED: `click/core.py` main() + CliRunner probe]

- `click.exceptions.Exit` and `Abort` subclass **`RuntimeError`**; `ClickException`/`UsageError` subclass `Exception`.
- `main()` in standalone mode: `(EOFError, KeyboardInterrupt)` → echo blank line → `raise Abort()` → prints `Aborted!`, `sys.exit(1)`; `ClickException` → `e.show()`, `sys.exit(e.exit_code)`; `OSError` with `EPIPE` → exit 1, other `OSError` re-raised.
- `sys.exit(n)` inside a command raises `SystemExit`, which is not an `Exception` and passes any `except Exception`.
- A `click.Group` subclass overriding `invoke(ctx)` sees every subcommand's exception, and CliRunner exercises it. Probe results:

| args | exit | stderr |
|---|---|---|
| command raising `RuntimeError("kaboom")` | 5 | `Unexpected error (RuntimeError): kaboom` |
| command raising `KeyboardInterrupt` | 130 | `Cancelled` |
| `ClickException` (re-raised by the guard) | 1 | `Error: bind` |
| `sys.exit(2)` | 2 | (unchanged) |
| `boom --help` | 0 | usage on stdout; body never runs |
| unknown command | 2 | click usage error |
| plain group, `KeyboardInterrupt` | 1 | `\nAborted!` |

- CliRunner in 8.3 exposes `result.stdout` and `result.stderr` separately.

### uvicorn 0.42.0 [VERIFIED: `uvicorn/main.py:597-614`, subprocess SIGINT probe]

- `uvicorn.run` wraps `server.run()` in `except KeyboardInterrupt: pass`. Measured: SIGINT to a click-wrapped `uvicorn.run` returns normally and the process exits **0** with nothing on stderr. So Ctrl-C on `serve` never reaches the Group guard. D-03's `serve` exception needs no special-casing, and a test pinning "serve's Ctrl-C does not exit 130" can stub `uvicorn.run` to return.
- `uvicorn.run` calls `sys.exit(STARTUP_FAILURE)` with **`STARTUP_FAILURE = 3`** when the server never started (for example a lifespan startup exception). That is a `SystemExit`, so the guard does not see it. It collides with saneless's exit 3 (Paperless). See Open Question 2.

### paperless-ngx task and error shapes [CITED + VERIFIED against source]

- DRF errors: `APIException` → `{"detail": "..."}`; `ValidationError` → `{"field": ["msg", ...]}`, non-field → `{"non_field_errors": [...]}`; `ValidationError.detail` "may be a list or dictionary of error details, and may also be a nested data structure" [CITED: django-rest-framework.org/api-guide/exceptions/]. So the renderer must handle: dict with `detail` (str or list), dict of field → list/str, top-level list, non-JSON (HTML), empty body.
- Duplicate detection on the latest release **v3.1.3** [VERIFIED: raw.githubusercontent.com tag v3.1.3 `consumer.py`, `tasks.py`, `serialisers.py`, `signals/handlers.py`]:
  - A checksum duplicate is **consumed as a new document by default**. It is rejected only when `CONSUMER_DELETE_DUPLICATES` is set (`consumer.py:1024`).
  - When rejected, the task returns `{"duplicate_of": N, "duplicate_in_trash": bool}` and `task_postrun_handler` sets status **FAILURE** (`handlers.py:1277-1278`).
  - API v9 (what saneless pins, `paperless.py:35`) renders `result` as `Not consuming: It is a duplicate of document #N` (`serialisers.py:2680`). API v10 `result_data` carries `duplicate_of` with **no** `error_message`/`reason`, so `_failure_message` would return `_NO_FAILURE_MESSAGE` there.
  - Paperless 2.x (e.g. 2.17.1) failed duplicates with `Not consuming <file>: It is a duplicate of <title> (#id).`
  - Detection rule that covers all three: `"duplicate of"` in the failure text (case-insensitive), or `"duplicate_of"` in `result_data`.

## Zero-Page Reachability (EXC-03 vs Phase 24 D-03)

Traced every path through `sane_backend.py` and `pipeline.py` [VERIFIED: code read]:

| Path | What happens at zero usable pages today | After this phase |
|---|---|---|
| ADF / feeder source (or Auto with `auto_source_mode="adf"`), nothing fed | `_acquire_pages` → `page_num == 0` → `FeederEmptyError("No paper detected in feeder")` (`sane_backend.py:775-776`) | **unchanged** (true cause, Phase 24 D-03) |
| ADF, sheets fed but every one failed integrity | `ScanError("All N page(s) fed were unreadable ...")` (`:781-787`) | **unchanged** (distinct true cause, Phase 24 D-06) |
| Flatbed / Auto-flatbed | `start()`+`snap()` returns one image or raises; an unreadable one raises (`:1257-1278`). Never zero. | unchanged (wrapped per EXC-01) |
| Manual duplex pass A or pass B | each pass is a `scan_pages` feeder call → `FeederEmptyError` from the backend | unchanged |
| **Any other `ScannerBackend` or test fake returning `ScanBatch(pages=[])`**, detection **on** | `_drop_empty_pages([])` → `"All pages were detected as empty"` (a lie: N-06) | **"No pages were scanned"** (`ScanError`) |
| same, detection **off** | `assemble_pdf([])` → raw img2pdf `ValueError` | **"No pages were scanned"** |
| same, manual duplex with one empty pass (counts differ) | `_DuplexMismatch` → `assemble_pdf([])` for that half → raw `ValueError` (`pipeline.py:734/740`) | **"No pages were scanned"** raised after the empty pass |
| Detection removed every page | `ScanError("All pages were detected as empty")` (`pipeline.py:504-506`) | **"All pages were blank"** |

**Conclusion: no genuine conflict.** The SANE backend never returns an empty batch, so "No pages
were scanned" cannot pre-empt the feeder message. It is the pipeline's contract check against the
`ScannerBackend` ABC. Placement:
1. `_scan_simplex` (after `scan_pages`, `pipeline.py:1048`) and after **each** manual-duplex pass
   (`:938` before the flip prompt, so nobody is asked to flip nothing; `:970` before the count
   comparison). One shared helper, `_require_pages(batch)`.
2. `_drop_empty_pages` then only ever sees a non-empty list, so its "all removed" branch is true
   by construction: reword to "All pages were blank".
3. `assemble_pdf` also refuses an empty list with `PdfError` as defence in depth (it is a public
   function), so img2pdf's `ValueError` is unreachable from any caller.

**Item for the user's awareness (not a blocker):** read literally, EXC-03 says "a scan that produces
zero pages reports 'No pages were scanned'". An ADF with nothing loaded still reports "No paper
detected in feeder", which is the more specific true cause and is what Phase 24 locked. This
research reads EXC-03 as targeting N-06's misleading and leaked messages, not as overriding D-03.

## CANCELLED Ripple Inventory (D-01)

[VERIFIED: grep of `src/`, `tests/`, templates, docs]

| Site | Change |
|---|---|
| `vocabulary.py:52-63` `JobState` | add `CANCELLED = "CANCELLED"` |
| `vocabulary.py:253-255` `TERMINAL_STATES` | add it; `ACTIVE_STATES`/`BUSY_STATES` untouched |
| `vocabulary.py:274-312` `state_label` | `"Cancelled"` arm (ty/pyrefly flag the missing arm) |
| `vocabulary.py:315-363` `progress_label` | arm (totality; `"Cancelled"`) and docstring "DONE, ERROR and FALLBACK" → four terminal states |
| `pipeline.py:65-99` `PipelineEvent.job_state` | matches on `PipelineEvent`, **not** `JobState`; no change |
| `job.py:93` `_ACTIVE_STATE_VALUES`, `fail_active_jobs` | derived from `ACTIVE_STATES`; no change |
| `job.py:315-326` schema | `state TEXT NOT NULL`, no CHECK constraint; **no migration** |
| `job.py:583` `JobState(row["state"])` | reads fine forward. **Rollback risk:** an older saneless reading a `CANCELLED` row raises `ValueError`. Same for a new `ErrorCategory` value (`:586`). Accept and note in the summary. |
| `job.py:892+` `latest_run_job` | filters `error_category IS NOT 'REJECTED'`; a CANCELLED row (category NULL) is returned as the job that just ended. Correct; IN-08 exclusion unaffected. |
| `job.py:445-450` `is_active`/`is_busy` | set membership; CANCELLED is neither, so polling stops and the Scan button re-enables with no template change to `scan_button.html` |
| `worker.py:1313-1338` `_scan_job` except | three explicit endings (see Pattern 5) |
| `worker.py:941-963` `_failure_record` | shutdown check must stay first (a shutdown-claimed `ABORTED` also produces `ScanCancelledError`) |
| `worker.py:897-918, 971-981` degraded health | counts only loop-level (store/prune) failures, never job endings; a cancel cannot count. Add a test anyway. |
| `worker.py:1264` `_status_cb` | `state not in ACTIVE_STATES` already ignores terminal states |
| `web/templates/partials/status.html:18-30` | new `{% elif job.state == JobState.CANCELLED %}` branch with the history-reload `hx-trigger="load"` div. **Without it a CANCELLED job renders nothing** (the if-chain has no else). |
| `web/templates/partials/history.html:7` | `status-cancelled` class arm |
| `web/static/app.css` | `.status-cancelled { color: var(--pico-muted-color); }` (see styling note) |
| `cli.py:63` `_STATUS_COL_WIDTH` | derived; "Cancelled" (9) < "Waiting for flip" (16); no change |
| `cli.py:453` `jobs --json` `state` | emits `CANCELLED` automatically |
| `tests/test_vocabulary.py:59` `test_job_state_has_exactly_nine_members` | becomes ten; `:125-160` partition and membership tests extend |
| `tests/test_job.py:1444` | seeds the whole enum; picks it up |
| `tests/test_browser.py:777-782` status class probe list, `:1217` AA contrast test, `:1242` forced-dark test | add `status-cancelled` |
| docs | `docs/how-to/cli-scripting.md:56-58` state list; `docs/reference/web-api.md:101` states; `:161` Abort "ends `ERROR`" → `CANCELLED` |

**Styling recommendation:** use Pico's own `--pico-muted-color` directly (UI-SPEC 23.1 convention
rule 4: "Pico semantic tokens that already swap per scheme are used directly and never
overridden"). Measured against the vendored `pico-2.1.1.min.css` values (`#646b79` light, `#7b8495`
dark) and the 23.1 surfaces: **5.36:1** on the light page, **4.77:1** on the dark page and `td`,
**4.53:1** on the dark card. All pass AA 4.5:1 [VERIFIED: computed with the WCAG formula; confirm
with the existing Playwright contrast helper]. No app-owned `--saneless-status-cancelled` pair is
needed. Record the ratios in `.planning/UI-SPEC.md` (convention rule 5).

## `ScanCancelledError` Placement

`except ScanError` / `isinstance(..., ScanError)` sites [VERIFIED: grep]:
1. `cli.py:317` (`scan`): would print `Scan error:` and exit 1.
2. `sane_backend.py:737` (re-raise saneless errors inside `_acquire_pages`): a cancel is never raised there.
3. `vocabulary.py:682` `classify_error`: would give `SCANNER`.

Also relevant: `pipeline.py:469-470` `_preserving` rebuilds `type(exc)(msg)` for a `SanelessError`,
so every new exception class must keep a single-message constructor. A cancel is raised before the
preservation window, so it never reaches there.

**Recommendation: `class ScanCancelledError(SanelessError)`**, not `ScanError`. Every consumer of
"a cancel" then has to name it on purpose (worker, CLI exit mapping). No existing
`except ScanError` can silently swallow it into exit 1 or ERROR. `pytest.raises(ScanError)` tests
of the abort path turn red, which is the signal that they need updating. `classify_error` keeps
returning `UNKNOWN` for it, and both consumers test for it before classifying.

## Architecture Patterns

### System flow (failure paths)

```
             CLI (cli.py)                                   Web worker (worker.py)
  argv -> click main -> GuardedGroup.invoke                 queue -> _scan_job
             |  (--help / usage errors: click Exit/          |
             |   UsageError re-raised untouched)             |
             v                                               v
     command body: require_sane() -> _load_cli_settings -> run_pipeline(...)
                                                   |
     +---------------------------------------------+----------------------------------+
     | scanner/sane_backend.py   pipeline.py              pdf.py        paperless.py   |
     | _sane.error/RuntimeError  empty batch ->           img2pdf/PIL   httpx/JSON     |
     | /AttributeError ->        ScanError("No pages      -> PdfError   -> PaperlessError|
     | ScanError(ctx: orig)      were scanned")                         (retry on       |
     | ImportError ->            flip ABORTED:                           TransportError, |
     | ConfigError(install hint)  cause? ScanError                       fail fast on    |
     |                            : ScanCancelledError                  UnsupportedProto)|
     +---------------------------------------------+----------------------------------+
                                                   |  SanelessError subclasses only
                 +---------------------------------+------------------------------+
                 v                                                                v
     GuardedGroup.invoke: except                                  _scan_job except Exception:
       KeyboardInterrupt -> one line, 130                          1 coordinator.aborted_by_shutdown
       ScanCancelledError -> one line, 130                            -> ERROR RESTART_REASON, INFO
       SanelessError -> classify_error -> ExitCode                 2 ScanCancelledError
            (ConfigError 2, ScanError 1, Paperless 3, Pdf 4)          -> CANCELLED, INFO, no traceback
       Exception -> logger.exception + one line, 5                 3 else -> ERROR + category,
                                                                      logger.exception (EXC-05)
```

### Recommended file touch map
```
src/saneless/
  exceptions.py        # + PdfError, ScanCancelledError, describe(exc) helper
  vocabulary.py        # + JobState.CANCELLED, ErrorCategory.ASSEMBLY, ExitCode, exit_code_for()
  scanner/sane_backend.py  # wrap SANE call sites; require_sane()
  paperless.py         # ctor InvalidURL; retry set; one body renderer; poll continuation; duplicate wording
  pdf.py               # boundary catch -> PdfError; empty-list refusal
  pipeline.py          # _require_pages; "All pages were blank"; flip match -> cancel vs prompt failure
  config.py            # TOMLDecodeError / OSError / UnicodeDecodeError -> ConfigError under header
  auto_profiles.py     # tomlkit.parse -> ConfigError
  worker.py            # three endings; exc_info logging
  cli.py               # GuardedGroup; ClickFlipCoordinator prompt-failure cause; per-command catches removed
  web/templates/partials/{status,history}.html, web/static/app.css
docs/how-to/troubleshoot-a-failed-scan.md (new) + mkdocs.yml nav
```

### Pattern 1: one last-resort handler as a `click.Group` subclass
**What:** subclass `click.Group`, override `invoke`, and pass `cls=` to `@click.group`. Verified
above with CliRunner. It runs after click has parsed `--help` (help raises `Exit` during
`make_context`, which the guard re-raises), so CFG-10 and D-05's "--help never imports sane" hold.
**Order of except clauses matters:**
```python
# Source: verified probe against click 8.3.1 (scratchpad click_probe.py)
class _GuardedGroup(click.Group):
    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except (click.exceptions.Exit, click.exceptions.Abort, click.ClickException):
            raise  # click's own control flow; Exit/Abort are RuntimeError subclasses
        except KeyboardInterrupt:
            click.echo("Cancelled", err=True)
            ctx.exit(ExitCode.CANCELLED)
        except SanelessError as exc:
            ...  # one line; ctx.exit(exit_code_for(...))
        except Exception as exc:
            ...  # logger.exception(...); one line naming type(exc).__name__; ctx.exit(ExitCode.UNEXPECTED)
```
`ctx.exit(code)` raises click `Exit`, which click's `main` turns into `sys.exit(code)`. Avoid
PLR0911 (more than 6 `return`s) by assigning in a `match` or chain and exiting once.
`ClickException` stays click's (exit 1 today for `serve`'s bind failure; see Open Question 2).

### Pattern 2: `ExitCode` defined once, total over categories
```python
# vocabulary.py -- IntEnum precedent: scanner/sane_backend.py GeometryUnit(IntEnum)
class ExitCode(IntEnum):
    SUCCESS = 0
    SCAN = 1
    CONFIG = 2
    PAPERLESS = 3
    PDF = 4
    UNEXPECTED = 5
    CANCELLED = 130

def exit_code_for(category: ErrorCategory) -> ExitCode:
    match category:
        case ErrorCategory.FEEDER | ErrorCategory.SCANNER: code = ExitCode.SCAN
        case ErrorCategory.CONFIG: code = ExitCode.CONFIG
        case ErrorCategory.UPLOAD: code = ExitCode.PAPERLESS
        case ErrorCategory.ASSEMBLY: code = ExitCode.PDF
        case ErrorCategory.UNKNOWN | ErrorCategory.REJECTED: code = ExitCode.UNEXPECTED
        case _: assert_never(category)
    return code
```
This reuses `classify_error` as the single exception→meaning rule, so the worker's category and the
CLI's exit code cannot disagree. A cancel is not a category (D-01) and is tested first. The
doc-truth test iterates `ExitCode` and asserts each value appears as a row in both documented
tables (precedent: `tests/test_deployment_config.py:323-331`).

### Pattern 3: `require_sane()` next to `_ensure_sane`
```python
# scanner/sane_backend.py
def require_sane() -> None:
    """Import python-sane now, or raise ConfigError with the import's reason and an install hint."""
    try:
        _ensure_sane()
    except ImportError as exc:   # ModuleNotFoundError is a subclass
        msg = (f"python-sane cannot be imported ({describe(exc)}). Install the SANE "
               "development package (libsane-dev or sane-backends-devel) and reinstall "
               "saneless; see Install on Bare Metal")
        raise ConfigError(msg) from exc
```
Call it as the **first statement** of `scan`, `devices`, `auto-profiles` and `serve` (by a
module-global name `cli.py` imports, so tests can patch `saneless.cli.require_sane`), and also from
`SaneBackend.__init__` in place of the bare `_ensure_sane()`, so the web app and any other
construction get the same translation. `--help` never reaches a command body. `jobs` does not call
it. Test seam for the failure: `monkeypatch.setattr(sane_backend, "sane", None)` plus
`monkeypatch.setitem(sys.modules, "sane", None)` (gives `ModuleNotFoundError`), and a patched
importer raising `ImportError("libsane.so.1: cannot open shared object file ...")` for the
extension case. Existing CLI tests replace `saneless.cli.SaneBackend` and never import real sane,
so they need `require_sane` patched too, or they rely on python-sane being installed. It is
installed in CI via `libsane-dev` (`.github/workflows/ci.yml:26,43,65`). Prefer patching, for
hermeticity.

### Pattern 4: telling a WR-08 prompt failure from an operator abort (no fourth `FlipOutcome`)
**Recommended:** the coordinator records *why*, and the pipeline turns that into a type.
- `FlipCoordinator` (ABC, `pipeline.py:118-150`) gains a **concrete** property with a default,
  e.g. `abort_cause -> BaseException | None` returning `None`. It is not abstract, so
  `WorkerFlipCoordinator` and the test coordinators need no change.
- `ClickFlipCoordinator._prompt`'s `except Exception` branch (`cli.py:148-157`) stores the
  exception and claims `ABORTED` **under one lock**, exactly like
  `WorkerFlipCoordinator.abort_for_shutdown` (`worker.py:245-261`, "held across a shutdown's claim
  and its marker"). Use `FlipAnswerSlot.offer` (it returns whether this call claimed) rather than
  `settle`, and set the cause only if the offer won. Otherwise a Ctrl-C that claimed first could be
  misreported as a prompt failure.
- `_scan_manual_duplex`'s `case FlipOutcome.ABORTED:` (`pipeline.py:956-958`):
  `cause = flip.coordinator.abort_cause`; if `cause is not None`, `raise ScanError(f"Flip prompt failed: {describe(cause)}") from cause` (exit 1, ERROR); otherwise `raise ScanCancelledError("Manual duplex scan cancelled at the flip prompt")`.
- The worker's shutdown-claimed abort also arrives as `ScanCancelledError`, and `_failure_record`
  already checks `coordinator.aborted_by_shutdown` first → ERROR `RESTART_REASON`. Keep that order.

Alternative considered: always raise `ScanCancelledError` and let the CLI inspect its own
coordinator. It is simpler, but the pipeline would raise a "cancelled" type for a broken terminal
and log it as such. Rejected.

### Pattern 5: the worker's three endings (EXC-04/EXC-05)
```python
except Exception as exc:
    if coordinator is not None and coordinator.aborted_by_shutdown:       # 1 shutdown
        owed = _OwedWrite(JobState.ERROR, error=RESTART_REASON)
        self._finish_or_owe(job.id, owed)
        logger.info("Job %s ended by shutdown: %s", job.id, exc)
    elif isinstance(exc, ScanCancelledError):                             # 2 cancel
        self._finish_or_owe(job.id, _OwedWrite(JobState.CANCELLED, error=str(exc)))
        logger.info("Job %s cancelled: %s", job.id, exc)
    else:                                                                 # 3 failure
        category = classify_error(exc)
        self._finish_or_owe(job.id, _OwedWrite(JobState.ERROR, error=str(exc), category=category))
        logger.exception("Job %s failed (%s)", job.id, category.value.lower())
    return
```
This replaces the `category is None` sentinel with an explicit branch. `_failure_record` is also
used by `_best_effort_fail` (`worker.py:1003-1005`); it can keep returning `(RESTART_REASON, None)`
there, or be split. `logger.exception` must be called inside the `except` block (it reads
`sys.exc_info()`). Otherwise pass `exc_info=exc`. Whether a CANCELLED row carries the message in
`error` is a UI choice. `finish_job` writes whatever is passed; `latest_run_job` does not care.

Test pattern (precedent `tests/test_cli.py:725`, `tests/test_app_lifespan.py:286`):
```python
with caplog.at_level(logging.INFO, logger="saneless.worker"):
    ...
failed = [r for r in caplog.records if r.levelno == logging.ERROR and job.id in r.getMessage()]
assert failed and failed[0].exc_info is not None and failed[0].exc_info[1] is raised_exc
cancelled = [r for r in caplog.records if "cancelled" in r.getMessage()]
assert cancelled[0].levelno == logging.INFO and cancelled[0].exc_info is None
```

### Pattern 6: one Paperless body renderer (D-09)
`_render_error_body(response) -> str`: try `response.json()`. If it is a dict with `detail`, use
`detail` (joined if it is a list). Else, if it is a dict, use the first key whose value is a
non-empty list/str, as `field: message`. Else, if it is a non-empty list, use the first str.
Otherwise use `" ".join(response.text.split())`, cut to ~200 characters with `…`. DEBUG-log the
full body. Reuse it for the upload 4xx (`paperless.py:300-306`) and the poll non-200 (`:432-437`),
and delete `_truncated_body`/`_MAX_ERROR_BODY_CHARS`. **Check `tests/test_paperless.py:513-516`**:
it asserts `"truncated"` appears and length < 1000, and must be rewritten to the new shape. The
reason phrase comes from `response.reason_phrase` (httpx), which gives `(400 Bad Request)`.

### Pattern 7: poll continuation within the monotonic deadline (D-11)
Wrap only the `self._client.get(...)` call: `except httpx.TransportError as exc: last_transport_error = exc`, then fall through to the existing `remaining`/sleep logic (no `continue` that skips the deadline check). On expiry, append `; last error: <describe(last)>` to the `PaperlessTimeoutError` message. Keep the non-200 immediate raise. `UnsupportedProtocol` during a poll cannot occur, because the upload with the same base URL already succeeded.

### Anti-Patterns to Avoid
- **`except (ValueError, OSError, img2pdf.ImageOpenError)` in `pdf.py`** (M-17's literal fix): misses bare `Exception`, `TypeError`, `SystemError`.
- **Catching `httpx.TransportError` before `httpx.UnsupportedProtocol`**: retries a malformed URL three times with backoff.
- **Rendering `TOMLDecodeError.doc` or chaining it into a logged traceback that formats `doc`**: the document holds the token. `str(exc)` and `.msg`/`.lineno`/`.colno` are safe.
- **Narrowing `_load_cli_settings`'s `except Exception` to `TOMLDecodeError` only**: `PermissionError`/`UnicodeDecodeError` from an unreadable or non-UTF-8 config then become exit 5 "Unexpected error". Convert all three in `load_settings`/`_build_settings` so the loader raises one type.
- **A broad `except Exception` in the CLI guard placed above click's exceptions**: turns `--help` (`Exit`) and usage errors into exit 5.
- **`ScanCancelledError(ScanError)`** without reordering all three `ScanError` sites.
- **Logging a cancel with `logger.exception`**: N-08 says not at ERROR level.
- **Turning `_set_geometry`'s broad catch (`sane_backend.py:536-544`) into a `ScanError`**: it is a designed fallback to cropping (M-15), not a leak.

## Call-Site Inventory

### SANE (`scanner/sane_backend.py`) [VERIFIED: code read]
| Line | Call | Raises | Wrap as | Message shape (D-08) |
|---|---|---|---|---|
| 1047 | `_ensure_sane()` in `__init__` | `ImportError` | `ConfigError` (via `require_sane`) | install hint |
| 1060 | `sane.init()` | `_sane.error` | `ScanError` | `Could not initialise SANE: <orig>` (no re-entry guard: HARD-05) |
| 1078 | `sane.open(device_id)` | `_sane.error`, `RuntimeError` | `ScanError` | `Could not open scanner <device>: <orig>` |
| 1084 | `dev.close()` in `finally` | `_sane.error` (rare) | leave, or suppress-and-log; do not mask the body's exception | — |
| 1094 | `sane.get_devices()` | `_sane.error` | `ScanError` | `Could not list scanners: <orig>` |
| 1123, 1203 | `dev.get_options()` | `_sane.error` | `ScanError` | `Could not read options from <device>: <orig>` |
| 993-995 | `dev.source/mode/resolution = ...` | `_sane.error`, `AttributeError` | `ScanError`, **one try per assignment** so the option and value are named | `Could not set mode to 'Lineart' on <device>: Invalid argument` |
| 997 | `int(dev.resolution)` read-back | `_sane.error`, `AttributeError` | `ScanError` | `Could not read back resolution from <device>: <orig>` |
| 1257-1258 | `dev.start()`, `dev.snap()` (single-sheet path) | `_sane.error`, `RuntimeError` | `ScanError`; optionally map the exact string `Document feeder out of documents` to `FeederEmptyError(_FEEDER_EMPTY_MESSAGE)`, mirroring python-sane's own iterator rule (`sane.py:130`) | `Scanner error on <device>: <orig>` |
| 741-743 | `_acquire_pages` broad catch | already `ScanError("Scanner error on page N: <orig>")` | keep (Phase 24 D-03); only apply `describe()` for empty messages | — |
| 536-544 | `_set_geometry` broad catch | designed crop fallback | **keep** | — |

`_configure_device` does not currently receive the device name. Pass `device_id` in, or wrap in
`scan_pages` with the name in scope, and watch PLR0913 (5 args max). A small record is the house
precedent (`_DeliveryContext`). Do not absorb HARD-02 (keep pages before a mid-batch error),
HARD-03 (cancel-then-wait), HARD-04 (flatbed timeout) or HARD-05 (`sane.init()` guard).

### httpx (`paperless.py`)
| Line | Site | Change |
|---|---|---|
| 222 | `httpx.Client(**kwargs)` | `except httpx.InvalidURL` → `PaperlessError("Paperless URL <url> is not valid: <orig>")` |
| 271-315 | upload loop | `except httpx.HTTPStatusError` (4xx immediate with rendered body; 5xx retry) ; `except httpx.UnsupportedProtocol` → immediate; `except httpx.TransportError` → retry; `json.JSONDecodeError` on `response.json()` → `PaperlessError` |
| 318-329 | exhausted | `Upload failed after N attempts` naming the last error via `describe()`; the fallback stays |
| 332-383 | consume-dir copy | raises raw `OSError`. `_preserving` converts it to `PaperlessError` at the pipeline boundary, but the module boundary still leaks it. Wrap in `upload_document`: `PaperlessError("Could not copy the PDF to the consume directory <dir>: <orig>")` |
| 427-462 | `poll_task` | Pattern 7; JSON decode → `PaperlessError`; duplicate wording on FAILURE |
| 509-539 | `get_tags`/`get_correspondents` | `except httpx.HTTPError` and `JSONDecodeError` → `PaperlessError`. `routes.py:82` catch is unaffected (SWP-04 is Phase 32). |

Duplicate wording (D-10): in the FAILURE branch, when the duplicate rule matches, append something
like `; the document may already be in Paperless, check before scanning again`.

Fallback on a malformed URL (D-10 "leaning towards yes"): the planner decides. Taking the fallback
when `consume_dir` is set is consistent with "no retry ≠ no fallback". Note that the ctor
`InvalidURL` path fails before any scan (at client construction), so there the fallback question
does not arise.

### img2pdf / Pillow (`pdf.py:126-202`)
One `try:` around the body from `output_dir.mkdir` to `pdf_path.write_bytes`, then
`except Exception as exc: msg = f"Could not assemble {len(images)} page(s) into {output_dir / filename}: {describe(exc)}"; raise PdfError(msg) from exc`.
Before it: `if not images: raise PdfError("Could not assemble a PDF: no pages were given")`. The
`RuntimeError` at `:197-198` becomes a `PdfError` raised inside the try. A `PdfError` raised inside
the `try` would be caught and re-wrapped by the `except Exception`, so either raise it outside the
try or `except PdfError: raise` first.

### TOML (`config.py`, `auto_profiles.py`, `cli.py`)
- `config.py::_build_settings` (`:974-1024`): around the `Settings(...)` construction add
  `except tomllib.TOMLDecodeError as exc:` → line `f"line {exc.lineno}, column {exc.colno}: {exc.msg}"`
  under the existing header, raised `from exc`. This is safe because `str()` has no document;
  success criterion 1 wants `__cause__`. If the user prefers Phase 27's `from None` rule
  (T-27-13), the test asserts `__context__` instead. Also `except OSError` →
  `f"cannot read the file: {exc.strerror}"` and `except UnicodeDecodeError` →
  `"the file is not valid UTF-8 ..."`.
  Note the current structure raises `ConfigError` outside the `except` with `from None`. A cause
  can be kept by storing it.
- `auto_profiles.py:649` `tomlkit.parse(text)`: `except tomlkit.exceptions.ParseError as exc` →
  `ConfigError(f"Cannot update {config_path}: line {exc.line}, column {exc.col}: ...")`. Import
  `ParseError` from `tomlkit.exceptions`. The worker's `_persist_generated_profiles`
  (`worker.py:830-856`) already catches `ConfigError`.
- `cli.py:233-238`: after the loader raises one type, delete the generic branch. An unexpected
  error from `configure_logging` then reaches the Group guard (exit 5). `configure_logging` does not
  raise for an unwritable log (`logging_config.py:54-67`).

### CLI per command
| Command | Today | After |
|---|---|---|
| `scan` (`cli.py:249-324`) | catches `ScanError` (1), `PaperlessError` (3); `ConfigError` "No scanner found" and `SaneBackend`/`PaperlessClient` construction leak | `require_sane()` first; remove per-command catches; guard maps all; keep `finally: paperless.close()` |
| `devices` (`:364-425`) | nothing caught | `require_sane()`; guard; doc table gains 1, 2, 5, 130 |
| `jobs` (`:428-483`) | fresh install **already works**: `mkdir data_dir` at `:438` (measured: exit 0, empty table). A corrupt DB raises `sqlite3.DatabaseError` → guard exit 5 | no `require_sane`; guard |
| `serve` (`:486-522`) | `ClickException` bind (1); ctor leaks | `require_sane()`; guard maps ctor `PaperlessError` (3); Ctrl-C unchanged (0) |
| `auto-profiles` (`:555-596`) | catches `ConfigError` (2), `OSError` (2) | `require_sane()`; guard; keep the `OSError` → 2 message (documented) |

Printing: `ConfigError` messages already carry their own header (`cli.py:229-231`), so no prefix.
Others keep today's documented prefixes, `Scan error:` and `Paperless error:` (quoted in
`docs/how-to/set-up-adf-duplex.md:95`), and add `PDF error:`. D-06 line for unexpected errors.
For Ctrl-C outside the prompt, one line such as `Cancelled`.

**Log-file truth for D-06:** `configure_logging` falls back to stderr silently if the file cannot
be opened, and returns nothing. "Full details in <log_file>" would then be false. Either have it
return whether the file handler attached, or inspect the root handlers. **Before logging is
configured, `logging.lastResort` prints an ERROR record *with its traceback* to stderr**
[VERIFIED: probe]. A `logger.exception` in the guard before `_load_cli_settings` ran therefore
breaks "one line". Track in `ctx.obj` whether logging is configured. If it is not, print only the
line, and put the traceback on stderr only with `-v` (the planner's call per D-06).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Catch-all for HTTP failures | a tuple of httpx subclasses | `httpx.TransportError` / `httpx.HTTPError` bases | Hierarchy is documented and stable; tuples rot |
| Exit handling around click | wrapping `saneless:main` with `standalone_mode=False` | `click.Group.invoke` override | CliRunner exercises it; `main` wrapper is invisible to CliRunner tests |
| Ctrl-C on `serve` | a SIGINT handler | nothing: uvicorn already swallows it | Measured exit 0 |
| TOML line/column | regex over `str(exc)` | `exc.lineno`, `exc.colno`, `exc.msg` (tomllib); `exc.line`, `exc.col` (tomlkit) | Attributes are the API |
| Muted colour | new hex pair | `var(--pico-muted-color)` | Already swaps per scheme and passes AA |
| DRF error text | ad-hoc string search | `response.json()` shape dispatch per DRF docs | DRF documents `detail`/field/`non_field_errors` |

## Runtime State Inventory

(Not a rename phase, but a new persisted enum value.)

| Category | Items Found | Action Required |
|----------|-------------|-----------------|
| Stored data | `jobs.state` TEXT gains `CANCELLED`; `jobs.error_category` TEXT gains the PDF category value. No CHECK constraint (`job.py:315-326`) | None forward. Rollback to an older build fails to read those rows (`JobState(...)` ValueError); mention in summary/CHANGELOG |
| Live service config | None. No external service stores job states | None |
| OS-registered state | None | None |
| Secrets/env vars | None renamed | None |
| Build artifacts | None | None |

## Common Pitfalls

### Pitfall 1: `InvalidURL` is a constructor failure
**What goes wrong:** a catch in `upload_document` never sees it; `serve` dies in `create_app`.
**How to avoid:** wrap `httpx.Client(...)` in `PaperlessClient.__init__`.
**Warning sign:** a test that builds the client with `http://host:abc` raises before any request.

### Pitfall 2: click's `Exit`/`Abort` are `RuntimeError`s
**What goes wrong:** a guard `except Exception` turns `--help` into "Unexpected error (Exit)" exit 5.
**How to avoid:** re-raise `click.exceptions.Exit`, `Abort`, `ClickException` first; test `saneless scan --help` and an unknown option through the guarded group.

### Pitfall 3: `logging.lastResort` tracebacks before logging setup
**What goes wrong:** "exactly one stderr line" fails for an error raised before `_load_cli_settings` configured logging.
**How to avoid:** do not call `logger.exception` when no handler is configured; gate on `ctx.obj` state.

### Pitfall 4: `TOMLDecodeError` in tests
**What goes wrong:** `TOMLDecodeError("x")` → `DeprecationWarning` → test error under `filterwarnings=error`, and no `lineno`.
**How to avoid:** parse real bad text, or construct with `(msg, doc, pos)`.

### Pitfall 5: KeyboardInterrupt in pytest
**What goes wrong:** a `KeyboardInterrupt` that escapes a test aborts the whole pytest session.
**How to avoid:** raise it only inside `CliRunner.invoke` (click converts it even without the guard, so a RED test fails cleanly with exit 1) or catch it explicitly in direct tests.

### Pitfall 6: Ctrl-C mid-ADF-read and process exit
**What goes wrong:** `_acquire_pages`'s `ThreadPoolExecutor` worker is a non-daemon thread still inside `next(iterator)`; `concurrent.futures` joins it at interpreter exit, so exit 130 is delayed until the SANE read returns [ASSUMED: from CPython `concurrent.futures.thread` atexit behaviour, not measured here].
**How to avoid:** out of scope (HARD-03, Phase 29). Tests must not depend on real device timing. Note the delay in the troubleshooting page only if it is verified.

### Pitfall 7: uvicorn exit 3
**What goes wrong:** a lifespan startup failure exits 3 via `uvicorn.run`'s `sys.exit`, which docs would read as "Paperless error".
**How to avoid:** see Open Question 2.

### Pitfall 8: success criterion 2 "one line" vs Phase 27's multi-line config header
**What goes wrong:** D-12's TOML error is two lines (header + `  line 12, column 5: ...`), and Phase 27 renders one line per validation problem.
**How to avoid:** the bad-config CLI test asserts "one error report" (header plus exactly one indented problem line), not literally one line. Scanner, Paperless and PDF cases assert one line.

### Pitfall 9: `_preserving` rebuilds `type(exc)(msg)`
**What goes wrong:** a new exception class with extra required constructor arguments breaks preservation.
**How to avoid:** keep `PdfError`/`ScanCancelledError` single-message.

### Pitfall 10: PLR0911/PLR0912 in dispatchers
**What goes wrong:** an `isinstance` chain with seven `return`s fails ruff.
**How to avoid:** assign in a `match` and return once (the vocabulary style).

### Pitfall 11: CONTEXT names `user_message`; the function is `error_message`
`vocabulary.py:438`. Its docstring says it is deliberately unwired (Phase 30); add only the new category arm.

## Code Examples

### `describe()` helper for D-08's empty-message rule
```python
# exceptions.py
def describe(exc: BaseException) -> str:
    """Return ``str(exc)``, or the exception's class name when that is empty (D-08)."""
    return str(exc) or type(exc).__name__
```
Verified need: `str(httpx.ReadTimeout(""))` is `''`.

### SANE option assignment naming the option
```python
for name, value in (("source", effective_source), ("mode", settings.mode), ("resolution", settings.resolution)):
    try:
        setattr(dev, name, value)
    except Exception as exc:  # _sane.error or AttributeError from python-sane
        msg = f"Could not set {name} to {value!r} on {device_id}: {describe(exc)}"
        raise ScanError(msg) from exc
```
(`source` only when `has_source_option`. `setattr` with a Protocol-typed `dev` type-checks in both
checkers. If not, write three explicit try blocks.)

### Parametrised boundary test shape (success criterion 1)
```python
@pytest.mark.parametrize("error_cls", [img2pdf.AlphaChannelError, img2pdf.ExifOrientationError,
    img2pdf.ImageOpenError, img2pdf.JpegColorspaceError, img2pdf.NegativeDimensionError,
    img2pdf.PdfTooLargeError, img2pdf.UnsupportedColorspaceError])
def test_img2pdf_errors_become_pdf_error(monkeypatch, tmp_path, error_cls, sample_pil_images):
    original = error_cls("boom from img2pdf")
    def raising(*_a, **_kw): raise original
    monkeypatch.setattr("saneless.pdf.img2pdf.convert", raising)
    with pytest.raises(PdfError, match="boom from img2pdf") as excinfo:
        assemble_pdf(sample_pil_images, tmp_path, "x.pdf", dpi=300)
    assert excinfo.value.__cause__ is original
```
httpx rows use `httpx.MockTransport` raising `ReadError`, `WriteError`, `RemoteProtocolError`,
`ConnectTimeout`, `UnsupportedProtocol` (precedent `tests/test_paperless.py:35-37`). SANE rows use
`tests/fake_sane.py` raising an exception class standing in for `_sane.error`.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| paperless-ngx fails every checksum duplicate | v3.x consumes duplicates by default; rejects only with `CONSUMER_DELETE_DUPLICATES`, as a FAILURE task with `duplicate_of` | before v3.1.3 (exact version not pinned down) | D-10's "retry fails as a duplicate" holds only with that setting; otherwise a lost-response retry silently creates a second document |
| `TOMLDecodeError(msg)` free-form | `TOMLDecodeError(msg, doc, pos)` with `lineno`/`colno` | Python 3.14 | Free-form construction warns |
| click `mix_stderr` | `result.stdout`/`result.stderr` always separate | click 8.2 | Tests read `result.stderr` |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A `KeyboardInterrupt` during a live ADF read delays process exit until the in-flight `next(iterator)` returns (non-daemon executor thread joined at exit) | Pitfall 6 | Only the troubleshooting wording; HARD-03 owns the fix |
| A2 | `setattr(dev, name, value)` on the `SaneDevice` Protocol passes ty and pyrefly | Code Examples | Fall back to three explicit try blocks |
| A3 | The paperless-ngx version that switched duplicates to "consume by default" is some 3.x release before 3.1.3 (not pinned) | State of the Art | Wording only |

## Open Questions

1. **Duplicate-on-retry on current paperless-ngx (for the user).**
   - What we know: on v3.1.3, a duplicate is only rejected (FAILURE, "It is a duplicate of document #N") when `CONSUMER_DELETE_DUPLICATES` is on. By default the retried upload becomes a second, silent copy.
   - What's unclear: whether D-10's accepted risk should also be documented as "may create a duplicate document" for default v3 installs.
   - Recommendation: implement the duplicate wording for the FAILURE case as locked. Add one sentence to `consume-directory-fallback.md`/the troubleshooting page. No code change beyond D-10.
2. **`serve` exit codes that do not fit D-07.** `serve`'s port-bind failure is `ClickException` exit 1, documented at `cli-commands.md:126` as "Port bind error", but D-07 row 1 means "Scan error". Separately, `uvicorn.run` exits 3 on a lifespan startup failure, which collides with "Paperless error".
   - Recommendation: keep bind = 1 (documented meaning; this phase "only adds codes") and document it as a `serve`-specific row. For uvicorn's 3, either document it in the `serve` table ("3 also: the web server failed to start; see the log") or check `server.started` by running `uvicorn.Server` directly. Planner decides; flag it in the doc-truth test so the table is honest.
3. **`StorageError` exit code.** It is a saneless type, not listed in D-07, and `classify_error` gives `UNKNOWN`, so it would print "Unexpected error (StorageError)" and exit 5. Recommendation: accept 5 (a newer or foreign DB is not user-fixable config), or map it to 2 via a new branch. Low impact.
4. **`__cause__` vs Phase 27's `from None` for `ConfigError`.** Criterion 1 wants `__cause__` to be the third-party exception. `TOMLDecodeError`'s `str()` holds no value, so `from exc` is safe for TOML, `OSError` and `UnicodeDecodeError`. `ValidationError` must stay `from None` (it embeds inputs). No user decision needed; state it in the plan.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | all | yes | 3.14.2 | — |
| uv | all commands | yes | project venv | — |
| libsane + python-sane | sane_hardware tests, real import | yes | libsane.so.1, python-sane 2.9.2 | fakes (`tests/fake_sane.py`) |
| Real scanner | none (do not use) | an HP LaserJet 3030 (`hpaio`) is visible to `sane.get_devices()` on this host | — | **Tests must never open it**: use fakes / SANE `test` backend |
| Chromium via Playwright | CANCELLED styling tests | yes (playwright 1.58.0) | — | — |
| mkdocs | nav check (`uv run mkdocs build --strict` if used) | yes | 1.6.1 | — |

**Missing dependencies with no fallback:** none.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 (+ pytest-timeout, pytest-playwright 0.7.2) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (filterwarnings=error, strict markers, timeout 60) |
| Quick run command | `uv run pytest tests/test_vocabulary.py tests/test_cli.py tests/test_paperless.py tests/test_pdf.py -x -q -m "not browser and not sane_hardware"` |
| Full suite command | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m sane_hardware && uv run pytest -m browser` |
| Gates | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests && uv run prek run --stage pre-push --all-files` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EXC-01 | SANE errors (`open`, each option set, `start`/`snap`, `get_devices`, `get_options`, `init`) → `ScanError` with device/option in message, `__cause__` original | unit (parametrised) | `uv run pytest tests/test_scanner.py -k boundary -x` | extend ✅ |
| EXC-01 | httpx `ReadError`/`WriteError`/`RemoteProtocolError`/`ConnectTimeout` retry then fallback; `UnsupportedProtocol` immediate; ctor `InvalidURL`; JSON decode; get_tags/correspondents; poll continues through transport errors to the deadline | unit (MockTransport) | `uv run pytest tests/test_paperless.py -x` | extend ✅ |
| EXC-01 | seven img2pdf classes + `ValueError` + `OSError` + `SystemError` + `RuntimeError(None)` → `PdfError` | unit (parametrised, 7+ rows) | `uv run pytest tests/test_pdf.py -x` | extend ✅ |
| EXC-01 | TOML syntax / unreadable / non-UTF-8 → `ConfigError` with header and line/column; tomlkit parse → `ConfigError` | unit | `uv run pytest tests/test_config.py tests/test_auto_profiles.py -k toml -x` | extend ✅ |
| EXC-02 | bad config (2), broken scanner (1), unreachable Paperless (3), unassemblable PDF (4), python-sane missing (2, hint), unexpected (5), Ctrl-C (130), `--help` with no sane (0) | CliRunner | `uv run pytest tests/test_cli.py -k exit -x` | extend ✅ |
| EXC-02 | doc tables in `cli-scripting.md`/`cli-commands.md` list every `ExitCode` | doc-truth | `uv run pytest tests/test_deployment_config.py -k exit -x` | extend ✅ |
| EXC-03 | empty batch → "No pages were scanned" (detection on and off, duplex pass A and pass B); all removed → "All pages were blank"; feeder-empty message unchanged | unit | `uv run pytest tests/test_pipeline.py -k "pages" -x` | extend ✅ (rewrite `:421`) |
| EXC-04 | web Abort → `CANCELLED`, INFO, no traceback, not degraded; timeout → ERROR; shutdown → ERROR RESTART_REASON; CLI `n`/Ctrl-C/EOF → 130; prompt failure → 1 | unit + CliRunner | `uv run pytest tests/test_worker.py tests/test_cli.py -k "cancel or abort" -x` | extend ✅ |
| EXC-04 | CANCELLED renders muted, not red, in light/dark/forced-dark; AA contrast; Scan button re-enables; history refresh | browser (Playwright) | `uv run pytest tests/test_browser.py -m browser -k cancel` | extend ✅ |
| EXC-04 | `JobState` has ten members; partition; labels | unit | `uv run pytest tests/test_vocabulary.py -x` | extend ✅ |
| EXC-05 | classified and UNKNOWN failures logged with `exc_info` whose value is the raised exception | unit (caplog) | `uv run pytest tests/test_worker.py -k exc_info -x` | extend ✅ |

### Sampling Rate
- **Per task commit:** the quick run command plus the file-specific command above.
- **Per wave merge:** full non-browser suite plus `ty` and `pyrefly check src tests`.
- **Phase gate:** full suite including `-m browser` and `-m sane_hardware`, then `prek --stage pre-push --all-files`, before `/gsd-verify-work`.

### Wave 0 Gaps
- [ ] No new test framework or fixture files are required. All test modules exist.
- [ ] Optional: a small `tests/test_exception_boundaries.py` if the planner wants the per-library parametrised tests in one place rather than spread across four modules. Either satisfies criterion 1.
- [ ] A CANCELLED job fixture helper for `test_web_state_rendering.py`/`test_browser.py` (mirror the FALLBACK fixture used at `tests/test_browser.py:1076-1093`).

## Security Domain

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | — |
| V3 Session Management | no | — |
| V4 Access Control | no (flip routes unchanged) | — |
| V5 Input Validation | yes | Paperless bodies: parse JSON, render one line, cap ~200 characters; TOML key names escaped |
| V6 Cryptography | no | — |
| V7 Error Handling and Logging | **yes (core of phase)** | One-line user messages; tracebacks to the log only; never log secrets |
| V8 Data Protection | yes | Token (`SecretStr`, CFG-05) never in messages/logs |

### Known Threat Patterns
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Token disclosure via `TOMLDecodeError.doc` in a rendered or logged message | Information disclosure | Render `lineno`/`colno`/`msg` only; never `doc`; test that the token string is absent from the message (precedent `_assert_value_absent`, `tests/test_config.py:797`) |
| Token disclosure via httpx exception text | Information disclosure | httpx messages do not include headers (measured messages above); the base URL is shown but the `Authorization` header is not. Do not interpolate `exc.request.headers`. `-v` does not raise httpx to DEBUG (`logging_config.py:75-80`, T-27-23) |
| Upstream body flooding `job.error`/UI/terminal (5 KB HTML, M-17) | DoS / spoofing of UI text | D-09 one-line ~200 character renderer; full body at DEBUG only; the template autoescapes `job.error` |
| Terminal-escape injection via SANE device names or TOML keys in one-line CLI output | Tampering | Device names come from SANE; use `!r` for option values as in D-08's example; TOML keys through `_escape_name` |
| Log traceback of `ValidationError` embedding inputs | Information disclosure | Keep `raise ConfigError(...) from None` for `ValidationError` (Phase 27 D-14) |
| Last-resort handler printing arbitrary `str(exc)` of a non-saneless exception | Information disclosure (low) | Accepted by D-06; the log file already receives it |

## Doc Surfaces to Change (exact)

| File:line | Change |
|---|---|
| `docs/how-to/troubleshoot-a-failed-scan.md` | **new** (D-13) |
| `mkdocs.yml:44-50` | add under How-To Guides |
| `docs/how-to/cli-scripting.md:56-58` | state list gains `CANCELLED`; add the label "Cancelled" to the human-label list |
| `docs/how-to/cli-scripting.md:73-78` | rows 4, 5, 130; row 1 example "no pages scanned" still true |
| `docs/how-to/cli-scripting.md:90-92` | abort at the flip prompt → exit 130; timeout stays 1 |
| `docs/reference/cli-commands.md:11-15` | global note: every command exits 5 on unexpected error, 130 on Ctrl-C (not serve) |
| `docs/reference/cli-commands.md:31-38` | scan: row 1 loses "manual duplex aborted"; rows 2 (no scanner found, python-sane missing), 4, 5, 130 |
| `docs/reference/cli-commands.md:40` | abort/Ctrl-C/EOF → cancelled, exit 130; prompt read failure → exit 1 |
| `docs/reference/cli-commands.md:57-61` (devices), `:100-104` (jobs), `:121-126` (serve), `:142-148` (auto-profiles) | per-command rows |
| `docs/reference/web-api.md:101` | states list gains `CANCELLED` |
| `docs/reference/web-api.md:161` | Abort ends `CANCELLED` with the cancel message |
| `docs/how-to/set-up-adf-duplex.md:95` | message, exit 130; prompt failure exit 1; web Abort shows Cancelled |
| `docs/explanation/architecture.md:30` | "An abort or a timeout fails the job" → abort cancels, timeout fails |
| `docs/explanation/architecture.md:56` | "an aborted flip" removed from failure list; add the cancel ending |
| `docs/explanation/architecture.md:46` | (pre-existing inaccuracy: "auth failure" does not fall back, a 4xx raises at once; fix while there) |
| `docs/explanation/consume-directory-fallback.md:11, 54` | which upload failures retry and fall back (every transient transport error; malformed URL no retry); duplicate caveat |
| `docs/explanation/empty-page-detection.md` (~line 60-70) | add the "All pages were blank" behaviour; there is no existing sentence to replace |
| `docs/how-to/install-bare-metal.md:75-95` | python-sane-missing symptom + cross-link |
| `docs/how-to/scanner-host-discovery.md:70` | cross-link |

## Sources

### Primary (HIGH confidence)
- Project venv probes (2026-09-15): httpx 0.28.1, img2pdf 0.6.3, pillow 12.1.1, python-sane 2.9.2, click 8.3.1, uvicorn 0.42.0, pydantic-settings 2.13.1, tomlkit 0.14.0, CPython 3.14.2 `tomllib/_parser.py`
- Context7 `/encode/httpx`: exception hierarchy (`_exceptions.py` docstring), quickstart exceptions
- Code read: `src/saneless/{cli,pipeline,worker,vocabulary,job,paperless,pdf,config,auto_profiles,logging_config,exceptions}.py`, `scanner/{base,sane_backend}.py`, `web/{app,routes}.py`, templates, `app.css`, vendored `pico-2.1.1.min.css`
- paperless-ngx source at tag v3.1.3 (raw.githubusercontent.com): `src/documents/consumer.py`, `tasks.py`, `serialisers.py`, `signals/handlers.py`; v2.17.1 `consumer.py`

### Secondary (MEDIUM confidence)
- Django REST framework API guide, exceptions: django-rest-framework.org/api-guide/exceptions/
- GitHub discussions paperless-ngx #3075, #4711 (duplicate message examples)

### Tertiary (LOW confidence)
- A1 (executor join delaying exit), not measured

## Metadata

**Confidence breakdown:**
- Exception facts: HIGH (executed)
- Architecture/patterns: HIGH for click/uvicorn (executed); MEDIUM for Pattern 4's exact API shape (a design recommendation)
- Pitfalls: HIGH except A1
- Paperless duplicate behaviour: HIGH for v3.1.3 and v2.17.1 source; version boundary unpinned

**Research date:** 2026-09-15
**Valid until:** 2026-10-15 (pins are stable; paperless-ngx moves faster, so re-check duplicate handling if the pin changes)
