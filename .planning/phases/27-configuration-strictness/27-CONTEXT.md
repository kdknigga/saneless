# Phase 27: Configuration Strictness - Context

**Gathered:** 2026-09-15
**Status:** Ready for planning

<domain>
## Phase Boundary

A wrong config is caught at load with a message that names the right place, and a config rewrite
is durable on the deployment the docs recommend. Delivers CFG-01..CFG-11 (review findings M-09,
M-10, M-18, M-19, M-20, M-21, M-24, M-30, N-15, N-25, and the logging half of U-01; doc rows 6,
8, 11, 13, 14, 20, 27):

- Nested `extra="forbid"` on every config model, with every validation error rendered by its
  full `loc` (CFG-01)
- A `--config` path that is missing (or is not a file) exits 2 naming the path (CFG-02)
- `~` expansion on path settings; `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` for defaults (CFG-03)
- `log_level` validated; `-v` really means DEBUG (CFG-04)
- Paperless token as `SecretStr` (CFG-05)
- The profile `title` key is actually read (CFG-06)
- `auto-profiles --force` merges instead of replacing (CFG-07)
- Atomic, UTF-8, mode-preserving, comment-preserving config rewrites (CFG-08) shipped together
  with the config-*directory* mount in compose and docs (CFG-09)
- `saneless <subcommand> --help` needs no valid config (CFG-10)
- Loaded config path and env-sourced key names logged at INFO on startup (CFG-11)
- The configuration reference, CLI reference, scripting how-to, empty-page explanation, Docker
  reference, and compose how-to/example corrected in-phase for every sentence this phase makes true

**Not in this phase:**
- Placeholder/empty token detection and refusing or flagging it — APPL-07, **Phase 30**
- Commenting out the compose `environment:` block / the Paperless service in the example —
  DOCS-06 / APPL-11, later phases. This phase changes only the config volume line and its comments.
- `kris-knigga` → `kdknigga` image and URL rename — **Phase 31** (leave the image line alone)
- Wrapping `tomllib`/`tomlkit` parse errors as saneless exceptions — EXC, **Phase 28**. (This
  phase's error renderer handles pydantic `ValidationError`; a TOML syntax error keeps today's path.)
- Local-time rendering of timestamps, including the `Scan <timestamp>` fallback title — APPL-12,
  **Phase 30**
- A per-profile title hint in the web form — APPL-05 territory, **Phase 30**

</domain>

<decisions>
## Implementation Decisions

### `auto-profiles --force` merge (CFG-07)
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

### Atomic rewrites and the Docker mount (CFG-08, CFG-09)
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

### Bad-config messages (CFG-01, CFG-02, CFG-05)
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

### Profile title (CFG-06)
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

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/REQUIREMENTS.md` lines 97-109: CFG-01..CFG-11 verbatim
- `.planning/ROADMAP.md` § "Phase 27: Configuration Strictness": goal, 5 success criteria, and
  the CFG-08/CFG-09 must-ship-together note
- `.planning/ROADMAP.md` lines ~20 (ordering note 4): why the directory mount and atomic write
  share a phase

### Review findings (the source of every CFG requirement)
- `.planning/reviews/2026-09-09-code-review.md` § M-09 (line ~469): `--force` replaces
  hand-written profiles
- same file § M-10 (~479): non-atomic, locale-encoded rewrite
- same file § M-18 (~598), M-19 (~608), M-20 (~618), M-21 (~628): nested forbid, missing
  `--config`, `~`/XDG, `log_level`/`-v`
- same file § M-24 (~676): inert `title` key
- same file § M-30 (~780): read-only compose mount, false "fails to start" doc claim
- same file N-15 (~869) `SecretStr`, N-25 (~893) `--help` needs config
- same file § U-01 (~1117): env placeholder overrides file; CFG-11 takes only its "log which
  config and which env keys" part
- same file doc-accuracy table rows 6, 8, 11, 13, 14, 20, 27 (~lines 993-1014)

### Prior phase decisions carried forward
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md` D-14/D-15: single
  `data_dir`, hardcoded state-home default kept in step with `log_file` for this phase's XDG edit;
  Dockerfile sets the container `data_dir`
- `.planning/phases/24-scanner-truthfulness/24-CONTEXT.md` D-16: orphan prune of `auto_generated`
  profiles only; unflagged profiles never touched
- `.planning/phases/25-manual-duplex/25-CONTEXT.md` D-06: `duplex` is a generated key, written
  only when non-`none`
- `.planning/phases/26-worker-and-web-robustness/26-CONTEXT.md` D-16..D-18: profiles written only
  to the loaded `config_path`; write failure keeps them in memory

### External specs
- XDG Base Directory Specification — <https://specifications.freedesktop.org/basedir-spec/latest/>
  (unset/empty/relative values are ignored in favour of the defaults)

