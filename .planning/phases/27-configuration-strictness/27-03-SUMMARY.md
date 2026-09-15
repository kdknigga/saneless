---
phase: 27-configuration-strictness
plan: 03
subsystem: config
tags: [pydantic, pydantic-settings, validation, error-rendering, environment, logging]
requires:
  - "27-01 (SecretStr token, LogLevel Literal, default_title alias)"
provides:
  - "extra='forbid' on ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig (populate_by_name kept) and explicitly on Settings"
  - "_render_error_lines(errors, env_data): value-free one-line-per-error renderer with difflib hints and valid-key lists"
  - "Environment attribution (D-12) and unknown SANELESS_* rejection (D-13) in _build_settings"
  - "load_settings: explicit path expanduser + is_file() (CFG-02); discovery uses is_file()"
  - "env_sourced_keys() -> list[str] and log_config_sources(settings) -> None (CFG-11), exported"
affects:
  - "27-06 (cli.py must print ConfigError text without doubling 'Configuration error', and call log_config_sources after configure_logging)"
  - "27-05 docs (environment-variables.md: unknown SANELESS_* now rejected)"
tech-stack:
  added: []
  patterns:
    - "EnvSettingsSource(Settings)() as the single oracle for env contributions"
    - "Render ValidationError from loc/type/msg only; raise ConfigError(msg) from None outside the except block"
    - "User-controlled names escaped via repr (quoted with !r, or repr()[1:-1] inside brackets)"
key-files:
  created: []
  modified:
    - src/saneless/config.py
    - tests/test_config.py
decisions:
  - "Env-attributed generic errors read 'environment variable NAME: <key path> in [section]: <msg>'; unknown keys read 'environment variable NAME: unknown key K in [section] ...'"
  - "_section_owning also recognises ProfileConfig keys and names them '[profiles.<name>]' (e.g. top-level resolution = 300)"
  - "An unknown top-level name that is not a section or key gets both a difflib section hint and [profiles.X], plus the valid-sections list"
  - "The SettingsError line is 'environment: <message>' (field and source only); the variable is not guessed from the message"
  - "Final error lines (TOML, env-attributed, unknown-env, SettingsError) are sorted together under one header"
  - "_ENV_PREFIX / _ENV_DELIMITER constants now feed both SettingsConfigDict and the unknown-env scan"
requirements: [CFG-01, CFG-02, CFG-05, CFG-11]
metrics:
  duration: "~35 min"
  completed: 2026-09-15
  tasks: 2
  files: 2
---

# Phase 27 Plan 03: Strict config loading with value-free, attributed error rendering Summary

A wrong config now fails at load and names the right place without echoing a value. Every model forbids unknown keys, and all pydantic errors are rendered as one `[section] key` line each under a header naming the file. An error whose value came from a `SANELESS_*` variable names that variable, and an unknown `SANELESS_*` name is rejected. A missing or non-file `--config` is a named `ConfigError`, and two new functions report which file and which environment keys were used.

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | Nested forbid, the loc/msg renderer, and the CFG-02 load path | 5b3b8f2 | 25b7715 |
| 2 | Env attribution, unknown SANELESS_* rejection, SettingsError, CFG-11 functions | 4cb90f5 | 113e5ab |

### Task 1 (CFG-01, CFG-02, CFG-05; D-10, D-11, D-14)
- `model_config = ConfigDict(extra="forbid")` is set on the three plain sections. `ProfileConfig` uses `ConfigDict(extra="forbid", populate_by_name=True)`, with a comment explaining why the legacy-duplex before-validator is still valid, and `Settings` sets `extra="forbid"` explicitly.
- `_VALID_SECTIONS` is removed. Private helpers are added: `_SECTION_MODELS`, `_escape_name`, `_valid_keys` (alias or name), `_match_candidates` (names plus aliases), `_section_owning`, `_format_loc_path` (`default_tags[0]`), `_describe_unknown_key`, `_describe_unknown_top_level`, `_render_error`, and `_render_error_lines`.
- Rendered examples:
  - `[paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir`
  - `[paperless] unknown key 'web_port'; it belongs in [output]`
  - `unknown section 'Paperless' (did you mean [paperless]?)`
  - `unknown section 'default' (did you mean [profiles.default]?); valid sections: ...`
  - `[output] web_port: Input should be a valid integer, ...`
  - `[profiles]: Value error, A 'default' profile must be defined in config`
- `_build_settings` renders into a list, then runs `raise ConfigError(msg) from None` outside the `except` block, so no `__cause__` or `__context__` is attached. A TOML syntax error still propagates as `TOMLDecodeError` (a `ValueError`, left for Phase 28).
- `load_settings` expands `~` on an explicit path and requires `is_file()`; otherwise it raises `Config file not found or not a regular file: <path>`. Discovery skips directories.

