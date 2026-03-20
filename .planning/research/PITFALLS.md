# Domain Pitfalls

**Domain:** SANE scanner-to-paperless-ngx bridge
**Researched:** 2026-03-20

## Critical Pitfalls

Mistakes that cause rewrites, data loss, or major stability issues.

### Pitfall 1: python-sane File Descriptor Leak on Repeated init/exit Cycles

**What goes wrong:** The python-sane C extension leaks file descriptors when `sane.init()` and `sane.exit()` are called repeatedly. Each cycle leaks approximately one fd. A long-running service that re-initializes SANE on each scan request will exhaust fds and crash.
**Why it happens:** Open bug in python-sane (#93). The C-level cleanup in `sane.exit()` does not release all resources acquired by `sane.init()`.
**Consequences:** Process hits ulimit, all file operations fail, service goes down hard with no obvious error message. May take days to manifest in production.
**Prevention:** Call `sane.init()` exactly once at process startup. Never call `sane.exit()` except at shutdown. If the SANE connection becomes stale or a device disappears, handle recovery by reopening the device -- not by re-initializing the entire library. Wrap in a singleton manager class.
**Detection:** Monitor open file descriptor count (`/proc/self/fd`) in the health endpoint. Alert if fd count grows monotonically across scan jobs.
**Confidence:** HIGH -- confirmed open issue with reproduction case.
**Phase:** Core scanner integration (Phase 1). Must be designed in from the start.

### Pitfall 2: python-sane Segfaults from Progress Callback Misuse

**What goes wrong:** Passing a progress callback with the wrong signature to `SaneDev.snap()` causes a segmentation fault, not a Python exception. Similarly, if a progress callback raises an exception, the C extension dereferences NULL and core dumps.
**Why it happens:** Two open bugs (#81, #103). The C extension does not validate callback arity or handle callback exceptions gracefully. The progress callback MUST accept exactly two arguments (lines_scanned, total_lines).
**Consequences:** Entire process crashes with no Python traceback. In a web service, this kills all active connections and requires external restart.
**Prevention:** Either avoid progress callbacks entirely (simplest), or wrap any callback in a defensive adapter that catches all exceptions and enforces the two-argument signature. Never pass user-provided or dynamically-constructed callables directly.
**Detection:** Process exit code 139 (SIGSEGV). Systemd/container restart count increasing.
**Confidence:** HIGH -- confirmed open issues with known root causes.
**Phase:** Core scanner integration (Phase 1).

### Pitfall 3: ADF multi_scan() End-of-Feed Detection is Unreliable

**What goes wrong:** `multi_scan()` returns a `_SaneIterator` that raises `StopIteration` when the ADF is empty, but some scanner/driver combinations feed an extra page, return a corrupt final image, or hang instead of signaling completion. HP scanners are specifically known to feed N+1 pages when N pages are loaded (#73).
**Why it happens:** SANE backend drivers vary wildly in quality. The ADF "no more documents" signal (`SANE_STATUS_NO_DOCS`) is not implemented consistently. Some drivers return `SANE_STATUS_EOF` or `SANE_STATUS_GOOD` with garbage data, or block indefinitely waiting for paper.
**Consequences:** Scans hang until timeout, produce PDFs with blank/corrupt trailing pages, or the iterator never terminates and the worker thread blocks forever.
**Prevention:**
1. Always wrap ADF iteration with a timeout (per-page, not per-job). If a single page takes longer than 2-3x the expected duration, cancel the scan.
2. Validate each scanned image immediately: check dimensions are nonzero, file size is above a minimum, image is not pure white/black.
3. Implement empty page detection as a filter in the scan pipeline, not just as an optional post-processing step.
4. Use `sane.cancel()` explicitly after the iterator completes or on any error to reset the scanner state.
**Detection:** Scan jobs that take abnormally long. Pages with file size near zero. Worker thread stuck in blocked state.
**Confidence:** HIGH -- confirmed across multiple projects (scanservjs #745, python-sane #73).
**Phase:** ADF scanning (Phase 2). Design the timeout and validation architecture in Phase 1 so it is ready.

### Pitfall 4: Scanner Device Locking and Exclusive Access Races

**What goes wrong:** SANE devices typically allow only one open handle at a time. If the previous scan's device handle is not properly closed (due to exception, timeout, or process crash), subsequent `sane.open()` calls fail with "device busy" and the scanner becomes unusable until saned is restarted or the stale handle times out.
**Why it happens:** Python exception paths skip `dev.close()`. The GC may not call finalizers promptly. Network saned connections have their own timeout separate from the client.
**Consequences:** Scanner stuck in "busy" state. All subsequent scan requests fail. Users see cryptic errors. May require manual saned restart.
**Prevention:**
1. Use context managers (`__enter__`/`__exit__`) for all device access. Never rely on GC to close devices.
2. Implement a device lock at the application level (the Queue-based single-job design already helps).
3. Add explicit `sane.cancel()` before `dev.close()` on every code path, including error paths.
4. Add a "force release" admin action that calls close + cancel and reopens the device.
5. Set `data_connect_timeout` on the saned server to prevent stale connections from lingering.
**Detection:** "Device busy" errors in scan attempts. Health endpoint should probe device availability periodically.
**Confidence:** HIGH -- universal SANE behavior, confirmed in Arch Wiki and saned manpage.
**Phase:** Core scanner integration (Phase 1). The device lifecycle management must be correct from day one.

### Pitfall 5: Scanner Options Are Device-Specific and Capabilities Are Misreported

**What goes wrong:** Code that assumes scanner options like `source`, `resolution`, `mode`, or `duplex` have standard names or values breaks across different backends. A Brother scanner uses `source: "Automatic Document Feeder(left aligned)"` while an Epson uses `source: "ADF"`. Some scanners report supporting duplex but do not actually produce two-sided output.
**Why it happens:** SANE standardizes the protocol but not option semantics. Each backend (epson2, hpaio, brother, etc.) implements its own option names, value constraints, and behaviors.
**Consequences:** Scan profiles that work on one scanner fail silently or crash on another.
**Prevention:**
1. At device open time, enumerate all available options and cache them. Expose this to users via the API/UI.
2. Never hardcode option names or values. Use the scanner abstraction layer to map user-facing names to device-specific values discovered at runtime.
3. Validate every option value against the device's reported constraints before setting it.
4. Provide a "device capabilities" endpoint/CLI command that dumps raw SANE options for debugging.
**Detection:** Errors when setting scan options. Scans returning unexpected format/resolution.
**Confidence:** HIGH -- confirmed across scanservjs, python-sane issues, and SANE documentation.
**Phase:** Scanner abstraction layer (Phase 1). This is the primary reason the abstraction layer exists.

## Moderate Pitfalls

### Pitfall 6: saned Network Timeout and Firewall Data Port Issues

**What goes wrong:** saned uses a control connection (port 6566) plus separate data connections on random high ports. Firewalls that only open port 6566 cause scans to hang at the moment pixel data transfer begins.
**Prevention:**
1. Document `data_portrange` requirement clearly for users. Require a known range (e.g., 10000-10100) in setup docs.
2. Implement client-side timeouts on every SANE operation (not just overall job timeout).
3. Health endpoint should test an actual scan parameter read (not just TCP connect) to verify the data path works.
**Detection:** Scans that start (job accepted) but never produce data. Connection timeouts only on the data transfer phase.
**Confidence:** HIGH -- documented in saned(8) manpage.
**Phase:** Network scanner setup/documentation (Phase 1), timeout architecture (Phase 1).

### Pitfall 7: img2pdf Requires Seekable File Input and Has EXIF Pitfalls

**What goes wrong:** img2pdf requires seekable file descriptors. Scanned images from some devices contain invalid EXIF orientation data (value 0), causing img2pdf to error out. Pillow's decompression bomb protection rejects large scanned images (600+ DPI full-page scans exceed the default pixel limit).
**Prevention:**
1. Always write scanned images to temporary files on disk before passing to img2pdf. Never pipe directly.
2. Strip or validate EXIF data before PDF assembly.
3. Set `PIL.Image.MAX_IMAGE_PIXELS` appropriately for scanner output (600 DPI A4 = ~34.8M pixels; 1200 DPI = ~139M pixels; Pillow default limit is 89.5M pixels).
**Detection:** img2pdf raising `ValueError` about EXIF or `PIL.Image.DecompressionBombError`.
**Confidence:** HIGH -- documented on img2pdf PyPI page.
**Phase:** PDF assembly (Phase 2).

### Pitfall 8: paperless-ngx Task Polling Race Conditions and Failure Modes

**What goes wrong:** The upload endpoint returns HTTP 200 with a task UUID immediately, before any processing. The task may not appear in the tasks endpoint immediately (race condition). The task may be stuck in "PENDING" forever if workers are overloaded.
**Prevention:**
1. Implement polling with exponential backoff and a maximum wait time. Start at 1s, cap at 30s.
2. Handle the case where the task UUID is not found on first poll (retry after short delay).
3. Distinguish three terminal states: SUCCESS (with document ID), FAILURE (with error message), and TIMEOUT (polling limit exceeded).
4. Log the full task response body on failure.
**Detection:** Jobs stuck in "ingesting" state forever. Task UUID returning 404 on first poll.
**Confidence:** MEDIUM -- based on API documentation and community discussions.
**Phase:** Paperless-ngx integration (Phase 1).

### Pitfall 9: Manual Duplex Page Interleaving Logic is Error-Prone

**What goes wrong:** Manual duplex requires two ADF passes. The interleaving logic breaks if: front and back page counts don't match, the user didn't flip correctly, or a page was detected as blank and removed before interleaving.
**Prevention:**
1. Validate that front count equals back count before interleaving. If mismatched, fail with clear error.
2. Apply blank page detection AFTER interleaving, not before. Otherwise page counts diverge.
3. Show the first-page thumbnail of the second pass so user can verify correct flip.
4. Provide a "cancel second pass" option that still saves the fronts-only scan.
**Detection:** PDFs with pages in wrong order. Front/back count mismatch errors.
**Confidence:** MEDIUM -- logic pitfall based on domain analysis.
**Phase:** ADF duplex (Phase 2). Must be carefully designed and heavily tested.

### Pitfall 10: Temporary File Cleanup Failures on Errors

**What goes wrong:** Scanned images written to temp files are not cleaned up when exceptions occur mid-pipeline. A 600 DPI color A4 page is ~100MB as uncompressed TIFF or ~30MB as PNG.
**Prevention:**
1. Use `tempfile.TemporaryDirectory` as a context manager for each scan job.
2. Never use individual `tempfile.NamedTemporaryFile` with `delete=False`.
3. Add a periodic cleanup sweep as a safety net (delete temp dirs older than 1 hour).
4. Size the container's tmpfs appropriately -- at minimum 500MB for a 10-page 600 DPI color scan.
**Detection:** Disk usage alerts. `df -h /tmp` in health endpoint.
**Confidence:** HIGH -- universal pattern in scan processing pipelines.
**Phase:** Core scan pipeline (Phase 1). The temp file strategy must be established early.

## Minor Pitfalls

### Pitfall 11: SANE Device Discovery is Slow Over Network

**What goes wrong:** `sane.get_devices()` with network backends can take 10-30 seconds because it probes all configured net backends and waits for timeouts on unreachable hosts.
**Prevention:** Cache device list with a configurable TTL (default 5 minutes). Provide an explicit "refresh devices" action. Never block the scan request path on device discovery.
**Detection:** Slow UI load times. Web UI timeout on initial page load.
**Confidence:** HIGH -- documented behavior, confirmed in Arch Wiki troubleshooting.
**Phase:** Scanner discovery (Phase 1).

### Pitfall 12: python-sane Option Setting Uses Magic __setattr__

**What goes wrong:** python-sane devices use `__setattr__` to set scanner options. Typos in option names silently create Python attributes instead of raising errors. `dev.resoltuion = 300` succeeds but does nothing to the scanner.
**Prevention:** Always set options through the abstraction layer, which validates option names against the device's reported option list. Never set options directly on the `SaneDev` object from user-facing code.
**Detection:** Scans producing unexpected output despite correct-looking configuration.
**Confidence:** HIGH -- inherent python-sane API design.
**Phase:** Scanner abstraction layer (Phase 1).

### Pitfall 13: paperless-ngx Permission/Owner Scoping on API Upload

**What goes wrong:** Documents uploaded via the API are owned by the token's user. Other paperless-ngx users may not see uploaded documents.
**Prevention:** Document that the API token user becomes the document owner. Recommend using a shared/admin account token.
**Detection:** Users reporting that scanned documents don't appear in their paperless-ngx view.
**Confidence:** MEDIUM -- reported in paperless-ngx community discussions.
**Phase:** Paperless-ngx integration (Phase 1).

### Pitfall 14: Container Image Missing libsane or Backend Drivers

**What goes wrong:** A Docker image that installs `python-sane` but not `libsane1` or specific backend packages will fail at import time or when connecting to saned. The `net` backend must be explicitly enabled.
**Prevention:** Base image should install `libsane1` (runtime) and `sane-utils` (for `net` backend and config files). Verify `net` backend is listed in `/etc/sane.d/dll.conf`. Test the import of `sane` in the container build step.
**Detection:** `ImportError` on `import sane`. `sane.get_devices()` returning empty list despite working saned.
**Confidence:** HIGH -- standard container packaging issue.
**Phase:** Containerization (Phase 4).

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| Scanner integration | fd leak (#1), segfault (#2), device locking (#4), option names (#5) | Init-once pattern, no progress callbacks, context managers, abstraction layer |
| ADF scanning | End-of-feed (#3), manual duplex interleave (#9) | Per-page timeouts, image validation, count matching |
| PDF assembly | img2pdf EXIF/seekable (#7), temp files (#10) | Write to disk, strip EXIF, TemporaryDirectory context manager |
| Paperless-ngx | Task polling (#8), permissions (#13) | Exponential backoff, document token ownership |
| Network/saned | Firewall data ports (#6), slow discovery (#11) | data_portrange docs, device cache |
| Container | Missing libs (#14) | Build-time import test, minimal backend install |
| Config/options | Magic setattr (#12), misreported capabilities (#5) | Abstraction layer validates all option access |

## Sources

- python-sane GitHub issues: #70 (duplex), #73 (ADF double-feed), #81 (segfault), #93 (fd leak), #103 (callback crash)
- img2pdf PyPI page: https://pypi.org/project/img2pdf/
- saned(8) manpage
- Arch Wiki SANE: https://wiki.archlinux.org/title/SANE
- scanservjs issues: https://github.com/sbs20/scanservjs/issues
- paperless-ngx API docs and community discussions

---
*Pitfalls research for: saneless*
*Researched: 2026-03-20*
