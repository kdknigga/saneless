---
status: awaiting_human_verify
trigger: "Scanner shows 'Memory is low' error when scanning via saneless, causing 'Error during device I/O' failure"
created: 2026-03-22T00:00:00Z
updated: 2026-03-22T03:00:00Z
---

## Current Focus

hypothesis: python-sane's snap() cannot drain sane_read() fast enough for hpaio-over-net backends. Replacing the flatbed scan path with scanimage subprocess (proven C tool, same data flow as simple-scan) should solve the buffer overflow.
test: User tests the scanimage-based flatbed scan with same scanner and settings.
expecting: Scanner completes scan without "Memory is low" error; image data returned successfully.
next_action: User verification of the fix.

## Symptoms

expected: Scanner completes scan and returns image data to saneless
actual: Scanner displays "Memory is low" on its panel, then saneless gets "Error during device I/O" after ~37 seconds
errors: "Error during device I/O" in saneless logs, "Memory is low" on physical scanner display
reproduction: POST /api/scan with HP LaserJet 3030 via network print server (net:printserver:hpaio:/usb/hp_LaserJet_3030?serial=00MXBM121742)
started: Specific to saneless; other SANE-based software works fine

## Eliminated

- hypothesis: Missing dev.start() before dev.snap() causes buffer overflow
  evidence: User tested the fix (start() added before snap()). Scanner still completes full mechanical scan, then shows "Memory is low" error. The fix was correct per the API but doesn't address the real issue.
  timestamp: 2026-03-22T02:00:00Z

## Evidence

- timestamp: 2026-03-22T00:01:00Z
  checked: sane_backend.py flatbed scan path (line 434-435)
  found: Flatbed path calls `dev.snap()` WITHOUT calling `dev.start()` first
  implication: Missing sane_start() before sane_read() loop -- critical SANE API sequencing error

- timestamp: 2026-03-22T00:02:00Z
  checked: python-sane sane.py (SaneDev.scan method and _SaneIterator.__next__)
  found: Both python-sane's own scan() method and _SaneIterator.__next__() call start() before snap(). saneless's flatbed path is the only call to snap() that skips start().
  implication: Confirms this is a deviation from the expected SANE API call sequence

- timestamp: 2026-03-22T00:03:00Z
  checked: _sane.c SaneDev_snap C source code (python-pillow/Sane GitHub)
  found: snap() calls sane_get_parameters() then sane_read() loop. It does NOT call sane_start() internally. sane_start() must be called by the caller before snap().
  implication: Without sane_start(), the snap() read loop either fails immediately or triggers undefined backend behavior

- timestamp: 2026-03-22T00:04:00Z
  checked: ADF code path (_scan_adf_pages)
  found: ADF path uses multi_scan() which creates _SaneIterator. Iterator's __next__() calls device.start() then device.snap(True). This correctly sequences the SANE calls.
  implication: ADF path is correct; only flatbed path has the bug

- timestamp: 2026-03-22T00:05:00Z
  checked: Source routing logic in scan_pages() lines 423-438
  found: "Auto" source goes to flatbed path when "Flatbed" IS in available_sources (since _is_adf_source("Auto") returns False). Goes to ADF path only when "Flatbed" is NOT available.
  implication: If HP 3030 reports Flatbed in its sources, the buggy flatbed path (missing start()) is used.

- timestamp: 2026-03-22T02:00:00Z
  checked: User testing of start()+snap() fix
  found: Fix did NOT resolve the issue. Scanner still completes full mechanical scan, THEN "Memory is low" error. GNOME Document Scanner (simple-scan) works perfectly at same settings (300 DPI, color, full page), painting preview line-by-line during scan.
  implication: The issue is not about missing start(). The scanner CAN do incremental transfer (proven by simple-scan). python-sane's data flow is fundamentally different from simple-scan's.

- timestamp: 2026-03-22T02:01:00Z
  checked: python-sane _sane.c SaneDev_snap and SaneDev_start C source code (full review)
  found: start() calls sane_start() with GIL released. snap() calls sane_get_parameters() then enters sane_read() loop, reading one scan line at a time with GIL released for each line. The inner loop is tight C code. sane_get_parameters() is called BEFORE the read loop starts -- this goes over the network for net backend.
  implication: The sane_get_parameters() RPC call after sane_start() may introduce a delay before first sane_read(). More importantly, snap() accumulates entire image in C malloc'd buffers before returning anything to Python.

- timestamp: 2026-03-22T02:02:00Z
  checked: SANE net backend (net.c) sane_start() implementation
  found: net backend sane_start() sends SANE_NET_START RPC, receives a data port number, connects to that port via TCP, returns. This should be fast and NOT block for scan duration. sane_read() then reads from this data port.
  implication: sane_start() itself is not the blocker. The issue may be in the hpaio backend's behavior when proxied through sane-net, or in python-sane's overhead between start() and first read().

- timestamp: 2026-03-22T02:03:00Z
  checked: scanimage availability and version
  found: scanimage (sane-backends) 1.0.32 available at /usr/bin/scanimage
  implication: Can use scanimage subprocess as alternative scanning approach, proven to work with same SANE infrastructure that simple-scan uses.

- timestamp: 2026-03-22T03:00:00Z
  checked: Implemented scanimage subprocess flatbed scan, full test suite
  found: All 43 scanner tests pass (including 6 new scanimage-specific tests), full suite 77 pass, all quality checks clean. Flatbed path now uses `scanimage -d <device> --format=png --mode=... --resolution=... --source=...` subprocess instead of python-sane's snap().
  implication: Fix is code-complete. Needs user hardware verification to confirm scanner memory overflow is resolved.

## Resolution

root_cause: python-sane's snap() (C extension) does not drain sane_read() data fast enough during the scan for the hpaio-over-net backend combination. The scanner's internal buffer fills before snap() finishes reading. simple-scan and scanimage work because they read data in a tight C loop without the Python overhead between sane_start() and the sane_read() loop.
fix: Replaced the flatbed scan path with a `scanimage` subprocess approach. Instead of using python-sane's `dev.start()` + `dev.snap()`, the flatbed path now shells out to `scanimage --format=png` which reads scanner data in a tight C loop identical to simple-scan (proven working). The device handle is released before calling scanimage so there is no SANE handle conflict. ADF scans still use python-sane's multi_scan() (unaffected). Added `_scan_flatbed_scanimage()` function and `_resolve_source()` method to separate source validation from scanning. Source validation still uses python-sane to read device options, then closes the handle before scanimage opens it.
verification: 43 scanner tests pass (6 new scanimage-specific tests). Full suite 77 tests pass. ty, pyrefly, ruff, prek all clean. Awaiting user hardware verification.
files_changed: [src/saneless/scanner/sane_backend.py, tests/test_scanner.py]
