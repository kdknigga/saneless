# Phase 24: Scanner Truthfulness - Context

**Gathered:** 2026-09-12
**Status:** Ready for planning

<domain>
## Phase Boundary

The scanner layer reports what actually happened. Everything in this phase lives in
`src/saneless/scanner/base.py`, `src/saneless/scanner/sane_backend.py` (515 lines),
`src/saneless/auto_profiles.py`'s slug/capability logic, and the SANE test doubles in
`tests/`, plus the scanner-discovery and ADF documentation corrected in-phase.

Concretely, this phase closes seven gaps that exist in the code today:

1. **`classify_source()` is not yet the only rule.** Phase 21 shipped it and wired
   `sane_backend.scan_pages` (`:492`), but `auto_profiles.py:182` and `:193` still ask
   `"flatbed" in s.lower()` — a second classification rule, which is the exact shape of
   defect C-06.
2. **Every first-page failure is reported as "No paper detected in feeder."**
   `sane_backend.py:403-406` converts any exception on page 0 into `FeederEmptyError`, so a
   jam, an open cover, a busy device and an I/O error all tell the user to load paper (M-11).
   The `try/except` around `dev.multi_scan()` at `:372-375` is unreachable with the real API,
   which returns an iterator and cannot raise.
3. **The backend drops pages on its own.** `_validate_page_image` (`:159-212`) discards any
   page with mean > 254 and stddev < 1, regardless of the profile's
   `enable_empty_page_detection` toggle. In Lineart a clean blank page is exactly mean 255.0 /
   stddev 0.0, so it is dropped before the pipeline sees it — breaking manual-duplex parity and
   making the documented "keep all pages" setting untrue (M-14).
4. **The Pillow crop fallback is unreachable.** `_set_geometry` (`:95-133`) assumes assigning
   `dev.tl_x` raises on a device without that option. The real `SaneDev.__setattr__`
   (verified in the installed `sane.py:188`) stores unknown keys silently, so the function
   returns `True`, no crop happens, and `paper_size` is silently ignored (M-15). It also writes
   `br_x = 210.0` assuming millimetres, though the unit lives at option-tuple index 5 (N-03).
5. **Resolution is never read back.** SANE backends clamp or snap resolution and report it;
   `dev.resolution = 5000` returned 1200 on the `test` backend. Options are also set
   mode→resolution→source (`:484-487`), letting a source change reload descriptors and clamp
   the earlier values (M-16).
6. **`get_capabilities` ignores range constraints.** Only `isinstance(constraint, list)` is
   handled (`:330-335`), so a range-reporting device prints an empty resolutions line and
   auto-profiles silently falls back to 300 (N-01). The same parsing is duplicated at `:460-466`.
7. **The fakes model a SANE API that does not exist.** Three doubles in `tests/test_scanner.py`
   (`MockSaneDev:49`, `MockSaneModule:144`, `_FakeSaneDevice:169`) raise where the real library
   stores, return `iter(list)` where the real iterator calls `start()`/`snap()` per page, and
   use "ADF" and list constraints throughout; `tests/test_pipeline.py` uses
   `MagicMock(spec=ScannerBackend)`, which accepts any `ScanSettings`. These fakes are why four
   shipped defects have green tests (M-32).

Requirements in scope: **SCNR-01 through SCNR-08**.

**Not in this phase:**
- Close-while-reading after a timeout, and the flatbed timeout / validation parity — M-12,
  M-13 → **Phase 29** (HARD-03, HARD-04). This phase edits the same functions; do not fold
  them in.
- Mid-batch page retention on error — N-02 → **Phase 29** (HARD-02).
- `sane.init()` re-entry guard and `sane.exit()` — N-04 → **Phase 29** (HARD-05).
- "No pages were scanned" / "All pages were blank" wording, and wrapping SANE exceptions at
  every boundary — N-06, M-17 → **Phase 28** (EXC-01, EXC-03).
- The `duplex` profile field, `FlipCoordinator`, `SCANNING_REVERSE` — **Phase 25** (DPLX-01..07).
  `pipeline._is_manual_duplex` (`:376-378`) and `worker.py:186-187` stay as they are.
- `PIL.Image.MAX_IMAGE_PIXELS` set in three modules — N-10 → **Phase 32** (SWP).
- `auto-profiles --force` merge semantics generally — CFG-07 → **Phase 27**. This phase adds
  only the orphan prune that its own rename makes necessary (D-16).

</domain>

<decisions>
## Implementation Decisions

All eighteen decisions below are **locked for planning**. A planner may implement them, not
relitigate them. A planner who believes one is wrong should raise it rather than silently
implement the other branch. Three diverge from the option flagged "Recommended" during
discussion — **D-01**, **D-07** and **D-14** — and each is called out where it appears.

### Source classification (SCNR-01)

- **D-01: `SourceKind.UNKNOWN` keeps today's single-page routing. C-06's "safer default" is
  DECLINED, not deferred again.** **DIVERGES from the recommendation.** The alternative —
  "if the device exposes a `source` option, treat anything that is not the flatbed entry as
  multi-page" — was floated by C-06 and explicitly held for this phase by Phase 21's D-11
  ("a real bet about unseen scanners"). The user chose to keep `uses_feeder is False` for
  UNKNOWN (`base.py:97-101`).
  - **Roadmap success criterion 1 is still met.** "Automatic Document Feeder" does not reach
    UNKNOWN — it classifies `FEEDER` on the `"document feeder"` token (`base.py:53`). The
    criterion is satisfied by the classifier Phase 21 already shipped plus D-02's remaining
    call sites, not by changing the UNKNOWN default.
  - **Record this as settled.** Phase 21 deferred it here; this phase answers it. A future
    planner or verifier should not re-open it as an unmade decision. The residual risk — a
    genuinely new feeder name returning a one-page PDF until someone adds a token — is
    accepted knowingly, and D-04's page cap bounds the opposite failure.

