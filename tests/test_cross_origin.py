"""
Tests for the cross-site request guard.

A third-party page open in the operator's browser can submit a form or call
``fetch`` against saneless and start a scan or answer a flip prompt (N-22).
ROBU-10 rejects such requests with a check modelled on Go 1.25's
``net/http.CrossOriginProtection`` (D-20):

1. ``Sec-Fetch-Site`` present: allow ``same-origin`` and ``none``, reject
   everything else, ``same-site`` included.
2. ``Sec-Fetch-Site`` absent, ``Origin`` present: allow only when Origin's
   host[:port] equals ``Host`` or an ``X-Forwarded-Host`` entry (D-21).
3. Both absent: allow, because curl and scripts are not CSRF vectors.

Branch 2 exists because browsers send ``Sec-Fetch-Site`` only to potentially
trustworthy URLs.  saneless's documented deployment is plain HTTP to a LAN IP,
where the header is never sent, so a rule built on it alone would protect
nothing there.  ``TestClient`` sends neither header by default, which makes the
plain-HTTP branch directly testable.

Covers requirement ROBU-10 (decisions D-20, D-21, D-22, D-23).
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from saneless.web.cross_origin import is_cross_origin_request

LAN_HOST = "192.168.1.5:8080"
LAN_ORIGIN = f"http://{LAN_HOST}"
EVIL_ORIGIN = "http://evil.example"


@pytest.mark.parametrize(
    ("method", "headers", "rejected"),
    [
        pytest.param(
            "GET",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-get-cross-site-allowed",
        ),
        pytest.param(
            "HEAD",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-head-cross-site-allowed",
        ),
        pytest.param(
            "OPTIONS",
            {"sec-fetch-site": "cross-site", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="safe-options-cross-site-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-origin"},
            False,
            id="branch1-same-origin-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "none"},
            False,
            id="branch1-none-allowed",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-site"},
            True,
            id="branch1-same-site-rejected",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "cross-site"},
            True,
            id="branch1-cross-site-rejected",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "SAME-ORIGIN"},
            False,
            id="branch1-value-case-insensitive",
        ),
        pytest.param(
            "POST",
            {"sec-fetch-site": "same-origin", "origin": EVIL_ORIGIN, "host": LAN_HOST},
            False,
            id="branch1-decides-over-foreign-origin",
        ),
        pytest.param(
            "POST",
            {"origin": LAN_ORIGIN, "host": LAN_HOST},
            False,
            id="branch2-origin-matches-host-allowed",
        ),
        pytest.param(
            "POST",
            {"origin": EVIL_ORIGIN, "host": LAN_HOST},
            True,
            id="branch2-foreign-origin-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "http://192.168.1.5:8000", "host": LAN_HOST},
            True,
            id="branch2-same-host-other-port-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "null", "host": LAN_HOST},
            True,
            id="branch2-null-origin-rejected",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "scan.example",
            },
            False,
            id="branch2-x-forwarded-host-allowed",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "other.example, scan.example",
            },
            False,
            id="branch2-x-forwarded-host-list-allowed",
        ),
        pytest.param(
            "POST",
            {
                "origin": "https://scan.example",
                "host": "127.0.0.1:8080",
                "x-forwarded-host": "other.example",
            },
            True,
            id="branch2-x-forwarded-host-mismatch-rejected",
        ),
        pytest.param(
            "POST",
            {"origin": "http://SCAN.example:8080", "host": "scan.EXAMPLE:8080"},
            False,
            id="branch2-host-case-insensitive",
        ),
        pytest.param(
            "POST",
            {"host": LAN_HOST},
            False,
            id="branch3-no-headers-allowed",
        ),
        pytest.param(
            "PUT",
            {"sec-fetch-site": "cross-site"},
            True,
            id="put-branch1-cross-site-rejected",
        ),
        pytest.param(
            "DELETE",
            {"origin": EVIL_ORIGIN, "host": LAN_HOST},
            True,
            id="delete-branch2-foreign-origin-rejected",
        ),
        pytest.param(
            "PATCH",
            {"origin": LAN_ORIGIN, "host": LAN_HOST},
            False,
            id="patch-branch2-origin-matches-host-allowed",
        ),
        pytest.param(
            "PATCH",
            {"sec-fetch-site": "same-site"},
            True,
            id="patch-branch1-same-site-rejected",
        ),
        pytest.param(
            "DELETE",
            {},
            False,
            id="delete-branch3-no-headers-allowed",
        ),
    ],
)
def test_is_cross_origin_request(
    method: str, headers: dict[str, str], *, rejected: bool
) -> None:
    """The verdict follows the three D-20 branches and D-21's X-Forwarded-Host."""
    assert is_cross_origin_request(method, Headers(headers)) is rejected
