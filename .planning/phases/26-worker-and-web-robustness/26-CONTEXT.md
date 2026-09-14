# Phase 26: Worker and Web Robustness - Context

**Gathered:** 2026-09-14
**Status:** Ready for planning

<domain>
## Phase Boundary

The server survives everything the pipeline, the job store, and housekeeping can throw at
it, and the browser always reflects reality. Eleven requirements (ROBU-01..ROBU-11) close
review findings C-09, C-10, M-01, M-03, M-04, M-33, N-20, N-21, N-22, N-24 and doc rows 23,
26 and 33.

The work lives in `src/saneless/worker.py` (guarded loop, stop flag, degraded health,
startup profile generation), `src/saneless/web/app.py` (lifespan ordering, crash recovery,
error rendering, cross-site middleware), `src/saneless/web/routes.py` (`def` handlers, 429 /
503 / 422, input validation), `src/saneless/web/cache.py` (single-flight),
`src/saneless/config.py` (the loaded config path travels with `Settings`),
`src/saneless/auto_profiles.py` (duplicated search list), `src/saneless/web/templates/`
(server-owned Scan button, message slot, `htmx-config`, vendored asset tags),
`src/saneless/web/static/` (vendored htmx and Pico; `app.js` deleted),
`.github/workflows/ci.yml` (a browser job), `tests/test_worker.py` / `tests/test_browser.py`
/ `tests/test_web.py`, and the docs that describe the behaviour changed here.

Concretely, the phase closes these gaps that exist in the code today:

1. **The worker can die silently.** `ScanWorker._run` (`worker.py:333-339`) has no guard;
   `_process_job` guards only the pipeline call. `update_state` before the `try`,
   `finish_job` inside the `except`, and `prune` in the `finally` (`:483-486`) can all raise
   and end the thread (C-09).
2. **Backpressure is a hang.** `submit()` does a blocking `put` on a `maxsize=10` queue
   (`:197`, `:222`) from an `async def` route, and `stop()` enqueues its `None` sentinel the
   same way (`:210`), ignoring the result of `join(timeout=5)` (C-09).
3. **Every route is `async def`** (`routes.py`, all handlers) while everything it calls
   blocks: sync `httpx`, sqlite, the worker (M-01).
4. **The Scan button never re-enables.** `static/app.js` reads `evt.detail.target`, which is
   the detached pre-swap element for `outerHTML` swaps (C-10). The button is rendered in
   `index.html` and re-implemented in JS: two sources of truth.
5. **No crash recovery.** `JobStore.fail_active_jobs()` exists (`job.py:793`, STOR-05) but has
   no production caller; `lifespan` (`app.py:71-85`) starts the worker, and at shutdown
   closes Paperless and the store under a possibly-live thread (M-03).
6. **Profiles are generated inside the first job** (`worker._maybe_auto_generate`,
   `:341-373`) and written to `resolve_config_path()` with no argument, which ignores
   `--config` and falls back to `./saneless.toml` in the daemon's CWD; the in-memory
   `settings.profiles` dict is mutated from the worker thread with no lock (M-04).
7. **No input validation** on `POST /api/scan` (any profile, any title length) or
   `POST /api/cache/invalidate` (`resource: str`) (N-20).
8. **htmx 2.0.8 and Pico 2.1.1 load from `cdn.jsdelivr.net`** (`base.html:8,10`) with no
   integrity hash; the UI is unusable on an offline LAN and the browser tests are excluded
   from CI (`ci.yml:46`) because they need CDN egress (N-21, M-33).
9. **No cross-site protection** on state-changing POSTs (N-22).

Not in this phase (owned elsewhere, do not absorb): queue position "N ahead of you"
(APPL-08, Phase 30), flip owner token (APPL-09, Phase 30), stale-on-error cache and fetch
cause logging (SWP-04, Phase 32), the one-sentence no-login / all-interfaces notice on the
getting-started and Docker pages (DOCS-05), logging the loaded config path at INFO (CFG-11,
Phase 27), atomic config rewrite and config-directory mount (CFG-08/09, Phase 27), SANE
scan cancellation (HARD-03, Phase 29).

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)
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

### Rejection visibility (ROBU-02, ROBU-08, and every error path)
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

### Shutdown and worker health (ROBU-01, ROBU-03, ROBU-06)
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

