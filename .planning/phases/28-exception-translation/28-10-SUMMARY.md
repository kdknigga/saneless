---
phase: 28-exception-translation
plan: 10
subsystem: paperless-client
tags: [exceptions, httpx, polling, duplicates, tdd]
requires:
  - "saneless.exceptions.describe (28-01)"
  - "saneless.paperless._render_error_body, self._base_url (28-06)"
provides:
  - "PaperlessClient.poll_task keeps polling through httpx.TransportError until its monotonic deadline"
  - "saneless.paperless._DUPLICATE_HINT and _is_duplicate_failure"
  - "saneless.paperless._one_line_reason (one-line cause text; HTTPStatusError as status, reason, body)"
  - "PaperlessClient._fetch_collection; get_tags/get_correspondents raise only PaperlessError"
affects: [28-14]
tech-stack:
  added: []
  patterns:
    - "try/except/else around only the poll GET, so every path falls through to the deadline check"
    - "Per-response reading extracted into _finished_task to keep poll_task under the branch limit"
    - "One-line cause renderer shared by upload exhaustion, poll timeout and metadata fetch messages"
key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - tests/test_paperless.py
decisions:
  - "An HTTPStatusError cause is rendered as '<status> <reason>: <one-line body>' rather than httpx's two-line text, in every message this plan produces and in the exhausted/fast-fail upload message (EXC-02)"
  - "Paperless task failure text is whitespace-collapsed into the one-line message; its length stays unbounded (T-28-41 accepted)"
  - "last_transport_error is not cleared by a later successful 200 PENDING poll: the timeout still names the last transport error seen"
metrics:
  duration: 20min
  completed: 2026-09-15
  tasks: 2
  files: 2
requirements: [EXC-01]
---

# Phase 28 Plan 10: Paperless Read-Side Boundary Summary

A network blip while polling an accepted upload no longer fails the job. `poll_task` logs the transport error and keeps polling within the same monotonic deadline. If the deadline passes, the timeout message names the last error. When a task fails as a duplicate (v9 or v2 text, or v10 `result_data.duplicate_of`), the message tells the user the document may already be in Paperless. `get_tags` and `get_correspondents` now raise only `PaperlessError`. Every message this plan touches is a single line, including the "failed after N attempts" upload message.

## What Was Built

### Task 1: poll through transport errors, duplicate hint, non-JSON poll body
- **`poll_task`**
  - Only the `self._client.get(...)` call is wrapped.
  - `except httpx.TransportError as exc` records the error and logs `Polling task %s failed, retrying until the deadline: %s`.
  - The `else` arm hands the response to `_finished_task`.
  - Both paths reach the unchanged `remaining` check and the clamped `time.sleep`. There is no `continue`.
  - On expiry: with no transport error the message is unchanged. Otherwise `; last error: <reason>` is appended and the error is raised `from` the last transport error.
- **`_finished_task(task_id, response)`**
  - A non-200 still raises `Paperless task poll failed (<status> <reason>): <line>` at once (OUTC-07).
  - A non-JSON 200 raises `Paperless at <url> returned a task response that is not JSON: ...`, chained to the ValueError.
  - SUCCESS returns the task.
  - FAILURE or REVOKED raises `Paperless task <id> ended <STATUS>: <text>`, with its whitespace collapsed. When `_is_duplicate_failure` matches, `; the document may already be in Paperless; check before scanning again` is added.
- **`_is_duplicate_failure(task, message)`**: matches `"duplicate of"` case-insensitively in the text, or a `duplicate_of` key in `result_data`. Its docstring records D-10's accepted risk (`CONSUMER_DELETE_DUPLICATES`).
- **`_one_line_reason(exc)`**: an `HTTPStatusError` becomes `<status> <reason>: <_render_error_body>`. Anything else is `describe(exc)` with its whitespace collapsed.
- **Tests** (`TestPollTaskFailureTranslation`, plus one upload test), all using the `sleeps` recorder:
  - ReadError twice, then SUCCESS: 3 calls, sleeps `[0.5, 1.0]`.
  - ConnectError, empty ReadTimeout and RemoteProtocolError, each raised until a 0.05 s deadline. Each checks the exact message with `last error: describe(exc)`, the cause, no newline, and that every poll but the last was followed by a sleep.
  - Other poll cases:
    - PENDING until the deadline keeps the plain message with no cause.
    - A 401 fails after 1 call.
    - A non-JSON 200 fails, chained to a ValueError.
  - Duplicate and failure-text cases:
    - v9, v2 (capital "Duplicate") and v10 `result_data` duplicates get the hint.
    - A non-duplicate failure gets no hint.
    - A multi-line failure text becomes one line.
  - `test_5xx_exhausted_message_is_one_line`: `Upload to Paperless at http://paperless:8000 failed after 3 attempts: 503 Service Unavailable: down`.

