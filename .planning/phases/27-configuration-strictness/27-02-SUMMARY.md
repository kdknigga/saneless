---
phase: 27-configuration-strictness
plan: 02
subsystem: config-persistence
tags: [atomic-write, fsync, bind-mount, ebusy, symlink, tdd]
requires: []
provides:
  - "saneless.atomic_write.replace_file_atomically(path: Path, text: str) -> Path"
affects:
  - "27-04 (wires replace_file_atomically into write_profiles_to_config)"
  - "27-05 (directory-mount compose/docs change that ships with it)"
tech-stack:
  added: []
  patterns:
    - "mkstemp in target dir + fsync + Path.replace + finally-flag cleanup"
    - "fchown (PermissionError suppressed) before fchmod"
    - "EBUSY -> ConfigError with msg variable, chained from the OSError"
    - "real-kernel test via unshare user+mount namespace, literal argv, paths in env"
key-files:
  created:
    - src/saneless/atomic_write.py
    - tests/test_atomic_write.py
  modified: []
decisions:
  - "Read-only existing target refused with PermissionError via os.access(W_OK) before any temp file (Pitfall 6)"
  - "New file keeps mkstemp 0600; no fchown/fchmod when there is no original"
  - "Directory fsync is best effort (contextlib.suppress(OSError)) after the rename"
  - "Real-kernel unshare bind-mount tests included: they pass ruff with no suppression and skip when user namespaces are unavailable"
metrics:
  duration: "~20 min"
  completed: 2026-09-15
  tasks: 2
  files: 2
requirements: [CFG-08, CFG-09]
---

# Phase 27 Plan 02: Atomic config replace primitive Summary

`replace_file_atomically` writes UTF-8 bytes to a `mkstemp` file beside the real (symlink-resolved) target, copies the original owner and mode, fsyncs it, renames it with `Path.replace`, and fsyncs the directory as a best effort. It removes the temp file on every failure. It refuses a read-only target and turns an EBUSY rename (a config bind-mounted as a single file) into a `ConfigError` that tells the operator to mount the directory.

## What was built

- **`src/saneless/atomic_write.py`** exports `replace_file_atomically(path, text) -> Path`, which returns `path.resolve()`.
  - D-05: temp file from `tempfile.mkstemp(dir=target.parent, prefix=".<name>.", suffix=".tmp")`. Bytes are written with no newline translation, then `os.fsync` runs before `tmp.replace(target)`. A `replaced` flag in `finally` unlinks the temp file on any exception, including KeyboardInterrupt. `_fsync_directory` then fsyncs the directory with `OSError` suppressed.
  - D-06: `os.fchown(fd, st_uid, st_gid)` runs with `PermissionError` suppressed, then `os.fchmod(fd, S_IMODE(st_mode))`. Both happen before any content is written.
  - D-07: the function writes through symlinks, and the temp file is created in the real file's directory.
  - D-08: `errno.EBUSY` raises `ConfigError("Cannot replace <target>: it is bind-mounted as a single file. Mount its directory instead (see docs/how-to/deploy-docker-compose.md).")`. Other `OSError`s are re-raised unchanged. There is no fallback.
  - Pitfall 6: an existing target that fails `os.access(W_OK)` raises `PermissionError(EACCES)` before any temp file exists. This keeps `tests/test_worker.py`'s unwritable-file test meaningful.
- **`tests/test_atomic_write.py`** has 18 tests:
  - CRLF/UTF-8 byte exactness
  - 0600 for a new file
  - no leftover temp files
  - temp file created in the target directory (recorded `mkstemp`)
  - fsync of the file before the replace (recorded order)
  - fsync EIO and replace EXDEV failures leave the original intact and no temp file
  - a failed directory fsync does not fail the write, and the test checks that the directory fsync was attempted
  - symlink write-through
  - read-only refusal (skipped under root)
  - 0640 mode preserved
  - `fchown(original uid/gid)` called before `fchmod`
  - a refused fchown is skipped
  - a new file copies neither owner nor mode
  - monkeypatched EBUSY becomes `ConfigError`
  - directory-mount layout (`config/config.toml`)
  - real single-file bind mount inside `unshare --user --map-root-user --mount`: the kernel returns EBUSY, which becomes ConfigError
  - real directory bind mount: the host `config.toml` is replaced

## Commits

| Task | Gate | Commit | Message |
|------|------|--------|---------|
| 1 | RED | 5bd692e | test(27-02): add failing tests for atomic config replace |
| 1 | GREEN | 39c21e4 | feat(27-02): add replace_file_atomically durable config write |
| 2 | RED | e1d824c | test(27-02): add failing tests for mode/owner copy and EBUSY translation |
| 2 | GREEN | 3c31cab | feat(27-02): preserve mode/owner and translate EBUSY to ConfigError |

## Verification

- `uv run pytest tests/test_atomic_write.py -q`: 18 passed. `-k "atomic or crlf or utf8 or symlink or readonly"` selected 10 at Task 1. `-k "bind_mount or ebusy"` selects 4.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean. The 4 pyrefly warnings are hidden by default and do not come from these files; a pyrefly run on just the two new files reports 0.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1450 passed.
- Acceptance greps:
  - `os.replace`, `write_text`, `read_text`, `noqa`, and `type: ignore` do not appear in the source.
  - `.replace(target)` is present.
  - The `os.fchown` line (133) comes before the `os.fchmod` line (134).
  - `errno.EBUSY` and `Mount its directory instead` are present.
- On this host (kernel 5.14), the Task 2 RED run showed that the real kernel really returns `[Errno 16] Device or resource busy` when renaming over a single-file bind mount.

## Deviations from Plan

**1. [Rule 3 - Blocking] Docstring and comment wording changed to satisfy literal acceptance greps**
- **Found during:** Task 2 acceptance check
- **Issue:** The paperless-style docstring named ``os.replace``, and a comment named ``write_text``. The plan's acceptance criteria require `grep "os.replace"` and `grep "write_text\|read_text"` to return nothing.
- **Fix:** Reworded both to explain the same point without the literal names.
- **Files modified:** src/saneless/atomic_write.py
- **Commit:** 3c31cab

**2. Test 1 RED commit import grouping**
- At RED time `saneless.atomic_write` did not exist, so the commit hook's ruff treated it as third-party and merged it into the `pytest` import group. The Task 1 GREEN commit put the blank-line grouping back once the module existed. The only effect is cosmetic churn in the test file's import block.

**Optional unshare test included, not omitted:** it passes `ruff check` with no suppression. All argv elements are literals (`/usr/bin/unshare`, `/bin/sh -c '<literal script>'`), and the per-test paths, `sys.executable`, and the Python snippet travel through `env`, so S603/S607 do not fire. A fixture probe skips it when `/usr/bin/unshare` is missing or unprivileged user/mount namespaces are refused, as they may be on CI. The monkeypatched EBUSY test remains the requirement's evidence.

## Known Stubs

None. Nothing calls this module until plan 27-04, as the plan intends.

## Threat Flags

None. The only new surface is the file-write path, and the plan's threat model covers it (T-27-write, T-27-05..09).

## Self-Check: PASSED

- FOUND: src/saneless/atomic_write.py
- FOUND: tests/test_atomic_write.py
- FOUND commits: 5bd692e, 39c21e4, e1d824c, 3c31cab
