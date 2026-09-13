# Phase 24: Scanner Truthfulness - Research

**Researched:** 2026-09-12
**Domain:** SANE scanner backend semantics (python-sane 2.9.2), test-double fidelity, profile slug generation
**Confidence:** HIGH — almost every claim below was executed against the real SANE `test` backend and the installed `sane.py` on this machine, not recalled.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

`.planning/phases/24-scanner-truthfulness/24-CONTEXT.md` is **authoritative**. Its 18 decisions are
locked for planning. The decision statements are reproduced verbatim below; the full rationale,
rejected alternatives and evidence for each live in CONTEXT.md and **must be read before planning**.

### Locked Decisions

**Source classification (SCNR-01)**

- **D-01: `SourceKind.UNKNOWN` keeps today's single-page routing. C-06's "safer default" is
  DECLINED, not deferred again.** *(DIVERGES from the recommendation.)* Roadmap success criterion 1
  is still met — "Automatic Document Feeder" classifies `FEEDER` on the `"document feeder"` token.
  Record this as settled; do not re-open it.
- **D-02: `classify_source()` becomes the only *flatbed* rule too.** `auto_profiles.py:182` and
  `:193` route through `classify_source(s) is SourceKind.FLATBED`.

**Page acquisition (SCNR-02, SCNR-03)**

- **D-03: the first-page special case is deleted; `StopIteration` is the only feeder-empty signal.**
  Any other exception becomes a `ScanError` carrying the SANE text
  (`f"Scanner error on page {n}: {exc}"`). The unreachable `try/except` around `dev.multi_scan()`
  goes with it. The zero-page `FeederEmptyError` is the correct and only empty-feeder path.
- **D-04: an iteration guard as a module-level constant.** `_MAX_ADF_PAGES` in `sane_backend.py`;
  exceeding it raises `ScanError` naming the cap and the page count. **W-01's accept rationale must
  also be corrected**: the correct statement is "unbounded on non-feeder hardware; bounded from
  Phase 24 by `_MAX_ADF_PAGES`."
- **D-05: the backend keeps integrity checks and drops content policy.** `_validate_page_image`
  keeps zero-dimension and minimum-raw-size; loses the pure-white and pure-black checks.
- **D-06: an integrity failure skips that page; only a wholly rejected batch raises.**
- **D-07: the integrity-skip count is surfaced now, not left to a log line.** *(DIVERGES from the
  recommendation.)* **The channel is D-12's result object**, not a second mechanism. **Do not fold
  the count into `pages_removed`.** **Keep the object minimal.**
- **D-08: a partial skip may break manual-duplex parity, and that is accepted and documented.**
  The verifier must not read SCNR-03's "manual-duplex page parity survives" as forbidding this.

**Geometry (SCNR-04)**

- **D-09: presence is checked before assignment, and the swallowed exception is logged.** Note the
  naming asymmetry: SANE names the options with hyphens, python-sane exposes them as `dev.tl_x`.
- **D-10: a `GeometryUnit` total enum with `match` + `assert_never`.** All seven SANE unit codes get
  an arm: `UNIT_MM` writes mm directly; `UNIT_PIXEL` converts using the D-11 read-back resolution;
  the other five log and fall through to the crop fallback. **There is no `UNIT_CM` and no
  `UNIT_INCH`** — no planner should add those branches to the SANE side.

**Resolution read-back (SCNR-05)**

- **D-11: options are set source-first and the resolution is read back.** Order becomes
  `source` → `mode` → `resolution` → geometry. After assignment, `actual = int(dev.resolution)`;
  warn if it differs, naming both.
- **D-12: one small backend result object carries the actual DPI and the integrity-skip count.**
  The pipeline passes the **actual** DPI to `crop_to_paper_size` and `assemble_pdf`
  (`pipeline.py:748`, `:789`). Rejected: stamping DPI into `.info["dpi"]`; using actual DPI for
  cropping only.

**Capabilities (SCNR-06)**

- **D-13: a typed range field on `DeviceCapabilities`, and one `_constraint()` helper.** The range is
  exposed as `(min, max, step)` as the device reported it; `pick_closest_resolution` honours it;
  `devices --capabilities` prints it. One helper replaces the parsing duplicated at
  `sane_backend.py:330-335` and `:460-466`, covering `None`, `(min, max, step)` and a list.
  Rejected: expanding common DPIs inside the range into `resolutions`.

**Profile slugs (SCNR-01 second half / N-09)**

- **D-14: every source slugs from its own name. All four friendly names are dropped.** *(DIVERGES
  from the recommendation.)* Collisions become impossible by construction. Known cost accepted: the
  documented names change and are corrected in-phase. The `"back"` special case becomes dead code
  and is removed.
- **D-15: `_slugify` is hardened to a strict `[a-z0-9-]` character set.** Drops or replaces anything
  outside the set, collapses hyphen runs, strips leading/trailing hyphens, guards the empty result.
  Because normalisation can itself collide, the write path needs a deterministic tie-break.
- **D-16: orphaned auto-generated profiles are pruned on write.** Removes profiles carrying
  `auto_generated = true` absent from the freshly generated set. **Profiles without the flag are
  never touched.** Must survive tomlkit's comment-preserving round trip and needs its own test.

**Test doubles and the real backend (SCNR-07, SCNR-08)**

- **D-17: one shared fake module, faithful to python-sane 2.9.2, used by both test modules.**
  `FakeSaneDev` + `FakeSaneModule` + a local error type mirroring `_sane.error`, in its own `tests/`
  module imported by `tests/test_scanner.py` **and** `tests/test_pipeline.py`. The fake must use
  realistic source names and at least one `(min, max, step)` range constraint.
- **D-18: a registered pytest marker, deselected by default, and CI runs it.** Mirrors the `browser`
  marker exactly. Points `SANE_CONFIG_DIR` at a `tmp_path` containing a `dll.conf` naming only
  `test`, and drives `SaneBackend` against `test:0`. **CI already has what it needs — verify before
  adding anything.** Fallback decided in advance: if the backend is not installed in CI, keep the
  marker and module, drop the CI step, record why.

### Claude's Discretion

Genuinely open to the planner (each is answered with a recommendation in **Open Questions** below):

- `_MAX_ADF_PAGES`'s value (D-04) — note a per-call cap is naturally per-pass; say so where written.
- Whether `_MIN_PAGE_BYTES` stays at 10 KB.
- The name and exact shape of D-12's result object (minimal; not HARD-01's page records).
- The marker's name (D-18) and the precise `dll.conf` / `SANE_CONFIG_DIR` fixture shape.
- The tie-break rule when two normalised slugs collide (D-15).
- Whether `DeviceCapabilities` keeps `resolutions` alongside the new range field (D-13) — one fact
  must not end up with two independently-settable representations.
- The `auto_source_mode` override at `sane_backend.py:494-501` — look at it deliberately and record
  the finding either way.
- Which doc sentences get rewritten (minimum list in CONTEXT.md).

### Deferred Ideas (OUT OF SCOPE)

- **User-facing scan-area units in cm and inches** — its own phase. Nothing here implements it.
- **C-06's "safer default" for unrecognised sources** — declined outright by D-01, not deferred.
- Close-while-reading after timeout; flatbed timeout and validation parity — **Phase 29** (M-12/HARD-03, M-13/HARD-04).
- Mid-batch page retention on error — **Phase 29** (N-02/HARD-02).
- `sane.init()` re-entry guard and `sane.exit()` — **Phase 29** (N-04/HARD-05).
- Exception wrapping at boundaries; "No pages were scanned" wording — **Phase 28** (M-17/EXC-01, N-06/EXC-03).
- The `duplex` profile field, `FlipCoordinator`, `SCANNING_REVERSE` — **Phase 25** (DPLX-01..07).
  `pipeline._is_manual_duplex` (`:376-378`) and `worker.py:186-187`/`:193-194` **stay as they are**.
- `PIL.Image.MAX_IMAGE_PIXELS` in one place — **Phase 32** (N-10/SWP).
- `auto-profiles --force` merge semantics generally — **Phase 27** (CFG-07). Only D-16's orphan prune lands here.
- Plain-language error display — **Phase 30** (U-05/APPL-04).
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description (from REQUIREMENTS.md:64-71) | Research Support |
|----|------------------------------------------|------------------|
| **SCNR-01** | `sane_backend`, `auto_profiles`, the pipeline and the worker all route feeder decisions through `classify_source()`; "Automatic Document Feeder" scans a full stack; auto-profiles never collapses two distinct feeder sources into one slug [C-06, N-09] | § Finding 5 (complete inventory of surviving ad-hoc rules — including one D-02 does not name), § Finding 6 (the `== "Auto"` hidden classifier), § D-14/D-15 slug measurements |
| **SCNR-02** | A first-page SANE error other than the exact "Document feeder out of documents" message is raised as a `ScanError` carrying the SANE message, never "No paper detected" [M-11] | § Finding 3 — the four real `_sane.error` message strings measured against the test backend, and the exact `read_return_value` recipe to reproduce each |
| **SCNR-03** | The backend never drops blank pages; empty-page detection happens only in the pipeline, only when the profile enables it; manual-duplex page parity preserved [M-14] | § Finding 2 — **executed proof**: all ten real test-backend pages are dropped today; per-picture mean/stddev table |
| **SCNR-04** | Geometry set only when the device reports `tl_x`/`tl_y`/`br_x`/`br_y`; otherwise the Pillow crop fallback runs and is proven reachable; units read from the descriptor [M-15, N-03] | § Finding 7 (the real `__setattr__` contract), § Finding 8 (measured unit code and the silent geometry clamp) |
| **SCNR-05** | Crop and page-size maths use the resolution read back after all options are set; options set source-first [M-16] | § Finding 9 — measured clamping (`5000 → 1200.0`, `0 → 1.0`) and the `float`-not-`int` return type |
| **SCNR-06** | `get_capabilities` honours range constraints (min/max/step) and reports them in `devices --capabilities` [N-01] | § Finding 4 — reproduced live: `resolutions=[]` from a device whose constraint is `(1.0, 1200.0, 1.0)` |
| **SCNR-07** | Test doubles mirror python-sane 2.9.2: unknown options stored silently, bad value raises `_sane.error`, structurally wrong access raises `AttributeError`, `multi_scan()` cannot raise [M-32] | § The python-sane Contract — all four behaviours executed, plus a **fifth** CONTEXT.md does not name |
| **SCNR-08** | An opt-in integration module drives the real `test` backend through a `SANE_CONFIG_DIR` scoped to `tmp_path` and proves ten pages through a long feeder name [M-32] | § Finding 1 — **the fixture hazard**, three scenarios measured, and a validated working fixture shape |
</phase_requirements>

