---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 03
subsystem: pdf
tags: [img2pdf, pikepdf, path-traversal, sanitisation, mediabox, dpi, security]

# Dependency graph
requires:
  - phase: 22-storage
    provides: the job database and data_dir layout the preserved PDFs land in
provides:
  - "sanitise_title_for_filename: an allow-list sanitiser turning an untrusted title into one safe path segment"
  - "build_pdf_filename: {timestamp}-{job id}-{slug}.pdf, unique per job rather than per second"
  - "assemble_pdf(images, output_dir, filename, dpi): filename and dpi both required, no defaults"
  - "Fixed-DPI page layout: an A4 page scanned at 300 DPI is a 595 x 842 pt page"
  - "PipelineRequest.job_id, defaulted empty, supplied by the worker in plan 23-07"
affects: [23-06 preservation guard, 23-07 worker job_id wiring, 24 device DPI read-back]

# Tech tracking
tech-stack:
  added: [pikepdf (promoted from img2pdf transitive to explicit dev dependency)]
  patterns:
    - "Allow-list sanitisation for anything user-supplied that becomes a path segment"
    - "Required-not-defaulted parameters when a default would silently preserve the bug being removed"
    - "Assert the security property (resolved path stays inside base), not the sanitised string"

key-files:
  created: []
  modified:
    - src/saneless/pdf.py
    - src/saneless/pipeline.py
    - tests/test_pdf.py
    - pyproject.toml
    - uv.lock
    - .pre-commit-config.yaml

key-decisions:
  - "filename is REQUIRED on assemble_pdf, not defaulted: a default would have kept all seven existing call sites green while silently preserving the exact collision D-09 removes"
  - "Uniqueness comes from the uuid4 job id, not the timestamp: two jobs submitted in the same second with the same title would otherwise collide, and shutil.move overwrites silently"
  - "The job id is put through the same sanitiser as the title (Rule 2): it is a path segment like any other, and 'expected to be a uuid4' is not a control this code owns"
  - "An empty sanitisation result drops its segment rather than substituting 'untitled', which would be a lie about what the operator typed"
  - "_handle_duplex_mismatch drops its redundant notify parameter to stay inside PLR0913 rather than suppressing the rule"
  - "Tasks 2 and 3 share one commit: with filename required and ty/pyrefly as pre-commit hooks, the signature change and its call-site sweep cannot be split without committing a type-broken tree"

patterns-established:
  - "Allow-list over deny-list: re.sub(r'[^a-z0-9]+', '-') makes /, \\, .., ~ and NUL unrepresentable by construction rather than by enumeration"
  - "Parametrised hostile-input lists (HOSTILE_TITLES) so adding a future attack case is one line"
  - "MediaBox assertions always round; the true values are 595.2 x 841.92 and 595.2 x 841.68, never exactly 595 x 842"
  - "Negative assertions that pin the bug (box != [0, 0, 1860, 2631]) so a test cannot pass for an unrelated reason"

requirements-completed: [OUTC-05, OUTC-06]

# Metrics
duration: 12min
completed: 2026-09-11
---

# Phase 23 Plan 03: Unique PDF Names and Honest Page Geometry Summary

**An allow-list filename sanitiser with parametrised path-traversal tests, a job-id-keyed `build_pdf_filename`, and an `assemble_pdf` that passes img2pdf's fixed-DPI layout function so an A4 page scanned at 300 DPI is a 595 x 842 pt page instead of a 1860 x 2631 pt one.**

## Performance

- **Duration:** 12 min
- **Started:** 2026-09-11T15:09:25Z
- **Completed:** 2026-09-11T15:21:24Z
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments

