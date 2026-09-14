---
phase: 25
slug: manual-duplex
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-09-13
---

# Phase 25 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `25-RESEARCH.md` § "Validation Architecture" (line 1174). Where this file and
> RESEARCH.md disagree, RESEARCH.md is the measured source — fix this file.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-timeout 2.4.0 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_pipeline.py tests/test_worker.py tests/test_config.py -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~34 s (measured 34.18 s, 2026-09-13; 966 tests collected, independently confirmed) |
| **Static gates** | `uv run ruff check .` · `uv run ruff format --check .` · `uv run ty check` · `uv run pyrefly check src tests` |

**Strictness already live:** `filterwarnings = ["error"]`, `xfail_strict`, `--strict-markers`,
`--strict-config`, `timeout = 60` with `timeout_method = "signal"`.

**No new pytest marker is needed.** `--strict-markers` is live — if the planner adds one it must be
registered in `pyproject.toml` in the same commit or collection fails.

**`pyrefly` is always invoked with paths named** (`src tests`), never bare — Phase 23.1 D-10.

---

## Sampling Rate

- **After every task commit:** the module(s) that task touched (e.g. `uv run pytest tests/test_pipeline.py -q`) — a few seconds against a ~34 s full suite
- **After every plan wave:** `uv run pytest -q` plus `uv run ruff check .` and `uv run ty check`
- **Before `/gsd-verify-work`:** full suite green **plus all four** static gates
- **Max feedback latency:** ~35 s

> ⚠ **No git hook runs pytest.** The commit-stage hooks run ruff, ty and pyrefly only. "Every commit
> leaves the suite green" is therefore a discipline the plan must encode in task verification steps —
> nothing will catch a violation automatically.
>
> Conversely, adding an enum member *without* its `match` arms **is** caught at commit stage by the
> type checkers. `JobState.SCANNING_REVERSE` and both label arms must land in **one** commit.

---

## Per-Task Verification Map

*Not yet fillable — task IDs do not exist until plans are written. The planner MUST populate this
table, one row per task, drawing the Automated Command column from the requirement map below.*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| {25-NN-NN} | {NN} | {N} | DPLX-{NN} | T-25-{NN} / — | {expected secure behavior or "N/A"} | {unit/e2e/static} | `{command}` | ✅ / ❌ W0 | ⬜ pending |

### Requirement → observable map (from RESEARCH.md, use to fill the table above)

| Req ID | Observable | Test Type | Automated Command |
|--------|-----------|-----------|-------------------|
| DPLX-01 | `ProfileConfig().duplex == "none"`; three literals accepted, a fourth rejected | unit | `uv run pytest tests/test_config.py -q` |
| DPLX-01 | `source` reaches `ScanSettings` verbatim for a non-duplex profile | unit | `uv run pytest tests/test_pipeline.py -q` |
| DPLX-01 | **Grep gate:** no strategy read of `source` survives in `src/` | static | `grep -rn '"manual" in\|"duplex" in\|_is_manual_duplex' src/` |
| DPLX-02 | Legacy `source` translates via TOML, env var **and** direct construction | unit, parametrised | `uv run pytest tests/test_config.py -q` |
| DPLX-02 | An explicit `duplex` is **not** overwritten by the translation | unit | `uv run pytest tests/test_config.py -q` |
| DPLX-02 | One WARNING naming the profile **and** the replacement | unit + `caplog` | `uv run pytest tests/test_config.py -q` |
| DPLX-02 | A legacy config still scans end to end | e2e | `uv run pytest tests/test_outcomes_e2e.py -q` |
| DPLX-03 | `worker.py` no longer inspects `profile.source` | static + unit | grep + `uv run pytest tests/test_worker.py -q` |
| DPLX-03 | Dispatch is a total `match`; a third variant fails the type gate | static | `uv run ty check` · `uv run pyrefly check src tests` |
| DPLX-04 | No coordinator ⇒ `ConfigError` **and SANE never touched** (`get_devices.assert_not_called()`) | unit | `uv run pytest tests/test_pipeline.py -q` |
| DPLX-04 | CLI prompt appears **between** `Scanning...` and `Scanning reverse sides...` | unit (`CliRunner(input="y\n")`) | `uv run pytest tests/test_cli.py -q` |
| DPLX-04 | Non-TTY manual duplex exits **2** naming an interactive terminal | unit | `uv run pytest tests/test_cli.py -q` |
| DPLX-04 | CLI `n` ⇒ `ABORTED` ⇒ job fails, fronts not uploaded | unit | `uv run pytest tests/test_cli.py -q` |
| DPLX-04 | Web Continue ⇒ `CONTINUED`; the **second** signal is dropped (D-16) | unit | `uv run pytest tests/test_worker.py -q` |
| DPLX-04 | Two passes through the real `SaneBackend` against `"Automatic Document Feeder"` | integration (fake SANE) | `uv run pytest tests/test_pipeline.py -q` |
| DPLX-05 | `flip_timeout_seconds = 0` ⇒ `ERROR` naming the flip wait | e2e | `uv run pytest tests/test_outcomes_e2e.py -q` |
| DPLX-05 | A **second** job reaches terminal after a timeout (worker unparked) | e2e | `uv run pytest tests/test_outcomes_e2e.py -q` |
| DPLX-06 | Pass B persists `SCANNING_REVERSE` (blocking-stub scanner) | unit | `uv run pytest tests/test_worker.py -q` |
| DPLX-06 | `aria-busy` and **no** `flip-prompt` during `SCANNING_REVERSE` | unit, parametrised | `uv run pytest tests/test_web_state_rendering.py -q` |
| DPLX-06 | Abort at the prompt ⇒ `ERROR` naming the flip prompt | unit | `uv run pytest tests/test_worker.py -q` |
| DPLX-06 | **Grep gate:** `wait_transition` / `_transition_event` appear nowhere | static | `grep -rn "wait_transition\|_transition_event" src/ tests/` |
| DPLX-06 | Nine `JobState` members, each with a label ≠ its raw value | unit, parametrised | `uv run pytest tests/test_vocabulary.py -q` |
| DPLX-07 | Round trip yields `default` for flatbed-only, feeder-only **and** mixed | unit, parametrised | `uv run pytest tests/test_auto_profiles.py -q` |
| DPLX-07 | `duplex = "hardware"` emitted only for `FEEDER_DUPLEX`, omitted otherwise | unit | `uv run pytest tests/test_auto_profiles.py -q` |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Six enablers. The first is **production code that is also a test enabler** — without it DPLX-04's
prompt test cannot be written at all.

