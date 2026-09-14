# Phase 26: Worker and Web Robustness - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-14
**Phase:** 26-worker-and-web-robustness
**Areas discussed:** Rejection visibility, Shutdown mid-scan (and worker health), Startup auto-profiles, Cross-site guard

---

## Rejection visibility

**Which error responses should htmx swap into the status area?**

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit list | Only 422, 429, 503; a real 500 is not swapped and the status area survives | |
| All 4xx and 5xx | `[45]..` swaps; an unhandled 500's plain body could replace #status-area | |
| All, with catch-all handler | Swap every 4xx/5xx and render any unexpected error as a partial | ✓ |

**Where should errors from requests that don't target the status area land?**

| Option | Description | Selected |
|--------|-------------|----------|
| Retarget to status | `HX-Retarget` + `HX-Reswap` on every error response | ✓ |
| Swap in place | Per-target error renderings (e.g. disabled option) | |
| Status for POSTs only | POST errors retarget; background GETs not swapped | |

**How should an error message and the 1 s status poll coexist?**

| Option | Description | Selected |
|--------|-------------|----------|
| Separate message slot | Sibling region receives errors; poll continues; cleared by next successful submit | ✓ |
| Message + job, poll resumes | Message visible ~1 s until next poll | |
| Message replaces status | No poll; progress lost until reload | |

**Should a rejected submit (429/503) be recorded as a job?**

