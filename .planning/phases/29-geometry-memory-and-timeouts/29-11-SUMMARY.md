---
phase: 29-geometry-memory-and-timeouts
plan: 11
subsystem: docs
tags: [documentation, doc-truth, D-20, DOCS-01, phase-gate, architecture, troubleshooting]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "29-01..29-10 -- the spool, the bounded assembly, the timeouts and wedge, the preservation rules and the SANE lifecycle this page now describes"
  - phase: 29-geometry-memory-and-timeouts
    provides: "29-09-SUMMARY.md's verbatim message wordings and the on-disk artefact-name caveat"
provides:
  - "D-20: architecture.md's diagram, Pipeline Orchestration, PDF Assembly, a new 'Memory, disk and timeouts' subsection and the Worker Thread Model shutdown all describe what shipped"
  - "the byte-for-byte overclaim is gone; the embed is described as lossless-but-not-byte-identical"
  - "six other pages corrected: troubleshooting, ADF duplex, consume-directory fallback, configuration, environment variables, docker"
  - "the first doc-truth test this phase's claims have ever had, falsified against seven mutations"
  - "29-VALIDATION.md complete: 24 of 24 rows re-run and observed passing, nyquist_compliant true"
  - "the phase gate, green"
affects: [30]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A doc-truth claim is pinned by the substrings that carry it, never by a whole sentence, so a reword passes and a dropped guarantee fails"
    - "When the page a test pins was corrected earlier in the same plan, prove the RED by mutation instead of claiming a red that did not happen"

key-files:
  created:
    - .planning/phases/29-geometry-memory-and-timeouts/29-11-SUMMARY.md
  modified:
    - docs/explanation/architecture.md
    - docs/how-to/troubleshoot-a-failed-scan.md
    - docs/how-to/set-up-adf-duplex.md
    - docs/explanation/consume-directory-fallback.md
    - docs/reference/configuration.md
    - docs/reference/environment-variables.md
    - docs/reference/docker.md
    - src/saneless/scanner/sane_backend.py
    - tests/test_deployment_config.py
    - .planning/phases/29-geometry-memory-and-timeouts/29-VALIDATION.md

key-decisions:
  - "'byte-identical', not 'byte-for-byte identical', in the corrected sentence: the action mandates saying the embed is not byte-identical to the scanner's raw output, while the criterion and the new test both forbid the literal string 'byte-for-byte'. The unhyphenated form satisfies both without losing the distinction."
  - "_MAX_ADF_PAGES' memory paragraph was EXTENDED with the disk half, not rewritten: 29-02 already corrected it and dropped the Phase-29 future-work reference, and re-wording prior-wave rationale to satisfy a plan written before that wave landed would be vandalism."
  - "The heading '## Page count mismatch handling' became '## When a manual duplex scan does not come out whole', because the section now carries the general rule the action asked for and not only the mismatch case. Checked first: nothing links to the old anchor."
  - "Two sentences outside Finding 11's table were corrected because this phase falsified them too -- troubleshoot-a-failed-scan's 'the scanned pages are only held in memory while the PDF is built' (assembly failure now preserves the page files) and its cancel section, which now states the keeps-nothing half of the rule."
  - "environment-variables.md's table has no description column, so the per-page reserve wording went in a paragraph under the Output table rather than into a row."

patterns-established:
  - "Where a plan's expectation and the shipped behaviour differ, the shipped behaviour is what the documentation says, and the difference is recorded here."

requirements-completed: [HARD-01, HARD-02, HARD-03, HARD-04, HARD-05]

# Metrics
duration: ~65min
completed: 2026-09-15
---

# Phase 29 Plan 11: Documentation Truth and the Phase Gate Summary

**Every sentence this phase falsified is corrected against what actually shipped -- not against
what the plan expected -- and the architecture page's new "Memory, disk and timeouts" subsection
is pinned by the first doc-truth test any of these claims has ever had, falsified against seven
mutations.**

## Performance

- **Duration:** ~65 min
- **Tasks:** 3 of 3
- **Commits:** 4
- **Files modified:** 10 (7 docs, 1 source comment, 1 test, 1 validation map)
- **Suite:** 2033 passing (2032 at the base commit, +1)

## Task Commits

