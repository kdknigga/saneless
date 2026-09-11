# Phase 23: Honest Outcomes and Never Lose a Scan - Context

**Gathered:** 2026-09-11
**Status:** Ready for planning

<domain>
## Phase Boundary

A job's recorded state is always the truth, and a scanned document is never destroyed by a
downstream failure.

Concretely, this phase closes five gaps that exist in the code today:

1. **`worker.py:244-250` discards the pipeline's answer.** It calls `run_pipeline(...)`, throws the
   returned `ScanResult` away, and writes `JobState.DONE` unconditionally. Every fact the pipeline
   computed — the outcome, the three page counts, the duplex-mismatch warning — is lost. This is
   C-03 in one line, and it is the single most important thing this phase fixes.
2. **`paperless.py:210-248` never raises.** `poll_task` returns `FAILURE` as a dict, returns
   `{"status": "TIMEOUT"}` on timeout, silently re-loops any non-200, and accumulates `elapsed` from
   sleep time only so the documented 300 s timeout is unbounded in wall clock. `pipeline.py:521-527`
   then ignores the return value entirely and sets `outcome = ScanOutcome.SUCCESS` regardless.
3. **`pipeline.py:448` wraps the whole run in `TemporaryDirectory`.** Any raise after assembly
   unwinds it and deletes the finished PDF.
4. **`pdf.py:55` hardcodes `output.pdf`.** Every document reaches paperless with that original
   filename, and every consume-directory copy (`paperless.py:202-203`) collides with the last one.
   `img2pdf.convert(image_paths)` at `pdf.py:56` passes no layout function, so an A4 page scanned at
   300 DPI gets a 2480 x 3508 pt MediaBox instead of 595 x 842.
5. **The job database lives in the disposable temp directory** (`cli.py:227`, `web/app.py:59`), and
   `docker-compose.yml:21` mounts the durable `saneless-data` volume *at* `/tmp/saneless` — the
   deployment the docs recommend has its durable volume and its scratch space as the same directory.

Requirements in scope: OUTC-01 through OUTC-10.

**Not in this phase:** page-image spooling to disk (Phase 29, M-08), `$XDG_STATE_HOME` expansion
(Phase 27), the `kris-knigga` -> `kdknigga` image rename in `docker-compose.yml` (Phase 31),
read-back DPI from the device and real SANE error messages (Phase 24), plain-language error display
(Phase 30).

</domain>

<decisions>
## Implementation Decisions

All sixteen decisions below are **locked for planning**. A planner may implement them, not
relitigate them. A planner who believes one is wrong should raise it rather than silently implement
the other branch. Where a choice diverges from the option flagged "Recommended" during discussion it
is called out explicitly (**D-14** is the only one).

### Outcome vocabulary

- **D-01: `ScanOutcome` gains no `FAILED` member. Failures raise.** `outcome` is
  `SUCCESS | FALLBACK`, and stays `NULL` on the error path; `JobState.ERROR` carries the failure.
  Rationale: Phase 21's D-07 deferred this decision here precisely because a *returned* `FAILED` is
  unreachable once `poll_task` raises. Writing `FAILED` from the worker's `except` branch would make
  the member reachable, but it would encode the same fact as `JobState.ERROR` in a second column —
  two columns that can disagree, about a job that has exactly one fate. The comment in
  `vocabulary.py:57-60` that reserves the question should be replaced with the answer.

- **D-02: `JobState.FALLBACK` is added, and a total `ScanOutcome -> JobState` function maps onto it.**
  The function lives in `vocabulary.py` behind `match` + `assert_never`, mirroring
  `PipelineEvent.job_state` at `pipeline.py:48-87`. `SUCCESS -> DONE`, `FALLBACK -> FALLBACK`.
  Rationale: Phase 21's D-08 *measured* that a `dict[Enum, str]` missing a member produces no
  diagnostic from either `ty` or `pyrefly`, while the same enum in a `match` with `assert_never` is
  caught by both. An inline `if result.outcome is ScanOutcome.FALLBACK: ... else: ...` in the worker
  would let a future third member fall silently into the `else`. Behind `assert_never`, it fails the
  Phase 20 CI gate at edit time.

