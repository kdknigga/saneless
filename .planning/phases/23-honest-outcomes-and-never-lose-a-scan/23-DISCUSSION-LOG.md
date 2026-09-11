# Phase 23: Honest Outcomes and Never Lose a Scan - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-11
**Phase:** 23-honest-outcomes-and-never-lose-a-scan
**Areas discussed:** Outcome vocabulary, Preservation scope, Timeout semantics, data_dir rollout

All four offered gray areas were selected for discussion. Sixteen decisions were made across
sixteen questions. One choice (D-14) diverged from the option flagged "Recommended".

---

## Outcome vocabulary

### Q1 — Does ScanOutcome gain a FAILED member in this phase?

| Option | Description | Selected |
|--------|-------------|----------|
| No — failures stay raise-only (Recommended) | `outcome` stays SUCCESS\|FALLBACK, NULL when the job raised; JobState.ERROR carries the failure. Phase 21's D-07 test is reachability, and a *returned* FAILED is unreachable once poll_task raises. A worker-written FAILED would duplicate JobState.ERROR in a second column. | ✓ |
| Yes — worker writes FAILED on the error path | `outcome` non-NULL for every terminal job; one total column for renderers, at the cost of the duplication above. | |
| You decide | Claude picks based on vocabulary size and column non-redundancy. | |

**User's choice:** No — failures stay raise-only
**Notes:** Resolves the question Phase 21's D-07 explicitly deferred to this phase.

### Q2 — How does a returned ScanOutcome become a persisted JobState?

| Option | Description | Selected |
|--------|-------------|----------|
| Total mapping fn in vocabulary.py (Recommended) | `ScanOutcome -> JobState` behind match + assert_never, mirroring PipelineEvent.job_state at pipeline.py:48-87. Phase 21's D-08 measured that both ty and pyrefly catch a missing member in that form and neither catches it in a dict. | ✓ |
| Inline branch in the worker | Whole decision visible at one call site, but a third ScanOutcome member would silently take the else branch. | |
| You decide | Claude picks and records the reasoning. | |

**User's choice:** Total mapping fn in vocabulary.py
**Notes:** Consistent with the project's own enforcement doctrine — a missed member fails the Phase 20 CI gate at edit time.

### Q3 — How does the worker persist outcome, warning, and the three page counts?

| Option | Description | Selected |
|--------|-------------|----------|
| New terminal-write method (Recommended) | `finish_job(...)` on JobStore: one @_locked write setting state, outcome, warning and the three page counts together. update_state keeps its signature and never names the result columns. | ✓ |
| Extend update_state with new optional params | Fewer methods, but update_state's SQL is an unconditional SET — every in-flight write would blank the result columns unless the SQL became conditionally built. | |
| You decide | Claude picks and records the reasoning. | |

**User's choice:** New terminal-write method
**Notes:** Surfaced during discussion — `update_state` at job.py:616-624 already writes NULL to `error` and `error_category` on every call, which is what makes extending it hazardous.

### Q4 — What does a FALLBACK job say to the user, and how is it treated visually?

| Option | Description | Selected |
|--------|-------------|----------|
| "Saved to folder" — warning treatment (Recommended) | Amber styling distinct from both green Complete and red Failed; the `warning` column carries the metadata-not-applied detail already documented at consume-directory-fallback.md:62. | ✓ |
| "Complete (fallback)" — success variant | Least alarming, but buries a real degradation inside the word Complete — close kin to the silent-DONE behaviour this phase removes. | |
| "Needs attention" — error-adjacent | Most likely to get acted on, but overstates it: nothing was lost and no rescan is needed. | |

**User's choice:** "Saved to folder" — warning treatment
**Notes:** FALLBACK's membership in TERMINAL_STATES was not offered as a choice — Phase 21's D-09 parametrised XOR test forces it.

---

## Preservation scope

### Q1 — How is the assembled PDF protected from the TemporaryDirectory unwind?

