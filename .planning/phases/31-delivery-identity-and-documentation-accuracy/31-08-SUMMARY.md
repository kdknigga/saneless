---
phase: 31-delivery-identity-and-documentation-accuracy
plan: 08
subsystem: documentation
tags: [audit, doc-truth, verification-record, docs-01, docs-02, d-41, d-44]
requires:
  - "31-01 through 31-07 -- every documentation change this phase makes must be on the tree before it can be audited"
provides:
  - ".planning/phases/31-delivery-identity-and-documentation-accuracy/31-AUDIT.md -- the 34-row disposition table, the 17-claim D-44 re-check, and the phase-gate evidence"
  - "two doc-truth tests closing the two rows the audit found still false"
  - "a release-rehearsal placeholder section for Plan 31-10 to fill in"
affects:
  - "Plan 31-10 -- the audit's release-rehearsal table is the section it fills in (D-21, D-22)"
  - "Phase 33 -- web-api.md's OpenAPI claims were left alone as that phase's work, as the plan directed"
tech-stack:
  added: []
  patterns:
    - "doc-truth assertions whose expectation is derived from the source constant (ProfileConfig defaults, the set state_label can return) rather than from a literal"
    - "an audit artifact whose Defence column distinguishes test-defended rows from point-in-time reads, and understates rather than overstates when a row is split"
key-files:
  created:
    - .planning/phases/31-delivery-identity-and-documentation-accuracy/31-AUDIT.md
  modified:
    - tests/test_deployment_config.py
    - docs/how-to/configure-scan-profiles.md
    - docs/getting-started/first-web-ui-scan.md
decisions:
  - "Re-verified all 34 rows against the tree rather than transcribing 31-RESEARCH.md section 9; that is what found rows 10 and 4, both of which the inventory had cleared"
  - "Row 10 was a stale sentence, not a behaviour gap, so D-43's default applied and the escalation tripwire did not fire"
  - "Where a row's truth has two halves and only one is pinned, the Defence column reads 'point-in-time read' and the evidence names both; understating coverage invites re-checking, overstating invites a reader to skip it"
  - "Both audit tables live in one commit rather than one per task, because a half-written verification record is not a state the history benefits from having"
metrics:
  duration: "~50 min"
  completed: 2026-09-18
  tasks: 2
  commits: 3
  files_changed: 4
---

# Phase 31 Plan 08: Final Documentation Audit Summary

Re-verified all 34 rows of the 2026-09-09 review's false-claims table and all 17 of its
"already correct" claims against the shipped tree, found two rows that were still false, fixed
both, and recorded every disposition with a test name or a `file:line` and an explicit
permanently-defended-versus-point-in-time marker.

## What Was Built

**The audit artifact** (`f0a4cf8`) --
`.planning/phases/31-delivery-identity-and-documentation-accuracy/31-AUDIT.md`, 218 lines.
YAML frontmatter carrying `phase: 31`, the audit timestamp, the commit audited, a `scores:`
block and an empty `gaps:` list, plus a `findings_this_audit:` block naming the two rows this
plan had to close itself. Then: a short section defining what the two Defence values mean, the
34-row table, the 17-claim D-44 re-check table, the phase-gate evidence table, a
clearly-marked release-rehearsal placeholder for Plan 31-10, and the note about why the rename
needs no migration work.

**Two doc-truth tests and the corrections they pin** (`d8a4c6f` RED, `c9689a1` GREEN) --
`test_profile_howto_tuning_advice_matches_the_empty_page_rule` and
`test_first_web_ui_scan_names_real_history_labels`, both in `tests/test_deployment_config.py`
under a `Phase 31: what the final audit itself found` banner. The suite went 78 → 80 tests.

## The two rows the audit found still false

Both are recorded as deviations below, but they are the substance of this plan, so they belong
here too. **`31-RESEARCH.md` § 9 classified both as "corrected by Phases 21-30".** Its
inventory was taken on 2026-09-18 before any plan in this phase ran, and the plan explicitly
instructed re-verification against the tree rather than transcription. That instruction is the
only reason these were caught.

### Row 10 was never corrected at all

