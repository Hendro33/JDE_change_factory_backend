"""The Hoogwegt Arnhem sample data set loads into an empty installation and
every story reads as the stage it was written for -- through the real
lifecycle, API and as-built records."""

from .conftest import TEST_PASSWORD, _apply_csrf_header

EXPECTED = {
    "CR-HW-001": ("done", "done"), "CR-HW-004": ("done", "done"), "CR-HW-005": ("release", "waiting"),
    "CR-HW-006": ("validation", "waiting"), "CR-HW-007": ("delivery", "waiting"),
    "CR-HW-008": ("solution_review", "waiting_decision"), "CR-HW-009": ("solution_review", "waiting_decision"),
    "CR-HW-010": ("solution_review", "blocked"), "CR-HW-011": ("solution_review", "waiting_decision"),
    "CR-HW-012": ("solutioning", "waiting"), "CR-HW-013": ("story_review", "waiting_decision"),
    "CR-HW-014": ("story_review", "waiting_decision"), "CR-HW-016": ("story_review", "waiting"),
    "CR-HW-017": ("done", "closed"), "CR-HW-018": ("understand", "blocked"), "CR-HW-019": ("understand", "waiting"),
}


def test_loads_every_stage_and_reads_through_the_api(isolated_dirs):
    from fastapi.testclient import TestClient

    from jde_api_service.main import app
    from jde_api_service.persistence.db import ensure_schema
    from jde_api_service.sample_data import hoogwegt
    from jde_api_service.services import auth_service

    ensure_schema()
    auth_service.create_user("admin@test.local", TEST_PASSWORD, "Admin", user_id="u-admin")
    result = {story: (phase, health) for story, phase, health in hoogwegt.load("admin@test.local")}
    for story, expected in EXPECTED.items():
        assert result[story] == expected, story
    from jde_api_service.services import classification_service

    # Loading again adds nothing new and keeps what a person changed.
    classification_service.set_classification("CR-HW-014", "hoogwegt", priority="Urgent", change_type=None,
                                              actor_id="u-admin", actor="Admin", expected_revision=None)
    assert dict((s, (p, h)) for s, p, h in hoogwegt.load("admin@test.local")) == result
    assert classification_service.get("CR-HW-014")["priority"] == "Urgent"

    with TestClient(app) as client:
        assert client.post("/auth/login", json={"email": "admin@test.local", "password": TEST_PASSWORD}).status_code == 200
        _apply_csrf_header(client)
        h = {"X-Customer-Id": "hoogwegt"}
        changes = client.get("/changes", headers=h).json()
        assert {c["id"] for c in changes} >= set(EXPECTED)
        delivered = client.get("/changes/CR-HW-003/as-built", headers=h).json()
        assert [r["status"] for r in delivered["records"]] == ["final"]
        assert all(c["complete"] for c in delivered["checkpoints_now"]), delivered["checkpoints_now"]
        story = client.get("/changes/CR-HW-003", headers=h).json()
        assert story["exactChange"]["execution"]["writeState"] == "applied"
        assert (story["priority"], story["changeType"]) == ("Medium", "Defect")
        types = {t["type"] for t in client.get("/metrics", headers=h).json()["changeTypes"]}
        assert types == {"Defect", "Enhancement", "New Functionality", "Other"}
        ratings = client.get("/changes/CR-HW-003/ratings", headers=h).json()
        assert ratings["businessImpact"]["confirmedBy"] == "Marieke Jansen"
        metrics = client.get("/metrics?period=year", headers=h).json()
        assert metrics["performance"]["changeVolume"] >= 15
        assert {d["id"] for d in client.get("/business-domains", headers=h).json()} == {
            "DOM-HW-SELL", "DOM-HW-SUPPLY", "DOM-HW-FIN", "DOM-HW-QUAL"}
