---
phase: 26
slug: worker-and-web-robustness
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-09-14
---

# Phase 26 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `26-RESEARCH.md` § Validation Architecture. Task IDs are filled in once plans exist.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2, pytest-playwright 0.7.2 (Playwright 1.58.0), pytest-timeout 2.4.0 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` (markers `browser`, `sane_hardware`; `filterwarnings=error`; `timeout=60`) |
| **Quick run command** | `uv run pytest tests/test_worker.py tests/test_web.py tests/test_web_state_rendering.py -x -q` |
| **Full suite command** | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m browser` |
| **Gate commands** | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` |
| **Estimated runtime** | ~90 seconds (non-browser) + browser suite |

---

## Sampling Rate

- **After every task commit:** quick run command plus the specific new test file the task touches
- **After every plan wave:** full suite (both markers) + `uv run prek run --stage pre-push --all-files`
- **Before `/gsd-verify-work`:** full suite green and the CI `browser` job green on the pushed branch
- **Max feedback latency:** 120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | ROBU-01 | — | store/prune exceptions logged with `exc_info`; worker alive; next job served | unit (threaded) | `uv run pytest tests/test_worker.py -k "guard or survives" -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-01 / D-10..D-12 | DoS | N loop failures → degraded; `/health` 503; probe clears; pipeline errors never count | unit | `uv run pytest tests/test_worker.py -k degraded -x` ; `uv run pytest tests/test_web.py -k health -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-02 | DoS (C-09) | queue full → 429 + `Retry-After`; REJECTED ERROR row; HX retarget vs JSON branch | integration | `uv run pytest tests/test_web_errors.py -k "429 or queue_full" -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-02 | — | 429 message visible in browser while `#status-area` keeps polling | e2e (browser) | `uv run pytest tests/test_browser.py -m browser -k queue_full` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-02 / D-06 | — | rejected row never becomes the status-area fallback, incl. after store reopen | unit + integration | `uv run pytest tests/test_job.py -k latest_run -x` ; `uv run pytest tests/test_web.py -k rejected -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-02 / ROBU-03 | DoS | `stop()` never blocks on a full queue; wakes idle worker < 0.5 s; aborts open flip | unit | `uv run pytest tests/test_worker.py -k "stop" -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| TBD | TBD | TBD | ROBU-04 | — | every status response carries exactly one OOB `#scan-btn`; index has one; `app.js` absent | integration | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| TBD | TBD | TBD | ROBU-04 / ROBU-11 | — | click Scan → terminal → button enabled, one `#scan-btn`, no `app.js`, no egress | e2e (browser, CI) | `uv run pytest tests/test_browser.py -m browser -k scan_button` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-05 | DoS (M-01) | no coroutine route endpoints; `/health` < 1 s while another request blocks | unit + integration | `uv run pytest tests/test_web.py -k "coroutine or health_answers" -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-05 | — | cache single-flight (8 concurrent → 1 fetch); profile snapshot lock (no dict-size RuntimeError) | unit (threaded) | `uv run pytest tests/test_cache.py -k single_flight -x` ; `uv run pytest tests/test_worker.py -k profile_lock -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| TBD | TBD | TBD | ROBU-06 | — | active rows (incl. PENDING) failed with restart reason before worker's first store call; join timeout → WARNING, store/Paperless left open | integration | `uv run pytest tests/test_app_lifespan.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-07 | — | startup generation writes to `settings.config_path`; env-only memory + INFO; unwritable WARNING; once only; `is_bare_default` shapes | unit | `uv run pytest tests/test_worker.py -k startup_generation -x` ; `uv run pytest tests/test_config.py -k config_path -x` ; `uv run pytest tests/test_auto_profiles.py -k bare -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-08 | Tampering / DoS (N-20) | unknown profile, 257-char title, bogus resource → 422, no row, message slot partial | integration | `uv run pytest tests/test_web_errors.py -k 422 -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | D-01 / D-04 | Info disclosure | catch-all 500 renders fixed message (HX) or JSON; `exc_info` logged; 404/405 via renderer | integration | `uv run pytest tests/test_web_errors.py -k "500 or 404" -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-09 | Tampering (N-21) | template `integrity` == sha384(file bytes); no external URLs; licences; Pico coupling contract; wheel contains vendor files | unit + smoke | `uv run pytest tests/test_vendor_assets.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-09 / ROBU-11 | — | whole browser suite (DARK-01/02 included) passes with non-test-server requests aborted | e2e (browser, CI) | `uv run pytest -m browser` | ✅ file / ❌ W0 override | ⬜ pending |
| TBD | TBD | TBD | ROBU-10 | Tampering / Spoofing (N-22) | D-20 three branches; X-Forwarded-Host; `null` Origin 403; every unsafe route covered; 403 logs Origin/Host/XFH with `%r` | integration | `uv run pytest tests/test_cross_origin.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | ROBU-10 | — | default bind `0.0.0.0` documented | docs check | `rg -n "0\.0\.0\.0" docs/reference` | n/a | ⬜ pending |
| TBD | TBD | TBD | ROBU-11 | — | CI browser job runs `-m browser` after `playwright install --with-deps chromium` | config check | `rg -n "playwright install --with-deps chromium" .github/workflows/ci.yml` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_web_errors.py` — renderer, handlers (422/429/403/404/500), htmx vs JSON branches, `Retry-After`
- [ ] `tests/test_cross_origin.py` — D-20 branches + generative unsafe-route test
- [ ] `tests/test_vendor_assets.py` — SRI bytes, no external URLs, licences, coupling contract
- [ ] `tests/test_app_lifespan.py` — recovery ordering, guarded close, startup prune
- [ ] New tests inside `tests/test_worker.py`, `tests/test_web.py`, `tests/test_web_state_rendering.py`, `tests/test_cache.py`, `tests/test_config.py`, `tests/test_auto_profiles.py`, `tests/test_job.py`, `tests/test_browser.py`
- [ ] `_BrowserTestScanner` gate Event (for the ROBU-02 browser test)
- [ ] No framework install needed

---

## Manual-Only Verifications

All phase behaviors have automated verification. (The CI `browser` job going green on the pushed branch is observed, not hand-tested.)

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
