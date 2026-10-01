# Web API

saneless exposes a web API at the configured host and port (default `0.0.0.0:8080`). The web UI uses these endpoints via HTMX. They exist for the web UI: most return HTML fragments for HTMX to swap into the page rather than JSON, so their responses change along with the UI. Only `GET /health` and `GET /api/paperless/test` return JSON on success. Error responses are different: every endpoint answers a request without `HX-Request: true` with a JSON error body, as described under [Errors](#errors).

Direct calls, such as from `curl` or a script, work too, but only those two JSON endpoints and the JSON error body have a stable shape to build on.

## Endpoint Overview

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Web UI main page |
| GET | `/health` | Health check (no auth) |
| GET | `/api/paperless/test` | Test paperless-ngx connection |
| POST | `/api/scan` | Start a scan job |
| GET | `/api/jobs/current/status` | Poll current job status |
| GET | `/api/jobs/{job_id}/status` | Poll the status of one named job |
| GET | `/api/checks` | Render the system status strip from cache |
| POST | `/api/checks/refresh` | Re-run every check now and render the strip |
| GET | `/api/tags` | Fetch paperless-ngx tags, optionally filtered |
| GET | `/api/correspondents` | Fetch paperless-ngx correspondents |
| GET | `/api/profiles/description` | One profile's description sentence |
| GET | `/api/profiles/multi-page` | The Multiple pages checkbox for one profile |
| GET | `/api/profiles/tags` | The tag list ticked with one profile's default tags |
| GET | `/api/profiles/correspondent` | The correspondent dropdown set to one profile's default |
| GET | `/api/metadata` | Both metadata lists, their help, and the Scan button, in one answer |
| GET | `/api/metadata/probe` | Whether a list that could not be loaded can be loaded now |
| POST | `/api/cache/invalidate` | Refresh cached metadata |
| GET | `/api/jobs/history` | Job history table |
| POST | `/api/flip/continue` | Continue manual duplex scan |
| POST | `/api/flip/abort` | Abort manual duplex scan |
| POST | `/api/multi-page/answer` | Answer a multi-page scan's open question |

---

## Endpoint Details

### `GET /`

Renders the main web UI page with scan form, live status indicator, and job history table.

The page asks paperless-ngx for nothing, so it renders at once even when paperless-ngx is
slow or down. Where the tag list and the correspondent dropdown go, it says each is
loading, and once the page has rendered it asks [`GET /api/metadata`](#get-apimetadata)
for both, naming the profile the dropdown shows. If Profile changes before the answer
lands, the page asks again for the new profile and abandons the earlier request. A list
that could not be loaded keeps asking whether it can be loaded now, through
[`GET /api/metadata/probe`](#get-apimetadataprobe), and that asking changes nothing on
the form. Once it can, the page asks for the list again with what the form shows at that
moment, and drops or abandons that request if Profile is changing at the same time. So the
lists never come back for a profile no longer chosen, and a tick or a choice made in the
meantime is kept. A tick or a choice made while that last request is itself in flight is
not kept, as with a filter or a refresh. That request is normally answered at once from
the cache, but with the cache disabled (`paperless_cache_ttl_seconds = 0`) it asks
paperless-ngx for the list again and can take as long as any list request. Until the
`GET /api/metadata` answer lands, the Scan button
is disabled with a line beneath it saying it waits for the lists; that answer releases it
whether the lists arrived or could not be loaded. A form that shows neither list waits for
nothing.

**Response:** HTML page.

---

### `GET /health`

Health check for container orchestration and monitoring.

**Response format:** JSON

| Status Code | Body | Condition |
|-------------|------|-----------|
| 200 | `{"status": "ok"}` | Worker thread is alive and its job store is working |
| 503 | `{"status": "error", "detail": "job store failing"}` | Worker thread is alive but degraded: the job store is failing |
| 503 | `{"status": "error", "detail": "worker thread is down"}` | Worker thread is not running |

The worker becomes degraded after three job-store failures in a row (its own job writes or its periodic history prune), when three retries in a row fail to write a job record it owes, or at startup when the job store cannot be written. A scan whose job records are all written, a prune that succeeds, or an owed record written starts the count again. The worker retries owed records after each scan that ends cleanly and every 5 seconds while no scan is running, and a scan that ends cleanly also starts the retry count again, so on an idle worker a job store that stays broken degrades it within about 15 seconds. While degraded, new scans are refused with `503`. Degraded clears itself: while no scan is running, the worker checks the job store every 5 seconds, and the first check in which the job store accepts writes again, including any job records it still owes, returns `/health` to `200`.

---

### `GET /api/paperless/test`

Tests the connection to the configured paperless-ngx instance.

**Response format:** JSON

| Status Code | Body | Condition |
|-------------|------|-----------|
| 200 | `{"status": "connected"}` | paperless-ngx answered with a 2xx |
| 200 | `{"status": "token_rejected"}` | Server reachable, token rejected (401 or 403) |
| 200 | `{"status": "not_found"}` | Server reachable, but the paperless-ngx API is not at the configured URL (404) |
| 200 | `{"status": "server_error"}` | Server reachable, but answered 5xx or any other unclassified non-2xx |
| 200 | `{"status": "unreachable"}` | Server is not reachable (connection refused, DNS failure, connect or read timeout) |
| 200 | `{"status": "incompatible_version"}` | Server reachable, but refused the API version (406): saneless needs paperless-ngx 2.16 or later, which allows API version 9 or 10 |
| 500 | `{"status": "error", "detail": "..."}` | Unexpected failure inside saneless while running the test |
| 503 | `{"status": "error", "detail": "TimeoutError"}` | The caller could not get a turn to wait for a running test; carries a `Retry-After` header |

The six 200 values are the complete set, and each is a stable wire contract: `connected`
is returned for a 2xx and nothing else, so a 404 or a 500 is now reported as its own
outcome rather than as a working connection.

saneless asks for API version 9 on its first request, which every paperless-ngx from 2.16
on allows, and for version 10 once an answer has said the server allows it (paperless-ngx
3.x does). A paperless-ngx before 2.16 allows only versions up to 7, so it answers `406`,
reported here as `incompatible_version`. If a server that allowed 10 answers `406` to it
later, for example after paperless-ngx is rolled back from 3.x to 2.x, saneless goes back
to version 9: an upload or a list fetch asks again at once with 9, and every later request
starts at 9.

The result is shared and reused for 2 seconds; concurrent calls do not each contact
paperless-ngx. A call inside that window gets the same status code and body as the call
that ran the test, the `500` included, so calling the endpoint in a loop sends at most one
request to paperless-ngx every 2 seconds. While a test is running, a caller that has a
previous result to fall back on gets that result at once instead of waiting -- against an
unreachable paperless-ngx a test can take the client's full 30-second timeout. Only the
first callers after start-up, before any result exists, wait for the running test, and
no more than two of them at a time. One that waits longer than 35 seconds, or that arrives
while two others are already waiting, is answered `503` with `"detail": "TimeoutError"`
and a `Retry-After: 30` header. That answer is not shared: the caller never got a turn,
so there is no result to share.

---

### `POST /api/scan`

Starts a new scan job. Accepts form data (designed for HTMX form submission).

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `profile` | string | yes | Scan profile name from config |
| `title` | string | no | Document title, at most 118 characters so paperless-ngx keeps it whole, with no tab or other control character (auto-generated from timestamp if empty) |
| `tags` | int[] | no | Paperless-ngx tag IDs, at most 100, each from 1 to 2147483647 |
| `correspondent` | int | no | Paperless-ngx correspondent ID, from 1 to 2147483647 |
| `multi_page` | boolean | no | `on` when Multiple pages is ticked. Chosen for this scan only; it is not stored on the profile or the job. Absent means one pass. Refused with `422` on a manual-duplex profile |
| `tags_profile` | string | no | The profile whose defaults the web UI's tag list was showing. When it names a profile other than `profile`, the submitted `tags` are ignored and `profile`'s `default_tags` apply. Absent means the `tags` are used as sent |
| `correspondent_profile` | string | no | The same, for the correspondent dropdown: when it names another profile, `correspondent` is ignored and `profile`'s `default_correspondent` applies |

The web UI sends the two `*_profile` fields so that a tag list or dropdown still showing
another profile's defaults never files them under the profile you picked. That happens
while the list is still being replaced after a profile change, or when replacing it
failed. A script that posts its own `tags` and `correspondent` can leave them out.

Until the page's lists have loaded, both fields say `(lists loading)`, which names no
profile, so a scan started in that moment gets the profile's `default_tags` and
`default_correspondent`, just as the untouched form does once the lists are in. Scan is
held while the lists load, but a scan finishing during the load can release it early.

**Responses:**

| Status Code | Meaning |
|-------------|---------|
| 200 | The job is queued. HTML partial: the status indicator for HTMX swap, plus an out-of-band Scan button and an out-of-band clear of any earlier error message. |
| 400 | A browser posted the scan form by itself, which happens only with JavaScript off. The answer is an HTML page saying no scan was started; see [A browser with JavaScript off](#a-browser-with-javascript-off). |
| 403 | The request was blocked as cross-site. See [Cross-site requests](#cross-site-requests). |
| 422 | The request is not valid: the profile does not exist, Multiple pages was asked for on a manual-duplex profile, the title is longer than 118 characters, the title contains a tab or another control character, more than 100 `tags` were sent, a tag or correspondent ID is outside 1 to 2147483647 (the range of a paperless-ngx ID), or a required field is missing. No job is created. |
| 429 | The scan queue is full: 10 jobs are already waiting to start. The response carries `Retry-After: 30`. |
| 503 | The worker is not running, or it is degraded (see [`GET /health`](#get-health)). |

A `429` or `503` is a refused attempt, not a missing one: it is recorded in job history as a failed job ("Not started: ..."), provided the job store accepts the write. A `400` or `422` records nothing. Error bodies follow [Errors](#errors).

A title holding a control character -- a tab, an escape character, any other C0 or C1 control, or DEL -- is refused rather than cleaned up, so the title stored is always the one that was typed. The web page's title box cannot produce a newline, so in practice this is a pasted tab or a request that did not come from the page. Accented letters and other ordinary characters, a no-break space included, are accepted.

---

### `GET /api/jobs/current/status`

Returns the current or most recent job status. Used by HTMX polling to update the status indicator.

**Response:** HTML partial with job state. Possible states: `PENDING`, `SCANNING`, `AWAITING_FLIP`, `AWAITING_NEXT_PASS`, `AWAITING_BLANK_DECISION`, `AWAITING_RETRY`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`, `FALLBACK`, `CANCELLED`.

`AWAITING_FLIP` and the three multi-page states `AWAITING_NEXT_PASS`, `AWAITING_BLANK_DECISION` and `AWAITING_RETRY` mean the job is waiting for a person: the flip of a manual duplex stack, or the answer to a multi-page question (see [`POST /api/multi-page/answer`](#post-apimulti-pageanswer)). The job is not finished, so the partial keeps polling and the Scan button stays disabled, but the scanner is idle and the partial shows no busy spinner: the browser that started the job sees the question and its buttons, and every other client sees one line saying what the job is waiting for, such as `Waiting for the next page...`. The Scan button reads `Waiting for you…` during a multi-page question.

`DONE`, `ERROR`, `FALLBACK` and `CANCELLED` are terminal: once the job reaches one of them the partial stops polling and the Scan button is enabled again. The terminal partial also reloads the job history table and the checks strip once, so the finished job's row appears and the strip stops saying a scan is running. The main page (`GET /`) renders the same job without that reload, because it has just rendered both. `CANCELLED` means the operator stopped the scan on purpose, such as with Abort scan at the flip prompt. The web UI shows it as `Cancelled: <title>` in muted grey, not as an error.

What the partial shows depends on who asks. The browser that started the job sees its title, preview and error or warning text. Every other client sees the state, the outcome and the page counts under the title `Scan (title hidden)`, with no preview and a fixed sentence in place of the text. See [who can read what](#what-an-unauthenticated-client-can-read).

**Query parameters:** all three are written by saneless into the poll URL it hands the page. A client never needs to compose them.

| Parameter | Type | Description |
|-----------|------|-------------|
| `seen` | string, optional | A token for what the polling element is showing. When nothing the viewer would see has changed, the poll is answered `204` with no body, and htmx leaves the element and anything focused inside it in place. The token is a keyed hash, so it is meaningless outside the running server. A value that is missing, wrong or longer than any token is ignored and the full partial is returned; it is never refused. |
| `attempt` | integer, optional | Which step of the lost-contact backoff this poll is on (see below). A value out of range is clamped rather than refused with a `422`. |
| `focus` | string, optional | `scan` after an Abort: the first partial whose Scan button is enabled marks that button `autofocus`, so keyboard focus lands on Scan once the stopped job has ended. Any other value is ignored and never echoed back. |

**When the job cannot be read.** If saneless cannot read the job from its job store, or cannot render the partial, the poll is still answered `200`, never with an error. The response is a status indicator holding one amber line, `Cannot read the scan's progress right now — retrying...`, with no Scan button, no job text and no page title, and the page's message area is left alone. Its poll URL carries the next `attempt`, and its interval steps from 2 seconds to 5 and then to 15, where it stays. At 15 seconds the line no longer changes, so each poll from then on is answered `204`. Until then each step swaps the same line in again, so a screen reader may read it up to three times. Polling never stops: once the job can be read again, the next poll returns the real partial and the backoff starts over. The cause goes to the server log only.

---

### `GET /api/jobs/{job_id}/status`

Returns the status of one named job, rather than whichever job is current. The web UI polls this after a submit, so the browser that started a scan keeps following *its* job even when another one is running: that is what decides whether the flip prompt or a multi-page question is shown to you, or the line saying what someone else's scan is waiting for.

**Path parameter:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `job_id` | string | The job to report on. Opaque: it is a database lookup key and nothing else |

**Response:** HTML partial, the same shape as [`GET /api/jobs/current/status`](#get-apijobscurrentstatus).

Knowing a job's id does not show you more of it: the partial follows the same owner rule as [`GET /api/jobs/current/status`](#get-apijobscurrentstatus).

An id that names no job is **not** a `404`. The partial falls back to the current-or-most-recent rendering, so a browser whose job has aged out of history keeps working, and a caller cannot use the status code to discover which job ids exist.

It takes the same `seen` and `attempt` query parameters and answers a job it cannot read the same way. The retrying line keeps polling this job's URL only when `job_id` is a well-formed job id. Otherwise it polls `/api/jobs/current/status`, so nothing from the path is echoed back unchecked.

---

### `GET /api/checks`

Renders the system status strip -- the Configuration, Scanner, Paperless, Profiles, Fallback and Data folder rows shown at the top of the page, the same six checks `saneless doctor` prints.

**This is a cache read and never probes.** However many browser tabs are open, the underlying checks run no more often than their cache allows, so watching the page cannot generate scanner or paperless-ngx traffic.

**Response:** HTML partial (the whole strip body, for `outerHTML` swap).

Before any results exist the response is six `Checking...` rows carrying a self-poll; once results exist the body it returns carries no poll trigger, so the polling stops on its own. While a scan is running the scanner check is skipped and the strip says so rather than probing a device that is in use.

A poll whose request fails ends too, and what it ends in depends on the failure. A counter above the range is read as the end of the chain: it clamps to the larger cap, which is the give-up body, so the strip comes back with no poll attached. A counter below the range -- which nothing on the page ever sends -- is the one input that does not end the chain: it is read as zero, the number a page render starts at, so it comes back as the first body of a fresh chain, bounded like any other. Any failure inside the strip's own rendering comes back as the cold-start body with no poll attached: six named rows, the `Check again` button, and the line saying the checks have not run yet. A genuine error response to the poll's own request -- a counter that is not a number at all -- is deliberately not retargeted into the status area the way every other htmx error in this application is, so it replaces the strip, and the poll ends with it. That exemption is for the strip fetching itself and for nothing else: the `Check again` button aims at the same element, but it is a POST, and an error answering it goes to the status area like every other click's, leaving the strip and the button on the page. The strip does not come back on its own once that error body has replaced it: the body carries no `checks-body` id, so the reload every terminal job state fires at the strip and the out-of-band strip a scan submit carries both find nothing to replace, and a later scan's paused note has nowhere to land. Reloading the page is what restores it. That is accepted because the only request that can reach this response is the poll's own, carrying a counter nothing on the page ever mints, so the tab it empties is the one that crafted it.

One case is left, and it is stated here rather than left to be rediscovered: a request that gets no response at all -- the appliance switched off mid-poll, the connection reset -- produces nothing to replace the strip with, so that tab goes on asking every couple of seconds until it is closed. It is asking an origin that is not answering, so there is nothing on the page for it to overwrite and no scanner or paperless-ngx traffic behind it.

That poll also has a second ending, for the case where the first one never comes -- and which of two forms it takes depends on whether anything is actually running.

If nothing is in flight, the strip gives up after about twenty seconds and stops asking. That is the case where the checks are not going to run at all: the background refresher has stopped, say, and a tab left open on a hallway tablet in front of a half-broken appliance would otherwise ask forever. Giving up costs nothing that was on the page: the six rows stay, the `Check again` button stays, and the line beneath them reads "The checks have not run yet. Press Check again to try now.", which is the one way forward left. Pressing it starts a fresh attempt, and a fresh count, whenever somebody comes back to it.

If a check *is* running -- the first one, still working through an unreachable scanner -- the strip keeps asking instead, for about three minutes, and the line beneath the rows reads "The first check is still running. This can take a couple of minutes if the scanner is not reachable." Two facts make that the right answer rather than the earlier one. A single check can legitimately take far longer than twenty seconds: name resolution has no timeout of its own, and a deployment with no configured scanner host goes straight into the SANE enumeration, which on Linux spends around two minutes on a host that is switched off. And telling somebody to press a button that starts the very thing already running is advice that cannot help -- the press collapses into the check in flight and the page waits exactly as long either way.

The longer window is still a window. After about three minutes the strip stops asking whatever it was waiting for, and the line goes back to saying the checks have not run yet: a check that has been running that long is one whose thread may well have died holding the lock, and at that point the button really is the only thing that can put an answer on the page.

---

### `POST /api/checks/refresh`

Re-runs every check immediately, ignoring the cache, and returns the refreshed strip. This is the `Check again` button: an operator who has just plugged the scanner back in should not have to wait out a TTL.

**Response:** HTML partial (the strip body, for `outerHTML` swap).

A failure inside the strip's own rendering comes back the way it does from [`GET /api/checks`](#get-apichecks): as the cold-start body at `200`, five named rows and the `Check again` button, with no poll attached. A failure in the re-run itself is an ordinary error response, shown in the status area, and the strip is left as it was.

During a scan, the checks that would touch the scanner are skipped -- an explicit click does not get to interrupt a scan in progress -- and the Scanner row keeps its "not checked while a scan is running" message. That decision is taken from the job the scan worker reports it is running, and not from whether some other health check happened to be busy at the same moment: when another check is holding the scanner briefly -- the capability read saneless does at start-up, for instance -- the row says the scanner was busy and names no scan at all.

Contention is not the only way to reach that busy row, and the reason is an ordering worth stating. Whether a scan is running is read *once*, before the checks run, and the worker records the job it is about to start *before* it takes the scanner. A scan that starts inside that window is therefore invisible to the sample, so the scanner check goes ahead, finds the scanner taken and produces the busy row -- while the line above the rows, which is composed after the checks have finished, has since seen the job and says the strip is paused during a scan. The two are not contradictory: nothing was checked either way, which is exactly what both of them say. The next refresh replaces the row with the ordinary paused one.

One case remains where that message outlives the scan that earned it. A skipped row stored *during* a real scan stays on the strip until the next probe replaces it, so for up to one refresh interval after a scan finishes the strip can still say a scan is running. Unlike a row produced by contention, that one was true when it was written; it is stale rather than wrong, and pressing `Check again` replaces it at once.

Simultaneous refreshes are collapsed into one. A request that arrives while a refresh is already under way -- another click, or the background refresh the page keeps warm -- re-renders the strip as it currently stands instead of running every check a second time. The answer the in-flight refresh is about to produce is the same answer, seconds away, and repeating the work would mean a second request to paperless-ngx and a second pair of write tests for the sake of it.

That answer is delivered without a second click. The strip returned by a collapsed refresh asks for itself once more, so the in-flight result appears on the page as soon as it lands -- pressing the button always produces an answer, even when the press happened to land on top of a refresh already running. The asking is bounded the same way the start-up poll is, and by the same pair of bounds: while the refresh it is waiting for is still running the strip keeps asking for about three minutes, and once nothing is running it stops within about twenty seconds. So a check wedged against an unreachable host cannot leave a tab asking forever.

A refresh collapsed this way also does not consume the floor described next. Nothing was probed, so nothing was spent, and the very next press is honoured immediately rather than refused as too soon.

There is also a floor under how often this can probe at all: a refresh is honoured at most once every couple of seconds. A request arriving sooner re-renders the current strip without probing -- the same status code and the same partial as an honoured one, because an early click is not an error and there is nothing to report about it. So holding the button down, or scripting the endpoint in a loop, cannot generate additional scanner or paperless-ngx traffic beyond that rate, and cannot delay a scan by contending for the scanner -- and neither can a loop that always collides with a running refresh, because a collapsed request performs no check of any kind. Waiting the couple of seconds out restores the full bypass: the button still ignores the cache's own, much longer freshness window, which is the whole reason it exists.

If the check registry itself fails, the previous results stay on the page rather than blanking, and the failure is logged.

---

### `GET /api/tags`

Fetches paperless-ngx tags for the tag picker, which is a checkbox list. Uses cached data when available. Every list route asks paperless-ngx with a 2-second connect and 5-second read budget, the same budget the status strip's paperless-ngx check uses, so a list answers within seconds even when paperless-ngx is slow or down.

If a refresh cannot reach paperless-ngx, the picker shows the last list fetched successfully, rather than an error; the cause is logged, and while paperless-ngx stays unreachable the refresh is retried, and the cause logged again, once per `paperless_cache_ttl_seconds` (60 by default).

If there has never been a list, the picker says the tags could not be loaded from paperless-ngx, and never that there are none: `No tags in paperless-ngx yet.` appears only when paperless-ngx answered with no tags. The failure is logged once and remembered for up to 15 seconds, or for `paperless_cache_ttl_seconds` if that is shorter. Requests in the meantime answer at once without asking paperless-ngx again, and the first request after that asks again. The ↻ refresh button asks again at once.

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | no | Filter text, at most 100 characters. Longer is rejected with `422` before any work. Filtering is done by saneless over the cached list -- `q` is never sent to paperless-ngx, and never appears in the response |
| `tags` | int[] | no | The tag ids currently ticked, at most 100, each from 1 to 2147483647; more, or an id outside that range, is rejected with `422`. They ride along so that a filtered re-render keeps your selection: a tag you ticked and then filtered out of view stays selected and is still submitted |

**Response:** HTML partial (the whole tag block including its wrapper, for `outerHTML` swap).

A ticked id that is not in the list still comes back ticked, so a submit never loses it. If
the list was fetched and the id is missing from it, the row reads, for example,
`tag 7 (no longer in paperless-ngx; will be skipped)`. If paperless-ngx could not be asked,
the row reads `tag 7`, with no claim either way: whether there is no earlier list, or the
page is showing the last list fetched successfully, which cannot show that a tag is gone.

---

### `GET /api/correspondents`

Fetches paperless-ngx correspondents for the dropdown selector. Uses cached data when available, with the same 2-second connect and 5-second read budget as `GET /api/tags`. If a refresh cannot reach paperless-ngx, the dropdown offers the last list fetched successfully, rather than an error. If there has never been one, it offers only `No correspondent`, and the help line under the dropdown says the correspondents could not be loaded from paperless-ngx. That failure is remembered and retried as described under `GET /api/tags`.

With `show_correspondent = false` in `[web]`, no route fetches correspondents: this one, the refresh, the profile change, `GET /api/metadata` and `GET /api/metadata/probe` all answer without asking paperless-ngx.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `correspondent` | int | no | The correspondent currently chosen, from 1 to 2147483647, which comes back selected; anything else is rejected with `422` before any work. An empty value, which the dropdown sends for `No correspondent`, means none. The page sends it when it fills in a list that could not be loaded, so the choice is kept |

**Response:** HTML partial (`<option>` elements for HTMX swap), followed by the help line under the dropdown (`#correspondent-help`) out of band, so the line always describes the list the dropdown holds.

---

### `GET /api/profiles/description`

Returns the one-sentence description of a scan profile, for the help line beneath the profile dropdown. The web UI fetches it when the selection changes.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile` | string | yes | The profile name. Validated against the configured profiles before anything else happens; an unknown name is rejected with `422` |

**Response:** the description text alone, with no wrapper element. The text comes from the loaded profile's `description` field, so a profile you took over and described yourself shows your wording rather than the generated one. A profile with no description returns nothing, and the help line is empty.

---

### `GET /api/profiles/multi-page`

Returns the Multiple pages checkbox, with its help line, for one scan profile. The web UI fetches it when the profile selection changes, so the checkbox is disabled for a manual-duplex profile and enabled for any other.

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile` | string | yes | The profile name. Validated against the configured profiles before anything else happens; an unknown name is rejected with `422` |
| `multi_page` | boolean | no | `on` when the checkbox was ticked before the change. A ticked box stays ticked when the new profile allows it |

**Responses:**

| Status Code | Meaning |
|-------------|---------|
| 200 | HTML partial: the checkbox and its wrapper, which replaces the one on the page. On a manual-duplex profile the checkbox is disabled and unticked, and the help line reads `Not available with manual duplex.` |
| 422 | The profile does not exist. |

The checkbox is never ticked when the page loads: Multiple pages is chosen for each scan.

---

### `GET /api/profiles/tags`

Returns the tag list with one profile's `default_tags` ticked. The web UI fetches it when
the profile selection changes, and it replaces whatever was ticked before: an untouched
form scans with the chosen profile's defaults, and a form you cleared scans with none. The
page itself opens with the first profile's defaults ticked in the same way.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile` | string | yes | The profile name. Validated against the configured profiles before anything else happens; an unknown name is rejected with `422` |

**Responses:**

| Status Code | Meaning |
|-------------|---------|
| 200 | HTML partial: the whole tag list including its wrapper, which replaces the one on the page, and an out-of-band `tags_profile` field naming the profile (see [`POST /api/scan`](#post-apiscan)). A default tag that paperless-ngx no longer has is ticked and labelled with a note, as described under `GET /api/tags`; leave it ticked and the finished job carries a warning that it was skipped, or untick it |
| 422 | The profile does not exist. |

---

### `GET /api/profiles/correspondent`

Returns the correspondent dropdown with one profile's `default_correspondent` selected, or
`No correspondent` when it has none. The web UI fetches it when the profile selection
changes, and it replaces the earlier choice, for the same reason as
`GET /api/profiles/tags`. A default correspondent that paperless-ngx no longer has is
selected and labelled, for example, `correspondent 12 (no longer in paperless-ngx; will be
skipped)`; one that cannot be checked because paperless-ngx is unreachable reads
`correspondent 12`.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile` | string | yes | The profile name. Validated against the configured profiles before anything else happens; an unknown name is rejected with `422` |

**Responses:**

| Status Code | Meaning |
|-------------|---------|
| 200 | HTML partial: the whole `<select>` element, which replaces the one on the page, an out-of-band `correspondent_profile` field naming the profile (see [`POST /api/scan`](#post-apiscan)), and the out-of-band help line, as under `GET /api/correspondents` |
| 422 | The profile does not exist. |

---

### `GET /api/metadata`

Renders both metadata lists, the help line under the correspondent dropdown and the Scan
button, in one answer. It is built for a page that renders at once and loads its lists
afterwards: one request, not one per list, because only the server knows when both lists
are done, and the Scan button is held until they are. Both lists are fetched with the
2-second connect and 5-second read budget described under `GET /api/tags`.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `profile` | string | no | The profile whose defaults to show |

The tag list comes back with the profile's `default_tags` ticked and the dropdown with its
`default_correspondent` selected, and both profile markers (`tags_profile`,
`correspondent_profile`) come back naming the profile. The page asks again whenever
Profile changes before it is answered, abandoning the request in flight. The markers are
always sent all the same, so whichever answer lands last leaves each list agreeing with its
marker (see [`POST /api/scan`](#post-apiscan)).

A request naming no configured profile (none, or a profile removed from the configuration
since the page rendered) gets the lists with nothing ticked or chosen and no marker, so
the page's markers still say the lists have not answered, and a scan submitted from it gets
the submitted profile's defaults. It is not refused: the loader polls, so an error would
land in the page's alert slot on every tick and hold the Scan button for good.

**Response:** `200` with an HTML body whose main part, empty, replaces the loader element
that asked, which removes it. A list that could not be loaded carries its own hidden retry,
inside the tag list or inside the help line under the dropdown, which asks
[`GET /api/metadata/probe`](#get-apimetadataprobe) 15 seconds after its last answer, or
`paperless_cache_ttl_seconds` after it if that is shorter, but never sooner than 5 seconds
after it. Each answer replaces the retry, so the wait starts when the answer lands. The
failure is forgotten that long after the failed request to paperless-ngx ended, so each
retry really asks paperless-ngx, unless another request has just asked it. The floor keeps
a short or disabled cache (`0`) from making every open page ask paperless-ngx once a
second while it is down. Whatever later replaces that list, such as a
profile change, a filter or a refresh, brings a retry of its own if the list still could
not be loaded, and none once it could.
The rest is out of band:

| Element | When |
|---------|------|
| The tag list (`#tags-list`), whole | When `show_tags` is on. A list that could not be loaded says so, and the ticked ids still come back ticked |
| The correspondent dropdown (`#correspondent-select`), whole, and its help line (`#correspondent-help`) | When `show_correspondent` is on. A list that could not be loaded says so in the help line |
| The profile markers | For each shown list, when the request names a configured profile |
| The Scan button (`#scan-btn`) | Always. It is disabled only while a scan is active or the appliance is blocked, exactly as on the page: a list that could not be loaded releases it just as a loaded one does. If the job store cannot be read, the failure is logged and the button is rendered as though no scan were active, still disabled on a blocked appliance; the status poll corrects it once it can read the job again. While the job store keeps failing, the poll shows that it cannot read the scan's progress and leaves the button as it is, and a scan started then is refused or queued as described under [`POST /api/scan`](#post-apiscan) |
| The Scan hold reason (`#scan-hold-reason`) | Emptied, except on a blocked appliance, whose page renders the blocked reason instead and has no hold reason to empty |

A hidden list is neither fetched nor rendered.

| Status Code | Meaning |
|-------------|---------|
| 200 | The HTML described above |

---

### `GET /api/metadata/probe`

Says whether a list that could not be loaded can be loaded now. The hidden retry that such
a list carries asks this (see [`GET /api/metadata`](#get-apimetadata)). It carries
nothing of the form, and its answer is a fresh copy of the retry, which replaces the retry
and nothing else, so a retry in flight can never put back a tick or a choice, nor a
profile's defaults after Profile has changed. The list is fetched through the cache with
the budget described under `GET /api/tags`.

**Query parameter:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `resource` | string | yes | The list to ask about: `tags` or `correspondents` |

| Status Code | Meaning |
|-------------|---------|
| 200 | The body is a fresh copy of the hidden retry, which replaces the one that asked, so the next retry comes one interval after this answer. If the list can be loaded, an `HX-Trigger` header also fires `tags-recovered` or `correspondents-recovered`. The page then asks [`GET /api/tags`](#get-apitags) or [`GET /api/correspondents`](#get-apicorrespondents) for that list, carrying the filter and the ticked tags, or the chosen correspondent, as they are at that moment. It drops that request if the list's own profile change is in flight, and abandons it if Profile changes while it is in flight, because the profile change renders the list afresh. Without the header, the list still cannot be loaded, and the retry asks again later |
| 204 | The list is hidden. Nothing is fetched, and no page renders a retry for it |
| 422 | Any other `resource` value, or none, before any work |

---

### `POST /api/cache/invalidate`

Invalidates a specific cache entry and returns fresh data from paperless-ngx.

If paperless-ngx cannot be reached, the response is built from the last list that was fetched successfully, and the failure is logged as a warning. If there has never been one, the response says the list could not be loaded, as `GET /api/tags` and `GET /api/correspondents` do, never that it is empty. A refresh clears the remembered failure, so it asks paperless-ngx again at once, within the floor below. The response does not mark a last good list as stale; the checks strip at the top of the page is what shows paperless-ngx as unreachable.

**Query parameter:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `resource` | string | Resource to invalidate: `tags` or `correspondents` |

**Response:** HTML partial with refreshed data. Any other `resource` value, or none, is rejected with `422` before the cache is touched. So is a form body carrying more than 100 `tags`, or a tag id outside 1 to 2147483647, the ticked tag ids a tag refresh sends so it can keep the selection. A correspondent refresh sends the chosen `correspondent` the same way, bounded the same way, and the refreshed options keep it selected.

At most one refetch per resource every 2 seconds; a sooner call returns the cached list. It
is answered with the same status code and the same partial as a call that refetched,
because an early click is not an error, and the list it gets is at most a couple of seconds
older than the one a refetch would have fetched. The two resources have separate floors, so
refreshing the tags does not delay a refresh of the correspondents.

---

### `GET /api/jobs/history`

Returns the job history table body (most recent 50 jobs).

**Response:** HTML partial (table rows for HTMX swap).

Every client gets every row, with its time, profile, outcome and page counts. Only the rows this browser started show their title; every other row shows `Scan (title hidden)`. See [who can read what](#what-an-unauthenticated-client-can-read).

---

### `POST /api/flip/continue`

Tells a manual duplex job waiting in `AWAITING_FLIP` that the stack has been flipped, so the worker starts pass B (back sides) and the job moves to `SCANNING_REVERSE`.

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | The id of the job the answer is for. The web UI's Continue button sends it automatically. A request without it is rejected with `422`. |

**Response:** HTML partial (the status indicator for HTMX swap), for the job named by `job_id`. An id that names no job falls back to the current job, else the most recent one. A call arriving just after the job ended reports that job, not the idle "Ready to scan." state. The partial's poll keeps following the job it answered, through [`GET /api/jobs/{job_id}/status`](#get-apijobsjob_idstatus), so the next poll cannot report another browser's scan in its place. The endpoint does not wait for pass B to start, so the partial shows whatever state the job has recorded at that moment; the one-second status poll picks up pass B from there.

While that job is still recorded `AWAITING_FLIP` and its flip wait has been answered -- by this call or by an earlier one -- the partial shows an acknowledgment instead of the Continue and Abort scan buttons: `Flip confirmed. Scanning reverse sides next...` after a Continue, `Aborting scan...` after an Abort.

---

### `POST /api/flip/abort`

Tells a manual duplex job waiting in `AWAITING_FLIP` to stop at the flip prompt. Pass B never starts, nothing is uploaded, and the job ends `CANCELLED` (not `ERROR`) with the message `Manual duplex scan cancelled at the flip prompt`. A job whose flip wait timed out still ends `ERROR`, and so does one whose flip wait was ended by the server shutting down, which is recorded with `The server restarted before this scan finished`.

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | The id of the job the answer is for. The web UI's Abort scan button sends it automatically. A request without it is rejected with `422`. |

**Response:** HTML partial (the status indicator for HTMX swap), for the job named by `job_id` and polling that job, as `/api/flip/continue` does, with the same acknowledgment while an answered job is still recorded `AWAITING_FLIP`.

---

#### How the two flip endpoints interact

- **The first answer is final.** A job waiting at the flip prompt accepts exactly one answer -- Continue, Abort, or the `operator_wait_timeout_seconds` timeout, whichever comes first -- and ignores everything after it. An Abort that arrives after a Continue is dropped: pass B carries on, and the endpoint returns the job's current status rather than an error. The same applies to a Continue after an Abort, and to either call after the wait has timed out.
- **An answer counts only for the named job, once it is waiting.** The worker accepts an answer only for the job named by `job_id`, and only once that job has reached `AWAITING_FLIP`. An answer sent during pass A, one naming a different job from the one at the flip prompt, or one arriving after the job ended is dropped: it changes nothing, and the endpoint still returns the current status rather than an error. A direct API caller should poll `/api/jobs/current/status` for `AWAITING_FLIP` before answering.
- **A repeated click cannot reach the next job.** Because every answer names its job, a double-clicked or retried Continue or Abort is dropped once the job it names has moved on, even when another manual duplex job is already queued behind it.
- **The prompt is acknowledged, then replaced.** After an accepted answer the partial shows the acknowledgment until the job leaves `AWAITING_FLIP`. Once pass B starts the status indicator shows `Scanning reverse sides...`.

---

### `POST /api/multi-page/answer`

Answers the question a multi-page scan is waiting on. A multi-page job asks one question after every pass, and waits in one of three states while it does:

| State | Question | Answers it offers |
|-------|----------|-------------------|
| `AWAITING_NEXT_PASS` | Is there another page? | `NEXT`, `FINISH`, `RESCAN`, `ABORT` (`FINISH` only once the document holds a page) |
| `AWAITING_BLANK_DECISION` | The pages just scanned look blank: keep them? | `SKIP_BLANKS`, `KEEP_BLANKS`, `RESCAN` |
| `AWAITING_RETRY` | The last scan failed: try it again? | `NEXT` (scan again), `FINISH`, `ABORT` |

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | The id of the job the answer is for. |
| `prompt` | integer, 1 or more | yes | The number of the question the answer is for. Every question a job asks has a new number, and the web UI's buttons send the number of the question they were drawn for. |
| `answer` | string | yes | One of `NEXT`, `RESCAN`, `FINISH`, `ABORT`, `SKIP_BLANKS` or `KEEP_BLANKS`. |

A missing field, a `prompt` below 1, or an `answer` that is not one of the answers above is rejected with `422` before anything reaches the worker.

**Response:** HTML partial (the status indicator for HTMX swap), for the job named by `job_id`, else the current job, else the most recent one. The partial's poll keeps following the job it answered. The endpoint does not wait for the next pass to start; the one-second status poll picks it up.

While the job is still recorded in its waiting state and the question has been answered, the partial shows an acknowledgment instead of the buttons, for example `Scanning more pages...` after `NEXT` or `Finishing the document...` after `FINISH`.

- **Only the browser that started the scan can answer.** An answer from any other browser is dropped, and so are its buttons: only the browser holding the job's owner cookie is shown the question at all. Every other browser sees one line saying what the job is waiting for.
- **An answer counts only for the question that is open.** It is dropped when it names another job, names a question that has already been answered or replaced, or is an answer that question does not offer, such as `FINISH` on a document that holds no page yet. A dropped answer changes nothing, and the endpoint still returns the current status rather than an error.
- **The first answer is final.** A repeated or double-clicked answer to the same question is dropped, so it cannot answer the next question too.
- **The scanner's error is for the owner only.** After a failed pass, the owner's question shows what the scanner reported, with host paths and addresses replaced by the setting that names them. No other browser is shown the error.

How the job ends depends on the answer, or on its absence:

- `FINISH` uploads the pages kept as one document, and the job ends `DONE`.
- `ABORT` uploads nothing and keeps nothing, and the job ends `CANCELLED`, not `ERROR`. The web UI's Abort scan button asks the browser to confirm first; the endpoint itself does not.
- A question nobody answers within `operator_wait_timeout_seconds` finishes the document with the pages kept, and the job ends `DONE` with a warning. If the document holds no page at that point, because every page so far was skipped as blank, nothing is uploaded and the job ends `ERROR`, the same failure as a scan whose every page looked blank.
- A document that holds 500 pages or more after a scan is finished without another question, and the job ends `DONE` with a warning. A scan already running is not cut short, so the document can hold up to 999 pages.
- A server stop while the job waits ends it `ERROR` with `The server restarted before this scan finished`, followed by where the pages kept were saved in `failed/`. Nothing is uploaded.

See [Scan a Multi-Page Document](../how-to/scan-a-multi-page-document.md) for the whole flow.

---

## Errors

Every error response saneless renders -- a refused scan, a validation failure, a refused Host, a blocked cross-site request, an unknown path, an unexpected server error -- has one of two body forms, chosen by the `HX-Request` request header. The status code is the same either way.

| Request | Body | Extra headers |
|---------|------|---------------|
| `HX-Request: true` (the web UI) | HTML fragment containing the message | `HX-Retarget: #status-message` and `HX-Reswap: innerHTML`, so the message appears in the page's message area instead of the element the request was aimed at |
| Anything else | `{"status": "error", "detail": "<message>"}` | none |

A `429` carries `Retry-After: 30` in both forms.

### A browser with JavaScript off

The web page submits the scan form through htmx. A browser with JavaScript off, or one whose htmx failed to load, submits the form by itself and shows the answer as a whole page. The form is a `POST`, so the title never appears in a URL, and saneless answers that submit with a page of its own rather than either form above:

- It applies to `POST /api/scan` when the request has no `HX-Request: true` header and its `Accept` header names `text/html`, which is what a browser sends when it submits a form.
- The answer is `400` with an HTML page titled `Scan not started — saneless`. It says scanning from the page needs JavaScript, and that `saneless scan` on the server works without it, and links back to `/`.
- No scan is started and nothing is recorded in job history. The check comes before every other one, so this is the answer even when a field is invalid or the appliance cannot scan.
- The page echoes nothing that was submitted. It carries the same [security headers](#security-headers) and `Cache-Control: no-store` as every other error.

A script or `curl` is unaffected: they send `Accept: */*`, or no `Accept` at all, and get the responses documented under [`POST /api/scan`](#post-apiscan).

A refused `POST /api/scan` from the web UI also carries the Scan button out-of-band, enabled and marked `autofocus`, so keyboard focus returns to the button that was pressed rather than being left on the page body. Focus is never moved into the message area. A refusal because the paperless-ngx token or address is unset carries no button: the button is disabled on such an appliance, so there is nothing to return focus to.

The message is a fixed sentence chosen by saneless for the kind of error. It never echoes request input or internal exception text. One refusal repeats one value beside the sentence: a `421` names the `Host` it refused, so you know which name to add to `[web] allowed_hosts`. The value is shown under Technical details in the HTML fragment and as a separate `"host"` field in the JSON form (`{"status": "error", "detail": "<message>", "host": "<host>"}`). Control characters in it are shown as visible escapes, and it is cut to at most 255 characters. The error from [`GET /api/paperless/test`](#get-apipaperlesstest) and the `503` bodies from [`GET /health`](#get-health) keep their own shapes, documented above.

### Every rejection

One sentence per kind of refusal, and every sentence is a fixed developer constant -- no request input, no exception text, no file path, no token value, no paperless-ngx URL. The name in the first column is saneless's internal identifier for the case; it does not appear in the response, and is listed so a log line or a bug report can name one unambiguously.

| Rejection | Status | Message |
|-----------|--------|---------|
| `QUEUE_FULL` | 429 | The scan queue is full. Wait for a scan to finish, then try again. |
| `WORKER_DOWN` | 503 | The scan service is not running, so the scan was not started. Restart saneless, then try again. |
| `WORKER_DEGRADED` | 503 | Job history cannot be saved right now, so the scan was not started. Check the server's free disk space and log, then try again. |
| `TOKEN_UNSET` | 503 | The paperless-ngx API token has not been set, so the scan was not started. Put a real API token in the saneless config file, then restart saneless. |
| `URL_UNSET` | 503 | The paperless-ngx address has not been set, so the scan was not started. Set paperless.url in the saneless config file, then restart saneless. |
| `UNKNOWN_PROFILE` | 422 | That scan profile does not exist. Reload the page to see the current profiles. |
| `MULTI_PAGE_MANUAL_DUPLEX` | 422 | Multiple pages is not available with manual duplex, so the scan was not started. Untick Multiple pages or choose another profile, then try again. |
| `TITLE_TOO_LONG` | 422 | The title is too long. Shorten it to 118 characters or fewer. |
| `TITLE_HAS_CONTROL` | 422 | The title contains a tab or another control character. Remove it, then try again. |
| `INVALID_REQUEST` | 422 | The request was not valid. Reload the page, then try again. |
| `CROSS_SITE` | 403 | This request was blocked because it did not come from the saneless page. If saneless is behind a reverse proxy, make sure the proxy passes the original Host header. |
| `NOT_FOUND` | 404 | That page or action does not exist. Reload the page, then try again. |
| `METHOD_NOT_ALLOWED` | 405 | That action is not allowed. Reload the page, then try again. |
| `INTERNAL` | 500 | Something went wrong on the server. Check the server log for details, then try again. |
| `CLIENT_ERROR` | 400 | The request could not be completed. Reload the page, then try again. |
| `HOST_NOT_ALLOWED` | 421 | saneless does not answer to this address. Add the host name you used to [web] allowed_hosts in the saneless config file, then restart saneless. |

**`TOKEN_UNSET` is new, and it is deliberately not `WORKER_DEGRADED`.** The scan service is working perfectly well; nobody set the paperless-ngx API token. Saying "the scan service was unavailable" would send a household member looking for a broken server, so the refusal says what is actually wrong and which file fixes it. A placeholder token counts as unset: saneless keeps a small fixed list of literals such as `changeme` and `your-api-token-here`, and an empty or whitespace-only token is the same case. See [Docker: placeholder tokens are detected](docker.md#placeholder-tokens-are-detected).

While the token is unset the Scan button also renders disabled with the reason beneath it, and the status strip's Paperless row is red. The button is a courtesy; the route guard is the enforcement, so a direct `POST /api/scan` is refused just the same. Like `QUEUE_FULL`, `WORKER_DOWN` and `WORKER_DEGRADED`, a `TOKEN_UNSET` refusal records the attempt in job history as a failed job -- `Not started: the paperless-ngx API token has not been set` -- so an attempt that never scanned is still visible.

**`URL_UNSET` is the same refusal for an empty `paperless.url`.** An empty address loads, so `serve` can start and the status strip's Paperless row can say `The paperless-ngx address has not been set.`, but an upload to it is certain to fail, and no copy is made to the consume directory for it. So the Scan button renders disabled with its own reason, `POST /api/scan` is refused, and the attempt is recorded as `Not started: the paperless-ngx address has not been set`. When the token is unset as well, `TOKEN_UNSET` is the one reported.

---

## Notes

- Most endpoints return **HTML partials** designed for HTMX swap. Only `/health` and `/api/paperless/test` return JSON, along with the JSON error form described under [Errors](#errors).
- There is no authentication on the API. saneless assumes a trusted LAN; use a reverse proxy for auth if needed. The web server binds to `web_host`, which defaults to `0.0.0.0` -- all network interfaces -- so every host that can reach the port can use the API.
- `POST /api/scan` returns once the job is queued or refused; it does not wait for the scan. Poll `/api/jobs/current/status` for progress.

### What an unauthenticated client can read

Anything that can reach the port can read, without logging in:

- the queue state: whether a scan is running or waiting, and how many are ahead;
- each job's outcome, error category and page counts;
- profile names and their descriptions;
- `Scan (title hidden)` as the title of every scan another browser started;
- the paperless-ngx tag and correspondent lists, because the scan form needs them.

Only the browser that started a scan sees its title, its preview and the name of a kept PDF. saneless recognises that browser by its `saneless_owner` cookie. The cookie is set when the browser submits a scan and lasts a year. The kept PDF is named relative to the data directory, as `failed/<file>.pdf`. A job that recorded no owner is nobody's: every browser sees its generic title, although anyone may still answer its flip prompt. Jobs written before saneless recorded owners are like this, and so are most refused submits.

Error and warning text on the web never carries a host path or the paperless-ngx URL. The owner sees the text with each directory and web address replaced by the name of its setting, such as `<output.tmp_dir>` or `<paperless.url>`, and any other absolute path, such as a device node, replaced by `<path>`. Every other browser sees a fixed sentence instead, which says where the full text is. On the server, the log and `saneless jobs --json` keep the full text.

The cookie is not a password. A client that sends a guessed or copied cookie is treated as its owner. saneless assumes a trusted LAN, and a reverse proxy with authentication is the answer where that is not enough.

### Host check

saneless answers a request only when its `Host` header names saneless. The check covers every request, of any method, including `GET /health` and the static files, and it runs before any other check. Without configuration, saneless answers to:

- IP addresses: dotted IPv4 such as `192.168.1.5`, and IPv6 in brackets such as `[::1]`;
- `localhost`, and any other name without a dot, such as a Docker Compose service name;
- names ending in `.local`, `.home.arpa`, `.internal` or `.lan`.

The port is ignored, case does not matter, and one trailing dot is dropped. To reach saneless by any other name, such as a reverse proxy's public name, add it to [`[web] allowed_hosts`](configuration.md#web). The list adds to the names above and never replaces them, so `http://<lan-ip>:8080` always keeps working.

Only `Host` decides. `X-Forwarded-Host` is never trusted, so a trusted value there does not rescue a request whose `Host` is refused.

A reverse proxy must therefore pass the browser's original `Host`. One that replaces it with its upstream's name, such as `saneless:8080`, sends a name saneless always answers to, which turns the Host check off for every request through the proxy. saneless answers those requests as usual, but when a trusted `Host` arrives beside an `X-Forwarded-Host` naming a different host, it logs a warning naming both, at most once an hour. Any client can send that pair of headers, so the warning describes what arrived rather than asserting that a proxy exists.

A request whose `Host` is a well-formed name that saneless does not answer to gets `421` with the `HOST_NOT_ALLOWED` sentence and the refused `Host` beside it (see [Errors](#errors)). The server logs one warning per refusal naming the `Host`, the method and the path. A request with no `Host`, an empty one, two of them, or one that is not a valid host name gets `400` with the `CLIENT_ERROR` sentence.

### Cross-site requests

saneless rejects state-changing requests that did not come from a saneless page, so a web page on another site cannot start a scan or answer a flip prompt in your browser. `GET`, `HEAD` and `OPTIONS` requests are never checked; every other request is checked as follows:

- If the browser sends `Sec-Fetch-Site`, the values `same-origin` and `none` are allowed and anything else (`same-site`, `cross-site`) is rejected. A paperless-ngx page on the same host but another port counts as `same-site` and is rejected.
- Otherwise, if the request has an `Origin` header, its host and port must equal the `Host` header or one of the entries in `X-Forwarded-Host`.
- A request with neither header -- `curl`, scripts, other non-browser clients -- is allowed.

A rejected request gets `403` with the message "This request was blocked because it did not come from the saneless page. If saneless is behind a reverse proxy, make sure the proxy passes the original Host header.", and the server logs a warning that names the `Origin`, `Host`, `X-Forwarded-Host` and `Sec-Fetch-Site` values it saw.

Browsers do not send `Sec-Fetch-Site` to a plain-HTTP address such as `http://<lan-ip>:8080`, and browsers without Fetch Metadata support (Safari before 16.4, for example) do not send it at all, so those requests are checked by `Origin` against `Host`. A reverse proxy in front of saneless must therefore preserve the original `Host` header (nginx: `proxy_set_header Host $host;`), over HTTPS as well as plain HTTP; otherwise every scan from such a browser is rejected unless the proxy sets `X-Forwarded-Host`, and even then the [Host check](#host-check) cannot refuse DNS rebinding through the proxy. See [Running behind a reverse proxy](../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy).

**The Host check is what stops DNS rebinding.** A hostile site can make its own hostname resolve first to its server and then to saneless's LAN address. The browser then treats the hostile page and saneless as the same origin, so the `Origin` and `Host` headers agree and the cross-site check above passes. Before the Host check existed, such a page could read job titles, scan previews and your paperless-ngx tag and correspondent names, as well as start scans. Its requests still carry the hostile hostname in `Host`, though, and the Host check refuses them with `421` before they reach anything else.

That protection holds only while `[web] allowed_hosts` names only hosts you control. Adding a name tells saneless to answer to it; there is no wildcard, so a single entry cannot turn the check off. Two further measures remain useful as defence in depth:

- Put saneless behind a reverse proxy that answers only requests for its own hostname, and make saneless itself reachable only by the proxy (for example, set `web_host` to an address only the proxy can reach). See [Answering only your own hostname](../how-to/deploy-docker-compose.md#answering-only-your-own-hostname).
- Use a DNS resolver with rebind protection, which refuses to return private addresses for public hostnames. Many home routers offer this, as do dnsmasq (`--stop-dns-rebind`) and Pi-hole.

### Security headers

Every response carries these three headers, whatever its status: pages, partials, static files, and every error, a `500` included.

| Header | Value |
|--------|-------|
| `Content-Security-Policy` | `default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |

- **No page may be framed.** `frame-ancestors 'none'` stops a page on another site from showing saneless inside a frame and tricking you into clicking Scan or a flip answer. Such a click would really be yours, so the cross-site check above could not tell it apart. `X-Frame-Options: DENY` says the same to browsers too old to read `frame-ancestors`.
- **No inline script or style runs anywhere.** Scripts, stylesheets and connections must come from saneless itself. saneless's pages contain no inline script, `<style>` element or `style` attribute. The htmx configuration in every page also turns off htmx's own injected style, its `eval` and its handling of swapped-in `<script>` elements. If markup were ever injected into a response, htmx could not be used to run it.
- **Images may be `data:` URIs.** The icons in Pico's form controls and the scan preview thumbnail are both sent as `data:` images.
- `base-uri 'none'` and `form-action 'self'` stop injected markup from changing where relative links resolve or where a form is sent.
- `nosniff` stops a browser from guessing a content type the server did not send.

A reverse proxy may add a policy of its own. It cannot loosen this one, because a browser enforces every `Content-Security-Policy` it receives.

**Nothing but a static file may be cached.** Every response except the files under `/static/` also carries `Cache-Control: no-store`, errors included. The page, the status poll and Job History differ by browser: the browser that started a job sees its real title and thumbnail, and every other browser sees a generic title (see [What an unauthenticated client can read](#what-an-unauthenticated-client-can-read)). A caching reverse proxy that kept the owner's copy could otherwise serve it to anyone. The static files are the same for everyone and stay cacheable.

### No API schema or interactive documentation

**saneless serves no generated API schema and no interactive API documentation.** `GET /openapi.json`, `GET /docs` and `GET /redoc` answer `404` exactly like any unknown path, with the error body described under [Errors](#errors).

This is deliberate. saneless is an appliance on a trusted LAN that serves an HTMX UI, and its endpoints are the UI's own. Removing the schema is not a security control: the page at `GET /` and the fragments it loads name the endpoints the UI calls, this page lists all of them, and the API has no authentication. See [Cross-site requests](#cross-site-requests) for what does and does not protect it. The generated schema also never worked: the route handlers' `Response` return annotation was imported for type checking only, so the framework could not resolve it and the schema endpoint answered `500`. That annotation now resolves, but the endpoints stay off. Even generated correctly, the schema would describe the HTML-fragment endpoints as returning JSON, and nothing in the app, its documentation or its tests consumes a schema.

This page -- the endpoint table and the per-endpoint sections above -- is the reference for the API.
