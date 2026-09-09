# Feature Research

**Domain:** Self-hosted "appliance" bridge between a shared physical device (SANE scanner) and a document management system (paperless-ngx), used on a home LAN by a mix of technical operators and non-technical family members.
**Researched:** 2026-09-09
**Confidence:** MEDIUM-HIGH (peer behaviour verified against official docs and release notes where possible; a few observations come from vendor support articles and video walkthroughs and are marked LOW)

**Scope note.** This milestone hardens an existing product. Everything in `.planning/PROJECT.md` "Validated" is out of scope for re-research. This file covers only the new user-facing capabilities demanded by review section 11 (`U-01`..`U-10`) and the user-visible half of the CRITICAL fixes (`C-01`..`C-05`, `C-09`) plus `M-07`, `M-23`. Every row ties back to a finding ID in `.planning/reviews/2026-09-09-code-review.md`.

---

## What the ecosystem actually does

Peer behaviour, gathered so the categories below are evidence-driven rather than asserted.

| Tool | Setup status / doctor | Job outcome reporting | Queue visibility | Friendly errors | Profile labels |
|---|---|---|---|---|---|
| **paperless-ngx** | `GET /api/status/` + a **System Status** dialog: per-subsystem OK/error for database (incl. unapplied migrations), redis, celery, search index, classifier, sanity check, plus storage total/available, version, install type, OS. Users paste the JSON into support threads. v3 added `view_system_status` permission and a task summary inside status. | Dedicated **Tasks / File Tasks** view listing started / queued / failed tasks with the failure text attached to each. | Task list shows queued vs started; no per-user position. | Partial: the task row carries paperless's own `result` text (often a raw traceback); v3 shipped "localize some more task result messages". | Not applicable (no scan profiles). |
| **Nextcloud** | **Administration → Overview → "Security & setup warnings"**: a built-in configuration checker whose findings render at the top of the admin page, each with a documentation link that explains the fix. | n/a | n/a | Each warning is a plain-language sentence plus a "how to fix" doc page. | n/a |
| **Home Assistant** | **Repairs / issue registry**: issues carry a severity (`CRITICAL`/`ERROR`/`WARNING`), a translation key for human text, and either `is_fixable=True` with a guided repair flow or a `learn_more_url`. Issues persist until fixed. | n/a | n/a | This *is* the friendly-error model: never a traceback, always "what broke + what to do". | n/a |
| **Immich** | Container healthchecks + `immich-admin` CLI (admin operations, `schema-check`); a getting-started wizard on first load. No unified "is my setup right" screen. | Per-job-type counters. | **Administration → Jobs** shows active/waiting counts per queue. An open feature request asks for per-job progress and ETA — i.e. queue *position* is valued and still missing. | Mixed; server errors surface as toasts ("Failed to get queues"). | n/a |
| **scanservjs** (closest analogue: SANE web UI, Docker, home LAN) | **None.** Only an About page with system info and `/api-docs`. Its own docs concede scanner detection is "the most precarious" step and hand the user off to a troubleshooting page. | Files land in a list; a known issue report describes a 33-page batch dying at assembly with the pages only recoverable from `data/temp`. | Single scanner, no queue UI. | Not systematised. | Scanner options are exposed raw (source/resolution/mode) with config-file "device overrides" for scanners that mis-report. No named human profiles. |
| **NAPS2** | `--listdevices`; no doctor. | Page thumbnails accumulate in the workspace; the user sees the pages. | n/a (desktop, single user). | n/a | **Profiles are first class with a user-set "Display Name"**, and every third-party setup guide instructs "change Display Name to something descriptive". Source is labelled **"Glass"** vs **"Feeder"** in plain words. CLI selects by profile name: `naps2.console -p "Canon MP495 (color)"`. |
| **SANE CLI** (`scanimage`, `scanadf`) | `scanimage -L`. | `-p` prints scan progress percentage. | n/a | Raw backend errors. | n/a |
| **sane-scan-pdf** | n/a | Blank-page removal with a tunable threshold; per-page progress messages. | n/a | n/a | n/a |
| **Mealie / Kavita** | First-run wizard; Mealie keeps showing a warning while the shipped default credentials are still in use (LOW confidence — from a walkthrough video, not docs). | n/a | n/a | n/a | n/a |
| **Play Framework** (config precedent) | A secret of `"changeme"` causes the application to **fail to start in production**, deliberately, so the placeholder cannot survive to runtime. | n/a | n/a | n/a | n/a |
| **brew doctor / flutter doctor** (doctor precedent) | Named checks with ✓/✗ per category and a summary line ("Doctor found issues in 1 category"). `brew doctor` exits 0 **only** when there are no warnings, so CI and scripts can gate on it. | n/a | n/a | Each ✗ line states the specific misconfiguration. | n/a |

