---
phase: 28
slug: exception-translation
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-09-15
---

# Phase 28 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `28-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-timeout (60 s) + pytest-playwright 0.7.2; `filterwarnings = ["error"]`; strict markers |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_vocabulary.py tests/test_cli.py tests/test_paperless.py tests/test_pdf.py -x -q -m "not browser and not sane_hardware"` |
| **Full suite command** | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m sane_hardware && uv run pytest -m browser` |
| **Gate command** | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` |
| **Estimated runtime** | quick ~5 s; full non-browser ~60 s; browser ~60 s |

---

## Sampling Rate

- **After every task commit:** Run the quick run command plus the task's file-specific command
- **After every plan wave:** Run the full non-browser suite plus the gate command
- **Before `/gsd-verify-work`:** Full suite (incl. `-m browser`, `-m sane_hardware`) green, then `uv run prek run --stage pre-push --all-files`
- **Max feedback latency:** 5 seconds (quick), 120 seconds (wave)

---

## Per-Task Verification Map

Filled by the planner per task. Requirement → test map from research:

| Requirement | Behavior | Test Type | Automated Command | File Exists | Status |
|-------------|----------|-----------|-------------------|-------------|--------|
| EXC-01 | SANE errors (open, option set, start/snap, get_devices, get_options, init) → `ScanError` with context, `__cause__` original | unit (parametrised) | `uv run pytest tests/test_scanner.py -k boundary -x` | extend ✅ | ⬜ pending |
| EXC-01 | httpx transport errors retry → fallback; `UnsupportedProtocol` immediate; ctor `InvalidURL`; get_tags/correspondents; poll continues to deadline | unit (MockTransport) | `uv run pytest tests/test_paperless.py -x` | extend ✅ | ⬜ pending |
| EXC-01 | seven img2pdf classes + ValueError + OSError + SystemError + RuntimeError → `PdfError` | unit (parametrised) | `uv run pytest tests/test_pdf.py -x` | extend ✅ | ⬜ pending |
| EXC-01 | TOML syntax / unreadable / non-UTF-8 → `ConfigError` with header + line/column, no `doc`/token; tomlkit parse → `ConfigError` | unit | `uv run pytest tests/test_config.py tests/test_auto_profiles.py -k toml -x` | extend ✅ | ⬜ pending |
| EXC-02 | bad config 2, unusable job database (`StorageError`) 2 with the db path and no "Unexpected error", broken scanner 1, unreachable Paperless 3, PDF 4, python-sane missing 2 + hint, non-saneless exception 5, Ctrl-C 130, serve bind/startup failure 2, `--help` without sane 0 | CliRunner | `uv run pytest tests/test_cli.py -k exit -x` | extend ✅ | ⬜ pending |
| EXC-02 | doc exit-code tables list every `ExitCode` | doc-truth | `uv run pytest tests/test_deployment_config.py -k exit -x` | extend ✅ | ⬜ pending |
| EXC-03 | empty batch → "No pages were scanned" (simplex, duplex pass A/B); all removed → "All pages were blank"; feeder-empty message unchanged | unit | `uv run pytest tests/test_pipeline.py -k pages -x` | extend ✅ | ⬜ pending |
| EXC-04 | web Abort → CANCELLED, INFO, no traceback, not degraded; timeout → ERROR; shutdown → ERROR RESTART_REASON; CLI n/Ctrl-C/EOF → 130; prompt failure → 1 | unit + CliRunner | `uv run pytest tests/test_worker.py tests/test_cli.py -k "cancel or abort" -x` | extend ✅ | ⬜ pending |
| EXC-04 | CANCELLED renders muted in light/dark; AA contrast; Scan button re-enables | browser (Playwright) | `uv run pytest tests/test_browser.py -m browser -k cancel` | extend ✅ | ⬜ pending |
| EXC-05 | classified and UNKNOWN failures logged with `exc_info` = raised exception | unit (caplog) | `uv run pytest tests/test_worker.py -k exc_info -x` | extend ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

### Per-task map (planner)

