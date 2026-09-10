---
phase: 22-job-store-hardening
plan: 06
subsystem: documentation
tags: [docs, mkdocs, traceability, phase-gate, verification, d-31]

# Dependency graph
requires:
  - phase: 22-job-store-hardening (plans 22-01..22-05)
    provides: "the shipped behaviour this plan documents and verifies -- the migration ladder, the RLock, the six columns, the single mapping, and the two unwired query methods"
provides:
  - "docs/reference/configuration.md: retention rows that describe what prune() actually deletes"
  - "docs/explanation/architecture.md § Job Storage: the persistence section the page never had"
  - "22-VALIDATION.md signed off, with one stale -k expression corrected and recorded"
  - "a per-criterion traceability record for ROADMAP § Phase 22's five success criteria"
affects: [22-verification, 23-honest-outcomes, 26-worker-robustness, 30-appliance-ux, 31-docs-audit]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "docs house style held: ` -- ` never an em dash, backticked identifiers, prose over bullets, why over what"
    - "reserved-but-unwritten schema columns named once, on one physical line with the 'nothing writes them' clause, so a line-based grep cannot read the names without the caveat"

key-files:
  created: []
  modified:
    - docs/reference/configuration.md
    - docs/explanation/architecture.md
    - .planning/phases/22-job-store-hardening/22-VALIDATION.md

decisions:
  - "The VALIDATION map's `-k no_public_self_call` was stale, not the coverage; corrected the map rather than renaming a green test"
  - "REQUIREMENTS.md STOR-01..05 left at Pending and handed to the orchestrator, because the plan directed the edit at ROADMAP.md which this executor is forbidden to touch"
  - "docker.md, cli-commands.md and first-web-ui-scan.md checked against the tree and deliberately left unchanged"

metrics:
  duration: "~25 min"
  completed: 2026-09-10
  tasks: 2
  commits: 2
---

# Phase 22 Plan 06: Storage Docs and Phase Gate Summary

Corrected the one storage sentence this project actually had wrong, gave `architecture.md` the
persistence section it never had, and ran the phase-level gate -- which caught a stale `-k`
expression that had been sitting in the validation map since planning.

## What Shipped

**`docs/reference/configuration.md`** -- the two retention rows. `history_retention_days` said
*"Days to keep completed job history"* and `history_max_rows` said *"Maximum job history entries
in SQLite"*. `prune()` deletes by `created_at` and never looks at whether a job reached a
terminal state, so it does not keep *completed* history, it keeps *all* history. Both cells now
say so, in the same four-column shape, sentence case, no trailing period.

**`docs/explanation/architecture.md` § Job Storage** -- a new `##` section at `:53`, between
`## Worker Thread Model` (`:45`) and `## Configuration Layer` (`:67`). Six paragraphs: the single
`JobStore`-owned connection and the WAL-before-autocommit ordering; the `threading.RLock` and why
two caller populations sharing one connection require it; the `PRAGMA user_version` ladder and how
fresh, legacy and already-migrated databases each enter it; why an unrecognised schema is refused
loudly at open rather than at the first write; what retention actually deletes; and the six
reserved columns.

**`22-VALIDATION.md`** -- signed off, all 17 map rows recorded green, with two corrections noted
in the file itself (below).

## Per-Criterion Traceability: ROADMAP § Phase 22

All five criteria are met. Three carry a wording divergence between the criterion and the
shipped code that is worth stating plainly rather than glossing.

| # | Criterion | Evidence | Verdict |
|---|-----------|----------|---------|
| 1 | Two threads × 200 rounds, zero exceptions, no interleaved-transaction corruption | `test_stress_two_threads_survive_two_hundred_rounds`; `STRESS_ROUNDS = 200`, `STRESS_WORKERS = 2`; 1 passed | **Met** |
| 2 | A v1.0 database opens, migrates, reports the current `user_version`; the bare `ALTER TABLE ... except: pass` is gone | `-k migration` 5 passed, `-k journal_mode` 1 passed; `grep 'except.*:\s*pass' src/saneless/job.py` → 0 | **Met** |
| 3 | Six result columns present after a single migration step | `_migrate_v2` adds all six in one step; `-k columns` 10 passed, `-k outcome_roundtrip` 3 passed | **Met** |
| 4 | `fail_active_jobs()` marks every non-terminal job FAILED with a "server restarted" reason; `list_pending()` returns queued jobs in creation order | `-k fail_active` 5 passed, `-k list_pending` 3 passed | **Met, with two wording divergences** |
| 5 | The row-to-`Job` mapping and its column list appear exactly once; `prune()` reports its count from one statement | `-k single_mapping` 5 passed, `-k prune` 7 passed, `-k prune_shuffled` 2 passed, `-k prune_concurrent` 2 passed | **Met** |

