# Phase 30: Appliance Layer - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-16
**Phase:** 30-appliance-layer
**Areas discussed:** Status strip caching and busy skip, Plain-language errors, Placeholder token and refusal, Profile labels and ordering, Queue position and owner token, Help text and simpler form, Page counts and timestamps

**Areas offered but not selected:** Shared check list shape (doctor/strip registry, `--json`, per-check probes)

---

## Status strip caching and busy skip

### Strip cache TTL

| Option | Description | Selected |
|--------|-------------|----------|
| 30s | Survives a reload and a few htmx swaps; Refresh covers impatience | ✓ |
| 5s | Common health-report default; re-probes on nearly every page load | |
| 120s | Cheapest; a recovered scanner stays red for two minutes | |
| You decide | | |

**User's choice:** 30s

### What the strip shows while a scan is active

| Option | Description | Selected |
|--------|-------------|----------|
| Last known results + a note | Cached checks with "Paused during scan — last checked HH:MM" | ✓ |
| Skip only scanner-touching checks | Everything else runs live; scanner row says "Not checked" | |
| Hide the strip entirely | One "Checks paused while scanning" line | |
| You decide | | |

**User's choice:** Last known results + a note

### How the strip stays fast with the scanner host unplugged

| Option | Description | Selected |
|--------|-------------|----------|
| Per-check timeout + whole-strip budget | Synchronous, no threads, no new state | |
| Background refresh thread | Page always renders from cache; adds a second long-lived thread | ✓ |
| Probe on the strip's own htmx request | Index renders instantly with a spinner; strip flashes empty each load | |
| You decide | | |

**User's choice:** Background refresh thread
**Notes:** Chosen against the recommendation. Drove three follow-up questions below, since a long-lived thread needs a lifecycle, a cold-start story, and a shutdown path.

### Refresh button behaviour

| Option | Description | Selected |
|--------|-------------|----------|
| Bypass cache; refuse during a scan | Force re-probe; during a scan only non-scanner checks re-run | ✓ |
| Bypass cache unconditionally | Operator intent overrides the busy skip | |
| Re-render from cache | Only meaningful with a background thread | |
| You decide | | |

**User's choice:** Bypass cache; refuse during a scan

### When the refresh thread runs (follow-up)

| Option | Description | Selected |
|--------|-------------|----------|
| Continuous from startup | Always warm; probes every 30s even with nobody watching | |
| Lazy — only while someone is watching | Idles when the page has not been loaded recently | ✓ |
| Continuous with idle backoff | 30s when watched, minutes when idle; most moving parts | |
| You decide | | |

**User's choice:** Lazy — only while someone is watching
**Notes:** Chosen against the recommendation. Rationale fits an appliance that sits unattended most of the day.

### Cold start before the first pass completes (follow-up)

| Option | Description | Selected |
|--------|-------------|----------|
| "Checking…" + htmx poll | Server start never delayed | ✓ |
| Synchronous first pass at startup | Always truthful, but delays startup by the timeout budget | |
| "Unknown" until the next natural load | No poll, no route, nothing useful until reload | |
| You decide | | |

**User's choice:** "Checking…" + htmx poll

### Thread shutdown (follow-up)

| Option | Description | Selected |
|--------|-------------|----------|
| Follow the Phase 26 worker precedent | Daemon, stop Event, bounded join in lifespan shutdown | ✓ |
| Daemon, no join | Less code; a probe mid-shutdown can log into a closed handler | |
| You decide | | |

**User's choice:** Follow the Phase 26 worker precedent

---

## Plain-language errors

### Shape of the next-step text in vocabulary.py

| Option | Description | Selected |
|--------|-------------|----------|
| One function → frozen pair | `error_advice(category)` → dataclass with `.message` / `.next_step`; one match, one assert_never | ✓ |
| Second parallel function | Mirrors state_label/progress_label; two match statements can drift | |
| Module-level frozen mapping | Data not code; loses assert_never exhaustiveness | |
| You decide | | |

