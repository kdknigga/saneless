# Phase 24: Scanner Truthfulness - Pattern Map

**Mapped:** 2026-09-13
**Files analyzed:** 22 (2 created, 20 modified)
**Analogs found:** 20 / 22

This phase is overwhelmingly **subtractive and in-place**. Only two files are new, and almost every
new symbol has a shipped in-repo precedent. The map below names the precedent for each, with the
exact lines to copy.

Three things were **measured during mapping** and are not in CONTEXT.md or RESEARCH.md. They are
called out in place and repeated in § Measured Corrections at the end:

1. The D-17 shared fake is importable **only** as `from tests.fake_sane import ...` — the flat form
   RESEARCH.md's `tests/fake_sane.py` phrasing implies raises `ModuleNotFoundError`.
2. D-12's blast radius is **larger than RESEARCH.md's "~10 fakes/stubs"**: 23 `MagicMock` sites,
   25 stub assignments, 6 concrete implementers, ~40 `list(...scan_pages(...))` call sites.
3. `docs/how-to/configure-scan-profiles.md:128` carries a slug lie CONTEXT.md does not name, and it
   is wrong both before and after D-14.

---

## File Classification

### Created

| New File | Role | Data Flow | Closest Analog | Match Quality |
|----------|------|-----------|----------------|---------------|
| `tests/fake_sane.py` | test double (shared module) | request-response (device protocol emulation) | `tests/test_scanner.py:49-141` (`MockSaneDev` + `MockSaneModule`) | role-match — the thing being replaced |
| `tests/test_sane_hardware.py` | integration test module | file-I/O + request-response (real device) | `tests/test_browser.py` (marker + session fixture) | role-match |

### New symbols in existing files

