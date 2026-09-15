---
phase: 28-exception-translation
plan: 06
subsystem: paperless-client
tags: [exceptions, httpx, retry, fallback, tdd]
requires:
  - "saneless.exceptions.describe (28-01)"
provides:
  - "saneless.paperless._render_error_body"
  - "PaperlessClient ctor raises PaperlessError for httpx.InvalidURL"
  - "upload_document raises only PaperlessError; retries every transient TransportError and 5xx"
affects: [28-09, 28-10, 28-14]
tech-stack:
  added: []
  patterns:
    - "Ordered except chain: HTTPStatusError, UnsupportedProtocol, TransportError, HTTPError catch-all"
    - "try/except/else in the retry loop so success returns from the else arm"
    - "Single bounded one-line body renderer shared by upload 4xx and poll non-200"
key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - tests/test_paperless.py
decisions:
  - "UnsupportedProtocol breaks out of the attempt loop with no sleep; it still takes the consume-dir fallback, and without one raises 'Could not reach Paperless at <url>: <text>'"
  - "3xx responses keep their pre-existing treatment (retried like 5xx); only 400-499 fail fast"
  - "max_retries keeps its name; docstring now says 'Maximum number of upload attempts, including the first'"
  - "upload_document split into _form_fields, _post_document, _back_off and _fall_back_to_consume_dir to stay under PLR0912"
metrics:
  duration: 25min
  completed: 2026-09-15
  tasks: 2
  files: 2
requirements: [EXC-01]
---

# Phase 28 Plan 06: Paperless Upload Boundary Summary

The Paperless client's construction and upload path now report failures only as `PaperlessError`. Each message names the base URL and httpx's own text (read through `describe`) and is chained to its cause. A malformed URL fails when the client is built. Every transient transport failure and every 5xx is retried. A scheme-less URL fails fast but still takes the fallback. One renderer turns every Paperless error body into a single line of at most 200 characters.

## What Was Built

### Task 1: one error-body renderer (D-09)
- **`_render_error_body(response: httpx.Response) -> str`**
  - Logs the full body at DEBUG.
  - Parses JSON through `_json_error_text` and picks, in order:
    - DRF `detail` (a string, or a list joined with spaces)
    - the first field whose value is a non-empty string or list, as `field: message`
    - the first string of a top-level list
  - Anything else uses the raw text.
  - Whitespace is always collapsed, JSON-derived text included, so no newline gets through (T-28-24).
  - An empty result becomes `(empty response body)`. Anything longer than `_MAX_BODY_LINE_CHARS = 200` is cut and gets `…`.
- `_first_message` is a small helper shared by the field and list branches.
- `_truncated_body` and `_MAX_ERROR_BODY_CHARS` are deleted.
- A non-200 from `poll_task` now reads `Paperless task poll failed (<status> <reason>): <line>`.
- **Tests**
  - `TestRenderErrorBody`: six parametrised DRF shapes, a 5000-character HTML page, an empty body, a multi-line JSON detail, and the full body at DEBUG.
  - `test_non_200_body_is_truncated` is replaced by `test_non_200_body_is_rendered_as_one_line`, which checks the exact 401 message, and `test_non_200_html_body_cannot_flood_the_message`: a roughly 5 KB page must give one line under 300 characters.

### Task 2: ctor InvalidURL, retry set, fast fail, attempts, wrapped copy (D-08, D-10)
- **`__init__`**
  - Stores `self._base_url`.
  - Builds `httpx.Client(...)` inside a try. `httpx.InvalidURL` becomes `Paperless URL <url> is not valid: <text>`, and the token is never interpolated.
- **`upload_document` loop**, in this order:
  1. `HTTPStatusError`: a 4xx raises `Paperless rejected the upload (<status> <reason>): <rendered body>`; anything else is recorded, then backs off.
  2. `UnsupportedProtocol`: recorded, `fast_fail`, `break`.
  3. `TransportError`: recorded, then backs off.
  4. `HTTPError`: raises `Could not upload to Paperless at <url>: <text>`.
  - On success the `else` arm returns the `UploadResult`.
- **Helpers split out of `upload_document`**
  - `_post_document`: holds the one attempt. `response.json()` has its own try, and a `ValueError` there becomes `Paperless at <url> returned a response that is not JSON: ...`. The null-task-id check is kept.
  - `_back_off`: logs the warning with `describe(exc)` via `%s` args, then sleeps `2**attempt`, except after the last attempt.
  - `_fall_back_to_consume_dir`: puts the mkdir and the atomic delivery inside `except OSError`, which raises `Could not copy the PDF to the consume directory <dir>: <text>`. On success it logs `Upload failed; copied PDF to %s`.
