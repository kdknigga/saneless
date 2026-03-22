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
| 200 | `{"status": "connected"}` | Successful connection with valid token |
| 200 | `{"status": "token_rejected"}` | Server reachable but token is invalid |
| 200 | `{"status": "unreachable"}` | Server is not reachable |
| 502 | `{"status": "error", "detail": "..."}` | Unexpected failure |

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

**Response:** HTML partial with job state. Possible states: `PENDING`, `SCANNING`, `AWAITING_FLIP`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`.

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

Signals the worker to continue with pass B (back sides) of a manual duplex scan. Only meaningful when the current job is in `AWAITING_FLIP` state.

**Response:** HTML partial with updated job status.

---

### `POST /api/flip/abort`

Signals the worker to abort the current manual duplex scan. Cancels the job when in `AWAITING_FLIP` state.

**Response:** HTML partial with updated job status.

---

## Notes

- Most endpoints return **HTML partials** designed for HTMX swap. Only `/health` and `/api/paperless/test` return JSON.
- There is no authentication on the API. saneless assumes a trusted LAN; use a reverse proxy for auth if needed.
- The `/api/scan` endpoint returns immediately after queuing the job. Poll `/api/jobs/current/status` for progress updates.
