---
phase: 24-scanner-truthfulness
plan: "04"
subsystem: scanner
tags: [scanner, sane, empty-page-detection, ci, docs, tdd, security]
requires:
  - "24-01 — tests/test_sane_hardware.py and the sane_hardware marker; the RED ten-page assertion"
  - "24-03 — the _acquire_pages/_next_page_with_timeout split that made room for the counter"
provides:
  - "integrity-only _validate_page_image — zero dimensions and _MIN_PAGE_BYTES, nothing else"
  - "rejected_pages — the local integrity-rejection counter 24-07 turns into ScanBatch.pages_rejected"
  - "ScanError on a wholly rejected batch, so assemble_pdf([]) is unreachable from the scanner"
  - "a CI step running the sane_hardware marker"
  - "CONTRIBUTING's gate documented as six commands, matching CI exactly"
affects:
  - "24-05 — inherits scan_pages at 11/12 branches, still unsplit; still owns the resolution Protocol lie"
  - "24-07 — owns D-07/D-12: surfacing rejected_pages as ScanBatch.pages_rejected"
  - "25 — manual duplex inherits D-08's accepted parity consequence, now recorded in code"
  - "29 — HARD-02 owns partial-result recovery, which D-06 deliberately does not provide"
tech-stack:
  added: []
  patterns:
    - "Validate integrity at the bottom, decide content policy at the top"
    - "Turn a RED test green by changing behaviour, never by touching the assertion"
    - "Name a field in a comment only when a probe does not forbid the token"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-04-SUMMARY.md
  modified:
    - src/saneless/scanner/sane_backend.py
    - tests/test_scanner.py
    - tests/test_sane_hardware.py
    - .github/workflows/ci.yml
    - CONTRIBUTING.md
    - docs/explanation/empty-page-detection.md
decisions:
  - "The integrity-rejection counter is named rejected_pages and stays local; 24-07 promotes it"
  - "The all-rejected message names the fed count and the two integrity reasons, so the operator hears 'unreadable', not 'load paper'"
  - "The plan's ImageStat probe named pipeline.py, where the count is 0; the statistics live in pages.py and the intent was verified there"
  - "CI's sane_hardware step is added and passes locally; it has NOT been observed running on GitHub Actions from this worktree"
metrics:
  tasks: 3
  commits: 5
  tests_added: 5
  tests_inverted: 2
  suite: "827 passed, 33 deselected (baseline was 823 passed, 33 deselected)"
  hardware_suite: "4 passed (baseline was 2 passed, 1 failed)"
  commit_span: "10m11s (10:29:19 -> 10:39:30)"
completed: 2026-09-13
---

# Phase 24 Plan 04: The Scanner Stops Deciding What a Page Is Worth Summary

The backend now answers only "is this a readable image?"; whether a page is worth keeping is decided one layer up, under the toggle the user can actually see — and SCNR-08's ten-page assertion, RED since plan 24-01, went green without a character of it changing.

## The Headline: SCNR-08 Turned Green by Behaviour Change

This is the claim the whole plan exists to support, so here is the evidence in full.

**Before** (recorded in 24-01, reproduced verbatim at the start of this plan against the same worktree):

```
uv run pytest -m sane_hardware -q
1 failed, 2 passed

pages = list(SaneBackend().scan_pages("test:0", settings))
>       assert len(pages) == 10
E       assert 0 == 10
E        +  where 0 = len([])

WARNING  saneless.scanner.sane_backend:sane_backend.py:204 Page 1: pure black (mean=0.0, stddev=0.0), skipping
... Pages 2 through 10, identical ...
```

**After:**

```
uv run pytest -m sane_hardware -q
4 passed
```

The assertion `assert len(pages) == 10` is **byte-identical** to the day 24-01 wrote it. The markers, the fixture and the settings are untouched. `git show cc800c0 -- tests/test_sane_hardware.py` changes that test's *docstring* and adds a new test below it; the assertion line is not in the diff. What moved was the behaviour underneath it.

