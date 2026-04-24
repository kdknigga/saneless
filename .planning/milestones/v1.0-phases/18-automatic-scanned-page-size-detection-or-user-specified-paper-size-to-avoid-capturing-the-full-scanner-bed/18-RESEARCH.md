# Phase 18: Scanned Page Size / Scan Area Control - Research

**Researched:** 2026-03-23
**Domain:** SANE scanner geometry options, paper size presets, Pillow image cropping
**Confidence:** HIGH

## Summary

This phase adds a `paper_size` field to `ProfileConfig` that constrains the scan area to standard paper dimensions. The primary mechanism is setting SANE geometry options (`br-x`, `br-y`) before scanning, which instructs the scanner hardware to acquire only the specified area. When the scanner does not support geometry options (or they are read-only), a Pillow crop fallback trims the full-bed image to the target dimensions.

The implementation follows the established pattern from Phase 16 (`auto_source_mode`): new field on `ProfileConfig` with a `Literal` type, flows through `ScanSettings` to `scan_pages()`, auto-generated profiles use the default value. python-sane exposes geometry options via underscore-converted attribute names (`dev.br_x`, `dev.br_y`) and provides `is_settable()` to check writability. Paper size presets map directly to mm dimensions, which is the native unit for SANE geometry options.

**Primary recommendation:** Add `paper_size: Literal["full", "a4", "letter", "legal", "a3", "a5"] = "full"` to `ProfileConfig`, set SANE `br_x`/`br_y` in `scan_pages()` when paper_size is not "full", and implement a Pillow crop fallback when geometry options are missing or read-only.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Primary approach is setting SANE geometry options (`br-x`, `br-y`) before scanning to constrain the acquisition area at the hardware level -- faster scan, smaller image, less data
- **D-02:** If the scanner does not expose geometry options (or they are read-only), fall back to scanning the full bed and cropping with Pillow to the target paper dimensions based on DPI
- **D-03:** No auto-detection of paper edges via image analysis -- this is a known-dimensions approach, not a computer vision approach
- **D-04:** New `paper_size` field on `ProfileConfig` with named presets: `"full"`, `"a4"`, `"letter"`, `"legal"` (and potentially other common sizes)
- **D-05:** Default value is `"full"` -- preserves current full-bed behavior. Users opt in to paper size constraint. Zero-change upgrade path
- **D-06:** Preset names map to known mm dimensions internally (e.g., A4 = 210x297mm, Letter = 215.9x279.4mm)
- **D-07:** `paper_size` field flows through `ScanSettings` dataclass to `scan_pages()`, following the established pattern from `auto_source_mode` (Phase 16)
- **D-08:** Auto-generated profiles keep `paper_size = "full"` (the default). No inference from scanner geometry -- avoids wrong guesses
- **D-09:** Users who care about paper size configure it manually in their TOML profile

### Claude's Discretion
- Whether `paper_size` should apply to ADF scans, flatbed only, or both -- decide based on how SANE ADF geometry actually works in practice
- Which additional paper size presets to include beyond A4, Letter, Legal (e.g., A3, A5, Tabloid)
- How to parse SANE geometry option constraints (min/max ranges vs discrete values)
- Whether to set `tl-x`/`tl-y` to 0 explicitly or leave them at default (scanner origin)
- Exact Pillow crop logic for the fallback path (centered crop vs top-left aligned)
- Test structure and mock approach for geometry option handling

### Deferred Ideas (OUT OF SCOPE)
- Custom paper dimensions via `"WxH"` string format (e.g., `paper_size = "210x297"`) -- could be added later if presets prove insufficient
- Auto-detection of paper edges via image analysis -- different feature entirely
- Web UI dropdown for paper size selection per-scan (would be a UI enhancement phase)
</user_constraints>

## Project Constraints (from CLAUDE.md)

- Python 3.14, `uv` for package management
- All code must pass ruff linting, ruff formatting, ty type checking, pyrefly type checking with zero errors
- No `# type: ignore`, `# noqa`, or rule suppression
- Ruff D rules require docstrings on all public modules, classes, and functions
- `prek` for pre-commit hooks (not `pre-commit`)
- Prefer external packages over reimplementing solved problems
- Pillow already a dependency (used for image processing throughout)

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| python-sane | 2.9.2 | SANE geometry option access | Already in project; `dev.br_x`/`dev.br_y` attribute API |
| Pillow | 12.1.1+ | Crop fallback when geometry unavailable | Already in project; `Image.crop()` for box extraction |
| pydantic | 2.x | `ProfileConfig` field validation via `Literal` type | Already in project; established pattern from `auto_source_mode` |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| img2pdf | 0.6.3+ | PDF assembly (unchanged) | Receives cropped images; handles variable page sizes natively |

