# 0002. Scanners are listed in a short-lived child process

## Status

Accepted, 2026-10-03.

## Context

libsane's net backend keeps one control connection per scanner host open between listings. Once the saned on that host restarts or dies, the next device listing can read a reply that was never filled in, and the whole process dies from SIGSEGV, SIGABRT or SIGBUS. A listing can also hang for minutes inside a blocking C call on a host that has silently gone away. Neither can be contained in the process that made the call.

## Decision

Every scanner listing -- the Scanner check, auto-detecting a device, start-up profile generation, and the device-listing commands -- runs in a short-lived child process with a deadline, through `saneless.scanner.listing`. The child is a clean exec of the interpreter on the child script, in isolated mode, with a literal argv and the device id passed on stdin. The child is killed and reaped before the listing returns.

The invariant: no listing happens in the server's own process, and a caller holding the scanner gate releases it only after the child has been reaped, so nothing is left inside libsane.

## Consequences

- A crash in libsane while listing is a failed listing, and a hang is a process stopped at the deadline. The server stays up.
- Each listing costs a process start, measured at about 14 ms.
- python-sane must be importable from the interpreter's own site-packages, because isolated mode ignores every `PYTHON*` variable.

**Alternatives rejected:** listing in the server's own process; a `fork` child, which inherits the stale control connection and crashed after every saned restart; the "spawn" start method, which costs about 340 ms per listing and leaves a resource tracker running; the "forkserver" start method, which leaves two resident helper processes.