### Task 2 (CFG-01 D-12/D-13, CFG-05, CFG-11)
- `_env_contribution()` returns `EnvSettingsSource(Settings)()`. `_env_variable_for(loc, env_data)` walks the case-folded contribution using the exact string elements of `loc`, then matches `SANELESS_` + the path joined by `__` against `os.environ` case-insensitively, trying the longest path first. A JSON-valued `SANELESS_OUTPUT` is found through the prefix.
- `_unknown_env_lines(environ)` checks each variable's first segment. If it names no section, the line gets a `SANELESS_<SECTION>__<REST>` hint when the segment starts with `<section>_`, otherwise a difflib hint, and always lists the valid sections.
- `SettingsError` from the env contribution becomes the line `environment: error parsing value for field "paperless" from source "EnvSettingsSource"`. It carries no value and is not chained.
- `env_sourced_keys()` returns sorted, escaped dotted leaves. `log_config_sources(settings)` logs a single `logger.info("Configuration: %s; from environment: %s", ...)`.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1515 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean (pyrefly: 0 errors, and its 4 warnings were already there in unrelated files)
- Task 1 selector `-k "unknown_key or wrong_section or renders_every_error or never_echoes or missing_config or not_a_file"`: 18 passed at Task 1 GREEN (at least 12 required)
- Task 2 selector `-k "attributes_env or unknown_env or config_sources"`: 20 passed (at least 10 required). The wider selector passed 28.
- Greps:
  - `extra="forbid"` appears 6 times (at least 5 required).
  - `_VALID_SECTIONS` is gone, and there are no `["input"]`/`["ctx"]` reads.
  - `from None` is in `_build_settings`.
  - `is_file()` appears twice.
  - `EnvSettingsSource(Settings)` appears once.
  - Both public signatures match exactly once.
  - No f-string is passed to `logger.*`.

## TDD Gate Compliance

Both tasks have a `test(27-03)` RED commit followed by a `feat(27-03)` GREEN commit. RED was confirmed each time: 20 failures for Task 1, and 16 for Task 2. The Task 2 tests use `config_mod.env_sourced_keys` attribute access, so the RED run failed per test instead of at collection.

## Deviations from Plan

**1. [Rule 3 - Blocking] Flipped `test_log_level_env_is_validated` to `ConfigError`**
- **Found during:** Task 1 RED
- **Issue:** The plan's list of tests to flip missed this one. It expected a raw `ValidationError` from `load_settings`, and every load-time validation error is now a `ConfigError`.
- **Fix:** It now expects `ConfigError` and keeps `match="log_level"`.
- **Commit:** 5b3b8f2

**2. Test adjustment: the valid-names test loads `sample_toml`**
- `SANELESS_PROFILES__RECEIPT__TITLE` with no file replaces the built-in `default` profile dict, which fails the default-profile check. The plan says "(with default profile present)", so all three accepted-name cases load `sample_toml`.

**3. `_section_owning` also covers profile keys**
- It returns `profiles.<name>` for a `ProfileConfig` key found in a plain section or at the top level, e.g. `[paperless] resolution`. This keeps to D-11 ("it belongs in") and does not change the shapes the tests expect.

**Acceptance grep note:** `grep -n "p.exists()"` still matches `tmp.exists()` in `validate_settings_dirs`. That is a substring hit on existing writability code, not on discovery. The discovery `p.exists()` is gone. The nearest-ancestor rewrite of `validate_settings_dirs` belongs to the CFG-03 plan.

## Known Stubs

None.

## Threat Flags

None. All touched surface is in the plan's threat model:
- T-27-token: loc/msg only, `from None`, and tests on str, repr and the chain
- T-27-env: the unknown-env scan
- T-27-10: `SANELESS_CONFIG_PATH` is rejected
- T-27-11: names are escaped, tested with an embedded newline in a key and in a top-level name
- T-27-12: names only, with a caplog test
- T-27-13: `SettingsError` is wrapped with no chain, and a test checks the value is absent

## Self-Check: PASSED

- FOUND: src/saneless/config.py (log_config_sources, env_sourced_keys, _render_error_lines, _unknown_env_lines)
- FOUND: tests/test_config.py (TestUnknownKeyRendering, TestConfigErrorsNeverEchoValues, TestExplicitConfigPath, TestEnvironmentAttribution, TestUnknownEnvironmentVariables, TestConfigSources)
- FOUND commits: 5b3b8f2, 25b7715, 4cb90f5, 113e5ab