---

## Project Constraints (from CLAUDE.md)

The planner must verify every plan against these. They are as binding as the locked decisions.

| Constraint | Consequence for this phase |
|---|---|
| Python 3.14; `uv` only (not pip/poetry/conda) | All commands are `uv run ...`. No new runtime dependency is needed or permitted (REQUIREMENTS § Out of Scope). |
| `uv run ruff check .` + `ruff format .` clean | `_scan_adf_pages` is **at** ruff's branch limit today (see § Complexity Budget) — helper extraction is forced, not optional. |
| `uv run ty check` **and** `uv run pyrefly check src tests` both clean | Two checkers disagree; both must pass. `DeviceCapabilities`/`SaneDevice` type changes must satisfy both. |
| **No suppressions**: no `# type: ignore`, no `# noqa`, no rule disabling, no `--no-verify`, no `SKIP=` | The `SaneDevice` Protocol currently lies about `resolution: int` (§ Finding 9). Fixing it must be done properly, not annotated away. |
| Ruff `D` rules on | Every new public module/class/function — the shared fake module, `GeometryUnit`, D-12's result object — needs a docstring. RED test files must be ruff-clean too. |
| Always `uv run pyrefly check src tests` — never the bare form | Phase 23.1 D-10. Applies to every verification command written into a plan. |
| Prefer external packages over reimplementing | Nothing to add here — see § Don't Hand-Roll; the correct move in this phase is to *delete* hand-rolled logic, not add a library. |
| `prek run --all-files` (not `pre-commit`) | `--all-files` is load-bearing; `prek run` alone silently skips almost everything on a clean tree. |
| Playwright MCP for all browser validation; never "manual-only" | **No browser surface in this phase.** The only user-visible change is CLI text (`devices --capabilities`). No `browser`-marked work. |

---

## Summary

This phase is unusual in that nearly nothing needs to be invented — the correct behaviour is already
specified by python-sane's own source and by SANE's option model, and the current code simply
disagrees with both. Accordingly, research here was **verification, not discovery**: the real
`test` backend was driven live through every path this phase touches, and every one of the seven
defects CONTEXT.md lists was reproduced. Three of them produce executable evidence a plan can turn
straight into a failing test.

Two results change how the phase should be planned. **First**, the D-18 integration fixture has a
hazard nobody had checked: `SANE_CONFIG_DIR` is only honoured if it is exported **before the first
`sane.init()` in the process**, and `sane.exit()` + re-init does *not* reset it. A naive
function-scoped `monkeypatch.setenv` fixture fails — measured. A session-scoped fixture works, and
was validated running in the same process as the existing 74-test `tests/test_scanner.py`.
**Second**, the SANE `test` backend's default picture is *solid black*, so today's
`_validate_page_image` discards **all ten** pages of the ten-page ADF stack and `scan_pages` returns
an empty list. That is SCNR-03's defect demonstrated end-to-end, and it means SCNR-08's headline
test **cannot pass until D-05 lands** — a hard ordering constraint between two plans.

The largest genuine design question is D-12's result object, and it is bigger than it looks:
`scan_pages` is a **generator**, and `list(generator)` discards a generator's return value (measured).
So there is no way to carry the actual DPI and the skip count out of the current signature at all —
D-12 necessarily changes the `ScannerBackend` ABC and every one of the ~10 fakes and stubs that
implement it. A recommendation and a rejected-alternatives analysis are in § Open Questions Q3.

Finally, one locked decision rests on a claim that is **half wrong** and the planner should know
before writing the test: D-15 says `adf-(left-aligned)` is "not a legal TOML bare key". Literally
true, but the implied consequence is false — tomlkit quotes it automatically and `tomllib`
round-trips it cleanly (measured). D-15 remains correct on its other, stronger grounds (`/` passes
straight through; whitespace-only names slug to `--`), which are also now measured. Do not write a
test asserting that the un-hardened slug *breaks* the config file; it does not.

**Primary recommendation:** Plan this as four sequential concerns — (1) classification + slugs +
prune, (2) page acquisition + the D-12 signature change, (3) geometry + read-back + capabilities,
(4) fakes + the real-backend integration module — and land **D-05 before SCNR-08's integration
test**, because that test provably cannot pass in the other order.

---

## Architectural Responsibility Map

This is a backend/library phase; the tiers below are *layers within the application*, since there is
no browser or CDN surface involved.

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Source classification (`classify_source`) | `scanner/base.py` (device vocabulary) | — | Phase 21 D-03: the scanner package owns it and must not depend on the job vocabulary. |
| Feeder-vs-single-page **routing** | `scanner/sane_backend.py` | — | It is the only layer that knows what `multi_scan()` vs `snap()` means. |
| Page **integrity** (zero dims, truncated buffer) | `scanner/sane_backend.py` | — | M-14's principle: validate integrity at the bottom. |
| Page **content policy** (blank removal) | `pipeline._drop_empty_pages` | — | D-05: policy the user can see and toggle belongs at the top. Moving *up* is the whole of SCNR-03. |
| Geometry unit conversion (`GeometryUnit`) | `scanner/` | — | SANE-specific; must not land in `vocabulary.py` (Phase 21 D-03). |
| Resolution read-back | `scanner/sane_backend.py` | `pipeline` (consumes actual DPI) | The device is the source of truth; the pipeline is the consumer that must stop assuming. |
| DPI → PDF MediaBox / crop maths | `pipeline` → `pdf.assemble_pdf`, `paper_sizes.crop_to_paper_size` | — | `pdf.py:156` already names this seam: "the line that changes is the `dpi` argument". |
| Profile slug generation | `auto_profiles.py` | — | Naming is a config concern, not a routing concern (the distinction D-14 collapses on purpose). |
| Orphan profile prune | `auto_profiles.write_profiles_to_config` | — | The only place that writes config; tomlkit round-trip lives here. |
| Capability constraint parsing | `scanner/sane_backend._constraint()` | `auto_profiles`, `cli` (consumers) | D-13 is as much a dedup as a feature. |

**Tier check the planner should apply:** anything in this phase that reaches for a *profile* field
inside `scanner/` is misassigned, and anything in `pipeline` that re-derives a fact from a SANE
string is misassigned. Both mistakes exist in the code today.

---

## Standard Stack

### Core

No new libraries. Everything needed is installed and already a declared dependency.

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `python-sane` | **2.9.2** `[VERIFIED: sane.__version__ read from the installed interpreter]` | The SANE binding this phase must model faithfully | Already a project dependency (`pyproject.toml`: `python-sane>=2.9.1`). Its source at `.venv/lib/python3.14/site-packages/sane.py` is the **authoritative** spec for D-17. |
| `_sane` (C extension) | ships with python-sane 2.9.2 `[VERIFIED: imported, `_sane.error` inspected]` | Unit/type/cap constants and the real error type | `_sane.error` subclasses `Exception` directly (`(error, Exception, BaseException, object)`). Read constants from `_sane`, never from `sane.UNIT_STR` (a display dict). |
| `tomlkit` | `>=0.14.0` (installed) | D-16's comment-preserving prune | Already used by `write_profiles_to_config`. Deletion preserves comments — measured, § Finding 11. |
| `pytest` | 9.0.2 `[VERIFIED: pyproject dev group + live run]` | D-18's marker, the whole suite | `strict_markers`, `strict_config`, `xfail_strict`, `filterwarnings=["error"]`, `timeout=60` are all live. |
| `Pillow` | `>=12.1.1` | `ImageStat` for the surviving integrity checks | Already used. |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| SANE `test` backend | `libsane-test.so.1.0.32` at `/usr/lib64/sane/` `[VERIFIED: local filesystem]` | SCNR-08's real-backend target | Only under the D-18 marker. Configured by `/etc/sane.d/test.conf`; ships **commented out** in the system `dll.conf` (`/etc/sane.d/dll.conf:95` reads `#test` — `[VERIFIED: grep]`). |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Driving the real `test` backend | Only the D-17 fakes | Rejected by SCNR-08 and by M-32's core lesson: a fake that is more convenient than the truth verifies the code against the fake. The measured proof is § Finding 2 — a defect the fakes hid for four releases surfaced on the *first* real run. |
| Reading constants from `_sane` | `sane.UNIT_STR` / `TYPE_STR` | Rejected: those are display dicts for `Option.__repr__`. CONTEXT's standing verification-rigor rule already required reading `_sane`; confirmed correct. |
| A new "scan result" dependency | A stdlib frozen dataclass | No library needed; `pipeline.ScanResult` is the in-house precedent. |

**Installation:** none. This phase adds **zero** packages.

**Version verification performed:**
```bash
uv run python -c "import sane, _sane; print(sane.__version__)"   # -> 2.9.2
```

---

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.**

The Package Legitimacy Gate was considered and correctly skipped: REQUIREMENTS.md § Out of Scope
forbids new runtime dependencies, and every library this phase touches (`python-sane`, `tomlkit`,
`Pillow`, `pytest`) is already pinned in `uv.lock` and already exercised by 742 passing tests. No
`npm`/`pip`/`cargo` install step appears anywhere in the recommended work, so there is no
slopsquatting surface to audit.

