# Phase 27: Configuration Strictness - Research

**Researched:** 2026-09-15
**Domain:** pydantic / pydantic-settings validation and error rendering, durable config-file rewrites (tomlkit + POSIX atomic replace), Click lazy loading, stdlib logging, XDG basedir
**Confidence:** HIGH. Every load-bearing behaviour below was run against the project's own locked library versions in this session. The few `[ASSUMED]` items are listed in the Assumptions Log.

## Summary

The phase needs no new dependencies. Everything it asks for is already in the lock file (pydantic 2.12.5, pydantic-settings 2.13.1, tomlkit 0.14.0, click 8.3.1, uvicorn 0.42.0) or in the stdlib (`difflib`, `tempfile`, `os`, `stat`, `errno`, `logging`). The hard part is not the libraries. It is seven behaviours that are easy to get wrong and that the probes confirmed:

1. **`str(ValidationError)` contains the raw input value.** A mistyped `tokne = "…"` puts the token in the exception text. The renderer must build its lines from `exc.errors()` using `loc` and `msg` only. It must also raise `ConfigError(...) from None`, because a chained `__cause__` prints the token in any traceback.
2. **Click runs the group callback before it parses the subcommand's `--help`, and `ctx.resilient_parsing` is `False` there.** Of the two mechanisms CONTEXT offers for CFG-10, "return early under `resilient_parsing`" does not work. Settings have to load lazily, per command.
3. **`output: OutputConfig = OutputConfig()` fixes the default paths at import time.** A `default_factory` on `data_dir`/`log_file` alone is not enough for "computed at call time". The `Settings.output` field itself needs `Field(default_factory=OutputConfig)`.
4. **`os.replace` over a single-file bind mount fails with `EBUSY`, and over a directory bind mount it succeeds.** Both were verified on this kernel with `unshare --user --map-root-user --mount` plus `mount --bind`. That is D-08's failure and D-09's fix, reproduced without Docker.
5. **`Path.read_text()` rewrites CRLF to LF** through universal newlines. To keep the user's line endings (D-05), read with `read_bytes().decode("utf-8")` and write bytes.
6. **pydantic-settings already computes the environment's contribution.** `EnvSettingsSource(Settings)()` returns the nested dict of env-sourced values, using pydantic-settings' own case-folding, `__` splitting and JSON rules. That dict is the right oracle for the CFG-11 key list and for D-12 attribution. Unknown top-level `SANELESS_*` names never reach it, so D-13 needs a separate `os.environ` scan.
7. **An inline `profiles = { … }` section plus `tomlkit.table()` produces invalid TOML.** That bug is already present in today's writer. Re-parse the dumped text with `tomllib.loads` before any replace, so a bad write can never reach disk.

**Primary recommendation:** Build four small units, each one TDD-testable on its own:
- a pure error renderer in `config.py`
- an atomic replace helper (new module) that returns and raises typed results
- a merge-aware `write_profiles_to_config` that returns a frozen `ProfileWriteResult`
- a memoised `_load_cli_settings(ctx)` helper in `cli.py` that every command calls

Wire docs and compose in the same plan wave as the atomic writer (CFG-08 and CFG-09 must ship together).

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### `auto-profiles --force` merge (CFG-07)
- **D-01: A profile saneless did not create is never touched, and it is reported, not silently
  skipped.** A same-named profile without a truthy `auto_generated` is left byte-for-byte alone,
  under `--force` too. The command names it and says why, e.g.
  `Skipped 'default': not created by auto-profiles (no auto_generated = true); rename or delete it to regenerate.`
  There is **no** stronger flag to override this. The way to hand a profile back to the tool is to
  delete or rename it.
- **D-02: In an `auto_generated` profile, the generated keys belong to the tool, and `--force`
  overwrites them in place.** The owned key set is fixed:
  `source, resolution, mode, auto_source_mode, duplex, auto_generated`. Under `--force` those keys
  are written onto the *existing* tomlkit table (`table[key] = value`), never a fresh
  `tomlkit.table()` assigned over it. Every other key in the table (`default_tags`,
  `default_correspondent`, `title`, `paper_size`, thresholds, `enable_empty_page_detection`) and the
  table's comments survive. A hand-edited owned key, e.g. `resolution = 600`, **is** overwritten:
  while the flag is set, the tool owns those keys. To keep a hand edit, remove `auto_generated`.
- **D-03: An owned key the fresh generation does not write is deleted from the table.** Example:
  the file has `duplex = "hardware"`, and regeneration yields `duplex = "none"`, which is omitted
  by the Phase 25 D-06 "write only when non-default" rule. The refreshed table must read the way a
  fresh generation would, so the stale key is removed. This keeps the "omit defaults" convention
  instead of writing `duplex = "none"` / `auto_source_mode = "flatbed"` explicitly.