- **The phase's one new attack surface is closed by construction.** A user-supplied job title becomes part of a filesystem path for the first time in this codebase. `sanitise_title_for_filename` collapses everything outside `[a-z0-9]` to a single hyphen, so `/`, `\`, `..`, `~` and NUL are not *filtered* — they are unrepresentable. Nine hostile inputs are parametrised and each asserts the security property (`resolved.is_relative_to(base)`), not a string equality.
- **Two jobs with the same title now produce two files.** Every document previously reached paperless as `output.pdf`, and two preserved scans overwrote each other in `failed/`. Uniqueness now derives from the uuid4 job id.
- **A4 is A4.** `img2pdf.default_dpi` is 96, so an unlayouted 2480x3508 raster was laid out as 1860x2631 pt. Passing `get_fixed_dpi_layout_fun((dpi, dpi))` — a 2-tuple, not a scalar — yields 595.2 x 841.92, which rounds to the 595 x 842 the roadmap asks for.
- **The duplex halves get distinct names** (`...-tax-return-fronts.pdf` / `...-tax-return-backs.pdf`), both carrying the job id, so plan 23-06's single preservation guard cannot have one half destroy the other.
- **Full suite green:** 604 passed. `ruff check`, `ruff format --check`, `ty check` and `pyrefly check` all clean, with no `# type: ignore`, no `# noqa`, and no rule disabled anywhere in the diff.

## Task Commits

1. **Task 1 (RED): failing sanitiser and filename tests** — `215ce60` (test)
2. **Task 1 (GREEN): allow-list sanitiser and build_pdf_filename** — `27be875` (feat)
3. **Task 2 (RED): failing MediaBox tests, pikepdf promoted to a dev dependency** — `dfd2733` (test)
4. **Tasks 2 (GREEN) + 3: required filename/dpi, fixed-DPI layout, all three pipeline call sites** — `2a554e0` (feat)

TDD gate sequence verified in `git log`: `test(...)` → `feat(...)` → `test(...)` → `feat(...)`. Both RED commits were run and observed failing before their GREEN counterpart — the second one failed at exactly the documented `[0, 0, 1860, 2631]`, confirming the test was measuring the real bug.

## Files Created/Modified

- `src/saneless/pdf.py` — `sanitise_title_for_filename`, `build_pdf_filename`, and `assemble_pdf` now taking a required `filename` and `dpi` and passing `layout_fun`
- `src/saneless/pipeline.py` — `PipelineRequest.job_id`; all three `assemble_pdf` call sites named and DPI-aware; `_handle_duplex_mismatch` takes `dpi` and derives `notify`
- `tests/test_pdf.py` — `TestSanitiseTitleForFilename`, `TestBuildPdfFilename`, `TestMediaBox`; the seven existing `TestAssemblePdf` call sites updated
- `pyproject.toml`, `uv.lock` — `pikepdf` promoted to an explicit dev dependency
- `.pre-commit-config.yaml` — pyrefly hook given explicit paths (see deviations)

## Decisions Made

**`filename` is required, with no default.** This is the deliberate choice `23-VALIDATION.md` asked to be recorded. A default of `"output.pdf"` would have kept all seven existing `TestAssemblePdf` call sites compiling and green while silently preserving the exact collision D-09 exists to remove. Requiring it at the type level forces every caller — present and future — to name its own file, and the type checkers enumerated the three production call sites for free.

**Uniqueness comes from the job id, not the clock.** The timestamp leads the name only so a directory listing sorts usefully. Two jobs submitted in the same second with the same title would collide on a timestamp alone, and a collision is not cosmetic: `shutil.move` onto an explicit destination overwrites silently, so two same-named PDFs preserved into `failed/` would destroy one scan.

**An empty sanitisation result drops its segment.** `"日本語"` and `"..."` both sanitise to `""`, and `build_pdf_filename` omits the segment rather than substituting a literal `untitled` — which would be a lie about what the operator typed.

**`profile.resolution` is the authoritative DPI, with no fallback branch.** PIL carries no DPI on this path at all (`crop_to_paper_size(...).info` is `{}`), so a "prefer the image's own DPI" branch would be unreachable dead code, and a PNG round-trip degrades 300 to 299.9994 anyway. `assemble_pdf`'s docstring records this and names Phase 24 as the phase that adds device read-back, with the exact line that changes.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] pyrefly pre-commit hook fails inside a git worktree**

