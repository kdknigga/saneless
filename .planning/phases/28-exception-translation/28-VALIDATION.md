---
phase: 28
slug: exception-translation
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| EXC-02 | bad config 2, broken scanner 1, unreachable Paperless 3, PDF 4, python-sane missing 2 + hint, unexpected 5, Ctrl-C 130, serve bind/startup failure 2, `--help` without sane 0 | CliRunner | `uv run pytest tests/test_cli.py -k exit -x` | extend ✅ | ⬜ pending |
| EXC-02 | doc exit-code tables list every `ExitCode` | doc-truth | `uv run pytest tests/test_deployment_config.py -k exit -x` | extend ✅ | ⬜ pending |
| EXC-03 | empty batch → "No pages were scanned" (simplex, duplex pass A/B); all removed → "All pages were blank"; feeder-empty message unchanged | unit | `uv run pytest tests/test_pipeline.py -k pages -x` | extend ✅ | ⬜ pending |
| EXC-04 | web Abort → CANCELLED, INFO, no traceback, not degraded; timeout → ERROR; shutdown → ERROR RESTART_REASON; CLI n/Ctrl-C/EOF → 130; prompt failure → 1 | unit + CliRunner | `uv run pytest tests/test_worker.py tests/test_cli.py -k "cancel or abort" -x` | extend ✅ | ⬜ pending |
| EXC-04 | CANCELLED renders muted in light/dark; AA contrast; Scan button re-enables | browser (Playwright) | `uv run pytest tests/test_browser.py -m browser -k cancel` | extend ✅ | ⬜ pending |
| EXC-05 | classified and UNKNOWN failures logged with `exc_info` = raised exception | unit (caplog) | `uv run pytest tests/test_worker.py -k exc_info -x` | extend ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements. Optional: a CANCELLED job fixture helper for `tests/test_browser.py` mirroring the FALLBACK fixture.

---

## Manual-Only Verifications

All phase behaviors have automated verification. Tests must never open a real SANE device (a real scanner is visible on the dev host).

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