**Two conclusions that drive the categories.**

1. Against the self-hosted admin-tool class (paperless-ngx, Nextcloud, Home Assistant, Immich), a setup/status view is **table stakes** — every one of them has one, and paperless-ngx users reflexively paste its output into support threads. saneless is currently below that bar.
2. Against the *scanner* class (scanservjs, NAPS2), a status view and honest per-job outcome reporting would be a **differentiator** — scanservjs has neither, and its documentation openly names scanner detection as the step that breaks. This is precisely where saneless can be better than the incumbent it will be compared to.

---

## Feature Landscape

### Table Stakes (users expect these; missing = product feels incomplete)

| Feature | Finding | Why Expected | Complexity | Notes |
|---|---|---|---|---|
| **Setup status strip** on the index page: Scanner / Paperless / Profiles / Fallback, each green-or-red with the identifying detail ("found EPSON XP-7100 on host X", "token rejected at http://…") | U-03 | Universal in the peer class (paperless System Status, Nextcloud setup checks). Today every setup mistake surfaces only as a failed scan, after the family already tried to use it, with `docker logs` empty (M-28). | **MEDIUM** | Back it with one check registry shared with `doctor`. `/api/paperless/test` already exists and already distinguishes connected / token-rejected / unreachable — it just has no caller. Follow the Nextcloud pattern: each red item names the exact env var or config key to change. |
| **`saneless doctor` CLI** running the same checks, human-readable, non-zero exit on any red | U-03 | `brew doctor` / `flutter doctor` are the recognised shape. Also lets the compose healthcheck and the quick start reference one command. | **LOW-MEDIUM** | Exit-code contract worth copying from `brew doctor`: 0 only when everything is green. Add `--json` for scripting parity with `devices --json` / `jobs --json`, which already exist. **Must share one implementation with the status strip** — M-05 is the standing lesson about the same rule living in three places. |
| **Honest per-job outcome line**: "Scanned 10 pages, removed 2 blank, uploaded 8" in the status area and page counts in job history | U-02 | NAPS2 shows every captured page; sane-scan-pdf reports blank removal; paperless reports per-task results. Counts are also the only thing that would have made C-06 (ten-page stack → one-page PDF) visible to a family member. | **LOW** once C-03 lands | Two job columns (`pages_scanned`, `pages_uploaded`) plus two template lines. The review itself says so. For manual duplex, show "fronts 10, backs 10" during pass B. |
| **`FALLBACK` job state and a `warning` field, rendered distinctly** | C-03, U-02 | The docs already promise a distinct FALLBACK status; `JobState` has no such member, so a consume-dir drop and a duplex mismatch both render as a green "Complete". Fabricated success is the single worst appliance failure. | **LOW-MEDIUM** | Purely a consequence of C-03's `PipelineResult`; the UI work is a new branch in `partials/status.html` and a new class in `partials/history.html`. Note `history.html` already colours only DONE/ERROR. |
| **Plain-language error + suggested next step, with raw text behind a `<details>` disclosure** | U-05 | Home Assistant Repairs and Nextcloud setup warnings are the model: never a traceback to a non-developer, always "what broke + what to do". Today `status.html:23` prints `str(exc)` verbatim, which can be kilobytes of HTML from a 4xx. | **MEDIUM** | `ErrorCategory` (FEEDER / CONFIG / SCANNER / UPLOAD / UNKNOWN) already exists in `job.py:36` and is stored but never rendered (N-14). Map category + C-03 outcome → message + action. **Blocked on M-11**: today every first-page ADF error is misreported as "No paper detected", so a friendly message written on top of that would be confidently wrong. |
| **Human profile labels and descriptions, generated at startup, sensible default order** | U-04 | NAPS2 makes Display Name the thing you set first, and every setup guide says so; it also uses "Glass" and "Feeder", not "flatbed" and "ADF". `adf-simplex` means nothing to a family member. | **LOW-MEDIUM** | Add `label` and `description` to `ProfileConfig` (`config.py:63`, which currently has no such fields). Generate from the source classification: "Feeder, single-sided" / "Feeder, both sides" / "Glass (flatbed)". Order feeder-first on sheet-fed devices. Render label in the `<option>`, description as hint text. |
| **Profiles present before the first scan** (generate at startup, keep in memory if config is read-only, and say so on the strip) | U-04, M-04, M-30 | An appliance is configured before anyone touches it. Today the first person to scan sees a different form than the second, and under the recommended read-only mount the profiles never persist. | **MEDIUM** | Straddles the appliance layer and step 5 (config). The status strip is what makes the read-only case honest instead of silent. |
| **Placeholder-token detection at startup, one place for the secret** | U-01 | Play Framework fails to start on `"changeme"`; paperless-ngx v3 shipped "Don't allow the example secret key as a secret key"; Mealie nags while defaults are in use. Today compose's `SANELESS_PAPERLESS__TOKEN=changeme` silently beats the config file the docs tell you to write. | **LOW** | Ship the compose env block commented out with a one-line note that env beats file. Log at startup which config file loaded and which keys came from the environment. See the anti-feature note on *refusing to start*. |
| **Consume-directory fallback and `failed/` mounted by default in the recommended compose** | U-08, C-04, C-05 | "Never lose your work" is the baseline promise. paperless-ngx's own community pushed hard on exactly this (a document a service accepted must not be thrown away on a downstream failure). Paperless being down for an update is the one outage a home operator will definitely hit. | **LOW** (compose + docs) / **MEDIUM** (volume layout) | Both directories must live on the `saneless-data` volume, and the strip must say "Fallback: not configured; scans cannot be kept if Paperless is down". Depends on C-05 for unique names or the fallback silently overwrites itself. |
| **Manual duplex with a real CLI flip prompt and a flip timeout** | C-01, C-02, M-07 | `scanimage --batch-prompt` already waits for Enter between pages, and scanservjs's `Manual` batch mode prompts between pages. A CLI that documents a prompt and does not pause is below the ecosystem floor. | **MEDIUM** | Prompt inside the status callback and set the flip event before returning; make the pipeline refuse a manual-duplex request with no flip event. Timeout matters because the single worker is otherwise parked forever (M-07). |
| **Local-time display of all job timestamps** | M-23 | Every peer shows local time. `history.html` currently does `strftime("%Y-%m-%d %H:%M")` on a UTC value with no label. | **LOW** | Batch it with the U-02 template work — same two templates. |
| **Help text under jargon fields** ("Correspondent — who sent this document? Optional.") | U-07 | GOV.UK's hint-text pattern is the reference; "Correspondent" is paperless's word, not a household word. | **LOW** | One line per control in `index.html`. |
| **Trust-model sentence and a "Which setup do I have?" docs page** | U-09, U-10 | Users are entitled to know they are running a login-less service on every interface. And the scanner-connection question (USB on host / network scanner / remote `saned`) is where the "five minutes" claim actually breaks, with pages that currently contradict each other. | **LOW** (docs only) | Three columns with the exact compose lines for each setup; link from the quick start prerequisites; have the status strip confirm which one won. |

