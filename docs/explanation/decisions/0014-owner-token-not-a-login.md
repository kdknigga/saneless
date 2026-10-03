# 0014. The owner token is a guard, not a login

## Status

Accepted, 2026-10-03.

## Context

A manual duplex scan stops at a flip prompt, and a multi-page scan asks questions between passes. On a shared appliance, anyone with the page open could press Continue on a stack they did not load. saneless has no user accounts and is served over plain HTTP on a trusted LAN.

## Decision

`saneless.web.routes` gives the browser that submits a scan a random owner token in an HttpOnly, SameSite=Lax cookie with a one-year lifetime, renewed on every accepted submit, and records the token on the job. Only a request presenting the matching token may see that job's details or answer its prompts. The comparison is constant-time, and the token never appears in a log line or in the markup. A job with no recorded owner is anyone's.

The invariant: the token keeps a second person at the same appliance from answering a prompt for paper they did not load. It is not an authentication mechanism and grants nothing beyond that job.

## Consequences

- The household member at the scanner sees their own job's title, preview and prompts, and others see a generic title.
- A scripted client that sends a guessed cookie of its own and no browser fetch headers can still answer a prompt. That is accepted on a trusted LAN, not overlooked.
- The cookie carries no Secure flag, because on plain HTTP that flag would stop it being sent rather than harden it.

**Alternatives rejected:** treating the token as a login credential and building authentication on it; a session cookie that a browser restart would lose.
