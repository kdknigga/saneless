# Web API

saneless exposes a web API at the configured host and port (default `0.0.0.0:8080`). The web UI uses these endpoints via HTMX. They can also be called directly for integration.

## Endpoint Overview

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Web UI main page |
| GET | `/health` | Health check (no auth) |
| GET | `/api/paperless/test` | Test paperless-ngx connection |
| POST | `/api/scan` | Start a scan job |
| GET | `/api/jobs/current/status` | Poll current job status |
| GET | `/api/tags` | Fetch paperless-ngx tags |
| GET | `/api/correspondents` | Fetch paperless-ngx correspondents |
| POST | `/api/cache/invalidate` | Refresh cached metadata |
| GET | `/api/jobs/history` | Job history table |
| POST | `/api/flip/continue` | Continue manual duplex scan |
| POST | `/api/flip/abort` | Abort manual duplex scan |

---

## Endpoint Details

### `GET /`

Renders the main web UI page with scan form, live status indicator, and job history table.

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

The worker becomes degraded after three job-store failures in a row (its own job writes or its periodic history prune), when a job record it could not write is still failing to write for three idle ticks in a row (about 15 seconds), or at startup when the job store cannot be written. While degraded, new scans are refused with `503`. Degraded clears itself: while no scan is running, the worker checks the job store every 5 seconds, and the first check in which the job store accepts writes again, including any job records it still owes, returns `/health` to `200`.

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
| 502 | `{"status": "error", "detail": "..."}` | Unexpected failure inside saneless while running the test |

The five 200 values are the complete set, and each is a stable wire contract: `connected`
is returned for a 2xx and nothing else, so a 404 or a 500 is now reported as its own
outcome rather than as a working connection.

---

### `POST /api/scan`

Starts a new scan job. Accepts form data (designed for HTMX form submission).

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `profile` | string | yes | Scan profile name from config |
| `title` | string | no | Document title, at most 256 characters (auto-generated from timestamp if empty) |
| `tags` | int[] | no | Paperless-ngx tag IDs |
| `correspondent` | int | no | Paperless-ngx correspondent ID |

**Responses:**