| Option | Description | Selected |
|--------|-------------|----------|
| No row | Reject before the row is committed (review's advice) | |
| ERROR row in history | Record the attempt as an ERROR job | ✓ |

**User's choice:** Catch-all rendering, retarget to a separate message slot, ERROR rows for rejected submits.
**Notes:** 422 still creates no row (ROBU-08 says "before creating a job"). Claude flagged afterwards (in CONTEXT D-06) that the rejection row would win D-17's "most recent" fallback once the running job ends; the planner must resolve this without changing D-17's meaning.

---

## Shutdown mid-scan (and worker health)

**On Ctrl-C / docker stop, what happens to a job mid-scan or at the flip prompt?**

| Option | Description | Selected |
|--------|-------------|----------|
| Abandon it | Stop flag, Abort any open flip wait, bounded join; next startup marks it ERROR | ✓ |
| Wait for it | Longer join until terminal; risks SIGKILL, contradicts ROBU-02 | |
| Abandon, but record now | Also write ERROR at shutdown; races the live thread | |

**If the join expires with the thread alive, what about the store and Paperless client?**

| Option | Description | Selected |
|--------|-------------|----------|
| Leave them open | Warn, skip close(), process exit reclaims | ✓ |
| Close anyway | Deterministic release; noisy traceback from the stuck thread | |

**Bounded join length?**

| Option | Description | Selected |
|--------|-------------|----------|
| 5 seconds | Today's value; fits Docker's 10 s grace | ✓ |
| 2 seconds | Snappier Ctrl-C | |
| Configurable | New `output.shutdown_timeout_seconds` | |

**Should the worker ever give up on persistent loop-level failures?**

| Option | Description | Selected |
|--------|-------------|----------|
| Never give up | Log and keep serving; /health tied to thread liveness | |
| Give up after N in a row | Loop exits, /health 503, orchestrator restarts | |
| Stay alive, degrade health | Thread keeps running; /health 503 while last N loop-level ops failed | ✓ |

**While degraded, what should POST /api/scan do?**

| Option | Description | Selected |
|--------|-------------|----------|
| Reject with 503 | Same path as a dead worker | ✓ |
| Accept and try | Health is advisory only | |

**What clears the degraded state?** (Claude raised it: rejecting scans removes the jobs whose success would clear the state)

| Option | Description | Selected |
|--------|-------------|----------|
| Idle-tick probe | Cheap store probe every few seconds while degraded | ✓ |
| Time-based expiry | Expire after ~60 s; next submit is the trial | |

**User's choice:** Abandon, leave resources open on timeout, 5 s, degrade health rather than die, reject scans while degraded, recover via idle probe.
**Notes:** User asked for more questions after the first four, which produced the degraded-submit and recovery questions.

---

## Startup auto-profiles

**Where in startup does generation run?**

| Option | Description | Selected |
|--------|-------------|----------|
| Worker's first act | Server already up, /health answers, early submits queue behind it | ✓ |
| Block lifespan startup | Dropdown right on first load; no connections until SANE answers | |
| Block with a timeout | Wait a few seconds, then continue in background | |

**Scanner unreachable at boot?**

| Option | Description | Selected |
|--------|-------------|----------|
| Once per start | Log real exception, keep bare default, no generation in job path | ✓ |
| Retry before first job | Keeps a generation branch in the job path | |
| Periodic retry while bare | Repeated SANE enumeration on idle appliance | |

**No config file loaded (env-only)?**

| Option | Description | Selected |
|--------|-------------|----------|
| Memory only | Merge under lock, write nothing, INFO log | ✓ |
| Create a config file | Writes a file the operator never asked for | |
| Skip generation entirely | Env-only keeps bare default | |

**Loaded config file not writable?**

| Option | Description | Selected |
|--------|-------------|----------|
| Memory + WARNING | Usable this run; warn with path and OSError | ✓ |
| Bare default + WARNING | Identical behaviour across restarts | |

**User's choice:** All recommended options.
**Notes:** None.

---

## Cross-site guard

Claude reported beforehand that browsers send `Sec-Fetch-Site` only to secure contexts (HTTPS or localhost), so on saneless's plain-HTTP LAN deployment the review's rule would protect nothing. Checked via web search against Go 1.25 `CrossOriginProtection` write-ups.

**Which cross-site rule?**

| Option | Description | Selected |
|--------|-------------|----------|
| Go-style with Origin fallback | Sec-Fetch-Site → Origin vs Host → allow if neither | ✓ |
| Sec-Fetch-Site only | Literal ROBU-10; no protection on http://<lan-ip> | |
| Require one header | Blocks curl/scripts without Origin | |

**Plain-HTTP proxy that rewrites Host?**

| Option | Description | Selected |
|--------|-------------|----------|
| Document Host preservation | No new config; doc + diagnostic log | |
| Also accept X-Forwarded-Host | Not forgeable by a browser without preflight | |
| Trusted-origins config key | New config surface | |

**User's choice:** Free text: "Does it make sense to do both Document Host preservation and Also accept X-Forwarded-Host?" Claude answered that the two complement each other: the code covers Traefik and Caddy, and the docs cover nginx, which by default sets neither header. The user confirmed "Yes, both".

**Rejected cross-site POST response?**

| Option | Description | Selected |
|--------|-------------|----------|
| 403 via shared renderer | Same error path; visible message; diagnosable proxy misconfig | ✓ |
| Bare 403, no body | Silent in the UI | |

**Scope / defaults:** accepted app-wide middleware on every non-GET/HEAD/OPTIONS method, and `web_host` stays `0.0.0.0` (documented, per DOCS-05).

---

## Claude's Discretion

- Vendored asset layout, SRI sourcing, license notices (versions fixed: htmx 2.0.8, Pico 2.1.1)
- Server-owned Scan button mechanics (shared include, OOB swap, `hx-disabled-elt`)
- Metadata cache single-flight mechanism
- Retry-After value; queue depth stays 10
- Message and ERROR-row wording; "server restarted" reason text
- Degraded-health N, probe interval, probe statement, /health JSON shape
- ROBU-08 title cap; `resource` Literal
- Startup prune placement
- `wait_for_state` helper and worker test conversion
- Whether generated profiles replace the bare `default` entry in memory
- CI browser job shape and in-test egress blocking

## Deferred Ideas

- Paperless connect timeout plus caching "unreachable" (M-01 second half): no v2.0 requirement maps it
- Tag/correspondent id validation against warm cache (N-20 extra)
- Configurable shutdown join, queue depth, trusted origins
- Periodic auto-profile retry