1. **Task 1: correct the architecture explanation page** — `e5f9e3e`
2. **Task 2: correct the six other documents and the `_MAX_ADF_PAGES` comment** — `301a54b`
3. **Task 3: the doc-truth test** — `116f51b`; **the completed validation map** — `3e7b25d`

## Where the plan and the shipped behaviour differed

The plan was written before waves 6-8 landed, so several of its figures and expectations were
superseded. The documentation follows what shipped, in every case.

| The plan said | What shipped | What the docs say |
|---|---|---|
| assembly measured "131 MB at both 12 and 48 pages" (29-CONTEXT D-03, from research) | 29-08 measured **72.7-73.3 MiB at both 4 and 16 pages** with its own fixture | No absolute figure is quoted for assembly. The operator-facing claim is that it does **not grow with page count**, which is the part that is stable across fixtures and the part someone sizing a machine needs. |
| the memory sentence could cover scanning and assembly together | two different mechanisms with two different proofs: a live-image high-water of 2 during scanning, and a flat peak RSS during assembly | Two separate claims in two places -- "roughly one decoded page in memory **while scanning**" in the new subsection, and "its memory does not grow with page count" under PDF Assembly. Neither sentence is allowed to cover the other's mechanism. |
| `_MAX_ADF_PAGES`' comment still names "Phase 29's HARD-01/HARD-02" as future work | 29-02 already rewrote that paragraph; `git grep 'Phase 29' -- src/` was **already 0** at this plan's base | The paragraph was extended with the disk half the action asked for, and otherwise left exactly as 29-02 wrote it. |
| Finding 11's table is the complete list of falsified sentences | it is not; two more were found | Both corrected, listed under Deviations. |
| the preserved artefacts are named `(partial)` / `(fronts)` / `(backs)` | `build_pdf_filename` runs the title through a sanitiser whose allow-list drops brackets (29-09) | Every documented example uses the **on-disk** form, e.g. `…-invoice-partial.pdf`. |
| 29-VALIDATION.md's D-20 row needed its plan id filled in | the planner had already written `29-11 T3` | Only the status columns moved. Twelve *other* rows still read `❌ W0 ⬜ pending` although the work shipped in waves 1-7, so all of them were re-run and flipped (see below). |

## What each page now says

**`docs/explanation/architecture.md`** — the five D-20 changes and nothing else.

- The pipeline diagram loses `PIL Images` for `[Page Spool] -> spooled PNGs + ordered page records`,
  and names the qpdf merge beside img2pdf. The fallback branch's `\->` still sits exactly under the
  arrow it branches from, at column 58 (verified, not eyeballed).
- **Pipeline Orchestration** says pages are spooled as they arrive and that document order comes
  from the record list, never from sorting or globbing the directory, then states in four bullets
  what each way of ending a scan keeps: N pages as a partial PDF, a duplex job's fronts, the page
  files themselves when assembly fails, and nothing at all for a cancel -- with the reason the
  cancel case is different (`failed/` is never pruned, so preserving a cancel would only leave the
  operator files to delete).
- **PDF Assembly** says assembly converts one page at a time and merges, so its memory does not
  grow with page count, and names what the rejected shape would have done instead. The
  byte-for-byte claim is replaced by an honest one: the page is encoded once as the PNG the spool
  wrote, that PNG's data is what the PDF carries, it is **not byte-identical** to the raw data the
  scanner sent (that data was never stored), and no pixel changes.
- **A new `### Memory, disk and timeouts`** subsection, six bullets in D-20's order, written for an
  operator. One measured figure appears — "two live page images, the same for a 3-page job as for a
  12-page one" — because sizing a machine is exactly what it helps with.
- **Worker Thread Model** gains the SANE lifecycle paragraph (once per process, explicit shutdown
  at each entry point, never from an interpreter-exit hook, never from a request, skipped and
  logged while a read is outstanding), and the shutdown sentence now closes the scanner alongside
  the store and the client. The handle-released-between-passes sentence gains its one exception and
  links to the new subsection.

**`docs/how-to/troubleshoot-a-failed-scan.md`** — the flip-timeout and flip-prompt entries say the
fronts are kept and where. Three new symptoms in the page's existing bullet format: a scan that
stopped part-way (with the real `Scanner error on page 40: … The 39 page(s) scanned before the
error were preserved at …-invoice-partial.pdf` line), a page that timed out (`Page 3 timed out
after 120s`), and the refusal (`The scan will be possible again as soon as the scanner releases it.
Restart saneless if it does not.`) with the note that the wording is literal — a transient hang
clears itself. Every quoted message is the delivered string, checked against the source, not
invented.

