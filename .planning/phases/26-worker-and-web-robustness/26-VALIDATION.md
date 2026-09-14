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
| 26-06-T1 | 26-06 | 3 | ROBU-01 | DoS | store/prune exceptions logged with `exc_info`; worker alive; next job served | unit (threaded) | `uv run pytest tests/test_worker.py -k "TestWorkerGuard or prune" -x` | ❌ W0 | ⬜ pending |
| 26-06-T2 | 26-06 | 3 | ROBU-01 / D-10..D-12 | DoS | N loop failures → degraded; probe clears; pipeline errors never count; recovery reconciles orphans | unit | `uv run pytest tests/test_worker.py -k degraded -x` | ❌ W0 | ⬜ pending |
| 26-10-T1 | 26-10 | 4 | ROBU-01 / D-10 | DoS | `/health` 503 "job store failing" / "worker thread is down" | integration | `uv run pytest tests/test_web.py -k health -x` | ❌ W0 | ⬜ pending |
| 26-10-T2 | 26-10 | 4 | ROBU-02 | DoS (C-09) | queue full → 429 + `Retry-After`; REJECTED ERROR row; HX retarget vs JSON branch | integration | `uv run pytest tests/test_web_errors.py -k "429 or queue_full or 503" -x` | ❌ W0 | ⬜ pending |
| 26-05-T1 | 26-05 | 2 | ROBU-02 / D-01..D-04 | Info disclosure | renderer: HX partial + retarget, JSON otherwise, Retry-After on 429 | integration | `uv run pytest tests/test_web_errors.py -x` | ❌ W0 | ⬜ pending |
| 26-13-T1 | 26-13 | 7 | ROBU-02 | — | 429 message visible in browser, recorded in history, legible, aligned, wraps | e2e (browser) | `uv run pytest tests/test_browser.py -m browser -k TestRequestErrorSlot` | ❌ W0 | ⬜ pending |
| 26-13-T2 | 26-13 | 7 | ROBU-02 / D-03 | — | message survives polling; cleared by a successful scan | e2e (browser) | `uv run pytest tests/test_browser.py -m browser -k "survives_status_polling or clears_the_error"` | ❌ W0 | ⬜ pending |
| 26-01-T2 | 26-01 | 1 | ROBU-02 / D-06 | — | `latest_run_job` skips REJECTED rows, incl. after store reopen | unit | `uv run pytest tests/test_job.py -k "latest_run or probe" -x` | ❌ W0 | ⬜ pending |
| 26-10-T2 | 26-10 | 4 | ROBU-02 / D-06 | — | rejected row never becomes the status-area fallback | integration | `uv run pytest tests/test_web.py -k rejected -x` | ❌ W0 | ⬜ pending |
| 26-01-T1 | 26-01 | 1 | ROBU-02 / ROBU-08 | Info disclosure | S3 copy and enums pinned verbatim; totality | unit | `uv run pytest tests/test_vocabulary.py -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| 26-04-T1 | 26-04 | 2 | ROBU-02 / ROBU-03 | DoS | `stop()` never blocks on a full queue; wakes idle worker < 0.5 s; aborts open flip; bounded join; converted tests | unit | `uv run pytest tests/test_worker.py -k "TestWorkerStopAndSubmit" -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| 26-11-T1 | 26-11 | 5 | ROBU-04 | — | every status response carries exactly one OOB `#scan-btn` identical to the index copy; index has one | integration | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| 26-11-T2 | 26-11 | 5 | ROBU-04 / ROBU-08 | — | form wiring + `hx-disinherit`; `maxlength=256`; `app.js` absent and 404 | integration | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ file / ❌ W0 tests | ⬜ pending |
| 26-12-T2 | 26-12 | 6 | ROBU-04 / ROBU-11 | — | click Scan → terminal → button enabled, one `#scan-btn`; busy while held; disinherit trap | e2e (browser, CI) | `uv run pytest tests/test_browser.py -m browser -k TestServerOwnedScanButton` | ❌ W0 | ⬜ pending |
| 26-10-T1 | 26-10 | 4 | ROBU-05 | DoS (M-01) | no coroutine route endpoints; `/health` < 1 s while another request blocks; cache single-flight | unit + integration | `uv run pytest tests/test_web.py -k "coroutine or health_answers" -x` ; `uv run pytest tests/test_cache.py -k get_or_fetch -x` | ❌ W0 | ⬜ pending |
| 26-04-T2 | 26-04 | 2 | ROBU-05 / D-19 | Tampering (race) | profile snapshot lock (no dict-size RuntimeError) | unit (threaded) | `uv run pytest tests/test_worker.py -k profile_lock -x` | ❌ W0 | ⬜ pending |
| 26-09-T1 | 26-09 | 4 | ROBU-06 | Integrity | active rows (incl. PENDING) failed with restart reason before `worker.start()`; recovery failure starts degraded | integration | `uv run pytest tests/test_app_lifespan.py -x` | ❌ W0 | ⬜ pending |
| 26-09-T2 | 26-09 | 4 | ROBU-06 / D-09 | Integrity | join timeout → WARNING, store/Paperless left open; normal close order | integration | `uv run pytest tests/test_app_lifespan.py -x` | ❌ W0 | ⬜ pending |
| 26-02-T1 | 26-02 | 1 | ROBU-07 / D-16 | Tampering | `load_settings` records the path; not env/TOML-settable; one search list | unit | `uv run pytest tests/test_config.py -k "LoadedConfigPath or config_path" -x` | ❌ W0 | ⬜ pending |
| 26-02-T2 | 26-02 | 1 | ROBU-07 | — | `is_bare_default` shapes table | unit | `uv run pytest tests/test_auto_profiles.py -k untouched_default_shapes -x` | ✅ file / ❌ W0 test | ⬜ pending |
| 26-08-T1 | 26-08 | 4 | ROBU-07 / D-14..D-18 | Tampering | startup generation writes to `settings.config_path`; env-only memory + INFO; unwritable WARNING; once only; before first job | unit | `uv run pytest tests/test_worker.py -k startup_generation -x` | ❌ W0 | ⬜ pending |
| 26-08-T2 | 26-08 | 4 | ROBU-07 | — | suite green with generation at startup; no stray files | suite | `uv run pytest -m "not browser and not sane_hardware" -q` | ✅ | ⬜ pending |
| 26-10-T2 | 26-10 | 4 | ROBU-08 | Tampering / DoS (N-20) | unknown profile, 257-char title, bogus resource → 422, no row, input never echoed | integration | `uv run pytest tests/test_web_errors.py -k 422 -x` | ❌ W0 | ⬜ pending |
| 26-05-T1 | 26-05 | 2 | D-01 / D-04 | Info disclosure | catch-all 500 renders fixed message (HX) or JSON; `exc_info` logged; 404/405 via renderer | integration | `uv run pytest tests/test_web_errors.py -k "500 or 404 or 405" -x` | ❌ W0 | ⬜ pending |
| 26-05-T2 | 26-05 | 2 | D-01 / D-03 | — | htmx-config restates 3 entries; one empty slot above `#status-area` | integration | `uv run pytest tests/test_web_errors.py -k "slot or htmx_config" -x` | ❌ W0 | ⬜ pending |
| 26-03-T1 | 26-03 | 1 | ROBU-09 | Tampering | vendor dir excluded from byte-rewriting hooks; `-text` | config check | `git check-attr text src/saneless/web/static/vendor/htmx-2.0.8.min.js` | n/a | ⬜ pending |
| 26-03-T2 | 26-03 | 1 | ROBU-09 | Tampering (N-21) | template `integrity` == sha384(file bytes); no external URLs; licences; Pico coupling contract; wheel contains vendor files | unit + smoke | `uv run pytest tests/test_vendor_assets.py -x` | ❌ W0 | ⬜ pending |
| 26-12-T1 | 26-12 | 6 | ROBU-09 / ROBU-11 | Tampering | whole browser suite (DARK-01/02 included) passes with non-test-server requests aborted | e2e (browser, CI) | `uv run pytest -m browser` | ✅ file / ❌ W0 override | ⬜ pending |
| 26-07-T1 | 26-07 | 3 | ROBU-10 | Tampering / Spoofing (N-22) | D-20 three branches; X-Forwarded-Host; `null` Origin rejected | unit | `uv run pytest tests/test_cross_origin.py -x` | ❌ W0 | ⬜ pending |
| 26-07-T2 | 26-07 | 3 | ROBU-10 / D-22 / D-23 | Tampering | every unsafe route covered generatively; 403 via renderer; log line with `%r` | integration | `uv run pytest tests/test_cross_origin.py -x` | ❌ W0 | ⬜ pending |
| 26-13-T3 | 26-13 | 7 | ROBU-10 / D-20 | DoS | plain-HTTP LAN origin scan not blocked; no Sec-Fetch-Site sent | e2e (browser) | `uv run pytest tests/test_browser.py -m browser -k PlainHttpLanOrigin -rs` | ❌ W0 | ⬜ pending |
| 26-14-T1/T2 | 26-14 | 6 | ROBU-10 | Info disclosure | default bind `0.0.0.0` documented; proxy Host guidance; API error statuses | docs check | `rg -n "0\.0\.0\.0" docs/reference` | n/a | ⬜ pending |
| 26-14-T3 | 26-14 | 6 | — | — | master UI-SPEC updated (no app.js / CDN / SRI gap) | docs check | `! grep -n "app\.js\|cdn\.jsdelivr" .planning/UI-SPEC.md` | n/a | ⬜ pending |
| 26-12-T3 | 26-12 | 6 | ROBU-11 | Supply chain | CI browser job runs `-m browser` after `playwright install --with-deps chromium` | config check | `rg -n "playwright install --with-deps chromium" .github/workflows/ci.yml` | ❌ W0 | ⬜ pending |

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
