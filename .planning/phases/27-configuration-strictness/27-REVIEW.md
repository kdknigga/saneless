---
phase: 27-configuration-strictness
reviewed: 2026-09-15T16:03:07Z
depth: deep
files_reviewed: 32
files_reviewed_list:
  - src/saneless/atomic_write.py
  - src/saneless/auto_profiles.py
  - src/saneless/cli.py
  - src/saneless/config.py
  - src/saneless/logging_config.py
  - src/saneless/web/app.py
  - src/saneless/web/routes.py
  - src/saneless/worker.py
  - tests/conftest.py
  - tests/test_atomic_write.py
  - tests/test_auto_profiles.py
  - tests/test_cli.py
  - tests/test_config.py
  - tests/test_deployment_config.py
  - tests/test_logging.py
  - tests/test_outcomes_e2e.py
  - tests/test_web.py
  - tests/test_worker.py
  - docker-compose.yml
  - saneless.toml.example
  - docs/explanation/architecture.md
  - docs/explanation/empty-page-detection.md
  - docs/getting-started/first-cli-scan.md
  - docs/getting-started/quick-start.md
  - docs/how-to/cli-scripting.md
  - docs/how-to/configure-scan-profiles.md
  - docs/how-to/deploy-docker-compose.md
  - docs/how-to/install-bare-metal.md
  - docs/reference/cli-commands.md
  - docs/reference/configuration.md
  - docs/reference/docker.md
  - docs/reference/environment-variables.md
findings:
  critical: 2
  warning: 8
  info: 7
  total: 17
status: issues_found
---

# Phase 27: Code Review Report

**Reviewed:** 2026-09-15T16:03:07Z
**Depth:** deep
**Files Reviewed:** 32
**Status:** issues_found

## Narrative Findings (AI reviewer)

## Summary

I reviewed the config loader and error renderer, the atomic write helper, the tomlkit merge,
the lazy CLI settings, logging, the worker's persist path, and the docs, against D-01..D-17.
The token handling holds up. I found no path that echoes `input`, `ctx`, or a chained
`ValidationError`, and `SettingsError` text names only the field. The atomic write orders its
steps correctly: fsync before rename, chown before chmod, and temp cleanup in `finally`.

I checked the merge by running `write_profiles_to_config` against real files. Two defects are
confirmed:

1. The ownership test (`_is_auto_generated`) does not read the flag the way the loader does.
   A hand-written profile can be overwritten or deleted as a result, which breaks D-01.
2. The "valid TOML" guard only checks that the output parses. It does not check that the output
   means the same thing. One valid TOML layout (dotted keys at the top level) gets rewritten
   into a file that saneless then refuses to load.

Other confirmed problems:

- A legacy `:ro` mount gets a misleading "Permission denied" message.
- `--config ""` quietly skips CFG-02.
- A `~user` path escapes the D-10 renderer as a raw `RuntimeError`.
- Environment-variable attribution names the wrong source for JSON-valued variables.

Every item marked "confirmed" was reproduced in a scratch script, or in an unprivileged mount
namespace for the read-only mount.

## Critical Issues

### CR-01: A hand-written profile with a string `auto_generated` is overwritten or deleted (D-01 violation, data loss)

**File:** `src/saneless/auto_profiles.py:448-450` (used at `:709` by the merge and `:808` by the orphan prune)
**Issue:** `_is_auto_generated` returns `bool(table.get("auto_generated", False))`, which is plain
Python truthiness. The loader parses the same value with pydantic's lax `bool`. There, `"false"`,
`"no"`, `"off"`, `"0"`, `"f"` and `"n"` all mean **False**. So the loader says a profile is
hand-written while the writer says the tool owns it. Confirmed:

```toml
[profiles.adf]
source = "mine"
auto_generated = "false"    # load_settings -> auto_generated=False

[profiles.zzz]
source = "z"
auto_generated = "no"       # load_settings -> auto_generated=False
```

- `auto-profiles --force` reports `Refreshed: 'adf'` and replaces `source = "mine"` with the
  generated source.
- Any `auto-profiles` run, with or without `--force`, reports `Removed: 'zzz'` and deletes the
  table.

D-01 says a profile without a truthy `auto_generated` "is never touched". The docs say the
same: "A profile without `auto_generated = true` is never changed".

**Fix:** Decide ownership the way the loader does, and treat anything that does not validate as
not owned:
```python
from pydantic import TypeAdapter, ValidationError

_BOOL = TypeAdapter(bool)

def _is_auto_generated(table: object) -> bool:
    if not isinstance(table, Mapping):
        return False
    try:
        return _BOOL.validate_python(table.get("auto_generated", False))
    except ValidationError:
        return False
```
A stricter option is `value is True`. Add a test with `"false"` and `"no"` for both the refresh
path and the prune path.

