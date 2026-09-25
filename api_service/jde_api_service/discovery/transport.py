"""
How discovery reads reach an AIS server -- or a clearly labelled
simulation of one.

  * SimulatedAisEndpoint: a stand-in that answers from the ONE shared
    simulated DEV estate (jde_mcp_server.sim_estate) for the company and the
    profile's environment -- the same estate simulated execution changes, so
    an applied simulated change is visible to the next discovery read.
    Chosen ONLY when the profile's connection_mode is "simulation"; every
    result it produces is labelled SIMULATION.
  * LiveAisTransport: httpx with TLS verification, no redirects, a short
    timeout and exactly one permitted destination: the host of the saved AIS
    address. Its trust (see Trust) comes from the company's connection
    settings in Jade. The server operator can still lock live discovery off
    (JDE_DISCOVERY_LIVE_ENABLED=false) or narrow destinations
    (JDE_DISCOVERY_ALLOWED_HOSTS). There is no fallback in either direction:
    a live profile that cannot connect fails; it never quietly simulates.

Both only accept a ReadPlan that has passed capabilities.assert_read_semantics,
and only the fixed auth and read endpoints. No retries, no pagination.
A circuit breaker stops calls to a company's endpoint after repeated
transport failures.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .. import config as _config  # noqa: F401 -- makes jde_mcp_server importable
from . import capabilities
from .capabilities import AUTH_ENDPOINTS, ReadPlan

from jde_mcp_server import sim_estate  # noqa: E402

# Documented defaultconfig fields Jade keeps (server-level defaults).
DEFAULTCONFIG_KEYS = ("aisVersion", "defaultEnvironment", "defaultRole", "defaultJasServer")
LIVE_ENABLED_ENV = "JDE_DISCOVERY_LIVE_ENABLED"
ALLOWED_HOSTS_ENV = "JDE_DISCOVERY_ALLOWED_HOSTS"
# Optional server-level PEM bundle, used only when the connection settings
# carry no uploaded certificate. Verification itself is never switched off.
CA_BUNDLE_ENV = "JDE_DISCOVERY_CA_BUNDLE"
SIMULATION_LABEL = "SIMULATION -- simulated AIS endpoint, not the customer's JDE"

BREAKER_THRESHOLD = 3
BREAKER_OPEN_SECONDS = 300


class TransportError(RuntimeError):
    """The endpoint could not be reached, refused us, or answered badly."""


class AuthenticationFailed(TransportError):
    pass


class DestinationNotAllowed(TransportError):
    pass


@dataclass
class Session:
    """An authenticated AIS session. `context` is what the token-request
    response itself states about THIS session (documented v2 response:
    top-level environment, role, jasserver, username; userInfo.appsRelease).
    A key the response did not contain is absent -- never filled in from
    the profile or from server defaults."""

    token: str
    context: dict[str, Any] = field(default_factory=dict)


def server_defaults_from(data: dict[str, Any]) -> dict[str, Any]:
    """The documented defaultconfig keys plus any short scalar key naming a
    version or release (the field names vary by Tools release). Server level:
    never evidence of what a session uses."""
    out = {k: data.get(k) for k in DEFAULTCONFIG_KEYS if k in data}
    for k, v in data.items():
        if k not in out and isinstance(v, (str, int, float)) and re.search("version|release", k, re.I) and len(str(v)) <= 40:
            out[k] = v
    return out


def session_context_from_token_response(data: dict[str, Any]) -> dict[str, Any]:
    user = data.get("userInfo") or {}
    out = {"environment": data.get("environment"), "role": data.get("role"), "jasserver": data.get("jasserver"),
           "username": data.get("username") or user.get("username"), "apps_release": user.get("appsRelease")}
    return {k: v for k, v in out.items() if v not in (None, "")}


@dataclass
class ReadResult:
    records: list[dict[str, Any]]
    more_records: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def payload_sha256(self) -> str:
        blob = json.dumps({"records": self.records, "meta": self.meta}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class Trust:
    """A company's connection trust, configured by its Admin in Jade: the
    host of the saved AIS address and, optionally, the AIS server's
    certificate (or its CA) uploaded in the settings. An uploaded
    certificate only lets Jade verify that server; certificate and
    host-name/IP verification are never switched off."""

    host: str = ""
    ca_pem: str = ""
    ca_sha256: str = ""
    ca_missing: bool = False  # the settings name a certificate that is not stored


def server_lock() -> bool:
    """The server operator can still lock live discovery off for the whole
    deployment (JDE_DISCOVERY_LIVE_ENABLED=false). Otherwise the company's
    own connection settings decide."""
    return os.environ.get(LIVE_ENABLED_ENV, "").strip().lower() == "false"


def live_allowed_by_deployment(trust: Optional[Trust] = None) -> bool:
    """Live discovery is available unless the server operator has locked it
    off, and only while its TLS trust is usable. A certificate that is
    missing or unusable keeps live discovery OFF -- there is never a
    fallback to unverified TLS."""
    return not server_lock() and tls_trust(trust)[0]


def live_status_detail(trust: Optional[Trust] = None) -> str:
    if server_lock():
        return f"live discovery is locked off on this server by its operator ({LIVE_ENABLED_ENV}=false)"
    ok, detail = tls_trust(trust)
    return "available" if ok else f"live discovery is held OFF because TLS trust is unusable: {detail}"


def tls_context(trust: Optional[Trust] = None) -> ssl.SSLContext:
    """The one TLS configuration every live AIS request uses (token request,
    server defaults, reads, logout). A certificate uploaded in the settings,
    else the server's JDE_DISCOVERY_CA_BUNDLE, is trusted ONLY; otherwise the
    public trust store. Certificate and hostname/IP checks stay on. Raises
    if the trust cannot be loaded."""
    if trust and trust.ca_missing:
        raise FileNotFoundError("the certificate named in the settings is not stored")
    path = os.environ.get(CA_BUNDLE_ENV, "").strip()
    if trust and trust.ca_pem:
        ctx = ssl.create_default_context(cadata=trust.ca_pem)
    elif path:
        ctx = ssl.create_default_context(cafile=path)
    else:
        ctx = ssl.create_default_context()
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:  # defensive: never weaker than the default
        raise ssl.SSLError("TLS context is not verifying certificates and host names")
    return ctx


def tls_trust(trust: Optional[Trust] = None) -> tuple[bool, str]:
    """(usable, description) of the certificate trust the live transport uses."""
    if trust and trust.ca_missing:
        return False, "the certificate selected in the settings is not stored on the server; upload it again"
    if trust and trust.ca_pem:
        try:
            tls_context(trust)
        except (ssl.SSLError, OSError, ValueError) as exc:
            return False, f"the uploaded certificate is not usable ({type(exc).__name__})"
        return True, (f"only the certificate uploaded in the settings (sha256 {trust.ca_sha256[:16]}...), with "
                      "certificate and host-name/IP checks")
    path = os.environ.get(CA_BUNDLE_ENV, "").strip()
    try:
        tls_context()
    except FileNotFoundError:
        return False, f"{CA_BUNDLE_ENV} points to {path}, which does not exist on the server"
    except PermissionError:
        return False, f"{CA_BUNDLE_ENV} points to {path}, which the backend cannot read"
    except (ssl.SSLError, OSError, ValueError) as exc:
        return False, f"{CA_BUNDLE_ENV} ({os.path.basename(path)}) is not a usable certificate bundle ({type(exc).__name__})"
    if not path:
        return True, ("the public CA trust store, with certificate and host-name checks (for a self-signed or "
                      "private-CA AIS certificate, upload it in the connection settings)")
    return True, (f"only the CA bundle configured on the server ({os.path.basename(path)}), with certificate and "
                  "host-name/IP checks")


def diagnose(exc: Exception, host: str) -> str:
    """A plain explanation of why the BACKEND could not reach the endpoint.
    Never includes credentials, tokens or response bodies."""
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower():
        return (f"TLS: the backend does not trust the certificate presented by {host}. If the customer uses a "
                "self-signed or private-CA certificate, upload the AIS certificate (or its CA) in the connection "
                "settings; verification is never switched off")
    if "hostname" in text.lower() and "match" in text.lower():
        return f"TLS: the certificate presented does not match the host name {host}"
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout):
        return (f"timed out reaching {host} from the backend machine: a VPN or network route from the machine "
                "running Jade's backend may be required (your browser reaching JDE does not prove the backend can)")
    if isinstance(exc, httpx.ConnectError):
        if "Name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
            return (f"the backend machine cannot resolve {host} (DNS). A VPN or internal DNS from the machine "
                    "running Jade's backend may be required")
        return (f"the backend machine could not open a connection to {host}: a VPN, firewall rule or network "
                "route from the machine running Jade's backend may be required")
    return f"{type(exc).__name__} contacting the AIS endpoint"


def server_allowed_hosts() -> set[str]:
    """An optional server-operator narrowing of destinations."""
    return {h.strip().lower() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()}


def allowed_hosts(trust: Optional[Trust] = None) -> set[str]:
    """Permitted destinations: the operator's list when one is set on the
    server, otherwise exactly the host of the company's saved AIS address."""
    server = server_allowed_hosts()
    if server:
        return server
    return {trust.host} if trust and trust.host else set()


