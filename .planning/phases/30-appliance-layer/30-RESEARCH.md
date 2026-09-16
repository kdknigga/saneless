# Phase 30: Appliance Layer - Research

**Researched:** 2026-09-16
**Domain:** FastAPI + Jinja/htmx server-rendered UI, Click CLI, threading inside a
FastAPI lifespan, SANE/httpx probe bounding, pydantic-settings schema growth
**Confidence:** HIGH for everything read out of this tree; MEDIUM where a CONTEXT
decision needs an amendment; LOW only where explicitly labelled

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

Copied verbatim from `.planning/phases/30-appliance-layer/30-CONTEXT.md`
`<decisions>`. Nothing below is re-litigated by this research; where a decision
turns out to need a mechanical amendment it is called out in
**Amendments Required** and the decision's intent is preserved.

**Carried forward (already decided, do not re-litigate)**
- **Phase 23:** `Job` already carries `pages_scanned`, `pages_removed`, `pages_uploaded`
  (`job.py:497-499`). APPL-03 is a **display** requirement; no new columns, no migration.
- **Phase 22/23:** `owner_token` is already a column with **no writer** — `job.py:710`
  says so in a comment. Phase 30 is its intended writer. No migration.
- **Phase 21:** `error_message(category)` (`vocabulary.py:476`) is the plain-language map
  and is deliberately unwired. Its docstring names "the error-message rework" as its
  arrival and warns that the specific messages are **more truthful today** than a generic
  category sentence, so swapping one for the other is a regression. APPL-04 **adds** the
  category sentence and the next step; it never removes the specific message.
- **Phase 26 D-01/D-04/D-10:** the web layer renders every error through one renderer, and
  exception classes are *named, never interpreted*.
- **Phase 26 D-05:** a refused submit still writes a job row so history shows the attempt;
  `RequestRejection` + `rejection_message` + `rejection_status_code` is the mechanism, and
  `ErrorCategory.REJECTED` rows are skipped by `latest_run_job`.
- **Phase 26 D-08:** `worker.stop()` joins for at most `STOP_JOIN_SECONDS`; the worker
  thread is a daemon. This is the precedent every new thread follows.
- **Phase 26 ROBU-04:** the Scan button is server-owned and re-rendered out-of-band from
  server state. Phase 26 **deleted `app.js`** — there is no client script file, only
  `app.css` and vendored htmx 2.0.8 / PicoCSS 2.1.1. Nothing in this phase reintroduces one.
- **Phase 26 D-16..D-18 / Phase 27 D-08:** a read-only or bind-mount-broken config already
  keeps generated profiles in memory and logs it. APPL-06 only adds the *visible* report.
- **Phase 27 D-02/D-03:** the tool-owned profile key set is fixed, `--force` overwrites
  owned keys in place, and an owned key a fresh generation does not write is deleted. A
  profile without `auto_generated` is never touched.
- **Phase 27 D-09:** the recommended mount is read-write `./config:/etc/saneless`. `:ro`
  was explicitly rejected.
- **Phase 28 D-08:** the CLI failure line shape is
  `<what saneless was doing, with identifiers>: <original message>`, and the exit-code
  table (0/1/2/3/4/5/130) is locked and doc-truth tested.
- **Phase 25:** `FlipCoordinator` already has a bounded timeout on both front ends, and a
  flip answer is final. `SCANNING_REVERSE` is already a visible state.
- **REQUIREMENTS out-of-scope, still binding:** the container `HEALTHCHECK` keeps calling
  `/health`, never `doctor` (doctor does network I/O and would flap). saneless does **not**
  refuse to boot on a placeholder token — it starts red and refuses scans. No new runtime
  dependency. htmx stays on 2.x.

**The shared check list (APPL-01, APPL-02)**
- **D-01: a check has three states — ok / warn / fail — and `doctor` exits non-zero on
  `fail` only.**
- **D-02: one registry of checks, consumed by both `doctor` and the strip.** The five
  checks named by APPL-01 — scanner reachable and named, Paperless URL reachable and token
  accepted, profiles configured, consume-dir fallback configured, data dir writable — are
  defined once. Neither surface may define a check the other does not have. The Paperless
  check reuses the existing `ConnectionStatus` vocabulary and the logic behind
  `GET /api/paperless/test` (`routes.py:231`).
- `doctor --json` is **undecided** and left to the planner.

**Status strip: caching, freshness, and the busy scanner (APPL-02)**
- **D-03: the strip cache TTL is 30 seconds.**
- **D-04: a background refresh thread keeps the cache warm; the page always renders from
  cache.** Per-check timeouts are a second line of defence, not the mechanism.
- **D-05: the thread is lazy — it refreshes only while someone is watching.** The
  "recently loaded" signal is the planner's; keep it to a timestamp, not new persisted state.
- **D-06: cold start renders `Checking…` per check plus an htmx poll that swaps in the
  first real results.**
- **D-07: the thread follows the Phase 26 worker precedent** — daemon thread, a stop
  `Event`, joined with a bounded timeout in the same lifespan shutdown that stops the worker.
- **D-08: while a scan is active the checks are skipped and the strip shows the last known
  results with a note** — e.g. `Paused during scan — last checked 14:02`. This applies to
  the background thread too.
- **D-09: Refresh bypasses the cache and re-probes, resetting the TTL. During a scan it
  re-runs only the checks that do not touch the scanner.**

**Plain-language errors (APPL-04)**
- **D-10: one function returning a frozen pair.** `error_advice(category)` returns a frozen
  dataclass with `.message` and `.next_step`, built with a single `match` and
  `assert_never`.
- **D-11: next steps are keyed off `ErrorCategory` only** — seven of them. No per-exception
  refinement.
- **D-12: the CLI keeps today's line and gains a second one** (e.g. a `Try:` line).
- **D-13: the web `Technical details` disclosure holds the specific message, the error
  category, and the job id.** The log file path is deliberately **not** included. Collapsed
  by default (`<details>`).
- `job.error` keeps storing the specific message.

**Placeholder token and scan refusal (APPL-07)**
- **D-14: a placeholder is blank/whitespace, or a member of a small fixed literal set.**
  Includes `changeme` (`docker-compose.yml:30`), whatever `saneless.toml.example` carries,
  and the obvious `your-token-here` family. No shape heuristic.
- **D-15: the web layer refuses with a new `RequestRejection` member plus a disabled Scan
  button.** Writes a `REJECTED` job row. `WORKER_DEGRADED` is **not** reused.
- **D-16: `saneless scan` refuses with exit 2**, checked before the scanner is opened.
  `devices`, `auto-profiles` and `jobs` are unaffected. A configured consume-directory
  fallback does **not** soften it.
- **D-17: `./config/config.toml` is the one place for the secret.** The compose template
  ships its `environment:` block **fully commented out**, with a note that uncommenting it
  overrides the file.
- APPL-11's consume-directory mount is added to the same compose example with its two-line
  explanation; the missing-fallback row is a `warn`, not a `fail`.

**Profile labels, descriptions, and ordering (APPL-05, APPL-06)**
- **D-18: `label` and `description` are persisted, tool-owned keys.** They join Phase 27
  D-02's owned key set and behave exactly like `source` / `mode` / `resolution`. The
  operator's escape hatch is removing `auto_generated`.
- **D-19: generated text comes from what the code already knows.** `_duplex()`
  (`auto_profiles.py:305`) and `_auto_source_mode()` already classify the source.
- **D-20: the form keeps the `<select>` and gains a live description beneath it**, wired
  with `aria-describedby` and swapped by htmx on change. Not a radio group.
- **D-21: "sheet-fed" means the device reports no flatbed source** — the `has_flatbed` fact
  `_auto_source_mode` already receives. Feeder profiles sort first when it is true.
- **D-22: a read-only config location is an amber `warn` row, not a red one.** Its own row.

**Queue position and the owner-only flip prompt (APPL-08, APPL-09)**
- **D-23: the owner token is a session cookie, one per browser.** `HttpOnly`,
  `SameSite=Lax`, no `Max-Age`. Minted on the first scan submit and reused. Each job row
  records the token in the existing `owner_token` column.
- **D-24: the token gates the flip buttons and nothing else.** Other viewers see
  `Waiting for the stack to be flipped`.
- **D-25: the status area follows the job the browser submitted.** The status area gains an
  explicit followed job id; a browser that submitted nothing still sees the
  current/most-recent job as today.
- **D-26: when the owner's browser is gone, the Phase 25 flip timeout resolves it — there
  is no escape hatch.**
- **D-27: Abort confirms with htmx's `hx-confirm` attribute.**

**Form help text and the simpler form (APPL-10)**
- **D-28: hiding Tags and Correspondent is a config key**, e.g. `[web] show_tags` /
  `show_correspondent`. Not a per-browser toggle.
- **D-29: profile `default_tags` and `default_correspondent` still apply when the controls
  are hidden.**
- **D-30: the checkbox list replaces the multi-select everywhere**, not responsively. Touch
  targets at least 44×44px.
- **D-31: a filter box handles large tag lists**, with `hx-get="/api/tags?q=…"` on a
  debounced keyup swapping the list server-side.
- Each form control gets one line of help text in plain words.

**Page counts and local timestamps (APPL-03, APPL-12)**
- **D-32: counts appear as a sentence in the status area and as detail in the history
  table.** A `NULL` count renders **nothing at all**; never `0`.
- **D-33: manual duplex shows the front count and a live back count during pass B** —
  `Front: 12 pages · Scanning backs…` during `SCANNING_REVERSE`. The mechanism is the
  planner's.
- **D-34: timestamps render as `2026-09-16 14:03 CDT`** — `%Z` abbreviation in the server's
  local zone.
- **D-35: the zone name appears on every timestamp**, not once as a column caption. The
  planner must check the CLI table still fits at a normal terminal width.
- Conversion happens in one shared place.

### Claude's Discretion

- **The whole shared-check-list area** (D-01, D-02) — the registry shape and what each
  check probes are the planner's, within D-02's "defined once, both surfaces read it" rule.
- **`doctor --json`** — undecided, flagged for the researcher.
- Per-check timeout values behind the background thread (D-04).
- How "someone is watching" is detected for the lazy thread (D-05) — keep it to a
  timestamp, not persisted state.
- Whether `error_message` survives as an accessor or is folded into `error_advice` (D-10).
- How pass A's count is made readable mid-job (D-33).
- Whether the history table gets an extra column or an expandable row for counts (D-32).

### Deferred Ideas (OUT OF SCOPE)

- **`doctor --json`** — undecided (see Discretion). If no consumer is found, it does not ship.
- **Per-browser "simpler form" toggle** — rejected in favour of the config key (D-28).
- **Persistent owner cookie for a wall-mounted tablet** — rejected (D-23).
- **"Most-used tags first" ranking** — rejected for the tag picker (D-31).
- **Per-exception next steps** — rejected (D-11).
- **Mobile-optimised layout beyond the tag picker and help text** — `UIX-01`, out of phase.
- **Fixing the `kris-knigga` image reference in `docker-compose.yml`** — Phase 31, DLVR-01.
  This phase edits the same file without touching that line.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description (abridged from REQUIREMENTS.md:129-140) | Research Support |
|----|-------------|------------------|
| APPL-01 | `saneless doctor` runs a shared check list and exits non-zero on any failing check | §3 (Click integration, `_GuardedGroup`, exit-code choice), §1 (check registry), Pitfall 3 (`require_sane` refusal), Amendment A-1 |
| APPL-02 | Index page status strip, same checks, refreshed on load + button, TTL cache, skipped while scanning | §1 (lazy refresh thread), §2 (bounding probes), Pitfall 1 (lifespan shutdown ordering), Pitfall 2 (SANE mutual exclusion) |
| APPL-03 | Status area and history table show page counts; manual duplex front/back during pass B | §6 (population map, NULL rows, D-33 mechanism), Amendment A-4 |
| APPL-04 | Plain-language error message + next step, raw detail in a collapsed disclosure | §Code Examples `error_advice`, D-10/D-12 mapping, Pitfall 7 |
| APPL-05 | Profile `label` / `description`, dropdown labels + help text, feeder-first on sheet-fed | §7 (`_OWNED_KEYS` change set), Amendment A-3, §5 (`hx-trigger="change"` for D-20) |
| APPL-06 | Read-only config location: profiles in memory, strip says so | §7 (worker cannot currently distinguish "no file" from "write failed"), Amendment A-2 |
| APPL-07 | Placeholder token detected at startup, red strip, scans refused, compose has one secret place | §3/§4 (`RequestRejection` member, `scan` refusal), §10 (compose + docs) |
| APPL-08 | Queued job shows "Waiting for '<title>' to finish (N ahead of you)" via `list_pending()` | §6 / §4 (followed-job id in `_status_context`) |
| APPL-09 | Owner-only flip prompt via HttpOnly SameSite=Lax cookie; non-owner waiting copy; Abort confirms | §4 (cookie mechanics, `CrossOriginGuard` interaction), §5 (`hx-confirm`), Pitfall 5 (`PLR0913` on `create_job`) |
| APPL-10 | Help text per control, thumb-friendly tag checkboxes, operator can hide Tags/Correspondent | §5 (filter + checkbox hazards), Amendment A-5, new `[web]` config section |
| APPL-11 | Compose consume-directory mount + explanation; strip reports missing fallback | §10 (docs), §1 (fallback check is a `warn`) |
| APPL-12 | All user-facing timestamps in web UI and CLI in server local zone with zone named | §8 (verified `%Z` rendering, DTZ clearance, width arithmetic, `TZ` in the container) |
</phase_requirements>