**User's choice:** One function → frozen pair

### Granularity of the next step

| Option | Description | Selected |
|--------|-------------|----------|
| Category only | Seven next steps keyed off ErrorCategory, as APPL-04 says | ✓ |
| Category + optional exception override | Sharper advice, but reintroduces interpreting exceptions (Phase 26 D-10) | |
| You decide | | |

**User's choice:** Category only

### Does the CLI print the next step

| Option | Description | Selected |
|--------|-------------|----------|
| Add a second "Try:" line | Phase 28's line unchanged; advice beneath it | ✓ |
| Web only | Smallest blast radius on Phase 28's doc-truth tests | |
| Replace the CLI line with the plain sentence | The exact regression error_message's docstring warns about | |
| You decide | | |

**User's choice:** Add a second "Try:" line

### Contents of the "Technical details" disclosure

| Option | Description | Selected |
|--------|-------------|----------|
| Specific message + category + job id | All three already on the Job row | ✓ |
| Specific message only | What the status partial renders today, now collapsed | |
| + log file path | Most actionable, but puts a host path on a LAN-visible page | |
| You decide | | |

**User's choice:** Specific message + category + job id

---

## Placeholder token and refusal

### What counts as a placeholder

| Option | Description | Selected |
|--------|-------------|----------|
| Empty + a fixed literal list | Blank/whitespace plus "changeme", the example value, "your-token-here" | ✓ |
| Empty or whitespace only | Narrowest; misses the exact footgun the review found | |
| Literal list + shape heuristic | Also flags non-40-char-hex; risks refusing a future legitimate token | |
| You decide | | |

**User's choice:** Empty + a fixed literal list

### How the web layer refuses

| Option | Description | Selected |
|--------|-------------|----------|
| New RequestRejection + disabled button | REJECTED job row per Phase 26 D-05; server-owned button shows the reason | ✓ |
| Route guard only | Fewer template changes; leaves a button that can never work | |
| Reuse WORKER_DEGRADED | No new vocabulary, but the message would be untrue | |
| You decide | | |

**User's choice:** New RequestRejection + disabled button

### Does the CLI refuse

| Option | Description | Selected |
|--------|-------------|----------|
| `saneless scan` refuses, exit 2 | Checked before the scanner opens; devices/auto-profiles/jobs unaffected | ✓ |
| Refuse unless a fallback dir is configured | More forgiving; makes "refuses scans" conditional | |
| Warn only, never refuse | doctor and the web UI would disagree with scan | |
| You decide | | |

**User's choice:** `saneless scan` refuses, exit 2

### Where the secret lives in the compose template

| Option | Description | Selected |
|--------|-------------|----------|
| config.toml; env block commented out | Matches the read-write ./config mount from Phase 27 D-09 | ✓ |
| .env file via env_file: | Standard Docker practice; splits config across two files | |
| Env var interpolated from the shell | Nothing secret committed; most confusing for a household operator | |
| You decide | | |

**User's choice:** config.toml; env block commented out

---

## Profile labels and ordering

### Where label/description live and who owns them

| Option | Description | Selected |
|--------|-------------|----------|
| Persisted, tool-owned, --force overwrites | Joins Phase 27 D-02's owned key set; escape hatch is removing auto_generated | ✓ |
| Persisted, never overwritten once written | Treats a label as human prose; the only exception to D-02/D-03 | |
| Derived at read time, never written | No merge semantics at all; operator can never rename a profile | |
| You decide | | |

**User's choice:** Persisted, tool-owned, --force overwrites

### How the dropdown shows a description

| Option | Description | Selected |
|--------|-------------|----------|
| Select + live description below | aria-describedby, swapped by htmx on change | ✓ |
| Radio group fieldset | All descriptions visible at once; bigger form rewrite | |
| optgroup headings only | Zero JS; delivers ordering but arguably not descriptions | |
| You decide | | |

**User's choice:** Select + live description below

### The "sheet-fed" signal for feeder-first ordering

