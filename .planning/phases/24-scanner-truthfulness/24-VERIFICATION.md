---
phase: 24-scanner-truthfulness
verified: 2026-09-13T00:00:00Z
status: human_needed
score: 5/5 must-haves verified
overrides_applied: 0
deferred:
  - truth: "SCNR-01's literal wording: the pipeline and the worker route feeder decisions through classify_source()"
    addressed_in: "Phase 25"
    evidence: "Phase 25 success criterion 1: '`source` is never inspected for strategy anywhere in the codebase'; requirement DPLX-03: 'The manual-duplex decision is read in exactly one place; the duplicated detection rule in the worker ... are gone'"
  - truth: "docs/how-to/set-up-adf-duplex.md stops documenting `source = \"Manual Duplex\"` as the current way to select manual duplex"
    addressed_in: "Phase 25"
    evidence: "Phase 25 goal: '... with the ADF duplex how-to rewritten in-phase to stop documenting `source = \"Manual Duplex\"` as current'"
human_verification:
  - test: "Confirm the 10 KB `_MIN_PAGE_BYTES` floor is the right policy for single-sheet flatbed scans"
    expected: "A genuinely small legitimate scan (receipt, business card at low dpi) should not fail the job outright"
    why_human: "Product policy decision, not a derivable fact. Raised by the phase's own fix report as WR-03 'fixed: requires human verification'"
  - test: "Confirm that on a flatbed-less sheet-fed scanner, the device's first reported source is the right thing to back the `default` profile"
    expected: "The generated `default` profile is the one the operator would have chosen"
    why_human: "Product decision with no derivable answer. Raised by the phase's own fix report as CR-02 'fixed: requires human verification'"
  - test: "Run one scan against the operator's real scanner with a paper size set, and confirm the page is not short"
    expected: "The `br - tl` clamp detection either sets the area correctly or falls through to the crop, producing a correctly sized page"
    why_human: "The CR-03 defect was found by reasoning about real device geometry ranges. The SANE `test` backend is a second anchor and passes, but it is not the operator's device"
---

# Phase 24: Scanner Truthfulness Verification Report

**Phase Goal:** The scanner layer reports what actually happened — the single `classify_source()`
drives every feeder decision, real SANE errors carry their real messages, geometry and DPI are read
from the device rather than assumed, and the test doubles behave like python-sane 2.9.2 — with the
scanner-discovery and ADF docs corrected in-phase

