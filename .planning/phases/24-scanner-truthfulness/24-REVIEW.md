---
phase: 24-scanner-truthfulness
reviewed: 2026-09-13T00:00:00Z
depth: deep
files_reviewed: 26
files_reviewed_list:
  - src/saneless/scanner/base.py
  - src/saneless/scanner/sane_backend.py
  - src/saneless/scanner/__init__.py
  - src/saneless/auto_profiles.py
  - src/saneless/pipeline.py
  - src/saneless/pdf.py
  - src/saneless/cli.py
  - tests/fake_sane.py
  - tests/conftest.py
  - tests/test_scanner.py
  - tests/test_sane_hardware.py
  - tests/test_pipeline.py
  - tests/test_auto_profiles.py
  - tests/test_cli.py
  - tests/test_web.py
  - tests/test_web_state_rendering.py
  - tests/test_outcomes_e2e.py
  - tests/test_browser.py
  - tests/test_worker.py
  - .github/workflows/ci.yml
  - docs/explanation/empty-page-detection.md
  - docs/how-to/configure-scan-profiles.md
  - docs/how-to/set-up-adf-duplex.md
  - docs/reference/cli-commands.md
  - docs/reference/configuration.md
  - src/saneless/config.py
findings:
  critical: 3
  warning: 10
  info: 0
  total: 13
status: issues_found
---

# Phase 24: Code Review Report

**Reviewed:** 2026-09-13
**Depth:** deep
**Files Reviewed:** 26
**Status:** issues_found

## Summary

Phase 24 set out to make the scanner layer report what actually happened. In the areas it
aimed at directly — the `ScanBatch` contract, resolution read-back, source classification,
the geometry unit and clamp checks, and the replacement of three divergent SANE doubles
with `tests/fake_sane.py` — the work holds up under direct attack. I drove the shared fake
against the real `python-sane` 2.9.2 source (`.venv/.../sane.py:107-134`, `188-236`) line
by line; the five behaviours it claims to model are modelled correctly, including the one
that matters most (an unknown option name stored silently rather than raising), and the
`_SaneIterator` call pattern matches. That is a genuine improvement and the
`sane_hardware` integration tests give it a second, independent anchor.

Verified state, so the rest of this report is not arguing with the build: `uv run ruff
check .` clean, `ruff format --check` clean, `ty check` clean, `pyrefly check src tests`
0 errors, `pytest -m "not browser and not sane_hardware"` 905 passed / 35 deselected.
Exactly 7 suppressions exist in `src/` + `tests/` and this phase added none — confirmed
against the file list, so no regression there.

The defects are not in the parts the phase stared at. They are in the seams:

1. **The auto-profile writer can destroy its own config.** D-16's new orphan prune removes
   any auto-generated profile the new run does not name — including `default`, which
   `Settings` requires. I reproduced a complete bricking: after the prune every CLI
   command, `auto-profiles` included, exits 2 with "Configuration error". The tool cannot
   self-heal; recovery needs hand-edited TOML. This is new in this phase (CR-01), and it
   sits on top of a pre-existing hole where a flatbed-less scanner produces an unloadable
   config on the very first run (CR-02) — a hole the suite now *pins in place* with an
   assertion.
2. **D-19's clamp detection is half-implemented.** `_area_matches` receives the device's
   top-left read-back, binds it to `_tl_x`/`_tl_y`, and discards it. A device that clamps
   `tl` reports success and skips the crop fallback. Reproduced: an A4 request became a
   200 x 287 mm scan with no warning and no crop (CR-03).
3. Several smaller silences of exactly the kind the phase was written to remove — a source
   fallback that downgrades a multi-page scan to one page at INFO level, a flatbed path
   with no integrity checks at all, and a scale factor of `0.0` that passes an `is None`
   guard and writes a zero-size scan area.

Two fidelity slips remain in the new fake, and both are the same species D-17 exists to
eliminate. They do not currently hide a defect, but the module's entire value is that it
cannot drift, so they are worth closing while the reasoning is fresh.

## Critical Issues

