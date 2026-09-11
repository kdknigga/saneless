---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 04
subsystem: paperless
tags: [httpx, api-versioning, monotonic-deadline, strenum, atomic-rename, fsync, tdd]

# Dependency graph
requires:
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    plan: 01
    provides: "PaperlessTimeoutError(PaperlessError) and the five-member ConnectionStatus StrEnum with its total message lookup"
provides:
  - "An Accept: application/json; version=9 header on every paperless-ngx request"
  - "_extract_task / _task_status / _failure_message: both-wire-shape tolerance for API v9 and v10"
  - "_TERMINAL_STATUSES = {SUCCESS, FAILURE, REVOKED}, matching paperless-ngx's COMPLETE_STATUSES"
  - "poll_task returning only the successful task dict, raising PaperlessError on FAILURE/REVOKED with the Paperless message, raising PaperlessError on any non-200 on the first poll, and raising PaperlessTimeoutError on a time.monotonic() deadline that includes request time"
  - "A bounded (500-char) rendering of any interpolated upstream response body"
  - "test_connection returning ConnectionStatus, CONNECTED only for a 2xx, with httpx.TransportError (not just ConnectError) mapped to UNREACHABLE"
  - "PaperlessClient._deliver_to_consume_dir: staged dotfile write, fsync, atomic same-directory rename, unlink-on-failure"
affects: [23-05, 23-06, 23-08, 23-09]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Explicit API-version negotiation plus shape tolerance -- pin the version AND parse both shapes, because either alone leaves a failure mode open"
    - "Monotonic deadline with a time.sleep(min(delay, remaining)) clamp, so a poll never sleeps past its own deadline"
    - "Length-bounded interpolation of untrusted upstream bodies into messages that reach persistent storage and the UI"
    - "Atomic handoff to a watched directory: hidden .part staging file, fsync, same-filesystem rename, unlink-on-failure"

key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - tests/test_paperless.py

key-decisions:
  - "D-17: pin Accept: application/json; version=9 AND parse both wire shapes -- the code already assumed v9, so pinning makes a hidden assumption visible, and tolerating both means the client survives v9 support eventually being dropped"
  - "D-12: any non-200 raises on the first poll; the 4xx-raise / 5xx-retry split upload_document uses was rejected by name"
  - "A 200 carrying no task is still not an error -- Pitfall #8's race keeps being tolerated inside the deadline"
  - "REVOKED is terminal alongside FAILURE, because it is one of paperless-ngx's COMPLETE_STATUSES and polling it would produce a misattributed timeout"
  - "D-13: test_connection classification stays an ordered integer chain, not match + assert_never -- the input is a range of integers, not a closed set of members"
  - "httpx.TransportError was verified against the installed httpx 0.28.1 (it is the base of ConnectError, ConnectTimeout and ReadTimeout) rather than taken from memory"
  - "The consume-dir staging file is a dotfile, not merely a .part file -- the inotify consumer skips hidden files outright, which is the stronger half of RESEARCH assumption A3"
  - "The staged file is fsynced; the consume directory is not -- it is a handoff, not a system of record"
  - "Path.replace rather than os.replace, forced by ruff PTH105 and CLAUDE.md's ban on noqa; Path.replace delegates to os.replace with identical semantics"

patterns-established:
  - "Wire-shape parametrisation: a (payload_builder) fixture table with ids v9/v10, so a single test body proves the same property against both API versions and `-k api_version` selects the whole set"
  - "Handler call counters as the assertion that a failure is reported immediately rather than after a timeout"
  - "Named-extension assertion messages: the staging-file check explains why a dotfile/.part name exists, so a failure is legible without reading the implementation"
---

# Phase 23 Plan 04: Honest paperless-ngx Client Summary

**`poll_task` now speaks both paperless-ngx wire versions, raises instead of returning sentinels, honours its documented timeout on a monotonic clock, `test_connection` distinguishes five outcomes instead of claiming "connected" for a 404, and the consume-directory copy is atomic.**

## Performance

- **Duration:** ~24 min
- **Started:** 2026-09-11T15:25:54Z
- **Completed:** 2026-09-11T15:49:49Z
- **Tasks:** 3
- **Files modified:** 2

