# Phase 21 — Vocabulary and Contracts: Security Audit

**Audited:** 2026-09-10
**Phase:** 21 — Vocabulary and Contracts (pure-refactor consolidation)
**ASVS Level:** 1
**Block on:** high
**Audited tree:** `29e679c` (post code-review fixes `da52230`, `e6de95e`)
**Phase base:** `51daabc`
**Verdict:** SECURED — 16/16 threats resolved, 0 BLOCKERs, 1 WARNING (stale accept rationale)

> Threat IDs collide across the five plans — the same numeric ID means different
> things in different plans. Every ID below is namespaced `plan/ID` and must stay
> that way. `T-21-02`, `T-21-03` and `T-21-05` each denote two unrelated threats.

---

## Verification Method

Each threat was verified against the **current** implementation, not against the
plan text. The plans were written before the deep code review; commits `da52230`
and `e6de95e` moved the implementation afterwards, so plan prose was treated as a
claim to be checked, never as evidence.

Evidence standard applied: a mitigation is CLOSED only when a specific line of
shipped code implements it and (where the plan promised one) a test discriminates
its removal. `accept` rows were checked for whether the stated rationale is still
factually true of the current code; a rationale that has become false is recorded
as a finding even when the risk itself remains accepted.

Suite state at audit time: `uv run pytest -q` → **515 passed**.

---

## Threat Register — Results