**Verified:** 2026-09-13
**Status:** human_needed (5/5 criteria verified; 3 product-policy/hardware items escalated)
**Re-verification:** No — initial verification (skipped at phase close due to context exhaustion)

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A device whose feeder is named "Automatic Document Feeder" scans a full stack, and auto-profiles never collapses two distinct feeder sources into one slug | ✓ VERIFIED | **Routing:** `base.py:88-95` tests `lower == "auto"` by EXACT equality *before* the feeder tokens, so "Automatic Document Feeder" cannot be swallowed by an `auto` substring test; `_FEEDER_TOKENS` (`base.py:52`) includes `"document feeder"`, so the name classifies FEEDER with no `adf` token present. `sane_backend.py:1121-1122` derives `use_adf` from `classify_source(effective_source).uses_feeder` and from nothing else. Proved by `tests/test_scanner.py::TestSaneBackendAutomaticDocumentFeeder::test_automatic_document_feeder_yields_all_pages` (`:501`), which asserts **3 pages** come back *and* `snap()` is never called (`mock_dev.calls == _THREE_SHEET_FEEDER_CALLS`) — a count-only assertion would not have distinguished the two paths. Reinforced by `::TestAutoSourceRecognition::test_a_long_feeder_name_never_takes_the_auto_override` (`:1116`), which sets `auto_source_mode="flatbed"` and still gets 3 pages, proving the name is a feeder *by classification* rather than by the Auto override. Classification itself is parametrised at `:164-166`, including the two real aligned variants. **Slugs:** `auto_profiles.source_to_slug` (`:76-101`) slugs every source from its own name; the old kind→name mapping that collapsed "ADF Front" and "ADF Back" onto one profile is gone. `_claim_slug` (`:225-266`) gives the first claimant the bare slug and each later collider a `-2`/`-3` suffix, with a WARNING naming both sources, and `"default"` is reserved before the loop (`:355-357`) so a source slugging to `default` cannot be silently overwritten. Proved by `tests/test_auto_profiles.py:134` (`source_to_slug("ADF Front") != source_to_slug("ADF Back")`), `:572-588` (`_COLLIDING = ("ADF-Front", "ADF Front")` → `profiles["adf-front-2"].source == "ADF Front"`, i.e. neither source is lost), `:613` (warning names both) and `:724` (reserved-slug warning). **Against real libsane:** `tests/test_sane_hardware.py::test_capabilities_report_the_long_feeder_name` and `::test_ten_pages_come_back_through_the_feeder` — executed, see Probe Execution. |
| 2 | A first-page SANE error other than the exact "Document feeder out of documents" message surfaces as a `ScanError` carrying the SANE text, never as "No paper detected" | ✓ VERIFIED | `sane_backend._acquire_pages:730-743` has **no first-page special case**: `StopIteration` breaks, saneless's own `ScanError` (incl. `FeederEmptyError` and the timeout path) re-raises unchanged, and every other exception becomes `ScanError(f"Scanner error on page {page_num + 1}: {exc}") from exc` — the SANE text is interpolated verbatim and the original is chained. `FeederEmptyError(_FEEDER_EMPTY_MESSAGE)` is raised at exactly one place, `:775-776`, guarded by `if page_num == 0` — so "No paper detected in feeder" is reachable only when the feeder produced no pages at all. The unreachable `multi_scan()` guard is gone (`:717`, with the rationale at `:670-676`). Proved by `tests/test_scanner.py::TestAdfPageErrorsAreTruthful` (`:628-695`): `test_first_page_fault_surfaces_as_scan_error` is parametrised over `_MEASURED_SANE_FAULTS` (`:585-590` — "Error during device I/O", "Document feeder jammed", "Scanner cover is open", "Device busy") and asserts for each that the SANE message is in the text, that "page 1" is named, and — the load-bearing line — `not isinstance(exc_info.value, FeederEmptyError)`. `test_first_page_fault_keeps_the_original_as_cause` asserts `__cause__ is original`. `test_mid_stack_jam_names_the_one_based_page` proves a fourth-sheet jam says "page 4", not "page 0". `test_out_of_documents_still_means_an_empty_feeder` (`:687`) pins the converse: the one converted message is still the only route to the feeder message. |
| 3 | The backend drops no pages on its own; blank-page removal happens only in the pipeline, only when the profile enables it, and manual-duplex page parity survives | ✓ VERIFIED | `_validate_page_image` (`:587-623`) now performs exactly two integrity checks — nonzero dimensions and `raw_size >= _MIN_PAGE_BYTES` — and inspects nothing about what is printed on the page; the pure-white and pure-black content policy is deleted, with the reason recorded in the docstring (`:594-600`). Blank-page policy lives in exactly one place, `pipeline._drop_empty_pages` (`:311-344`), which returns the input list untouched when `not profile.enable_empty_page_detection` (`:330-332`) and otherwise filters on the profile's own user-visible mean/stddev thresholds. **Non-vacuous proof the backend keeps them:** `tests/test_scanner.py::test_pure_white_page_survives` (`:784`) and `::test_pure_black_page_survives` (`:808`) each feed a blank plus a content page and assert `len(pages) == 2` **and** `pages[0].convert("L").getextrema() == (255,255)` / `(0,0)` — so the blank itself survived rather than the content page arriving twice. **Parity:** `tests/test_pipeline.py::test_both_passes_run_through_the_backend_and_interleave` (`:797`) drives a real `SaneBackend` over the shared fake through both manual-duplex passes and asserts 6 assembled pages from 3 fronts + 3 backs. `_scan_manual_duplex` (`pipeline.py:720-735`) sums `pages_rejected` across both passes ("a sheet lost on either pass is a sheet lost") and compares raw counts *before* empty-page detection (`:724-731`). **Integrity failures are skipped and counted, never silently dropped:** `TestIntegrityFailuresAreSkippedAndCounted` (`:924-993`) proves one bad third sheet costs exactly one page and is named in a WARNING, that a wholly rejected batch *raises* rather than returning `[]` into `assemble_pdf([])`, and that a zero-page feeder still raises `FeederEmptyError` distinctly. The count leaves the backend only via `ScanBatch.pages_rejected` and is rendered through `_rejected_pages_warning` (`pipeline.py:472-501`), worded so it cannot be mistaken for blank-page removal. **Against real libsane:** `test_every_uniformly_black_page_survives_the_scanner_layer` — ten solid-black pages at mean 0.0/stddev 0.0, the exact statistics the deleted policy keyed on, all ten survive. Executed. |
| 4 | Geometry is written only when the device reports `tl_x`/`tl_y`/`br_x`/`br_y` with units read from the option descriptor; a test proves the Pillow crop fallback is reachable and uses the resolution read back after all options are set | ✓ VERIFIED | **Presence check:** `_missing_geometry_options` (`:379-393`) asks the device's own `get_options()` list for all four hyphenated names, and `_set_geometry` (`:515-527`) consults it *before* assigning anything — the only way to distinguish "has no geometry" from "accepted the write", since the real `SaneDev.__setattr__` stores an unknown name silently. `_geometry_scale` (`:396-426`) collapses the three refusal causes into one `None`, each logging its own WARNING. **Units from the descriptor:** `_geometry_unit` (`:341-376`) reads index 5 of the `br-x` option tuple; `GeometryUnit` (`:269-295`) enumerates all seven SANE unit codes and `_units_per_mm` (`:298-338`) dispatches with `match` + `assert_never` (deliberately not a dict, so a missing member is a type error rather than a silent mis-scale). **Order:** `scan_pages` calls `_configure_device` (`:1107`, which assigns source→mode→resolution and reads the resolution back at `:892`) and only then `_set_geometry(..., actual_resolution)` (`:1117-1119`); the crop at `:1178-1186` is likewise passed `actual_resolution`. **Reachability proved, not asserted:** `TestGeometryPresenceCheck::test_a_device_without_geometry_options_stores_br_y_silently` (`:1517`) first pins the premise — the fake stores `br_y` with `dev.assignments == []`, i.e. no device call — so the fallback tests below it cannot pass for the wrong reason; then `::test_the_crop_fallback_produces_a_correctly_sized_page` (`:1532`) scans a 3000x4000 page on a geometry-less device and asserts `pages[0].size == _A4_AT_300_DPI`, a size assertion rather than a return-value assertion. `::test_the_missing_option_is_named_in_the_warning` (`:1550`) shows one absent option (`br-y`) is enough. Units: `test_pixels_use_the_resolution_read_back_from_the_device` (`:1676`), `test_an_unconvertible_unit_falls_through_to_the_crop` (`:1698`), `test_a_unit_code_outside_sane_does_not_crash_the_scan` (`:1717`), `test_every_sane_unit_code_is_a_member` (`:1636`). Read-back resolution reaching the crop: `test_the_crop_uses_the_resolution_the_device_chose` (`:1874`). Source-first ordering: `TestDeviceOptionOrdering::test_options_are_assigned_source_first` (`:2203`) asserts `dev.assignments == ["source", "mode", "resolution"]`, and `::test_source_first_keeps_resolution_inside_a_narrowed_ceiling` (`:2244`) proves a 1000 dpi request lands inside a feeder's 600 dpi ceiling. Clamp detection (D-19) is covered by `TestClampedScanArea` (`:1784-1832`), including the clamped-`tl` case `_area_matches` (`:429-478`) was rewritten to catch. |
| 5 | The rewritten fakes match real python-sane semantics (unknown option stored silently, bad value for a known option raises `_sane.error`, structurally wrong access raises `AttributeError`), and an opt-in integration test drives the real SANE `test` backend through a `SANE_CONFIG_DIR` scoped to `tmp_path` to pull ten pages from a long feeder name | ✓ VERIFIED | **Fake semantics**, all three rows asserted in `tests/test_scanner.py::TestFakeSaneContract` (`:1905-2033`), parametrised so a future fake row cannot be added without being asserted: *unknown option stored silently* — `test_unknown_option_is_stored_silently` (`:1985`) sets `dev.no_such_option = 1` and reads it back, the exact inverse of the deleted geometry-less double that raised here and thereby made the crop fallback untestable; *bad value for a known option* — rows `("source", "Nope", FakeSaneError, "Invalid argument")` and `("mode", "Sepia", ...)` (`:1925-1926`), with `test_error_type_mirrors_the_real_error_mro` confirming the local error type stands in for `_sane.error`; *structurally wrong access* — `AttributeError` rows for read-only attributes (`dev`, `area`, `optlist`), buttons, groups, inactive and not-software-settable options (`:1943-1969`), plus the read side in `test_getattr_follows_the_measured_contract` (`:2006`). `test_multi_scan_cannot_raise` (`:2020`) pins that `multi_scan()` only constructs the iterator, so the backend's deleted guard was genuinely unreachable. One shared definition: `tests/fake_sane.py` holds `FakeSaneDev`/`FakeSaneModule`/`FakeSaneError` and is imported package-qualified by both `tests/test_scanner.py:25` and `tests/test_pipeline.py:42`; the three legacy hand-written SANE doubles are gone (the only remaining `Mock*`/`Stub*` classes in the suite are `ScannerBackend`-level stubs in `test_cli.py`/`test_web.py`, not `sane` module doubles). **Integration test:** `tests/test_sane_hardware.py` exists, is marked `@pytest.mark.sane_hardware` at class level (`:59`), and its autouse fixture (`:27-56`) writes `dll.conf` naming only the `test` backend into `tmp_path_factory.mktemp("sane.d")` and sets `SANE_CONFIG_DIR` to it via `pytest.MonkeyPatch.context()`, guaranteeing the variable is unset at session end. `test_ten_pages_come_back_through_the_feeder` (`:101-122`) scans `source="Automatic Document Feeder"` and asserts `len(pages) == 10`. The marker is registered in `pyproject.toml:154` (required — `--strict-markers` is live), deselected by the default command, and run as its own CI step (`.github/workflows/ci.yml:50`). **These tests were EXECUTED during this verification against real libsane: 5 passed** — see Probe Execution. |

