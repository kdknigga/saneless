---
phase: 26-worker-and-web-robustness
plan: 14
subsystem: docs
tags: [docs, web-api, architecture, reverse-proxy, health, ui-spec]
requires:
  - phase: 26-07
  - phase: 26-08
  - phase: 26-09
  - phase: 26-10
  - phase: 26-11
provides:
  - "web-api.md: degraded /health, POST /api/scan responses (403/422/429 + Retry-After/503), Errors section, Cross-site requests section, 0.0.0.0 bind default"
  - "architecture.md: threadpool routes, bounded queue, startup recovery, degraded health and self-healing probe, bounded shutdown, no app JavaScript"
  - "deploy-docker-compose.md: 'Running behind a reverse proxy' section (anchor running-behind-a-reverse-proxy)"
  - "configure-scan-profiles.md: generation at server startup"
  - ".planning/UI-SPEC.md updated for Phase 26 (all eight edits)"
affects: [DOCS-05, CFG-11, APPL-08]
tech-stack:
  added: []
  patterns: ["every doc claim checked against source at base 45e18b9 or an external primary source"]
key-files:
  created: []
  modified:
    - docs/reference/web-api.md
    - docs/explanation/architecture.md
    - docs/reference/configuration.md
    - docs/reference/environment-variables.md
    - docs/reference/cli-commands.md
    - docs/reference/docker.md
    - docs/how-to/deploy-docker-compose.md
    - docs/how-to/configure-scan-profiles.md
    - .planning/UI-SPEC.md
decisions:
  - "Referrer-Policy: no-referrer / Origin: null caveat (A4) left out: MDN says cors-mode requests are unaffected, and htmx 2.0.8 sends its requests with XMLHttpRequest, so the caveat does not apply to the web UI"
  - "Traefik claim narrowed to what its docs confirm (passHostHeader defaults to true); its X-Forwarded-Host default was not confirmed and is not stated"
  - "nginx example keeps proxy_set_header Host $host; plus a note to use $http_host when the proxy listens on a non-default port, since $host carries no port"
metrics:
  duration: "about 40 min"
  completed: 2026-09-14
requirements: [ROBU-10, ROBU-02, ROBU-05, ROBU-06, ROBU-07]
---

# Phase 26 Plan 14: Docs corrected in-phase Summary

The docs now describe how the server actually behaves after Phase 26. The web API reference covers the new error statuses and the degraded `/health` state, and it explains the cross-site request check along with the `0.0.0.0` bind default. The architecture page covers the threadpool, bounded shutdown, degraded mode and crash recovery. The operator pages cover the bind default, healthcheck meaning, reverse-proxy setup and profile generation at startup. The master UI-SPEC now matches the UI that ships.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Web API reference and architecture explanation | 5018bfd | docs/reference/web-api.md, docs/explanation/architecture.md |
| 2 | Bind address, health, reverse proxy, startup profiles | ed158c0 | configuration.md, environment-variables.md, cli-commands.md, docker.md, deploy-docker-compose.md, configure-scan-profiles.md |
| 3 | Master UI-SPEC catches up with Phase 26 | f20b881 | .planning/UI-SPEC.md |

## What changed

- **web-api.md.** `/health` gained a third row for `job store failing`, plus a note on when degraded starts and when it clears (three loop failures in a row, or a store write failure at startup; cleared by a successful probe every 5 s while idle). `POST /api/scan` gained a responses table. A 422 creates no job; a 429 (with `Retry-After: 30`) or a 503 is recorded in history as a failed job when the store accepts the write. `POST /api/cache/invalidate` now lists its 422. There is a new Errors section covering the `HX-Request` body forms, the retarget headers, identical status codes and messages that never echo input. The old note row 23 flagged ("returns immediately after queuing") is replaced. A new "Cross-site requests" section links to the proxy how-to, and the Notes state the `0.0.0.0` default.
- **architecture.md.** The "fully responsive" claim (doc row 26) is replaced by the real mechanism: plain `def` routes run on FastAPI's threadpool, and a full queue refuses work instead of blocking. New Startup, Failure handling and Shutdown paragraphs follow. The `app.js` sentence is replaced: no app JavaScript, a server-rendered Scan button re-rendered out of band, and vendored assets pinned with `integrity`.
- **Operator pages.**
  - `web_host` / `--host` / `SANELESS_OUTPUT__WEB_HOST` now say that `0.0.0.0` means all network interfaces.
  - `auto-profiles` names where it writes.
  - docker.md explains the 503 meanings and how restart policies behave.
  - deploy-docker-compose.md has a new reverse-proxy section.
  - configure-scan-profiles.md has a new "Generation at server startup" subsection. It covers: loaded file gets the profiles; no file means this run only; an unwritable file, such as the `:ro` mount in the compose examples, means this run only with a warning.