**`docs/how-to/set-up-adf-duplex.md`** — "Nothing is uploaded in either case" stays, because it is
still true, followed by what distinguishes the two cases now that one of them preserves. The flip
timeout says the fronts are kept and why (nobody decided to abandon the scan). The mismatch section
is re-headed and its "saneless does not discard your scans" is promoted to the general rule for any
pass-B or flip failure, with the cancel named as the one ending that keeps nothing.

**`docs/explanation/consume-directory-fallback.md`** — `failed/` now described as holding three
artefact kinds, with the failure-keeps-everything / cancel-keeps-nothing rule stated once.

**`docs/reference/configuration.md`** and **`docs/reference/environment-variables.md`** —
`min_free_space_mb` is the assembly reserve, checked before the scan **and** before each page,
against that page's size plus the reserve. Identical wording in both.

**`docs/reference/docker.md`** — two new bullets so an operator sizing a volume is not surprised:
partial PDFs (a jam on a 50-sheet job writes one, and the rescan writes a second complete one), and
page directories (~13 MB per A4 300 DPI colour page, so one can be larger than any PDF beside it).
The growth-warning bullet now says the count includes both kinds, matching
`_warn_if_failed_dir_growing`.

**`src/saneless/scanner/sane_backend.py`** — `_MAX_ADF_PAGES` gains a `WHAT IT STILL BOUNDS,
INDIRECTLY` paragraph: the page count is the ceiling on spool disk, but it is a ceiling and not the
check — `SpooledPageSink._check_room_for` refuses each page against its own size plus the reserve,
and that is what protects the disk.

## The doc-truth test

`tests/test_deployment_config.py::test_architecture_page_states_the_memory_disk_and_timeout_rules`,
selected by `-k architecture` (which matched **0** tests before this plan and 1 after), reading the
page through the existing `ARCHITECTURE` constant and a new `_subsection` helper that mirrors the
file's existing `_heading_section`.

Six claims, each asserted by the substrings that carry it rather than by a whole sentence, plus the
two negative assertions and the positive `lossless` one.

**The RED was proven by falsification, not claimed.** The test cannot fail on its own at HEAD,
because the page it pins was corrected two commits earlier in the same plan — the situation 29-07
hit and resolved the same way. Seven mutations, each run and each observed red:

| Mutation | Result |
|---|---|
| the page as it stood at `f70c435` | FAILS — no such subsection |
| `byte-for-byte` reintroduced | FAILS |
| `PIL Images` reintroduced in the diagram | FAILS |
| `min_free_space_mb` removed from the disk bullet | FAILS |
| the `docker stop` guarantee deleted | FAILS |
| "restart saneless" softened to "try later" | FAILS |
| the one-decoded-page bullet retitled "Memory use … is modest" | FAILS |

An eighth mutation initially passed — it rewrote a *different* sentence in the same bullet and left
the needle intact. That was a bad mutation, not a weak assertion, and it was replaced by one that
removes the claim itself. Recorded because a mutation that passes is worth reporting either way.

## Deviations from Plan

### Auto-fixed issues

**1. [Rule 1 — Bug] `troubleshoot-a-failed-scan.md` still claimed pages are only held in memory**

- **Found during:** Task 2, reading the whole file as the plan's `<read_first>` asked.
- **Issue:** The exit-4 section ended "The scanned pages are only held in memory while the PDF is
  built, so they are not kept when this fails: scan the document again once the cause is fixed."
  29-09 made that false — a `PdfError` now moves the spooled page files into
  `failed/<job-keyed-name>/`. It is not in Finding 11's table, and following the table literally
  would have shipped a page that tells an operator to rescan a document whose pages are sitting on
  disk.
- **Fix:** Rewritten to say the pages are preserved, where, and what they are (one PNG per sheet in
  scan order).
- **Committed in:** `301a54b`

**2. [Rule 2 — missing critical information] the cancel section did not state what a cancel keeps**

- **Found during:** Task 2.
- **Issue:** D-10 created an asymmetry an operator has to know about — a failure keeps everything it
  can, a cancel keeps nothing — and the exit-130 section said only "Nothing is uploaded", which was
  true before this phase and is now half the story.
