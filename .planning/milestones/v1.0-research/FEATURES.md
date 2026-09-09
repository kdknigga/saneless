# Feature Landscape

**Domain:** Scanner-to-document-management bridge (SANE to paperless-ngx)
**Researched:** 2026-03-20
**Competitors surveyed:** scanservjs, NAPS2, VueScan, HP Smart, Brother iPrint&Scan, paperless-ngx consume folder workflow

## Table Stakes

Features users expect. Missing = product feels incomplete or users fall back to existing tools.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Flatbed single-page scan | Every scanner tool does this; it is the baseline | Low | Simplest scan path, proves the pipeline end-to-end |
| ADF multi-page scan | Users with document feeders expect batch scanning; scanservjs and NAPS2 both support it | Medium | Requires `multi_scan()` iterator, page accumulation, error handling for empty feeder |
| PDF output | PDF is the universal document format; every competitor outputs PDF | Low | `img2pdf` handles this losslessly |
| Resolution and color mode selection | Every scanner tool exposes these; hiding them makes the tool feel crippled | Low | Exposed via scan profiles rather than raw controls |
| Scan profiles / presets | NAPS2, VueScan, scanservjs all offer saved settings; users scan the same document types repeatedly | Low | TOML-defined profiles, dropdown in UI. Table stakes because without them users must reconfigure every scan |
| Web-accessible UI | scanservjs's entire value proposition; HP Smart and Brother apps are app-based. A CLI-only tool is a non-starter for household members | Medium | FastAPI + simple frontend. Must work on phone browsers too |
| Live scan status / progress | Every scan app shows progress; a scan takes 10-60 seconds and users need feedback | Low | Polling endpoint, status enum (scanning/assembling/uploading/done/error) |
| Paperless-ngx API ingestion | This is saneless's core purpose; without it users are just using scanservjs | Medium | REST API upload with metadata, task polling until terminal state |
| Metadata entry before scan (title, tags, correspondent) | Users want documents categorized on arrival, not after the fact; this is the main advantage over consume-folder workflows | Medium | Requires fetching tag/correspondent lists from paperless-ngx API |
| Error reporting in UI | Generic errors or silent failures make the tool unusable for non-technical users | Low | Clear messages for: no paper, scanner busy, paperless unreachable, token invalid |
| Scanner discovery | Users expect to pick their scanner, not edit config files | Low | `sane.get_devices()` with device pinning in config |
| Temp file cleanup | Orphaned scan data fills disks; every production tool handles this | Low | Cleanup on success and on error paths |

## Differentiators

Features that set saneless apart from alternatives. Not universally expected, but high-value for the target audience.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| ADF manual duplex (two-pass with flip prompt) | NAPS2 supports manual interleaving but requires drag-and-drop reordering. VueScan has it. scanservjs does not. Automating the reverse-and-interleave for users with single-sided feeders is a genuine workflow improvement | High | Two-pass coordination via `threading.Event`, raw page count validation, reverse-and-interleave logic. The flip prompt with illustration is critical to avoid short-edge flip errors |
| Empty page detection (dual-threshold) | VueScan has blank page removal. scanservjs does not. The dual-threshold approach (mean luminance AND stddev) is more robust than naive mean-only detection, handling bleed-through and dust | Medium | Must convert to grayscale, compute stats. Configurable thresholds per profile |
| First-page thumbnail preview | No web-based scanner tool shows a preview before the full job completes. Gives immediate visual confirmation of orientation, feed quality, and correct document | Low | Generated after first page, base64 JPEG on job state. Low effort, high perceived quality |
| Paperless-ngx connection test (3-mode) | scanservjs has no paperless-ngx awareness at all. A dedicated test distinguishing "unreachable" vs "bad token" vs "OK" saves significant debugging time | Low | Three distinct failure modes from a single GET request |
| Consume directory fallback | For environments where REST API is unavailable (air-gapped, permissions issues). scanservjs uses pipelines to copy files but has no concept of fallback | Low | Config toggle, drop PDF to directory instead of API upload |
| Per-resource cache with manual refresh | Tag/correspondent dropdowns that are fast (TTL cache) but never stale (manual refresh button per resource). No competitor in this space offers this UX | Low | In-memory TTL cache, per-resource invalidation endpoint |
| Job history with pruning | scanservjs has no persistent history. Having a pruned SQLite history of past scans with status is valuable for debugging and audit | Low | SQLite, age + count pruning |
| CLI for automation | NAPS2 has CLI. scanservjs has API but no CLI. A proper CLI (`saneless scan`, `saneless devices`, `saneless jobs`) enables cron jobs and scripting | Low | `click`-based, shares core pipeline with web layer |
| Native hardware duplex support | For scanners that support it, single-pass duplex is the gold standard. scanservjs supports this via SANE source selection | Low | Just a source option (`ADF Duplex`), scanner does the work |
| Health endpoint for container orchestration | No competitor in the self-hosted scanning space exposes a health endpoint. Critical for Docker/Kubernetes auto-restart on deadlock | Low | `GET /health`, 200/503 based on worker thread state |