**Score:** 5/5 truths verified

### Deferred Items

Not yet met, but explicitly owned by a later phase in this milestone. Not actionable gaps.

| # | Item | Addressed In | Evidence |
|---|------|-------------|----------|
| 1 | SCNR-01's literal wording that **the pipeline and the worker** route feeder decisions through `classify_source()`. They do not: `pipeline.py:383-385` `_is_manual_duplex` and `worker.py:193-194` each carry their own `"manual" in source.lower() and "duplex" in source.lower()` rule. Both are *manual-duplex strategy* decisions rather than feeder-routing decisions — no feeder/flatbed routing choice is made in either module — but they are source-string inspection, which is what SCNR-01 set out to centralise. | Phase 25 | Phase 25 success criterion 1: "`source` is never inspected for strategy anywhere in the codebase". Requirement DPLX-03: "The manual-duplex decision is read in exactly one place; the duplicated detection rule in the worker and the `isinstance` dispatch on the two-outcome result are gone" |
| 2 | `docs/how-to/set-up-adf-duplex.md:68` still shows `source = "Manual Duplex"` as the way to select manual duplex | Phase 25 | Phase 25 goal, verbatim: "with the ADF duplex how-to rewritten in-phase to stop documenting `source = \"Manual Duplex\"` as current" |

