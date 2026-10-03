# 0009. An upload that may have arrived is never sent again

## Status

Accepted, 2026-10-03.

## Context

An upload to paperless-ngx can fail before the request reaches the server, or after it has. A connection refused, no free connection, a proxy that refused the tunnel or a body write that stalled cannot have delivered the whole document. A failure while reading the answer, a server error, or a success answer with no task id can follow a document paperless-ngx has already stored. Retrying or falling back to the consume directory in the second case could file the same scan twice.

## Decision

`saneless.paperless` sorts every upload failure by whether the upload can have reached paperless-ngx. Only a failure before send is retried, for a bounded budget, and only then may the consume directory fallback take over. A failure after send is never resent and never copied: the job ends amber, with the PDF kept for the operator.

The invariant: a document that may already be in paperless-ngx is never offered to it a second time, by any route.

## Consequences

- No scan is ever filed twice by saneless.
- An upload that did arrive but whose answer was lost needs the operator to check paperless-ngx and discard the kept PDF, or upload it by hand.
- Any new transport error has to be placed on one side of the line before it can be retried.

**Alternatives rejected:** retrying every transport error; falling back to the consume directory after any failure.