## Anti-Features

Features to explicitly NOT build. These add complexity without serving the core mission.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Built-in OCR | paperless-ngx already does OCR excellently with Tesseract. Duplicating it adds a heavy dependency (Tesseract) and produces worse results than letting paperless-ngx handle it with its full pipeline | Delegate to paperless-ngx. Document this as an explicit design choice |
| Image editing (crop, rotate, deskew, brightness/contrast) | NAPS2 and VueScan have these because they are general-purpose scan tools. saneless is a bridge to paperless-ngx, not a photo editor. Adding editing creates scope creep and a much more complex UI | Scan at good defaults. If a page is wrong, re-scan. paperless-ngx can rotate during processing |
| Multi-user authentication | The target is trusted LAN. Building auth correctly is a massive effort (sessions, tokens, RBAC). Reverse proxies (Traefik, Caddy, Authelia) already solve this | Document reverse proxy auth pattern in deployment guide |
| Cloud storage / email / non-paperless targets | v1 is paperless-ngx only. Supporting S3, Google Drive, email, etc. fragments the codebase and dilutes focus | Architecture supports future backends via abstraction layer, but do not build them |
| Scanner driver management / saned bundling | saned is a system service with USB passthrough, udev rules, and driver-specific config. Bundling it creates an unmaintainable mess | Require external saned. Document setup clearly |
| Film/slide scanning features | VueScan's film scanning, color restoration, infrared dust removal are for a completely different use case | Out of scope permanently. saneless is for documents |
| Internationalization / multi-language UI | scanservjs supports 16+ languages. For a v1 with a small self-hoster audience, i18n is pure overhead | English only. Structure UI strings for future extraction if demand warrants it |
| Image filters (autolevels, threshold, blur) | scanservjs has these. They add marginal value for document scanning where the goal is archival, not image quality optimization | Skip entirely. The scanner hardware and SANE backend handle image quality |
| Custom pipeline system | scanservjs's pipeline system is its most powerful and most complex feature. It enables arbitrary post-processing but requires JavaScript configuration and is hard to understand | Fixed pipeline: scan -> detect blanks -> assemble PDF -> upload. Predictable, testable, documented |
| Page reordering / drag-and-drop | NAPS2's signature feature. Requires a complex UI with page thumbnails, drag handles, and state management | If pages are wrong, re-scan. Manual duplex handles ordering algorithmically |
| Real-time scan preview (live image as scanner moves) | Some desktop apps show the image building line by line. Impractical over network SANE and adds enormous complexity | First-page thumbnail after scan completes is sufficient |
| scanbd / hardware button integration | Requires system-level daemon integration, scanner-specific button mapping, and event handling | Defer to future work. Architecture does not preclude it |
| Multiple simultaneous scanner support | The scanner is shared hardware. Supporting multiple scanners means multiple worker threads, device locking, and complex queue management | Single scanner at a time. Device selection in config or UI |

## Feature Dependencies

