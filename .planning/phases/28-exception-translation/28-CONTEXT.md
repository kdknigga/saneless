# Phase 28: Exception Translation - Context

**Gathered:** 2026-09-15
**Status:** Ready for planning

<domain>
## Phase Boundary

No third-party exception type escapes a module boundary, and no user ever sees a traceback.
Delivers EXC-01..EXC-05 (review findings M-17, N-06, N-08):

- Every SANE (`_sane.error`), httpx, img2pdf (all seven error classes, plus the `ValueError` /
  Pillow `OSError` / `RuntimeError` paths through `assemble_pdf`) and `tomllib` / `tomlkit`
  exception is caught at its call site and re-raised as a saneless type that carries the original
  message (EXC-01)
- The CLI prints one line and exits with a documented non-zero code for every failure, including a
  missing `python-sane` and an unexpected bug (EXC-02)
- Zero pages says "No pages were scanned"; "All pages were blank" only when empty-page detection
  actually removed every page; never a bare `ValueError` (EXC-03)
- An explicit abort at the flip prompt is a **cancelled** job, not a scanner failure (EXC-04)
- The worker logs every job failure with `exc_info` (EXC-05)
- A new troubleshooting how-to, and every doc sentence this phase makes true or false corrected
  in-phase

**Not in this phase:**
- Plain-language messages with a next step derived from `ErrorCategory`, and the collapsed
  "Technical details" disclosure: APPL-04, **Phase 30**. This phase keeps `job.error` as the
  (now well-formed) technical message; `user_message()` stays unwired except for the new members
  the total `match` forces it to gain.
- Keeping the pages when PDF assembly fails (they are still in memory only): HARD-01 spooling,
  **Phase 29**. A `PdfError` today still loses the scan; this phase only names it correctly.
- Keeping the N pages acquired before a mid-batch error (HARD-02), waiting for a cancelled SANE
  read before close so Ctrl-C mid-read is device-safe (HARD-03), flatbed/ADF timeout parity
  (HARD-04), the `sane.init()` guard (HARD-05): **Phase 29**. This phase only gives Ctrl-C a
  one-line message and exit code, not a safe device cancel.
- Mid-pass abort (aborting during pass B): **Phase 29** (Phase 25 D-16).
- Logging the cause in the web metadata-fetch catch and stale-on-error cache: SWP-04, **Phase 32**.
- Validating `paperless.url` at load time (`AnyHttpUrl`, M-17 item 2): no v2.0 requirement maps
  it. See Deferred.

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)
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

### Cancelled jobs (EXC-04, N-08)
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

### PdfError and exit codes (EXC-02)
- **D-04: `PdfError(SanelessError)` is a sibling of `ScanError`, with its own `ErrorCategory` and
  exit code 4.** The job record stops saying the scanner failed when the disk was full.
  `classify_error` gains the new branch (ordered `isinstance` chain, `vocabulary.py:661`) and
  `error_message` (`vocabulary.py`, referred to as the user-message map) gains the total-match entry. Everything `assemble_pdf` can raise becomes
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
  - **`serve` joins the same table (user decision 2026-09-15, after research).** A port-bind
    failure (documented today as exit 1) and a uvicorn startup failure (which uvicorn exits with
    3, colliding with "3 = Paperless error") are both caught and exit **2**, "can't start, fix
    your setup". Every command shares one table; the `serve` exit-code docs change accordingly.
    Ctrl-C on `serve` already exits 0 through uvicorn and needs no special case (measured).
  - "One line" means one *message*: a configuration error keeps Phase 27's header plus one line
    per problem (D-12), which success criterion 2 accepts.

### Wrapped message shape and retries (EXC-01)
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
  - Accepted risk (**corrected after research, re-confirmed by the user 2026-09-15**): when
    Paperless received the upload but the response was lost, a retry can **silently create a
    second copy** of the document. Current paperless-ngx (v3.1.3, verified in source) consumes a
    duplicate as a new document by default and fails it as a duplicate only when
    `CONSUMER_DELETE_DUPLICATES` is set. The user accepted the copy: a duplicate is easy to
    delete, a lost scan is not. Where Paperless *does* report a duplicate (failure text containing
    "duplicate of", or `duplicate_of` in `result_data`, covering v2 and v3), the failure message
    says the document may already be in Paperless, so the user checks before rescanning.
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

### Zero pages (EXC-03), locked by requirement
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

### Worker failure logging (EXC-05), locked by requirement
- Every job failure is logged with `exc_info` (`logger.exception` or `exc_info=True`), for
  classified categories as well as `UNKNOWN`. It replaces `logger.error("Job %s failed (%s): %s")`
  (`worker.py:1335-1337`). A cancel (D-01) logs at INFO without a traceback. A shutdown ending
  keeps its INFO line.