Neither deferral affects any of the five success criteria, all of which are verified independently of them.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/scanner/base.py` | `classify_source` as the only rule, `SourceKind.uses_feeder`, `ScanBatch`, ABC returning a batch | ✓ VERIFIED | `:55-113` (exact-equality `auto` first, `duplex` before feeder tokens, D-01 settled in-comment), `:39-49` (`uses_feeder` via `match`/`assert_never`), `:181-218` (`ScanBatch`, frozen, three fields), `:246-265` (`scan_pages` returns `ScanBatch`, not a generator) |
| `src/saneless/scanner/sane_backend.py` | Presence-checked geometry, unit enum, page cap, no content policy, `_constraint` helper, source-first config | ✓ VERIFIED | `:100` `_MAX_ADF_PAGES = 500`; `:233-266` single `_constraint()`; `:269-295` `GeometryUnit` (7 codes); `:379-426` presence + unit + scale; `:587-623` two integrity checks only; `:849-899` source-first + read-back; `:1190-1194` returns `ScanBatch` |
| `src/saneless/auto_profiles.py` | `classify_source`-driven, per-source slugs, collision tie-break, orphan prune, range-aware resolution | ✓ VERIFIED | `:47-101` `_slugify`/`source_to_slug` over `[a-z0-9-]`; `:225-266` `_claim_slug`; `:289-291`/`:334-343` every source question answered by `classify_source`; `:104-173` `_snap_into_range`/`pick_closest_resolution`; `:442` `_UNPRUNABLE`; `:500-513` orphan prune; `:494-498` refuses a non-table `[profiles]` |
| `src/saneless/pipeline.py` | Blank removal gated on the profile; actual DPI used for assembly; reject count surfaced distinctly | ✓ VERIFIED | `:311-344` `_drop_empty_pages`; `:925-930` `assemble_pdf(..., dpi=actual_dpi)`; `:720` `dpi = _duplex_resolution(...)`; `:472-501` `_rejected_pages_warning`; `:973-977` `pages_removed` is blank-detection only |
| `tests/fake_sane.py` | Single shared fake faithful to python-sane 2.9.2 | ✓ VERIFIED | 1085 lines; `FakeSaneError` (`:543`), `FakeSaneDev` (`:597`), `FakeSaneModule` (`:1017`); imported by `test_scanner.py:25` and `test_pipeline.py:42` |
| `tests/test_sane_hardware.py` | Marker-gated module driving real libsane, `SANE_CONFIG_DIR` under a pytest temp dir, ten-page assertion | ✓ VERIFIED | 142 lines, 5 tests, all executed and passing; see Truth 5 and Probe Execution |
| `pyproject.toml` | `sane_hardware` marker registered | ✓ VERIFIED | `:154`, under `[tool.pytest.ini_options] markers` |
| `.github/workflows/ci.yml` | Default run deselects the marker; a separate step runs it | ✓ VERIFIED | `:46` `-m "not browser and not sane_hardware"`; `:50` `-m sane_hardware`, with a comment recording that `libsane-dev` transitively supplies `libsane-test.so.1` so no extra apt package is needed |
| `src/saneless/cli.py` | `devices --capabilities` reports a reported range | ✓ VERIFIED | `:159-195` `_echo_capabilities`, extracted specifically to stay under `PLR0912` without suppressing it; range branch at `:181-186` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `sane_backend.scan_pages` | `base.classify_source` | `classify_source(effective_source).uses_feeder` | ✓ WIRED | `:1121-1122`; the Auto override at `:1135` keys on `SourceKind.AUTO`, not on `== "Auto"` |
| `auto_profiles.generate_profiles` | `base.classify_source` | flatbed detection + Auto routing | ✓ WIRED | `:334-343`, `:289-291`; the three former local string rules are gone |
| `auto_profiles.generate_profiles` | `_claim_slug` | per-source slug with tie-break | ✓ WIRED | `:359-360`, with `"default"` pre-reserved at `:355-357` |
| `sane_backend._configure_device` | `_set_geometry` | `actual_resolution` passed after all options are set | ✓ WIRED | `:1107-1119`; ordering is what makes the read-back authoritative |
| `sane_backend.ScanBatch` | `pipeline.assemble_pdf` | `dpi=actual_dpi` | ✓ WIRED | `:905-906` → `:929`; duplex path `:720` → `:735` → `:2038` test |
| `ScanBatch.pages_rejected` | `ScanResult.warning` | `_rejected_pages_warning` + `_join_warnings` | ✓ WIRED | `:907`, `:978`; kept out of `pages_removed` deliberately (`:973-975`) |
| `tests/test_scanner.py`, `tests/test_pipeline.py` | `tests/fake_sane.py` | package-qualified import | ✓ WIRED | `test_scanner.py:25`, `test_pipeline.py:42` |
| `.github/workflows/ci.yml` | `sane_hardware` marker | dedicated `pytest -m sane_hardware` step | ✓ WIRED | `ci.yml:50`; confirmed to select exactly the 5 tests |
| `cli.devices --capabilities` | `DeviceCapabilities.resolution_range` | `_echo_capabilities` range branch | ✓ WIRED | `cli.py:181-186`, proven end-to-end by `tests/test_cli.py:399` asserting the literal output line "Resolution range: 1 to 1200 dpi in steps of 1" |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|---------------------|--------|
| PDF page geometry | `actual_dpi` | `dev.resolution` read back in `_configure_device:892` → `ScanBatch.actual_resolution` → `assemble_pdf(dpi=...)` | Yes — `test_the_crop_uses_the_resolution_the_device_chose` and `test_the_duplex_mismatch_recovery_also_uses_the_actual_dpi` drive a substituting device and assert the substituted value is the one used | ✓ FLOWING |
| `devices --capabilities` resolution line | `caps.resolution_range` | `_constraint(raw_options, "resolution").span` → `DeviceCapabilities.resolution_range` → `_echo_capabilities` | Yes — `test_cli.py:399` asserts the rendered line from a range-only scanner; `test_sane_hardware.py:98` confirms real `test:0` yields `(1.0, 1200.0, 1.0)` and that `resolutions` stays `[]` rather than being synthesised | ✓ FLOWING |
| Generated profile `resolution` | `pick_closest_resolution(...)` | `capabilities.resolution_range` → `_snap_into_range` | Yes — `test_auto_profiles.py:242-307` parametrises the range shapes and asserts the result is on the step grid and inside the span, incl. a `(1.0, 200.0, 1.0)` ceiling below the 300 default | ✓ FLOWING |
| Feeder reject count in the job warning | `batch.pages_rejected` | `_acquire_pages` skip-and-count → `ScanBatch` → `_rejected_pages_warning` → `ScanResult.warning` | Yes — asserted by the 24-07 tests and by `TestIntegrityFailuresAreSkippedAndCounted`; wording proven distinct from blank-page removal | ✓ FLOWING |

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|-------------|-------------|--------|----------|
| SCNR-01 | Feeder decisions route through `classify_source()`; long feeder name scans a stack; no slug collapse | ⚠️ SATISFIED IN PART | The two halves the roadmap made contractual (Truth 1) are fully verified. `sane_backend` and `auto_profiles` route every source question through `classify_source`. **Residue:** `pipeline.py:385` and `worker.py:194` still inspect the source string for *manual-duplex strategy*. Deferred to Phase 25 (DPLX-03 / SC-1), which names exactly this. Not a blocker for the phase goal — no feeder/flatbed routing decision is made in either module |
| SCNR-02 | First-page SANE error raised as `ScanError` with the SANE message | ✓ SATISFIED | Truth 2 |
| SCNR-03 | Backend never drops blank pages; detection only in the pipeline behind the toggle; duplex parity preserved | ✓ SATISFIED | Truth 3. D-08's accepted reading is recorded at `sane_backend.py:690-699`: parity broken by *policy* is forbidden; parity broken by a sheet the device could not read is *reported* via `_handle_duplex_mismatch`, never hidden. That is the stronger guarantee, not a weakening |
| SCNR-04 | Geometry set only when reported; crop fallback proven reachable; units from the descriptor | ✓ SATISFIED | Truth 4 |
| SCNR-05 | Crop and page-size maths use the read-back resolution; options set source-first | ✓ SATISFIED | Truth 4 (`:1107-1119`, `:2203`, `:2244`, `:1874`) |
| SCNR-06 | `get_capabilities` honours range constraints and reports them in `devices --capabilities` | ✓ SATISFIED | Single `_constraint()` helper (`:233-266`) replacing two partial copies; `_as_span` (`:201-230`) refuses a malformed constraint rather than guessing; `DeviceCapabilities.resolution_range` typed and documented as mutually exclusive with `resolutions` (`base.py:126-167`); CLI render + test at `cli.py:181` / `test_cli.py:399`; real-device confirmation at `test_sane_hardware.py:98` |
| SCNR-07 | Test doubles mirror python-sane 2.9.2 on all four rows | ✓ SATISFIED | Truth 5 |
| SCNR-08 | Opt-in integration module drives the real `test` backend via `SANE_CONFIG_DIR`, ten pages via a long feeder name | ✓ SATISFIED | Truth 5; executed, 5 passed |

**Bookkeeping note (non-blocking, matches the Phase 23 finding):** `.planning/REQUIREMENTS.md` still marks
SCNR-01..08 as `Pending` in the traceability table (`:261-268`) with their checklist items unchecked
(`:64-71`), although ROADMAP.md marks all eight Phase 24 plans `[x]` and the code evidence above is
conclusive. This is a documentation gap, not a functional one, and it does not affect any success
criterion. Recommend flipping these rows — together with the OUTC-01..11 rows that Phase 23's
verification raised and that are still `Pending` — in a single housekeeping edit.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | — | — | None found |

- **Debt markers:** `grep -rnE "TBD|FIXME|XXX"` across all of `src/` and `tests/` returns **zero
  matches**. The debt-marker gate passes outright.
- **Suppressions:** zero `# type: ignore` and zero pyrefly/ty suppressions anywhere in `src/` or
  `tests/`. Seven `# noqa` comments exist, all ruff-rule exemptions carrying a stated reason, and
  **none were introduced by Phase 24** — the two in `sane_backend.py` (`:52` `PLW0603`, `:54`
  `PLC0415`, both for the deliberately deferred `sane` import) trace via `git log -S` to commit
  `f8e2988` in Phase 01. The phase repeatedly chose to *restructure* rather than suppress:
  `_scan_adf_pages` and `scan_pages` were split for `PLR0912` headroom (24-03, 24-05),
  `_echo_capabilities` was extracted for the same reason (`cli.py:161-166`), and
  `_DeliveryContext`/`_DuplexMismatch` exist to respect `PLR0913`. This matches CLAUDE.md's
  prohibition exactly.
