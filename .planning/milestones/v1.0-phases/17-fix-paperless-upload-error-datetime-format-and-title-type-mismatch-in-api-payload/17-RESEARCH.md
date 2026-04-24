# Phase 17: Fix Paperless upload error: datetime format and title type mismatch in API payload - Research

**Researched:** 2026-03-22
**Domain:** httpx multipart uploads, Paperless-ngx REST API
**Confidence:** HIGH

## Summary

This phase fixes two bugs in the Paperless-ngx upload path. Bug 1: `datetime.now(tz=UTC).isoformat()` produces a full ISO 8601 string like `2026-03-22T14:30:45.123456+00:00`, but the Paperless-ngx `post_document` endpoint expects a date-only string (`YYYY-MM-DD`) for the `created` field. Bug 2: the `upload_document()` method packs both string form fields and the PDF binary into a single `files=` parameter, requiring a `list[tuple[str, FileTypes]]` type annotation that does not match the `tuple[str, str]` shape of plain form fields.

Both fixes are straightforward. The datetime fix is a one-line change at two call sites in `pipeline.py`. The multipart fix restructures `upload_document()` in `paperless.py` to use `data=` for string fields and `files=` for the PDF binary, which is the idiomatic httpx approach and eliminates the type mismatch entirely.

**Primary recommendation:** Use `data=` dict for form fields + `files=` for the PDF upload. Change `.isoformat()` to `.strftime("%Y-%m-%d")` at both pipeline call sites.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Change `datetime.now(tz=UTC).isoformat()` to `datetime.now(tz=UTC).strftime("%Y-%m-%d")` at both call sites in `pipeline.py` (lines 134 and 421) -- Paperless-ngx `created` field expects a date, not a full datetime
- **D-02:** The `created` parameter type on `upload_document()` stays as `str | None` -- no need to change to `date` type since the API takes a string in the multipart form
- **D-03:** Separate form data fields from file uploads -- use `data=` parameter for simple string fields (`title`, `created`, `correspondent`, `tags`) and `files=` parameter only for the PDF binary. This is the idiomatic httpx approach and eliminates the type mismatch
- **D-04:** Remove the `fields: list[tuple[str, str]]` accumulation pattern and the `multipart_files` list merging. Instead, build a `data` dict for form fields and pass file separately
- **D-05:** Update `test_upload_with_created()` to verify the date-only format (`YYYY-MM-DD`) in the payload
- **D-06:** Add a test that verifies form fields are sent as `data=` (not `files=`) while the PDF is sent as `files=`

### Claude's Discretion
- Exact httpx `data=` vs `files=` parameter construction details
- Whether to add a helper function for date formatting or inline it
- Additional test cases beyond the two specified

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

## Standard Stack

No new dependencies. This phase modifies existing code using:

| Library | Version | Purpose | Notes |
|---------|---------|---------|-------|
| httpx | >=0.28.1 (already in deps) | HTTP client for multipart uploads | `data=` + `files=` is the standard mixed-content approach |
| datetime (stdlib) | N/A | Date formatting | `strftime("%Y-%m-%d")` replaces `isoformat()` |
| pytest | (already in dev deps) | Test framework | Existing test patterns reused |

**No installation needed.**

## Architecture Patterns

### Bug 1: Datetime Format (pipeline.py)

**Current (broken):**
```python
created = datetime.now(tz=UTC).isoformat()
# Produces: "2026-03-22T14:30:45.123456+00:00"
```

**Fixed:**
```python
created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
# Produces: "2026-03-22"
```

Two call sites:
1. `pipeline.py:134` -- `_handle_duplex_mismatch()` recovery path
2. `pipeline.py:421` -- `run_pipeline()` main upload path

**Discretion note:** Inline the `strftime` call directly -- no helper function needed. Both call sites are identical one-liners and a helper would add indirection for zero reuse benefit.

### Bug 2: Multipart Type Mismatch (paperless.py)