### CR-01: **BLOCKER** — The orphan prune deletes the required `default` profile and bricks the config

**File:** `src/saneless/auto_profiles.py:422-433` (prune), `:320-329` (conditional default)

**Issue:**
D-16's prune removes every auto-generated profile absent from the freshly generated set:

```python
orphans = [
    name
    for name, table in profiles_section.items()
    if name not in profiles and _is_auto_generated(table)
]
```

`generate_profiles` only emits a `default` profile when the device reports a flatbed source
(`:320-329`). `Settings.validate_default_profile` (`src/saneless/config.py:183-187`) makes
`default` mandatory. So a `default` that a *previous* `auto-profiles` run wrote — carrying
`auto_generated = true`, which is what the writer always stamps (`:445-446`) — becomes an
orphan the moment the user points saneless at a scanner with no flatbed. A dedicated
sheet-fed document scanner is the obvious case, and this is a paperless-ngx bridge, so it
is the expected case rather than an exotic one.

Reproduced end to end. Starting from a config that `auto-profiles` itself wrote for a
flatbed+ADF scanner, then re-running against an `ADF Duplex`-only device:

```
config after second auto-profiles run:

[profiles.adf-duplex]
source = "ADF Duplex"
...
devices              exit=2  Configuration error: 1 validation error for Settings
jobs                 exit=2  Configuration error: 1 validation error for Settings
scan --title x       exit=2  Configuration error: 1 validation error for Settings
auto-profiles        exit=2  Configuration error: 1 validation error for Settings
```

Every command fails, **including `auto-profiles` itself** — `cli()` loads settings before
dispatching to any subcommand (`src/saneless/cli.py:75-80`), so the tool cannot regenerate
its way out. The user must hand-edit TOML to recover. The prune runs regardless of
`--force`, so there is no flag that avoids it.

This is new in Phase 24: the pre-phase `write_profiles_to_config` had no prune at all
(confirmed against `4d6cb8c`).

Note this is also reachable through `worker.py:_maybe_auto_generate` (`:141-172`), which
calls `write_profiles_to_config` unattended on the first queued job.

**Fix:** Make `default` unprunable, and state why — it is not an ordinary profile, it is a
schema requirement:

```python
# ``default`` is required by Settings.validate_default_profile, so pruning it
# leaves a config saneless itself refuses to load -- and the CLI loads settings
# before dispatch, so not even `auto-profiles` could regenerate it.
_UNPRUNABLE = frozenset({"default"})

orphans = [
    name
    for name, table in profiles_section.items()
    if name not in profiles and name not in _UNPRUNABLE and _is_auto_generated(table)
]
```

Fixing CR-02 as well (always emitting a `default`) would make this guard redundant, but
both are cheap and the two failures have different triggers; keep whichever you fix, and
add a regression test that loads the written config back through `load_settings`.

---

### CR-02: **BLOCKER** — `auto-profiles` writes a config it cannot load, on a flatbed-less scanner

**File:** `src/saneless/auto_profiles.py:320-329`; pinned by `tests/test_auto_profiles.py:522-530`

**Issue:**
`generate_profiles` gates the `default` profile on a flatbed source existing:

```python
flatbed_sources = [
    s for s in capabilities.sources if classify_source(s) is SourceKind.FLATBED
]
if flatbed_sources:
    profiles["default"] = ProfileConfig(...)
```

For a sheet-fed scanner this produces no `default`, `write_profiles_to_config` writes that
set to disk, and the resulting file fails `Settings` validation. Reproduced on a clean
config with sources `["Automatic Document Feeder", "ADF Duplex"]`:

```
written: ['automatic-document-feeder', 'adf-duplex']
load_settings RAISED: ValidationError ... A 'default' profile must be defined in config
```

The `auto-profiles` command reports success (`Generated 2 profile(s) in ...`) and exits 0
while leaving the installation unusable.

The behaviour predates this phase, but three things make it in scope here. The phase
rewrote this function; CR-01's prune turns it from a first-run-only problem into one that
destroys working configs; and the phase added
`test_feeder_only_device_has_no_default_profile`, which asserts `"default" not in profiles`
— so the suite now actively pins the defect, and a correct fix will read as a test failure
rather than as a fix.

