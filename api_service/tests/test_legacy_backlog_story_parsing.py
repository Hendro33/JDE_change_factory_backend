"""
CR-BW-T001 was proposed directly to the mcp_server backlog (a proving
exercise, before this api_service existed) rather than through
orchestration_driver.py, so it has no EnhancementRun and no customer
link -- both are handled specially in change_service.py /
seed_service.py. This makes that pre-existing real record visible
through the API, without touching backlog.py or the record itself,
so the new Domain Owner governance flow has a real story to review.
"""

from __future__ import annotations

from jde_mcp_server import backlog

from .conftest import headers

_T001_STORY_TEXT = (
    "As a sales team member, I want delivery dates to default sensibly, "
    "so that nobody has to fix them by hand.\n\n"
    "business_context: Today the default is always today's date, which is wrong "
    "for items not immediately available.\n\n"
    "acceptance_criteria:\n"
    "- AC1: Defaults to order date + 7 working days. verified_by: test_script step 1\n"
    "- AC2: A manually entered date is never overwritten. verified_by: test_script step 2\n"
    "(a stray note that is not an AC line, and must be ignored)\n\n"
    'test_script: {"steps":[{"step":1,"action":"Create an order","expected_output":{"requestedDate":"2026-09-30"}},'
    '{"step":2,"action":"Enter a date manually","expected_output":{"requestedDate":"2026-10-15"}}]}\n\n'
    "open_questions:\n"
    "1. Should public holidays be excluded too?\n"
    "2. How many orders per week are affected?"
)


def _seed_legacy_story(customer_id: str = "bwm") -> str:
    story_id = "CR-BW-T001"
    backlog.propose_to_backlog(story_id, _T001_STORY_TEXT, {}, "Medium", source="Support / Topdesk, Topdesk T001")
    return story_id


def test_legacy_backlog_story_is_parsed_into_structured_fields(client):
    from jde_api_service.services.registry import get_customer_link_service

    story_id = _seed_legacy_story()
    get_customer_link_service().link(story_id, "bwm")

    r = client.get(f"/changes/{story_id}", headers=headers(customer="bwm"))
    assert r.status_code == 200
    us = r.json()["userStory"]

    assert us["statement"].startswith("As a sales team member")
    assert "business_context" not in us["statement"]  # section markers stripped out, not left in the statement
    assert us["businessContext"].startswith("Today the default is always today's date")

    assert [ac["id"] for ac in us["acceptanceCriteria"]] == ["AC1", "AC2"]
    assert us["acceptanceCriteria"][0]["text"] == "Defaults to order date + 7 working days."
    assert us["acceptanceCriteria"][0]["verifiedBy"] == "test_script step 1"

    assert len(us["testScript"]) == 2
    assert us["testScript"][0] == {"id": "T1", "action": "Create an order", "expected": "requestedDate: 2026-09-30"}

    assert us["openQuestions"] == [
        "Should public holidays be excluded too?",
        "How many orders per week are affected?",
    ]


def test_a_plain_unstructured_backlog_story_falls_back_unchanged(client):
    from jde_api_service.services.registry import get_customer_link_service

    story_id = "CR-BW-T099"
    backlog.propose_to_backlog(story_id, "Just a plain sentence with no sections at all.", {}, "Low")
    get_customer_link_service().link(story_id, "bwm")

    r = client.get(f"/changes/{story_id}", headers=headers(customer="bwm"))
    us = r.json()["userStory"]
    assert us["statement"] == "Just a plain sentence with no sections at all."
    assert us["acceptanceCriteria"] == []


def test_t001_customer_link_is_seeded_idempotently_at_startup(client):
    """ensure_t001_backlog_link runs on every app startup (see main.py's
    lifespan) -- verified here via the TestClient fixture, which drives
    the same startup path a real uvicorn run does (conftest.py)."""
    from jde_api_service.services.registry import get_customer_link_service

    # If a real CR-BW-T001 backlog record exists in this test's
    # isolated backlog dir it would now be linked; here we only prove
    # the link step itself ran and is idempotent, independent of
    # whether that record exists in this particular isolated run.
    service = get_customer_link_service()
    assert service.customer_for("CR-BW-T001") == "bwm"

    from jde_api_service.services.seed_service import ensure_t001_backlog_link

    ensure_t001_backlog_link(service)  # calling again must not raise or change the result
    assert service.customer_for("CR-BW-T001") == "bwm"