- **Stub patterns:** no placeholder returns, no empty handlers, no hardcoded-empty data reaching a
  rendered surface. The one `return []`-shaped construct examined (`_constraint` returning
  `_OptionConstraint(present=False, ...)`) is a genuine "device does not report this option" answer
  with a dedicated `present` field precisely so absence is not confused with an empty constraint.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Core scanner truthfulness classes | `uv run pytest tests/test_scanner.py -q -k "FakeSaneContract or AdfPageErrorsAreTruthful or GeometryPresenceCheck or AutomaticDocumentFeeder or ClampedScanArea or ResolutionReadBack or DeviceOptionOrdering"` | 60 passed, 138 deselected in 0.48s | ✓ PASS |
| Auto-profiles (slugs, collisions, prune, range) | `uv run pytest tests/test_auto_profiles.py -q` | 93 passed in 0.08s | ✓ PASS |
| Pipeline duplex + empty-page paths | `uv run pytest tests/test_pipeline.py -q -k "duplex or empty"` | 28 passed, 53 deselected in 0.17s | ✓ PASS |
| Marker selects exactly the integration module | `uv run pytest -m sane_hardware --collect-only -q` | 5/966 collected, 961 deselected | ✓ PASS |
| No debt markers in phase-touched code | `grep -rnE "TBD\|FIXME\|XXX" src/ tests/` | 0 matches | ✓ PASS |
| No type-checker suppressions | `grep -rc "type: ignore" src/ tests/` | 0 matches | ✓ PASS |

