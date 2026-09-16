# Phase 33: Disable the OpenAPI Schema and Docs Endpoints — Discussion Log

**Date:** 2026-09-16
**Mode:** default (interactive)

Human reference only. Downstream agents read `33-CONTEXT.md`, not this file.

---

## Gray areas presented

Four were offered; the user selected one.

| Area | Selected |
|---|---|
| Removal mechanism — `openapi_url=None` alone vs. all three kwargs explicit | no |
| Regression-guard shape — standalone 404 test vs. folded into the reachability proof; structural assertion on `app.routes`; fate of `_ROUTE_SKIPS` | no |
| **Latent root cause** — what happens to the unresolvable `-> Response` annotations | **yes** |
| Doc write-up scope — how much rationale in `web-api.md`; the "called directly for integration" claim | no |

The three unselected areas became Claude's discretion (see CONTEXT.md
`<decisions>` → "Claude's Discretion").

---

## Area: Latent root cause

### Q1 — After the endpoints are removed, what happens to the unresolvable `-> Response` annotations?

Presented, with the finding that `Response` is imported *only* under
`if TYPE_CHECKING:` in `src/saneless/web/routes.py:15` while the module carries
`from __future__ import annotations` — so the name does not exist at runtime at all,
which is sharper than the Phase 29 deferred note's "annotate their returns as
`Response`".

- **Leave, record as landmine** — code unchanged, a note in `web-api.md` and/or at the
  `FastAPI(...)` call explains the `TYPE_CHECKING` wire for anyone who wants a schema
  back.
- **Leave it silent** — endpoints gone, annotations are idiomatic for this repo, no note
  about the pydantic failure anywhere.
- **Fix the annotations too** — move `Response` to a runtime import, add
  `response_model=None` where needed. Explicitly flagged as contradicting the roadmap's
  "remove rather than fix" decision and as scope creep.

**Selected:** Fix the annotations too.

**Note:** the scope-creep flag was stated plainly in the option text before the choice
was made. Recorded as a deliberate override of the roadmap framing, not an accident.

### Q2 — If the annotations are fixed so a schema *can* generate, do the three endpoints still get removed?

- **Yes — still removed** — both halves ship; the fix is hardening, and criterion 1 is
  non-negotiable in the roadmap.
- **No — keep the endpoints** — would delete criteria 1, 2, 4 and 5 and leave an
  unauthenticated LAN box advertising an API surface; would need the roadmap phase
  rewritten.

**Selected:** Yes — still removed.

### Q3 — With `/openapi.json` gone, how do we prove the annotations are actually fixed and stay fixed?

- **Test calls `app.openapi()`** — in-process against the real app from `create_app()`;
  `openapi_url=None` suppresses route registration only, the method still generates. One
  test proves the schema builds *and* is not served.
- **Throwaway app with docs on** — second `FastAPI` from the same router with
  `openapi_url` enabled, driven through `TestClient`; duplicates app wiring and can
  drift.
- **No test — rely on checkers** — `ty` and `pyrefly` pass on today's broken code and
  never resolve forward refs the way pydantic does; proves nothing.

**Selected:** Test calls `app.openapi()`.

### Q4 — Which mechanism fixes the annotations?

- **Runtime import of `Response`** — one line out of `TYPE_CHECKING`; schema genuinely
  describes the routes; costs a documented exception to the repo's type-only-import
  convention.
- **`response_model=None` per route** — keeps the `TYPE_CHECKING` import; flagged as
  suppression rather than resolution (no response bodies documented) and 14 edit sites
  instead of 1.
- **Let research decide** — lock the intent, let `gsd-phase-researcher` verify against
  the installed FastAPI before the planner picks.

**Selected:** Let research decide.

### Q5 — Continue or move on?

**Selected:** Move on.

---

## Claude's discretion (user did not select these areas)

- Removal mechanism at `src/saneless/web/app.py:145` — leaning to naming all three
  kwargs explicitly; `/docs/oauth2-redirect` is a fourth path that disappears with them,
  and the three `_ROUTE_CALLS` entries at `tests/test_app_lifespan.py:513-515` must go.
- Regression-guard shape — standalone vs. folded 404 test, optional structural assertion
  on `app.routes`; keep the `_ROUTE_SKIPS` mechanism (it drops to `/static` only).
- Doc write-up scope in `docs/reference/web-api.md`, including whether line 3's "can
  also be called directly for integration" claim survives.

## Deferred ideas

- Web UI authentication — the "unauthenticated LAN appliance" reasoning justifies the
  removal but is not a commitment to add auth. Own phase if ever wanted.
- A hand-written API contract for the HTMX endpoints, if the "integration" claim is ever
  to be made true.

## Scope creep redirected

None during discussion. The one scope expansion (fixing the annotations) was offered as
an explicit option, chosen knowingly, and is recorded as a locked decision rather than
redirected.