| Option | Description | Selected |
|--------|-------------|----------|
| try/except around upload + poll (Recommended) | Literal OUTC-04: one guard inside run_pipeline's `with`, shutil.move to `<data_dir>/failed/`, re-raise with the path named. Smallest blast radius; the roadmap already flags the exact trap. | ✓ |
| Assemble outside the temp dir — default is kept | Inverts the failure mode so a bug leaves a stray file rather than losing a document, and no guard can be mis-scoped. Costs: reshapes the consume-dir copy and temp-dir semantics; a killed process leaves garbage needing a startup sweep. | |
| Broad guard from assembly to end of block | Catches more than OUTC-04 asks, including failures unrelated to delivery, which would be misreported in the job error. | |

**User's choice:** try/except around upload + poll
**Notes:** Cross-filesystem constraint established during discussion — `data_dir` and `tmp_dir` may differ, so `Path.rename`/`os.replace` raise EXDEV and `shutil.move` is required. The rejected "assemble outside the temp dir" option is recorded in CONTEXT.md so a planner does not re-derive it as an improvement.

### Q2 — What does "no page images or PDF are deleted" mean in practice?

| Option | Description | Selected |
|--------|-------------|----------|
| The PDF only (Recommended) | img2pdf is lossless, so the PDF is a complete record of every page image. Page-image disk lifecycle stays Phase 29's (M-08). | ✓ |
| The PDF and the page PNGs | Literal reading; pulls Phase 29's spooling redesign forward and multiplies peak disk use. | |
| You decide | Claude picks and records the reading. | |

**User's choice:** The PDF only
**Notes:** Surfaced during discussion — `assemble_pdf`'s own nested TemporaryDirectory at pdf.py:47 already deletes every page PNG before it returns, so page images are gone before upload begins regardless. The reading is recorded explicitly so the verifier does not read OUTC-04 the other way.

### Q3 — How much of the honest-outcomes treatment does the duplex-mismatch path get?

| Option | Description | Selected |
|--------|-------------|----------|
| Full parity (Recommended) | Both partial PDFs guarded, both tasks polled, outcome + warning + page counts persisted. Leaving it unpolled means a FAILURE on either half stays invisible — C-03 surviving in a corner. | ✓ |
| Preservation and warning, no polling | Half the request volume on an already-degraded path; accepts a FAILURE on either half being recorded as delivered. | |
| Warning persistence only | Literal OUTC-03 and nothing more; leaves both partial PDFs destroyable and both tasks unpolled. | |

**User's choice:** Full parity
**Notes:** `_handle_duplex_mismatch` (pipeline.py:188-249) currently uploads two PDFs, polls neither, and its warning dies with the discarded ScanResult. OUTC-03 and OUTC-10's parametrised "duplex mismatch" case both live in this phase.

### Q4 — Where does the unique PDF name get applied?

| Option | Description | Selected |
|--------|-------------|----------|
| At assembly — unique from birth (Recommended) | job_id into PipelineRequest, filename argument on assemble_pdf; the consume-dir copy and failed/ move inherit pdf_path.name. Also fixes what paperless records as every document's original filename. | ✓ |
| At the copy sites only | No change to PipelineRequest or assemble_pdf, but the naming rule lives at two drift-prone call sites and paperless keeps recording output.pdf. | |
| You decide | Claude picks the site and the sanitisation rule. | |

**User's choice:** At assembly — unique from birth
**Notes:** Sanitisation rule (character set, length cap, collision behaviour) remains Claude's discretion.

---

## Timeout semantics

### Q1 — On a poll timeout, is the PDF preserved to failed/ as well?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — preserve on FAILURE and TIMEOUT (Recommended) | Errs toward keeping: if the Celery worker actually died the local PDF is the only copy. Downside bounded by paperless's checksum duplicate detection; error names the task id. | ✓ |
| No — preserve only on FAILURE | The upload returned 200 with a task UUID, so paperless demonstrably has the bytes, and OUTC-04's literal trigger is not met by a timeout. Avoids stray files at the cost of no local copy if the task really died. | |
| Preserve on timeout, but into a separate directory | failed/ vs pending/ distinguishes "rejected" from "we stopped waiting". More honest; adds a second directory and doc concept for a rare case. | |