No new dependencies needed.

## Architecture Patterns

### Recommended Project Structure
```
src/saneless/
  config.py          # Add paper_size Literal field to ProfileConfig
  scanner/
    base.py          # Add paper_size field to ScanSettings dataclass
    sane_backend.py  # Set geometry options in scan_pages(); crop fallback
  pipeline.py        # Pass paper_size when constructing ScanSettings
  auto_profiles.py   # No change (default paper_size="full" is implicit)
  paper_sizes.py     # NEW: Paper size dimension lookup table
```

### Pattern 1: Paper Size Dimension Lookup
**What:** A module-level dictionary mapping preset names to (width_mm, height_mm) tuples.
**When to use:** Every time paper_size is resolved to actual mm dimensions.
**Example:**
```python
# Source: Standard ISO/ANSI paper dimensions
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}
```

### Pattern 2: SANE Geometry Option Setting (try/except)
**What:** Attempt to set geometry options; catch exceptions and fall back to crop.
**When to use:** In `scan_pages()` before `dev.start()` / `dev.multi_scan()`.
**Example:**
```python
# python-sane converts hyphens to underscores for attribute access
# SANE option "br-x" becomes dev.br_x
try:
    dev.tl_x = 0.0
    dev.tl_y = 0.0
    dev.br_x = width_mm
    dev.br_y = height_mm
    geometry_set = True
except (AttributeError, Exception):
    geometry_set = False
```

### Pattern 3: Pillow Crop Fallback
**What:** After scanning full bed, crop image to target dimensions using DPI.
**When to use:** When scanner geometry options are not available or read-only.
**Example:**
```python
# Top-left aligned crop (paper in scanner starts at top-left corner)
crop_width_px = int(width_mm * dpi / 25.4)
crop_height_px = int(height_mm * dpi / 25.4)
# Clamp to actual image dimensions
crop_width_px = min(crop_width_px, image.width)
crop_height_px = min(crop_height_px, image.height)
cropped = image.crop((0, 0, crop_width_px, crop_height_px))
```

### Pattern 4: Config Field Flow (established from Phase 16)
**What:** ProfileConfig -> ScanSettings -> scan_pages(), same pattern as auto_source_mode.
**When to use:** For all new per-profile settings.
**Example:**
```python
# In pipeline.py run_pipeline():
scan_settings = ScanSettings(
    source=profile.source,
    resolution=profile.resolution,
    mode=profile.mode,
    auto_source_mode=profile.auto_source_mode,
    paper_size=profile.paper_size,  # NEW
)
```

### Anti-Patterns to Avoid
- **Checking is_settable() preemptively:** The SANE option metadata parsing is complex and varies by backend. Simpler to try/except on the actual set operation.
- **Using dev['br-x'] bracket syntax:** Less readable than dev.br_x attribute syntax and harder to type-check.
- **Centered crop in fallback:** Paper is placed at the top-left corner of the flatbed glass. A top-left-aligned crop matches physical reality.
- **Setting geometry on ADF when paper_size is "full":** No-op; skip geometry entirely when paper_size is "full" to avoid touching scanner defaults.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Paper dimension lookup | Custom parsing/calculation | Static dictionary constant | ISO/ANSI dimensions are fixed; a dict is the right abstraction |
| mm-to-pixel conversion | Complex unit framework | Simple `mm * dpi / 25.4` formula | One formula, universally correct |
| Image cropping | Manual pixel array manipulation | `Pillow Image.crop()` | Battle-tested, handles all color modes |

## Discretion Recommendations

### Paper size applies to both ADF and flatbed
**Recommendation:** Apply to both. SANE geometry options work on ADF scanners too -- they constrain the scan area per page. An ADF scanning A4 documents on a scanner with A3-capable ADF benefits from `br_x`/`br_y` constraints. The Pillow crop fallback also works regardless of source.

### Additional paper size presets
**Recommendation:** Include A3, A5 beyond the required A4, Letter, Legal. These are the most common international sizes. Tabloid (279.4x431.8mm) is too uncommon for the initial set but easy to add later. The Literal type makes adding presets trivial.

### Setting tl-x/tl-y to 0 explicitly
**Recommendation:** Set `tl_x = 0.0` and `tl_y = 0.0` explicitly. Most scanners default to 0, but being explicit costs nothing and avoids edge cases where a previous scan left a non-zero origin. The try/except block already handles the case where these options don't exist.