---

## Summary

Every capability this phase needs already exists in the tree as an unwired part: the
plain-language error map (`vocabulary.py:476`), the three-way Paperless connection
vocabulary (`vocabulary.py:165`, `paperless.py:949`), the page-count columns
(`job.py:497-499`), `JobStore.list_pending()` (`job.py:933`), the `owner_token` column
(`job.py:501`), the source classification `_duplex()` / `_auto_source_mode()`
(`auto_profiles.py:280,305`), and the amber status token in `app.css:43,50`. Phase 30 is
overwhelmingly a **wiring and rendering** phase, not a new-machinery phase. No new runtime
dependency is needed and none is recommended: `socket`, `datetime`, `threading` and
`zoneinfo` from stdlib cover the remaining gaps.

Three things genuinely need new machinery, and all three are threading/boundary problems
rather than feature problems. First, the lazy background refresh thread (D-04..D-08) must
be woven into a lifespan whose shutdown currently assumes exactly one thread
(`web/app.py:121-142`): both threads must be stopped and confirmed before `paperless.close()`
and `scanner.close()`, because the refresh thread touches both. Second, nothing in
`scanner/sane_backend.py` mutually excludes two concurrent SANE calls — the only guard is
`_refuse_if_wedged`, which fires on a *stuck* read, not a running one — so D-08's
"skip scanner checks during a scan" is a **correctness** requirement, not a politeness one.
Third, `ScanResult` is only produced when the pipeline returns, and
`status_callback` is typed `Callable[[PipelineEvent], None]` with no payload
(`pipeline.py:354`), so D-33's live pass-B count has no channel today and needs one
(cheapest: the worker records pass A's count and exposes it beside `current_job_id`).

Five CONTEXT decisions need mechanical amendments that preserve their intent. They are
listed together under **Amendments Required** and each is a small, named change, not a
design substitution.

**Primary recommendation:** build `saneless/checks.py` as a pure, dependency-injected
registry of five three-state checks with a `ProbeBudget` per check, wrap it in a
`CheckCache` that takes an injectable monotonic clock, and drive it from one
`CheckRefresher` daemon thread whose stop/join mirrors `ScanWorker.stop()`
(`worker.py:496`). Have `doctor` and the strip route both call the same
`run_checks(...)`/`cached_checks(...)` pair. Everything else in the phase is rendering
against data that already exists.

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Health check definitions (five checks, three states) | Core library (`src/saneless/checks.py`) | — | D-02 requires one registry both surfaces read; it must not live in `web/` or `cli.py` |
| Check result caching + TTL | API/backend (`web/`) | — | A CLI `doctor` run is one-shot and must always probe fresh; only the server caches |
| Background refresh scheduling | API/backend (FastAPI lifespan thread) | — | Same tier as `ScanWorker`; shares its stop/join contract |
| Status strip rendering | Frontend server (Jinja partial) | Browser (htmx swap only) | No client script exists (Phase 26); every interaction is an `hx-` attribute |
| Cold-start "Checking…" poll | Frontend server | Browser | Server decides whether the partial carries `hx-trigger`; the browser only obeys |
| Owner identity (cookie mint + compare) | API/backend (`routes.py`) | Browser (cookie jar) | HttpOnly means the browser is a dumb carrier; all logic is server-side |
| Flip-button visibility | Frontend server | — | Rendered or not rendered by the server; never hidden with CSS |
| Error message + next step | Core library (`vocabulary.py`) | Frontend server, CLI | One `match`/`assert_never`, two renderers (D-10) |
| Profile `label`/`description` generation | Core library (`auto_profiles.py`) | Config file (persisted) | D-18 makes them tool-owned persisted keys |
| Profile ordering (feeder-first) | Frontend server (render-time sort) | — | Ordering is a presentation rule; the config file keeps generation order |
| Tag filter | API/backend (`/api/tags?q=`) + `MetadataCache` | Browser (debounced trigger) | D-31; the browser owns only the 300 ms debounce |
| Timestamp localisation | Core library (one shared formatter) | Frontend server (Jinja filter), CLI | CONTEXT: "one shared place so the web filter and the CLI table cannot disagree" |
| Placeholder-token detection | Core library (`config.py` or `vocabulary.py`) | API/backend, CLI | Used by the strip, the scan route guard and `saneless scan` — must be one predicate |

---

## Standard Stack

**No new runtime dependency.** REQUIREMENTS' Out of Scope table binds this
(`REQUIREMENTS.md:218-232`), and research confirms every capability is reachable from the
pinned set plus stdlib.

### Core (already pinned, `pyproject.toml:23-35`)

| Library | Version (verified installed) | Purpose in this phase | Why standard |
|---------|------------------------------|-----------------------|--------------|
| `fastapi` | 0.135.1 | Routes, lifespan, `TemplateResponse`, cookie set | Already the web tier |
| `httpx` | 0.28.1 | Paperless probe; **per-request `timeout=` override** is the bounding knob | Already the Paperless transport |
| `jinja2` | 3.1.6 | Every new partial and the shared timestamp filter | Already the template engine |
| `click` | 8.3.1 | `doctor` as a sixth `@cli.command()` under `_GuardedGroup` | Already the CLI |
| `tomlkit` | 0.14.0 | Writing `label` / `description` into `[profiles.*]` | Already the config writer |
| `python-sane` | 2.9.1 | The only scanner enumeration path | Already mandatory |
| htmx (vendored) | 2.0.8 | `hx-confirm`, `keyup changed delay:`, `hx-trigger="change"`, poll | Vendored + SRI-pinned; htmx 4 is out of scope |
| PicoCSS (vendored) | 2.1.1 | Amber `warn` token already exists in `app.css` | Vendored + SRI-pinned |

### Supporting (stdlib only)

| Module | Purpose | When to use |
|--------|---------|-------------|
| `socket` | Bounded TCP pre-probe of the saned port before any SANE call (§2) | When `scanner.host` is configured |
| `threading` (`Event`, `Thread`, `Lock`) | The refresh thread, mirroring `ScanWorker` | D-04, D-07 |
| `time.monotonic` | The check cache's clock — injectable so tests need no `sleep` | D-03 |
| `datetime.astimezone()` + `strftime("%Z")` | D-34's exact output, verified below | APPL-12 |
| `secrets.token_urlsafe` | Minting the owner token | D-23 |
| `os.access` / write-probe | The data-dir writability check | APPL-01 |

### Alternatives Considered

| Instead of | Could use | Tradeoff |
|------------|-----------|----------|
| `.astimezone()` (no arg) | `zoneinfo.ZoneInfo(settings.<tz>)` | Adds a config key nobody asked for; `astimezone()` already yields `%Z` correctly (verified) and honours the container's `TZ` |
| `socket` pre-probe of saned | Call `sane.get_devices()` and rely on a timeout | There is no timeout: `sane_get_devices` is a blocking C call with no Python-level bound (§2). The pre-probe is the only bound available |
| A new `CheckCache` class | Reuse `web/cache.MetadataCache` | `MetadataCache` is typed to `list[dict[str, object]]` and re-raises on fetch failure; the strip must *keep last-known-good* (D-08). CONTEXT already calls it "a pattern, not a drop-in" |
| `secrets.token_urlsafe` | `uuid.uuid4()` | Both fine; `token_urlsafe(32)` is the idiomatic opaque-session-token choice and is not mistakable for a job id in a log |

**Installation:** none. `uv sync` is unchanged; no `uv add` is expected in this phase.
A plan that introduces one is out of scope.

---

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.**

No `uv add` / `npm install` / `pip install` appears in any recommendation above. Every
library named is already in `pyproject.toml` and already in `uv.lock`, and the two
front-end assets are vendored bytes pinned by SRI in `base.html` and by hash in
`tests/test_vendor_assets.py`. `slopcheck` was therefore not run; there is nothing for it
to check. If a plan later proposes a new dependency, it contradicts both CONTEXT and
`REQUIREMENTS.md:218-232` and should be rejected rather than audited.

---

## Architecture Patterns

### System Architecture Diagram

```
                       ┌──────────────────────────────┐
  browser (htmx only)  │  GET /                       │
  ───────────────────► │   • renders strip FROM CACHE │
                       │   • stamps "watching" ts     │
                       └──────────┬───────────────────┘
                                  │ (never probes inline)
                                  ▼
                       ┌──────────────────────────────┐
  POST /api/checks/    │  CheckCache                  │◄──────┐
  refresh  ──────────► │   monotonic ts + results     │       │
  (bypass TTL, D-09)   │   keeps last-known-good      │       │
                       └──────────┬───────────────────┘       │
                                  │ read                      │ write
                                  ▼                           │
                       ┌──────────────────────────────┐       │
                       │  run_checks(registry, ctx)   │───────┘
                       │  ┌────────────────────────┐  │
                       │  │ scanner  (network I/O) │──┼──► socket connect (2 s)
                       │  │                        │  │     then sane.get_devices()
                       │  │ paperless(network I/O) │──┼──► httpx GET /api/tags/?page_size=1
                       │  │                        │  │     timeout=Timeout(5, connect=2)
                       │  │ profiles (pure)        │  │
                       │  │ fallback (pure/fs)     │  │
                       │  │ data dir (fs)          │  │
                       │  └────────────────────────┘  │
                       └──────────┬───────────────────┘
                                  ▲
                  ┌───────────────┴────────────────┐
                  │  CheckRefresher (daemon thread)│
                  │   loop: stop_event.wait(1 s)   │
                  │     watching?  ──no──► idle    │
                  │     TTL expired? ──no─► idle   │
                  │     scanner busy? ──yes─► run  │
                  │        the 4 non-scanner checks│
                  │     else run all 5             │
                  └───────────────┬────────────────┘
                                  │ asks
                                  ▼
                       ┌──────────────────────────────┐
                       │  ScanWorker                  │
                       │   current_job_id             │
                       │   scanner_gate (NEW)         │───► SaneBackend (exclusive)
                       │   front_pages   (NEW, D-33)  │
                       │   profile_storage (NEW, D-22)│
                       └──────────────────────────────┘

  CLI: saneless doctor ──► run_checks(registry, ctx)  ──► table + exit code
       (no cache, no thread, always fresh)
```

**Component responsibilities**

| File | Responsibility |
|------|----------------|
| `src/saneless/checks.py` (new) | The five check definitions, `CheckState` StrEnum (`OK`/`WARN`/`FAIL`), `CheckResult` frozen dataclass, `run_checks()`, and `worst_state()` → exit-code helper. No FastAPI, no Click imports. |
| `src/saneless/web/checks_cache.py` (new, or fold into `web/cache.py`) | Monotonic-clock TTL with last-known-good retention and an injectable clock. |
| `src/saneless/web/refresher.py` (new) | `CheckRefresher`: daemon thread, `Event` stop, bounded join, "watching" timestamp. |
| `src/saneless/web/routes.py` | `index()` stamps watching + renders from cache; `POST /api/checks/refresh`; `GET /api/checks` (cold-start poll); owner-cookie mint in `start_scan`; owner compare in `continue_flip`/`abort_flip`; `GET /api/profiles/{name}/description`; `GET /api/tags?q=`. |
| `src/saneless/cli.py` | `doctor` command; second CLI advice line (D-12); `scan` placeholder refusal (D-16); local-time `jobs` table. |
| `src/saneless/vocabulary.py` | `error_advice()`, the new `RequestRejection` member, `CheckState` labels, the placeholder-token predicate's copy. |
| `src/saneless/web/templates/partials/checks.html` (new) | The strip; also the swap target for the cold-start poll and the Refresh button. |

### Pattern 1: The stop-Event + bounded-join thread (D-07)

This is the Phase 26 shape, read out of `worker.py:405,496-531` and `web/app.py:121-142`.

```python
# Source: src/saneless/worker.py:405, 496-531 (the precedent this copies)
class CheckRefresher:
    def __init__(self, ...) -> None:
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._watch_lock = threading.Lock()
        self._last_watched = 0.0          # monotonic; D-05's "someone is watching"

    def start(self) -> None:
        self._thread.start()

    def note_watcher(self) -> None:
        """Called from a request thread by index() and the strip routes."""
        with self._watch_lock:
            self._last_watched = self._clock()

    def stop(self) -> bool:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout=STOP_JOIN_SECONDS)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stopping.wait(_TICK_SECONDS):   # wakes at once on stop
            ...
```

`Event.wait(timeout)` is the idle sleep — never `time.sleep` — so `stop()` wakes the
thread immediately instead of after a full tick. This is the same property
`queue.shutdown(immediate=True)` gives the worker (`worker.py:518`).

### Pattern 2: What today's lifespan does, and the one change it needs

Today (`web/app.py:70-143`), startup is: `validate_settings_dirs` →
`job_store.fail_active_jobs(RESTART_REASON)` (failure ⇒ `worker.mark_recovery_pending()`)
→ `job_store.prune(...)` (failure only logged) → `worker.start()` → `yield`.
Shutdown is: `if not worker.stop(): log and return` (deliberately leaving everything
open), else `paperless.close()` → `job_store.close()` → `scanner.close()`.

The refresh thread uses **both** the Paperless client and (possibly) the scanner, so:

```python
# startup, after worker.start():
refresher.start()

# shutdown:
worker_stopped = worker.stop()
refresher_stopped = refresher.stop()        # both stop events set before either join
if not (worker_stopped and refresher_stopped):
    logger.warning(...)                      # leave paperless/store/scanner open
    return
paperless.close(); job_store.close(); scanner.close()
```

Set both stop `Event`s before joining either, so the two joins overlap rather than
serialise (worst case stays `STOP_JOIN_SECONDS`, not 2×). `tests/test_app_lifespan.py:335`
asserts `calls == ["worker.stop", "paperless.close", "job_store.close"]` — that assertion
must be extended, and its extension is a legitimate "could not have passed before" test.

### Pattern 3: Three-state check registry (D-01, D-02)

```python
class CheckState(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"

@dataclass(frozen=True, slots=True)
class CheckResult:
    key: CheckKey            # StrEnum: SCANNER, PAPERLESS, PROFILES, FALLBACK, DATA_DIR
    state: CheckState
    message: str             # developer constant or a device/profile name — never raw exception text
    skipped: bool = False    # D-08: "Paused during scan"
```

`worst_state(results)` collapses to an exit code with a `match`/`assert_never` over
`CheckState`, keeping this module's established habit (`vocabulary.py:440,476,525,559`).
A `CheckKey` StrEnum with a `match`-based label function makes "neither surface may define
a check the other does not have" a type-checked fact rather than a convention.

### Pattern 4: Cold-start poll that stops itself (D-06)

Do **not** reach for HTTP 286. The tree already has the idiom: `status.html` emits its
`hx-get`/`hx-trigger` attributes **only** while `job.is_active`, so when the swapped-in
partial no longer carries them the poll ends. Mirror that:

```jinja
<div id="checks-strip"
     {% if checks is none %}
     hx-get="/api/checks" hx-trigger="load, every 2s" hx-swap="outerHTML"
     {% endif %}>
```

Verified in-tree: `partials/status.html` lines 1-6. Verified against htmx docs: 286 also
works, but it would need a route that deliberately returns a non-standard status, and the
`htmx-config` `responseHandling` array in `base.html` is already a tuned, fragile thing
(its comment warns that a partial override "would replace the array and stop every 2xx
swap"). Use the attribute idiom.

### Anti-Patterns to Avoid

- **Probing inside a request handler.** D-04's whole point. `index()` must read the cache
  and never call `run_checks`.
- **Calling `scanner.get_devices()` without checking the worker.** There is no lock
  underneath (Pitfall 2).
- **Adding a `CheckState` → colour mapping in the template.** Templates own no vocabulary
  (`web/app.py:159-161` comment). Add a `check_state_label` / class filter.
- **Rendering `0` for a NULL page count.** D-32 is explicit; `{% if job.pages_scanned is not none %}` not `{% if job.pages_scanned %}`.
- **Reusing `WORKER_DEGRADED` for the placeholder-token refusal.** D-15 forbids it.
- **Interpolating the token, the Paperless URL, or exception text into a strip message.**
  V7 / `paperless.py:461` (`_without_userinfo`) — the URL may carry Basic-auth credentials.
- **A second `match` over `ErrorCategory`.** D-10: `error_message` folds into
  `error_advice` or becomes a one-line accessor.

---

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Bounding a Paperless probe | A watchdog thread that abandons the call | `client.get(..., timeout=httpx.Timeout(5.0, connect=2.0))` | httpx has per-request granular timeouts; `TransportError` (already caught at `paperless.py:977`) is the base of `ConnectTimeout`/`ReadTimeout` |
| Local-zone formatting | `zoneinfo` + a new config key + DST arithmetic | `aware_dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")` | Verified to emit `2026-09-16 14:03 CDT`; no DTZ diagnostic |
| Confirm-before-Abort | A modal partial and a second route | `hx-confirm="…"` | One attribute, present in the vendored 2.0.8 bundle, accessible native dialog |
| Debounced filtering | Any JS | `hx-trigger="keyup changed delay:300ms"` | Verified present in htmx 2.0.8 (`delay`, `changed` tokens in the bundle) and documented |
| Single-flight metadata fetch | A new lock scheme for `/api/tags?q=` | `MetadataCache.get_or_fetch` (`web/cache.py:69`) | Already single-flight with a generation guard; filtering happens *after* the cache read |
| Queue position | A counter in the worker | `JobStore.list_pending()` (`job.py:933`) | Written and tested in Phase 22, explicitly waiting for APPL-08 |
| Plain-language Paperless states | A new mapping | `ConnectionStatus` + `connection_status_message` (`vocabulary.py:165,525`) | Five outcomes, already tested and doc-pinned |
| A "warn" colour | A new palette entry | `--saneless-status-fallback` (`app.css:43,50`) | `#a16207` / `#ca8a04`, already WCAG-AA-verified in both schemes by `tests/test_browser.py` (`_AMBER`) |

**Key insight:** this phase's risk is not in what it builds; it is in what it *touches*.
Four of the five surfaces are re-renders of data already in the row. The two places where a
hand-rolled solution actually costs something are the SANE probe bound (there is no
library answer — see §2) and the D-33 live count channel (there is no existing channel —
see §6).

---

## Findings by Research Question

### §1 — The lazy background refresh thread (D-04, D-05, D-07)

**What the lifespan does today** (`web/app.py:70-143`, docstring at 72-96): it is a single
`@contextlib.asynccontextmanager` closed over `paperless`, `job_store`, `cache`, `worker`
and `settings`, all built in `create_app` before it (lines 59-68). Startup order is fixed
and documented (D-13): `validate_settings_dirs(settings)` → `fail_active_jobs` (an
exception ⇒ `worker.mark_recovery_pending()`, not a refusal to start) → `prune` (failure
logged only) → `worker.start()` → `logger.info("App started")` → `yield`. Shutdown calls
`worker.stop()` and, **only if it returns True**, closes paperless, the job store and then
the scanner, in that order, with a long comment at 138-142 explaining that `sane_exit()`
closes every open handle while holding the GIL and must never run while a read is
outstanding.

`app.state` is the injection channel (`web/app.py:150-155`): `worker`, `job_store`,
`settings`, `paperless`, `cache`, `templates`. The refresher and the check cache belong
there too (`app.state.checks`, `app.state.refresher`).

**Stop-Event + bounded-join precedent** (`worker.py:357-362, 405-408, 496-531`): a
`threading.Event` named `_stopping` set once by `stop()`; a `daemon=True` thread built in
`__init__` and started separately; `stop()` sets the event, wakes the blocking wait, joins
with `timeout=STOP_JOIN_SECONDS` (5.0 s, `worker.py:62`, "read at call time, so tests can
shorten it"), and **returns whether the thread actually stopped** so the caller can decide
what to close. Copy all four properties, including the read-at-call-time constant.

**Signalling "someone is watching" without persisted state (D-05):** a monotonic
timestamp on the refresher, stamped by `index()` and by the strip routes
(`refresher.note_watcher()`), read by the thread under a small `threading.Lock`. Two
constants, both module-level and read at call time: `_WATCH_WINDOW_SECONDS` (how long
after the last page load the thread stays awake — 90 s gives three TTL cycles after the
operator walks away) and `_TICK_SECONDS` (the `Event.wait` granularity — 1.0 s keeps
Refresh responsive while costing nothing). A float attribute under a lock is a timestamp,
not state: it is not persisted, not in `app.state` as a mutable dict, and is reset by a
restart. This satisfies D-05 literally.

**Preventing a scanner probe mid-scan (D-08):** the naive gate, `worker.current_job_id is
not None`, has a real race — the refresher can read `None`, enter `get_devices()`, and the
worker can start a job a microsecond later. Two workable answers, in order of preference:

1. **A scanner gate on the worker** (recommended). Add `ScanWorker.scanner_gate`, a
   `threading.Lock` the worker acquires around the whole of `_scan_job`'s pipeline call and
   around `_read_generated_profiles`. The refresher does
   `if not gate.acquire(blocking=False): skip the scanner check`. This is real mutual
   exclusion, matches the existing `_profiles_lock` habit (`worker.py:373`), and makes
   D-08 true rather than probable.
2. **Advisory check only** (`worker.current_job_id`), accepting the race. Cheaper, and the
   window is small, but §2's Pitfall 2 explains why the consequence of losing the race is
   not "a slow probe" but "two concurrent SANE calls on one control wire".

Recommend (1) and say so in the plan. If (1) is judged too invasive for this phase, (2)
plus the `socket` pre-probe (§2) is defensible, because the pre-probe never enters SANE.

### §2 — Making the strip actually fast (success criterion 2)

**Paperless timeout knobs.** `PaperlessClient.__init__` sets one flat
`"timeout": 30.0` on the `httpx.Client` (`paperless.py:470`), which httpx expands to
30 s for connect, read, write *and* pool. `test_connection()` (`paperless.py:949-992`)
issues `self._client.get("/api/tags/", params={"page_size": 1})` and catches
`httpx.TransportError`, the base class of `ConnectError`, `ConnectTimeout` and
`ReadTimeout` — so a bounded probe needs no new exception handling at all. httpx supports
a per-request override (verified via Context7, `docs/advanced/timeouts.md`):

```python
response = self._client.get(
    "/api/tags/", params={"page_size": 1},
    timeout=httpx.Timeout(5.0, connect=2.0),
)
```

Recommendation: give `test_connection` an optional `timeout: httpx.Timeout | None = None`
so the strip and `doctor` pass a short budget while the existing
`GET /api/paperless/test` route keeps today's behaviour, or pass the same short budget
everywhere — the endpoint is a probe either way. **Cost of a bounded version:** ≤ 2 s to
discover an unreachable host instead of 30 s. Zero behavioural change on the happy path.

**The SANE reachability probe.** There is **no existing reachability call.** The nearest
thing is `SaneBackend.get_devices()` (`sane_backend.py:2036-2078`), which calls
`_refuse_if_wedged("the scanners", "list")` and then `sane.get_devices()`. That is a
blocking C call into libsane with **no timeout parameter at any layer** — not in
`python-sane`, not in `sane_get_devices(3)`, and not settable from Python. With the `net`
backend it opens a TCP connection to each entry of `SANE_NET_HOSTS`
(`sane_backend.py:874-880` sets that env var from `scanner.host`), so an unplugged host
means a connect that hangs until the OS gives up — on Linux, `tcp_syn_retries` defaults to
6, roughly 127 s. **This is exactly the hazard D-04 names, and there is no library knob
for it.**

The bounded pre-probe, which is what makes success criterion 2 true rather than merely
hidden:

```python
# saned's registered port is 6566 (IANA "sane-port"); sane-net(5) allows host:port.
def _saned_reachable(entry: str, timeout: float) -> bool:
    host, _, port = entry.partition(":")
    with socket.create_connection((host, int(port or 6566)), timeout=timeout):
        return True
```

`scanner.host` is colon-separated (`sane_backend.py:849`, `docker-compose.yml:33`), so the
parse must split on `:` for hosts *and* tolerate a `host:port` form — a genuine ambiguity
in sane-net's own syntax. Safest parse: treat a trailing all-digits segment as a port only
when the entry has exactly two segments. Flag this to the planner as a small, testable
helper with its own unit tests.

**Cost:** when the host is down, the scanner check answers `FAIL` in ≤ 2 s having made no
SANE call at all. When it is up, one extra TCP handshake (sub-millisecond on a LAN) before
`get_devices()`. When `scanner.host` is empty (USB on the host), there is no TCP to probe
and `get_devices()` runs directly — local USB enumeration is fast and bounded by the
kernel, not by a remote peer.

### §3 — `saneless doctor` as a Click command (APPL-01, D-01, D-02)

**How the machinery works.** `_GuardedGroup(click.Group)` (`cli.py:355-424`) overrides
`invoke()` with an ordered `except` chain: click control flow re-raised first
(`_CLICK_CONTROL_FLOW`, `cli.py:214`), then `KeyboardInterrupt`/`ScanCancelledError` → 130,
`StorageError` → 2, any other `SanelessError` → `exit_code_for(classify_error(exc))`,
anything else → `_report_unexpected` + 5. `ctx.exit(code)` is how every code is produced;
`ExitCode` is an `IntEnum` (`vocabulary.py:96-118`) and is the single definition.

`_load_cli_settings(ctx)` (`cli.py:451-500`) is called by each command on first need, never
by the group callback, so `--help` works with a broken config (CFG-10). It loads settings,
validates directories, configures logging, records `logging_configured` and `log_file` on
`ctx.obj`, emits the legacy-duplex warning and `log_config_sources`, and caches the
`Settings` on `ctx.obj["settings"]`.

**What a sixth command must do:** `@cli.command()` + `@click.pass_context`, a one-line
docstring (click uses it as help; ruff `D` requires it anyway), `settings =
_load_cli_settings(ctx)`, run the registry, print, `ctx.exit(code)`. It must **not** be
decorated onto a different group and must not catch its own exceptions — the guard is the
error path.

**Mapping three states onto an exit code.** Only `FAIL` is non-zero (D-01). Do **not** add
a new `ExitCode` member: the enum is doc-truth-pinned by
`tests/test_deployment_config.py:393` and `:403`, which assert the documented tables equal
exactly `frozenset(int(c) for c in ExitCode)`, and Phase 28 D-07 locked it. Use
`ExitCode.CONFIG` (2). That is already the established meaning of "can't start, fix your
setup" — `serve` uses it for a SANE init failure and an unbindable port
(`cli.py:775-780`, `cli.py:791-795`), and `cli-commands.md:23` describes 2 as
"Configuration, profile or setup error". Every red check is a setup problem. Do not split
a red Paperless check to 3: `doctor` reports a list, and one process has one exit code.

**Who consumes saneless exit codes today, and what a new command affects:**

| Consumer | Location | Effect of adding `doctor` |
|---|---|---|
| `test_cli_reference_command_exit_codes_are_real` | `tests/test_deployment_config.py:413-447` | **Will fail** until `docs/reference/cli-commands.md` grows a `## \`saneless doctor\`` section with an `**Exit codes:**` table *and* the test grows an explicit expected set. The test iterates every `## \`saneless <cmd>\`` heading and asserts each has the marker. This is the phase's best doc-truth hook. |
| `test_scripting_exit_code_table_matches_exit_code_enum` | `:393` | Unaffected if no new `ExitCode` member is added. |
| `test_cli_reference_global_exit_code_table_matches_exit_code_enum` | `:403` | Same. |
| `docs/reference/cli-commands.md:3` | literal "saneless provides **five** commands" | Must become six. No test pins the word (verified by grep), so it is a prose fix that a plan can easily miss. |
| Container `HEALTHCHECK` | `Dockerfile` / compose | Must stay on `/health`; REQUIREMENTS' Out of Scope table binds this. |

**`doctor --json`: no consumer found.** Nothing in `docs/`, `tests/`, `.github/`, the
Dockerfile or the compose file would read it. `devices --json` and `jobs --json` exist and
`docs/how-to/cli-scripting.md` documents them, so the precedent argues *for* consistency,
but consistency is not a consumer. **Recommendation: do not ship it.** Three reasons: the
Out of Scope table already refuses a `HEALTHCHECK` that calls `doctor`, which removes the
one scripted consumer anyone imagined; a JSON shape is a wire contract that would need
doc-truth tests of its own (`web-api.md` and `cli-scripting.md` both pin wire strings);
and CONTEXT explicitly says "If the researcher finds no consumer, it does not ship". The
human-readable table plus the exit code is the contract.

### §4 — Owner cookie mechanics (APPL-09, D-23, D-24)

**Setting a cookie on a `TemplateResponse`.** `state.templates.TemplateResponse(...)`
returns Starlette's `_TemplateResponse`, an `HTMLResponse` subclass, so `.set_cookie()` is
available directly:

```python
response = state.templates.TemplateResponse(
    request, "partials/status_response.html",
    {"job": job, "flip_answer": None, "clear_message": True},
)
if minted:
    response.set_cookie(
        "saneless_owner", token,
        httponly=True, samesite="lax", path="/",
        # no max_age / expires  => a session cookie that dies with the browser (D-23)
        # no secure=True        => the LAN deployment is plain HTTP
    )
return response
```

`start_scan` currently returns that expression directly at `routes.py:440-444`; it must
become a named local so the cookie can be attached. The mint rule (D-23) is
"minted on the first scan submit and reused for every later job from that browser":
read `request.cookies.get("saneless_owner")`; if absent or empty, mint
`secrets.token_urlsafe(32)` and set it on the response; either way, record it on the job
row.

**Interaction with `CrossOriginGuard`.** They are complementary and do not conflict.
The guard (`web/cross_origin.py:82-104`) allows GET/HEAD/OPTIONS unconditionally and, for
other methods, requires `Sec-Fetch-Site ∈ {same-origin, none}` or an `Origin` whose
host matches `Host`/`X-Forwarded-Host`; with neither header it allows the request (curl).
`SameSite=Lax` means the browser withholds the cookie on any cross-site POST, so even a
request the guard let through (a header-less non-browser client) carries no owner token
and therefore gets the non-owner rendering. There is no path where the cookie *weakens*
the guard. One thing to note explicitly in the plan: because the guard allows header-less
POSTs, a scripted `curl -b saneless_owner=…` can answer a flip. That is fine —
REQUIREMENTS states the owner token is "a footgun guard for the flip prompt, not an auth
mechanism".

**Do htmx swaps receive `Set-Cookie` normally?** Yes. htmx 2 issues ordinary
`XMLHttpRequest`s to the same origin, and the browser's cookie jar processes
`Set-Cookie` on an XHR response exactly as on a navigation; `HttpOnly` only hides the
cookie from `document.cookie`, which nothing here reads (there is no script file at all).
No htmx configuration is needed. Confidence: HIGH — this is browser behaviour, not an htmx
feature, and Playwright can assert it directly via `context.cookies()`.

**Where the comparison goes.** `continue_flip` (`routes.py:565`) and `abort_flip`
(`routes.py:598`) already scope by posted `job_id` per CR-01. Add, before signalling:
read the cookie, load the job, compare to `job.owner_token`. On mismatch, **do not error** —
D-16's established behaviour for a dropped flip answer is to return the current status
rather than an error, and D-24 wants the non-owner to see waiting copy. Render the status
partial unchanged. `partials/flip.html` must be given an `is_owner` context flag, and the
`{% include "partials/flip.html" %}` in `status.html:13` becomes a branch between the
prompt and `<p>Waiting for the stack to be flipped</p>`.

**`_status_context` change for D-25.** `_current_or_recent_job` (`routes.py:91-131`)
infers the job; D-25 adds an explicit followed id. The cleanest carrier that adds no
persisted state and no second cookie: the `POST /api/scan` response renders the status
partial with the job id baked into the poll URL
(`hx-get="/api/jobs/{{ job.id }}/status"`), so subsequent polls name the job the browser
submitted, and `GET /api/jobs/current/status` stays as the fallback for a browser that
submitted nothing. That is a new route, not a new cookie, and it is testable without a
browser.

### §5 — htmx-only interactions with no JS file

All four decided interactions work on htmx 2.0.8. Verified two ways: the tokens are
present in the vendored bundle `src/saneless/web/static/vendor/htmx-2.0.8.min.js`, and the
semantics are confirmed in the htmx docs via Context7.

| Decision | Attribute | In 2.0.8 bundle | Docs confirmation |
|---|---|---|---|
| D-27 Abort confirm | `hx-confirm="…"` | `hx-confirm` and `htmx:confirm` both present | Standard attribute since 1.x |
| D-31 debounced filter | `hx-trigger="keyup changed delay:300ms"` | `changed`, `delay`, `throttle` tokens present | `www/content/attributes/hx-trigger.md`: `hx-trigger="keyup changed delay:1s"` |
| D-20 live description | `hx-trigger="change"` on the `<select>` | native event name; no special support needed | — |
| D-06 cold-start poll | `hx-trigger="load, every 2s"`, stopped by swapping in a partial without the trigger | `every` polling documented; 286 also supported | `docs.md`: "The server can cancel polling by responding with HTTP status code 286" |

**No decided interaction needs a feature absent from 2.0.8.** Nothing here argues for
htmx 4.

**The Phase 26 `hx-disinherit` / `hx-disabled-elt` landmine.** `index.html:7-17` carries a
long comment: the form sets `hx-disabled-elt="#scan-btn"` and **must** keep
`hx-disinherit="hx-disabled-elt"`, because on htmx 2.0.8 an inherited `hx-disabled-elt`
makes the tags/correspondent refresh buttons strip `disabled` from a server-disabled Scan
button when their own requests finish (C-10 again; fixed only in 2.0.9). Consequences for
every control this phase adds inside the scan form (the tag filter input, the tag
checkboxes, the profile-description trigger):

- They inherit nothing harmful as long as they do not set `hx-disabled-elt` themselves.
  The existing `hx-disinherit` already covers them; do not remove it, and do not "tidy" it.
- `hx-disinherit` names exactly one attribute. If any new attribute is added **to the
  form** that children must not inherit (`hx-confirm` above all), the disinherit list must
  be extended in the same change. Put `hx-confirm` on the Abort **button** in
  `flip.html:43-45`, which is outside the form — no interaction at all.
- `hx-swap-oob` is the established mechanism for the server-owned Scan button
  (`status_response.html`, `scan_button.html`). The placeholder-token disabled state
  (D-15) belongs in `scan_button.html`, which is the **only** copy of that markup and
  whose comment warns that `tests/test_web_state_rendering.py` pins `type="submit"
  id="scan-btn"` as the first two attributes in that order.

**Two hazards CONTEXT does not address** (both are §Amendments, A-5 and A-6):

1. **The filter swap destroys checked state.** D-31 swaps the tag list server-side. Any
   tag the operator ticked before typing in the filter is replaced by a freshly rendered
   list and, if it falls outside the filter, disappears from the DOM entirely — so it is
   never submitted. The server must know what is currently checked. Minimum fix:
   `hx-include="#tags-list"` on the filter input so the current `tags` values ride along,
   and the route re-renders every selected tag as checked (and, ideally, pins selected
   tags to the top of the filtered list so they remain visible). One route parameter, one
   template condition, fully testable without a browser.
2. **A named filter input inside the scan form is submitted with the scan.** An
   `<input name="q">` inside `<form hx-post="/api/scan">` sends `q=…` with every submit.
   FastAPI ignores unknown form fields, so nothing breaks today — but that is an
   accidental property, not a decision. Either name it something the route explicitly
   ignores and add a regression test asserting a submit with a stray `q` still succeeds,
   or move the filter outside the `<form>` and target the list with `hx-target`. Recommend
   the latter: it is a markup change, not a contract.

### §6 — Page counts and the history table (APPL-03, D-32)

**Who populates the counts.** Exactly one writer:
`ScanWorker._scan_job`'s success path builds `JobResult(pages_scanned=result.pages_scanned,
pages_removed=result.pages_removed, pages_uploaded=result.pages_uploaded)` and hands it to
`_finish_or_owe` (`worker.py:1398-1410`). `ScanResult` is produced at
`pipeline.py:2127-2132` (`pages_scanned=len(records)`,
`pages_removed=len(records) - len(filtered)`, `pages_uploaded=len(filtered)`) and, for the
duplex-mismatch recovery, at `pipeline.py:1521-1523`
(`pages_removed=0` hardcoded, with a comment explaining that nothing is filtered on that
path, so 0 is a true measurement there and not a placeholder).

**Which states have counts, and which are NULL:**

| Terminal state | Counts | Why |
|---|---|---|
| `DONE` | populated | success path returns a `ScanResult` |
| `FALLBACK` | populated | same path; `job_state_for(ScanOutcome.FALLBACK)` |
| `ERROR` | **NULL** | `worker.py:1348-1352` — "No result argument: outcome, warning and all three page counts stay NULL" |
| `CANCELLED` | **NULL** | same except-branch |
| `ERROR`/`REJECTED` (refused submit) | **NULL** | `create_rejected_job` writes them NULL explicitly (`job.py:782-786`) |
| pre-Phase-23 rows | **NULL** | the columns were added by the migration ladder at `job.py:290-292` with no backfill |

So D-32's "a NULL count renders nothing at all" is load-bearing for four of the six cases,
not an edge case. In Jinja: `{% if job.pages_scanned is not none %}` — `{% if
job.pages_scanned %}` would hide a legitimate zero, which is a different lie.

**"Every terminal job shows pages …" (success criterion 3) is literally unachievable for
ERROR and CANCELLED rows, and D-32 already accepts that** — the criterion means "every
terminal job that recorded counts". Say so in the plan so the verifier does not chase a
phantom.

**D-33's live pass-B count.** The count is **not** available at `SCANNING_REVERSE` time and
must be threaded through. Evidence:

- `pipeline.py:1651` logs `"Pass A: scanned %d front page(s)", len(front_pages)` — the
  value exists in the pipeline at that moment.
- `pipeline.py:1693` emits `notify(PipelineEvent.SCANNING_REVERSE)` immediately after, and
  `status_callback` is typed `Callable[[PipelineEvent], None]` (`pipeline.py:354`) — a bare
  enum with **no payload**. Nothing else crosses that boundary.
- The worker's `_status_cb` (`worker.py:1296-1327`) only persists the state; its own
  comment says "The row is the only thing observers read".
- `ScanResult` (`pipeline.py:363-370`) is returned once, at the end.
- There is no `Job` column for a live count, and CONTEXT forbids a migration.

Three viable mechanisms, in order of preference:

1. **A second callback, mirroring `thumbnail_callback`.** `PipelineRequest` already carries
   `thumbnail_callback: Callable[[str], None]` (`pipeline.py:355`) precisely because a
   mid-run fact needed a channel. Add
   `pass_count_callback: Callable[[str, int], None] | None = None` (pass label, count),
   fired at `pipeline.py:1651` and again after pass B. The worker stores it on a
   `_current_front_pages: int | None` cleared in `_process_job`'s `finally`
   (`worker.py:1246-1254`), and exposes it as a property beside `current_job_id`
   (`worker.py:694-697`). `_status_context` reads it when the rendered job's state is
   `SCANNING_REVERSE`. **No signature change to the existing callback, no drift risk, no
   migration.**
2. Widen `status_callback` to `Callable[[PipelineEvent, int | None], None]`. Touches every
   call site (`pipeline.py:1376,1415,1501,1864,2037` and the CLI's `status_callback` at
   `cli.py:548`), and gives the CLI an argument it does not want.
3. A `PipelineEvent` member carrying data. Rejected: `PipelineEvent.job_state` is a
   `match`/`assert_never` projection and members are states, not payloads.

Recommend (1) and name it in the plan.

**History-table column count.** `partials/history.html` has four `<th>`s in
`index.html:76-81` and a `colspan="4"` empty-state row. Adding a counts column means
`colspan="5"` and a fifth `<th>` — a classic missed edit. The counts sentence is long
(`12 pages scanned, 2 blank removed, 10 uploaded`) and D-35 simultaneously widens the Time
column; on a phone (`UIX-01` is out of scope but the table already has
`.history-table-wrap`) a fifth column is expensive. **Recommendation: a compact form in a
fifth column (`12 / 2 / 10` with a `<th title>` or an `abbr`) or, better, a second line
inside the Title cell.** Either satisfies D-32; CONTEXT explicitly leaves the choice to the
planner.

### §7 — Profile `label` / `description` as tool-owned keys (D-18, D-19)

**Exactly what must change:**

| Location | Change |
|---|---|
| `config.py:250-282` `ProfileConfig` | Add `label: str = ""` and `description: str = ""`. `model_config` is `ConfigDict(extra="forbid", populate_by_name=True)`, so these must be real fields; defaulting to `""` keeps an old config loading (both Settings and every nested model are `extra="forbid"`, so a *new* key in an *old* saneless would fail — that direction is not this phase's problem). Bound them with `max_length` the way `default_title` is bounded (`config.py:277`) — a free-text field rendered into HTML deserves an explicit cap. |
| `auto_profiles.py:491-498` `_OWNED_KEYS` | Add `"label"` and `"description"`. Order matters: it is the file key order for a newly added table (`_generated_values` docstring, `auto_profiles.py:600`). Put them first — a human opening the file should read the label before the SANE source string. |
| `auto_profiles.py:590-622` `_generated_values` | Emit `label` and `description` **unconditionally** (unlike `auto_source_mode`/`duplex`, which are written only when non-default). Derive from `classify_source(profile.source)` / `SourceKind.uses_feeder` and `profile.duplex` per D-19. |
| `auto_profiles.py:340+` `generate_profiles` | Set `label=`/`description=` on each constructed `ProfileConfig`, alongside the existing `duplex=_duplex(source)` and `auto_source_mode=_auto_source_mode(...)`. |
| `auto_profiles.py:763-813` `_merge_profile` | **No change needed.** The refresh loop already iterates `_OWNED_KEYS` and sets/deletes; adding two keys to the tuple is sufficient. |
| `auto_profiles.py:817+` `write_profiles_to_config` | No change. |
| `docs/how-to/configure-scan-profiles.md:68,157` | The profile-field table gains two rows; line 157 spells out the owned-key list verbatim and must be extended. |

**Does Phase 27 D-03's "delete an owned key a fresh generation does not write" create a
hazard for a free-text field?** **No — because `_generated_values` will always write both
keys.** The deletion branch (`auto_profiles.py:809-812`) only fires for keys absent from
`values`, which is how a stale `duplex = "hardware"` gets pruned. With unconditional
emission the branch is unreachable for `label`/`description`.

The real hazard is different and CONTEXT does not mention it: **an already-deployed config
whose generated profiles predate this phase will never gain labels.** Startup generation
runs only when `is_bare_default(settings)` is true (`worker.py:757-762`,
`auto_profiles.py:207-234`), and a config with generated profiles is not bare, so the
worker returns early. Without `saneless auto-profiles --force`, those profiles keep
`label = ""` forever. See Amendment A-3.

Secondary note: D-18's overwrite rule means a hand-typed label on an `auto_generated = true`
profile is destroyed by `--force`. That is the documented, consistent rule and CONTEXT chose
it deliberately — but it is the first *free-text* casualty, so the how-to must say it in
plain words next to the escape hatch, not only in the owned-key list.

### §8 — Local timezone rendering (APPL-12, D-34, D-35)

**Verified, on this machine, with the project's own interpreter:**

```
TZ=America/Chicago  datetime(2026,9,16,19,3,tzinfo=UTC).astimezone()
  -> datetime(2026, 9, 16, 14, 3, tzinfo=timezone(timedelta(-1, 68400), 'CDT'))
  .strftime('%Y-%m-%d %H:%M %Z')  ->  '2026-09-16 14:03 CDT'
```

That is D-34's specimen string exactly. `astimezone()` with no argument builds a
fixed-offset `timezone` whose name comes from `time.localtime().tm_zone`, which is why
`%Z` yields `CDT` and not `UTC+00:00`. No `zoneinfo`, no new config key.

**DTZ clearance — verified with the project's ruff (0.15.7):** a file containing
`value.astimezone().strftime("%Y-%m-%d %H:%M %Z")` and checked with
`ruff check --select DTZ,PL` produces **no DTZ diagnostic**. DTZ targets naive
construction (`datetime.now()`, `utcnow()`, `fromtimestamp()`, `date.today()`,
`strptime` without `%z`, bare `datetime(...)`), none of which this uses. The input is
already aware: `_row_to_job` parses `created_at` with `datetime.fromisoformat` from a value
written as `datetime.now(tz=UTC).isoformat()` (`job.py:659`, `job.py:704`), so it carries
`+00:00`.

**Where the Jinja filters are registered:** `web/app.py:159-166`, on
`app.state.templates.env.filters`, immediately after `Jinja2Templates(...)` is built and
before any template is loaded — with a comment stating that "the templates own no
vocabulary of their own". `state_label`, `progress_label` and `flip_answer_label` are
registered there. The new `local_time` filter goes in the same block, and its
implementation belongs in `vocabulary.py` (or a tiny new module) so the CLI imports the
same function — CONTEXT's "one shared place".

**How the CLI renders times today:** `saneless jobs` prints
`f"{j.created_at.strftime('%Y-%m-%d %H:%M:%S'):<{ts_w}} "` at `cli.py:754`, with
`ts_w = 22` (`cli.py:742`). `saneless jobs --json` uses
`j.created_at.isoformat()` (`cli.py:731`) — **that is the machine contract and must stay
UTC ISO**; APPL-12 says "user-facing", and `docs/how-to/cli-scripting.md:64` documents
the JSON shape.

**Width arithmetic, measured:** `_STATUS_COL_WIDTH` computes to **16**
(`"Waiting for flip"` is the longest label; `cli.py:81`), `profile_w = 15`, three
separator spaces, and `title_w = max(15, cols - (ts_w + 15 + 16 + 3))` (`cli.py:745`).

| Format | Width | `ts_w` needed | `title_w` at 80 cols |
|---|---|---|---|
| today `%Y-%m-%d %H:%M:%S` | 19 | 22 (3 spare) | 24 |
| **`%Y-%m-%d %H:%M %Z`** | 16 + 1 + zone (3–5) = **19–22** | **22, unchanged** | **24, unchanged** |
| `%Y-%m-%d %H:%M:%S %Z` | 19 + 1 + 3–5 = 23–25 | 25 | 21 |

**Recommendation: drop the seconds in the CLI table and use `%Y-%m-%d %H:%M %Z` — the same
format as the web filter and the same format D-34 specifies. `ts_w` stays 22, `title_w`
stays 24 at 80 columns, and the two surfaces match byte for byte.** Worst realistic zone
abbreviation is 5 characters (glibc renders an abbreviation-less zone as `+0545`), which
fits 22 exactly. Keeping seconds would cost three characters of title on an 80-column
terminal and break D-35's "conversion happens in one shared place" by giving the two
surfaces different formats.

**Two things the plan must not miss:**

1. **`TZ` in the container.** `astimezone()` reads the host's zone. A Docker container
   has no `/etc/timezone` mapping by default and reports UTC, so APPL-12 would be
   *technically* satisfied and *practically* useless. `docker-compose.yml` and
   `docs/reference/docker.md` need a `TZ=America/Chicago`-style line with a one-line
   explanation. This is a real deliverable, not a nicety.
2. **`resolve_job_title`'s fallback title.** `config.py:333` renders
   `f"Scan {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')}"` and its docstring says in so
   many words: "The timestamp is rendered in UTC whatever the zone of `now` (local time is
   APPL-12)." That is an in-tree pointer at this phase. It is a user-facing timestamp (it
   becomes the Paperless document title). The plan should either change it to local and
   update the docs that quote the shape (`configure-scan-profiles.md:58`,
   `docs/reference/cli-commands.md`), or record an explicit decision not to. Silently
   leaving it is the one outcome that contradicts the code's own comment.

### §9 — Testing strategy

**How SANE is faked.** Two mechanisms, for two purposes.
`tests/conftest.py` exposes `StubScannerBackend`, a concrete `ScannerBackend` subclass
(not a `MagicMock` — `[Phase 03-03]` in STATE.md explains that an ABC + mock breaks the
lifespan), plus `scan_batch` and `spooling` helpers. `tests/fake_sane.py` (54 KB) is a fake
of the python-sane C module, monkeypatched onto `sane_backend.sane` — the module-level name
exists precisely so tests can swap it (`sane_backend.py:53`). `tests/conftest.py:183-254`
provides `reset_sane_process_state` / `sane_process_state` (autouse) so the `_INIT` guard
does not leak across tests. New scanner-check tests should use a `StubScannerBackend`
subclass whose `get_devices` raises or blocks, exactly as `_BrowserTestScanner`
(`test_browser.py:98-140`) does with its `threading.Event` gate.

**How Paperless is faked.** Three mechanisms. (a) Attribute patching on the live client:
`tests/test_web.py:104-113` assigns `app.state.paperless.get_tags = lambda: [...]`, and
`test_browser.py:326-345` does the same for `upload_document`/`poll_task`. (b) An
`httpx.MockTransport` passed as `PaperlessClient(_transport=...)` — the constructor takes
`_transport` for exactly this (`paperless.py:456`). (c) For a check that must be slow, a
`MockTransport` handler that sleeps is wrong (no `sleep`); instead assert the **timeout
argument** the client was called with. **The right test for success criterion 2 is not
"it was fast" but "the probe was issued with a bounded `httpx.Timeout`" plus "an
unreachable host yields `UNREACHABLE` rather than propagating".** That is deterministic
and needs no clock.

**How Playwright tests are structured.** Session-scoped `browser_server` fixture
(`test_browser.py:247-260`) runs the real app under uvicorn on a daemon thread bound to
port 0; `_start_uvicorn`/`_stop_uvicorn` (`:212-244`) manage it and assert the thread
ended. A module-level **`context` fixture override** (`:284-312`) installs an egress gate
that aborts any request not on `egress_allowlist` and asserts nothing was blocked, so the
whole module runs offline in CI. Colour expectations live in one block at `:79-90`
(`_AMBER = {"light": "rgb(161, 98, 7)", "dark": "rgb(202, 138, 4)"}`), pinned to Pico 2.1.1.

**Two browser contexts (success criterion 5): nothing in the module does this today**
(no `browser.new_context()` anywhere — verified by grep). The pattern to add:

```python
def test_owner_sees_flip_prompt_and_other_viewer_does_not(
    browser: Browser, browser_server: _BrowserServer, egress_allowlist: list[str],
) -> None:
    owner_ctx = browser.new_context()
    viewer_ctx = browser.new_context()
    for ctx in (owner_ctx, viewer_ctx):
        ctx.route("**/*", _make_gate(blocked, egress_allowlist))   # the gate must be applied by hand
    ...
```

**The egress gate is on the overridden `context` fixture only** — a hand-made second
context bypasses it silently, which would let a browser test reach the network in CI. The
plan must extract the gate closure into a module-level helper and apply it to **both**
contexts, and the assertion at teardown must cover both. This is a genuine trap.

Separate contexts give separate cookie jars, which is exactly what D-23's per-browser
token needs. Chromium 1228 is installed locally (`~/.cache/ms-playwright`), so no install
step is required.

**Testing a background thread deterministically, with no `time.sleep`.** `time.sleep`
appears six times in the suite today (`test_browser.py:226`, `test_web.py:166`,
`test_worker.py:390,626,2113,4472`) and Phase 32 removes them; this phase must add none.
Three techniques, all already used in-tree:

1. **`threading.Event` as the synchronisation point.** The worker tests and
   `_BrowserTestScanner.gate` both do this: the fake under test sets an `Event` when it is
   entered, the test waits on it with a bounded timeout, then sets a second `Event` to let
   it proceed. For `CheckRefresher`, give it an injectable "probe ran" hook or have the
   fake check set the event.
2. **Drive the loop body directly.** Make `CheckRefresher._tick()` a pure method the test
   calls synchronously — no thread at all for the logic tests. Reserve the threaded test
   for start/stop/join semantics only. This is the single highest-value design decision for
   testability in the phase.
3. **Injectable clock for the cache.** `CheckCache(ttl=30, clock=fake.now)` where the test
   advances a float. `web/cache.MetadataCache` calls `time.monotonic()` inline
   (`cache.py:54,66`), which is exactly why `tests/test_cache.py:26` has to
   `time.sleep(1.1)`. **Do not repeat that mistake.** An injectable clock defaulting to
   `time.monotonic` costs one parameter and removes every sleep from the TTL tests.

**What makes each new test "could not have passed before":** the lifespan-ordering
assertion at `test_app_lifespan.py:335` (extended for the refresher), the
`_command_exit_tables` iteration at `test_deployment_config.py:432` (which hard-fails the
moment `cli-commands.md` grows a `doctor` section without an exit-code table), the
completeness test over `ErrorCategory` that `error_message` currently satisfies alone
(`vocabulary.py:476` docstring: "the completeness test is this function's only consumer"),
and the `_OWNED_KEYS` round-trip tests in `test_auto_profiles.py`.

### §10 — Docs to correct in-phase

All eight paths named in CONTEXT's `canonical_refs` **exist** (verified by `find docs`):
`docs/reference/cli-commands.md`, `docs/how-to/cli-scripting.md`,
`docs/how-to/deploy-docker-compose.md`, `docs/reference/docker.md`,
`docs/how-to/configure-scan-profiles.md`, `docs/reference/configuration.md`,
`docs/reference/web-api.md`, `docker-compose.yml`.

**Additional pages this phase makes false, not listed in CONTEXT:**

| Path | What it asserts | Why this phase falsifies it |
|---|---|---|
| `docs/explanation/architecture.md:99` | "`outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning` and `owner_token` exist in the schema but **nothing writes any of them yet**, so they read back unset on every job and **no part of the UI or CLI displays them**." | Already half false (Phase 23 writes all but `owner_token`); APPL-03 and APPL-09 make it wholly false. Highest-value fix on this list. |
| `docs/reference/cli-commands.md:3` | "saneless provides **five** commands" | Six. No test pins the word. |
| `docs/reference/docker.md:178` | ships `SANELESS_PAPERLESS__TOKEN=changeme` in an example | D-14 makes `changeme` a detected placeholder and D-17 makes the file the one place for the secret. An example that ships a value the app flags red is self-contradictory. |
| `docs/how-to/cli-scripting.md:49` | `"created_at": "2026-03-22T14:30:00"` | The real output carries `+00:00` (`isoformat()` on an aware datetime). Pre-existing inaccuracy; worth fixing while APPL-12 is in hand, and worth stating explicitly that the JSON stays UTC while the table goes local. |
| `docs/reference/configuration.md` | has `[scanner]`, `[paperless]`, `[output]`, `[profiles.NAME]` sections | D-28's `[web]` is a **new top-level section** on `Settings` (`config.py:466-471`), so it needs a section of its own plus a row in `docs/reference/environment-variables.md` for `SANELESS_WEB__SHOW_TAGS` etc. |
| `docs/getting-started/first-web-ui-scan.md` | walks the current form | Gains the strip, help text, the checkbox tag picker and the profile labels. Not in CONTEXT's list. |
| `docs/PRD.md:146` | "**Test Connection button:** Located near the paperless-ngx URL…" — never built (N-29) | APPL-02 delivers the capability. Optional, but the PRD line is now answerable. |

Note the `[web]` placement question: `web_host` and `web_port` live in `OutputConfig`
(`config.py:368-369`), so `[web] show_tags` next to `[output] web_port` is mildly
incoherent. Two coherent options: put `show_tags`/`show_correspondent` under `[output]`
(no new section, no new env prefix, slightly odd name) or create `[web]` and leave
`web_host`/`web_port` where they are (a new section, documented). CONTEXT says "e.g.
`[web] show_tags`", so `[web]` is the steer; flag the incoherence in the config reference
so the next reader is not confused.

---

## Amendments Required

Each is a mechanical change that preserves the decision's intent. None substitutes a
different design.

**A-1 — `doctor` and `require_sane()` (APPL-01).**
Every SANE-using command calls `require_sane()` first, which raises `ConfigError` → exit 2
through the guard (`cli.py:518`, `:650`, `:769`). If `doctor` does the same, a machine
without python-sane gets *one line about python-sane* and learns nothing about its
Paperless token, its profiles, its fallback or its data dir — the opposite of what
APPL-01 is for. **Amendment:** `doctor` must not call `require_sane()` at the top. Catch
the import failure inside the scanner check and render it as a `FAIL` row
("python-sane is not installed"), so the other four checks still run and the exit code is
still 2. Same non-zero outcome, far more information.

**A-2 — APPL-06 needs a signal the worker does not currently keep.**
`_persist_generated_profiles` (`worker.py:832-905`) returns `None` for **two different**
situations — "no config file was loaded" (INFO, line 838) and "could not write the file"
(WARNING, lines 855 and 869) — and keeps no record of which. D-22 needs to distinguish
them, and a fresh `os.access()` probe at check time cannot: Phase 27 D-09's motivating
failure is EBUSY on a single-file bind mount, where the directory *is* writable and only
the rename fails. **Amendment:** the worker records the outcome of its one startup persist
attempt in a small StrEnum (`ProfileStorage.PERSISTED` / `IN_MEMORY_NO_CONFIG_FILE` /
`IN_MEMORY_UNWRITABLE`) and exposes it as a property; the profiles check reads it. This
is a handful of lines and it makes the amber row truthful instead of guessed.

**A-3 — pre-existing generated profiles never gain a `label` (APPL-05).**
Startup generation runs only when `is_bare_default(settings)` (`worker.py:757-762`), so a
config that already has generated profiles is skipped and keeps `label = ""` after an
upgrade — the dropdown would show a blank option. **Amendment:** the dropdown renders
`profile.label or name`, and `docs/how-to/configure-scan-profiles.md` says that
`saneless auto-profiles --force` is what backfills labels into an existing config. A
render-time fallback, not a migration. (Optional stronger version: the profiles check
emits a `warn` when any `auto_generated` profile has no label, with "run
`saneless auto-profiles --force`" as the next step. This is the kind of self-diagnosis
APPL-01 exists for.)

**A-4 — D-33's live count has no channel (APPL-03).**
`status_callback` is `Callable[[PipelineEvent], None]` and carries no payload
(`pipeline.py:354`); `notify(PipelineEvent.SCANNING_REVERSE)` at `pipeline.py:1693` cannot
carry `len(front_pages)`. **Amendment:** add a `pass_count_callback` to `PipelineRequest`
mirroring the existing `thumbnail_callback`, and have the worker hold the value beside
`current_job_id`, cleared in `_process_job`'s `finally` (`worker.py:1246-1254`). See §6 for
why the two alternatives are worse.

**A-5 — D-31's filter swap loses checked tags (APPL-10).**
See §5, hazard 1. **Amendment:** the filter request carries the currently-checked tag ids
(`hx-include`) and the route re-renders them checked; selected-but-filtered-out tags are
rendered too (pinned above the filtered list) so a tick can never be silently lost.

**A-6 — a named filter input inside the scan form is submitted with the scan (APPL-10).**
See §5, hazard 2. **Amendment:** place the filter input outside `<form hx-post="/api/scan">`
and target the tag list by id, or accept the stray field and pin it with a regression test.

**A-7 — the lifespan shutdown must gate on both threads (APPL-02).**
`web/app.py:123-143` closes `paperless`, `job_store` and `scanner` when `worker.stop()`
returns True. The refresh thread uses `paperless` and may be inside SANE. **Amendment:**
set both stop events, join both, and close only when both confirm — with the same
"leave everything open and return" branch otherwise. `tests/test_app_lifespan.py:335` must
be extended accordingly.

---

## Common Pitfalls

### Pitfall 1: Closing the Paperless client or SANE under a live refresh thread
**What goes wrong:** `paperless.close()` under an in-flight probe raises inside the
refresher; `scanner.close()` → `sane.exit()` while the refresher is inside
`sane_get_devices` is the close-while-calling sequence `sane_backend.shutdown()` documents
as a segfault risk (`sane_backend.py:929-935`).
**Why it happens:** the shutdown branch only knows about one thread.
**How to avoid:** Amendment A-7.
**Warning signs:** "Cannot operate on a closed database", `httpx.ClientClosedError` at
shutdown, or a test-suite segfault in the browser job.

### Pitfall 2: Two concurrent SANE calls
**What goes wrong:** the refresher calls `get_devices()` while the worker is inside
`scan_pages`. On the `net` backend both are RPCs on the same control wire.
**Why it happens:** there is **no mutual-exclusion lock** in `sane_backend.py`. The only
guard is `_refuse_if_wedged` (`sane_backend.py:2064`), which fires on a *stuck* read, not a
running one; `_INIT_LOCK` guards `sane_init`/`sane_exit` only, and `_WEDGE_LOCK` guards the
wedge record.
**How to avoid:** §1's `scanner_gate` on the worker, plus the `current_job_id` check as a
cheap first filter.
**Warning signs:** intermittent `Could not list scanners:` during a scan; a scan that
returns fewer pages than fed.

### Pitfall 3: `doctor` refusing before it can report
See Amendment A-1.

### Pitfall 4: Rendering `0` for a NULL page count
`{% if job.pages_uploaded %}` hides a true zero (a scan where every page was blank).
Use `is not none`. Four of six terminal cases are NULL (§6).

### Pitfall 5: `create_job` hits `PLR0913` when `owner_token` is added
**Verified with the project's ruff:** `PLR0913` counts keyword-only parameters, and
`max-args` is the default 5. `JobStore.create_job` (`job.py:669-675`) already has exactly
five non-`self` parameters (`profile`, `title`, `tags`, `correspondent`, `thumbnail`).
Adding `owner_token` is a lint failure, and this project adds no suppressions. `JobResult`
(`job.py:534-551`) exists for precisely this reason — its docstring says so.
**How to avoid:** bundle the submission fields into a frozen dataclass (the `_ScanForm` at
`routes.py:250-256` is already almost that shape), or drop `thumbnail` from `create_job`
(nothing passes it — `update_thumbnail` is the live writer, `worker.py:1288`) and spend the
slot on `owner_token`. Verify the "nothing passes it" claim before acting on it.

### Pitfall 6: The strip cache throwing away last-known-good
D-08 requires "the last known results with a note". `MetadataCache.get_or_fetch`
(`cache.py:69-116`) **re-raises** on a failing fetch and caches nothing; it is the *route*
that swallows the exception and substitutes `[]` (`routes.py:76-83`). CONTEXT's code_context
describes it as keeping a stale entry — that is not what the code does. The check cache must
keep the previous results explicitly and stamp them with their age.

### Pitfall 7: A second `match` over `ErrorCategory`
D-10 is explicit. `error_message` (`vocabulary.py:476-521`) already has the seven arms; a
sibling `error_next_step` with its own `match` is two statements that drift. One
`error_advice` returning a frozen `(message, next_step)` pair, with `error_message` as a
one-line accessor or gone entirely. The existing completeness test in
`tests/test_vocabulary.py` must be pointed at the new function.

### Pitfall 8: Removing `hx-disinherit` from the scan form
`index.html:11-14` explains that on htmx 2.0.8 an inherited `hx-disabled-elt` strips
`disabled` from the server-disabled Scan button when a child request finishes — C-10, fixed
only in 2.0.9, and htmx 4 is out of scope. Any new `hx-` attribute added **to the form**
must be added to the disinherit list in the same change.

### Pitfall 9: A second Playwright context bypassing the egress gate
The gate is installed on the overridden `context` fixture only
(`test_browser.py:284-312`). A hand-made `browser.new_context()` has no gate and can reach
the network in CI without failing anything. Extract the gate into a helper and apply it to
both contexts, asserting the shared `blocked` list at teardown.

### Pitfall 10: `TZ` is UTC in the container
§8. `astimezone()` is only as local as the host says it is.

### Pitfall 11: `colspan` drift in the history table
Four `<th>`s in `index.html:76-81` and `colspan="4"` in `history.html`. A fifth column
needs both edited.

---

## Runtime State Inventory

Phase 30 is not a rename or a migration, but three categories of state outside the source
tree change behaviour and are easy to miss.

| Category | Items found | Action required |
|----------|-------------|------------------|
| Stored data | `jobs.owner_token` — column exists (`job.py:501`), migrated in, **never written** (`job.py:715`, `:786`). Every existing row is NULL. | Code edit only. NULL means "pre-owner-token job"; the flip prompt must treat NULL as "nobody owns it" and render for everyone, or no existing in-flight job could ever be continued after an upgrade. **Decide this explicitly.** |
| Stored data | `jobs.pages_*` — NULL on every pre-Phase-23 row and on every ERROR/CANCELLED/REJECTED row (§6). | No migration. Rendering must handle NULL (D-32). |
| Live service config | Existing `config.toml` files on deployed machines: `[profiles.*]` tables written by Phase 27 carry no `label`/`description`. | Amendment A-3: render-time fallback plus a documented `auto-profiles --force`. |
| Live service config | `docker-compose.yml` `environment:` block currently sets `SANELESS_PAPERLESS__URL` and `SANELESS_PAPERLESS__TOKEN=changeme` (lines 29-30). Operators who copied it have those in *their* compose file, not in git. | D-17 comments the block out in the shipped template. The docs must tell an existing operator to remove their own env line, or their real config.toml stays overridden and the strip stays red. This is a documentation action, not a code one. |
| Secrets / env vars | `SANELESS_PAPERLESS__TOKEN` — name unchanged. No key rename. | None. |
| OS-registered state | None. | None — verified: no systemd unit, no task scheduler, no pm2 in the tree. |
| Build artifacts | None affected. Vendored htmx/Pico bytes and their SRI pins are untouched (htmx stays 2.0.8). | None. |
| New environment | `TZ` in the container is newly load-bearing (§8). | Compose + docs. |

---

## Code Examples

### Bounding the Paperless probe

```python
# Source: verified against httpx docs (Context7, docs/advanced/timeouts.md)
#         and src/saneless/paperless.py:949-992
def test_connection(self, timeout: httpx.Timeout | None = None) -> ConnectionStatus:
    try:
        response = self._client.get(
            "/api/tags/", params={"page_size": 1},
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
    except httpx.TransportError:      # base of ConnectError/ConnectTimeout/ReadTimeout
        logger.warning("Paperless is unreachable")
        return ConnectionStatus.UNREACHABLE
    ...
```

### `error_advice` (D-10)

```python
# Source: the shape of src/saneless/vocabulary.py:476-521, one match, assert_never
@dataclass(frozen=True, slots=True)
class ErrorAdvice:
    """What a reader is told about an error category, and what to do next."""

    message: str
    next_step: str


def error_advice(category: ErrorCategory) -> ErrorAdvice:
    match category:
        case ErrorCategory.FEEDER:
            advice = ErrorAdvice(
                "The document feeder is empty or jammed.",
                "Check the feeder is loaded and the lid is closed, then scan again.",
            )
        ...
        case _:
            assert_never(category)
    return advice
```

### The technical-details disclosure (D-13)

```jinja
{# Source: shape of src/saneless/web/templates/partials/status.html:31 #}
<p role="alert" class="status-error">&#10007; {{ job.error_category | error_message }}</p>
<p>{{ job.error_category | error_next_step }}</p>
<details>
  <summary>Technical details</summary>
  <p>{{ job.error }}</p>
  <p>Category: {{ job.error_category }}</p>
  <p>Job: {{ job.id }}</p>
</details>
```

### Local timestamp filter, shared by web and CLI

```python
# Verified output: '2026-09-16 14:03 CDT'; no DTZ diagnostic under ruff 0.15.7
LOCAL_TIME_FORMAT: Final = "%Y-%m-%d %H:%M %Z"


def local_time(value: datetime) -> str:
    """Render an aware timestamp in the server's local zone, naming the zone."""
    return value.astimezone().strftime(LOCAL_TIME_FORMAT)
```

Registered beside the existing filters at `web/app.py:163-165`; imported directly by
`cli.py`'s `jobs` table so the two cannot disagree.

### Abort confirmation (D-27)

```html
<!-- Source: src/saneless/web/templates/partials/flip.html:44 -->
<button hx-post="/api/flip/abort"
        hx-vals='{{ {"job_id": job.id} | tojson }}'
        hx-target="#status-area" hx-swap="outerHTML"
        hx-confirm="Abort this scan? The pages already scanned will be kept, but the scan will stop."
        class="secondary">Abort scan</button>
```

Outside the scan form, so the `hx-disinherit` landmine does not apply.

---

## State of the Art

| Old approach | Current approach | When changed | Impact here |
|---|---|---|---|
| Client-side JS for confirm/debounce/disable | Declarative `hx-` attributes only | Phase 26 deleted `app.js` | Every interaction in this phase is an attribute; no build step, no script |
| `hx-disabled-elt` inherited by children | `hx-disinherit="hx-disabled-elt"` on the form | htmx 2.0.8 bug, fixed in 2.0.9 | Do not remove; extend if a new inheritable attribute is added to the form |
| Flat `httpx` client timeout | Per-request `httpx.Timeout(read, connect=…)` | httpx 0.x, stable | The only knob that makes the Paperless probe bounded |
| Naive `datetime.utcnow()` | Aware datetimes + `astimezone()`; ruff `DTZ` enforces it | ruff `DTZ` enabled in this project | `astimezone().strftime("%Z")` is the compliant local-zone idiom |
| Bare `except:` around schema migration | `PRAGMA user_version` ladder with a loud `StorageError` | Phase 22 | No new columns are needed, so none of this is touched |

**Deprecated / outdated for this phase:**
- HTTP 286 for stopping a poll — supported, but the in-tree idiom (emit `hx-trigger` only
  while the poll is wanted) is already proven and does not touch the tuned `htmx-config`.
- `MetadataCache` as a drop-in for the check cache — wrong value type and wrong
  failure semantics (Pitfall 6).

---

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|---|---|---|
| A1 | saned's default TCP port is 6566 | §2 | The pre-probe checks the wrong port and always reports the scanner down. **Verify against `sane-net(5)` / `/etc/services` on the target machine before coding.** Mitigation: if the pre-probe fails, fall back to `get_devices()` rather than reporting `FAIL`, so a wrong port degrades to today's behaviour. |
| A2 | Linux's default connect timeout for an unreachable host is roughly 127 s (`tcp_syn_retries=6`) | §2 | The number in the plan's rationale is wrong; the *conclusion* (there is no Python-level bound) is independent of it and verified from the code. |
| A3 | `%Z` for a zone without an abbreviation renders as `+0545` (5 chars) under glibc | §8 | If some platform renders 6+ characters, `ts_w = 22` truncates the zone. Cheap insurance: compute `ts_w` from a rendered sample the way `_STATUS_COL_WIDTH` is computed from labels (`cli.py:81`). |
| A4 | FastAPI silently ignores unexpected form fields on a `Form(...)` route | §5 | If it 422s, a stray `q` breaks every scan submit. Trivially verifiable with one `TestClient` call; Amendment A-6's preferred fix sidesteps it entirely. |
| A5 | `thumbnail` is never passed to `JobStore.create_job` | Pitfall 5 | The proposed parameter swap would break a caller. Verify with a grep before relying on it; the dataclass-bundle alternative is unaffected. |
| A6 | Browsers process `Set-Cookie` on an htmx XHR response identically to a navigation | §4 | The owner token would never persist. Directly assertable in Playwright via `context.cookies()`; make that a Wave-1 test, not an assumption carried to the end. |
| A7 | No consumer anywhere wants `doctor --json` | §3 | A future script would need it. Low cost to add later; adding it now creates a wire contract with no reader. |

---

## Open Questions

1. **Does an existing NULL `owner_token` mean "everyone" or "nobody"?**
   - What we know: every row in every deployed database has `owner_token IS NULL`
     (`job.py:715`), and D-26 says a vanished owner is resolved by the flip timeout.
   - What's unclear: a manual-duplex job that was *in flight* across the upgrade has a NULL
     token, so under a strict rule nobody can press Continue and the job must time out.
   - Recommendation: treat NULL as unowned and render the prompt for everyone. It matches
     pre-upgrade behaviour exactly, affects only rows that predate the feature, and cannot
     be exploited (there is no way to create a NULL-token job after this phase).

2. **`[web]` as a new top-level section, or two keys under `[output]`?**
   - What we know: `web_host`/`web_port` already live in `OutputConfig`
     (`config.py:368-369`); `Settings` is `extra="forbid"` so a new section is a real
     schema addition; CONTEXT says "e.g. `[web] show_tags`".
   - Recommendation: create `[web]`, and add one sentence to
     `docs/reference/configuration.md` acknowledging that the bind address stayed in
     `[output]` for backwards compatibility. Moving `web_host`/`web_port` would be a
     breaking config change and is not in scope.

3. **`scanner_gate` on the worker, or advisory `current_job_id` only?**
   - What we know: there is no SANE mutual exclusion today (Pitfall 2); the race window is
     small but the consequence is a concurrent SANE call, not a slow page.
   - Recommendation: the gate. If the plan judges it too invasive, it must say so
     explicitly and record the accepted race — not leave it implicit.

4. **Does `resolve_job_title`'s `Scan <time>` fallback go local (APPL-12)?**
   - What we know: `config.py:333` renders UTC and its docstring points at APPL-12 as the
     change; it is a user-facing string that ends up as a Paperless document title.
   - Recommendation: change it, and say so in `docs/how-to/configure-scan-profiles.md`.
     Whatever is decided, decide it — the in-tree comment makes silence a regression.

5. **Fifth history column, or a second line in the Title cell (D-32)?**
   - Left to the planner by CONTEXT. §6 sets out the width cost of each.

---

## Environment Availability

| Dependency | Required by | Available | Version | Fallback |
|---|---|---|---|---|
| Python | everything | ✓ | 3.14.2 | — |
| `python-sane` + libsane | scanner check | ✓ | imports cleanly | Amendment A-1: a `FAIL` row, not a refusal |
| `httpx` | Paperless probe | ✓ | 0.28.1 | — |
| `fastapi` / `jinja2` | web tier | ✓ | 0.135.1 / 3.1.6 | — |
| `tomlkit` | profile label writing | ✓ | 0.14.0 | — |
| `ruff` | lint gate | ✓ | 0.15.7 | — |
| `ty` | type gate | ✓ | 0.0.80 | — |
| `pyrefly` | type gate | ✓ | 1.2.0 | — |
| Playwright chromium | browser tests | ✓ | chromium-1228 cached | — |
| A real scanner | nothing in this phase | ✗ | — | `StubScannerBackend` / `fake_sane` cover every path |
| A real paperless-ngx | nothing in this phase | ✗ | — | attribute patching + `httpx.MockTransport` |

**Missing dependencies with no fallback:** none.
**Missing dependencies with fallback:** real scanner and real Paperless — both already
faked by the suite; no `sane_hardware`-marked test is needed for this phase.

---

## Validation Architecture

### Test Framework

| Property | Value |
|---|---|
| Framework | pytest 9.x with `pytest-timeout`, `pytest-playwright` |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `--strict-markers --strict-config`, `filterwarnings=["error"]`, `timeout=60`, `timeout_method="signal"`) |
| Quick run command | `uv run pytest tests/test_web.py tests/test_cli.py -x -q` |
| Full suite command | `uv run pytest` |
| Gates | `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`, `uv run prek run --all-files` |

`filterwarnings = ["error"]` means a `DeprecationWarning` from any new code is a hard
failure. `timeout_method = "signal"` means a hung background thread fails one test rather
than the run — but a thread left running still leaks into later tests, so every new thread
needs a fixture that stops it.

### Phase Requirements → Test Map

| Req | Behaviour to sample | Type | Automated command | Exists? |
|---|---|---|---|---|
| APPL-01 | `doctor` exits 0 when all checks ok/warn, 2 when any fails | unit (CliRunner) | `pytest tests/test_doctor.py -x` | ❌ Wave 0 |
| APPL-01 | A placeholder token makes `doctor` exit non-zero | unit | same file | ❌ Wave 0 |
| APPL-01 | `doctor` reports all five checks with python-sane unavailable (A-1) | unit | same file | ❌ Wave 0 |
| APPL-01/02 | The registry is the *same object* both surfaces consume (no duplicate definition) | unit | `pytest tests/test_checks.py -x` | ❌ Wave 0 |
| APPL-02 | `GET /` never calls `run_checks` (spy asserts zero calls) | unit | `pytest tests/test_web_checks.py -x` | ❌ Wave 0 |
| APPL-02 | The Paperless probe is issued with a bounded `httpx.Timeout` (not the 30 s client default) | unit | `pytest tests/test_paperless.py -k timeout -x` | ❌ Wave 0 |
| APPL-02 | Scanner check is skipped and the strip says "Paused during scan" while a job is active | unit | `pytest tests/test_web_checks.py -k paused -x` | ❌ Wave 0 |
| APPL-02 | Refresh during a scan re-runs only non-scanner checks (D-09) | unit | same file | ❌ Wave 0 |
| APPL-02 | TTL expiry, driven by an injected clock — **no `sleep`** | unit | `pytest tests/test_checks_cache.py -x` | ❌ Wave 0 |
| APPL-02 | Refresher stops within the bounded join; lifespan closes nothing until both threads stop (A-7) | unit | `pytest tests/test_app_lifespan.py -x` | ⚠️ extend `:302-360` |
| APPL-02 | Cold start renders `Checking…` and the swapped-in partial carries no poll trigger | unit + browser | `pytest tests/test_web_checks.py tests/test_browser.py -k checking` | ❌ Wave 0 |
| APPL-03 | Counts sentence rendered for DONE and FALLBACK; **nothing** rendered for a NULL | unit | `pytest tests/test_web_state_rendering.py -k pages -x` | ❌ Wave 0 |
| APPL-03 | `Front: N pages · Scanning backs…` during `SCANNING_REVERSE` (A-4) | unit | `pytest tests/test_worker.py -k front_count -x` | ❌ Wave 0 |
| APPL-04 | Every `ErrorCategory` member has a message **and** a next step (completeness, `assert_never`) | unit | `pytest tests/test_vocabulary.py -k advice -x` | ⚠️ retarget existing completeness test |
| APPL-04 | `<details>` holds error text + category + job id and **not** the log path | unit | `pytest tests/test_web_state_rendering.py -k details -x` | ❌ Wave 0 |
| APPL-04 | CLI prints Phase 28's first line **unchanged** plus a second advice line (D-12) | unit | `pytest tests/test_cli.py -k advice -x` | ❌ Wave 0 |
| APPL-05 | Generated profiles carry the three label/description forms | unit | `pytest tests/test_auto_profiles.py -k label -x` | ❌ Wave 0 |
| APPL-05 | `--force` rewrites `label`/`description` in place and keeps `default_tags`/comments | unit | same file | ❌ Wave 0 |
| APPL-05 | Feeder profiles sort first when `has_flatbed` is false | unit | `pytest tests/test_web.py -k ordering -x` | ❌ Wave 0 |
| APPL-05 | Blank label falls back to the profile name (A-3) | unit | same | ❌ Wave 0 |
| APPL-06 | Unwritable config yields the amber warn row with the exact copy | unit | `pytest tests/test_checks.py -k readonly -x` | ❌ Wave 0 |
| APPL-07 | Placeholder set detected: `""`, whitespace, `changeme`, `your-api-token-here`, … | unit | `pytest tests/test_config.py -k placeholder -x` | ❌ Wave 0 |
| APPL-07 | `POST /api/scan` refuses with the **new** rejection member and writes a REJECTED row | unit | `pytest tests/test_web_errors.py -k placeholder -x` | ❌ Wave 0 |
| APPL-07 | `saneless scan` exits 2 **before** the scanner is opened (stub asserts zero opens) | unit | `pytest tests/test_cli.py -k placeholder -x` | ❌ Wave 0 |
| APPL-08 | Queued job renders `Waiting for 'X' to finish (N ahead of you)` from `list_pending()` | unit | `pytest tests/test_web_state_rendering.py -k queue -x` | ❌ Wave 0 |
| APPL-09 | `POST /api/scan` sets an HttpOnly, SameSite=Lax, session (no Max-Age) cookie | unit | `pytest tests/test_web.py -k owner_cookie -x` | ❌ Wave 0 |
| APPL-09 | Two `TestClient`s: owner gets the buttons, non-owner gets the waiting copy | unit | same | ❌ Wave 0 |
| APPL-09 | **Two browser contexts**, real cookie jars, owner vs non-owner rendering | browser | `pytest tests/test_browser.py -k two_contexts` | ❌ Wave 0 |
| APPL-09 | Abort renders `hx-confirm` and the native dialog fires | browser | `pytest tests/test_browser.py -k confirm` | ❌ Wave 0 |
| APPL-10 | Every control has help text; tag checkboxes ≥ 44×44 CSS px | browser | `pytest tests/test_browser.py -k touch_target` | ❌ Wave 0 |
| APPL-10 | `show_tags=false` hides the control **and** the profile's `default_tags` still apply (D-29) | unit | `pytest tests/test_web.py -k simple_form -x` | ❌ Wave 0 |
| APPL-10 | Filtering preserves already-checked tags (A-5) | unit + browser | `pytest -k tag_filter` | ❌ Wave 0 |
| APPL-11 | Compose has the consume mount; strip says "Fallback: not configured…" when absent | unit | `pytest tests/test_deployment_config.py -x` | ⚠️ extend |
| APPL-12 | `local_time` renders `%Z`; web and CLI use the **same** function | unit | `pytest tests/test_vocabulary.py -k local_time -x` | ❌ Wave 0 |
| APPL-12 | `saneless jobs` table still fits at 80 columns with the zone suffix | unit | `pytest tests/test_cli.py -k table_width -x` | ❌ Wave 0 |
| APPL-12 | `jobs --json` stays UTC ISO (machine contract unchanged) | unit | same file | ⚠️ may exist |

### Sampling Rate

- **Per task commit:** `uv run ruff check . && uv run ty check && uv run pytest <the module(s) touched> -x -q`
- **Per wave merge:** `uv run pytest` (full suite) + `uv run pyrefly check src tests`
- **Phase gate:** `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files` green, full suite green, browser module green **offline**, before `/gsd-verify-work`.

### What each success criterion actually requires sampling

1. **Criterion 1 (doctor + strip agree):** the measurable fact is *not* "both look the
   same" but "both call the same registry". Test it structurally: one test asserts
   `set(CheckKey) == {r.key for r in run_checks(...)}`, and a second asserts the strip
   template renders one row per `CheckKey`. Then a `doctor` exit-code test per state.
2. **Criterion 2 (fast with the scanner unplugged):** do **not** measure wall-clock — it
   is flaky and this suite forbids `sleep`. Measure the three facts that *cause* speed:
   (a) `GET /` issues zero probes; (b) the Paperless probe carries a bounded
   `httpx.Timeout`; (c) the scanner check is skipped while a job is active. A single
   optional wall-clock smoke test against a blackholed host may be added with a generous
   bound, but it must not be the gate.
3. **Criterion 3 (page counts everywhere):** a parametrised test over the six terminal
   cases (DONE, FALLBACK, ERROR, CANCELLED, REJECTED, pre-23 NULL row) asserting the
   rendered text, plus one worker test for the live pass-A count at `SCANNING_REVERSE`.
4. **Criterion 4 (plain-language errors):** the completeness test over `ErrorCategory` is
   the gate; a rendering test per surface (web `<details>`, CLI second line) is the proof.
5. **Criterion 5 (two contexts, labels, read-only strip):** the two-context Playwright test
   is the only honest sample. Per CLAUDE.md this is **not** manual — `browser.new_context()`
   twice, with the egress gate applied to both (Pitfall 9).

### Wave 0 Gaps

- [ ] `tests/test_checks.py` — the registry, the five checks, three states (APPL-01, -02, -06, -11)
- [ ] `tests/test_checks_cache.py` — TTL with an **injected clock**, last-known-good retention (APPL-02)
- [ ] `tests/test_web_checks.py` — routes, cold-start poll, paused-during-scan, Refresh (APPL-02)
- [ ] `tests/test_doctor.py` — CliRunner exit codes and table output (APPL-01)
- [ ] Extend `tests/test_app_lifespan.py` — the refresher's start/stop and the close-ordering assertion at `:335` (A-7)
- [ ] Extend `tests/test_deployment_config.py` — `doctor`'s command exit-code table, the compose consume mount, the commented env block, the "six commands" line
- [ ] Extend `tests/test_browser.py` — a module-level egress-gate helper and the two-context fixture (Pitfall 9)
- [ ] Retarget the `ErrorCategory` completeness test in `tests/test_vocabulary.py` at `error_advice`
- No framework install needed.

---

## Security Domain

`security_enforcement` is not set to `false` in `.planning/config.json`, so this section applies.

### Applicable ASVS Categories

| ASVS Category | Applies | Standard control in this phase |
|---|---|---|
| V2 Authentication | **no** | There is no login, by decision. REQUIREMENTS states the owner token is "a footgun guard for the flip prompt, not an auth mechanism". Do not let a plan drift it into one. |
| V3 Session Management | **partially** | D-23's cookie is a session identifier in shape. Controls: `HttpOnly`, `SameSite=Lax`, no `Max-Age`, `secrets.token_urlsafe(32)` (≥128 bits), never logged, never rendered into HTML. `Secure` is deliberately omitted — the LAN deployment is plain HTTP and `Secure` would silently disable the cookie. |
| V4 Access Control | **partially** | Owner-only rendering of Continue/Abort. Enforcement must be **server-side** (do not render the buttons), never CSS. The comparison should use `secrets.compare_digest` rather than `==`: it costs nothing and removes a timing question from review. |
| V5 Input Validation | **yes** | New inputs: `q` (tag filter), `resource`-style path params for the profile description, `job_id` on the flip routes (already `Form(...)`). Follow `MetadataResource`'s precedent (`routes.py:54`) — a `Literal` or a lookup against the known profile set, so an unknown value is a 422 before any work. The filter `q` must be used as a Python-side `in` filter over already-cached data, **never** interpolated into a Paperless query URL. |
| V6 Cryptography | **no** | No crypto beyond `secrets` for token generation. Nothing hand-rolled. |
| V7 Error Handling & Logging | **yes** | The dominant category here. `RequestRejection` messages are developer constants that never interpolate request input or exception text; the new placeholder-token member must follow. D-13 deliberately excludes the log file path from the disclosure — a host filesystem path on a LAN-visible page. The Paperless URL may carry Basic-auth credentials (`paperless.py:461` `_without_userinfo`), so the Paperless check message must use `_display_url` or, better, no URL at all. |
| V9 Communication | **n/a** | Plain HTTP on a trusted LAN, documented; a reverse proxy is the stated answer. |
| V13 API & Web Service | **yes** | `CrossOriginGuard` already covers every non-safe method app-wide (`web/app.py:148`). New POST routes (`/api/checks/refresh`) inherit it automatically — that is the point of the app-wide middleware; do not add a per-route dependency. |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard mitigation | Status here |
|---|---|---|---|
| Reflected XSS via the tag filter `q` echoed into the list | Tampering | Jinja autoescape (on by default for `.html` in `Jinja2Templates`) | Do not echo `q` into the response at all; it is a filter, not a label |
| Token leakage into logs | Information disclosure | Never log the cookie value; log "owner matched/did not match" | New — must be a review item |
| Placeholder token echoed to the page | Information disclosure | The strip says "the token is a placeholder", never the value | D-15's message is a developer constant |
| Paperless URL with `user:pass@` rendered on the strip | Information disclosure | `_display_url` (`paperless.py:461`) | Existing control; reuse it |
| CSRF on the new refresh POST | Spoofing | `CrossOriginGuard`, app-wide | Automatic |
| Cross-origin cookie theft | Spoofing | `SameSite=Lax` + `HttpOnly` + no script file | By construction |
| Path traversal via a profile name in a description route | Tampering | Look the name up in the worker's profile set under the lock (`worker.get_profile`), 422 on miss | Precedent exists at `routes.py:420-423` |
| DoS via unbounded probes | DoS | The lazy thread (D-05) plus the TTL (D-03) bound the probe rate; the pre-probe bounds each one | By design |

---

## Sources

### Primary (HIGH confidence)

- **This repository, read directly** — every `file:line` citation above was read in this
  session: `src/saneless/web/app.py`, `web/routes.py`, `web/cache.py`,
  `web/cross_origin.py`, `web/errors.py`, `worker.py`, `pipeline.py`, `job.py`,
  `config.py`, `cli.py`, `vocabulary.py`, `paperless.py`, `auto_profiles.py`,
  `scanner/sane_backend.py`, `scanner/base.py`, every template under
  `web/templates/`, `web/static/app.css`, `web/static/vendor/htmx-2.0.8.min.js`,
  `pyproject.toml`, `docker-compose.yml`, `saneless.toml.example`, `tests/conftest.py`,
  `tests/test_web.py`, `tests/test_browser.py`, `tests/test_cache.py`,
  `tests/test_app_lifespan.py`, `tests/test_deployment_config.py`, and all of `docs/`.
- **Measured on this machine** — `TZ=America/Chicago … astimezone().strftime(...)` →
  `2026-09-16 14:03 CDT`; `ruff check --select DTZ,PL` on a probe file confirming no DTZ
  diagnostic for `astimezone().strftime` and confirming `PLR0913` counts keyword-only
  parameters; `_STATUS_COL_WIDTH == 16` and the `title_w` table; installed versions of
  ruff/ty/pyrefly/httpx/fastapi/jinja2/tomlkit/python-sane/chromium.
- **Context7 `/bigskysoftware/htmx`** — `www/content/attributes/hx-trigger.md`
  (`keyup changed delay:1s`), `www/content/docs.md` (polling with `every`, HTTP 286),
  `www/content/attributes/hx-sync.md`, `www/content/examples/active-search.md`.
- **Context7 `/encode/httpx`** — `docs/advanced/timeouts.md` (per-request `timeout=`,
  `httpx.Timeout(10.0, connect=60.0)`, the four timeout kinds), `docs/advanced/extensions.md`.
- **Planning documents** — `.planning/phases/30-appliance-layer/30-CONTEXT.md`,
  `.planning/REQUIREMENTS.md:129-140,218-232`,
  `.planning/reviews/2026-09-09-code-review.md:1086-1228`, `.planning/STATE.md`,
  `.planning/config.json`.

### Secondary (MEDIUM confidence)

- The vendored `htmx-2.0.8.min.js` bytes, grepped for `hx-confirm`, `changed`, `delay`,
  `throttle`, `hx-disinherit`, `hx-disabled-elt`, `hx-include`, `286`. Presence of a token
  is strong evidence the feature is compiled in, but is not a semantic guarantee — the
  Context7 docs above supply the semantics.

### Tertiary (LOW confidence — flagged for validation)

- saned's default TCP port (6566) and Linux's default SYN-retry timeout — training
  knowledge, not verified in this session. See Assumptions A1 and A2; A1 has a named
  mitigation.

---

## Metadata

**Confidence breakdown:**

| Area | Level | Reason |
|---|---|---|
| Existing-code facts (line numbers, call graphs, what is wired) | **HIGH** | Every claim read from the file in this session |
| Timestamp rendering and DTZ/PLR0913 lint behaviour | **HIGH** | Executed against this project's own toolchain |
| htmx 2.0.8 capabilities | **HIGH** | Docs (Context7) + the vendored bundle |
| httpx timeout mechanics | **HIGH** | Official docs via Context7 |
| SANE probe bounding | **MEDIUM** | The *absence* of a timeout is verified from the code; the port number and OS timeout figure are assumptions (A1, A2) with a named fallback |
| Cookie behaviour over htmx XHR | **MEDIUM** | Browser-spec reasoning, not executed here (A6); Playwright-assertable in Wave 1 |
| `doctor --json` having no consumer | **MEDIUM** | Exhaustive grep of `docs/`, `tests/`, compose, Dockerfile — a negative claim, so bounded by what was searched |
| Pitfalls | **HIGH** | Each derived from a specific in-tree comment, test assertion, or measured lint result |

**Research date:** 2026-09-16
**Valid until:** 2026-10-16 (30 days — the stack is pinned and vendored; nothing here
moves without a deliberate dependency bump)