## Accomplishments

- Every request carries `Accept: application/json; version=9` beside `Authorization`. The client already *assumed* v9 while paperless-ngx serves v10 to a header-less client, so this turns a silent assumption into an explicit contract.
- `poll_task` reaches a terminal status against **both** wire shapes: v9's bare list with uppercase statuses and a flat `result` string, and v10's `{"count","next","previous","results"}` with lowercase statuses and `result_data["error_message"]`. This was the latent catastrophe the plan was sequenced around — `isinstance(tasks, list)` was `False` against a real v10 server, so no terminal status was *ever* observed, masked only because `pipeline.py` discards the return value.
- `FAILURE` and `REVOKED` raise `PaperlessError` carrying the message paperless-ngx supplied, from whichever of the two fields holds it. A failure with neither field still raises, with a stand-in message, because that string is what OUTC-01 records as the job error and it must never be empty.
- `REVOKED` is terminal. It is one of paperless-ngx's `COMPLETE_STATUSES`; the old code looped on it, which after this phase would have turned an administrative cancellation into a misattributed 300-second timeout.
- Any non-200 raises on the **first** poll, proven by a handler counter asserting exactly one request. A revoked token is now reported as a 401 in under a second instead of as a timeout five minutes later.
- A 200 carrying no task still does not raise. Pitfall #8's "the task is not visible immediately after the upload that created it" race is preserved for both wire shapes, and `_extract_task` returning `None` says so in its docstring.
- The deadline is `time.monotonic()`-based and includes request time. The old `elapsed` accumulator counted only sleep time, so with a 30 s per-request `httpx` timeout the real ceiling was roughly `timeout + 30 * polls`, unbounded relative to the documented 300 s. `time.sleep(min(delay, remaining))` clamps the backoff so a poll never sleeps past its own deadline — asserted by a wall-clock test that a 0.05 s budget costs under 0.5 s, 0.5 s being exactly the old unconditional first sleep.
- The `{"status": "TIMEOUT"}` sentinel is gone. A deadline-expired poll raises `PaperlessTimeoutError` naming the task id, so the user can look the task up in paperless before acting on the preserved file.
- `test_connection` returns a `ConnectionStatus` member and reports `CONNECTED` only for a 2xx. 404 is `NOT_FOUND` (the API is not where the URL points) and 5xx is `SERVER_ERROR` (paperless-ngx itself is unwell) — two different things to fix that were both previously reported as `"connected"`.
- `httpx.ConnectTimeout` and `httpx.ReadTimeout` no longer escape to `routes.py`'s blanket handler and surface as HTTP 502 `{"status": "error"}`. The handler was broadened to `httpx.TransportError`.
- The consume-directory copy stages to a hidden `.{name}.part` file, flushes, `fsync`s, and renames atomically **inside** the consume directory. `shutil.copy2` previously wrote incrementally into a directory paperless-ngx watches with inotify.
- A copy that dies part-way unlinks its staging file and re-raises, so a failed handoff leaves nothing truncated for a retry or the consumer to find.

## Task Commits

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | API-version pin, both-shape task parsing, monotonic-deadline `poll_task` | `0286e33` | `src/saneless/paperless.py`, `tests/test_paperless.py` |
| 2 | `test_connection` returns `ConnectionStatus` with five outcomes | `fdf90ca` | `src/saneless/paperless.py`, `tests/test_paperless.py` |
| 3 | Staged write plus atomic rename for the consume directory | `ea61d81` | `src/saneless/paperless.py`, `tests/test_paperless.py` |

Task 1 was deliberately not split, per the plan's explicit instruction. Landing the raise-on-timeout semantics without the v10 shape fix in the same commit would have made every *successful* scan record as a timeout.

## Verification