**Fix:** Always produce a `default`, falling back to the device's first source when there
is no flatbed:

```python
# `default` is required by Settings, so it is emitted unconditionally. A flatbed
# backs it when the device has one; otherwise the device's first reported source
# does, because a config without this key is one saneless refuses to load.
default_source = flatbed_sources[0] if flatbed_sources else None
if default_source is None and capabilities.sources:
    default_source = capabilities.sources[0]
if default_source is not None:
    profiles["default"] = ProfileConfig(
        source=default_source, resolution=resolution, mode=mode, auto_generated=True
    )
```

Then re-point `tests/test_auto_profiles.py:522-530` to assert the new guarantee — that the
written config round-trips through `load_settings` — rather than the current absence.

---

### CR-03: **BLOCKER** — D-19's clamp check ignores the top-left read-back, so a clamped area is missed

**File:** `src/saneless/scanner/sane_backend.py:452-456`, `:511-529`

**Issue:**
`_set_geometry` writes all four corners and then reads the area back to catch a silent
clamp — the whole point of D-19. But `_area_matches` discards half of what it read:

```python
(_tl_x, _tl_y), (actual_x, actual_y) = actual
expected_x, expected_y = expected
within_x = abs(actual_x - expected_x) <= tolerance
within_y = abs(actual_y - expected_y) <= tolerance
```

`expected` is the full paper dimension (`width_mm * scale`), which is only the requested
*area* if `tl` really landed on 0. The code assumes that rather than checking it, although
it has the measured value in hand. A device whose `tl-x`/`tl-y` range does not start at 0
clamps the `dev.tl_x = 0.0` write, `br` still matches, the function returns True, and
`_maybe_crop` is skipped.

Reproduced against a device whose geometry range is `(10.0, 300.0, 1.0)`:

```
_set_geometry returned: True (True => no crop fallback)
device area: ((10.0, 10.0), (210.0, 297.0))
actual scanned box: 200.0 x 287.0 mm  (requested 210.0 x 297.0)
```

An A4 request silently yields a 200 x 287 mm page — 10 mm lost on each axis, no warning,
no crop, and a PDF MediaBox that disagrees with its own content. That is precisely the
silent-substitution failure `_area_matches`' own docstring says it exists to catch
("Accepting an assignment is not the same as honouring it"), reappearing through the
corner the check declines to look at.

This is not D-19 being wrong — the tolerance-not-equality decision is correct and should
stay. It is the check being incompletely implemented.

**Fix:** Compare the box the device reports, not just its far corner:

```python
(tl_x, tl_y), (br_x, br_y) = actual
expected_x, expected_y = expected
# The area is br - tl. Comparing br alone assumes tl landed on 0, which is the
# one thing a device with a non-zero tl minimum will not do -- and it clamps
# that write exactly as silently as it clamps br.
actual_x, actual_y = br_x - tl_x, br_y - tl_y
within_x = abs(actual_x - expected_x) <= tolerance
within_y = abs(actual_y - expected_y) <= tolerance
```

`tests/fake_sane.py` already supports the repro: `build_option_table(geometry_range=(10.0,
300.0, 1.0))` produces the device, so the regression test costs one fixture line.

## Warnings

### WR-01: **WARNING** — A scale factor of `0.0` passes the `is None` guard and writes a zero-size scan area

**File:** `src/saneless/scanner/sane_backend.py:298-338`, `:506-510`

**Issue:** `_units_per_mm` returns `resolution / _MM_PER_INCH` for `UNIT_PIXEL`, which is
`0.0` when the device reads back a resolution that truncates to 0. `_set_geometry` only
rejects `None`:

```python
scale = _geometry_scale(raw_options, resolution)
if scale is None:
    return False
```

so `0.0` proceeds, `expected` becomes `(0.0, 0.0)`, the device stores a zero-size box, and
`_area_matches` agrees with itself (0 == 0 within a tolerance that is also 0). Reproduced:

