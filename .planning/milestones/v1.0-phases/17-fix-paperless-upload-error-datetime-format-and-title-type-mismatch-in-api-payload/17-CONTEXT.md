# Phase 17: Fix Paperless upload error: datetime format and title type mismatch in API payload - Context

**Gathered:** 2026-03-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Fix two bugs in the Paperless-ngx upload payload that cause API errors: (1) the `created` field sends a full ISO 8601 datetime with microseconds and timezone, but Paperless-ngx expects a date-only string (`YYYY-MM-DD`) or a datetime without microseconds; (2) the `title` field is typed as `str` but when mixed into the multipart `files=` list as `tuple[str, str]` alongside file upload tuples `tuple[str, tuple[str, BinaryIO, str]]`, the type annotation `list[tuple[str, FileTypes]]` is incorrect and may cause type checker failures or unexpected encoding.

</domain>

<decisions>
## Implementation Decisions

### Datetime format fix
- **D-01:** Change `datetime.now(tz=UTC).isoformat()` to `datetime.now(tz=UTC).strftime("%Y-%m-%d")` at both call sites in `pipeline.py` (lines 134 and 421) — Paperless-ngx `created` field expects a date, not a full datetime
- **D-02:** The `created` parameter type on `upload_document()` stays as `str | None` — no need to change to `date` type since the API takes a string in the multipart form

### Title / multipart type fix
- **D-03:** Separate form data fields from file uploads — use `data=` parameter for simple string fields (`title`, `created`, `correspondent`, `tags`) and `files=` parameter only for the PDF binary. This is the idiomatic httpx approach and eliminates the type mismatch
- **D-04:** Remove the `fields: list[tuple[str, str]]` accumulation pattern and the `multipart_files` list merging. Instead, build a `data` dict for form fields and pass file separately

### Test fixes
- **D-05:** Update `test_upload_with_created()` to verify the date-only format (`YYYY-MM-DD`) in the payload
- **D-06:** Add a test that verifies form fields are sent as `data=` (not `files=`) while the PDF is sent as `files=`

### Claude's Discretion
- Exact httpx `data=` vs `files=` parameter construction details
- Whether to add a helper function for date formatting or inline it
- Additional test cases beyond the two specified

</decisions>

<canonical_refs>
## Canonical References

No external specs — requirements are fully captured in decisions above. The Paperless-ngx API behavior is documented in their REST API docs (the `post_document` endpoint), but the fix is straightforward enough to not require external reference.

</canonical_refs>

<code_context>
## Existing Code Insights

### Bug Location 1: datetime format
- `src/saneless/pipeline.py:134` — duplex mismatch recovery path: `created = datetime.now(tz=UTC).isoformat()`
- `src/saneless/pipeline.py:421` — main upload path: `created = datetime.now(tz=UTC).isoformat()`

### Bug Location 2: multipart type mismatch
- `src/saneless/paperless.py:99-116` — builds `fields: list[tuple[str, str]]` then spreads into `multipart_files: list[tuple[str, FileTypes]]` with the PDF tuple. The `FileTypes` annotation doesn't match the `tuple[str, str]` shape of form fields.

### Established Patterns
- httpx client is already used (`self._client.post`)
- Retry loop with exponential backoff exists (lines 108-153)
- The `data=` + `files=` split is standard httpx for mixed form/file uploads

### Integration Points
- `PaperlessClient.upload_document()` is called from `pipeline.py` at two sites
- Tests in `tests/test_paperless.py` mock the HTTP handler

</code_context>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 17-fix-paperless-upload-error-datetime-format-and-title-type-mismatch-in-api-payload*
*Context gathered: 2026-03-22 via --auto mode*
