# Phase 33: Disable the OpenAPI Schema and Docs Endpoints - Context

**Gathered:** 2026-09-16
**Status:** Ready for planning

<domain>
## Phase Boundary

A LAN-exposed saneless stops advertising an API surface it does not have, and the
annotation defect that made the schema unbuildable is fixed rather than hidden behind
the removal. Delivers API-01 (found during Phase 29 plan 29-10's route-reachability
proof, not by the 2026-09-09 review — no `C-`/`M-`/`N-` finding ID).

- `GET /openapi.json`, `GET /docs` and `GET /redoc` answer 404 on a running app — no
  500, no partial schema, no interactive shell — proven by a test that drives all three
- `_ROUTE_SKIPS` in `tests/test_app_lifespan.py` no longer carries an `/openapi.json`
  entry, and the every-route assertion (`uncovered == set()`) still passes, so the skip
  is removed rather than widened
- The `-> Response` annotations in `src/saneless/web/routes.py` resolve at runtime, so
  `app.openapi()` builds a clean schema in-process even though the app serves none
- No shipped file — docs, README, or test — claims saneless serves an OpenAPI schema or
  interactive API documentation
- The reason is recorded in `docs/reference/web-api.md`: unauthenticated LAN appliance
  serving an HTMX UI, nothing consumes a schema
- Phase 29's `deferred-items.md` entry is closed with a pointer to this phase

**Not in this phase:**
- **Authentication for the web UI.** The "unauthenticated LAN appliance" framing is the
  *reason* for the removal, not an invitation to add auth here. No requirement covers it.
- **Any new API endpoint, or a hand-written API contract to replace the generated one.**
  Nothing consumes one; adding one is a new capability.
- **Changing what the existing HTMX endpoints return.** Handler bodies are untouched;
  only return *annotations* (and possibly one import site) change.

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)
- **Phase 29 D-19:** every route the app serves is either driven by `_ROUTE_CALLS` or
  skipped with a named reason in `_ROUTE_SKIPS`; a route that is neither fails the test.
  Removing routes must keep that invariant honest, never widen a skip to cover it.
- **Phase 28 D-01 / `src/saneless/web/errors.py`:** every error response renders through
  `render_error`, and `rejection_for_status()` already maps a bare 404 to
  `RequestRejection.NOT_FOUND`. A deleted route therefore produces the app's own 404
  page with no new handler, no bespoke "no API here" response, and no special-casing
  that would make the three paths distinguishable from any other unknown path.
- **Roadmap Phase 33 criterion 1** is fixed: the three paths must 404. Fixing the
  annotations is hardening on top of the removal, never a reason to keep serving a
  schema.

### Latent root cause — the `-> Response` annotations (discussed)
- **D-01: The annotation defect is fixed, not left as a documented landmine.** The
  roadmap framed this phase as "remove rather than fix" and recorded fixing as
  out of scope; the user explicitly overrode that. Both halves ship in this phase.
  - Evidence the defect is real and sharper than the Phase 29 note says:
    `src/saneless/web/routes.py` has `from __future__ import annotations` (line 3) and
    imports `Response` **only** under `if TYPE_CHECKING:` (line 15, as
    `from starlette.responses import Response`). `Response` is not a runtime name at
    all, so pydantic cannot resolve the forward reference by any means. 14 of the 17
    handlers annotate `-> Response`; `health` and `paperless_test` annotate
    `-> dict[str, str] | JSONResponse` and `index` annotates `-> Response`.
- **D-02: The three endpoints are still removed.** Fixing the annotations makes a schema
  *possible*; the appliance still serves none. Keeping the endpoints because the schema
  now works was rejected — it deletes criteria 1, 2, 4 and 5, and leaves an
  unauthenticated LAN box advertising an API surface.
