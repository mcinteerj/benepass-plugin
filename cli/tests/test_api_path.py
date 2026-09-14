"""The API path is caller-supplied (`benepass api <path>`), so it is validated.

Every request carries `Authorization: Bearer <access token>`, and httpx sends it
to whatever host the URL resolves to. A path that does not start with a single
"/" can change that host — which would hand the credential to someone else — so
the URL builder refuses one rather than trusting the caller.
"""

from __future__ import annotations

import httpx
import pytest

from benepass import api


def test_a_normal_path_builds_the_benepass_url() -> None:
    assert (
        api.build_url("/v2/me/accounts/")
        == "https://api.benefitsapi.com/v2/me/accounts/"
    )


@pytest.mark.parametrize(
    "path",
    [
        "@evil.example/v2/me/",  # userinfo trick: host becomes evil.example
        ".evil.example/v2/me/",  # suffix trick: api.benefitsapi.com.evil.example
        "//evil.example/v2/me/",  # protocol-relative
        "\\\\evil.example/v2/me/",  # backslashes some parsers treat as slashes
        "v2/me/accounts/",  # no leading slash at all
        "https://evil.example/v2/me/",
    ],
)
def test_a_path_that_could_change_the_host_is_refused(path: str) -> None:
    with pytest.raises(api.BenepassError, match="invalid API path"):
        api.build_url(path)


def test_the_refusal_is_not_cosmetic() -> None:
    """The rejected forms really do resolve elsewhere — that is why they are rejected."""
    assert httpx.URL(f"{api.API_BASE_URL}@evil.example/v2/me/").host == "evil.example"
    assert (
        httpx.URL(f"{api.API_BASE_URL}.evil.example/v2/me/").host
        == "api.benefitsapi.com.evil.example"
    )
