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
| 200 | `{"status": "ok"}` | Worker thread is alive |
| 503 | `{"status": "error", "detail": "worker thread is down"}` | Worker thread has stopped |

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
| `title` | string | no | Document title (auto-generated from timestamp if empty) |
| `tags` | int[] | no | Paperless-ngx tag IDs |
| `correspondent` | int | no | Paperless-ngx correspondent ID |

**Response:** HTML partial (status indicator for HTMX swap).

---

### `GET /api/jobs/current/status`

Returns the current or most recent job status. Used by HTMX polling to update the status indicator.

**Response:** HTML partial with job state. Possible states: `PENDING`, `SCANNING`, `AWAITING_FLIP`, `SCANNING_REVERSE`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`, `FALLBACK`.

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

**Response:** HTML partial with refreshed data.

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

Tells a manual duplex job waiting in `AWAITING_FLIP` to stop at the flip prompt. Pass B never starts, nothing is uploaded, and the job ends `ERROR` with `Manual duplex scan aborted at the flip prompt`.

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

## Notes

- Most endpoints return **HTML partials** designed for HTMX swap. Only `/health` and `/api/paperless/test` return JSON.
- There is no authentication on the API. saneless assumes a trusted LAN; use a reverse proxy for auth if needed.
- The `/api/scan` endpoint returns immediately after queuing the job. Poll `/api/jobs/current/status` for progress updates.