### Differentiators (competitive advantage)

| Feature | Finding | Value Proposition | Complexity | Notes |
|---|---|---|---|---|
| **Status strip + `doctor` sharing one check registry, with an actionable fix line per red item** | U-03 | scanservjs — the tool saneless will be compared to — has *no* diagnostics view at all, and names scanner detection as its most precarious step. Being the SANE web UI that tells you why it can't see your scanner is a real, defensible advantage. | **MEDIUM** | Borrow the Home Assistant Repairs shape: severity + human text + one suggested action (+ optionally a docs anchor). Keep the check list short and concrete; resist growing it into a metrics dashboard. |
| **Queue visibility with position**: "Waiting for 'Tax return' to finish — 1 ahead of you" | U-06 | Immich shows queue *counts* but not per-user position, and has an open request for exactly this class of visibility. A queued job currently shows "Starting scan…" forever, which reads as a hang. | **LOW-MEDIUM** after C-09 | Requires the bounded queue from C-09; show position rather than a bare 429 when full. Cheap once the queue is introspectable. |
| **Owner-only flip prompt via a browser token, plus confirm-before-abort** | U-06 | No peer does this. Today every open tab in the house renders the same live Continue/Abort buttons, so a child on a tablet can cancel a parent's 50-page job — and with C-04 unfixed, destroy the fronts. Non-owners should see "Waiting for the stack to be flipped". | **MEDIUM** | Token in `localStorage`/cookie set at submit time, echoed on the job row. **Frame it as a footgun guard, never as access control** — see anti-features. Depends on M-02 so the job actually leaves AWAITING_FLIP during pass B. |
| **Auto-generated human profile labels from device capabilities** | U-04 | NAPS2 makes the *human* type a display name. saneless can derive "Feeder, both sides" from the scanner's own option list at startup — zero-configuration naming. | **LOW-MEDIUM** | Needs the single `classify_source` from C-06; today `auto_profiles.source_to_slug` and the backend disagree about what an ADF is, so generated labels would be wrong for Epson/Canon/Brother feeders. |
| **Plain-English single-sentence outcome, not a status code** | U-02, U-05 | "Scanned 10, removed 2 blank, uploaded 8" and "Paperless is not responding; your scan has been kept at …" are the appliance voice. paperless-ngx surfaces its own raw task text; nobody in the scanner class does this. | **LOW** after C-03 | The technical detail still exists — behind the disclosure — so the operator loses nothing. |
| **Phone-friendly checkbox tag picker, with the option to hide Tags/Correspondent entirely** | U-07 | GOV.UK's design system is explicit: *"Avoid adding functionality to allow selecting multiple options … there's a history of poor usability and assistive technology support"*, and recommends a checkbox list instead. A native `<select multiple>` on a phone is a modal requiring multi-select gestures. Hiding the fields lets an operator ship a one-button family form and let per-profile defaults do the work. | **MEDIUM** | `default_tags` and `default_correspondent` already exist on `ProfileConfig`; the missing piece is `title` (M-24). If the list grows, the GOV.UK "option select + filter box" pattern (filter input over a checkbox list, checked items always visible) is the accessible upgrade path and still needs no build step. |