| New Symbol | Lands In | Role | Data Flow | Closest Analog | Match Quality |
|------------|----------|------|-----------|----------------|---------------|
| `GeometryUnit` (D-10) | `scanner/sane_backend.py` | vocabulary enum | transform (unit → mm) | `SourceKind.uses_feeder`, `base.py:40-50` | exact |
| `ScanBatch` (D-12) | `scanner/base.py` | result value object | request-response | `ScanResult`, `pipeline.py:137-145` | exact |
| `_constraint()` (D-13) | `scanner/sane_backend.py` | parsing utility | transform | the two duplicated blocks it replaces, `:330-335` / `:460-466` | exact (dedup) |
| `_MAX_ADF_PAGES` (D-04) | `scanner/sane_backend.py` | module constant | — | `_MIN_PAGE_BYTES`, `sane_backend.py:64-66` | exact |
| `resolution_range` (D-13) | `DeviceCapabilities`, `base.py:114-121` | dataclass field | — | `DeviceCapabilities.resolutions`, same class | exact |
| `_next_page_with_timeout` / `_acquire_pages` (complexity split) | `scanner/sane_backend.py` | private helpers | streaming | `_set_geometry` / `_maybe_crop`, `:95-156` (Phase 18's split of the same file) | exact |

### Modified

| Modified File | Role | Data Flow | Decisions |
|---------------|------|-----------|-----------|
| `src/saneless/scanner/sane_backend.py` (515 L) | backend adapter | streaming + request-response | D-03..D-06, D-09..D-13, D-19, Q8 |
| `src/saneless/scanner/base.py` (176 L) | ABC + vocabulary | — | D-01 (comment), D-12, D-13 |
| `src/saneless/auto_profiles.py` (279 L) | config generator | transform + file-I/O | D-02, D-14, D-15, D-16, Q9 |
| `src/saneless/pipeline.py` | orchestrator | batch | D-05 (policy lands here), D-12 consumption |
| `src/saneless/pdf.py` | assembler | file-I/O | D-12 (docstring at `:156` only) |
| `src/saneless/cli.py` | CLI presenter | request-response | D-13 rendering at `:211-223` |
| `tests/test_scanner.py` (~1200 L) | unit tests | — | D-17 (delete 3 doubles), all scanner decisions |
| `tests/test_pipeline.py` (~1620 L) | unit tests | — | D-12 (23 stub sites), D-17 |
| `tests/test_auto_profiles.py` (~390 L) | unit tests | — | D-14/D-15/D-16 (41 slug references) |
| `tests/test_cli.py`, `tests/test_worker.py` | unit tests | — | D-14 slug churn (6 + 1 references) |
| `tests/conftest.py` | fixtures | — | D-12 (`mock_scanner`, `:116-124`) |
| `tests/test_outcomes_e2e.py`, `tests/test_web.py`, `tests/test_web_state_rendering.py`, `tests/test_browser.py` | tests | — | D-12 only (implement/stub `scan_pages`) |
| `pyproject.toml` | config | — | D-18 marker |
| `.github/workflows/ci.yml` | CI config | — | D-18 filter |
| `CONTRIBUTING.md` + 4 docs pages | docs | — | D-14, M-14, M-15, D-18 |

---

## Pattern Assignments

### `tests/fake_sane.py` — NEW (test double, request-response)

**Analog:** `tests/test_scanner.py:49-141` (`MockSaneDev` + `MockSaneModule`) — the code being
replaced. Copy its *shape*; invert its *semantics* per RESEARCH Finding 7.

**⚠️ Import form — MEASURED, and the plan must pin it.**
`tests/__init__.py` exists (it contains only `"""Test suite for saneless."""`), so `tests` is a
package. I created a probe helper and two probe test modules and ran them:

| Import form in a test module | Result |
|---|---|
| `from tests.fake_sane import FakeSaneDev` | **passes** (`Pytest: 1 passed`) |
| `from fake_sane import FakeSaneDev` | **`ModuleNotFoundError: No module named 'fake_sane'`** — collection error |

RESEARCH.md § Recommended Structure says "A plain `tests/fake_sane.py` is importable by both" —
true, but **only** in the package-qualified form. The bare form is the natural thing to write and it
fails at collection. No test module in the suite does `from tests.X import ...` today, so this is
the first; there is no existing line to copy the import style from.

`tests/conftest.py:217-231` records the sibling failure and the reasoning behind it:

```python
@pytest.fixture(name="wait_for_state")
def _wait_for_state_fixture() -> Callable[..., Job]:
    """
    Hand the wait_for_state helper to a test module.

    pytest 9 imports test modules in ``importlib`` mode, so ``tests/`` never
    lands on ``sys.path`` and ``from conftest import wait_for_state`` raises
    ``ModuleNotFoundError``.  A fixture is the supported route for a conftest
    helper, and it is the only one that also keeps ty and pyrefly happy.
    """
    return wait_for_state
```

Note what this means: `conftest.py` is **not** importable, but `tests/fake_sane.py` **is** (as
`tests.fake_sane`), because the latter is a normal package submodule and the former is not. The
planner may put the fake in `tests/fake_sane.py` as D-17/RESEARCH specify — it just has to write
`from tests.fake_sane import ...`. The conftest-fixture route above is the fallback if that offends
`ty`/`pyrefly`, and it is the house precedent for sharing across test modules.

**Class shape to copy** (`tests/test_scanner.py:49-73`, trimmed):

```python
class MockSaneDev:
    """Mock SANE device returned by sane.open()."""

    def __init__(self) -> None:
        """Initialize mock device with default settings."""
        self.mode = "color"
        self.resolution = 300
        self.source = "Flatbed"
        self.tl_x: float = 0.0
        self.tl_y: float = 0.0
        self.br_x: float = 0.0
        self.br_y: float = 0.0
        self._cancel_called = False
        self._close_called = False
```

**Option-tuple shape to copy** (`tests/test_scanner.py:98-141`) — note the 9-tuple layout and that
today's fixture uses `"ADF"` and list-only constraints, both of which D-17 replaces with
`"Automatic Document Feeder"` and at least one `(min, max, step)`:

```python
    def get_options(self) -> list[tuple]:
        """
        Return sample SANE option tuples, or _options_impl if set.

        SANE option format:
        (index, name, title, desc, type, unit, size, cap, constraint)
        """
        if self._options_impl is not None:
            return self._options_impl
        return [
            (1, "source", "Scan source", "Source desc", 3, 0, 1, 5,
             ["Flatbed", "ADF", "ADF Duplex"]),
            (2, "resolution", "Resolution", "Res desc", 1, 4, 1, 5,
             [75, 150, 300, 600]),
            (3, "mode", "Scan mode", "Mode desc", 3, 0, 1, 5,
             ["color", "gray", "lineart"]),
        ]
```

**Module-fake shape to copy** (`tests/test_scanner.py:144-166`):

```python
class MockSaneModule:
    """Mock for the ``sane`` module (python-sane)."""

    def __init__(self) -> None:
        """Initialize mock module with default devices."""
        self.init_call_count = 0
        self._devices: list[tuple[str, str, str, str]] = [
            ("test:device:001", "TestVendor", "TestModel", "scanner"),
        ]
        self._mock_dev = MockSaneDev()

    def init(self) -> tuple[int, int, int]:
        """Simulate sane.init() and track call count."""
        self.init_call_count += 1
        return (1, 0, 3)

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        """Return the list of mock devices."""
        return self._devices

    def open(self, _device_id: str) -> MockSaneDev:
        """Return the shared mock device handle."""
        return self._mock_dev
```

**Monkeypatch seam to preserve** (`tests/test_scanner.py:16` and `:396-407`) — the fake module is
installed by replacing the module-level `sane` name, which `_ensure_sane()` (`sane_backend.py:46-52`)
leaves `None` until first use. Do not change this seam:

```python
import saneless.scanner.sane_backend as sane_backend_mod

@pytest.fixture
def mock_sane_module(monkeypatch: pytest.MonkeyPatch) -> MockSaneModule:
    """Patch sane module into sane_backend's namespace."""
    mock_sane = MockSaneModule()
    monkeypatch.setattr(sane_backend_mod, "sane", mock_sane)
    return mock_sane

@pytest.fixture
def sane_backend(mock_sane_module: MockSaneModule) -> SaneBackend:
    """Create a SaneBackend with mocked sane module."""
    _ = mock_sane_module  # side-effect: patches the sane module
    return SaneBackend()
```

**The anti-pattern to delete** (`tests/test_scanner.py:1026-1031`, `_NoGeometryDevice`) — this is the
exact inverse of the real `__setattr__` contract and is why M-15 shipped green:

```python
    def __setattr__(self, name: str, value: object) -> None:
        """Reject geometry attributes, accept everything else."""
        if name in {"tl_x", "tl_y", "br_x", "br_y"}:
            msg = f"Device does not support option '{name}'"
            raise AttributeError(msg)
        super().__setattr__(name, value)
```

The real library **stores silently** (RESEARCH § "The real `SaneDev.__setattr__` contract"). The
replacement models absence via `get_options()` not listing `tl-x`/`tl-y`/`br-x`/`br-y`, which is what
D-09's presence check reads.

---

### `tests/test_sane_hardware.py` — NEW (integration, marker-gated)

**Analog:** `tests/test_browser.py` — the only marker-gated, environment-dependent module in the
suite, and the precedent D-18 names explicitly.

**Marker application pattern** (`tests/test_browser.py:178-180`) — a class-level decorator, not
`pytestmark`:

```python
@pytest.mark.browser
class TestBrowserRendering:
    """PicoCSS and semantic HTML rendering tests."""
```

**Marker registration pattern** (`pyproject.toml:141-152`) — note `strict_markers = true` is live, so
an unregistered marker fails collection:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "browser: Playwright browser tests (requires chromium)",
]
addopts = ["-ra", "--strict-markers", "--strict-config"]
strict_markers = true
strict_config = true
xfail_strict = true
filterwarnings = ["error"]
timeout = 60
timeout_method = "signal"
```

**Session-fixture pattern** (`tests/test_browser.py:172-175`):

```python
@pytest.fixture(scope="session")
def browser_server_url(browser_server: _BrowserServer) -> str:
    """Return the live server's base URL, for tests that need nothing else."""
    return browser_server.url
```

**Gap:** `test_browser.py`'s session fixtures do **not** manipulate environment variables, and
`conftest.py`'s only env fixture is function-scoped (`clean_env`, `:89-94`, using the plain
`monkeypatch` fixture). RESEARCH's validated `pytest.MonkeyPatch.context()` at session scope has
**no in-repo analog** — see § No Analog Found.

**CI filter to widen** (`.github/workflows/ci.yml:46`) — one line, and per RESEARCH Finding 12 no new
apt package:

```yaml
      - run: uv run pytest -m "not browser"