`docs/how-to/configure-scan-profiles.md` still carried the exact sentence the reviewers
flagged: *"To adjust sensitivity, lower the thresholds to detect pages with faint content as
non-empty"*, with an example lowering the mean threshold from the 250.0 default to 240.0.

`src/saneless/pages.py:74` is `is_blank = mean > mean_threshold and stddev < stddev_threshold`.
Lowering the mean threshold makes **more** pages satisfy `mean > threshold`, so it discards
more pages, not fewer -- the precise inversion of what the sentence promises. The example also
pulled in two directions at once: its stddev of 3.0 *is* the conservative move, while its mean
of 240.0 is the aggressive one.

**Why it survived seven plans and a research pass.** `docs/explanation/empty-page-detection.md`
states the rule correctly, twice, at lines 23-26 and 49-51. Each page is internally consistent
and nothing ever compared them. A reader checking either page alone finds nothing wrong.

The fix states the rule before the example, raises the mean to 253.0, and links the explanation
page. The test derives its expectation from `ProfileConfig`'s own defaults, so it compares the
example's *direction* against the shipped defaults rather than pinning a literal.

### Row 4 was corrected, and the same page then grew a different error

The original conflation is genuinely gone: `partials/status.html:66` renders "Done" only under
`JobState.DONE`, and `:71` renders FALLBACK as "Saved to folder".

But `docs/getting-started/first-web-ui-scan.md:75` described the history table's Status column
as showing "(Done, Failed, Saved to folder, Scanning, and so on)". The history table renders
`state_label(job.state)`, and `state_label(JobState.DONE)` is **"Complete"**. The status area
directly above the table does say "Done" for the same state. The page named one surface's word
while describing the other's.

No keyword ban would catch that, which is why the replacement test derives its expectation from
the set `state_label` can return and checks every word the page lists against it. The page now
gives both words and says they are the same state, because a reader watching the page sees both
at once.

## Verification

All seven phase-gate commands plus both prek stages, run at `f0a4cf8`:

| Command | Outcome |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | 3115 passed |
| `uv run zizmor .` | exit 0, no findings |
| `uv run mkdocs build --strict` | exit 0 |
| `uv run ruff check .` | no issues |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | all checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks passed |
| `uv run prek run --stage pre-push --all-files` | all hooks passed, including the doc-truth harness |
| `git diff --stat .planning/reviews/2026-09-09-code-review.md` | empty -- the review is unedited (D-42) |

**Every citation in the artifact was mechanically checked**, not eyeballed: a script extracted
all 93 backticked `file:line` references and all 39 test names and confirmed each file exists,
each line number is within the file, and each test name is collected by pytest. The one
deliberate exception is `docs/PRD.md`, which row 34 cites precisely because it no longer exists.
That check caught four stale line numbers in the first draft (rows 4, 10, 14 and claim C4),
which were corrected before the artifact was committed.

**The one anchor this plan added was checked against the built site**, not against
`mkdocs build --strict`. Plan 31-06 established that strict mode validates pages but not
fragments, so a link to a nonexistent heading passes silently.
`site/explanation/empty-page-detection/index.html` carries `id="tuning-the-thresholds"` and the
built how-to carries `href="../../explanation/empty-page-detection/#tuning-the-thresholds"`.

**Both new assertions were confirmed to go red for the right reason** before the fix, which is
what Plan 31-02 found was not true of one of its predecessors. `test_profile_howto_tuning_...`
failed on the inverted phrase; `test_first_web_ui_scan_names_real_history_labels` failed with
`says the history table shows ['Done']`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Row 10's threshold advice was still inverted**

- **Found during:** Task 1, re-verifying row 10 against the tree
- **Issue:** `docs/how-to/configure-scan-profiles.md:224` told readers to lower both thresholds
  to keep faint pages. `pages.py:74` makes lowering the mean threshold discard more pages.
  `31-RESEARCH.md` § 9 had classified this row as corrected.
- **Fix:** Stated the dual-threshold rule before the example, raised the example's mean to
  253.0, linked the explanation page, and added
  `test_profile_howto_tuning_advice_matches_the_empty_page_rule`, whose expectation is derived
  from `ProfileConfig`'s defaults.