- **D-04: Results are reported grouped by action**, with a line printed only when it is non-empty:
  `Added: …` / `Refreshed: …` / `Skipped (not auto-generated): …` /
  `Removed (scanner no longer offers it): …`. `write_profiles_to_config` therefore has to return
  a structured result (added / refreshed / skipped / removed) instead of today's `list[str]` of
  written names. The worker's startup auto-generation log (`worker.py` `_persist_generated_profiles`)
  uses the same vocabulary.
  - Without `--force`, an existing flagged profile is still skipped as today. Planner decides
    whether that also appears under "Skipped" with a different reason ("already exists; use
    --force to refresh"). Keep today's hint text in spirit.
  - The Phase 24 D-16 orphan prune and the `_UNPRUNABLE = {"default"}` guard are unchanged. Pruned
    names now surface in the CLI output as "Removed", not only in the log.
- Fix the doc sentence at `docs/how-to/configure-scan-profiles.md` (doc row 8) to describe merge
  semantics, and add a test asserting `default_tags` and a comment survive `--force`, plus a test
  that a hand-written same-name profile is unchanged.

#### Atomic rewrites and the Docker mount (CFG-08, CFG-09)
- **D-05: Write the temp file in the target's own directory, then `fsync` it and `os.replace` it,
  with UTF-8 explicit on both read and write.** `newline` handling must not rewrite the user's line
  endings. tomlkit round-tripping keeps comments. The temp file is removed on every failure path.
  Fsyncing the directory after the rename is at the planner's discretion (recommended).
- **D-06: The new file gets the old file's mode and owner.** Copy `st_mode` permission bits onto
  the temp file, and `fchown` it to the original `st_uid`/`st_gid` *when the process is permitted*
  (root in the container). When chown is not permitted (non-root on bare metal, where the owner is
  already the writer), skip it silently. Otherwise a host-owned `config.toml` becomes root-owned
  after the first container rewrite and the user needs sudo to edit it.
- **D-07: Symlinks are written through, not replaced.** Resolve the config path to its real target,
  create the temp file beside the real file, and replace the real file. The symlink keeps working
  (dotfiles repos). When they differ, the log line names both the link and the target.
- **D-08: A rename that fails with `EBUSY` (the legacy single-file bind mount) is a clear failure,
  with no non-atomic fallback.** Translate it to a `ConfigError` along the lines of
  `Cannot replace /etc/saneless/config.toml: it is bind-mounted as a single file. Mount its directory instead (see docs/how-to/deploy-docker-compose.md).`
  The CLI exits non-zero. The worker logs it and keeps the generated profiles in memory, per Phase
  26 D-17/D-18, which already catches `ConfigError`. No startup mount-point probe.
- **D-09: The recommended compose mount is a read-write directory: `./config:/etc/saneless`**
  (no `:ro`), holding `config.toml`. It must be writable, because the atomic temp file is created
  in that directory and success criterion 3 requires `auto-profiles --force` to succeed against the
  documented mount. The review's `:ro` suggestion is explicitly rejected. The compose comments and
  `docs/how-to/deploy-docker-compose.md` / `docs/reference/docker.md` state truthfully that a
  missing `config.toml` means defaults plus environment variables, and that the container still
  starts (doc row 20). The old "container will fail to start" sentence goes.
  - Carried forward (Phase 26 D-16..D-18): with an empty mounted directory, the server does **not**
    create `config.toml`. Generated profiles are then used in memory, and the INFO message tells
    the operator to create the file.

#### Bad-config messages (CFG-01, CFG-02, CFG-05)
- **D-10: Every validation error is reported, one per line, under a header naming the source file.**
  Example:
  `Configuration error in /etc/saneless/config.toml:` then
  `  [paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir` /
  `  [profiles.default] unknown key 'resoluton' (did you mean 'resolution'?); valid keys: …` /
  `  [output] web_port: <pydantic's short msg, e.g. "Input should be a valid integer">`.
  Exit code 2, as today. Type and value errors are rendered in the same `[section] key` style as
  unknown keys, not as pydantic's multi-line dump. `[profiles.<name>]` is rendered from a `loc`
  like `('profiles', 'default', 'resoluton')`.
- **D-11: Unknown keys get a close-match suggestion *and* the valid-key list.** Use stdlib
  `difflib.get_close_matches` against `model_fields` names **plus aliases** (`title`). Derive the
  valid keys from `model_fields`, which removes the hand-maintained `_VALID_SECTIONS`. When the
  unknown key is a valid key of a *different* section, say so:
  `[paperless] unknown key 'web_port'; it belongs in [output]`. An unknown top-level name is
  suggested as `[profiles.X]` only when `X` is not a case-insensitive match of a real section, so
  `[Paperless]` → "did you mean [paperless]?" (M-18).
- **D-12: Name the environment variable when it is the source.** pydantic's error `loc` is
  identical for TOML and env (verified: `SANELESS_SCANNER__HOSTNAME=x` yields
  `extra_forbidden loc=('scanner','hostname')`). While rendering, check whether the matching
  `SANELESS_<SECTION>__<KEY>` (case-insensitive, including `PROFILES__<NAME>__<KEY>`) is set in
  the environment. If so, attribute the line to that variable; otherwise attribute it to the file.
- **D-13: Unknown `SANELESS_*` environment variables are rejected like TOML typos.**
  pydantic-settings silently ignores a top-level env name that matches no field (verified:
  `SANELESS_BOGUS` is ignored), so `SANELESS_PAPERLES__TOKEN` would leave the token unset without a
  word. Scan `os.environ` for `SANELESS_`-prefixed names whose first segment is not a known
  top-level field. Add them to the same error list, with a close-match hint, and exit 2.
- **D-14: Values are never echoed in a config error.** pydantic errors carry `input`, and for
  `tokne = "…"` that is the token. The renderer prints location, key, and pydantic's `msg` only,
  never `input` or `ctx` values (CFG-05). A test must prove a token-shaped value in a mistyped key
  does not appear in the rendered message.
- **CFG-02 (locked by requirement):** an explicit `--config` path that does not exist *or is not a
  regular file* is a `ConfigError` naming the (expanded) path, exit 2. This also covers Docker's
  directory-named-`config.toml` trap when it is passed explicitly. Auto-discovery must use
  `is_file()` rather than `exists()`, so a directory at a search path is not "found".

#### Profile title (CFG-06)
- **D-15: `title` is a literal string, not a template.** Rename
  `ProfileConfig.default_title_template` → `default_title`, keeping `alias="title"`, so the TOML
  and env spelling (`SANELESS_PROFILES__DEFAULT__TITLE`) is unchanged. No placeholder
  vocabulary. The docs (`docs/reference/configuration.md`, `docs/how-to/configure-scan-profiles.md`,
  `saneless.toml.example`) stop saying "template". Update the two existing tests in
  `tests/test_config.py` that name the old attribute.
- **D-16: One title rule for both front ends: typed title → profile `title` → `Scan <timestamp>`.**
  A title that is blank after stripping counts as blank. The CLI's `--title` becomes **optional**,
  and `--title ""` counts as blank, so `saneless scan --profile receipt` from cron works.
  Implement the rule once, shared by `web/routes.py` (currently `routes.py:408-409`) and
  `cli.py scan`, rather than duplicating it. The timestamp stays UTC as today; local time is
  APPL-12.
- **D-17: Resolved server-side only.** The web form is unchanged: no placeholder hint and no
  htmx on profile change. `docs/reference/cli-commands.md` synopsis drops `--title` from required.

### Claude's Discretion
User accepted these stated defaults without discussion. The planner may refine them but should not
reverse them without cause.
- **`-v` scope (CFG-04):** `-v` sets the effective level of the `saneless` logger hierarchy to
  DEBUG and keeps mirroring to stderr. It does not blanket-enable DEBUG on the root logger for
  httpx/uvicorn/multipart. Whether uvicorn's `log_level` follows `-v` is at the planner's
  discretion. Fix `docs/reference/cli-commands.md:12` and `docs/explanation/empty-page-detection.md:54`
  (which cites a nonexistent `--log-level DEBUG`) to match. Adding a real `--log-level` option is
  **not** planned.
- **`log_level` validation:** a `Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"]` with a
  "before" validator that upper-cases the input. Whether `warn` is accepted as an alias of
  `WARNING` is the planner's call; if not, D-11 should suggest it. Use `logging.getLevelNamesMapping()`
  rather than `getattr`. The uvicorn name is derived from the validated value.
- **XDG (CFG-03):** read `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` by hand. An unset, empty, or
  relative value falls back to `~/.config` / `~/.local/state`, per the basedir spec. No
  `platformdirs` dependency: Linux-only SANE app, two variables. Defaults are computed at call
  time, not import (the `config_search_paths()` precedent). Search order becomes
  `./saneless.toml` → `$XDG_CONFIG_HOME/saneless/config.toml` → `/etc/saneless/config.toml`.
  `data_dir` and `log_file` defaults move to `$XDG_STATE_HOME/saneless` in one edit (Phase 23 D-14
  set this up). The Dockerfile's `SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless` is unaffected.
- **`~` expansion:** an "after" validator on `tmp_dir`, `data_dir`, `log_file`, `consume_dir`
  (`expanduser` on non-empty values only; no `$VAR` expansion), plus the `--config` path.
  `validate_settings_dirs` walks up to the nearest existing ancestor instead of checking only
  `parent` (M-20).
- **`SecretStr` (CFG-05):** `PaperlessConfig.token: SecretStr`, with `get_secret_value()` only at
  the construction sites (`cli.py` scan, `web/app.py:60-64`, and any others the planner finds).
  `repr(settings)` and every log/error path are proven token-free by test. Opening the log file
  0600 (N-15's optional suggestion) is **not** planned.
- **`--help` without config (CFG-10):** settings load lazily per command, or the group callback
  returns early under `ctx.resilient_parsing` / help. Mechanism is the planner's choice; the
  outcome is that `saneless serve --help` prints help with a broken config and creates no log
  directory. `configure_logging` moves inside the error handling (M-21).
- **Startup config log line (CFG-11):** INFO, once per process start after logging is configured:
  the loaded config path (or "no config file; defaults + environment") and the dotted *names* of
  keys sourced from the environment (e.g. `paperless.url, paperless.token`). Never values.
- **Error rendering home:** `_build_settings` / a dedicated renderer in `config.py`, raising
  `ConfigError`. Exact wording is the planner's, within D-10..D-14.

### Deferred Ideas (OUT OF SCOPE)
- Per-profile title hint in the web form (input placeholder that follows the profile dropdown) —
  Phase 30 alongside APPL-05 profile labels
- Title placeholders (`{date}`, `{profile}`) — rejected for now; Paperless workflows already offer
  title templating after consumption. Revisit only on user demand.
- A stronger `auto-profiles` flag that merges into hand-written profiles — rejected (D-01)
- Startup probe that warns when the loaded config file is itself a mount point — rejected in favour
  of the write-time EBUSY message (D-08)
- Log file opened with mode 0600 (N-15's optional hardening) — not planned; candidate for Phase 32
  sweep
- A real `--log-level` CLI option — not planned; `-v` covers CFG-04

Also out of this phase (from CONTEXT `<domain>`): placeholder/empty token detection (APPL-07, Phase 30); commenting out the compose `environment:` block or Paperless service (DOCS-06 / APPL-11); the image/URL rename (Phase 31); wrapping `tomllib`/`tomlkit` parse errors (EXC, Phase 28); local-time timestamps (APPL-12, Phase 30).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CFG-01 | Unknown keys in any section rejected with section, key, valid keys | Pattern 1 (nested forbid and verified locs), Pattern 2 (renderer), Pattern 3 (env attribution and unknown-env scan); Pitfalls 1, 2, 9 |
| CFG-02 | Missing `--config` exits 2 naming the path | Pattern 6 (`load_settings` expand plus `is_file`); tests to flip in Pitfall 11 |
| CFG-03 | `~` expanded; `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` honoured | Pattern 7 (XDG helpers, `default_factory` on the `output` field); Pitfalls 3, 10 |
| CFG-04 | `log_level` validated; `-v` means DEBUG | Pattern 8 (Literal plus before-validator, `saneless` logger level); Pitfall 12 |
| CFG-05 | Token is `SecretStr`, absent from repr, logs, and errors | Pattern 1 probe (`repr` masks); Pitfall 1 (`str(ValidationError)` leaks, `from None`); construction-site inventory |
| CFG-06 | Profile `title` is read when the title is blank | Pattern 9 (shared `resolve_job_title`, `worker.get_profile`) |
| CFG-07 | `--force` merges owned keys only; never touches unflagged profiles | Pattern 5 (in-place tomlkit merge, `ProfileWriteResult`); tomlkit shape probes |
| CFG-08 | Atomic, UTF-8, mode- and comment-preserving rewrite | Pattern 4 (atomic replace helper); verified EBUSY, CRLF, symlink, and chown probes |
| CFG-09 | Compose and docs mount the config directory | Doc inventory in "Docs and Compose Change List" (includes lines CONTEXT did not list) |
| CFG-10 | Subcommand `--help` works without valid config | Pattern 6 (lazy memoised loader); probe shows `resilient_parsing` does not help |
| CFG-11 | Loaded config path and env-sourced key names logged at INFO | Pattern 3 (`EnvSettingsSource(Settings)()` leaves); emitted from the lazy loader after `configure_logging` |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.14, managed with `uv` only (`uv add`, `uv run`, `uv sync`). No pip or poetry.
- Must pass, with zero errors or warnings: `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, `uv run pyrefly check src tests` (always name the paths).
- No `# type: ignore`, no `# noqa` for new suppressions, no disabled rules. Fix the issue. (Existing `# noqa: ARG003` lines in `settings_customise_sources` are pre-existing and required by the signature.)
- No mypy or pyright.
- Ruff `D` rules: every public module, class, and function has a docstring. D203/D212 are ignored, so the summary line goes on the second line of a multi-line docstring (the project's existing style).
- Hooks run through `prek`: `uv run prek run --all-files`. Commit-stage hooks type-check `src/` only, so a TDD RED commit is allowed. Never `--no-verify`, never `SKIP=`, never a stub to force RED through.
- PEP 758 bracketless `except A, B:` is valid and must not be flagged.
- Prefer external packages over reimplementing solved problems, *but* CONTEXT locks "no `platformdirs`" for XDG.
- Browser validation uses Playwright MCP and is never "manual-only". This phase changes no UI, and D-17 keeps the form unchanged. The web title fallback is covered by a TestClient test.
- `pytest` runs with `filterwarnings = ["error"]` (pyproject.toml:160), so any new DeprecationWarning fails the suite. A probe confirmed `populate_by_name=True` emits none under `-W error` on pydantic 2.12.5.
- Never merge PRs (user memory). This affects nothing in planning.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Schema strictness (`extra="forbid"`, Literal, SecretStr, path expansion) | Config model (`config.py`) | — | pydantic validates at the input boundary. Every construction path (TOML, env, direct) is covered. |
| Error rendering (loc to `[section] key`, difflib, env attribution) | Config loader (`config.py` `_build_settings` / renderer) | CLI prints it | Only the loader knows the file path and the environment. The CLI just echoes a `ConfigError`. |
| Unknown `SANELESS_*` detection | Config loader (`load_settings`) | — | Must not run inside `Settings()`, because tests build `Settings` directly. It is a load-time policy. |
| XDG and search paths | Config module functions (call time) | — | `config_search_paths()` precedent. |
| Atomic durable file replace | New I/O helper module | Called by `auto_profiles.write_profiles_to_config` | Pure filesystem concern with no TOML knowledge. Unit-testable with monkeypatched `os` calls. |
| Merge semantics (owned keys, skip unflagged) | `auto_profiles.py` | Worker and CLI consume the result | Already owns the tomlkit round-trip and `_is_auto_generated`. |
| Lazy settings, `-v`, CFG-11 line | CLI (`cli.py`) | `logging_config.py` | The process entry point owns "once per process start". |
| Title resolution | Shared pure function (`config.py`) | Web route and CLI call it | D-16 says "implement once". |
| Profile lookup for title on the web | Worker (`get_profile`, under the lock) | Route | Profiles are swapped at startup under `_profiles_lock` (D-19). `settings.profiles` read from the route would bypass the lock. |
| Deployment shape (mount) | `docker-compose.yml`, docs | — | CFG-09 is documentation and example config. |

## Standard Stack

### Core (all already locked; no new runtime dependencies)
| Library | Version (verified `uv run`) | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pydantic | 2.12.5 | `ConfigDict(extra="forbid")`, `SecretStr`, `Literal`, `field_validator`, `ValidationError.errors()` | Already the model layer `[VERIFIED: uv run python]` |
| pydantic-settings | 2.13.1 | `EnvSettingsSource` (env contribution oracle), `TomlConfigSettingsSource`, `SettingsConfigDict(extra="forbid")` | Already the settings layer `[VERIFIED: uv run python]` |
| tomlkit | 0.14.0 | Comment-preserving in-place table edits | Already used by the writer `[VERIFIED: uv run python]` |
| click | 8.3.1 | CLI; `CliRunner` tests | Already used `[VERIFIED: uv run python]` |
| uvicorn | 0.42.0 | `log_level` accepts names `critical/error/warning/info/debug/trace`, or an int | `[VERIFIED: uvicorn.config.LOG_LEVELS]` |
| stdlib `difflib` | 3.14 | `get_close_matches` for did-you-mean | CONTEXT D-11 |
| stdlib `tempfile`/`os`/`stat`/`errno` | 3.14 | `mkstemp`, `fchown`, `fchmod`, `fsync`, `replace`, `O_DIRECTORY` | POSIX atomic replace |
| stdlib `tomllib` | 3.14 | Re-parse guard on the dumped text before replace | Already used by pydantic-settings |

### Supporting (dev, already installed)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | >=9.0.2 | `monkeypatch`, `caplog`, `tmp_path` | All tests |
| pytest-timeout | >=2.4.0 | 60 s signal timeout | Already global |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Hand-rolled XDG helpers | `platformdirs` | **Locked out by CONTEXT.** Two variables on a Linux-only app. |
| `EnvSettingsSource(Settings)()` for env keys | Re-deriving from `os.environ` with prefix, delimiter, and case rules | Re-deriving duplicates pydantic-settings' JSON-valued-field and case-fold rules and will drift. Use the source. |
| PyYAML to assert compose shape in a test | Plain-text line assertions | `yaml` is only a transitive dev dependency (via mkdocs-material) and untyped for ty and pyrefly. Use text assertions. |
| Real bind-mount EBUSY test via `unshare` | Monkeypatched `os.replace` raising `OSError(errno.EBUSY, …)` | The monkeypatch is deterministic and CI-safe. An `unshare` test is optional and must `skipif` when unprivileged user namespaces are unavailable. |

**Installation:** none.

## Package Legitimacy Audit

No external packages are installed or added by this phase. Every library above is already in `uv.lock`, and XDG handling is hand-written per CONTEXT (no `platformdirs`). slopcheck was not run because there is nothing to check.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| (none added) | — | — | — | — | — | — |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Architecture Patterns

### System Architecture Diagram

```
                      saneless [--config P] [-v] <cmd> [--help]
                                     |
                         click group callback (NO loading)
                         stores config_path, verbose in ctx.obj
                                     |
              +----------------------+-----------------------+
              | <cmd> --help                                 | <cmd> runs
              v                                              v
      Click prints help, exit 0                 _load_cli_settings(ctx)  (memoised)
      (no settings, no log dir)                              |
                                                             v
                                   load_settings(P) -----------------------------------+
                                     | P given: expanduser; not is_file -> ConfigError  |
                                     | P None:  first is_file() of config_search_paths  |
                                     |          (./saneless.toml, $XDG_CONFIG_HOME/..., |
                                     |           /etc/saneless/config.toml)             |
                                     v                                                  |
                          unknown SANELESS_* scan (os.environ, casefold) -> env_errors  |
                                     v                                                  |
                          Settings(_toml_file=...)  sources: init > env > toml          |
                                     | ValidationError?                                 |
                                     |    yes -> render(errors(), env_data, path) ------+-> ConfigError (from None)
                                     |    no, but env_errors -> ConfigError             |      |
                                     v                                                  |      v
                           validate_settings_dirs (nearest existing ancestor)           |  CLI: echo, exit 2
                                     v
                           configure_logging(level from validated Literal; -v -> saneless=DEBUG)
                                     v
                           warn_on_legacy_duplex_sources ; log_config_sources (CFG-11 INFO)
                                     v
          +--------------------+-----------+---------------+--------------------------+
          v                    v           v               v                          v
        scan              devices        jobs           serve                   auto-profiles
   resolve_job_title    (unchanged)   (unchanged)   uvicorn log_level         generate_profiles
   token.get_secret_                                 from validated value      |
   value() -> Paperless                              create_app: unwrap token  v
   Client                                            worker startup gen --> write_profiles_to_config(path, gen, force)
                                                     (same writer, force=F)       | parse (bytes->utf-8, keep CRLF)
                                                                                  | prune orphans (auto_generated only)
                                                                                  | add / refresh owned keys in place /
                                                                                  |   skip unflagged / skip existing
                                                                                  | tomlkit.dumps -> tomllib.loads guard
                                                                                  v
                                                                        replace_file_atomically(path, text)
                                                                          resolve symlink -> real target
                                                                          os.access W_OK check (read-only file intent)
                                                                          mkstemp(dir=target.parent)
                                                                          fchown (suppress PermissionError) -> fchmod
                                                                          write bytes, fsync, os.replace
                                                                            EBUSY -> ConfigError (mount the directory)
                                                                          fsync dir (best effort); unlink temp on failure
                                                                                  v
                                                                        ProfileWriteResult(added, refreshed,
                                                                          skipped_unowned, skipped_existing,
                                                                          removed, target)
                                                             CLI: grouped lines   |   worker: same vocabulary in log,
                                                                                  |   _profiles_after_persist(result)
```

### Recommended Project Structure (changes only)
```
src/saneless/
├── config.py            # forbid on all models; SecretStr; Literal log_level; default_title;
│                        # XDG helpers; expanduser validators; output default_factory;
│                        # renderer + unknown-env scan + env_sourced_keys(); resolve_job_title();
│                        # load_settings is_file/expanduser; validate_settings_dirs ancestor walk
├── atomic_write.py      # NEW: replace_file_atomically(path, text) -> Path  (name at planner's discretion)
├── auto_profiles.py     # ProfileWriteResult; merge loop; bytes read; tomllib guard; calls atomic helper
├── logging_config.py    # getLevelNamesMapping; verbose -> logging.getLogger("saneless").setLevel(DEBUG)
├── cli.py               # lazy memoised loader; --title optional; grouped auto-profiles output;
│                        # ConfigError from write -> exit 2; token unwrap; CFG-11 line
├── worker.py            # consume ProfileWriteResult; same vocabulary in log
└── web/
    ├── app.py           # token.get_secret_value()
    └── routes.py        # resolve_job_title(title, worker.get_profile(profile), now=...)
tests/
├── conftest.py          # clean_env also removes XDG_CONFIG_HOME / XDG_STATE_HOME
├── test_config.py       # CFG-01/02/03/04/05/06/11 unit tests
├── test_atomic_write.py # NEW: CFG-08 helper tests
├── test_auto_profiles.py# CFG-07 merge tests; CRLF/UTF-8 round-trip
├── test_logging.py      # CFG-04 -v effective level
├── test_cli.py          # CFG-02/06/07/08/10/11 through CliRunner
├── test_web.py          # CFG-06 web fallback
├── test_worker.py       # result consumption; EBUSY keeps profiles in memory
└── test_deployment_config.py  # NEW: CFG-09 compose/docs text assertions
```

### Pattern 1: Nested `extra="forbid"`, with the locs it actually produces
**What:** Add `model_config = ConfigDict(extra="forbid")` to `ScannerConfig`, `PaperlessConfig`, `OutputConfig`. Use `ConfigDict(extra="forbid", populate_by_name=True)` on `ProfileConfig`. Add `extra="forbid"` explicitly to `Settings`' `SettingsConfigDict`.

**Verified locs and types** (probe against pydantic 2.12.5 / pydantic-settings 2.13.1, TOML and env) `[VERIFIED: uv run python probe]`:

| Input | `type` | `loc` | `msg` |
|---|---|---|---|
| `[paperless] tokne = "SECRET"` | `extra_forbidden` | `('paperless','tokne')` | `Extra inputs are not permitted` |
| `[profiles.default] resoluton = 600` | `extra_forbidden` | `('profiles','default','resoluton')` | same |
| top-level `tokne_top = 1` | `extra_forbidden` | `('tokne_top',)` | same |
| `[Paperless]` table | `extra_forbidden` | `('Paperless',)` | same |
| `[output] web_port = "abc"` | `int_parsing` | `('output','web_port')` | `Input should be a valid integer, unable to parse string as an integer` |
| `default_tags = ["x"]` | `int_parsing` | `(..., 'default_tags', 0)` | integer `loc` element |
| `SANELESS_PROFILES__DEFAULT="notjson"` | `model_type` | `('profiles','default')` | `Input should be a valid dictionary or instance of …` |
| `SANELESS_PAPERLESS__URL__X=deep` | `string_type` | `('paperless','url')` | `Input should be a valid string` |
| `log_level="TRACE"` (Literal) | `literal_error` | `('output','log_level')` | `Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'` |
| missing default profile | `value_error` | `('profiles',)` | `Value error, A 'default' profile must be defined in config` |

- Errors within one `ValidationError` arrive in field order (paperless, output, profiles, then top-level extras). Sort for stable output if tests compare whole messages.
- **Every `msg` checked (int_parsing, literal_error, greater_than_equal, bool_parsing, string_type, model_type, extra_forbidden) contained no input value.** `input` carries the value, so never read it.
- `alias="title"` plus `populate_by_name=True` plus `extra="forbid"`: `title = "x"` and `default_title = "x"` are both accepted, and `loc` uses the alias. `ProfileConfig.model_fields` holds `default_title` with `.alias == "title"`. For the valid-key *display*, use `field.alias or name` (show `title`, not `default_title`). difflib should match against both.
- `_translate_legacy_manual_duplex` (a before-validator adding `duplex`) still works under forbid (probed with `source = "Manual Duplex"`).
- **ty and pyrefly both accept** `ProfileConfig(default_title="x")` and `ProfileConfig(title="x")` with `populate_by_name=True`, and both flag an unknown kwarg `[VERIFIED: ty + pyrefly probe]`. Without `populate_by_name`, ty emits `pydantic-discarded-extra-argument` for the field-name form. Keep `populate_by_name=True`. pydantic docs describe `validate_by_name=True` as the newer equivalent `[CITED: github.com/pydantic/pydantic docs/concepts/alias.md]`, but switching is optional and neither emits a warning today.

### Pattern 2: The error renderer (D-10, D-11, D-14)
**What:** A pure function that turns `exc.errors()` plus env data plus the path into lines. Only `loc` and `msg` are read.

```python
# Source: shape derived from verified error dicts above; stdlib difflib
import difflib

_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "scanner": ScannerConfig, "paperless": PaperlessConfig, "output": OutputConfig,
}

def _valid_keys(model: type[BaseModel]) -> list[str]:
    """User-facing key names: the alias where one exists (``title``), else the field name."""
    return [field.alias or name for name, field in model.model_fields.items()]

def _match_candidates(model: type[BaseModel]) -> list[str]:
    """Field names plus aliases, for difflib (D-11)."""
    names = list(model.model_fields)
    return names + [f.alias for f in model.model_fields.values() if f.alias]

def _describe_unknown_key(section_label: str, key: str, model: type[BaseModel]) -> str:
    owner = _section_owning(key, exclude=model)   # "output", "profiles.<name>", or None
    if owner is not None:
        return f"[{section_label}] unknown key {key!r}; it belongs in [{owner}]"
    hint = difflib.get_close_matches(key, _match_candidates(model), n=1)
    did_you_mean = f" (did you mean {hint[0]!r}?)" if hint else ""
    return (f"[{section_label}] unknown key {key!r}{did_you_mean}; "
            f"valid keys: {', '.join(_valid_keys(model))}")
```

Rules the renderer needs, in order:
1. `loc[0]` not in `Settings.model_fields`, so it is an unknown top-level name:
   - If `loc[0].casefold()` equals a real section, write `unknown section [Paperless] (did you mean [paperless]?)`.
   - Else, if `loc[0]` is a key of some section, write `it belongs in [output]` (for example, a top-level `web_port`).
   - Else suggest `[profiles.X]` (this keeps today's `test_wrong_section_name_gives_helpful_error` passing, since it matches `profiles\.default`), plus a difflib hint against the section names.
2. `loc[0]` in the three plain sections, `len(loc) == 2`, type `extra_forbidden`: unknown key in that section.
3. `loc[0] == "profiles"`, `len(loc) >= 3`: section label `profiles.<loc[1]>`, model `ProfileConfig`.
4. Any other type: `[<section label>] <key path>: <msg>`. An integer loc element renders as `default_tags[0]`.
5. **Render every user-controlled name with `!r`** (keys, section names, profile names). TOML quoted keys can hold control characters: the probe `"tok\nne[31m" = 1` parsed. `repr` escapes them, which is the project's existing log-forgery defence (`web/errors.py:14`).
6. Header: `Configuration error in {path}:` when a file was loaded, else `Configuration error (defaults and environment):`. `cli.py` currently prefixes `Configuration error: ` itself. Change the echo so the prefix is not doubled (for example, print `str(exc)` directly for `ConfigError`, and keep the prefix for other exceptions).
7. `raise ConfigError("\n".join(lines)) from None`. See Pitfall 1.

### Pattern 3: What the environment contributed (D-12, D-13, CFG-11)
**Verified** `[VERIFIED: probe against saneless.config.Settings]`:
```python
from pydantic_settings import EnvSettingsSource
EnvSettingsSource(Settings)()
# SANELESS_PAPERLESS__TOKEN=t SANELESS_PROFILES__RECEIPT__TITLE=R SANELESS_OUTPUT='{"web_port": 9}'
# -> {'paperless': {'token': 't'}, 'output': {'web_port': 9}, 'profiles': {'receipt': {'title': 'R'}}}
```
- Reads `env_prefix="SANELESS_"`, `env_nested_delimiter="__"`, and `case_sensitive=False` from `Settings.model_config`. Names are case-folded: `SANELESS_PROFILES__Receipt__TITLE` becomes profile `receipt`, and `saneless_paperless__url` is honoured.
- JSON-valued whole sections (`SANELESS_OUTPUT='{…}'`, `SANELESS_PROFILES='{…}'`) appear exploded, so the leaf list is right for both spellings.
- Invalid JSON for a complex field (`SANELESS_PAPERLESS=notjson`) raises `pydantic_settings.exceptions.SettingsError` (a `ValueError`) from both the source and `Settings()`. It is not a `ValidationError`. Its message names the field and source, not the value. Either wrap it into `ConfigError` in `_build_settings` (it is an env error, not a TOML syntax error, so it is not Phase 28's tomllib scope), or leave it to the CLI's generic catch. Recommended: wrap it, naming the variable.
- **CFG-11 key names:** flatten the dict to dotted leaves, e.g. `['output.web_port', 'paperless.token', 'profiles.receipt.title']`. Names only.
- **D-12 attribution:** for an error `loc`, walk the env dict by the string elements of `loc`. If the path, or any prefix of it, exists in the env dict, the value came from the environment (env beats file after pydantic-settings' deep merge). To name the variable, try `SANELESS_` + `__`.join(parts[:i]).upper() from longest to shortest, matching `os.environ` keys case-insensitively, and print the name as it is actually spelled in the environment. Because the env dict is already case-folded, a TOML `[profiles.Receipt]` error is not misattributed to an env var that created `receipt`.
- **D-13 unknown top-level names:** the source ignores them (verified for `SANELESS_PAPERLES__TOKEN`, `SANELESS_CONFIG_PATH`, `SANELESS_SCANNER_HOST`). Scan them yourself:
  ```python
  prefix = "saneless_"
  for name in os.environ:
      if not name.casefold().startswith(prefix):
          continue
      first = name[len(prefix):].split("__", 1)[0].casefold()
      if first not in Settings.model_fields:   # scanner, paperless, output, profiles
          ...  # error line naming `name`, with a hint
  ```
  - Hint quality: difflib's 0.6 cutoff misses the common single-underscore mistake (`SANELESS_OUTPUT_WEB_PORT` scores about 0.57 against `output`). Add a specific hint: when `first` starts with `<section>_`, suggest `SANELESS_<SECTION>__<REST>`.
  - Known valid names stay valid: `SANELESS_OUTPUT__DATA_DIR` (Dockerfile:24), `SANELESS_PROFILES__RECEIPT__TITLE`, `SANELESS_PROFILES` (JSON), and `SANELESS_OUTPUT` (JSON), because their first segment is a field.
- Run the D-13 scan in `load_settings` / `_build_settings`, **not** in a `Settings` validator. Direct `Settings(...)` construction in conftest and tests must stay unaffected.

### Pattern 4: Atomic replace helper (D-05..D-08)
**Verified primitives** `[VERIFIED: probes on this host, kernel 5.14 / RHEL 9]`:
- `tempfile.mkstemp(dir=…)` creates the file with mode `0o600` (`O_EXCL`, random name, so no symlink race on the temp itself).
- `os.replace(tmp, target)` onto a **single-file bind mount** gives `OSError` with `errno == errno.EBUSY`. Onto a file inside a **directory bind mount**, it succeeds.
- A non-root `os.fchown(fd, own_uid, own_gid)` succeeds (no-op). `os.fchown(fd, 0, 0)` gives `PermissionError` (`EPERM`).
- `Path.resolve()` on a symlink returns the real file. Replacing the real file keeps the link working, and the mode `0o640` copied with `fchmod` survived.
- `os.open(dir, os.O_RDONLY | os.O_DIRECTORY)` plus `os.fsync` works on a local filesystem.
- `Path.read_text(encoding="utf-8")` turns `b"a = 2\r\n"` into `"a = 2\n"`. `open(..., newline="")` or `read_bytes().decode("utf-8")` keeps `\r\n`.

```python
# Source: POSIX rename(2)/fsync(2) semantics; primitives verified by probe above
import contextlib, errno, os, stat, tempfile
from pathlib import Path
from saneless.exceptions import ConfigError

def replace_file_atomically(path: Path, text: str) -> Path:
    """
    Replace ``path``'s contents with ``text`` durably, writing through symlinks.

    Returns:
        The real file that was replaced (differs from ``path`` for a symlink).

    Raises:
        ConfigError: The target is a single-file bind mount (EBUSY).
        OSError: Any other filesystem failure; the temp file is removed first.
    """
    target = path.resolve()
    try:
        original: os.stat_result | None = target.stat()
    except FileNotFoundError:
        original = None
    if original is not None and not os.access(target, os.W_OK):
        # Keep today's meaning of a read-only file: replace() only needs a writable
        # directory and would otherwise silently rewrite a chmod 0444 file.
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as handle:
            if original is not None:
                # chown BEFORE chmod: chown(2) may clear set-id bits.
                with contextlib.suppress(PermissionError):
                    os.fchown(handle.fileno(), original.st_uid, original.st_gid)
                os.fchmod(handle.fileno(), stat.S_IMODE(original.st_mode))
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, target)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                msg = (f"Cannot replace {target}: it is bind-mounted as a single file. "
                       "Mount its directory instead (see docs/how-to/deploy-docker-compose.md).")
                raise ConfigError(msg) from exc
            raise
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)
    _fsync_directory(target.parent)   # best effort: suppress OSError (see Pitfall 7)
    return target
```
Notes for the planner:
- Using `try/finally` with a `replaced` flag avoids a blind `except BaseException` and still cleans up on `KeyboardInterrupt`.
- **New file (no original):** CONTEXT does not decide its mode or owner. See Open Question 1. mkstemp leaves `0o600`.
- The worker already catches `(OSError, ConfigError)` and logs `type(exc).__name__: exc` (worker.py:826-837). EBUSY therefore arrives as `ConfigError` with the fix text, and the existing "used for this run only" warning stays correct (D-08).
- `cli.py auto_profiles` catches nothing from the writer today (cli.py:489), so an EBUSY would be a traceback. Add `except ConfigError` (and probably `OSError`) that echoes the message, then `sys.exit(2)` (cli-commands.md:146 already documents 2 as "Configuration error").
- D-07 log line: when `target != path.absolute()`, log `"Wrote profiles to %s (symlink to %s)"`.

### Pattern 5: Merge-aware writer and structured result (D-01..D-04)
**Verified tomlkit behaviour** `[VERIFIED: probes, tomlkit 0.14.0]`:
- `table["source"] = "ADF Front"` on an existing `[profiles.default]` keeps the table comment, the key's **inline comment** (`# inline c`), key order, and every other key. `del table["duplex"]` removes the line. A new key such as `table["mode"] = …` is appended at the end of the table.
- A dotted-key profile (`dotted.source = "ADF"` under `[profiles]`) and an inline-table profile (`inl = { … }`) are both `Mapping`/`MutableMapping`. Set and delete work, and the output re-parses. Deleting from an inline table leaves a cosmetic double space.
- A split super-table (`[profiles.a]` … `[output]` … `[profiles.b]`) comes back as `OutOfOrderTableProxy`. Set, delete, and add-table work and re-parse correctly.
- **Bug (pre-existing):** `profiles = { default = { source = "x" } }` (inline section) plus `section["n"] = tomlkit.table()` dumps `profiles = { default = {...}, n = source = "q"\n}`, which is **invalid TOML**. Guard it: when the section is a `tomlkit.items.InlineTable`, add `tomlkit.inline_table()` instead. Also **always** run `tomllib.loads(new_text)` before replacing and raise `ConfigError` when it fails, so no shape can corrupt the user's file.
- CRLF: existing lines keep `\r\n` when the text is read without newline translation. **Lines tomlkit adds use `\n`**, giving mixed endings. To honour D-05 fully, when the original contains `\r\n`, normalise lone `\n` in the dumped text to `\r\n` (`re.sub(r"(?<!\r)\n", "\r\n", out)`), then run the tomllib guard.

```python
# Source: existing auto_profiles.py loop + verified in-place tomlkit semantics
_OWNED_KEYS: Final = ("source", "resolution", "mode", "auto_source_mode", "duplex", "auto_generated")

@dataclass(frozen=True)
class ProfileWriteResult:
    """What one write did to the config file, grouped by action (D-04)."""
    path: Path                                  # the real file replaced
    added: tuple[str, ...] = ()
    refreshed: tuple[str, ...] = ()
    skipped_not_generated: tuple[str, ...] = ()  # D-01 (also under --force)
    skipped_existing: tuple[str, ...] = ()       # flagged, but no --force
    removed: tuple[str, ...] = ()                # orphan prune (Phase 24 D-16)

    @property
    def persisted(self) -> frozenset[str]:
        """Names whose file table now matches the generated profile."""
        return frozenset(self.added + self.refreshed)

def _generated_values(profile: ProfileConfig) -> dict[str, str | int | bool]:
    values: dict[str, str | int | bool] = {
        "source": profile.source, "resolution": profile.resolution, "mode": profile.mode}
    if profile.auto_source_mode != "flatbed":
        values["auto_source_mode"] = profile.auto_source_mode
    if profile.duplex != "none":
        values["duplex"] = profile.duplex
    values["auto_generated"] = True
    return values

# for name, profile in generated.items():
#     existing = profiles_section.get(name)
#     if existing is None:            -> add a new table (inline_table if section is InlineTable); added
#     elif not _is_auto_generated(existing): skipped_not_generated   (byte-for-byte untouched)
#     elif not force:                 skipped_existing
#     else:                           # MutableMapping narrowed by isinstance
#         values = _generated_values(profile)
#         for key in _OWNED_KEYS:
#             if key in values: existing[key] = values[key]
#             elif key in existing: del existing[key]          # D-03
#         refreshed
```
- Use this key insertion order for new tables, exactly as today (source, resolution, mode, [auto_source_mode], [duplex], auto_generated). Several existing tests read the file text.
- **Worker consumption:** `_profiles_after_persist(loaded, generated, written: list[str] | None)` becomes `(…, result: ProfileWriteResult | None)`, using `result.persisted` where it builds `set(written)`. The worker never passes `force`, so `refreshed` stays empty there. The worker log line becomes, for example: `Auto-profiles: wrote %s: added: a, b; skipped (not auto-generated): default` (only the non-empty groups, same vocabulary as the CLI).
- **CLI output:** print `Added: …` / `Refreshed: …` / `Skipped (not auto-generated): …` (with the D-01 reason per name, or once) / `Skipped (already exists; use --force to refresh): …` / `Removed (scanner no longer offers it): …`, and a header naming `result.path`. Replace today's `No new profiles written (use --force to overwrite).` with the grouped form. When every group is empty, print one line saying nothing changed.
- Keep `_is_auto_generated` as the only ownership test. It already treats a scalar entry as "not generated", so a stray scalar is reported as skipped and never overwritten.

### Pattern 6: Lazy CLI settings (CFG-10, M-21) and CFG-02
**Verified:** with `@click.group()`, `cli scan --help` runs the group callback first, with `resilient_parsing=False`, `invoked_subcommand='scan'`, and `args=[]`. `cli --help` and `cli` with no args exit before the callback. `scan --bogus` also runs the callback first `[VERIFIED: click 8.3.1 CliRunner probe]`. **Checking `resilient_parsing` cannot satisfy CFG-10.** Use lazy loading.

```python
# Source: click Context/ctx.obj pattern; existing cli.py callback body moved into a helper
@click.group()
@click.option("--config", "config_path", default=None, help="Path to config file.")
@click.option("-v", "--verbose", is_flag=True, help="Log debug detail from saneless to the log file and stderr.")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose

def _load_cli_settings(ctx: click.Context) -> Settings:
    """Load, validate and log settings once per process, on first use by a command."""
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached
    verbose = bool(ctx.obj.get("verbose"))
    try:
        settings = load_settings(ctx.obj.get("config_path"))
        validate_settings_dirs(settings)
        configure_logging(settings.output.log_file, settings.output.log_level,
                          settings.output.log_max_bytes, settings.output.log_backup_count,
                          verbose=verbose)
    except ConfigError as exc:
        click.echo(str(exc), err=True)          # renderer already has its header
        sys.exit(2)
    except Exception as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(2)
    warn_on_legacy_duplex_sources(settings)
    log_config_sources(settings)                # CFG-11
    ctx.obj["settings"] = settings
    return settings
```
- Every command (`scan`, `devices`, `jobs`, `serve`, `auto-profiles`) replaces `ctx.obj["settings"]` with `_load_cli_settings(ctx)`. `ctx.obj` is the root context's dict and is shared with subcommand contexts.
- Existing CLI tests keep working. They monkeypatch `saneless.cli.load_settings` and `saneless.cli.configure_logging`, and the helper still calls both by module-global name. `test_scan_config_error` (a ValueError from load gives exit 2 with "Configuration error") is still met by the generic branch.
- **CFG-02 in `load_settings`:**
  ```python
  if config_path:
      path = Path(config_path).expanduser()
      if not path.is_file():
          raise ConfigError(f"Config file not found or not a regular file: {path}")
  else:
      path = next((p for p in config_search_paths() if p.is_file()), None)
  ```
- **`--title` optional:** `@click.option("--title", default="", help="Document title (default: the profile's title, else 'Scan <time>').")`. `Done: {title}` must print the *resolved* title.

### Pattern 7: XDG and `~` (CFG-03)
**Spec** `[CITED: specifications.freedesktop.org/basedir/latest/ v0.8]`: "If `$XDG_CONFIG_HOME` is either not set or empty, a default equal to `$HOME/.config` should be used." Likewise `$XDG_STATE_HOME` defaults to `$HOME/.local/state`. "All paths set in these environment variables must be absolute. If an implementation encounters a relative path … it should consider the path invalid and ignore it."

```python
def _xdg_base(variable: str, *fallback: str) -> Path:
    value = os.environ.get(variable, "")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home().joinpath(*fallback)

def xdg_config_home() -> Path: return _xdg_base("XDG_CONFIG_HOME", ".config")
def xdg_state_home() -> Path:  return _xdg_base("XDG_STATE_HOME", ".local", "state")

class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    data_dir: str = Field(default_factory=lambda: str(xdg_state_home() / "saneless"))
    log_file: str = Field(default_factory=lambda: str(xdg_state_home() / "saneless" / "saneless.log"))

    @field_validator("tmp_dir", "data_dir", "log_file", mode="after")
    @classmethod
    def _expand_user(cls, value: str) -> str:
        return str(Path(value).expanduser()) if value else value

class Settings(BaseSettings):
    output: OutputConfig = Field(default_factory=OutputConfig)   # REQUIRED -- see Pitfall 3
```
- **Verified:** `output: OutputConfig = OutputConfig()` gave `/home/orig/...` after `HOME` changed. `Field(default_factory=OutputConfig)` gave `/home/changed/...`. A relative `XDG_STATE_HOME` was ignored, an absolute one honoured, `~/foo` expanded, `""` left empty `[VERIFIED: probe]`.
- Field "after" validators do **not** run on defaults unless `validate_default=True`. That is fine, because the defaults are already absolute.
- `PaperlessConfig.consume_dir` gets the same validator (empty means disabled, so it stays `""`).
- Consider making `ScannerConfig`/`PaperlessConfig` defaults on `Settings` `default_factory` too, for consistency. Not required: they have no call-time values.
- `config_search_paths()` becomes `(Path("./saneless.toml"), xdg_config_home() / "saneless" / "config.toml", Path("/etc/saneless/config.toml"))`.
- `validate_settings_dirs`: replace each `parent.exists()` check with a walk to the nearest existing ancestor:
  ```python
  def _nearest_existing_ancestor(path: Path) -> Path:
      for candidate in (path, *path.parents):
          if candidate.exists():
              return candidate
      return Path(path.anchor or ".")
  ```
  Then check `os.access(ancestor, os.W_OK)`. Reuse one helper for all three directories. This also removes today's triplicated code.

### Pattern 8: `log_level` and `-v` (CFG-04)
```python
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

log_level: LogLevel = "INFO"

@field_validator("log_level", mode="before")
@classmethod
def _normalise_log_level(cls, value: object) -> object:
    if isinstance(value, str):
        upper = value.strip().upper()
        return "WARNING" if upper == "WARN" else upper
    return value
```
- Verified: `"warn"` becomes `WARNING` and `" debug "` becomes `DEBUG`. `"TRACE"` gives `literal_error` with a msg that lists the five names and not the input `[VERIFIED: probe]`.
- `logging.getLevelNamesMapping()` → `{'CRITICAL': 50, 'FATAL': 50, 'ERROR': 40, 'WARN': 30, 'WARNING': 30, 'INFO': 20, 'DEBUG': 10, 'NOTSET': 0}` `[VERIFIED]`.
- uvicorn `LOG_LEVELS` = `critical, error, warning, info, debug, trace` `[VERIFIED: uvicorn/config.py]`. `settings.output.log_level.lower()` is always a valid key once validated. Recommendation: uvicorn follows the **configured** level, not `-v`, consistent with "no blanket DEBUG for uvicorn". uvicorn sets `uvicorn.error`, `uvicorn.access`, and `uvicorn.asgi` from it (config.py:390-397).
- `configure_logging`: `root.setLevel(logging.getLevelNamesMapping()[log_level])`. When `verbose`, set `logging.getLogger("saneless").setLevel(logging.DEBUG)`. **When not verbose, set it to `logging.NOTSET`**, so repeated calls (tests) do not leak DEBUG. All 14 `src/saneless` modules use `logging.getLogger(__name__)`, so they are all under `saneless` `[VERIFIED: grep]`. Handlers have no level, so saneless DEBUG records reach both the file and stderr handlers, while root stays at the configured level for httpx/multipart.
- `logging_config.configure_logging(log_level: str)` signature: tighten to the `LogLevel` alias, or keep `str` and look it up in the mapping. Tests pass literal strings either way.

### Pattern 9: Shared title rule (CFG-06, D-16)
```python
def resolve_job_title(typed: str | None, profile: ProfileConfig | None, *, now: datetime) -> str:
    """Typed title, else the profile's ``title``, else ``Scan <UTC time>`` (D-16)."""
    if typed is not None and typed.strip():
        return typed
    if profile is not None and profile.default_title.strip():
        return profile.default_title
    return f"Scan {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')}"
```
- **Web:** `routes.py:406-409` checks `state.worker.has_profile(profile)` and then builds the title. Replace it with a single `found = state.worker.get_profile(profile)`, `if found is None: raise RequestRejected(UNKNOWN_PROFILE)`, then `title = resolve_job_title(title, found, now=datetime.now(tz=UTC))`. That is one lock acquisition and no check-then-read gap.
- **CLI:** `resolve_job_title(title, settings.profiles[profile], now=datetime.now(tz=UTC))` after the unknown-profile check, before `PipelineRequest`. Echo `Done: {resolved}`.
- Put it in `config.py` next to `ProfileConfig`. Both `cli.py` and `web/routes.py` can import it without a cycle (`config.py` imports only `exceptions`).
- **Recommended:** bound `default_title` by `max_length=TITLE_MAX_LENGTH` (vocabulary.py:221, 256). The route's `Form(max_length=…)` only checks the *typed* title (routes.py:374), so without this a long profile title bypasses ROBU-08. `vocabulary.py` imports only `exceptions`, so importing it from `config.py` creates no cycle `[VERIFIED: grep imports]`.
- Rename the attribute: `default_title_template` → `default_title` (config.py:117, and tests/test_config.py:322-336). No other reader exists `[VERIFIED: grep src tests]`.

### Anti-Patterns to Avoid
- **Interpolating `str(exc)` or `exc.errors()[i]["input"]` into any message or log.** The token leaks (Pitfall 1).
- **Assigning `tomlkit.table()` over an existing profile under `--force`.** This is exactly M-09. Mutate in place.
- **`Path.write_text` / `Path.read_text` for the config file.** Truncate-then-write is non-atomic, and CRLF is translated.
- **Catching `ValidationError` in `Settings.__init__` or validators to implement D-13.** It breaks direct `Settings(...)` construction in the whole test suite.
- **`output: OutputConfig = OutputConfig()` with factory defaults inside.** It is still evaluated at import.
- **Setting the root logger to DEBUG for `-v`.** It floods the log with httpx/multipart output, contrary to CONTEXT.
- **A non-atomic fallback on EBUSY.** Locked out by D-08.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Which keys came from env; env name to nested path | Your own prefix, `__` split, case-fold, and JSON parsing | `EnvSettingsSource(Settings)()` | Matches pydantic-settings exactly, including JSON-valued sections and case folding (verified) |
| Did-you-mean | Levenshtein code | `difflib.get_close_matches` | CONTEXT D-11; stdlib |
| Level-name validation | `getattr(logging, name)` or a hand table | `Literal[...]` plus `logging.getLevelNamesMapping()` | `getattr` accepts `warn` and crashes on `TRACE` (M-21) |
| TOML comment-preserving edits | Regex or line surgery | tomlkit in-place `table[key] = v` / `del table[key]` | Keeps inline comments and order (verified) |
| TOML validity of the output | Trusting tomlkit's dump | `tomllib.loads(new_text)` guard | tomlkit can emit invalid TOML for inline sections (verified) |
| Secret masking | Custom `__repr__` | `pydantic.SecretStr` | `repr` and `model_dump(mode="json")` show `'**********'` (verified) |
| Temp file creation | `path.with_suffix(".tmp")` (the review's snippet) | `tempfile.mkstemp(dir=target.parent)` | A fixed name races with concurrent writers and follows a planted symlink; mkstemp is `O_EXCL` with mode 0600 |

**Key insight:** pydantic-settings already knows where every value came from, and pydantic already knows exactly which key failed. The phase's job is to *render* that knowledge safely. Re-deriving it is how drift and secret leaks happen.

## Runtime State Inventory

This phase renames a model attribute (`default_title_template` → `default_title`) and changes the deployment mount shape. Each category was checked:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None. The attribute name is never persisted: the SQLite job store keeps the resolved `title` string per job, and TOML and env use the unchanged alias `title`. Verified by grep of `job.py` and `src/`. | None |
| Live service config | Operators' existing `docker-compose.yml` files use `./config.toml:/etc/saneless/config.toml:ro`. That config lives on user hosts, not in git. After this phase, a worker write on such a mount fails (read-only gives `OSError` EROFS, a writable single-file mount gives `EBUSY`), so the profiles stay in memory as today. | Docs change plus the D-08 message. Add a migration note to `deploy-docker-compose.md` ("move `config.toml` into `./config/` and mount the directory"), next to the existing upgrade section (lines ~180-215). |
| OS-registered state | None. No systemd units or task scheduler entries ship with the repo (verified: repo has no `*.service`). | None |
| Secrets/env vars | `SANELESS_PAPERLESS__TOKEN`: name unchanged, type becomes `SecretStr`. **New:** any stray `SANELESS_*` variable in an operator's environment that is not a real section (for example `SANELESS_CONFIG_PATH`, typos) now makes startup exit 2 (D-13). | Document it in `environment-variables.md`. No key renames. |
| Build artifacts | None. The Dockerfile sets `SANELESS_OUTPUT__DATA_DIR` (still valid) and has no `WORKDIR` in the runtime stage (cwd `/`). No egg-info or installed copies matter. | None |

## Common Pitfalls

### Pitfall 1: The token leaks through the exception, not the message
**What goes wrong:** You render lines from `loc` and `msg`, but `raise ConfigError(msg) from exc` attaches the `ValidationError`. Any traceback (pytest failure output, `logger.exception`, an uncaught path) prints `__cause__`, whose `str()` includes `input_value='SECRET…'`.
**Why it happens:** `str(ValidationError)` embeds inputs. The probe printed `STR contains secret? True`.
**How to avoid:** `raise ConfigError(rendered) from None`. Test with a token-shaped value under a mistyped key: assert it is absent from `str(err)`, from `repr(err)`, and from `err.__cause__` / `err.__context__` (expect `__suppress_context__` True), and from CliRunner output.
**Warning signs:** `from exc` on the ValidationError branch. Any test using `pytest.raises(..., match=...)` against the whole message that includes a value.

### Pitfall 2: A `SettingsError` is not a `ValidationError`
**What goes wrong:** `SANELESS_PAPERLESS=notjson` raises `pydantic_settings.exceptions.SettingsError` (a ValueError subclass) before validation. A renderer that catches only `ValidationError` lets it through to the CLI's generic catch.
**How to avoid:** Catch it in `_build_settings` and turn it into a `ConfigError` naming the variable. Its message contains the field name, not the value (verified).

### Pitfall 3: Import-time defaults defeat XDG
**What goes wrong:** Adding `default_factory` to `data_dir` changes nothing for a config with no `[output]` table. `Settings.output`'s default instance was built when `config.py` was imported.
**How to avoid:** `output: OutputConfig = Field(default_factory=OutputConfig)`. Test: change `HOME`/`XDG_STATE_HOME` via monkeypatch *after* import, then `Settings().output.data_dir` must follow.
**Warning signs:** Tests pass locally only because HOME never changes between import and use.

### Pitfall 4: CRLF silently normalised
**What goes wrong:** `read_text()` translates `\r\n`, so the rewrite converts the whole file to LF. Or tomlkit's new lines are LF inside a CRLF file.
**How to avoid:** Read bytes and decode UTF-8. Write bytes. Normalise added lines when the source used CRLF. Test with a CRLF fixture plus a non-ASCII comment (`# café`) under `LC_ALL=C`-independent code (bytes I/O is locale-free).

### Pitfall 5: chmod before chown drops bits, and chown can fail partially
**What goes wrong:** `chown(2)` clears set-user-ID and set-group-ID bits for non-root callers. Doing `fchmod` first can therefore lose part of the copied mode.
**How to avoid:** `fchown` first (with `PermissionError` suppressed), then `fchmod(S_IMODE(st_mode))`. A non-root user who owns the file but whose gid is a group they are not in gets EPERM for the whole call. Skipping silently is exactly D-06.

### Pitfall 6: `os.replace` ignores the target file's own permission bits
**What goes wrong:** Rename needs only a writable *directory*. A `chmod 0444 config.toml` that used to make the worker's write fail (a documented "cannot be written" case, `configure-scan-profiles.md:165`) would now be silently replaced, with mode 0444 preserved.
**How to avoid:** Check `os.access(target, os.W_OK)` before creating the temp file and raise `PermissionError`. Root (the container) bypasses it, as it bypassed `write_text` before, so this is behaviour parity rather than new policy. The existing worker test locks the directory too (test_worker.py:3839-3840), so it keeps passing either way.

### Pitfall 7: Directory fsync is not universally supported
**What goes wrong:** Some filesystems reject `fsync` on a directory fd with `EINVAL`. This is reported for some FUSE/network mounts and Docker Desktop shared folders `[ASSUMED]`. That would turn a completed write into an error.
**How to avoid:** Make the directory fsync best effort (`contextlib.suppress(OSError)`) *after* a successful replace. The data is already in place.

### Pitfall 8: Click's group callback runs before subcommand help
Verified above. Every command must call the lazy loader. Test: `saneless serve --help` with `load_settings` patched to raise, and `configure_logging` patched to record, gives exit 0, `--host` in the output, and `configure_logging` never called. Add a second test with a real broken config in `tmp_path` and a `log_file` under `tmp_path/logs`: the directory must not exist afterwards.

### Pitfall 9: Unknown-env rejection breaks an existing security test
`tests/test_config.py:206-218` `test_env_var_cannot_set_config_path` sets `SANELESS_CONFIG_PATH` and asserts `config_path is None`. Under D-13 this now raises `ConfigError`. Rewrite it to assert the rejection names `SANELESS_CONFIG_PATH`. That is strictly stronger for T-26-05.

### Pitfall 10: XDG variables leak into the suite on developer machines and CI
`clean_env` (conftest.py:130-135) removes only `SANELESS_*`. Tests that redirect `HOME` (test_config.py:164, 241; test_auto_profiles.py:454; test_worker.py:3799) will fail wherever `XDG_CONFIG_HOME`/`XDG_STATE_HOME` is set. GitHub-hosted Ubuntu runners are believed to set `XDG_CONFIG_HOME` `[ASSUMED]`. This developer's session sets neither (verified `env`). **Extend `clean_env` to `delenv("XDG_CONFIG_HOME")` and `delenv("XDG_STATE_HOME")`**, and have XDG tests set them explicitly.

### Pitfall 11: Existing tests that encode the old behaviour
The planner must schedule these edits as part of the RED/GREEN steps, not discover them at the gate:
| Test | Old assertion | New |
|---|---|---|
| test_auto_profiles.py:1223 `test_overwrite_with_force` | `auto_generated = false` profile overwritten under force | D-01: skipped, file unchanged |
| test_auto_profiles.py:1180-1241 (several) | `written` is a `list[str]` | `ProfileWriteResult` fields |
| test_config.py:173 `test_missing_explicit_path_is_recorded` | missing explicit path recorded | `ConfigError` naming the path (CFG-02) |
| test_config.py:206 `test_env_var_cannot_set_config_path` | silently ignored | rejected (D-13) |
| test_config.py:229 `test_config_search_paths_order` | `Path.home()/.config` | `xdg_config_home()` with XDG unset, plus a set-XDG case |
| test_config.py:40, 55 | `settings.paperless.token == expected` | `.get_secret_value()` |
| test_config.py:322-336 | `default_title_template` | `default_title` |
| test_config.py:78 (default missing, `ValueError`), 442 (flip timeout via TOML, `ValidationError`) | raw pydantic exception from `load_settings` | `ConfigError` (it is not a `ValueError` subclass); keep `match=` on the key name |
| test_config.py:253 invalid TOML `ValueError` | unchanged; tomllib errors are Phase 28 | do not catch `TOMLDecodeError` in the renderer |
| test_cli.py:212 `test_scan_requires_title` | exit != 0 without `--title` | exit 0; title falls back |
| test_cli.py:1219 `test_serve_log_level` | `"info"` | unchanged (still valid) |
| test_logging.py:134 `test_default_log_file_is_xdg_compliant` | `.local/state/saneless` in default | still true with XDG unset; add a set-XDG case |
| test_outcomes_e2e.py:663, 734 | `token=settings.paperless.token` into `PaperlessClient` | `.get_secret_value()` |

`PaperlessConfig(token="str")` in conftest and tests stays valid. pydantic accepts a `str` at runtime, and **ty and pyrefly both accepted `P(token=auth)` for a `SecretStr` field** `[VERIFIED: probe]`.

### Pitfall 12: The `saneless` logger level leaks between tests
`configure_logging(verbose=True)` sets `saneless` to DEBUG. The existing cleanup helpers (test_logging.py:20-26, test_cli.py:940-946) restore only root handlers and level. Reset `saneless` to NOTSET in `configure_logging` when not verbose, and add the reset to those cleanups.

## Code Examples

### CFG-11 startup line
```python
# Source: EnvSettingsSource usage verified against saneless.config.Settings
def env_sourced_keys() -> list[str]:
    """Dotted names of settings supplied by SANELESS_* variables. Names only, never values."""
    def leaves(node: Mapping[str, object], prefix: tuple[str, ...]) -> Iterator[str]:
        for key, value in node.items():
            if isinstance(value, Mapping) and value:
                yield from leaves(cast("Mapping[str, object]", value), (*prefix, key))
            else:
                yield ".".join((*prefix, key))
    return sorted(leaves(EnvSettingsSource(Settings)(), ()))

def log_config_sources(settings: Settings) -> None:
    """Log, at INFO, which file was loaded and which keys the environment set (CFG-11)."""
    source = str(settings.config_path) if settings.config_path else "no config file; defaults + environment"
    keys = env_sourced_keys()
    logger.info("Configuration: %s; from environment: %s", source, ", ".join(keys) or "(none)")
```
Test with `caplog.at_level(logging.INFO, logger="saneless.config")` and a `SANELESS_PAPERLESS__TOKEN=tok-SECRET` env: the record contains `paperless.token` and not `tok-SECRET`.

### SecretStr behaviour (verified)
```python
repr(settings)                       # ... token=SecretStr('**********') ...
settings.model_dump()                # {'paperless': {'token': SecretStr('**********'), ...}}
settings.model_dump(mode="json")     # {'paperless': {'token': '**********', ...}}
settings.paperless.token.get_secret_value()   # 'real-SECRET'
```
Construction sites that must unwrap `[VERIFIED: grep]`: `src/saneless/cli.py:236`, `src/saneless/web/app.py:62`, `tests/test_outcomes_e2e.py:663`, `tests/test_outcomes_e2e.py:734`. `PaperlessClient` keeps `token: str` (paperless.py:215 builds the header).

### EBUSY test without Docker
```python
def test_single_file_bind_mount_is_a_config_error(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("[profiles.default]\n", encoding="utf-8")
    def busy(_src: object, _dst: object) -> None:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
    monkeypatch.setattr("saneless.atomic_write.os.replace", busy)
    with pytest.raises(ConfigError, match="Mount its directory"):
        replace_file_atomically(target, "x = 1\n")
    assert target.read_text(encoding="utf-8") == "[profiles.default]\n"
    assert list(tmp_path.iterdir()) == [target]          # temp file cleaned up
```
Optional real-mount test (skip unless `unshare --user --map-root-user --mount true` succeeds). It is verified locally, but CI user namespaces may be restricted `[ASSUMED]`.

### chown without root
Monkeypatch `os.fchown` to record `(uid, gid)` and assert it received the original `st_uid`/`st_gid`. A second test makes it raise `PermissionError` and asserts the write still succeeds. Mode: create the target with `0o640` and assert `stat.S_IMODE(target.stat().st_mode) == 0o640` after the write.

## Docs and Compose Change List

Line numbers re-verified this session. **Bold** entries are ones CONTEXT's canonical_refs did not list.

| File:line | Current wording (abridged) | Change |
|---|---|---|
| `docker-compose.yml:4` | "bind config.toml to /etc/saneless/config.toml (read-only)" | directory mount, read-write, holds config.toml |
| `docker-compose.yml:16-19` | "WARNING: config.toml must exist… `./config.toml:/etc/saneless/config.toml:ro`" | `./config:/etc/saneless`; a missing config.toml means defaults plus env and the container still starts. **Leave line 12 (image) and 23-28 (environment block) alone.** |
| `saneless.toml.example:21` | `# title = ""` | comment that it is a literal default title used when the title is left blank |
| `docs/how-to/deploy-docker-compose.md:14` | "Create a `config.toml` on the host" | create `./config/config.toml` |
| `docs/how-to/deploy-docker-compose.md:27-28` | "must exist… mounted read-only… will fail to start" | truthful: missing means defaults plus env, container starts; the directory must be writable for profile writes |
| `docs/how-to/deploy-docker-compose.md:52, 71` | `./config.toml:/etc/saneless/config.toml:ro` "read-only bind mount" | `./config:/etc/saneless`, explain why (atomic replace needs the directory; single-file mount gives EBUSY) |
| `docs/how-to/deploy-docker-compose.md:~180-215` | upgrade section | **add a migration note for existing single-file mounts** |
| `docs/reference/docker.md:31` | "`/etc/saneless/config.toml` Configuration file (mount read-only) Yes" | `/etc/saneless` directory, read-write |
| `docs/reference/docker.md:97, 121, 137, 155` | `./config.toml:/etc/saneless/config.toml:ro` | `./config:/etc/saneless` |
| **`docs/getting-started/quick-start.md:19`** | `-v ./config.toml:/etc/saneless/config.toml:ro` | directory mount (leave the `$(pwd)` question to DOCS-06) |
| `docs/reference/configuration.md:11` | "`~/.config/saneless/config.toml` -- XDG config directory" | `$XDG_CONFIG_HOME/saneless/config.toml` (default `~/.config/…`) |
| `docs/reference/configuration.md:44, 45` | `~/.local/state/…` "(XDG state directory)" | `$XDG_STATE_HOME/saneless` (default `~/.local/state/saneless`); note that `~` is expanded in path settings |
| `docs/reference/configuration.md:46` | "Log level: DEBUG, INFO, WARNING, ERROR" | add CRITICAL; case-insensitive; anything else is rejected at load |
| `docs/reference/configuration.md:34` | token row | optional: never logged or echoed |
| `docs/reference/configuration.md:72, 119, 127` | "Default title template" | literal default title used when the title is blank |
| **`docs/reference/configuration.md` (new note)** | — | unknown keys in any section are rejected with the valid keys listed |
| `docs/how-to/configure-scan-profiles.md:60, 178` | "Default title template…" | literal; typed title takes precedence, then `Scan <time>` |
| `docs/how-to/configure-scan-profiles.md:145, 153` | "Use `--force` to overwrite existing auto-generated profiles"; "Profiles you wrote yourself are never touched." | merge semantics per D-02/D-03: owned keys refreshed in place; `default_tags`, `title`, and comments kept; unflagged profiles skipped and reported |
| **`docs/how-to/configure-scan-profiles.md:165`** | "because it is mounted read-only, as in the Docker Compose examples" | no longer true of the examples; say "for example, a read-only mount or a single-file bind mount (EBUSY)" |
| `docs/reference/cli-commands.md:12` | "Enable debug logging (sets log level to DEBUG)" | `-v` logs saneless's own DEBUG detail to the log file and mirrors to stderr; library loggers keep the configured level |
| `docs/reference/cli-commands.md:21, 26` | `scan --title TEXT`, "*(required)*" | `[--title TEXT]`, default: profile `title`, else `Scan <time>` |
| **`docs/reference/cli-commands.md:138, 148, 152`** | "`--force` Overwrite existing profiles in config file"; "Without `--force`, existing profiles are preserved" | merge semantics plus grouped output; exit 2 on the single-file-mount EBUSY |
| `docs/how-to/cli-scripting.md:77` (CONTEXT said ~71) | "missing config file" example for exit 2 | already says it; now true. Optionally add "unknown config key or `SANELESS_*` variable". Scripts may drop `--title`. |
| `docs/explanation/empty-page-detection.md:54` | "run saneless with `--log-level DEBUG`" | `saneless -v …` (or `log_level = "DEBUG"`) |
| `docs/reference/environment-variables.md` | (no statement about unknown names) | add: a `SANELESS_*` name whose first segment is not `scanner`, `paperless`, `output`, or `profiles` is rejected at startup (exit 2) |
| **`docs/reference/environment-variables.md:61`** | "Profile fields cannot be set via environment variables" | Doc row 5, owned by DOCS-01 (Phase 31). It is false (verified `SANELESS_PROFILES__RECEIPT__TITLE` works) and sits next to this phase's edit. Planner may fix it here opportunistically or leave it. |
| `docs/explanation/architecture.md:85` | "search paths follow XDG conventions" | now true; optionally mention `$XDG_CONFIG_HOME` |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `populate_by_name=True` | `validate_by_name=True` (+ `validate_by_alias`) | pydantic 2.11 | Optional; no warning on 2.12.5 (verified with `-W error`) `[CITED: pydantic docs alias.md]` |
| `path.with_suffix('.tmp')` + `os.replace` | `mkstemp(dir=…)` + fsync + `os.replace` + directory fsync | long-standing POSIX practice | Unique temp name, correct permissions, durable rename |
| Mounting a single config file into a container | Mounting the containing directory | — | Atomic replace needs rename inside the mount (verified EBUSY) |

**Deprecated/outdated:** the `_VALID_SECTIONS` tuple (config.py:336), removed in favour of `Settings.model_fields` (D-11).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | GitHub-hosted Ubuntu runners set `XDG_CONFIG_HOME` | Pitfall 10 | Low. The recommended conftest `delenv` is correct either way. |
| A2 | Directory `fsync` can fail with EINVAL on some FUSE, network, or Docker Desktop mounts | Pitfall 7 | Low. Best-effort suppression is harmless if it never fails. |
| A3 | Unprivileged user namespaces (`unshare -r`) may be restricted on CI runners (AppArmor on Ubuntu 24.04) | Code Examples (optional real-mount test) | Low. The test is optional and `skipif`-guarded; the monkeypatch test is the requirement's evidence. |

Everything else was verified by a probe, a grep, or official docs in this session.

## Open Questions

1. **Mode and owner of a *newly created* config file** (the CLI `auto-profiles` with no loaded config writes `./saneless.toml`)
   - What we know: D-06 covers only an existing file. mkstemp leaves `0o600`. Today's `write_text` gives `0o666 & ~umask` (usually 0644), owned by the writer.
   - What's unclear: whether a new file should be 0600 (it will hold a token) or umask-derived.
   - Recommendation: keep `0o600`. It is the safer default for a secrets-bearing file, and nothing that runs in the container creates one: the worker never creates a file (D-09 carry-forward). State it in the docs.
2. **CLI write target in the container when no config is loaded.** The runtime image has no `WORKDIR`, so `docker exec … saneless auto-profiles` with an empty `/etc/saneless` writes `/saneless.toml` inside the container, outside any mount. CONTEXT keeps `./saneless.toml` as the target. Recommendation: leave it, but have the CLI output name the absolute resolved path (it already prints the path, so use `result.path`). The compose how-to can tell users to `touch ./config/config.toml` first. Do not change the target without a user decision.
3. **"Skipped" wording without `--force`** (D-04 leaves this to the planner). Recommendation: a separate group, `Skipped (already exists; use --force to refresh): …`, so the D-01 reason stays unambiguous.
4. **DOCS-02 wording is now stale.** REQUIREMENTS DOCS-02 says README should show "the required `--title`", but D-16 makes `--title` optional. This is flagged for the Phase 31 owner, and no action is needed in this phase beyond the cli-commands synopsis.
5. **uvicorn under `-v`.** Recommendation: uvicorn follows the configured `log_level` only.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | all | yes | 3.14.2 | — |
| uv | all commands | yes | (project tool) | — |
| pydantic / pydantic-settings / tomlkit / click / uvicorn | code | yes | 2.12.5 / 2.13.1 / 0.14.0 / 8.3.1 / 0.42.0 | — |
| `unshare` (util-linux) with user namespaces | optional real EBUSY test | yes, locally (verified working) | — | monkeypatched `os.replace` |
| docker / podman | optional manual compose check | yes (`/usr/bin/docker`, `/usr/bin/podman`) | — | not needed |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none. The optional items are nice-to-have only.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest >=9.0.2 with pytest-timeout (60 s, signal); `filterwarnings = ["error"]`; `--strict-markers --strict-config` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (lines ~150-162) |
| Quick run command | `uv run pytest tests/test_config.py tests/test_atomic_write.py tests/test_auto_profiles.py tests/test_logging.py tests/test_cli.py -x -q` (today's four existing files: 261 tests in about 1.5 s) |
| Full suite command | `uv run pytest -m "not browser and not sane_hardware"` (the CI `test` job) |
| Gate | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CFG-01 | `[paperless] tokne` gives a message naming `paperless`, `'tokne'`, did-you-mean `'token'`, valid keys | unit | `uv run pytest tests/test_config.py -k unknown_key -x` | tests/test_config.py yes (new tests) |
| CFG-01 | `[profiles.default] resoluton` gives `[profiles.default]` plus `'resolution'` suggestion; `title` shown in valid keys | unit | `… -k profile_unknown_key` | yes |
| CFG-01 | `[paperless] web_port` gives "belongs in [output]"; `[Paperless]` gives "did you mean [paperless]" and no `[profiles.Paperless]` | unit | `… -k wrong_section` | yes |
| CFG-01 | all errors reported, one per line, under a header naming the file; type errors in `[section] key: msg` form | unit | `… -k renders_every_error` | yes |
| CFG-01/D-12 | `SANELESS_SCANNER__HOSTNAME` error line names the variable (case-insensitive spelling preserved) | unit | `… -k attributes_env` | yes |
| CFG-01/D-13 | `SANELESS_PAPERLES__TOKEN` rejected with hint; `SANELESS_OUTPUT__DATA_DIR`, `SANELESS_PROFILES__RECEIPT__TITLE`, JSON `SANELESS_OUTPUT` accepted; `SANELESS_CONFIG_PATH` rejected | unit | `… -k unknown_env` | yes (rewrite test at :206) |
| CFG-01/D-14 + CFG-05 | token-shaped value in a mistyped key absent from the message, `repr`, the exception chain (`__cause__` None, `__suppress_context__`), and CliRunner stderr | unit + CLI | `… -k never_echoes` | yes |
| CFG-02 | `load_settings("/nope.toml")` and a directory path give `ConfigError` naming the expanded path; `saneless --config /nope.toml jobs` exits 2 with the path; auto-discovery skips a directory named `saneless.toml` | unit + CLI | `uv run pytest tests/test_config.py tests/test_cli.py -k "missing_config or not_a_file" -x` | yes (flip :173) |
| CFG-03 | `XDG_CONFIG_HOME` honoured in search paths; empty or relative ignored; `XDG_STATE_HOME` moves `data_dir`/`log_file` defaults after import; `~` expanded in tmp_dir, data_dir, log_file, consume_dir, and `--config`; `validate_settings_dirs` flags an unwritable grandparent | unit | `uv run pytest tests/test_config.py -k "xdg or expanduser or ancestor" -x` | yes |
| CFG-04 | `log_level="trace"` gives ConfigError naming `[output] log_level`; `"warn"` gives WARNING; `-v` makes `saneless.pipeline` effective level DEBUG while root and `httpx` stay at the configured level; not-verbose resets | unit | `uv run pytest tests/test_config.py tests/test_logging.py -k "log_level or verbose" -x` | yes |
| CFG-04 | `serve` passes a valid uvicorn level | CLI | `uv run pytest tests/test_cli.py -k serve_log_level` | yes (existing) |
| CFG-05 | `repr(settings)` and `model_dump(mode="json")` lack the token; CFG-11 log line lacks the token (caplog); construction sites unwrap | unit | `uv run pytest tests/test_config.py -k secret -x` | yes |
| CFG-06 | blank or whitespace typed title gives the profile `title`, else `Scan <UTC>`; typed wins; `saneless scan --profile receipt` without `--title` succeeds and prints `Done: Receipt`; web `POST /api/scan` with empty title creates a job titled from the profile | unit + CLI + TestClient | `uv run pytest tests/test_config.py tests/test_cli.py tests/test_web.py -k title -x` | yes (flip test_cli :212) |
| CFG-07 | `--force` on a flagged profile keeps `default_tags`, `title`, and a comment, overwrites `resolution`, deletes a stale `duplex`; unflagged same-name profile byte-identical under force; result groups correct; orphan shows as removed; CLI prints grouped lines | unit + CLI | `uv run pytest tests/test_auto_profiles.py tests/test_cli.py -k "force or merge or grouped" -x` | yes (flip :1223) |
| CFG-07 | worker consumes the result; memory equals what a restart loads | unit | `uv run pytest tests/test_worker.py -k startup_generation -x` | yes |
| CFG-08 | temp in the same directory, removed on write, fsync, or replace failure; mode 0640 preserved; fchown called with the original ids and EPERM tolerated; symlink target replaced and link kept; CRLF and `# café` preserved byte-for-byte except edited lines; EBUSY gives ConfigError with the fix text and the original file untouched; read-only (0444) file refused; invalid dumped TOML refused (inline `profiles = {…}`) | unit | `uv run pytest tests/test_atomic_write.py tests/test_auto_profiles.py -k "atomic or crlf or utf8 or ebusy or symlink or inline" -x` | test_atomic_write.py NO (Wave 0) |
| CFG-08 | `auto-profiles` EBUSY gives exit 2 with the message, no traceback; worker EBUSY keeps profiles in memory with a WARNING naming ConfigError | CLI + worker | `uv run pytest tests/test_cli.py tests/test_worker.py -k ebusy -x` | yes |
| CFG-09 | `docker-compose.yml` has `./config:/etc/saneless` with no `:ro` and no `config.toml:/etc/saneless/config.toml`; no doc under `docs/` contains `:/etc/saneless/config.toml:ro` or "fail to start if the file is missing" | static text | `uv run pytest tests/test_deployment_config.py -x` | NO (Wave 0) |
| CFG-09 (SC3) | `auto-profiles --force` succeeds when the config sits in a directory-mounted location | unit (directory in tmp_path) plus optional `unshare` real-mount test | `uv run pytest tests/test_atomic_write.py -k bind_mount` | NO (Wave 0) |
| CFG-10 | `serve --help`, `scan --help`, `auto-profiles --help` exit 0 with `load_settings` raising; `configure_logging` not called; with a real broken config, the log directory is not created | CLI | `uv run pytest tests/test_cli.py -k help -x` | yes |
| CFG-11 | one INFO record after `configure_logging` naming the config path (or "no config file") and `paperless.token, paperless.url`, with no values | CLI (caplog) | `uv run pytest tests/test_cli.py -k config_sources -x` | yes |

### Sampling Rate
- **Per task commit:** the quick run command above (under 5 s).
- **Per wave merge:** full non-browser suite plus the gate command.
- **Phase gate:** full suite green, `uv run prek run --all-files`, and `uv run prek run --stage pre-push --all-files` before `/gsd-verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_atomic_write.py`: covers CFG-08 helper behaviour (EBUSY, mode, chown, symlink, cleanup, read-only, dir fsync best effort)
- [ ] `tests/test_deployment_config.py`: CFG-09 text assertions on `docker-compose.yml` and `docs/**/*.md`
- [ ] `tests/conftest.py`: extend `clean_env` to remove `XDG_CONFIG_HOME` and `XDG_STATE_HOME`
- [ ] Cleanup helpers in `tests/test_logging.py` and `tests/test_cli.py` (TestLegacyDuplexWarningReachesLogFile) reset `logging.getLogger("saneless")` level
- No framework install needed.

## Security Domain

`security_enforcement` is absent from `.planning/config.json`, which means enabled. Target: ASVS Level 1.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No login in scope (DOCS-05 is later). The token is an outbound credential. |
| V3 Session Management | no | — |
| V4 Access Control | partial | Config write target is only `Settings.config_path` (PrivateAttr, unforgeable from env or TOML, and `SANELESS_CONFIG_PATH` is now rejected outright) |
| V5 Validation, Sanitization and Encoding | **yes** | pydantic `extra="forbid"`, `Literal`, bounded ints, `max_length` on the profile title; `!r` on user-controlled names in error lines (control-character and log-forgery escaping) |
| V6 Stored Cryptography | no | No crypto. Never hand-roll it. |
| V7 Error Handling and Logging | **yes** | Errors carry location and msg only; `from None` on ValidationError; CFG-11 logs names not values; the CFG-05 test proves no token in logs |
| V8 Data Protection | **yes** | `SecretStr` for the token; unwrap only at the two client construction sites |
| V12 Files and Resources | **yes** | `mkstemp` in the target directory (O_EXCL, 0600, random name); mode and owner copied before rename; symlink resolved once; temp removed on every failure path; read-only intent honoured; tomllib guard prevents writing corrupt config |
| V14 Configuration | **yes** | Unknown keys and env vars rejected; missing explicit config is fatal; directory mount documented |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Token echoed via pydantic `input` / `str(ValidationError)` / chained traceback | Information disclosure | Render from `loc` and `msg` only; `raise ConfigError(...) from None`; regression test on message, repr, and chain |
| Token in `repr(settings)` or a debug log of settings | Information disclosure | `SecretStr` (verified `'**********'` in repr and JSON dump) |
| Env typo silently unsets the token (`SANELESS_PAPERLES__TOKEN`), leading to confusing auth failures | Tampering / Repudiation | D-13 rejection at startup |
| Forged write target via env (`SANELESS_CONFIG_PATH`) | Tampering / Elevation | PrivateAttr (Phase 26) plus D-13 rejection |
| Control characters in TOML keys forging stderr or log lines | Spoofing / Tampering | `!r` formatting of every user-controlled name |
| Truncated or corrupt config after a crash mid-write | Denial of service | temp, fsync, `os.replace`, directory fsync; tomllib guard |
| Temp file readable by others during the write window | Information disclosure | mkstemp 0600; mode widened to the original's only after contents are written and synced, just before rename (no wider than the file already was) |
| Pre-created or symlinked temp path (`config.toml.tmp`) redirecting a root write | Tampering / Elevation | mkstemp random name with O_EXCL (never a fixed suffix) |
| Symlink at the config path redirecting a root container write to an arbitrary file | Tampering / Elevation | **Accepted, no regression.** `write_text` already followed symlinks. The path and its directory are operator-controlled. Resolve once and log link to target (D-07). Document that the config directory must not be writable by untrusted users. |
| Ownership change making the host file root-owned (a usability issue that can lead to a sudo edit habit) | — | fchown to the original uid/gid when permitted (D-06) |
| Read-only config silently rewritten because rename ignores file mode | Tampering | `os.access(W_OK)` pre-check (Pitfall 6) |

## Sources

### Primary (HIGH confidence)
- Local probes (`uv run python`, this session) against pydantic 2.12.5, pydantic-settings 2.13.1, tomlkit 0.14.0, click 8.3.1, uvicorn 0.42.0: error types and locs, SecretStr repr and dump, `str(ValidationError)` leak, `EnvSettingsSource` output and case folding, JSON-valued env, `SettingsError`, `default_factory` timing, tomlkit in-place, dotted, inline, split, and CRLF behaviour, Click callback order, ty and pyrefly acceptance of `SecretStr` str input and alias forms
- Local kernel probes: `unshare --user --map-root-user --mount` plus `mount --bind` gives EBUSY on a file mount and success on a directory mount; mkstemp mode; fchown EPERM; symlink write-through; directory fsync
- Context7 `/pydantic/pydantic`: alias validation (`validate_by_name`, `validate_by_alias`, `populate_by_name`)
- Context7 `/pydantic/pydantic-settings`: `case_sensitive`, `env_nested_delimiter`, JSON parsing of complex env values
- XDG Base Directory Specification v0.8: https://specifications.freedesktop.org/basedir/latest/ (default and relative-path rules, quoted)
- Codebase (grep and read): `src/saneless/{config,cli,auto_profiles,logging_config,worker,web/app,web/routes,vocabulary}.py`, `tests/{conftest,test_config,test_cli,test_auto_profiles,test_logging,test_worker}.py`, `docker-compose.yml`, `Dockerfile`, `saneless.toml.example`, all listed docs, `.github/workflows/ci.yml`, `pyproject.toml`

### Secondary (MEDIUM confidence)
- `.planning/reviews/2026-09-09-code-review.md` M-09, M-10, M-18..M-21, M-24, M-30, N-15, N-25, U-01, doc table rows (source findings; claims re-verified above where load-bearing)

### Tertiary (LOW confidence)
- GitHub runner `XDG_CONFIG_HOME` and user-namespace availability, and directory-fsync EINVAL on FUSE (Assumptions A1-A3). Mitigations are correct regardless.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH. No additions; versions read from the running environment.
- Architecture: HIGH. Each pattern's core behaviour was executed. The mechanism choices (lazy loader, `EnvSettingsSource` oracle, `default_factory` on the field) follow directly from probe results.
- Pitfalls: HIGH. Eleven of twelve were reproduced or read directly from code and tests. Pitfall 7 is MEDIUM (assumed filesystem behaviour, mitigation harmless).

**Research date:** 2026-09-15
**Valid until:** 2026-10-15 (stable libraries; re-check if pydantic-settings or tomlkit are bumped by Dependabot before planning executes)
