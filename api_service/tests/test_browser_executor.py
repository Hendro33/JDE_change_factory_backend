"""
The browser executor (executors/browser.py): the Functional Agent applies
what AIS cannot -- a version's processing options, a batch version's data
selection -- in the customer's JD Edwards web client, in Chromium on Jade's
server.

Real: Chromium, the host guard, the pinned certificate, Jade's own sign-in
as the DEV write user, the agent's guarded tools, the screenshots and the
read-back. Replaced at their boundaries: the web client (a fake over real
HTTPS, tests/fixtures/fake_web_client.py), AIS (the fake AIS server) and
the model (a scripted agent calling the same tools the model would).
"""

from __future__ import annotations

import json

import pytest

from ._discovery import ready_company
from .conftest import headers
from .test_agent_execution import WRITE_PASSWORD, _items, _propose, setup_execution
from .test_configuration_change_sets import READS, SCOPE
from .test_jde_live_readiness import _cert
from .test_stage1_execution_safeguards import _approve, _save_scope

H = headers("vdb")
BROWSER_ITEMS = {"tool": "configuration_change_set", "summary": "Webshop entry and invoice print", "items": [
    {"capability_id": "processing_option_update", "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE",
     "value": "SW", "purpose": "webshop entry defaults to SW"},
    {"capability_id": "batch_version_data_selection", "application": "R42565", "version": "CIQ0001",
     "specification": "Order Type (DCTO) is equal to SW", "purpose": "invoice print picks up webshop orders"},
]}


def _browser_ready() -> bool:
    try:
        from jde_api_service.executors import browser
    except Exception:  # noqa: BLE001
        return False
    return browser.available()[0]


pytestmark = pytest.mark.skipif(not _browser_ready(), reason="Chromium (playwright) is not installed here")


@pytest.fixture()
def web(client, tmp_path):
    from .fixtures.fake_web_client import FakeWebClient

    cert, key = _cert(tmp_path, "webclient", "127.0.0.1")
    fwc = FakeWebClient(cert, key, password=WRITE_PASSWORD)
    ready_company(client, "vdb", approvedReads=READS)
    _save_scope(client, "vdb", SCOPE)
    r = client.post("/admin/jde/certificates", headers=H, json={"pem": open(cert).read()})
    assert r.status_code == 201, r.text
    sha = r.json()["sha256"]
    setup_execution(client, web_client_url=fwc.url, test=False)
    cur = client.get("/admin/jde/execution", headers=H).json()
    r = client.put("/admin/jde/execution", headers=H, json={**cur["config"], "webCaCertificateSha256": sha,
                                                           "expectedRevision": cur["revision"]})
    assert r.status_code == 200, r.text
    r = client.post("/admin/jde/execution/test", headers=H)
    assert r.json()["results"]["web_client"]["state"] == "ok", r.json()["results"]
    assert r.json()["results"]["ais_write_sign_in"]["state"] == "ok", r.json()["results"]
    yield fwc
    fwc.close()


def _scripted_agent(monkeypatch, script):
    """Stand in for the model: `script(tools)` calls the browser tools the
    way the agent would, then the run ends with a result."""
    import claude_agent_sdk as sdk

    from jde_api_service.executors import browser

    captured = {}

    def build(tools):
        captured["tools"] = tools
        return tools

    async def fake_query(prompt, options):
        captured.setdefault("prompts", []).append(prompt)
        await script(captured["tools"])
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                                session_id="t", result="done")

    monkeypatch.setattr(browser, "BUILD_TOOLS", build)
    monkeypatch.setattr(sdk, "query", fake_query)
    return captured


async def _call(tools, name, **args):
    result = await getattr(tools, "call")(name, getattr(tools, name), args)
    return json.loads(result["content"][0]["text"]), bool(result.get("is_error"))


def _ref(look: dict, **match) -> str:
    for c in look["controls"]:
        if all(str(c.get(k, "")) == str(v) for k, v in match.items()):
            return c["ref"]
    raise AssertionError(f"no control {match} in {look['controls']}")


async def _agent_applies_both(tools):
    item = tools.item
    if item["kind"] == "processing_option":
        await _call(tools, "browser_open", path=f"po?app={item['application']}&ver={item['version']}")
        look, _ = await _call(tools, "browser_look")
        # Delete is refused, whatever the page says.
        refused, is_error = await _call(tools, "browser_click", ref=_ref(look, id="del"))
        assert is_error and "never clicks" in refused["error"]
        await _call(tools, "browser_fill", ref=_ref(look, id=f"po_{item['option']}"), value=item["value"])
        await _call(tools, "browser_click", ref=_ref(look, id="ok"))
        await _call(tools, "browser_open", path=f"po?app={item['application']}&ver={item['version']}")
        look, _ = await _call(tools, "browser_look")
        shown = next(c["value"] for c in look["controls"] if c.get("id") == f"po_{item['option']}")
        await _call(tools, "report_outcome", applied=True, observed=shown, note="saved and reopened")
    else:
        await _call(tools, "browser_open", path=f"ds?app={item['application']}&ver={item['version']}")
        look, _ = await _call(tools, "browser_look")
        await _call(tools, "browser_fill", ref=_ref(look, id="spec"), value=item["specification"])
        await _call(tools, "browser_click", ref=_ref(look, id="ok"))
        await _call(tools, "browser_open", path=f"ds?app={item['application']}&ver={item['version']}")
        look, _ = await _call(tools, "browser_look")
        shown = next(c["value"] for c in look["controls"] if c.get("id") == "spec")
        await _call(tools, "report_outcome", applied=True, observed=shown, note="saved and reopened")