- **Files modified:** `docs/how-to/configure-scan-profiles.md`, `tests/test_deployment_config.py`
- **Commits:** `d8a4c6f` (RED), `c9689a1` (GREEN)
- **D-43 disposition:** stale sentence, not a behaviour gap. The escalation tripwire did not
  fire and no behaviour was added.

**2. [Rule 1 - Bug] The web UI walkthrough named a status label the history table never renders**

- **Found during:** Task 1, re-verifying row 4 against `vocabulary.state_label`
- **Issue:** `docs/getting-started/first-web-ui-scan.md:75` said the history table's Status
  column shows "Done"; it shows "Complete". Strictly outside row 4's original claim, but on the
  same page, in the same class of defect, and discovered while disposing that row.
- **Fix:** Both words given with the fact that they are the same state, plus
  `test_first_web_ui_scan_names_real_history_labels`, derived from `JobState` and `state_label`.
- **Files modified:** `docs/getting-started/first-web-ui-scan.md`, `tests/test_deployment_config.py`
- **Commits:** `d8a4c6f` (RED), `c9689a1` (GREEN)

### Process deviations

**3. Both audit tables landed in one commit rather than one per task**

The plan splits the artifact into two tasks -- the 34-row table, then the D-44 re-check and the
evidence section. Both write the same file. Committing the file half-written, with a
"Verification evidence" section that did not yet exist and a document that claimed a status its
own body could not support, would have put a misleading verification record in the history for
the sake of commit granularity. The two tables are in `f0a4cf8` together. Both tasks' acceptance
criteria were checked independently before the commit.

### Escalations

None. **Zero of the 34 rows required new behaviour.** Row 10, the only row still genuinely
false, was a sentence that had drifted from code that was correct the whole time, so D-43's
default applied: correct the sentence. Row 15 is the closest thing to a behaviour change in the
whole table, and that was Plan 31-01's `--version` work, not this plan's.

### Authentication gates

None.

## Deferred Issues

**Row 19's README link text.** `README.md:74` renders as
`[saneless.github.io](https://kdknigga.github.io/saneless/)` -- the visible text says
`saneless.github.io` while the href is `kdknigga.github.io`. The URL is correct and the naming
guard passes, so this is not a row-19 failure; it is a cosmetic mismatch between link text and
target that predates this plan and is outside the 34 rows. Recorded here rather than fixed,
per the scope boundary.

**`docs/reference/web-api.md`'s OpenAPI claims** were left untouched, as the plan directed:
they belong to Phase 33.

## Known Stubs

The audit's "Release rehearsal evidence" section is a deliberate, clearly-marked placeholder
with four `*pending 31-10*` rows. Plan 31-10 fills it in with the RC tag, the workflow run URL,
and the `pip install` / `docker pull` output that D-21 and D-22 require. It is marked as
pending in the document body rather than left blank, and the frontmatter's `status: passed`
refers to the 34-row and D-44 audits, which are complete.

## Threat Flags

None. This plan added no network endpoint, auth path, file access pattern or schema change. The
audit artifact quotes the old owner slug freely and lives under `.planning/`, which the naming
guard excludes by construction (D-10) and which `.dockerignore`'s allow-list keeps out of the
build context -- so T-31-34's acceptance holds without needing an exemption.

## Self-Check: PASSED

- `.planning/phases/31-delivery-identity-and-documentation-accuracy/31-AUDIT.md` -- FOUND
- `tests/test_deployment_config.py` -- FOUND (80 tests, 2 new)
- `docs/how-to/configure-scan-profiles.md` -- FOUND
- `docs/getting-started/first-web-ui-scan.md` -- FOUND
- Commit `d8a4c6f` -- FOUND
- Commit `c9689a1` -- FOUND
- Commit `f0a4cf8` -- FOUND
- 34 numbered rows in the audit table -- confirmed by
  `grep -cE '^\| *[0-9]+ \|' 31-AUDIT.md` = 34
- All 39 cited test names collected by pytest -- confirmed
- All 93 cited `file:line` references resolve -- confirmed, `docs/PRD.md` excepted by design
- `.planning/reviews/2026-09-09-code-review.md` unchanged -- confirmed
- `STATE.md` and `ROADMAP.md` not modified -- confirmed