```
_units_per_mm(UNIT_PIXEL, 0) = 0.0
_set_geometry returned: True area: ((0.0, 0.0), (0.0, 0.0))
```

`actual_resolution` comes from `int(dev.resolution)` (`:862`), so any device reporting
below 1.0 dpi reaches this. Unlikely, but the failure mode is a zero-area scan reported as
success, which is worse than the crop fallback it bypasses.

**Fix:** Guard the value, not just its absence, at the one place it is consumed:

```python
scale = _geometry_scale(raw_options, resolution)
if scale is None or scale <= 0.0:
    # A non-positive scale cannot denominate a paper size; cropping is honest.
    return False
```

---

### WR-02: **WARNING** — The silent `Auto` source fallback downgrades a multi-page scan to one page

**File:** `src/saneless/scanner/sane_backend.py:802-808`, `:1103-1109`

**Issue:** When the requested source is absent, `_resolve_source` substitutes `"Auto"` and
logs at INFO:

```python
if "Auto" in available_sources:
    logger.info("Source '%s' not available, falling back to 'Auto'", effective_source)
    effective_source = "Auto"
```

`scan_pages` then classifies the *effective* source, so `classify_source("Auto")` is
`AUTO`, and routing is decided by `settings.auto_source_mode`, which defaults to
`"flatbed"` (`base.py:177`). A profile asking for `"ADF Duplex"` on a device that offers
only `Flatbed` and `Auto` therefore returns **one page from a whole stack**, with nothing
above INFO to say so. This is the same class of silence as M-11 and C-06 that the phase
removed elsewhere; the substitution is visible for resolution (WARNING at `:863-868`) but
not for the source.

**Fix:** Raise the level and say what changed about routing, since the fallback can alter
page count:

```python
if "Auto" in available_sources:
    logger.warning(
        "Source '%s' not available; falling back to 'Auto', whose routing is "
        "decided by auto_source_mode='%s' and may not be multi-page",
        effective_source,
        # requires threading settings.auto_source_mode in, or logging it in scan_pages
    )
```

Logging it in `scan_pages` beside the existing Auto-routing INFO (`:1105-1109`) avoids
changing `_resolve_source`'s signature.

---

### WR-03: **WARNING** — The flatbed path applies no integrity checks at all

**File:** `src/saneless/scanner/sane_backend.py:1115-1125`

**Issue:** The feeder path validates every page through `_validate_page_image` and counts
rejections; the flatbed path does neither:

```python
dev.start()
image = dev.snap()
image.info.pop("exif", None)
acquired = [image]
pages_rejected = 0
```

The justification given is that "A flatbed exposes one sheet at a time and the caller sees
any failure as an exception" — but the two checks are for the case where there is *no*
exception: a zero-dimension image, or a buffer too small to be a real page, returned
successfully. Such an image flows into `_maybe_crop` and then `assemble_pdf`, where
`img.save(..., format="PNG")` on a 0x0 image is the first thing to notice. The same image
arriving from a feeder is skipped, counted and reported.

**Fix:** Run the same check, and let a failure be fatal here since there is no next sheet
to fall back to:

```python
dev.start()
image = dev.snap()
if not _validate_page_image(image, 1):
    msg = (
        "The scanner returned an unreadable page (zero dimensions, or below "
        f"{_MIN_PAGE_BYTES} bytes of image data)"
    )
    raise ScanError(msg)
```

---

### WR-04: **WARNING** — A source whose slug is `default` is silently lost, defeating `_claim_slug`

**File:** `src/saneless/auto_profiles.py:304-329`

**Issue:** `_claim_slug` exists so that "N distinct source strings yield N profiles"
(`tests/test_auto_profiles.py:553-561`, N-09). But `claimed` never reserves `"default"`,
and the flatbed default is assigned afterwards with a bare `profiles["default"] = ...`, so
it overwrites any source-derived profile that claimed that slug. Reproduced:

```
sources: ['Default', 'Flatbed'] -> profiles: {'default': 'Flatbed', 'flatbed': 'Flatbed'}
```

Two sources in, one source represented — exactly the invariant `_claim_slug` was added to
guarantee, broken by the line that runs after it.