- **Found during:** Task 1 (first commit attempt)
- **Issue:** This executor runs in a worktree at `.claude/worktrees/agent-*`, and `.claude/worktrees/` is in the repo's `.gitignore` (line 314). Pyrefly's `use-ignore-files` is on by default, so every source file was filtered out and the hook failed with `No Python files matched patterns`. This blocked *every* commit in the plan, and `--no-verify` is forbidden.
- **Fix:** Changed the hook entry from `uv run pyrefly check` to `uv run pyrefly check src tests`, with a comment explaining why. Per pyrefly's documentation (confirmed via Context7): `use-ignore-files` "is bypassed when files are explicitly specified for checking." Naming the paths is the documented mechanism, not a workaround, and it behaves identically from the main checkout.
- **Files modified:** `.pre-commit-config.yaml`
- **Verification:** `uv run pyrefly check src tests` reports 0 errors; the hook passed on all four commits.
- **Committed in:** `215ce60`
- **Note for the orchestrator:** this is a shared file. It is a one-line entry change plus a comment, so a merge with a sibling wave-1 worktree should be trivial, but it is worth a look.

**2. [Rule 2 - Missing Critical] The job id was not sanitised**

- **Found during:** Task 1 (GREEN)
- **Issue:** The plan specified `job_id[:8]` spliced straight into the filename. In production the job id is a uuid4 and every character survives the allow-list untouched — but the id is a path segment joined against `consume_dir` and `failed/` exactly like the title, and a caller passing `../../etc` would have produced `../../et` as a segment. A path segment that is merely *expected* to be safe is not a control this function owns.
- **Fix:** `build_pdf_filename` puts the job id through `sanitise_title_for_filename` before truncating. Zero behavioural change for real uuid4s.
- **Files modified:** `src/saneless/pdf.py`
- **Verification:** `TestBuildPdfFilename.test_a_hostile_job_id_also_cannot_escape`, parametrised over all nine hostile inputs.
- **Committed in:** `27be875`

**3. [Rule 3 - Blocking] `_handle_duplex_mismatch` exceeded the PLR0913 argument limit**

- **Found during:** Task 3
- **Issue:** Adding the plan's `dpi: int` parameter took the function to 6 arguments against a limit of 5. Ruff counts keyword-only arguments too, so `*, dpi: int` did not help. Raising `max-args` or adding `# noqa` are both forbidden by CLAUDE.md.
- **Fix:** Removed the `notify` parameter, which was redundant — it is exactly `request.status_callback or _noop_callback`, and `request` is already passed in. The function now derives it, with a comment saying so. No test called the helper directly; all 30 `PipelineRequest` constructions are keyword-based.
- **Files modified:** `src/saneless/pipeline.py`
- **Verification:** `uv run ruff check .` clean; all duplex-mismatch tests pass via `run_pipeline`.
- **Committed in:** `2a554e0`

**4. [Rule 1 - Bug] Duplicated `assemble_pdf` call in an existing test**

- **Found during:** Task 2
- **Issue:** `test_assemble_multiple_pages` called `assemble_pdf` for the single-page case twice, with a `mkdir(exist_ok=True)` between them — a copy-paste artefact. `assemble_pdf` already creates its own `output_dir`.
- **Fix:** Collapsed to one call and dropped the redundant `mkdir`s. The assertion is unchanged.
- **Files modified:** `tests/test_pdf.py`
- **Committed in:** `2a554e0`

### Structural deviations

**Tasks 2 and 3 share commit `2a554e0`.** The plan asked for them to be separately reviewable, and its `<verification>` section anticipated a transient window where `pipeline.py` still calls `assemble_pdf` with two positional arguments. That window cannot be committed: `ty` and `pyrefly` run as pre-commit hooks over the working tree, and `--no-verify` is forbidden. Splitting would have required either a default for `filename` (explicitly forbidden — it would preserve the collision) or a type-broken commit. The plan's more important constraint won. Task 3's dataclass change could not be split out either, because the hooks read the working tree rather than the index.