- **UI-SPEC.md.** All eight edits from 26-UI-SPEC "Master UI-SPEC Edits Required" are applied. Where old text described the removed behaviour as current, it was also fixed: the component count, the Title input `maxlength`, the `.status-error` locations, the busy CTA copy row, the form-semantics line and the Color note about the CDN build.

## External verification sources (T-26-59)

- **A5, Docker does not restart an unhealthy container. Verified and documented.** Docker's restart-policy table defines every policy by the container exiting or stopping (https://docs.docker.com/engine/containers/start-containers-automatically/). The HEALTHCHECK reference describes `unhealthy` only as a health status (https://docs.docker.com/reference/dockerfile/#healthcheck). Corroborated by docker/compose#4826 ("restart policies only take effect based on the exit code") and moby/moby#28400 (feature request to restart on unhealthy).
- **Caddy. Verified and documented.** "By default, Caddy passes through incoming headers—including `Host`—to the backend", and "The `X-Forwarded-Host` header is still passed by default" (https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
- **Traefik. Partly verified; only the verified part is documented.** "By default, passHostHeader is true" (https://doc.traefik.io/traefik/routing/services). Whether Traefik sets `X-Forwarded-Host` by default was not confirmed, so the docs do not say it.
- **nginx. Verified.** Its default is `proxy_set_header Host $proxy_host`. `$host` is the server name, and `$http_host` is the unchanged `Host` header (https://nginx.org/en/docs/http/ngx_http_proxy_module.html). This is the basis for the `$http_host` non-default-port note.
- **A4, `Referrer-Policy: no-referrer` makes `Origin: null`. Left out.** MDN (https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Referrer-Policy) says requests in `cors` mode are never affected, so the null Origin mainly hits native form submissions. saneless's POSTs go through htmx 2.0.8, which uses `XMLHttpRequest`: `new XMLHttpRequest` is present in the vendored file and `fetch(` is not. The caveat as worded does not apply to the web UI.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Accuracy] Traefik wording narrowed.** The plan said "Traefik and Caddy set X-Forwarded-Host by default". Only Caddy's X-Forwarded-Host default and Traefik's `passHostHeader=true` could be confirmed, so the docs say exactly those two things. Commit ed158c0.

**2. [Rule 2 - Correctness] nginx `$http_host` note added.** `$host` carries no port, so a plain-HTTP proxy on a non-80 port would still fail the Origin-vs-Host check. The nginx docs confirm this. The required `proxy_set_header Host $host;` line is kept once. Commit ed158c0.

**3. [Rule 1 - Accuracy] Extra stale UI-SPEC statements fixed.** These fall outside the eight listed edits but describe removed or changed behaviour as current: component count 18/7 is now 19/10, the busy-CTA copy row, the "CDN build linked by base.html" note, the form-semantics line and the `app.css` line count. Commit f20b881.

## Deferred Issues

- `.planning/UI-SPEC.md` "Known copy inconsistencies" #1 still says the correspondent "none" option reads `-- None --` after hydration. At base 45e18b9, both `index.html` and `partials/correspondents.html` render `No correspondent`, so it looks already resolved. This is not one of the eight Phase 26 edits, so it was left alone for a later UI-SPEC pass.

## Known Stubs

None.

## Verification

- Task 1 verify: the forbidden phrases are gone, and `Retry-After`, `job store failing`, `X-Forwarded-Host` and `0.0.0.0` are present in web-api.md. `uv run prek run --all-files` passed.
- Task 2 verify: all greps passed. `0.0.0.0` matches in configuration.md, environment-variables.md, cli-commands.md and web-api.md. `## Running behind a reverse proxy` and `proxy_set_header Host $host;` each match once. prek passed.
- Task 3 verify: no `app.js`, `cdn.jsdelivr` or `SRI not currently applied`. `status-message` matches 11 times and `sha384-` twice, and `scan_button.html` and `0.0625rem` are present. The inconsistency #2 line contains "resolved".
- `git diff --stat 45e18b9 HEAD -- docs/getting-started` is empty (DOCS-05 scope respected).

## Self-Check: PASSED

- FOUND: docs/reference/web-api.md, docs/explanation/architecture.md, docs/how-to/deploy-docker-compose.md, docs/how-to/configure-scan-profiles.md, .planning/UI-SPEC.md
- FOUND commits: 5018bfd, ed158c0, f20b881