### Task 2: metadata fetches raise only PaperlessError
- **`_fetch_collection(path, noun)`**
  - The GET and `raise_for_status()` sit under `except httpx.HTTPError`. `response.json()` sits under `except ValueError`.
  - Both raise `Could not fetch <noun> from Paperless at <url>: <_one_line_reason>`, chained to the cause.
  - The results-or-list tolerance is kept.
- **`get_tags` / `get_correspondents`**: now one-line delegations. Their docstrings gain a Raises section.
- `routes.py` is untouched.
- **Tests** (`TestMetadataFetchTranslation`)
  - 10 parametrised failure cases: both methods × ConnectError, empty ReadTimeout, 500, 403 and a non-JSON 200. Each checks the exact message, the cause type, that the error is not an httpx type, that there is no newline, and 1 call.
  - 4 success cases: list and paginated responses, for both methods.

## Verification

- `uv run pytest tests/test_paperless.py -k "poll or duplicate" -q`: 27 passed in 1.4 s.
- `uv run pytest tests/test_paperless.py tests/test_outcomes_e2e.py -q`: passed.
- `uv run pytest tests/test_paperless.py -k metadata -q`: 14 passed.
- `uv run pytest tests/test_web.py tests/test_cache.py -q`: 60 passed.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1858 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pyrefly check src tests` all report 0 errors. `uv run prek run --stage pre-push --all-files` passed.
- Acceptance greps:
  - `except httpx.TransportError as exc` matches twice: the upload loop and `poll_task`.
  - `last error: ` matches once.
  - `_DUPLICATE_HINT` matches twice.
  - The poll deadline still uses `time.monotonic()`, and there is no `time.time()`.
  - `Could not fetch {noun} from Paperless at` matches once.
  - `raise_for_status()` is counted 2 times.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1/2 - Bug, EXC-02] Multi-line messages from httpx's HTTPStatusError text (carried forward from 28-06)**
- **Found during:** handed over from 28-06; fixed in Task 1.
- **Issue:** httpx's `str(HTTPStatusError)` for a 5xx is two lines. `Upload to Paperless at <url> failed after N attempts: ...` carried it verbatim, so a CLI or web message spanned two lines. The plan's own metadata messages (500/403 through `raise_for_status`) would have done the same.
- **Fix:**
  - Added `_one_line_reason`. It renders a status error as `<status> <reason>: <one-line body>` and collapses whitespace in anything else.
  - It is used for the exhausted and fast-fail upload reason, the poll `last error`, the non-JSON poll message, and both metadata messages.
  - Poll failure text is also whitespace-collapsed.
  - Tests assert no newline and pin the exact text.
- **Plan wording affected:** Task 2's behaviour said a metadata message "ends with `describe(cause)`". For the 500 and 403 cases it now ends with `500 Internal Server Error: boom` and `403 Forbidden: <detail>`. Transport and non-JSON cases still end with `describe(cause)`.
- **Files modified:** src/saneless/paperless.py, tests/test_paperless.py
- **Commits:** e54491b (test), 72dc5ce (fix)

**2. [Rule 3 - Blocking/structure] Response handling extracted from poll_task**
- **Issue:** Adding transport, JSON and duplicate handling inline would have pushed `poll_task` over the branch limit (PLR0912), as 28-06 found for upload.
- **Fix:** Per-response reading moved into `_finished_task`. `poll_task` uses `try/except/else` instead of the plan's `response = None` sentinel. The behaviour is the same, and only the GET is inside the `try`.
- **Commit:** 72dc5ce

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | e54491b | 72dc5ce |
| 2 | f0494c8 | 66f9591 |

- Task 1 RED: 10 failed and 3 passed. The 3 passing tests pin behaviour that does not change: the plain timeout, a 401 failing at once, and a non-duplicate failure.
- Task 2 RED: 10 failed, all failure cases. The 4 success-shape tests passed, as expected.
- No refactor commits.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/paperless.py (`_one_line_reason`, `_is_duplicate_failure`, `_finished_task`, `_fetch_collection`)
- FOUND: tests/test_paperless.py (`TestPollTaskFailureTranslation`, `TestMetadataFetchTranslation`)
- FOUND commits: e54491b, 72dc5ce, f0494c8, 66f9591
