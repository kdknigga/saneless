---
phase: 27-configuration-strictness
verified: 2026-09-15T16:26:23Z
status: passed
score: 16/16 must-haves verified
overrides_applied: 0
---

# Phase 27: Configuration Strictness Verification Report

**Phase Goal:** A wrong config is caught at load with a message that names the right place, and a
config rewrite is durable on the deployment the docs recommend — nested `extra="forbid"` with
full-`loc` error rendering, atomic UTF-8 comment-preserving writes, `~`/XDG expansion, validated
log level, `SecretStr` token — with the configuration reference and compose example updated
in-phase
**Verified:** 2026-09-15T16:26:23Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP Success Criteria, verified against real behaviour)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1a | A typo'd key under `[paperless]` is rejected at load, naming `paperless`, the bad key, and the valid keys | VERIFIED | Live CLI run: `saneless --config bad.toml jobs` with `tokne = "supersecrettoken12345"` → `Configuration error in bad.toml:\n  [paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir`, exit 2. Token value not echoed. |
| 1b | A `--config` path that does not exist exits 2 naming the path | VERIFIED | Live CLI run: `saneless --config /tmp/.../does-not-exist.toml jobs` → `Config file not found or not a regular file: /tmp/.../does-not-exist.toml`, exit 2. `config.py load_settings` uses `is_file()` not `exists()` (catches Docker's directory trap). |
| 2a | `~` and `$XDG_CONFIG_HOME`/`$XDG_STATE_HOME` honoured for config/data locations | VERIFIED | Live run with `XDG_CONFIG_HOME` set to a scratch dir and `HOME` faked: settings loaded `$XDG_CONFIG_HOME/saneless/config.toml`, and `data_dir = "~/scratch_data_dir_marker"` resolved to `$HOME/scratch_data_dir_marker`. `xdg_config_home()`/`xdg_state_home()` computed at call time (config.py:84-154), `config_search_paths()` uses XDG order. |
| 2b | An invalid `log_level` is rejected | VERIFIED | Live run: `[output] log_level = "TRACE"` → `Configuration error in badlevel.toml:\n  [output] log_level: Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'`, exit 2. |
| 2c | `-v` sets the effective level to DEBUG | VERIFIED | Live `configure_logging(..., verbose=True)` call: `logging.getLogger('saneless').getEffectiveLevel()` = DEBUG while root stays INFO (logging_config.py:80, matches CFG-04's documented scope). |
| 3 | `auto-profiles --force` succeeds against the documented directory mount, replaces only generated keys, leaves `default_tags` and hand-written profiles untouched | VERIFIED | `docker-compose.yml` mounts `./config:/etc/saneless` read-write (not `:ro`). Real-kernel evidence: `tests/test_atomic_write.py::TestRealBindMount` (4 tests) actually **ran** on this host (not skipped — `unshare` available) and passed, including `test_real_directory_bind_mount_replaces_the_host_file`, proving a real bind-mounted directory supports the atomic rename. Live scratch check: a profile with `auto_generated = "false"` (string) under `--force` was reported `Skipped (not auto-generated)` and the file was byte-for-byte unchanged (CR-01 fix confirmed live, not just by unit test). `tests/test_auto_profiles.py` has dedicated tests for `default_tags`/comment survival under `--force` (line ~1419) and hand-written-profile-unchanged (line ~1531). |
| 4a | The Paperless token never appears in `repr(settings)`, logs, or error messages | VERIFIED | Live: `repr(Settings(paperless=PaperlessConfig(token='supersecrettoken12345',...)))` → `token=SecretStr('**********')`. `_render_error_lines` (config.py:868-888) reads only `loc`/`type`/`msg` from pydantic errors, never `input`/`ctx`; `ConfigError` is raised `from None` to avoid a chained `ValidationError` printing the input. |
| 4b | Loaded config path and env-sourced keys logged at INFO on startup | VERIFIED | Live: `-v jobs` run logged `INFO saneless.config Configuration: <path>; from environment: (none)`. `log_config_sources`/`env_sourced_keys` (config.py:1190-1227) implement this, called from `cli.py _load_cli_settings` after `configure_logging`. |
| 5a | `saneless <subcommand> --help` works with no valid configuration file | VERIFIED | Live: `saneless --config /nonexistent/bad.toml scan --help` → prints help, exit 0, no config load attempted (`cli()` group callback defers loading to `_load_cli_settings`, called only inside each command, never for `--help`/`resilient_parsing`). |
| 5b | A blank title falls back to the profile's documented `title` key | VERIFIED | `resolve_job_title` (config.py:308-332) is the single shared rule used by both `cli.py:272` (scan) and `web/routes.py:413`. `tests/test_web.py::test_scan_blank_title_uses_profile_title` and CLI equivalents pass. `ProfileConfig.default_title` (alias `title`) is a literal string field, not a template (D-15). |

**Score:** 9/9 roadmap success-criteria clauses verified (16/16 counting CFG-01..CFG-11 sub-claims below)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | Nested `extra="forbid"`, error renderer, XDG, `~`, `SecretStr`, `log_level` validation, CFG-11 logging | VERIFIED | All present: `ScannerConfig`/`PaperlessConfig`/`OutputConfig`/`ProfileConfig` all have `model_config = ConfigDict(extra="forbid")` (lines 213, 224, 255, 339); `Settings.model_config` sets `extra="forbid"` explicitly (line 460); `PaperlessConfig.token: SecretStr` (line 230); `_normalise_log_level` + `LogLevel` Literal (lines 76-81, 388-410); `_xdg_base`/`xdg_config_home`/`xdg_state_home` (84-154); `_expand_user` with `~user` RuntimeError→ValueError translation (157-185, WR-03 fix); `_render_error`/`_render_error_lines`/`_env_variable_for` (D-10..D-14, WR-04 fix); `env_sourced_keys`/`log_config_sources` (CFG-11). |
| `src/saneless/atomic_write.py` | Temp file, fsync, rename, UTF-8, mode/owner preservation, EBUSY/read-only mount handling | VERIFIED | `replace_file_atomically` (187-291): `tempfile.mkstemp` in target's own directory, `_copy_owner_and_mode` (chown before chmod, refused errnos skipped per WR-02 fix), UTF-8 bytes write, `fsync` before rename, `Path.replace`, EBUSY → `ConfigError` (D-08), `_read_only_mount_error` for the legacy `:ro` case (WR-01 fix), temp file removed in `finally` on every failure path including `KeyboardInterrupt`. |
| `src/saneless/auto_profiles.py` | Merge semantics (D-01..D-04), CRLF-safe, round-trip-checked rewrite | VERIFIED | `_is_auto_generated` uses `TypeAdapter(bool)` matching the loader's lax bool (CR-01 fix, confirmed live above); `_merge_profile` writes owned keys onto the existing table, deletes stale owned keys (D-02/D-03); `write_profiles_to_config` groups results (added/refreshed/skipped/removed, D-04); `_render_checked` re-parses and compares to `doc.unwrap()` before replacing (CR-02 fix); CRLF normalisation only when the whole file is CRLF (WR-08 fix). |
| `src/saneless/cli.py` | Lazy settings load for `--help`, `-v`, CFG-11 line, `--title` optional | VERIFIED | `cli()` group callback stores `config_path`/`verbose` only, no load (line 182-190); `_load_cli_settings` (193-246) is the sole load point, called per-command; scan's `--title` has `default=""` and is optional (line 256-258); `write_profiles_to_config` and grouped CLI output wired (D-04). |
| `docker-compose.yml` | `./config:/etc/saneless` read-write directory mount | VERIFIED | Line 22: `- ./config:/etc/saneless` — no `:ro`, with comments explaining the atomic-write requirement and the missing-file behaviour. |
| Docs: `configuration.md`, `deploy-docker-compose.md`, `docker.md`, `cli-commands.md`, `cli-scripting.md`, `empty-page-detection.md`, `environment-variables.md`, `configure-scan-profiles.md` | Corrected in-phase for every sentence this phase makes true | VERIFIED | No "template" wording remains for `title`; no "container will fail to start" claim; XDG wording present in configuration.md (lines 11, 60-61); `-v`/`--help`/exit-2 documented in cli-commands.md; unknown-env-var rejection documented in environment-variables.md; deploy-docker-compose.md's WR-06 correction names `/saneless.toml` (not `/app/saneless.toml`) as the in-image write target, matching the actual `WORKDIR`-less runtime image. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `cli.py scan` / `web/routes.py` | `config.py resolve_job_title` | shared function call | WIRED | Both call sites (`cli.py:272`, `web/routes.py:413`) use the same function; no duplicated title logic. |
| `cli.py scan` / `web/app.py create_app` | `PaperlessConfig.token` | `.get_secret_value()` | WIRED | Only two unwrap sites in the codebase (`cli.py:289`, `web/app.py:62`), matching the CONTEXT.md decision. |
| `cli.py auto_profiles` | `auto_profiles.write_profiles_to_config` | direct call, `ConfigError`/`OSError` → exit 2 | WIRED | `cli.py:581-594` catches both and exits 2 with the message, no traceback. |
| `worker.py` | `auto_profiles.ProfileWriteResult` | structured result consumption | WIRED | `worker.py` imports and type-annotates against `ProfileWriteResult` (lines 48, 124, 803), using the same added/refreshed/skipped/removed vocabulary as the CLI. |
| `_load_cli_settings` | `log_config_sources` / `warn_on_legacy_duplex_sources` | called after `configure_logging` | WIRED | `cli.py:240-243`, confirmed live: the CFG-11 line appeared in the log/stderr output of a real `-v jobs` run. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Typo'd `[paperless]` key rejected, exit 2, no leak | `saneless --config bad.toml jobs` | `Configuration error in bad.toml:\n  [paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir`, exit 2 | PASS |
| Missing `--config` path exits 2 naming path | `saneless --config /tmp/.../does-not-exist.toml jobs` | `Config file not found or not a regular file: ...`, exit 2 | PASS |
| Invalid `log_level` rejected | `saneless --config badlevel.toml jobs` (log_level="TRACE") | `[output] log_level: Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'`, exit 2 | PASS |
| `$XDG_CONFIG_HOME` honoured, `~` expanded | Python: `load_settings()` with `XDG_CONFIG_HOME`/`HOME` overridden | Loaded the XDG-located file; `data_dir` resolved through `HOME` | PASS |
| `-v` sets `saneless` logger to DEBUG, root stays INFO | Python: `configure_logging(..., verbose=True)` | `saneless` effective level DEBUG, root level INFO | PASS |
| `--help` works with an unloadable config | `saneless --config /nonexistent/bad.toml scan --help` | Help text printed, exit 0 | PASS |
| Token masked in `repr` | Python: `repr(Settings(paperless=PaperlessConfig(token=...)))` | `token=SecretStr('**********')` | PASS |
| CR-01 fix: string `auto_generated = "false"` treated as hand-written under `--force` | Python: `write_profiles_to_config(cfg, {'mine': ...}, force=True)` | `Skipped (not auto-generated): 'mine' ...`; file byte-for-byte unchanged | PASS |

### Probe Execution

| Probe | Command | Result | Status |
|-------|---------|--------|--------|
| `tests/test_atomic_write.py::TestRealBindMount` (real-kernel, unprivileged user+mount namespace) | `uv run pytest tests/test_atomic_write.py -k RealBindMount -v` | 4 passed (0 deselected-by-skip; `unshare` available on this host), including the directory-mount success case and both read-only-mount EBUSY/EROFS cases | PASS — ran, did not skip |
| Targeted phase test suite | `uv run pytest tests/test_config.py tests/test_cli.py tests/test_auto_profiles.py tests/test_atomic_write.py tests/test_logging.py tests/test_deployment_config.py -q -m "not browser and not sane_hardware"` | 485 passed | PASS |
| Ruff on touched modules | `uv run ruff check src/saneless/config.py src/saneless/atomic_write.py src/saneless/auto_profiles.py src/saneless/cli.py src/saneless/logging_config.py` | All checks passed | PASS |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|-------------|-----------------|-------------|--------|----------|
| CFG-01 | 27-03, 27-08 | Unknown keys rejected at load, naming section/key/valid keys | SATISFIED | Live spot-check + `_describe_unknown_key`/`_describe_unknown_top_level` (config.py) |
| CFG-02 | 27-03, 27-06, 27-08 | `--config` missing path exits 2 naming path | SATISFIED | Live spot-check; `is_file()` check; `--config ""` also handled (WR-05 fix) |
| CFG-03 | 27-05, 27-07, 27-08 | `~` expansion, XDG honoured | SATISFIED | Live spot-check; `_expand_user`, `xdg_config_home`/`xdg_state_home` |
| CFG-04 | 27-01, 27-06, 27-08 | `log_level` validated, `-v` → DEBUG | SATISFIED | Live spot-checks (both) |
| CFG-05 | 27-01, 27-03, 27-06, 27-08 | `SecretStr` token, never leaked | SATISFIED | Live `repr()` check; `_render_error_lines` never touches `input`/`ctx` |
| CFG-06 | 27-01, 27-05, 27-06, 27-08 | `title` key sets default job title | SATISFIED | `resolve_job_title` shared rule; `default_title` literal field; test coverage in test_web.py |
| CFG-07 | 27-04, 27-05, 27-08 | `--force` merges, preserves `default_tags`, never touches non-owned profiles | SATISFIED | Live CR-01 spot-check; `_merge_profile`/`_is_auto_generated`; dedicated tests |
| CFG-08 | 27-02, 27-04, 27-08 | Atomic, UTF-8, mode-preserving rewrites | SATISFIED | `atomic_write.py` full read; real-kernel bind-mount tests ran and passed |
| CFG-09 | 27-02, 27-05, 27-08 | Compose/docs mount the config directory | SATISFIED | `docker-compose.yml` line 22 (`./config:/etc/saneless`, no `:ro`); docs updated |
| CFG-10 | 27-06, 27-08 | `--help` needs no valid config | SATISFIED | Live spot-check |
| CFG-11 | 27-03, 27-06, 27-08 | Loaded path + env-sourced keys logged at INFO | SATISFIED | Live spot-check; `log_config_sources`/`env_sourced_keys` |

No orphaned requirements: all CFG-01..CFG-11 IDs appear in at least one plan's `requirements:` frontmatter and are cross-referenced above; REQUIREMENTS.md lists the same 11 IDs for Phase 27 (status column shows "Pending" — a tracking-checkbox update, not evidence of missing implementation; every ID has direct code/test/behavioural evidence above).

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | No `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` found in `config.py`, `atomic_write.py`, `auto_profiles.py`, `cli.py`, `logging_config.py`, `web/routes.py`, `web/app.py`, `worker.py` | — | — |

### Code Review Findings (27-REVIEW.md / 27-REVIEW-FIX.md)

All 10 in-scope findings (2 critical: CR-01, CR-02; 8 warning: WR-01..WR-08) from the post-execution
code review were fixed in dedicated commits (`b85a257` through `548d939`), each with a regression
test except the two pure-doc/docstring fixes (WR-06, WR-07). This verifier independently reproduced
CR-01 live (string `auto_generated` correctly treated as hand-written) and confirmed the real-kernel
`TestRealBindMount` suite (covering WR-01's read-only-mount fix and D-08's EBUSY fix) actually ran
on this host rather than being silently skipped.

### Human Verification Required

None. Every success criterion had either a live CLI/Python behavioural reproduction in this
verification pass, a real-kernel filesystem test that actually executed (not skipped) on this host,
or direct code inspection of the wiring with corroborating unit tests. No visual/UI-only claims are
part of this phase's success criteria (config/CLI/filesystem behaviour only).

### Gaps Summary

No gaps found. All 11 CFG requirements, all 5 roadmap success criteria (decomposed into 9 checkable
clauses), and all 10 in-scope code-review findings have direct, reproduced evidence in the actual
codebase and its running behaviour — not merely SUMMARY.md narrative.

---

_Verified: 2026-09-15T16:26:23Z_
_Verifier: Claude (gsd-verifier)_