- **After the loop**
  - A configured consume directory takes the fallback, including after `UnsupportedProtocol`.
  - Otherwise `Could not reach Paperless at <url>: ...` (fast fail) or `Upload to Paperless at <url> failed after N attempts: ...` is raised from `last_error`.
- The class and method docstrings now cover:
  - which failures are retried and which fail fast
  - that the fallback still applies after `UnsupportedProtocol`
  - D-10, M-17, and the D-10 amendment's accepted duplicate-document risk
- **Tests**
  - `TestPaperlessUrlValidation`: exact InvalidURL message, the cause, and the token not appearing in the message.
  - `TestUploadFailureTranslation`: a `sleeps` fixture patches `saneless.paperless.time.sleep`. It covers:
    - six transient classes, each with and without a fallback: 3 calls, `[1, 2]` sleeps
    - RemoteProtocolError twice, then success
    - an empty `ReadTimeout` ends with `: ReadTimeout`
    - UnsupportedProtocol with and without a fallback: 1 call, no sleep
    - an empty URL through the real transport
    - a 400 field error, exact text, both with and without a consume dir, and no copy either way
    - 503 ×3, raised and falling back
    - a non-JSON 200
    - TooManyRedirects
  - `TestConsumeDir`: the failed staged write now expects a `PaperlessError` caused by an `OSError`. A new test puts a regular file where the directory should go, so mkdir fails even as root.
  - The old "retries" match is now "attempts".

## Verification

- `uv run pytest tests/test_paperless.py tests/test_outcomes_e2e.py tests/test_pipeline.py -q`: 196 passed in 13 s. The slowest tests are the three older retry tests that really sleep, 3 s each.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1759 passed.
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pyrefly check src tests` all report 0 errors.
- Acceptance greps:
  - None of these match: `_truncated_body`, `_MAX_ERROR_BODY_CHARS`, `except (httpx.ConnectError, httpx.TimeoutException)`, `retries"`.
  - Each of these matches once: `def _render_error_body(response: httpx.Response) -> str:`, `except httpx.InvalidURL as exc`, and `except httpx.UnsupportedProtocol` (line 401, above `except httpx.TransportError` at 405).
  - `attempts: ` matches.
  - `Token {token}` appears only in the header construction.
- `-k body` selects well over 7 tests, and all pass.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Existing staged-write test expected a raw OSError**
- **Found during:** Task 2 RED
- **Issue:** `test_a_failed_staged_write_leaves_the_directory_empty` asserted `pytest.raises(OSError)`. That contradicts EXC-01's rule that a consume-directory copy failure is a `PaperlessError`.
- **Fix:** The test now expects the `PaperlessError` message and checks that `__cause__` is an `OSError`. The empty-directory check stays.
- **Commit:** 3c6f419

**2. [Rule 3 - Blocking] PLR0912 on upload_document**
- **Found during:** Task 2 GREEN
- **Issue:** With the new clauses, `upload_document` had 13 branches, over the limit of 12.
- **Fix:** Moved form-field building into `_form_fields`, next to the `_post_document`, `_back_off` and `_fall_back_to_consume_dir` helpers the plan suggested. No rule was suppressed.
- **Commit:** b7875f4

### Notes
- **No UnsupportedProtocol through MockTransport:** with `url=""` and a MockTransport, httpx measured a plain `ValueError` rather than `UnsupportedProtocol`. The UnsupportedProtocol counting tests therefore raise it from the handler, and a separate test builds the client with `url=""` and no mock transport. That surfaces the real `UnsupportedProtocol` without any network access.
- **Consume-dir failure setup:** the consume-dir mkdir failure uses a regular file in place of the directory rather than chmod, so no root skip is needed.
- **Multi-line 5xx message:** httpx's `HTTPStatusError` text for a 5xx is two lines (`Server error '503 …' for url '…'` followed by `For more information check: …`). The exhausted-attempts message carries it through `describe` as the plan specifies. That text comes from httpx, not from the upstream, so T-28-24 is not affected. If a single-line CLI message is wanted there, plan 28-09 or 28-10 could render only the first line.

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | 5a0d899 | f9a8a92 |
| 2 | 3c6f419 | b7875f4 |

The Task 1 RED run failed at collection with an ImportError for `_render_error_body`. The Task 2 RED run had 23 failures, all on the old wording or on the raw httpx/OSError escaping. No refactor commits.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/paperless.py (`_render_error_body`, `except httpx.InvalidURL as exc`, `except httpx.UnsupportedProtocol`)
- FOUND: tests/test_paperless.py (`TestRenderErrorBody`, `TestPaperlessUrlValidation`, `TestUploadFailureTranslation`)
- FOUND commits: 5a0d899, f9a8a92, 3c6f419, b7875f4
