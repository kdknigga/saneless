---
phase: 01-core-pipeline
plan: 02
subsystem: scanner, pdf, api
tags: [python-sane, img2pdf, httpx, abc, pillow, paperless-ngx]

requires:
  - phase: 01-core-pipeline/01
    provides: "config.py (ScannerConfig, PaperlessConfig, ProfileConfig, OutputConfig), exceptions.py (ScanError, PaperlessError)"
provides:
  - "ScannerBackend ABC with get_devices, get_capabilities, scan_pages"
  - "SaneBackend wrapping python-sane with init-once and context-managed lifecycle"
  - "assemble_pdf function using img2pdf with TemporaryDirectory cleanup"
  - "PaperlessClient with upload, retry, fallback, task polling, connection test"
affects: [01-core-pipeline/03, pipeline, cli]

tech-stack:
  added: [img2pdf, httpx, pillow]
  patterns: [scanner-abc, context-managed-devices, lazy-import, multipart-upload, exponential-backoff]

key-files:
  created:
    - src/saneless/scanner/__init__.py
    - src/saneless/scanner/base.py
    - src/saneless/scanner/sane_backend.py
    - src/saneless/pdf.py
    - src/saneless/paperless.py
    - tests/test_scanner.py
    - tests/test_pdf.py
    - tests/test_paperless.py
  modified:
    - tests/conftest.py

key-decisions:
  - "Lazy import for python-sane: sane module imported at SaneBackend construction, not at module import time, allowing tests and imports without libsane-dev"
  - "Multipart files list for httpx upload: combined form fields and file into single files parameter to avoid httpx data+files mixing issues"
  - "Poll delay starting at 0.5s for faster test execution while maintaining exponential backoff pattern"

patterns-established:
  - "Scanner ABC pattern: ScannerBackend defines get_devices/get_capabilities/scan_pages interface"
  - "Context-managed device lifecycle: _open_device context manager with cancel-before-close on all paths"
  - "Lazy sane import: module-level sentinel + _ensure_sane() for deferred C extension loading"
  - "MockTransport testing: httpx.MockTransport for HTTP client tests without network"
  - "TemporaryDirectory cleanup: temp images in context manager, auto-cleaned on success and error"

requirements-completed: [SCAN-01, SCAN-02, SCAN-03, ARCH-01, ARCH-03, PDF-01, PDF-02, LOG-04, PLSS-01, PLSS-02, PLSS-03]

duration: 5min
completed: 2026-03-20
---

# Phase 01 Plan 02: Core Domain Modules Summary

**Scanner ABC + SaneBackend with init-once/context-managed lifecycle, img2pdf PDF assembly with temp cleanup, and paperless-ngx client with retry/fallback/polling**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-20T16:54:29Z
- **Completed:** 2026-03-20T17:00:00Z
- **Tasks:** 2
- **Files modified:** 9

## Accomplishments
- Scanner abstraction layer with ABC (get_devices, get_capabilities, scan_pages) and SaneBackend implementation
- PDF assembly via img2pdf with TemporaryDirectory cleanup on both success and error paths
- Paperless-ngx REST client with upload retry (3x exponential backoff), consume-dir fallback, task polling, and 3-mode connection test
- All 55 tests pass (14 scanner + 5 PDF + 16 paperless + 20 from Plan 01)

## Task Commits

Each task was committed atomically:

1. **Task 1: Scanner abstraction layer (RED)** - `51cf801` (test)
2. **Task 1: Scanner abstraction layer (GREEN)** - `f8e2988` (feat)
3. **Task 2: PDF assembly + paperless client (RED)** - `2632872` (test)
4. **Task 2: PDF assembly + paperless client (GREEN)** - `7b6a74c` (feat)

_TDD tasks had separate RED and GREEN commits._

## Files Created/Modified
- `src/saneless/scanner/__init__.py` - Package init with lazy SaneBackend import
- `src/saneless/scanner/base.py` - ScannerBackend ABC, DeviceInfo, DeviceCapabilities, ScanSettings dataclasses
- `src/saneless/scanner/sane_backend.py` - SaneBackend wrapping python-sane with init-once, context-managed devices, source validation
- `src/saneless/pdf.py` - assemble_pdf using img2pdf with TemporaryDirectory cleanup
- `src/saneless/paperless.py` - PaperlessClient with upload, retry, fallback, polling, connection test
- `tests/test_scanner.py` - 14 tests covering ABC contract, SaneBackend with mocked sane module
- `tests/test_pdf.py` - 5 tests covering PDF assembly, temp cleanup, output path
- `tests/test_paperless.py` - 16 tests covering upload, retry, fallback, polling, connection, auth header
- `tests/conftest.py` - Added sample_pil_image and sample_pil_images fixtures

## Decisions Made
- Lazy import for python-sane: sane module imported at SaneBackend construction, not at module import time, allowing tests and imports without libsane-dev installed
- Combined multipart files list for httpx upload: form fields and file in single files parameter avoids httpx data+files mixing issues
- Poll delay starting at 0.5s for faster test execution while maintaining exponential backoff pattern
- _transport parameter on PaperlessClient constructor for clean httpx.MockTransport injection in tests

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Lazy import for python-sane**
- **Found during:** Task 1 (Scanner abstraction layer)
- **Issue:** python-sane not installed (deferred from Plan 01 due to libsane-dev requirement). Top-level `import sane` in sane_backend.py prevented module import and test collection.
- **Fix:** Changed to module-level sentinel with `_ensure_sane()` deferred import function. Tests monkeypatch `sane_backend.sane` directly.
- **Files modified:** src/saneless/scanner/sane_backend.py, src/saneless/scanner/__init__.py (lazy __getattr__)
- **Verification:** All 14 scanner tests pass with mocked sane module
- **Committed in:** f8e2988 (Task 1 GREEN commit)

**2. [Rule 1 - Bug] httpx multipart upload encoding**
- **Found during:** Task 2 (Paperless client)
- **Issue:** Mixing `data=` and `files=` parameters in httpx.Client.post caused TypeError with MockTransport (tuple in bytes sequence)
- **Fix:** Combined form fields and file into single `files=` multipart list
- **Files modified:** src/saneless/paperless.py
- **Verification:** All 16 paperless tests pass
- **Committed in:** 7b6a74c (Task 2 GREEN commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Both fixes necessary for correct operation. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Scanner abstraction, PDF assembly, and paperless client all tested and ready for integration
- Plan 03 can wire these into the pipeline with CLI commands
- python-sane remains deferred (requires libsane-dev system headers) but the abstraction layer isolates this dependency

---
*Phase: 01-core-pipeline*
*Completed: 2026-03-20*