**User's choice:** Yes — preserve on FAILURE and TIMEOUT
**Notes:** Backed by research into paperless-ngx's API — post_document returns HTTP 200 immediately with the task UUID and consumption runs asynchronously on Celery, so a timeout means only that we stopped waiting. Deciding rationale: a stray file is recoverable, a deleted scan is not.

### Q2 — Where does PaperlessTimeoutError sit in the exception hierarchy?

| Option | Description | Selected |
|--------|-------------|----------|
| Subclass of PaperlessError (Recommended) | Mirrors FeederEmptyError(ScanError). The isinstance chain at vocabulary.py:246 and the catch at cli.py:142 keep working; both map to ErrorCategory.UPLOAD without a new arm. | ✓ |
| Sibling under SanelessError | Forces every call site to name timeouts explicitly, but breaks the isinstance chain and the cli catch, and diverges from the established exceptions.py pattern. | |
| You decide | Claude picks, including whether classify_error gains a distinct category. | |

**User's choice:** Subclass of PaperlessError

### Q3 — What does poll_task do with a non-200 response?

| Option | Description | Selected |
|--------|-------------|----------|
| Raise immediately on any non-200 (Recommended) | Literal OUTC-07. Today a non-200 is silently re-polled until timeout, so a revoked token burns 300 s and is misreported as a timeout. Risk: a transient 502 during a paperless restart kills a recoverable job. | ✓ |
| Raise on 4xx, retry 5xx within the deadline | Mirrors upload_document's split at paperless.py:179-194 and tolerates a restart mid-poll; deviates from OUTC-07's "any non-200" wording. | |
| You decide | Claude picks the reading. | |

**User's choice:** Raise immediately on any non-200
**Notes:** The monotonic-deadline half of OUTC-07 was not offered as a choice — `elapsed` at paperless.py:227 accumulates only sleep time, making the documented 300 s timeout unbounded in wall clock. That fix is mechanical.

### Q4 — What shape does test_connection return now that 404 and 5xx are distinct?

| Option | Description | Selected |
|--------|-------------|----------|
| A ConnectionStatus enum in vocabulary.py (Recommended) | CONNECTED / TOKEN_REJECTED / NOT_FOUND / SERVER_ERROR / UNREACHABLE with its message behind match + assert_never. StrEnum keeps routes.py:128's JSON wire values identical for the three existing cases. | ✓ |
| Keep returning a plain str | Smallest diff and zero wire risk, but reintroduces the stringly-typed protocol N-38 identified and Phase 21 removed — in the one method that survived that sweep. | |
| You decide | Claude picks the shape and confirms wire compatibility. | |

**User's choice:** A ConnectionStatus enum in vocabulary.py
**Notes:** Surfaced during discussion — the return value escapes into a public JSON contract documented at docs/reference/web-api.md:55-56, so wire compatibility for the three existing values is a hard constraint.

---

## data_dir rollout

### Q1 — Where do the job database and failed/ live by default?

| Option | Description | Selected |
|--------|-------------|----------|
| Both under ~/.local/state/saneless (Recommended) | Literal OUTC-09. Same hardcoded Path.home() style as log_file at config.py:86, so Phase 27's XDG expansion changes both in one edit. One setting, one volume, one doc explanation. | ✓ |
| Split — DB in state, failed/ in data | More spec-correct: a preserved scan is a user document, which the XDG spec puts under $XDG_DATA_HOME. Costs a second setting, a second volume, and a deviation from OUTC-09. | |
| You decide | Claude picks and records whether the DB and failed/ share a parent. | |

**User's choice:** Both under ~/.local/state/saneless
**Notes:** The state-vs-data tension was raised explicitly and knowingly set aside in favour of OUTC-09's single setting.

### Q2 — What happens to an existing tmp_dir/saneless.db when data_dir lands?

| Option | Description | Selected |
|--------|-------------|----------|
| One-time move at startup (Recommended) | Move the .db plus any -wal/-shm sidecars before opening a connection, log at WARNING. Existing users keep their history across the upgrade. | |
| No migration — start clean | History is prunable ephemera (7 days / 500 rows by default) and the documents are in paperless, so the loss is bounded and the scans unaffected. Avoids a startup filesystem mutation that can fail in a container. | ✓ |
| You decide | Claude picks and records failure behaviour and testing. | |