```

**CONTRIBUTING paragraph to mirror** (`CONTRIBUTING.md:83-84`) — one short paragraph directly under
the five-checks table, plus the two `-m "not browser"` occurrences at `:67` and `:76` which must
both be widened or the documented gate stops matching CI:

```markdown
The `browser` marker deselects the Playwright tests, which need a real Chromium
install. Run those locally with `uv run pytest -m browser` when you touch the web UI.
```

---

### `GeometryUnit` (D-10) — `scanner/sane_backend.py` (vocabulary enum, transform)

**Analog:** `src/saneless/scanner/base.py:40-50` — same package, an enum property using
`match`/`assert_never`. This is the closest analog because Phase 21 D-03 forbids `scanner/` depending
on `vocabulary.py`, so the pattern must be copied rather than imported.

```python
class SourceKind(StrEnum):
    """What kind of scan source a SANE source name denotes."""

    FLATBED = "FLATBED"
    FEEDER = "FEEDER"
    FEEDER_DUPLEX = "FEEDER_DUPLEX"
    AUTO = "AUTO"
    UNKNOWN = "UNKNOWN"

    @property
    def uses_feeder(self) -> bool:
        """Whether this kind of source feeds a stack of sheets."""
        match self:
            case SourceKind.FEEDER | SourceKind.FEEDER_DUPLEX:
                feeds = True
            case SourceKind.FLATBED | SourceKind.AUTO | SourceKind.UNKNOWN:
                feeds = False
            case _:
                assert_never(self)
        return feeds
```

**Secondary analog** — `src/saneless/vocabulary.py:235-268` (`job_state_for`) carries the *docstring
paragraph* that justifies the form, and D-10 rests on the same measurement. Copy this reasoning into
`GeometryUnit`'s docstring rather than re-deriving it:

```python
    This is a ``match`` with ``assert_never`` and not a
    ``dict[ScanOutcome, JobState]`` on purpose.  A dict missing a member draws
    no diagnostic from either ``ty`` or ``pyrefly``; the same enum in a match
    is caught by both, at edit time, before a third outcome can fall silently
    through an ``else``.
```

**Note on the enum base:** every enum in this codebase is a `StrEnum` (`SourceKind`, `JobState`,
`ErrorCategory`, `ScanOutcome`, `ConnectionStatus`). `GeometryUnit` maps **integer** SANE codes
(`UNIT_NONE`=0 … `UNIT_MICROSECOND`=6), so it is the first `IntEnum`-shaped member set in the
project. There is no in-repo analog for an `IntEnum`; the `match`/`assert_never` half is what
transfers.

---

### `ScanBatch` / D-12's result object — `scanner/base.py` (result value object)

**Analog:** `src/saneless/pipeline.py:137-145` — a small dataclass of facts a stage computed,
named by CONTEXT.md § Reusable Assets as the precedent:

```python
@dataclass
class ScanResult:
    """How a scan pipeline run resolved, and how many pages it moved."""

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
```

Note it is a plain `@dataclass`, **not** `frozen=True`. RESEARCH Q3 recommends `frozen=True` for the
new object. There is no frozen-dataclass precedent in `scanner/` or `pipeline.py`; `DeviceInfo`,
`DeviceCapabilities` and `ScanSettings` (`base.py:104-132`) are all plain `@dataclass`. Either
choice is defensible — the plan should state which and why, because copying the analog verbatim
yields a *mutable* object.

**Field-ordering trap to copy the awareness of** (`pipeline.py:119-124`, the `PipelineRequest`
docstring immediately above `ScanResult`):

```python
    It is defaulted rather than required, and sits with the other defaulted
    fields: moving it into the non-default block above would reorder the
    dataclass and break positional construction.
```

#### D-12 blast radius — MEASURED, and larger than RESEARCH.md estimated

RESEARCH Q3 says "~10 fakes/stubs". The real counts, by `grep` over `tests/ src/`:

| Category | Count | Locations |
|---|---|---|
| Production call sites (`list(scanner.scan_pages(...))`) | **3** | `pipeline.py:577` (duplex pass A), `:597` (pass B), `:628` (`_scan_simplex`) |
| ABC + implementation | **2** | `scanner/base.py:162-175`, `sane_backend.py:430-515` |
| `MagicMock(spec=ScannerBackend)` construction sites | **23** | `conftest.py:119`; `test_outcomes_e2e.py:449`; `test_pipeline.py:80, 147, 371, 402, 429, 460, 495, 546, 581, 609, 646, 682, 715, 747, 786, 825, 858, 968, 1002, 1082, 1616` |
| `scan_pages.return_value` / `.side_effect` assignments | **25** | `conftest.py:123`; `test_outcomes_e2e.py:450`; `test_pipeline.py:81, 148, 305, 334, 372, 403, 430, 461, 496, 547, 582, 610, 647, 683, 716, 748, 787, 826, 859, 969, 1003, 1083, 1617` |
| Concrete classes defining `scan_pages` | **6** | `test_scanner.py:238` (`MockBackend`), `test_browser.py:96` (`_BrowserTestScanner`), `test_web.py:63`, `test_web_state_rendering.py:79`, `test_cli.py:126` (`MockSaneBackend`), `test_cli.py:240` (`FailScanner`) |
| `list(...scan_pages(...))` assertions in `test_scanner.py` | **~40** | `test_scanner.py:385` through `:1177` |
| **Total `scan_pages` references** | **89** across 8 test files + 3 production sites | |

**Highest-leverage single edit:** `tests/conftest.py:116-124`. The `mock_scanner` fixture is
consumed by many tests by name, so changing it once fixes every consumer that does not build its own
mock:

```python
@pytest.fixture
def mock_scanner() -> MagicMock:
    """Return a mock ScannerBackend that yields a single image with content."""
    scanner = MagicMock(spec=ScannerBackend)
    img = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 90], fill="black")
    scanner.scan_pages.return_value = iter([img])
    return scanner
```

**Two-call stub pattern** (manual duplex) that must keep working — `test_pipeline.py:461` and
`test_outcomes_e2e.py:450`. Any new signature has to preserve "call A then call B return different
things":

```python
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]
```
```python
    scanner.scan_pages.side_effect = [iter(_pages(count)) for count in scan_passes]