# ---------------------------------------------------------------------
# Circuit breaker, per company
# ---------------------------------------------------------------------
_breakers: dict[str, dict[str, float]] = {}
_breaker_lock = threading.Lock()


def breaker_open(company_id: str) -> Optional[float]:
    with _breaker_lock:
        b = _breakers.get(company_id)
        if b and b.get("open_until", 0) > time.time():
            return b["open_until"]
        return None


def record_transport_outcome(company_id: str, ok: bool) -> None:
    with _breaker_lock:
        b = _breakers.setdefault(company_id, {"failures": 0, "open_until": 0})
        if ok:
            b["failures"], b["open_until"] = 0, 0
            return
        b["failures"] += 1
        if b["failures"] >= BREAKER_THRESHOLD:
            b["open_until"] = time.time() + BREAKER_OPEN_SECONDS


def reset_breaker(company_id: str) -> None:
    with _breaker_lock:
        _breakers.pop(company_id, None)


# ---------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------
def reset_simulations() -> None:
    """Tests only: forget every simulated estate in the current estate directory."""
    sim_estate.reset()


_OPS = {
    "EQUAL": lambda a, b: a == b, "NOT_EQUAL": lambda a, b: a != b, "LESS": lambda a, b: a < b,
    "GREATER": lambda a, b: a > b, "LESS_EQUAL": lambda a, b: a <= b, "GREATER_EQUAL": lambda a, b: a >= b,
    "STR_START_WITH": lambda a, b: str(a).startswith(str(b)),
}