| Option | Description | Selected |
|--------|-------------|----------|
| No flatbed source on the device | The has_flatbed fact `_auto_source_mode` already uses | ✓ |
| Any profile has auto_source_mode = "adf" | Works with no device present; wrong for flatbed+feeder machines | |
| An explicit config key | Most controllable; a new key for something the device knows | |
| You decide | | |

**User's choice:** No flatbed source on the device

### APPL-06 read-only config: how loud

| Option | Description | Selected |
|--------|-------------|----------|
| Amber informational row | Scanning works, so not red and does not fail doctor | ✓ |
| Red check, doctor exits non-zero | Would fail a deliberately read-only deployment that works | |
| Fold into the existing profiles check | Fewest rows; easier to miss | |
| You decide | | |

**User's choice:** Amber informational row
**Notes:** This answer forced the three-state check model recorded as D-01, inside the one area not selected for discussion.

---

## Queue position and owner token

### Owner cookie lifetime

| Option | Description | Selected |
|--------|-------------|----------|
| Session cookie, one token per browser | Dies with the browser; two tabs stay one owner | ✓ |
| Session cookie, fresh token per submit | Tightest scoping; two tabs disown each other | |
| Persistent, long Max-Age | Suits a wall-mounted tablet; writes a durable identifier to a shared-LAN device | |
| You decide | | |

**User's choice:** Session cookie, one token per browser

### How much the token gates

| Option | Description | Selected |
|--------|-------------|----------|
| Gate the flip buttons only | Everyone sees the same status area | ✓ |
| Gate buttons and follow the viewer's own job | Serves APPL-08 directly; makes the page per-viewer | |
| Gate the whole status area | Wrong for an appliance anyone can walk up to | |
| You decide | | |

**User's choice:** Gate the flip buttons only

### When the owner's browser is gone

| Option | Description | Selected |
|--------|-------------|----------|
| Flip timeout resolves it, nothing else | Same case as a human who walked away; Phase 25 already handles it | ✓ |
| Any browser may answer after a grace period | Faster recovery; the guard silently stops guarding | |
| Any browser may Abort, only the owner may Continue | Asymmetric but defensible; more branches | |
| You decide | | |

**User's choice:** Flip timeout resolves it, nothing else

### Abort confirmation without app.js

| Option | Description | Selected |
|--------|-------------|----------|
| hx-confirm attribute | One declarative attribute; native dialog, unstyled but accessible | ✓ |
| Two-step server swap | Fully styled; new route and partial for one button | |
| Inline `<details>` disclosure | Cheapest; a disclosure is not a confirmation | |
| You decide | | |

**User's choice:** hx-confirm attribute

### What a queued submitter sees (follow-up)

| Option | Description | Selected |
|--------|-------------|----------|
| Its own pending job, then hand off | Status area follows the submitted job id; "N ahead of you" from list_pending() | ✓ |
| Running job + a queued line | No per-viewer state; never says "N ahead of you" to the person waiting | |
| Own job via the owner cookie | Same effect, but makes the page per-viewer | |
| You decide | | |

**User's choice:** Its own pending job, then hand off
**Notes:** Asked because "gate the flip buttons only" left APPL-08's "N ahead of you" without a surface to render on.

---

## Help text and simpler form

### How the operator hides Tags and Correspondent

| Option | Description | Selected |
|--------|-------------|----------|
| Config key | One appliance, one form shape; testable without a browser | ✓ |
| Per-browser toggle in a cookie | Phone gets the simple form; new per-viewer state | |
| Both | Most flexible; two sources of truth for form shape | |
| You decide | | |

**User's choice:** Config key

### Do profile defaults still apply when hidden

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, always | Hiding a control changes the form, not the scan | ✓ |
| No — hidden means no metadata | Makes the simple form strictly worse than the full one | |
| You decide | | |

**User's choice:** Yes, always

### Checkbox list scope

