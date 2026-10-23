"""Register C1, C3, K7, K15, M6/N1: the role homes and the one figure strip,
walked with role-separated users on the Hoogwegt sample."""

from __future__ import annotations

from .conftest import TEST_PASSWORD, _apply_csrf_header


def _setup(client):
    from jde_api_service.sample_data import hoogwegt
    from jde_api_service.services import auth_service, membership_service
    from jde_api_service.services.registry import get_change_service

    auth_service.create_user("hw-admin@test.local", TEST_PASSWORD, "HW Admin", user_id="u-hwadmin")
    hoogwegt.load("hw-admin@test.local")
    doms = sorted({c.business_domain_id for c in get_change_service().list_for_customer("hoogwegt") if c.business_domain_id})
    users = {"do": ("u-do", ["domain_owner"]), "am": ("u-am", ["product_manager"]),
             "both": ("u-both", ["domain_owner", "product_manager"])}
    for key, (uid, roles) in users.items():
        auth_service.create_user(f"{uid}@test.local", TEST_PASSWORD, uid, user_id=uid)
        membership_service.create_membership(uid, "hoogwegt", roles, created_by=None)
        if "domain_owner" in roles:
            m = next(x for x in membership_service.list_company_members("hoogwegt") if x["user_id"] == uid)
            membership_service.update_membership_roles(m["membership_id"], set(roles), set(doms), actor_user_id="u-hwadmin")
    return doms


def _session(uid: str):
    from fastapi.testclient import TestClient

    from jde_api_service.main import app

    c = TestClient(app)
    assert c.post("/auth/login", json={"email": f"{uid}@test.local", "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(c)
    c.headers["X-Customer-Id"] = "hoogwegt"
    return c


def test_the_domain_owner_home_has_needs_you_and_your_requests(client):
    _setup(client)
    w = _session("u-do").get("/work").json()
    # Every open business question in the Domain Owner's domains is a row, with its proposed answer.
    assert w["questions"], w["questions"]
    assert all(q["issue"]["owner"] == "domain_owner" for q in w["questions"])
    # A story whose next action is the question is shown once, as the question.
    ids_needs = {c["id"] for c in w["needsYou"]}
    assert not ids_needs & {q["changeId"] for q in w["questions"]}
    assert w["yourRequests"] and all(c["id"] not in ids_needs for c in w["yourRequests"])
    new = [c for c in w["needsYou"] if c["lifecycle"]["nextAction"]["action"] == "start_analysis"]
    assert w["attentionCount"] == len(w["needsYou"]) - len(new) + (1 if new else 0) + len(w["questions"])
    assert w["stuck"] == [] and w["perDomain"] == []   # AM sections are not the Domain Owner's


def test_the_application_manager_home_has_stuck_and_per_domain(client):
    _setup(client)
    w = _session("u-am").get("/work").json()
    assert w["stuck"] and all(s["reason"] for s in w["stuck"])
    assert w["perDomain"] and all(d["inFlight"] > 0 for d in w["perDomain"])
    assert w["yourRequests"] == []
    assert not any(q["issue"]["owner"] == "domain_owner" for q in w["questions"])


def test_one_figure_strip_everywhere(client):
    _setup(client)
    am, do = _session("u-am"), _session("u-do")
    portfolio = {f["key"]: f["value"] for f in am.get("/figures").json()}
    for c in (am, do):
        w = c.get("/work").json()
        mine = {f["key"]: f["value"] for f in w["figures"]}
        assert mine["needs_you"] == w["attentionCount"]
        for key, value in portfolio.items():
            assert mine[key] == value, key   # the same label shows the same number for everyone


def test_a_story_without_a_domain_goes_to_the_application_manager(client):
    from jde_mcp_server import backlog

    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-HOME-NODOM")
    rec = backlog._load("S-HOME-NODOM")  # noqa: SLF001
    rec["story_detail"] = {"statement": "As a buyer I want X", "acceptance_criteria": [{"id": "AC1", "text": "x"}],
                           "open_questions": ["Should the report include closed orders?"]}
    backlog._save("S-HOME-NODOM", rec)  # noqa: SLF001
    w = client.get("/work", headers={"X-Customer-Id": "vdb"}).json()
    q = [x for x in w["questions"] if x["changeId"] == "S-HOME-NODOM"]
    assert q and q[0]["issue"]["owner"] == "product_manager"


def test_the_clarification_route_follows_the_answers(client):
    """Register E12: the Domain Owner is asked the question itself (not
    'run the Architect again'); once answered, the AM is asked to rerun --
    never 'Waiting for Domain Owner' with everything answered."""
    _setup(client)
    do, am = _session("u-do"), _session("u-am")
    lc = do.get("/changes/CR-HW-010").json()["lifecycle"]
    assert lc["nextAction"]["owner"] == "domain_owner" and lc["nextAction"]["summary"].startswith("Answer JADE's question")
    assert "Architect" not in lc["nextAction"]["summary"]
    q = next(i for i in lc["issues"] if i["kind"] == "business" and not i.get("resolution"))
    r = do.post(f"/changes/CR-HW-010/issues/{q['id']}/resolution", json={"status": "answered",
                                                                         "answer": "It differs per destination country."})
    assert r.status_code == 200, r.text
    lc = am.get("/changes/CR-HW-010").json()["lifecycle"]
    assert lc["nextAction"]["action"] == "rerun_solutioning" and lc["nextAction"]["owner"] == "product_manager"
    w = am.get("/work").json()
    assert any(c["id"] == "CR-HW-010" for c in w["needsYou"])
    assert not any(c["id"] == "CR-HW-010" for c in do.get("/work").json()["needsYou"])


def test_usefulness_measures_come_from_recorded_events(client):
    _setup(client)
    rows = {r["key"]: r for r in _session("u-am").get("/insights/usefulness").json()}
    assert set(rows) == {"manual_starts", "waiting", "repeat_questions", "verified_outcomes", "first_time_right"}
    assert all(r["definition"] for r in rows.values())