**Current (broken):**
```python
fields: list[tuple[str, str]] = [("title", title)]
# ... accumulate more fields ...
multipart_files: list[tuple[str, FileTypes]] = [
    *fields,  # tuple[str, str] doesn't match FileTypes
    ("document", (pdf_path.name, f, "application/pdf")),
]
response = self._client.post("/api/documents/post_document/", files=multipart_files)
```

**Fixed:**
```python
data: dict[str, str | list[str]] = {"title": title}
if created is not None:
    data["created"] = created
if correspondent is not None:
    data["correspondent"] = str(correspondent)
if tags:
    data["tags"] = [str(tag_id) for tag_id in tags]

with pdf_path.open("rb") as f:
    response = self._client.post(
        "/api/documents/post_document/",
        data=data,
        files={"document": (pdf_path.name, f, "application/pdf")},
    )
```

**Key detail -- tags as repeated fields:** Paperless-ngx expects tags as repeated form fields (`tags=1&tags=2`). When using `data=` with httpx, passing a list value for a key automatically sends repeated fields. The dict approach `{"tags": [str(t) for t in tags]}` handles this correctly.

### Anti-Patterns to Avoid
- **Mixing string tuples into `files=`:** The `files=` parameter is typed for file-like objects. String form fields belong in `data=`.
- **Using `isoformat()` for API date fields:** Many APIs (including Paperless-ngx) expect date-only strings, not full ISO 8601 datetimes with timezone and microseconds.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Repeated form fields for tags | Manual multipart encoding | httpx `data={"tags": [list]}` | httpx handles repeated field encoding automatically |

## Common Pitfalls

### Pitfall 1: Tags encoding with data= dict
**What goes wrong:** Using a plain dict `{"tags": "1"}` sends a single tag. Using `{"tags": [1, 2]}` with int values may encode differently than string values.
**Why it happens:** httpx `data=` encodes values as strings, but the list must contain strings explicitly for type checker satisfaction.
**How to avoid:** Convert tag IDs to strings: `[str(t) for t in tags]`. Only include the key when tags is non-empty.
**Warning signs:** Tags not appearing on uploaded documents.

### Pitfall 2: FileTypes import removal
**What goes wrong:** After removing the `multipart_files: list[tuple[str, FileTypes]]` pattern, the `FileTypes` import in the `TYPE_CHECKING` block becomes unused.
**Why it happens:** The import was only needed for the old type annotation.
**How to avoid:** Remove the `from httpx._types import FileTypes` import from the `TYPE_CHECKING` block. Ruff will flag this as F811/F401.
**Warning signs:** Linter warnings about unused imports.

### Pitfall 3: Test handler inspection of data= vs files=
**What goes wrong:** When testing the `data=` vs `files=` split, both parameters end up in the same multipart body. You cannot distinguish them by inspecting `request.content` alone.
**Why it happens:** httpx merges `data=` and `files=` into a single `multipart/form-data` body.
**How to avoid:** Inspect the raw multipart body for the presence of `Content-Disposition: form-data; name="document"; filename="..."` (file field has filename attribute) vs `Content-Disposition: form-data; name="title"` (data field has no filename). Or capture the call kwargs using a mock/spy pattern on the httpx client.
**Warning signs:** Test passes but does not actually verify the separation.

### Pitfall 4: Existing test_upload_with_created already passes date-only
**What goes wrong:** The existing test at line 117 already passes `created="2026-03-20"` -- a date-only string. This test validates the client accepts a date string, not that pipeline.py generates the right format.
**Why it happens:** The datetime format bug is in `pipeline.py`, not in `paperless.py`.
**How to avoid:** D-05 should verify the format at the pipeline level (where `strftime` is called), or add a regex assertion in the paperless test to reject datetime-format strings.
**Warning signs:** Tests pass but the real bug path is untested.

## Code Examples

### Complete upload_document refactor (paperless.py lines 99-121)

```python
# Source: httpx official docs - combining data= and files=
data: dict[str, str | list[str]] = {"title": title}
if created is not None:
    data["created"] = created
if correspondent is not None:
    data["correspondent"] = str(correspondent)
if tags:
    data["tags"] = [str(tag_id) for tag_id in tags]

with pdf_path.open("rb") as f:
    response = self._client.post(
        "/api/documents/post_document/",
        data=data,
        files={"document": (pdf_path.name, f, "application/pdf")},
    )
```