**The one system-package claim in this phase is audited instead, because it is the phase's only
external-supply-chain assumption:**

| Claim | Source 1 | Source 2 | Verdict |
|---|---|---|---|
| CI's `libsane-dev` provides the SANE `test` backend | `packages.ubuntu.com/noble/amd64/libsane-dev/filelist` lists `/usr/lib/x86_64-linux-gnu/sane/libsane-test.so` and `libsane-test.a` | `packages.ubuntu.com/noble/amd64/libsane1/filelist` lists `libsane-test.so.1` and `libsane-test.so.1.2.1`; `libsane-dev` **Depends: libsane1 (= 1.2.1-7build4)** | **CONFIRMED** — see § Finding 12 |

CONTEXT.md flagged this as single-sourced (a second lookup had returned HTTP 402) and asked for
independent verification. It is now **two-sourced**, and the mechanism is understood rather than
assumed: the versioned `.so.1` that `sane-dll` actually dlopens ships in `libsane1`, which
`libsane-dev` hard-depends on at an exact version. `.github/workflows/ci.yml:26` and `:43` already
install `libsane-dev` in **both** jobs. D-18's documented fallback should not be needed.

---

## Architecture Patterns

### System Architecture Diagram

Data flow for one scan, showing where each decision is made **after** this phase. Bold markers show
the decisions that move or appear.

```
                        ┌───────────────────────────────┐
   SANE device ───────► │ dev.get_options()             │
   (test:0, hpaio, …)   │  → _constraint(name)  **D-13**│
                        └──────────────┬────────────────┘
                                       │ sources, modes,
                                       │ resolution range
                        ┌──────────────▼────────────────┐
                        │ classify_source(name)         │  ◄── the ONLY rule **D-01/D-02**
                        │   → SourceKind                │      (base.py, no job vocabulary)
                        └──────┬─────────────────┬──────┘
                               │                 │
                  uses_feeder  │                 │  not a feeder
                               │                 │
         ┌─────────────────────▼──────┐   ┌──────▼───────────────┐
         │ set options SOURCE-FIRST   │   │ dev.start(); snap()  │
         │  source→mode→resolution    │   └──────┬───────────────┘
         │  actual = int(dev.res)     │          │
         │  warn if ≠ requested **D-11**│        │
         └─────────────┬──────────────┘          │
                       │                          │
         ┌─────────────▼───────────────┐          │
         │ geometry: all four of       │          │
         │  tl-x tl-y br-x br-y present?│         │
         │  unit from descriptor[5]    │          │
         │    → GeometryUnit  **D-10** │          │
         └──────┬──────────────┬───────┘          │
           set  │              │ absent/unsupported unit
                │              ▼                  │
                │      ┌───────────────────┐      │
                │      │ Pillow crop       │◄─────┘
                │      │ at ACTUAL dpi     │  **D-12** (was: requested dpi)
                │      └─────────┬─────────┘
                │                │
         ┌──────▼────────────────▼──────────────────────┐
         │ multi_scan() loop, bounded by _MAX_ADF_PAGES │  **D-04**
         │  StopIteration → feeder empty (ONLY signal)  │  **D-03**
         │  other exc     → ScanError w/ SANE text      │  **D-03**
         │  integrity only: dims, min bytes  **D-05**   │
         │    fail → skip + count; all fail → raise     │  **D-06**
         └──────────────────────┬───────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ result object          │  **D-12** — pages
                    │  pages / actual dpi /  │  + actual DPI + skip count
                    │  pages_rejected        │     leave by ONE route  **D-07**
                    └───────────┬────────────┘
                                │
   ═══════════ scanner/ ════════╪═══════════ pipeline ═══════════
                                │
                 ┌──────────────▼───────────────┐
                 │ _drop_empty_pages(profile)   │ ◄── the ONLY blank-page
                 │  toggle + user thresholds    │     policy  **D-05**
                 └──────────────┬───────────────┘
                                │
                 ┌──────────────▼───────────────┐
                 │ assemble_pdf(dpi=ACTUAL)     │  **D-12**
                 │ img2pdf fixed-dpi layout     │  (pdf.py:156's named seam)
                 └──────────────┬───────────────┘
                                ▼
                         paperless-ngx
```

**The one sentence that captures the phase:** every arrow that used to carry an *assumption*
(the source's meaning, the DPI, the unit, the page's worth) now carries a *measurement*.

### Recommended Structure

No new packages or directories. Placement of new symbols:

```
src/saneless/scanner/
├── base.py            # SourceKind, classify_source (unchanged behaviour; D-01 comment updated)
│                      #   + DeviceCapabilities gains the D-13 range field
│                      #   + D-12's result object (see Q3 — belongs here, not in pipeline.py)
└── sane_backend.py    # + GeometryUnit (D-10), _constraint() (D-13), _MAX_ADF_PAGES (D-04)
                       #   _scan_adf_pages and scan_pages BOTH need splitting (see Complexity Budget)

tests/
├── <shared fake module>          # D-17: FakeSaneDev + FakeSaneModule + local error type
│                                 #   imported by test_scanner.py AND test_pipeline.py
└── <integration module>          # D-18: marker-gated, SANE_CONFIG_DIR, test:0
```

**Naming note for the shared fake module:** it must not be discovered as a test module that imports
another test module (D-17 explicitly rejects that coupling). A plain `tests/fake_sane.py` is
importable by both without pytest collecting it as a test (no `test_` prefix).

### Pattern 1: Total lookup as `match` + `assert_never` (the house pattern D-10 follows)

**What:** an exhaustive mapping is a *function* with `match`/`assert_never`, never a `dict`.
**When to use:** `GeometryUnit`, and any enum introduced in this phase.
**Why it is not a style preference:** Phase 21's D-08 **measured** that a `dict[Enum, str]` missing a
member produces no diagnostic from either `ty` or `pyrefly`, while the same enum in a `match` with
`assert_never` is caught by both. If SANE ever adds a unit, this fails the Phase 20 CI gate at edit
time instead of silently mis-scaling a page.

```python
# Source: src/saneless/scanner/base.py:40-50 (SourceKind.uses_feeder), the shipped precedent
match self:
    case SourceKind.FEEDER | SourceKind.FEEDER_DUPLEX:
        feeds = True
    case SourceKind.FLATBED | SourceKind.AUTO | SourceKind.UNKNOWN:
        feeds = False
    case _:
        assert_never(self)
return feeds
```

### Pattern 2: Parametrise over the real thing, never a hand-written list (Phase 21 D-09)

Tests for `GeometryUnit` must parametrise over `list(GeometryUnit)`, so adding a member fails the
suite by itself. The same applies to `SourceKind` coverage added for D-02.

### Pattern 3: Fakes are concrete classes, not `MagicMock`

STATE.md records `StubScanner` and `_BrowserTestScanner` as deliberate choices. D-17 extends the same
instinct to the SANE layer. **The measured justification is in § Finding 2**: `MagicMock(spec=...)`
and the three hand-written doubles are precisely why ten dropped pages read as a passing test.

### Anti-Patterns to Avoid

- **Re-deriving a source's meaning from its string** anywhere outside `classify_source`. Five sites
  still do; § Finding 5 lists every one, including one CONTEXT.md does not name.
- **Comparing a SANE string with `==` for routing.** `effective_source == "Auto"` is case- and
  whitespace-sensitive and already wrong (§ Finding 6).
- **Assuming a device accepted what you assigned.** Measured: resolution clamps silently, and so does
  geometry (§ Findings 8, 9).
- **Letting a low layer make a policy the high layer advertises as a toggle** — M-14's principle, and
  the whole of SCNR-03.
- **Writing a test whose fake raises where the real library stores.** That is M-15 exactly, and
  `_NoGeometryDevice` (`tests/test_scanner.py:1016-1031`) still does it today.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| "Is this source a feeder?" | Another `"adf" in name.lower()` | `classify_source()` | It already exists, already documents why its branch order is load-bearing, and duplicating it is the literal content of C-06. |
| Constraint parsing | A third copy of the `isinstance(constraint, list)` block | One `_constraint()` helper (D-13) | It is already duplicated at `:330-335` and `:460-466` and both copies are wrong the same way (N-01). |
| Blank-page detection in the backend | Mean/stddev thresholds in `sane_backend` | `pipeline.filter_empty_pages` under the profile toggle | Already exists, already configurable, already documented. The backend copy is the defect. |
| Exhaustive enum dispatch | `dict[GeometryUnit, ...]` | `match` + `assert_never` | Measured to be the only form both type checkers enforce (Phase 21 D-08). |
| TOML rewriting with comments | String surgery on the config file | `tomlkit` delete + `dumps` | Measured to preserve comments across a prune (§ Finding 11). |
| Slug/path sanitising in one function | Reusing `_slugify` for filenames | Keep the two sanitisers separate | Phase 23 D-19, restated by D-15. `_slugify` passes `/` through — **measured**, § Finding 10. |
| Per-page timeouts | A new mechanism | The existing `ThreadPoolExecutor` wrapper | It works; its *teardown* is M-12, and that is **Phase 29's**, not this phase's. |

**Key insight:** this phase's work is overwhelmingly **subtractive**. Four of the seven defects are
fixed by deleting code (the first-page special case, the unreachable `multi_scan` guard, the
white/black checks, the four hard-coded slug names). The risk is a planner treating "truthfulness"
as an invitation to add machinery. The one genuine addition — D-12's result object — is explicitly
constrained by CONTEXT.md to stay minimal.

---

## Runtime State Inventory

**This phase renames profile slugs (D-14), so it is a rename/refactor phase and this section
applies.** A grep audit finds files; it does not find runtime state. Each category was checked.