### Divergences worth stating

**Criterion 4 says FAILED; there is no `JobState.FAILED`.** Jobs move to `JobState.ERROR`. Plan
22-05 made this explicit in the method docstring and pinned it with a test that asserts no
`JobState.FAILED` member is added -- the value is persisted as TEXT and read back through the
enum constructor, so adding a member would be a data migration. The criterion is met on
substance; its wording names a state that does not exist.

**Criterion 4 says a "server restarted" reason; the default is `"Interrupted by restart"`.**
Same meaning, different string, and it is a parameter rather than a literal. Recorded so a later
phase wiring `fail_active_jobs()` does not assert on the criterion's wording.

**Criterion 3 says "most stay unused until later phases"; in fact *all six* stay unused.**
Nothing writes `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning` or
`owner_token`. They read back `None` on every job. The criterion is met and then some -- the
reality is stricter than the wording -- but "most" would let a reader infer that some column has
a writer today. None does.

### What this phase did NOT ship

Stated so no reader of this SUMMARY infers more than was built:

- `fail_active_jobs()` and `list_pending()` exist and are **called by nothing**. Startup recovery
  is Phase 26 (ROBU-05); queue display is Phase 30 (APPL-08).
- The six columns are **written by nothing**. Phase 23 owns `outcome` and `warning`; Phase 30
  owns the page counts and `owner_token`.
- `owner_token` is **not an auth mechanism**. REQUIREMENTS.md § Out of Scope fixes it as a
  footgun guard for the Phase 30 flip prompt on a trusted LAN. No documentation sentence
  presents it otherwise (T-22-18).
- `close()` was **deliberately not hardened** against use-after-close -- Phase 26, D-15.
- The database **still lives under `tmp_dir`**. Moving it onto `output.data_dir` is Phase 23
  (OUTC-09, D-18), and the docs continue to describe `tmp_dir`, because that is still true.

## Gate Results

Run chained in one invocation so a failure anywhere fails the whole thing:

```
uv run pytest -m "not browser" -q && uv run ruff check . && uv run ruff format --check . \
  && uv run ty check && uv run pyrefly check src tests
```

| Gate | Result |
|------|--------|
| `pytest -m "not browser" -q` | **537 passed**, 8 deselected -- matches the wave baseline exactly |
| `ruff check .` | No issues found |
| `ruff format --check .` | 40 files already formatted |
| `ty check` | All checks passed |
| `pyrefly check src tests` | **0 errors** |
| `mkdocs build --strict` | exit 0 |
| Chained exit | **0** |

**On the pyrefly invocation.** A bare `uv run pyrefly check` cannot work inside a
`.claude/worktrees/` executor checkout: the worktree path is matched by `.gitignore:314`, so
pyrefly skips every include pattern, reports *"No Python files matched patterns"*, and exits **1**.
Reproduced here to confirm. The explicit-path form `pyrefly check src tests` is what was run and
what reports 0 errors. **The authoritative bare `pyrefly check` belongs to the orchestrator's
post-merge run on the main checkout.** Commits used `SKIP=pyrefly-checker`; every other hook,
including project-wide `ty`, ran and passed. No commit used `--no-verify`.

### Per-`-k` collected counts

Each expression run individually with its **collected** count checked, not just its exit status.