def test_the_agent_applies_what_ais_cannot_in_the_web_client_with_a_screenshot_of_every_step(web, client, monkeypatch):
    captured = _scripted_agent(monkeypatch, _agent_applies_both)
    rec = _propose("S-BR-1", BROWSER_ITEMS)
    assert [(i["executor"], i["route"]) for i in rec["operation"]["items"]] == [("agent", "browser")] * 2
    _approve(rec["change_id"])
    r = client.post("/changes/S-BR-1/delivery/agents/run", headers=H)
    assert r.status_code == 202, r.text
    view = _items(client, "S-BR-1")
    assert view["I1"]["deliveryState"] == "applied", view["I1"]
    assert view["I2"]["deliveryState"] == "applied", view["I2"]
    # The processing option is read back LIVE through AIS; the data selection as the agent saw it after reopening.
    assert view["I1"]["applied"]["source"].startswith("live AIS read") and view["I1"]["applied"]["observed"] == "SW"
    assert view["I1"]["applied"]["before"] == "S3"
    assert view["I2"]["applied"]["observed"] == {"specification": "Order Type (DCTO) is equal to SW"}
    assert "observed in the web client by the agent" in view["I2"]["applied"]["source"]
    assert not view["I2"]["applied"]["live"]
    # Every step has a stored screenshot.
    from jde_api_service.persistence import blob_store

    shots = view["I1"]["applied"]["screenshots"]
    assert len(shots) >= 6 and all(blob_store.default().get(k)[:8] == b"\x89PNG\r\n\x1a\n" for k in shots)
    r = client.get("/changes/S-BR-1/delivery/screenshots", headers=H, params={"key": shots[0]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    other = shots[0].replace("executions/vdb/", "executions/bwm/")
    assert client.get("/changes/S-BR-1/delivery/screenshots", headers=H, params={"key": other}).status_code == 404
    # Jade signed in (the agent never had the password); nothing was deleted; only the web client was used.
    assert web.sign_ins and all(s["User"] == "JADEWRITE" and s["Environment"] == "JDV920" and s["Role"] == "JADEWRITE"
                                for s in web.sign_ins)
    assert not web.deleted
    assert all(WRITE_PASSWORD not in p for p in captured["prompts"])
    assert "Order Type (DCTO) is equal to SW" in captured["prompts"][1]
    change = client.get("/changes/S-BR-1", headers=H).json()
    assert change["exactChange"]["execution"]["writeState"] == "applied"


def test_a_read_back_that_differs_stops_the_item(web, client, monkeypatch):
    async def sloppy(tools):
        item = tools.item
        await _call(tools, "browser_open", path=f"po?app={item['application']}&ver={item['version']}")
        look, _ = await _call(tools, "browser_look")
        await _call(tools, "browser_fill", ref=_ref(look, id=f"po_{item['option']}"), value="SX")
        await _call(tools, "browser_click", ref=_ref(look, id="ok"))
        await _call(tools, "report_outcome", applied=True, observed="SW", note="claims SW")

    _scripted_agent(monkeypatch, sloppy)
    rec = _propose("S-BR-BAD", {**BROWSER_ITEMS, "items": BROWSER_ITEMS["items"][:1]})
    _approve(rec["change_id"])
    client.post("/changes/S-BR-BAD/delivery/agents/run", headers=H)
    view = _items(client, "S-BR-BAD")
    # Jade's live read shows SX, whatever the agent said: stopped for reconciliation.
    assert view["I1"]["deliveryState"] == "unknown" and "'SX'" in view["I1"]["deliveryDetail"]


def test_the_browser_reaches_only_the_customers_web_client_and_never_signs_in_for_the_agent(web, client, monkeypatch):
    async def wanders(tools):
        refused, is_error = await _call(tools, "browser_open", path="https://example.com/")
        assert is_error and "only the customer's web client" in refused["error"]
        look, _ = await _call(tools, "browser_look")
        await _call(tools, "report_outcome", applied=False, observed="", note="nothing to do")

    _scripted_agent(monkeypatch, wanders)
    rec = _propose("S-BR-WANDER", {**BROWSER_ITEMS, "items": BROWSER_ITEMS["items"][1:]})
    _approve(rec["change_id"])
    client.post("/changes/S-BR-WANDER/delivery/agents/run", headers=H)
    view = _items(client, "S-BR-WANDER")
    # The agent changed nothing: nothing could have been saved, so a person may take the item over.
    assert view["I1"]["deliveryState"] == "agent_could_not_apply" and view["I1"]["handoverAllowed"]