- **Fix:** One sentence stating both halves and the reason. The same rule is stated once in
  `architecture.md`, once in `consume-directory-fallback.md` and once in `set-up-adf-duplex.md`,
  each in that page's own register.
- **Committed in:** `301a54b`

### Acceptance criteria that could not hold as literally written

**3. `grep -ci 'byte-for-byte'` must be 0, while the action mandates saying the embed is "not
byte-identical to the scanner's raw output"**

The two are compatible only if the sentence avoids the hyphenated triple. It does:
"It is not **byte-identical** to the raw data the scanner sent over the wire". The distinction the
action asks for is intact, the criterion returns 0, and the new test's negative assertion — which
is what a future reverter would trip — targets the exact old phrasing.

**4. Task 2's `grep -ci 'Phase 29' src/saneless/scanner/sane_backend.py` returns 0 — but it already
did, at the base commit**

`git grep -ci 'Phase 29' -- src/` was **0 across the whole source tree** before this plan ran. 29-02
had already rewritten the `_MAX_ADF_PAGES` memory paragraph and dropped the future-work reference,
and the executor's brief names that paragraph among the four not to re-word. The criterion and the
`done` line are satisfied; what this plan added is the disk half the action also asked for, as an
extension. Verified afterwards: still 0.

**5. Task 3's "fill in the D-20 row with this plan's id" needed no edit; the *other* rows did**

The D-20 row already read `29-11 T3` — the planner wrote it. What the action's second clause asked
for ("set every remaining row's status from `pending` to its observed state") was the real work:
twelve rows still read `❌ W0 | ⬜ pending` although waves 1-7 had shipped and proven them, because
only 29-09 and 29-10 flipped their own. **Every one of the 24 rows was re-run at this commit**
rather than transcribed from a summary, and all 24 passed. The file is in the plan's `<action>` but
not its `files_modified`; the action is normative and no sibling agent is running this wave.

---

**Total deviations:** 2 auto-fixed (1 × Rule 1, 1 × Rule 2), 3 acceptance criteria resolved in
favour of the `<action>` with direct proof.
**Impact on plan:** No scope creep. Both auto-fixes are sentences this phase itself falsified,
which is exactly what D-20 and DOCS-01's standing rule cover.

## Issues Encountered

- **The shell hook rewrites `grep`.** `grep -ci needle file` prints `file:0` instead of `0`, so
  every `grep -c … | grep -qx 0` idiom in the plan's verify blocks returns 1 no matter what the
  file says. Confirmed by `od -c`. Every verification grep in this plan was run with
  `/usr/bin/grep`, and `git status`/`log`/`add`/`commit` with `/usr/bin/git`, as the brief warned.
