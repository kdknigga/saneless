# Phase 2 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md)
**Screenshots:** Not captured — no dev server detected (code-only audit)

---

## Important Scope Note

Phase 2 (adf-and-multi-page) is a **pure backend implementation phase**. It delivers scanner
pipeline code, domain types, and threading infrastructure. There is no new UI rendered by this
phase. The audit therefore applies the 6-pillar framework to the developer-facing surfaces that
do exist: user-visible strings (error messages, status callbacks, CLI output), internal API
contracts exposed to a future web layer, and the developer experience of the codebase itself.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 4/4 | All user-facing strings are clear, specific, and actionable |
| 2. Visuals | 3/4 | No new visual surfaces; CLI table layout is functional but column widths are fixed-width strings not adaptive |
| 3. Color | 4/4 | No color system introduced; no hardcoded colors or regressions |
| 4. Typography | 4/4 | No typography choices introduced; text output uses consistent casing conventions |
| 5. Spacing | 4/4 | No spacing system introduced; no arbitrary padding or visual regressions |
| 6. Experience Design | 4/4 | Comprehensive state machine, error categorization, loading/error/empty states all covered |

**Overall: 23/24**

---

## Top 3 Priority Fixes

1. **CLI `devices` table uses fixed-width column padding** — If a device name exceeds 30 characters or a model name exceeds 20 characters, the table will misalign. Impact: users with verbose scanner names see garbled tabular output. Fix: compute column widths dynamically from actual data before rendering (`max(len(d.name) for d in device_list)` etc.), or use `click`'s `echo` with a formatting library.

2. **Status message `"Awaiting flip..."` is hardcoded as a string sentinel in both pipeline.py and worker.py** — The worker detects the flip state by matching the literal string `msg == "Awaiting flip..."`. This creates a fragile coupling: a typo in either location silently breaks AWAITING_FLIP state transitions with no test failure. Impact: manual duplex jobs would never enter AWAITING_FLIP state if the string drifts. Fix: extract a module-level constant `_MSG_AWAITING_FLIP = "Awaiting flip..."` shared between pipeline.py and worker.py, or use a structured callback (e.g., an enum event type) instead of raw strings.

3. **`_scan_adf_pages` post-loop `FeederEmptyError` is unreachable in the normal iteration path** — Lines 348-349 in sane_backend.py raise `FeederEmptyError` if `page_num == 0` after the loop, but the loop's `except Exception` block on the first iteration (line 326) already raises `FeederEmptyError` unconditionally on any error. The `StopIteration` branch breaks the loop immediately, and `page_num` is incremented after a successful page, so `page_num == 0` after the loop is only reachable if the iterator yields zero items and raises `StopIteration` on the first call — yet that path would increment `page_num` to 0 then break, not hit 0 after the loop. Impact: dead code that creates false confidence about empty-feeder detection coverage; may mask a logic gap if a future refactor removes the inner exception handler. Fix: add a test that exercises the `StopIteration`-on-first-call path and verify which branch actually fires, then remove or document the post-loop guard.

---

## Detailed Findings

### Pillar 1: Copywriting (4/4)

All user-visible strings are specific and informative:

- `pipeline.py:82` — `"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"` — includes exact counts, actionable
- `pipeline.py:127` — `"Awaiting flip..."` — clear verb, appropriate ellipsis for in-progress state
- `pipeline.py:132` — `"Manual duplex scan cancelled by user"` — distinguishes user-initiated cancel from error
- `pipeline.py:265` — `"All pages were detected as empty"` — states the cause, not just the symptom
- `sane_backend.py:292` — `"No paper detected in feeder"` — plain language, no jargon
- `sane_backend.py:313-316` — `"Page {N} timed out after {N}s"` — includes page number and duration
- `pipeline.py:217` — `"No scanner found: settings.scanner.device is empty and auto-detection found no devices"` — gives both conditions, explains what to check
- `sane_backend.py:399-401` — `"Device does not support source '{s}'. Available: {list}"` — lists valid options

CLI output (cli.py) also scores well:
- `"Configuration error: {exc}"` (line 59) — prefixed with context
- `"Scan error: {exc}"` (line 119) — exit code 1 matches severity
- `"Paperless error: {exc}"` (line 122) — exit code 3 distinguishes from scan errors
- `"No scanners found."` (line 276) — concise terminal message

No generic `"Something went wrong"`, `"Error occurred"`, or `"Try again"` patterns found.

### Pillar 2: Visuals (3/4)