The `pure black` warning is not merely out-voted, it is **gone**: `grep -c 'pure black\|pure white' src/saneless/scanner/sane_backend.py` is 0. There is no code path left that could emit it.

## What Was Built

### Task 1 — the content policy is deleted (D-05, Q2)

RED `1a48af3`, GREEN `91db80c`.

The four constants `_SCANNER_WHITE_MEAN_THRESHOLD`, `_SCANNER_WHITE_STDDEV_THRESHOLD`, `_SCANNER_BLACK_MEAN_THRESHOLD` and `_SCANNER_BLACK_STDDEV_THRESHOLD` are gone with their comment block, along with checks 3 and 4 and the now-unused `ImageStat` import. `grep -rn '_SCANNER_WHITE\|_SCANNER_BLACK' src/ tests/` returns nothing.

**The two integrity checks are retained deliberately**, and this is the load-bearing distinction: zero dimensions and `_MIN_PAGE_BYTES` ask "did the device hand back something decodable?", which is a question the bottom layer is the only one positioned to answer. "Is this page worth keeping?" is a different question with a user-visible setting attached, and it now has exactly one answer site.

`_validate_page_image`'s docstring no longer quotes the superseded user decision. It states the two checks and, in one sentence, why blank-page policy is not there: the profile exposes `enable_empty_page_detection` with visible thresholds, so a page discarded here would be discarded behind the user's back.

**Q2, `_MIN_PAGE_BYTES` stays at 10 KB**, and its comment now carries the measurement that earns it, which matters more than it did when it was one of four checks and matters a lot now that it is one of two: the smallest legitimate real page measured against the SANE `test` backend is **69,620 bytes** (Gray, 75 dpi, 80x100 mm bed) — nearly 7x the threshold — and a 300 dpi colour page is 3.3 MB. It demonstrably does not fire on legitimate small pages.

The two tests that asserted the defect were **inverted, not deleted**. Their reasoning was the right reasoning pointed the wrong way: the observation that a clean blank page reaches exactly mean 255.0 / stddev 0.0 was the argument for dropping it, and is now the argument that proves it survives. Each also asserts via `getextrema()` that the blank *itself* came back, so the test cannot pass by the content page arriving twice.

### Task 2 — skip one, raise on all (D-06, D-08)

RED `ce547be`, GREEN `ebfa5d7`.

`_acquire_pages` counts integrity rejections in a local named **`rejected_pages`** — the name plan 24-07 promotes to `ScanBatch.pages_rejected`. A rejection keeps the existing per-page WARNING and continues, because raising on the first failure would make SCNR-03 true by construction while failing a fifty-sheet job over one bad sheet.

Two conditions that used to collapse into one are now distinct:

| Condition | Reported as |
|---|---|
| No pages fed at all | `FeederEmptyError("No paper detected in feeder")` — unchanged |
| Pages fed, every one rejected | `ScanError` naming the fed count and both integrity reasons |

The second is M-14's literal prescription and it is what makes `assemble_pdf([])` unreachable from this path. The message says "unreadable", never "load paper", because those are different things and the operator acts differently on each.

**The counter is kept local and out of the blank-page count.** Phase 23 defined that field as empty-page detection and Phase 30 renders it to users as "pages removed as blank"; routing a corrupt page through it would be a new small lie in a phase about removing them. `grep -c 'pages_removed' src/saneless/scanner/sane_backend.py` is 0.

Branches: `_acquire_pages` 8 → **9** of 12. `scan_pages` is untouched at **11 of 12** — 24-05 must still split it before adding anything.

### Task 3 — CI, the docstring, and two true documents

Commit `cc800c0`.

A fourth hardware test asserts all ten pages survive **and** that every one is still uniformly black (`getextrema() == (0, 0)`). That is SCNR-03 proven against real libsane rather than against a double, and it is the strongest available evidence that the layer no longer judges content: the pages that survive are precisely the ones the deleted policy keyed on.