- **D-03: The fix is proven by a test that calls `app.openapi()` in-process on the real
  app from `create_app()`.** `openapi_url=None` suppresses route *registration* only;
  the `openapi()` method still generates. One test therefore proves both halves — the
  schema builds cleanly, and it is not served over HTTP. A future handler that
  reintroduces a `TYPE_CHECKING`-only return annotation breaks this test on the day it
  lands.
  - Rejected: constructing a throwaway `FastAPI` from the same router with
    `openapi_url` enabled and driving `/openapi.json` through `TestClient` — duplicates
    `create_app()`'s wiring in the test and can drift from it.
  - Rejected: no test, relying on `ty` and `pyrefly`. Both pass on today's broken code;
    neither resolves forward references the way pydantic does at runtime. It proves
    nothing and the regression returns silently.
- **D-04: The fix mechanism is left to research.** Locked intent: the return annotations
  must resolve at runtime and `app.openapi()` must build without raising. Two candidates
  were named and neither is verified against the installed FastAPI/pydantic versions:
  1. Move `from starlette.responses import Response` out of `if TYPE_CHECKING:` in
     `routes.py`. One line, and the schema genuinely describes the routes. Costs a
     documented exception to the repo's type-only-import convention — the import would
     need a comment saying why it is load-bearing at runtime.
  2. Add `response_model=None` to the affected route decorators, keeping the
     `TYPE_CHECKING` import. Flagged during discussion as suppression rather than
     resolution — the schema would generate but document no response bodies — and 14
     edit sites instead of 1. Research must say plainly whether this satisfies D-03's
     "builds a clean schema" bar before the planner may choose it.
  - **Research must also settle** whether FastAPI resolves return annotations at route
    *registration* time or only at schema-generation time. The app starts fine today and
    only `/openapi.json` 500s, which points at schema-generation time, but this was not
    verified during discussion and it changes what a fix has to touch.

### Claude's Discretion
The user selected only "Latent root cause" for discussion. These are Claude's to decide,
guided by the carried-forward decisions above:
- **Removal mechanism.** `FastAPI(openapi_url=None)` alone versus naming all three
  (`openapi_url=None, docs_url=None, redoc_url=None`) at `src/saneless/web/app.py:145`.
  Lean toward the explicit three: self-documenting at the call site, and it survives
  someone re-enabling `openapi_url` later without re-exposing `/docs` and `/redoc`.
  Note that `/docs/oauth2-redirect` is a fourth registered path that disappears with
  them, and that `_ROUTE_CALLS` currently drives `/docs`, `/docs/oauth2-redirect` and
  `/redoc` (`tests/test_app_lifespan.py:513-515`) — those three entries must go too, or
  the map names routes the app no longer serves.
- **Regression-guard shape.** Whether the three-path 404 proof is a standalone test or
  folded into the existing reachability proof; whether a structural assertion (no
  schema/docs path present in `app.routes`) is added on top of the 404s. `_ROUTE_SKIPS`
  drops to just its `/static` entry — keep the map and its D-19 contract, do not delete
  the mechanism.