### Anti-Features (surface appeal, real problems)

| Feature | Why Requested | Why Problematic | Alternative |
|---|---|---|---|
| **Refusing to start when the token is a placeholder** (U-01) | Play Framework does exactly this and it is the "safe by default" instinct. | For an appliance the operator's only diagnostic surface *is* the web UI. A process that exits on a bad token takes the status strip down with it and leaves the operator back at empty `docker logs` (M-28). | Start, serve the UI, show **Paperless: token is the placeholder value — set it in one place** on the strip, and refuse to *scan* (or refuse only the upload) rather than refusing to boot. `doctor` exits non-zero, so orchestration can still gate on it. |
| **A full multi-step first-run setup wizard** (Mealie/Kavita style) | It is what "appliance" evokes, and peers in the general self-hosted class have one. | A wizard needs identity, persisted "wizard completed" state, and a writable config — all three fight the project's own constraints (no auth, read-only config mount in the recommended compose, config file is authoritative). It also duplicates the status strip's job in a form the user sees exactly once. | Status strip (always visible, always current) + "Which setup do I have?" docs page (U-10). A wizard is worth reconsidering only if config-writing becomes reliable and auth ever lands. |
| **Real user accounts to solve the shared-flip-prompt problem** (U-06) | The ownership problem looks like an auth problem. | Multi-user auth is explicitly Out of Scope in `PROJECT.md` and is a milestone of its own. Worse, a browser token shipped alongside auth-flavoured language would give operators a false sense of protection. | Browser token for accident prevention only. Document it in exactly those words ("stops the wrong tab pressing Abort; it is not a login") next to the U-09 trust-model sentence. |
| **A "Retry upload" button for failed jobs in the web UI** | Genuinely wanted in the peer ecosystem — paperless-ngx has a long-running discussion demanding retry instead of discarding failed documents. | Needs a durable PDF store with identity, dedupe against a possibly-already-consumed document, and job resurrection semantics. That is a feature, not a fix, and it lands on top of a job store that is only now getting a lock (C-07). | Ship C-04's `failed/` directory plus an error message that names the exact path, and the consume-dir fallback on by default (U-08). Retry becomes a v2.x feature once the durable store exists. |
| **A JS combobox / tag-picker library for U-07** | Multi-select on a phone genuinely needs replacing. | Breaks the standing "Jinja2 + HTMX, no build step" and "PicoCSS classless, no build step" decisions — two of the project's logged Key Decisions — for one control. | Server-rendered checkbox list; add an HTMX-driven filter input only if the tag list actually gets long (the GOV.UK option-select pattern). |
| **Per-page progress percentage / ETA** | Immich has an open request for job progress and ETA; `scanimage -p` prints a percentage. | For an ADF the total page count is not known until the feeder empties, so any percentage is a lie, and a lying progress bar is worse than none in a system whose whole problem was reporting failure as success (C-03). | A running count — "Scanning page 7…" — then the final honest tally (U-02). |
| **Reusing `GET /health` as the setup-status source** | It exists and it is already green/red. | Two audiences, two contracts: `/health` answers "should the orchestrator restart me?" (worker thread alive) and must stay fast and dependency-free; the status strip answers "is my setup right?" and needs to reach out to Paperless and the scanner. Coupling them means a slow Paperless makes the container look unhealthy — which is the M-01 failure mode again. | Separate `/api/status` (or similar) for the strip and `doctor`; leave `/health` as-is. Note M-29 is already an unresolved port mismatch in the healthcheck. |
| **A free-form profile editor in the web UI** | "Profiles have labels now, so let me edit them here." | The config file is the source of truth, the recommended mount is read-only (M-30), and `--force` merge/atomic-write behaviour is still being repaired (M-09, M-10). A UI writer on top of that is a second writer racing an unstable one. | Labels are generated (U-04) or hand-written in TOML. The strip reports what was loaded and whether it was persistable. |
| **Growing the status strip into a metrics dashboard** (disk, memory, uptime, versions) | paperless-ngx's status payload includes storage and OS, and it is genuinely useful in support threads. | Every added row dilutes the four things a household operator must act on, and each one is a new thing that can be red for reasons nobody can fix. | Keep the strip to Scanner / Paperless / Profiles / Fallback. Put the long-form detail in `saneless doctor --json`, which is where a support thread can copy it from — the same split paperless-ngx effectively has between its dialog and its API payload. |

