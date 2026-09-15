---
phase: 27-configuration-strictness
plan: 07
subsystem: config
tags: [xdg, pydantic, default-factory, expanduser, writability]
requires:
  - "27-03 (extra=forbid, CFG-02 expanduser on --config, discovery with is_file)"
provides:
  - "xdg_config_home() -> Path and xdg_state_home() -> Path, exported, read at call time"
  - "config_search_paths(): ./saneless.toml, $XDG_CONFIG_HOME/saneless/config.toml, /etc/saneless/config.toml"
  - "OutputConfig.data_dir/log_file defaults via Field(default_factory) under $XDG_STATE_HOME/saneless"
  - "Settings.scanner/paperless/output use Field(default_factory=...)"
  - "~ expansion (not $VAR) on tmp_dir, data_dir, log_file, consume_dir"
  - "_nearest_existing_ancestor and _require_writable behind validate_settings_dirs"
  - "tests/conftest.py clean_env removes XDG_CONFIG_HOME and XDG_STATE_HOME"
affects:
  - "27-08 docs (XDG wording now matches code)"
  - "tests/test_logging.py xdg_compliant default test (still passes with XDG unset)"
tech-stack:
  added: []
  patterns:
    - "Call-time defaults via Field(default_factory=...) on nested sections, never an import-time instance"
    - "One shared module-level _expand_user helper called by per-model after-validators"
key-files:
  created: []
  modified:
    - src/saneless/config.py
    - tests/conftest.py
    - tests/test_config.py
decisions:
  - "data_dir/log_file default factories are private module functions (_default_data_dir, _default_log_file) rather than lambdas, for docstrings and line length"
  - "A missing directory is reported as '<label> parent is not writable: <nearest existing ancestor>'; an existing one as '<label> is not writable: <dir>'"
  - "~ expansion lives in one private _expand_user function called by OutputConfig._expand_paths and PaperlessConfig._expand_consume_dir"
requirements: [CFG-03]
metrics:
  duration: "~25 min"
  completed: 2026-09-15
  tasks: 2
  files: 3
---

# Phase 27 Plan 07: XDG base directories, ~ expansion and nearest-ancestor writability Summary

Config discovery now looks in `$XDG_CONFIG_HOME/saneless/config.toml`. The `data_dir` and `log_file` defaults now live under `$XDG_STATE_HOME/saneless`. Both are read when settings are built, not when the module is imported. A leading `~` in any path setting is expanded. A deep missing directory under an unwritable ancestor now fails at startup.

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | XDG helpers, call-time state defaults, XDG search path | eced065 | 91f7932 |
| 2 | ~ expansion on path settings, nearest-existing-ancestor writability | 93e42d4 | d0b9879 |

### Task 1 (CFG-03, Pitfall 3)
- `_xdg_base(variable, *fallback)` uses a value only when it is non-empty and absolute; otherwise it falls back to `Path.home().joinpath(*fallback)`. It is exposed as `xdg_config_home()` and `xdg_state_home()`, both in `__all__`. No `platformdirs`.
- The `OutputConfig.data_dir` and `log_file` defaults use `Field(default_factory=_default_data_dir / _default_log_file)`.
- `Settings.scanner`, `paperless` and `output` use `Field(default_factory=...)`. A comment explains that a plain `OutputConfig()` default would freeze HOME/XDG at import.
- The second entry of `config_search_paths()` is `xdg_config_home() / "saneless" / "config.toml"`.
- `clean_env` now also removes `XDG_CONFIG_HOME` and `XDG_STATE_HOME`, with an updated docstring.
- Tests (`TestXdgBaseDirectories`, 13 selected by `-k xdg`) cover:
  - unset, absolute, empty and relative values, for both variables
  - the XDG search path
  - loading a config from under `$XDG_CONFIG_HOME`
  - `XDG_STATE_HOME` set after import moving `Settings().output` and `OutputConfig()` defaults
  - a HOME change after import moving the defaults
  - the conftest hygiene assertion
- `test_config_search_paths_order` was rewritten to expect `xdg_config_home()`.

