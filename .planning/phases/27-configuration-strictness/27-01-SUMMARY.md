---
phase: 27-configuration-strictness
plan: 01
subsystem: config
tags: [pydantic, secretstr, logging, web, title]
requires: []
provides:
  - "PaperlessConfig.token as SecretStr (unwrapped only in cli.py scan and web/app.py)"
  - "LogLevel Literal alias and OutputConfig._normalise_log_level before-validator"
  - "ProfileConfig.default_title (alias title, max_length=TITLE_MAX_LENGTH)"
  - "resolve_job_title(typed, profile, *, now) in saneless.config"
affects:
  - "27-03 (error renderer renders literal_error / string_too_long raised here)"
  - "27-06 (cli.py scan title handling and -v/log_level wiring build on these types)"
tech-stack:
  added: []
  patterns:
    - "pydantic SecretStr for credentials, unwrapped at construction sites only"
    - "Literal + mode='before' field_validator for case-insensitive enum-like config"
    - "Pure shared rule function in config.py consumed by front ends"
key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/cli.py
    - src/saneless/web/app.py
    - src/saneless/web/routes.py
    - tests/test_config.py
    - tests/test_web.py
    - tests/test_outcomes_e2e.py
decisions:
  - "log_level accepts 'warn' as an alias of WARNING (logging.getLevelNamesMapping lists WARN)"
  - "LogLevel is a plain module-level Literal assignment, not a PEP 695 type statement, matching the plan's grep and avoiding TypeAliasType handling in pydantic"
  - "start_scan replaces has_profile + fallback with one worker.get_profile lookup under the profile lock"
requirements: [CFG-04, CFG-05, CFG-06]
metrics:
  duration: "~6 min"
  completed: 2026-09-15
  tasks: 2
  files: 7
---

# Phase 27 Plan 01: SecretStr token, validated log_level, and shared title rule Summary

The Paperless token is now a pydantic `SecretStr`, masked in `repr` and JSON dumps and unwrapped only where `PaperlessClient` is built. `output.log_level` is a validated `Literal` that trims input, ignores case, and reads `warn` as `WARNING`. The profile `title` key, which used to do nothing, is now a bounded literal `default_title`. The new `resolve_job_title` rule applies it in `POST /api/scan`.

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | SecretStr token and validated log_level | cfd4077 | 933498e |
| 2 | Literal default_title and the shared title rule, wired into the web route | 4dc9606 | 5563208 |

### Task 1 (CFG-04 validation half, CFG-05)
- `PaperlessConfig.token: SecretStr = SecretStr("")`, with a comment naming the two unwrap sites.
- `LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]` is exported in `__all__`, and `OutputConfig.log_level: LogLevel = "INFO"`.
- `_normalise_log_level` (`mode="before"`) strips and upper-cases string input and maps `WARN` to `WARNING`. Input that is not a string is passed through unchanged so pydantic rejects it.
- `get_secret_value()` is called in `cli.py` scan and `web/app.py` create_app, and at the two `tests/test_outcomes_e2e.py` construction sites. `PaperlessClient` is unchanged.
- Tests: `TestSecretToken` (4 tests) and `TestLogLevelValidation` (9 tests, including an env-var TRACE rejection). The two existing token assertions now compare `.get_secret_value()`.

### Task 2 (CFG-06 config + web half; D-15, D-16, D-17)
- `default_title_template` is renamed to `default_title: str = Field(default="", alias="title", max_length=TITLE_MAX_LENGTH)`. `TITLE_MAX_LENGTH` is imported from `saneless.vocabulary`, which creates no import cycle.
- `resolve_job_title(typed, profile, *, now)` sits next to `ProfileConfig` and is exported. The rule: a typed title that is non-blank after stripping, else the profile title if non-blank, else `Scan <UTC YYYY-MM-DD HH:MM>`. Titles are returned as given, not stripped.
- `web/routes.py start_scan`: `found = state.worker.get_profile(profile)`. If that is `None` the request is rejected with `UNKNOWN_PROFILE`; otherwise `title = resolve_job_title(title, found, now=datetime.now(tz=UTC))`. The Args docstring is updated. No template was touched.
- Tests: alias and env tests renamed to `default_title`, plus max-length reject/accept tests and `TestResolveJobTitle` (8 cases). In `test_web.py`, a local `titled_client` fixture (profile `title="Receipt"`, `worker.submit` stubbed to ACCEPTED) backs blank/whitespace -> "Receipt", typed -> "Typed", and an unknown profile -> 422 with no row written.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1460 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean (0 errors; pyrefly's 4 warnings were already there, in unrelated files)
- Acceptance greps: `token: SecretStr` x1; the `LogLevel = Literal[...]` line x1; `get_secret_value()` in src/saneless on exactly 2 lines (cli.py, web/app.py); `default_title_template` nowhere in src/tests; `has_profile` absent from routes.py; `resolve_job_title(title, found` x1; no 27-01 commit touches `src/saneless/web/templates`
- `-k "secret or log_level"` selects 13 tests; `-k title` over test_config + test_web selects 17 tests. All pass.

## TDD Gate Compliance

Both tasks have a `test(27-01)` RED commit followed by a `feat(27-01)` GREEN commit. The RED failures were confirmed before implementing: AttributeError/assertion failures for Task 1, and an ImportError for `resolve_job_title` plus blank-title assertion failures for Task 2. In Task 2, two of the four new web tests (typed title wins, unknown profile rejected) passed at RED. That is expected, because they guard behaviour that already existed and must survive the rewrite.

## Deviations from Plan

**1. [Rule 3 - Blocking] Reworded the SecretStr comment in config.py**
- **Found during:** Task 1
- **Issue:** The comment first read `get_secret_value()`, which would have made the acceptance grep count 3 lines instead of 2.
- **Fix:** The comment now says `get_secret_value` without parentheses.
- **Commit:** 933498e

**2. Test construction uses `model_validate` for invalid log levels**
- `OutputConfig.model_validate({"log_level": ...})` is used instead of `OutputConfig(log_level="warn")`. Passing a non-Literal string or an int to the typed constructor would be flagged by ty/pyrefly in `tests/`. The behaviour under test is the same.

Otherwise the plan was executed as written.

## Known Stubs

None.

## Threat Flags

None. All surface touched is already in the plan's threat model: T-27-token, T-27-01, T-27-02 and T-27-04 are mitigated as specified.

## Self-Check: PASSED

- FOUND: src/saneless/config.py (resolve_job_title, SecretStr, LogLevel)
- FOUND: src/saneless/web/routes.py (resolve_job_title wiring)
- FOUND commits: cfd4077, 933498e, 4dc9606, 5563208
