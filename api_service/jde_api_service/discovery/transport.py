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
    timeout and a deployment-controlled destination allowlist. Chosen only
    when the profile says "live" AND the deployment enables live discovery
    (JDE_DISCOVERY_LIVE_ENABLED=true) AND the host is in
    JDE_DISCOVERY_ALLOWED_HOSTS. There is no fallback in either direction:
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


def live_allowed_by_deployment() -> bool:
    return os.environ.get(LIVE_ENABLED_ENV, "").strip().lower() == "true"


def allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()}


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
                 *, transport: Optional[httpx.BaseTransport] = None) -> None:
        if not live_allowed_by_deployment():
            raise DestinationNotAllowed(
                f"live discovery is switched off for this deployment ({LIVE_ENABLED_ENV} is not true)"
            )
        host = (urlparse(base_url).hostname or "").lower()
        if urlparse(base_url).scheme != "https":
            raise DestinationNotAllowed("live discovery only uses https")
        if host not in allowed_hosts():
            raise DestinationNotAllowed(
                f"{host} is not in this deployment's discovery destination allowlist ({ALLOWED_HOSTS_ENV})"
            )
        self.company_id = company_id
        self.base_url = base_url
        self._client = httpx.Client(
            verify=True, follow_redirects=False, timeout=httpx.Timeout(timeout_seconds), transport=transport,
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
            raise TransportError(f"{type(exc).__name__} contacting the AIS endpoint") from exc
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
        return "endpoint answered over verified TLS"

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
            return ReadResult([{k: data.get(k) for k in DEFAULTCONFIG_KEYS if k in data}],
                              meta={"endpoint": "defaultconfig", "shape": "unverified"})
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


def transport_for(company_id: str, config, *, live_transport: Optional[httpx.BaseTransport] = None):
    """The transport the profile asks for -- never a substitute."""
    if config.connection_mode == "simulation":
        return SimulatedAisEndpoint(company_id, config.environment)
    return LiveAisTransport(company_id, config.ais_base_url, config.limits.timeout_seconds, transport=live_transport)