The ten-page test's docstring no longer declares itself RED. `grep -c 'expected to fail\|RED' tests/test_sane_hardware.py` is 0 — which needed care, because that probe is case-sensitive and matches substrings, so an uppercase `REQUIRED` or `ACQUIRED` anywhere in the file would have tripped it.

`docs/explanation/empty-page-detection.md`'s "keep all pages" promise is true for the first time. The added paragraph states that empty-page detection is the only place content is judged, that the scanner backend never discards a page for its content, and that the only pages it skips are ones it could not read at all — described as integrity checks, explicitly not as blank-page detection.

## The CI Step — Stated Precisely

`.github/workflows/ci.yml` gained one step in the `test` job: `uv run pytest -m sane_hardware`. `grep -c 'pytest -m sane_hardware'` is 1, `apt-get install` is still 2 and `libsane` is still **2** — no new package, exactly as D-18 requires.

**Honesty note, because this phase is about truthfulness.** I cannot observe GitHub Actions from this worktree, so I am **not** claiming "the CI step went green on the first run". What is verified is that the marker passes locally (4 passed) against the same `libsane-test.so.1` that `libsane-dev` pulls in transitively, and that the workflow file is valid (`check yaml` passed at commit). **D-18's fallback was not taken** — the step is present and the marker is intact. If the step does fail on the runner because the backend is genuinely absent there, D-18's fallback is decided in advance: keep the marker and the module, drop the CI step, record why. Do not improvise a different one.

## Deviations from Plan

### 1. [Acceptance-probe correction] The `ImageStat` probe named the wrong module

- **Plan probe:** "`grep -c 'ImageStat' src/saneless/pipeline.py` is at least 1 — content policy still measures statistics, just one layer up."
- **Measured:** **0**. `pipeline.py` never imports `ImageStat`; `_drop_empty_pages` delegates to `filter_empty_pages`, and the statistics are computed in `src/saneless/pages.py` (`from PIL.ImageStat import Stat`, line 18).
- **Resolution:** the probe's *intent* — that content policy still measures statistics above the backend — is verified at the real location: `grep -c 'ImageStat' src/saneless/pages.py` is **1**. The requirement is unweakened; only the path was wrong. `_drop_empty_pages` was read and **not modified**, as instructed.

### 2. [Rule 1 — self-inflicted] My own comment broke the `pages_removed` probe

- **Found during:** Task 2 verification.
- **Issue:** I explained the D-07 boundary with a comment saying the counter is "deliberately *not* folded into pages_removed". Correct reasoning, but it put the forbidden token in the file and `grep -c 'pages_removed'` read **1** against a criterion of 0.
- **Fix:** reworded to "the pipeline's blank-page removal count", keeping the full reasoning and the Phase 23 / Phase 30 references. Behaviour identical; probe now 0. The field genuinely is not used either way.

### 3. [Rule 1 — self-inflicted] My CI comment would have broken the "no new package" probe

- **Found during:** Task 3 verification, prompted by the workflow-security hook firing on the edit.
- **Issue:** my explanatory comment named `libsane1` and `libsane-dev`, which would have pushed `grep -c libsane .github/workflows/ci.yml` from the required 2 to **4** — a criterion specifically designed to detect a new apt package being smuggled in.
- **Fix:** reworded to "the SANE runtime pulled in transitively by the headers installed above". Count back to 2.
- **On the security hook itself:** it fired because the file is a GitHub Actions workflow. The added step is a static `run:` with no `${{ }}` interpolation and no event-derived input, so there is no injection surface. No remediation was needed.

### 4. [Completeness] A sixth "five" the plan did not enumerate

- The plan listed the heading, the intro sentence, the table, the reproduce block, the "All five must exit 0" sentence and the two marker paragraphs. There is a further "still runs all five checks on the pull request" at `CONTRIBUTING.md:180`, in the `--no-verify` section, which the `grep -c 'five checks\|five commands\|All five'` = 0 criterion also covers.
- Updated to six. Final counts: `five` references **0**, `six` references 5, `pytest -m sane_hardware` in CONTRIBUTING **3** (criterion: at least 2).

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m sane_hardware -q` | **4 passed**, 0 failed (baseline 2 passed, 1 failed) |
| `uv run pytest -m "not browser and not sane_hardware" -q` | **827 passed**, 0 failed (baseline 823) |
| `uv run pytest tests/test_scanner.py -q` | **121 passed** (117 baseline + 4) |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -rn '_SCANNER_WHITE\|_SCANNER_BLACK' src/ tests/` | no matches |