| `-k` expression | Collected / Passed |
|-----------------|--------------------|
| `migration` | 5 / 5 |
| `migration_legacy` | 1 / 1 |
| `migration_guard` | 1 / 1 |
| `migration_idempotent` | 1 / 1 |
| `journal_mode` | 1 / 1 |
| `columns` | 10 / 10 |
| `outcome_roundtrip` | 3 / 3 |
| `locked_coverage` | 1 / 1 |
| `no_public_self_call` | **0 / 0 — stale, see below** |
| `no_public_method` (corrected) | 1 / 1 |
| `stress` | 1 / 1 |
| `prune` | 7 / 7 |
| `prune_shuffled` | 2 / 2 |
| `prune_concurrent` | 2 / 2 |
| `single_mapping` | 5 / 5 |
| `fail_active` | 5 / 5 |
| `list_pending` | 3 / 3 |

## Findings

**1. The VALIDATION map's `-k no_public_self_call` matched no test in the suite.** It collected
0 of 39. The behaviour *is* covered -- the test is
`test_no_public_method_calls_another_public_method` at `tests/test_job.py:826`, and it passes.
The map's expression was stale, not the coverage. Corrected the map to `-k no_public_method`
rather than renaming a green test, and recorded the correction in `22-VALIDATION.md` itself so
it is not a silent edit. **This is the check T-22-20 was written for, and it earned its place.**

**2. The map's stated premise for that check was itself wrong.** It claimed an unmatched `-k`
"exits zero and would report a false green". On this pytest an unmatched `-k` exits **5**
(`NO_TESTS_COLLECTED`), so it would have *broken* a chained gate, not passed one silently.
Verified directly. The collected-count check still found the stale row -- but for a different
reason than the map gave: the stale row was never in the chain at all, because the chain runs
the whole suite rather than the `-k` slices. Corrected in the file.

**3. `git merge-base HEAD master` is not a phase scope boundary here.** The plan's containment
assertion used it; `master` predates the entire milestone, so it returns `a87b3dd` and the
diffstat shows all 68 files in the project. Used the real phase boundary instead --
`231d48a`, the last Phase 21 commit.

**4. A first-pass equivalence check falsely flagged `test_prune_no_deletions` as modified.**
The regex that extracted test bodies stopped at `^def ` but not `^class `, so it swept trailing
class content into the comparison. With the class boundary respected, all three protected prune
tests are byte-identical to phase start. Recorded because the first result, taken at face value,
would have reported a D-16 violation that does not exist.

## Scope and Suppression Containment

**Suppressions: 7 tree-wide, zero added by this phase.** Counted independently in Python because
`grep | wc -l` reported 9 -- the extra two were the output filter's own header lines, not
matches. All 7 pre-date Phase 22: `config.py:124,125`, `scanner/__init__.py:23`,
`scanner/sane_backend.py:48,50`, `test_cli.py:553`, `test_web.py:397`. `job.py`,
`exceptions.py` and `test_job.py` carry **zero**.

**Scope, `231d48a..HEAD` restricted to non-`.planning/` paths:**

```
docs/explanation/architecture.md |   14 +
docs/reference/configuration.md  |    4 +-
src/saneless/exceptions.py       |    5 +
src/saneless/job.py              |  720 +++++++++++++-----
tests/test_job.py                | 1041 ++++++++++++++++++++++++-
```

Exactly the five files the plan permits. `worker.py`, `web/app.py`, `web/routes.py` and `cli.py`
are untouched across the whole phase.

**Protected artifacts intact:** `test_prune_by_age`, `test_prune_by_count` and
`test_prune_no_deletions` are byte-identical to phase start (D-16). Both `time.sleep(0.01)` calls
survive, at `tests/test_job.py:449` and `:501` (D-32); no sleep gate was added.

## Doc Pages Checked and Deliberately Left Unchanged

Each was read against the shipped tree, not assumed. Phase 31's DOCS-01 owns the milestone-wide
audit; this phase corrects only what it made or found false.

| Page | Sentence | Verdict |
|------|----------|---------|
| `docs/reference/docker.md:30` | `/tmp/saneless` -- "Scan temp files and SQLite database" | **Still true.** The database has not moved; D-18 puts that in Phase 23. Unchanged. |
| `docs/reference/cli-commands.md:85` | "List recent scan job history from the SQLite database." | **Still true.** `saneless jobs` reads `list_recent()` unchanged. Unchanged. |
| `docs/getting-started/first-web-ui-scan.md:46-54` | § Check job history, "four columns: Time, Profile, Title, Status" | **Still true.** Verified against `web/templates/partials/history.html`, which renders exactly those four `<td>`s. This phase rendered no new column. Unchanged. |

