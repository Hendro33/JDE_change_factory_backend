"""
Tests for jira_gateway.test_live_connection -- Admin > Integrations >
Jira's "Test Connection" button. Exercised against httpx.MockTransport,
same convention as test_jira_http_gateway.py. Covers the design's own
requirements: always a real call regardless of mock mode (there is no
mock-mode branch inside this function at all -- it takes a transport
purely as a test seam), never anything but a safe, token-free message,
and a distinct outcome for "bad credentials" vs. "bad/inaccessible
project" vs. "can't reach the site at all".
"""

from __future__ import annotations

import httpx
import pytest

from jde_api_service.services.jira_gateway import InvalidJiraBaseUrl, normalize_jira_base_url
from jde_api_service.services.jira_gateway import test_live_connection as check_live_connection

# pytest would otherwise try to collect the imported test_live_connection
# itself as a test function (its name matches pytest's own naming
# convention) -- the alias above avoids that; every call site below uses it.


def test_normalize_jira_base_url_strips_trailing_slash():
    assert normalize_jira_base_url("https://x.atlassian.net/") == "https://x.atlassian.net"


def test_normalize_jira_base_url_rejects_empty():
    with pytest.raises(InvalidJiraBaseUrl, match="required"):
        normalize_jira_base_url("")


def test_normalize_jira_base_url_rejects_a_project_or_queue_link():
    with pytest.raises(InvalidJiraBaseUrl, match="project, queue, board or issue link"):
        normalize_jira_base_url("https://x.atlassian.net/jira/software/projects/CON/issues")


def test_normalize_jira_base_url_rejects_non_http_scheme():
    with pytest.raises(InvalidJiraBaseUrl):
        normalize_jira_base_url("ftp://x.atlassian.net")


def test_normalize_jira_base_url_rejects_a_bare_host_with_no_scheme():
    with pytest.raises(InvalidJiraBaseUrl):
        normalize_jira_base_url("x.atlassian.net")


def test_bad_site_url_is_refused_before_any_network_call():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net/browse/CON-1", email="bot@example.com", api_token="secret",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False
    assert "project, queue, board or issue link" in message


def test_missing_fields_are_refused_before_any_network_call():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    ok, message = check_live_connection(
        base_url="", email="bot@example.com", api_token="secret", transport=httpx.MockTransport(handler)
    )
    assert ok is False
    assert "site url" in message.lower()

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net", email="", api_token="", transport=httpx.MockTransport(handler)
    )
    assert ok is False
    assert "email and api token" in message.lower()


def test_successful_connection_without_a_project_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/myself"
        return httpx.Response(200, json={"displayName": "Jade Bot", "emailAddress": "bot@example.com"})

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net/", email="bot@example.com", api_token="secret",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True
    assert "Jade Bot" in message
    assert "secret" not in message


def test_successful_connection_with_an_accessible_project():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "Jade Bot"})
        assert request.url.path.endswith("/project/CON")
        return httpx.Response(200, json={"name": "ConsultIQ Pilot"})

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net", email="bot@example.com", api_token="secret", project_key="CON",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True
    assert "CON" in message
    assert "ConsultIQ Pilot" in message


def test_bad_credentials_are_reported_as_authentication_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net", email="bot@example.com", api_token="wrong-token",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False
    assert "authentication failed" in message.lower()
    assert "wrong-token" not in message


def test_inaccessible_project_is_reported_distinctly_from_bad_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "Jade Bot"})
        return httpx.Response(404, json={})

    ok, message = check_live_connection(
        base_url="https://x.atlassian.net", email="bot@example.com", api_token="secret", project_key="NOPE",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False
    assert "NOPE" in message
    assert "Jade Bot" in message  # auth already succeeded -- the message says so


def test_unreachable_site_is_reported_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    ok, message = check_live_connection(
        base_url="https://does-not-exist.example", email="bot@example.com", api_token="secret",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False
    assert "does-not-exist.example" in message
