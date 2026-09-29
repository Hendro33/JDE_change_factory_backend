"""The first live connection: TLS trust, identity capture, authentication
limits, the dedicated account, the bound sample read and readiness.
The connection is always live. No real JDE: a local HTTPS server (real
TLS) or this file's own httpx mock of the customer's AIS."""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import ssl
import threading

import httpx
import pytest

from ._discovery import save_credential, save_profile
from .conftest import headers
from .test_discovery_policy import _live

H = headers("vdb")


# ---------------------------------------------------------------------
# A fake AIS for the live transport (no network)
# ---------------------------------------------------------------------
def fake_ais(requests: list, *, env="JPS920", role="JADEREAD", release="9.2.26.2", f00941=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/tokenrequest"):
            return httpx.Response(200, json={"username": body.get("username"), "environment": env, "role": role,
                                             "jasserver": "https://jas.example", "userInfo": {"token": "tok", "appsRelease": "E920"}})
        if path.endswith("/logout"):
            return httpx.Response(200, json={})
        if path.endswith("/defaultconfig"):
            return httpx.Response(200, json={"aisVersion": release, "defaultEnvironment": "JPS920", "defaultRole": "*ALL",
                                             "defaultJasServer": "https://jas.example"})
        if path.endswith("/dataservice"):
            table = body["targetName"]
            rows = {"F0005": [{"F0005_DRSY": "00", "F0005_DRRT": "DT", "F0005_DRKY": "SO", "F0005_DRDL01": "Sales Order"}],
                    "F00941": f00941 or []}[table]
            return httpx.Response(200, json={f"fs_DATABROWSE_{table}": {"data": {"gridData": {"rowset": rows, "summary": {"moreRecords": False}}}}})
        return httpx.Response(404)
    return handler


READS = [{"capabilityId": "udc_values", "targets": ["00/DT"], "fields": ["DRSY", "DRRT", "DRKY", "DRDL01"]},
         {"capabilityId": "table_browse", "targets": ["F00941"], "fields": ["EMENHV", "EMPATHCD"], "filterFields": ["EMENHV"]}]


def live_profile(client, monkeypatch, requests, *, profile=None, **fake):
    _live(client, monkeypatch, fake_ais(requests, **fake))
    save_profile(client, connectionMode="live", environment="JPS920", role="JADEREAD", pathCode="",
                 expectedApplicationRelease="9.2", expectedToolsRelease="9.2.26.2", approvedReads=READS,
                 limits={"maxRecords": 5, "timeoutSeconds": 10}, **(profile or {}))
    save_credential(client)


def items(client):
    env = client.get("/admin/jde/profile", headers=H).json()["health"]["environment"]
    return {i["item"]: i for i in env["facets"]["items"]}, env


# ---------------------------------------------------------------------
# 1. The CA bundle is really used, for every request, with host/IP checks
# ---------------------------------------------------------------------
def _cert(tmp_path, name, ip):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


@pytest.fixture()
def https_ais(tmp_path):
    """A real TLS server on 127.0.0.1 answering the AIS endpoints."""
    seen: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(("GET", self.path))
            self._reply({"aisVersion": "9.2.26.2", "defaultEnvironment": "JPS920", "defaultRole": "*ALL"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append(("POST", self.path))
            if self.path.endswith("/tokenrequest"):
                self._reply({"environment": "JPS920", "role": "JADEREAD", "userInfo": {"token": "t", "appsRelease": "E920"}})
            elif self.path.endswith("/dataservice"):
                self._reply({"fs_DATABROWSE_F0005": {"data": {"gridData": {"rowset": [{"F0005_DRKY": "SO"}]}}}})
            else:
                self._reply({})
            del body

    good_cert, good_key = _cert(tmp_path, "ais", "127.0.0.1")
    other_cert, _ = _cert(tmp_path, "other", "127.0.0.1")
    wrong_ip_cert, wrong_ip_key = _cert(tmp_path, "wrongip", "127.0.0.2")

    def serve(cert, key):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    servers = {"good": serve(good_cert, good_key), "wrong_ip": serve(wrong_ip_cert, wrong_ip_key)}
    yield {"seen": seen, "good_cert": good_cert, "other_cert": other_cert, "wrong_ip_cert": wrong_ip_cert,
           "port": servers["good"].server_address[1], "wrong_ip_port": servers["wrong_ip"].server_address[1]}
    for s in servers.values():
        s.shutdown()


def _trust(cert_path: str, host: str = "127.0.0.1"):
    import hashlib

    from jde_api_service.discovery import transport

    pem = open(cert_path).read()
    return transport.Trust(host=host, ca_pem=pem, ca_sha256=hashlib.sha256(pem.encode()).hexdigest())


def test_the_uploaded_certificate_is_used_for_every_request_with_hostname_checks(https_ais, monkeypatch, tmp_path):
    """Trust comes only from the customer's settings: the certificate the
    Admin uploaded, or the public CAs -- never from the server environment."""
    from jde_api_service.discovery import capabilities, transport

    monkeypatch.setenv("JDE_DISCOVERY_LIVE_ENABLED", "true")
    base = f"https://127.0.0.1:{https_ais['port']}"

    # No uploaded certificate: the public trust store refuses the private certificate before any HTTP is exchanged.
    with pytest.raises(transport.TransportError, match="upload the AIS certificate"):
        transport.LiveAisTransport("vdb", base, 5, trust=transport.Trust(host="127.0.0.1")).check_reachability()
    # The wrong certificate is refused too.
    with pytest.raises(transport.TransportError):
        transport.LiveAisTransport("vdb", base, 5, trust=_trust(https_ais["other_cert"])).check_reachability()
    assert https_ais["seen"] == []

    # The right certificate: token request, server defaults, a read and logout all succeed over it.
    t = transport.LiveAisTransport("vdb", base, 5, trust=_trust(https_ais["good_cert"]))
    ctx = t._client._transport._pool._ssl_context  # the context httpx actually uses
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert "verified TLS" in t.check_reachability()
    session = t.authenticate("JADEREAD_USER", "pw", "JPS920", "JADEREAD")
    plan = capabilities.build_plan(capabilities.CAPABILITIES["udc_values"], "00/DT", ["DRKY"], [], 5, environment="JPS920")
    t.read(plan, session)
    t.logout(session)
    assert [p for _, p in https_ais["seen"]] == ["/jderest/defaultconfig", "/jderest/v2/tokenrequest",
                                                "/jderest/v2/dataservice", "/jderest/v2/tokenrequest/logout"]

    # Host/IP verification stays on: a certificate for 127.0.0.2 is refused on 127.0.0.1.
    with pytest.raises(transport.TransportError):
        transport.LiveAisTransport("vdb", f"https://127.0.0.1:{https_ais['wrong_ip_port']}", 5,
                                   trust=_trust(https_ais["wrong_ip_cert"])).check_reachability()
    # The only destination is the saved AIS address.
    with pytest.raises(transport.DestinationNotAllowed, match="not the AIS address saved"):
        transport.LiveAisTransport("vdb", base, 5, trust=_trust(https_ais["good_cert"], host="ais.elsewhere.example"))


def test_a_missing_or_invalid_uploaded_certificate_keeps_live_discovery_off(monkeypatch, tmp_path):
    from jde_api_service.discovery import transport

    monkeypatch.setenv("JDE_DISCOVERY_LIVE_ENABLED", "true")
    missing = transport.Trust(host="ais-vdb.customer.example", ca_sha256="a" * 64, ca_missing=True)
    assert not transport.live_allowed_by_deployment(missing) and "not stored" in transport.live_status_detail(missing)
    bad = transport.Trust(host="ais-vdb.customer.example", ca_pem="not a certificate", ca_sha256="b" * 64)
    assert not transport.live_allowed_by_deployment(bad) and "not usable" in transport.live_status_detail(bad)
    with pytest.raises(transport.DestinationNotAllowed, match="held OFF"):
        transport.LiveAisTransport("vdb", "https://ais-vdb.customer.example", 5, trust=bad)


# ---------------------------------------------------------------------
# 2-3. Identity capture and what Test Connection may do
# ---------------------------------------------------------------------
def test_test_connection_only_authenticates_reads_server_identity_and_logs_out(client, monkeypatch):
    requests: list = []
    live_profile(client, monkeypatch, requests)
    r = client.post("/admin/jde/test-connection", headers=H).json()
    assert r["outcome"] == "ok", r
    assert [(m, p) for m, p, _ in requests] == [("GET", "/jderest/defaultconfig"), ("POST", "/jderest/v2/tokenrequest"),
                                                ("GET", "/jderest/defaultconfig"), ("POST", "/jderest/v2/tokenrequest/logout")]
    token_body = requests[1][2]
    assert token_body["username"] == "JADEDISC" and token_body["environment"] == "JPS920" and token_body["role"] == "JADEREAD"
    assert token_body["password"] == "s3cret-Discovery-pw"  # from the encrypted store only
    assert "s3cret-Discovery-pw" not in json.dumps(r)
    its, env = items(client)
    assert its["path code"]["status"] == "pending"  # never derived from the environment name
    assert its["Tools / server release"]["status"] == "verified" and its["Tools / server release"]["reported"] == "9.2.26.2"
    assert any("JPS920" in n or "*ALL" in n for n in env["facets"]["notes"])


def test_an_environment_name_difference_is_shown_not_corrected(client, monkeypatch):
    requests: list = []
    live_profile(client, monkeypatch, requests, env="JPS920")
    save_profile(client, connectionMode="live", environment="PS920", role="JADEREAD", pathCode="",
                 expectedApplicationRelease="9.2", expectedToolsRelease="9.2.26.2", approvedReads=READS,
                 limits={"maxRecords": 5, "timeoutSeconds": 10})
    r = client.post("/admin/jde/test-connection", headers=H).json()
    assert r["outcome"] == "failed"
    its, env = items(client)
    item = its["session environment"]
    assert (item["status"], item["configured"], item["reported"]) == ("mismatch", "PS920", "JPS920")
    view = client.get("/admin/jde/profile", headers=H).json()
    assert view["config"]["environment"] == "PS920"  # not rewritten
    assert not view["ready"]


def test_all_role_proves_authentication_but_never_readiness_or_a_read(client, monkeypatch):
    requests: list = []
    live_profile(client, monkeypatch, requests, role="*ALL")
    r = client.post("/admin/jde/test-connection", headers=H).json()
    view = r["profile"]
    assert view["health"]["authentication"]["state"] == "ok"
    its, _ = items(client)
    assert its["session role"]["status"] == "mismatch" and "*ALL" in its["session role"]["detail"]
    assert not view["ready"] and not view["discoveryEnabled"]
    sent = len(requests)
    s = client.post("/admin/jde/sample-read", headers=H,
                    json={"capabilityId": "udc_values", "target": "00/DT", "maxRecords": 5}).json()
    assert s["outcome"] == "blocked" and "*ALL" in s["detail"]
    assert len(requests) == sent
    r = client.put("/admin/jde/profile", headers=H, json={**client.get("/admin/jde/profile", headers=H).json()["config"],
                                                          "role": "*all", "expectedRevision": view["revision"]})
    assert r.status_code == 422  # *ALL can never be the configured role


# ---------------------------------------------------------------------
# 4-6. The bound sample read, the path code and readiness
# ---------------------------------------------------------------------
def test_the_sample_read_is_bound_to_the_approved_request(client, monkeypatch):
    requests: list = []
    live_profile(client, monkeypatch, requests)
    assert client.post("/admin/jde/test-connection", headers=H).json()["outcome"] == "ok"
    body = {"capabilityId": "udc_values", "target": "00/DT", "maxRecords": 5}
    preview = client.post("/admin/jde/sample-read/preview", headers=H, json=body).json()
    sent_before = len(requests)
    assert preview["url"] == "https://ais-vdb.customer.example/jderest/v2/dataservice" and preview["method"] == "POST"
    assert preview["body"]["targetName"] == "F0005" and preview["body"]["maxPageSize"] == "5"
    assert preview["body"]["returnControlIDs"] == "F0005.DRSY|F0005.DRRT|F0005.DRKY|F0005.DRDL01"
    assert [c["controlId"] for c in preview["body"]["query"]["condition"]] == ["F0005.DRSY", "F0005.DRRT"]
    assert len(requests) == sent_before  # the preview sends nothing
    for bad in ({**body, "fields": ["DRKY"]}, {**body, "maxRecords": 6}, {**body, "target": "00/UM"}):
        r = client.post("/admin/jde/sample-read", headers=H, json=bad)
        assert r.status_code == 422 or r.json()["outcome"] == "blocked", bad
    assert len(requests) == sent_before
    r = client.post("/admin/jde/sample-read", headers=H, json=body).json()
    assert r["outcome"] == "ok", r
    assert r["evidence"]["request_sha256"] == preview["request_sha256"]
    sent = [b for m, p, b in requests[sent_before:] if p.endswith("/dataservice")]
    assert sent == [preview["body"]]  # exactly the previewed request


def test_path_code_comes_only_from_jde(client, monkeypatch):
    requests: list = []
    live_profile(client, monkeypatch, requests, f00941=[{"F00941_EMENHV": "JPS920", "F00941_EMPATHCD": "PS920"}])
    assert client.post("/admin/jde/test-connection", headers=H).json()["outcome"] == "ok"
    r = client.post("/admin/jde/sample-read", headers=H, json={
        "capabilityId": "table_browse", "target": "F00941", "maxRecords": 1,
        "filters": [{"field": "EMENHV", "op": "=", "value": "JPS920"}]}).json()
    assert r["outcome"] == "ok", r
    view = client.get("/admin/jde/profile", headers=H).json()
    identity = next(g for g in view["readiness"] if g["id"] == "identity")
    pc = next(i for i in identity["items"] if i["id"] == "path_code")
    assert pc["satisfied"] and "PS920" in pc["detail"] and "JPS920" in pc["detail"]


def test_readiness_needs_the_dedicated_account_and_network_restriction(client, monkeypatch):
    requests: list = []
    # No network restriction recorded yet (the shared test profile has one).
    live_profile(client, monkeypatch, requests, f00941=[{"F00941_EMENHV": "JPS920", "F00941_EMPATHCD": "PS920"}],
                 profile={"networkRestriction": {}})
    assert client.post("/admin/jde/test-connection", headers=H).json()["outcome"] == "ok"
    for body in ({"capabilityId": "udc_values", "target": "00/DT", "maxRecords": 5},
                 {"capabilityId": "table_browse", "target": "F00941", "maxRecords": 1,
                  "filters": [{"field": "EMENHV", "op": "=", "value": "JPS920"}]}):
        assert client.post("/admin/jde/sample-read", headers=H, json=body).json()["outcome"] == "ok"
    view = client.get("/admin/jde/profile", headers=H).json()
    groups = {g["id"]: g for g in view["readiness"]}
    assert list(groups) == ["connectivity", "identity", "jde_authorization", "network_restriction", "runtime_safeguards"]
    assert groups["connectivity"]["satisfied"] and groups["identity"]["satisfied"]
    assert not groups["jde_authorization"]["satisfied"] and not groups["network_restriction"]["satisfied"]
    # The read-only discovery user never writes: changes go through the agents' separate DEV write user.
    safeguards = {i["id"]: i for i in groups["runtime_safeguards"]["items"]}
    assert safeguards["writes_disabled"]["satisfied"] and "separate DEV write user" in safeguards["writes_disabled"]["detail"]
    assert not view["ready"]
    assert client.post("/admin/jde/enable", headers=H, json={"expectedRevision": view["revision"]}).status_code != 200

    # A statement alone does not count: no evidence document -> still blocked.
    account = {"username": "JADEDISC", "role": "JADEREAD", "verifiedBy": "Customer security lead", "verifiedOn": "2026-09-25",
               "method": "security_configuration_review", "permitsApprovedReads": True, "rejectsProhibitedOperations": True}
    cfg = view["config"]
    save_profile(client, **{k: v for k, v in cfg.items() if k not in ("dedicatedAccount", "networkRestriction")},
                 dedicatedAccount=account,
                 networkRestriction={"backendSourceAddress": "203.0.113.10", "restrictedToSource": True,
                                     "evidence": "OCI security list rule 7 allows 7077 from 203.0.113.10 only"})
    view = client.get("/admin/jde/profile", headers=H).json()
    authz = next(g for g in view["readiness"] if g["id"] == "jde_authorization")
    assert not authz["satisfied"] and "no evidence document" in authz["items"][0]["detail"]
    assert next(g for g in view["readiness"] if g["id"] == "network_restriction")["satisfied"]

    doc = client.post("/admin/jde/artifacts", headers=H, json={
        "kind": "reference_document", "objectName": "JADEREAD", "objectType": "SECURITY", "exportFormat": "text",
        "exportedAt": "2026-09-25T08:00:00+00:00", "docTitle": "Security Workbench export for JADEREAD",
        "fileName": "jaderead_security.txt", "contentBase64": "UmVhZCBvbmx5OiBGMDAwNSwgRjAwOTQx"}).json()
    save_profile(client, **{k: v for k, v in cfg.items() if k not in ("dedicatedAccount", "networkRestriction")},
                 dedicatedAccount={**account, "evidenceArtifactIds": [f"{doc['artifactId']}@r{doc['revision']}"]},
                 networkRestriction={"backendSourceAddress": "203.0.113.10", "restrictedToSource": True,
                                     "evidence": "OCI security list rule 7 allows 7077 from 203.0.113.10 only"})
    # A material edit re-requires the machine checks for the new revision.
    view = client.get("/admin/jde/profile", headers=H).json()
    assert not view["ready"]
    assert client.post("/admin/jde/test-connection", headers=H).json()["outcome"] == "ok"
    for body in ({"capabilityId": "udc_values", "target": "00/DT", "maxRecords": 5},
                 {"capabilityId": "table_browse", "target": "F00941", "maxRecords": 1,
                  "filters": [{"field": "EMENHV", "op": "=", "value": "JPS920"}]}):
        assert client.post("/admin/jde/sample-read", headers=H, json=body).json()["outcome"] == "ok"
    view = client.get("/admin/jde/profile", headers=H).json()
    assert view["ready"], [(g["id"], [i for i in g["items"] if i["required"] and not i["satisfied"]]) for g in view["readiness"]]
    assert client.post("/admin/jde/enable", headers=H, json={"expectedRevision": view["revision"]}).status_code == 200
