# Decision records

These records hold choices a contributor could undo by mistake: each one looked at an obvious alternative and turned it down for a reason that is not visible in the code. Each record is short, written in the present tense, and kept out of the site navigation. Code that depends on one ends its comment with a pointer to it, so the reason travels with the invariant.

- [0001. A source saneless does not recognise scans one page](0001-unknown-sources-scan-one-page.md)
- [0002. Scanners are listed in a short-lived child process](0002-listing-in-a-child-process.md)
- [0003. The scanner gate is a lock, not an advisory check](0003-scanner-gate-is-a-lock.md)
- [0004. The profiles mapping is replaced wholesale, never mutated](0004-profiles-replaced-wholesale.md)
- [0005. The backend hands each page to a declared page sink](0005-page-sink-contract.md)
- [0006. The PDF is built one page at a time and merged with qpdf](0006-per-page-pdf-and-qpdf-merge.md)
- [0007. A feeder pass stops at a fixed page cap](0007-auto-feeder-page-cap.md)
- [0008. There is no storage error category](0008-no-storage-error-category.md)
- [0009. An upload that may have arrived is never sent again](0009-no-resend-after-send.md)
- [0010. Web handlers read one typed services object](0010-typed-services-accessor.md)
- [0011. The health strip is filled lazily and its polling is bounded](0011-lazy-bounded-health-strip.md)
- [0012. The web server publishes no API schema](0012-no-openapi.md)
- [0013. Health-strip glyphs use text-presentation characters only](0013-text-presentation-glyphs.md)
- [0014. The owner token is a guard, not a login](0014-owner-token-not-a-login.md)
- [0015. Every dependency update waits seven days](0015-dependabot-cooldown.md)