### Crop alignment for fallback
**Recommendation:** Top-left aligned crop (origin 0,0). When a user places a sheet of paper on a flatbed, they align it to the top-left corner. Cropping from (0,0) matches this physical convention. Centered crop would be wrong for real usage.

### Geometry option constraint parsing
**Recommendation:** Don't parse constraints for validation. Just set the value and let SANE enforce its own constraints (it will clamp or error). The try/except pattern already handles errors. If the requested paper size exceeds the scanner bed, the scanner will either clamp to its max or raise an error -- both are acceptable outcomes. Log the effective geometry after setting for debugging.

### Test structure
**Recommendation:** Extend `MockSaneDev` with `br_x`, `br_y`, `tl_x`, `tl_y` attributes and add geometry options to `get_options()` return. Add a `_FakeSaneDevice` variant that lacks geometry attributes to test the fallback path. Use separate test classes: `TestPaperSizeGeometry` and `TestPaperSizeCropFallback`.

## Common Pitfalls

### Pitfall 1: SANE option names use hyphens, Python attributes use underscores
**What goes wrong:** Trying `dev.br-x` is a syntax error; `dev.__setattr__('br-x', val)` bypasses python-sane's option machinery.
**Why it happens:** SANE option names contain hyphens (`br-x`, `tl-y`), but Python identifiers cannot.
**How to avoid:** Use `dev.br_x`, `dev.br_y`, `dev.tl_x`, `dev.tl_y`. python-sane's `Option.__init__` does `self.py_name = self.name.replace("-", "_")` and `__setattr__` looks up options by this converted name.
**Warning signs:** `AttributeError` or option not being set at all.

### Pitfall 2: SANE geometry units vary by scanner
**What goes wrong:** Setting `br_x = 210` assuming mm, but scanner reports in pixels.
**Why it happens:** SANE spec says geometry SHOULD be in mm, but some backends use pixels.
**How to avoid:** Most document scanners use mm (the SANE spec strongly recommends it for geometry). For the try/except pattern this is acceptable: if the unit is wrong, the crop will be wrong but harmless (just a bad crop, no data loss). Log the attempt for debugging.
**Warning signs:** Scanned image dimension doesn't match expected paper size.

### Pitfall 3: Paper size exceeding scanner bed dimensions
**What goes wrong:** Setting `br_x = 297` (A3 width) on a scanner with max 215.9mm (Letter-width bed).
**Why it happens:** User selects a paper size larger than the scanner supports.
**How to avoid:** SANE backends typically clamp to their maximum. The try/except handles errors. For the crop fallback, clamp crop dimensions to actual image dimensions (`min(crop_px, image.width)`).
**Warning signs:** Scanner returns an error instead of clamping; image is same size as full bed despite paper_size being set.

### Pitfall 4: DPI mismatch in crop fallback
**What goes wrong:** Crop dimensions calculated using one DPI but scanner used a different effective DPI.
**Why it happens:** Scanner may not support the exact requested DPI and uses the closest available.
**How to avoid:** Use the resolution from `ScanSettings` (which is what the user requested). If the scanner used a different resolution, the crop will be slightly off but still a reasonable approximation. An alternative is to compute from the actual image dimensions if the scanner bed size is known, but that's over-engineering for this phase.
**Warning signs:** Crop edges are slightly off from the paper boundary.

### Pitfall 5: Pillow crop with out-of-bounds box
**What goes wrong:** `Image.crop()` with coordinates exceeding image dimensions returns a padded (black) image.
**Why it happens:** Calculated crop dimensions based on paper size exceed the actual scanned image size.
**How to avoid:** Clamp crop box to `(0, 0, min(crop_w, img.width), min(crop_h, img.height))` before calling crop.
**Warning signs:** Black borders appearing on cropped images.

## Code Examples

### Paper Size Dimensions Module
```python
# Source: ISO 216, ANSI/ASME Y14.1
from typing import Literal

PaperSize = Literal["full", "a3", "a4", "a5", "letter", "legal"]

PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "a3": (297.0, 420.0),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}
```