**User's choice:** No migration — start clean
**Notes:** **This diverges from the recommended option** and is flagged as such in CONTEXT.md (D-14). Discussion surfaced that job.py:468 enables `PRAGMA journal_mode=WAL`, so the database is up to three files and an unclean shutdown leaves committed transactions in the `-wal` — a `.db`-only move would silently lose them. The clean start sidesteps that correctness burden entirely. The old file is left in place; release notes mention it for anyone who wants their history.

### Q3 — How does the container get a durable data_dir?

| Option | Description | Selected |
|--------|-------------|----------|
| Dockerfile sets it; compose mounts there (Recommended) | ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless in the image, compose repoints saneless-data there, /tmp/saneless unmounted. A plain `docker run -v` is then correct without the operator knowing the env var exists. | ✓ |
| Compose sets the env var and the mount | Keeps the image free of path policy, but a `docker run` without that env var silently writes the DB and preserved scans to /root/.local/state and loses both on recreation. | |
| You decide | Claude picks the path, where it is declared, and whether a VOLUME instruction is added. | |

**User's choice:** Dockerfile sets it; compose mounts there
**Notes:** The image runs as root, so an unset data_dir would resolve to /root/.local/state/saneless — covered by no volume. The kris-knigga → kdknigga image rename in the same file was explicitly ruled out of this phase (Phase 31 owns it).

### Q4 — Where do derived paths (db_path, failed_dir) get defined?

| Option | Description | Selected |
|--------|-------------|----------|
| Computed properties on OutputConfig (Recommended) | Defined once in config.py, type-checked by ty and pyrefly, read by both entry points and the pipeline. validate_settings_dirs gains a data_dir writability check so a bad path fails at startup, not at preservation time. | ✓ |
| A new paths.py module | Same single-source benefit, keeps config.py a pure data model; one more module in a codebase where config.py is the obvious place to look. | |
| Leave it at the call sites | Smallest diff, but turns two drift-prone copies of a path expression into three — in the phase whose job is making sure the preserved file is where the error message says it is. | |

**User's choice:** Computed properties on OutputConfig
**Notes:** The db-path expression is currently duplicated verbatim at cli.py:227 and web/app.py:59.

---

## Claude's Discretion

Handed to the planner and executor, with reasoning to be recorded in the plan:

- The OUTC-10 end-to-end test's shape and hermeticity — it must drive the real worker through five
  outcomes while `poll_task` and `upload_document` both sleep, and it runs in the Phase 20 gate on
  every push.
- The img2pdf DPI layout function (OUTC-06) and which DPI is authoritative, given that reading DPI
  back from the device is Phase 24's.
- The exact FALLBACK rendering across the status partial, history table and `saneless jobs`, within
  the "Saved to folder" / warning-treatment decision.
- Which doc sentences get rewritten (at minimum consume-directory-fallback.md:66 and
  web-api.md:55-56).
- The sanitisation rule for the unique PDF filename.
- Whether the default 300 s `paperless_task_timeout` is still right now that a timeout is fatal.

## Deferred Ideas

Nothing out of scope was proposed. The following are boundary clarifications made while deciding —
work this phase deliberately does not do:

- Assembling the PDF outside the temporary directory so "kept" is the default (considered under the
  preservation question; sound, but reshapes more than this phase should).
- Page images spooled to disk and preserved — Phase 29 (M-08).
- `$XDG_STATE_HOME` / `~` expansion — Phase 27.
- Read-back DPI and real SANE error messages — Phase 24.
- Plain-language error display via `error_message()` — Phase 30 (U-05).
- The `kris-knigga` → `kdknigga` image rename — Phase 31 (M-25..M-31).
- The general "no `time.sleep` in tests" sweep — Phase 32 (M-34).
- Splitting `failed/` into `$XDG_DATA_HOME` as user data distinct from state.
