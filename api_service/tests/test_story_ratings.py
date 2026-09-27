"""Planning ratings must not substitute for any lifecycle approval."""
from .conftest import headers
from .test_delivery_queue_and_metrics import _seed_and_enhance, _through_domain_owner_approval
from jde_api_service.services.registry import get_change_service, get_domain_review_service
from jde_api_service.services.story_ratings import view
from jde_api_service.services.orchestration_driver import _user_story_from_summary

H = headers(customer="bwm")

def setup(client, monkeypatch):
    return _seed_and_enhance(client, monkeypatch, "Ratings fixture")

def get(client, sid):
    r = client.get(f"/changes/{sid}/ratings", headers=H)
    assert r.status_code == 200, r.text
    return r.json()

def confirm(client, sid, current, key="business_impact", value="Low"):
    field = {"business_impact":"businessImpact", "business_benefit":"businessBenefit", "technical_impact":"technicalImpact"}[key]
    return client.post(f"/changes/{sid}/ratings/confirm", headers=H, json={
        "key":key,"value":value,"sourceHash":current[field]["sourceHash"],"expectedRevision":current["revision"]})

def test_confirm_is_audited_persistent_and_not_an_approval(client, monkeypatch):
    sid=setup(client,monkeypatch)
    before=get_domain_review_service().get(sid).stage
    current=get(client,sid)
    assert current["businessImpact"]["status"] == "not_assessed"
    assert current["canConfirmBusiness"] and not current["canConfirmTechnical"]
    r=confirm(client,sid,current)
    assert r.status_code == 200,r.text
    assert r.json()["businessImpact"]["confirmed"] == "Low"
    assert r.json()["businessImpact"]["confirmedBy"] == "Hendro"
    assert get(client,sid)["businessImpact"]["status"] == "confirmed"
    review=get_domain_review_service().get(sid)
    assert review.stage == before
    assert review.rating_confirmations[-1].actor_id == "u-hendro"
    assert client.get("/delivery-queue",headers=H).json() == []
    assert confirm(client,sid,current).status_code == 409
    assert confirm(client,sid,get(client,sid),"business_benefit","Small").status_code == 200
    assert confirm(client,sid,get(client,sid),"business_benefit","Low").status_code == 422
    assert confirm(client,sid,get(client,sid),"business_impact","Small").status_code == 422

def test_changed_story_invalidates_confirmation(client, monkeypatch):
    sid=setup(client,monkeypatch)
    current=get(client,sid)
    assert confirm(client,sid,current).status_code == 200
    service=get_domain_review_service()
    review=service.get(sid)
    review.history[-1].user_story.statement += " Revised scope."
    service._save(review)
    updated=get(client,sid)
    assert updated["businessImpact"]["status"] == "stale"
    assert updated["businessImpact"]["confirmed"] is None
    assert confirm(client,sid,current).status_code == 409

def test_stages_and_tenant_boundaries(client, monkeypatch, ellen_client):
    sid=setup(client,monkeypatch)
    current=get(client,sid)
    assert ellen_client.get(f"/changes/{sid}/ratings",headers=H).status_code == 403
    assert client.get(f"/changes/{sid}/ratings",headers=headers("vdb")).status_code == 404
    assert confirm(client,sid,current,"technical_impact","Medium").status_code == 409
    _through_domain_owner_approval(client,sid)
    assert confirm(client,sid,get(client,sid)).status_code == 409
    r=client.post(f"/changes/{sid}/domain-review/application-manager-approve",headers=H,json={"note":"Ready"})
    assert r.status_code == 200,r.text
    current=get(client,sid)
    assert current["canConfirmTechnical"] and not current["canConfirmBusiness"]
    assert confirm(client,sid,current,"technical_impact","High").status_code == 200

def test_revoked_domain_assignment_cannot_confirm(client, monkeypatch):
    from jde_api_service.persistence.db import connection
    sid=setup(client,monkeypatch)
    current=get(client,sid)
    with connection() as conn:
        conn.execute("DELETE FROM domain_assignments")
        conn.execute("DELETE FROM membership_roles WHERE role = 'admin'")
    assert confirm(client,sid,current).status_code == 403

def test_proposals_are_not_confirmations(client,monkeypatch):
    sid=setup(client,monkeypatch)
    change=get_change_service().get_for_customer(sid,"bwm")
    change.user_story.business_impact_rating="Medium"
    change.user_story.business_benefit_rating="High"
    ratings=view(change)
    assert ratings.business_impact.proposed == "Medium"
    assert ratings.business_benefit.proposed == "High"
    assert ratings.business_impact.confirmed is None
    assert ratings.business_impact.status == "proposed"
    assert ratings.technical_impact.status == "not_assessed"

def test_ai_parser_keeps_only_supported_scales():
    story=_user_story_from_summary({"user_story":{"statement":"Example", "business_impact_rating":"Low", "business_benefit_rating":"Small"}})
    assert story.business_impact_rating == "Low"
    assert story.business_benefit_rating == "Small"
    story=_user_story_from_summary({"user_story":{"statement":"Example", "business_impact_rating":"unknown", "business_benefit_rating":"Low"}})
    assert story.business_impact_rating is None and story.business_benefit_rating is None