| # | Namespaced ID | Category | Disposition | Status | Evidence |
|---|---|---|---|---|---|
| 1 | 21-01/T-21-01 | Tampering | mitigate | CLOSED | `src/saneless/vocabulary.py:134-135`, `:178-179`, `:218-219` — `case _: assert_never(...)` in all three total lookups. Store boundary intact: `src/saneless/job.py:176`, `:251`. Argument recorded in `tests/test_vocabulary.py:237-250` docstring |
| 2 | 21-01/T-21-02 | Info disclosure | mitigate | CLOSED | `src/saneless/vocabulary.py:206-219` — five developer-authored constants, zero interpolation. D-12 unwired confirmed: only references to `error_message` in `src/` are its own `def` and `__all__` entry |
| 3 | 21-01/T-21-03 | Tampering (XSS) | accept | CLOSED | `grep -rn '\| safe' src/saneless/web/templates/` → 0 matches. Autoescape confirmed at the library, not assumed: starlette 0.52.1 `Jinja2Templates._create_env` does `env_options.setdefault("autoescape", True)`; `web/app.py:87` passes no `env_options` |
| 4 | 21-02/T-21-03 | Tampering | mitigate | CLOSED | `src/saneless/scanner/base.py:88-101` — `.strip().lower()`, substring tests only, no shell/path/SQL/HTML sink; returns closed `SourceKind`; `UNKNOWN` default has `uses_feeder is False` (`base.py:46-47`) |
| 5 | 21-02/T-21-04 | DoS | accept | **CLOSED — rationale corrected; bound now shipped** | See § Finding W-01. Unbounded on non-feeder hardware; **bounded from Phase 24 by `_MAX_ADF_PAGES`** (`sane_backend.py`, plan 24-03). The risk itself remains accepted by explicit user decision (D-11 AMENDED, `e6de95e`); the register's original one-page bound was false as written and is withdrawn |
| 6 | 21-03/T-21-02 | Tampering (XSS) | mitigate | CLOSED | `partials/history.html:8` `{{ job.state \| state_label }}`, `partials/status.html:9` `{{ job.state \| progress_label }}`; filters bound to the vocabulary functions at `web/app.py:92-93`; grep gate returns 0 |
| 7 | 21-03/T-21-05 | DoS | mitigate | CLOSED | Store-boundary guard not weakened: `JobState(row[3])` still the only path into `Job.state` from SQLite — `job.py:176` (`get_job`), `job.py:251` (`list_recent`); the only other `Job(...)` construction is `create_job` (`job.py:124`) from in-process values, no row |
| 8 | 21-03/T-21-06 | Info disclosure | accept | CLOSED | `{{ job.error }}` unchanged, verified by diff: `git diff 51daabc..HEAD -- partials/status.html` leaves that line untouched. Rendered autoescaped, no `\| safe`. Net change: zero |
| 9 | 21-04/T-21-07 | Tampering | mitigate | CLOSED | `src/saneless/worker.py:214-218` — `if state not in ACTIVE_STATES: return`; `ACTIVE_STATES` (`vocabulary.py:68-76`) excludes `DONE`/`ERROR`. Discriminating test `tests/test_worker.py:1087` `assert JobState.DONE not in from_callback` plus exact-sequence assertion at `:1096-1101` |
| 10 | 21-04/T-21-08 | Repudiation | mitigate | CLOSED | `worker.py:222-231` — busy path writes then `set()`; `AWAITING_FLIP` path `clear()` **before** the write. New discriminating test `tests/test_worker.py:757` `test_transition_event_is_clear_while_awaiting_flip` asserts the event state directly (`wait_transition(0.1) is False` inside the flip window) |
| 11 | 21-04/T-21-02 | Info disclosure | accept | CLOSED | `error_message` wired nowhere (see #2). `worker.py:252-260` still uses `classify_error(exc)` and writes `error=str(exc)` plus the same log line |
| 12 | 21-05/T-21-09 | Tampering | mitigate | CLOSED (strengthened) | `paperless.py:46-48` typed fields; `:50-65` `__post_init__` rejects all four contradictory combinations; `:160-164` rejects a JSON `null` task id with `PaperlessError`. Sentinel gone — the only `"fallback"` string left in `src/` is the docstring at `paperless.py:35`. Five contract tests, `tests/test_paperless.py:514-586` |
| 13 | 21-05/T-21-10 | Info disclosure | accept | CLOSED | Rationale re-checked and still true: the value is the same `dest` already logged at `paperless.py:204` `logger.warning("All retries exhausted. Copied PDF to %s", dest)`. `grep -rn consume_dir_path src/` shows zero consumers outside `paperless.py` — not rendered, not returned to the web layer |
| 14 | 21-05/T-21-11 | Repudiation | accept | CLOSED | Rationale still true despite post-review change: `run_pipeline` now returns `ScanOutcome.FALLBACK` (`pipeline.py:482-490`), but `ScanOutcome`/`ScanResult` have **zero consumers** in `worker.py`, `cli.py`, `web/app.py`, and `worker.py:250` writes `JobState.DONE` unconditionally. Documentation made honest in the same phase — `docs/explanation/consume-directory-fallback.md` now states the status does not distinguish the two paths |
| 15 | 21-05/T-21-12 | DoS | transfer | CLOSED | Transfer honoured **and** documented. Code untouched: `git diff 51daabc..HEAD -- src/saneless/paperless.py` contains zero lines mentioning `poll_task`. Transfer target exists in the plan of record: `.planning/ROADMAP.md:118-123` Phase 23 owns OUTC-01/OUTC-07, success criterion 1 names the FAILURE and monotonic-deadline behaviour |
| 16 | ALL/T-21-SC | Tampering | mitigate | CLOSED | `git diff --stat 51daabc..HEAD -- uv.lock pyproject.toml` → empty. Every import added across the phase is stdlib (`dataclasses`, `enum`, `typing`, `__future__`) or first-party `saneless.*` — verified by diffing added `^\+(import\|from)` lines across all of `src/` |

**Closed: 16/16. Open (BLOCKER): 0.**

---

## Findings

### W-01 (WARNING, MEDIUM) — 21-02/T-21-04's accept rationale is factually false; the risk itself stays accepted

**Register text:** *"Bounded: `multi_scan()` on a single-sheet path yields one page, which is today's behaviour."*

That bound is not established anywhere in the code, the tests, or the vendor
library, and the library evidence contradicts it.

**Evidence:**

1. `python-sane`'s `_SaneIterator.__next__` terminates **only** on an exact string
   match:

   ```python
   except Exception as e:
       if str(e) == 'Document feeder out of documents':
           raise StopIteration
       else:
           raise
   ```

   On a device that is not a feeder, `dev.start()` + `dev.snap()` succeed, so the
   iterator re-scans the platen instead of stopping. Neither a single page nor
   any page at all is guaranteed.

2. saneless imposes **no page cap**. The only bound in `_scan_adf_pages` is
   `_DEFAULT_PAGE_TIMEOUT_SECONDS = 120.0` per page (`sane_backend.py:62`), which a
   succeeding scan satisfies every iteration.

3. Pages are materialised, not streamed: `pipeline.py:310`, `:330`, `:361` all do
   `list(scanner.scan_pages(...))`. Unbounded page count is unbounded memory.

4. The opposite failure mode is also not "one page": every exception on page 0 is
   swallowed by a bare `except Exception` and re-raised as
   `FeederEmptyError("No paper detected in feeder")` (`sane_backend.py:372-375`
   and `:404-406`), and a zero-page iterator hits the same message at `:426-427`.
   So the realistic outcomes on non-feeder hardware are a hard job failure with a
   **misleading** message, or an unbounded rescan loop — not a bounded single page.

**Why the blast radius grew after the plans were written.** D-11 as amended
(`e6de95e`) routes any source name containing `"duplex"` through `multi_scan()`,
including `"Manual Duplex"` and `"Card Duplex"`, which carry no feeder token. The
review-fix tests confirm the reach extends even to `"Flatbed Duplex"`
(`tests/test_auto_profiles.py::test_duplex_outranks_flatbed` → `adf-duplex`).

**Trigger path.** `sane_backend.scan_pages` validates the requested source against
the device's option list only when `has_source_option` is true (`:469`). A
flatbed-only scanner exposing no `source` option gets **no** validation, and
`classify_source(effective_source)` — reading config text — decides routing at
`:492`. A flatbed-only device with `source = "Manual Duplex"` is exactly the
config the pre-amendment docs prescribed.

**Assessment.** Not a BLOCKER at ASVS L1: this is local resource consumption on a
self-hosted LAN appliance, reachable only through the operator's own
configuration, with no remote-attacker-controlled input. The residual risk was put
to the user and consciously accepted — 21-CONTEXT.md D-11 (AMENDED) names the
counter-scenario verbatim and records that it is "undocumented… not covered by
tests and was not verified against hardware." The compensating control shipped in
the same commit is documentation only: `docs/how-to/set-up-adf-duplex.md` now says
manual duplex requires a feeder.

**Required action — DISCHARGED 2026-09-13 by Phase 24, plan 24-03:**
- ~~Rewrite the T-21-04 rationale in the register.~~ **Done.** The register row and the Accepted Risks Log row now both read to the effect of *"unbounded on non-feeder hardware; bounded from Phase 24 by `_MAX_ADF_PAGES`."* The withdrawn wording survives in exactly one place — the verbatim quotation of the original register text at the top of this finding — which is retained deliberately, because the history of the false rationale is the point.
- ~~Phase 24 … should carry a page cap or an iteration guard for `_scan_adf_pages`, and should stop mapping every `multi_scan()` failure to "No paper detected in feeder."~~ **Done.** `_MAX_ADF_PAGES = 500` bounds the loop in `_acquire_pages` and names both itself and the page count in the `ScanError` it raises (D-04). Separately, the first-page special case and the unreachable guard around `multi_scan()` are deleted, so a jam, an open cover, a busy device and an I/O error each surface as `ScanError` carrying the SANE text, and a feeder that produced zero pages is now the only remaining source of that message (D-03).

**What the cap does not do.** It bounds the *iteration*, not memory: at A4/300 dpi colour 500 pages is roughly 13 GB, and `pipeline.py` still materialises pages with `list()`. Memory bounding is Phase 29's HARD-01/HARD-02 and is explicitly not claimed here. The cap is also **per `scan_pages()` call**, so Phase 25's two manual-duplex passes get one cap each — per-pass, not per-job.

---

## Unregistered Flags

No summary in this phase contains a `## Threat Flags` section — the executors used
`## Threat Model Dispositions Honoured` (21-01, 21-03, 21-04, 21-05) and
`## Threat Model Notes` (21-02) instead. Those sections were read in their place.
21-02 states affirmatively: *"No new threat surface. No network endpoint, auth
path, file access pattern, or schema change was introduced."* That claim was
independently checked against the phase diff and holds — no route, no auth code,
no SQL schema, and no new filesystem write path was added.

Two controls appeared during the post-review fixes with no register row. Both are
**strengthening**, and neither opens attack surface. Recorded here so they are not
mistaken for undeclared behaviour later:

| Item | Where | Assessment |
|---|---|---|
| `UploadResult.__post_init__` contradictory-state guard | `paperless.py:50-65` | Informational. Narrows 21-05/T-21-09 further than planned |
| `upload_document` rejects a JSON `null` task id | `paperless.py:160-164` | Informational. Closes a real pre-existing defect — `str(None)` produced the truthy string `"None"`, which was then handed to `poll_task`. Adjacent to the transferred 21-05/T-21-12 but does not consume it |

---

## Accepted Risks Log

| ID | Risk | Accepted because | Owner / revisit |
|---|---|---|---|
| 21-01/T-21-03 | XSS via a label string reaching a template | Labels are developer-authored constants; Jinja2 autoescape verified in force at starlette 0.52.1. **Standing control: no `\| safe` filter on any label, anywhere under `src/saneless/web/templates/`.** Currently 0 occurrences | Standing — re-grep every phase that touches templates |
| 21-02/T-21-04 | A non-feeder source routed to `multi_scan()` | Explicit user decision recorded in 21-CONTEXT.md D-11 (AMENDED) and `e6de95e`. No longer documentation-only: **unbounded on non-feeder hardware; bounded from Phase 24 by `_MAX_ADF_PAGES`** (plan 24-03), which also made the feeder error messages truthful. The original "bounded to one page" rationale is withdrawn — see W-01. Residual: the cap bounds iteration, not memory | Phase 29 (HARD-01/HARD-02) for the memory bound |
| 21-03/T-21-06 | Raw exception text shown via `{{ job.error }}` | Pre-existing, deliberately unchanged; net change this phase is zero. Substituting a generic message before Phase 24 makes the underlying messages truthful would be a regression, not a fix | Phase 30 (U-05), depends on Phase 24 |
| 21-04/T-21-02 | `error_message()` replacing specific error text | Deliberately not wired (D-12); `job.error` and the log line are byte-unchanged | Phase 30 (U-05) |
| 21-05/T-21-10 | `consume_dir_path` carrying a filesystem path | No new disclosure — the same path was already logged pre-phase; it is a locally-configured directory, not user input, and has zero consumers outside `paperless.py` | Revisit if the value is ever surfaced in the UI |
| 21-05/T-21-11 | A consume-dir-only document recorded as `DONE` | Pre-existing and unchanged; `ScanOutcome.FALLBACK` now exists as vocabulary but has no consumer. Documentation was corrected in-phase to stop promising a `FALLBACK` status that does not exist | Phase 23 (OUTC-02) |

## Transferred Risks Log

| ID | Risk | Transferred to | Verification |
|---|---|---|---|
| 21-05/T-21-12 | `poll_task` FAILURE and timeout handling | Phase 23 — OUTC-01, OUTC-07 | Target documented at `.planning/ROADMAP.md:118-123` with a matching success criterion; `poll_task` confirmed untouched across `51daabc..HEAD` |

---

## Observations (no action required)

- `worker.py:186-187` and `pipeline.py:185` still derive "is this a manual-duplex profile?" from substring tests on the profile source. This is a different question from `classify_source`'s "is this a feeder?", so it is not a duplicate classification rule in the T-21-03 sense, and neither site reaches an injection sink. Noted only because Phase 24 inherits the surrounding code.
- `tests/test_auto_profiles.py` is in the phase diff, which reads as contradicting 21-02-SUMMARY.md's "completely unedited" claim. It is not a discrepancy: that claim was scoped to 21-02's own two commits, and the file was touched later by the review-fix commit `da52230`, additive-only (two new tests).