**Fix:** Seed the claim table so the tie-break covers the reserved name too:

```python
# "default" is claimed up front: the flatbed default profile is assigned after
# this loop, so a source slugging to it would otherwise be overwritten -- the
# very loss _claim_slug exists to prevent.
claimed: dict[str, str] = {"default": "<the default profile>"} if has_flatbed else {}
```

---

### WR-05: **WARNING** — A malformed `profiles` key crashes the config writer with a raw `AttributeError`

**File:** `src/saneless/auto_profiles.py:420-426`

**Issue:** The parsed document is cast to a shape it is not guaranteed to have:

```python
profiles_section = cast("dict[str, object]", doc["profiles"])
orphans = [name for name, table in profiles_section.items() if ...]
```

`cast` is a promise to the type checker, not a check. A config with `profiles = "oops"`
reaches `.items()` on a tomlkit `String`. Reproduced:

```
RAISED: AttributeError 'String' object has no attribute 'items'
```

The user sees an unhandled `AttributeError` traceback out of `auto-profiles` rather than a
configuration error. `_is_auto_generated` already guards this exact risk one level down
(`:376-378`, "Anything that is not a mapping ... is therefore never pruned"); the container
itself is not given the same treatment.

**Fix:** Narrow before using, consistent with `_is_auto_generated`:

```python
section = doc["profiles"]
if not isinstance(section, Mapping):
    msg = f"[profiles] in {config_path} is not a table; refusing to overwrite it"
    raise ConfigError(msg)
profiles_section = cast("dict[str, object]", section)
```

---

### WR-06: **WARNING** — The shared fake's `area` raises `KeyError` where python-sane raises `AttributeError`

**File:** `tests/fake_sane.py:890-897`

**Issue:** The fake implements `area` as a property reading `_values` directly:

```python
values = self._values
return ((float(values["tl_x"]), ...), ...)
```

On a device whose option table omits the geometry options — the exact table
`build_option_table(omit=...)` exists to produce — this raises `KeyError`. The real library
composes `area` from attribute reads (`sane.py:220`), so it raises
`AttributeError("No such attribute: tl_x")`. Reproduced side by side:

```
raised: KeyError 'tl_x'
attribute read raised: AttributeError No such attribute: tl_x
```

No test currently reaches it, because `_set_geometry` checks presence before reading
`dev.area`. But that ordering is a property of today's production code, and the fake's
entire premise (module docstring: "The specification is sane.py ... not this docstring") is
that it cannot quietly diverge. A future reordering would meet a `KeyError` the real
library never raises.

**Fix:** Compose it the way the library does, so the divergence cannot reappear:

```python
@property
def area(self) -> tuple[tuple[float, float], tuple[float, float]]:
    """The scan area, reflecting whatever clamping the device applied."""
    # Composed from attribute reads, as sane.py:220 does, so a device without
    # the geometry options raises AttributeError rather than KeyError.
    return (
        (float(self.tl_x), float(self.tl_y)),
        (float(self.br_x), float(self.br_y)),
    )
```

---

### WR-07: **WARNING** — The fake's `start()` makes `start_error_page` silently unreachable past the page budget, and delays the end-of-feed probe

**File:** `tests/fake_sane.py:934-940`

**Issue:** Two ordering problems in one method:

```python
self.calls.append("start")
if self._page_delay:
    time.sleep(self._page_delay)
if self._page_index >= self._pages:
    raise FakeSaneError(_FEEDER_EMPTY_MESSAGE)
if self._start_error is not None and self._page_index == self._start_error_page:
    raise self._start_error
```

First, the budget check precedes the error check, so `FakeSaneDev(pages=3,
start_error=..., start_error_page=3)` never fires its error — the test silently becomes a
clean-feed test rather than failing loudly as a misconfiguration.

Second, `_page_delay` is applied to the final end-of-feed probe as well, so a timeout test
pays one delay more than the page count implies. Harmless today, but it makes wall-clock
reasoning in `TestSaneBackendPerPageTimeout` wrong by one unit.