### Troubleshooting docs
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
- **Name of the PDF `ErrorCategory`** (e.g. `ASSEMBLY` or `PDF`) and its `error_message` sentence.
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

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/REQUIREMENTS.md` lines 111-117: EXC-01..EXC-05 verbatim
- `.planning/REQUIREMENTS.md` APPL-04 (line ~132), HARD-01..05 (lines ~121-125), SWP-04: the
  neighbouring requirements that fence this phase
- `.planning/ROADMAP.md` § "Phase 28: Exception Translation": goal and four success criteria
- `.planning/ROADMAP.md` line ~23: documentation is cross-cutting; correct the sentences in-phase

### Review findings (the source of every EXC requirement)
- `.planning/reviews/2026-09-09-code-review.md` § M-17 (lines 573-594): every leak site, the
  seven-step fix list. Item 2's URL validation is deferred; item 5's "at least when UNKNOWN" is
  widened to every failure by EXC-05.
- same file N-06 (line 849): zero pages / bare `ValueError`
- same file N-08 (line 853): cancel recorded as scanner failure; "do not log it at ERROR level"
- same file U-05 (line ~1158): the plain-language reframing, which is Phase 30's, not this phase's
- same file § "Step 6" (line 1074): the remediation summary

### Prior phase decisions carried forward
- `.planning/phases/25-manual-duplex/25-CONTEXT.md` D-09 (three `FlipOutcome`s), D-15 (abort
  raises plain `ScanError`; classification is Phase 28's), D-16 (a flip answer is final)
- `.planning/phases/24-scanner-truthfulness/24-CONTEXT.md` D-03 (truthful feeder-empty message;
  see the EXC-03 reconciliation note)
- `.planning/phases/26-worker-and-web-robustness/26-CONTEXT.md` D-01/D-04 (one error renderer),
  D-10 (worker guard, degraded health; a cancel must not count), D-15 (name exception classes, never
  guess causes)
- `.planning/phases/27-configuration-strictness/27-CONTEXT.md` D-10/D-14 (config error header,
  exit 2, no values echoed), CFG-10 (`--help` without config)
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md`: `FALLBACK` as its own
  terminal state (the precedent for D-01), preservation guard, OUTC-07 poll deadline
- `.planning/phases/23.1-dark-mode-and-the-commit-gate/23.1-UI-SPEC.md`: status colour tokens and
  the dark-scheme override convention the CANCELLED styling follows
- `.planning/UI-SPEC.md`: project status-class conventions

### Docs to create or correct in-phase
- **New:** `docs/how-to/troubleshoot-a-failed-scan.md` + `mkdocs.yml` nav (D-13)
- `docs/how-to/cli-scripting.md` lines 69-78: exit-code table gains 4, 5, 130
- `docs/reference/cli-commands.md`: every command's exit-code table (lines ~31, 57, 100, 121, 142);
  line 36 (manual duplex abort is now 130, not 1); line 40 (abort / Ctrl-C wording)
- `docs/how-to/set-up-adf-duplex.md` (manual duplex section): abort is recorded as Cancelled
- `docs/reference/web-api.md`: the `CANCELLED` job state
- `docs/explanation/consume-directory-fallback.md`: which upload failures now retry and fall back
  (D-10)
- `docs/how-to/install-bare-metal.md` § Troubleshooting: python-sane-missing message, cross-link
- `docs/how-to/scanner-host-discovery.md` § Troubleshooting: cross-link
- `docs/explanation/architecture.md`: exception boundaries and the CANCELLED terminal state, where
  it describes them
- `docs/explanation/empty-page-detection.md`: "All pages were blank" wording (EXC-03)

### External references
- img2pdf 0.6.x: the seven exception classes listed in D-04 (verified via `dir(img2pdf)` in the
  project venv)