| Plan-Task | Wave | Requirement / Decision | Automated Command | Status |
|-----------|------|------------------------|-------------------|--------|
| 28-01-T1 | 1 | EXC-01, EXC-02 (types, describe, ExitCode) D-04 D-07 D-08 | `uv run pytest tests/test_exceptions.py tests/test_vocabulary.py -x -q` | ⬜ pending |
| 28-01-T2 | 1 | EXC-04 (JobState.CANCELLED + templates) D-01 | `uv run pytest tests/test_vocabulary.py tests/test_web_state_rendering.py -x -q` | ⬜ pending |
| 28-02-T1 | 1 | EXC-01 (TOML syntax/unreadable/non-UTF-8) D-12 | `uv run pytest tests/test_config.py -k "toml or utf or unreadable or secret or token" -x -q` | ⬜ pending |
| 28-02-T2 | 1 | EXC-01 (tomlkit parse) D-12 | `uv run pytest tests/test_auto_profiles.py -k toml -x -q` | ⬜ pending |
| 28-03-T1 | 1 | EXC-03 | `uv run pytest tests/test_pipeline.py -k "pages or empty or blank" -x -q` | ⬜ pending |
| 28-04-T1 | 2 | EXC-01 (img2pdf seven classes + untyped + Pillow), EXC-03 (empty list) D-04 | `uv run pytest tests/test_pdf.py -k Boundary -x -q` | ⬜ pending |
| 28-05-T1 | 2 | EXC-01 (SANE init/open/get_devices), EXC-02 (install hint) D-05 | `uv run pytest tests/test_scanner.py -k "Boundary or require_sane" -x -q` | ⬜ pending |
| 28-05-T2 | 2 | EXC-01 (option set, read-back, get_options, start/snap) D-08 | `uv run pytest tests/test_scanner.py -x -q` | ⬜ pending |
| 28-06-T1 | 2 | EXC-01 (one body renderer) D-09 | `uv run pytest tests/test_paperless.py -k "body or poll" -x -q` | ⬜ pending |
| 28-06-T2 | 2 | EXC-01 (ctor InvalidURL, retry set, UnsupportedProtocol, fallback) D-10 | `uv run pytest tests/test_paperless.py -x -q` | ⬜ pending |
| 28-07-T1 | 2 | EXC-04 (worker CANCELLED), EXC-05 (exc_info) D-01 D-02 | `uv run pytest tests/test_worker.py -k "cancel or exc_info or shutdown or degraded" -x -q` | ⬜ pending |
| 28-08-T1 | 2 | EXC-04 (browser muted, AA, terminal) D-01 | `uv run pytest tests/test_browser.py -m browser -k "cancel or Cancel or contrast or dark or colour" -x -q` | ⬜ pending |
| 28-08-T2 | 2 | UI-SPEC rows | `grep -c "status-cancelled" .planning/UI-SPEC.md` | ⬜ pending |
| 28-09-T1 | 2 | EXC-02 (log-file truth) D-06 | `uv run pytest tests/test_logging.py -k attached -x -q` | ⬜ pending |
| 28-09-T2 | 2 | EXC-01, EXC-02 (JobStore open failure -> StorageError naming the db path) D-07 amendment D-08 | `uv run pytest tests/test_job.py -k unusable -x -q` | ⬜ pending |
| 28-09-T3 | 2 | EXC-02 (guard: 1/2 incl. job database StorageError/3/4/5 non-saneless only/130, --help 0) D-03 D-06 D-07 D-07 amendment D-12 | `uv run pytest tests/test_cli.py -k "exit or Exit or config_error or help" -x -q` | ⬜ pending |
| 28-10-T1 | 3 | EXC-01 (poll continues to deadline, duplicate hint) D-10 D-11 | `uv run pytest tests/test_paperless.py -k "poll or duplicate" -x -q` | ⬜ pending |
| 28-10-T2 | 3 | EXC-01 (get_tags/correspondents) D-11 | `uv run pytest tests/test_paperless.py -k metadata -x -q` | ⬜ pending |
| 28-11-T1 | 3 | EXC-04 (abort_cause) D-02 | `uv run pytest tests/test_cli.py -k "Prompt or abort_cause" -x -q` | ⬜ pending |
| 28-11-T2 | 3 | EXC-04 (CLI 130, prompt failure 1, web CANCELLED) D-01 D-02 D-03 | `uv run pytest tests/test_pipeline.py tests/test_cli.py tests/test_worker.py -k "abort or cancel or flip or Prompt or shutdown or timeout" -x -q` | ⬜ pending |
| 28-12-T1 | 4 | EXC-02 (python-sane missing 2 + hint; jobs/--help unaffected) D-05 | `uv run pytest tests/test_cli.py -k "python_sane or help or jobs" -x -q` | ⬜ pending |
| 28-12-T2 | 4 | EXC-02 (serve bind/startup 2, Ctrl-C 0) D-07 amendment | `uv run pytest tests/test_cli.py -k "serve or Serve" -x -q` | ⬜ pending |
| 28-13-T1 | 5 | EXC-02 doc-truth exit tables D-07 D-13 | `uv run pytest tests/test_deployment_config.py -k "exit_code or flip_prompt" -x -q` | ⬜ pending |
| 28-13-T2 | 5 | D-13 troubleshooting how-to + nav | `uv run pytest tests/test_deployment_config.py -x -q && uv run mkdocs build --strict -d "$(mktemp -d)"` | ⬜ pending |
| 28-14-T1 | 4 | EXC-04 docs (web-api, duplex, architecture) | `uv run mkdocs build --strict -d "$(mktemp -d)"` + grep criteria | ⬜ pending |
| 28-14-T2 | 4 | EXC-01/EXC-03 docs (fallback, empty-page) | `uv run mkdocs build --strict -d "$(mktemp -d)"` + grep criteria | ⬜ pending |

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. Optional: a CANCELLED job fixture helper for `tests/test_browser.py` mirroring the FALLBACK fixture.

---

## Manual-Only Verifications

All phase behaviors have automated verification. Tests must never open a real SANE device (a real scanner is visible on the dev host).

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 120s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-09-15
