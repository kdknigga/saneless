# 0002. Scanners are listed in a short-lived child process

## Status

Accepted, 2026-10-03.

## Context

libsane's net backend keeps one control connection per scanner host open between listings. Once the saned on that host restarts or dies, the next device listing can read a reply that was never filled in, and the whole process dies from SIGSEGV, SIGABRT or SIGBUS. A listing can also hang for minutes inside a blocking C call on a host that has silently gone away. Neither can be contained in the process that made the call.

## Decision

Every SANE query outside a scan -- listing scanners for the Scanner check, auto-detection, start-up profile generation and the device-listing commands; opening the device for the Scanner check; reading a device's capabilities for the CLI and the start-up profiles the web form offers -- runs in a short-lived child process with a deadline, through `saneless.scanner.listing`. The child is a clean exec of the interpreter on the child script, in isolated mode, in a session of its own, with a literal argv and the device id passed on stdin, and it loads the thread unwinder before python-sane. The child is killed and reaped before the query returns. The scan child of ADR 0016 is started through the same launcher, `saneless.scanner.child_launch`.

The invariant: no SANE query happens in the server's own process, and a caller holding the scanner gate releases it only after the child has been reaped, so nothing is left inside libsane.

## Consequences

- A crash in libsane during a query is a failed query, and a hang is a process stopped at the deadline. The server stays up.
- Each query costs a process start, measured at about 16 ms; a whole listing of the SANE test backend, SANE's start-up included, takes about 29 ms.
- python-sane must be importable from the interpreter's own site-packages, because isolated mode ignores every `PYTHON*` variable.

**Alternatives rejected:** querying SANE in the server's own process; a `fork` child, which inherits the stale control connection and crashed after every saned restart; the "spawn" start method, which costs about 340 ms per listing and leaves a resource tracker running; the "forkserver" start method, which leaves two resident helper processes.
