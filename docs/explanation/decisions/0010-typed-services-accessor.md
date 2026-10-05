# 0010. Web handlers read one typed services object

## Status

Accepted, 2026-10-03.

## Context

The web application's handlers need the same collaborators: the scan worker, the job store, the settings, the paperless-ngx client, the check cache and refresher, the templates, and a few smaller values. Kept as separate untyped attributes on the application state, a misspelt name or a wrong keyword passed to a collaborator reaches no type checker, and a test can set one attribute while the code reads another.

## Decision

The application holds one frozen `Services` dataclass, and every handler and error handler reads it through the plain function `services(request)` in `saneless.web.services`, which narrows the type for both type checkers. It holds the scan worker, the job store, the settings, the paperless-ngx client, the metadata cache, the check cache and refresher, the templates, the two rate floors, the shared connection-test result, the status-token key and whether scanning is blocked. The one value that changes after start-up, whether the lifespan owns the scanner, lives in a small mutable `AppLifecycle` holder inside it, not in a reassigned field. Code that holds the app rather than a request, the server's stop hook and `serve`, reads the same object and acts only when it is a `Services`. Tests read it through `services_of(app)`, and a test that swaps one collaborator stores a new `Services` built with `dataclasses.replace`. The old per-collaborator attributes on the application state do not exist.

The invariant: there is one source for each collaborator, it is typed, and a reader of a removed attribute fails with `AttributeError` instead of reading a stale copy.

## Consequences

- A misspelt collaborator or a bogus keyword in a handler is a type error in both checkers.
- Handler signatures stay as they are; each handler makes one call.
- Swapping a collaborator in a test means replacing the whole `Services` object, which is slightly more ceremony than assigning one attribute.

**Alternatives rejected:** a FastAPI `Depends` parameter in every handler; keeping the per-collaborator attributes beside the services object.
