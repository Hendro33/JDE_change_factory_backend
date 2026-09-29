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
        # Application stacks (the agents' form requests): every form action
        # received, the users who signed in, and whether forms report buttons.
        self.stacks: dict[int, dict] = {}
        self.form_actions: list[dict] = []
        self.signed_in: list[tuple[str, str, str]] = []  # (company, username, role)
        self.report_action_controls = True

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
            self.signed_in.append((company, body.get("username"), body.get("role")))
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
        if path == "/jderest/v2/appstack":
            return self._appstack(company, environment, body)
        m = re.match(r"^/jderest/v3/orchestrator/([A-Za-z0-9_\-]+)$", path)
        if m:
            name = m.group(1)
            self._fault(company, environment, "orchestration", name)
            status, answer = self.orchestrations.get(name, lambda p: (200, {"result": "ok"}))(body)
            return httpx.Response(status, json=answer)
        return httpx.Response(404, json={"message": f"no such AIS endpoint {path}"})

    # -- application stacks: the JD Edwards configuration applications ---------------
    def _appstack(self, company: str, environment: str, body: dict) -> httpx.Response:
        action = body.get("action")
        if action == "open":
            req = body.get("formRequest") or {}
            form = FORMS.get(req.get("formName"))
            if form is None:
                return httpx.Response(500, json={"message": f"form {req.get('formName')} not found"})
            stack_id = len(self.stacks) + 1
            self.stacks[stack_id] = {"company": company, "environment": environment, "form": req["formName"],
                                     "header": {}, "qbe": {}, "state": 1, "selected": None, "context": {}}
            return self._form_answer(stack_id, self._apply(stack_id, req.get("formActions") or []))
        stack = self.stacks.get(body.get("stackId"))
        if stack is None or stack["company"] != company:
            return httpx.Response(400, json={"message": "no such application stack"})
        if action == "close":
            self.stacks.pop(body.get("stackId"), None)
            return httpx.Response(200, json={})
        req = body.get("actionRequest") or {}
        if not stack["form"].endswith("_" + str(req.get("formOID"))):
            return httpx.Response(400, json={"message": "formOID is not the current form"})
        return self._form_answer(body["stackId"], self._apply(body["stackId"], req.get("formActions") or []))

    def _apply(self, stack_id: int, actions: list[dict]) -> list[str]:
        """Apply form actions to the stack's current form; returns errors."""
        stack = self.stacks[stack_id]
        form = FORMS[stack["form"]]
        errors: list[str] = []
        pending_rows: list[dict] = []
        for a in actions:
            self.form_actions.append({"form": stack["form"], **a})
            if "gridAction" in a:
                ga = a["gridAction"]
                ids = {v: k for k, v in form["columns"].items()}
                for ev in ga.get("gridRowInsertEvents") or []:
                    pending_rows.append({"insert": True, "values": {ids[str(c["columnID"])]: c["value"]
                                                                    for c in ev["gridColumnEvents"]}})
                for ev in ga.get("gridRowUpdateEvents") or []:
                    pending_rows.append({"insert": False, "row": ev["rowNumber"],
                                         "values": {ids[str(c["columnID"])]: c["value"] for c in ev["gridColumnEvents"]}})
                continue
            cmd, cid = a.get("command"), str(a.get("controlID"))
            if cmd == "SetControlValue":
                alias = {v: k for k, v in form["fields"].items()}.get(cid)
                if alias is None:
                    errors.append(f"no control {cid}")
                else:
                    stack["header"][alias] = a.get("value", "")
            elif cmd == "SetQBEValue":
                col = re.match(r"^1\[(\d+)\]$", cid)
                alias = {v: k for k, v in form["columns"].items()}.get(col.group(1)) if col else None
                stack["qbe"][alias] = a.get("value", "")
            elif cmd == "SelectRow":
                stack["selected"] = int(cid.split(".")[1])
            elif cmd == "DoAction":
                title = form["buttons"].get(cid)
                if title is None:
                    errors.append(f"no button {cid}")
                elif title == "Delete":
                    raise AssertionError("TEST FIXTURE: Jade pressed Delete")
                elif title in ("Find",):
                    pass
                elif title in ("Add", "Select"):
                    rows = self._rows(stack)
                    ctx = dict(stack["header"])
                    if title == "Select":
                        if stack["selected"] is None or stack["selected"] >= len(rows):
                            errors.append("no row selected")
                            continue
                        ctx.update(rows[stack["selected"]])
                    stack.update({"form": form["next"][title], "context": ctx, "selected": None,
                                  "header": ({k: ctx.get(k, "") for k in FORMS[form["next"][title]]["fields"]}
                                             if title == "Select" or form["layout"] == "grid" else
                                             {k: ctx.get(k, "") for k in FORMS[form["next"][title]]["fields"]
                                              if k in form["fields"]}),
                                  "mode": "add" if title == "Add" else "update"})
                    return errors
                elif title == "OK":
                    fault = ais_estate.peek_fault(stack["company"], stack["environment"], "form_commit", stack["form"])
                    if fault and fault["mode"] == "commit_then_timeout":
                        errors += self._commit(stack, pending_rows)
                        with ais_estate.edit(stack["company"], stack["environment"], actor="fake AIS",
                                             reason="test condition consumed") as e:
                            ais_estate.take_fault(e, "form_commit", stack["form"])
                        raise httpx.ReadTimeout("test condition: the commit was applied, the answer never came")
                    self._fault(stack["company"], stack["environment"], "form_commit", stack["form"])
                    errors += self._commit(stack, pending_rows)
        return errors

    def _table_rows(self, stack: dict) -> tuple[str, dict, list[dict]]:
        form = FORMS[stack["form"]]
        estate = ais_estate.load(stack["company"], stack["environment"])
        return form["table"], form["map"], estate["tables"].get(form["table"]) or []

    def _rows(self, stack: dict) -> list[dict]:
        """The grid rows of the current form, as form aliases."""
        form = FORMS[stack["form"]]
        table, amap, rows = self._table_rows(stack)
        out = []
        for r in rows:
            f = {alias: r.get(col, "") for alias, col in amap.items()}
            if all(stack["header"].get(h) in (None, "", f.get(h)) for h in form.get("filter", [])) and \
                    all(v in ("", None, f.get(k)) for k, v in stack["qbe"].items()):
                out.append(f)
        if form["layout"] == "grid" and form.get("revision") and stack.get("mode") == "update":
            out = [r for r in out if all(r.get(h) == stack["context"].get(h) for h in form.get("filter", []))]
        return out

    def _commit(self, stack: dict, pending_rows: list[dict]) -> list[str]:
        form = FORMS[stack["form"]]
        table, amap = form["table"], form["map"]
        shown = self._rows(stack) if form["layout"] == "grid" else []
        with ais_estate.edit(stack["company"], stack["environment"], actor="JD Edwards (form commit)",
                             reason=f"{stack['form']} OK") as e:
            rows = e["tables"].setdefault(table, [])
            if form["layout"] == "grid":
                for p in pending_rows:
                    values = {**{h: stack["header"].get(h, "") for h in form.get("filter", [])}, **p["values"]}
                    if p["insert"]:
                        key = {amap[k]: values.get(k, "") for k in form["key"]}
                        if any(all(str(r.get(c)) == str(v) for c, v in key.items()) for r in rows):
                            return ["record already exists"]
                        rows.append({amap[k]: v for k, v in values.items()})
                    else:
                        target = shown[p["row"]]
                        row = next(r for r in rows if all(str(r.get(amap[k])) == str(target.get(k)) for k in form["key"]))
                        row.update({amap[k]: v for k, v in p["values"].items()})
            else:
                values = dict(stack["header"])
                key = {amap[k]: values.get(k, "") for k in form["key"]}
                existing = next((r for r in rows if all(str(r.get(c)) == str(v) for c, v in key.items())), None)
                if stack.get("mode") == "add":
                    if existing is not None:
                        return ["record already exists"]
                    rows.append({amap[k]: v for k, v in values.items()})
                else:
                    existing.update({amap[k]: v for k, v in values.items() if k not in form["key"]})
        return []

    def _form_answer(self, stack_id: int, errors: list[str]) -> httpx.Response:
        stack = self.stacks[stack_id]
        stack["state"] += 1
        form = FORMS[stack["form"]]
        data: dict[str, Any] = {f"z_{alias}_{cid}": {"id": int(cid), "title": alias, "value": stack["header"].get(alias, "")}
                                for alias, cid in form["fields"].items()}
        rows = self._rows(stack) if form["columns"] else []
        data["gridData"] = {"id": 1, "columns": {f"z_{a}_{c}": a for a, c in form["columns"].items()},
                            "rowset": [{"rowIndex": i, **{f"z_{a}_{c}": {"value": r.get(a, "")}
                                                          for a, c in form["columns"].items()}}
                                       for i, r in enumerate(rows)]}
        body: dict[str, Any] = {"title": form["title"], "data": data,
                                "errors": [{"CODE": "0001", "TITLE": e} for e in errors], "warnings": []}
        if self.report_action_controls:
            body["actionControls"] = [{"id": cid, "title": t} for cid, t in form["buttons"].items()]
        return httpx.Response(200, json={"stackId": stack_id, "stateId": stack["state"], "rid": f"r{stack_id}",
                                         "currentApp": stack["form"], f"fs_{stack['form']}": body})

    @staticmethod
    def _estate(company: str, environment: str) -> dict:
        estate = ais_estate.load(company, environment)
        if not estate["reachable"]:
            raise httpx.ConnectError("test condition: AIS unreachable")
        return estate