**Gate state (run by the developer on commit `781bef5` immediately before this verification, cited as
given, not re-run):** `uv run ruff check .` — no issues; `uv run ruff format --check .` — 43 files
already formatted; `uv run ty check` — all checks passed; `uv run pyrefly check src tests` — 0 errors
(4 pre-existing warnings, not grown); `uv run pytest -m "not browser and not sane_hardware" -q` —
931 passed.

### Probe Execution

This repository declares no `scripts/*/tests/probe-*.sh` files, and none are referenced by any Phase 24
plan or summary. The phase's equivalent runnable check is the marker-gated integration module, which
was **executed in this verification process rather than taken on the summaries' word**:

| Probe | Command | Result | Status |
|-------|---------|--------|--------|
| SCNR-08 real-libsane integration suite | `uv run pytest -m sane_hardware -q` | **5 passed, 961 deselected in 0.25s** (exit 0) | ✓ PASS |

This is the strongest single piece of evidence in the report, and it is worth being precise about what
it establishes. The suite ran against **real libsane** on this machine, not against a double: it
enumerated `test:0` by name (a positive assertion, deliberately chosen so that an empty device list
cannot pass for the right reason), read `resolution_range == (1.0, 1200.0, 1.0)` with `resolutions == []`
straight from the device, pulled **ten pages** through the source string "Automatic Document Feeder",
and confirmed all ten arrive uniformly black — the exact statistics the deleted content policy keyed
on. Criteria 1, 3, 5 and SCNR-06 therefore each have a real-hardware anchor in addition to their
fake-based unit proofs. The run is fast (0.25s) because the SANE `test` backend synthesises pages in
memory at 75 dpi Gray; that is expected and does not indicate the scans were skipped, since the page
count and per-page pixel extrema are both asserted.