```

**The ABC to change** (`scanner/base.py:161-175`) — note the `Yields:` docstring section, which
becomes `Returns:` if RESEARCH Q3(a) is taken:

```python
    @abstractmethod
    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """
        Acquire pages from scanner.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Yields:
            PIL Image objects for each scanned page.

        """
```

**Consumption sites in `pipeline.py`** — the three that must unpack the batch:

```python
# pipeline.py:576-578 (duplex pass A)
    # Pass A: scan fronts
    front_pages = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Pass A: scanned %d front page(s)", len(front_pages))

# pipeline.py:595-598 (duplex pass B)
    notify(PipelineEvent.SCANNING_REVERSE)
    back_pages = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Pass B: scanned %d back page(s)", len(back_pages))

# pipeline.py:628-629 (simplex)
    images = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Scanned %d page(s)", len(images))
```

**The two `dpi=` sites D-12 redirects** — `pipeline.py:747-751` (duplex-mismatch recovery) and
`:782-790` (the main path). The comment at `:782-784` is now wrong and must be rewritten, not just
the argument:

```python
                    _DeliveryContext(
                        dpi=profile.resolution,
                        failed_dir=settings.output.failed_dir,
                        task_timeout=settings.output.paperless_task_timeout,
                    ),
```
```python
        # profile.resolution is the authoritative DPI: the same value
        # crop_to_paper_size uses for its crop arithmetic, so the cropped
        # shape and the declared page size cannot disagree.
        pdf_path = assemble_pdf(
            filtered,
            tmp_path,
            filename=build_pdf_filename(request.job_id, request.title),
            dpi=profile.resolution,
        )
```

**Where D-07's skip count must NOT go** (`pipeline.py:830-836`) — `pages_removed` is blank-page
detection and CONTEXT.md D-07 forbids folding the integrity count into it:

```python
        result = ScanResult(
            outcome=outcome,
            pages_scanned=len(images),
            pages_removed=len(images) - len(filtered),
            pages_uploaded=len(filtered),
            warning=warning,
        )
```

Note `pages_scanned=len(images)` is the line CONTEXT.md D-07 cites: it already excludes
backend-skipped pages, which is the quiet lie the count exists to remove.

**The seam `pdf.py` already documents** (`pdf.py:147-157`) — the only change in that file is this
docstring paragraph; the function body is untouched:

```python
    ``dpi`` is likewise supplied by the caller, from ``profile.resolution``,
    and is deliberately **not** read from the images. On this path PIL carries
    no DPI at all: images arrive from ``dev.snap()`` and go through
    ``crop_to_paper_size``, whose ``Image.crop()`` returns a fresh image whose
    ``.info`` is measured as ``{}``. ...
    Phase 24 adds device read-back; the line that changes is the ``dpi``
    argument at the call sites in ``pipeline.py``, not anything in here.
```

---

### `_constraint()` (D-13) — `scanner/sane_backend.py` (parsing utility, transform)

**Analog:** the two blocks it replaces. Both handle only `isinstance(constraint, list)` — the N-01
defect, duplicated.

**Copy 1** (`sane_backend.py:323-335`, inside `get_capabilities`):

```python
            for opt in raw_options:
                # SANE option tuple:
                # (index, name, title, desc, type, unit, size, cap, constraint)
                if len(opt) < 9:
                    continue
                name = opt[1]
                constraint = opt[8]
                if name == "source" and isinstance(constraint, list):
                    sources = [str(s) for s in constraint]
                elif name == "resolution" and isinstance(constraint, list):
                    resolutions = [int(r) for r in constraint]
                elif name == "mode" and isinstance(constraint, list):
                    modes = [str(m) for m in constraint]
```

**Copy 2** (`sane_backend.py:460-466`, inside `scan_pages`) — note it `break`s on the first `source`
option, and uses `len(opt) >= 9` where copy 1 uses `len(opt) < 9: continue`:

```python
            for opt in raw_options:
                if len(opt) >= 9 and opt[1] == "source":
                    has_source_option = True
                    constraint = opt[8]
                    if isinstance(constraint, list):
                        available_sources = [str(s) for s in constraint]
                    break
```

**Critical difference the helper must preserve:** copy 2 sets `has_source_option = True` on option
*presence*, independently of the constraint shape. A `_constraint()` that returns only the parsed
constraint loses that distinction, and `scan_pages:469-487` branches on it. The helper needs to
report presence and constraint separately, or `scan_pages` keeps its own presence test.

**D-09's presence check reuses the same loop.** `_set_geometry` currently takes no option list
(`:95`); the hyphen/underscore asymmetry (RESEARCH § "Presence check before assignment") means the
presence lookup uses `"tl-x"` while the assignment uses `dev.tl_x`. `sane_backend.py` has no existing
constant for geometry option names — `_set_geometry:118-121` writes the attribute names inline:

```python
    try:
        dev.tl_x = 0.0
        dev.tl_y = 0.0
        dev.br_x = width_mm
        dev.br_y = height_mm
    except Exception:
        logger.warning(
            "Scanner does not support geometry options, will crop after scanning",
        )
        return False
```

That bare `except Exception` at `:122` is M-15's second half — D-09 adds the exception to the log
line. The `SaneDevice` Protocol (`:215-231`) is the one place both spellings could be reconciled;
today it declares only the underscore attribute names.

---

### `_MAX_ADF_PAGES` (D-04) — `scanner/sane_backend.py` (module constant)

**Analog:** `sane_backend.py:60-66` — the house style is a typed module constant with a comment that
shows the arithmetic justifying the value. RESEARCH Q1 asks for the cap's honest bound to be written
down; this is the form to write it in.

```python
# Per-page timeout: 2x a generous single-page scan estimate (60s at 600 DPI).
# At 300 DPI typical scan is ~10-15s, so 120s is very conservative.
_DEFAULT_PAGE_TIMEOUT_SECONDS: float = 120.0