- **No re-wording of prior-wave docstrings was needed beyond the one addition.** The four named in
  the brief (`_MAX_ADF_PAGES`, the module docstring's cancel+close line, `_scan_adf_pages`' timeout
  paragraph, `sane_backend.py`'s module docstring line 7 and the `SaneBackend` class docstring) were
  each checked for truth against what shipped. All current. Only `_MAX_ADF_PAGES` was touched, and
  only additively.
- **`deferred-items.md`'s `GET /openapi.json` 500 was left alone,** as the brief and 29-10 both
  require, and nothing written here contradicts it: no page in this plan's file set claims saneless
  serves a generated API schema.

## Phase Gate

Run at `3e7b25d`, output read from redirected files.

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware"` | **2033 passed**, 74 deselected, 56.7 s |
| `uv run pytest -m browser` | **68 passed**, 2039 deselected, 20.5 s |
| `uv run pytest -m sane_hardware` | **6 passed**, 2101 deselected, 0.9 s |
| `uv run pytest tests/test_deployment_config.py -k architecture -x -q` | 1 passed (0 before this plan) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 55 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | exit 0, 21 Passed |
| `uv run prek run --stage pre-push --all-files` | exit 0, including `ty (full)`, `pyrefly (full)`, `ruff (no fix)` and `ruff format --check` |

**The browser suite ran.** Chromium is available in this environment and all 68 browser tests
passed; nothing was marked manual. **The hardware suite ran too** — libsane's `test` backend is
present, so all 6 opt-in tests executed rather than skipping.

### Every validation-map row, re-run

All 24 passed at this commit: `twelve_page_order`, `interleave_records`, `live_page_images`,
`sink`, `pdf merge` (6), `partial_scan_preserved`, `pass_b_preserves_fronts` (4),
`cancel_preserves_nothing`, `failed_dir_counts_directories` (3), `close_not_called_while_blocked`,
`did_not_respond_to_cancel`, `process_exits`, `post_cancel_page_discarded`, `wedge` (2),
`keyboard_interrupt_mid_read`, `flatbed_timeout`, `flatbed_unreadable`, `init_once` (7),
`sane_lifecycle`, `closes_the_backend` (6), `test_spool.py` (20), `no_second_encode` (2),
`architecture`, and the opt-in `-m sane_hardware -k cancel`.

### Suppressions, across the whole phase

| Check | Phase-29 baseline (29-06) | Now |
|---|---|---|
| `git grep -c 'noqa' -- src tests` | config.py 2, scanner/__init__.py 1, scanner/base.py 1, sane_backend.py 3, test_cli.py 1, test_web.py 1 | **identical** |
| `git grep -c 'type: ignore' -- src tests` | 0 | **0** |

No `--no-verify`, no `SKIP=`, no disabled rules. Every commit in this plan ran the hooks; the
output of each is above the commit in the log.

## The one manual verification

`29-VALIDATION.md` lists exactly one, and it stands: **behaviour against the operator's real
network scanner over `net`/`hpaio`** (HARD-03). The `net` backend's blocking-cancel RPC cannot be
reproduced without that hardware. To exercise it: run a long ADF scan, pull the network mid-read,
and confirm the job fails with the timeout message, the next scan is refused with the restart line,
and `saneless serve` still stops on Ctrl-C and `docker stop`.

Everything else is automated, including the browser UI (Playwright, 68 tests) and the real-libsane
cancel sequence (behind the `sane_hardware` marker, 6 tests). Per CLAUDE.md nothing was marked
"needs human" that a tool could have driven.

## Known Stubs

None. This plan added no code path — one comment paragraph, one test, and prose.

## Threat Flags

None. No new network endpoint, auth path, file-access pattern or schema at a trust boundary.

### Threat model follow-through

| Threat ID | Disposition | Where it landed |
|---|---|---|
| T-29-46 | mitigate | The byte-for-byte claim is corrected, and the new test's negative assertion fails on a revert — falsified, not assumed. |
| T-29-47 | mitigate | `docker.md` and `consume-directory-fallback.md` both name the two new artefact kinds, with the per-page size that makes a page directory the larger surprise, and both repeat that saneless never prunes `failed/`. |
| T-29-48 | accept | The quoted messages carry a page count and `data_dir`-relative paths only. Checked: no credential, host or token appears in any example added here (ASVS V7). |

## Next Phase Readiness

- **Phase 30 (APPL-01/02/03)** inherits an architecture page that already describes the wedge as a
  state with a recovery, which is the fact a degraded `/health` and a status-strip line would read.
  The new subsection is where that behaviour is documented; a `/health` change should extend it
  rather than start a new one.
- **The doc-truth test is a seam, not a one-off.** `ARCHITECTURE_MEMORY_CLAIMS` is a table of
  (claim, needles) pairs; a later phase that adds a guarantee to that subsection adds a row.
- **DOCS-01's milestone-wide sweep and DOCS-03 (`docs/PRD.md`) were deliberately not started**, as
  the plan required. This plan corrected only what Phase 29 falsified.
- **`deferred-items.md` still holds the `/openapi.json` finding**, untouched. It wants its own
  decision about whether saneless should serve generated API docs at all.

## Self-Check: PASSED

Files, all FOUND on disk in this worktree: `docs/explanation/architecture.md`,
`docs/how-to/troubleshoot-a-failed-scan.md`, `docs/how-to/set-up-adf-duplex.md`,
`docs/explanation/consume-directory-fallback.md`, `docs/reference/configuration.md`,
`docs/reference/environment-variables.md`, `docs/reference/docker.md`,
`src/saneless/scanner/sane_backend.py`, `tests/test_deployment_config.py`,
`.planning/phases/29-geometry-memory-and-timeouts/29-VALIDATION.md`, and this file.

Commits, all FOUND in `git log`: `e5f9e3e`, `301a54b`, `116f51b`, `3e7b25d`.

`STATE.md` and `ROADMAP.md` were not touched, as instructed. No file was deleted:
`git diff --diff-filter=D --name-only f70c435..HEAD` is empty.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
