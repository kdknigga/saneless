---
phase: 21
slug: vocabulary-and-contracts
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-09-10
backfilled: 2026-09-10
---

# Phase 21 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `21-RESEARCH.md` § Validation Architecture. Every command below was
> executed against this repository during research — none is aspirational.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-timeout 2.4.0 (already installed — no Wave 0 framework install) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — `--strict-config`, `--strict-markers`, `xfail_strict`, `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"` |
| **Quick run command** | `uv run pytest -q -m "not browser" tests/test_<module>.py` |
| **Full suite command** | `uv run pytest -m "not browser"` |
| **Browser suite** | `uv run pytest -m browser` (8 tests, deselected by default) |
| **Estimated runtime** | ~27 seconds full suite |
| **Measured baseline (2026-09-10)** | **`332 passed, 8 deselected in 26.83s`** on a clean tree |

**Phase-specific note:** pytest alone cannot detect this phase's primary defect class. A
missing enum arm is invisible to the test suite and visible only to `ty` / `pyrefly`. The
type-check trio is therefore part of the per-task sampling command, not just the phase gate.

---

## Sampling Rate

- **After every task commit:**
  `uv run ruff check . && uv run ty check && uv run pyrefly check src tests && uv run pytest -q -m "not browser" <touched test module>`
- **After every plan wave:** `uv run pytest -m "not browser"` — must be **≥ 332 passed, 0 failed**
- **After any template edit:** additionally `uv run pytest -m browser` (templates are the one surface no type checker guards)
- **Before `/gsd-verify-work`:** all five CONTRIBUTING checks green, plus both grep gates
- **Max feedback latency:** ~30 seconds (full suite) / ~5 seconds (single module + type checks)

---

## Per-Task Verification Map

