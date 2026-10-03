# 0010. Web handlers read one typed services object

## Status

Accepted, 2026-10-03.

## Context

The web application's handlers need the same collaborators: the scan worker, the job store, the settings, the paperless-ngx client, the check cache and refresher, the templates, and a few smaller values. Kept as separate untyped attributes on the application state, a misspelt name or a wrong keyword passed to a collaborator reaches no type checker, and a test can set one attribute while the code reads another.

## Decision

The application holds one frozen `Services` dataclass, and every handler reads it through the plain function `services(request)` in `saneless.web.services`, which narrows the type for both type checkers. Mutable per-app state lives in a small mutable holder inside it, not in reassigned fields. A test that swaps one collaborator builds a new `Services` with `dataclasses.replace`. The old per-collaborator attributes on the application state do not exist.

The invariant: there is one source for each collaborator, it is typed, and a reader of a removed attribute fails with `AttributeError` instead of reading a stale copy.

## Consequences

- A misspelt collaborator or a bogus keyword in a handler is a type error in both checkers.
- Handler signatures stay as they are; each handler makes one call.
- Swapping a collaborator in a test means replacing the whole `Services` object, which is slightly more ceremony than assigning one attribute.

**Alternatives rejected:** a FastAPI `Depends` parameter in every handler; keeping the per-collaborator attributes beside the services object.
