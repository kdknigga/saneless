# Phase 26: Worker and Web Robustness - Research

**Researched:** 2026-09-14
**Domain:** Python threading (worker lifecycle), FastAPI/Starlette error handling and middleware, htmx 2 response handling and OOB swaps, vendored static assets with SRI, Fetch Metadata CSRF defence, Playwright in CI
**Confidence:** HIGH for the mechanisms. Nearly every load-bearing claim was checked against installed source (htmx 2.0.8, Starlette 0.52.1, FastAPI 0.135.1, uvicorn 0.42.0, Go 1.25 `csrf.go`) or by running it on the project interpreter.

## Summary

Every mechanism this phase needs already exists in the stack. None needs a new dependency. Five findings change how the plans should be written:

1. **`queue.Queue.shutdown(immediate=True)`** (Python 3.13+) gives D-07 a stop flag with no sentinel. It wakes a blocked `get()` in about 0.2 ms, makes later `put_nowait` raise `queue.ShutDown`, and never depends on queue capacity. This matters: 98 web tests currently run in 1.8 s and each `TestClient` exit calls `worker.stop()`. A polling tick alone would add tens of seconds to the suite.
2. **htmx 2.0.8 has two traps that would recreate C-10.**
   - `<meta name="htmx-config">` is a *shallow* merge, so `responseHandling` must restate all three entries.
   - `hx-disabled-elt` on the form is *inherited* by the tags and correspondent selects inside it, which have their own `hx-trigger="load"` requests. In 2.0.8 the attribute strips `disabled` from an element the server rendered disabled (fixed only in 2.0.9). So an unguarded `hx-disabled-elt` would re-enable the Scan button on page load during an active job. The form needs `hx-disinherit="hx-disabled-elt"`.
3. **Starlette puts an `Exception` handler in the outermost `ServerErrorMiddleware`.** That layer sits *outside* user middleware and re-raises after sending the response. So the cross-site middleware cannot raise `HTTPException` to reach the shared renderer; it must call the renderer directly. Tests of the 500 path need `TestClient(app, raise_server_exceptions=False)`.
4. **The vendored files' bytes are fragile under this repo's prek hooks.** `htmx.min.js` ends in `;` and `pico.min.css` ends in `}`, neither with a newline, and `end-of-file-fixer` / `trailing-whitespace` run at commit. One commit would silently change the bytes, and the browser would then refuse to load the assets because of the SRI mismatch. Exclude `static/vendor/` from the hooks and pin the hashes with a bytes-vs-template test.
5. **D-06 does not need a schema migration** (STOR-03 explicitly wanted "later phases never add columns ad hoc"). A new `ErrorCategory.REJECTED` member plus an `error_category IS NOT ?` fallback query marks rejected-at-submit rows durably, survives restarts, and leaves D-17's contract ("the job that just ended") unchanged.

**Primary recommendation:** Build the worker around `Queue.get(timeout=IDLE_TICK)` + `Queue.shutdown(immediate=True)` + a `threading.Event` degraded flag. Route every error through one `render_error()` function, called by the exception handlers and by a pure-ASGI cross-origin middleware. Make the Scan button a single include that htmx-route responses re-render out-of-band. Vendor the two npm files byte-for-byte, with a test that recomputes the SRI hashes.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### Carried forward (already decided, do not re-litigate)
- **"FAILED" means `JobState.ERROR`.** There is no `FAILED` member and none is added
  (STOR-05, `job.py:808-811`). `fail_active_jobs(reason)` is the seam; this phase gives it
  its caller. Its predicate derives from `ACTIVE_STATES`, so `PENDING` jobs lost from the
  in-memory queue are failed too, which is correct: resuming a queued job after a restart
  would scan whatever paper is in the feeder by then.
- **`is_bare_default` already compares the full model** (fixed in Phase 25 as WR-04,
  `auto_profiles.py:197-222`). ROBU-07's second clause is *verified by a test covering the
  shapes*, not rebuilt.
- **D-17 (Phase 25): one "current job, else most recent" lookup** (`_current_or_recent_job`)
  for every status route. **D-16: a flip answer is final.** See D-06 below for an
  interaction this phase must handle.
- **Pico coupling contract (23.1-UI-SPEC § "Coupling contract for Phase 26")**: vendor
  `pico.min.css` **2.1.1** only; do **not** vendor `pico.colors.css`; reintroduce no
  `data-theme` on `<html>`; keep the `--saneless-status-fallback` block; move the DARK-01 /
  DARK-02 browser tests into CI once assets are local.
- **`web_host` default stays `0.0.0.0`.** DOCS-05's wording ("binds all interfaces by
  default") already assumes it. ROBU-10's "the default bind address is documented" is met by
  documentation, not by changing the default.
- **The Scan button stays disabled while a job is active** (today's `index.html` rule:
  `job.is_active` → disabled; "Waiting for flip…" at `AWAITING_FLIP`; "Scanning…" while
  busy). ROBU-04 changes who renders it, not the rule.

#### Rejection visibility (ROBU-02, ROBU-08, and every error path)
- **D-01: every 4xx and 5xx response is swapped, backed by an app-wide catch-all renderer.**
  htmx 2's default `responseHandling` does not swap `[45]..`, so `base.html` carries a
  `<meta name="htmx-config">` override that swaps error responses (keeping `204` unswapped).
  Because an unplanned body (Starlette's plain-text `Internal Server Error`) must never be
  what lands, the app registers exception handlers that render *every* error as the shared
  error partial: `HTTPException`s the routes raise (429, 503, 403), FastAPI's
  `RequestValidationError` (422), and a catch-all `Exception` handler (500, logged with
  `exc_info`). One rendering rule for every error; no route builds its own error HTML.
- **D-02: error responses are retargeted, never swapped into their original target.** Every
  error response sets `HX-Retarget` (to the message slot, D-03) and `HX-Reswap`, so an error
  from the tag/correspondent refresh (which targets a `<select>`) or the history load (which
  targets the table) can never inject a partial into the wrong element.
- **D-03: errors land in a separate message slot, not in `#status-area`.** A sibling region
  next to the polled `#status-area` (for example `#status-message`, `role="alert"`) receives
  error partials. The 1 s poll keeps replacing `#status-area` underneath, so a 429 that
  arrives mid-scan neither disappears on the next poll nor stops the progress display. The
  message stays until the next *successful* `POST /api/scan` clears it (for example, an
  out-of-band swap emptying the slot in the success response).
- **D-04: requests without `HX-Request` get a non-HTML error body.** The renderer branches on
  the `HX-Request` header: htmx requests get the partial + retarget headers; everything else
  (curl, scripts, `/health`) keeps a short JSON body. The status code is identical on both
  branches. `Retry-After` is set on 429 regardless of branch.
- **D-05: a submit rejected for capacity or availability (429 queue full, 503 worker down or
  degraded) is recorded as an `ERROR` job row in history.** This is a deliberate departure
  from the review's "create the job row only after the enqueue succeeds" (C-09): the user
  wants the attempt visible in Job History. The row's `error` text says what happened in
  user terms (queue full / scanner service unavailable). For the degraded case (D-11) the
  row is written only if the store accepts the write; the response is 503 either way.
  **422 is different: ROBU-08 requires rejection "before creating a job"**, so unknown
  profile, over-long title, and a bad `resource` create no row.
- **D-06: the rejected-submit row must not hijack the status area.** *(Consequence of D-05
  the planner must resolve.)* A row rejected with 429 is created *after* the job that is
  still running. When that job ends and `worker.current_job_id` clears, D-17's "else most
  recent" fallback would pick the rejection row and show "Error: queue full" instead of the
  scan that just finished. The fallback must skip rows that were rejected at submit and never
  ran, so D-17's meaning ("the job that just ended") is preserved. Choose the mechanism (a
  recorded marker, an ordering on finish time, or similar) in research/planning. If the only
  workable fix changes D-17's contract, raise it with the user, do not silently change it.

#### Shutdown and worker health (ROBU-01, ROBU-03, ROBU-06)
- **D-07: shutdown abandons an in-flight job rather than waiting for it.** `stop()` sets a
  stop flag (the `None` sentinel is deleted, so stopping never depends on queue capacity).
  If a flip wait is open, it is answered with Abort so the thread exits at once. A scan still
  inside a SANE read is abandoned. The daemon thread dies with the process, and the next
  startup's `fail_active_jobs` records it as ERROR with a "server restarted" reason. No
  shutdown-time state write is attempted for the abandoned job (that would race the
  still-running thread's own final write).
- **D-08: the bounded join is 5 seconds** (today's value; fits inside Docker's 10 s SIGKILL
  grace with room for uvicorn). Not configurable.
- **D-09: if the join expires with the thread alive, the store and Paperless client are left
  open.** Log a WARNING naming the job id, skip `job_store.close()` and `paperless.close()`,
  and let the process exit reclaim them. The stuck thread never hits "Cannot operate on a
  closed database". This satisfies ROBU-06's "the worker stops before the store closes"
  literally: the store closes only after a confirmed stop.
- **D-10: the worker never exits on its own; its health degrades instead.** ROBU-01's guard
  logs every exception with `exc_info` and keeps serving. A pipeline exception is a *job*
  failure (the job ends ERROR, which is normal). A failure of the loop's own operations (job
  store writes around the pipeline, `prune`, the store call inside the failure path) is a
  *loop-level* failure. After N consecutive loop-level failures the worker is **degraded**:
  the thread keeps running, but `/health` returns 503 with a detail that distinguishes
  "job store failing" from "worker thread is down". Pipeline errors (scanner jam, upload
  rejection) never count toward N.
- **D-11: while degraded, `POST /api/scan` rejects with 503**, the same path as a dead worker
  (D-05 row rule applies). The user is not asked to feed paper into a job that cannot be
  recorded.
- **D-12: an idle-tick store probe clears the degraded state.** While degraded, the worker's
  idle loop (the `queue.get(timeout=…)` tick that replaces the blocking `get`) runs a cheap
  store probe every few seconds (a trivial read and write inside a transaction). The first
  success clears it. Recovery needs no user scan, so a freed disk heals on its own.
- **D-13: lifespan order is recovery, then worker.** Startup: validate dirs →
  `fail_active_jobs(<"server restarted" reason>)` → startup prune → `worker.start()`.
  Shutdown: `worker.stop()` → (only if the thread stopped) close Paperless and the store.
  `prune` moves out of the per-job `finally`; housekeeping must never fail the job path.

#### Startup auto-profiles (ROBU-07, ROBU-05 profile lock)
- **D-14: generation is the worker thread's first act, before it takes any job.** The server
  is already accepting requests and `/health` answers. A scan submitted meanwhile queues
  behind generation. A page loaded in those first seconds may list only `default` until
  reloaded, which is accepted. No generation code remains in the job path (`_process_job`
  no longer calls `_maybe_auto_generate`).
- **D-15: tried once per start.** Scanner off or unreachable at boot → log a WARNING carrying
  the real exception class (not the current blanket "scanner unreachable", which asserts a
  cause the code cannot know), keep the bare default. Retry = restart the service or run
  `saneless auto-profiles`. No retry before the first job, no periodic retry.
- **D-16: the config path that was actually loaded travels with the settings.** `load_settings`
  records it (for example `Settings.config_path: Path | None`, `None` when no file was
  found). The worker writes to that path, never to a path it re-derives. The duplicated
  three-path search list in `auto_profiles.resolve_config_path` and `config.load_settings`
  collapses to one.
- **D-17: no config file loaded (env-only) → profiles live in memory only.** Merge into
  `settings.profiles` under the profile lock so the dropdown works, write no file, and log at
  INFO that no config file was loaded so the generated profiles were not persisted (naming
  `--config` / the search locations). No file appears in the CWD, `$HOME`, or `/`.
- **D-18: the loaded file is not writable → profiles are used in memory, with a WARNING.**
  The WARNING names the path and the real `OSError` and says the profiles will not survive a
  restart. The outcome matches D-17 but is louder, because the operator did supply a file.
