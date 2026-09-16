# Phase 30: Appliance Layer - Context

**Gathered:** 2026-09-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Everything a non-technical household member sees. One shared health check list behind
both `saneless doctor` and a cached status strip on the index page; page counts on every
terminal job; plain-language errors with a suggested next step; human profile labels;
queue position; an owner-only flip prompt; form help text with a thumb-friendly tag
picker; and local timestamps. Requirements APPL-01 through APPL-12, from review §11
(U-01..U-08, M-23, M-30).

**Not in this phase:** the `kdknigga/saneless` rename and the trust-model / "which setup
do I have" docs (Phase 31, DLVR/DOCS), and test-suite hygiene (Phase 32). The
`docker-compose.yml` image reference still says `kris-knigga`; this phase edits the file
for the secret and the consume mount but does **not** fix the name — that is DLVR-01,
which has a CI grep guard behind it.

Each new surface gets its help text and its documentation written in-phase. No new
runtime dependency.

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)
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

### The shared check list (APPL-01, APPL-02)
This area was not selected for discussion, but D-22's amber answer forced its shape, so the
consequences are recorded here rather than left for the planner to guess.
- **D-01: a check has three states — ok / warn / fail — and `doctor` exits non-zero on
  `fail` only.** A `warn` is a true statement about a deployment that still works (D-22's
  read-only config is the motivating case), so it must not fail a scripted health gate.
  Both surfaces read the same three-state result.
- **D-02: one registry of checks, consumed by both `doctor` and the strip.** The five
  checks named by APPL-01 — scanner reachable and named, Paperless URL reachable and token
  accepted, profiles configured, consume-dir fallback configured, data dir writable — are
  defined once. Neither surface may define a check the other does not have; the success
  criterion is that both render the same list. The Paperless check reuses the existing
  `ConnectionStatus` vocabulary and the logic behind `GET /api/paperless/test`
  (`routes.py:231`), which already distinguishes three failure modes.
- `doctor --json` is **undecided** and left to the planner. If it is added, it prints the
  same three-state results; if it is not, the human-readable table plus the exit code is
  the contract. The researcher should say whether anything downstream wants it.

### Status strip: caching, freshness, and the busy scanner (APPL-02)
- **D-03: the strip cache TTL is 30 seconds.** Long enough that a reload and a few htmx
  swaps never re-probe the network; short enough that unplugging the scanner surfaces
  before the operator gives up. The Refresh button covers impatience.
- **D-04: a background refresh thread keeps the cache warm; the page always renders from
  cache.** This is what makes success criterion 2 true — an unplugged scanner host means a
  TCP connect that hangs until the OS gives up, and no page render may ever wait on it.
  Per-check timeouts are still expected as a second line of defence, but they are not the
  mechanism.
- **D-05: the thread is lazy — it refreshes only while someone is watching.** It wakes when
  the index page has been loaded recently and idles otherwise. An appliance nobody is
  looking at must not generate a Paperless HTTP request and a scanner probe every 30
  seconds forever. The "recently loaded" signal is the planner's; keep it to a timestamp,
  not new persisted state.
- **D-06: cold start renders `Checking…` per check plus an htmx poll that swaps in the
  first real results.** Server start is never delayed by a probe. This is a deliberate
  continuation of Phase 26's non-blocking startup.
- **D-07: the thread follows the Phase 26 worker precedent** — daemon thread, a stop
  `Event`, joined with a bounded timeout in the same lifespan shutdown that stops the
  worker. A stuck probe can never block process exit, and a probe in flight during
  shutdown cannot log into a closed handler.
- **D-08: while a scan is active the checks are skipped and the strip shows the last known
  results with a note** — e.g. `Paused during scan — last checked 14:02`. This applies to
  the background thread too: it must not probe the scanner mid-scan. The operator still
  sees token, profiles, fallback and data-dir state, which never needed the scanner.
- **D-09: Refresh bypasses the cache and re-probes, resetting the TTL. During a scan it
  re-runs only the checks that do not touch the scanner**, consistent with D-08. An
  explicit click does not get to defeat the exclusive-scanner rule.

### Plain-language errors (APPL-04)
- **D-10: one function returning a frozen pair.** `error_advice(category)` returns a frozen
  dataclass with `.message` and `.next_step`, built with a single `match` and
  `assert_never`, so a new `ErrorCategory` member cannot be added without a next step. The
  existing `error_message` becomes a thin accessor or is folded in; either way there is one
  match statement, not two that can drift.
- **D-11: next steps are keyed off `ErrorCategory` only** — seven of them, exactly as
  APPL-04 says. No per-exception refinement: Phase 26 D-10 locked "named, never
  interpreted", and category-derived advice is what the review asked for. Sharper detail
  lives in the disclosure, not in a new classification layer.