### CR-02: The merge guard accepts output that parses but means something different, and the result will not load

**File:** `src/saneless/auto_profiles.py:650-665` (`_render_checked`), triggered by `_merge_profile` at `:700-707`
**Issue:** The guard only runs `tomllib.loads(new_text)`. tomlkit can emit valid TOML whose
meaning has changed. With top-level dotted profile keys, adding a table moves the second dotted
line under the new header. Confirmed input, run **without** `--force`:

```toml
profiles.default.source = "x"
profiles.default.auto_generated = true
```

Output:

```toml
profiles.default.source = "x"

[profiles.adf]
...
auto_generated = true
profiles.default.auto_generated = true   # now profiles.adf.profiles.default.auto_generated
```

`tomllib` accepts this, so the file is replaced. The next `load_settings` then fails with
`[profiles.adf] unknown key 'profiles'`. Every command stops working, `auto-profiles` included,
until the operator edits the TOML by hand. The code comment promises the opposite: "text that
does not parse never replaces a working config".

**Fix:** Check that the output means what the merged document means, not just that it parses:
```python
try:
    reparsed = tomllib.loads(new_text)
except tomllib.TOMLDecodeError:
    reparsed = None
if reparsed != doc.unwrap():
    msg = (f"Cannot update {config_path}: the merged profiles would not round-trip "
           "through TOML; refusing to rewrite it, so the file is left intact")
    raise ConfigError(msg) from None
```
Add the dotted-key file above as a regression test, for both force and no-force runs.

## Warnings

### WR-01: A legacy `:ro` single-file mount reports "Permission denied" instead of the mount fix

**File:** `src/saneless/atomic_write.py:111-115`
**Issue:** On a read-only filesystem, `os.access(target, os.W_OK)` returns False (EROFS), even
for root. The helper then raises `PermissionError(EACCES)`. Confirmed in a user and mount
namespace with `mount -o remount,bind,ro`: the result is
`PermissionError [Errno 13] Permission denied`. The CLI prints
`Cannot write /etc/saneless/config.toml: Permission denied`, and the worker logs the same text.

The old docs recommended exactly this `:ro` single-file mount, so this is the most common state
an upgrading user is in. They never see D-08's "Mount its directory instead" message, and the
real errno (EROFS) is replaced with a wrong one. That sends operators after file permissions.
**Fix:** Detect a read-only filesystem before the `os.access` check and report it as a
`ConfigError` that names the fix:
```python
if original is not None and os.statvfs(target).f_flag & os.ST_RDONLY:
    msg = (f"Cannot replace {target}: it is on a read-only mount. Mount its directory "
           "read-write instead (see docs/how-to/deploy-docker-compose.md).")
    raise ConfigError(msg)
```

### WR-02: Owner and mode copying can abort a valid write, and a non-root rewrite silently loses the group

**File:** `src/saneless/atomic_write.py:132-134`
**Issue:** Two problems:
- **Only `PermissionError` is suppressed around `fchown`.** In a rootless container or user
  namespace, a host file owned by an unmapped uid shows up as the overflow uid. `fchown` to that
  uid fails with `EINVAL`. Some FUSE, CIFS and vfat mounts return `EOPNOTSUPP` or `EINVAL`.
  Confirmed by simulating `EINVAL`: the whole rewrite fails with `OSError [Errno 22]`, even
  though replacing the file would have worked. The same applies to `fchmod` on filesystems
  without Unix permission support.
- **Non-root, group-writable file.** Consider a config owned by `root:saneless` with mode 0664,
  written by the `saneless` service user. `fchown(uid, gid)` fails as a whole with EPERM, so the
  group is not kept either, even though `fchown(fd, -1, gid)` would succeed.

`docs/how-to/configure-scan-profiles.md:147` and `docs/reference/cli-commands.md:150` say a
rewrite "keeps its mode and owner", and that is not true in either case.
**Fix:**
```python
try:
    os.fchown(fd, original.st_uid, original.st_gid)
except OSError:
    with contextlib.suppress(OSError):
        os.fchown(fd, -1, original.st_gid)   # at least keep the group
with contextlib.suppress(OSError):  # or log at DEBUG
    os.fchmod(fd, stat.S_IMODE(original.st_mode))
```
Also qualify the docs: the owner is kept "when the process is permitted to set it".

### WR-03: A `~user` path or an unknown home escapes the D-10 renderer as a raw `RuntimeError`