Phase 2 introduces no new web UI surfaces. The only visual surfaces are CLI table outputs.

**Minor issue — fixed-width column truncation in `cli.py`:**

```python
# cli.py:167-171
header = f"{'Name':<30} {'Vendor':<15} {'Model':<20} {'Type'}"
...
click.echo(f"{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}")
```

The `30/15/20` character caps are hardcoded. A device with name `"brother5:bus2;dev3"` (18 chars) fits fine, but enterprise scanners with longer SANE names (e.g. `"net:192.168.1.100:hp_officejet_8600_plus"`) would overflow and misalign the table.

**Jobs table** (`cli.py:216`) has the same pattern: `{'Title':<30}` truncates long document titles silently.

No regressions to any web UI visual layer — routes.py and app.py are unchanged by Phase 2.

### Pillar 3: Color (4/4)

Phase 2 introduces no color tokens, CSS, Tailwind classes, or design system changes. No hardcoded hex colors or `rgb()` calls found in any Phase 2 files. No regressions to existing color usage in the web layer.

### Pillar 4: Typography (4/4)

Phase 2 introduces no font size, font weight, or typographic changes. Log message conventions are consistent: all use `%s`-style lazy interpolation, consistent use of lowercase for log content, sentence-case for exception messages. No regressions.

### Pillar 5: Spacing (4/4)

Phase 2 introduces no spacing tokens or layout changes. No arbitrary pixel/rem values found. No regressions to any visual layout.

### Pillar 6: Experience Design (4/4)

This is the core strength of Phase 2. The state machine and error coverage are thorough:

**Loading states:**
- `JobState.SCANNING` set in `worker.py:203` before pipeline starts
- `JobState.ASSEMBLING` set in `worker.py:227` during PDF assembly
- `JobState.UPLOADING` set in `worker.py:230` during upload
- Status callbacks (`"Scanning..."`, `"Assembling PDF..."`, `"Uploading to paperless-ngx..."`) passed to web layer

**Awaiting-input states:**
- `JobState.AWAITING_FLIP` introduced in `job.py:29`, set in `worker.py:225`
- `continue_flip()` and `abort_flip()` provide clean external control surface
- `wait_transition()` helper (`worker.py:105`) allows test and web layer to synchronize on state change

**Error states:**
- `ErrorCategory` enum (`job.py:36-43`) with FEEDER / CONFIG / SCANNER / UPLOAD / UNKNOWN
- `_categorize_error()` in `worker.py:130-149` maps exception types to categories
- `FeederEmptyError` as a subclass of `ScanError` allows callers to catch both narrow and broad
- All error paths set both `error` string and `error_category` on job record

**Empty states:**
- `ScanError("All pages were detected as empty")` prevents zero-page PDF assembly
- `FeederEmptyError("No paper detected in feeder")` for pre-scan feeder checks
- Scanner-level validation (`_validate_page_image`) rejects zero-dimension, undersized, pure white/black pages before they reach the pipeline

**Destructive action protection:**
- Manual duplex abort (`abort_flip()`) sets `abort_event` before unblocking `flip_event`, ensuring the pipeline checks abort status before starting pass B — clean cancellation path

**One logic concern (noted in Top 3):** The post-loop `FeederEmptyError` guard in `_scan_adf_pages` (sane_backend.py:348-349) appears to be dead code under the current exception structure.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/pages.py`
- `/home/kris/git/saneless/src/saneless/pipeline.py`
- `/home/kris/git/saneless/src/saneless/worker.py`
- `/home/kris/git/saneless/src/saneless/scanner/sane_backend.py`
- `/home/kris/git/saneless/src/saneless/exceptions.py`
- `/home/kris/git/saneless/src/saneless/job.py`
- `/home/kris/git/saneless/src/saneless/config.py`
- `/home/kris/git/saneless/src/saneless/cli.py`
- `/home/kris/git/saneless/src/saneless/web/routes.py` (unchanged, verified no regressions)
- `/home/kris/git/saneless/tests/test_pages.py`
- `/home/kris/git/saneless/.planning/phases/02-adf-and-multi-page/02-01-SUMMARY.md`
- `/home/kris/git/saneless/.planning/phases/02-adf-and-multi-page/02-02-SUMMARY.md`
- `/home/kris/git/saneless/.planning/phases/02-adf-and-multi-page/02-03-SUMMARY.md`
- `/home/kris/git/saneless/.planning/phases/02-adf-and-multi-page/02-CONTEXT.md`