- **D-12: the CLI keeps today's line and gains a second one.** Phase 28 D-08's shape is
  printed unchanged, then the next step is printed beneath it (e.g. a `Try:` line). Phase
  28's doc-truth exit-code and message-shape tests keep passing, scripts parsing the first
  line are unaffected, and a human gets the advice.
- **D-13: the web `Technical details` disclosure holds the specific message, the error
  category, and the job id.** All three are already on the `Job` row. The log file path is
  deliberately **not** included — it is a host filesystem path on a LAN-visible page. The
  disclosure is collapsed by default (`<details>`), and the plain sentence plus next step
  are what the reader sees first.
- `job.error` keeps storing the specific message. Nothing about storage changes; this is a
  rendering decision.

### Placeholder token and scan refusal (APPL-07)
- **D-14: a placeholder is blank/whitespace, or a member of a small fixed literal set.**
  That set includes `changeme` (shipped today at `docker-compose.yml:30`), whatever
  `saneless.toml.example` carries, and the obvious `your-token-here` family. Explicit and
  testable. No shape heuristic — refusing a legitimate token from a future Paperless
  version is worse than missing an exotic placeholder.
- **D-15: the web layer refuses with a new `RequestRejection` member plus a disabled Scan
  button.** The new member sits alongside `WORKER_DOWN` with its own message and status
  code and writes a `REJECTED` job row (Phase 26 D-05), so the attempt appears in history.
  The server-owned Scan button (ROBU-04) additionally renders disabled with the reason.
  The route guard is the enforcement; the button is the courtesy. `WORKER_DEGRADED` is
  **not** reused — saying "the scan service was unavailable" when the truth is "nobody set
  the token" is exactly the untruthfulness this milestone removes.
- **D-16: `saneless scan` refuses with exit 2**, checked before the scanner is opened so no
  paper moves for an upload that cannot succeed. `devices`, `auto-profiles` and `jobs` are
  unaffected — they never touch Paperless. The refusal is unconditional: a configured
  consume-directory fallback does **not** soften it, so doctor, the web UI and `scan` all
  agree on whether the appliance can work.
- **D-17: `./config/config.toml` is the one place for the secret.** The compose template
  ships its `environment:` block **fully commented out**, with a note that uncommenting it
  overrides the file. This matches the read-write `./config:/etc/saneless` mount Phase 27
  D-09 locked, and it removes the silent override the review found in U-01.
- APPL-11's consume-directory mount is added to the same compose example with its two-line
  explanation, and the fallback check reports
  `Fallback: not configured; scans cannot be kept if Paperless is down` when absent. That
  is a `warn`, not a `fail` (D-01).

### Profile labels, descriptions, and ordering (APPL-05, APPL-06)
- **D-18: `label` and `description` are persisted, tool-owned keys.** They join Phase 27
  D-02's owned key set and behave exactly like `source` / `mode` / `resolution`:
  `--force` overwrites them in place, and D-03's "an owned key a fresh generation does not
  write is deleted" applies. One consistent rule, no special case. The operator's escape
  hatch is the documented one — remove `auto_generated` to take the profile over — and the
  how-to guide must say so.
- **D-19: generated text comes from what the code already knows.** `_duplex()`
  (`auto_profiles.py:305`) and `_auto_source_mode()` already classify the source; their
  docstrings name APPL-05 as their eventual reader. "Feeder, single-sided" / "Feeder,
  double-sided" / "Glass (flatbed)" are derived from `SourceKind` and `duplex`, not from a
  new probe.
- **D-20: the form keeps the `<select>` and gains a live description beneath it**, wired
  with `aria-describedby` and swapped by htmx on change. Keeps the existing control and
  markup; one small route. Not a radio group — that is a larger form rewrite and grows
  unbounded with profile count.
- **D-21: "sheet-fed" means the device reports no flatbed source.** That is exactly the
  `has_flatbed` fact `_auto_source_mode` already receives. No new config key and no new
  probing; it is the literal definition. Feeder profiles sort first when it is true.
- **D-22: a read-only config location is an amber `warn` row, not a red one.** Wording
  along the lines of `Profiles: generated in memory — the config location is read-only, so
  they are lost on restart.` Scanning works, so it must not be red and must not fail
  `doctor`'s exit code. It is its own row, not a caveat buried in the profiles check.