> Back-filled 2026-09-10 against the five plans committed in `0af19ca`. Task IDs follow
> `{phase}-{plan}-{task}`. There is no separate Wave 0 in this phase: every test listed as a
> gap below is written inside the same task that implements the behaviour it covers
> (task-level TDD — write, observe red, implement, commit once), so the wave column is the
> owning plan's wave, not a notional Wave 0.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 21-01-01 | 21-01 | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_state_is_classified_exactly_once` | new in task | ⬜ pending |
| 21-01-01 | 21-01 | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_busy_states_is_active_minus_awaiting_flip` | new in task | ⬜ pending |
| 21-01-02 | 21-01 | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_state_has_a_label` | new in task | ⬜ pending |
| 21-01-02 | 21-01 | 1 | CTR-01 | T-21-01 | Unknown state rejected, not silently passed through | unit | `uv run pytest -q tests/test_vocabulary.py::test_state_label_rejects_unknown_values` | new in task | ⬜ pending |
| 21-01-02 | 21-01 | 1 | CTR-05 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_category_has_a_message` | new in task | ⬜ pending |
| 21-01-03 | 21-01 | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_job.py` (re-export check — imports at `:14`) | ✅ | ⬜ pending |
| 21-01-03 | 21-01 | 1 | CTR-01 | — | N/A | unit | `uv run python -c "from saneless.job import Job, JobState; ..."` (`is_active` / `is_busy`) | ✅ | ⬜ pending |
| 21-02-01 | 21-02 | 1 | CTR-04 | T-21-03 | Closed enum returned; `UNKNOWN` is the safe default | unit | `uv run pytest -q tests/test_scanner.py::TestClassifySource` | new in task | ⬜ pending |
| 21-02-01 | 21-02 | 1 | CTR-04 | — | Exact-match `"auto"`, not substring | grep gate | `command grep -nE '"auto" in lower' src/saneless/scanner/base.py` returns nothing | new in task | ⬜ pending |
| 21-02-02 | 21-02 | 1 | CTR-04 | — | N/A | unit | `uv run pytest -q tests/test_scanner.py -k automatic_document_feeder` — **could not have passed before this phase (D-11)** | new in task | ⬜ pending |
| 21-02-02 | 21-02 | 1 | CTR-04 | — | N/A | unit | `uv run pytest -q tests/test_auto_profiles.py::TestSourceToSlug` — must stay green **unedited** (N-09 guard) | ✅ | ⬜ pending |
| 21-03-01 | 21-03 | 2 | CTR-01 | T-21-02 | Labels are developer constants — never `\| safe` | integration | `uv run pytest -q tests/test_web.py -k "label or history or scan_button"` | ✅ edit `:36`, `:378` | ⬜ pending |
| 21-03-02 | 21-03 | 2 | CTR-01 | — | N/A | e2e | `uv run pytest -q -m browser` (measured 8 passed in 1.64s) | ✅ | ⬜ pending |
| 21-04-01 | 21-04 | 2 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestPipelineEventEnum` (`list(PipelineEvent)` × `job_state` totality) | ✅ extend | ⬜ pending |
| 21-04-02 | 21-04 | 2 | CTR-01 | — | Job never marked DONE from inside the pipeline's tempdir | integration | `uv run pytest -q tests/test_worker.py -k "transition or EnumDispatch or flip"` | ✅ | ⬜ pending |
| 21-04-02 | 21-04 | 2 | CTR-05 | — | N/A | unit | `uv run pytest -q tests/test_worker.py -k ErrorCategory` | ✅ | ⬜ pending |
| 21-04-03 | 21-04 | 2 | CTR-01 | — | N/A | integration | `uv run pytest -q tests/test_cli.py::TestScanCommand::test_scan_status_output` (prose byte-identical) | ✅ | ⬜ pending |
| 21-05-01 | 21-05 | 3 | CTR-03 | — | N/A | unit | `uv run pytest -q tests/test_paperless.py -k "fallback or consume_dir"` — **atomic across all five stub sites** | ✅ edit `:231`, `:425`, `:452` | ⬜ pending |
| 21-05-02 | 21-05 | 3 | CTR-02 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestRunPipeline` | ✅ edit `:51` | ⬜ pending |
| 21-05-02 | 21-05 | 3 | CTR-02 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestManualDuplex` | ✅ edit `:496-497`, `:531` | ⬜ pending |
| 21-05-03 | 21-05 | 3 | CTR-03 | — | N/A | grep gate | `! command grep -rn '"fallback"' src/ tests/` | plan step, not suite | ⬜ pending |
| 21-05-03 | 21-05 | 3 | CTR-03 | — | N/A | grep gate | `! command grep -rn 'FALLBACK' docs/ README.md` | plan step, not suite | ⬜ pending |
| all | all | all | all | — | N/A | type gate | `uv run ty check && uv run pyrefly check src tests` — **the phase's actual enforcement mechanism** | ✅ Ph20 CI | ⬜ pending |
| all | all | all | all | — | N/A | lint gate | `uv run ruff check . && uv run ruff format --check .` (watch `PLR0911`) | ✅ Ph20 CI | ⬜ pending |
| all | all | all | all | — | N/A | full suite | `uv run pytest -m "not browser"` — **≥ 332 passed** | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

**Resolved — there is no separate Wave 0 in this phase.** Every gap below is written inside
the task that implements the behaviour it covers, under task-level TDD (`tdd="true"` +
`<behavior>` + `<green_at_every_commit>`): the executor writes the test, runs it, observes the
failure, implements, then commits both together. A standalone Wave 0 that landed failing tests
in their own commit would violate the phase's green-at-every-commit constraint.

- [x] `tests/test_vocabulary.py` — D-09 parametrised completeness tests (`JobState` × label, classification XOR, `ErrorCategory` × message, D-10's raise test, `BUSY_STATES` derivation) → **owned by 21-01 Tasks 1 and 2**
- [x] `tests/test_scanner.py::TestClassifySource` — parametrised class over the harvested real-world source-string table, including the three flagged ambiguities → **owned by 21-02 Task 1**
- [x] `tests/test_scanner.py` — "Automatic Document Feeder yields N pages, not 1"; needs a `_FakeSaneDevice` whose `raw_options` source constraint carries the long name (model on `:115` and `:503-540`) → **owned by 21-02 Task 2**
- [x] `tests/test_pipeline.py::TestPipelineEventEnum` — extended with the `list(PipelineEvent)` × `job_state` totality test → **owned by 21-04 Task 1**
- [x] Two grep gates as **plan verification steps, not pytest tests** → **owned by 21-05 Task 3**
- [x] Framework install: **none needed** — pytest 9.0.2, pytest-timeout 2.4.0, and the `browser` marker are all already configured. Chromium verified present during planning: `8 passed, 332 deselected in 1.64s`

> `command grep` is mandatory in every gate above: this shell's `grep` is a ugrep wrapper
> whose `-qv` exit status is wrong and which misclassified commits during Phase 20 (see
> `20-CONTEXT.md` D-02 verification note).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Rendered status area and Scan button are visually unchanged across all five active states | CTR-01 | Residual only. The browser suite (verified working: 8 passed in 1.64s) and 21-03 Task 2's template assertions cover the rendered output; what no assertion pins is the `AWAITING_FLIP` / `BUSY_STATES` split (D-04) driving `aria-busy` and button text together in a real browser | Start `saneless serve`, drive a manual-duplex scan, confirm the button reads "Waiting for flip…" *without* `aria-busy` at the flip prompt and "Scanning…" *with* `aria-busy` in every other active state |

*Everything else in this phase has automated verification. Per CLAUDE.md, browser-checkable
behaviour is never deferred to manual-only — the row above is the narrow residual that the
automated browser suite genuinely does not assert.*

---

## Open Interpretation — resolved, flagged for the verifier

Research raised assumption **A7** (medium risk): does success criterion 1's "the worker, web
templates, and CLI all read them from the same module" require `saneless jobs`
(`cli.py:262`) to print `state_label()` instead of today's raw `DONE` / `ERROR`?

**Resolution for this phase: keep the raw values.** The CLI *does* read from the shared
module — its progress prose comes from `vocabulary.py` — which satisfies criterion 1.
Switching the `jobs` table to friendly labels would change user-visible CLI output, and the
phase goal's "no behaviour change" is unconditional with exactly one deliberate exception
(D-11). `cli.py:238`'s `--json` output is a machine contract and stays raw regardless.
Human-friendly CLI output belongs with Phase 30's appliance layer.

This is a one-line change if the verifier reads criterion 1 the other way. The plan must state
the choice explicitly so it reads as a decision rather than an omission.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify — confirmed against all 13 tasks in `0af19ca`
- [x] Sampling continuity: no 3 consecutive tasks without automated verify — every task in every plan carries one
- [x] Wave 0 covers all MISSING references — resolved by folding each into its implementing task (see above)
- [x] No watch-mode flags — all commands are single-shot `uv run`
- [x] Feedback latency < 30s — full suite measured 26.83s; single module + type checks ~5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-10 (back-filled after planning; plan-checker verified the
requirement→command map matches all five plans' `<automated>` commands exactly)