### Date format fix (pipeline.py, two sites)

```python
# Line 134 and line 421 - identical change
created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
```

### Test: verify date-only format

```python
def test_upload_with_created_date_only_format(self, sample_pdf: Path) -> None:
    """Upload rejects datetime format, accepts date-only YYYY-MM-DD."""
    captured_data: dict[str, str] = {}

    def handler(_request: httpx.Request) -> httpx.Response:
        content = _request.content.decode("utf-8", errors="replace")
        captured_data["content"] = content
        return httpx.Response(200, json="task-id")

    transport = _make_transport(handler)
    client = PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        _transport=transport,
    )
    client.upload_document(sample_pdf, title="Test", created="2026-03-22")
    content = captured_data["content"]
    assert "2026-03-22" in content
    # Ensure no time component leaked through
    assert "T" not in content.split("2026-03-22")[1].split("\r\n")[0]
    client.close()
```

### Test: verify data/files separation

```python
def test_form_fields_sent_as_data_not_files(self, sample_pdf: Path) -> None:
    """Form fields (title, created) use data=, PDF uses files=."""
    captured_data: dict[str, bytes] = {}

    def handler(_request: httpx.Request) -> httpx.Response:
        captured_data["body"] = _request.content
        return httpx.Response(200, json="task-id")

    transport = _make_transport(handler)
    client = PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        _transport=transport,
    )
    client.upload_document(sample_pdf, title="Test", created="2026-03-22")
    body = captured_data["body"].decode("utf-8", errors="replace")
    # Document field has filename attribute (file upload)
    assert 'name="document"; filename=' in body
    # Title field has NO filename attribute (form data)
    assert 'name="title"' in body
    assert 'name="title"; filename=' not in body
    client.close()
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest with strict markers and strict config |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_paperless.py tests/test_pipeline.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| D-01 | Date format is YYYY-MM-DD at pipeline call sites | unit | `uv run pytest tests/test_pipeline.py -x -k "created or upload"` | Partially (pipeline tests mock upload_document; need to verify created arg format) |
| D-03 | Form fields via data=, PDF via files= | unit | `uv run pytest tests/test_paperless.py -x -k "data_not_files or form_fields"` | No -- Wave 0 |
| D-05 | test_upload_with_created verifies date-only | unit | `uv run pytest tests/test_paperless.py::TestUploadDocument::test_upload_with_created -x` | Exists but needs assertion update |
| D-06 | Test data/files separation | unit | `uv run pytest tests/test_paperless.py -x -k "form_fields"` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_paperless.py tests/test_pipeline.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green + `uv run ruff check .` + `uv run ty check` + `uv run pyrefly check`

### Wave 0 Gaps
- [ ] New test for D-06: form fields sent as `data=` not `files=` in `tests/test_paperless.py`
- [ ] Update existing `test_upload_with_created` to assert date-only format (D-05)

## Sources

### Primary (HIGH confidence)
- [httpx official docs - QuickStart](https://www.python-httpx.org/quickstart/) -- confirms `data=` + `files=` combined usage for mixed multipart uploads
- Direct code inspection of `src/saneless/paperless.py` and `src/saneless/pipeline.py` -- confirmed exact bug locations and patterns

### Secondary (MEDIUM confidence)
- [httpx multipart test examples](https://github.com/encode/httpx/blob/master/tests/test_multipart.py) -- reference for testing multipart encoding
- [Python httpx form-data upload tutorial](https://www.slingacademy.com/article/python-how-to-upload-files-with-httpx-form-data/) -- confirms list values in data= produce repeated fields

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new deps, stdlib datetime and existing httpx
- Architecture: HIGH -- direct code inspection, exact line numbers known, httpx docs confirm approach
- Pitfalls: HIGH -- based on direct code reading and known httpx behavior

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable -- stdlib datetime and httpx multipart are mature APIs)
