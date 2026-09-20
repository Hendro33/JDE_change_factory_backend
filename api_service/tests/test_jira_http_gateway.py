"""
Tests for JiraHttpGateway -- the live connector's actual HTTP request
construction, exercised against httpx.MockTransport rather than a real
Jira site. Confirms the exact request shapes the architecture
assessment specified: enhanced JQL search, transition discovery by the
target status NAME (via transitions[].to.name, not by the transition's
own action name), and Atlassian Document Format for the comment body.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jde_api_service.services.jira_gateway import JiraHttpGateway


def _gateway(transport: httpx.MockTransport) -> JiraHttpGateway:
    return JiraHttpGateway(email="bot@example.com", api_token="secret-token", transport=transport)


def test_search_issues_sends_configured_jql_and_parses_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "issues": [
                {
                    "key": "CON-42",
                    "id": "10042",
                    "fields": {
                        "summary": "Default delivery date is wrong",
                        "description": {
                            "type": "doc", "version": 1,
                            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "It defaults to today."}]}],
                        },
                        "reporter": {"displayName": "Ellen Vos"},
                        "created": "2026-09-01T09:00:00.000+0000",
                        "issuetype": {"name": "Change"},
                        "priority": {"name": "Medium"},
                        "customfield_10057": None,
                    },
                }
            ],
        })

    gateway = _gateway(httpx.MockTransport(handler))
    issues = gateway.search_issues_in_status(
        base_url="https://bicycleworks.atlassian.net", project_key="CON",
        status_name="Ready for Jade", jade_id_field="customfield_10057",
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/rest/api/3/search/jql")
    assert captured["body"]["jql"] == 'project = "CON" AND status = "Ready for Jade" ORDER BY created ASC'
    assert "customfield_10057" in captured["body"]["fields"]

    assert len(issues) == 1
    issue = issues[0]
    assert issue.key == "CON-42"
    assert issue.summary == "Default delivery date is wrong"
    assert issue.description == "It defaults to today."
    assert issue.reporter == "Ellen Vos"
    assert issue.metadata == {"workType": "Change", "priority": "Medium"}
    assert issue.jade_id_field_value is None


def test_search_issues_paginates_using_next_page_token():
    pages = [
        {"issues": [{"key": "CON-1", "id": "1", "fields": {}}], "nextPageToken": "page-2"},
        {"issues": [{"key": "CON-2", "id": "2", "fields": {}}]},
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(calls) - 1])

    gateway = _gateway(httpx.MockTransport(handler))
    issues = gateway.search_issues_in_status(
        base_url="https://x.atlassian.net", project_key="CON",
        status_name="Ready for Jade", jade_id_field="customfield_1",
    )

    assert [i.key for i in issues] == ["CON-1", "CON-2"]
    assert "nextPageToken" not in calls[0]
    assert calls[1]["nextPageToken"] == "page-2"


def test_find_transition_id_matches_target_status_name_not_action_name():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "transitions": [
                {"id": "11", "name": "Start progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Hand off to Jade", "to": {"name": "Jade - In Progress"}},
            ]
        })

    gateway = _gateway(httpx.MockTransport(handler))
    transition_id = gateway.find_transition_id(
        base_url="https://x.atlassian.net", issue_key="CON-42", target_status_name="Jade - In Progress",
    )
    assert transition_id == "31"


def test_find_transition_id_returns_none_when_target_not_reachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"transitions": [{"id": "11", "name": "Close", "to": {"name": "Closed"}}]})

    gateway = _gateway(httpx.MockTransport(handler))
    assert gateway.find_transition_id(
        base_url="https://x.atlassian.net", issue_key="CON-42", target_status_name="Jade - In Progress",
    ) is None


def test_add_comment_sends_atlassian_document_format():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(201, json={})

    gateway = _gateway(httpx.MockTransport(handler))
    gateway.add_comment(base_url="https://x.atlassian.net", issue_key="CON-42", body="Jade has accepted this request.")

    assert captured["url"].endswith("/rest/api/3/issue/CON-42/comment")
    adf = captured["body"]["body"]
    assert adf["type"] == "doc"
    assert adf["content"][0]["content"][0]["text"] == "Jade has accepted this request."


def test_set_field_puts_only_the_configured_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    gateway = _gateway(httpx.MockTransport(handler))
    gateway.set_field(base_url="https://x.atlassian.net", issue_key="CON-42", field_id="customfield_10057", value="CR-JIRA-CON-42")

    assert captured["method"] == "PUT"
    assert captured["body"] == {"fields": {"customfield_10057": "CR-JIRA-CON-42"}}


def test_live_gateway_refuses_without_credentials():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be reached
        raise AssertionError("no request should be sent without credentials")

    gateway = JiraHttpGateway(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="no Jira credentials"):
        gateway.set_field(base_url="https://x.atlassian.net", issue_key="CON-1", field_id="customfield_1", value="x")
