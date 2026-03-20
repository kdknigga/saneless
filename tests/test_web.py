"""
Xfail test stubs for web endpoint tests (Phase 3).

Covers requirements: UI-01 through UI-08, PROF-03, PLSS-04,
HLTH-01, HLTH-02, LOG-03.
"""

import pytest


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_page_loads() -> None:
    """GET / returns 200 with form elements (UI-01)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_status_polling() -> None:
    """GET /api/jobs/current/status returns status partial (UI-02)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_status_polling_active_job() -> None:
    """Active job triggers hx-trigger polling attributes (UI-02)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_flip_prompt() -> None:
    """AWAITING_FLIP state shows flip prompt with PRD wording (UI-03)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_thumbnail_display() -> None:
    """Job with thumbnail shows base64 img tag (UI-04)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_job_history() -> None:
    """GET /api/jobs/history returns job list (UI-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_scan_button_disabled() -> None:
    """Scan button disabled during active job (UI-07)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_cache_invalidate() -> None:
    """POST /api/cache/invalidate refreshes resource (UI-08)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_profile_dropdown() -> None:
    """Profile names from settings appear in dropdown (PROF-03)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_scan_form_submit() -> None:
    """POST /api/scan creates job and returns status (PLSS-04)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_health_endpoint() -> None:
    """GET /health returns 200 with status ok (HLTH-01)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_health_no_auth() -> None:
    """GET /health requires no authentication (HLTH-02)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-03")
def test_error_display() -> None:
    """Error state shows error message in status area (LOG-03)."""
    pytest.fail("Not implemented")