# Minimum raw image data size in bytes. A valid scanned page at any reasonable
# resolution will be well above this. Catches corrupt/truncated pages.
_MIN_PAGE_BYTES: int = 10_000  # 10 KB
```

**Constants to delete** (D-05) — `sane_backend.py:68-74`, and with them checks 3 and 4 of
`_validate_page_image` (`:182-210`):

```python
# Thresholds for pure white/black detection at the scanner level.
# These are intentionally extreme (tighter than the configurable empty-page
# thresholds in pages.py) to only catch obviously invalid images.
_SCANNER_WHITE_MEAN_THRESHOLD: float = 254.0
_SCANNER_WHITE_STDDEV_THRESHOLD: float = 1.0
_SCANNER_BLACK_MEAN_THRESHOLD: float = 1.0
_SCANNER_BLACK_STDDEV_THRESHOLD: float = 1.0
```

**Checks that survive** (`_validate_page_image:169-180`) — copy this logging shape for D-06's
skip-and-count, which needs the page number and the reason:

```python
    # Check 1: Nonzero dimensions
    if page_image.size[0] == 0 or page_image.size[1] == 0:
        logger.warning(
            "Page %d: zero dimensions (%s), skipping", page_num, page_image.size
        )
        return False

    # Check 2: Minimum file size (raw pixel data)
    raw_size = len(page_image.tobytes())
    if raw_size < _MIN_PAGE_BYTES:
        logger.warning("Page %d: too small (%d bytes), skipping", page_num, raw_size)
        return False
```

Also delete `ImageStat` from the imports at `sane_backend.py:24` once checks 3 and 4 go — it has no
other user in the module.

---

### `_scan_adf_pages` helper split — `scanner/sane_backend.py` (12/12 branches, zero headroom)

**Analog:** `_set_geometry` (`:95-133`) and `_maybe_crop` (`:136-156`) — Phase 18's extraction from
this same function, in this same file. Both are **module-level private functions taking `dev` or
`image` plus explicit arguments**, not methods; both have full `Args:`/`Returns:` docstrings; and
`_maybe_crop` uses a keyword-only boolean (`*, geometry_set: bool`) because ruff's `FBT` rules are
enabled and forbid positional booleans:

```python
def _maybe_crop(
    image: Image.Image,
    settings: ScanSettings,
    *,
    geometry_set: bool,
) -> Image.Image:
    """
    Apply Pillow crop fallback when geometry options were not set.

    Args:
        image: Scanned page image.
        settings: Scan settings with paper_size and resolution.
        geometry_set: Whether SANE geometry was already applied.

    Returns:
        Cropped image, or original if no crop needed.

    """
    if settings.paper_size != "full" and not geometry_set:
        return crop_to_paper_size(image, settings.paper_size, settings.resolution)
    return image
```

**Code to delete** (D-03) — the unreachable guard (`:371-375`) and the first-page special case
(`:403-410`):

```python
        feeder_empty_msg = "No paper detected in feeder"
        try:
            iterator = dev.multi_scan()
        except Exception as exc:
            raise FeederEmptyError(feeder_empty_msg) from exc
```
```python
                except Exception as exc:
                    error_str = str(exc).lower()
                    if page_num == 0:
                        raise FeederEmptyError(feeder_empty_msg) from exc
                    # After first page, end-of-feed signals
                    if "out of documents" in error_str or "no docs" in error_str:
                        break
                    raise
```

**Code that survives as the only feeder-empty path** (`:427-428`):

```python
        if page_num == 0:
            raise FeederEmptyError(feeder_empty_msg)
```

**Timeout block to extract into `_next_page_with_timeout`** (`:382-396`) — RESEARCH's recommended
split; keep it verbatim, only relocated. Note its `finally` (`:421-425`) deletes the iterator before
cancel and must move or stay with the loop:

```python
                    future = executor.submit(next, iterator)
                    try:
                        page_image = _as_image(future.result(timeout=timeout_per_page))
                    except FuturesTimeoutError as timeout_exc:
                        logger.error(
                            "Page %d timed out after %.0fs",
                            page_num + 1,
                            timeout_per_page,
                        )
                        timeout_msg = (
                            f"Page {page_num + 1} timed out after "
                            f"{timeout_per_page:.0f}s"
                        )
                        raise ScanError(timeout_msg) from timeout_exc
```

**`PLR0913` precedent** (RESEARCH § Complexity Budget): max-args is 5 and already load-bearing —
`JobResult` and `_DeliveryContext` exist solely to stay under it. `_DeliveryContext` at
`pipeline.py:747-751` is the shape to copy if a helper needs more than five arguments.

---

### `scan_pages` option ordering + read-back (D-11, Q8) — `scanner/sane_backend.py:483-501`

**Current code** — the ordering D-11 inverts and the `== "Auto"` comparison Q8 replaces:

```python
            # Set device options
            dev.mode = settings.mode
            dev.resolution = settings.resolution
            if has_source_option:
                dev.source = effective_source

            # Set scan area geometry for paper size constraint (D-01)
            geometry_set = _set_geometry(dev, settings.paper_size)

            use_adf = classify_source(effective_source).uses_feeder

            # D-04: Override for "Auto" source using config-driven routing
            if effective_source == "Auto":
                use_adf = settings.auto_source_mode == "adf"
                logger.info(
                    "Auto source routing: auto_source_mode='%s', use_adf=%s",
                    settings.auto_source_mode,
                    use_adf,
                )
```

**Analog for the fix** — `classify_source` is already called on the line directly above (`:492`).
Q8's change is to make `:495` consult the same classifier:
`if classify_source(effective_source) is SourceKind.AUTO:`. Note `SourceKind` is **not currently
imported** by `sane_backend.py` — `:28-34` imports only `classify_source`:

```python
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScannerBackend,
    ScanSettings,
    classify_source,
)
```

**Type lie to fix** (`SaneDevice` Protocol, `:215-231`) — `resolution: int` where the real device
returns `float` (RESEARCH Finding 9). CLAUDE.md forbids annotating around it:

```python
class SaneDevice(Protocol):
    """Protocol describing the SANE device handle interface."""

    mode: str
    resolution: int
    source: str
    tl_x: float
    tl_y: float
    br_x: float
    br_y: float

    def get_options(self) -> list: ...
    def start(self) -> None: ...
    def snap(self) -> Image.Image: ...
    def multi_scan(self) -> Iterator[Image.Image]: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...