---

## Feature Dependencies

```
[C-03 PipelineResult + JobState.FALLBACK + warning field]
    ├──enables──> [U-02 page counts in status + history]
    ├──enables──> [U-02/C-03 FALLBACK rendered distinctly]
    └──enables──> [U-05 outcome-aware friendly messages]

[M-11 correct scanner error classification]
    └──required-by──> [U-05 plain-language errors]
                          └──also-requires──> [M-17 exception translation at boundaries]
                          └──also-requires──> [N-14 render the stored ErrorCategory]

[C-06 single classify_source]
    ├──required-by──> [U-04 auto-generated profile labels]
    └──required-by──> [U-03 status strip "Scanner: found X"]

[C-08 default profile always emitted] ──required-by──> [U-04 profiles present at startup]
[M-04 startup generation + honoured --config path] ──required-by──> [U-04]
[M-09/M-10 safe, atomic, merge-preserving config writes] ──required-by──> [U-04]

[C-09 bounded queue + put_nowait + 429]
    └──required-by──> [U-06 queue position "1 ahead of you"]

[M-02 job leaves AWAITING_FLIP during pass B]
    └──required-by──> [U-06 owner-only flip prompt]
[M-07 flip timeout] ──required-by──> [C-02 CLI flip prompt (unattended safety)]
[C-01 duplex profile field] ──required-by──> [C-02, U-04 duplex labels]

[C-04 preserve PDF in failed/] ──┐
[C-05 unique PDF names]        ──┼──required-by──> [U-08 fallback on by default in compose]
[C-03 FALLBACK state]          ──┘

[M-01 sync routes] ──required-by──> [U-03 status strip]   (a Paperless probe on the event loop re-freezes the UI)
[M-05 single state enum + one label map] ──required-by──> [U-02, U-05, U-06] (all add states/labels)
[C-10 server-owned Scan button via hx-swap-oob] ──conflicts-with──> [U-07 form rework] (same templates)
[M-28 logs on stdout] ──enhances──> [U-03] (doctor is useless if its findings can't be corroborated in logs)
```

### Dependency Notes

