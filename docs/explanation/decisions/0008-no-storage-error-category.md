# 0008. There is no storage error category

## Status

Accepted, 2026-10-03.

## Context

Every error a scan can raise is classified into an error category, and the category is persisted on the job record and mapped to a CLI exit code. A failure to open or use the job store is not a scan failure: it happens before or around any job, and it is a problem with the appliance's setup.

## Decision

`saneless.vocabulary` has no storage member in its error category. A job-store failure classifies as unknown, and the CLI guard maps it by exception type to the configuration exit code. The mapping is done by type rather than by classifying the error as a configuration failure, because the category is persisted on job records, and a store that cannot open is not a job's configuration failure.

The invariant: every error category is a value some job record can actually carry.

## Consequences

- A script sees a job-store failure as a setup problem, through the exit code.
- No persisted category exists that no job could ever carry, and readers of job history need no case for it.
- The exit-code mapping has one type-based exception beside the category table, which a reader has to know about.

**Alternatives rejected:** a storage member in the error category, which would be a persisted value no job could ever carry; classifying a store failure as a configuration category.