| Option | Description | Selected |
|--------|-------------|----------|
| Replace everywhere | One control, one partial, one set of tests | ✓ |
| Responsive — select on wide, checkboxes on narrow | Two same-named controls in one form is a footgun | |
| Keep the select, enlarge touch targets | Native multi-select on a phone is the complaint APPL-10 exists to fix | |
| You decide | | |

**User's choice:** Replace everywhere

### Handling 50+ tags

| Option | Description | Selected |
|--------|-------------|----------|
| Filter box, htmx-driven | hx-get="/api/tags?q=…" on debounced keyup; reuses route and cache | ✓ |
| Scrollable capped-height list | Zero new routes; still scrolling, awkward on touch | |
| Selected + most-used first | Requires usage ranking saneless does not keep | |
| You decide | | |

**User's choice:** Filter box, htmx-driven

---

## Page counts and timestamps

### Timestamp format

| Option | Description | Selected |
|--------|-------------|----------|
| `2026-09-16 14:03 CDT` | %Z abbreviation; what a person reads on a clock | ✓ |
| `2026-09-16 14:03 (America/Chicago)` | Unambiguous IANA name; verbose in a table cell | |
| `2026-09-16 14:03 -0500` | Always derivable; least human-readable | |
| You decide | | |

**User's choice:** `2026-09-16 14:03 CDT`

### Where the zone name appears

| Option | Description | Selected |
|--------|-------------|----------|
| Once, near the table | Keeps 20 history rows readable; one place to get right | |
| On every timestamp | A copy-pasted line is self-describing; eats table width | ✓ |
| You decide | | |

**User's choice:** On every timestamp
**Notes:** Chosen against the recommendation. CONTEXT.md records the cost: the CLI table's truncation logic (`cli.py:208`) now competes with the zone suffix and must be checked at a normal terminal width.

### Where page counts appear

| Option | Description | Selected |
|--------|-------------|----------|
| Status area sentence + history detail | NULL renders nothing, never "0" | ✓ |
| Status area only | APPL-03 names the history table explicitly | |
| Dedicated "Pages" column ("12/2/10") | Compact but cryptic for the intended reader | |
| You decide | | |

**User's choice:** Status area sentence + history detail

### Manual duplex during pass B

| Option | Description | Selected |
|--------|-------------|----------|
| Front count + live back count | Needs pass A's count readable mid-job | ✓ |
| Front count only | Reassures without a per-page counter in the reverse pass | |
| Both counts only when the job ends | Contradicts roadmap success criterion 3 | |
| You decide | | |

**User's choice:** Front count + live back count

---

## Claude's Discretion

The user chose "Other"/"You decide" on nothing — every presented question got an explicit
selection. Discretion below is from areas that were never asked, or details deliberately
left open:

- **The shared check list (D-01, D-02)** — offered as a gray area, not selected. The
  three-state ok/warn/fail model and the "defined once, both surfaces read it" registry
  rule were derived from the amber answer in the profile-labels area and recorded as
  decisions so the planner does not re-derive them.
- **`doctor --json`** — left undecided; flagged for the researcher to say whether anything
  downstream wants it.
- **Per-check timeout values** behind the background refresh thread.
- **How "someone is watching" is detected** for the lazy thread — constrained to a
  timestamp, not persisted state.
- **Whether `error_message` survives as an accessor** or is folded into `error_advice`.
- **How pass A's count is made readable mid-job.**
- **Whether history gains an extra column or an expandable row** for page counts.

## Deferred Ideas

- Per-browser "simpler form" toggle — rejected in favour of the config key.
- Persistent owner cookie for a wall-mounted tablet — rejected as durable identity on a
  shared LAN.
- "Most-used tags first" ranking in the tag picker — would require usage state saneless
  does not keep.
- Per-exception next steps — rejected as re-interpreting exceptions; revisit only if seven
  category-level next steps prove too coarse.
- Mobile-optimised layout beyond the tag picker and help text — already `UIX-01` in
  REQUIREMENTS' future list.
- Fixing the `kris-knigga` image reference in `docker-compose.yml` — Phase 31 (DLVR-01),
  even though this phase edits the same file.
