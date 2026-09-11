# Stack Research

**Domain:** SANE scanner → paperless-ngx bridge (Python 3.14 appliance; FastAPI + HTMX web UI + Click CLI + threaded worker)
**Milestone:** v2.0 "Prep for release" — hardening driven by `.planning/reviews/2026-09-09-code-review.md`
**Researched:** 2026-09-09
**Confidence:** HIGH (most claims executed against the installed venv or verified against upstream registries; exceptions marked inline)

---

## Headline verdict

**v2.0 needs zero new runtime dependencies.**

Every capability the code review demands — thread-safe SQLite, sync routes, bounded queue with 429, strict config, atomic TOML writes, correct PDF DPI, page spooling, vendored front-end assets, CSRF protection, hermetic tests, `saneless doctor`, truthful SANE fakes — is satisfied by the Python standard library, the packages already pinned, or build/CI configuration.

The dependency work for this milestone is **subtraction and pinning**, not addition:

| Action | Item | Why |
|---|---|---|
| **Remove** | `pydantic-settings[toml]` extra → plain `pydantic-settings` | On Python ≥ 3.11 the TOML source imports stdlib `tomllib`; the extra pulls `tomli` 2.4.0 into every install and container layer for nothing (executed: `pydantic_settings/sources/providers/toml.py` lines 28-41) |
| **Remove** | `httpx` from `[dependency-groups].dev` | Already a runtime dependency; duplicate listing (N-30) |
| **Remove** | `"License :: OSI Approved :: MIT License"` classifier | PEP 639 forbids it alongside `License-Expression`; PyPI **MUST** reject the upload (N-30 — see "Packaging" below) |
| **Bump** | 8 runtime/dev pins (table below) | Several current pins are 1-3 majors behind; two carry fixes the milestone needs |
| **Vendor** | htmx 2.0.10, PicoCSS 2.1.1 | N-21 — offline LAN is the product's stated environment |
| **Pin** | 6 GitHub Actions to commit SHAs | M-26, N-31 |
| **Add (dev only, optional)** | `zizmor`, `pytest-timeout` | Workflow security audit; CI hang guard |

---

## Recommended Stack (deltas only)

The base stack is fixed and validated (see `.planning/PROJECT.md` → Validated). Only changes are listed.

### Runtime dependency changes

| Package | Current pin | Locked | Recommended pin | Why change |
|---|---|---|---|---|
| `pydantic-settings[toml]` | `>=2.13.1` | 2.13.1 | **`pydantic-settings>=2.15.0`** (drop `[toml]`) | 2.15.0 is current (2026-08-07). Dropping the extra removes `tomli` from the runtime closure. Nothing in the milestone needs a 2.14/2.15 feature — the bump is hygiene, the extra removal is the real win. |
| `pydantic` (transitive) | — | 2.12.5 | leave transitive | `SecretStr`, `AnyHttpUrl`, `ConfigDict(extra="forbid")`, nested `extra_forbidden` error `loc` tuples all verified working on 2.12.5. |
| `tomlkit` | `>=0.14.0` | 0.14.0 | **`>=0.15.1`** | Current (2026-07-17). `dumps()` round-trip API unchanged; bump is hygiene for M-10 work. |
| `img2pdf` | `>=0.6.3` | 0.6.3 | **keep `>=0.6.3`** | 0.6.3 **is** the latest (2025-11-05). Everything M-06/M-08 needs is present — verified below. |
| `python-sane` | `>=2.9.1` | 2.9.2 | **`>=2.9.2`** | 2.9.2 is latest (2025-07-21). Sdist-only → CI and Docker must install `libsane-dev` before `uv sync` (M-26). |
| `pillow` | `>=12.1.1` | 12.1.1 | **`>=12.3.0`** | Current (2026-07-01). Security/decoder fixes; relevant because the app decodes untrusted-ish scanner output. |
| `fastapi` | `>=0.135.1` | 0.135.1 | **keep** (optionally `>=0.141.1`) | No milestone capability needs a bump. Sync-`def`-in-threadpool and `Form(max_length=...)` both verified on 0.135.1. If you bump, note 0.141.1 allows `starlette>=0.46.0`, so a 1.x Starlette can be resolved — pin Starlette explicitly if you bump. |
| `httpx` | `>=0.28.1` | 0.28.1 | **keep** | 0.28.1 **is** the latest (2024-12-06). `Timeout(connect=, read=, write=, pool=)` and `TransportError`/`HTTPError` hierarchy verified. |
| `uvicorn` | `>=0.42.0` | 0.42.0 | **`>=0.52.4`** | Current (2026-08-19). Also relevant to M-21: uvicorn's `log_level` table accepts only lowercase `critical/error/warning/info/debug/trace`. |
| `click` | `>=8.3.1` | 8.3.1 | keep | Latest. `ctx.obj`/`ctx.resilient_parsing` (N-25) and `click.confirm` (C-02 flip prompt) are long-standing API. |
| `jinja2` | `>=3.1.6` | 3.1.6 | keep | Custom filters (`to_local`, M-23) need no bump. |

### Dev dependency changes

| Package | Current | Recommended | Why |
|---|---|---|---|
| `pytest` | `>=9.0.2` | `>=9.1.1` | Current. |
| `playwright` | `>=1.58.0` | `>=1.62.0` | Current; needed for the C-10 browser scan-cycle test. |
| `pytest-playwright` | `>=0.7.0` | `>=0.9.0` | Current. |
| `ruff` | `>=0.15.5` | `>=0.16.6` | Current. Keep `sync-with-uv` hook in step. |
| `ty` | `>=0.0.21` | `>=0.0.79` | Current. M-25 makes `ty check` a CI gate — bump before wiring it in so CI and local agree. |
| `pyrefly` | `>=0.55.0` | `>=1.2.0` | Current, and now past 1.0. Same reasoning as `ty`. |
| `prek` | `>=0.3.5` | `>=0.5.2` | Current. |
| `mkdocs-material` | `>=9.7.6` | `>=9.7.7` | Current. N-31: docs workflow must build via `uv run` so the lock applies instead of `pip install mkdocs-material`. |
| `zizmor` | — | **add `>=1.30.1`** | Audits `.github/workflows/` for unpinned actions, over-broad `permissions`, template injection. Directly serves M-25/M-26/N-31 and keeps them from regressing. |
| `pytest-timeout` | — | **add `>=2.4.0`** (optional) | The milestone adds real-thread worker tests (C-09) and two-thread store tests (C-07). A regression that deadlocks hangs CI forever without a global `timeout = 60`. |

