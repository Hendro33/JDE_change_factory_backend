"""
JDE connection trust from the app: an Admin uploads the AIS server
certificate and saves the address; no server settings are needed. Certificate
and host-name/IP verification stay on, a private key is refused, and a saved
password only ever goes to the address and certificate it was entered for.
"""

from __future__ import annotations

import httpx
import pytest

from ._discovery import save_credential, save_profile
from .conftest import headers
from .test_jde_live_readiness import READS, _cert, fake_ais, https_ais  # noqa: F401 -- https_ais is a fixture

H = headers("vdb")


def _upload(client, pem: str, company: str = "vdb"):
    return client.post("/admin/jde/certificates", headers=headers(company), json={"pem": pem})


def _clear_server_settings(monkeypatch):
    for k in ("JDE_DISCOVERY_LIVE_ENABLED", "JDE_DISCOVERY_ALLOWED_HOSTS", "JDE_DISCOVERY_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)


def test_an_uploaded_certificate_alone_connects_over_verified_tls(client, https_ais, monkeypatch):  # noqa: F811
    from jde_api_service.discovery import service, transport

    _clear_server_settings(monkeypatch)
    monkeypatch.setattr(service, "LIVE_HTTP_TRANSPORT", None)  # the real network stack
    base = f"https://127.0.0.1:{https_ais['port']}"
    cert = _upload(client, open(https_ais["good_cert"]).read())
    assert cert.status_code == 201, cert.text
    summary = cert.json()
    assert "127.0.0.1" in summary["certificates"][0]["names"] and len(summary["sha256"]) == 64
    save_profile(client, connectionMode="live", aisBaseUrl=base, environment="JPS920", role="JADEREAD",
                 pathCode="", approvedReads=READS, caCertificateSha256=summary["sha256"])
    save_credential(client)
    view = client.get("/admin/jde/profile", headers=H).json()
    assert view["certificate"]["coversHost"] and view["credentialBound"] and view["liveAllowedByDeployment"]
    r = client.post("/admin/jde/test-connection", headers=H).json()
    reach = client.get("/admin/jde/profile", headers=H).json()["health"]["reachability"]
    assert reach["state"] == "ok" and "uploaded in the settings" in reach["detail"], r
    assert ("POST", "/jderest/v2/tokenrequest") in https_ais["seen"]

    # Verification stays on: that certificate does not make a server for 127.0.0.2 acceptable on 127.0.0.1.
    wrong = open(https_ais["wrong_ip_cert"]).read()
    trust = transport.Trust(host="127.0.0.1", ca_pem=wrong, ca_sha256="x" * 64)
    with pytest.raises(transport.TransportError):
        transport.LiveAisTransport("vdb", f"https://127.0.0.1:{https_ais['wrong_ip_port']}", 5,
                                   trust=trust).check_reachability()


def test_certificate_uploads_are_validated_and_company_scoped(client, tmp_path):
    cert_path, key_path = _cert(tmp_path, "ais", "10.0.0.5")
    pem, key = open(cert_path).read(), open(key_path).read()
    assert _upload(client, pem + key).status_code == 422          # never a private key
    assert _upload(client, "hello").status_code == 422            # not a certificate
    assert _upload(client, pem * 200).status_code == 422          # too large
    ok = _upload(client, pem)
    assert ok.status_code == 201 and ok.json()["certificates"][0]["names"] == ["10.0.0.5"]
    # Another company cannot select this company's certificate.
    body = {"connectionMode": "live", "caCertificateSha256": ok.json()["sha256"]}
    from ._discovery import profile_body

    r = client.put("/admin/jde/profile", headers=headers("nhd"),
                   json={**profile_body("nhd", **body), "expectedRevision": None})
    assert r.status_code == 422 and "not one uploaded for this company" in r.json()["detail"]


def test_a_saved_password_only_goes_to_the_address_and_certificate_it_was_entered_for(client, monkeypatch):
    from jde_api_service.discovery import service

    _clear_server_settings(monkeypatch)
    requests: list = []
    monkeypatch.setattr(service, "LIVE_HTTP_TRANSPORT", httpx.MockTransport(fake_ais(requests)))
    save_profile(client, connectionMode="live", aisBaseUrl="https://ais-a.customer.example", environment="JPS920",
                 role="JADEREAD", pathCode="", approvedReads=READS)
    save_credential(client)
    assert client.get("/admin/jde/profile", headers=H).json()["credentialBound"]

    # The address changes: the saved password is not sent there.
    save_profile(client, connectionMode="live", aisBaseUrl="https://ais-b.elsewhere.example", environment="JPS920",
                 role="JADEREAD", pathCode="", approvedReads=READS)
    view = client.get("/admin/jde/profile", headers=H).json()
    assert not view["credentialBound"] and "re-enter" in view["credentialStorage"]
    requests.clear()
    r = client.post("/admin/jde/test-connection", headers=H).json()
    assert r["outcome"] == "failed" and "re-enter" in r["detail"]
    assert ("POST", "/jderest/v2/tokenrequest") not in requests
    # Entered again for the new address: usable.
    save_credential(client)
    assert client.get("/admin/jde/profile", headers=H).json()["credentialBound"]