| Status Code | Meaning |
|-------------|---------|
| 200 | The job is queued. HTML partial: the status indicator for HTMX swap, plus an out-of-band Scan button and an out-of-band clear of any earlier error message. |
| 403 | The request was blocked as cross-site. See [Cross-site requests](#cross-site-requests). |
| 422 | The request is not valid: the profile does not exist, the title is longer than 256 characters, or a required field is missing. No job is created. |
| 429 | The scan queue is full: 10 jobs are already waiting to start. The response carries `Retry-After: 30`. |
| 503 | The worker is not running, or it is degraded (see [`GET /health`](#get-health)). |

A `429` or `503` is a refused attempt, not a missing one: it is recorded in job history as a failed job ("Not started: ..."), provided the job store accepts the write. A `422` records nothing. Error bodies follow [Errors](#errors).

---

### `GET /api/jobs/current/status`

Returns the current or most recent job status. Used by HTMX polling to update the status indicator.

**Response:** HTML partial with job state. Possible states: `PENDING`, `SCANNING`, `AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`, `FALLBACK`, `CANCELLED`.

`DONE`, `ERROR`, `FALLBACK` and `CANCELLED` are terminal: once the job reaches one of them the partial stops polling and the Scan button is enabled again. `CANCELLED` means the operator stopped the scan on purpose, such as with Abort scan at the flip prompt. The web UI shows it as `Cancelled: <title>` in muted grey, not as an error.

---

### `GET /api/tags`

Fetches paperless-ngx tags for the dropdown selector. Uses cached data when available.

**Response:** HTML partial (`<option>` elements for HTMX swap).

---

### `GET /api/correspondents`

Fetches paperless-ngx correspondents for the dropdown selector. Uses cached data when available.

**Response:** HTML partial (`<option>` elements for HTMX swap).

---

### `POST /api/cache/invalidate`

Invalidates a specific cache entry and returns fresh data from paperless-ngx.

**Query parameter:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `resource` | string | Resource to invalidate: `tags` or `correspondents` |

**Response:** HTML partial with refreshed data. Any other `resource` value, or none, is rejected with `422` before the cache is touched.

---

### `GET /api/jobs/history`

Returns the job history table body (most recent 50 jobs).

**Response:** HTML partial (table rows for HTMX swap).

---

### `POST /api/flip/continue`

Tells a manual duplex job waiting in `AWAITING_FLIP` that the stack has been flipped, so the worker starts pass B (back sides) and the job moves to `SCANNING_REVERSE`.

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | The id of the job the answer is for. The web UI's Continue button sends it automatically. A request without it is rejected with `422`. |

**Response:** HTML partial (the status indicator for HTMX swap), for the current job, else the most recent one. A call arriving just after the job ended reports that job, not the idle "Ready to scan." state. The endpoint does not wait for pass B to start, so the partial shows whatever state the job has recorded at that moment; the one-second status poll picks up pass B from there.

While that job is still recorded `AWAITING_FLIP` and its flip wait has been answered -- by this call or by an earlier one -- the partial shows an acknowledgment instead of the Continue and Abort scan buttons: `Flip confirmed. Scanning reverse sides next...` after a Continue, `Aborting scan...` after an Abort.

---

### `POST /api/flip/abort`

Tells a manual duplex job waiting in `AWAITING_FLIP` to stop at the flip prompt. Pass B never starts, nothing is uploaded, and the job ends `CANCELLED` (not `ERROR`) with the message `Manual duplex scan cancelled at the flip prompt`. A job whose flip wait timed out still ends `ERROR`, and so does one whose flip wait was ended by the server shutting down, which is recorded with `The server restarted before this scan finished`.

**Request fields (form-encoded):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job_id` | string | yes | The id of the job the answer is for. The web UI's Abort scan button sends it automatically. A request without it is rejected with `422`. |

**Response:** HTML partial (the status indicator for HTMX swap), for the current job, else the most recent one, with the same acknowledgment as `/api/flip/continue` while an answered job is still recorded `AWAITING_FLIP`.

---

#### How the two flip endpoints interact

- **The first answer is final.** A job waiting at the flip prompt accepts exactly one answer -- Continue, Abort, or the `flip_timeout_seconds` timeout, whichever comes first -- and ignores everything after it. An Abort that arrives after a Continue is dropped: pass B carries on, and the endpoint returns the job's current status rather than an error. The same applies to a Continue after an Abort, and to either call after the wait has timed out.
- **An answer counts only for the named job, once it is waiting.** The worker accepts an answer only for the job named by `job_id`, and only once that job has reached `AWAITING_FLIP`. An answer sent during pass A, one naming a different job from the one at the flip prompt, or one arriving after the job ended is dropped: it changes nothing, and the endpoint still returns the current status rather than an error. A direct API caller should poll `/api/jobs/current/status` for `AWAITING_FLIP` before answering.
- **A repeated click cannot reach the next job.** Because every answer names its job, a double-clicked or retried Continue or Abort is dropped once the job it names has moved on, even when another manual duplex job is already queued behind it.
- **The prompt is acknowledged, then replaced.** After an accepted answer the partial shows the acknowledgment until the job leaves `AWAITING_FLIP`. Once pass B starts the status indicator shows `Scanning reverse sides...`.

---

## Errors

Every error response saneless renders -- a refused scan, a validation failure, a blocked cross-site request, an unknown path, an unexpected server error -- has one of two body forms, chosen by the `HX-Request` request header. The status code is the same either way.

| Request | Body | Extra headers |
|---------|------|---------------|
| `HX-Request: true` (the web UI) | HTML fragment containing the message | `HX-Retarget: #status-message` and `HX-Reswap: innerHTML`, so the message appears in the page's message area instead of the element the request was aimed at |
| Anything else | `{"status": "error", "detail": "<message>"}` | none |

A `429` carries `Retry-After: 30` in both forms.

The message is a fixed sentence chosen by saneless for the kind of error. It never echoes request input or internal exception text. The error from [`GET /api/paperless/test`](#get-apipaperlesstest) and the `503` bodies from [`GET /health`](#get-health) keep their own shapes, documented above.

---

## Notes

- Most endpoints return **HTML partials** designed for HTMX swap. Only `/health` and `/api/paperless/test` return JSON, along with the JSON error form described under [Errors](#errors).
- There is no authentication on the API. saneless assumes a trusted LAN; use a reverse proxy for auth if needed. The web server binds to `web_host`, which defaults to `0.0.0.0` -- all network interfaces -- so every host that can reach the port can use the API.
- `POST /api/scan` returns once the job is queued or refused; it does not wait for the scan. Poll `/api/jobs/current/status` for progress.

### Cross-site requests

saneless rejects state-changing requests that did not come from a saneless page, so a web page on another site cannot start a scan or answer a flip prompt in your browser. `GET`, `HEAD` and `OPTIONS` requests are never checked; every other request is checked as follows:

- If the browser sends `Sec-Fetch-Site`, the values `same-origin` and `none` are allowed and anything else (`same-site`, `cross-site`) is rejected. A paperless-ngx page on the same host but another port counts as `same-site` and is rejected.
- Otherwise, if the request has an `Origin` header, its host and port must equal the `Host` header or one of the entries in `X-Forwarded-Host`.
- A request with neither header -- `curl`, scripts, other non-browser clients -- is allowed.

A rejected request gets `403` with the message "This request was blocked because it did not come from the saneless page. If saneless is behind a reverse proxy, make sure the proxy passes the original Host header.", and the server logs a warning that names the `Origin`, `Host`, `X-Forwarded-Host` and `Sec-Fetch-Site` values it saw.

Browsers do not send `Sec-Fetch-Site` to a plain-HTTP address such as `http://<lan-ip>:8080`, and browsers without Fetch Metadata support (Safari before 16.4, for example) do not send it at all, so those requests are checked by `Origin` against `Host`. A reverse proxy in front of saneless should therefore preserve the original `Host` header (nginx: `proxy_set_header Host $host;`) or set `X-Forwarded-Host`, over HTTPS as well as plain HTTP; otherwise every scan from such a browser is rejected. See [Running behind a reverse proxy](../how-to/deploy-docker-compose.md#running-behind-a-reverse-proxy).

**This check does not stop DNS rebinding.** It compares the page's origin with the address the browser sent the request to, and it cannot tell whether that address really is saneless. A hostile site can make its own hostname resolve first to its server and then to saneless's LAN address. The browser then treats the hostile page and saneless as the same origin, the `Origin` and `Host` headers agree, and the page's requests pass the check. Because the API has no authentication, such a page can start scans. To close this gap, do one of the following:

- Put saneless behind a reverse proxy that answers only requests for its own hostname, and make saneless itself reachable only by the proxy (for example, set `web_host` to an address only the proxy can reach). A rebinding page's requests carry the hostile hostname in `Host`, so the proxy refuses them. See [Answering only your own hostname](../how-to/deploy-docker-compose.md#answering-only-your-own-hostname).
- Use a DNS resolver with rebind protection, which refuses to return private addresses for public hostnames. Many home routers offer this, as do dnsmasq (`--stop-dns-rebind`) and Pi-hole.