class SimulatedAisEndpoint:
    mode = "simulation"
    label = SIMULATION_LABEL

    def __init__(self, company_id: str, environment: str, *, calls: Optional[list] = None) -> None:
        self.company_id = company_id
        self.environment = environment
        # Every (method, path) this endpoint received -- tests assert on it.
        self.calls = calls if calls is not None else []

    def _estate(self) -> dict[str, Any]:
        estate = sim_estate.load(self.company_id, self.environment)
        if not estate["reachable"]:
            raise TransportError("simulated endpoint unreachable")
        return estate

    def _fault(self, target: str) -> None:
        """An explicit test condition on this read (sim_estate.add_fault)."""
        if sim_estate.peek_fault(self.company_id, self.environment, "discovery_read", target) is None:
            return
        with sim_estate.edit(self.company_id, self.environment, actor="simulated AIS",
                             reason=f"test condition consumed: discovery_read {target}") as estate:
            fault = sim_estate.take_fault(estate, "discovery_read", target)
        if fault:
            raise TransportError(f"simulated {fault['mode']} (TEST CONDITION){': ' + fault['message'] if fault['message'] else ''}")

    def check_reachability(self) -> str:
        self._estate()
        return "simulated endpoint answered"

    def authenticate(self, username: str, password: str, environment: str, role: str) -> Session:
        self.calls.append(AUTH_ENDPOINTS["token_request"])
        estate = self._estate()
        if not (username and password) or not estate["accept_credentials"]:
            raise AuthenticationFailed("simulated AIS refused the credential")
        s = estate["session"]
        if (s["granted_environments"] is not None and environment not in s["granted_environments"]) or (
                s["granted_roles"] is not None and role not in s["granted_roles"]):
            raise AuthenticationFailed("simulated AIS refused the requested environment or role")
        if not s["report_context"]:
            return Session("simulated-token", {})
        return Session("simulated-token", session_context_from_token_response({
            "username": username, "environment": environment, "role": role, "jasserver": s["jasserver"],
            "userInfo": {"token": "simulated-token", "appsRelease": s["apps_release"]}}))

    def logout(self, token) -> None:
        self.calls.append(AUTH_ENDPOINTS["logout"])

    def read(self, plan: ReadPlan, token: str) -> ReadResult:
        capabilities.assert_read_semantics(plan)
        self.calls.append((plan.method, plan.path))
        estate = self._estate()
        if plan.endpoint == "defaultconfig":
            return ReadResult([{k: estate["defaultconfig"].get(k) for k in DEFAULTCONFIG_KEYS if k in estate["defaultconfig"]}],
                              meta={"endpoint": "defaultconfig"})
        if plan.endpoint == "poservice":
            key = f"{plan.body['applicationName']}|{plan.body['version']}"
            self._fault(key)
            values = estate["processing_options"].get(key)
            if values is None:
                return ReadResult([], meta={"endpoint": "poservice", "found": False})
            rows = [{"option": k, "value": v} for k, v in sorted(values.items())]
            if plan.fields:
                rows = [r for r in rows if r["option"] in plan.fields]
            return ReadResult(rows[: plan.max_records], more_records=len(rows) > plan.max_records,
                              meta={"endpoint": "poservice"})
        body = plan.body or {}
        table = body["targetName"]
        self._fault(table)
        rows = estate["tables"].get(table)
        if rows is None:
            return ReadResult([], meta={"endpoint": "dataservice", "table": table, "found": False})
        for cond in (body.get("query") or {}).get("condition", []):
            col = cond["controlId"].split(".", 1)[1]
            val = cond["value"][0]["content"]
            rows = [r for r in rows if _OPS[cond["operator"]](str(r.get(col, "")), val)]
        projected = [{f: r.get(f) for f in plan.fields} for r in rows]
        return ReadResult(projected[: plan.max_records], more_records=len(projected) > plan.max_records,
                          meta={"endpoint": "dataservice", "table": table})