```
Scanner Discovery --> Flatbed Scan --> PDF Assembly --> Paperless-ngx Upload
                  \-> ADF Multi-page Scan --> Empty Page Detection --> PDF Assembly
                  \-> ADF Manual Duplex --> Flip Prompt UI --> Reverse-and-Interleave --> PDF Assembly

Scan Profiles --> All Scan Types (profiles configure source, resolution, mode)

Paperless-ngx Connection Test --> Metadata Dropdowns (tags, correspondents)
                              \-> API Upload

Metadata Dropdowns --> Per-resource Cache + Refresh

Job Queue (worker thread) --> All Scan Types (single concurrent scan)
                          \-> Job History (SQLite)

First-page Thumbnail --> Live Status Indicator (thumbnail embedded in job state)

Health Endpoint --> Container HEALTHCHECK (independent of scan pipeline)
CLI --> Core Pipeline (shares scan/device/job logic with web layer)
```

## MVP Recommendation

Prioritize (Phase 1 - prove the pipeline works end-to-end):
1. Scanner discovery and device selection
2. Flatbed single-page scan with configurable resolution/mode
3. PDF assembly via img2pdf
4. Paperless-ngx REST API upload with title metadata
5. Paperless-ngx task polling
6. CLI (`saneless scan`, `saneless devices`)
7. Pydantic-settings config (TOML + env vars)
8. Basic error handling and logging

Then (Phase 2 - ADF and multi-page):
1. ADF multi-page scanning
2. Empty page detection (dual threshold)
3. Native hardware duplex
4. ADF manual duplex with flip prompt
5. Scan profiles with per-profile defaults

Then (Phase 3 - Web UI):
1. FastAPI web layer with worker thread
2. Profile selector, metadata fields, scan button
3. Tag/correspondent dropdowns with cache and refresh
4. Live status indicator
5. First-page thumbnail preview
6. Manual duplex flip prompt with illustration
7. Job history table
8. Paperless-ngx connection test
9. Health endpoint

Then (Phase 4 - Packaging):
1. Consume directory fallback
2. OCI container image with HEALTHCHECK
3. pip-installable package
4. `saneless jobs` CLI command

**Defer indefinitely:** OCR, image editing, multi-language, cloud targets, image filters, pipeline system, page reordering, hardware button integration.

## Competitive Positioning

| Capability | scanservjs | NAPS2 | VueScan | saneless (planned) |
|------------|-----------|-------|---------|-------------------|
| Web UI | Yes | No (desktop) | No (desktop) | Yes |
| SANE backend | Yes | No (WIA/TWAIN) | Yes | Yes |
| Paperless-ngx integration | No (file copy only) | No | No | Native (API + metadata) |
| Manual duplex | No | Yes (manual reorder) | Yes | Yes (automated interleave) |
| Empty page detection | No | No | Yes | Yes (dual threshold) |
| OCR | Yes (Tesseract) | Yes | Yes | No (delegates to paperless-ngx) |
| Image editing | Basic filters | Full (crop, rotate, deskew) | Full (advanced) | No (by design) |
| Scan profiles | Via config file | Yes (GUI) | Yes | Yes (TOML + UI) |
| Container deployment | Yes (Docker) | No | No | Yes (OCI) |
| CLI | No (API only) | Yes | Yes | Yes |
| Health monitoring | No | No | No | Yes |
| Job history | No | No | No | Yes (SQLite) |

saneless's niche is clear: the only web-accessible, container-deployable scanner tool with native paperless-ngx integration and metadata-at-scan-time. It does not compete with NAPS2/VueScan on image editing or general-purpose scanning -- it competes on the "scan to archive" workflow.

## Sources

- scanservjs GitHub README and documentation (fetched 2026-03-20): https://github.com/sbs20/scanservjs
- NAPS2 official website (fetched 2026-03-20): https://www.naps2.com/
- VueScan official website (fetched 2026-03-20): https://www.hamrick.com/
- saneless PRD (local): /home/kris/git/saneless/docs/PRD.md
- Confidence: MEDIUM -- based on official project pages and training data knowledge of these tools. Could not access paperless-ngx docs directly (403).