### Vendored front-end assets (N-21)

Download once into `src/saneless/web/static/vendor/` and delete the CDN `<link>`/`<script>` in `base.html`. The wheel already ships non-Python files under `src/saneless/web/static/` (verified in the review), so no packaging change is needed.

| Asset | Version | Source URL | SHA-384 (SRI, computed 2026-09-09) |
|---|---|---|---|
| `htmx.min.js` | **2.0.10** (npm `latest`, 2026-04-21) | `https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js` | `q2oWHKMnJry5BOtYUZkXcyieUmqzXIjdmKDYicmMspegPENZr4UrGc656JYEgJoo` |
| `pico.min.css` | **2.1.1** (npm `latest`, 2025-03-15) | `https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css` | `A7sjIT5RmlS1ydB0kbJg+I4Xs0HCcEGUS0rPtB/+i8M40McPf+LtFGZjuIUuWo82` |

Notes:
- Current code loads htmx **2.0.8** pinned and `@picocss/pico@2` **floating major** — the floating major is the more urgent half of N-21.
- npm publishes `htmx.org@next = 4.0.0`. **Do not take htmx 4** — it is a rewrite with different swap/event semantics; the C-10 fix is written against htmx 2 OOB behaviour. Stay on the 2.x line.
- Record both digests in a test (`hashlib.sha384` over the vendored file) so an upgrade is a deliberate, reviewed change rather than a silent one.
- N-44: `<html data-theme="auto">` is a **no-op in Pico v2** (only `light` and `dark` are valid). Omit the attribute entirely for auto behaviour.

---

## Capability → exact API map

This is the core deliverable: for each new capability the review demands, the library, the exact call, and where it lands.

### 1. Thread-safe SQLite from web thread + worker thread (C-07, N-13, N-17, N-39)

**Library:** stdlib `sqlite3` + `threading`. **No new dependency.**

Verified on this machine: Python 3.14.2, `sqlite3.threadsafety == 3` (SQLite compiled `SERIALIZED`), `sqlite3.sqlite_version == 3.34.1`, `Connection.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL (-1)`, `isolation_level == ''`.

```python
# src/saneless/job.py — JobStore.__init__
self._lock = threading.RLock()
self._conn = sqlite3.connect(db_path, check_same_thread=False)
self._conn.row_factory = sqlite3.Row          # kills the 3x row-mapping duplication (N-17)
if db_path != ":memory:":
    self._conn.execute("PRAGMA journal_mode=WAL")   # no-op on :memory:, so guard it
self._conn.execute("PRAGMA busy_timeout=5000")
self._conn.execute("PRAGMA foreign_keys=ON")

# every public method
def update_state(self, ...) -> None:
    with self._lock, self._conn:   # RLock serialises; `with conn` commits/rolls back
        self._conn.execute("UPDATE jobs SET ... WHERE id=?", (...))
```

Key points:
- `threadsafety == 3` means the **C layer** is safe; it does **not** serialise the *implicit transaction state* Python's `sqlite3` keeps per connection. That is exactly the failure the review reproduced 10/10 times. The `RLock` is still required.
- `RLock` (not `Lock`) because `prune()` and `list_recent()` will call each other's locked helpers once N-17's `_row_to_job` refactor lands.
- **Do not set `conn.autocommit = False`.** Under PEP 249 autocommit mode (Python 3.12+) the `with conn:` block no longer means "commit this unit of work"; keep `LEGACY_TRANSACTION_CONTROL`.
- N-13 migrations: `PRAGMA user_version` + an ordered list of migration callables. Stdlib only.
- N-17 `prune()` correctness: use `cursor.rowcount` from the single `DELETE`, not two `COUNT(*)` queries.
- N-39: derive the DB path from a new `output.state_dir` (XDG state), not `tmp_dir`.

**Rejected alternative:** per-thread connections via `threading.local()`. It removes the transaction-interleaving bug but replaces it with `database is locked` contention at the file level, needs WAL + `busy_timeout` anyway, and leaves no single place to add `fail_active_jobs()` (M-03). The RLock is smaller, and the review measured it green 10/10.

### 2. Sync routes and non-blocking HTTP (M-01, N-23)

**Library:** existing FastAPI 0.135.1 / Starlette 0.52.1 / anyio 4.12.1 / httpx 0.28.1. **No new dependency.**

Verified in the installed source, `starlette/routing.py:68`:
```python
func if is_async_callable(func) else functools.partial(run_in_threadpool, func)
```
and executed: `anyio.to_thread.current_default_thread_limiter().total_tokens == 40`.

- **The whole fix is deleting the word `async`** from every handler in `src/saneless/web/routes.py`. Starlette then runs each handler in the anyio worker threadpool (40 slots by default) and the event loop stays free.
- **Do not raise the thread limiter.** 40 concurrent blocking handlers is far beyond what a single-scanner household appliance sees; a bigger pool would only mean more simultaneous 30-second httpx waits.
- **Do not use `asyncio.to_thread` / `run_in_threadpool` manually** inside `async def` handlers — that is the same fix with more code and more places to forget.
- httpx timeout split (M-01). Verified signature `Timeout(timeout=UNSET, *, connect, read, write, pool)` — if you pass any keyword you must pass **all four**, or supply the positional default:
  ```python
  timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
  ```
- Exception surface for the M-17 boundary translation, verified by introspection:
  - `httpx.TransportError.__subclasses__()` → `TimeoutException`, `NetworkError`, `ProxyError`, `UnsupportedProtocol`, `ProtocolError`. Catch **`httpx.TransportError`** for the retry branch — it covers `ConnectError`, `ReadError`, `WriteError`, `RemoteProtocolError`, `ConnectTimeout`, `ReadTimeout` and the empty-URL `UnsupportedProtocol`.
  - `httpx.HTTPError.__subclasses__()` → `RequestError`, `HTTPStatusError`. Catch **`httpx.HTTPError`** as the final backstop before re-raising `PaperlessError`.
- N-23 stale-on-error cache: keep the existing hand-rolled TTL cache in `web/cache.py`. **Do not add `cachetools`** — the change is "return the last good value when refresh raises", about five lines.

### 3. Bounded queue + HTTP 429 backpressure (C-09)

**Library:** stdlib `queue`, `contextlib`; FastAPI `HTTPException`. **No new dependency.**

```python
# worker.submit
try:
    self._queue.put_nowait(job)
except queue.Full:
    raise WorkerBusyError("scan queue is full") from None

# routes.start_scan  (now a plain `def`)
except WorkerBusyError as exc:
    raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "30"}) from exc
except WorkerUnavailableError as exc:
    raise HTTPException(status_code=503, detail=str(exc)) from exc
```