- **U-02 requires C-03:** the counts exist in the pipeline today and are discarded because `run_pipeline` returns an unread `dict`. Until there is a typed result record with a home in the job row, there is nothing to render. The review's own estimate: "U-02 is two columns and two template lines after C-03 adds the result record."
- **U-05 requires M-11 before anything else:** every first-page ADF failure is currently labelled "No paper detected" when the real cause may be a jam, an open lid, or a busy device. Writing a friendly message on top of a wrong classification produces a confidently wrong instruction — strictly worse than the raw text it replaces.
- **U-04 requires C-06:** `auto_profiles.source_to_slug` recognises "document feeder"/"feeder" while the backend only matches the substring "adf". Generating labels from the losing classifier would print "Feeder, single-sided" on a profile that then takes the flatbed path.
- **U-03 requires M-01:** the strip's Paperless check is a network call. On today's `async def` routes that call blocks the event loop, so adding the strip would reintroduce the M-01 freeze on every page load.
- **U-06 requires C-09:** "1 ahead of you" needs a queue you can inspect and a bounded queue that returns a position instead of blocking the event loop.
- **U-08 requires C-04 and C-05 together:** mounting the consume directory without unique names means an outage leaves exactly one surviving PDF (C-05), and without the `failed/` directory a 401 still deletes the scan outright (C-04). Shipping the mount alone would look like a fix and not be one.
- **U-07 conflicts with C-10 in scheduling, not in design:** both rewrite `index.html`/`partials/status.html`, and C-10's fix moves the Scan button into a shared include rendered by exactly one template. Do the form rework *after* C-10, not beside it.
- **M-23 co-locates with U-02:** the same two templates render timestamps and would render counts. One pass, not two.

---

## MVP Definition

### Ship in v2.0 (the appliance layer, review step 11)

Ordered so each item's prerequisite has already landed in steps 1–5.

- [ ] **Status strip + `saneless doctor` over one shared check registry** (U-03) — the review's own #1; every other setup defect becomes visible before the first scan instead of after it.
- [ ] **Page counts, FALLBACK state, and warnings rendered** (U-02, C-03) — makes C-06-class page loss visible on the spot; nearly free once C-03 landed.
- [ ] **Plain-language errors with a next step, raw text behind a disclosure** (U-05) — the difference between "I can fix this" and "I'll tell the operator".
- [ ] **Human profile labels/descriptions, generated at startup, sensible order** (U-04) — an appliance is configured before anyone touches it.
- [ ] **Placeholder-token check at startup + one place for the secret in compose** (U-01) — removes the most common silent 401.
- [ ] **Consume-dir fallback and `failed/` on by default in the recommended compose** (U-08) — "never lose your work" must be the default, not an optional step.
- [ ] **CLI flip prompt + flip timeout + visible reverse-pass state** (C-01, C-02, M-07, M-02) — manual duplex is the feature most consumer ADF owners need and it currently does not work.
- [ ] **Local-time timestamps** (M-23) — one-line credibility fix, same templates as U-02.
- [ ] **Help text under each form control** (U-07, first half) — cheapest usability win in the milestone.
- [ ] **Trust-model sentence + "Which setup do I have?" docs page** (U-09, U-10) — docs-only, unblocks the quick start's "five minutes" claim.

### Add once the core is proven (v2.x)

- [ ] **Queue position and owner-only flip prompt with confirm-before-abort** (U-06) — trigger: C-09's bounded queue is in and a second household member has actually collided with a running job. MINOR severity in the review; genuinely valuable but not what blocks the release.
- [ ] **Checkbox / filterable tag picker and hide-Tags-and-Correspondent option** (U-07, second half) — trigger: the help text alone has not moved family tagging behaviour, or someone actually tries it on a phone.
- [ ] **`Sec-Fetch-Site` check so an external page cannot trigger scans through a family browser** (U-09 / N-22) — trigger: the trust-model sentence is written and the operator wants more than a sentence.
- [ ] **`doctor --json` consumed by the compose healthcheck** — trigger: `doctor`'s check registry has stabilised.

### Defer (v3+)

- [ ] **Retry-failed-upload button** — needs a durable, identified PDF store and dedupe; `failed/` plus a path in the error message is the v2.0 answer.
- [ ] **Setup wizard** — needs writable config and identity; the status strip covers the same ground continuously.
- [ ] **Web-based profile editor** — needs the config-writing path (M-09, M-10, M-30) to be boring first.
- [ ] **Any authentication** — explicitly Out of Scope; revisit only as its own milestone, and do not let the U-06 browser token drift into it.

