---
phase: 27-configuration-strictness
fixed_at: 2026-09-15T16:20:38Z
review_path: .planning/phases/27-configuration-strictness/27-REVIEW.md
iteration: 1
findings_in_scope: 10
fixed: 10
skipped: 0
status: all_fixed
---

# Phase 27: Code Review Fix Report

**Fixed at:** 2026-09-15T16:20:38Z
**Source review:** .planning/phases/27-configuration-strictness/27-REVIEW.md
**Iteration:** 1

**Summary:**
- Findings in scope: 10 (2 critical, 8 warning; Info excluded by `critical_warning` scope)
- Fixed: 10
- Skipped: 0

Verification after all fixes: `uv run pytest -m "not browser and not sane_hardware"`
passed (1663 tests). `ruff check .`, `ruff format --check .`, `ty check` and
`pyrefly check src tests` are clean, and `prek run --stage pre-push --all-files`
passes. No `# noqa` or `# type: ignore` was added. Except for WR-06 and WR-07,
which are doc and docstring corrections, every fix has a regression test that
failed before the fix was applied.

## Fixed Issues

### CR-01: A hand-written profile with a string `auto_generated` is overwritten or deleted

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** b85a257
**Applied fix:** `_is_auto_generated` now checks the flag with `TypeAdapter(bool)`,
which is the same lax pydantic bool that `ProfileConfig.auto_generated` uses. A value
that fails validation counts as not owned. New tests use `"false"`, `"no"`, `"off"`,
`"0"`, `"f"` and `"n"`. They check that the loader and writer agree, that `--force`
leaves the file byte-for-byte unchanged, and that the prune removes nothing, with and
without force. Two more tests cover `"yes"`, which is still owned, and `"maybe"`, which
is not owned.
**Status:** fixed: requires human verification (logic change)

### CR-02: The merge guard accepts output that parses but means something different

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** 3c8c186
**Applied fix:** `_render_checked` now re-parses the output with `tomllib` and compares
it to `doc.unwrap()`. If the text does not parse, or parses to different data, it raises
`ConfigError` ("does not round-trip through TOML; refusing to rewrite it"), and the file
is not replaced. The D-05 atomic write is unchanged. Both sides go through a small
`_comparable` normaliser first, which handles two harmless parser differences:
- `tomllib` reads CRLF inside a multi-line string as LF, while tomlkit keeps the CRLF.
- NaN never equals itself.

Without this, a CRLF config holding a multi-line string, or a `nan`, would be refused
on every run. Regression tests:
- The dotted-key file from the review, with and without `--force`. The file stays
  unchanged, no temp file is left behind, and the file still loads.
- A stubbed dump that parses to different data.
- A CRLF multi-line string plus `nan`, which must still be accepted.

**Status:** fixed: requires human verification (logic change)

### WR-01: A legacy `:ro` single-file mount reports "Permission denied" instead of the mount fix