**Integration gotcha (verified against htmx 2 source/docs):** htmx 2's default `responseHandling` is
```js
[{code:'204', swap:false}, {code:'[23]..', swap:true}, {code:'[45]..', swap:false, error:true}]
```
so a bare `429` from `/api/scan` will **not** be swapped into the DOM — the family member clicking Scan sees nothing happen. Two honest options; take the first:

1. Extend the config in `base.html` so 429 renders the "queue is full" partial while keeping the correct status code:
   ```html
   <meta name="htmx-config" content='{"responseHandling":[
     {"code":"204","swap":false},
     {"code":"[23]..","swap":true},
     {"code":"429","swap":true},
     {"code":"[45]..","swap":false,"error":true}]}'>
   ```
2. Or use the `response-targets` htmx extension — an extra vendored file for one status code. Not worth it.

Also: replace the `stop()` sentinel with a `threading.Event` + `queue.get(timeout=0.5)` loop so shutdown never depends on queue capacity, and wrap the store call in the crash handler with `contextlib.suppress(Exception)`.

**Do not add** `rq`, `celery`, `arq`, or Redis. Already an explicit Out of Scope decision in `PROJECT.md`, and nothing in the review changes it.

### 4. Server-owned Scan button via `hx-swap-oob` (C-10, M-05)

**Library:** htmx 2.0.10 (vendored). **No new dependency.** Net effect: **delete** `src/saneless/web/static/app.js` button logic.

Verified against htmx v2 docs (Context7 `/bigskysoftware/htmx/v2.0.4`):
- `hx-swap-oob="true"` on an element in the response replaces the same-`id` element already in the DOM, independent of the request's target. This is exactly the "server already knows the button state" fix.
- `hx-disabled-elt="#scan-btn"` on the form disables the button for the duration of the request, replacing the `htmx:beforeRequest` handler.
- Caveat from the docs: for OOB strategies other than `outerHTML` the encapsulating tag pair is stripped. Using plain `hx-swap-oob="true"` on a top-level `<button id="scan-btn">` in the status partial avoids that entirely.

Put the button markup in exactly one `{% include %}` rendered by both `index.html` and `partials/status.html`, driven by a single `ACTIVE_STATES: frozenset[JobState]` and a `Job.is_active` property (M-05). Type the label map as `dict[JobState, str]` so `ty`/`pyrefly` flag a missing member.

### 5. Strict configuration (M-18, M-19, M-20, M-21, N-15, N-43, U-01)

**Library:** existing `pydantic` 2.12.5 + `pydantic-settings`. **No new dependency.**

All of the following was **executed** against the installed venv:

```python
class PaperlessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")          # M-18
    url: AnyHttpUrl | None = None                       # M-17: reject "" at load time
    token: SecretStr = SecretStr("")                    # N-15
    consume_dir: Path | None = None                     # N-43
```

Verified results:
- Nested `extra="forbid"` yields errors with **full location tuples**: `('paperless', 'tokne')` and `('profiles', 'default', 'resoluton')`. So `_build_settings` can render the exact M-18 message using `loc[:-1]` for the section and `Model.model_fields` for the valid-key list — and `_VALID_SECTIONS` can be deleted (`PaperlessConfig.model_fields` → `['url', 'token']`).
- `SecretStr` redacts in `repr`: `Paperless(url='', token=SecretStr('**********'))`, and `.get_secret_value()` returns the plaintext. Two call sites need `.get_secret_value()` (Authorization header build, and any `saneless doctor` check).
- `Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"]` + a `mode="before"` validator that upper-cases. Pair with stdlib **`logging.getLevelNamesMapping()`** (3.11+) instead of `getattr(logging, name)`, which is what turns `TRACE` into an `AttributeError` today.
- Path normalisation (M-20): use `Path` fields plus a `mode="after"` field validator returning `v.expanduser()`. Pydantic accepts a `str` from TOML and coerces to `Path`, so this is a one-line validator per field and it eliminates the nine `Path(...)` re-wraps in N-43.

**XDG directories:** use **stdlib**, not `platformdirs`.
```python
def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var)
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / default
    return base / "saneless"
# state:  _xdg("XDG_STATE_HOME",  ".local/state")
# config: _xdg("XDG_CONFIG_HOME", ".config")
```
Rationale: the project ships `Operating System :: POSIX :: Linux` only, so `platformdirs`' entire value (macOS/Windows conventions) is dead weight, and the spec's "must be absolute or ignored" rule is three lines. `platformdirs` 4.11.8 is currently a **dev-only transitive** (mkdocs) — promoting it to runtime is a real new dependency. If cross-platform support ever lands, revisit.

**Source precedence (U-01):** current order returned by `settings_customise_sources` is `(init, env, toml)` — first wins, so **environment beats the TOML file**. That is the correct precedence; the bug is that nobody is told. Fix without new machinery:
- add `config_path: Path | None = Field(default=None, exclude=True)` to `Settings` and set it in `load_settings` after construction (M-04, U-01);
- at startup log, at INFO, the loaded path plus `sorted(k for k in os.environ if k.startswith("SANELESS_"))` — **names only, never values** (the token is in there);
- refuse a placeholder token (`""`, `"changeme"`) at startup and surface it on the status strip.

`TomlConfigSettingsSource(settings_cls, toml_file=..., deep_merge=False)` — the `deep_merge` kwarg exists in 2.13.1 and later. Leave it `False`: the app wants "env key overrides file key", not table merging.

### 6. Atomic TOML config writes (M-10, M-09)

**Library:** `tomlkit` ≥ 0.15.1 + stdlib `os`, `tempfile`, `shutil`. **No new dependency.**

```python
doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
...                                          # M-09: mutate the existing table in place
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_path.parent,
                                 prefix=".saneless-", suffix=".toml.tmp",
                                 delete=False) as fh:
    fh.write(tomlkit.dumps(doc))
    fh.flush()
    os.fsync(fh.fileno())
tmp = Path(fh.name)
shutil.copymode(config_path, tmp)            # preserve 0600 — the file holds the token
os.replace(tmp, config_path)                 # atomic within the same filesystem
```

- `NamedTemporaryFile(dir=...)` beats the review's `with_suffix(".toml.tmp")` because two writers cannot collide on the name, and it keeps the temp file on the same filesystem so `os.replace` is atomic.
- `os.fsync` before `replace` is what makes it durable on the small always-on box the review is worried about.
- `encoding="utf-8"` on **both** read and write.
- M-09 in-place update (`existing["source"] = ...`) is a tomlkit property, not an API change — tomlkit preserves comments and untouched keys only if you mutate rather than reassign the table.