---

## Feature Prioritization Matrix

| Feature | Finding | User Value | Implementation Cost | Priority |
|---|---|---|---|---|
| Status strip (Scanner/Paperless/Profiles/Fallback) | U-03 | HIGH | MEDIUM | P1 |
| `saneless doctor` sharing the same checks | U-03 | HIGH | LOW-MEDIUM | P1 |
| Page counts + blank-removal tally | U-02 | HIGH | LOW (after C-03) | P1 |
| FALLBACK state + warning rendered distinctly | C-03, U-02 | HIGH | LOW-MEDIUM | P1 |
| Plain-language errors + `<details>` disclosure | U-05 | HIGH | MEDIUM | P1 |
| Profile labels/descriptions at startup | U-04 | HIGH | LOW-MEDIUM | P1 |
| Placeholder-token check + one secret location | U-01 | HIGH | LOW | P1 |
| Fallback + `failed/` mounted by default | U-08, C-04 | HIGH | LOW-MEDIUM | P1 |
| CLI flip prompt + flip timeout | C-01, C-02, M-07 | HIGH | MEDIUM | P1 |
| Local-time timestamps | M-23 | MEDIUM | LOW | P1 |
| Help text on form controls | U-07 | MEDIUM | LOW | P1 |
| Trust-model + "Which setup do I have?" docs | U-09, U-10 | MEDIUM | LOW | P1 |
| Queue position ("1 ahead of you") | U-06 | MEDIUM | LOW-MEDIUM | P2 |
| Owner-only flip prompt + confirm-before-abort | U-06 | MEDIUM | MEDIUM | P2 |
| Checkbox tag picker + hide-fields option | U-07 | MEDIUM | MEDIUM | P2 |
| `Sec-Fetch-Site` origin guard | U-09 / N-22 | LOW-MEDIUM | LOW | P2 |
| `doctor --json` in the compose healthcheck | U-03 | LOW | LOW | P3 |
| Retry-failed-upload | (beyond C-04) | MEDIUM | HIGH | P3 |
| Setup wizard | — | LOW | HIGH | P3 |

**Priority key:** P1 = must ship in v2.0 · P2 = should have, add when the P1 prerequisites are proven · P3 = future.

---

## Competitor Feature Analysis

| Capability | scanservjs (closest peer) | paperless-ngx / Nextcloud / Home Assistant | NAPS2 | Our approach |
|---|---|---|---|---|
| Setup status | None; About page only; docs admit detection is the fragile step | Central, per-subsystem, green/red, with a fix link (Nextcloud) or a copyable payload (paperless) | None | **Four-item strip** — Scanner, Paperless, Profiles, Fallback — each with the identifying detail and one suggested action, and `doctor` as the same checks in a terminal (U-03) |
| Doctor CLI | None | `immich-admin schema-check`; paperless `/api/status/` | None | **`saneless doctor`**, `brew doctor` exit-code semantics, `--json` for scripting (U-03) |
| Job outcome | Files appear in a list; a batch that dies at assembly leaves pages only in `data/temp` | Task list with per-task result text, often raw | Pages visible as thumbnails | **One honest sentence with counts** plus FALLBACK/warning states, technical detail behind a disclosure (U-02, U-05, C-03) |
| Queue | n/a | Immich shows active/waiting counts; nobody shows position | n/a | **Position, by name**: "Waiting for 'Tax return' — 1 ahead of you" (U-06) |
| Friendly errors | Raw | HA Repairs is the gold standard (severity + human text + fix flow / learn-more) | n/a | **Category → message + next step**, reusing the already-stored `ErrorCategory`, with raw text in `<details>` (U-05) |
| Profile labels | Raw SANE options, config-file overrides | n/a | User-typed **Display Name**; "Glass" / "Feeder" wording | **Auto-generated labels** ("Feeder, both sides", "Glass (flatbed)") plus descriptions, feeder-first ordering (U-04) |
| Manual duplex | `Manual` batch mode prompts between pages | n/a | Explicit duplex profiles | **Real prompt in web and CLI**, a `duplex` profile field, and a flip timeout (C-01, C-02, M-07) |
| Placeholder secrets | n/a | paperless v3 rejects the example secret key; Play fails to start on `changeme`; Mealie nags | n/a | **Detect and show red; refuse to scan, not to boot** (U-01 + U-03) |
| Multi-select tags | n/a | paperless uses a rich JS picker | n/a | **Checkbox list, no build step**, per GOV.UK guidance; hideable entirely (U-07) |

