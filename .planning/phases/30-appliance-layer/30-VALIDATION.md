---
phase: 30
slug: appliance-layer
status: approved-for-execution
nyquist_compliant: true
wave_0_complete: false
created: 2026-09-16
---

# Phase 30 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `30-RESEARCH.md` § Validation Architecture (lines 1441-1539).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x with `pytest-timeout`, `pytest-playwright` |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — `testpaths=["tests"]`, `--strict-markers --strict-config`, `filterwarnings=["error"]`, `timeout=60`, `timeout_method="signal"` |
| **Quick run command** | `uv run pytest tests/test_web.py tests/test_cli.py -x -q` |
| **Full suite command** | `uv run pytest` |
| **Measured runtime** | quick **5.6s / 188 tests**; full **77s / 2123 tests** (measured 2026-09-16, all green) |

**Type/lint gates (every one must be clean, zero suppressions):**
`uv run ruff check .` · `uv run ruff format --check .` · `uv run ty check` ·
`uv run pyrefly check src tests` · `uv run prek run --all-files`

**Two config facts that change how this phase is tested:**
- `filterwarnings = ["error"]` — any `DeprecationWarning` from new code is a hard failure.
- `timeout_method = "signal"` — a hung background thread fails one test, not the run, but a
  thread left running **leaks into later tests**. Every new thread needs a fixture that
  stops it.

---

## Sampling Rate

- **After every task commit:** `uv run ruff check . && uv run ty check && uv run pytest <modules touched> -x -q`
- **After every plan wave:** `uv run pytest` (full) + `uv run pyrefly check src tests`
- **Before `/gsd-verify-work`:** full suite green, browser module green **offline**,
  `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files` green
- **Max feedback latency:** ~6s per-task (quick run), ~80s per-wave (full suite)

---

## Per-Task Verification Map

> Task IDs are assigned by `gsd-planner`. The planner MUST map every task to a row below
> and carry the requirement, test type and automated command forward into each PLAN.md
> `<automated>` verify block. Rows marked ❌ W0 depend on a Wave 0 test file.