### Setting Geometry in scan_pages()
```python
# In sane_backend.py scan_pages(), after setting mode/resolution/source:
if settings.paper_size != "full":
    dims = PAPER_SIZES_MM.get(settings.paper_size)
    if dims:
        width_mm, height_mm = dims
        try:
            dev.tl_x = 0.0
            dev.tl_y = 0.0
            dev.br_x = width_mm
            dev.br_y = height_mm
            logger.info(
                "Scan area set to %s: %.1f x %.1f mm",
                settings.paper_size,
                width_mm,
                height_mm,
            )
        except Exception:
            logger.warning(
                "Scanner does not support geometry options, "
                "will crop after scanning"
            )
            # geometry_set flag tracked for post-scan crop decision
```

### Pillow Crop Fallback
```python
# After scanning, if geometry was not set:
def crop_to_paper_size(
    image: Image.Image,
    paper_size: str,
    dpi: int,
) -> Image.Image:
    """Crop image to paper size dimensions based on DPI."""
    if paper_size == "full":
        return image
    dims = PAPER_SIZES_MM.get(paper_size)
    if dims is None:
        return image
    width_mm, height_mm = dims
    crop_w = min(int(width_mm * dpi / 25.4), image.width)
    crop_h = min(int(height_mm * dpi / 25.4), image.height)
    return image.crop((0, 0, crop_w, crop_h))
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Full-bed scan always | Configurable paper_size per profile | This phase | Reduces scan time, file size, and processing for standard paper sizes |

**Relevant:** img2pdf handles variable page sizes natively. Each page in the PDF gets its own dimensions based on the image size. No changes to `assemble_pdf()` are needed -- smaller images produce smaller PDF pages automatically.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2+ |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_scanner.py tests/test_config.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| D-01 | SANE geometry options set when paper_size is not "full" | unit | `uv run pytest tests/test_scanner.py -x -k "paper_size_geometry"` | Wave 0 |
| D-02 | Pillow crop fallback when geometry not available | unit | `uv run pytest tests/test_scanner.py -x -k "paper_size_crop"` | Wave 0 |
| D-04 | ProfileConfig accepts paper_size Literal values | unit | `uv run pytest tests/test_config.py -x -k "paper_size"` | Wave 0 |
| D-05 | Default paper_size is "full" | unit | `uv run pytest tests/test_config.py -x -k "paper_size_default"` | Wave 0 |
| D-07 | paper_size flows from ProfileConfig through ScanSettings to scan_pages | unit | `uv run pytest tests/test_pipeline.py -x -k "paper_size"` | Wave 0 |
| D-08 | Auto-generated profiles use paper_size="full" | unit | `uv run pytest tests/test_auto_profiles.py -x -k "paper_size"` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_scanner.py tests/test_config.py tests/test_pipeline.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- None -- existing test infrastructure covers all needed areas. New test classes/methods extend existing test files.

## Open Questions

1. **Scanner behavior when geometry exceeds bed size**
   - What we know: SANE spec says backends should clamp or error. Most backends clamp.
   - What's unclear: Whether all backends in practice clamp gracefully or raise.
   - Recommendation: Try/except handles both cases. Log a warning if the set value differs from what was requested (can check by reading back).

2. **ADF geometry behavior across scanner models**
   - What we know: SANE geometry options work for ADF on most modern scanners.
   - What's unclear: Some older ADF scanners may ignore geometry for ADF scans.
   - Recommendation: Apply geometry uniformly; fallback crop handles any case where it didn't take effect. No need to special-case ADF vs flatbed.

## Sources

### Primary (HIGH confidence)
- [python-sane sane.py source](https://github.com/python-pillow/Sane/blob/main/sane.py) - Option name conversion (hyphen to underscore), `__setattr__` geometry handling
- [python-sane example.py](https://github.com/python-pillow/Sane/blob/main/example.py) - `dev.br_x = 320.` pattern, try/except for geometry
- [python-sane docs](https://python-sane.readthedocs.io/en/latest/) - `is_settable()` method, option constraint format
- [SANE standard spec](http://www.sane-project.org/html/doc014.html) - Geometry option requirements (mm units, range constraints)
- Existing codebase: `sane_backend.py`, `config.py`, `pipeline.py`, `auto_profiles.py` - Established patterns

### Secondary (MEDIUM confidence)
- [img2pdf documentation](https://manpages.debian.org/testing/img2pdf/img2pdf.1.en.html) - Variable page size support confirmed

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - No new libraries; all existing dependencies
- Architecture: HIGH - Follows established Phase 16 pattern exactly
- Pitfalls: HIGH - Verified via python-sane source code and SANE spec
- Geometry API: HIGH - Confirmed via python-sane source: `Option.py_name = name.replace("-", "_")`

**Research date:** 2026-03-23
**Valid until:** 2026-04-23 (stable domain, SANE spec unchanged)