- **D-02: `classify_source()` becomes the only *flatbed* rule too.** `auto_profiles.py:182`
  (`has_flatbed = any("flatbed" in s.lower() ...)`) and `:193`
  (`flatbed_sources = [s for s in ... if "flatbed" in s.lower()]`) are a second
  classification rule that survived Phase 21's sweep. Both route through
  `classify_source(s) is SourceKind.FLATBED`. SCNR-01's "every feeder decision" is read to
  include the flatbed decisions that select `auto_source_mode` and the `default` profile,
  because they answer the same question from the same strings.

### Page acquisition (SCNR-02, SCNR-03)

- **D-03: the first-page special case is deleted; `StopIteration` is the only feeder-empty
  signal.** M-11's prescription, taken literally. `sane_backend.py:403-406`'s
  `if page_num == 0: raise FeederEmptyError(...)` goes; any other exception becomes a
  `ScanError` carrying the SANE text (`f"Scanner error on page {n}: {exc}"`). The
  `try/except` wrapping `dev.multi_scan()` at `:372-375` is **unreachable with the real
  library** — `multi_scan()` returns an iterator object and cannot raise — and goes with it.
  The zero-page `FeederEmptyError` at `:427-428` is the correct and only empty-feeder path.

- **D-04: an iteration guard as a module-level constant.** A `_MAX_ADF_PAGES` in
  `sane_backend.py`; exceeding it raises `ScanError` naming the cap and the page count. This
  discharges the obligation the roadmap's Phase 24 note and Phase 21's security audit
  (finding **W-01**, `.planning/phases/21-vocabulary-and-contracts/21-SECURITY.md`) both
  assign here: python-sane's `_SaneIterator.__next__` stops only on the exact string
  `"Document feeder out of documents"`, so on non-feeder hardware `start()`/`snap()` keep
  succeeding, the per-page timeout is satisfied every iteration, and `pipeline.py:577`/`:597`
  do `list(scanner.scan_pages(...))` — unbounded pages is unbounded memory.
  - Rejected: a `scanner` config knob (a permanent user-facing surface, and Phase 27's
    CFG-01 nested `extra="forbid"` work would have to know about it) and a per-profile cap
    (another `ProfileConfig` field, auto-profiles emission, and a per-pass-vs-per-job question
    that belongs to Phase 25).
  - **W-01's accept rationale must also be corrected**, per the audit's own required action:
    the register says "bounded: `multi_scan()` on a single-sheet path yields one page", which
    is false. The correct statement is "unbounded on non-feeder hardware; bounded from
    Phase 24 by `_MAX_ADF_PAGES`."

- **D-05: the backend keeps integrity checks and drops content policy.** `_validate_page_image`
  keeps the zero-dimension and minimum-raw-size checks and loses the pure-white and pure-black
  checks (`_SCANNER_WHITE_MEAN_THRESHOLD`, `_SCANNER_WHITE_STDDEV_THRESHOLD`,
  `_SCANNER_BLACK_*` at `:71-74`). Blank-page policy belongs to `_drop_empty_pages`
  (`pipeline.py:304-337`), under the profile's toggle and thresholds, where the user can see it.