### Startup auto-profiles (ROBU-07, ROBU-05 profile lock)
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

### Cross-site guard (ROBU-10)
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

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/REQUIREMENTS.md` § "Worker and Web Robustness": ROBU-01..ROBU-11 (lines 83-95)
- `.planning/ROADMAP.md` § "Phase 26: Worker and Web Robustness": goal, five success
  criteria, and the two notes (htmx `responseHandling`; worker tests converted here, not in
  Phase 32; the 23.1 DARK-03 coupling)
- `.planning/REQUIREMENTS.md` APPL-08, APPL-09, DOCS-05, SWP-04, CFG-08, CFG-09, CFG-11: the
  neighbouring requirements that fence this phase's scope

### Review findings being remediated
- `.planning/reviews/2026-09-09-code-review.md` § C-09 (worker death, blocking put, stop
  sentinel; includes a guarded-loop sketch). **Note that D-05 departs from its "create the
  row only after enqueue" advice.**
- same file § C-10 (Scan button; OOB-swap fix and required browser test)
- same file § M-01 (`async def` routes that block; `def` fix and router comment)
- same file § M-03 (no crash recovery; shutdown closes store under a live worker)
- same file § M-04 (config path re-derived; `is_bare_default`; unlocked profile mutation;
  "move auto-generation to an explicit startup step")
- same file § M-33 (data-loss paths lacking tests), § N-20 (input validation), § N-21
  (CDN assets), § N-22 (cross-site POSTs, bind address), § N-24 (worker test sleeps)
- same file § 8 doc rows 23 (`web-api.md` "returns immediately"), 26 (`architecture.md`
  "fully responsive"), 33 (`job.py` docstring "crash recovery")

### UI contracts
- `.planning/phases/23.1-dark-mode-and-the-commit-gate/23.1-UI-SPEC.md` § "Coupling contract
  for Phase 26 (ROBU-09)" and § "Verification Contract (Playwright computed-colour tests)"
  (the DARK tests that move into CI)
- `.planning/UI-SPEC.md`: project UI conventions (status classes, dark-scheme override
  convention recorded in 23.1)

### Prior phase decisions this phase builds on
- `.planning/phases/25-manual-duplex/25-CONTEXT.md` D-12..D-17 (`SCANNING_REVERSE`, the
  flip coordinator, abort finality, the shared status lookup)
- `.planning/phases/22-job-store-hardening/22-CONTEXT.md`: `RLock` on every store method,
  `fail_active_jobs` / `list_pending` (STOR-05)
- `.planning/phases/20-ci-gate/20-CONTEXT.md`: the CI job layout; browser tests were
  deferred to this phase

### External references (cross-site design)
- Go `net/http.CrossOriginProtection` (Go 1.25): the algorithm D-20 follows
- https://www.alexedwards.net/blog/preventing-csrf-in-go: walkthrough of the Sec-Fetch-Site
  → Origin-vs-Host fallback
- https://simonwillison.net/2025/Oct/15/csrf-in-go: notes that `localhost` counts as a
  secure context and gets `Sec-Fetch-Site` (so a localhost-only test cannot exercise the
  plain-HTTP fallback branch by itself)

### Docs corrected in-phase (roadmap rule: correct what you changed)
- `docs/reference/web-api.md`: `:173` "returns immediately after queuing"; add 429 +
  `Retry-After`, 503 (down/degraded), 422, 403; `/health` degraded semantics; `:172` the
  no-auth / trusted-LAN note gains the cross-site guard and proxy Host guidance
- `docs/explanation/architecture.md:52`: "keeps the web layer fully responsive"; worker
  thread model (stop flag, degraded health, startup generation, crash recovery)
- `docs/reference/configuration.md:55`, `docs/reference/environment-variables.md:56`,
  `docs/reference/cli-commands.md:116`: document the `0.0.0.0` default bind explicitly
- `docs/how-to/deploy-docker-compose.md` / `docs/how-to/install-bare-metal.md`: reverse-proxy
  Host / X-Forwarded-Host guidance, where the planner finds the best home
- `src/saneless/job.py:1-7` module docstring (doc row 33)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `JobStore.fail_active_jobs(reason) -> int` (`job.py:793`): ready-made ROBU-06 recovery
  with an `ACTIVE_STATES`-derived predicate and a defaulted reason seam; needs only a caller.
- `JobStore.list_pending()` (STOR-05): exists for APPL-08; not needed here.
- `JobStore` `@_locked` / `RLock` (`job.py:462`, `:508`): every store method is already
  thread-safe, so `def` routes on the threadpool are safe against the store.
- `ACTIVE_STATES` / `TERMINAL_STATES` (`vocabulary.py`), `Job.is_active` / `Job.is_busy`
  (`job.py:413`): the button and poll rules already use these; the shared button include
  reuses them.
- `WorkerFlipCoordinator.signal_abort()` (`worker.py:115`): D-07's "answer the open flip wait
  with Abort" at shutdown; it drops the signal if the prompt is not armed (pass A still
  running), which is the correct abandon behaviour.
- `_current_or_recent_job` / `_status_context` (`routes.py:69-138`): the single status lookup
  D-06 must adjust.
- `tests/test_browser.py` session-scoped real uvicorn server fixture (`:122-174`) with a stub
  scanner: the base for the ROBU-11 test and the offline route block.
- `is_bare_default` (`auto_profiles.py:197`): already full-model equality.

### Established Patterns
- **Templates own no vocabulary:** labels come from `state_label` / `progress_label` /
  `flip_answer_label` filters and the `JobState` global registered in `create_app`
  (`app.py:92-111`). New partials (button include, message slot, error partial) follow the
  same rule.
- **Total enums with `match` + `assert_never`** (Phases 21, 24, 25): any new state-like
  enum (e.g. worker health: healthy / degraded / down) follows it.
- **`logger.warning(..., exc_info=True)` with the real exception**, never a guessed cause.
- **Docstrings explain the "why" and cite finding IDs** (`CR-01`, `D-16`, `M-02`): match the
  density in `worker.py` / `routes.py`.
- **No `# type: ignore` / `# noqa`**; both `ty` and `pyrefly check src tests` must pass.
  `app.py:103-110` shows the house way around a checker false positive (typed widening, not
  suppression).