### Queue position and the owner-only flip prompt (APPL-08, APPL-09)
- **D-23: the owner token is a session cookie, one per browser.** `HttpOnly`,
  `SameSite=Lax`, no `Max-Age` — it dies with the browser. Minted on the first scan submit
  and reused for every later job from that browser, so two tabs on one device do not disown
  each other. Nothing durable is written to a device on a LAN the project treats as shared.
  Each job row records the token in the existing `owner_token` column.
- **D-24: the token gates the flip buttons and nothing else.** Everyone sees the same
  status area — state, title, page counts, errors. Only Continue/Abort are owner-only;
  other viewers see `Waiting for the stack to be flipped`. This is a footgun guard, not an
  auth mechanism (REQUIREMENTS says so explicitly).
- **D-25: the status area follows the job the browser submitted.** The `POST /api/scan`
  swap and its polls track that job id, so a queued submitter sees
  `Waiting for '<title>' to finish (N ahead of you)` from `list_pending()`
  (`job.py:933`), then normal progress once it starts. The status area currently *infers*
  a job via `_current_or_recent_job` (`routes.py:91`); it gains an explicit followed job
  id. A browser that submitted nothing still sees the current/most-recent job as today.
- **D-26: when the owner's browser is gone, the Phase 25 flip timeout resolves it — there
  is no escape hatch.** A human who closed the tab is the same case as a human who walked
  away, which the bounded `FlipCoordinator` timeout already handles. No second timer, no
  state in which the guard silently stops guarding.
- **D-27: Abort confirms with htmx's `hx-confirm` attribute.** One declarative attribute on
  the existing button — no script file (there is none), no extra route, no new state. The
  native dialog is unstyled but fully accessible.

### Form help text and the simpler form (APPL-10)
- **D-28: hiding Tags and Correspondent is a config key**, e.g. `[web] show_tags` /
  `show_correspondent`. One appliance, one configured form shape — the household member
  never sees a control the owner turned off. Fits the strict-config model and is testable
  without a browser. Not a per-browser toggle.
- **D-29: profile `default_tags` and `default_correspondent` still apply when the controls
  are hidden.** Hiding a control changes the form, not the scan — "press Scan and it is
  filed as Receipts" is the whole point of the simple form. This mirrors how a blank title
  already falls back to the profile title (`resolve_job_title`).
- **D-30: the checkbox list replaces the multi-select everywhere**, not responsively. One
  control, one partial, one set of tests; two same-named controls in one form is a
  footgun. Touch targets must be at least 44×44px.
- **D-31: a filter box handles large tag lists**, with `hx-get="/api/tags?q=…"` on a
  debounced keyup swapping the list server-side. No script file, reuses the existing route
  and its `MetadataCache`, and scales past the 50+ tags a real Paperless install carries.