| Category | Items Found | Action Required |
|----------|-------------|-----------------|
| **Stored data** | **The user's live `saneless.toml`** (untracked) holds four profiles; three carry `auto_generated = true`, `[profiles.default]` does not. D-14 renames `adf-simplex` → `adf` and `flatbed-scan` → `flatbed`; `auto` is unchanged because `Auto` already slugs to `auto`. | **Data migration, already decided:** D-16's prune. Without it the user silently holds six profiles, two stale but still functional. The prune **is** the migration. |
| | **The job store** (`<data_dir>/saneless.db`) stores `job.profile` as a **slug string**. Historic rows will reference `adf-simplex` after the rename. | **Verify, then decide.** History rows are display-only (`saneless jobs`, the history table); a stale slug there is a factual record of what was run, not a broken reference. Recommend: **no migration**, and say so explicitly in the plan so a verifier does not read it as an oversight. Confirm no code path *resolves* `job.profile` back to a live profile for a finished job. |
| **Live service config** | None. saneless has no external service holding these strings — no n8n, no Datadog, no Tailscale ACLs. Paperless-ngx stores documents and tags, never profile slugs. `[VERIFIED: grep across src/ for any outbound use of the slug]` | None. |
| **OS-registered state** | None. saneless registers no systemd units, cron entries, or scheduler tasks carrying a profile slug. The Docker image and compose file reference `saneless` the *package*, never a profile. | None. |
| **Secrets / env vars** | None affected. `SANELESS_SCANNER__HOST`, `SANE_NET_HOSTS` and the Paperless token are untouched by this phase. **New env var introduced:** `SANE_CONFIG_DIR`, but only *inside* the D-18 test fixture — never in production code or a shipped config. | None. Ensure the fixture sets it hermetically (see § Finding 1) and never leaks it to other tests. |
| **Build artifacts / installed packages** | None. No package rename, no entry-point change, no `pyproject.toml` `[project]` change. `pyproject.toml` changes only in the `markers` list (D-18). No reinstall needed. | None. |

**Additional runtime state specific to this phase, outside the five standard categories:**

- **In-process SANE library state.** `sane.init()` caches the backend set at first call for the life
  of the process, and `sane.exit()` does **not** reliably reset it (measured — § Finding 1). This is
  runtime state that the *test suite* must respect. It is the single most likely cause of a D-18
  test that passes alone and fails in the suite.
- **`--profile` values in user shell history / scripts.** `saneless scan --profile adf-simplex` in a
  user's cron or script breaks after D-14. Not saneless-managed state, but it **is** a user-visible
  break and belongs in the doc corrections D-14 already mandates. Recommend one line in the
  auto-profiles doc noting that regenerating profiles can rename them.

---

## Common Pitfalls

### Pitfall 1: `SANE_CONFIG_DIR` set too late — the D-18 fixture that silently does nothing

**What goes wrong:** the integration test asserts `test:0` is present, and gets the developer's real
scanner instead (or, in CI, an empty list).
**Why it happens:** SANE reads the config directory when the dll backend initialises. Setting the env
var after any earlier `sane.init()` in the same pytest process is too late. A function-scoped
`monkeypatch.setenv` fixture is therefore **not sufficient**, which is the natural thing to write.
**How to avoid:** set it once, session-scoped, before the first `SaneBackend()` is ever constructed.
Validated shape in § Finding 1.
**Warning signs:** the test passes when run alone and fails in the full suite — or, far worse,
*passes in CI for the wrong reason* because CI has no real scanner, so an empty device list and a
missing test backend look similar. Assert `"test:0" in names` positively; never assert "not empty".

### Pitfall 2: Writing SCNR-08's ten-page test before D-05 lands

**What goes wrong:** the test asserts ten pages and gets zero, and it looks like the feeder name or
the routing is broken.
**Why it happens:** the `test` backend's default picture is *solid black*; today's
`_validate_page_image` drops every page as "pure black" (measured, § Finding 2).
**How to avoid:** order the plans so D-05's removal of the content checks lands first, or write the
test as RED against exactly this behaviour and make D-05 turn it green — which is the better TDD
framing and the one to prefer.
**Warning signs:** ten `Page N: pure black … skipping` warnings in the captured log while the
assertion reports `0 == 10`.

### Pitfall 3: Believing the device took the value you assigned

**What goes wrong:** a scan is cropped to the wrong size, or a geometry request is silently ignored.
**Why it happens:** SANE clamps to the constraint and reports success. Measured: `resolution = 5000`
→ `1200.0`; `resolution = 0` → `1.0`; and on `test:0`, `br_x = 210.0` (A4) is **clamped to 200.0**
because the device's geometry range is `(0.0, 200.0, 1.0)`.
**How to avoid:** D-11's read-back for resolution. **And note that geometry has the same problem** —
see § Open Questions Q7.
**Warning signs:** a page whose bottom/right quarter is missing; `dev.area` not matching what was written.

### Pitfall 4: Modelling `_sane` error behaviour with only two branches

**What goes wrong:** the D-17 fake is faithful for the two cases CONTEXT.md names and wrong for a third.
**Why it happens:** CONTEXT.md names "bad value → `_sane.error`" and "structurally wrong →
`AttributeError`". There is a **third**: a value of the wrong *Python type* raises a plain
`TypeError` from the C layer before SANE sees it (`resolution = "banana"` →
`TypeError: SANE_FIXED requires a floating point number`). Measured.
**How to avoid:** model all three in the fake. § The python-sane Contract has the full table.
**Warning signs:** a test that asserts `_sane.error` for a type error and passes only because the
fake was written to match the assertion.

### Pitfall 5: Assuming the generator can carry a return value out

**What goes wrong:** D-12's actual-DPI and skip-count plumbing is written as a generator `return`
and silently evaporates.
**Why it happens:** `list(gen)` discards `StopIteration.value` — measured. Only `yield from`
captures it, and none of the three pipeline call sites use `yield from`.
**How to avoid:** change the signature (§ Open Questions Q3). Do not attempt a clever generator trick.
**Warning signs:** `actual_dpi` is always the default; no test fails because the value is never read.

### Pitfall 6: Extracting helpers *after* adding branches

**What goes wrong:** ruff `PLR0912` fails and there is no legal way out, because CLAUDE.md forbids
both raising the limit and suppressing the rule.
**Why it happens:** `_scan_adf_pages` is at **12 branches against a limit of 12** — zero headroom —
and this phase adds the `_MAX_ADF_PAGES` check to it. Measured.
**How to avoid:** split first, then add. § Complexity Budget has the exact numbers.

### Pitfall 7: `filterwarnings = ["error"]` and the real backend

**What goes wrong:** an unrelated `ResourceWarning` from an unclosed device fails the integration test.
**Why it happens:** `_SaneIterator.__del__` calls `device.cancel()`; teardown ordering matters.
**Status:** **not observed** in the spike — the integration module ran clean under the project's real
pytest configuration. Recorded as a thing to watch, not a known defect. Keep `del iterator` before
cancel/close, as the current code already does.

---

## Code Examples

All examples below are **measured behaviour**, not illustrations.

### The real `SaneDev.__setattr__` contract (the source of M-15)

```python
# Source: .venv/lib/python3.14/site-packages/sane.py:188-213 (python-sane 2.9.2)
def __setattr__(self, key, value):
    d = self.__dict__
    if key in ('dev', 'optlist', 'area', 'sane_signature', 'scanner_model'):
        raise AttributeError("Read-only attribute: " + key)

    if key not in self.opt:
        d[key] = value          # <-- UNKNOWN OPTION IS STORED SILENTLY. No device call.
        return                  #     This is why _set_geometry always returns True.

    opt = d['opt'][key]
    if opt.type == _sane.TYPE_BUTTON:  raise AttributeError("Buttons don't have values: " + key)
    if opt.type == _sane.TYPE_GROUP:   raise AttributeError("Groups don't have values: " + key)
    if not _sane.OPTION_IS_ACTIVE(opt.cap):   raise AttributeError("Inactive option: " + key)
    if not _sane.OPTION_IS_SETTABLE(opt.cap): raise AttributeError("Option can't be set by software: " + key)
    ...
    result = d['dev'].set_option(opt.index, value)
    if result & _sane.INFO_RELOAD_OPTIONS:
        self.__load_option_dict()   # <-- why D-11's source-first ordering matters
```

### The real ADF iterator (the source of D-03 and D-04)

```python
# Source: .venv/lib/python3.14/site-packages/sane.py:107-131
class _SaneIterator:
    def __next__(self):
        try:
            self.device.start()
            return self.device.snap(True)
        except Exception as e:
            if str(e) == 'Document feeder out of documents':   # EXACT string, the ONLY stop
                raise StopIteration
            else:
                raise

# multi_scan() cannot raise -- it only constructs the iterator:
def multi_scan(self):
    return _SaneIterator(self)
```

### Presence check before assignment (D-09) — note the hyphen/underscore asymmetry

```python
# Measured against test:0 -- get_options() reports HYPHENS, attributes use UNDERSCORES.
# OPT (24, 'tl-x', 'Top-left x', ..., type=2, unit=3, size=4, cap=5, (0.0, 200.0, 1.0))
_GEOMETRY_OPTIONS = ("tl-x", "tl-y", "br-x", "br-y")   # names in get_options()
# ...but assignment is dev.tl_x / dev.br_y.  Both spellings are load-bearing; a single
# set of constants used for both lookups and assignment will be wrong on one side.
```

### Driving the real backend (the validated D-18 shape)

```python
# Session-scoped: SANE_CONFIG_DIR must be exported BEFORE the first sane.init() in the process.
# A function-scoped monkeypatch.setenv is too late -- measured, see Finding 1.
@pytest.fixture(scope="session", autouse=True)
def _sane_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point SANE at a dll.conf naming only the `test` backend."""
    d = tmp_path_factory.mktemp("sane.d")
    (d / "dll.conf").write_text("test\n")
    with pytest.MonkeyPatch.context() as mp:     # session-safe form of monkeypatch
        mp.setenv("SANE_CONFIG_DIR", str(d))
        yield
```

