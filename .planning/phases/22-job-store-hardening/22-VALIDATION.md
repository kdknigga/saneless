---
phase: 22
slug: job-store-hardening
status: complete
nyquist_compliant: true
wave_0_complete: true
created: 2026-09-10
---

# Phase 22 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (with `pytest-timeout`; `filterwarnings = ["error"]`, `--strict-config`, `--strict-markers`) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_job.py -q` |
| **Full suite command** | `uv run pytest -m "not browser" -q` |
| **Estimated runtime** | ~28 s full suite (511 tests, 8 deselected — measured 2026-09-10; the "332" in Phase 20 CONTEXT.md is stale); `tests/test_job.py` alone ~1 s |

**Per-test hang guard:** `timeout = 60`, `timeout_method = "signal"`. The 200-round concurrency test measured **0.28 s** — 0.5 % of budget.

**Static gates (all must be clean, no suppressions — CLAUDE.md):**
`uv run ruff check .` · `uv run ruff format --check .` · `uv run ty check` · `uv run pyrefly check src tests`

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_job.py -q` (~1 s)
- **After every plan wave:** `uv run pytest -m "not browser" -q` plus all four static gates
- **Before `/gsd-verify-work`:** Full suite green AND all four static gates clean
- **Max feedback latency:** 30 s

---

## Per-Task Verification Map

Task IDs are placeholders until the planner assigns them; the **Requirement**, **Test Type** and **Automated Command** columns are the binding part.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 22-01-xx | 01 | 1 | STOR-02 | — | Fresh `:memory:` store opens at `user_version = 2` with 16 columns | unit | `uv run pytest tests/test_job.py -k migration -q` | ✅ | ✅ green |
| 22-01-xx | 01 | 1 | STOR-02 | T-22-01 | S3 fixture (`dc9b8af` shape) migrates to v2; row data survives | unit | `uv run pytest tests/test_job.py -k migration_legacy -q` | ✅ | ✅ green |
| 22-01-xx | 01 | 1 | STOR-02 | T-22-01 | S2 fixture raises `StorageError` naming the missing column; `user_version` stays 0, columns untouched, no leaked connection (D-05, D-23, D-25) | unit | `uv run pytest tests/test_job.py -k migration_guard -q` | ✅ | ✅ green |
| 22-01-xx | 01 | 1 | STOR-02 | — | Re-opening an already-migrated store is idempotent (still v2, 16 columns) | unit | `uv run pytest tests/test_job.py -k migration_idempotent -q` | ✅ | ✅ green |
| 22-01-xx | 01 | 1 | STOR-02 | T-22-02 | **File-backed** store reads back `PRAGMA journal_mode == 'wal'` (D-21 — the `:memory:`-blind failure) | unit | `uv run pytest tests/test_job.py -k journal_mode -q` | ✅ | ✅ green |
| 22-02-xx | 02 | 2 | STOR-03 | — | All six columns present after migration; `Job` exposes them defaulting to `None` | unit | `uv run pytest tests/test_job.py -k columns -q` | ✅ | ✅ green |
| 22-02-xx | 02 | 2 | STOR-03 | — | `outcome` round-trips as `ScanOutcome \| None`; page counts as `int \| None` (D-08, D-09) | unit | `uv run pytest tests/test_job.py -k outcome_roundtrip -q` | ✅ | ✅ green |
| 22-03-xx | 03 | 2 | STOR-01 | T-22-03 | Every public `JobStore` method carries the `@_locked` marker — reflective, no exemption list (D-12, D-20) | unit | `uv run pytest tests/test_job.py -k locked_coverage -q` | ✅ | ✅ green |
| 22-03-xx | 03 | 2 | STOR-01 | T-22-03 | No public method calls another public method — `ast` walk (D-24; guards the non-nesting transaction hazard) | unit | `uv run pytest tests/test_job.py -k no_public_method -q` | ✅ | ✅ green |
| 22-03-xx | 03 | 2 | STOR-01 | T-22-04 | 2 threads × 200 rounds: zero exceptions **and** the four data invariants (D-14, D-29) | unit | `uv run pytest tests/test_job.py -k stress -q` | ✅ | ✅ green |
| 22-04-xx | 04 | 3 | STOR-04 | — | Existing `test_prune_by_age` / `test_prune_by_count` / `test_prune_no_deletions` pass **unchanged** (D-16 equivalence evidence). Their `time.sleep(0.01)` at `:27`/`:79` stays — see D-32; no sleep gate in this phase | unit | `uv run pytest tests/test_job.py -k prune -q` | ✅ | ✅ green |
| 22-04-xx | 04 | 3 | STOR-04 | T-22-05 | Shuffled-insert-order prune retains exactly the newest `max_rows` (D-30 — pins an undocumented SQLite plan) | unit | `uv run pytest tests/test_job.py -k prune_shuffled -q` | ✅ | ✅ green |
| 22-04-xx | 04 | 3 | STOR-04 | — | `prune()` count exact under a concurrent insert (`cursor.rowcount`, one statement) | unit | `uv run pytest tests/test_job.py -k prune_concurrent -q` | ✅ | ✅ green |
| 22-04-xx | 04 | 3 | STOR-04 | — | `_COLUMNS` is the sole column list; `_row_to_job` the sole mapping (source assertion + no duplicate SELECT list) | unit | `uv run pytest tests/test_job.py -k single_mapping -q` | ✅ | ✅ green |
| 22-05-xx | 05 | 3 | STOR-05 | — | `fail_active_jobs()` moves every non-terminal job to `JobState.ERROR` with a "server restarted" reason, terminal rows untouched (D-19) | unit | `uv run pytest tests/test_job.py -k fail_active -q` | ✅ | ✅ green |
| 22-05-xx | 05 | 3 | STOR-05 | — | `list_pending()` returns queued jobs in creation order; predicate derived from `ACTIVE_STATES`, params `sorted()` (D-26) | unit | `uv run pytest tests/test_job.py -k list_pending -q` | ✅ | ✅ green |
| 22-06-xx | 06 | 4 | STOR-01..05 | — | Whole suite green; all four static gates clean with zero suppressions | integration | `uv run pytest -m "not browser" -q && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Recorded by plan 22-06 (2026-09-10).** Every row above was run individually and its
*collected* count checked, not just its exit status. Two corrections were made to this map
rather than to the tree, and neither changes what is covered:

1. The STOR-01 self-call row read `-k no_public_self_call`, which matches **no test in the
   suite** and collected 0 of 39. The behaviour is covered -- the test is named
   `test_no_public_method_calls_another_public_method` (`tests/test_job.py:826`) -- so the
   map's expression was stale, not the coverage. Corrected to `-k no_public_method`, which
   collects 1 and passes.
2. The map's premise that an unmatched `-k` "exits zero and would report a false green" is
   wrong on this pytest: an unmatched `-k` exits **5** (`NO_TESTS_COLLECTED`), so it would
   have broken the chained gate rather than passing it silently. The collected-count check
   is still what found the stale expression, because the stale row was never part of the
   chain -- the chain runs the whole suite, not the `-k` slices.

The final integration row's `uv run pyrefly check src tests` was run as `uv run pyrefly check src tests`.
A bare `pyrefly check` cannot work inside a `.claude/worktrees/` executor checkout: the path is
matched by `.gitignore:314`, so pyrefly checks nothing and exits 1. The explicit-path form
reports `0 errors`. The authoritative bare invocation belongs to the post-merge run on the main
checkout.

---

## Wave 0 Requirements

- [ ] `tests/test_job.py` — extend the existing 162-line file. **`test_jobstore_migration_adds_column` (line 134) must be REWRITTEN, not restyled** — it builds an S2 database, which under D-05 now correctly raises (D-28).
- [ ] `tests/test_job.py` — a **named** `tmp_path` file-backed fixture. Note the file is *not* `:memory:`-only: `:134` already takes `tmp_path` and `:149` builds a file-backed store, and `tests/test_cli.py:392,411,522,812` add four more. That coverage is incidental, and the one instance in `test_job.py` is inside the test D-28 rewrites — so the WAL guard must be its own named test rather than relying on it.
- [ ] S3 and S2 schema fixtures (helper functions constructing the `dc9b8af` and `5bd6158` shapes with raw `sqlite3`, not through `JobStore`).
- [ ] No framework install needed — pytest, `pytest-timeout` and the config are live from Phase 20.

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

This phase touches no browser surface, no hardware, and no external network service. Every criterion in ROADMAP.md § Phase 22 is reachable from `tests/test_job.py` plus the four static gates. The one criterion that could have been argued manual — "a database file written by v1.0 opens, migrates, and reports the current `user_version`" — is a `tmp_path` fixture under D-04/D-28, not a field upgrade.

---

## Notes on validation rigor

Two claims in this phase are **a tool's own contract, not ours**, and are deliberately NOT tested:

- That `threading.RLock` serialises — Python's contract. What *is* tested is that every public method takes it (reflective coverage, D-12) and that the resulting data is correct (the four invariants, D-14).
- That an unlocked store fails — deliberately not written. On Python 3.14 with `autocommit = False` the unlocked failure signature is unstable (observed `TypeError` on `:memory:`, `InterfaceError` on a file), so such a test pins an implementation detail rather than our behaviour (D-14, D-29).

Conversely, two claims **are** genuinely uncertain and each gets a dedicated test:

- The WAL/autocommit ordering, because `:memory:` hides the failure (D-21).
- `prune()`'s `LIST SUBQUERY` materialisation, because it is not a documented SQLite guarantee and this machine ships libsqlite 3.34.1 while CI may ship newer (D-30).

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 30 s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** signed off 2026-09-10 by plan 22-06