**Do not add** `atomicwrites` (archived/unmaintained) or `filelock`. Nine lines of stdlib.

### 7. Correct PDF page geometry (M-06) and page spooling (M-08)

**Library:** `img2pdf` 0.6.3 (latest) + Pillow. **No new dependency.**

Executed against the installed package:
- `img2pdf.default_dpi == 96.0` — this **is** the M-06 root cause.
- `img2pdf.convert(*images, outputstream=None, **kwargs)`
- `img2pdf.get_fixed_dpi_layout_fun(fixed_dpi)` → returns `default_layout_fun(w, h, fixed_dpi)`, i.e. it **overrides whatever DPI the input images claim**.

```python
layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
with output_path.open("wb") as fh:
    img2pdf.convert([str(p) for p in page_paths], layout_fun=layout, outputstream=fh)
```

- Prefer the **layout function** over `Image.save(..., dpi=(dpi, dpi))` as the single source of truth. Setting PNG DPI as well is harmless but gives you two places to be wrong; the layout fun wins regardless.
- `outputstream=` is the M-08 half: without it, `convert()` materialises the entire PDF as one `bytes` object.
- Pass **paths**, not decoded images, so the resident set is one page.
- Read the DPI back from the device (M-16) before using it — `SaneDev.resolution` after `start()`.
- Test assertion: A4 at 300 DPI → `2480 / 300 * 72 = 595.2` × `3508 / 300 * 72 = 841.9` → MediaBox ≈ `[0 0 595 842]`.
- Per-page size estimate instead of `tobytes()` (M-08): use `SaneDev.get_parameters()` → `(format, last_frame, (pixels_per_line, lines), depth, bytes_per_line)`. `bytes_per_line * lines` is the exact figure with no copy.

**img2pdf exception surface (M-17) — important correction to the review.** Verified: `ImageOpenError`, `AlphaChannelError`, `ExifOrientationError`, `JpegColorspaceError`, `NegativeDimensionError`, `PdfTooLargeError`, `UnsupportedColorspaceError` each subclass `Exception` **directly**; there is no common `img2pdf.Error` base. The review's suggested `except (ValueError, OSError, img2pdf.ImageOpenError)` therefore leaks six of the seven. Either enumerate all seven in a module-level tuple, or make this the one justified `except Exception` boundary:

```python
_IMG2PDF_ERRORS = (
    img2pdf.AlphaChannelError, img2pdf.ExifOrientationError, img2pdf.ImageOpenError,
    img2pdf.JpegColorspaceError, img2pdf.NegativeDimensionError,
    img2pdf.PdfTooLargeError, img2pdf.UnsupportedColorspaceError,
)
except (ValueError, OSError, *_IMG2PDF_ERRORS) as exc:
    raise ScanError(f"PDF assembly failed: {exc}") from exc
```

- N-10: set `PIL.Image.MAX_IMAGE_PIXELS` **once** in `saneless/__init__.py:main`, delete the three import-time mutations.
- Disk pre-check per page: stdlib `shutil.disk_usage(tmp_dir)`.

### 8. CSRF / origin protection without auth (N-22, U-09)

**Library:** none. Stdlib header check in a Starlette middleware or a FastAPI dependency. **No new dependency.**

Verified (MDN): `Sec-Fetch-Site` is a **forbidden request header** — scripts cannot set or spoof it; only the browser sends it. It is sent for `fetch`/XHR (so htmx requests carry it), takes `same-origin` / `same-site` / `cross-site` / `none`, and has been Baseline-available across browsers since **March 2023**.

```python
_SAFE_FETCH_SITES = {"same-origin", "none"}   # none = user typed the URL / bookmark

def require_same_origin(request: Request) -> None:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in _SAFE_FETCH_SITES:
        raise HTTPException(status_code=403, detail="cross-site request rejected")
```

- Absent header ⇒ allow (curl, `saneless` CLI hitting the API, pre-2023 browsers). This is the right trade: the attack being blocked is *a browser the family already has open*, and browsers always send it.
- Apply as a dependency on the POST routes (`/api/scan`, `/api/cache/invalidate`, `/api/flip/*`), not globally, so `GET /health` stays trivially reachable for the container healthcheck.
- Pair with defaulting `output.web_host` to `127.0.0.1`, with the Docker image/`docker-compose.yml` setting `0.0.0.0`.

**Do not add** `starlette-csrf`, `fastapi-csrf-protect`, or `itsdangerous`-backed sessions. Token-based CSRF needs a session cookie and a login this product deliberately does not have (`PROJECT.md` Out of Scope: multi-user auth). Adding a session store to defend an unauthenticated LAN appliance is the wrong shape.

Input validation (N-20) is likewise dependency-free and **verified on the pinned FastAPI 0.135.1**: `Annotated[str, Form(max_length=256)]` returns `422` with `type == "string_too_long"`; `Literal["tags", "correspondents"]` for the `resource` query parameter gets a 422 for free.

### 9. Delivery: CI, release, container (M-25, M-26, M-31, N-26, N-31, N-30)

**Actions — current release tags and their commit SHAs** (resolved 2026-09-09 via `git ls-remote`; the repo currently uses the majors in brackets):

| Action | Latest tag (published) | Commit SHA to pin | Currently used |
|---|---|---|---|
| `actions/checkout` | `v7.0.1` (2026-07-20) | `3d3c42e5aac5ba805825da76410c181273ba90b1` | `@v5` |
| `astral-sh/setup-uv` | `v10.0.1` (2026-08-14) | `20cfd1bf945f4377ade1205e4dbc17946fc9a30d` | `@v7` |
| `pypa/gh-action-pypi-publish` | `v1.14.2` (2026-07-29) | `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` | `@v1.12` — **does not resolve** (M-26) |
| `docker/login-action` | `v4.6.0` (2026-07-29) | `dbcb813823bdd20940b903addbd779551569679f` | `@v3` |
| `docker/metadata-action` | `v6.2.0` (2026-07-02) | `dc802804100637a589fabce1cb79ff13a1411302` | `@v5` |
| `docker/build-push-action` | `v7.3.0` (2026-07-01) | `53b7df96c91f9c12dcc8a07bcb9ccacbed38856a` | `@v6` |

Pin as `uses: owner/repo@<sha> # vX.Y.Z` and add `.github/dependabot.yml` with the `github-actions` ecosystem so the SHAs get maintained PRs. Run `zizmor .github/workflows/` in CI to keep it honest.