Acceptance probes: `_SCANNER_*` in backend 0; `ImageStat` in backend 0; `ImageStat` in `pages.py` 1; non-comment `_MIN_PAGE_BYTES: int = 10_000` 1; `69,620` in the comment 1; `pages_removed` 0; `duplex` 6 and `parity` 3 in the backend; `pure black|pure white` in backend 0; `expected to fail|RED` in the hardware module 0; `pytest -m sane_hardware` in ci.yml 1; `apt-get install` 2; `libsane` 2; `five …` in CONTRIBUTING 0; `pytest -m sane_hardware` in CONTRIBUTING 3.

All five commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no stub. The two pre-existing `# noqa` in `_ensure_sane` were not touched.

## D-08 Recorded in Code, Not Only in Planning

`_acquire_pages`'s docstring now carries the accepted consequence in full, because a verifier reads code and not only plans: one skipped front makes `len(front_pages) != len(back_pages)`, which `pipeline.py` routes to `_handle_duplex_mismatch` — two partial PDFs plus a warning instead of one interleaved document. It states explicitly that SCNR-03's parity requirement forbids parity broken by **policy** (the backend silently discarding a clean blank back page) and not parity broken by a page that could not be read at all. Parity broken that way is reported, never hidden.

## Known Stubs

None.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-11 | mitigate | Both integrity checks retained; a truncated or zero-dimension buffer still cannot reach `assemble_pdf`. Only the *content* checks were removed |
| T-24-12 | mitigate | The all-rejected `ScanError`. A device returning only corrupt pages can no longer produce an empty PDF recorded as a success |
| T-24-13 | accept | No new apt package and no new Python package. Verified by probe: `apt-get install` and `libsane` counts in ci.yml are unchanged at 2 |
| T-24-14 | transfer | `rejected_pages` is local; its user-facing channel is D-12's result object in **plan 24-07**, which exists as `24-07-PLAN.md` — a real destination, checked |
| T-24-SC | accept | No package-manager install occurred in this plan |

## Threat Flags

None. This plan adds no network endpoint, no auth path and no schema change. It **removes** a silent data-loss path and adds one loud failure where a silent empty result used to be.

## Notes for Downstream Plans

- **24-05 inherits `scan_pages` at 11 of 12 branches**, unchanged by this plan. Split it before adding D-11's read-back, D-09's presence check or D-12's assembly. `_acquire_pages` is now at 9 of 12.
- **24-05 still owns the Protocol lie.** `SaneDevice.resolution` is declared `int` while the real device and the shared fake both return `float`. Untouched here by design; the new tests drive `scan_pages` through the `mock_sane_module` seam, so nothing forced the structural check. Fix the Protocol, do not annotate around it.
- **24-07 owns `rejected_pages`.** The local is ready to be promoted to `ScanBatch.pages_rejected`. It must stay separate from the blank-page count for the Phase 30 reason recorded above.
- **If the CI `sane_hardware` step is red on the runner**, take D-18's pre-decided fallback and nothing else.

## Self-Check: PASSED

- `src/saneless/scanner/sane_backend.py` — FOUND
- `tests/test_scanner.py` — FOUND
- `tests/test_sane_hardware.py` — FOUND
- `.github/workflows/ci.yml` — FOUND
- `CONTRIBUTING.md` — FOUND
- `docs/explanation/empty-page-detection.md` — FOUND
- Commit `1a48af3` — FOUND
- Commit `91db80c` — FOUND
- Commit `ce547be` — FOUND
- Commit `ebfa5d7` — FOUND
- Commit `cc800c0` — FOUND