**`pyproject.toml` / `uv.lock` were modified** although they are not in this plan's `files_modified`. The plan's `<interfaces>` authorised this conditionally ("If that plan has not landed, run `uv add --dev pikepdf` first"), and plan 23-01 had not landed in this worktree. Plan 23-01 Task 1 does the same promotion; both produce the identical `"pikepdf>=10.5.1"` line, so the merge should be clean.

---

**Total deviations:** 4 auto-fixed (2 blocking, 1 missing critical, 1 bug) + 2 structural
**Impact on plan:** No scope creep. The two blocking fixes were the price of committing at all; the Rule 2 fix closes a real hole in the plan's own threat model; the structural merge preserves the plan's explicit "no default for filename" constraint at the cost of commit granularity.

## Issues Encountered

- **`ty` rejected iterating `pdf.pages[0].MediaBox`** — pikepdf's own stubs declare `Object.__iter__` as returning `Iterable[Object]`, which has no `__next__`. Rather than suppress it, the tests read geometry through `pikepdf.Page.mediabox` (stub-typed `-> Array`) wrapped in `pikepdf.Rectangle`, which is pikepdf's intended typed API and also resolves an inherited MediaBox from the page tree correctly. Centralised in a `_rounded_media_box` helper.
- **`D301`** fired on the sanitiser docstring, which needs literal backslashes to describe what it strips. Fixed with ruff's own recommendation — an `r"""` docstring — not a suppression.
- **`filterwarnings = ["error"]`** was a live concern for `pikepdf.open` per RESEARCH Pitfall 6. No warnings materialised; the tests use `with pikepdf.open(...)` so no `ResourceWarning` can leak either.

## Threat Flags

None. This plan's changes sit entirely inside the trust boundary the plan's `<threat_model>` already enumerated, and T-23-09 through T-23-13 are each covered by a test. T-23-14 (the title echoed as paperless's original filename) remains accepted and unchanged.

## Verification Against Success Criteria

| Criterion | Result |
|-----------|--------|
| Allow-list, caps at 60, returns `""` when fully stripped | PASS |
| Every traversal input stays inside its base directory | PASS (9 inputs x 2 parametrised classes) |
| `build_pdf_filename` distinct for two jobs sharing a title | PASS |
| `filename` and `dpi` required; hardcoded output name gone from `pdf.py` | PASS (`grep -c` returns 0) |
| A4 at 300 DPI rounds to `[0, 0, 595, 842]`, provably not `[0, 0, 1860, 2631]` | PASS (both assertions) |
| `PipelineRequest.job_id`; 3 call sites pass `filename` and `dpi=profile.resolution` | PASS |
| Hardcoded output name appears nowhere in `src/` | PASS (`grep -rc` returns 0 across every file) |
| Full suite, ruff, ruff format, ty, pyrefly | PASS (604 passed, 0 errors) |
| No `# type: ignore`, `# noqa`, or disabled rule in the diff | PASS |

## Self-Check: PASSED

- `src/saneless/pdf.py` — FOUND
- `src/saneless/pipeline.py` — FOUND
- `tests/test_pdf.py` — FOUND
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-03-SUMMARY.md` — FOUND
- Commits `215ce60`, `27be875`, `dfd2733`, `2a554e0` — all FOUND in `git log`

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- **Plan 23-06** can build its preservation guard on `pdf_path.name`: the name is unique per job and both duplex halves are distinct, so moving into `failed/` cannot overwrite.
- **Plan 23-07** must wire `job.id` into `PipelineRequest(job_id=...)` at `worker.py:234`. Until it does, production PDFs are named from the timestamp and title alone — correct and safe, but not yet collision-proof between two same-second same-title jobs. This is the one loose end.
- **Phase 24** owns device DPI read-back; the line to change is the `dpi=` argument at the three `pipeline.py` call sites, recorded in `assemble_pdf`'s docstring.
- **No blockers.**

---
*Phase: 23-honest-outcomes-and-never-lose-a-scan*
*Completed: 2026-09-11*
