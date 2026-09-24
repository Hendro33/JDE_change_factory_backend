"""Shared set-up for the Architect Environment Discovery tests."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from .conftest import headers

ENVIRONMENTS = {"vdb": "JDV920", "bwm": "JDVBWM"}


def profile_body(company: str = "vdb", **overrides) -> dict:
    # Day-aligned, so saving the profile twice in one test is not a window change.
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    body = {
        "connectionMode": "simulation",
        "aisBaseUrl": f"https://ais-{company}.customer.example",
        "environment": ENVIRONMENTS.get(company, "JDV920"),
        "role": "JADEDISC",
        "expectedApplicationRelease": "9.2",
        "expectedToolsRelease": "9.2.8.2",
        "pathCode": "DV920",
        "customerContact": "Pat Customer",
        "cncContact": "Chris CNC",
        "networkRoute": "site-to-site VPN to the DEV AIS server only",
        "isolationEvidence": "CNC confirmed the environment maps to DEV data only (ticket CNC-12)",
        "routingIsolationConfirmed": True,
        "privilegeStatement": "JADEDISC: read-only role limited to the listed tables",
        "privilegeConfirmed": True,
        "runtimeAttestationConfirmed": True,
        "runtimeAttestationEvidence": "CNC (Chris, ticket CNC-12): JDV920 runs path code DV920 on Tools 9.2.8.2",
        "approvedReads": [
            {"capabilityId": "udc_values", "targets": ["00/DT"], "fields": ["DRSY", "DRRT", "DRKY", "DRDL01"]},
            {"capabilityId": "object_librarian", "targets": ["P4210", "P554210", "B5542001"],
             "fields": ["SIOBNM", "SIFUNO", "SISY", "SIMD"]},
            {"capabilityId": "processing_option_values", "targets": ["P4210|CIQ0001"]},
            {"capabilityId": "table_browse", "targets": ["F4211"], "fields": ["DOCO", "DCTO", "LNID", "LTTR"],
             "filterFields": ["DCTO"]},
        ],
        "discoveryWindow": {"startsAt": (now - timedelta(days=1)).isoformat(),
                            "endsAt": (now + timedelta(days=2)).isoformat()},
        "limits": {"maxRecords": 10, "timeoutSeconds": 5},
        "dataSharingPolicy": "configuration_and_artifacts",
    }
    body.update(overrides)
    return body


def save_profile(client, company: str = "vdb", **overrides) -> dict:
    current = client.get("/admin/jde/profile", headers=headers(company)).json()
    expected = current["revision"] if current["configured"] else None
    r = client.put("/admin/jde/profile", headers=headers(company),
                   json={**profile_body(company, **overrides), "expectedRevision": expected})
    assert r.status_code == 200, r.text
    return r.json()


def save_credential(client, company: str = "vdb", password: str = "s3cret-Discovery-pw") -> dict:
    rev = client.get("/admin/jde/profile", headers=headers(company)).json()["revision"]
    r = client.put("/admin/jde/credential", headers=headers(company),
                   json={"username": "JADEDISC", "password": password, "expectedRevision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def verify_and_enable(client, company: str = "vdb") -> dict:
    from jde_api_service.discovery import transport

    r = client.post("/admin/jde/test-connection", headers=headers(company))
    assert r.json()["outcome"] == "ok", r.text
    for read in client.get("/admin/jde/profile", headers=headers(company)).json()["config"]["approvedReads"]:
        r = client.post("/admin/jde/sample-read", headers=headers(company), json={"capabilityId": read["capabilityId"]})
        assert r.json()["outcome"] == "ok", r.text
    rev = client.get("/admin/jde/profile", headers=headers(company)).json()["revision"]
    r = client.post("/admin/jde/enable", headers=headers(company), json={"expectedRevision": rev})
    assert r.status_code == 200, r.text
    return r.json()["profile"]


def ready_company(client, company: str = "vdb", **overrides) -> dict:
    save_profile(client, company, **overrides)
    save_credential(client, company)
    return verify_and_enable(client, company)


def upload_artifact(client, company: str = "vdb", **overrides) -> dict:
    body = {
        "kind": "technical_export", "objectName": "B5542001", "objectType": "BSFN", "exportFormat": "c_source",
        "customerEnvironment": ENVIRONMENTS.get(company, "JDV920"), "pathCode": "DV920", "release": "9.2",
        "sourceLocation": "source/B5542001.c", "repository": "git@customer.example:jde/custom.git",
        "commitRef": "a1b2c3d", "exportedAt": "2026-09-20T10:00:00+00:00",
        "runtimeCorrespondence": "matches_dev_runtime",
        "runtimeStatement": "CNC: built into the DV920 package of 18 Sep 2026", "runtimeStatedBy": "Chris CNC",
        "fileName": "B5542001.c",
        "content": "/* Custom credit check */\nif (mnCreditLimit < mnOrderTotal) { jdeErrorSet(\"4A7\"); }\n",
    }
    body.update(overrides)
    content = body.pop("content")
    body["contentBase64"] = base64.b64encode(content.encode() if isinstance(content, str) else content).decode()
    r = client.post("/admin/jde/artifacts", headers=headers(company), json=body)
    assert r.status_code == 200, r.text
    return r.json()