### Deviations Assessed

- **`SANE_CONFIG_DIR` is scoped to `tmp_path_factory.mktemp("sane.d")`, not the function-scoped
  `tmp_path`.** The roadmap criterion says "scoped to `tmp_path`". The fixture is session-scoped and
  `autouse`, and the docstring (`test_sane_hardware.py:29-51`) records this as *measured*, not
  stylistic: `SANE_CONFIG_DIR` is honoured only before the first `sane.init()` in a process,
  `sane.exit()` plus a re-init does not reset it, and backends accumulate across re-inits so a
  developer's real scanner would never leave the device list. The natural function-scoped
  `monkeypatch.setenv` was measured to fail. The intent behind the criterion — a pytest-managed
  temporary directory, isolated from the developer's real SANE config and torn down afterwards — is
  fully met, and `pytest.MonkeyPatch.context()` guarantees the variable is unset at session end.
  Accepted; this is the correct implementation of the requirement, and the literal wording was simply
  written before the constraint was measured.
- **`docs/how-to/scanner-host-discovery.md` was not edited**, although the phase goal's wording
  ("the scanner-discovery and ADF docs corrected in-phase") implies work there. `24-02-SUMMARY.md:97`
  records this as a deliberate, mapped finding: the page was read end to end during pattern mapping,
  every claim already matched `sane_backend.py`, and the file has zero diff across the phase. I
  independently confirm the file was not modified. The ADF doc *was* edited — `set-up-adf-duplex.md`
  gained the "Manual duplex needs a document feeder" admonition explaining that a `duplex`-named
  source is treated as a feeder — and `configure-scan-profiles.md:134` was corrected to describe the
  real slug rule (it had advertised `flatbed-color-300`/`adf-gray-150`, a shape no version of
  `source_to_slug` ever produced). I did not re-read `scanner-host-discovery.md` end to end myself, so
  the "already correct" claim is accepted on the recorded audit rather than independently reproduced;
  it gates no success criterion.
- **D-08: a skipped front breaks manual-duplex parity**, and this is accepted deliberately rather than
  papered over (`sane_backend.py:690-699`). A page the device could not read genuinely means the two
  passes no longer correspond, so the mismatch is routed to `_handle_duplex_mismatch` — two partial
  PDFs plus a warning — instead of being hidden. Criterion 3's "parity survives" is satisfied under
  the explicit reading that it forbids parity broken by *policy*, which is precisely what was removed.

