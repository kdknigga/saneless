# Deferred items — Phase 29

Out-of-scope findings recorded rather than fixed, per the executor's scope
boundary (only issues caused by the current task's changes are auto-fixed).

## `GET /openapi.json` returns 500 (found by 29-10 T3, pre-existing)

Driving every route in the app's table for D-19's reachability proof showed
that FastAPI cannot generate this app's OpenAPI schema. The route handlers in
`src/saneless/web/routes.py` annotate their returns as `Response` under
`from __future__ import annotations`, and pydantic raises
`PydanticUserError: ... is not fully defined` rather than resolving the
forward reference, so the request ends in a 500.

- **Not caused by this plan.** No route signature, return annotation or
  `response_model` was touched by 29-10; the same failure reproduces at the
  plan's base commit.
- **No user-facing impact known.** saneless serves an HTMX UI, not a JSON API
  for third parties; nothing in the app, the docs or the tests fetches the
  schema. `/docs` and `/redoc` are served (they are static shells) but will not
  populate.
- **Two candidate fixes**, both out of scope here: give the handlers concrete
  return annotations plus `response_model=None` where needed, or disable the
  generated documentation endpoints outright with
  `FastAPI(openapi_url=None)`, which is the more defensible default for a
  LAN-exposed appliance.
- 29-10's D-19 proof skips the path explicitly, naming this reason in
  `_ROUTE_SKIPS` in `tests/test_app_lifespan.py`, so the skip is auditable
  rather than silent.
