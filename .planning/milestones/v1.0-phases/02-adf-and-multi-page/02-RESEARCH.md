# Phase 2: ADF and Multi-Page - Research

**Researched:** 2026-03-20
**Domain:** SANE ADF scanning, image processing, manual duplex coordination
**Confidence:** HIGH

## Summary

Phase 2 extends the existing flatbed single-page pipeline to handle multi-page ADF scanning in three modes: simplex, hardware duplex, and manual duplex (two-pass with flip). The core technical challenges are: (1) wrapping python-sane's `multi_scan()` / `_SaneIterator` with per-page timeouts and image validation to handle unreliable ADF end-of-feed signals, (2) implementing manual duplex as a two-pass scan with `threading.Event` coordination and page-count validation before interleaving, (3) detecting and discarding empty pages using Pillow's `ImageStat` mean/stddev on grayscale conversion, and (4) generating a base64 JPEG thumbnail after the first page.

The existing codebase already has the right abstractions in place: `ScannerBackend.scan_pages()` returns `Iterator[Image.Image]`, the `ScanWorker` runs in a daemon thread, `JobState` is a `StrEnum`, and `ProfileConfig` uses pydantic. All changes extend existing modules rather than introducing new architectural patterns. No new external dependencies are needed -- Pillow (already installed at 12.1.1) provides `ImageStat` for empty page detection and `Image.thumbnail()` for thumbnails.

**Primary recommendation:** Extend `SaneBackend.scan_pages()` to use `dev.multi_scan()` for ADF sources, add an `AWAITING_FLIP` state to `JobState`, add empty page thresholds to `ProfileConfig`, and insert thumbnail generation + empty page filtering as inline steps in `run_pipeline()`.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- Extend existing `scan_pages()` to yield multiple pages via `multi_scan()` when source is ADF -- keeps `ScannerBackend` interface unchanged
- Wrap ADF iteration with per-page timeout (not per-job) -- cancel if single page exceeds 2-3x expected duration
- Validate each scanned image inline: nonzero dimensions, minimum file size, not pure white/black
- Discard corrupt/zero-size trailing pages with logging (HP scanner N+1 page behavior)
- Use `sane.cancel()` explicitly after iterator completes or on any error
- Empty ADF feeder: catch `sane.error` on first `multi_scan()` iteration, inspect error string for out-of-paper, translate to "No paper detected in feeder" (SCAN-10)
- Hardware duplex: set source to `ADF Duplex` (or device equivalent), pages arrive interleaved from hardware
- Manual duplex: two-pass via `threading.Event`, pass A gets fronts, worker sets `awaiting_flip` and blocks, web layer sets event for pass B
- Pass B pages reversed then interleaved: A1, B1, A2, B2...
- Raw page count validation BEFORE empty page detection -- mismatch fails with clear error (SCAN-07)
- Empty page detection AFTER raw count check on BOTH passes
- Dual-threshold algorithm: mean luminance > T_mean AND stddev < T_std
- Both thresholds configurable per profile in TOML config
- Inline pipeline filter between acquisition and PDF assembly
- Log computed mean/stddev for each page with keep/discard decision
- Convert to grayscale before computing statistics
- Thumbnail: JPEG, long edge <= 300px, base64-encoded on job state, generated after first page before remaining pages scanned
- Served via job status endpoint, no separate image endpoint