```

The Protocol also has **no `area` attribute**, which D-19's geometry read-back needs. Adding it is
part of D-19's cost and the fakes must provide it.

---

### `auto_profiles.py` — D-02, D-14, D-15, D-16, Q9

**`_slugify` to harden (D-15)** — `auto_profiles.py:33-44`. Measured today: `/` passes straight
through, and whitespace-only input yields `--`:

```python
def _slugify(lower: str) -> str:
    """
    Slugify an already-lowercased source name.

    Args:
        lower: Lowercased SANE source name.

    Returns:
        The name with spaces and underscores replaced by hyphens.

    """
    return lower.replace(" ", "-").replace("_", "-")
```

**`source_to_slug` to collapse (D-14)** — `auto_profiles.py:47-82`. All four hard-coded slugs go; the
`"back"` special case and its comment go with them. After D-14 the whole `match` collapses to
`return _slugify(source.lower())`, and `classify_source`/`SourceKind` may no longer be needed by this
function at all — but D-02/Q9 still need both in `generate_profiles`, so keep the import:

```python
    lower = source.lower()
    match classify_source(source):
        case SourceKind.AUTO:
            slug = "auto-scan"
        case SourceKind.FLATBED:
            slug = "flatbed-scan"
        case SourceKind.FEEDER_DUPLEX:
            slug = "adf-duplex"
        case SourceKind.FEEDER:
            # Not a thin passthrough: "ADF Back" classifies as FEEDER because
            # it IS a feeder for routing purposes, but it must not slug to
            # "adf-simplex" or it collides with "ADF Front" (the N-09 defect).
            # "Which scan path do I take?" and "what do I name this profile?"
            # are different questions with different equivalence classes.
            slug = _slugify(lower) if "back" in lower else "adf-simplex"
        case SourceKind.UNKNOWN:
            slug = _slugify(lower)
        case unhandled:
            assert_never(unhandled)
    return slug
```

**The three ad-hoc rules in `generate_profiles`** — `auto_profiles.py:178-200`. D-02 names `:182`
and `:193`; RESEARCH Finding 5 / Q9 adds `:181`. All three are in one 20-line span:

```python
    for source in capabilities.sources:
        slug = source_to_slug(source)
        auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
        if source.lower() == "auto":                                    # <- Q9 (:181)
            has_flatbed = any("flatbed" in s.lower() for s in capabilities.sources)  # <- D-02 (:182)
            auto_source_mode = "adf" if not has_flatbed else "flatbed"
        profiles[slug] = ProfileConfig(
            source=source,
            resolution=resolution,
            mode=mode,
            auto_generated=True,
            auto_source_mode=auto_source_mode,
        )

    # Set default to flatbed if available
    flatbed_sources = [s for s in capabilities.sources if "flatbed" in s.lower()]  # <- D-02 (:193)
    if flatbed_sources:
```

**Collision tie-break (D-15/Q5) has no analog.** `profiles[slug] = ...` at `:184` is an unguarded
dict assignment — a collision silently overwrites. Nothing in the codebase does deduplicated naming
with a numeric suffix. See § No Analog Found.

**`pick_closest_resolution` to extend (D-13)** — `auto_profiles.py:85-102`. The empty-list fallback
at `:100-101` is exactly the N-01 symptom (a range-reporting device silently gets 300):

```python
    if not resolutions:
        return target
    return min(resolutions, key=lambda r: abs(r - target))
```

**`write_profiles_to_config` to add the prune to (D-16)** — `auto_profiles.py:252-278`. The loop only
`continue`s and assigns; nothing deletes. `profiles_section` is a `cast("dict[str, object]", ...)`
over a tomlkit table, so a prune step reading `auto_generated` off each existing table has to narrow
that `object` back to something indexable — the `cast` at `:260` is the pattern to extend:

```python
    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()

    if "profiles" not in doc:
        doc.add("profiles", tomlkit.table(is_super_table=True))

    profiles_section = cast("dict[str, object]", doc["profiles"])

    written: list[str] = []
    for name, profile in profiles.items():
        if name in profiles_section and not force:
            continue
        profile_table = tomlkit.table()
        profile_table.add("source", profile.source)
        profile_table.add("resolution", profile.resolution)
        profile_table.add("mode", profile.mode)
        if profile.auto_source_mode != "flatbed":
            profile_table.add("auto_source_mode", profile.auto_source_mode)
        auto_generated_flag = True
        profile_table.add("auto_generated", auto_generated_flag)
        profiles_section[name] = profile_table
        written.append(name)

    config_path.write_text(tomlkit.dumps(doc))
    return written
```

The flag the prune keys on is `ProfileConfig.auto_generated` (`config.py:79`, `bool = False`), and
`:272-273` is where it is written — note the value goes through a named local, which is how ruff's
`FBT` rules were satisfied for a positional boolean argument.

**Existing write-path tests to model D-16's test on:** `tests/test_auto_profiles.py:294`
(`test_skip_existing_without_force`), `:316` (`test_overwrite_with_force`), `:338`
(`test_writes_auto_source_mode_adf`), `:370` (`test_auto_generated_profiles_omit_paper_size`). All
four are `(self, tmp_path: Path)` tests that write a TOML file and re-read it.

---

### `cli.py:211-223` — D-13 rendering (presenter, request-response)

```python
    if capabilities:
        click.echo()
        for d in device_list:
            caps = scanner.get_capabilities(d.name)
            click.echo(f"Capabilities for {d.name}:")
            click.echo(f"  Sources: {', '.join(caps.sources)}")
            click.echo(f"  Resolutions: {', '.join(str(r) for r in caps.resolutions)}")
            click.echo(f"  Modes: {', '.join(caps.modes)}")
            if caps.raw_options:
                click.echo("  Raw options:")
                for opt in caps.raw_options:
                    if len(opt) >= 2:
                        click.echo(f"    {opt[1]}")