| Check | Result |
|-------|--------|
| `uv run pytest -q` | 684 passed |
| `uv run pytest tests/test_paperless.py -q` | 56 passed |
| `uv run pytest tests/test_paperless.py -k api_version -q` | 9 passed (criterion: >= 4) |
| `uv run pytest tests/test_paperless.py -k "poll or api_version" -q` | 15 passed |
| `uv run pytest tests/test_paperless.py -k connection -q` | 20 passed |
| `uv run pytest tests/ -q -k "routes or web"` | 80 passed |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| No `# type: ignore` / `# noqa` / disabled rule in the diff | confirmed — the only `noqa`s in the repo are pre-existing and in other files |
| `git diff --name-only` lists `src/saneless/web/routes.py` | no — routes.py is untouched |

Grep criteria on `src/saneless/paperless.py`:

| Pattern | Required | Actual |
|---------|----------|--------|
| `time.monotonic` | >= 2 | 2 |
| `elapsed` | 0 | 0 |
| `TIMEOUT` | 0 | 0 |
| `REVOKED` | >= 1 | 4 |
| `Accept` | >= 1 | 4 |
| `min(delay, remaining)` | 1 | 1 |
| `httpx.TransportError` | >= 1 | 1 |
| `return "connected"` | 0 | 0 |
| `Distinguishes three states` | 0 | 0 |
| `shutil.copy2` | 0 | 0 |
| `unlink(missing_ok=True)` | >= 1 | 1 |
| `fsync` | 1 | 4 — one call, three docstring mentions (see Deviations) |
| `os.replace` | 1 | 2 docstring mentions, **zero calls** (see Deviations) |

### Confirmations recorded rather than assumed

- **`httpx.TransportError` is the right base class.** Verified by inspecting the MRO of the installed httpx 0.28.1: `ConnectError`, `ConnectTimeout` and `ReadTimeout` all list `TransportError`. (`httpx.InvalidURL` does *not* — it derives straight from `Exception` — and is out of scope here.)
- **`web/routes.py` needs no edit.** Confirmed by serialising through the same path FastAPI uses: `JSONResponse(content=jsonable_encoder({"status": ConnectionStatus.CONNECTED})).body` is `b'{"status":"connected"}'`, byte-identical to the plain-string form. All five members serialise to their documented values. The 80 web/route tests pass unchanged.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `os.replace` is a ruff `PTH105` violation, so the rename uses `Path.replace`**

- **Found during:** Task 3
- **Issue:** The plan stated that "`os.replace` on `Path` arguments is NOT a ruff `PTH` violation; `PTH` targets `os.path`". That is incorrect. `PTH105` is specifically `os.replace() should be replaced by Path.replace()`, and ruff flagged the line. CLAUDE.md forbids `# noqa` and forbids disabling rules, and per the executor contract CLAUDE.md takes precedence over plan instructions.
- **Fix:** The call is `staged.replace(dest)`. This is not a weakening of the guarantee: `pathlib.Path.replace` delegates to `os.replace` and has identical semantics, including the unconditional overwrite of an existing destination that `Path.rename` does not guarantee on all platforms. The helper's docstring states this explicitly, names `os.replace` as the underlying primitive, and records why it is not called directly.
- **Consequence for the plan's acceptance criteria:** `grep -c 'os.replace' src/saneless/paperless.py` returns 2, but **both matches are docstring prose, not a call**. Flagging this explicitly rather than letting the grep pass silently: the `key_links` entry asserting an `os\.replace` pattern in this file is satisfied only in the documentation sense. The *property* it was checking — an atomic same-directory rename — is genuinely present and is proven by tests, not by the grep.
- **Files modified:** `src/saneless/paperless.py`
- **Commit:** `ea61d81`

**2. [Rule 3 - Blocking] `pyrefly check` with no arguments is a no-op inside a `.claude/worktrees/` worktree**

- **Found during:** Task 1 verification
- **Issue:** `uv run pyrefly check` skipped every file, reporting `No Python files matched patterns`, because the worktree lives under `.claude/worktrees/` and pyrefly honours `.gitignore`, which ignores `.claude/`. It exits 0 while checking nothing — a silently vacuous gate.
- **Fix:** Verification used `uv run pyrefly check src tests` with explicit paths, which checks the files and reports `0 errors`. No config change was made: this is an artefact of the worktree location, not of the repo, and the `prek` pyrefly hook passes file paths explicitly so it is unaffected (confirmed — it ran and passed on all three commits).
- **Files modified:** none
- **Commit:** n/a