- **D-03: `FALLBACK` joins `TERMINAL_STATES`.** Not a choice — Phase 21's D-09 parametrised test
  asserts `(state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)` over `list(JobState)`, so the new
  member must land in exactly one set, and the job is finished. It must also gain arms in
  `state_label()`, `progress_label()`, and any other total function over `JobState`; the D-09 tests
  will fail until it does, which is the intended mechanism.

- **D-04: a new `finish_job()` on `JobStore` performs the terminal write.** One `@_locked` method
  that sets `state`, `outcome`, `warning` and the three page counts together, plus `error` and
  `error_category` on the failure path. `update_state` keeps its current signature and **never names
  the six Phase 22 result columns**.
  Rationale: `update_state`'s SQL at `job.py:616-624` is an unconditional
  `SET state = ?, error = ?, error_category = ?` — it writes `NULL` to `error` and `error_category`
  on every call. Extending it with six more columns would blank the result columns on every in-flight
  `SCANNING`/`ASSEMBLING`/`UPLOADING` transition, unless the SQL became conditionally built, which is
  exactly the ad-hoc schema drift Phase 22 spent a phase eliminating. This is the write path Phase
  22's D-07 deliberately left unbuilt ("no production code writes them in this phase").

- **D-05: a `FALLBACK` job reads "Saved to folder" and is styled as a warning.** Amber/warning
  treatment in the status partial and the history table, visually distinct from both the green
  Complete and the red Failed, and distinct in `saneless jobs` output. The job's `warning` column
  carries the detail that title, tags and correspondent were **not** applied — which
  `docs/explanation/consume-directory-fallback.md:62` already documents as the real consequence.
  Rejected: "Complete (fallback)", which buries a real degradation inside the word Complete and is
  close kin to the silent-DONE behaviour this phase exists to remove; and "Needs attention", which
  overstates it — nothing was lost and no rescan is needed. Label register matches the existing
  short title-case labels (`DONE` -> "Complete", `ERROR` -> "Failed").

### Preservation

- **D-06: one `try/except` spanning `upload_document` and `poll_task`, inside `run_pipeline`'s
  `with` block.** On any exception: `shutil.move` the PDF to `<data_dir>/failed/`, then re-raise with
  the destination path named in the message. This is the literal OUTC-04 shape and the roadmap's own
  note flags the trap — wrapping only the upload call means a correct FAILURE raise unwinds the
  `TemporaryDirectory` and deletes the document this phase exists to protect.
  - **`shutil.move`, not `Path.rename` / `os.replace`.** Once `data_dir` and `tmp_dir` are separate
    settings they can sit on different filesystems, and `rename` raises
    `OSError: [Errno 18] Invalid cross-device link` across a device boundary. `shutil.move` falls
    back to copy-then-unlink.
  - Considered and rejected: assembling the PDF *outside* the temporary directory so that "kept" is
    the default and success is what deletes. Structurally stronger — no guard can be mis-scoped — but
    it reshapes the consume-directory copy and temp-dir semantics, and a killed process would leave
    garbage needing a startup sweep. Recorded here so the planner does not re-derive it as an
    improvement.
  - Also rejected: a broad guard from `assemble_pdf` to the end of the block, which would catch
    failures in code unrelated to delivery and report them in the job error as delivery failures.

- **D-07: only the PDF is preserved; page images are not.** OUTC-04's phrase "no page images or PDF
  are deleted on that path" is satisfied by the PDF, because img2pdf assembly is lossless and the PDF
  is therefore a complete record of every page image. Note that `assemble_pdf`'s own nested
  `TemporaryDirectory` at `pdf.py:47` already deletes every page PNG *before it returns*, so the page
  images are gone before upload begins regardless. Changing that means giving the pipeline ownership
  of the page files, which is Phase 29's M-08 ("pages spooled to disk in explicit order").
  **The verifier must not read OUTC-04 as requiring PNGs in `failed/`.**