---

## Confidence and Gaps

| Claim | Confidence | Basis |
|---|---|---|
| paperless-ngx exposes a System Status with per-subsystem checks; v3 rejects the example secret key and added a Tasks UI | HIGH | Official release notes and a verbatim `/api/status/` payload from maintainer-answered support threads |
| Nextcloud ships a built-in configuration checker on the admin overview with per-warning fix docs | HIGH | Official admin manual, "Warnings on admin page" |
| Home Assistant Repairs model (severity, `is_fixable`, `learn_more_url`) | HIGH | Official developer docs |
| GOV.UK advises against `<select multiple>` and recommends checkbox lists | HIGH | GOV.UK Design System, Select component |
| `brew doctor` exits 0 only when clean; `flutter doctor` prints per-category ✓/✗ with a summary | MEDIUM-HIGH | Multiple secondary sources agreeing, plus quoted terminal output |
| scanservjs has no diagnostics/status view and names detection as its fragile step | MEDIUM-HIGH | Its own documentation site, fetched directly |
| NAPS2 profile Display Name / "Glass" vs "Feeder" wording; `-p "<profile name>"` CLI | MEDIUM-HIGH | Official NAPS2 command-line docs plus two independent vendor setup guides |
| `scanimage --batch-prompt` waits for Enter; `--batch-double` for manual duplex | MEDIUM | Linux Magazine SANE command-line article; not re-verified against the current man page |
| Immich shows queue counts but not per-user position | MEDIUM | Official jobs/workers docs plus an open feature request for progress/ETA |
| Mealie warns persistently while default credentials are in use | LOW | A single walkthrough video transcript; not confirmed in Mealie docs |

**Gaps for the roadmap to resolve later, not now.**

- The exact set of checks in the status strip is a design decision, not a research finding. Four is the review's proposal and matches the four things that can be wrong; resist adding a fifth without a failure story behind it.
- Whether `doctor` should probe the scanner by opening the device (slow, may fail on a busy scanner) or only by listing devices needs a phase-level decision. Note M-13's finding that the flatbed path has no timeout — a naive probe could hang `doctor` itself.
- The browser-token mechanism for U-06 (cookie vs `localStorage`, lifetime, what happens on a page reload mid-job) is unspecified and deserves a short phase-research pass.
- Nobody in the surveyed set solves "shared physical device, multiple household viewers, no auth". U-06's design is genuinely novel here and should be prototyped small.

## Sources

- paperless-ngx: release notes (v3 System Status task summary, `view_system_status`, "Don't allow the example secret key"), `/api/status/` payloads in maintainer-answered discussions, failed-file-task discussions (#8252, #10632, #12220)
- Nextcloud admin manual — "Warnings on admin page" (security & setup warnings)
- Home Assistant developer docs — Repairs / issue registry
- Immich docs — Jobs and Workers, Server Commands (`immich-admin`), Architecture; feature request for job progress/ETA
- scanservjs documentation site (features, running, batch Auto vs Manual, About page) and issue #414 (batch assembly failure)
- NAPS2 — Command Line Usage (profiles, `--listdevices`); third-party profile setup guides (Display Name, Glass vs Feeder)
- SANE command-line reference material (`scanimage --batch-prompt`, `--batch-double`, `-p`; `scanadf`), sane-scan-pdf blank-page threshold
- GOV.UK Design System — Select, Checkboxes, Question pages; GOV.UK accessibility blog on filterable checkbox lists
- Homebrew / Flutter doctor behaviour and exit codes
- Play Framework `SecretConfiguration` (`"changeme"` fails to start in production)
- `.planning/reviews/2026-09-09-code-review.md` sections 3, 10 (step 11), and 11; `.planning/PROJECT.md`; `src/saneless/web/templates/`, `src/saneless/job.py`, `src/saneless/config.py`, `src/saneless/web/routes.py`, `src/saneless/cli.py`

---
*Feature research for: self-hosted scanner-to-paperless-ngx appliance, v2.0 "Prep for release"*
*Researched: 2026-09-09*