```

The `Resolutions:` line at `:217` is the one that prints blank on a range-reporting device
(RESEARCH Finding 4). The `if caps.raw_options:` guard at `:219` is the analog for conditionally
rendering whichever of `resolutions` / `resolution_range` is populated.

**Existing CLI test to extend:** `tests/test_cli.py` asserts on `auto-profiles` output at `:755-756`
and `:801` using the old slugs — those are part of D-14's churn, not D-13's.

---

### Test churn — D-14's exact blast radius (MEASURED)

Per-file counts of the four slugs `adf-simplex` / `adf-duplex` / `flatbed-scan` / `auto-scan`
(`.pyc` files excluded):

| File | Count | Notes |
|---|---|---|
| `tests/test_auto_profiles.py` | **41** | CONTEXT.md D-14 estimated "roughly eight assertions" — it is 41 references across ~25 assertions. `:26-83` (the whole `TestSourceToSlug` class), `:172-252`, `:342`, `:358` |
| `tests/test_cli.py` | 6 | `:755, :756, :772, :779, :791, :801` |
| `src/saneless/auto_profiles.py` | 5 | `:66, :68, :70, :74, :77` |
| `docs/how-to/configure-scan-profiles.md` | 2 | `:91` (`[profiles.auto-scan]`) + `:77` (link text, not a slug) |
| `tests/test_worker.py` | 1 | `:914` |
| `tests/test_scanner.py` | 1 | `:326` (a doc reference, not a slug assertion) |
| `docs/reference/configuration.md` | 1 | `:127` |
| `docs/explanation/architecture.md`, `docs/index.md`, `docs/getting-started/first-cli-scan.md`, `docs/getting-started/first-web-ui-scan.md` | 1 each | link text (`set-up-adf-duplex.md`), **not** slugs — do not rename |

**Test-assertion style to update** (`tests/test_auto_profiles.py:26-36`) — one assertion per method,
docstring naming the mapping. After D-14 these become identity assertions:

```python
    def test_flatbed(self) -> None:
        """Flatbed source maps to flatbed-scan slug."""
        assert source_to_slug("Flatbed") == "flatbed-scan"

    def test_adf(self) -> None:
        """ADF source maps to adf-simplex slug."""
        assert source_to_slug("ADF") == "adf-simplex"
```

**Two tests whose *reasoning* D-14 makes obsolete, not just their values** — `:66-74` and `:80-83`.
Deleting them loses the recorded reasoning; rewriting them to assert the new behaviour keeps it:

```python
    def test_auto_is_matched_exactly_not_as_a_substring(self) -> None:
        """
        "Automatic Document Feeder" is a feeder, not an Auto source (CTR-04).

        The name begins with the letters "auto"; a substring test would slug it
        "auto-scan" and route a stack of pages down the single-page path.
        """
        assert source_to_slug("Automatic Document Feeder") == "adf-simplex"
        assert source_to_slug("Auto") == "auto-scan"

    def test_adf_back_fallback(self) -> None:
        """ADF Back produces a slug that is not adf-simplex or adf-duplex."""
        slug = source_to_slug("ADF Back")
        assert slug not in ("adf-simplex", "adf-duplex")
```

`test_adf_back_fallback` is the N-09 guard D-14 generalises; after D-14 it should assert
`source_to_slug("ADF Back") == "adf-back"` and gain a sibling proving `"ADF Front"` differs.

---

### Docs

**M-15's unreachable promise** — `docs/how-to/configure-scan-profiles.md:115-116`:

```markdown
When your scanner supports SANE geometry options, saneless sets the scan area at the hardware
level. Otherwise, it crops the image after scanning.
```

**M-14's untrue promise** — `docs/explanation/empty-page-detection.md:58-68`:

```markdown
## Disabling Empty Page Detection

If you want to keep all scanned pages regardless of content, disable detection in the profile:

...

This is useful when scanning documents where blank pages are intentional (such as forms with designated blank backs).
```

**D-14's renamed slugs** — `docs/how-to/configure-scan-profiles.md:91` and
`docs/reference/configuration.md:127`, both the same block:

```toml
[profiles.auto-scan]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```

**⚠️ A slug lie CONTEXT.md does not name** — `docs/how-to/configure-scan-profiles.md:128`:

```markdown
This creates profiles like `flatbed-color-300`, `adf-gray-150`, etc. based on what your scanner hardware actually supports.
```

`flatbed-color-300` and `adf-gray-150` are **not** names `source_to_slug` has ever produced. This
sentence is false today (actual: `flatbed-scan`, `adf-simplex`) and still false after D-14 (actual:
`flatbed`, `adf`). It sits four lines from a sentence D-14 already requires editing, and the standing
phase rule — correct the written description of whatever behaviour you change — reaches it.

**A doc that D-14 accidentally makes *more* correct** — `docs/how-to/set-up-adf-duplex.md:17` and
`:32` hand-write `[profiles.adf]` (source `"ADF"`) and `[profiles.duplex]` (source `"ADF Duplex"`).
After D-14, `"ADF"` generates exactly `adf`, so the hand-written example and the generated name
converge. Worth one sentence rather than a rewrite. `docs/reference/cli-commands.md:51` describes
`--capabilities` as "Show raw SANE options for each device", which D-13 widens.

**`docs/how-to/scanner-host-discovery.md`** — read end to end. Its claims are all about
`SANE_NET_HOSTS` and container networking, and they match `sane_backend.py:250-257` exactly. Nothing
in this phase changes them. The phase goal names "the scanner-discovery page"; the mapped finding is
that **it needs no correction** — the planner should record that rather than hunt for one.

---

## Shared Patterns

### Total lookups: `match` + `assert_never`, never a `dict`
**Source:** `src/saneless/scanner/base.py:43-50`; reasoning paragraph at `vocabulary.py:245-249`.
**Apply to:** `GeometryUnit` (D-10), and any enum dispatch added this phase.

### Module constant with the arithmetic in the comment
**Source:** `src/saneless/scanner/sane_backend.py:60-66`.
**Apply to:** `_MAX_ADF_PAGES` (D-04), and to `_MIN_PAGE_BYTES`'s retained-justification comment (Q2).

### Logging: lazy `%s` interpolation, never f-strings
**Source:** `sane_backend.py:171-173`, `:192-197`, `:387-391`. Ruff's `G` rules are enabled and
reject f-strings in logging calls. Every new WARNING this phase adds (D-06 skip, D-09 missing option,
D-10 unsupported unit, D-11 clamped DPI, D-15 slug collision, D-19 clamped area) follows this form:

```python
        logger.warning(
            "Page %d: pure white (mean=%.1f, stddev=%.1f), skipping",
            page_num,
            mean_val,
            stddev_val,
        )