- **D-06: an integrity failure skips that page; only a wholly rejected batch raises.** M-14's
  literal prescription ("Raise, or return a distinct signal, when every fed page was
  rejected"). A single corrupt page is logged at WARNING with its page number and reason and
  skipped; if every fed page failed, the backend raises rather than returning an empty list
  into `assemble_pdf([])`.
  - Rejected: raising on the first integrity failure. It would make SCNR-03's "the backend
    drops no pages on its own" true by construction and protect duplex parity structurally,
    but it fails a 50-sheet job on one bad page, and partial-result recovery is Phase 29's
    HARD-02.

- **D-07: the integrity-skip count is surfaced now, not left to a log line.**
  **DIVERGES from the recommendation**, which was to log only and let Phase 29's HARD-01
  page records carry per-page facts. The user chose visibility, on the grounds that this is
  the phase named "truthfulness". Today `pages_scanned = len(images)` (`pipeline.py:832`)
  already excludes skipped pages, so a 10-sheet stack with one corrupt page silently reports
  9 and nobody learns a page was lost.
  - **The channel is D-12's result object**, not a second mechanism. This was chosen
    deliberately so that "actual DPI" and "pages rejected" leave the scanner by one route.
  - **Do not fold the count into `pages_removed`.** Phase 23 defined that field as blank-page
    detection and Phase 30's APPL-03 renders it to users as "pages removed as blank"; a
    corrupt page reported as blank would be a new small lie in a phase about removing them.
  - **Keep the object minimal.** It must not grow into HARD-01's ordered page-record design.

- **D-08: a partial skip may break manual-duplex parity, and that is accepted and documented.**
  One integrity-failed front makes `len(front_pages) != len(back_pages)`, which
  `pipeline.py:601-602` routes to `_handle_duplex_mismatch` — two partial PDFs plus a warning
  rather than one interleaved document. This is the honest response: a corrupt page genuinely
  means the two passes no longer correspond, and Phase 23's D-08 already gave that path full
  outcome/warning/page-count parity.
  - **The verifier must not read SCNR-03's "manual-duplex page parity survives" as forbidding
    this.** What SCNR-03 removes is parity broken by *policy* — the backend silently dropping
    a clean blank back page. Parity broken by a genuinely unreadable page is a different fact
    and is reported, not hidden.
  - Rejected: raising on integrity failures only during a duplex pass. It satisfies SCNR-03
    most literally, but the backend would need to know it is mid-duplex, and `duplex` as a
    first-class concept is Phase 25's DPLX-01.

### Geometry (SCNR-04)

- **D-09: presence is checked before assignment, and the swallowed exception is logged.**
  `_set_geometry` reads the option list it can already fetch and confirms all four of
  `tl-x`, `tl-y`, `br-x`, `br-y` are present before assigning; if any is missing it logs and
  returns `False`, so the Pillow crop fallback runs. Note the naming asymmetry: SANE names the
  options with hyphens, python-sane exposes them as `dev.tl_x` / `dev.br_y`. The bare
  `except Exception` at `:122` also gains a log line — M-15's second half.

- **D-10: a `GeometryUnit` total enum with `match` + `assert_never`.** All seven SANE unit
  codes get an arm: `UNIT_MM` (3) writes millimetres directly; `UNIT_PIXEL` (1) converts using
  the resolution read back in D-11; `UNIT_NONE`, `UNIT_BIT`, `UNIT_DPI`, `UNIT_PERCENT` and
  `UNIT_MICROSECOND` log and fall through to the crop fallback.
  - **Verified during discussion, against `_sane` itself rather than the Python wrapper:** the
    complete unit set is `UNIT_NONE`=0, `UNIT_PIXEL`=1, `UNIT_BIT`=2, `UNIT_MM`=3, `UNIT_DPI`=4,
    `UNIT_PERCENT`=5, `UNIT_MICROSECOND`=6. **There is no `UNIT_CM` and no `UNIT_INCH`.** A
    backend cannot report either, so no planner should add those branches to the SANE side.
  - The enum form is the house pattern: Phase 21's D-08 *measured* that a `dict[Enum, str]`
    missing a member produces no diagnostic from either `ty` or `pyrefly`, while the same enum
    in a `match` with `assert_never` is caught by both. If SANE ever adds a unit, this fails
    the Phase 20 CI gate at edit time instead of silently mis-scaling a page.

### Resolution read-back (SCNR-05)

- **D-11: options are set source-first and the resolution is read back.** Order becomes
  `source` → `mode` → `resolution` → geometry (today `sane_backend.py:484-487` sets mode and
  resolution before source). After assignment, `actual = int(dev.resolution)`; if it differs
  from the requested value, warn naming both.

- **D-12: one small backend result object carries the actual DPI and the integrity-skip count.**
  `scan_pages` yields `Image` only, so neither fact can currently leave the backend. One
  minimal object serves both D-07 and this decision rather than inventing two channels. The
  pipeline then passes the **actual** DPI to `crop_to_paper_size` and to `assemble_pdf`
  (`pipeline.py:748` and `:789`, both currently `dpi=profile.resolution`).
  - `src/saneless/pdf.py:156` already anticipates this: *"Phase 24 adds device read-back; the
    line that changes is the `dpi` argument."* That is the seam.
  - **This matters for correctness, not tidiness.** Phase 23 made `profile.resolution`
    authoritative for `img2pdf.get_fixed_dpi_layout_fun`, so a device that substitutes DPI now
    produces both a mis-cropped page (M-16's cut-off quarter) *and* a wrong MediaBox —
    re-opening part of OUTC-06, which was Phase 23's headline fix.
  - Rejected: stamping DPI into each PIL image's `.info["dpi"]`. Phase 23 *measured*
    `crop_to_paper_size(...).info` as `{}` and found a PNG round-trip degrades 300 to
    `299.9994`, so that channel is both new and lossy — and the skip count would still have
    nowhere to go.
  - Rejected: using the actual DPI for cropping only and leaving the PDF on
    `profile.resolution`.

### Capabilities (SCNR-06)

- **D-13: a typed range field on `DeviceCapabilities`, and one `_constraint()` helper.** The
  resolution range is exposed as `(min, max, step)` as the device reported it;
  `pick_closest_resolution` (`auto_profiles.py:85-102`, which today returns the target
  unchanged when the list is empty) honours it; `devices --capabilities` (`cli.py:217`) prints
  it. One helper replaces the constraint parsing duplicated at `sane_backend.py:330-335` and
  `:460-466`, covering all three documented shapes: `None`, `(min, max, step)`, and a list.
  - Rejected: expanding common DPIs that fall inside the range into `resolutions`. Every
    existing consumer would work untouched, but the printed list would be saneless's invention
    rather than the device's answer — poor fit for a phase named truthfulness.

### Profile slugs (SCNR-01 second half / N-09)

- **D-14: every source slugs from its own name. All four friendly names are dropped.**
  **DIVERGES from the recommendation.** `source_to_slug` stops hard-mapping and slugifies the
  device's own wording for `FLATBED`, `AUTO`, `FEEDER` and `FEEDER_DUPLEX` alike. Collisions
  become impossible by construction, and the function stays pure and single-argument.
  - **Known cost, accepted knowingly:** `auto-scan` is documented at
    `docs/how-to/configure-scan-profiles.md:91` and `docs/reference/configuration.md:127`, and
    `flatbed-scan` / `adf-simplex` / `adf-duplex` all change. Those doc sentences are corrected
    in-phase.
  - The `"back"` special case at `auto_profiles.py:71-77` — the partial N-09 guard Phase 21's
    review added — becomes dead code and is removed. Its comment records the reasoning that
    this decision generalises.
  - Test churn is known and bounded: roughly eight assertions in `tests/test_auto_profiles.py`
    (`:28`, `:32`, `:36`, `:40`, `:44`, `:48`, `:52`, `:64`, `:73-78`, `:82-83`) plus slug
    references in `tests/test_cli.py` and `tests/test_worker.py`.

- **D-15: `_slugify` is hardened to a strict `[a-z0-9-]` character set.** Today it only replaces
  spaces and underscores (`auto_profiles.py:33-44`), so Canon's `"ADF (left aligned)"` emits
  `adf-(left-aligned)`, which is awkward as a `--profile` value and as a URL fragment. The rule
  drops or replaces anything outside the set, collapses hyphen runs, strips leading and trailing
  hyphens, and guards the empty result.
  - **CORRECTED 2026-09-13 by research, verified by execution.** The original wording above claimed
    `adf-(left-aligned)` is "not a legal TOML bare key". **That is false.** tomlkit quotes such a key
    automatically and `tomllib` round-trips it cleanly — measured both ways. **Do not write a test
    asserting the un-hardened slug breaks the config file**; it does not, and such a test would
    encode a false belief as a regression guard.
  - **The decision still stands, on stronger and now-measured grounds.** `_slugify` passes `/`
    straight through — `"Flachbett/Einzug"` → `flachbett/einzug`, measured — which is one careless
    reuse away from a filesystem path, and a punctuation-heavy or whitespace-only source name
    degenerates to `--` or to the empty string. The hardening is an input-validation control over a
    device-supplied string (ASVS V5/V12), not a TOML-syntax fix.
  - **This is the same weakness Phase 23's D-19 identified** when it forbade reusing `_slugify`
    for PDF filenames because it passes `/` and `..` through. The two sanitisers stay separate
    per D-19; only the character-set weakness is fixed in both places' spirit.
  - Because normalisation can itself collide (`"ADF-Front"` and `"ADF Front"`), the write path
    needs a deterministic tie-break rather than assuming injectivity.

- **D-16: orphaned auto-generated profiles are pruned on write.** `write_profiles_to_config`
  (`auto_profiles.py:231-278`) today only adds or overwrites — it never deletes — so D-14's
  rename would strand every previously generated profile. It gains a prune step that removes
  profiles carrying `auto_generated = true` which are absent from the freshly generated set.
  **Profiles without the flag are never touched.**
  - This respects CFG-07's wording literally ("replaces only the keys it generates, preserves
    user-added keys such as `default_tags`, and never touches a profile it did not create"),
    so it does not pre-empt Phase 27 — it supplies the cleanup this phase's own rename requires.
  - Measured on the user's live `saneless.toml` (untracked, sources `Auto`, `ADF`, `Flatbed`):
    `[profiles.default]` has no `auto_generated` key and is untouched; `[profiles.auto]` is
    unchanged because `Auto` already slugs to `auto`; `[profiles.adf-simplex]` and
    `[profiles.flatbed-scan]` are pruned and replaced by `adf` and `flatbed`. Without the prune
    the user would hold six profiles, two of them stale but still functional — which is the
    quiet duplication this decision prevents.
  - The prune must survive tomlkit's comment-preserving round trip and needs its own test.

### Test doubles and the real backend (SCNR-07, SCNR-08)

- **D-17: one shared fake module, faithful to python-sane 2.9.2, used by both test modules.**
  The three doubles collapse into a single `FakeSaneDev` + `FakeSaneModule` + a local error
  type mirroring `_sane.error`, living in its own `tests/` module imported by
  `tests/test_scanner.py` **and** `tests/test_pipeline.py`. One definition of "what python-sane
  does" means a fake cannot drift from real semantics in only one file — which is exactly how
  M-32 says four defects earned green tests.
  - **The three-way contract, read from the installed `sane.py` during discussion:** an unknown
    option name is stored silently (`:188`); a structurally wrong access — read-only, button,
    group, inactive, or not software-settable — raises `AttributeError` (`:192-206`, `:228-235`);
    a bad *value* for a known option raises `_sane.error`. `multi_scan()` cannot raise. The
    iterator calls `start()` and `snap()` per page and converts exactly one message,
    `"Document feeder out of documents"`, to `StopIteration` (`:107-131`).
  - The fake must also use realistic source names ("Automatic Document Feeder") and at least
    one `(min, max, step)` range constraint, so C-06- and N-01-class defects cannot pass again.
  - Rejected: importing the fake from `tests/test_scanner.py` into `tests/test_pipeline.py`
    (M-32's literal wording, but one test module importing another couples collection order),
    and giving the pipeline its own concrete `ScannerBackend` stub instead (closer to existing
    house practice — `StubScanner`, `_BrowserTestScanner` — but a SANE-level bug in a duplex
    path would then never surface in pipeline tests, which is what M-32 asks for).

- **D-18: a registered pytest marker, deselected by default, and CI runs it.** The opt-in
  integration module mirrors the existing `browser` marker exactly: registered in
  `pyproject.toml`'s `markers` list (`strict_markers = true` is live), deselected by default,
  documented in `CONTRIBUTING.md`. It points `SANE_CONFIG_DIR` at a `tmp_path` containing a
  `dll.conf` naming only `test`, and drives `SaneBackend` against `test:0`.
  - **Why `SANE_CONFIG_DIR` is required rather than incidental:** the `test` backend ships
    **commented out** in the system `/etc/sane.d/dll.conf`, so it is not loaded unless a
    frontend supplies its own config directory.
  - **CI already has what it needs — verify before adding anything.** Debian's contents search
    places `/usr/lib/x86_64-linux-gnu/sane/libsane-test.so` in **`libsane-dev`**, which
    `.github/workflows/ci.yml:26` and `:43` already install. If that holds, the only CI change
    is letting the new marker through the `-m "not browser"` filter at `:44` — no new apt
    package. **This rests on a single source** (a second lookup returned HTTP 402), so the
    planner must confirm it against a real CI run rather than trust it.
  - **Fallback, decided in advance so nobody guesses:** if the backend turns out not to be
    installed in CI, keep the marker and the module, drop the CI step, and record why — the
    test stays developer-machine-only, exactly as the DARK browser tests did until Phase 26.
  - Locally verified during discussion: python-sane **2.9.2** imports, and
    `/usr/lib64/sane/libsane-test.so` and `/etc/sane.d/test.conf` are both present.

### Added after research (2026-09-13)

- **D-19: a clamped geometry area is detected on read-back and falls through to the crop.**
  Research (its Q7) measured that writing A4's 210 mm to a device whose `br_x` range is
  `(0.0, 200.0, 1.0)` clamps to 200.0 with **no error and no exception**, so `_set_geometry` can
  return `True` having set an area that is not the one requested. This is the same
  silent-substitution shape as M-16's DPI clamping, but it is named by none of D-01..D-18, so the
  user was asked rather than it being decided unilaterally.
  - **Resolution:** after assigning the four geometry values, read them back. If the effective area
    differs materially from the requested paper size, log at WARNING naming both requested and
    actual, and return `False` so D-09's existing Pillow crop fallback runs. The crop then produces
    a correctly sized page even though the device refused the area.
  - **Why this shape:** it reuses a code path this phase is already building (D-09's `return False`
    into `_maybe_crop`), adds no new vocabulary, and does **not** grow D-12's result object — which
    research warns is already bigger than scoped, because `scan_pages` is a generator and `list()`
    discards `StopIteration.value`.
  - Rejected: carrying the actual geometry out alongside `actual_resolution` in D-12's object (more
    consistent with D-12, but grows the object that was deliberately kept minimal); and deferring it
    (no later phase owns it, so it would need a backlog entry, and SCNR-04 would be satisfied while
    the scan area could still be silently wrong — the failure shape this phase exists to remove).
  - **"Materially" needs a tolerance, not exact equality.** SANE geometry options are `TYPE_FIXED`,
    so a round-tripped value can differ in the low bits without the device having clamped anything.
    The tolerance is Claude's discretion; do not assert equality.

- **A2 CONFIRMED — historic `job.profile` needs no migration after D-14's rename.** Research flagged
  this as a LOW-confidence assumption requiring one confirming grep before "no migration" could be
  accepted, rather than inherited. Confirmed 2026-09-13: the stored slug reaches exactly three
  display-only sites — `cli.py:246` (JSON field), `cli.py:276` (truncated table column), and
  `web/templates/partials/history.html:5` — there is no retry, rerun, or resubmit path anywhere in
  the codebase, and the profile dropdown iterates live `settings.profiles` keys rather than historic
  rows. No stored slug is ever fed back into `settings.profiles[...]`, so a renamed profile simply
  leaves old jobs displaying the name that was true when they ran, which is accurate history.
  **No migration task is needed** — and a planner should not invent one.

### Claude's Discretion

Genuinely open to the planner. Make the call and record the reasoning in the plan:

- **`_MAX_ADF_PAGES`'s value** (D-04). Generous enough that no real stack reaches it. Note that
  Phase 25's two manual-duplex passes each call `scan_pages` separately, so a per-call cap is
  naturally per-pass; say so explicitly wherever it is written down.
- **Whether `_MIN_PAGE_BYTES` stays at 10 KB** (`sane_backend.py:66`) now that it is one of only
  two surviving integrity checks.
- **The name and exact shape of D-12's result object**, subject to the constraint that it stays
  minimal and does not become Phase 29's HARD-01 page-record design.
- **The marker's name** (D-18) and the precise `dll.conf` / `SANE_CONFIG_DIR` fixture shape.
- **The tie-break rule when two normalised slugs collide** (D-15).
- **Whether `DeviceCapabilities` keeps `resolutions` alongside the new range field, or derives
  one from the other** (D-13) — with the constraint that one fact must not end up with two
  independently-settable representations.
- **The `auto_source_mode` override at `sane_backend.py:494-501`.** Phase 21's D-11 named it as
  Phase 24's. It is config-driven routing rather than classification, so it may be correct
  as-is; the planner must look at it deliberately and record the finding either way.
- **Which doc sentences get rewritten.** At minimum:
  `docs/how-to/configure-scan-profiles.md:115-116` (the crop-fallback promise M-15 made
  unreachable), `docs/explanation/empty-page-detection.md:58-68` (the "keep all pages" promise
  M-14 made untrue), the generated profile names in
  `docs/how-to/configure-scan-profiles.md:91` and `docs/reference/configuration.md:127` (D-14),
  and the scanner-discovery and ADF pages the phase goal names. The standing phase rule holds:
  whatever behaviour you change, you correct its written description in the same phase.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/ROADMAP.md` § "Phase 24: Scanner Truthfulness" — the goal, the five success
  criteria, and the note carrying Phase 21's W-01 page-cap obligation into this phase.
- `.planning/ROADMAP.md` § Overview, ordering point 3 — why scanner truthfulness must land
  before manual duplex: the backend's own blank-page removal changes duplex page parity, and
  duplex tests written against untruthful fakes assert a system that will not exist after this
  phase.
- `.planning/ROADMAP.md` § "Phase 25", § "Phase 28", § "Phase 29", § "Phase 32" — the phases
  that own the adjacent findings this phase must NOT fold in (see Phase Boundary).
- `.planning/REQUIREMENTS.md` lines 64-71 — SCNR-01 through SCNR-08 verbatim with their
  review-finding tags.

### The findings this phase resolves
- `.planning/reviews/2026-09-09-code-review.md` § C-06 (~line 270) — the single-classifier
  finding, the confirmed real-world feeder names, and the experiment against the SANE `test`
  backend in which a ten-page feeder yielded one page. **Its "safer default" is DECLINED by
  D-01 — do not implement it.**
- `.planning/reviews/2026-09-09-code-review.md` § M-11 (~line 499) — why the `page_num == 0`
  branch is wrong and why the guard around `multi_scan()` is unreachable.
- `.planning/reviews/2026-09-09-code-review.md` § M-14 (~line 541) — the three consequences of
  scanner-level blank dropping, and the "validate integrity at the bottom, decide content
  policy at the top" principle behind D-05/D-06.
- `.planning/reviews/2026-09-09-code-review.md` § M-15 (~line 551) — the real
  `SaneDev.__setattr__` behaviour that makes the crop fallback unreachable.
- `.planning/reviews/2026-09-09-code-review.md` § M-16 (~line 561) — device resolution
  substitution, and the option-ordering trap behind D-11.
- `.planning/reviews/2026-09-09-code-review.md` § M-32 (~line 802) — the fake-versus-real
  difference table and the `SANE_CONFIG_DIR` recipe behind D-17/D-18.
- `.planning/reviews/2026-09-09-code-review.md` § N-01, N-03, N-09 (~lines 837, 841, 855) —
  range constraints, geometry units, and slug collisions.
- **M-12, M-13, N-02, N-04 (~lines 509, 531, 839, 843) are Phase 29's** even though they sit in
  the same review section and touch the same functions. Read them to know what NOT to build.

### Prior-phase decisions that bind this phase
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` — **D-03** (`SourceKind` and
  `classify_source()` live in `scanner/base.py`; the scanner package must not depend on the job
  vocabulary), **D-08** (the measured `match`/`assert_never` vs `dict` enforcement finding that
  D-10 rests on), **D-09** (parametrise over the real thing, never a hand-written list),
  **D-11 and D-11 AMENDED** (the classifier's branch order, why `"duplex"` is tested before the
  feeder tokens, and the explicit list of what Phase 24 still owns).
- `.planning/phases/21-vocabulary-and-contracts/21-SECURITY.md` § **W-01** — the unbounded
  `_scan_adf_pages` finding, its evidence, and the required correction to the accept rationale
  that D-04 discharges.
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md` — **D-08** (the
  duplex-mismatch path has full outcome/warning/page-count parity, which is what makes D-08 here
  acceptable), **D-19** (`_slugify` passes `/` and `..` through; filename and slug sanitisers
  stay separate), and the Deferred Ideas entry that names read-back DPI and real SANE error
  messages as this phase's.
- `.planning/phases/22-job-store-hardening/22-CONTEXT.md` — **D-08** (`NULL` page counts mean
  "never recorded", not a measured zero — relevant to how D-07's skip count is persisted).
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Hang guard" — `timeout = 60`,
  `filterwarnings = ["error"]`, `--strict-config` and `--strict-markers` are live. D-18's new
  marker must be registered or collection fails, and the integration module must finish well
  inside the timeout.
- `.planning/phases/23.1-dark-mode-and-the-commit-gate/23.1-CONTEXT.md` — **D-01/D-02** (type
  checks are `src`-only at commit and full at pre-merge-commit and pre-push, so a TDD RED
  commit needs no suppression), **D-10** (always run `uv run pyrefly check src tests` — never
  the bare form).

### Source files this phase changes
- `src/saneless/scanner/sane_backend.py` — `_set_geometry` (`:95-133`), `_maybe_crop`
  (`:136-156`), `_validate_page_image` (`:159-212`), the `SaneDevice` Protocol (`:215-231`),
  `get_capabilities` (`:303-342`), `_scan_adf_pages` (`:344-428`), `scan_pages` (`:430-515`),
  and the threshold constants at `:62-74`.
- `src/saneless/scanner/base.py` — `DeviceCapabilities` (`:114-121`) gains the range field;
  `classify_source` (`:56-101`) is unchanged in behaviour, but its D-01 comment at `:97-100`
  ("belongs to Phase 24, not here") must be updated to record the answer.
- `src/saneless/auto_profiles.py` — `_slugify` (`:33-44`), `source_to_slug` (`:47-82`),
  `generate_profiles` (`:172-202`, including `:182` and `:193`),
  `pick_closest_resolution` (`:85-102`), `write_profiles_to_config` (`:231-278`).
- `src/saneless/pipeline.py` — `_drop_empty_pages` (`:304-337`) is where blank policy now lives
  exclusively; the `assemble_pdf` call sites at `:748` and `:789` take the actual DPI; the
  `list(scanner.scan_pages(...))` calls at `:577`, `:597` and in `_scan_simplex` are where
  D-12's result object arrives.
- `src/saneless/pdf.py:126-195` — `assemble_pdf`'s `dpi` argument and the docstring at `:147-162`
  that already names this phase.
- `src/saneless/paper_sizes.py:28-32` — `crop_to_paper_size(image, paper_size, dpi)`.
- `src/saneless/cli.py:211-221` — `devices --capabilities` rendering; `:320-344` — the
  `auto-profiles --force` path.
- `tests/test_scanner.py` (44 KB), `tests/test_pipeline.py` (65 KB), `tests/test_auto_profiles.py`
  (14 KB), `tests/test_cli.py`, `tests/test_worker.py`.
- `pyproject.toml` — the `markers` list; `.github/workflows/ci.yml:44` — the `-m "not browser"`
  filter.

### Docs whose claims this phase changes
- `docs/how-to/configure-scan-profiles.md:115-116` — "When your scanner supports SANE geometry
  options… Otherwise, it crops the image after scanning." Currently unreachable for the case it
  describes (M-15). Also `:91`'s `[profiles.auto-scan]` (D-14).
- `docs/explanation/empty-page-detection.md:58-68` — "If you want to keep all scanned pages
  regardless of content, disable detection… useful when scanning documents where blank pages are
  intentional." Untrue for clean blanks today (M-14).
- `docs/reference/configuration.md:127` — `[profiles.auto-scan]` (D-14).
- `docs/how-to/set-up-adf-duplex.md` and the scanner-discovery how-to — the ADF and discovery
  pages the phase goal names.
- `CONTRIBUTING.md` — the new marker and how to run the integration module (D-18).

### External
- python-sane 2.9.2, installed at
  `.venv/lib/python3.14/site-packages/sane.py` — **the authoritative source for D-17's fakes.**
  `_SaneIterator` (`:107-134`), `SaneDev.__setattr__` (`:188-213`), `SaneDev.__getattr__`
  (`:215-235`), `multi_scan` (`:348`), and the `UNIT_STR` / `TYPE_STR` maps (`:13-23`).
- `sane-test(5)` — <https://manpages.ubuntu.com/manpages/jammy/man5/sane-test.5.html>. Documents
  `/etc/sane.d/test.conf`, the `number_of_devices` option, the `read-delay` option the timeout
  path would use, and **that the backend ships commented out in `dll.conf`** — the reason
  `SANE_CONFIG_DIR` is required.
- `sane-dll(5)` — the dynamic backend loader and `SANE_CONFIG_DIR` semantics.
- SANE standard, cancellation: a frontend must not call another operation until a cancelled
  operation has returned. Background for the M-12 boundary — **Phase 29's, not this phase's.**

</canonical_refs>

<code_context>
## Existing Code Insights

### Verified facts (measured during discussion, 2026-09-12)

- python-sane **2.9.2** imports cleanly in this environment.
- The SANE `test` backend is installed locally: `/usr/lib64/sane/libsane-test.so` (→
  `.so.1.0.32`) and `/etc/sane.d/test.conf`.
- `_sane` exposes exactly seven unit constants — `UNIT_NONE`=0, `UNIT_PIXEL`=1, `UNIT_BIT`=2,
  `UNIT_MM`=3, `UNIT_DPI`=4, `UNIT_PERCENT`=5, `UNIT_MICROSECOND`=6. **No CM, no INCH.**
- `write_profiles_to_config` never deletes (`auto_profiles.py:274-278`) — the whole basis of D-16.
- The user's live `saneless.toml` is untracked and holds four profiles; three carry
  `auto_generated = true` and `[profiles.default]` does not.
- `.github/workflows/ci.yml` installs `libsane-dev` in **both** jobs (`:26`, `:43`) and runs
  `pytest -m "not browser"` (`:44`).
- `pytest` config: `markers = ["browser: …"]`, `strict_markers`, `strict_config`,
  `xfail_strict`, `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"`.

### Reusable Assets

- **`classify_source()` and `SourceKind` (`scanner/base.py:31-101`)** already exist, already
  carry `uses_feeder` behind `match`/`assert_never`, and already document why the branch order
  is load-bearing. This phase wires the remaining call sites; it does not rewrite the rule.
- **The `browser` marker** is the exact precedent D-18 follows — a registered, default-deselected
  marker with a CI filter and a CONTRIBUTING note.
- **`_constraint()`-shaped parsing already exists twice** (`sane_backend.py:330-335`, `:460-466`);
  D-13 is as much a dedup as a feature.
- **`ScanResult` (`pipeline.py:138-145`)** is the precedent for D-12's object: a small frozen
  dataclass of facts a stage computed, with a `warning` field for the thing that needs saying.
- **`PipelineEvent.job_state` and `vocabulary.py`'s total lookups** are the pattern D-10's
  `GeometryUnit` follows.

### Established Patterns

- **Total lookups are functions with `match` + `assert_never`, never dicts** — Phase 21's D-08
  measured this against this project's own `ty` and `pyrefly`.
- **Parametrise over `list(EnumType)`, never a hand-written list of names** (Phase 21 D-09), so a
  new member fails the suite by itself.
- **Fakes are concrete classes, not `MagicMock`** — STATE.md records `StubScanner` and
  `_BrowserTestScanner` as deliberate choices. D-17 extends the same instinct to the SANE layer.
- **TDD is enabled** (`workflow.tdd_mode: true`), and since Phase 23.1 a RED commit lands with
  hooks enabled and **no suppression of any kind** — no `--no-verify`, no `SKIP=`, no
  `# type: ignore`, no `# noqa`, no stub that makes the symbol exist. The RED test file must
  still be ruff-clean.
- **Ruff `D` rules are on**; every public module, class and function needs a docstring.
- **Always `uv run pyrefly check src tests`** — never the bare form (23.1 D-10).

### Integration Points

- `sane_backend.scan_pages` is where D-01, D-03, D-09, D-10, D-11 and D-12 all land, and it is
  already at ruff's complexity limits — expect helper extraction, as Phase 18 did with
  `_set_geometry`/`_maybe_crop`.
- `pipeline._drop_empty_pages` becomes the *only* place blank policy is decided.
- `pipeline.py:748` and `:789` are the two `dpi=profile.resolution` call sites that become the
  actual DPI.
- `auto_profiles.generate_profiles` and `write_profiles_to_config` carry D-02, D-14, D-15 and D-16.
- `cli.py:211-221` renders whatever D-13 exposes.

### Constraints the architecture imposes

- **`scanner/` must not depend on the job vocabulary** (Phase 21 D-03). `GeometryUnit` and the
  range type belong in `scanner/`, not `vocabulary.py`.
- **`sane` is imported lazily** via `_ensure_sane()` (`sane_backend.py:46-52`) and monkeypatched
  as `sane_backend.sane` in tests — D-17's fake module must preserve that seam.
- **No new runtime dependencies** (REQUIREMENTS.md § Out of Scope).

</code_context>

<specifics>
## Specific Ideas

- The user chose the **conservative** branch on unseen scanners (D-01) and the **aggressive**
  branch on naming (D-14) in the same session. That is not inconsistent: routing a stack down
  the wrong path silently loses pages, while a profile name that changes is visible and
  recoverable. Read it as a preference for failures that announce themselves.
- **"Surface the count now" (D-07) was chosen against a recommendation** that cited the Phase 29
  overlap. The reasoning that decided it: this is the phase named truthfulness, and a scan that
  reports nine pages from a ten-sheet stack is exactly the class of quiet lie it exists to
  remove. The mitigation is to keep the object minimal, not to withhold the fact.
- **The cm/inch request was checked rather than implemented.** The user asked for cm and inches
  alongside mm and pixels; SANE cannot report either. Rather than adding dead branches, the
  user confirmed the user-facing half should become **its own phase**, and the SANE side gets a
  total enum that proves the branch set is complete.
- The standing verification-rigor rule applied twice: the SANE unit set was read from `_sane`
  rather than the Python wrapper's display dict, and the CI package claim is recorded as
  **single-sourced and needing confirmation** rather than stated as fact.

</specifics>

<deferred>
## Deferred Ideas

- **User-facing scan-area units in cm and inches** — a profile could let the user express a
  custom scan area in cm or inches, converted to mm before reaching SANE. Raised during the
  geometry discussion; the user explicitly agreed it is a **new capability deserving its own
  phase**, not a truthfulness fix. Nothing in Phase 24 should implement it.

Boundary clarifications made while deciding — work this phase deliberately does **not** do, so
a planner does not pull it forward and a verifier does not fail the phase for its absence:

- **C-06's "safer default" for unrecognised sources** — **declined outright** by D-01, not
  deferred. Recorded so it is not re-raised as an open question a third time.
- **Waiting for the cancelled read before `close()`, and the flatbed timeout and validation
  parity** — Phase 29 (M-12/HARD-03, M-13/HARD-04).
- **Keeping the N pages already acquired when a mid-batch error occurs** — Phase 29
  (N-02/HARD-02). This is what would soften D-06's all-or-nothing edge.
- **A process-level `sane.init()` guard and `sane.exit()` at shutdown** — Phase 29 (N-04/HARD-05).
- **Wrapping SANE exceptions at every module boundary, and the "No pages were scanned" /
  "All pages were blank" wording** — Phase 28 (M-17/EXC-01, N-06/EXC-03). D-03 makes the
  messages truthful; Phase 28 makes them well-typed.
- **The `duplex` profile field and one place deciding manual duplex** — Phase 25 (DPLX-01,
  DPLX-03). `pipeline._is_manual_duplex` and the duplicated rule in `worker.py` stay.
- **`auto-profiles --force` merge semantics in general** — Phase 27 (CFG-07). Only the orphan
  prune D-14 makes necessary lands here.
- **Setting `PIL.Image.MAX_IMAGE_PIXELS` in one place** — Phase 32 (N-10/SWP).
- **Plain-language error display built on truthful messages** — Phase 30 (U-05/APPL-04), which
  Phase 21's D-12 explains depends on this phase landing first.

</deferred>

---

*Phase: 24-scanner-truthfulness*
*Context gathered: 2026-09-12*