- httpx exception hierarchy: `TransportError` subclasses (`ConnectError`, `ReadError`,
  `WriteError`, `RemoteProtocolError`, `PoolTimeout`, `UnsupportedProtocol`, …) vs `InvalidURL`
  (not a `TransportError`). Use Context7 to confirm against the pinned httpx version.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/exceptions.py`: `SanelessError` → `ConfigError`, `ScanError` → `FeederEmptyError`,
  `PaperlessError` → `PaperlessTimeoutError`, `StorageError`. `PdfError` and the cancel exception
  are added here.
- `classify_error` (`vocabulary.py:661-686`): ordered `isinstance` chain with an `UNKNOWN` fallback;
  gains the PDF (and, if the cancel exception flows through it, cancel) branches in the right order.
- `user_message` (`vocabulary.py:~440-480`), `state_label` (`vocabulary.py:274`),
  `TERMINAL_STATES` (`vocabulary.py:253`): total `match` + `assert_never`, so the type checkers
  list every site a new member must reach.
- `ScanWorker._failure_record` + `WorkerFlipCoordinator.aborted_by_shutdown` (`worker.py:941-963`):
  the existing seam that already separates "shutdown" from "failure"; CANCELLED is the third ending.
- `ClickFlipCoordinator` (`cli.py:~90-158`): already maps `n` / Ctrl-C / `click.Abort` to
  `ABORTED` and a broken prompt (WR-08) to `ABORTED` with a logged traceback. D-02 separates the
  latter.
- `_truncated_body` (`paperless.py:126-142`) and `_failure_message` (`paperless.py:97`): body and
  task-failure renderers to reconcile with D-09.
- `_ensure_sane` (`sane_backend.py:50-56`): the lazy `import sane`; the D-05 check can reuse its
  import attempt.
- `_load_cli_settings` (`cli.py:193-246`): the single config-load error handler (exit 2) that D-12
  narrows.

### Established Patterns
- `raise SanelessType(msg) from exc`, with `msg` built in a variable first (ruff `EM`/`TRY` rules
  are active).
- Total enums with `match` + `assert_never` for any new state-like value (JobState, ErrorCategory,
  the exit-code definition).
- Docstrings explain the "why" and cite finding IDs (`M-17`, `N-06`, `N-08`, `D-0x`); match the
  density in `worker.py` / `pipeline.py`.
- No `# type: ignore` / `# noqa` additions; `ty` and `pyrefly check src tests` both clean. TDD RED
  commits are allowed.
- Log with the real exception (`exc_info`), never a guessed cause (Phase 26 D-15).
- Doc-truth tests pin documentation claims to code (Phase 27 plan 08).

### Integration Points (current leak sites)
- `scanner/sane_backend.py`: `sane.open`, each option assignment, `start()` / `snap()`; the
  broad catch at `:536`, `:741-743`.
- `paperless.py`: upload loop `:270-330` (retry set `:289`, 4xx body `:300-306`, exhausted message
  `:328`), `poll_task` `:385-460` (no transport handling), `get_tags` `:509`, `get_correspondents`
  `:525`. `test_connection` `:492` already catches `TransportError`.
- `pdf.py:130-202` `assemble_pdf`: `img2pdf.convert`, PNG saves, the `RuntimeError` at `:197`.
- `pipeline.py`: flip `match` `:948-966` (ABORTED → cancel exception; TIMED_OUT stays `ScanError`);
  `_drop_empty_pages` `:474-506`; zero-page precondition before `assemble_pdf` (`:734`, `:740`,
  `:1220`); "No scanner found" `ConfigError` `:1079`.
- `worker.py:1313-1338`: job-level catch → CANCELLED vs ERROR, `exc_info` logging.
- `cli.py`: `scan` catches `:317-322` (only `ScanError` / `PaperlessError` today); `devices`
  `:~383`, `auto-profiles` `:~496-505`, `jobs`, `serve` `:~569-595` all construct `SaneBackend` or
  the store; a last-resort handler and a `KeyboardInterrupt` → 130 path wrap them.
- `auto_profiles.py:640-720`: tomlkit parse / tomllib re-parse on the write path.
- `web/templates/partials/status.html:21-27`, `partials/history.html:7`: state → CSS class
  branches gain CANCELLED; `web/static/*.css` status tokens.
- Tests: `tests/test_cli.py`, `tests/test_paperless.py`, `tests/test_pdf.py`,
  `tests/test_pipeline.py`, `tests/test_worker.py`, `tests/test_sane_backend*.py`,
  `tests/test_config.py`, `tests/test_browser.py` (CANCELLED colour), doc-truth test module.

</code_context>

<specifics>
## Specific Ideas

- Message shapes the user accepted:
  `Could not set mode to 'Lineart' on epson2:libusb:001:004: Invalid argument`,
  `Could not reach Paperless at http://paperless:8000: [Errno 111] Connection refused`,
  `Paperless rejected the upload (400 Bad Request): title: This field may not be blank.`,
  `Unexpected error (<ExceptionType>): <message>. Full details in <log_file>`,
  `Scan cancelled at the flip prompt`.
- The exit-code table in D-07 is the contract for the docs, the doc-truth test, and every command.
- A cancel is recorded, shown and exited as a deliberate stop in every layer: CANCELLED state,
  neutral styling, INFO log, exit 130. It is never red, never ERROR, never a traceback.
- The troubleshooting page's error-5 section tells the user to attach the log file to a bug report.

</specifics>

<deferred>
## Deferred Ideas

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

No pending todos matched this phase.

</deferred>

---

*Phase: 28-exception-translation*
*Context gathered: 2026-09-15*