- Each form control gets one line of help text in plain words ("Who sent this document?
  Optional."), as APPL-10 specifies.

### Page counts and local timestamps (APPL-03, APPL-12)
- **D-32: counts appear as a sentence in the status area and as detail in the history
  table** — e.g. `12 pages scanned, 2 blank removed, 10 uploaded` under the Done/Fallback
  line. A `NULL` count renders **nothing at all**; it must never render `0`, which would be
  a lie about a job recorded before Phase 23.
- **D-33: manual duplex shows the front count and a live back count during pass B** —
  `Front: 12 pages · Scanning backs…` during `SCANNING_REVERSE`. This requires pass A's
  count to be readable mid-job, not only at the end; the mechanism is the planner's.
- **D-34: timestamps render as `2026-09-16 14:03 CDT`** — `%Z` abbreviation in the server's
  local zone. Ambiguous across continents in theory; this is one appliance on one LAN.
- **D-35: the zone name appears on every timestamp**, not once as a column caption. A
  copy-pasted line is then self-describing. Note the cost: it eats width in both the web
  history table and the CLI table, whose truncation logic (`cli.py:208`) already competes
  for columns — the planner must check the CLI table still fits at a normal terminal width.
- Conversion happens in one shared place so the web filter and the CLI table cannot
  disagree about the zone or the format.

### Claude's Discretion
- **The whole shared-check-list area** (D-01, D-02) — not selected for discussion. The
  three-state model is forced by D-22's amber answer; the registry shape and what each
  check probes are the planner's, within D-02's "defined once, both surfaces read it" rule.
- **`doctor --json`** — undecided, flagged for the researcher.
- Per-check timeout values behind the background thread (D-04).
- How "someone is watching" is detected for the lazy thread (D-05) — keep it to a
  timestamp, not persisted state.
- Whether `error_message` survives as an accessor or is folded into `error_advice` (D-10).
- How pass A's count is made readable mid-job (D-33).
- Whether the history table gets an extra column or an expandable row for counts (D-32).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's source of truth
- `.planning/reviews/2026-09-09-code-review.md` §11 (lines 1086-1228) — "Usability review:
  the appliance test". U-01..U-10 in full, with the reviewer's reasoning. Every APPL
  requirement traces here.
- `.planning/reviews/2026-09-09-code-review.md` §10 (lines 1060-1085) — remediation order;
  step 11 is "Make it an appliance" and states the prerequisites this phase inherits.
- `.planning/REQUIREMENTS.md` — APPL-01..APPL-12 (lines 129-140), plus the Out of Scope
  table (lines 218-232), which binds: no `HEALTHCHECK` calling doctor, no refuse-to-boot on
  a placeholder token, no new runtime dependency, no htmx 4, and the owner token is a
  footgun guard and not authentication.
- `.planning/ROADMAP.md` — Phase 30 goal and its five success criteria.
- `.planning/research/FEATURES.md` — cited by REQUIREMENTS for the "start red and refuse
  scans" decision.

### Decisions this phase builds directly on
- `.planning/phases/26-worker-and-web-robustness/26-CONTEXT.md` — D-01/D-04/D-05/D-08/D-10
  and D-16..D-18: the single error renderer, refused-submit job rows, the worker stop/join
  precedent, named-not-interpreted exceptions, and in-memory profiles on an unwritable
  config.
- `.planning/phases/26-worker-and-web-robustness/26-UI-SPEC.md` — the server-owned Scan
  button, out-of-band swaps, and the vendored-asset / no-client-script constraint.
- `.planning/phases/27-configuration-strictness/27-CONTEXT.md` — D-02/D-03 (tool-owned
  profile keys and `--force` merge semantics, which D-18 extends) and D-08/D-09 (the
  read-write `./config:/etc/saneless` mount and the EBUSY failure, which D-17 and D-22
  depend on).
- `.planning/phases/28-exception-translation/28-CONTEXT.md` — D-01..D-08: `CANCELLED`, the
  exit-code table, the CLI failure-line shape that D-12 must not break, and the
  `ErrorCategory`/`PdfError` classification D-11 keys off.
- `.planning/phases/25-manual-duplex/25-CONTEXT.md` — the `FlipCoordinator` contract, the
  bounded timeout D-26 relies on, and `SCANNING_REVERSE`.
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md` — where
  `pages_scanned` / `pages_removed` / `pages_uploaded` came from and what each counts.
- `.planning/phases/23.1-dark-mode-and-the-commit-gate/23.1-UI-SPEC.md` — the status-token
  colour convention and the WCAG AA requirement in both schemes. Every new status colour
  in this phase (amber `warn` especially) must follow it.

### Docs that must be corrected in-phase
- `docs/reference/cli-commands.md` — gains `saneless doctor`.
- `docs/how-to/cli-scripting.md` — the exit-code table; `doctor`'s exit semantics (D-01).
- `docs/how-to/deploy-docker-compose.md` and `docs/reference/docker.md` — the secret's one
  place (D-17) and the consume-directory mount (APPL-11).
- `docs/how-to/configure-scan-profiles.md` — `label` / `description` as tool-owned keys and
  the "remove `auto_generated` to take it over" escape hatch (D-18).
- `docs/reference/configuration.md` — the new `[web]` keys (D-28).
- `docs/reference/web-api.md` — any new route (strip refresh, profile description, tag
  filter) and the new `RequestRejection` member (D-15).
- `docker-compose.yml` — D-17 and APPL-11. **Do not** fix the `kris-knigga` image
  reference here; that is DLVR-01 in Phase 31.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `vocabulary.py:476` `error_message(category)` — the plain-language map, written and
  tested but deliberately unwired, waiting for this phase.
- `vocabulary.py:165` `ConnectionStatus` + `connection_status_message` and
  `routes.py:231` `GET /api/paperless/test` — the Paperless check already exists in three
  distinct failure modes; the check list reuses it rather than re-probing differently.
- `job.py:501` `owner_token` column with no writer (`job.py:710` comment) — APPL-09's
  storage is already migrated in.
- `job.py:497-499` `pages_scanned` / `pages_removed` / `pages_uploaded` — APPL-03's data is
  already recorded and currently rendered nowhere (`grep` over `web/` finds zero hits).
- `job.py:933` `JobStore.list_pending()` — APPL-08's "N ahead of you" source.
- `web/cache.py` `MetadataCache` — monotonic-clock TTL with `get_or_fetch` that keeps a
  stale entry when a fetch fails (WR-05). The check-list cache follows this pattern; the
  class itself is typed to `list[dict[str, object]]` so it is a pattern, not a drop-in.
- `auto_profiles.py:305` `_duplex()` and `:280` `_auto_source_mode()` — the source
  classification D-19 and D-21 read. Both docstrings already name APPL-05.
- `auto_profiles.py:590` `_generated_values()` and `_OWNED_KEYS` — where D-18 adds
  `label` / `description`.
- `cli.py:208` `_truncate()` — terminal-aware column widths the CLI table already uses;
  D-35's zone suffix competes with it.

### Established Patterns
- **Server-rendered Jinja partials under `web/templates/partials/`** — `status.html`,
  `error.html`, `flip.html`, `history.html`, `scan_button.html`, `status_response.html`.
  New surfaces are new partials, swapped by htmx; `status_response.html` is the
  out-of-band wrapper.
- **`match` + `assert_never` over StrEnums in `vocabulary.py`** with a completeness test —
  how this codebase makes the type checkers find every consumer. D-10 keeps it.
- **No client script.** `app.js` was deleted in Phase 26; only `app.css` and vendored
  `htmx-2.0.8.min.js` / `pico-2.1.1.min.css` ship. Every interaction is an htmx attribute.
- **Developer-constant user-facing strings.** `RequestRejection` messages and
  `error_message` never interpolate request input or exception text (V7). D-15's new
  member and D-10's next steps follow the same rule.
- **`extra="forbid"` on `ProfileConfig`** — adding `label`/`description` is a real schema
  change, and an old config without them must still load.

### Integration Points
- `web/routes.py:176` `index()` — where the status strip renders.
- `web/routes.py:133` `_status_context()` and `:91` `_current_or_recent_job()` — D-25's
  explicit followed-job id lands here.
- `web/routes.py:371` `start_scan()` — where the owner cookie is minted (D-23) and where
  the placeholder-token refusal guards (D-15).
- `web/routes.py:565`/`:598` `continue_flip` / `abort_flip` — where owner-token checking
  goes (D-24); both already scope answers by `job_id` per CR-01.
- `web/app.py` lifespan — where the refresh thread starts and is joined (D-07), beside the
  worker.
- `cli.py` `_GuardedGroup` — where `doctor` is registered as a sixth command alongside
  `scan` / `devices` / `jobs` / `serve` / `auto-profiles`.
- `templates/index.html` — the profile select (D-20), the tags select becoming a checkbox
  list with a filter (D-30, D-31), help text (APPL-10), and the strip's placement.

</code_context>

<specifics>
## Specific Ideas

- Status strip while scanning: `Paused during scan — last checked 14:02`.
- Read-only config row: `Profiles: generated in memory — the config location is read-only,
  so they are lost on restart.`
- Missing fallback row: `Fallback: not configured; scans cannot be kept if Paperless is
  down` (a `warn`, not a `fail`).
- Queue position: `Waiting for 'Tax return' to finish (1 ahead of you)`.
- Non-owner at the flip prompt: `Waiting for the stack to be flipped`.
- Manual duplex pass B: `Front: 12 pages · Scanning backs…`.
- Terminal job counts: `12 pages scanned, 2 blank removed, 10 uploaded`.
- Profile labels: `Feeder, single-sided` / `Feeder, double-sided` / `Glass (flatbed)`.
- Help text: `Who sent this document? Optional.`
- Timestamp: `2026-09-16 14:03 CDT`.
- CLI error output keeps Phase 28's first line and adds a second, e.g. a `Try:` line.

</specifics>

<deferred>
## Deferred Ideas

- **`doctor --json`** — not deferred so much as undecided (see Claude's Discretion). If the
  researcher finds no consumer, it does not ship.
- **Per-browser "simpler form" toggle** — rejected in favour of the config key (D-28). If
  a household ever wants per-device form shapes, it is a small follow-up, not this phase.
- **Persistent owner cookie for a wall-mounted tablet** — rejected (D-23) because it writes
  a durable identifier to a shared-LAN device. Revisit only if a real deployment asks.
- **"Most-used tags first" ranking** — rejected for the tag picker (D-31); it would require
  usage state saneless does not keep.
- **Per-exception next steps** — rejected (D-11) as re-interpreting exceptions. If seven
  category-level next steps prove too coarse in practice, that is a later refinement.
- **Mobile-optimised layout beyond the tag picker and help text** — already `UIX-01` in
  REQUIREMENTS' future list, explicitly out of this phase.
- **Fixing the `kris-knigga` image reference in `docker-compose.yml`** — Phase 31, DLVR-01,
  which has a CI grep guard. This phase edits the same file without touching that line.

</deferred>

---

*Phase: 30-appliance-layer*
*Context gathered: 2026-09-16*
