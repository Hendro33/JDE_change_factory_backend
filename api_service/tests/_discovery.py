"""Shared set-up for the Architect Environment Discovery tests."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from .conftest import headers


def sim_edit(company: str, reason: str, environment: str | None = None):
    """Change the fake AIS server's DEV state as an explicit, recorded test condition."""
    from .fixtures import ais_estate

    return ais_estate.edit(company, environment or ENVIRONMENTS.get(company, "JDV920"), actor="test",
                           reason=f"TEST CONDITION: {reason}")

ENVIRONMENTS = {"vdb": "JDV920", "bwm": "JDVBWM"}


def profile_body(company: str = "vdb", **overrides) -> dict:
    # Day-aligned, so saving the profile twice in one test is not a window change.
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    body = {
        "connectionMode": "live",
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
            {"capabilityId": "table_browse", "targets": ["F4211", "F00941"],
             "fields": ["DOCO", "DCTO", "LNID", "LTTR", "EMENHV", "EMPATHCD"], "filterFields": ["DCTO", "EMENHV"]},
        ],
        "networkRestriction": {"backendSourceAddress": "203.0.113.10", "restrictedToSource": True,
                               "evidence": "customer firewall rule 7 allows AIS from 203.0.113.10 only"},
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


def _account_evidence(client, company: str) -> str:
    """The customer's evidence that the dedicated account is narrowly
    privileged (a reference document), uploaded once per company."""
    r = client.post("/admin/jde/artifacts", headers=headers(company), json={
        "kind": "reference_document", "objectName": "JADEDISC", "objectType": "SECURITY", "exportFormat": "text",
        "exportedAt": "2026-09-25T08:00:00+00:00", "docTitle": "Security Workbench export for JADEDISC",
        "fileName": "jadedisc_security.txt", "contentBase64": base64.b64encode(b"Read only: F0005, F00941").decode()})
    assert r.status_code == 200, r.text
    return f"{r.json()['artifactId']}@r{r.json()['revision']}"


def dedicated_account(client, company: str) -> dict:
    return {"username": "JADEDISC", "role": "JADEDISC", "verifiedBy": "Customer security lead",
            "verifiedOn": "2026-09-25", "method": "security_configuration_review", "permitsApprovedReads": True,
            "rejectsProhibitedOperations": True, "evidenceArtifactIds": [_account_evidence(client, company)]}


def verify_and_enable(client, company: str = "vdb") -> dict:
    """Test Connection, run every approved sample read (the environment
    master read establishes the path code), then Enable -- exactly what an
    Administrator does in Administration > JD Edwards."""
    r = client.post("/admin/jde/test-connection", headers=headers(company))
    assert r.json()["outcome"] == "ok", r.text
    env = client.get("/admin/jde/profile", headers=headers(company)).json()["config"]["environment"]
    for read in client.get("/admin/jde/profile", headers=headers(company)).json()["config"]["approvedReads"]:
        for target in (read.get("targets") or [""]):
            body = {"capabilityId": read["capabilityId"], "target": target}
            if target == "F00941":
                body.update({"maxRecords": 1, "filters": [{"field": "EMENHV", "op": "=", "value": env}]})
            r = client.post("/admin/jde/sample-read", headers=headers(company), json=body)
            assert r.json()["outcome"] == "ok", r.text
    view = client.get("/admin/jde/profile", headers=headers(company)).json()
    assert view["ready"], [(g["id"], [i for i in g["items"] if i["required"] and not i["satisfied"]])
                           for g in view["readiness"]]
    r = client.post("/admin/jde/enable", headers=headers(company), json={"expectedRevision": view["revision"]})
    assert r.status_code == 200, r.text
    return r.json()["profile"]


def ready_company(client, company: str = "vdb", **overrides) -> dict:
    overrides.setdefault("dedicatedAccount", dedicated_account(client, company))
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