### Deliberate departures from the plan's letter

**3. `fsync` appears on four lines, not one.** One is the `os.fsync(staged_file.fileno())` call; the other three are the helper docstring explaining the durability decision the plan asked to be recorded (file fsync yes, directory fsync no). The acceptance criterion's intent — exactly one fsync call — is met.

**4. TDD RED and GREEN are combined in each commit.** `prek` runs `ty` and `pyrefly` on every commit and both reject a test file referencing symbols or behaviour that does not exist yet; `--no-verify`, `# type: ignore` and `SKIP=` are all forbidden by CLAUDE.md or by the plan. Tests were written and run first, red was observed, and the observed red output is recorded in each commit message:
   - Task 1: 13 failed, 2 passed on `-k "poll or api_version"`. The only two passes were the v9 success and v9 no-task cases — precisely the v9-only behaviour the old code had, which is itself evidence the parametrisation is discriminating.
   - Task 2: 14 of 20 failed on `-k connection`, including 404/500/503/302/429 all classifying as `connected` and `ReadTimeout` escaping the handler.
   - Task 3: `test_a_failed_staged_write_leaves_the_directory_empty` failed with `DID NOT RAISE OSError` — `shutil.copy2` uses `sendfile` on Linux and never reaches the patched copy loop, and nothing cleaned up after a partial write.

   This constraint was independently hit by all three wave-1 executors and is documented in the plan's critical notes.

## Threat Mitigations Applied

| Threat | Mitigation |
|--------|------------|
| T-23-15 (spoofing: unpinned API version) | Both halves landed: `Accept: application/json; version=9` pins the contract, and `_extract_task` / `_task_status` / `_failure_message` tolerate both shapes so the client survives v9 support being dropped. |
| T-23-16 (info disclosure: unbounded error body) | `_truncated_body` caps the interpolated body at 500 characters with an explicit `[truncated, N characters total]` marker. Only the status code and that excerpt are interpolated — no request headers, no base URL. Asserted by `test_non_200_body_is_truncated`, which feeds a 10 000-character body and requires the resulting message under 1 000 characters. |
| T-23-17 (DoS: unbounded poll wall clock) | `time.monotonic()` deadline including request time, plus the `min(delay, remaining)` sleep clamp. A monotonic clock also cannot be moved by an NTP step. |
| T-23-18 (DoS: revoked token burning the timeout) | Non-200 raises on the first poll, asserted by a handler counter requiring exactly one request. |
| T-23-19 (tampering: half-written PDF consumed) | Hidden `.{name}.part` staging file, `fsync`, atomic same-directory rename, `unlink(missing_ok=True)` on the failure path. |
| T-23-20 (repudiation: a FAILURE recorded as success) | `poll_task` returns only on SUCCESS; FAILURE and REVOKED raise, and the timeout sentinel is replaced by `PaperlessTimeoutError`. The caller no longer has the option of not noticing. |

No package changes were made in this plan; no `uv add` was run.

## Known Stubs

None.

## Threat Flags

None. No new network endpoint, auth path, file-access pattern or schema change at a trust boundary was introduced beyond the two the threat model already covers, and the consume-directory write surface is narrowed rather than widened.

## Expected Intermediate State

Per the plan's `<verification>` section: `pipeline.py` still discards `poll_task`'s return value and the preservation guard does not exist yet, so a FAILURE or a timeout now propagates out of `run_pipeline`, unwinds the `TemporaryDirectory`, and the PDF is lost — the same behaviour an upload error already has today. Plan 23-06 closes this. It is **not** the catastrophic inversion the phase sequencing warns about, because Task 1 fixed the v9/v10 misparse in the same commit that made timeouts raise, so a successful scan is never mistaken for a timeout.

## Self-Check: PASSED

- `src/saneless/paperless.py` — FOUND
- `tests/test_paperless.py` — FOUND
- Commit `0286e33` — FOUND
- Commit `fdf90ca` — FOUND
- Commit `ea61d81` — FOUND
- No tracked file deleted between the base commit and HEAD — confirmed
- No untracked files left behind — confirmed