- **D-08: the duplex-mismatch path gets full parity.** `_handle_duplex_mismatch`
  (`pipeline.py:188-249`) today uploads two PDFs and polls neither, and its warning dies with the
  discarded `ScanResult`. In this phase: both partial PDFs sit inside the same preservation guard,
  both tasks are polled, and outcome + warning + page counts are persisted. Rationale: this path is
  already an anomaly, so it is the one most likely to hold a document the user needs; leaving it
  unpolled means a paperless `FAILURE` on either half is still recorded as delivered, which is C-03
  surviving in a corner. OUTC-03 and OUTC-10's parametrised "duplex mismatch" case both live in this
  phase.

- **D-09: the PDF is uniquely named at assembly, not at the copy sites.** Add `job_id` to
  `PipelineRequest` (`pipeline.py:93-104`), give `assemble_pdf` a filename argument, and build the
  name once — timestamp + job id + sanitised title, per OUTC-05. The consume-directory copy
  (`paperless.py:202`) and the `failed/` move then use `pdf_path.name` and inherit uniqueness for
  free: one naming function, one call site. This also fixes what paperless records as each
  document's original filename, which is `output.pdf` for every document today.
  - Sanitisation rule (slug-safe character set, length cap, collision behaviour) is Claude's
    discretion — see below.
  - OUTC-05's second half stands: the consume-directory copy is written to a `.part` file and
    renamed atomically. The rename must happen **inside** the consume directory, not across from
    `tmp_dir`, or it is not atomic and may not even succeed.

### Timeouts and error semantics

- **D-10: the PDF is preserved on FAILURE *and* on TIMEOUT.** A timeout is genuinely ambiguous —
  paperless-ngx's `post_document` returns HTTP 200 immediately with a task UUID and consumption runs
  asynchronously on Celery workers, so a task can legitimately sit in `PENDING`/`STARTED` for minutes
  and a timeout means only that we stopped waiting. The decision errs toward keeping: if the Celery
  worker actually died (OOM, crash, restart) the local PDF is the only copy, and that is the loss
  this phase is named after. The downside — a stray file the user re-ingests — is bounded, because
  paperless detects duplicates by checksum on consumption. The raised error must name the task id so
  the user can check paperless before acting on the preserved file.
  - Considered and rejected: preserving only on FAILURE, on the argument that a 200 + task UUID
    proves paperless has the bytes and OUTC-04's literal trigger ("upload **and** consume-directory
    fallback both fail") is not met by a timeout. Sound reasoning, but it trades a recoverable stray
    file for an unrecoverable lost scan.

- **D-11: `PaperlessTimeoutError` subclasses `PaperlessError`.** Mirrors the existing
  `FeederEmptyError(ScanError)` precedent in `exceptions.py:30-31`. The `isinstance` chain at
  `vocabulary.py:246` and the `except PaperlessError` at `cli.py:142` keep working unchanged, and
  both map to `ErrorCategory.UPLOAD` without a new arm. Callers that need to distinguish a timeout
  narrow to the subclass deliberately.