### Task 2 (CFG-03, M-20)
- Private `_expand_user(value)` returns `str(Path(value).expanduser())` for a non-empty value and returns empty values unchanged. It never calls `expandvars`.
- Two after-validators use it:
  - `OutputConfig._expand_paths` on `tmp_dir`, `data_dir` and `log_file`
  - `PaperlessConfig._expand_consume_dir` on `consume_dir`
- `_nearest_existing_ancestor(path)` walks `(path, *path.parents)`. `_require_writable(label, directory)` checks `os.access(ancestor, W_OK)`.
- `validate_settings_dirs` now makes three calls, the last one only when `consume_dir` is set. The triplicated `exists()`/`parent.exists()` code is gone, and the `not writable` wording is kept.
- Tests:
  - `TestPathExpansion` (6) covers constructors, TOML, `SANELESS_OUTPUT__LOG_FILE`, empty `consume_dir`, and literal `$HOME/x`.
  - Five tests were added to `TestValidateSettingsDirs`: the deep missing path under an unwritable ancestor for each of the three settings (skipped as root, chmod restored in `finally`, message names the ancestor), a deep missing path under a writable ancestor that passes, and a unit test for `_nearest_existing_ancestor`.

## Verification

- Non-browser suite (`uv run pytest -m "not browser and not sane_hardware" -q`): 1574 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean. pyrefly reported 0 errors, and its 4 warnings were already there in unrelated files.
- Test selectors:
  - `-k "xdg or search_path"`: 17 passed
  - `-k xdg`: 13 passed (at least 8 required)
  - `-k "expanduser or ancestor"`: 11 passed (at least 5 required)
  - `-k "xdg or expanduser or ancestor or validate"`: 30 passed
- Acceptance greps:
  - Each of these matched once: `def xdg_config_home() -> Path`, `def xdg_state_home() -> Path`, `output: OutputConfig = Field(default_factory=OutputConfig)`, `def _nearest_existing_ancestor`.
  - No matches for `Path.home() / ".local"`, `Path.home() / ".config"` or `expandvars`, and no `platformdirs` in `pyproject.toml` or `src`.
  - `parent.exists()` count is 0.
  - Both `XDG_CONFIG_HOME` and `XDG_STATE_HOME` appear in `tests/conftest.py`.

## TDD Gate Compliance

Both tasks have a `test(27-07)` RED commit followed by a `feat(27-07)` GREEN commit. RED was confirmed each time:
- **Task 1:** 13 failed. `test_xdg_variables_are_removed_before_each_test` passed at RED because the conftest change is test infrastructure committed with RED, as the plan directs.
- **Task 2:** 8 failed.

## Deviations from Plan

**1. Default factories are named functions, not lambdas**
- The plan allowed `_default_data_dir()` / `_default_log_file()` as a fallback. I used them from the start: they can carry docstrings, and they keep the lines under 88 characters. Behaviour is identical.

**2. `expanduser()` sits in a shared helper, not directly inside a validator body**
- The plan explicitly allows "a shared module-level private function both call". Both field validators delegate to `_expand_user`, so the acceptance grep matches in the helper.

**3. XDG tests parametrise a small `_XdgBase` dataclass**
- Parametrising variable, function and fallback separately tripped ruff PLR0913 (too many arguments). One dataclass param keeps each test at five arguments or fewer, without a suppression.

## Known Stubs

None.

## Threat Flags

None. The touched surface is covered by the plan's threat model:
- T-27-25: relative or empty XDG values are ignored, and tested.
- T-27-26: only `~` is expanded, and a test checks that `$HOME/x` stays literal.
- T-27-27: accepted.
- T-27-28: the nearest-ancestor check.

## Self-Check: PASSED

- FOUND: src/saneless/config.py (xdg_config_home, xdg_state_home, _expand_user, _nearest_existing_ancestor, _require_writable)
- FOUND: tests/conftest.py (XDG_CONFIG_HOME, XDG_STATE_HOME in clean_env)
- FOUND: tests/test_config.py (TestXdgBaseDirectories, TestPathExpansion, ancestor tests)
- FOUND commits: eced065, 91f7932, 93e42d4, d0b9879