- [ ] `src/saneless/cli.py` — `_stdin_is_interactive()` seam. `CliRunner` reports
      `isatty() is False` (measured), so without this seam D-11's two halves are mutually
      untestable: the non-TTY refusal fires inside the very test meant to reach `click.confirm`.
- [ ] `src/saneless/config.py` — a module logger (`logging.getLogger(__name__)`). None exists
      today, and D-04's `caplog` assertion needs `logger="saneless.config"`.
- [ ] `tests/conftest.py` — a concrete always-continue `FlipCoordinator` stub (~10 migrated
      pipeline tests depend on it; must land in the same commit as the refusal guard).
- [ ] `tests/test_worker.py` — a scanner stub whose **second** `scan_pages` blocks on a test-held
      `Event`, so `SCANNING_REVERSE` is observable rather than instantaneous.
- [ ] `tests/test_outcomes_e2e.py` — `_Case` gains a `flip_timeout: int` field and a flip-timeout
      case; `_build_settings` passes it through to `OutputConfig`.
- [ ] `tests/test_pipeline.py:815` — retarget `report_sources` from `["Flatbed", "ADF Manual
      Duplex"]` to a realistic feeder list. Without this the integration test still asserts the
      fiction C-01 exists to remove.

*No new test files and no framework install are needed — every target module already exists.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|

**All phase behaviors have automated verification.** The three observation problems that looked
manual are each solved by a measured technique:

- **A real ten-minute flip wait would blow the 60 s SIGALRM.** Never wait:
  `flip_timeout_seconds` is an `int`, `0` is valid, and `Event.wait(0)` measures **4 µs**. The
  identical technique is already documented at `tests/test_outcomes_e2e.py:104-118` for
  `paperless_task_timeout`, including why `0` and not `0.05`.
- **`CliRunner` is not a TTY.** The refusal test asserts the *unpatched* behaviour — honest,
  because `CliRunner` genuinely is not a terminal — while the prompt test patches the Wave-0 seam.
  Neither test lies.
- **Two-pass duplex against real SANE code.** Already solved at `tests/test_pipeline.py:806-856`,
  which drives the real `SaneBackend` over `FakeSaneDev` and asserts
  `dev.assignments.count("source") == 2` and `dev.calls.count("snap") == 6`.

Per CLAUDE.md, browser-based checks are never "manual-only" — the 30 browser tests are deselected
in CI by `-m "not browser"`, not exempted from automation.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 35s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
