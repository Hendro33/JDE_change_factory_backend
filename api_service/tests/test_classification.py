"""A story's priority and change type: chosen when a request is raised, changed
later by a member who may write, revision-checked, and counted by Insights."""

from .conftest import headers

H = headers("bwm")


def _raise(client, **extra):
    r = client.post("/change-requests", headers=H, json={
        "title": "Credit hold on key accounts", "businessSource": "Business", "rawContent": "Orders go on hold.",
        **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_defaults_until_someone_classifies(client):
    rid = _raise(client)
    change = client.get(f"/changes/{rid}", headers=H).json()
    assert (change["priority"], change["changeType"], change["classificationRevision"]) == ("Medium", "Other", 0)


def test_chosen_when_raised_then_changed_with_history(client):
    rid = _raise(client, priority="Urgent", changeType="Defect")
    change = client.get(f"/changes/{rid}", headers=H).json()
    assert (change["priority"], change["changeType"], change["classificationRevision"]) == ("Urgent", "Defect", 1)
    assert change["classifiedBy"] == "Hendro"

    r = client.put(f"/changes/{rid}/classification", headers=H, json={"priority": "High", "expectedRevision": 1})
    assert r.status_code == 200, r.text
    assert (r.json()["priority"], r.json()["changeType"], r.json()["classificationRevision"]) == ("High", "Defect", 2)
    # A save based on an older revision is refused.
    stale = client.put(f"/changes/{rid}/classification", headers=H, json={"changeType": "Enhancement",
                                                                        "expectedRevision": 1})
    assert stale.status_code == 409
    from jde_api_service.services import classification_service
    assert [h.get("priority") for h in classification_service.get(rid)["history"]] == ["Urgent", "High"]


def test_values_roles_and_customers_are_enforced(client, viewer_client):
    rid = _raise(client)
    assert client.put(f"/changes/{rid}/classification", headers=H,
                      json={"priority": "Critical", "expectedRevision": 0}).status_code == 422
    assert client.put(f"/changes/{rid}/classification", headers=H,
                      json={"changeType": "Configuration", "expectedRevision": 0}).status_code == 422
    assert viewer_client.put(f"/changes/{rid}/classification", headers=H,
                             json={"priority": "Low", "expectedRevision": 0}).status_code == 403
    assert client.put(f"/changes/{rid}/classification", headers=headers("vdb"),
                      json={"priority": "Low", "expectedRevision": 0}).status_code == 404


def test_insights_count_change_types(client):
    _raise(client, changeType="New Functionality")
    _raise(client, changeType="New Functionality")
    _raise(client, changeType="Enhancement")
    types = {t["type"]: t["count"] for t in client.get("/metrics", headers=H).json()["changeTypes"]}
    assert types["New Functionality"] == 2 and types["Enhancement"] == 1