- **D-19: profile mutation and profile reads share one lock.** The worker's merge and every
  request-thread read of `settings.profiles` (the index dropdown, ROBU-08's unknown-profile
  check, the worker's own lookup) go through the same lock, or read a snapshot taken under
  it. Once the routes become `def` (ROBU-05), this concurrency is real.

#### Cross-site guard (ROBU-10)
- **D-20: Go-style fetch-metadata check with Origin fallback** (modelled on Go 1.25
  `net/http.CrossOriginProtection`). Browsers send `Sec-Fetch-Site` only to secure contexts
  (HTTPS or `localhost`), so on saneless's documented deployment,
  `http://<lan-ip>:8080`, it is **absent**. The review's rule ("reject when present and not
  same-origin") would protect nothing there. The rule:
  1. `Sec-Fetch-Site` present → allow `same-origin` and `none`; reject `same-site` and
     `cross-site` (a Paperless UI on the same host at another port is `same-site`, and is
     rejected);
  2. absent, `Origin` present → allow only if Origin's host[:port] matches `Host` **or**
     `X-Forwarded-Host` (D-21);
  3. both absent → allow (curl, scripts, non-browser clients are not CSRF vectors).
  The ROBU-10 tests cover all three branches, including the plain-HTTP branch.
- **D-21: `X-Forwarded-Host` is accepted, and the docs cover Host preservation.** A cross-site
  form or `fetch` cannot set `X-Forwarded-Host` without a CORS preflight saneless never
  answers, so a browser cannot forge it. Accepting it makes proxies that set it (Traefik,
  Caddy) work with no configuration. nginx by default neither preserves `Host` nor adds
  `X-Forwarded-Host`, so the docs say to do one or the other
  (`proxy_set_header Host $host`). No trusted-origins config key.
- **D-22: a rejection is a 403 through the shared error renderer** (D-01/D-04): htmx requests
  see a message in the slot saying the request was blocked because it did not come from this
  page; the log line names `Origin`, `Host`, and `X-Forwarded-Host` so a misconfigured proxy
  is diagnosable in one read.
- **D-23: app-wide middleware on every method except GET, HEAD and OPTIONS**, so a POST route
  added later cannot forget the check. Not a per-route dependency.

### Claude's Discretion
- **Vendored asset layout and SRI.** File location (for example `static/vendor/`), whether
  the version sits in the filename, how the SHA-384 `integrity` values are sourced and
  pinned, a test that the template's hash matches the file's bytes, and shipping the
  upstream license notices (htmx 0BSD, Pico MIT). Versions stay at today's pins: htmx
  **2.0.8**, Pico **2.1.1** (contract above). Confirm the wheel includes the files.
- **Server-owned Scan button (ROBU-04).** One shared include renders the button; status
  responses re-render it with `hx-swap-oob`; the form uses `hx-disabled-elt` in place of the
  `beforeRequest` handler; `app.js` and its `<script>` tag are deleted. The C-10 lesson
  applies: prove it in a browser, not by reading.
- **Metadata cache single-flight (ROBU-05).** Mechanism (per-key lock with a re-check, or
  similar) and its test. Stay inside ROBU-05. Stale-on-error is SWP-04, not this phase.
- **Retry-After value and queue depth.** Keep `maxsize=10`; pick a sensible fixed
  `Retry-After`; not configurable.
- **User-facing wording** of the 429 / 503 / 422 / 403 messages and the ERROR row texts; the
  "server restarted" reason text passed to `fail_active_jobs`.
- **Degraded-health parameters:** N, the probe interval, the probe's exact statement, and the
  `/health` 503 JSON shape (keep it minimal, per `docs/PRD.md`'s health requirement).
- **ROBU-08 limits:** title cap (review suggests `Form(max_length=256)`);
  `resource: Literal["tags", "correspondents"]`.
- **Where startup prune runs** and whether a periodic prune exists at all (it must not sit in
  the per-job path).
- **`wait_for_state` helper (ROBU-03)** and the conversion of worker tests that relied on a
  draining `stop()` (37 `worker.stop()` calls, 25 `time.sleep` calls in `tests/test_worker.py`
  today). Convert the tests that break under a non-draining stop. Wider sleep cleanup is not
  required here.
- **Whether generated profiles replace the bare `default` entry** in memory. Generation only
  runs when the set is exactly the bare default (and DPLX-07 always emits a `default`), so
  replacing it is today's intent. M-04's "merge only new names" was written against the old
  field-subset `is_bare_default`. Decide, and keep it under the D-19 lock.
- **CI browser job shape (ROBU-11):** install Chromium, run `-m browser`. Prove "no CDN egress"
  inside the test itself, for example with a Playwright route that aborts every request not
  addressed to the test server, so offline behaviour is proven locally too and not just
  assumed from the CI network.

### Deferred Ideas (OUT OF SCOPE)
- **Paperless connect timeout separate from the upload timeout, and caching the
  "unreachable" outcome** (M-01's second half). ROBU-05 does not include it, and no v2.0
  requirement found in `REQUIREMENTS.md` maps it. Worth checking at milestone audit whether
  it should become a requirement (Phase 29 "Timeouts" is the natural home).
- **Validating tag and correspondent ids against the warm cache** (N-20's extra suggestion):
  not in ROBU-08.
- **Configurable shutdown join, queue depth, or trusted origins.** Rejected for now as
  new config surface.
- **Periodic auto-profile retry while the profile set is bare.** Rejected (D-15);
  `saneless auto-profiles` covers it.
- Already owned elsewhere: queue position (APPL-08), flip owner token (APPL-09), stale-on-error
  cache (SWP-04), no-login notice (DOCS-05), config path logging (CFG-11), scan cancellation
  (HARD-03).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ROBU-01 | Worker loop survives any exception from pipeline, job store, or prune; logged with `exc_info`; keeps serving | § Pattern 1 (guarded loop, job-vs-loop failure split, degraded Event, `PRAGMA user_version` probe verified to write a WAL frame) |
| ROBU-02 | Full queue returns 429 + `Retry-After` + visible message; event loop never blocks; shutdown never blocks on worker | § Pattern 1 (`put_nowait` + `queue.Full`), § Pattern 3 (htmx-config shallow merge, `HX-Retarget` verified in source), § Pattern 4 (renderer), D-06 mechanism |
| ROBU-03 | Stop flag + bounded join; worker tests converted to `wait_for_state` | § Pattern 1 (`Queue.shutdown(immediate=True)` verified), § Test Inventory (`wait_for_state` already exists in `tests/conftest.py:228`) |
| ROBU-04 | Scan button re-enables without reload; server-owned via OOB; no duplicate `id="scan-btn"`; `app.js` deleted | § Pattern 5 (OOB outerHTML is synchronous, `hx-disabled-elt` re-enables only the detached node, `hx-disinherit` required), response-template split to avoid a duplicate id |
| ROBU-05 | Blocking routes are `def`; `/health` answers during a scan; cache single-flight; profile mutation locked | § Pattern 6 (verified: `def` handler runs on "AnyIO worker thread"; `/health` latency 0.00 s vs 2.80 s under `async def`), single-flight code, D-19 copy-on-write under lock |
| ROBU-06 | Non-terminal jobs failed with "server restarted" before worker starts; worker stops before store closes | § Pattern 7 (lifespan ordering, `stop()` returns whether the thread stopped) |
| ROBU-07 | Startup generation from the loaded config path; `is_bare_default` covers every untouched shape | § Pattern 8 (`PrivateAttr` verified not env-settable, unlike a public field; single search list; shapes table) |
| ROBU-08 | 422 for unknown profile / over-long title before a job exists; `resource` validated | § Pattern 6 (verified `Annotated[str, Form(max_length=256)]` and `Literal` query → 422 through the custom handler) |
| ROBU-09 | Vendored htmx/Pico with pinned versions + SHA-384 SRI; works offline | § Pattern 9 (hashes computed from registry-verified bytes; wheel inclusion verified; prek byte-corruption pitfall) |
| ROBU-10 | Cross-site POSTs rejected via `Sec-Fetch-Site`; default bind documented | § Pattern 10 (Go 1.25 `Check()` source quoted; W3C Fetch Metadata "potentially trustworthy URL" rule cited) |
| ROBU-11 | Browser test: click Scan, terminal status, button enabled, in CI, no CDN egress | § Pattern 11 (override pytest-playwright `context` with an egress-recording route; CI job YAML) |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- Python 3.14; `uv` only (no pip/poetry); `prek` for hooks (`uv run prek run --all-files`, never bare `prek run`, which checks only the staged set).
- Zero errors from `uv run ruff check .`, `uv run ruff format .`, `uv run ty check`, `uv run pyrefly check src tests` (always name the paths).
- **No `# type: ignore`, no `# noqa`, no disabling rules.** Fix issues properly. `app.py:103-110` shows the house workaround for a checker false positive: a typed `Any` widening, not a suppression.
- Ruff `D` rules: docstrings on every public module, class, and function. D203/D212 ignored, so write multi-line summaries on the second line. Ruff rule set includes `ASYNC`, `S`, `FBT`, `PL`, `ANN`, `EM`, `G` (use `logger.x("...%s", arg)`, never f-strings in log calls), `PT`, `DTZ`.
- Tests: `S101`, `ARG`, `S104-106` ignored in `tests/**`. `filterwarnings = ["error"]`, `timeout = 60` (signal), `--strict-markers`.
- Prefer external packages over hand-rolling, and prefer libraries available in Context7.
- **Playwright browser checks are automated, never "manual-only".**
- TDD mode is on (`workflow.tdd_mode: true`). A RED commit is allowed because commit hooks type-check `src/` only. Never `--no-verify`, never `SKIP=`, never a stub to force a RED commit through.
- pyrefly inside a gitignored `.claude/worktrees/` checkout filters every file unless paths are named. `use_worktrees: true` is set, so every verify command must be `uv run pyrefly check src tests`.
- Memory rule (`feedback_never_merge_prs`): Claude pushes branches and opens PRs; merging is the user's alone.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Guarded job loop, stop flag, degraded health, store probe | Worker thread (`worker.py`) | Job store (`job.py` probe method) | The loop owns its own failure accounting; the store owns the SQL. |
| Startup profile generation + profile lock | Worker thread (first act) | Config (`config.py` records the path) | D-14 puts generation in the worker thread (keeps SANE calls on one thread); the loaded path is a settings fact (D-16). |
| Crash recovery, startup prune, guarded close | Web lifespan (`app.py`) | Job store (`fail_active_jobs`, `prune`) | Ordering against `worker.start()` / `stop()` is a lifecycle concern (D-13). |
| Backpressure decision (429/503) | Worker API (`submit` result) | Routes (map to HTTP + rejection row) | The worker knows capacity and health; HTTP semantics belong to the route. |
| Error rendering (every 4xx/5xx) | Web: `web/errors.py` renderer | Exception handlers + cross-origin middleware call it | One rendering rule (D-01), reachable from middleware, which sits outside `ExceptionMiddleware`. |
| Cross-site guard | Web: pure ASGI middleware | Renderer | App-wide on unsafe methods (D-23). |
| Input validation (422) | Routes (FastAPI params + profile check) | Renderer (via `RequestValidationError` handler) | Validation happens before any store write (ROBU-08). |
| Scan button state | Server templates (shared include + OOB) | Browser (htmx `hx-disabled-elt` during the request only) | Server is the single source of truth (C-10). |
| Error visibility | Browser: htmx `responseHandling` config | Server `HX-Retarget` / `HX-Reswap` headers | htmx drops 4xx/5xx bodies by default. |
| Static assets | CDN-free package data (`web/static/vendor/`) | Wheel build (`uv_build`) | Offline LAN (N-21). |
| "Most recent job that ran" lookup | Job store query | Routes (`_current_or_recent_job`) | The skip predicate is data (D-06), so it lives in SQL. |

## Standard Stack

### Core (all already installed; no new runtime dependencies)
| Library | Version (installed, verified `uv pip list`) | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib `queue` | 3.14.2 | Bounded job queue with `put_nowait`, `get(timeout)`, `shutdown(immediate=True)` | `Queue.shutdown` added in 3.13 [CITED: docs.python.org/3.14/library/queue.html]; behaviour verified on the project interpreter |
| Python stdlib `threading` | 3.14.2 | `Event` (stop, degraded), `Lock` (profiles, cache keys) | Standard primitives |
| FastAPI | 0.135.1 | `def` routes on the threadpool, `Form(max_length=…)`, `Literal` params, `exception_handler` | Existing framework [VERIFIED: installed source] |
| Starlette | 0.52.1 | `ServerErrorMiddleware`/`ExceptionMiddleware` layering, pure ASGI middleware, `StaticFiles` | Existing [VERIFIED: installed source + Context7 `/kludex/starlette`] |
| uvicorn | 0.42.0 | Server; logs "Exception in ASGI application" on re-raise | Existing [VERIFIED: installed source] |
| pydantic / pydantic-settings | 2.12.5 / 2.13.1 | `PrivateAttr` for the loaded config path | Existing [VERIFIED: probe script] |
| htmx (vendored file, not a Python dep) | **2.0.8** (locked; npm latest is 2.0.10) | `responseHandling`, `hx-swap-oob`, `hx-disabled-elt`, `hx-disinherit` | Locked pin [VERIFIED: npm registry + source read] |
| Pico CSS (vendored file) | **2.1.1** (locked; also npm latest) | Styling | Locked pin [VERIFIED: npm registry] |

### Supporting (dev, already installed)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | 9.0.2 | Test runner | All tests |
| pytest-playwright | 0.7.2 | `page` / `context` fixtures | ROBU-11, ROBU-02 visibility, DARK tests in CI |
| playwright | 1.58.0 | Chromium automation, `BrowserContext.route` | Egress blocking |
| pytest-timeout | 2.4.0 | 60 s per-test ceiling | Already configured |
| anyio | 4.12.1 | (transitive) threadpool behind `def` routes | Nothing to add |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `Queue.shutdown(immediate=True)` | `get(timeout=0.1)` tick only | Satisfies D-07 literally but adds up to 100 ms per `stop()`, which is significant across ~140 lifespan/worker tests; also delays real shutdown. Rejected. |
| `Queue.shutdown` | A `threading.Condition` + `deque` bounded buffer | A hand-rolled `queue.Queue`. Rejected (don't hand-roll). |
| `ErrorCategory.REJECTED` marker (D-06) | Migration v3 adding a `rejected`/`started_at` column | STOR-03 said later phases should not add columns ad hoc; more code (ladder step, `_COLUMNS`, `Job`, `_row_to_job`). Rejected unless the user prefers a column. |
| `ErrorCategory.REJECTED` | In-memory `worker.last_job_id` fallback | Not durable: after a restart, `list_recent(1)` can still return a rejection row, violating D-06's letter. Rejected. |
| `ErrorCategory.REJECTED` | Ordering on a finish timestamp | Needs a new column and still shows a 503 rejection when no job ran. Rejected. |
| Pure ASGI middleware | `@app.middleware("http")` (`BaseHTTPMiddleware`) | Known contextvars and streaming limitations [CITED: Starlette docs, middleware.md]. Rejected. |
| SRI literal in `base.html` + bytes test | Compute hashes at startup and inject | Hashing whatever is on disk defeats the point of pinning. Rejected. |

**Installation:** none. The two vendored files come from the npm tarballs (see Pattern 9); no `npm install` in the repo.

## Package Legitimacy Audit

No Python packages are added. The two vendored front-end files come from npm tarballs and were checked as follows.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| htmx.org@2.0.8 | npm | first published 2020-05; 2.0.8 on 2025-10-25 | 194,009 / wk | github.com/bigskysoftware/htmx | [OK] | Approved (vendor `dist/htmx.min.js` only) |
| @picocss/pico@2.1.1 | npm | first published 2019-12; 2.1.1 on 2025-03-15 | 34,829 / wk | github.com/picocss/pico | [OK] | Approved (vendor `css/pico.min.css` only) |

- Tarball SHA-512 recomputed locally and matched the registry's `dist.integrity` for both.
- The npm tarball files are byte-identical (`cmp`) to the `cdn.jsdelivr.net` URLs `base.html` loads today.
- Neither file references an external URL (no `@import`, no `sourceMappingURL`; Pico's `url()` values are all `data:` URIs).
- Neither package's lifecycle scripts matter: nothing is installed, the files are copied.

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

## Architecture Patterns

### System Architecture Diagram

```
Browser (htmx 2.0.8 from /static/vendor, SRI-checked)
  |  POST /api/scan  (form, hx-disabled-elt=#scan-btn, hx-disinherit)
  v
uvicorn -> ServerErrorMiddleware [Exception handler -> render_error 500, re-raises]
        -> CrossOriginGuard (pure ASGI; unsafe methods only)
               | reject -> render_error(403) -----------------------------+
               v allow                                                    |
        -> ExceptionMiddleware [HTTPException 4xx/5xx, RequestValidationError 422 -> render_error]
        -> Router -> def handler (AnyIO threadpool)                       |
              |                                                           |
              | validate (profile under lock, title len)  -- 422 -------->+
              | job_store.create_job (PENDING)                            |
              | worker.submit(job) -> ACCEPTED | QUEUE_FULL | UNAVAILABLE |
              |      QUEUE_FULL/UNAVAILABLE: finish_job(ERROR, REJECTED) -> 429/503 ->+
              v ACCEPTED                                                  v
        status_response.html:                          render_error(request, status, msg):
          #status-area (poll if active)                   HX-Request? -> partials/error.html
          + OOB #scan-btn (server state)                               + HX-Retarget #status-message
          + OOB clear #status-message (scan success only)              + HX-Reswap innerHTML
                                                           else -> {"status":"error","detail":msg}
                                                           429 -> Retry-After (both branches)

Worker thread (daemon):
  start -> generate profiles once (bare default only; write to settings.config_path or keep in memory)
  loop until stop Event / queue.ShutDown:
     get(timeout=IDLE_TICK)
       Empty  -> if degraded: probe store (read + PRAGMA user_version write) -> success clears degraded
              -> rate-limited prune (guarded)
       job    -> _process_job: update_state / run_pipeline / finish_job
                   pipeline exception -> job ERROR (job failure, not counted)
                   store exception    -> propagates -> loop-level failure (count; N -> degraded Event)

Lifespan: validate dirs -> fail_active_jobs("server restarted") -> prune -> worker.start()
          ... yield ...
          worker.stop() [set stop, queue.shutdown(immediate), abort flip, join 5 s]
             stopped? -> paperless.close(); job_store.close()   else WARNING(job id), leave open
```

### Recommended Project Structure (changes only)
```
src/saneless/
├── config.py                 # single CONFIG_SEARCH_PATHS + find_config_file(); Settings._config_path PrivateAttr + config_path property
├── auto_profiles.py          # resolve_config_path deleted (callers use settings.config_path)
├── vocabulary.py             # + ErrorCategory.REJECTED (+ error_message arm); + WorkerHealth enum, SubmitResult enum, rejection message labels
├── job.py                    # + _SELECT_LATEST_RUN / latest_run_job(); + probe(); docstring (doc row 33)
├── worker.py                 # guarded loop, stop(), submit() -> SubmitResult, health, startup generation, profile lock + accessors
├── web/
│   ├── app.py                # lifespan ordering, install_error_handlers(app), add_middleware(CrossOriginGuard)
│   ├── errors.py             # NEW: render_error(), exception handlers
│   ├── cross_origin.py       # NEW: CrossOriginGuard pure ASGI middleware + is_cross_origin(headers, method)
│   ├── cache.py              # + get_or_fetch() single-flight
│   ├── routes.py             # every handler `def`; router comment; validation; 429/503; D-06 lookup
│   ├── templates/
│   │   ├── base.html         # vendored <link>/<script> with integrity; htmx-config meta; no app.js
│   │   ├── index.html        # form: hx-disabled-elt + hx-disinherit; {% include "partials/scan_button.html" %}; #status-message slot
│   │   └── partials/
│   │       ├── scan_button.html     # NEW: the only place the button markup exists (oob flag)
│   │       ├── status_response.html # NEW: status.html + OOB button (+ optional OOB message clear) for htmx routes
│   │       └── error.html           # NEW: message partial
│   └── static/
│       ├── app.css
│       ├── app.js            # DELETED
│       └── vendor/           # NEW
│           ├── htmx-2.0.8.min.js
│           ├── pico-2.1.1.min.css
│           ├── LICENSE-htmx.txt   # 0BSD, copied from the tarball
│           └── LICENSE-pico.md    # MIT, copied from the tarball
.pre-commit-config.yaml       # top-level exclude for static/vendor/
.gitattributes                # NEW: vendor files -text
.github/workflows/ci.yml      # + browser job
```

### Pattern 1: Guarded worker loop with stop flag, degraded health, and store probe (ROBU-01/03, D-07..D-12)

**What:** A loop that never exits on its own. Pipeline failures stay job failures; store failures in the loop's own bookkeeping count toward degraded.

**Verified facts it rests on:**
- `Queue.shutdown(immediate=True)` wakes a thread blocked in `get(timeout=5)` in 0.0002 s with `queue.ShutDown`. Later `put_nowait` raises `queue.ShutDown`, `qsize()` is 0, and `ShutDown` subclasses `Exception` [VERIFIED: ran on Python 3.14.2].
- `PRAGMA user_version = <current value>` inside `with conn:` appends a 4152-byte WAL frame on an otherwise idle database, so it genuinely exercises the write path. A read-only transaction does not [VERIFIED: ran against sqlite 3.34.1 in WAL mode].

```python
# Sketch -- names are recommendations.  Parameters are Claude's discretion (D-10/D-12):
_DEGRADED_AFTER = 3          # consecutive loop-level failures
_IDLE_TICK_SECONDS = 5.0     # get() timeout; also the probe cadence while degraded
_PRUNE_INTERVAL_SECONDS = 3600.0
_JOIN_SECONDS = 5.0          # D-08

def _run(self) -> None:
    self._generate_startup_profiles()       # D-14: first act; wrapped in its own try/except Exception
    while not self._stopping.is_set():
        try:
            job = self._queue.get(timeout=_IDLE_TICK_SECONDS)
        except queue.Empty:
            self._idle_housekeeping()        # probe when degraded, rate-limited prune; both guarded
            continue
        except queue.ShutDown:
            break
        try:
            self._process_job(job)           # returns normally after a *pipeline* failure
        except Exception:
            logger.exception("Worker loop failed while handling job %s", job.id)
            self._record_loop_failure()      # increments; sets self._degraded Event at N
            self._best_effort_fail(job.id)   # finish_job(ERROR, ...) inside its own try; failure only logged
        else:
            self._record_loop_success()      # resets the consecutive counter (does NOT clear degraded; the probe does)

def stop(self) -> bool:
    """Set the stop flag, wake the loop, abort an open flip wait, join; report whether it stopped."""
    self._stopping.set()
    self._queue.shutdown(immediate=True)     # queued-but-unstarted jobs are abandoned; startup recovery fails them
    coordinator = self._flip_coordinator
    if coordinator is not None:
        coordinator.arm()                    # safe at shutdown: pre-answering Abort is exactly the intent
        coordinator.signal_abort()
    if self._thread.is_alive():
        self._thread.join(timeout=_JOIN_SECONDS)
    return not self._thread.is_alive()

def submit(self, job: Job) -> SubmitResult:
    if not self.is_alive or self._degraded.is_set():
        return SubmitResult.UNAVAILABLE
    try:
        self._queue.put_nowait(job)
    except queue.Full:
        return SubmitResult.QUEUE_FULL
    except queue.ShutDown:
        return SubmitResult.UNAVAILABLE
    return SubmitResult.ACCEPTED
```

**Design notes for the planner:**
- **Job-vs-loop split falls out of the existing structure.** `_process_job`'s `try` already wraps only `run_pipeline` + `finish_job`. Restructure so the pipeline call has its own `except Exception` that records the job failure. A store exception from `update_state(SCANNING)`, from `finish_job` on either branch, or from `update_thumbnail` inside a callback then propagates to `_run` and is counted. A callback store failure propagates *through* `run_pipeline`, so catch the pipeline's exception, attempt `finish_job`, and let a failure of that `finish_job` escape as loop-level.
- **`prune` leaves `_process_job` entirely** (D-13). Recommend a rate-limited idle-tick prune (hourly) in addition to the startup prune, so a long-running appliance still honours `history_max_rows`. A prune failure is a loop-level failure (D-10 names prune) but can never fail a job.
- **Stop can race a new flip coordinator.** `stop()` may read `_flip_coordinator is None` just before the worker builds one for a job it had already dequeued. That job would reach `AWAITING_FLIP` and wait up to `flip_timeout_seconds` (600 s). In `_status_cb`, when `AWAITING_FLIP` arrives and `self._stopping.is_set()`, call `coordinator.signal_abort()` right after `arm()`. Also check `self._stopping` before starting a dequeued job.
- **Degraded state must be readable across threads.** Use a `threading.Event` for degraded (set and cleared only by the worker thread; read by request threads). The consecutive counter is touched only by the worker thread and needs no lock.
- **Recommended health model:** a `WorkerHealth` StrEnum (`HEALTHY`, `DEGRADED`, `DOWN`) with a total `match` + `assert_never` label function, following the Phase 21/24/25 pattern. `DOWN` = thread not alive; `DEGRADED` = Event set.

### Pattern 2: D-06 -- rejected-at-submit rows never become "most recent" (no migration)

**Mechanism (recommended):** add `ErrorCategory.REJECTED` (value `"REJECTED"`), and give the D-17 fallback a store query that skips it.

```python
# job.py -- follows the module's statement-constant discipline (S608-safe: only module literals interpolated)
_SELECT_LATEST_RUN = f"{_SELECT_ALL} WHERE error_category IS NOT ? ORDER BY created_at DESC LIMIT 1"
"""The newest job that was not rejected at submit.  `IS NOT` is NULL-safe in SQLite, so rows with no
category (every non-error job, and restart-failed jobs) are included."""

@_locked
def latest_run_job(self) -> Job | None:
    with self._conn:
        row = self._conn.execute(_SELECT_LATEST_RUN, (ErrorCategory.REJECTED.value,)).fetchone()
    return None if row is None else self._row_to_job(row)
```

- **Why this satisfies D-06 without changing D-17:** D-17's contract is "current job, else the most recent job". The rows it now skips are, by construction, jobs that never ran, so "the job that just ended" is preserved. **This is a mechanism choice inside D-06's latitude, not a D-17 contract change**, so no user escalation is needed.
- **Writing the row (D-05) reuses existing methods, with no new insert path:**
  - **429:** `create_job` (PENDING) → `submit` returns `QUEUE_FULL` → `finish_job(job.id, JobState.ERROR, error=<user text>, error_category=ErrorCategory.REJECTED)`. The row must exist before `put_nowait` in the healthy path anyway; otherwise the worker could dequeue an id with no row, and `UPDATE ... WHERE id=?` silently touches 0 rows.
  - **503:** the route checks the result and writes the same `create_job` + `finish_job` pair inside `try/except Exception` (log WARNING with `exc_info`; D-05 says the row is written only if the store accepts it) and returns 503 regardless.
- **Cost:**
  - `vocabulary.error_message` needs a `REJECTED` arm (total match, or ty/pyrefly fail).
  - `tests/test_vocabulary.py:70-80` pins "the five documented categories (CTR-05)" and must be updated.
  - APPL-04 (Phase 30) derives next-step advice from `ErrorCategory`, and a rejected submit genuinely needs its own advice ("wait and try again"), so the member earns its place.
- **Alternatives rejected** (see Alternatives table): a migration column, in-memory last-job id, finish-time ordering.

### Pattern 3: htmx 2.0.8 error-response configuration (D-01, D-02, D-03)

**Verified in `htmx.js` 2.0.8 source** [VERIFIED: tarball SHA-512 matches registry]:
- Default `responseHandling: [{code:'204',swap:false},{code:'[23]..',swap:true},{code:'[45]..',swap:false,error:true}]` (line 264).
- `mergeMetaConfig()` does `htmx.config = mergeObjects(htmx.config, metaConfig)`, a **shallow** merge (lines 5102-5114). A meta config listing only a `[45]..` entry would *replace* the whole array and drop the 2xx rule. **Restate all three.**
- The meta is read in `ready()` (on DOMContentLoaded), so it may sit anywhere in `<head>`.
- `handleAjaxResponse` reads `HX-Retarget` (via `resolveRetarget`, a document-level selector) and `HX-Reswap` **for every status code**, after `responseHandling` and before the `shouldSwap` check. Once `[45]..` has `swap: true`, the retarget applies to error responses (lines 4850-4870).
- Keeping `error: true` on the `[45]..` entry still fires `htmx:responseError`. Harmless, and the right semantics.

```html
<!-- base.html <head> -- JSON inside single-quoted attribute, as in htmx's own docs [CITED: Context7 /bigskysoftware/htmx docs.md "Configure htmx Response Handling"] -->
<meta name="htmx-config"
      content='{"responseHandling":[{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"[45]..","swap":true,"error":true}]}'>
```

```html
<!-- index.html: the slot is a SIBLING of the polled #status-area, so the 1 s outerHTML poll never replaces it -->
{% include "partials/status.html" %}
<div id="status-message" role="alert"></div>
```

**Clearing the slot on a successful scan only (D-03).** The `POST /api/scan` success response carries:
```html
<div id="status-message" hx-swap-oob="innerHTML"></div>
```
With `innerHTML`, `oobSwap` uses the OOB element's *children* (none) as the fragment, so the slot empties while keeping its `role="alert"` container [VERIFIED: `oobSwap` + `isInlineSwap` source, lines 1443-1510]. **Do not include this element in poll or flip responses**, or a 429 shown mid-scan would vanish within a second.

### Pattern 4: One error renderer, reachable from handlers and middleware (D-01, D-02, D-04, D-22)

**Verified layering** [VERIFIED: `fastapi/applications.py:1021-1067`, `starlette/middleware/errors.py`]:
```
ServerErrorMiddleware(handler = app.exception_handlers[Exception or 500])   # outermost; re-raises after responding
  -> user middleware (CrossOriginGuard)                                      # HTTPException raised here is NOT handled
    -> ExceptionMiddleware(handlers = everything else, MRO lookup)
      -> AsyncExitStackMiddleware -> router
```

**Verified behaviours** (scratch app on the installed stack):

| Case | Result |
|------|--------|
| `Annotated[str, Form(max_length=256)]` with 257 chars | 422 through a custom `RequestValidationError` handler, `loc=('body','title')`, custom headers preserved |
| `resource: Literal["tags","correspondents"]` query, `bogus` | 422 through the same handler |
| `raise fastapi.HTTPException(429, headers={"Retry-After": "10"})` in a `def` route | Handler registered on `starlette.exceptions.HTTPException` receives it (FastAPI's subclass, MRO lookup), headers intact |
| Router 404 and 405 (including `StaticFiles` 404 for a deleted `/static/app.js`) | Same `StarletteHTTPException` handler |
| `RuntimeError` in a `def` route | `Exception` handler's response is sent (500); the handler ran; the route ran on "AnyIO worker thread" |
| Same with default `TestClient(app)` | **The exception is re-raised into the test.** Use `TestClient(app, raise_server_exceptions=False)` for 500-path tests |
| uvicorn after the re-raise | Logs "Exception in ASGI application" with the traceback, then closes the transport because the response has started; the client has the full response [VERIFIED: `uvicorn/protocols/http/h11_impl.py:413-419`] |

The last row means **the catch-all traceback is logged twice** (the handler's own `exc_info` log from D-01, plus uvicorn's). Accept it and note it in a comment; the alternative (a catch-all ASGI middleware that swallows) contradicts D-01's "registers ... a catch-all Exception handler".

```python
# web/errors.py -- sketch
def render_error(request: Request, status_code: int, message: str, *, retry_after: int | None = None) -> Response:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)          # D-04: both branches
    if request.headers.get("HX-Request") == "true":
        headers |= {"HX-Retarget": "#status-message", "HX-Reswap": "innerHTML"}
        return request.app.state.templates.TemplateResponse(
            request, "partials/error.html", {"message": message}, status_code=status_code, headers=headers,
        )
    return JSONResponse({"status": "error", "detail": message}, status_code=status_code, headers=headers)

def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)       # 403/404/405/422(explicit)/429/503
    app.add_exception_handler(RequestValidationError, _validation_error)     # 422 -- never echo exc.errors() input values
    app.add_exception_handler(Exception, _unhandled)                         # 500 -- logger.error(..., exc_info=exc)
```
- Messages are developer constants and never interpolate exception text or user input. Recommend a vocabulary-owned enum + label function (templates own no vocabulary), for example `RequestRejection.QUEUE_FULL | UNAVAILABLE | INVALID_PROFILE | TITLE_TOO_LONG | INVALID_REQUEST | CROSS_SITE | INTERNAL` with a total `match`.
- `Request(scope).app` works inside ASGI middleware because `Starlette.__call__` sets `scope["app"] = self` before building the middleware stack [VERIFIED: `starlette/applications.py:103-107`].
- **Handler typing:** `add_exception_handler` annotations are loose. If ty or pyrefly object to a narrowed `exc: RequestValidationError` parameter, widen the parameter to `Exception` and narrow with `isinstance` inside; never suppress.

### Pattern 5: Server-owned Scan button with OOB swap (ROBU-04)

**Verified in htmx 2.0.8 source:**
1. `disableElements` resolves `hx-disabled-elt` **at request time** and stores element references (lines 3388-3401).
2. `xhr.onload` calls `responseHandler` (the swap) **before** `removeRequestIndicators` (lines 4572-4579).
3. With `defaultSwapDelay: 0` and no view transitions, `swap()` runs `doSwap()` synchronously (lines 2040-2046), including OOB swaps.
4. **Consequence:** when the response OOB-replaces `#scan-btn` via `outerHTML`, `removeRequestIndicators` strips `disabled` from the *detached old node* only. The server-rendered replacement keeps whatever state the server gave it.
5. Settling copies only `class`, `style`, `width`, `height` (`attributesToSettle`), so `disabled` is never "settled" back from the old node (lines 178, 1418-1440).
6. **Inheritance trap:** `findAttributeTargets` uses `getClosestAttributeValue`, which inherits from ancestors unless the ancestor has `hx-disinherit` (lines 466-497). The tags and correspondent `<select>`s and refresh buttons sit inside the form and issue their own requests (`hx-trigger="load"`, `hx-post`).
7. **2.0.8-specific:** 2.0.9's changelog fixes "`hx-disabled-elt` to preserve elements that were already `disabled` in the source HTML" [CITED: github.com/bigskysoftware/htmx CHANGELOG.md]. On 2.0.8, an inherited `hx-disabled-elt` would therefore *enable* a button the server rendered disabled, on page load during an active job.

```html
<!-- index.html form -->
<form hx-post="/api/scan" hx-target="#status-area" hx-swap="outerHTML"
      hx-disabled-elt="#scan-btn" hx-disinherit="hx-disabled-elt">
  ...
  {% include "partials/scan_button.html" %}
</form>
```
```html
{# partials/scan_button.html -- the ONLY copy of the button.  Keep `type="submit" id="scan-btn"` as the
   first two attributes: tests/test_web_state_rendering.py:_SCAN_BUTTON regex depends on that order. #}
<button type="submit" id="scan-btn"{% if oob %} hx-swap-oob="true"{% endif %}
        {% if job and job.is_active %}disabled{% if job.is_busy %} aria-busy="true"{% endif %}{% endif %}>
    {% if job and job.state == JobState.AWAITING_FLIP %}Waiting for flip&#8230;{% elif job and job.is_busy %}Scanning&#8230;{% else %}Scan{% endif %}
</button>
```
```html
{# partials/status_response.html -- used by every htmx route that renders status (scan, poll, flip continue/abort) #}
{% include "partials/status.html" %}
{% with oob = true %}{% include "partials/scan_button.html" %}{% endwith %}
{% if clear_message %}<div id="status-message" hx-swap-oob="innerHTML"></div>{% endif %}
```
- **Do not put the OOB button inside `partials/status.html`.** `index.html` includes `status.html`, so the full page would carry two `id="scan-btn"` elements (htmx ignores `hx-swap-oob` on initial render), which is exactly what ROBU-11 forbids.
- **Terminal transition:** the terminal poll response has no `hx-trigger`, so polling stops, and the same response's OOB button renders enabled. No JavaScript is involved.
- **Behaviour change to record in the UI spec:** between click and response, `hx-disabled-elt` only disables the button; the label becomes "Scanning…" when the response's OOB copy lands (one round-trip later) instead of instantly via `app.js`.
- **Delete `static/app.js` and its `<script>` tag.** Update `tests/test_browser.py::test_scan_button_re_enables_after_a_fallback_swap`: it mimics `app.js` and asserts `aria-busy == "false"`, which the server-rendered button never emits (it omits the attribute).

### Pattern 6: `def` routes, validation, single-flight cache, profile lock (ROBU-05, ROBU-08, D-19)

**Verified:** a blocking `async def` route delayed a concurrent `/health` by 2.80 s under `TestClient`; the same route as `def` delayed it by 0.00 s [VERIFIED: scratch run]. This is the discriminating test pattern for "`/health` answers during a scan":

```python
def test_health_answers_while_a_request_blocks(client: TestClient) -> None:
    gate = threading.Event()
    app = _app(client)
    app.state.paperless.get_tags = lambda: (gate.wait(5), [])[1]   # block inside a route's I/O
    app.state.cache.invalidate("tags")
    worker = threading.Thread(target=lambda: client.get("/api/tags"))
    worker.start()
    try:
        time.sleep(0.2)                      # let the slow request enter its handler
        started = time.monotonic()
        assert client.get("/health").status_code == 200
        assert time.monotonic() - started < 1.0
    finally:
        gate.set()
        worker.join(5)
```
Pair it with a structural guard that fails the moment someone "fixes" a route back to `async def`:
```python
def test_no_route_handler_is_a_coroutine(app: FastAPI) -> None:
    for route in app.routes:
        if isinstance(route, APIRoute):
            assert not inspect.iscoroutinefunction(route.endpoint), route.path
```

**Validation (ROBU-08):**
```python
@router.post("/api/scan")
def start_scan(
    request: Request,
    profile: Annotated[str, Form()],
    title: Annotated[str, Form(max_length=_TITLE_MAX)] = "",     # _TITLE_MAX = 256
    tags: list[int] = _TAGS_FORM_DEFAULT,
    correspondent: Annotated[int | None, Form()] = None,
) -> Response:
    state = request.app.state
    if not state.worker.has_profile(profile):                    # snapshot under the D-19 lock
        raise HTTPException(status_code=422, detail=...)          # before create_job -> no row
    ...

@router.post("/api/cache/invalidate")
def invalidate_cache(request: Request, resource: Literal["tags", "correspondents"]) -> Response: ...
```

**Single-flight cache** (per-key lock + re-check; failure is not cached, and SWP-04 owns stale-on-error):
```python
def get_or_fetch(self, key: str, fetch: Callable[[], list[dict[str, object]]]) -> list[dict[str, object]]:
    cached = self.get(key)
    if cached is not None:
        return cached
    with self._locks_guard:
        key_lock = self._key_locks.setdefault(key, threading.Lock())
    with key_lock:
        cached = self.get(key)            # another thread may have filled it while we waited
        if cached is not None:
            return cached
        data = fetch()                    # raises -> lock released by `with`, nothing cached
        self.set(key, data)
        return data
```
Test: 8 threads behind a `threading.Barrier`, `fetch` sleeps 0.1 s and counts calls; assert the count is 1 and every thread got the same list. **Known limitation (deferred, M-01 second half):** while Paperless is unreachable, waiting threads retry the fetch one after another, each paying the connect timeout. Name this in the docstring and point to the deferred item.

**Profile lock (D-19), recommended shape:** the worker owns `_profiles_lock` and exposes `profile_names() -> list[str]` and `has_profile(name) -> bool`. Its own lookup in `_process_job` goes through a locked read. The startup merge **builds a new dict and rebinds `settings.profiles` under the lock** rather than mutating in place. Any unlocked reader (for example `pipeline.py:1125`, which runs on the worker thread after generation has finished) therefore sees either the old or the new dict, never one mutated mid-iteration.
- **Replace vs merge (discretion):** replace. Generation runs only when the set is exactly the full-model bare default (Phase 25 WR-04), and DPLX-07 always emits `default`, so replacing loses nothing. Re-check `is_bare_default` under the lock immediately before rebinding.

### Pattern 7: Lifespan ordering and guarded close (ROBU-06, D-13, D-09)

```python
@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    validate_settings_dirs(settings)
    failed = job_store.fail_active_jobs(RESTART_REASON)      # e.g. "Interrupted: the server restarted before this scan finished"
    if failed:
        logger.warning("Marked %d interrupted job(s) as failed at startup", failed)
    _guarded_prune(job_store, settings)                      # log WARNING with exc_info; never blocks startup
    worker.start()
    yield
    if not worker.stop():
        logger.warning(
            "Scan worker did not stop within %s s (job %s still running); leaving the job store "
            "and Paperless client open for process exit", JOIN_SECONDS, worker.current_job_id,
        )
        return
    paperless.close()
    job_store.close()
```
- `stop()` blocks the event loop for at most 5 s during lifespan shutdown, when uvicorn is no longer serving. That satisfies "shutdown never blocks on the worker" (bounded). Wrapping it in `anyio.to_thread.run_sync` is optional.
- **Open design point (see Open Questions):** if `fail_active_jobs` itself raises at startup (disk full), either abort startup, or start the worker already degraded so its first successful probe reconciles the orphans. D-13 does not say which.

### Pattern 8: The loaded config path travels with `Settings` (D-16, D-17, D-18)

**Verified:** a public `config_path: Path | None = None` field on `Settings` is populated from `SANELESS_CONFIG_PATH` (and would be from a TOML key), which is wrong. A `PrivateAttr` is not settable from the environment and does not appear in validation. It does participate in model `__eq__` (two `Settings` with different paths compare unequal); no test compares whole `Settings` objects [VERIFIED: probe script + grep].

```python
# config.py
CONFIG_SEARCH_PATHS: tuple[Path, ...] = (
    Path("./saneless.toml"),
    Path.home() / ".config" / "saneless" / "config.toml",
    Path("/etc/saneless/config.toml"),
)

class Settings(BaseSettings):
    ...
    _config_path: Path | None = PrivateAttr(default=None)

    @property
    def config_path(self) -> Path | None:
        """The TOML file these settings were loaded from, or None when only defaults and env were used."""
        return self._config_path

def load_settings(config_path: str | None = None) -> Settings:
    path = Path(config_path) if config_path else next((p for p in CONFIG_SEARCH_PATHS if p.exists()), None)
    settings = _build_settings(toml_file=path)
    settings._config_path = path       # an explicit --config is recorded even if missing; CFG-02 (Phase 27) makes that exit 2
    return settings
```
- `auto_profiles.resolve_config_path` is deleted.
- **CLI `auto-profiles`** writes to `settings.config_path`, or `Path("./saneless.toml")` when it is `None`. That preserves today's explicit-command behaviour and is documented in `cli-commands.md`; the daemon never uses that fallback (D-17). Flag it for CFG-03 (XDG) in Phase 27.
- **Worker startup generation outcomes:**
  - `config_path is None` → merge in memory, INFO naming `--config` and `CONFIG_SEARCH_PATHS` (D-17).
  - `write_profiles_to_config` raises `OSError` → merge in memory, WARNING naming the path and the error (D-18).
  - `ConfigError` (`[profiles]` not a table) → same handling as D-18.
  - Scanner call raises → WARNING with `type(exc).__name__` and `exc_info=True`; keep the bare default (D-15).
- Update the test that proves no stray files: run generation with `config_path=None` inside a `monkeypatch.chdir(tmp_path)` and assert `tmp_path` has no new files.

**`is_bare_default` shapes to cover (ROBU-07 clause 2).** Existing tests already cover: bare `Settings()`, multiple profiles, customised default, `auto_generated`, manual-duplex default, another field customised, and TOML spelling out default values. Add these untouched shapes:

| Shape | Expected |
|-------|----------|
| TOML file with no `[profiles]` section at all | True |
| TOML with an empty `[profiles.default]` table | True |
| Env-only, no file (`load_settings()` with nothing found) | True |
| Env var setting a default field to its default value (e.g. `SANELESS_PROFILES__DEFAULT__RESOLUTION=300`) | True [ASSUMED: pydantic-settings nested-env merge yields an equal model; the test will prove it] |
| `title = ""` written via the alias | True |
| Legacy `source = "Manual Duplex"` default | False (translated to `duplex = "manual"`) |

Parametrize one test over these.

### Pattern 9: Vendored assets with SRI (ROBU-09)

**Computed from registry-verified tarball bytes** (tarball SHA-512 == npm `dist.integrity`; files `cmp`-identical to jsDelivr):

| File (vendored as) | Source URL | Bytes | `integrity` |
|---|---|---|---|
| `static/vendor/htmx-2.0.8.min.js` | `https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.8.tgz` → `package/dist/htmx.min.js` | 51250 | `sha384-/TgkGk7p307TH7EXJDuUlgG3Ce1UVolAOFopFekQkkXihi5u/6OCvVKyz1W+idaz` |
| `static/vendor/pico-2.1.1.min.css` | `https://registry.npmjs.org/@picocss/pico/-/pico-2.1.1.tgz` → `package/css/pico.min.css` | 83319 | `sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU` |
| `static/vendor/LICENSE-htmx.txt` | same tarball → `package/LICENSE` (Zero-Clause BSD) | | |
| `static/vendor/LICENSE-pico.md` | same tarball → `package/LICENSE.md` (MIT) | | |

```html
<link rel="stylesheet" href="/static/vendor/pico-2.1.1.min.css"
      integrity="sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU">
<link rel="stylesheet" href="/static/app.css">
<script src="/static/vendor/htmx-2.0.8.min.js"
        integrity="sha384-/TgkGk7p307TH7EXJDuUlgG3Ce1UVolAOFopFekQkkXihi5u/6OCvVKyz1W+idaz"></script>
```
- **Fetch in a task step with the exact verification used here:** download the tarball, check `openssl dgst -sha512 -binary | base64` against `npm view <pkg>@<ver> dist.integrity`, extract, copy the two files and licences, then compute `openssl dgst -sha384 -binary <file> | base64 -w0`.
- **Wheel inclusion verified:** `uv build --wheel` (uv_build 0.10.x) packaged a probe `static/vendor-probe/probe.min.js` and an extensionless `LICENSE` under `saneless/web/static/`. The Dockerfile builds the wheel, so the image carries the files too. `.gitignore` does not match the vendor paths (`git check-ignore` exit 1).
- **Pinning test** (non-browser, runs in the fast CI job): parse every `integrity="sha384-…"` in `base.html`, map `href`/`src` to `STATIC_DIR`, and assert `base64(sha384(bytes)) == value`. Also assert:
  - no `http://` or `https://` in any template;
  - `app.js` is absent from disk and from templates;
  - `pico.colors.css` is absent;
  - `<html` carries no `data-theme`;
  - `app.css` still contains `--saneless-status-fallback`;
  - both licence files exist.
- **Same-origin SRI** needs no `crossorigin` attribute [ASSUMED: SRI's CORS requirement applies only to cross-origin fetches; the offline browser test proves the files load].

### Pattern 10: Cross-origin guard (ROBU-10, D-20..D-23)

**Go 1.25 `(*CrossOriginProtection).Check`, verbatim logic** [VERIFIED: `go/src/net/http/csrf.go` on `release-branch.go1.25`]:
1. Method `GET`, `HEAD`, `OPTIONS` → allow.
2. `Sec-Fetch-Site`: `""` → fall through; `"same-origin"` or `"none"` → allow; any other value → reject (unless exempt).
3. `Origin == ""` → allow ("Either the request is same-origin or not a browser request").
4. `url.Parse(origin).Host == req.Host` → allow. Host includes the port, and the scheme is not compared ("We fail open ... HSTS").
5. Otherwise reject. `Origin: null` parses to an empty host, so it is rejected.

**Why branch 2 is needed** [CITED: w3c.github.io/webappsec-fetch-metadata]: the Fetch Metadata spec's header-append algorithm returns early when the request URL "is not a potentially trustworthy URL". `http://192.168.x.y:8080` is not potentially trustworthy, so no `Sec-Fetch-Site` is sent. `http://127.0.0.1` and `http://localhost` *are* potentially trustworthy (Secure Contexts), so a Playwright test against the session server only exercises branch 1.

```python
# web/cross_origin.py -- pure ASGI (Starlette docs: BaseHTTPMiddleware has contextvars limitations)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

def cross_origin_verdict(method: str, headers: Headers) -> bool:
    """Return True when the request must be rejected (Go 1.25 algorithm + X-Forwarded-Host, D-20/D-21)."""
    if method in _SAFE_METHODS:
        return False
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site not in {"same-origin", "none"}
    origin = headers.get("origin")
    if origin is None:
        return False
    origin_host = urlsplit(origin).netloc.lower()          # "null" -> "" -> never matches
    candidates = {headers.get("host", "").lower()}
    candidates |= {h.strip().lower() for h in headers.get("x-forwarded-host", "").split(",") if h.strip()}
    return origin_host == "" or origin_host not in candidates

class CrossOriginGuard:
    def __init__(self, app: ASGIApp) -> None: self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            if cross_origin_verdict(request.method, request.headers):
                logger.warning("Blocked cross-site %s %s: Origin=%r Host=%r X-Forwarded-Host=%r Sec-Fetch-Site=%r", ...)
                response = render_error(request, 403, <CROSS_SITE message>)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
```
- Log header values with `%r` so a crafted header cannot inject log lines.
- **Recommendation beyond Go:** compare lowercased hosts (Go compares exact strings; browsers lowercase `Origin`, and curl can send a mixed-case `Host`). Do not normalise default ports: Go doesn't, and browsers omit `:80`/`:443` in both headers.
- **Proxy nuance for the docs:** behind an **HTTPS** reverse proxy the browser sends `Sec-Fetch-Site: same-origin`, so branch 1 allows the request and Host preservation is irrelevant. Host / `X-Forwarded-Host` matters only for a **plain-HTTP** proxy. Document `proxy_set_header Host $host;` (or `X-Forwarded-Host $host`) for nginx.
- **TestClient sends neither header**, so every existing POST test lands in branch 3 and passes unchanged. `TestClient`'s default `Host` is `testserver`.
- **Generative test for D-23:** iterate `app.routes`, and for every route whose `methods` contains a non-safe method, POST with `Sec-Fetch-Site: cross-site` and assert 403. A future POST route is covered automatically.

### Pattern 11: Browser tests offline, and the CI job (ROBU-11, ROBU-09, DARK tests)

**Egress proof for every browser test in the module:** override pytest-playwright's `context` fixture (a fixture may request the same-named fixture it overrides). `page` then derives from the routed context for every browser test, DARK-01/02 included.

```python
@pytest.fixture
def context(context: BrowserContext, browser_server: _BrowserServer) -> Iterator[BrowserContext]:
    """Abort and record every request not addressed to the test server, proving the UI needs no internet."""
    blocked: list[str] = []
    def _gate(route: Route) -> None:
        url = route.request.url
        if url.startswith(browser_server.url + "/") or url == browser_server.url:
            route.continue_()
        else:
            blocked.append(url)
            route.abort()
    context.route("**/*", _gate)
    yield context
    assert blocked == [], f"the page tried to reach the internet: {blocked}"
```
[CITED: playwright.dev/python/docs/api/class-browsercontext `route`; class-route `abort`/`continue_`]. `data:` URIs (Pico's SVG icons, thumbnails) are not network requests and are not routed [ASSUMED]. Keep `test_pico_css_applied` and `test_htmx_loaded` as the load canaries, and rewrite the module docstring that describes the CDN dependency.

**ROBU-11 test outline (deterministic DONE):**
1. Patch `app.state.paperless.upload_document` to return `UploadResult(delivered_to_api=True, task_uuid="t")` and `app.state.paperless.poll_task` to return `None`. It is the same instance the worker holds, and `test_web.py`'s `mock_paperless` does the same. **Without this**, the upload to `localhost:9999` spends about 3 s in retry backoff (`time.sleep(2**attempt)` for attempts 0 and 1) before ending ERROR, beyond Playwright's 5 s default `expect` timeout once pipeline time is added.
2. `page.goto(url)`; assert `page.locator("script[src*='app.js']")` has count 0.
3. Click `#scan-btn`; `expect(page.locator("#status-area .status-done")).to_be_visible(timeout=15_000)`.
4. `expect(page.locator("#scan-btn")).to_be_enabled()`; `assert page.evaluate("document.querySelectorAll('[id=\"scan-btn\"]').length") == 1`.
5. `page.request.get(url + "/static/app.js").status == 404`.
6. Teardown: restore the patched attributes and delete the created job rows (the existing fixtures show the session-scoped cleanup discipline).

**ROBU-02 visibility test outline:** give `_BrowserTestScanner` a `threading.Event` gate (open by default) that `scan_pages` waits on.
1. Close the gate and `page.goto` (idle page, button enabled).
2. Fill the queue from the test thread via `httpx.post(url + "/api/scan", data=...)` until one returns 429 (no `Origin`, so branch 3 allows it).
3. Click Scan in the browser; `expect(page.locator("#status-message")).to_contain_text(<queue-full copy>)`, and assert `#status-area` still exists.
4. Open the gate, wait for all rows to reach terminal, delete them.

**CI job** (same pinned action SHAs as the existing jobs; `python-sane` compiles, so SANE headers are required before `uv sync`):
```yaml
  browser:
    name: browser
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install SANE development headers
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends libsane-dev
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - run: uv sync --locked
      - run: uv run playwright install --with-deps chromium
      - run: uv run pytest -m browser
```
[CITED: playwright.dev/python/docs/browsers "install --with-deps"]. The `test` job keeps `-m "not browser and not sane_hardware"`. Update `CONTRIBUTING.md:60-90` (CI job table, and the "browser tests run locally only" text).

### Anti-Patterns to Avoid
- **`hx-disabled-elt` on the form without `hx-disinherit`:** re-enables a server-disabled button on page load (2.0.8 bug, inheritance). This is C-10 again.
- **An OOB button inside `partials/status.html`:** duplicate `id="scan-btn"` on the full page.
- **A meta `htmx-config` with only a `[45]..` entry:** shallow merge wipes the 2xx swap rule, and the whole UI stops swapping.
- **Raising `HTTPException` from the cross-origin middleware:** `ExceptionMiddleware` is inside it, so the response becomes an unhandled 500.
- **A shutdown sentinel `put` or a blocking `put` in `submit`:** C-09.
- **A public `Settings.config_path` field:** settable from env and TOML.
- **Mutating `settings.profiles` in place from the worker thread:** iterate-while-mutate race with `def` routes.
- **Echoing `RequestValidationError.errors()` into the partial or logs:** it contains the raw 100k-character title.
- **Committing vendored files through the default prek hooks:** bytes change, SRI fails, assets refuse to load.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Waking a blocked worker for shutdown | Sentinel objects, `Condition`+`deque` buffers | `queue.Queue.shutdown(immediate=True)` | Stdlib since 3.13; no capacity dependency; instant wake |
| Bounded, non-blocking enqueue | Size checks + `put` | `Queue.put_nowait` → `queue.Full` | Atomic check-and-put |
| Request validation | Manual `len(title)` / resource checks | `Annotated[str, Form(max_length=…)]`, `Literal[...]` params | FastAPI produces `RequestValidationError` → the shared 422 renderer |
| Threadpool offload of blocking routes | `run_in_executor` wrappers | Plain `def` handlers | FastAPI already runs them via `run_in_threadpool` |
| Button re-enable logic | JavaScript lifecycle handlers | `hx-swap-oob` server render + `hx-disabled-elt` | Server is the one source of truth (C-10) |
| Error body visibility | JS `htmx:beforeSwap` handlers | `responseHandling` meta config + `HX-Retarget` / `HX-Reswap` headers | Declarative, verified in source |
| CSRF defence | Tokens / cookies | Fetch Metadata + Origin check (Go 1.25 algorithm) | No state, no template changes; D-20 |
| "Test server only" network | Custom proxies / firewalls in CI | Playwright `context.route` abort + record | Proven in-test, locally and in CI |
| Integrity pinning | Runtime hashing | Literal `integrity` + bytes test | The browser enforces it |

**Key insight:** each bug this phase closes came from a second, hand-written copy of something the platform already provides (a JS copy of the button rule, a hand-rolled search list, a sentinel protocol). The fixes are deletions plus declarative configuration.

## Common Pitfalls

### Pitfall 1: prek rewrites vendored minified files
**What goes wrong:** `end-of-file-fixer` appends `\n` to `htmx.min.js` (ends in `;`) and `pico.min.css` (ends in `}`). `trailing-whitespace` / `mixed-line-ending` may touch them too. The committed bytes no longer match `integrity`, and the browser silently refuses to execute or apply them.
**Why it happens:** the hooks run on every staged file at commit (`.pre-commit-config.yaml`).
**How to avoid:** add a top-level `exclude: ^src/saneless/web/static/vendor/` to `.pre-commit-config.yaml`, and a `.gitattributes` line `src/saneless/web/static/vendor/** -text` (no EOL conversion on Windows checkouts). The SRI bytes test is the backstop.
**Warning signs:** a commit touching `vendor/` shows "Fixing …" hook output; `test_browser` canaries fail with unstyled pages.

### Pitfall 2: generation now runs at app start in every test app with a bare default
**What goes wrong:** `tests/test_web_state_rendering.py` (`profiles={"default": ProfileConfig()}`) and `conftest.default_settings` (bare) now trigger startup generation.
- The state-rendering `_StubScanner` reports Flatbed, so the in-memory profiles change (for example `default` + `flatbed`) while requests read them.
- In worker tests, `MagicMock(spec=ScannerBackend)` returns MagicMocks from `get_devices` / `get_capabilities`, so generation likely raises inside and logs a WARNING. That extra log record can break `caplog` assertions (`test_the_log_says_whether_a_signal_was_claimed_or_dropped`) and call-count assertions on `mock_scanner`.
**How to avoid:** run the full suite right after the generation move. Tests that must not generate either use a non-bare profile set or assert on specific records. Because `Settings(...)` built directly has `config_path=None`, D-17 guarantees no file writes. Today's code could write `./saneless.toml` into the repo root; the repo currently has an untracked-looking `saneless.toml` there, so check it is not a test artefact.
**Warning signs:** new WARNING lines in worker tests, a flaky profile-dropdown assertion.

### Pitfall 3: a non-draining `stop()` abandons queued jobs in tests
**What goes wrong:** `submit(job); time.sleep(0.5); worker.stop(); assert state == DONE` works today because the sentinel drains the queue (N-24). With `shutdown(immediate=True)`, anything still queued is discarded.
**How to avoid:** replace `time.sleep(...)` before `stop()` with `wait_for_state(store, job.id, TERMINAL_STATES)` (fixture exists: `tests/conftest.py:228-295`). See the inventory below.

### Pitfall 4: 500-path tests explode in `TestClient`
**What goes wrong:** `ServerErrorMiddleware` re-raises after sending the handler's response, and `TestClient` re-raises by default.
**How to avoid:** `TestClient(app, raise_server_exceptions=False)` for catch-all tests. Add a test-only failing route with `app.add_api_route` on the fixture app.

### Pitfall 5: the flip-wait race at shutdown
**What goes wrong:** `stop()` finds no coordinator; the worker then starts a dequeued manual-duplex job, reaches `AWAITING_FLIP`, and waits 600 s. The join expires; in tests the thread lingers.
**How to avoid:** check `_stopping` before starting a job and on the `AWAITING_FLIP` event (`arm()` then `signal_abort()`).

### Pitfall 6: a store failure strands a job in an active state while degraded
**What goes wrong:** `finish_job` fails, so the row stays `SCANNING`. The status area polls "Scanning…" with a disabled button until restart.
**How to avoid:** a best-effort `finish_job(ERROR)` in the loop guard. **Recommended (open question):** when the idle probe succeeds, run `fail_active_jobs(<reason>)` *before* clearing degraded. At that moment the queue is empty (the `get` just timed out), no job is current, and D-11 rejected every submit while degraded, so every active row is an orphan.

### Pitfall 7: `Playwright expect` default 5 s timeout vs pipeline time
**What goes wrong:** the ROBU-11 wait for a terminal state times out when Paperless is unreachable (about 3 s of retry backoff plus scan).
**How to avoid:** stub the Paperless instance methods for DONE, and pass an explicit `timeout=15_000`.

### Pitfall 8: `Origin: null` from a same-origin POST
**What goes wrong:** a reverse proxy adds `Referrer-Policy: no-referrer`; browsers then send `Origin: null` on same-origin POSTs over plain HTTP, and branch 2 rejects every scan with 403.
**How to avoid:** document it in the proxy guidance. The 403 log line (Origin/Host/X-Forwarded-Host) makes it diagnosable [ASSUMED: Fetch spec serialises the request origin as `null` under `no-referrer` for non-CORS requests; not re-verified this session].

### Pitfall 9: Docker "unhealthy" does not restart a compose container
**What goes wrong:** operators assume a degraded 503 makes Docker restart the container. `restart: unless-stopped` restarts only on exit.
**How to avoid:** say so in `docs/reference/docker.md` when documenting degraded health [ASSUMED: Docker Engine does not act on HEALTHCHECK status outside Swarm; confirm before writing the doc sentence].

## Code Examples

The patterns above carry the load-bearing snippets. Two more short ones:

### Store probe (D-12)
```python
@_locked
def probe(self) -> None:
    """Prove the database can be read and written: a count, then a same-value user_version write (appends a WAL frame)."""
    with self._conn:
        self._conn.execute(_COUNT_JOBS).fetchone()
        version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
        # A PRAGMA argument cannot be bound; `version` is an int read from the database itself.
        self._conn.execute(f"PRAGMA user_version = {version}")
```

### `/health` with a total enum
```python
@router.get("/health", response_model=None)
def health(request: Request) -> dict[str, str] | JSONResponse:
    match request.app.state.worker.health:
        case WorkerHealth.HEALTHY:
            return {"status": "ok"}
        case WorkerHealth.DEGRADED:
            detail = "job store failing"
        case WorkerHealth.DOWN:
            detail = "worker thread is down"
        case _:
            assert_never(request.app.state.worker.health)
    return JSONResponse(status_code=503, content={"status": "error", "detail": detail})
```
(Shape note: ruff `PLR0911` caps returns at 6, and ty/pyrefly need the single-assignment-per-arm shape recorded in ICM for Phase 21.)

## Test Inventory (what breaks, what to convert)

**`tests/test_worker.py`** (2119 lines):

| Tests | Why affected | Action |
|---|---|---|
| `test_worker_processes_job` (:190), `test_worker_sets_error_on_failure` (:221), `TestScanWorkerManualDuplex` (:347, :391, :433, :472, :548), `TestWorkerIntermediateStates` (:790, :837), `TestWorkerErrorCategories` (:888-:1016, five), `test_jobs_processed_sequentially` (:1499, two jobs), `TestWorkerEnumDispatch::test_worker_status_cb_dispatches_on_pipeline_event` (:1707) | `sleep` + `stop()` + assert terminal: relied on draining `stop()` as a backstop | Convert to `wait_for_state(..., TERMINAL_STATES)` before `stop()` (ROBU-03) |
| `test_worker_prunes_after_job_completion` (:1810), `test_finish_prunes_history_after_a_fallback_outcome` (:2094) | Prune leaves the per-job path (D-13) | Rewrite: startup prune in lifespan, rate-limited idle prune in the worker, and "prune raising does not kill the worker / does not fail the job" |
| `TestLazyAutoGenerate` (:1531-:1700, four tests) | Monkeypatch `saneless.worker.resolve_config_path` (deleted); generation moves to startup | Rewrite as startup-generation tests: bare default → generated in memory; `config_path` set → written there; unwritable → WARNING + in memory; scanner raises → WARNING naming the class; tried once |
| `test_worker_starts_and_stops` (:173) | Keeps passing | Extend: `stop()` returns True; a second `stop()` is safe; `submit` after stop → `UNAVAILABLE` |
| `TestWorkerFinish` (:1886+), `TestWorkerPassB` (:1135+), `TestFlipSignalsAreJobScoped` (:1321+) | Already use `wait_for_state` or gated scanners | Re-run; check gated-scanner tests still release their gates before `stop()` (the thread otherwise lingers up to 5 s per test) |
| `test_worker.py:1026` comment referencing "`ScanWorker.stop()`'s 5 s join" | Still true | Keep |
| **New:** guard (store raises in `update_state` / `finish_job` / `prune`, worker alive, next job served, `exc_info` logged); degraded after N; `/health` detail; probe clears; stop wakes within 0.5 s from idle; stop aborts an open flip; queue full → `QUEUE_FULL` | | Add |

**`tests/test_web.py`:**
- `test_cache_invalidate` (`?resource=tags`) still 200. Add `bogus` → 422 with no side effects.
- `test_scan_form_submit` still 200; add assertions for OOB `#scan-btn` + message clear.
- `test_page_loads` should additionally assert `count('id="scan-btn"') == 1`.
- `test_scan_form_no_hx_on` stays.
- Health tests extend for degraded/down.

**`tests/test_web_state_rendering.py`:**
- `_SCAN_BUTTON` regex requires `<button type="submit" id="scan-btn"` first; the shared include must keep that order.
- Parametrized button tests read `GET /`; add poll-response variants asserting the OOB copy mirrors the index copy for every `JobState`.

**`tests/test_browser.py`:**
- Module docstring and the `_PICO_SURFACE` comment (CDN, "held for phase 26").
- `test_scan_button_re_enables_after_a_fallback_swap` (`_DISABLE_SCAN_BUTTON` mimics `app.js`; asserts `aria-busy == "false"`) must be rewritten to rely on the OOB response.
- Fixture-level `worker._current_job_id` writes remain valid if the attribute name is kept.

**`tests/test_auto_profiles.py`:** `TestResolveConfigPath` (:400-411) is replaced by `CONFIG_SEARCH_PATHS` / `load_settings` path-recording tests in `tests/test_config.py`.

**`tests/test_vocabulary.py:70-80`:** the ErrorCategory membership set gains `REJECTED`.

**`tests/test_outcomes_e2e.py:90-95`:** the comment names `resolve_config_path()`; update it.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Sentinel `None` in queue to stop a worker | `Queue.shutdown()` | Python 3.13 (2024-10) | No capacity dependency, instant wake |
| htmx 1.x hardcoded "don't swap errors" | htmx 2 `responseHandling` config | htmx 2.0 | Declarative error swapping |
| CSRF tokens | Fetch Metadata (`Sec-Fetch-Site`) + Origin fallback | Go 1.25 (2025-08) standard library adopted it | Stateless; all modern browsers since 2023 per Go docs |
| `hx-disabled-elt` strips pre-existing `disabled` | Preserves it | htmx 2.0.9 (2026-04-15) | Pinned 2.0.8 still has the old behaviour, so use `hx-disinherit` |

**Deprecated/outdated:** `static/app.js` lifecycle handlers (C-10); `UI-SPEC.md` § interaction contract lines 305-346 describing them (ui-phase should update it).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A nested env var set to a default value (`SANELESS_PROFILES__DEFAULT__RESOLUTION=300`) yields a model equal to `ProfileConfig()` | Pattern 8 shapes table | The shapes test expects True and fails; adjust the expectation or drop the row |
| A2 | Same-origin SRI needs no `crossorigin` attribute | Pattern 9 | Assets refuse to load; caught at once by the offline browser canaries |
| A3 | Playwright does not route `data:` URIs, so Pico's inline SVGs and thumbnails don't show up as blocked | Pattern 11 | The egress fixture reports false positives; filter `data:` explicitly |
| A4 | Browsers send `Origin: null` on same-origin POSTs under `Referrer-Policy: no-referrer` | Pitfall 8 | Doc sentence slightly wrong; no code impact |
| A5 | Docker Engine (non-Swarm) does not restart a container on an unhealthy HEALTHCHECK | Pitfall 9 | Doc sentence wrong; verify before writing |
| A6 | Chromium sends an `Origin` equal to the page origin on same-origin htmx XHR POSTs over plain HTTP to a LAN IP | Pattern 10 | Every scan on the documented deployment would 403. Mitigation: optional LAN-IP browser test (Open Question 3) |
| A7 | `ErrorCategory.REJECTED` (rather than a column) is acceptable as the D-06 marker | Pattern 2 | User may prefer a migration column; the planner can surface it in plan review |

## Open Questions

1. **Startup recovery failure (disk full at boot).**
   - What we know: D-13 orders recovery before `worker.start()`; `fail_active_jobs` can raise.
   - What's unclear: abort startup (container restart loop, `/health` unreachable) or start degraded.
   - Recommendation: start with the worker pre-degraded and let the first successful probe run `fail_active_jobs` then clear degraded (Pitfall 6). It unifies both cases, keeps `/health` answering with a truthful 503, and needs no new decision beyond D-10..D-12. Confirm in plan review.
2. **Error text for a flip job aborted by shutdown.** `stop()`'s Abort makes the pipeline raise "aborted at the flip prompt", which the worker records, and that is untrue (the operator didn't abort). Recommendation: when `_stopping` is set, the worker's pipeline-failure path writes the restart reason instead. This is the worker thread's own final write, so it doesn't break D-07's "no shutdown-time state write" (which is about lifespan). Low stakes.
3. **Real-browser proof of branch 2 (plain HTTP).** A `TestClient` test covers the rule (required by CONTEXT specifics). A stronger, optional proof: a second uvicorn fixture bound to `0.0.0.0`, reached through the runner's non-loopback IP (discovered with a UDP `connect` + `getsockname`), where Chromium sends no `Sec-Fetch-Site`. Click Scan and expect success. Skip when no non-loopback address exists. It proves A6 on the documented deployment shape.
4. **CLI `auto-profiles` with no config file** keeps writing `./saneless.toml` (today's documented behaviour). Recommendation: keep it; Phase 27 CFG-03 (XDG) is the place to move it.
5. **Uncommitted `.python-version` change** (a `3.13` line appended, in git status). It is unrelated to this phase and `uv` still resolves 3.14.2. Do not include it in phase commits.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python | Everything | ✓ | 3.14.2 (GIL enabled) | — |
| uv | Build, run, wheel check | ✓ | 0.10.3 | — |
| Playwright + Chromium (local) | Browser tests | ✓ | playwright 1.58.0; `~/.cache/ms-playwright/chromium-1208/1217/1228` | — |
| Chromium (CI) | ROBU-11 job | via `playwright install --with-deps chromium` | — | — |
| libsane-dev (CI) | `uv sync` builds python-sane | apt in each job | — | — |
| openssl | Computing SRI in the vendoring task | ✓ | system | `python -c hashlib` |
| npm / node | Optional (registry metadata only) | ✓ | system | `curl` the tarball URLs directly |
| slopcheck | Legitimacy audit | ✓ | pyenv shim | — |
| gh | Resolving action SHAs (none new needed) | ✓ | — | — |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** none.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2, pytest-playwright 0.7.2 (Playwright 1.58.0), pytest-timeout 2.4.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (markers `browser`, `sane_hardware`; `filterwarnings=error`; `timeout=60`) |
| Quick run command | `uv run pytest tests/test_worker.py tests/test_web.py tests/test_web_state_rendering.py -x -q` |
| Full suite command | `uv run pytest -m "not browser and not sane_hardware" && uv run pytest -m browser` |
| Gate commands | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ROBU-01 | `update_state`/`finish_job`/`prune` raising → logged with `exc_info`, worker alive, next job DONE | unit (threaded) | `uv run pytest tests/test_worker.py -k "guard or survives" -x` | ❌ Wave 0 (new tests in existing file) |
| ROBU-01 / D-10..12 | N loop failures → degraded; `/health` 503 "job store failing"; probe success clears; pipeline errors never count | unit | `uv run pytest tests/test_worker.py -k degraded -x` ; `uv run pytest tests/test_web.py -k health -x` | ❌ Wave 0 |
| ROBU-02 | Queue full → 429 + `Retry-After`; ERROR row with `REJECTED`; HX branch has `HX-Retarget: #status-message`; JSON branch otherwise | integration (TestClient) | `uv run pytest tests/test_web_errors.py -k "429 or queue_full" -x` | ❌ Wave 0 (`tests/test_web_errors.py`) |
| ROBU-02 | 429 message actually visible in the browser while `#status-area` persists | e2e (browser) | `uv run pytest tests/test_browser.py -m browser -k queue_full` | ❌ Wave 0 |
| ROBU-02 / D-06 | After a 429 row, the finished job (not the rejection) is what the status area shows; also after a store reopen | unit (store) + integration | `uv run pytest tests/test_job.py -k latest_run -x` ; `uv run pytest tests/test_web.py -k rejected -x` | ❌ Wave 0 |
| ROBU-02 | `stop()` never blocks on a full queue | unit | `uv run pytest tests/test_worker.py -k "stop and full" -x` | ❌ Wave 0 |
| ROBU-03 | `stop()` wakes an idle worker in < 0.5 s, returns True; aborts an open flip; converted tests use `wait_for_state` | unit | `uv run pytest tests/test_worker.py -x` | ✅ file; conversions + new tests Wave 0 |
| ROBU-04 | Poll/scan/flip responses carry exactly one OOB `#scan-btn` mirroring `index.html` for every `JobState`; index has exactly one `id="scan-btn"`; `app.js` absent | integration | `uv run pytest tests/test_web_state_rendering.py -x` | ✅ file; new parametrized tests Wave 0 |
| ROBU-04 / ROBU-11 | Click Scan → terminal → button enabled, one `#scan-btn`, no `app.js` request, no egress | e2e (browser, CI) | `uv run pytest tests/test_browser.py -m browser -k scan_button` | ❌ Wave 0 |
| ROBU-05 | No `APIRoute` endpoint is a coroutine function | unit | `uv run pytest tests/test_web.py -k coroutine -x` | ❌ Wave 0 |
| ROBU-05 | `/health` answers in < 1 s while another request blocks in its I/O | integration (threaded TestClient) | `uv run pytest tests/test_web.py -k health_answers -x` | ❌ Wave 0 |
| ROBU-05 | 8 concurrent `get_or_fetch` → one fetch | unit | `uv run pytest tests/test_cache.py -k single_flight -x` | ✅ file; test Wave 0 |
| ROBU-05 / D-19 | Readers iterate profile snapshots while generation rebinds; no `RuntimeError: dictionary changed size` over 200 rounds | unit (threaded) | `uv run pytest tests/test_worker.py -k profile_lock -x` | ❌ Wave 0 |
| ROBU-06 | Lifespan: active rows (incl. PENDING) are ERROR with restart reason *before* the worker's first store call; startup prune runs | integration | `uv run pytest tests/test_app_lifespan.py -x` | ❌ Wave 0 (`tests/test_app_lifespan.py`) |
| ROBU-06 / D-09 | Join timeout → WARNING naming the job id; store and Paperless not closed | integration (gated scanner) | `uv run pytest tests/test_app_lifespan.py -k join_timeout -x` | ❌ Wave 0 |
| ROBU-07 | Startup generation writes to `settings.config_path`; env-only → memory + INFO, no files in CWD; unwritable → WARNING + memory; scanner raises → WARNING naming the class; once only | unit | `uv run pytest tests/test_worker.py -k startup_generation -x` | ❌ Wave 0 (replaces `TestLazyAutoGenerate`) |
| ROBU-07 | `load_settings` records the path (explicit, searched, none); `SANELESS_CONFIG_PATH` cannot set it | unit | `uv run pytest tests/test_config.py -k config_path -x` | ❌ Wave 0 |
| ROBU-07 | `is_bare_default` shapes table | unit | `uv run pytest tests/test_auto_profiles.py -k bare -x` | ✅ file; parametrized test Wave 0 |
| ROBU-08 | Unknown profile, 257-char title, `resource=bogus` → 422, no job row created, message slot partial | integration | `uv run pytest tests/test_web_errors.py -k 422 -x` | ❌ Wave 0 |
| D-01/D-04 | Catch-all 500 renders the partial (HX) or JSON (non-HX), logs `exc_info`; 404/405 go through the renderer | integration (`raise_server_exceptions=False`) | `uv run pytest tests/test_web_errors.py -k "500 or 404" -x` | ❌ Wave 0 |
| ROBU-09 | Template `integrity` == sha384(file bytes); no external URLs in templates; licences present; coupling contract (no `pico.colors.css`, no `data-theme`, fallback block present) | unit | `uv run pytest tests/test_vendor_assets.py -x` | ❌ Wave 0 (`tests/test_vendor_assets.py`) |
| ROBU-09 | UI styled and htmx active with all non-test-server requests aborted (every browser test, DARK-01/02 included) | e2e (browser, CI) | `uv run pytest -m browser` | ✅ file; `context` override Wave 0 |
| ROBU-09 | Wheel contains `saneless/web/static/vendor/*` | smoke | `uv build --wheel -o "$TMP" && python -c "zipfile check"` (in a test using `tmp_path`, or a plan verify step) | ❌ Wave 0 |
| ROBU-10 | Branch 1 (same-origin/none allow; same-site/cross-site 403), branch 2 (foreign Origin 403; Host match; X-Forwarded-Host match incl. comma list; `null` 403), branch 3 allow; GET cross-site allowed; every unsafe route covered generatively; 403 logs Origin/Host/XFH | integration | `uv run pytest tests/test_cross_origin.py -x` | ❌ Wave 0 (`tests/test_cross_origin.py`) |
| ROBU-10 | Default bind `0.0.0.0` documented in configuration / env-vars / CLI / web-api docs | docs check | `rg -n "0\.0\.0\.0" docs/reference` (plan verify) | n/a |
| ROBU-11 | CI browser job exists and runs `-m browser` after `playwright install --with-deps chromium` | config check | `rg -n "playwright install --with-deps chromium" .github/workflows/ci.yml` (plan verify); real proof = the job going green on the PR | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** the quick run command, plus the specific new test file.
- **Per wave merge:** full non-browser suite + `uv run pytest -m browser` + gate commands (`uv run prek run --stage pre-push --all-files`).
- **Phase gate:** full suite (both markers) green, and the CI `browser` job green on the pushed branch, before `/gsd-verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_web_errors.py`: renderer, handlers (422/429/403/404/500), htmx vs JSON branches, `Retry-After`
- [ ] `tests/test_cross_origin.py`: D-20 branches + generative unsafe-route test
- [ ] `tests/test_vendor_assets.py`: SRI bytes, no external URLs, licences, coupling contract
- [ ] `tests/test_app_lifespan.py`: recovery ordering, guarded close, startup prune
- [ ] New tests inside `tests/test_worker.py`, `tests/test_web.py`, `tests/test_web_state_rendering.py`, `tests/test_cache.py`, `tests/test_config.py`, `tests/test_auto_profiles.py`, `tests/test_job.py`, `tests/test_browser.py`
- [ ] `_BrowserTestScanner` gate Event (for the ROBU-02 browser test)
- [ ] No framework install needed

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Trusted-LAN decision (REQUIREMENTS out-of-scope table); DOCS-05 notice is another phase |
| V3 Session Management | no | No sessions (APPL-09 cookie is Phase 30) |
| V4 Access Control | yes (CSRF) | Fetch Metadata + Origin/Host check middleware on unsafe methods (D-20..D-23) |
| V5 Input Validation | yes | FastAPI `Form(max_length)`, `Literal` params, profile allow-list under lock; 422 before any write |
| V6 Cryptography | no (SRI is integrity pinning, browser-enforced) | `integrity="sha384-…"` literals; never hand-hash at runtime |
| V7 Error Handling & Logging | yes | Generic developer-authored messages; no exception text or request input in responses; `exc_info` server-side; `%r` for header values in logs |
| V12/V14 Files & Configuration | yes | Vendored, integrity-pinned assets; no third-party runtime fetch; vendor dir excluded from byte-rewriting hooks |

### Known Threat Patterns for FastAPI + htmx on a LAN

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Cross-site form POST starts a scan (N-22) | Tampering | `CrossOriginGuard`: `Sec-Fetch-Site`, else Origin vs Host/X-Forwarded-Host |
| Forged `X-Forwarded-Host` from a malicious page | Spoofing | Custom header → CORS preflight → OPTIONS gets no CORS approval → browser never sends the POST (D-21) |
| CDN compromise / offline failure (N-21) | Tampering / DoS | Vendored files + SRI |
| Queue exhaustion hangs the event loop (C-09) | DoS | `put_nowait` → 429; `def` routes; bounded join |
| Unbounded title stored (N-20) | DoS / Tampering | `max_length=256` |
| Error partial echoing input (XSS) | Tampering | Constant messages; Jinja autoescape (already proven by `test_fallback_warning_is_escaped_not_injected`) |
| Log injection via crafted Origin/Host | Repudiation | `%r` formatting in the 403 log line |
| Stack traces / exception text to clients | Information disclosure | Catch-all renders a fixed message; details only in the server log |

## Sources

### Primary (HIGH confidence)
- htmx 2.0.8 `dist/htmx.js` from `registry.npmjs.org/htmx.org/-/htmx.org-2.0.8.tgz` (SHA-512 verified): `responseHandling` default, `mergeMetaConfig`, `handleAjaxResponse` retarget/reswap, `oobSwap`, `isInlineSwap`, `disableElements`/`removeRequestIndicators` ordering, `getAttributeValueWithDisinheritance`, `attributesToSettle`, synchronous `swap`
- htmx `CHANGELOG.md` (github.com/bigskysoftware/htmx master): 2.0.9 `hx-disabled-elt` fix; 2.0.10 notes
- Context7 `/bigskysoftware/htmx/v2.0.4`: response-handling meta config and response headers docs
- Installed source: `starlette/middleware/errors.py` (re-raise), `starlette/applications.py` & `fastapi/applications.py` `build_middleware_stack`, `fastapi/routing.py` `run_in_threadpool`, `fastapi/exception_handlers.py`, `uvicorn/protocols/http/h11_impl.py`
- Context7 `/kludex/starlette`: exception handlers, `ServerErrorMiddleware` placement, pure ASGI middleware, `BaseHTTPMiddleware` limitations
- Go `src/net/http/csrf.go` (release-branch.go1.25): `CrossOriginProtection.Check`
- W3C Fetch Metadata Request Headers (w3c.github.io/webappsec-fetch-metadata): "potentially trustworthy URL" early return; `Sec-Fetch-Site` values
- docs.python.org/3.14/library/queue.html: `Queue.shutdown`, `queue.ShutDown` (added 3.13)
- Context7 `/websites/playwright_dev_python`: `BrowserContext.route`, `Route.abort`, `install --with-deps`
- npm registry metadata + slopcheck for htmx.org / @picocss/pico; SRI computed with `openssl dgst -sha384`
- Empirical runs on this machine: `Queue.shutdown` wake latency; FastAPI 422/429/404/405/500 handler behaviour; `def` vs `async def` `/health` latency; `PrivateAttr` vs field env settability; `PRAGMA user_version` WAL write; `uv build --wheel` static inclusion; `git check-ignore`

### Secondary (MEDIUM confidence)
- `.planning/reviews/2026-09-09-code-review.md` § C-09, C-10, M-01, M-03, M-04, N-20..N-24 (project's own executed findings)

### Tertiary (LOW confidence)
- Assumptions A1-A7 above

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH. No new dependencies; versions read from the lockfile environment; vendored bytes verified against the registry.
- Architecture: HIGH. Every framework interaction the design depends on was read in source or executed.
- Pitfalls: HIGH for 1-7 (source- or run-verified), MEDIUM/LOW for 8-9 (doc sentences flagged as assumptions).

**Research date:** 2026-09-14
**Valid until:** 2026-10-14 (stable pins; revisit if htmx is bumped past 2.0.8, which changes `hx-disabled-elt` semantics and the SRI value)
