"""
The BicycleWorks Manufacturing BV pilot dataset: 9 mock Topdesk
tickets + 2 direct business inputs, entering the common ChangeRequest
model via the mock Topdesk connector and create_direct respectively.
"""

from __future__ import annotations

from jde_api_service.persistence.pilot_data_bicycleworks import (
    CUSTOMER_ID,
    DIRECT_INPUTS,
    TOPDESK_TICKETS,
)
from jde_api_service.services.registry import get_change_request_service
from jde_api_service.services.seed_service import ensure_bicycleworks_pilot_dataset

from .conftest import headers

ALL_STABLE_IDS = [f"CR-BW-{t.ticket_number}" for t in TOPDESK_TICKETS] + [
    f"CR-BW-{d.request_id}" for d in DIRECT_INPUTS
]


def test_seeding_creates_all_eleven_records_exactly_once(isolated_dirs):
    service = get_change_request_service()

    created_first = ensure_bicycleworks_pilot_dataset(service)
    assert sorted(created_first) == sorted(ALL_STABLE_IDS)
    assert len(service.list_for_customer(CUSTOMER_ID)) == 11

    # Re-running must be a no-op: same records, nothing duplicated.
    created_second = ensure_bicycleworks_pilot_dataset(service)
    assert created_second == []
    assert len(service.list_for_customer(CUSTOMER_ID)) == 11


def test_all_records_are_scoped_to_bicycleworks_only(isolated_dirs):
    service = get_change_request_service()
    ensure_bicycleworks_pilot_dataset(service)

    for stable_id in ALL_STABLE_IDS:
        record = service.get(stable_id)
        assert record is not None, f"missing {stable_id}"
        assert record.customer_id == "bwm"


def test_topdesk_tickets_carry_topdesk_source_type_and_reference(isolated_dirs):
    service = get_change_request_service()
    ensure_bicycleworks_pilot_dataset(service)

    t001 = service.get("CR-BW-T001")
    assert t001.source_type.value == "TOPDESK"
    assert t001.source_reference == "Topdesk T001"
    assert t001.business_source == "Support / Topdesk"
    assert t001.title == "Default delivery date is wrong"


def test_direct_inputs_carry_direct_source_type(isolated_dirs):
    service = get_change_request_service()
    ensure_bicycleworks_pilot_dataset(service)

    p001 = service.get("CR-BW-P001")
    assert p001.source_type.value == "DIRECT"
    assert p001.business_source == "Business"
    assert p001.title == "New dealer returns process"


def test_source_text_is_preserved_verbatim(isolated_dirs):
    service = get_change_request_service()
    ensure_bicycleworks_pilot_dataset(service)

    t007 = service.get("CR-BW-T007")
    assert "Customer: C10045" in t007.raw_content
    assert "Credit limit: €50,000" in t007.raw_content

    t009 = service.get("CR-BW-T009")
    assert t009.raw_content == (
        "JDE is causing problems with our bike orders. Please fix it urgently. "
        "Management is unhappy."
    )


def test_no_ai_generated_fields_are_pre_populated(client):
    r = client.get("/changes/CR-BW-T003", headers=headers(customer="bwm"))
    assert r.status_code == 200
    change = r.json()
    assert change["state"] == "RECEIVED"
    assert change["userStory"] is None
    assert change["architectDecision"] is None
    assert change["implementationSpec"] is None
    assert change["exactChange"] is None
    assert change["evidence"] == []


def test_pilot_dataset_is_visible_through_the_api_scoped_to_bicycleworks(client):
    r = client.get("/changes", headers=headers(customer="bwm"))
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()}
    assert ids == set(ALL_STABLE_IDS)

    # And invisible under a different, unrelated customer.
    r = client.get("/changes", headers=headers(customer="vdb"))
    assert not any(c["id"].startswith("CR-BW-") for c in r.json())


def test_t009_is_deliberately_inadequate_and_not_fixed_up(client):
    r = client.get("/changes/CR-BW-T009", headers=headers(customer="bwm"))
    change = r.json()
    assert change["originalRequest"] == (
        "JDE is causing problems with our bike orders. Please fix it urgently. "
        "Management is unhappy."
    )
    # No invented business impact, complexity, or anything else beyond
    # what raw intake actually carries.
    assert all(v == "" for v in change["businessImpact"].values())
    assert change["complexitySignal"] == "Unknown"


def test_bicycleworks_customer_is_registered_and_reachable_by_consultant(client):
    r = client.get("/session", headers=headers(customer=None))
    body = r.json()
    bwm = next((c for c in body["customers"] if c["id"] == "bwm"), None)
    assert bwm is not None
    assert bwm["name"] == "BicycleWorks Manufacturing BV"
    assert bwm["environment"] == "DEV"