### Claude's Discretion
- Default empty page threshold values (T_mean, T_std)
- Per-page timeout duration calculation
- Pillow MAX_IMAGE_PIXELS setting for high-DPI scans
- EXIF validation/stripping before PDF assembly
- Internal module organization for new page processing code
- Thumbnail JPEG quality setting
- How `ScanSettings` is extended (additional fields vs separate config)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SCAN-04 | ADF multi-page scan collecting all pages | `dev.multi_scan()` returns `_SaneIterator`, yields PIL Images; per-page timeout + validation handles unreliable end-of-feed |
| SCAN-05 | ADF duplex via native hardware duplex | Set source to `ADF Duplex` (device-reported name); pages arrive pre-interleaved from hardware |
| SCAN-06 | ADF manual duplex (two-pass with flip) | `threading.Event` for pass coordination; reverse-and-interleave for page ordering |
| SCAN-07 | Raw page count match validation before interleaving | Compare `len(pass_a)` vs `len(pass_b)` before any empty page filtering |
| SCAN-08 | Empty page detection with dual-threshold algorithm | `PIL.ImageStat.Stat` on grayscale: `mean[0]` and `stddev[0]` with configurable thresholds per `ProfileConfig` |
| SCAN-09 | First-page thumbnail (JPEG, <=300px, base64) | `Image.thumbnail((300, 300), Image.LANCZOS)` then `io.BytesIO` + `base64.b64encode`; store on `Job` model |
| SCAN-10 | Empty feeder produces specific error | Catch exception from first `multi_scan()` iteration; match `"Document feeder out of documents"` string |
| SCAN-11 | Single scan job at a time, concurrent requests queued | Already implemented: `ScanWorker` uses `queue.Queue(maxsize=10)` with single consumer thread |
| SCAN-12 | Scanning runs in background worker thread | Already implemented: `ScanWorker._run()` in daemon thread; extend for `AWAITING_FLIP` state |

</phase_requirements>

## Standard Stack

### Core (already installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| python-sane | 2.9.2 | SANE scanner access | Only Python binding for SANE; `multi_scan()` provides ADF iterator |
| Pillow | 12.1.1 | Image processing | `ImageStat.Stat` for empty page detection, `Image.thumbnail()` for thumbnails |
| img2pdf | 0.6.3 | PDF assembly | Already used in Phase 1; lossless image-to-PDF |
| pydantic-settings | 2.13.1+ | Config management | Already used; extend `ProfileConfig` with threshold fields |

### Supporting (stdlib, no install needed)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `threading.Event` | stdlib | Manual duplex pass coordination | Worker blocks on event between pass A and pass B |
| `io.BytesIO` | stdlib | In-memory JPEG buffer for thumbnail | base64 encode without temp file |
| `base64` | stdlib | Encode thumbnail as base64 string | Store on Job model, serve via status endpoint |
| `PIL.ImageStat` | Pillow | Image statistics (mean, stddev) | Empty page detection |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| PIL.ImageStat for blank detection | numpy histogram | numpy is a heavy dependency; ImageStat uses Pillow's built-in histogram, no extra install |
| Image.thumbnail() | Manual resize calculation | thumbnail() handles aspect ratio automatically, uses LANCZOS |
| threading.Event | asyncio.Event | Worker is already threading-based; mixing async adds complexity |

**Installation:** No new packages needed. All dependencies already in `pyproject.toml`.

## Architecture Patterns

### Recommended Changes to Existing Structure
```
src/saneless/
    scanner/
        base.py          # Add fields to ScanSettings (or keep separate)
        sane_backend.py  # Extend scan_pages() for ADF multi_scan()
    pipeline.py          # Add thumbnail gen + empty page filter steps
    worker.py            # Add threading.Event for manual duplex
    job.py               # Add AWAITING_FLIP state, thumbnail field to Job
    config.py            # Add threshold fields to ProfileConfig
    exceptions.py        # Add FeederEmptyError (subclass of ScanError)
    pages.py             # NEW: empty page detection + thumbnail generation
```

### Pattern 1: ADF Multi-Page Scan via _SaneIterator
**What:** Use `dev.multi_scan()` which returns a `_SaneIterator`. The iterator calls `dev.start()` then `dev.snap(no_cancel=True)` per page. StopIteration raised when exception message equals `"Document feeder out of documents"`.
**When to use:** Any scan where source contains "ADF" (simplex or duplex).
**Example:**
```python
# Source: python-sane sane.py _SaneIterator.__next__
def _scan_adf_pages(
    self, dev: SaneDevice, timeout_per_page: float
) -> Iterator[Image.Image]:
    iterator = dev.multi_scan()
    for page_image in iterator:
        # Validate: nonzero dimensions, not corrupt
        if page_image.size[0] == 0 or page_image.size[1] == 0:
            logger.warning("Skipping zero-dimension page")
            continue
        yield page_image
```