**Fix:** Check the armed error first, and skip the delay on the probe:

```python
self.calls.append("start")
if self._page_index >= self._pages:
    # No delay here: the end-of-feed probe is not a page being scanned.
    raise FakeSaneError(_FEEDER_EMPTY_MESSAGE)
if self._page_delay:
    time.sleep(self._page_delay)
if self._start_error is not None and self._page_index == self._start_error_page:
    raise self._start_error
```

Consider also raising in `__init__` when `start_error_page >= pages`, so an unreachable
arming is a test error rather than a silent no-op.

---

### WR-08: **WARNING** — Two CLI scanner stubs duck-type the ABC, one with a contradictory return annotation

**File:** `tests/test_cli.py:88-131`, `:230-239`

**Issue:** `MockSaneBackend` and `FailScanner` are plain classes monkeypatched over
`saneless.cli.SaneBackend`; neither subclasses `ScannerBackend`. `FailScanner` goes further
and annotates the method against the ABC:

```python
def scan_pages(self, *_args: object, **_kwargs: object) -> None:
    """Raise a scan error."""
```

`ScannerBackend.scan_pages` returns `ScanBatch`. The annotation is "true" only because the
body always raises, and `ty`/`pyrefly` cannot check it against anything. These are the two
stubs that would *not* have failed when `ScanBatch` replaced the generator this phase —
every stub that does subclass (`test_web.py:49`, `test_web_state_rendering.py:60`,
`test_browser.py:75`) was caught by the compiler.

**Fix:** Subclass the ABC so the type checkers see the contract, and let the raising stub
declare the real return type:

```python
class FailScanner(ScannerBackend):
    """Scanner that always raises ScanError."""

    def __init__(self, host: str = "") -> None: ...
    def get_devices(self) -> list[DeviceInfo]: ...
    def get_capabilities(self, device_id: str) -> DeviceCapabilities: ...
    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        msg = "Paper jam"
        raise ScanError(msg)
```

---

### WR-09: **WARNING** — The failed-directory threshold warning fires once per rescued file

**File:** `src/saneless/pipeline.py:285-291`

**Issue:** `_warn_if_failed_dir_growing` is called inside the per-file loop:

```python
for pdf_path in pdf_paths:
    ...
    shutil.move(pdf_path, destination)
    destinations.append(destination)
    _warn_if_failed_dir_growing(failed_dir)
```

The duplex-mismatch recovery passes two PDFs under one guard
(`pipeline.py:592`), so a single failure emits the same "N preserved scans have
accumulated" WARNING twice, with different counts. The docstring calls it "one WARNING".
Minor, but it is log noise on the one path already flagged as an anomaly.

**Fix:** Move the call after the loop, where it reflects the finished state:

```python
        for pdf_path in pdf_paths:
            ...
            destinations.append(destination)
        if destinations:
            _warn_if_failed_dir_growing(failed_dir)
```

---

### WR-10: **WARNING** — Docs describe the prune without its consequence, and the exit-code table is incomplete

**File:** `docs/how-to/configure-scan-profiles.md:137`, `docs/reference/cli-commands.md:60-70`

**Issue:** The how-to now says "Auto-generated profiles that a new run no longer produces
are removed, so a rename does not leave a stale duplicate behind." The same page states at
line 12 that "The `default` profile is required". Both are true and the reader is left to
discover CR-01 themselves. The CLI reference lists only exit codes 0 and 1 for
`auto-profiles`, while the command can leave the installation in a state where every
invocation exits 2.

Documentation is source in this project, and this is the one user-facing text that could
have warned about the behaviour under review.

**Fix:** Once CR-01 and CR-02 are fixed, add one sentence stating the guarantee rather than
the hazard:

```markdown
Regenerating never removes the `default` profile: saneless requires it, and a config
without it is one saneless refuses to load. Every other auto-generated profile a new run
no longer produces is removed, so a rename does not leave a stale duplicate behind.
Profiles you wrote yourself are never touched.
```

If the fix lands differently, document the hazard and add exit code 2 to the table instead.

---

_Reviewed: 2026-09-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