`dll.conf` alone is sufficient — **no `test.conf` copy is needed** (measured: the backend falls back
to its compiled-in defaults, which already give two devices and a ten-sheet feeder).

---

## Verified Findings

This section is the evidence base. Each finding was executed on 2026-09-12 against
python-sane 2.9.2 and `libsane-test.so.1.0.32` on this machine.

### Finding 1 — `SANE_CONFIG_DIR` is only honoured before the first `sane.init()` `[VERIFIED: executed]`

Three scenarios, each in a fresh process:

| Scenario | `get_devices()` | `open("test:0")` |
|---|---|---|
| env set **before** `init()` | `['test:0', 'test:1']` ✅ | OK |
| `init()` first, **then** env + `init()` again | `['hpaio:…', 'test:0', 'test:1']` — backends **accumulate**, the real scanner never leaves | OK |
| `init()`, `sane.exit()`, env, `init()` | `['hpaio:…']` ❌ — **the new config dir is not picked up**, and `genesys` logged a config error | OK |

Two consequences the planner must design around:

1. **`sane.exit()` + re-init is not a reset.** Do not build the fixture on it.
2. **`open("test:0")` succeeds even without `dll.conf`.** `sane-dll` resolves the backend prefix and
   dlopens the library directly; `dll.conf` governs **enumeration only**. So a test that merely
   *scans* `test:0` needs no config dir at all — only the enumeration assertion does. This makes the
   fixture cheaper than CONTEXT.md assumed, and it explains why a half-broken fixture can still
   appear to work.

**Validated end to end:** the session-scoped fixture above was run (a) alone and (b) in the same
process as the real `tests/test_scanner.py`. Result: **74 passed**, the enumeration assertion passed
in both arrangements, total 0.83 s. The integration module costs essentially nothing against the
60 s timeout.

### Finding 2 — SCNR-03 reproduced end to end: all ten real pages are dropped `[VERIFIED: executed]`

Driving `SaneBackend.scan_pages("test:0", source="Automatic Document Feeder")` today:

```
WARNING  Page 1: pure black (mean=0.0, stddev=0.0), skipping
… ×10 …
assert 0 == 10
```

Ten pages were fed, ten were acquired, and the **backend discarded every one**. The feeder name and
the routing are correct; the content policy destroys the scan. Per-picture measurements:

| `test-picture` | mode/dpi | size | raw bytes | mean | stddev | dropped by today's backend? |
|---|---|---|---|---|---|---|
| Solid black (**the default**) | Gray 75 | 236×295 | 69,620 | 0.0 | 0.0 | **YES** |
| Solid black | Color 300 | 944×1181 | 3,344,592 | 0.0 | 0.0 | **YES** |
| Solid white | Gray 75 | 236×295 | 69,620 | 255.0 | 0.0 | **YES** |
| Solid white | Color 300 | 944×1181 | 3,344,592 | 255.0 | 0.0 | **YES** |
| Color pattern | Color 300 | 944×1181 | 3,344,592 | 57.8 | 34.6 | no |
| Grid | Gray 75 | 236×295 | 69,620 | 127.4 | 127.5 | no |

This is M-14 demonstrated with real hardware semantics, and it is the strongest single argument for
D-05. It also gives the integration module a free extra assertion: with `test_picture = "Solid
white"` and `enable_empty_page_detection = false`, the pipeline must deliver **ten** pages.

### Finding 3 — the real first-page error messages (SCNR-02's test vectors) `[VERIFIED: executed]`

With `enable_test_options = true` and `read_return_value` set, the first `next()` produces:

| `read_return_value` | Result | Exact message |
|---|---|---|
| `SANE_STATUS_IO_ERROR` | `_sane.error` | `Error during device I/O` |
| `SANE_STATUS_JAMMED` | `_sane.error` | `Document feeder jammed` |
| `SANE_STATUS_COVER_OPEN` | `_sane.error` | `Scanner cover is open` |
| `SANE_STATUS_DEVICE_BUSY` | `_sane.error` | `Device busy` |
| `SANE_STATUS_NO_DOCS` | **`StopIteration`** | — (the genuine feeder-empty signal) |
| `Default` | page returned | — |

Today all four `_sane.error` cases become `FeederEmptyError("No paper detected in feeder")`. After
D-03 each must surface as `ScanError` carrying the text above. **These are exact strings a test can
assert**, and the `read_return_value` recipe makes them reproducible in the D-18 module rather than
only in the fake.

### Finding 4 — N-01 reproduced against the real device `[VERIFIED: executed]`

```
SOURCES: ['Flatbed', 'Automatic Document Feeder']   RESOLUTIONS: []
```

`test:0` reports resolution as `(1.0, 1200.0, 1.0)` — a range — and `get_capabilities` returns an
empty list, so `devices --capabilities` prints a blank line and `pick_closest_resolution` falls back
to 300 regardless of the device. Note the constraint members are **floats**, which matters for D-13's
field type (§ Open Questions Q6).

### Finding 5 — the complete inventory of surviving ad-hoc classification rules `[VERIFIED: grep across src/]`

SCNR-01 says "every feeder decision". CONTEXT.md's D-02 names two sites. There are **five**, and one
is not named:

| Site | Rule | Disposition |
|---|---|---|
| `auto_profiles.py:182` | `any("flatbed" in s.lower() …)` | **D-02** — route via `classify_source` |
| `auto_profiles.py:193` | `[s for s in … if "flatbed" in s.lower()]` | **D-02** — route via `classify_source` |
| **`auto_profiles.py:181`** | **`if source.lower() == "auto":`** | **NOT named by D-02.** Same defect class — a second rule answering "is this the Auto source?". Should route via `classify_source(source) is SourceKind.AUTO`. Flagged for the planner. |
| `sane_backend.py:495` | `if effective_source == "Auto":` | Discretion item — see Finding 6. **Phase 21's D-11 explicitly assigns this to Phase 24.** |
| `sane_backend.py:404` | `error_str = str(exc).lower()` + `"out of documents"`/`"no docs"` | Dies with **D-03**. |
| `pipeline.py:378`, `worker.py:193-194` | manual-duplex `"manual" … "duplex"` | **Phase 25 — do NOT touch.** |

### Finding 6 — the `auto_source_mode` override *is* a classification rule, and it is already buggy `[VERIFIED: executed]`

CONTEXT.md leaves this to the planner's judgement, "config-driven routing rather than
classification, so it may be correct as-is". Measured, it is **not** correct as-is:

| Source string | `== "Auto"` | `classify_source` |
|---|---|---|
| `"Auto"` | True | `AUTO` |
| `"auto"` | **False** | `AUTO` |
| `" AUTO "` | **False** | `AUTO` |
| `"Automatic Document Feeder"` | False | `FEEDER` ✅ |

A device reporting its source as lowercase `auto` classifies as `AUTO` (so `uses_feeder` is False and
it takes the single-page path) **but skips the override entirely** — so the user's
`auto_source_mode = "adf"` is silently ignored and a stack returns one page. That is the exact
user-visible failure C-06 exists to eliminate, surviving in a second place.

**Recommendation: change it.** Guard with `classify_source(effective_source) is SourceKind.AUTO`.
The *decision* it makes (consult config) stays config-driven; only the *recognition* of the Auto
source moves to the single classifier. This satisfies SCNR-01's "every feeder decision" literally,
costs three tokens, and Phase 21's D-11 already names this line as Phase 24's to own.

### Finding 7 — the python-sane contract, all five behaviours `[VERIFIED: executed]`

D-17 must model these. CONTEXT.md names four; the third row is the one it does not.

| Action | Real behaviour | Fake must |
|---|---|---|
| Assign an **unknown** option name | Stored silently in `__dict__`; no device call, no raise | store it, return nothing |
| Assign a **bad value** to a known option (`mode="Lineart"`, `source="Nope"`) | `_sane.error: Invalid argument` | raise the local error type |
| Assign a **wrong Python type** (`resolution="banana"`) | **`TypeError: SANE_FIXED requires a floating point number`** — raised by the C layer before SANE | raise `TypeError` |
| Read/assign a **button or group**; read an **inactive** option; assign a **read-only** attr (`dev`) | `AttributeError` with a specific message (`Buttons don't have values: button`, `Inactive option: …`, `Read-only attribute: dev`) | raise `AttributeError` |
| Read a genuinely absent attribute | `AttributeError: No such attribute: <name>` | raise `AttributeError` |
| `multi_scan()` | Returns `_SaneIterator`; **cannot raise** | return an iterator that calls `start()`+`snap()` per page |

The existing `_NoGeometryDevice` (`tests/test_scanner.py:1016-1031`) raises `AttributeError` on
geometry assignment — the **exact inverse** of row 1, and the reason M-15 shipped green.

### Finding 8 — geometry units and a second silent clamp `[VERIFIED: executed]`

`test:0` reports `tl-x`/`br-x` as: `type=2` (`TYPE_FIXED`), **`unit=3` (`UNIT_MM`)**, constraint
`(0.0, 200.0, 1.0)`.

Writing an A4 box (`br_x = 210.0`, `br_y = 297.0`) and reading `dev.area` back gives
**`((0.0, 0.0), (200.0, 200.0))`** — clamped, silently, with no error and no `INFO_INEXACT` visible to
the caller. So geometry has exactly the disease D-11 cures for resolution. See § Open Questions Q7.

The complete unit set was re-confirmed directly from `_sane`, independent of CONTEXT.md:
`UNIT_NONE=0, UNIT_PIXEL=1, UNIT_BIT=2, UNIT_MM=3, UNIT_DPI=4, UNIT_PERCENT=5, UNIT_MICROSECOND=6`.
**No `UNIT_CM`, no `UNIT_INCH`.** CONTEXT.md is correct and D-10's seven arms are complete.