### Pattern 2: Manual Duplex with threading.Event
**What:** Two-pass ADF scan with event-based coordination. Worker scans pass A, sets state to `AWAITING_FLIP`, blocks on `threading.Event.wait()`. External caller (web layer) sets the event to resume pass B.
**When to use:** When source is "ADF Manual Duplex" or equivalent.
**Example:**
```python
# Worker side
front_pages = list(self._scan_adf_pages(dev, timeout))
job_store.update_state(job_id, JobState.AWAITING_FLIP)
flip_event.wait()  # Blocks until user clicks Continue

back_pages = list(self._scan_adf_pages(dev, timeout))
if len(front_pages) != len(back_pages):
    raise ScanError(
        f"Page count mismatch: {len(front_pages)} fronts, "
        f"{len(back_pages)} backs"
    )

# Interleave: backs are in reverse order from flipped stack
back_pages.reverse()
interleaved = []
for front, back in zip(front_pages, back_pages):
    interleaved.append(front)
    interleaved.append(back)
```

### Pattern 3: Empty Page Detection as Inline Filter
**What:** Convert each page to grayscale, compute mean luminance and stddev via `ImageStat.Stat`. Discard if mean > T_mean AND stddev < T_std.
**When to use:** After raw page count validation (manual duplex) or after all pages acquired (simplex/hw duplex).
**Example:**
```python
# Source: Pillow 12.1.1 ImageStat documentation
from PIL import ImageStat

def is_empty_page(
    image: Image.Image,
    mean_threshold: float = 250.0,
    stddev_threshold: float = 5.0,
) -> bool:
    gray = image.convert("L")
    stats = ImageStat.Stat(gray)
    mean = stats.mean[0]
    stddev = stats.stddev[0]
    logger.debug("Page stats: mean=%.1f, stddev=%.1f", mean, stddev)
    return mean > mean_threshold and stddev < stddev_threshold
```

### Pattern 4: First-Page Thumbnail Generation
**What:** After first page acquired, generate a JPEG thumbnail with long edge <= 300px, base64-encode, store on Job.
**Example:**
```python
import base64
import io

def generate_thumbnail(
    image: Image.Image, max_edge: int = 300, quality: int = 85
) -> str:
    thumb = image.copy()
    thumb.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")
```

### Anti-Patterns to Avoid
- **Calling `dev.snap()` in a loop instead of `multi_scan()`:** snap() without `no_cancel=True` cancels the scan between pages, breaking ADF multi-page flow.
- **Applying empty page detection before page count validation in manual duplex:** This causes count mismatches when legitimate blank backs are removed before comparison.
- **Using `dev.start()` directly for ADF:** The `_SaneIterator` handles `start()` calls internally; calling it manually causes double-start errors.
- **Relying solely on `StopIteration` for ADF end detection:** Some drivers hang instead of raising; always pair with per-page timeout.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Image statistics | Manual pixel-by-pixel computation | `PIL.ImageStat.Stat` | Optimized C implementation, handles all modes, returns mean/stddev directly |
| Thumbnail with aspect ratio | Manual size calculation + resize | `Image.thumbnail((300, 300))` | Handles aspect ratio preservation, modifies in-place |
| Base64 encoding | Custom encoding | `base64.b64encode()` | Stdlib, correct padding, handles binary data |
| Thread synchronization | Polling loops with sleep | `threading.Event` | Efficient wait/notify, no busy-waiting |
| Page interleaving | Complex index math | `zip(fronts, reversed_backs)` + flatten | Clear, readable, correct |

**Key insight:** All the building blocks exist in Pillow and stdlib. The complexity is in the orchestration (timeout handling, error translation, state management), not the individual operations.

## Common Pitfalls

### Pitfall 1: _SaneIterator Destructor Calls device.cancel()
**What goes wrong:** The `_SaneIterator.__del__` calls `self.device.cancel()`. If you close the device before the iterator is garbage collected, the destructor raises an exception.
**Why it happens:** python-sane issue #50 -- the iterator tries cleanup on an already-closed device.
**How to avoid:** The fix in commit 238d61f suppresses the exception in `__del__`. With python-sane 2.9.2 this should be resolved. However, explicitly delete the iterator reference before closing the device as defense: `del iterator` before `dev.cancel()` / `dev.close()`.
**Warning signs:** Exception traces during garbage collection mentioning `_SaneIterator.__del__`.