- **TDD RED commits are allowed** (commit hooks type-check `src/` only; GATE-01).

### Integration Points
- `create_app` / `lifespan` (`app.py:43-113`): recovery ordering (D-13), exception handlers
  (D-01), cross-site middleware (D-23), guarded close (D-09).
- `cli.serve` (`cli.py:438-468`) → `create_app(settings, scanner)`: `settings` carries the
  loaded config path (D-16); `cli` root loads it at `cli.py:182`.
- `cli auto-profiles` (`cli.py:475-497`) uses `resolve_config_path(config_path_str)`: shares
  the single search list after D-16.
- `routes.py` handlers: every one becomes `def` (ROBU-05), with a router comment saying why.
- `base.html`: CDN `<link>`/`<script>` → vendored tags with `integrity`; add the
  `htmx-config` meta; remove the `app.js` script tag.
- `index.html:57-60` button → shared include; add the message slot next to the status
  include.
- `partials/status.html`: gains the OOB button; `#status-area` itself stays the poll target.
- `.github/workflows/ci.yml:46`: the fast gate keeps `not browser`; a browser job is added.

</code_context>

<specifics>
## Specific Ideas

- The 429 must be *seen* in a real browser, not just returned. A ROBU-02 browser check needs a
  setup where the page's Scan button is enabled but the queue is full (for example, the page
  loaded before the queue was filled through the API), because the button is disabled while
  a job is active.
- ROBU-11's assertions, verbatim from the roadmap: click Scan, wait for the terminal status,
  `#scan-btn` enabled again, exactly one `id="scan-btn"` in the DOM, `app.js` gone (not
  served, not referenced), with no CDN egress.
- The cross-site test must exercise the **Origin fallback branch** explicitly (a request with
  no `Sec-Fetch-Site` and a foreign `Origin` → 403). A browser pointed at `127.0.0.1` /
  `localhost` sends `Sec-Fetch-Site`, so a browser test alone would only cover branch 1.
- Log lines this phase adds are for the operator's "why didn't that work?" moment: the
  worker guard (job id + `exc_info`), the degraded transition and recovery, the abandoned job
  at shutdown, the unpersisted profiles (path + reason), the 403 (Origin/Host/X-Forwarded-Host).

</specifics>

<deferred>
## Deferred Ideas

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

</deferred>

---

*Phase: 26-worker-and-web-robustness*
*Context gathered: 2026-09-14*