| Requirement | Behaviour to sample | Test Type | Automated Command | File Exists | Status |
|---|---|---|---|---|---|
| APPL-01 | `doctor` exits 0 when all checks ok/warn, 2 when any fails | unit (CliRunner) | `uv run pytest tests/test_doctor.py -x` | ❌ W0 | ⬜ pending |
| APPL-01 | A placeholder token makes `doctor` exit non-zero | unit | `uv run pytest tests/test_doctor.py -x` | ❌ W0 | ⬜ pending |
| APPL-01 | `doctor` reports all five checks with python-sane unavailable (A-1) | unit | `uv run pytest tests/test_doctor.py -x` | ❌ W0 | ⬜ pending |
| APPL-01/02 | The registry is the *same object* both surfaces consume | unit | `uv run pytest tests/test_checks.py -x` | ❌ W0 | ⬜ pending |
| APPL-02 | `GET /` never calls `run_checks` (spy asserts zero calls) | unit | `uv run pytest tests/test_web_checks.py -x` | ❌ W0 | ⬜ pending |
| APPL-02 | The Paperless probe carries a bounded `httpx.Timeout`, not the 30s client default | unit | `uv run pytest tests/test_paperless.py -k timeout -x` | ❌ W0 | ⬜ pending |
| APPL-02 | Scanner check skipped and strip says "Paused during scan" while a job is active | unit | `uv run pytest tests/test_web_checks.py -k paused -x` | ❌ W0 | ⬜ pending |
| APPL-02 | Refresh during a scan re-runs only non-scanner checks (D-09) | unit | `uv run pytest tests/test_web_checks.py -x` | ❌ W0 | ⬜ pending |
| APPL-02 | TTL expiry driven by an **injected clock** — no `sleep` | unit | `uv run pytest tests/test_checks_cache.py -x` | ❌ W0 | ⬜ pending |
| APPL-02 | Refresher stops within the bounded join; lifespan closes nothing until both threads stop (A-7) | unit | `uv run pytest tests/test_app_lifespan.py -x` | ⚠️ extend `:302-360` | ⬜ pending |
| APPL-02 | Cold start renders `Checking…`; the swapped-in partial carries no poll trigger | unit + browser | `uv run pytest tests/test_web_checks.py tests/test_browser.py -k checking` | ❌ W0 | ⬜ pending |
| APPL-03 | Counts sentence for DONE and FALLBACK; **nothing** for a NULL | unit | `uv run pytest tests/test_web_state_rendering.py -k pages -x` | ❌ W0 | ⬜ pending |
| APPL-03 | `Front: N pages · Scanning backs…` during `SCANNING_REVERSE` (A-4) | unit | `uv run pytest tests/test_worker.py -k front_count -x` | ❌ W0 | ⬜ pending |
| APPL-04 | Every `ErrorCategory` member has a message **and** a next step (`assert_never`) | unit | `uv run pytest tests/test_vocabulary.py -k advice -x` | ⚠️ retarget existing | ⬜ pending |
| APPL-04 | `<details>` holds error text + category + job id and **not** the log path | unit | `uv run pytest tests/test_web_state_rendering.py -k details -x` | ❌ W0 | ⬜ pending |
| APPL-04 | CLI prints Phase 28's first line **unchanged** plus a second advice line (D-12) | unit | `uv run pytest tests/test_cli.py -k advice -x` | ❌ W0 | ⬜ pending |
| APPL-05 | Generated profiles carry the three label/description forms | unit | `uv run pytest tests/test_auto_profiles.py -k label -x` | ❌ W0 | ⬜ pending |
| APPL-05 | `--force` rewrites `label`/`description` in place, keeps `default_tags` + comments | unit | `uv run pytest tests/test_auto_profiles.py -k label -x` | ❌ W0 | ⬜ pending |
| APPL-05 | Feeder profiles sort first when `has_flatbed` is false | unit | `uv run pytest tests/test_web.py -k ordering -x` | ❌ W0 | ⬜ pending |
| APPL-05 | Blank label falls back to the profile name (A-3) | unit | `uv run pytest tests/test_web.py -k ordering -x` | ❌ W0 | ⬜ pending |
| APPL-06 | Unwritable config yields the amber warn row with the exact copy | unit | `uv run pytest tests/test_checks.py -k readonly -x` | ❌ W0 | ⬜ pending |
| APPL-07 | Placeholder set detected: `""`, whitespace, `changeme`, `your-api-token-here`, … | unit | `uv run pytest tests/test_config.py -k placeholder -x` | ❌ W0 | ⬜ pending |
| APPL-07 | `POST /api/scan` refuses with the **new** rejection member and writes a REJECTED row | unit | `uv run pytest tests/test_web_errors.py -k placeholder -x` | ❌ W0 | ⬜ pending |
| APPL-07 | `saneless scan` exits 2 **before** the scanner is opened (stub asserts zero opens) | unit | `uv run pytest tests/test_cli.py -k placeholder -x` | ❌ W0 | ⬜ pending |
| APPL-08 | Queued job renders `Waiting for 'X' to finish (N ahead of you)` from `list_pending()` | unit | `uv run pytest tests/test_web_state_rendering.py -k queue -x` | ❌ W0 | ⬜ pending |
| APPL-09 | `POST /api/scan` sets an HttpOnly, SameSite=Lax, session (no Max-Age) cookie | unit | `uv run pytest tests/test_web.py -k owner_cookie -x` | ❌ W0 | ⬜ pending |
| APPL-09 | Two `TestClient`s: owner gets the buttons, non-owner the waiting copy | unit | `uv run pytest tests/test_web.py -k owner_cookie -x` | ❌ W0 | ⬜ pending |
| APPL-09 | **Two browser contexts**, real cookie jars, owner vs non-owner rendering | browser | `uv run pytest tests/test_browser.py -k two_contexts` | ❌ W0 | ⬜ pending |
| APPL-09 | Abort renders `hx-confirm` and the native dialog fires | browser | `uv run pytest tests/test_browser.py -k confirm` | ❌ W0 | ⬜ pending |
| APPL-10 | Every control has help text; tag checkboxes ≥ 44×44 CSS px | browser | `uv run pytest tests/test_browser.py -k touch_target` | ❌ W0 | ⬜ pending |
| APPL-10 | `show_tags=false` hides the control **and** `default_tags` still apply (D-29) | unit | `uv run pytest tests/test_web.py -k simple_form -x` | ❌ W0 | ⬜ pending |
| APPL-10 | Filtering preserves already-checked tags (A-5) | unit + browser | `uv run pytest -k tag_filter` | ❌ W0 | ⬜ pending |
| APPL-11 | Compose has the consume mount; strip says "Fallback: not configured…" when absent | unit | `uv run pytest tests/test_deployment_config.py -x` | ⚠️ extend | ⬜ pending |
| APPL-12 | `local_time` renders `%Z`; web and CLI use the **same** function | unit | `uv run pytest tests/test_vocabulary.py -k local_time -x` | ❌ W0 | ⬜ pending |
| APPL-12 | `saneless jobs` table still fits at 80 columns with the zone suffix | unit | `uv run pytest tests/test_cli.py -k table_width -x` | ❌ W0 | ⬜ pending |
| APPL-12 | `jobs --json` stays UTC ISO (machine contract unchanged) | unit | `uv run pytest tests/test_cli.py -k table_width -x` | ⚠️ may exist | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## What each success criterion actually requires sampling

Decompose each criterion into what is **genuinely uncertain** versus what is a library's
own contract, and test only the uncertain part.

1. **Criterion 1 — doctor and the strip agree.** The measurable fact is not "both look the
   same" but "both call the same registry". Assert it structurally:
   `set(CheckKey) == {r.key for r in run_checks(...)}`, plus a template test that the strip
   renders one row per `CheckKey`. Then a `doctor` exit-code test per state.