None of the assessed deviations weakens a property the phase goal depends on.

### Human Verification Required

All three items come from the phase's own code-review fix report, which flagged them as
`fixed: requires human verification` rather than assuming them correct. None is a browser or UI check
— per CLAUDE.md those must be automated, and this phase has no browser surface. Two are product-policy
decisions with no derivable answer, and one asks for a run against the operator's physical scanner.

#### 1. Confirm the 10 KB minimum-page-bytes floor is right for single-sheet scans

**Test:** Scan a small legitimate original — a receipt or business card — at a low resolution on the
flatbed, through a profile whose paper size is set.
**Expected:** Decide whether it should produce a small PDF or fail the job.
**Why human:** WR-03 changed a flatbed page below `_MIN_PAGE_BYTES` (`sane_backend.py:77`, 10 KB) from
flowing through to a hard `ScanError` (`:1165-1171`), for symmetry with the feeder path. The threshold
is justified against the SANE `test` backend's smallest legitimate page (69,620 bytes, ~7x the floor),
but whether a *fatal* response is the right policy for a single sheet, where there is no next page to
skip to, is a product decision.

#### 2. Confirm the default-profile source on a flatbed-less scanner

**Test:** Run `saneless auto-profiles` against a sheet-fed document scanner with no flatbed and inspect
the generated `[profiles.default]`.
**Expected:** The `default` profile is backed by the source you would have chosen.
**Why human:** CR-02 made `generate_profiles` always emit a `default` profile (`auto_profiles.py:369-380`),
because `Settings.validate_default_profile` makes the key mandatory and a generated set without it is
written to disk and then refused on the next load. On a device with no flatbed it falls back to the
device's *first reported source* (`:344-346`). That is the only honest candidate available, but which
source a given scanner reports first is a product question, not a derivable fact.

#### 3. One geometry run against real scanner hardware

**Test:** Scan an A4 page on the operator's own scanner with `paper_size = "a4"` and check the output
is a full A4 page, neither short nor cut off.
**Expected:** Either the device accepts the area, or the clamp is detected and the Pillow crop fallback
produces a correctly sized page.
**Why human:** CR-03 rewrote `_area_matches` (`:429-478`) to compare the reported box `br - tl` rather
than the far corner alone, after a device with a `(10.0, 300.0, 1.0)` `tl` range was found to turn an
A4 request into a 200x287 mm scan reported as success. The arithmetic is verified against the shared
fake and the `sane_hardware` suite passes against the SANE `test` backend, so this is a confirmation
rather than an open question — but the original defect was found by reasoning about real device ranges,
and the `test` backend is not the operator's device.

### Gaps Summary

**No gaps block the phase goal.** All five roadmap success criteria are verified against the code and
against tests that were executed during this verification, including — unusually — the opt-in
integration suite, which passed against real libsane and gives criteria 1, 3 and 5 a hardware anchor
rather than a fake-only one.

The audit was run adversarially: for each criterion I looked first for the way it could be satisfied
vacuously, and in each case the tests forestall it. The blank-page tests assert the blank image's own
pixel extrema rather than merely counting pages, so a content page arriving twice would fail them. The
crop-fallback test asserts the output's pixel dimensions rather than a boolean return, and is preceded
by a test pinning the premise that the fake *stores* an unknown option — the exact defect that let the
old geometry double keep an unreachable fallback green. The long-feeder-name test asserts `snap()` is
never called, not just that three pages arrived. The first-page-fault tests assert
`not isinstance(exc, FeederEmptyError)` on every parametrised row. These are the assertions that would
have caught the defects this phase set out to fix, and they are present rather than approximated.

Two items fall short of the requirements' *literal* wording and both are explicitly owned by Phase 25,
so they are recorded as deferred rather than as gaps: the manual-duplex source-string rules still
duplicated in `pipeline.py:385` and `worker.py:194` (Phase 25 SC-1 and DPLX-03 name exactly this), and
the ADF how-to still showing `source = "Manual Duplex"` (Phase 25's goal names exactly this file and
string). Neither touches a feeder-routing decision, so neither weakens criterion 1.

Status is `human_needed` rather than `passed` solely because the phase's own fix report left three
items marked `requires human verification` and they remain open. Two are product-policy confirmations
and one asks for a single run against physical hardware; none is a browser check, and none indicates
defective code. Nothing here should block planning Phase 25.

One housekeeping item is carried forward: `.planning/REQUIREMENTS.md` still marks SCNR-01..08
`Pending`, as it does the Phase 23 OUTC rows. That tracker is now two phases behind the code.

---

_Verified: 2026-09-13_
_Verifier: Claude (gsd-verifier)_