### Finding 9 — resolution clamps, and returns `float` `[VERIFIED: executed]`

| Requested | Read back |
|---|---|
| 5000 | `1200.0` |
| 300 | `300.0` |
| 0 | `1.0` |
| 150 | `150.0` |

Two notes for D-11: the return is **`float`**, so `int(dev.resolution)` is required (as D-11 says);
and the `SaneDevice` Protocol at `sane_backend.py:219` declares `resolution: int`, which is a **type
lie** — the Protocol says `int`, the real object returns `float`. D-17's fake must return `float` to
be faithful, which will make the Protocol's dishonesty visible to `ty`/`pyrefly`. Fix the Protocol;
do not annotate around it (CLAUDE.md forbids suppression).

Also measured, relevant to D-11's ordering rationale: on this backend, setting `source` after
`resolution` did **not** clamp the resolution (1200.0 survived). The ordering change is still
correct — `__setattr__` reloads the option dict on `INFO_RELOAD_OPTIONS`, and feeders commonly have a
different ceiling — but the planner should know that **`test:0` will not demonstrate the bug**. The
ordering needs a fake-based test (a `FakeSaneDev` whose `source` assignment narrows the resolution
constraint), not a real-backend one.

### Finding 10 — slug behaviour measured, and one CONTEXT claim corrected `[VERIFIED: executed]`

| Source name | today's `_slugify` | today's `source_to_slug` |
|---|---|---|
| `ADF (left aligned)` | `adf-(left-aligned)` | `adf-simplex` |
| `Automatic Document Feeder(left aligned)` | `automatic-document-feeder(left-aligned)` | `adf-simplex` |
| `ADF-Front` | `adf-front` | `adf-simplex` |
| `ADF Front` | `adf-front` | `adf-simplex` ← **collides with the row above after D-14** |
| `ADF  Duplex` (double space) | `adf--duplex` | `adf-duplex` |
| `Flachbett/Einzug` | **`flachbett/einzug`** — `/` passes straight through | `flachbett/einzug` |
| `"  "` (whitespace only) | **`--`** | `--` |

**Correction to D-15's stated rationale — the planner must not write a test around it.** D-15 says
`adf-(left-aligned)` is "not a legal TOML bare key". Literally true (bare keys are `[A-Za-z0-9_-]`),
but the implied consequence is **false**: tomlkit emits it as a *quoted* key and `tomllib` parses it
back cleanly —

```
[profiles."adf-(left-aligned)"]
source = "ADF (left aligned)"
```
```
tomllib round-trip: OK
```

**D-15 still stands**, on its other and stronger grounds, all now measured: `/` passthrough (a real
hazard and the same weakness Phase 23 D-19 identified), `--` from whitespace-only names, `adf--duplex`
from double spaces, and awkwardness as a `--profile` CLI value and URL fragment. Plan D-15 on those;
do not assert that the un-hardened slug breaks the config file.

### Finding 11 — D-16's prune preserves comments `[VERIFIED: executed]`

`del doc["profiles"]["adf-simplex"]` followed by `tomlkit.dumps` removed exactly that table and left
both a file-level comment and a user's inline comment inside `[profiles.default]` intact, with the
untouched `[profiles.keep-me]` preserved. D-16 is feasible as specified; its "needs its own test"
requirement is cheap to satisfy.

### Finding 12 — CI already has the `test` backend `[VERIFIED: two independent package sources]`

- `libsane-dev` (noble) ships `/usr/lib/x86_64-linux-gnu/sane/libsane-test.so` and `libsane-test.a`.
- `libsane1` (noble) ships `libsane-test.so.1` and `libsane-test.so.1.2.1` — the versioned object
  `sane-dll` actually dlopens.
- `libsane-dev` **Depends: libsane1 (= 1.2.1-7build4)**, so installing the former guarantees the latter.
- `.github/workflows/ci.yml:26` and `:43` already install `libsane-dev` in **both** jobs.

**The only CI change needed is letting the new marker through the `-m "not browser"` filter at `:44`.**
No new apt package. D-18's fallback path should not be required. `[CITED: packages.ubuntu.com/noble/amd64/libsane-dev/filelist, packages.ubuntu.com/noble/amd64/libsane1/filelist, packages.ubuntu.com/noble/libsane-dev]`

---

## Complexity Budget

`[VERIFIED: ruff run with descending max-branches to find exact counts]`

Ruff's `PL` rules are enabled; `C901` is **not** in the project's `select` list, so mccabe complexity
is not enforced — but `PLR0912` (max-branches, default **12**) is, and CLAUDE.md forbids both raising
the limit and suppressing the rule.

| Function | Branches | Limit | Headroom | Consequence |
|---|---|---|---|---|
| `_scan_adf_pages` | **12** | 12 | **0** | Adding D-04's cap check **will** break the build. Split *before* adding. |
| `scan_pages` | **11** | 12 | 1 | D-11's read-back + warn, D-09's presence check and D-12's assembly cannot all fit. Split. |
| `get_capabilities` | under limit | 12 | some | D-13's `_constraint()` helper *reduces* it. |
| `source_to_slug` | under limit | 12 | some | D-14 *removes* branches (four cases collapse to one). |
| `write_profiles_to_config` | under limit | 12 | some | D-16 adds a prune loop; watch it. |

`PLR0913` (max-args, default **5**, `self` not counted) is already load-bearing in this codebase —
both `JobResult` and `_DeliveryContext` exist solely to stay under it. If D-12's object needs
threading into a helper, follow that established pattern rather than adding parameters.

**Recommended split for `_scan_adf_pages`** (the phase both adds to it and removes from it):
`_next_page_with_timeout(...)` (the executor/future/timeout block) and `_acquire_pages(...)` (the
loop, the cap, the integrity skip and the count). D-03 and D-05 together *delete* several branches,
so the post-phase function may well come in comfortably under the limit — but the intermediate
commits must also pass, and TDD means intermediate commits exist.

---

## State of the Art

| Old approach (in this codebase) | Correct approach | Evidence |
|---|---|---|
| Infer feeder-ness from a substring, in several places | One classifier, consulted everywhere | C-06; `classify_source` already shipped in Phase 21 |
| Infer "feeder empty" from "it failed on iteration zero" | Recognise the one signal the library gives you (`StopIteration`) and report everything else truthfully | M-11; Finding 3 |
| Validate content at the lowest layer | Integrity at the bottom, policy at the top | M-14; Finding 2 |
| Assume assignment succeeded | Read back; the device is the source of truth | M-16; Findings 8, 9 |
| Assume millimetres | Read the unit from the descriptor | N-03; Finding 8 |
| Handle only list constraints | Handle all three documented shapes | N-01; Finding 4 |
| Fakes written for convenience | Fakes derived from the third-party source, backed by one real run | M-32; Findings 1, 2, 7 |

**Deprecated/outdated in this codebase:**
- `_SCANNER_WHITE_*` / `_SCANNER_BLACK_*` thresholds (`sane_backend.py:71-74`) — removed by D-05.
- The `page_num == 0` special case and the `try/except` around `multi_scan()` — removed by D-03; the
  latter is provably unreachable.
- The `"back"` special case (`auto_profiles.py:71-77`) — dead code after D-14.
- The four hard-coded slugs — replaced by D-14.
- `_NoGeometryDevice`'s raising `__setattr__` — inverts the real contract (Finding 7).

---

## Assumptions Log

Everything else in this document was executed or cited. These are the claims that were **not**
independently verified and that a planner should treat as provisional.

| # | Claim | Section | Risk if Wrong |
|---|---|---|---|
| A1 | Real-world ADF capacities top out around 500 sheets, so 500 is a cap no real stack reaches | Open Questions Q1 | A production scanner with a larger hopper fails a legitimate job. Mitigated by the error naming the cap, so the fix is one constant. |
| A2 | Historic `job.profile` slugs in the job store are display-only and need no migration after D-14 | Runtime State Inventory | A stale slug in a resolved lookup would raise. **The planner must confirm** no code path resolves `job.profile` to a live profile for a finished job before accepting "no migration". |
| A3 | GitHub's `ubuntu-latest` is Ubuntu 24.04 (noble), matching the package pages checked | Finding 12 | If the runner moves to a release where `libsane1` splits out the test backend, D-18's CI step fails. Detected immediately by the CI run; D-18's fallback is already decided. |
| A4 | No `ResourceWarning`/teardown warning will surface under `filterwarnings=["error"]` in a fuller integration module | Pitfall 7 | A flaky integration test. Observed clean in the spike, but the spike had three tests, not the full set. |
| A5 | `test:0`'s behaviour is representative enough of real backends for the option-reload path | Finding 9 | It is **not**, for the source→resolution clamp specifically — already flagged; that case needs a fake, not the real backend. |

---

## Open Questions

These are CONTEXT.md's explicit discretion items. Each gets a recommendation and the reasoning to
record in the plan.

**Q1 — `_MAX_ADF_PAGES`'s value (D-04).**
*What we know:* the cap's job is to stop a non-terminating loop, and `pipeline.py` materialises pages
with `list()`. *What's unclear:* a value generous enough for real hardware.
**Recommendation: 500**, with this written down honestly: at A4/300 dpi colour a page is ~26 MB, so
500 pages is ~13 GB — **the cap does not bound memory in any useful sense, and must not be described
as if it does.** It bounds the *unbounded loop*, which is W-01's actual finding. Memory bounding is
Phase 29's HARD-01/HARD-02 territory. 500 matches the largest production ADF hoppers, so no real
stack reaches it. **Also record, as D-04 requires:** Phase 25's two manual-duplex passes each call
`scan_pages` separately, so a per-call cap is **per-pass**, not per-job.