Workflow requirements the milestone must satisfy:
- Top-level `permissions: { contents: read }` on **every** workflow (`ci.yml`, `release.yml`, `docs.yml`); escalate per-job only: `id-token: write` on the PyPI publish job (trusted publishing), `packages: write` on the GHCR job, `contents: write` on the docs deploy job.
- `sudo apt-get install -y libsane-dev` **before** `uv sync --locked` — `python-sane` 2.9.2 is sdist-only and compiles against `sane/sane.h`.
- `uv sync --locked` (not bare `uv sync`) so CI fails on a stale lock.
- `uv run pytest -m "not browser"` in the fast gate; a separate job with `uv run playwright install --with-deps chromium` for the browser tests (needed for C-10's regression test).
- `uv run ty check` **and** `uv run pyrefly check src tests` — the project's own mandatory gates, absent from the release workflow today.
- A tag-vs-`project.version` assertion job before publishing.
- `git grep -n -e kris-knigga -e 'saneless\.github\.io' -- ':!.planning' ':!site'` as a CI step (M-27); must print nothing.
- Build docs with `uv run mkdocs` so the lock's `mkdocs-material` 9.7.7 is used, not an unpinned `pip install`.

**Container (N-26, M-28, M-29, M-31):**
- Pin both base images by digest: `python:3.14-slim@sha256:…` and `ghcr.io/astral-sh/uv:0.10.3@sha256:…` (currently `uv:latest` — irreproducible). Resolve the digests at implementation time with `docker buildx imagetools inspect`.
- `RUN useradd --system --create-home --uid 10001 saneless` + `USER saneless`; `WORKDIR /data`; `ENV SANELESS_OUTPUT__TMP_DIR=/data/tmp SANELESS_OUTPUT__STATE_DIR=/data/state`.
- `.dockerignore` allow-list — `*`, then `!pyproject.toml`, `!uv.lock`, `!README.md`, `!LICENSE`, `!src/`. This is the fix that stops the developer's real `saneless.toml` (live Paperless token) from entering the build cache.
- Keep the `HEALTHCHECK` on `curl -f http://localhost:8080/health`. **Do not** point it at `saneless doctor` — doctor talks to the scanner and Paperless, which is far too heavy and noisy for a 30-second interval and would flap the container whenever Paperless restarts. `/health` answers "is the process serving and is the worker alive"; that is the healthcheck's job.

**Packaging (N-30) — verified by building a wheel:**
```toml
license = "MIT"
license-files = ["LICENSE"]
# and DELETE: "License :: OSI Approved :: MIT License" from classifiers
```
Executed with the pinned backend `uv_build` 0.10.3 → wheel contains `demo-0.1.0.dist-info/licenses/LICENSE`, and METADATA says `Metadata-Version: 2.4`, `License-Expression: MIT`, `License-File: LICENSE`. So the backend already supports PEP 639; no build-system change is needed.

**Trap, also executed:** `uv_build` builds **without error** when the SPDX `license` and a license classifier are both present. PyPI does not — PEP 639 requires the index to reject any upload carrying `License-Expression` alongside license classifiers. Leaving the classifier in place turns the first `git push --tags` into a failed publish *after* the tests pass. Delete the classifier in the same commit as the `license` key.

Also consider widening `requires = ["uv_build>=0.10.3,<0.11.0"]` — uv/`uv_build` is at 0.12.12; keep the ceiling one minor above whatever CI uses, and bump both together.

### 10. Hermetic, fast tests (M-33, M-34, N-18, N-24, N-40)

**Library:** pytest 9.1.1 built-ins. **No new test libraries.**

| Need | API | Note |
|---|---|---|
| Fake sleep (N-18) | `monkeypatch.setattr(saneless.paperless.time, "sleep", recorder)` | Assert the recorded schedule is `[1, 2]` — no test currently pins the backoff |
| Fake monotonic clock (N-24) | `monkeypatch.setattr(saneless.web.cache.time, "monotonic", lambda: fake[0])` | Patch the **module attribute the code reads**, not `time.monotonic` globally |
| Hermetic env | autouse fixture: `monkeypatch.chdir(tmp_path)` + `monkeypatch.setattr(Path, "home", lambda: tmp_path)` | Stops `test_env_prefix` reading the developer's real `./saneless.toml` (M-34) |
| XDG isolation (M-20 tests) | `monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))` | Also `XDG_CONFIG_HOME` |
| Per-test temp dirs | `tmp_path`, `tmp_path_factory` | Delete the fixed `$TMPDIR/saneless-test` constants |
| Free port | `socket.socket().bind(("127.0.0.1", 0))` | Already done correctly in the browser fixture — copy it into `test_serve_custom_host_port` |
| Root-hostile chmod tests | `@pytest.mark.skipif(os.geteuid() == 0, reason="chmod is a no-op for root")` | Docker CI runs as root |
| Two-thread store stress (C-07) | `threading.Barrier(2)` + `ThreadPoolExecutor` | Deterministic start, no sleeps |
| HTTP fakes | `httpx.MockTransport` (already used) | Keep |
| SANE fakes | Hand-written, mirroring real `sane.py` (below) | Keep |
| CI hang guard | `pytest-timeout` `timeout = 60` in `[tool.pytest.ini_options]` | The only optional dev addition |

**Explicitly do not add a time-travel library:**
- **freezegun** freezes `time.monotonic()` and lets it go *backwards*, which violates the contract the app relies on for cache TTLs and the M-22 `poll_task` deadline, and it breaks asyncio event loops (its own `real_asyncio` escape hatch exists for this reason). Using it here would make the TestClient tests flaky.
- **time-machine** deliberately **does not mock `time.monotonic()`** at all — so it cannot drive the N-24 cache-TTL test, which is the main thing you would want it for.

Two `monkeypatch.setattr` calls do the whole job with no failure mode. Also skip `pytest-httpx`/`respx` (`httpx.MockTransport` already covers it, and the review praises the current approach) and `pytest-xdist` (the suite's problem is 38 seconds of `sleep`, not CPU).

### 11. Truthful `python-sane` fakes (M-32, M-12, M-15, N-01, N-03, N-04)

**Source of truth:** the installed `python-sane` 2.9.2 pure-Python wrapper, `.venv/lib/python3.14/site-packages/sane.py` (420 lines). Everything below was read from that file — the fakes must match it exactly.

**Module surface**
| Call | Real behaviour |
|---|---|
| `sane.init()` | `-> (sane_ver, maj, min, patch)`; raises `_sane.error` |
| `sane.exit()` | Exists, takes no args. Currently **never called** (N-04) |
| `sane.get_devices(localOnly=False)` | `-> list[(device_name, vendor, model, type)]` |
| `sane.open(devname)` | `-> SaneDev`; `SaneDev.__init__` raises **`RuntimeError("No such scan device '%s'")`** when the name is not in the signature list — *not* `_sane.error` |
| Error type | `_sane.error` (reachable as `sane._sane.error`); message strings like `'Invalid argument'`, `'Document feeder out of documents'` |

**`SaneDev.__setattr__` — the single most important thing the current fakes get wrong (M-15):**
```python
if key in ('dev','optlist','area','sane_signature','scanner_model'): raise AttributeError("Read-only attribute: ...")
if key not in self.opt:  d[key] = value; return          # ← SILENTLY SUCCEEDS
if opt.type == TYPE_BUTTON: raise AttributeError("Buttons don't have values: ...")
if opt.type == TYPE_GROUP:  raise AttributeError("Groups don't have values: ...")
if not OPTION_IS_ACTIVE(opt.cap):   raise AttributeError("Inactive option: ...")
if not OPTION_IS_SETTABLE(opt.cap): raise AttributeError("Option can't be set by software: ...")
if isinstance(value, int) and opt.type == TYPE_FIXED: value = float(value)   # int→float coercion
result = dev.set_option(opt.index, value)                # ← _sane.error for a bad VALUE
if result & INFO_RELOAD_OPTIONS: self.__load_option_dict()
```
So: **unknown option name ⇒ no error at all**; a *known* option with a *rejected value* ⇒ `_sane.error`; a structurally wrong option ⇒ `AttributeError`. The current fake raises on unknown names, which is why the crop-fallback tests pass against behaviour that cannot happen. The fix is a presence check (`"br_x" in dev.opt`) before assignment, plus a fake that models all three outcomes.

`__getattr__` mirrors it: `optlist` → `list(self.opt)`, `area` → `((tl_x, tl_y), (br_x, br_y))`, unknown key → `AttributeError("No such attribute: ...")`.

**`_SaneIterator` (multi_scan) — exact semantics:**
```python
def __next__(self):
    try:
        self.device.start()
        return self.device.snap(True)
    except Exception as e:
        if str(e) == 'Document feeder out of documents':
            raise StopIteration
        raise
def __del__(self):
    try: self.device.cancel()
    except Exception: pass
```
- `multi_scan()` itself only returns `_SaneIterator(self)` — it **cannot raise**. `test_empty_feeder_out_of_documents_error` is built on an impossibility (M-32).
- The end-of-feeder signal is an **exact string match**, nothing else. That is why any first-page error currently becomes "no paper" (M-11).
- `__del__` calls `cancel()` — hence the "drop the iterator before calling `dev.cancel()`" rule the code already follows (rewrite that comment per N-34).

**Other real methods the fakes must expose:** `start()`, `snap(no_cancel=False, progress=None)` (raises `RuntimeError("Scanner returned no data")` on empty data; builds `Image.frombuffer` in `RGB` if 3 samples else `L`), `scan()`, `cancel()`, `close()`, `fileno()`, `get_options()`, `get_parameters()` → `(format, last_frame, (pixels_per_line, lines), depth, bytes_per_line)`, `__enter__`/`__exit__` → `close()`.

**Option tuple layout** (`Option.__init__`, indices into the raw `get_options()` tuple) — resolves N-01 and N-03 exactly:
`0 index, 1 name, 2 title, 3 desc, 4 type, 5 unit, 6 size, 7 cap, 8 constraint`
- **index 5 is the unit** — read it (`UNIT_MM` vs `UNIT_PIXEL`) before writing `br_x = 210.0` (N-03).
- **index 8 is the constraint** and is one of `None` / `(min, max, step)` / `list` (N-01). Write one `_constraint(raw_options, name)` helper and delete the duplicated tuple parsing.
- `Option.py_name` replaces `-` with `_`; `is_active()` / `is_settable()` wrap the cap bits.

**N-04 process-level init:** module-level `_INITIALISED` flag guarding `sane.init()`, a `close()` that calls `sane.exit()`, and `SANE_NET_HOSTS` written **before** the first `init()` — the env var has no effect afterwards, so a second backend with a different host is silently ignored today.

**Opt-in real-backend integration tests** (M-32) — no new dependency, uses SANE's own `test` backend:
```python
tmp = tmp_path / "sane.d"; tmp.mkdir()
(tmp / "dll.conf").write_text("test\n")
monkeypatch.setenv("SANE_CONFIG_DIR", str(tmp))
# then drive SaneBackend against "test:0"
```
Gate behind a `sane_hardware` marker registered in `[tool.pytest.ini_options].markers` and skipped when `libsane` is unavailable. The `test` backend exposes `read-delay`, which is what makes the M-12/M-13 timeout paths testable for real.

### 12. `saneless doctor` and startup self-checks (U-03, U-01)

**Library:** existing `click` 8.3.1. **No new dependency.**

- A `@cli.command("doctor")` that runs the same check functions the web status strip calls, prints a line per check, and `raise SystemExit(1)` on any red — so it works as a compose one-shot and a docs smoke test.
- Return a list of small dataclasses (`CheckResult(name, ok, detail, hint)`) from a `saneless/doctor.py` module; the CLI renders them as text, the web route renders them into the status-strip partial. One implementation, two presentations — this is the M-05/N-36 lesson applied preemptively.
- **Do not add `rich`.** The CLI already formats tables by hand with terminal-aware widths (Phase 12) and Click's `secho` covers colour. Adding `rich` for one command creates a second, inconsistent output style.
- N-25: move settings loading out of the Click **group** callback into each command (or return early on `ctx.resilient_parsing`) so `saneless doctor --help` works on a broken config — which is precisely when someone runs `doctor`.

---

## Installation

```bash
# Runtime pin changes (edit pyproject.toml, then):
uv lock --upgrade-package pydantic-settings \
        --upgrade-package tomlkit \
        --upgrade-package pillow \
        --upgrade-package uvicorn \
        --upgrade-package python-sane
uv sync --locked

# Dev additions
uv add --dev "zizmor>=1.30.1" "pytest-timeout>=2.4.0"

# Dev bumps
uv add --dev "pytest>=9.1.1" "playwright>=1.62.0" "pytest-playwright>=0.9.0" \
             "ruff>=0.16.6" "ty>=0.0.79" "pyrefly>=1.2.0" "prek>=0.5.2" \
             "mkdocs-material>=9.7.7"
uv run playwright install chromium

# Vendor front-end assets (N-21)
mkdir -p src/saneless/web/static/vendor
curl -fsSL -o src/saneless/web/static/vendor/htmx.min.js \
  https://cdn.jsdelivr.net/npm/htmx.org@2.0.10/dist/htmx.min.js
curl -fsSL -o src/saneless/web/static/vendor/pico.min.css \
  https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css
# then verify against the SHA-384 digests in the table above and pin them in a test
```

pyproject edits (not expressible as `uv` commands):

```toml
license = "MIT"                       # was: {text = "MIT"}
license-files = ["LICENSE"]
classifiers = [ ... ]                 # DELETE "License :: OSI Approved :: MIT License"
dependencies = [
    "pydantic-settings>=2.15.0",      # was: pydantic-settings[toml]>=2.13.1
    ...
]
[dependency-groups]
dev = [ ... ]                         # DELETE the duplicate "httpx>=0.28.1"
```

---

## Alternatives Considered

| Recommended | Alternative | When the alternative would be right |
|---|---|---|
| `threading.RLock` + WAL on one `sqlite3` connection | Per-thread connections via `threading.local()` | If a second worker thread is ever added, or if a long-running read must not block writes. Today it trades a measured-fixed bug for lock contention and scatters the M-03 startup reconciliation. |
| `threading.RLock` + stdlib `sqlite3` | SQLAlchemy Core / `aiosqlite` / `sqlite-utils` | If the schema grew beyond one table, or if the app went fully async. `job.py` is ~200 lines against one table; an ORM would be more code than it replaces. |
| stdlib XDG resolution | `platformdirs` 4.11.8 | If saneless ever targets macOS or Windows. Today the project ships a Linux-only classifier and this would be the sole new runtime dependency. |
| `Sec-Fetch-Site` header check | `starlette-csrf`, `fastapi-csrf-protect` | If the app gains sessions/auth. Token CSRF needs a session cookie the product deliberately does not have. |
| `monkeypatch` fake clocks | `time-machine`, `freezegun` | Neither is usable here — see "What NOT to Use". If you ever need to freeze `datetime.now()` across many modules for date-formatting tests (M-23), `time-machine` becomes defensible; a `local_now()` helper you can patch in one place is still simpler. |
| stdlib `queue.Queue` + `put_nowait` | `rq` + Redis, `arq`, `celery` | Already an explicit Out of Scope decision. Would apply only if multiple scanners / multiple hosts entered scope. |
| Keep `img2pdf` 0.6.3 | `pypdf` / `reportlab` for page assembly | Only if PDF/A output or per-page OCR-layer insertion were needed. img2pdf's whole point is lossless passthrough — swapping it away would re-encode scans. |
| Hand-rolled TTL cache with stale-on-error | `cachetools` | If cache policy grew beyond one TTL dict. N-23's fix is ~5 lines. |
| htmx 2.0.10 | htmx 4.0.0 (`next` on npm) | Not for this milestone. htmx 4 changes swap and event semantics; the C-10 OOB fix and the 429 `responseHandling` config are written against htmx 2. Revisit after v2.0 ships. |
| `/health` for the container HEALTHCHECK | `saneless doctor` | Never for HEALTHCHECK — doctor performs network I/O to the scanner and Paperless and would flap the container during a routine Paperless restart. Use doctor as an operator command and a one-shot compose service. |

---

## What NOT to Use

| Avoid | Specific problem | Use instead |
|---|---|---|
| `pydantic-settings[toml]` extra | On Python ≥ 3.11 the TOML source imports stdlib `tomllib`; the extra installs `tomli` 2.4.0 that is never imported. Verified in `sources/providers/toml.py`. | Plain `pydantic-settings>=2.15.0` |
| Keeping the MIT **classifier** alongside `license = "MIT"` | `uv_build` builds it happily (verified), but PEP 639 requires PyPI to **reject** any upload with `License-Expression` plus a license classifier. The first release tag would fail after the tests pass. | `license = "MIT"` + `license-files = ["LICENSE"]`, classifier deleted |
| `freezegun` | Freezes and can rewind `time.monotonic()`, breaking the cache TTL and `poll_task` deadline invariants and asyncio event loops (its `real_asyncio` flag exists for this). | `monkeypatch.setattr(module.time, "monotonic", fake)` |
| `time-machine` | Deliberately does **not** mock `time.monotonic()` — cannot drive the N-24 cache-TTL test at all. | Same as above |
| `pytest-httpx`, `respx` | `httpx.MockTransport` already exercises real request building and multipart encoding; the review names this as a strength. | Keep `httpx.MockTransport` |
| `atomicwrites` / `python-atomicwrites` | Unmaintained/archived; M-10 is nine lines of `tempfile` + `os.replace`. | stdlib |
| `platformdirs` as a **runtime** dep | Sole value is non-Linux conventions the project does not target; currently only a dev transitive. | 5-line stdlib XDG helper |
| `starlette-csrf` / `fastapi-csrf-protect` / session middleware | Token CSRF needs a session cookie; the product has no auth and no sessions by design. | `Sec-Fetch-Site` check (browser-set, unspoofable) |
| `rich` for `saneless doctor` | Would create a second, inconsistent CLI output style next to the existing hand-rolled tables. | `click.secho` + the existing table helper |
| `alembic` for the N-13 migration | One table, five columns, single-process. | `PRAGMA user_version` + an ordered migration list |
| Raising the anyio thread limiter above 40 | More concurrent 30-second blocking Paperless calls, not fewer. The default is already generous for a single-scanner appliance. | Leave it; fix the timeouts (M-01) instead |
| `conn.autocommit = False` (PEP 249 mode) | Changes what `with conn:` means, silently breaking the C-07 commit/rollback pattern. | Keep `LEGACY_TRANSACTION_CONTROL` and use `with self._lock, self._conn:` |
| htmx 4.x | Rewrite with different swap/event semantics; would invalidate the C-10 OOB fix. | htmx 2.0.10 |
| `@picocss/pico@2` (floating major) and any CDN reference | Offline LAN is the product's stated environment; a floating major can change the UI without a commit. | Vendored `pico.min.css` 2.1.1 with a pinned SHA-384 in a test |
| Bare `429` for `/api/scan` with no htmx config change | htmx 2's default `responseHandling` does not swap `[45]..` — the user sees nothing happen. | Add `{"code":"429","swap":true}` to the `htmx-config` meta tag |
| `except (ValueError, OSError, img2pdf.ImageOpenError)` (as written in M-17) | img2pdf's seven error classes each subclass `Exception` directly — there is no shared base, so six leak. | Enumerate all seven, or make this the one justified `except Exception` boundary |
| `uv sync` (unlocked) in CI | Hides a stale `uv.lock`; a release could ship different versions than were tested. | `uv sync --locked` |
| Floating action majors (`@v5`, `@v7`, `@v3`) | Tag mutation is a live supply-chain vector, and `@v1.12` already does not resolve (M-26). | Full commit SHAs + Dependabot + `zizmor` |
| `uv:latest` and undigested `python:3.14-slim` | Two builds of the same commit can differ. | Digest-pinned base images |

---

## Version Compatibility

| Package | Compatible with | Notes |
|---|---|---|
| `pydantic-settings` 2.15.0 | `pydantic>=2.7.0` | Current lock has pydantic 2.12.5 — fine. |
| `fastapi` 0.135.1 | `starlette` 0.52.1, `pydantic>=2.9` | Sync `def` → threadpool verified in this exact Starlette. |
| `fastapi` 0.141.1 (if bumped) | `starlette>=0.46.0` — **unbounded upward** | Starlette 1.6.0 exists. If you bump FastAPI, add an explicit `starlette` pin or you may silently resolve across a major. Not required for this milestone. |
| `python-sane` 2.9.2 | sdist-only; needs `libsane-dev` / `sane-backends-devel` at build time | Must be installed in CI **and** the Docker builder stage before `uv sync`. |
| `img2pdf` 0.6.3 | `pillow` 12.x | `default_dpi == 96.0`; `layout_fun` overrides per-image DPI. |
| `uv_build` 0.10.3 | PEP 639 / Metadata 2.4 | Verified by building a wheel: `License-Expression`, `License-File`, `dist-info/licenses/`. No backend change needed. |
| `sqlite3` (CPython 3.14.2) | SQLite 3.34.1 on the review host | `threadsafety == 3`; WAL and `busy_timeout` both available (SQLite ≥ 3.7). Do not assume a newer SQLite in CI/Docker — `python:3.14-slim` ships a different build. |
| `htmx` 2.0.10 | `hx-swap-oob`, `hx-disabled-elt`, `htmx.config.responseHandling` | All three are htmx 2 features; none exist in the same form in htmx 1 or 4. |
| `Sec-Fetch-Site` | Baseline across browsers since March 2023 | Absent-header must be treated as allow (CLI/curl). |
| `logging.getLevelNamesMapping()` | Python ≥ 3.11 | Safe on the 3.14 floor. |
| `Connection.autocommit` | Python ≥ 3.12 | Available but explicitly **not** to be used (see above). |

---

## Sources

- **Installed venv introspection (executed, HIGH):** `.venv/.../sane.py` (python-sane 2.9.2 full API surface, `__setattr__`/`_SaneIterator`/`Option` layout); `img2pdf` 0.6.3 (`default_dpi`, `convert` signature, `get_fixed_dpi_layout_fun` source, seven error classes and their bases); `httpx` 0.28.1 (`Timeout.__init__` signature, `TransportError`/`HTTPError` subclass sets); `starlette/routing.py:68` (sync handler → `run_in_threadpool`); `anyio` 4.12.1 default thread limiter = 40; `pydantic_settings/sources/providers/toml.py` (stdlib `tomllib` on ≥3.11); CPython 3.14.2 `sqlite3` threadsafety/autocommit.
- **Executed experiments (HIGH):** nested `extra="forbid"` error `loc` tuples and `SecretStr` repr redaction under pydantic 2.12.5; `Annotated[str, Form(max_length=8)]` → 422 `string_too_long` on FastAPI 0.135.1; wheel build with `uv_build` 0.10.3 proving PEP 639 support and proving it does **not** reject the classifier conflict.
- **Context7 `/pydantic/pydantic-settings`** — `TomlConfigSettingsSource`, `settings_customise_sources`, `deep_merge`, `SecretStr` in nested models.
- **Context7 `/bigskysoftware/htmx/v2.0.4`** — `hx-swap-oob` (including the tag-stripping caveat), `hx-disabled-elt`, `htmx.config.responseHandling` defaults `[{204,false},{[23]..,true},{[45]..,false,error}]`.
- **PyPI JSON API (HIGH, 2026-09-09)** — latest versions for pydantic-settings 2.15.0, fastapi 0.141.1, httpx 0.28.1, img2pdf 0.6.3, python-sane 2.9.2, tomlkit 0.15.1, pillow 12.3.0, uvicorn 0.52.4, starlette 1.6.0, platformdirs 4.11.8, pytest 9.1.1, playwright 1.62.0, ruff 0.16.6, ty 0.0.79, pyrefly 1.2.0, prek 0.5.2, zizmor 1.30.1, pytest-timeout 2.4.0.
- **npm registry (HIGH, 2026-09-09)** — `htmx.org` latest 2.0.10 / next 4.0.0; `@picocss/pico` latest 2.1.1. SRI digests computed locally with `openssl dgst -sha384`.
- **GitHub API + `git ls-remote` (HIGH, 2026-09-09)** — action release tags and their annotated-tag commit SHAs.
- **[MDN — `Sec-Fetch-Site`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Site)** (HIGH) — forbidden header, sent on fetch/XHR, Baseline since March 2023.
- **[PEP 639](https://peps.python.org/pep-0639/)** (HIGH) — PyPI MUST reject `License-Expression` alongside license classifiers.
- **[Python Packaging User Guide — writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license)** (HIGH) — `license` / `license-files`, backend support table.
- **[uv build backend docs](https://docs.astral.sh/uv/concepts/build-backend/)** (MEDIUM — superseded by the local build experiment, which is HIGH).
- **[freezegun `time.monotonic` issues](https://github.com/spulec/freezegun/issues/384)** and **[time-machine: monotonic not mocked](https://github.com/adamchainz/time-machine/issues/29)** (MEDIUM) — basis for the "no time-travel library" recommendation.
- **[zizmor audit rules](https://docs.zizmor.sh/audits/)** (MEDIUM) — `unpinned-uses` and `excessive-permissions` audits.
- **Project sources read:** `.planning/reviews/2026-09-09-code-review.md` §§1-5, 10, 11; `.planning/PROJECT.md`; `pyproject.toml`; `uv.lock`; `Dockerfile`; `.github/workflows/{release,docs}.yml`; `src/saneless/config.py`; `src/saneless/web/templates/base.html`.

---
*Stack research for: v2.0 release hardening of a SANE → paperless-ngx bridge*
*Researched: 2026-09-09*