### Pitfall 2: ADF End-of-Feed String Matching is Fragile
**What goes wrong:** `_SaneIterator.__next__` checks `str(e) == 'Document feeder out of documents'` as an exact string match. Different SANE backends may return different error strings for the same condition.
**Why it happens:** SANE backends are inconsistent. Some return different English strings, some return localized strings.
**How to avoid:** Wrap the multi_scan iterator with additional end-of-feed detection: (1) catch any `_sane.error` and check for common substrings ("out of documents", "no docs", "jammed"), (2) enforce per-page timeout as a safety net, (3) validate each page image is not corrupt/empty.
**Warning signs:** ADF scans that hang after last page instead of completing.

### Pitfall 3: HP Scanners Feed N+1 Pages
**What goes wrong:** Some HP scanners (confirmed in python-sane issue #73) feed one extra blank/corrupt page after the actual documents.
**Why it happens:** Driver bug -- the scanner signals "ready for another page" before detecting the feeder is empty.
**How to avoid:** Validate every page image: check `image.size != (0, 0)`, check that the image data is not entirely white/black (via the same empty page detection). Discard trailing invalid pages with a log warning.
**Warning signs:** PDFs with one extra blank page at the end.

### Pitfall 4: Manual Duplex Count Mismatch After Empty Page Removal
**What goes wrong:** If empty page detection runs before page count comparison, legitimate blank backs get removed, causing a false count mismatch.
**Why it happens:** Ordering error -- the PRD explicitly requires raw count check BEFORE empty page detection.
**How to avoid:** Strict ordering: (1) scan pass A pages, (2) scan pass B pages, (3) compare raw counts, (4) interleave, (5) THEN apply empty page detection to the interleaved result.
**Warning signs:** Manual duplex always failing with count mismatch when scanning documents with blank backs.

### Pitfall 5: EXIF Orientation Breaks img2pdf
**What goes wrong:** Some scanner drivers embed EXIF data with invalid orientation values (e.g., orientation=0). img2pdf reads EXIF orientation and crashes with a ValueError.
**Why it happens:** Scanner SANE backends generate TIFF/JPEG with incorrect EXIF metadata.
**How to avoid:** Strip EXIF data from all scanned images before passing to img2pdf. Pillow makes this easy: save/reload without EXIF, or use `image.info.pop('exif', None)` before saving.
**Warning signs:** img2pdf raising `ValueError` about EXIF rotation.

### Pitfall 6: Pillow Decompression Bomb at High DPI
**What goes wrong:** 1200 DPI A4 scan = ~139M pixels. Pillow's default `MAX_IMAGE_PIXELS` is 89.5M. `Image.open()` or `ImageStat.Stat()` raises `DecompressionBombError`.
**Why it happens:** Safety limit in Pillow to prevent memory exhaustion from malicious files.
**How to avoid:** Already handled in `sane_backend.py` and `pdf.py`: `PIL.Image.MAX_IMAGE_PIXELS = 200_000_000`. Ensure the same setting is applied in any new module that processes images (e.g., the empty page detection module).
**Warning signs:** `DecompressionBombError` at 600+ DPI scanning.

## Code Examples

### ADF Scan with Timeout and Validation
```python
# Wrapping multi_scan with per-page timeout
import signal
from collections.abc import Iterator
from PIL import Image

def scan_adf_with_timeout(
    dev: SaneDevice,
    timeout_seconds: float,
) -> Iterator[Image.Image]:
    """Yield validated pages from ADF with per-page timeout."""
    iterator = dev.multi_scan()
    page_num = 0
    while True:
        try:
            # Use threading timeout since signal not safe in non-main thread
            page_image = next(iterator)
        except StopIteration:
            break
        except Exception as exc:
            error_str = str(exc).lower()
            if "out of documents" in error_str or "no docs" in error_str:
                break
            raise

        page_num += 1
        # Validate
        if page_image.size[0] == 0 or page_image.size[1] == 0:
            logger.warning("Page %d: zero dimensions, skipping", page_num)
            continue
        yield page_image
```

### Empty Page Filter
```python
# Source: Pillow 12.1.1 ImageStat docs
from PIL import Image, ImageStat

def filter_empty_pages(
    pages: list[Image.Image],
    mean_threshold: float,
    stddev_threshold: float,
) -> list[Image.Image]:
    """Remove empty pages based on dual-threshold algorithm."""
    kept = []
    for i, page in enumerate(pages):
        gray = page.convert("L")
        stats = ImageStat.Stat(gray)
        mean_val = stats.mean[0]
        stddev_val = stats.stddev[0]
        is_empty = mean_val > mean_threshold and stddev_val < stddev_threshold
        logger.info(
            "Page %d: mean=%.1f, stddev=%.1f -> %s",
            i + 1, mean_val, stddev_val,
            "DISCARD" if is_empty else "KEEP",
        )
        if not is_empty:
            kept.append(page)
    return kept
```

### Manual Duplex Interleave
```python
def interleave_duplex(
    fronts: list[Image.Image],
    backs: list[Image.Image],
) -> list[Image.Image]:
    """Interleave front and back pages for manual duplex.

    Backs are reversed because the user flips the stack face-down,
    so the last front's back is scanned first in pass B.
    """
    backs_reversed = list(reversed(backs))
    result = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result
```

### Thumbnail Generation
```python
import base64
import io
from PIL import Image

def make_thumbnail(image: Image.Image, max_edge: int = 300) -> str:
    """Generate base64 JPEG thumbnail with long edge <= max_edge."""
    thumb = image.copy()
    thumb.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")
```

### Empty Feeder Detection
```python
# Source: python-sane sane.py _SaneIterator
def detect_empty_feeder(dev: SaneDevice) -> None:
    """Attempt first page; translate empty-feeder to specific error."""
    iterator = dev.multi_scan()
    try:
        first_page = next(iterator)
    except StopIteration:
        raise ScanError("No paper detected in feeder")
    except Exception as exc:
        error_lower = str(exc).lower()
        if "out of documents" in error_lower or "no docs" in error_lower:
            raise ScanError("No paper detected in feeder") from exc
        raise
    # If we get here, first page succeeded -- yield it back
    return first_page, iterator
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `dev.snap()` in loop for ADF | `dev.multi_scan()` iterator | python-sane 2.x | Handles start/cancel per page correctly |
| Manual pixel counting for blank detection | `PIL.ImageStat.Stat` | Always available in Pillow | Fast C implementation, no numpy needed |
| `Image.resize()` for thumbnails | `Image.thumbnail()` | Pillow 2.x+ | Preserves aspect ratio, simpler API |
| Global empty page threshold | Per-profile thresholds | Design decision | Different scanners/paper produce different backgrounds |

**Deprecated/outdated:**
- python-sane 2.8.x had the `_SaneIterator.__del__` exception bug (fixed in 2.9.x)
- Pillow `Image.ANTIALIAS` is deprecated, use `Image.LANCZOS` instead

## Open Questions

1. **Per-page timeout implementation in non-main thread**
   - What we know: `signal.alarm` only works in the main thread. The worker runs in a daemon thread.
   - What's unclear: Best approach for per-page timeout in a thread.
   - Recommendation: Use `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=N)` to wrap each `next(iterator)` call, or use a watchdog timer thread. The simpler approach is a wrapper that runs `next(iterator)` in a sub-thread with `thread.join(timeout)`.

2. **Default empty page threshold values**
   - What we know: Pure white page = mean 255.0, stddev 0.0. Scanned "white" pages typically have mean 240-252 and stddev 2-8 due to paper texture and scanner noise.
   - Recommendation: `T_mean = 250.0`, `T_std = 5.0` as defaults. These catch truly blank pages while allowing pages with faint text (which would increase stddev significantly).

3. **Thumbnail JPEG quality**
   - What we know: Thumbnail is small (<=300px long edge), base64-encoded on job record.
   - Recommendation: Quality 85 produces ~2-3KB base64 for a 300px thumbnail. Good balance of quality and size.

4. **EXIF stripping approach**
   - What we know: Some SANE backends add invalid EXIF. img2pdf chokes on orientation=0.
   - Recommendation: After each page acquisition, clear EXIF: `image.info.pop("exif", None)`. This is safe for scanned images where orientation is always upright.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2+ |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest tests/ -v` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SCAN-04 | ADF multi-page yields all pages from feeder | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendADFScan -x` | Needs new tests |
| SCAN-05 | Hardware duplex sets ADF Duplex source, pages pre-interleaved | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendDuplex -x` | Needs new tests |
| SCAN-06 | Manual duplex two-pass with interleave | unit | `uv run pytest tests/test_pipeline.py::TestManualDuplex -x` | Needs new tests |
| SCAN-07 | Page count mismatch raises ScanError | unit | `uv run pytest tests/test_pipeline.py::TestManualDuplexCountMismatch -x` | Needs new tests |
| SCAN-08 | Empty page detection with dual thresholds | unit | `uv run pytest tests/test_pages.py::TestEmptyPageDetection -x` | Needs new file |
| SCAN-09 | Thumbnail generation (JPEG, <=300px, base64) | unit | `uv run pytest tests/test_pages.py::TestThumbnailGeneration -x` | Needs new file |
| SCAN-10 | Empty feeder raises specific error | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendEmptyFeeder -x` | Needs new tests |
| SCAN-11 | Single job at a time, queued | unit | `uv run pytest tests/test_worker.py::TestScanWorkerQueuing -x` | Partially exists |
| SCAN-12 | Background worker thread | unit | `uv run pytest tests/test_worker.py -x` | Exists, needs extension |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest tests/ -v && uv run prek run`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_pages.py` -- covers SCAN-08 (empty page detection) and SCAN-09 (thumbnail generation)
- [ ] New test classes in `tests/test_scanner.py` -- covers SCAN-04, SCAN-05, SCAN-10 (ADF, duplex, empty feeder)
- [ ] New test classes in `tests/test_pipeline.py` -- covers SCAN-06, SCAN-07 (manual duplex coordination)
- [ ] Extended mock infrastructure: `MockSaneDev` needs `multi_scan()` returning a mock `_SaneIterator`
- [ ] `conftest.py` needs multi-page image fixtures and empty page fixtures

## Sources

### Primary (HIGH confidence)
- [python-sane sane.py source](https://github.com/python-pillow/Sane/blob/main/sane.py) - `_SaneIterator` class, `multi_scan()`, `snap(no_cancel=True)` API
- [python-sane docs](https://python-sane.readthedocs.io/en/latest/) - API reference for SaneDev
- [Pillow ImageStat docs](https://pillow.readthedocs.io/en/stable/reference/ImageStat.html) - Stat class with mean, stddev properties
- Verified Pillow 12.1.1 installed, python-sane 2.9.2 on PyPI
- Existing codebase: `base.py`, `sane_backend.py`, `pipeline.py`, `worker.py`, `job.py`, `config.py` -- read in full

### Secondary (MEDIUM confidence)
- [python-sane issue #70](https://github.com/python-pillow/Sane/issues/70) - Duplex scanning discussion, `ADF Duplex` source usage
- [python-sane issue #23](https://github.com/python-pillow/Sane/issues/23) - HP ADF multi_scan hang, "Document feeder out of documents" string
- [python-sane issue #50](https://github.com/python-pillow/Sane/issues/50) - _SaneIterator destructor cleanup (fixed in later versions)
- `.planning/research/PITFALLS.md` - Pitfalls #3, #5, #7, #9 directly relevant

### Tertiary (LOW confidence)
- Per-page timeout approach in non-main thread -- multiple approaches possible, needs implementation testing

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all libraries already installed and verified, APIs confirmed via source code and docs
- Architecture: HIGH - extending existing patterns (iterator, state machine, pydantic config), no new architectural concepts
- Pitfalls: HIGH - confirmed via python-sane issue tracker and existing PITFALLS.md research
- Empty page thresholds: MEDIUM - default values (250.0 mean, 5.0 stddev) based on analysis but will vary by scanner hardware

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable domain, python-sane and Pillow are mature)
