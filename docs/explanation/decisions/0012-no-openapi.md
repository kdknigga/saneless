# 0012. The web server publishes no API schema

## Status

Accepted, 2026-10-03.

## Context

FastAPI generates an OpenAPI schema and serves interactive documentation for it by default. saneless is an unauthenticated LAN appliance serving an HTMX UI, and its endpoints are the UI's own: they exchange HTML fragments, not a stable machine contract. The generated schema endpoint also failed on some of those routes.

## Decision

The application is created with the schema URL and both documentation URLs switched off. All three are named explicitly, so turning the schema back on later cannot bring the documentation pages back with it by accident. The endpoints a script may use are documented by hand in the web API reference.

The invariant: the server advertises no generated API contract.

## Consequences

- No schema or documentation page exists to drift from what the UI actually does, or to fail on a route.
- A script author reads the hand-written reference instead of a generated one.
- Re-enabling the schema is a deliberate change to three arguments, not a default to restore.

**Alternatives rejected:** serving the generated schema and its documentation pages; fixing the schema generation for every route and keeping it.