## Deviations from Plan

**1. [Rule 3 - Blocking] The plan directed the traceability edit at `.planning/ROADMAP.md`;
this executor is forbidden to write it.**

- **Found during:** Task 2
- **Issue:** Task 2 says to update "`.planning/ROADMAP.md`'s traceability table rows for
  STOR-01..05 from Pending to Complete". The orchestrator's brief forbids modifying `ROADMAP.md`
  and `STATE.md`. Separately, the traceability table does not live in `ROADMAP.md` at all -- the
  STOR-01..05 rows are `REQUIREMENTS.md:222-226`, and the checkboxes are `REQUIREMENTS.md:26-30`.
- **Fix:** Left both files untouched and recorded the verified-complete status here instead.
  Precedent supports this: `231d48a docs(21): add phase verification and mark CTR requirements`
  shows requirement marking is done at phase verification, not inside an executor worktree.
- **Files modified:** none
- **Handoff:** **STOR-01..STOR-05 are verified complete by the evidence in the traceability
  table above. The orchestrator or verifier should mark `REQUIREMENTS.md:26-30` and
  `:222-226`, and tick ROADMAP's `22-06-PLAN.md` line and the Phase 22 checkbox.**

**2. [Rule 1 - Bug] Corrected a stale `-k` expression in `22-VALIDATION.md`.**

- **Found during:** Task 2
- **Issue:** `-k no_public_self_call` collected 0 of 39 tests.
- **Fix:** Corrected to `-k no_public_method`, which collects and passes the test that covers the
  behaviour. Also corrected the map's incorrect claim about the exit code of an unmatched `-k`.
  Both corrections are written into `22-VALIDATION.md` rather than applied silently.
- **Files modified:** `.planning/phases/22-job-store-hardening/22-VALIDATION.md`
- **Commit:** `b60b2f8`

**3. [Environment] The worktree guard fired, for the fourth consecutive wave.**

- `git symbolic-ref HEAD` → `refs/heads/worktree-agent-abadfbe691b75630a` (namespace OK, not
  protected). `git merge-base HEAD b1ecfdc` returned `a87b3dd`, not `b1ecfdc`: HEAD was spawned
  at the stale `ed2d620` again. Reset to `b1ecfdc` as the guard prescribes, then confirmed all
  five prior plans (22-01..22-05) present before doing any work. **This is now a reproducible
  spawn defect, not an intermittent one, and is worth fixing upstream rather than absorbing in
  every executor.**

## Known Stubs

None introduced. The six reserved columns are pre-existing unwritten schema, documented as such
and owned by Phases 23 and 30; this plan added no code.

## Threat Flags

None. This plan changed documentation and one planning file; it added no network endpoint, auth
path, file access pattern or schema change. T-22-18 (no `owner_token`-as-auth sentence) and
T-22-19 (the retention claim) are both discharged by the Task 1 edits and asserted by grep;
T-22-20 is discharged by the collected-count checks, which caught a real stale row.

## Self-Check: PASSED

- `docs/reference/configuration.md` — FOUND, `completed job history` count 0
- `docs/explanation/architecture.md` — FOUND, `## Job Storage` count 1, ordered `:45` → `:53` → `:67`
- em-dash count 0 in both doc pages
- `owner_token|pages_scanned|pages_removed|pages_uploaded` across `docs/` — 1 line, and it states nothing writes them
- `data_dir` in `configuration.md` — 0 matches, Phase 23's move not pre-announced
- `.planning/phases/22-job-store-hardening/22-VALIDATION.md` — FOUND, `nyquist_compliant: true`, `wave_0_complete: true`, 0 pending rows outside the legend
- commit `65b7c28` — FOUND
- commit `b60b2f8` — FOUND
- `STATE.md` / `ROADMAP.md` — NOT MODIFIED (confirmed absent from this branch's diff)