**File:** `src/saneless/config.py:172` (`_expand_user`), `src/saneless/config.py:1105` (`load_settings`)
**Issue:** `Path.expanduser()` raises `RuntimeError("Could not determine home directory.")`
for `~nosuchuser/...`, and for `~` when there is no HOME or passwd entry. pydantic only turns
`ValueError` and `AssertionError` into validation errors, so the exception passes straight
through `Settings()` and `_build_settings`. Confirmed: `data_dir = "~bogususer/x"` reaches the
caller as `RuntimeError`. The CLI's generic handler then prints
`Configuration error: Could not determine home directory.`, which names no file, section or key.
That breaks D-10. `--config ~bogus/c.toml` fails the same way.
**Fix:**
```python
def _expand_user(value: str) -> str:
    if not value:
        return value
    try:
        return str(Path(value).expanduser())
    except RuntimeError:
        msg = "cannot expand '~': unknown user or home directory"
        raise ValueError(msg) from None
```
In `load_settings`, catch the same `RuntimeError` and raise
`ConfigError(f"Cannot expand '~' in --config path: {config_path}")`.

### WR-04: Environment-variable attribution blames the wrong source (D-12)

**File:** `src/saneless/config.py:772-786` (`_env_variable_for`)
**Issue:** The shorter-prefix fallback runs even when walking `env_data` stopped early, which
means the failing key did not come from the environment. Confirmed cases:
- **False positive.** Set `SANELESS_OUTPUT='{"web_port": 1234}'` and put `[output] tmpdir = "x"`
  in the TOML. The error reads `environment variable 'SANELESS_OUTPUT': unknown key 'tmpdir'`,
  but the typo is in the file. `SANELESS_PROFILES='{...}'` with a TOML profile typo behaves the
  same way.
- **False negative.** Set `SANELESS_PAPERLESS__URL__X=1` with a TOML file that has no
  `[paperless]` section. The error reads `[paperless] url: Input should be a valid string`, under
  the header `Configuration error in <file>`. It blames the file for a key the file does not
  contain.

**Fix:** Fall back to a shorter prefix only when every string element of `loc` was found in
`env_data`. When the node at `loc` is an env-sourced mapping, look for variables that start with
the candidate followed by `__`:
```python
str_parts = [e for e in loc if isinstance(e, str)]
if parts != str_parts[: len(parts)] or len(parts) < len(str_parts):
    return None  # value not (fully) from the environment
...
if isinstance(node, dict):
    deeper = [n for f, n in by_folded.items() if f.startswith(candidate + "__")]
    if deeper:
        return sorted(deeper)[0]
```

### WR-05: `--config ""` skips CFG-02 and quietly uses a discovered file

**File:** `src/saneless/config.py:1104`
**Issue:** `if config_path:` treats an empty string as "no path given". Confirmed:
`load_settings("")` loads `./saneless.toml`. A script running
`saneless --config "$CFG" auto-profiles --force` with `CFG` unset or empty does not exit 2, as
CFG-02 and `docs/how-to/cli-scripting.md` promise. Instead it loads, and writes to, whichever
file discovery finds (`./saneless.toml`, the XDG config, or `/etc/saneless/config.toml`).
**Fix:** `if config_path is not None:`. An empty string then reaches `Path("").is_file()`, which
is False, and raises `ConfigError`. Add a CLI test for `--config ""`.

### WR-06: Docker docs say `auto-profiles` writes into the mounted directory, but with no `config.toml` it writes `/app/saneless.toml`

**File:** `docs/how-to/deploy-docker-compose.md:34`, `src/saneless/cli.py:581`
**Issue:** The note says the directory must be writable "because `saneless auto-profiles` and
the profile generation at startup rewrite `config.toml` there". It also says (D-09) that an empty
directory is a supported state. In that state, `settings.config_path` is None, so the CLI writes
`./saneless.toml` relative to the image's `WORKDIR /app`. That file:
- lives in the container layer, so recreating the container loses it;
- is first in the search order, so later `docker compose exec saneless saneless ...` commands,
  and a `docker compose restart`, load it instead of a `config.toml` the operator adds to
  `./config` afterwards.

The operator sees `Profiles in /app/saneless.toml` and nothing in `./config`.
**Fix:** At minimum, correct the doc: with no `config.toml`, create it first (for example
`touch config/config.toml`), or run `saneless --config /etc/saneless/config.toml auto-profiles`.
Better, when no file was loaded and `/etc/saneless` is a writable directory, have the CLI refuse
and name that path, instead of writing to CWD.

### WR-07: The "unwritable log exits 2" claim is untrue, and its test only exercises a mocked path