# ---------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------
class LiveAisTransport:
    mode = "live"
    label = "LIVE -- customer AIS endpoint"

    def __init__(self, company_id: str, base_url: str, timeout_seconds: int,
                 *, transport: Optional[httpx.BaseTransport] = None, trust: Optional[Trust] = None) -> None:
        if not live_allowed_by_deployment(trust):
            raise DestinationNotAllowed(live_status_detail(trust))
        host = (urlparse(base_url).hostname or "").lower()
        if urlparse(base_url).scheme != "https":
            raise DestinationNotAllowed("live discovery only uses https")
        if host not in allowed_hosts(trust):
            raise DestinationNotAllowed(
                f"{host} is not a permitted destination (the server operator limits destinations with {ALLOWED_HOSTS_ENV})"
            )
        self.company_id = company_id
        self.base_url = base_url
        trust_ok, trust_detail = tls_trust(trust)
        if not trust_ok:
            raise DestinationNotAllowed(f"TLS trust is not usable: {trust_detail}")
        self.host = host
        self.tls_detail = trust_detail
        # ONE client, ONE verifying TLS context, for every request of this transport.
        self._client = httpx.Client(
            verify=tls_context(trust), follow_redirects=False, timeout=httpx.Timeout(timeout_seconds), transport=transport,
        )

    def _send(self, method: str, path: str, *, token: Optional[str] = None,
              body: Optional[dict] = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["jde-AIS-Auth"] = token
        try:
            resp = self._client.request(method, f"{self.base_url}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            record_transport_outcome(self.company_id, False)
            raise TransportError(diagnose(exc, self.host)) from None
        if 300 <= resp.status_code < 400:
            record_transport_outcome(self.company_id, False)
            raise TransportError(f"the endpoint answered with a redirect (HTTP {resp.status_code}); redirects are refused")
        if resp.status_code in (401, 403):
            record_transport_outcome(self.company_id, True)  # reachable; the credential or role was refused
            raise AuthenticationFailed(f"AIS refused the request (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            record_transport_outcome(self.company_id, False)
            raise TransportError(f"AIS answered HTTP {resp.status_code}")
        record_transport_outcome(self.company_id, True)
        try:
            return resp.json()
        except ValueError as exc:
            raise TransportError("AIS answered with something that is not JSON") from exc

    def check_reachability(self) -> str:
        method, path = capabilities.READ_ENDPOINTS["defaultconfig"]
        self._send(method, path)
        return f"endpoint answered over verified TLS ({self.tls_detail})"

    def authenticate(self, username: str, password: str, environment: str, role: str) -> Session:
        method, path = AUTH_ENDPOINTS["token_request"]
        data = self._send(method, path, body={"username": username, "password": password,
                                              "environment": environment, "role": role,
                                              "deviceName": "JadeDiscovery"})
        token = ((data.get("userInfo") or {}).get("token")) or data.get("token")
        if not token:
            raise AuthenticationFailed("AIS returned no session token")
        return Session(token, session_context_from_token_response(data))

    def logout(self, session) -> None:
        token = session.token if isinstance(session, Session) else session
        method, path = AUTH_ENDPOINTS["logout"]
        try:
            self._send(method, path, token=token, body={"token": token})
        except TransportError:
            pass  # the session expires on its own; never retried

    def read(self, plan: ReadPlan, session) -> ReadResult:
        capabilities.assert_read_semantics(plan)
        token = session.token if isinstance(session, Session) else session
        data = self._send(plan.method, plan.path, token=token, body=plan.body)
        if plan.endpoint == "defaultconfig":
            return ReadResult([server_defaults_from(data)], meta={"endpoint": "defaultconfig", "shape": "unverified"})
        if plan.endpoint == "poservice":
            options = data.get("processingOptions") or {}
            rows = [{"option": k, "value": (v or {}).get("value") if isinstance(v, dict) else v}
                    for k, v in sorted(options.items())]
            if plan.fields:
                rows = [r for r in rows if r["option"] in plan.fields]
            return ReadResult(rows[: plan.max_records], meta={"endpoint": "poservice", "shape": "unverified"})
        table = plan.body["targetName"]
        grid = {}
        for key, value in data.items():
            if key.startswith("fs_DATABROWSE_") and isinstance(value, dict):
                grid = ((value.get("data") or {}).get("gridData") or {})
        rowset = grid.get("rowset") or []
        records = [{f: row.get(f"{table}_{f}") for f in plan.fields} for row in rowset[: plan.max_records]]
        more = bool((grid.get("summary") or {}).get("moreRecords"))
        return ReadResult(records, more_records=more, meta={"endpoint": "dataservice", "table": table,
                                                            "shape": "unverified"})


def transport_for(company_id: str, config, *, live_transport: Optional[httpx.BaseTransport] = None,
                  trust: Optional[Trust] = None):
    """The transport the profile asks for -- never a substitute."""
    if config.connection_mode == "simulation":
        from ..services.customer_service import is_demo_company

        if not is_demo_company(company_id):
            raise DestinationNotAllowed(
                "this connection is set to Simulation, which only exists for demo customers; switch it to Live "
                "and enter the customer's real AIS address"
            )
        return SimulatedAisEndpoint(company_id, config.environment)
    return LiveAisTransport(company_id, config.ais_base_url, config.limits.timeout_seconds, transport=live_transport,
                            trust=trust)