# The JD Edwards configuration forms the fake serves: control ids, buttons
# (with a Delete button the executor must never press), and how each form
# alias maps to its table column.
FORMS: dict[str, dict] = {
    "P0004A_W0004AA": {"title": "Work With User Defined Codes", "layout": "grid", "table": "F0005",
                       "fields": {"SY": "7", "RT": "9"}, "filter": ["SY", "RT"],
                       "columns": {"KY": "40", "DL01": "41", "DL02": "42", "SPHD": "43"},
                       "buttons": {"15": "Find", "14": "Select", "16": "Add", "17": "Delete", "12": "Close"},
                       "next": {"Add": "P0004A_W0004AB", "Select": "P0004A_W0004AB"},
                       "key": ["SY", "RT", "KY"],
                       "map": {"SY": "DRSY", "RT": "DRRT", "KY": "DRKY", "DL01": "DRDL01", "DL02": "DRDL02",
                               "SPHD": "DRSPHD", "HRDC": "DRHRDC"}},
    "P0004A_W0004AB": {"title": "User Defined Codes", "layout": "grid", "table": "F0005", "revision": True,
                       "fields": {"SY": "7", "RT": "9"}, "filter": ["SY", "RT"],
                       "columns": {"KY": "1", "DL01": "2", "DL02": "3", "SPHD": "4", "HRDC": "5"},
                       "buttons": {"11": "OK", "12": "Cancel", "13": "Delete"}, "next": {},
                       "key": ["SY", "RT", "KY"],
                       "map": {"SY": "DRSY", "RT": "DRRT", "KY": "DRKY", "DL01": "DRDL01", "DL02": "DRDL02",
                               "SPHD": "DRSPHD", "HRDC": "DRHRDC"}},
    "P40040_W40040B": {"title": "Work With Document Types", "layout": "header", "table": "F40039",
                       "fields": {"DCTO": "30"}, "filter": [], "columns": {"DCTO": "20", "DCT4": "21", "DL01": "22"},
                       "buttons": {"15": "Find", "14": "Select", "16": "Add", "17": "Delete"},
                       "next": {"Add": "P40040_W40040A", "Select": "P40040_W40040A"}, "key": ["DCTO"],
                       "map": {"DCTO": "DCTO", "DCT4": "DCT4", "DL01": "DCDL01"}},
    "P40040_W40040A": {"title": "Document Type Revisions", "layout": "header", "table": "F40039",
                       "fields": {"DCTO": "30", "DCT4": "31", "DL01": "32"}, "filter": [], "columns": {},
                       "buttons": {"11": "OK", "12": "Cancel"}, "next": {}, "key": ["DCTO"],
                       "map": {"DCTO": "DCTO", "DCT4": "DCT4", "DL01": "DCDL01"}},
}


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