- **Doc write-up scope** in `docs/reference/web-api.md`. Note the doc's current opening
  claim (line 3) that the endpoints "can also be called directly for integration" —
  after this phase that is the only remaining API-ish claim in the file, and the
  endpoints return HTML fragments for HTMX rather than a stable JSON contract.
  Criterion 3 ("no shipped file claims saneless serves an OpenAPI schema or interactive
  API documentation") does not strictly reach it, but it is adjacent and worth a
  judgment call.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirement and evidence
- `.planning/REQUIREMENTS.md` §API-01 (line 197) — the requirement text, including the
  three 404s, the `_ROUTE_SKIPS` drop, and the `web-api.md` rationale
- `.planning/ROADMAP.md` §"Phase 33: Disable the OpenAPI Schema and Docs Endpoints" —
  goal and the five success criteria this phase is judged against
- `.planning/phases/29-geometry-memory-and-timeouts/deferred-items.md` §"`GET
  /openapi.json` returns 500" — the original finding, its two candidate fixes, and the
  entry criterion 5 requires closing with a pointer back to this phase

### Code this phase touches
- `src/saneless/web/app.py:145` — `app = FastAPI(lifespan=lifespan)`, the single site
  where the schema and docs URLs are configured
- `src/saneless/web/routes.py:3,15` — `from __future__ import annotations` and the
  `TYPE_CHECKING`-only `Response` import that together break schema generation
- `tests/test_app_lifespan.py:507-548` — `_ROUTE_CALLS` and `_ROUTE_SKIPS`
- `tests/test_app_lifespan.py:551+` —
  `test_sane_lifecycle_across_startup_every_route_and_shutdown`, the D-19 reachability
  proof carrying the `uncovered == set()` assertion
- `src/saneless/web/errors.py:101` (`rejection_for_status`) and `:243`
  (`install_error_handlers`) — why a removed route already 404s through the app's own
  error page

### Docs to correct
- `docs/reference/web-api.md` — 220 lines; the endpoint table and per-endpoint detail.
  Criterion 4's rationale lands here. Line 3 carries the "can also be called directly
  for integration" claim.

### Prior context
- `.planning/phases/29-geometry-memory-and-timeouts/29-CONTEXT.md` — D-19's route
  reachability contract
- `.planning/codebase/CONVENTIONS.md` — the repo's import and annotation conventions,
  which D-04 candidate 1 would need a documented exception to

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`rejection_for_status()` / `render_error()`** (`src/saneless/web/errors.py`): a
  removed route's 404 already renders as `RequestRejection.NOT_FOUND` through the app's
  standard error page. No new handler, no new template, nothing to build for criterion 1
  beyond deleting the routes.
- **`_ROUTE_CALLS` / `_ROUTE_SKIPS`** (`tests/test_app_lifespan.py`): the existing
  every-route audit. Criterion 2's proof is an edit to this map plus the assertion that
  already exists, not a new mechanism.
- **`create_app(settings, scanner)`** (`src/saneless/web/app.py`): the single app
  factory, already exercised under `TestClient` in `test_app_lifespan.py`. D-03's
  `app.openapi()` test builds on the same factory.

### Established Patterns
- **Type-only imports live under `if TYPE_CHECKING:`** with
  `from __future__ import annotations` at the top of every module. This is the
  convention D-04 candidate 1 would carve an exception out of, and the reason the
  exception needs a comment rather than a silent import move.
- **Every error response goes through one renderer** (Phase 28 D-01). Do not add a
  bespoke response for the three removed paths — they must be indistinguishable from any
  other unknown path.
- **Skips are named, never silent** (Phase 29 D-19). Criterion 2 is specifically about
  removing a skip rather than broadening one.

### Integration Points
- `src/saneless/web/app.py:145` — the `FastAPI(...)` constructor call is the whole of
  the removal.
- `src/saneless/web/routes.py` return annotations (14 `-> Response`, plus the two
  `-> dict[str, str] | JSONResponse` handlers) — the whole of the fix.
- `tests/test_app_lifespan.py` — both the skip removal and, probably, the new 404 and
  `app.openapi()` proofs.
- `docs/reference/web-api.md` — the rationale.

</code_context>

<specifics>
## Specific Ideas

- The user overrode the roadmap's explicit "remove rather than fix" scoping when told
  plainly that fixing was scope creep. Treat D-01 as deliberate and locked: do not let
  the researcher, planner, or plan-checker re-litigate it back to "record as a landmine
  and move on" on the grounds that the roadmap said so.
- The user also declined to pick a fix mechanism when offered two concrete ones,
  choosing "let research decide" specifically because neither had been verified against
  the installed FastAPI. Research is expected to come back with a verified answer, not a
  restatement of the same two options.

</specifics>

<deferred>
## Deferred Ideas

- **Web UI authentication.** The "unauthenticated LAN appliance" reasoning is the stated
  justification for removing the schema. It is not a commitment to add auth, and no
  v2.0 requirement covers it. Needs its own phase if it is ever wanted.
- **A hand-written API contract for the HTMX endpoints.** If the "can also be called
  directly for integration" claim in `docs/reference/web-api.md` is ever to be made
  true, it needs a stable contract and tests to hold it. Out of scope here — this phase
  may correct or soften the claim, but does not make it true.

</deferred>

---

*Phase: 33-disable-the-openapi-schema-and-docs-endpoints*
*Context gathered: 2026-09-16*
