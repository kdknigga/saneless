---
phase: 24
slug: scanner-truthfulness
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-09-13
approved: 2026-09-13
---

# Phase 24 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `24-RESEARCH.md` § "Validation Architecture" (line 1038), whose figures were
> **executed**, not estimated.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-timeout 2.4.0 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py -q` |
| **Full suite command** | `uv run pytest -m "not browser"` |
| **Integration command** | `uv run pytest -m sane_hardware -q` |
| **Measured baseline** | **742 passed, 30 deselected in 29.48s** (executed during research) |
| **Estimated runtime** | ~30 seconds full suite; quick run materially faster |
| **Global timeout** | `timeout = 60`, `timeout_method = "signal"` |
| **Strictness** | `filterwarnings = ["error"]`, `xfail_strict`, `--strict-markers`, `--strict-config` |

**New marker:** `sane_hardware` **must** be registered in `pyproject.toml`'s `markers` list before
any test uses it — `--strict-markers` is live, so collection fails otherwise.

---

## Sampling Rate

- **After every task commit:** `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py -q`
- **After every plan wave:** `uv run pytest -m "not browser"` **plus** `uv run pytest -m sane_hardware -q`
- **Before `/gsd-verify-work`:** full suite green, `sane_hardware` green, and all four static gates
  clean — `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pyrefly check src tests` (paths named, never bare — 23.1 D-10)
- **Max feedback latency:** ~30 seconds

---

## Per-Requirement Verification Map

Task IDs are assigned during planning; this map is keyed by requirement so the planner can attach
each row to the task that satisfies it. "File Exists" means the test *file* exists today — ❌ W0
marks a Wave 0 gap that must be created before the row can go green.

| Req | Behavior | Threat Ref | Test Type | Automated Command | File Exists |
|-----|----------|-----------|-----------|-------------------|-------------|
| SCNR-01 | `classify_source` drives flatbed selection in `generate_profiles` (`:181`, `:182`, `:193`) | — | unit | `uv run pytest tests/test_auto_profiles.py -q` | ✅ |
| SCNR-01 | "Automatic Document Feeder" yields N>1 pages through `SaneBackend` | — | unit (fake) | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-01 | Two distinct feeder sources produce two distinct slugs (N-09) | V5 | unit | `uv run pytest tests/test_auto_profiles.py -q` | ✅ |
| SCNR-01 | `auto_source_mode` override recognises `"auto"` case-insensitively | — | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-02 | Each real `_sane.error` message surfaces as `ScanError` carrying the text, never `FeederEmptyError` | Repudiation | unit (fake), parametrised | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-02 | `StopIteration` on page 0 → `FeederEmptyError` (the only path) | — | unit | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-03 | A pure-white and a pure-black page both survive the backend | — | unit | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-03 | Blank removal happens only in `_drop_empty_pages`, only when enabled | — | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-03 | Duplex parity preserved when no page fails integrity | — | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-03 | D-06: one integrity failure skips and counts; all-fail raises | — | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-04 | Crop fallback **proven reachable** when any of the four geometry options is absent | — | unit | `uv run pytest tests/test_scanner.py -q` | ✅ (must fix `_NoGeometryDevice`) |
| SCNR-04 | `GeometryUnit` exhaustiveness, parametrised over `list(GeometryUnit)` | — | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-04 | `UNIT_PIXEL` converts using the read-back resolution | — | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-04 | **D-19:** a clamped geometry area is detected on read-back and falls through to the crop | — | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-05 | Options set source→mode→resolution; a source change narrowing the constraint is caught | — | unit (**fake only**) | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-05 | Actual DPI reaches `crop_to_paper_size` **and** `assemble_pdf` | — | unit | `uv run pytest tests/test_pipeline.py -q` | ❌ W0 |
| SCNR-06 | `(min,max,step)` populates the range field; list populates `resolutions`; `None` yields neither | V5 | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-06 | `devices --capabilities` prints the range | — | unit | `uv run pytest tests/test_cli.py -q` | ✅ |
| SCNR-07 | Fake mirrors real semantics: unknown option stored; bad value → error; wrong access → `AttributeError`; `multi_scan()` cannot raise | — | unit, parametrised | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| SCNR-07 | `tests/test_pipeline.py` uses the shared fake for at least one duplex path | — | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-08 | Real `test:0`: enumeration lists it; ten pages via "Automatic Document Feeder"; range honoured | — | integration (marker-gated) | `uv run pytest -m sane_hardware -q` | ❌ W0 |
| D-04 | Exceeding `_MAX_ADF_PAGES` raises `ScanError` naming cap and count | **W-01** (DoS) | unit | `uv run pytest tests/test_scanner.py -q` | ❌ W0 |
| D-16 | Prune removes orphaned `auto_generated` profiles, preserving unflagged ones **and comments** | — | unit | `uv run pytest tests/test_auto_profiles.py -q` | ❌ W0 |

---

## Wave 0 Requirements

- [ ] `tests/fake_sane.py` — the D-17 shared fake (`FakeSaneDev`, `FakeSaneModule`, local error type).
      **Blocks most rows above — build it first.**
- [ ] `tests/test_sane_hardware.py` — the D-18 module plus its **session-scoped** `SANE_CONFIG_DIR`
      fixture. A function-scoped `monkeypatch.setenv` was **measured to fail**: `SANE_CONFIG_DIR` is
      honoured only before the first `sane.init()` in the process, and `sane.exit()` does not reset it.
- [ ] `pyproject.toml` `markers` — register `sane_hardware` (collection fails otherwise).
- [ ] `.github/workflows/ci.yml:44` — widen the deselect filter. **No new apt package needed:**
      `libsane-dev` → `libsane1` already ships `libsane-test.so.1`.
- [ ] `CONTRIBUTING.md:83-84` — document the new marker beside `browser`.
- [ ] `tests/test_scanner.py:1016-1031` — fix `_NoGeometryDevice` to **store** rather than raise, so the
      existing SCNR-04 fallback tests become meaningful instead of decorative.

---

## Ordering Constraints (measured, not inferred)

1. **SCNR-08 is downstream of D-05.** The SANE `test` backend's default picture is solid black, so
   today's `_validate_page_image` discards all ten ADF pages and `scan_pages` returns `[]`. The
   ten-page assertion **cannot pass until the pure-black check is removed**. Write the integration
   test RED against that exact behaviour and let D-05 turn it green — do not schedule them in
   parallel waves.
2. **D-04 is downstream of a refactor.** `_scan_adf_pages` already sits at **12/12** branches against
   ruff's `PLR0912`. Adding the page cap breaks the build unless the function is split first.
3. **D-12 touches the ABC.** `scan_pages` is a generator and `list()` discards
   `StopIteration.value`, so the actual DPI and reject count cannot leave the backend without
   changing `ScannerBackend` and every fake that implements it.

---

## Manual-Only Verifications

**None.** There is no browser surface in this phase, and the one piece of hardware behaviour is
automated through the SANE `test` backend — which is the entire point of SCNR-08.

---

## Validation Sign-Off

Signed off 2026-09-13, after `gsd-plan-checker` verified the eight plans: **0 blockers, 4 warnings**.

- [x] All tasks have an `<automated>` verify or a Wave 0 dependency
- [x] Sampling continuity: no 3 consecutive tasks without an automated verify
- [x] Wave 0 covers every ❌ W0 row above — built by 24-01 (shared fake, marker registration,
      session-scoped fixture) and completed across 24-02..24-08
- [x] No watch-mode flags
- [ ] **Feedback latency < 30s — one deliberate, recorded exception.** 24-07 Task 2 runs
      `uv run pytest -m browser -q` (22 chromium tests) as a per-task verify, because
      `_BrowserTestScanner` is one of the six concrete `ScannerBackend` implementers the D-12 ABC
      change touches, and no type checker can catch that site. Accepted knowingly: it is a genuine
      correctness check and it applies to exactly one task out of 24. Every other task stays inside
      the ~30s envelope. Narrowing it to the specific browser tests that exercise
      `_BrowserTestScanner` is a valid alternative if the latency proves annoying in practice.
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-13 — with the latency exception above **recorded, not waived**.

**`wave_0_complete` deliberately stays `false`.** Wave 0's artifacts — `tests/fake_sane.py`,
`tests/test_sane_hardware.py`, and the `sane_hardware` marker registration — do not exist until
execution builds them. Flip that flag during execution, not at planning time.