**Q2 — Does `_MIN_PAGE_BYTES` stay at 10 KB?**
*What we know:* measured smallest legitimate real page = **69,620 bytes** (Gray, 75 dpi, 80×100 mm
bed) — nearly 7× the threshold. At 300 dpi colour it is 3.3 MB.
**Recommendation: keep 10 KB**, and add the measurement as a comment. It is one of only two surviving
integrity checks and it demonstrably does not fire on legitimate small pages.

**Q3 — D-12's result object: name and shape. ⚠️ The hardest question in the phase.**
*What we know, measured:* `scan_pages` is a **generator**; `list(gen)` **discards** the generator's
return value; only `yield from` captures it, and none of the three pipeline call sites use it. So the
facts cannot leave the backend without a **signature change**. Blast radius: the `ScannerBackend`
ABC, `SaneBackend`, and ~10 fakes/stubs (`tests/conftest.py:119`, `test_browser.py:96`,
`test_cli.py:126` and `:240`, `test_scanner.py:238`, plus ~15 `MagicMock(spec=ScannerBackend)` sites).

| Option | Verdict |
|---|---|
| **(a) `scan_pages` returns a frozen result object holding the pages** | **Recommended.** Streaming is already unused — all three call sites do `list(...)`. Bonus: the `with self._open_device(...)` block currently stays open until the generator is exhausted or GC'd; an eager return closes the device deterministically. |
| (b) Keep the generator, add a second `scan_batch()` method | Two methods that must agree; the old one stays wrong. |
| (c) `yield from` at the three call sites to capture `StopIteration.value` | Fragile, obscure, and silently degrades to `None` if a caller ever uses `list()` again. |

**Recommended shape** — in `scanner/base.py` (Phase 21 D-03: no job-vocabulary dependency), modelled
on `pipeline.ScanResult`:

```python
@dataclass(frozen=True)
class ScanBatch:
    """The pages one acquisition produced, and what the device actually did."""
    pages: list[Image.Image]
    actual_resolution: int      # read back from the device (D-11)
    pages_rejected: int         # integrity failures skipped (D-06/D-07)
```

Three fields, no per-page structure — which is exactly the line CONTEXT.md draws against HARD-01's
ordered page records. **Flag for the planner:** the deterministic-close side effect is a *consequence*,
not a goal; M-12/M-13's close-while-reading semantics remain **Phase 29's** and must not be folded in.

**Q4 — The marker name and fixture shape (D-18).**
**Recommendation: `sane_hardware`.** `integration` is too broad (Phase 29+ will want it for other
things); `sane` collides conceptually with the module name. Register in `pyproject.toml`'s `markers`
(`strict_markers` is live — an unregistered marker fails collection), deselect via
`-m "not browser and not sane_hardware"` in CI and CONTRIBUTING, and document it alongside the
existing `browser` paragraph at `CONTRIBUTING.md:83-84`.
**Fixture: session-scoped, `dll.conf` only, no `test.conf` copy** — validated in Finding 1. Use
`pytest.MonkeyPatch.context()` since the function-scoped `monkeypatch` fixture is unavailable at
session scope.

**Q5 — The slug collision tie-break (D-15).**
*What we know:* `"ADF-Front"` and `"ADF Front"` both normalise to `adf-front` (measured).
**Recommendation: keep the first, append `-2`, `-3`… to subsequent collisions, and log a WARNING
naming both source strings.** Deterministic given `capabilities.sources` order (which is the device's
own order, stable per device), preserves the common case unchanged, and — unlike "last wins" — never
silently loses a source, which is N-09's actual complaint. Needs a test with two colliding names.

**Q6 — Does `DeviceCapabilities` keep `resolutions` alongside the range (D-13)?**
*Constraint given:* one fact must not have two independently-settable representations.
**Recommendation: keep both fields, but make the range authoritative and derive nothing.** The two
are *different facts*, not two spellings of one: a device reports **either** a word list **or** a
range, never both. So model it as such — populate `resolutions` when the constraint is a list, and
`resolution_range` when it is a range; at most one is ever non-empty. That satisfies the constraint
literally (neither is derived from the other, and they cannot disagree), keeps all existing consumers
working, and is the honest representation of what the device said.
**Type note from Finding 4:** the constraint members are **floats** (`(1.0, 1200.0, 1.0)`). Decide
explicitly whether the field is `tuple[float, float, float]` (faithful) or coerced to `int`
(convenient for `pick_closest_resolution`); faithful-then-coerce-at-use is the better fit for a phase
named truthfulness. `cli.py:217` renders whichever is populated.

**Q7 — Geometry clamping (raised by research, not in CONTEXT.md).**
*What we know:* writing A4's 210 mm to a device whose range is `(0.0, 200.0, 1.0)` is silently clamped
to 200.0 (Finding 8). So `_set_geometry` can return `True` having set a scan area that is **not** the
requested paper size — the crop fallback does not run, and the page is quietly wrong.
*What's unclear:* whether closing this is in scope. It is arguably the geometry half of SCNR-04's
"geometry is written only when the device reports…" and of M-16's principle.
**Recommendation: read `dev.area` back after writing and fall back to the Pillow crop if it does not
match, or at minimum log a WARNING naming requested vs actual.** Cheap, squarely within SCNR-04/05's
"read it back" principle, and it uses the crop fallback D-09 is already making reachable. **Raise it
with the user before planning** rather than deciding unilaterally — it is adjacent to, but not named
by, any of the 18 decisions.

**Q8 — `auto_source_mode` at `sane_backend.py:495`.** Answered with evidence in Finding 6:
**change it** to `classify_source(...) is SourceKind.AUTO`. Phase 21's D-11 explicitly assigns this
line to Phase 24, and the current `== "Auto"` has a measured case-sensitivity defect.

**Q9 — `auto_profiles.py:181` (raised by research).** `source.lower() == "auto"` is a sixth ad-hoc
rule that D-02 does not name. **Recommendation: fold it into D-02's scope** — it is the same defect
class, in the same function, and fixing `:182`/`:193` while leaving `:181` would be conspicuous.

---

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| python-sane | Everything | ✓ | 2.9.2 | — |
| `_sane` C extension | D-10 constants, D-17 error fidelity | ✓ | bundled | — |
| SANE `test` backend (local) | SCNR-08 | ✓ | `libsane-test.so.1.0.32` at `/usr/lib64/sane/` | — |
| `/etc/sane.d/test.conf` (local) | Reference only | ✓ | present | Not needed — backend defaults suffice (Finding 1) |
| SANE `test` backend (CI) | SCNR-08 in CI | ✓ | via `libsane-dev` → `libsane1` | D-18's pre-decided fallback: keep marker + module, drop CI step |
| tomlkit | D-16 | ✓ | ≥0.14.0 | — |
| pytest + pytest-timeout | D-18 | ✓ | 9.0.2 / 2.4.0 | — |
| A real scanner | — | ✓ (hpaio HP LaserJet 3030 present) | — | Irrelevant; **and a hazard** — it is what an under-scoped `SANE_CONFIG_DIR` fixture picks up instead of `test:0` (Finding 1) |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none.

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 (+ pytest-timeout 2.4.0) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py -q` |
| Full suite command | `uv run pytest -m "not browser"` |
| Current baseline | **742 passed, 30 deselected in 29.48s** `[VERIFIED: executed]` |
| Global timeout | `timeout = 60`, `timeout_method = "signal"` |
| Strictness | `filterwarnings = ["error"]`, `xfail_strict`, `--strict-markers`, `--strict-config` |
| New marker | `sane_hardware` (Q4) — **must** be registered in `markers` or collection fails |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SCNR-01 | `classify_source` drives flatbed selection in `generate_profiles` (`:181`, `:182`, `:193`) | unit | `uv run pytest tests/test_auto_profiles.py -q` | ✅ |
| SCNR-01 | "Automatic Document Feeder" yields N>1 pages through `SaneBackend` | unit (fake) | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-01 | Two distinct feeder sources produce two distinct slugs (N-09) | unit | `uv run pytest tests/test_auto_profiles.py -q` | ✅ |
| SCNR-01 | `auto_source_mode` override recognises `"auto"` case-insensitively (Finding 6) | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-02 | Each of the four `_sane.error` messages surfaces as `ScanError` carrying the text, never `FeederEmptyError` | unit (fake), parametrised over Finding 3's table | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-02 | `StopIteration` on page 0 → `FeederEmptyError` (the only path) | unit | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-03 | A pure-white and a pure-black page both survive the backend | unit | `uv run pytest tests/test_scanner.py -q` | ✅ |
| SCNR-03 | Blank removal happens only in `_drop_empty_pages`, only when enabled | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-03 | Duplex parity preserved when no page fails integrity | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-03 | D-06: one integrity failure skips+counts; all-fail raises | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-04 | Geometry skipped and **crop fallback proven reachable** when any of the four options is absent (fake stores silently, per Finding 7) | unit | `uv run pytest tests/test_scanner.py -q` | ✅ (must fix `_NoGeometryDevice`) |
| SCNR-04 | `GeometryUnit` exhaustiveness, parametrised over `list(GeometryUnit)` | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-04 | `UNIT_PIXEL` converts using the read-back resolution | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-05 | Options set source→mode→resolution; a source change that narrows the constraint is caught | unit (**fake only** — see Finding 9/A5) | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-05 | Actual DPI reaches `crop_to_paper_size` **and** `assemble_pdf` | unit | `uv run pytest tests/test_pipeline.py -q` | ❌ Wave 0 |
| SCNR-06 | Range constraint `(min,max,step)` populates the new field; list populates `resolutions`; `None` yields neither | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-06 | `devices --capabilities` prints the range | unit | `uv run pytest tests/test_cli.py -q` | ✅ |
| SCNR-07 | Fake: unknown option stored; bad value → error type; wrong type → `TypeError`; button/inactive/read-only → `AttributeError`; `multi_scan()` cannot raise | unit, parametrised over Finding 7's table | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |
| SCNR-07 | `tests/test_pipeline.py` uses the shared fake for at least one duplex path | unit | `uv run pytest tests/test_pipeline.py -q` | ✅ |
| SCNR-08 | Real `test:0`: enumeration lists it; ten pages through "Automatic Document Feeder"; range constraint honoured | integration (marker-gated) | `uv run pytest -m sane_hardware -q` | ❌ Wave 0 |
| D-16 | Prune removes orphaned `auto_generated` profiles, preserves unflagged ones **and comments** | unit | `uv run pytest tests/test_auto_profiles.py -q` | ❌ Wave 0 |
| D-04 | Exceeding `_MAX_ADF_PAGES` raises `ScanError` naming cap and count | unit | `uv run pytest tests/test_scanner.py -q` | ❌ Wave 0 |

**No manual-only items.** There is no browser surface in this phase, and the one piece of hardware
behaviour is automated by the `test` backend — which is the entire point of SCNR-08.

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py -q`
- **Per wave merge:** `uv run pytest -m "not browser"` (full suite, ~30 s) **plus**
  `uv run pytest -m sane_hardware -q`
