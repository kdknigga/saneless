---
phase: 21
slug: vocabulary-and-contracts
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-09-10
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
  `uv run ruff check . && uv run ty check && uv run pyrefly check && uv run pytest -q -m "not browser" <touched test module>`
- **After every plan wave:** `uv run pytest -m "not browser"` — must be **≥ 332 passed, 0 failed**
- **After any template edit:** additionally `uv run pytest -m browser` (templates are the one surface no type checker guards)
- **Before `/gsd-verify-work`:** all five CONTRIBUTING checks green, plus both grep gates
- **Max feedback latency:** ~30 seconds (full suite) / ~5 seconds (single module + type checks)

---

## Per-Task Verification Map

> Task IDs are assigned by the planner. The requirement → command mapping below is fixed;
> the planner fills the Task ID and Plan columns when plans are written.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | 0 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_state_has_a_label` | ❌ W0 | ⬜ pending |
| TBD | TBD | 0 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_state_is_classified_exactly_once` | ❌ W0 | ⬜ pending |
| TBD | TBD | 0 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_busy_states_is_active_minus_awaiting_flip` | ❌ W0 | ⬜ pending |
| TBD | TBD | 0 | CTR-01 | T-21-01 | Unknown state rejected, not silently passed through | unit | `uv run pytest -q tests/test_vocabulary.py::test_state_label_rejects_unknown_values` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_job.py` (re-export check — imports at `:14`) | ✅ | ⬜ pending |
| TBD | TBD | 1 | CTR-01 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestPipelineEventEnum` (extend: `list(PipelineEvent)` × `job_state` totality) | ✅ extend | ⬜ pending |
| TBD | TBD | 1 | CTR-01 | — | N/A | integration | `uv run pytest -q tests/test_worker.py -k "transition or EnumDispatch or flip"` | ✅ | ⬜ pending |
| TBD | TBD | 2 | CTR-01 | T-21-02 | Labels are developer constants — never `\| safe` | integration | `uv run pytest -q tests/test_web.py -k "humanize or label or history or scan_button"` | ✅ edit `:378` | ⬜ pending |
| TBD | TBD | 2 | CTR-01 | — | N/A | e2e | `uv run pytest -m browser` | ✅ | ⬜ pending |
| TBD | TBD | 2 | CTR-01 | — | N/A | integration | `uv run pytest -q tests/test_cli.py::TestScanCommand::test_scan_status_output` (prose byte-identical) | ✅ | ⬜ pending |
| TBD | TBD | 1 | CTR-02 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestRunPipeline` | ✅ edit `:51` | ⬜ pending |
| TBD | TBD | 1 | CTR-02 | — | N/A | unit | `uv run pytest -q tests/test_pipeline.py::TestManualDuplex` | ✅ edit `:496-497`, `:531` | ⬜ pending |
| TBD | TBD | 1 | CTR-03 | — | N/A | unit | `uv run pytest -q tests/test_paperless.py -k "fallback or consume_dir"` | ✅ edit `:231`, `:425`, `:452` | ⬜ pending |
| TBD | TBD | 3 | CTR-03 | — | N/A | grep gate | `! command grep -rn '"fallback"' src/ tests/` | ❌ W0 (plan step, not suite) | ⬜ pending |
| TBD | TBD | 3 | CTR-03 | — | N/A | grep gate | `! command grep -rn 'FALLBACK' docs/ README.md` | ❌ W0 (plan step, not suite) | ⬜ pending |
| TBD | TBD | 0 | CTR-04 | T-21-03 | Closed enum returned; `UNKNOWN` is the safe default | unit | `uv run pytest -q tests/test_scanner.py::TestClassifySource` | ❌ W0 | ⬜ pending |
| TBD | TBD | 0 | CTR-04 | — | N/A | unit | `uv run pytest -q tests/test_scanner.py -k automatic_document_feeder` — **could not have passed before this phase (D-11)** | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | CTR-04 | — | N/A | unit | `uv run pytest -q tests/test_auto_profiles.py::TestSourceToSlug` — must stay green **unedited** (N-09 guard) | ✅ | ⬜ pending |
| TBD | TBD | 0 | CTR-05 | — | N/A | unit | `uv run pytest -q tests/test_vocabulary.py::test_every_category_has_a_message` | ❌ W0 | ⬜ pending |
| TBD | TBD | 1 | CTR-05 | — | N/A | unit | `uv run pytest -q tests/test_worker.py -k ErrorCategory` | ✅ | ⬜ pending |
| TBD | TBD | all | all | — | N/A | type gate | `uv run ty check && uv run pyrefly check` — **the phase's actual enforcement mechanism** | ✅ Ph20 CI | ⬜ pending |
| TBD | TBD | all | all | — | N/A | lint gate | `uv run ruff check . && uv run ruff format --check .` (watch `PLR0911`) | ✅ Ph20 CI | ⬜ pending |
| TBD | TBD | all | all | — | N/A | full suite | `uv run pytest -m "not browser"` — **≥ 332 passed** | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_vocabulary.py` — new module for the D-09 parametrised completeness tests: `JobState` × label, `JobState` × classification XOR, `ErrorCategory` × message, D-10's raise test, and the `BUSY_STATES` derivation check
- [ ] `tests/test_scanner.py::TestClassifySource` — new parametrised class over the harvested real-world source-string table, including the three flagged ambiguities
- [ ] `tests/test_scanner.py` — new "Automatic Document Feeder yields N pages, not 1" test; needs a `_FakeSaneDevice` whose `raw_options` source constraint carries the long name (model on `:115` and `:503-540`)
- [ ] `tests/test_pipeline.py::TestPipelineEventEnum` — extend with the `list(PipelineEvent)` × `job_state` totality test
- [ ] Two grep gates as **plan verification steps, not pytest tests**: `! command grep -rn '"fallback"' src/ tests/` and `! command grep -rn 'FALLBACK' docs/ README.md`
- [ ] Framework install: **none needed** — pytest 9.0.2, pytest-timeout 2.4.0, and the `browser` marker are all already configured

> `command grep` is mandatory in every gate above: this shell's `grep` is a ugrep wrapper
> whose `-qv` exit status is wrong and which misclassified commits during Phase 20 (see
> `20-CONTEXT.md` D-02 verification note).

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Rendered status area and Scan button are visually unchanged across all five active states | CTR-01 | The browser suite covers the happy path, but the `AWAITING_FLIP` / `BUSY_STATES` split (D-04) drives `aria-busy` and button text, which no assertion pins today | Start `saneless serve`, drive a manual-duplex scan, confirm the button reads "Waiting for flip…" without `aria-busy` at the flip prompt and "Scanning…" with `aria-busy` in every other active state |

*Everything else in this phase has automated verification.*

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

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