**Files modified:** `src/saneless/atomic_write.py`, `tests/test_atomic_write.py`, `docs/how-to/deploy-docker-compose.md`
**Commit:** 9fe1db1
**Applied fix:** Before the `os.access` check, `replace_file_atomically` now calls
`statvfs` on the existing file and on its directory:
- **File on a read-only mount, directory writable:** the file is its own mount point,
  so it raises the D-08 `ConfigError` ("bind-mounted as a single file. Mount its
  directory instead"). The EBUSY path uses the same text through a shared helper.
- **File and directory both read-only:** it raises `ConfigError` saying the file is on a
  read-only mount and the directory should be mounted read-write.
- **Read-only file on a writable mount:** still `PermissionError`.

There is no non-atomic fallback. Tests fake `statvfs` for all three cases. Two
real-kernel tests remount the bind mount read-only in an unprivileged user and mount
namespace, for the single-file case and the directory case. Both ran on this host. The
compose how-to's migration paragraph now says both mount types produce the same message.
**Status:** fixed

### WR-02: Owner and mode copying can abort a valid write, and a non-root rewrite silently loses the group

**Files modified:** `src/saneless/atomic_write.py`, `tests/test_atomic_write.py`, `docs/how-to/configure-scan-profiles.md`, `docs/reference/cli-commands.md`
**Commit:** bddb8d0
**Applied fix:** A new `_copy_owner_and_mode` helper keeps chown before chmod.
- `EPERM`, `EINVAL`, `EOPNOTSUPP` and `ENOTSUP` from `fchown` or `fchmod` are skipped,
  with a DEBUG log line. Any other errno, such as EIO, still fails the write, and the
  temp file is removed.
- If the owner cannot be set, it still tries `fchown(fd, -1, gid)` so the group is kept.

Tests cover EINVAL, EOPNOTSUPP and EPERM, the group-only fallback, an unsupported
`fchmod`, and EIO still failing. The two docs now say each attribute is kept only when
the process is permitted to set it.
**Status:** fixed

### WR-03: A `~user` path or an unknown home escapes the D-10 renderer as a raw `RuntimeError`

**Files modified:** `src/saneless/config.py`, `tests/test_config.py`
**Commit:** f0482f7
**Applied fix:** `_expand_user` now turns the `RuntimeError` into
`ValueError("cannot expand '~': unknown user or home directory")`. The error is rendered
as `[output] data_dir: Value error, cannot expand '~': ...` under the file header, and the
value is not echoed. `load_settings` turns the same error for `--config` into
`ConfigError("Cannot expand '~' in --config path: ...")`. Tests cover a TOML
`data_dir`, `consume_dir` and `--config`.
**Status:** fixed

### WR-04: Environment-variable attribution blames the wrong source (D-12)

**Files modified:** `src/saneless/config.py`, `tests/test_config.py`
**Commit:** f77b08a
**Applied fix:** `_env_variable_for` now returns None when the walk through `env_data`
reaches a key the environment did not supply. That key came from the file, so the
shorter-prefix fallback is skipped. The walk still stops without deciding at a list
index or a scalar leaf, so errors like `default_tags[1]` stay attributed to the
environment. When the value at `loc` is an env-built mapping and no variable matches
the exact path, a variable nested under `<path>__` is named. Tests cover:
- the `SANELESS_OUTPUT` JSON false positive
- the `SANELESS_PROFILES` JSON false positive
- the `SANELESS_PAPERLESS__URL__X` false negative
- a JSON section value that still names its variable

I also checked env type errors on an optional int, a list element and a literal, and all
three are still attributed correctly.
**Status:** fixed: requires human verification (logic change)

### WR-05: `--config ""` skips CFG-02 and quietly uses a discovered file

**Files modified:** `src/saneless/config.py`, `tests/test_config.py`, `tests/test_cli.py`
**Commit:** f587add
**Applied fix:** `load_settings("")` now raises `ConfigError("Config file path is empty
(was --config given an unset variable?)")`. Only `None` triggers discovery. I used a
dedicated message rather than letting `Path("")` report the misleading path `.`. There
is a loader test, and a CLI test showing `--config "" jobs` exits 2 with a discoverable
`./saneless.toml` present and no log directory created.
**Status:** fixed

### WR-06: Docker docs say `auto-profiles` writes into the mounted directory, but with no `config.toml` it writes `/app/saneless.toml`

**Files modified:** `docs/how-to/deploy-docker-compose.md`, `docs/reference/cli-commands.md`, `tests/test_deployment_config.py`
**Commit:** afdb547
**Applied fix:** Docs only. Per the guardrail, the CLI still writes to `./saneless.toml`
(RESEARCH Open Question 2). The compose how-to note now explains what happens when
`config.toml` is missing:
- the server keeps generated profiles in memory
- `docker compose exec saneless saneless auto-profiles` writes `/app/saneless.toml`
  inside the container
- that file is lost when the container is recreated, and until then it loads ahead of
  a later `config.toml`
- users should run `touch config/config.toml` first (I checked that an empty file
  loads)

The CLI reference names `/app/saneless.toml` for the image. New text tests in
`test_deployment_config.py` pin both pages.
**Status:** fixed

**Orchestrator correction (after afdb547):** `/app` was wrong. `WORKDIR /app`
exists only in the Dockerfile's builder stage. The runtime stage sets no
WORKDIR, and `podman image inspect python:3.14-slim` reports an empty
`WorkingDir`, so the container's working directory is `/` and the write lands
at `/saneless.toml`. That matches RESEARCH Open Question 2. Both doc pages and
both text tests were corrected in a follow-up `fix(27): WR-06` commit.

### WR-07: The "unwritable log exits 2" claim is untrue, and its test only exercises a mocked path

**Files modified:** `src/saneless/cli.py`, `src/saneless/logging_config.py`, `tests/test_cli.py`
**Commit:** 44254c9
**Applied fix:** Following the guardrail, the docstrings now match the real behaviour,
and there is no new exit path. `_load_cli_settings`, the generic handler comment, the
`configure_logging` docstring and the `TestLazySettingsLoading` docstring now say an
unwritable `log_file` falls back to stderr and the command keeps running. I found no
real failure in `configure_logging` that should exit 2. I replaced the mocked
`test_logging_setup_failure_is_a_config_error_exit_2` with two tests:
- **Real unwritable log:** real `configure_logging` with `logs` created as a regular
  file, so it fails even as root. Checks exit 0 and the stderr warning naming the log
  file.
- **Generic exit-2 handler:** a real TOML syntax error, replacing the stub.

Both tests pass without a code change, because the behaviour already existed; the fix
corrects the claims.
**Status:** fixed

### WR-08: CRLF handling rewrites the line endings of mixed-ending files (D-05)

**Files modified:** `src/saneless/auto_profiles.py`, `tests/test_auto_profiles.py`
**Commit:** f75f6bf
**Applied fix:** tomlkit's added lines are converted to CRLF only when the original has
at least one CRLF and its CRLF count equals its LF count. A mixed file, or a CRLF file
whose multi-line string holds a bare LF, is left as tomlkit wrote it. Tests:
- a mixed file keeps its original bytes and its single CRLF
- a bare LF inside a multi-line string in an otherwise CRLF file keeps its value

The existing all-CRLF and LF tests still pass.
**Status:** fixed: requires human verification (logic change)

---

_Fixed: 2026-09-15T16:20:38Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