- **Phase gate:** full suite green + `sane_hardware` green + `uv run ruff check .` +
  `uv run ruff format --check .` + `uv run ty check` + `uv run pyrefly check src tests`, all before
  `/gsd-verify-work`.

### Wave 0 Gaps

- [ ] `tests/fake_sane.py` — the D-17 shared fake (`FakeSaneDev`, `FakeSaneModule`, local error type).
      **Blocks most rows above**; build it first.
- [ ] `tests/test_sane_hardware.py` (or similar) — the D-18 module + session-scoped fixture.
- [ ] `pyproject.toml` `markers` — register `sane_hardware` (collection fails otherwise).
- [ ] `.github/workflows/ci.yml:44` — widen the deselect filter; no new apt package (Finding 12).
- [ ] `CONTRIBUTING.md:83-84` — document the new marker beside `browser`.
- [ ] Fix `tests/test_scanner.py:1016-1031` `_NoGeometryDevice` to **store**, not raise (Finding 7) —
      this is what makes the existing SCNR-04 fallback tests meaningful rather than decorative.

**Ordering constraint, measured (Finding 2):** SCNR-08's ten-page assertion **cannot pass until D-05
removes the pure-black check**. Sequence the plans so the integration test is written RED against
that exact behaviour and D-05 turns it green — do not schedule them in parallel waves.

---

## Security Domain

`security_enforcement` is absent from `.planning/config.json`, so it is treated as **enabled**.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | No auth surface in the scanner layer. |
| V3 Session Management | no | No sessions. |
| V4 Access Control | no | Unchanged by this phase. |
| V5 Input Validation | **yes** | Device-supplied strings (source names, option constraints) become profile slugs, TOML keys and CLI values. D-15's `[a-z0-9-]` hardening **is** the control. `_constraint()` must handle all three documented shapes without trusting the type. |
| V6 Cryptography | no | None involved. Nothing hand-rolled. |
| V12 File/Resource | **yes** | Slugs must not reach a filesystem path. Phase 23 D-19 already separated the filename sanitiser; D-15 must not re-merge them. |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| **Unbounded `multi_scan()` iteration on non-feeder hardware** (W-01) | Denial of Service | **D-04's `_MAX_ADF_PAGES`.** This phase *discharges* the accepted risk. Reproduced live in Finding 1's sibling probe: a **Flatbed** source driven through `multi_scan()` yielded ≥12 pages and would not stop. W-01's accept rationale must be corrected to "unbounded on non-feeder hardware; bounded from Phase 24 by `_MAX_ADF_PAGES`." |
| Unbounded memory from materialised pages (`list(scan_pages(...))`) | Denial of Service | **Partially** addressed. Be honest in the plan: the cap bounds iteration, not bytes (Q1). Full mitigation is Phase 29. |
| Device-controlled string reaching a filesystem path via a slug | Tampering / Path traversal | D-15's character-set hardening. **Measured**: `Flachbett/Einzug` → `flachbett/einzug` today, `/` intact. Not currently exploitable (slugs are TOML keys, not paths), but it is one careless reuse away — exactly Phase 23 D-19's reasoning. |
| Device-controlled string reaching a TOML key | Tampering | Not exploitable — tomlkit quotes and `tomllib` round-trips safely (Finding 10). Record this so the threat model is accurate rather than alarmist. |
| Misleading error text hiding a real fault (jam/cover/busy reported as "load paper") | Repudiation / poor incident response | **D-03.** This is a security-adjacent honesty fix: operators triaging failures are currently misled about the cause (Finding 3). |
| Test fixture leaking `SANE_CONFIG_DIR` into other tests | (Test hygiene) | Session-scoped `MonkeyPatch.context()` so it is unset at session end; never `os.environ[...] = ...` without teardown. |

---

## Sources

### Primary (HIGH confidence — executed or read locally)

- `.venv/lib/python3.14/site-packages/sane.py` (python-sane **2.9.2**) — `_SaneIterator` (`:107-134`),
  `SaneDev.__setattr__` (`:188-213`), `__getattr__` (`:215-235`), `multi_scan` (`:348`),
  `UNIT_STR`/`TYPE_STR` (`:13-23`). **The authoritative spec for D-17.**
- `_sane` C extension — unit/type/cap constants and `_sane.error`'s MRO, read directly.
- Live execution against `libsane-test.so.1.0.32` / `test:0` — Findings 1, 2, 3, 4, 7, 8, 9.
- Live execution under the project's real pytest configuration — Finding 1's fixture validation.
- `tomlkit` + `tomllib` round-trip execution — Findings 10, 11.
- `uv run ruff check --config lint.pylint.max-branches=N` — the Complexity Budget table.
- `/etc/sane.d/dll.conf:95` (`#test`) and `/etc/sane.d/test.conf` — local filesystem.
- Project sources: `sane_backend.py`, `scanner/base.py`, `auto_profiles.py`, `pipeline.py`, `pdf.py`,
  `paper_sizes.py`, `cli.py`, `worker.py`, `job.py`, `config.py`, `exceptions.py`, the test suite,
  `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `CONTRIBUTING.md`, `docs/`.
- Planning corpus: `24-CONTEXT.md`, `ROADMAP.md` § Phase 24, `REQUIREMENTS.md:64-71`,
  `reviews/2026-09-09-code-review.md` (C-06, M-11, M-14, M-15, M-16, M-32, N-01, N-03, N-09 and doc
  rows 6-12), `21-CONTEXT.md` (D-08, D-09, D-11 + AMENDED + EVIDENCE CORRECTION), `21-SECURITY.md`
  § W-01, `23-CONTEXT.md` (D-08, D-19), `22-CONTEXT.md` (D-08), `20-CONTEXT.md` § Hang guard,
  `23.1-CONTEXT.md` (D-01/D-02, D-10), `STATE.md`, `./CLAUDE.md`.

### Secondary (MEDIUM-HIGH confidence — official package metadata, two independent pages)

- `packages.ubuntu.com/noble/amd64/libsane-dev/filelist` — contains `libsane-test.so`, `libsane-test.a`.
- `packages.ubuntu.com/noble/amd64/libsane1/filelist` — contains `libsane-test.so.1`, `.so.1.2.1`.
- `packages.ubuntu.com/noble/libsane-dev` — `Depends: libsane1 (= 1.2.1-7build4)`.

### Tertiary (context only, not relied upon)

- Context7 `/websites/sane-project` — SANE `test` backend option documentation. Consulted for
  cross-checking units and constraint shapes; it confirmed the model (units include pixel, bits, mm,
  percent; constraints include range and word list) but added nothing the local `_sane` inspection had
  not already established more precisely. **python-sane itself is not in Context7**; the installed
  source is strictly better evidence and was used instead.

### Not consulted, deliberately

- `sane-test(5)` / `sane-dll(5)` manpages — every claim they would have supported (the backend ships
  commented out; `SANE_CONFIG_DIR` semantics; `number_of_devices`; `read-delay`) was instead measured
  directly on this machine, which is stronger evidence than documentation.

---

## Metadata

**Confidence breakdown:**

| Area | Level | Reason |
|------|-------|--------|
| python-sane semantics (D-17, D-03, D-09) | **HIGH** | Read from the installed source and executed. Five behaviours measured, including one CONTEXT.md does not name. |
| Real-backend behaviour (D-05, D-10, D-11, D-13, SCNR-08) | **HIGH** | Every claim executed against `test:0`. Three defects reproduced end to end. |
| D-18 fixture shape | **HIGH** | The failure mode was found *and* the working shape validated alongside the real suite. |
| CI package availability (D-18) | **HIGH** | Two independent package pages plus the dependency relation; mechanism understood, not assumed. |
| Slug behaviour (D-14, D-15) | **HIGH** | Measured, and one CONTEXT.md rationale corrected. |
| tomlkit prune (D-16) | **HIGH** | Executed. |
| Complexity budget | **HIGH** | Exact branch counts obtained from ruff itself. |
| D-12's object shape | **MEDIUM** | The *constraint* (generators discard return values) is measured and certain; the recommended shape is a design judgement the planner may refine. |
| `_MAX_ADF_PAGES` value | **LOW** | Assumption A1 — a judgement about hardware not in hand. Cheap to change; flagged accordingly. |
| Job-store slug migration (A2) | **LOW** | Needs one confirming grep during planning before "no migration" is accepted. |

**Research date:** 2026-09-12
**Valid until:** ~2026-10-12 (30 days). The stack is stable — python-sane 2.9.2 and the SANE test
backend are not fast-moving. The one item with a shorter half-life is A3 (the CI runner image), which
CI itself will falsify immediately if it changes.
