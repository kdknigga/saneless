---
phase: 23
slug: honest-outcomes-and-never-lose-a-scan
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-09-11
---

# Phase 23 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `23-RESEARCH.md` § "Validation Architecture". Task IDs are filled in by the planner.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 (+ pytest-timeout 2.4.0, pytest-playwright 0.7.0) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_vocabulary.py tests/test_pdf.py tests/test_paperless.py -x -q` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | Full suite well under the `timeout = 60` per-test ceiling; the five OUTC-10 cases together sleep < 1 s |

Strictness already in force (do not weaken to make a test pass): `filterwarnings = ["error"]`,
`xfail_strict = true`, `--strict-markers`, `--strict-config`, `timeout = 60`,
`timeout_method = "signal"`.

---

## Sampling Rate

- **After every task commit:** `uv run ruff check . && uv run ruff format --check . && uv run pytest <touched test module> -x -q`
- **After every plan wave:** `uv run pytest && uv run ty check && uv run pyrefly check`
- **Before `/gsd-verify-work`:** full suite green and both type checkers clean — this is the Phase 20 CI gate's exact contract
- **Max feedback latency:** < 60 s

---

## Per-Task Verification Map

Task IDs are assigned by the planner; the requirement-to-test mapping below is fixed and must be
preserved. "File Exists ❌ W0" means Wave 0 must create it before the implementing task runs.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | OUTC-01 | — | `poll_task` raises `PaperlessError` on FAILURE carrying the Paperless message; `PaperlessTimeoutError` past the deadline | unit | `uv run pytest tests/test_paperless.py -k "poll_task_failure or poll_task_timeout" -x` | ✅ `tests/test_paperless.py:250-300` — existing cases assert the OLD dict returns and must be rewritten | ⬜ pending |
| TBD | TBD | TBD | OUTC-01 | — | Worker records ERROR, never DONE, when the pipeline raises | unit | `uv run pytest tests/test_worker.py -k finish -x` | ❌ W0 — new `TestFinishJob` | ⬜ pending |
| TBD | TBD | TBD | OUTC-02 | — | `finish_job` persists FALLBACK + outcome + warning + three counts in one write | unit | `uv run pytest tests/test_job.py -k finish_job -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-02 | — | `state_label(FALLBACK) == "Saved to folder"`; ACTIVE/TERMINAL XOR holds; all total lookups cover the new member | unit, parametrised | `uv run pytest tests/test_vocabulary.py -x` | ✅ parametrised cases self-fail; four hand-written lists need edits | ⬜ pending |
| TBD | TBD | TBD | OUTC-02 | — | Status partial and history table render FALLBACK distinctly from DONE | unit | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ needs a FALLBACK case | ⬜ pending |
| TBD | TBD | TBD | OUTC-02 | — | `saneless jobs` renders FALLBACK distinctly | unit | `uv run pytest tests/test_cli.py -k jobs -x` | ✅ needs a FALLBACK case | ⬜ pending |
| TBD | TBD | TBD | OUTC-03 | — | Duplex mismatch persists warning + outcome + counts, never a silent DONE | integration | covered by the OUTC-10 parametrised case | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-04 | T-23-xx | PDF lands in `failed_dir` when upload raises **and** when poll raises | integration | `uv run pytest tests/test_pipeline.py -k preserv -x` | ❌ W0 — the poll-raises case is the one the roadmap note exists for | ⬜ pending |
| TBD | TBD | TBD | OUTC-04 | T-23-xx | The re-raised error names the destination path | integration | `uv run pytest tests/test_pipeline.py -k preserv -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-05 | T-23-xx | Two jobs with the same title yield two distinct filenames; sanitiser rejects `/`, `..`, control chars | unit | `uv run pytest tests/test_pdf.py -k name -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-05 | T-23-xx | Consume-dir copy goes via `.part` and no `.part` remains after the rename | unit | `uv run pytest tests/test_paperless.py -k consume -x` | ✅ extend the existing fallback case | ⬜ pending |
| TBD | TBD | TBD | OUTC-06 | — | A4 @ 300 DPI → MediaBox `[0, 0, 595, 842]` **after rounding** (actual 595.2 x 841.92) | unit | `uv run pytest tests/test_pdf.py -k mediabox -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-07 | — | Non-200 raises immediately (not after the timeout); deadline includes request time; 200-with-empty-list keeps polling | unit | `uv run pytest tests/test_paperless.py -k poll -x` | ✅ file exists, cases missing | ⬜ pending |
| TBD | TBD | TBD | OUTC-08 | — | 2xx / 401 / 403 / 404 / 5xx / ConnectError → five distinct members; JSON strings unchanged for the original three | unit, parametrised | `uv run pytest tests/test_paperless.py -k connection -x` | ✅ needs parametrisation over `list(ConnectionStatus)` | ⬜ pending |
| TBD | TBD | TBD | OUTC-09 | T-23-xx | `db_path`/`failed_dir` derive from `data_dir`; `validate_settings_dirs` rejects an unwritable `data_dir`; `JobStore` opens under a freshly created `data_dir` | unit | `uv run pytest tests/test_config.py -k data_dir -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | OUTC-10 | — | Real worker + real pipeline + stub scanner through SUCCESS / FAILURE / TIMEOUT / consume-dir fallback / duplex mismatch, asserting persisted state, outcome, page counts, file preservation | integration, parametrised | `uv run pytest tests/test_outcomes_e2e.py -x` | ❌ W0 — new file | ⬜ pending |
| TBD | TBD | TBD | OUTC-11 | T-23-xx | `poll_task` reaches a terminal status against BOTH a v9-shaped and a v10-shaped `/api/tasks/` response; client sends an explicit API-version `Accept` header | unit, parametrised | `uv run pytest tests/test_paperless.py -k api_version -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_outcomes_e2e.py` — new file, covers OUTC-10 and transitively OUTC-01/02/03/04
- [ ] `tests/test_pdf.py` — add MediaBox and filename-uniqueness/sanitisation classes (OUTC-05, OUTC-06)
- [ ] `tests/test_job.py` — add `TestFinishJob` (OUTC-02, D-04)
- [ ] `tests/test_config.py` — add `data_dir` / `db_path` / `failed_dir` / writability cases (OUTC-09)
- [ ] `tests/test_pipeline.py` — preservation-guard cases for **both** the upload-raises and poll-raises paths (OUTC-04)
- [ ] `tests/test_vocabulary.py` — update the four hand-written lists (the parametrised cases self-update)
- [ ] `tests/test_paperless.py` — rewrite the three `TestPollTask` cases from return-value to raise semantics; add non-200, empty-list-tolerance, and v9/v10 API-shape cases; parametrise `test_connection`
- [ ] `uv add --dev pikepdf` — promote the existing `img2pdf` transitive dependency to an explicit dev dependency
### Pre-existing test breakage this phase causes (found by pattern mapping, verified 2026-09-11)

None of these are flagged by RESEARCH.md. Each is an existing green test that **this phase turns
red**, so each needs a Wave 0 fix before the implementing task lands:

- [ ] `tests/conftest.py:133-140` — the `mock_paperless` fixture sets
      `poll_task.return_value = {"status": "SUCCESS"}`, encoding the *old* return-a-dict contract.
      It cannot express the FAILURE or TIMEOUT e2e cases once `poll_task` raises instead.
- [ ] `tests/test_cli.py::TestJobsCommand` (all 7 methods, e.g. `:402`, `:421`) — each builds
      `db_path = str(tmp_path / "saneless.db")` while setting `tmp_dir=str(tmp_path)`. Once
      `cli.py:227` reads `settings.output.db_path` (derived from `data_dir`, D-16), the CLI opens a
      *different* database than the test populated, and every case silently sees zero jobs.
- [ ] `tests/test_pdf.py::TestAssemblePdf` (7 call sites, `:18`–`:81`) — all call
      `assemble_pdf([img], tmp_path)` positionally and break the moment D-09 adds a `filename`
      argument. Decide deliberately whether `filename` is required (update all 7) or defaulted
      (keeps them green but weakens D-09's "unique from birth" guarantee at the type level).

### Remaining Wave 0 items

- [ ] `tests/conftest.py` — add a `wait_for_state(store, job_id, state, timeout)` polling helper. The existing worker tests use a flat `time.sleep(0.5)` 20+ times; ROBU-03 formalises this in Phase 26, but OUTC-10 needs it now and a shared helper avoids adding a 21st sleep.

No other new conftest fixtures are required — `default_settings`, `mock_scanner`, `sample_pil_images`
and `multi_page_images` already exist.

---

## Keeping OUTC-10 fast — the decided approach

**Use the seams that already exist. Add none.** Measured in this repository:

| Lever | Cost today | Cost with the lever |
|-------|-----------|---------------------|
| Upload retries (consume-dir fallback case) | 3.00 s at `max_retries=3` | **0.00 s** at `max_retries=1` (measured 3.00 / 1.00 / 0.00 for 3 / 2 / 1) |
| Poll timeout case | 0.5 s minimum (the unconditional first sleep) | exactly `paperless_task_timeout` once `time.sleep(min(delay, remaining))` clamps — set it to `0.05` |
| SUCCESS / FAILURE / duplex cases | 0 s (terminal status on the first poll, before any sleep) | unchanged |

All five parametrised cases together sleep well under one second. This is faster than monkeypatching
`time.sleep` and needs no patch — and it leaves the real sleep in place so the deadline clamp is
itself exercised.

Rejected alternatives, with reasons, so they are not re-proposed during execution:

- **Monkeypatch `time.sleep` in `saneless.paperless`** — disables the deadline clamp the test exists to prove. Phase 32 (M-34) owns that sweep anyway.
- **Inject a `sleep_fn` parameter** — a new production seam existing only for tests, for a problem two existing constructor parameters already solve.
- **Make the backoff base configurable** — same objection, plus a new config key with no user story.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Which API version the operator's own paperless-ngx serves | OUTC-11 | Requires the user's live instance; cannot be stubbed meaningfully | `curl -sI "$PAPERLESS_URL/api/" -H "Authorization: Token $TOKEN" \| grep -i x-api-version` |

This is informational only — it does **not** gate the phase. The OUTC-11 fix (pin the version header
**and** tolerate both response shapes) is correct regardless of the answer, and both shapes are
covered by automated parametrised tests. Everything else in this phase has automated verification.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all ❌ references above — two of the three pre-existing breakages are fixed in the same commit as their cause (23-02 Task 2, 23-03 Task 2) rather than in a separate wave; `conftest.mock_paperless` is neutralised pre-emptively in 23-01 Task 1
- [x] No watch-mode flags
- [x] Feedback latency < 60 s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-11 by gsd-plan-checker (verdict PASS, no blocking concerns)

`wave_0_complete` stays `false` until the Wave 0 items are actually executed — it tracks execution, not planning.

### Discretion item resolved by omission

`23-CONTEXT.md` left open "whether the default `paperless_task_timeout` of 300 s is still right now that a timeout is fatal". **No plan changes it — it stays 300 s**, per RESEARCH Open Question 3: changing the timeout in the same phase that makes timeouts fatal conflates two variables and makes a regression impossible to attribute. Recorded here so the omission is a decision rather than an oversight.