2. **Criterion 2 — fast with the scanner unplugged.** Do **not** gate on wall-clock; it is
   flaky and this suite forbids `sleep`. Measure the three facts that *cause* speed:
   (a) `GET /` issues zero probes, (b) the Paperless probe carries a bounded
   `httpx.Timeout`, (c) the scanner check is skipped while a job is active. A wall-clock
   smoke test against a blackholed host may exist with a generous bound, but must not be
   the gate.
3. **Criterion 3 — page counts everywhere.** A parametrised test over the six terminal
   cases (DONE, FALLBACK, ERROR, CANCELLED, REJECTED, pre-Phase-23 NULL row) asserting the
   rendered text, plus one worker test for the live pass-A count at `SCANNING_REVERSE`.
4. **Criterion 4 — plain-language errors.** The `ErrorCategory` completeness test is the
   gate; one rendering test per surface (web `<details>`, CLI second line) is the proof.
5. **Criterion 5 — two contexts, labels, read-only strip.** The two-context Playwright test
   is the only honest sample. Per CLAUDE.md this is **not** manual-only.

---

## Wave 0 Requirements

- [ ] `tests/test_checks.py` — the registry, the five checks, three states (APPL-01, -02, -06, -11)
- [ ] `tests/test_checks_cache.py` — TTL with an **injected clock**, last-known-good retention (APPL-02)
- [ ] `tests/test_web_checks.py` — routes, cold-start poll, paused-during-scan, Refresh (APPL-02)
- [ ] `tests/test_doctor.py` — CliRunner exit codes and table output (APPL-01)
- [ ] Extend `tests/test_app_lifespan.py` — refresher start/stop and the close-ordering assertion at `:335` (A-7)
- [ ] Extend `tests/test_deployment_config.py` — `doctor`'s exit-code table, the compose consume mount, the commented env block, the "six commands" line
- [ ] Extend `tests/test_browser.py` — a module-level egress-gate helper and the two-context fixture (Pitfall 9)
- [ ] Retarget the `ErrorCategory` completeness test in `tests/test_vocabulary.py` at `error_advice`

*No framework install needed — pytest, pytest-timeout and pytest-playwright are already pinned.*

---

## Thread and cache testing — three techniques, no `time.sleep`

Phase 32 forbids `time.sleep` in the suite; do not introduce it here.

1. **Injectable clock** — the TTL cache takes a `now: Callable[[], float]` defaulting to
   `time.monotonic`. Tests advance a fake counter.
2. **Directly-callable `_tick()`** — the refresh thread's body is a plain method the test
   calls synchronously, so the thread loop itself needs only one start/stop test.
3. **`Event` handshakes** — the test waits on a `threading.Event` the thread sets, with a
   bounded `wait(timeout=...)`, instead of sleeping and hoping.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real scanner reachability against physical hardware | APPL-01 | Requires a physical SANE device on the LAN | Out of suite scope; the fake backend covers the code path |

*Everything else, including all browser behaviour, is automated. Per CLAUDE.md, browser
checks may never be marked "manual-only" or "needs human".*

---

## Known hazards this phase must not trip

- **Playwright egress gate** lives on the overridden `context` fixture only
  (`tests/test_browser.py:284-312`). A hand-made `browser.new_context()` — which criterion 5
  requires — bypasses it silently and could reach the network in CI. Wave 0 must hoist the
  gate into a helper both contexts use.
- **`PLR0913`** counts keyword-only args: `JobStore.create_job` already has 5 non-self
  params, so adding `owner_token` as a parameter is a lint failure. Use `JobResult`-style
  grouping; this project adds no suppressions.
- **`tests/test_deployment_config.py:432`** asserts every ``## `saneless <cmd>` `` doc
  heading has an `**Exit codes:**` table — a `doctor` doc section without one hard-fails.
  This is the phase's best doc-truth hook.
- **`MetadataCache.get_or_fetch` re-raises and caches nothing** (`web/cache.py:69-116`).
  CONTEXT.md's code_context describes it as retaining a stale entry; it does not. The check
  cache must retain last-known-good explicitly for D-08.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all ❌ MISSING references above
- [x] No watch-mode flags
- [x] `nyquist_compliant: true` set in frontmatter
- [ ] No `time.sleep` introduced anywhere — *plan-set gate only; the browser plans
      (30-17, 30-19) each carry a grep asserting the count does not rise. Confirmed
      at execution, not at planning.*
- [ ] Feedback latency < 90s (full suite) — *77s measured before this phase's tests
      were added. Re-measure after execution.*

**Approval:** approved for execution 2026-09-16 — gsd-plan-checker returned
VERIFICATION PASSED against the 19-plan set with no blockers.

`wave_0_complete` stays `false` deliberately: the plan set *covers* every Wave 0 test
file by name, but the tests themselves are written during execution. Flip it when
Wave 0 lands.
