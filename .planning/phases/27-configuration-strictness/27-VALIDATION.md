---
phase: 27
slug: configuration-strictness
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-09-15
---

# Phase 27 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `27-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest >=9.0.2 + pytest-timeout (60 s, signal); `filterwarnings = ["error"]`; `--strict-markers --strict-config` |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_config.py tests/test_atomic_write.py tests/test_auto_profiles.py tests/test_logging.py tests/test_cli.py -x -q` |
| **Full suite command** | `uv run pytest -m "not browser and not sane_hardware"` |
| **Gate command** | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` |
| **Estimated runtime** | quick ~5 s; full suite ~60 s |

---

## Sampling Rate

- **After every task commit:** Run the quick run command
- **After every plan wave:** Run the full suite command plus the gate command
- **Before `/gsd-verify-work`:** Full suite green, `uv run prek run --all-files`, and `uv run prek run --stage pre-push --all-files`
- **Max feedback latency:** 5 seconds (quick), 90 seconds (wave)

---

## Per-Task Verification Map

Task IDs are assigned by the planner; each plan task must cite the row(s) it satisfies.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | TBD | TBD | CFG-01 | — | Unknown keys named with section, did-you-mean, valid keys; wrong-section hint; all errors one per line | unit | `uv run pytest tests/test_config.py -k "unknown_key or wrong_section or renders_every_error" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-01 (D-12/D-13) | T-27-env | Env var attribution; unknown `SANELESS_*` (incl. `SANELESS_CONFIG_PATH`) rejected; valid nested/JSON vars accepted | unit | `uv run pytest tests/test_config.py -k "attributes_env or unknown_env" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-01 (D-14) + CFG-05 | T-27-token | Token-shaped value absent from message, repr, exception chain, CLI stderr, CFG-11 log | unit + CLI | `uv run pytest tests/test_config.py tests/test_cli.py -k "never_echoes or secret" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-02 | — | Missing or non-file `--config` → ConfigError naming expanded path, exit 2; discovery skips directories | unit + CLI | `uv run pytest tests/test_config.py tests/test_cli.py -k "missing_config or not_a_file" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-03 | — | XDG vars honoured (empty/relative ignored), defaults computed at instantiation, `~` expanded, nearest-ancestor writability | unit | `uv run pytest tests/test_config.py -k "xdg or expanduser or ancestor" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-04 | — | Invalid `log_level` rejected by `[output] log_level`; `-v` → saneless hierarchy DEBUG, root/httpx unchanged; uvicorn level valid | unit + CLI | `uv run pytest tests/test_config.py tests/test_logging.py tests/test_cli.py -k "log_level or verbose or serve_log_level" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-06 | — | typed → profile title → `Scan <UTC>`; CLI without `--title` works; web empty title uses profile | unit + CLI + TestClient | `uv run pytest tests/test_config.py tests/test_cli.py tests/test_web.py -k title -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-07 | — | `--force` keeps user keys/comments, overwrites owned keys, deletes stale owned keys; unflagged profile byte-identical; grouped result + CLI output; worker consumes result | unit + CLI | `uv run pytest tests/test_auto_profiles.py tests/test_cli.py tests/test_worker.py -k "force or merge or grouped or startup_generation" -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-08 | T-27-write | mkstemp same dir, cleanup on every failure, fsync, mode + owner preserved (EPERM tolerated), symlink written through, CRLF/UTF-8 preserved, EBUSY → ConfigError with fix text, read-only refused, invalid TOML refused | unit | `uv run pytest tests/test_atomic_write.py tests/test_auto_profiles.py -k "atomic or crlf or utf8 or ebusy or symlink or inline or readonly" -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CFG-08 | T-27-write | `auto-profiles` EBUSY exits 2 with message, no traceback; worker EBUSY keeps in-memory profiles | CLI + worker | `uv run pytest tests/test_cli.py tests/test_worker.py -k ebusy -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-09 | — | compose uses `./config:/etc/saneless` rw; no doc contains single-file `:ro` mount or "fail to start" claim | static text | `uv run pytest tests/test_deployment_config.py -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CFG-09 (SC3) | — | `auto-profiles --force` succeeds against a directory-mounted config | unit (+ optional unshare) | `uv run pytest tests/test_atomic_write.py -k bind_mount -x` | ❌ W0 | ⬜ pending |
| TBD | TBD | TBD | CFG-10 | — | `<subcommand> --help` exits 0 with broken config; no logging configured; no log dir created | CLI | `uv run pytest tests/test_cli.py -k help -x` | ✅ | ⬜ pending |
| TBD | TBD | TBD | CFG-11 | T-27-token | One INFO record naming config path and env-sourced key names, no values | CLI (caplog) | `uv run pytest tests/test_cli.py -k config_sources -x` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_atomic_write.py` — CFG-08 helper behaviour (EBUSY, mode, chown, symlink, cleanup, read-only, CRLF/UTF-8, dir fsync best effort) and CFG-09 SC3 directory-mount case
- [ ] `tests/test_deployment_config.py` — CFG-09 text assertions on `docker-compose.yml` and `docs/**/*.md`
- [ ] `tests/conftest.py` — extend `clean_env` to remove `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
- [ ] Logging test cleanup helpers (`tests/test_logging.py`, `tests/test_cli.py` TestLegacyDuplexWarningReachesLogFile) reset `logging.getLogger("saneless")` level

No framework install needed.

---

## Manual-Only Verifications

All phase behaviors have automated verification. The real Docker directory-mount EBUSY/success behaviour is covered by the monkeypatched EBUSY unit test plus the optional `unshare` bind-mount test; running the actual container is not required.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s (quick)
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
