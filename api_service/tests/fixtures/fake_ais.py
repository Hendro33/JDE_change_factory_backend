"""
A fake AIS server at the HTTP boundary -- TEST FIXTURE ONLY.

The product talks to each customer's real AIS server over verified TLS
(discovery/transport.py). The tests replace only the network underneath it
(httpx.MockTransport): every request Jade sends -- token request, server
defaults, data and processing-option reads, orchestrations, logout -- goes
through the real transport, validation, logging and evidence code and is
answered here from ais_estate (per company and environment).

The company comes from the AIS host the test profile saves:
https://ais-<company>.customer.example
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

from . import ais_estate

_HOST = re.compile(r"^ais-([a-z0-9_-]+)\.customer\.example$")
DEFAULT_ENVIRONMENTS = {"vdb": "JDV920", "bwm": "JDVBWM"}


class FakeAis:
    def __init__(self) -> None:
        # (method, path, company) for every request -- tests assert on it.
        self.calls: list[tuple[str, str, str]] = []
        # orchestration name -> handler(payload) -> (status, json); default: 200 {"result": "ok"}
        self.orchestrations: dict[str, Callable[[dict], tuple[int, Any]]] = {}
        self._tokens: dict[str, tuple[str, str]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- helpers ---------------------------------------------------------------
    def _company(self, request: httpx.Request) -> Optional[str]:
        m = _HOST.match(request.url.host or "")
        return m.group(1) if m else None

    def _environment(self, request: httpx.Request, company: str) -> str:
        token = request.headers.get("jde-AIS-Auth")
        if token and token in self._tokens:
            return self._tokens[token][1]
        return DEFAULT_ENVIRONMENTS.get(company, "JDV920")

    @staticmethod
    def _fault(company: str, environment: str, operation: str, target: str) -> None:
        if ais_estate.peek_fault(company, environment, operation, target) is None:
            return
        with ais_estate.edit(company, environment, actor="fake AIS", reason=f"test condition consumed: {operation} {target}") as e:
            fault = ais_estate.take_fault(e, operation, target)
        if fault is None:
            return
        mode, message = fault["mode"], fault.get("message") or ""
        if mode == "fail_before_send":
            raise httpx.ConnectError(f"test condition: connection refused {message}")
        if mode == "fail":
            raise _Answer(500, {"message": f"test condition: error {message}"})
        raise httpx.ReadTimeout(f"test condition: {mode} {message}")

    # -- the server ----------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        company = self._company(request)
        path = request.url.path
        self.calls.append((request.method, path, company or ""))
        if company is None:
            raise httpx.ConnectError(f"no AIS server at {request.url.host}")
        try:
            return self._route(request, company, path)
        except _Answer as a:
            return httpx.Response(a.status, json=a.body)

    def _route(self, request: httpx.Request, company: str, path: str) -> httpx.Response:
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        environment = self._environment(request, company)
        if path == "/jderest/defaultconfig":
            estate = self._estate(company, environment)
            return httpx.Response(200, json=estate["defaultconfig"])
        if path == "/jderest/v2/tokenrequest":
            env = body.get("environment") or environment
            estate = self._estate(company, env)
            s = estate["session"]
            if not (body.get("username") and body.get("password")) or not estate["accept_credentials"]:
                return httpx.Response(401, json={"message": "invalid credentials"})
            if (s["granted_environments"] is not None and env not in s["granted_environments"]) or (
                    s["granted_roles"] is not None and body.get("role") not in s["granted_roles"]):
                return httpx.Response(403, json={"message": "environment or role not granted"})
            token = f"tok-{company}-{env}-{len(self._tokens)}"
            self._tokens[token] = (company, env)
            if not s["report_context"]:
                return httpx.Response(200, json={"userInfo": {"token": token}})
            return httpx.Response(200, json={"username": body["username"], "environment": env, "role": body.get("role"),
                                             "jasserver": s["jasserver"],
                                             "userInfo": {"token": token, "appsRelease": s["apps_release"]}})
        if path == "/jderest/v2/tokenrequest/logout":
            return httpx.Response(200, json={})
        if not request.headers.get("jde-AIS-Auth") in self._tokens:
            return httpx.Response(401, json={"message": "no session"})
        estate = self._estate(company, environment)
        if path == "/jderest/v2/poservice":
            key = f"{body['applicationName']}|{body['version']}"
            self._fault(company, environment, "discovery_read", key)
            values = estate["processing_options"].get(key)
            if values is None:
                return httpx.Response(200, json={"processingOptions": {}})
            return httpx.Response(200, json={"processingOptions": {k: {"value": v} for k, v in values.items()}})
        if path == "/jderest/v2/dataservice":
            table = body["targetName"]
            self._fault(company, environment, "discovery_read", table)
            rows = estate["tables"].get(table) or []
            for cond in (body.get("query") or {}).get("condition", []):
                col = cond["controlId"].split(".", 1)[1]
                val = cond["value"][0]["content"]
                rows = [r for r in rows if _OPS[cond["operator"]](str(r.get(col, "")), val)]
            limit = int(body.get("maxPageSize") or 10)
            return httpx.Response(200, json={f"fs_DATABROWSE_{table}": {"data": {"gridData": {
                "rowset": [{f"{table}_{k}": v for k, v in r.items()} for r in rows[:limit]],
                "summary": {"moreRecords": len(rows) > limit}}}}})
        m = re.match(r"^/jderest/v3/orchestrator/([A-Za-z0-9_\-]+)$", path)
        if m:
            name = m.group(1)
            self._fault(company, environment, "orchestration", name)
            status, answer = self.orchestrations.get(name, lambda p: (200, {"result": "ok"}))(body)
            return httpx.Response(status, json=answer)
        return httpx.Response(404, json={"message": f"no such AIS endpoint {path}"})

    @staticmethod
    def _estate(company: str, environment: str) -> dict:
        estate = ais_estate.load(company, environment)
        if not estate["reachable"]:
            raise httpx.ConnectError("test condition: AIS unreachable")
        return estate


class _Answer(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body


_OPS = {
    "EQUAL": lambda a, b: a == b, "NOT_EQUAL": lambda a, b: a != b, "LESS": lambda a, b: a < b,
    "GREATER": lambda a, b: a > b, "LESS_EQUAL": lambda a, b: a <= b, "GREATER_EQUAL": lambda a, b: a >= b,
    "STR_START_WITH": lambda a, b: str(a).startswith(str(b)),
}


def apply_in_dev(company: str, application: str, version: str, option: str, value: str,
                 environment: Optional[str] = None) -> None:
    """A person applying a processing-option value in DEV (the recorded route's
    real-world step), as a test action."""
    env = environment or DEFAULT_ENVIRONMENTS.get(company, "JDV920")
    with ais_estate.edit(company, env, actor="a person in JDE", reason=f"set {application}|{version} {option}") as e:
        ais_estate.set_processing_option(e, application, version, option, value)


def apply_row_in_dev(company: str, table: str, key: dict, values: dict, environment: Optional[str] = None) -> None:
    """A person adding or updating a configuration row in DEV (a UDC value in
    F0005, a document type in F40039, ...), as a test action."""
    env = environment or DEFAULT_ENVIRONMENTS.get(company, "JDV920")
    with ais_estate.edit(company, env, actor="a person in JDE", reason=f"set {table} {key}") as e:
        rows = e["tables"].setdefault(table, [])
        row = next((r for r in rows if all(str(r.get(k)) == str(v) for k, v in key.items())), None)
        if row is None:
            rows.append({**key, **values})
        else:
            row.update(values)