### Docs to correct in-phase
- `docs/reference/configuration.md` (lines 11, 44 XDG wording; 72, 119, 127 title)
- `docs/how-to/configure-scan-profiles.md` (60, 178 title; `--force` merge sentence)
- `docs/how-to/deploy-docker-compose.md` (14-28, 52, 71, 202 mount and missing-file claims)
- `docs/reference/docker.md` (31, 97, 121, 137, 155 mount lines)
- `docs/reference/cli-commands.md` (12 `-v`; 21 `--title` synopsis)
- `docs/how-to/cli-scripting.md` (exit code 2 for missing config, ~71)
- `docs/explanation/empty-page-detection.md` (54 nonexistent `--log-level DEBUG`)
- `docs/reference/environment-variables.md`: unknown `SANELESS_*` now rejected (D-13)
- `docker-compose.yml`, `saneless.toml.example`

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ConfigError` (`src/saneless/exceptions.py`): the error type for every load/write failure here.
  The CLI group already maps any config failure to `Configuration error: …` and exit 2
  (`cli.py:180-185`).
- `Settings.config_path` PrivateAttr (`config.py:225-243`): the only write target, not settable
  from env or TOML. Keep it that way.
- `config_search_paths()` (`config.py:430-446`): the single search list for load and CLI write
  target, computed at call time. Put the XDG logic here.
- `_is_auto_generated` and `_UNPRUNABLE` (`auto_profiles.py:425-456`): the ownership test and
  guard the merge builds on.
- tomlkit document round-trip already in `write_profiles_to_config` (`auto_profiles.py:459-553`).

### Established Patterns
- Nested models are plain `BaseModel`s with default `extra="ignore"` (`config.py:82-203`). Only
  `ProfileConfig` has a `model_config` (`populate_by_name=True`, which must be kept alongside
  `extra="forbid"`). `Settings` relies on pydantic-settings' implicit forbid; make it explicit.
- `ProfileConfig._translate_legacy_manual_duplex` is a `mode="before"` model validator. It must
  keep working with `extra="forbid"`: it only adds `duplex`, which is a real field.
- Load-time warnings that need the log file are emitted by a function the CLI calls after
  `configure_logging` (`warn_on_legacy_duplex_sources`, Phase 25 WR-05), not from a validator. The
  CFG-11 startup line follows the same pattern.
- Env source order is `(init, env, toml)` (`config.py:245-271`): environment beats file.
- `settings_customise_sources` pops a private `_toml_file` init kwarg. Env-sourced key detection
  for CFG-11 and D-12 can reuse the same prefix/delimiter (`SANELESS_`, `__`).
- Only non-default generated keys are written (`auto_source_mode`, `duplex`). D-03 extends this to
  refresh.

### Integration Points
- `cli.py cli()` group callback (`cli.py:162-198`): lazy loading for `--help`, `-v` level, CFG-11
  log line, `configure_logging` inside the error handling.
- `cli.py auto_profiles` (`cli.py:468-500`): grouped output (D-04), EBUSY/ConfigError exit.
- `cli.py scan` (`cli.py:200-260`): `--title` optional, shared title rule (D-16), `SecretStr`
  unwrap.
- `cli.py serve` (`cli.py:~440-465`): uvicorn `log_level` from the validated value.
- `web/app.py:60-64`: `PaperlessClient` construction unwraps the token.
- `web/routes.py:408-409`: blank-title fallback, replaced by the shared rule.
- `worker.py _persist_generated_profiles` (~`worker.py:800-860`): consumes the new structured
  write result, same vocabulary.
- `logging_config.configure_logging` (`logging_config.py`): `getattr(logging, …)` becomes the
  validated mapping; `-v` DEBUG.
- Tests: `tests/test_config.py`, `tests/test_auto_profiles.py`, `tests/test_cli.py`,
  `tests/test_logging.py`.

</code_context>

<specifics>
## Specific Ideas

- Error line shapes the user accepted (D-10..D-12):
  `[paperless] unknown key 'tokne' (did you mean 'token'?); valid keys: url, token, consume_dir`,
  `[paperless] unknown key 'web_port'; it belongs in [output]`,
  `environment variable SANELESS_SCANNER__HOSTNAME: unknown key 'hostname' in [scanner]`.
- EBUSY message names the fix and the doc page (D-08).
- `auto-profiles` output groups: Added / Refreshed / Skipped (not auto-generated) / Removed
  (scanner no longer offers it) (D-04).
- Verified behaviour worth encoding as tests: pydantic-settings puts a nested env typo at the same
  `loc` as a TOML typo, and silently ignores unknown top-level `SANELESS_*` names.

</specifics>

<deferred>
## Deferred Ideas

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

None of the above were todos; no pending todos matched this phase.

</deferred>

---

*Phase: 27-configuration-strictness*
*Context gathered: 2026-09-15*