```

### Error messages: build into a named local, then raise
**Source:** `sane_backend.py:392-396`, `:477-481`; `pipeline.py:335-336`. Ruff's `EM` rules forbid a
string literal or f-string directly inside `raise`. D-03's `f"Scanner error on page {n}: {exc}"` and
D-04's cap message both need this shape:

```python
                    msg = (
                        f"Device does not support source '{effective_source}'. "
                        f"Available: {available_sources}"
                    )
                    raise ScanError(msg)
```

### Exception chaining and the hierarchy
**Source:** `src/saneless/exceptions.py:19-45`. `FeederEmptyError` subclasses `ScanError`, so an
`except ScanError` catches both and order matters (`vocabulary.classify_error:362-370` documents
why). D-03 converts to `ScanError`, not `FeederEmptyError`, and must use `raise ... from exc`.

### Keyword-only booleans
**Source:** `_maybe_crop(..., *, geometry_set: bool)` at `sane_backend.py:136-141`;
`write_profiles_to_config(..., *, force: bool)` at `auto_profiles.py:231-236`. Ruff `FBT` is on.

### Docstrings on everything public
**Source:** every function in the files above. Ruff `D` rules are on, including on test files —
`pyproject.toml:128-133` exempts only `S101`, `ARG`, `S104/S105/S106` for `tests/**`. The new fake
module, every fake class and every fake method needs a docstring, and so does the RED test file.

### Concrete fakes, not `MagicMock`
**Source:** `tests/test_browser.py:74-101` (`_BrowserTestScanner`), `tests/test_scanner.py:215-242`
(`MockBackend`). Both subclass `ScannerBackend` explicitly:

```python
class _BrowserTestScanner(ScannerBackend):
    """Concrete scanner stub for browser tests."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return a single fake device."""
        return [
            DeviceInfo(
                name="test:browser:001",
                vendor="Test",
                model="Browser Scanner",
                device_type="virtual",
            ),
        ]
```

Note `test_cli.py:234-243` (`FailScanner`) and `test_web.py:63` do **not** subclass the ABC — they
duck-type. Those will not be caught by a type checker when the ABC signature changes, so D-12's
sweep has to find them by grep, not by running `ty`.

---

## No Analog Found

The planner should use RESEARCH.md's measured shapes for these, not a codebase pattern.

| Item | Role | Data Flow | Reason |
|------|------|-----------|--------|
| Session-scoped env-var fixture via `pytest.MonkeyPatch.context()` (D-18) | test fixture | — | `conftest.py:89-94` is the only env fixture and it is function-scoped using the plain `monkeypatch` fixture; `test_browser.py`'s session fixtures manipulate no environment. RESEARCH § Code Examples has the validated shape. |
| Deterministic collision tie-break for normalised names (D-15/Q5) | utility | transform | `auto_profiles.py:184` assigns `profiles[slug]` unguarded — a collision silently overwrites. No dedup-with-suffix logic exists anywhere in `src/`. |
| An `IntEnum` over external numeric codes (D-10) | vocabulary | — | All five project enums are `StrEnum`. The `match`/`assert_never` half transfers; the integer-valued member set does not. |
| A `frozen=True` dataclass (D-12, if RESEARCH Q3's recommendation is taken) | value object | — | `ScanResult`, `DeviceInfo`, `DeviceCapabilities`, `ScanSettings`, `PipelineRequest` are all plain `@dataclass`. Copying the analog verbatim yields a mutable object. |
| Driving a real external device under a marker (D-18) | integration test | file-I/O | `test_browser.py` gates on a marker but talks to an in-process server it starts itself. No test in the suite touches real system libraries or `/usr/lib`. |
| Reading a device value back after writing it (D-11, D-19) | backend | request-response | Nothing in `src/` currently reads back any value it assigned. This is the phase's core new idea and has no precedent by construction. |

---

## Measured Corrections to Upstream Documents

Recorded so the planner does not act on the superseded version.

1. **`tests/fake_sane.py` import form.** RESEARCH § Recommended Structure: "A plain
   `tests/fake_sane.py` is importable by both without pytest collecting it as a test". True only as
   `from tests.fake_sane import ...`. The bare `from fake_sane import ...` raises
   `ModuleNotFoundError` at collection — measured both ways against this repo's real pytest config.
   `tests/__init__.py` exists, which is what makes the package-qualified form work.

2. **D-12 blast radius.** RESEARCH Q3: "~10 fakes/stubs (`tests/conftest.py:119`,
   `test_browser.py:96`, `test_cli.py:126` and `:240`, `test_scanner.py:238`, plus ~15
   `MagicMock(spec=ScannerBackend)` sites)". Measured: **23** `MagicMock(spec=ScannerBackend)`
   construction sites, **25** `scan_pages` stub assignments, **6** concrete classes defining
   `scan_pages`, **~40** `list(...scan_pages(...))` call sites in `test_scanner.py` alone, and **89**
   total `scan_pages` references across 8 test files. Full enumeration in § D-12 blast radius above.
   This remains the highest-risk item in the phase and it is ~2.5× the estimate.

3. **D-14 test churn.** CONTEXT.md D-14: "roughly eight assertions in `tests/test_auto_profiles.py`".
   Measured: **41** slug references in that file, concentrated in `TestSourceToSlug` (`:26-83`) and
   the `generate_profiles` tests (`:172-252`), plus `:342` and `:358`.

4. **An unnamed doc lie.** `docs/how-to/configure-scan-profiles.md:128` claims auto-profiles creates
   names like `flatbed-color-300` / `adf-gray-150`. No version of `source_to_slug` has produced
   those. False before and after D-14.

5. **`docs/how-to/scanner-host-discovery.md` needs no change.** Read end to end; every claim matches
   `sane_backend.py:250-257`. The phase goal names it, so record the finding rather than inventing an
   edit.

---

## Metadata

**Analog search scope:** `src/saneless/` (all modules), `tests/` (all modules), `docs/`,
`pyproject.toml`, `.github/workflows/ci.yml`, `CONTRIBUTING.md`.
**Files read:** 22 source/test/config files; 6 doc sections; 1 executed pytest probe (created and
removed `tests/_probe_helper.py`, `tests/test_zz_probe_pkgstyle.py`, `tests/test_zz_probe_flat.py` —
verified removed, no source file modified).
**Pattern extraction date:** 2026-09-13
**Tooling note:** Serena's language server is reported broken on this project; all navigation used
`Read`, `Grep` and `Glob` per the orientation brief. Serena was not invoked.