**File:** `src/saneless/cli.py:197-201`, `src/saneless/logging_config.py:52-65`, `tests/test_cli.py:1179-1197`
**Issue:** The `_load_cli_settings` docstring says an unwritable log (M-21) gives "one message
and exit 2, never a traceback". But `configure_logging` catches `OSError` from `mkdir` and
`RotatingFileHandler`, falls back to stderr, and returns normally, so the command keeps running.
`test_logging_setup_failure_is_a_config_error_exit_2` replaces `configure_logging` with a stub
that raises `OSError`, which the real function never does. The test passes against behaviour the
product does not have.
**Fix:** Pick one behaviour. Either have `configure_logging` re-raise (as `ConfigError` naming
`log_file`), or correct the docstring to "falls back to stderr". Either way, test with the real
`configure_logging` and an unwritable `log_file` path.

### WR-08: CRLF handling rewrites the line endings of mixed-ending files (D-05)

**File:** `src/saneless/auto_profiles.py:651-654`
**Issue:** If a single `\r\n` appears anywhere in the original, every bare `\n` in the output is
converted, including lines the user wrote with LF. Confirmed: `[output]\r\nweb_port = 1\n...`
comes back as all CRLF. The conversion also changes the value of any multi-line string that
contained a bare LF. D-05 says "newline handling must not rewrite the user's line endings".
**Fix:** Normalise only the lines tomlkit added. One way is to pick the ending by majority and
skip the rewrite when the original already mixes endings. Another is to add new tables with an
explicit `\r\n` trivia instead of post-processing the whole text. At least, do not touch the
text when `original_text.count("\r\n") != original_text.count("\n")`.

## Info

### IN-01: The symlink log line fires for paths that are not symlinks

**File:** `src/saneless/auto_profiles.py:835-838`
**Issue:** `target != config_path.absolute()` compares a resolved path with an unnormalised one.
`--config ../cfg/config.toml`, or a working directory reached through a symlink (for example
`/home` -> `/var/home`), logs `(symlink to ...)` for a regular file.
**Fix:** Use `if config_path.is_symlink():`, or compare against
`Path(os.path.abspath(config_path))`.

### IN-02: `ProfileWriteResult.persisted` does not do what its docstring says for refreshed profiles

**File:** `src/saneless/auto_profiles.py:517-520`, `src/saneless/worker.py:149-153`
**Issue:** "Names whose file table now matches the generated profile": a refreshed table keeps
`default_tags`, `title` and the other user keys, so it does not match. `_profiles_after_persist`
would put the bare generated profile in memory, without those keys, which is not what a restart
loads. The worker never forces today, so this cannot happen yet, but the contract invites it.
**Fix:** Reword the docstring. If the worker ever refreshes, rebuild memory profiles from the
file, or keep `refreshed` out of `persisted`.

### IN-03: `write_profiles_to_config` has two `Args:` sections in its docstring

**File:** `src/saneless/auto_profiles.py:759-772`
**Fix:** Delete the first `Args:` block and keep the one after the CFG-08 paragraph.

### IN-04: The test env cleanup is case-sensitive, but the loader is not

**File:** `tests/conftest.py:139-141`
**Issue:** `key.startswith("SANELESS_")` misses lowercase variables such as `saneless_foo`.
`_unknown_env_lines` and pydantic-settings both fold case, so a developer's lowercase variable
breaks unrelated tests.
**Fix:** `if key.upper().startswith("SANELESS_"):`

### IN-05: Invalid JSON in an environment variable hides every TOML validation error

**File:** `src/saneless/config.py:965-978`
**Issue:** When `_env_contribution()` raises `SettingsError`, `Settings()` is never built, so
file errors are not reported until the variable is fixed. That falls short of D-10's "every
validation error is reported". Low impact.
**Fix:** Acceptable if intended. Otherwise say so in the header, for example "fix the
environment first".

### IN-06: The `configure_logging` "idempotent" comment overstates what happens

**File:** `src/saneless/logging_config.py:52-77`
**Issue:** Only the `saneless` logger level is idempotent. Every call adds more root handlers.
If the file handler falls back to stderr and `verbose` is also set, two stderr handlers are
attached and every record prints twice.
**Fix:** Skip the verbose stderr handler when the fallback already added one, and remove
previously added saneless handlers on each call.

### IN-07: The XDG `data_dir` default moves existing state without a migration note

**File:** `src/saneless/config.py:135-154`, `docs/reference/configuration.md` (`data_dir` row)
**Issue:** Before this phase the default was always `~/.local/state/saneless`. A bare-metal user
who sets `XDG_STATE_HOME` to something else now starts with an empty job database, and their
`failed/` scans are left behind at the old path with no warning. This is D-level behaviour, but
the docs give no upgrade note.
**Fix:** Add a sentence to the configuration reference or the upgrade notes: move
`~/.local/state/saneless` to `$XDG_STATE_HOME/saneless`, or pin `data_dir`.

---

_Reviewed: 2026-09-15T16:03:07Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
