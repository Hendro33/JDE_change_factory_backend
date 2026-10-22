"""JADE places a new story in the business domain whose owner reviews it;
the Domain Owner it lands with can move it to another domain -- also mid
review -- which hands it to that domain's owner, back at the start."""

from __future__ import annotations

from .conftest import headers
from .test_domain_owner_authority import _member, _story


def _user_story():
    from jde_api_service.models.change import UserStory

    return UserStory(statement="As a clerk I want X", business_context="ctx", acceptance_criteria=[], test_script=[])


def test_jade_places_the_story_in_the_domain_it_chose(client):
    from jde_api_service.services import orchestration_driver
    from jde_api_service.services.registry import get_domain_review_service

    sid = _story(client, "S-PLACE")
    assert orchestration_driver.place_in_domain("bwm", sid, _user_story(), {"id": "nope"}) is None
    assert orchestration_driver.place_in_domain("bwm", sid, _user_story(), None) is None
    placed = orchestration_driver.place_in_domain(
        "bwm", sid, _user_story(), {"id": "DOM-BWM-WAREHOUSE", "confidence": "high", "reason": "Stock handling"})
    assert placed == "DOM-BWM-WAREHOUSE"
    review = get_domain_review_service().get(sid)
    assert review.business_domain_id == "DOM-BWM-WAREHOUSE"
    assert review.domain_classification_note.startswith("Placed by JADE (high confidence)")
    # A story that already has a domain is never moved by JADE.
    assert orchestration_driver.place_in_domain("bwm", sid, _user_story(), {"id": "DOM-BWM-ORDER-FULFIL"}) is None
    assert "DOM-BWM-WAREHOUSE" in orchestration_driver._domains_block("bwm")  # noqa: SLF001


def test_the_domain_owner_moves_a_story_to_another_domain(client):
    from jde_api_service.services import orchestration_driver

    owner = _member("u-own", "own@test.local", "Owen Owner", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    sid = _story(client, "S-MOVE-ON")
    orchestration_driver.place_in_domain("bwm", sid, _user_story(), {"id": "DOM-BWM-WAREHOUSE"})
    assert owner.post(f"/changes/{sid}/domain-review/start", headers=headers("bwm"), json={}).status_code == 200
    r = owner.post(f"/changes/{sid}/domain-review/assign-domain", headers=headers("bwm"),
                   json={"businessDomainId": "DOM-BWM-ORDER-FULFIL", "note": "This is order handling"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["businessDomainId"] == "DOM-BWM-ORDER-FULFIL" and body["stage"] == "ready_for_domain_owner"
    assert "Owen Owner, Domain Owner" in body["domainClassificationNote"]
    # Now it is no longer theirs: they cannot move it again or review it.
    again = owner.post(f"/changes/{sid}/domain-review/assign-domain", headers=headers("bwm"),
                       json={"businessDomainId": "DOM-BWM-WAREHOUSE"})
    assert again.status_code == 403
    assert owner.post(f"/changes/{sid}/domain-review/start", headers=headers("bwm"), json={}).status_code == 403


def test_a_domain_owner_must_name_the_new_domain(client):
    from jde_api_service.services import orchestration_driver

    owner = _member("u-own", "own@test.local", "Owen Owner", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    sid = _story(client, "S-MOVE-NONE")
    orchestration_driver.place_in_domain("bwm", sid, _user_story(), {"id": "DOM-BWM-WAREHOUSE"})
    r = owner.post(f"/changes/{sid}/domain-review/assign-domain", headers=headers("bwm"), json={"uncertain": True})
    assert r.status_code == 422


def test_a_confirmed_process_places_the_story_in_its_domain(client):
    from types import SimpleNamespace

    from jde_api_service.process import story as story_process

    doms = [SimpleNamespace(apqc_code="9.0", id="FIN"), SimpleNamespace(apqc_code="9.3", id="GL"),
            SimpleNamespace(apqc_code="3.0", id="SELL")]
    assert story_process.domain_for_process(doms, "9.3.2.4").id == "GL"
    assert story_process.domain_for_process(doms, "9.6.1").id == "FIN"
    assert story_process.domain_for_process(doms, "3.5.4").id == "SELL"
    assert story_process.domain_for_process(doms, "4.4.3") is None