- **D-12: `poll_task` raises immediately on any non-200 response.** Literal OUTC-07. Today a non-200
  is silently ignored and re-polled until the timeout expires, so a revoked token or a moved endpoint
  burns the full 300 s and is then misreported as a timeout. The raised error carries the status code
  and response body.
  - Rejected: the 4xx-raise / 5xx-retry split that `upload_document` uses at `paperless.py:179-194`.
    It would tolerate a paperless restart mid-poll behind a reverse proxy, but it deviates from
    OUTC-07's "any non-200" wording.
  - The rest of OUTC-07 is mechanical, not a choice: `elapsed` at `paperless.py:227` accumulates only
    sleep time, so the deadline becomes `time.monotonic()`-based and includes request time.
  - The existing "task may not appear immediately after upload" race (Pitfall #8) is a **200 with an
    empty list**, not a non-200, and must keep being tolerated inside the deadline.

- **D-13: `test_connection` returns a `ConnectionStatus` StrEnum defined in `vocabulary.py`.**
  Members: `CONNECTED`, `TOKEN_REJECTED`, `NOT_FOUND`, `SERVER_ERROR`, `UNREACHABLE`, with the
  user-facing message behind `match` + `assert_never` like every other lookup in that module.
  `CONNECTED` only for a 2xx, per OUTC-08 — today `paperless.py:262-268` returns `"connected"` for
  anything that is not 401/403, including 404 and 500.
  - **Wire compatibility is required.** `web/routes.py:128` serialises the return value straight into
    JSON and `docs/reference/web-api.md:55-56` documents the exact strings. `StrEnum` keeps the three
    existing values byte-identical; the two new ones join them and `web-api.md` gains two rows.
  - This is the last stringly-typed protocol to survive Phase 21's N-38 sweep.

### data_dir

- **D-14: `output.data_dir` defaults to `~/.local/state/saneless`, and there is no migration of an
  existing database.** DIVERGES from the option flagged "Recommended" during discussion, which was a
  one-time startup move of `<tmp_dir>/saneless.db`. The user chose the clean start.
  - Rationale for the divergence, as discussed: job history is prunable ephemera (7 days / 500 rows
    by default per `config.py:90-91`), the documents themselves are in paperless, and the scans are
    unaffected — so the loss is bounded. It also avoids a startup filesystem mutation that can fail
    in a container with an unwritable old path, and avoids the WAL correctness burden: `job.py:468`
    enables `PRAGMA journal_mode=WAL`, so the database is up to three files and after an unclean
    shutdown the `-wal` holds committed transactions that a naive `.db`-only move would lose.
  - **The old file is left where it is.** Release notes / upgrade docs mention it so a user who wants
    their history can move it by hand.
  - `<data_dir>/saneless.db` for the database, `<data_dir>/failed/` for preserved scans. One setting
    covers both, per OUTC-09.
  - The default is written in the same hardcoded `Path.home() / ".local" / "state"` style as
    `log_file` at `config.py:86` — deliberately, so Phase 27's XDG expansion changes both in one
    edit. Do **not** read `$XDG_STATE_HOME` in this phase.
  - Considered and rejected: splitting the database into `$XDG_STATE_HOME` and `failed/` into
    `$XDG_DATA_HOME` on the argument that a preserved scan is a user document rather than disposable
    state. More spec-correct, but it costs a second setting, a second Docker volume, and a deviation
    from OUTC-09's single `data_dir`.

- **D-15: the Dockerfile sets the container's `data_dir`; compose mounts the volume there.**
  `ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless` in the image; `docker-compose.yml` repoints the
  `saneless-data` named volume to `/var/lib/saneless`; `/tmp/saneless` stops being mounted at all,
  which is the entire point of separating the two.
  - Rationale: the image runs as root, so an unset `data_dir` resolves to
    `/root/.local/state/saneless` — covered by no volume, gone on every container recreation, taking
    `failed/` scans with it. Baking it into the image means a plain `docker run -v` is correct
    without the operator knowing the env var exists, instead of silently losing data.
  - Whether the Dockerfile also gains a `VOLUME` instruction is Claude's discretion.
  - **Do not touch the `ghcr.io/kris-knigga/saneless` image name** in that file — the rename to
    `kdknigga` is Phase 31's (M-25..M-31).

- **D-16: `db_path` and `failed_dir` become computed `@property` on `OutputConfig` in `config.py`.**
  Defined once, visible to `ty` and `pyrefly`, and read by `cli.py:227`, `web/app.py:59` and the
  pipeline alike. Today the DB path expression is duplicated verbatim in the two entry points, and
  `failed/` would make a third site.
  - `validate_settings_dirs` (`config.py:206-240`) gains a `data_dir` writability check alongside the
    existing `tmp_dir` and `consume_dir` ones, so a bad path fails at startup rather than at
    preservation time — the worst possible moment to discover it.

### Claude's Discretion

The following were raised during discussion and explicitly handed to the planner and executor. Make
the call, and record the reasoning in the plan:

- **The OUTC-10 end-to-end test's shape and hermeticity.** It must drive the real worker and pipeline
  with a stub scanner through SUCCESS, Paperless FAILURE, TIMEOUT, consume-dir fallback and duplex
  mismatch, asserting persisted state, outcome, page counts and file preservation for each. The
  obstacle: `poll_task` sleeps (`paperless.py:243`) and `upload_document` sleeps `2**attempt`
  (`paperless.py:177`, `:194`), so a naive test is slow in CI. `httpx.MockTransport` via the existing
  `_transport` hook (`paperless.py:91`) is the established seam for the paperless side. Phase 32 owns
  the general "no `time.sleep`" sweep (M-34), but this test must be fast *now* — it runs in the Phase
  20 gate on every push.
- **The img2pdf DPI layout function (OUTC-06).** `img2pdf.get_fixed_dpi_layout_fun` is the documented
  mechanism. Which DPI is authoritative is the open question: `profile.resolution` is what we asked
  for, but the device may have delivered something else, and reading DPI back from the device is
  explicitly Phase 24's (SCNR block). Decide and record whether this phase uses the profile value,
  the PIL image's own DPI when present, or a documented preference order — and what happens if pages
  disagree.
- **The exact `FALLBACK` rendering** in the status partial, the history table, and `saneless jobs`,
  within D-05's decision ("Saved to folder", warning treatment). Includes whether the `warning` text
  is shown inline or on hover/detail, and whether `progress_label` needs a meaningful `FALLBACK` arm
  or only a total-lookup placeholder like the existing `DONE`/`ERROR` arms.
- **Which doc sentences get rewritten.** At minimum
  `docs/explanation/consume-directory-fallback.md:66` — Phase 21's D-13 rewrote it once to describe
  what actually happened *then*, and this phase rewrites it again now that a `FALLBACK` job status
  genuinely exists. Also `docs/reference/web-api.md:55-56` (two new `ConnectionStatus` rows),
  anything in `docs/` and `README.md` that describes where the job database lives, and the
  docker-compose volume documentation. The phase rule stands: each phase corrects the sentences that
  described the behaviour it changed, in that same phase.
- **The sanitisation rule for D-09's PDF filename** — character set, length cap, and what happens on
  an empty title after sanitisation.
- **Whether the default `paperless_task_timeout` of 300 s (`config.py:92`) is still right** now that
  a timeout is fatal and triggers preservation rather than being silently swallowed.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/ROADMAP.md` § "Phase 23: Honest Outcomes and Never Lose a Scan" — the five success
  criteria and the note warning that the preservation `try/except` must span both `upload_document`
  and `poll_task`.
- `.planning/REQUIREMENTS.md` § "Honest Outcomes and Never Lose a Scan" — OUTC-01 through OUTC-10,
  with their review-finding tags (C-03, C-04, C-05, M-06, M-22, N-12, N-39, U-08, M-33).
- `.planning/reviews/2026-09-09-code-review.md` — the source findings. C-03, C-04 and C-05 are this
  phase's spine; section 8's doc rows 3 and 4 are the doc claims OUTC-02 makes false.

### Prior-phase decisions that bind this phase
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` — **D-06** (Phase 21 deliberately
  shipped no `JobState.FALLBACK`, leaving it here), **D-07** (`ScanOutcome` shipped `SUCCESS` and
  `FALLBACK` only; `FAILED` deferred here, "together with the code path that produces it, if it
  produces one"), **D-08** (the measured `match`/`assert_never` vs `dict` enforcement finding),
  **D-09** (the parametrised `list(JobState)` completeness tests), **D-13** (the
  consume-directory-fallback doc sentence, rewritten once already).
- `.planning/phases/22-job-store-hardening/22-CONTEXT.md` — **D-07** (the six result columns exist
  with no writers; this phase supplies them), **D-08** (page counts are nullable `INTEGER` where
  `NULL` means "never recorded", not a measured zero), **D-09** (`outcome` is `TEXT` read back as
  `ScanOutcome | None`), **D-12** (`@_locked` on every public `JobStore` method, enforced by a
  reflective test — a new `finish_job` must carry it), **D-19** ("FAILED" in requirement prose means
  the existing `JobState.ERROR`; no rename).

### Source files this phase changes
- `src/saneless/worker.py:244-250` — the discarded `ScanResult` and the unconditional `DONE`.
- `src/saneless/pipeline.py` — `run_pipeline` (`:398-541`), `_handle_duplex_mismatch` (`:188-249`),
  `PipelineRequest` (`:93-104`), the `TemporaryDirectory` at `:448`.
- `src/saneless/paperless.py` — `poll_task` (`:210-248`), `test_connection` (`:250-268`), the
  consume-directory copy (`:196-205`), the `_transport` test seam (`:91`).
- `src/saneless/pdf.py:29-63` — `assemble_pdf`, the hardcoded `output.pdf`, the layout-function gap.
- `src/saneless/vocabulary.py` — `JobState`, `ScanOutcome` (and the reserved-decision comment at
  `:57-60`), `TERMINAL_STATES`, `state_label`, `progress_label`, `classify_error` at `:246`.
- `src/saneless/job.py` — `update_state` (`:597-624`), the `@_locked` decorator (`:415`), WAL at
  `:468`.
- `src/saneless/config.py` — `OutputConfig` (`:82-96`), `validate_settings_dirs` (`:206-240`),
  `log_file`'s hardcoded-home default at `:86`.
- `src/saneless/cli.py:227` and `src/saneless/web/app.py:59` — the duplicated db-path expression.
- `src/saneless/web/routes.py:124-128` — where `test_connection`'s value becomes JSON.
- `Dockerfile`, `docker-compose.yml:15-30` — the `saneless-data` volume currently mounted at
  `/tmp/saneless`.

### Docs whose claims this phase changes
- `docs/explanation/consume-directory-fallback.md` — `:62` (metadata not applied — becomes the
  `warning` text) and `:66` (the job-status claim, rewritten a second time).
- `docs/reference/web-api.md:55-56` — the documented `test_connection` JSON values.

### External
- <https://docs.paperless-ngx.com/api> — `post_document` returns HTTP 200 immediately with the task
  UUID; consumption is asynchronous on Celery. This is why a poll timeout is not a failure (D-10).
- <https://specifications.freedesktop.org/basedir-spec/latest/> — `$XDG_STATE_HOME` defaults to
  `$HOME/.local/state` and is specified for state that persists across restarts but is not important
  enough for `$XDG_DATA_HOME`. Background for D-14; note that reading the env var is Phase 27's.
- img2pdf documentation for `get_fixed_dpi_layout_fun` — the OUTC-06 mechanism.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets

- **`PipelineEvent.job_state` (`pipeline.py:48-87`)** is the working precedent for D-02's
  `ScanOutcome -> JobState` function: a property over a `match` with `assert_never`, documented
  including the case that maps to `None`. Copy its shape and its docstring discipline.
- **`UploadResult.__post_init__` (`paperless.py:50-65`)** already rejects the two contradictory
  delivered/undelivered combinations, and `pipeline.py:518-521` notes that testing `task_uuid is not
  None` *is* `delivered_to_api` while narrowing the type. The FALLBACK branch this phase persists is
  already correctly computed there — only the worker's discard stands in the way.
- **`FeederEmptyError(ScanError)` (`exceptions.py:30-31`)** is the hierarchy precedent D-11 follows.
- **`httpx.MockTransport` via the `_transport` kwarg (`paperless.py:91`)** is the existing seam for
  stubbing paperless in tests — OUTC-10's end-to-end test should use it rather than inventing a new
  one.
- **`@_locked` (`job.py:415-451`)** plus Phase 22's D-24 reflective/AST test: a new `finish_job` must
  carry the decorator or that test fails, which is the intended safety net.

### Established Patterns

- **Total lookups are functions with `match` + `assert_never`, never dicts.** Phase 21's D-08
  measured this against this project's own toolchain. Everything new in `vocabulary.py` follows it,
  including `ConnectionStatus`'s message lookup (D-13) and the outcome mapping (D-02).
- **Parametrise over `list(EnumType)`, never a hand-written list of names.** That is what makes a new
  member fail the suite automatically (Phase 21 D-09). Adding `JobState.FALLBACK` and
  `ConnectionStatus` should break existing completeness tests until their arms exist.
- **Enums round-trip through SQLite as `TEXT`,** read back via the constructor
  (`JobState(row[3])`, `ErrorCategory(row[5]) if row[5] else None`). `outcome` follows the nullable
  form per Phase 22's D-09.
- **`NULL` page counts mean "never recorded", not a measured zero** (Phase 22 D-08). A job that fails
  before the scanner opens must leave them `NULL`, not write `0`.
- **Phase 22 added every result column this milestone needs in one migration.** This phase should
  need **no new migration**. If the planner finds a column missing, that is a finding worth raising
  loudly — it means Phase 22's D-11 sweep missed something.

### Integration Points

- `worker._process_job` (`worker.py:171-268`) is where the `ScanResult` arrives and where `finish_job`
  replaces the two `update_state` calls at `:250` and `:254-259`.
- `run_pipeline` (`pipeline.py:398-541`) is where the preservation guard goes, and where `job_id`
  enters via `PipelineRequest`.
- `web/routes.py` and the Jinja templates render `JobState`; `cli.py` renders the `jobs` table. All
  three need the `FALLBACK` treatment from D-05.
- `config.py`'s `OutputConfig` is where `data_dir`, `db_path` and `failed_dir` land (D-14, D-16), and
  `validate_settings_dirs` is where the writability check joins its two siblings.

</code_context>

<specifics>
## Specific Ideas

- The user's phrasing for a FALLBACK job is **"Saved to folder"** — short, title-case, and in the
  same register as the existing "Complete" and "Failed". Not "Complete (fallback)", not
  "Needs attention".
- Preserve-on-timeout was chosen knowingly, with the paperless-ngx Celery behaviour and the
  checksum-duplicate-detection backstop both on the table. The reasoning that decided it: a stray
  file is recoverable, a deleted scan is not.
- The clean-start choice for `data_dir` (D-14) was made against a recommendation to migrate. The user
  accepted losing existing job history on upgrade in exchange for not running a filesystem mutation
  at startup.

</specifics>

<deferred>
## Deferred Ideas

Nothing out of scope was proposed during discussion. Recorded below are the boundary clarifications
made while deciding — each one is work this phase deliberately does **not** do, so a planner does not
pull it forward and a verifier does not fail the phase for its absence.

- **Assembling the PDF outside the temporary directory** so "kept" is the default and success is what
  deletes it (considered under D-06). Structurally stronger than a guard, but reshapes the
  consume-directory copy and needs a startup sweep for orphans. Would be a sound future hardening
  pass; not this phase.
- **Page images spooled to disk and preserved alongside the PDF** — Phase 29 (M-08).
- **`$XDG_STATE_HOME` / `~` expansion** for `data_dir` and `log_file` — Phase 27. Both are hardcoded
  `Path.home()` today, deliberately kept consistent so Phase 27 changes them together.
- **Read-back DPI from the device, and real SANE error messages** — Phase 24. This phase uses
  whatever DPI source the discretion item settles on and does not query the scanner.
- **Plain-language error display** via `error_message()` — Phase 30 (U-05), which depends on Phase 24
  making the underlying messages truthful first. Phase 21's D-12 explains why wiring it earlier is a
  user-visible regression.
- **The `kris-knigga` -> `kdknigga` image rename** in `docker-compose.yml` — Phase 31 (M-25..M-31),
  even though this phase edits the same file.
- **The general "no `time.sleep` in tests" sweep** — Phase 32 (M-34). OUTC-10's own test must still
  be fast now.
- **Splitting `failed/` into `$XDG_DATA_HOME`** as user data distinct from state (considered under
  D-14). Rejected as a deviation from OUTC-09's single `data_dir`; noted in case a later milestone
  revisits the storage layout.

</deferred>

---

*Phase: 23-honest-outcomes-and-never-lose-a-scan*
*Context gathered: 2026-09-11*
