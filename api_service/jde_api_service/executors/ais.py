"""
The AIS executor: applies exactly one approved configuration item in the
customer's DEV system through AIS application-stack form requests, signed
in as the customer's dedicated DEV write user.

How a form is driven, and why it is safe to drive a form Jade has not seen
before:

  * The FORM MAP (FORM_MAPS below, version-controlled like the capability
    catalogue) names, per table, the configuration application, its
    Work With form and its revision form, and which item field is which
    form field -- by data dictionary alias, never by control number.
  * Control numbers are resolved LIVE from the form JD Edwards returns:
    a field is used only if exactly one control on the form carries its
    data dictionary alias; a button is pressed only if JD Edwards itself
    reports a control with that exact title. A button whose title is not
    on the short list of permitted titles (Find, Select, Add, OK) is never
    pressed -- so a Delete, Copy or row exit can never be triggered, even
    by a wrong map. Anything that cannot be resolved exactly stops the
    item before anything is saved, with the reason.
  * Every request is checked by assert_write_semantics before it leaves:
    only the application-stack endpoint, the form opened read-only, only
    the approved fields set, no grid row deletion, no business function,
    report or orchestration.
  * The attempt is recorded BEFORE the first request (execution.begin_item)
    and closed with what is known afterwards: applied only when the live
    read-back shows exactly the approved values; not_sent when nothing was
    saved; otherwise unknown -- the item stops, is flagged and reconciled,
    and is never retried blindly.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

APPSTACK = ("POST", "/jderest/v2/appstack")
PERMITTED_BUTTONS = {"find", "select", "add", "ok"}
# Buttons whose press saves the form (the commit boundary).
COMMIT_BUTTONS = {"ok"}
FORBIDDEN_KEYS = {"bsfnName", "orchestration", "batchJob", "submit", "reportName", "gridRowDeleteEvents",
                  "dataServiceType", "outputType_delete"}
_GRID = "1"

# ---------------------------------------------------------------------
# The form map: which application configures which table.
#
#   layout "grid":   the revision form edits the table's rows in its grid
#                    (User Defined Codes: one row per code).
#   layout "header": the revision form edits one row in its header fields.
#   key:     item key field -> form field alias (data dictionary alias)
#   header:  key fields set on the Work With form to find the row
#   row_key: key fields that identify the row in the grid
# Item value fields are matched to form fields by their data dictionary
# alias: the alias itself, or the column alias without its two-letter
# table prefix (DRDL01 -> DL01).
# ---------------------------------------------------------------------
FORM_MAPS: dict[str, dict[str, Any]] = {
    "F0005": {
        "application": "P0004A", "title": "User Defined Codes", "version": "ZJDE0001",
        "work_with": "W0004AA", "revision": "W0004AB", "layout": "grid",
        "key": {"DRSY": "SY", "DRRT": "RT", "DRKY": "KY"},
        "header": ["DRSY", "DRRT"], "row_key": ["DRKY"],
    },
    "F40039": {
        "application": "P40040", "title": "Document Type Maintenance", "version": "ZJDE0001",
        "work_with": "W40040B", "revision": "W40040A", "layout": "header",
        "key": {"DCTO": "DCTO"}, "header": ["DCTO"], "row_key": ["DCTO"],
    },
    "F40205": {
        "application": "P40205", "title": "Line Type Constants", "version": "ZJDE0001",
        "work_with": "W40205B", "revision": "W40205A", "layout": "header",
        "key": {"LNTY": "LNTY"}, "header": ["LNTY"], "row_key": ["LNTY"],
    },
    "F40203": {
        "application": "P40204", "title": "Order Activity Rules", "version": "ZJDE0001",
        "work_with": "W40204A", "revision": "W40204B", "layout": "grid",
        "key": {"DCTO": "DCTO", "LNTY": "LNTY", "LNST": "LNST"},
        "header": ["DCTO", "LNTY"], "row_key": ["LNST"],
    },
}


def form_map_for(item: dict) -> Optional[dict]:
    if item.get("kind") not in ("udc_value", "setup_row"):
        return None
    fm = FORM_MAPS.get(str(item.get("table") or "").upper())
    if fm is None:
        return None
    if set(item.get("key") or {}) != set(fm["key"]):
        return None  # the item names another key than the form works with
    return fm


class NotSent(RuntimeError):
    """Stopped before anything was saved in JD Edwards, with the reason."""


class NotAWriteOfThisItem(RuntimeError):
    """A request that is not exactly the approved item's form request --
    never sent."""


# ---------------------------------------------------------------------
# Reading the forms JD Edwards returns
# ---------------------------------------------------------------------
_FIELD = re.compile(r"^z_([A-Z0-9#$@]+)_(\d+)$")


def _norm_title(title: Any) -> str:
    return re.sub(r"[^a-z]", "", str(title or "").lower())


@dataclass
class Form:
    """One form as JD Edwards returned it."""

    name: str  # e.g. P0004A_W0004AA
    fields: dict[str, list[str]] = field(default_factory=dict)  # alias -> control ids (header)
    columns: dict[str, list[str]] = field(default_factory=dict)  # alias -> column ids (grid)
    buttons: dict[str, list[str]] = field(default_factory=dict)  # normalised title -> control ids
    buttons_reported: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)  # {"rowIndex": n, alias: value}
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)  # header alias -> value

    @property
    def oid(self) -> str:
        return self.name.split("_", 1)[1] if "_" in self.name else self.name


def _messages(raw: Any) -> list[str]:
    out = []
    for m in raw or []:
        if isinstance(m, dict):
            out.append(" ".join(str(m.get(k)) for k in ("CODE", "TITLE", "DESC", "ERRORCONTROL") if m.get(k)).strip()
                       or json.dumps(m)[:200])
        else:
            out.append(str(m)[:200])
    return out


def parse_form(response: dict) -> Form:
    """The one form in an application-stack answer. Raises NotSent when the
    answer holds no form."""
    forms = [(k, v) for k, v in response.items() if k.startswith("fs_") and isinstance(v, dict)]
    if len(forms) != 1:
        raise NotSent(f"AIS answered with {len(forms)} forms where Jade expected one")
    name, body = forms[0][0][3:], forms[0][1]
    form = Form(name=name, errors=_messages(body.get("errors")), warnings=_messages(body.get("warnings")))
    data = body.get("data") or {}
    for key, value in data.items():
        m = _FIELD.match(key)
        if m and isinstance(value, dict):
            form.fields.setdefault(m.group(1), []).append(m.group(2))
            form.values[m.group(1)] = value.get("value", value.get("internalValue"))
    grid = data.get("gridData") or {}
    for key in (grid.get("columns") or {}):
        m = _FIELD.match(key)
        if m:
            form.columns.setdefault(m.group(1), []).append(m.group(2))
    for row in grid.get("rowset") or []:
        parsed = {"rowIndex": row.get("rowIndex")}
        for key, value in row.items():
            m = _FIELD.match(key)
            if m:
                parsed[m.group(1)] = (value or {}).get("value") if isinstance(value, dict) else value
        form.rows.append(parsed)
    # Buttons and exits as JD Edwards reports them (showActionControls).
    controls = body.get("actionControls")
    if controls is None:
        controls = data.get("actionControls")
    if isinstance(controls, dict):
        controls = [{"id": k, **v} if isinstance(v, dict) else {"id": k, "title": v} for k, v in controls.items()]
    if isinstance(controls, list):
        form.buttons_reported = True
        for c in controls:
            if isinstance(c, dict) and c.get("id") is not None:
                form.buttons.setdefault(_norm_title(c.get("title")), []).append(str(c["id"]))
    return form


def _one(ids: list[str], what: str, form: Form) -> str:
    if len(ids) != 1:
        raise NotSent(f"{form.name}: {what} is {'not on the form' if not ids else 'on the form more than once'} -- "
                      "nothing was saved")
    return ids[0]


def field_id(form: Form, alias: str, *, grid: bool = False) -> str:
    source = form.columns if grid else form.fields
    candidates = [alias] + ([alias[2:]] if len(alias) > 4 else [])
    found = [(c, source[c]) for c in candidates if c in source]
    if len(found) > 1:
        raise NotSent(f"{form.name}: field {alias} matches more than one form field ({', '.join(c for c, _ in found)})")
    if not found:
        raise NotSent(f"{form.name}: no {'grid column' if grid else 'field'} carries {alias} -- nothing was saved")
    return _one(found[0][1], f"field {alias}", form)


def button_id(form: Form, title: str) -> str:
    t = _norm_title(title)
    if t not in PERMITTED_BUTTONS:
        raise NotAWriteOfThisItem(f"the {title!r} button is never pressed by Jade")
    if not form.buttons_reported:
        raise NotSent(f"{form.name}: AIS did not report the form's buttons, so Jade cannot confirm which control is "
                      f"{title!r} -- nothing was saved")
    return _one(form.buttons.get(t, []), f"the {title!r} button", form)


# ---------------------------------------------------------------------
# The request guard
# ---------------------------------------------------------------------
def _walk(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def assert_write_semantics(body: dict, *, allowed_fields: set[str], allowed_buttons: set[str],
                           allowed_forms: set[str]) -> None:
    """The last check before an application-stack request leaves Jade: the
    form is one of the item's own forms, it is opened read-only, only the
    item's resolved fields are set and only confirmed permitted buttons are
    pressed. Raises NotAWriteOfThisItem."""
    action = body.get("action")
    if action not in ("open", "execute", "close"):
        raise NotAWriteOfThisItem(f"application-stack action {action!r} is not allowed")
    present = {k for k, _ in _walk(body)} & FORBIDDEN_KEYS
    if present:
        raise NotAWriteOfThisItem(f"the request carries {', '.join(sorted(present))}")
    if action == "open":
        req = body.get("formRequest") or {}
        if req.get("formName") not in allowed_forms:
            raise NotAWriteOfThisItem(f"form {req.get('formName')!r} is not this item's form")
        if req.get("formServiceAction") != "R":
            raise NotAWriteOfThisItem("a form is only ever opened read-only")
        actions = req.get("formActions") or []
    elif action == "execute":
        req = body.get("actionRequest") or {}
        if not any(f.endswith("_" + str(req.get("formOID"))) for f in allowed_forms):
            raise NotAWriteOfThisItem(f"form {req.get('formOID')!r} is not this item's form")
        actions = req.get("formActions") or []
    else:
        return
    for a in actions:
        if "gridAction" in a:
            ga = a["gridAction"]
            if set(ga) - {"gridID", "gridRowInsertEvents", "gridRowUpdateEvents"}:
                raise NotAWriteOfThisItem("only grid row inserts and updates are allowed")
            for ev in [*ga.get("gridRowInsertEvents", []), *ga.get("gridRowUpdateEvents", [])]:
                for col in ev.get("gridColumnEvents") or []:
                    if col.get("command") != "SetGridCellValue" or f"g{col.get('columnID')}" not in allowed_fields:
                        raise NotAWriteOfThisItem(f"grid column {col.get('columnID')} is not one of the item's fields")
            continue
        command = a.get("command")
        cid = str(a.get("controlID"))
        if command in ("SetControlValue", "SetQBEValue"):
            if cid not in allowed_fields:
                raise NotAWriteOfThisItem(f"control {cid} is not one of the item's fields")
        elif command == "DoAction":
            if cid not in allowed_buttons:
                raise NotAWriteOfThisItem(f"control {cid} is not a confirmed permitted button")
        elif command == "SelectRow":
            if not re.match(r"^1\.\d+$", cid):
                raise NotAWriteOfThisItem(f"row selection {cid!r} is not a grid row")
        else:
            raise NotAWriteOfThisItem(f"form action {command!r} is not allowed")


# ---------------------------------------------------------------------
# The session: one application stack, signed in as the write user
# ---------------------------------------------------------------------
class Stack:
    def __init__(self, company_id: str, fm: dict, log: list[dict]) -> None:
        from ..discovery import profile_service, service as discovery_service, transport
        from . import settings

        profile = profile_service.load(company_id)
        if profile is None or not profile_service.is_active(profile):
            raise NotSent("the customer's JD Edwards connection is not enabled")
        config = profile["config"]
        if transport.breaker_open(company_id):
            raise NotSent("the connection's circuit breaker is open after repeated failures; try again later")
        s = settings.load(company_id)
        try:
            self.client = transport.transport_for(company_id, config,
                                                  live_transport=discovery_service.LIVE_HTTP_TRANSPORT,
                                                  trust=profile_service.trust_for(company_id, config))
            username, password = settings.write_credential(company_id)
        except Exception as exc:  # noqa: BLE001 -- nothing was sent
            raise NotSent(str(exc)) from None
        role = (s["config"].write_role if s else "") or config.role
        try:
            self.session = self.client.authenticate(username, password, config.environment, role)
        except transport.TransportError as exc:
            raise NotSent(f"the DEV write user could not sign in to AIS: {exc}") from None
        env = self.session.context.get("environment")
        if env and env != config.environment:
            self.client.logout(self.session)
            raise NotSent(f"the write user's session runs in {env}, not the configured DEV environment "
                          f"{config.environment}")
        self.environment = config.environment
        self.fm, self.log = fm, log
        self.forms = {f"{fm['application']}_{fm['work_with']}", f"{fm['application']}_{fm['revision']}"}
        self.stack: dict[str, Any] = {}
        self.allowed_fields: set[str] = set()
        self.allowed_buttons: set[str] = set()
        self.committing = False  # set right before the request that presses OK

    def _send(self, body: dict) -> Form:
        assert_write_semantics(body, allowed_fields=self.allowed_fields, allowed_buttons=self.allowed_buttons,
                               allowed_forms=self.forms)
        method, path = APPSTACK
        entry = {"action": body["action"], "at": time.time(),
                 "request": {k: v for k, v in body.items() if k != "token"}}
        self.log.append(entry)
        answer = self.client._send(method, path, token=self.session.token, body={**body, **self.stack})
        entry["answer_sha256"] = hashlib.sha256(json.dumps(answer, sort_keys=True, default=str).encode()).hexdigest()
        for k in ("stackId", "stateId", "rid"):
            if k in answer:
                self.stack[k] = answer[k]
        form = parse_form(answer)
        entry["form"] = form.name
        entry["errors"], entry["warnings"] = form.errors, form.warnings
        return form

    def open(self, form_oid: str, actions: list[dict]) -> Form:
        return self._send({"action": "open", "formRequest": {
            "formName": f"{self.fm['application']}_{form_oid}", "version": self.fm["version"],
            "formServiceAction": "R", "showActionControls": True, "formActions": actions,
            "maxPageSize": "10"}})

    def execute(self, form: Form, actions: list[dict]) -> Form:
        return self._send({"action": "execute", "actionRequest": {
            "formOID": form.oid, "showActionControls": True, "formActions": actions, "maxPageSize": "10"}})

    def close(self) -> None:
        try:
            if self.stack:
                method, path = APPSTACK
                self.client._send(method, path, token=self.session.token, body={"action": "close", **self.stack})
        except Exception:  # noqa: BLE001 -- the stack expires on its own
            pass
        finally:
            self.client.logout(self.session)

    # -- allow-listing what this request may touch -------------------------------
    def field(self, form: Form, alias: str, *, grid: bool = False) -> str:
        cid = field_id(form, alias, grid=grid)
        self.allowed_fields.add(f"g{cid}" if grid else cid)
        return cid

    def qbe(self, form: Form, alias: str) -> str:
        cid = field_id(form, alias, grid=True)
        self.allowed_fields.add(f"1[{cid}]")
        return f"1[{cid}]"

    def button(self, form: Form, title: str) -> str:
        cid = button_id(form, title)
        self.allowed_buttons.add(cid)
        return cid


# ---------------------------------------------------------------------
# Applying one item
# ---------------------------------------------------------------------
@dataclass
class Outcome:
    sent: bool  # a request that may save (the commit) left Jade
    detail: str
    errors: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)


def _set(cid: str, value: Any) -> dict:
    return {"command": "SetControlValue", "controlID": cid, "value": "" if value is None else str(value)}


def _item_fields(item: dict, fm: dict) -> dict[str, str]:
    """Form alias for each key and value field of the item."""
    out = {k: fm["key"][k] for k in item["key"]}
    for alias in item["values"]:
        out[alias] = alias
    return out


def apply_item(company_id: str, item: dict, log: list[dict]) -> Outcome:
    """Drive the configuration application for exactly this item. Raises
    NotSent when it stopped before the commit; returns the Outcome when the
    commit was sent (whether JD Edwards accepted it is established by the
    read-back, not by this answer)."""
    fm = form_map_for(item)
    if fm is None:
        raise NotSent("no AIS form map covers this item")
    stack = Stack(company_id, fm, log)
    try:
        return _drive(stack, item, fm)
    except (NotSent, NotAWriteOfThisItem):
        raise
    except Exception as exc:  # noqa: BLE001
        if stack.committing:
            raise
        # Opening, searching or navigating the forms failed: nothing that saves was sent.
        raise NotSent(f"AIS could not open or navigate the form before anything was saved: {exc}") from None
    finally:
        stack.close()


def _row_matches(row: dict, item: dict, fm: dict) -> bool:
    return all(str(row.get(fm["key"][k], "")).strip() == str(item["key"][k]).strip() for k in fm["row_key"])


def _drive(stack: Stack, item: dict, fm: dict) -> Outcome:
    log = stack.log
    # 1. Work With form, opened read-only; the header keys find the rows.
    ww = stack.open(fm["work_with"], [])
    actions = [_set(stack.field(ww, fm["key"][k]), item["key"][k]) for k in fm["header"]]
    if fm["layout"] == "header":
        actions += [{"command": "SetQBEValue", "controlID": stack.qbe(ww, fm["key"][k]), "value": str(item["key"][k])}
                    for k in fm["row_key"]]
    actions.append({"command": "DoAction", "controlID": stack.button(ww, "Find")})
    ww = stack.execute(ww, actions)
    if ww.errors:
        raise NotSent(f"{ww.name} refused the search: {'; '.join(ww.errors)}")
    matching = [r for r in ww.rows if _row_matches(r, item, fm)]
    # 2. Into the revision form: Add for a new row, Select for an existing one.
    if item["action"] == "add":
        if matching:
            raise NotSent(f"{ww.name} already shows the row this item adds -- nothing was saved")
        rev = stack.execute(ww, [{"command": "DoAction", "controlID": stack.button(ww, "Add")}])
    else:
        if len(matching) != 1:
            raise NotSent(f"{ww.name} shows {len(matching)} rows for the item's key, not exactly one -- nothing was "
                          "saved")
        rev = stack.execute(ww, [{"command": "SelectRow", "controlID": f"1.{matching[0]['rowIndex']}"},
                                 {"command": "DoAction", "controlID": stack.button(ww, "Select")}])
    if rev.errors:
        raise NotSent(f"{rev.name} could not be opened for the item: {'; '.join(rev.errors)}")
    if not rev.name.endswith("_" + fm["revision"]):
        raise NotSent(f"JD Edwards opened {rev.name}, not the revision form {fm['revision']} -- nothing was saved")
    # 3. The approved values, on exactly the item's row.
    fields = _item_fields(item, fm)
    actions = []
    if fm["layout"] == "grid":
        for k in fm["header"]:
            actions.append(_set(stack.field(rev, fm["key"][k]), item["key"][k]))
        columns = [(stack.field(rev, fields[k], grid=True), item["key"][k]) for k in fm["row_key"]] if (
            item["action"] == "add") else []
        columns += [(stack.field(rev, a, grid=True), v) for a, v in item["values"].items()]
        events = [{"command": "SetGridCellValue", "columnID": cid, "value": "" if v is None else str(v)}
                  for cid, v in columns]
        if item["action"] == "add":
            grid = {"gridID": _GRID, "gridRowInsertEvents": [{"gridColumnEvents": events}]}
        else:
            rows = [r for r in rev.rows if _row_matches(r, item, fm)]
            if len(rows) != 1:
                raise NotSent(f"{rev.name} shows {len(rows)} grid rows for the item's key, not exactly one -- nothing "
                              "was saved")
            grid = {"gridID": _GRID, "gridRowUpdateEvents": [{"rowNumber": rows[0]["rowIndex"],
                                                              "gridColumnEvents": events}]}
        actions.append({"gridAction": grid})
    else:
        if item["action"] == "add":
            actions += [_set(stack.field(rev, fm["key"][k]), item["key"][k]) for k in fm["key"]]
        else:
            for k in fm["key"]:
                shown = rev.values.get(fm["key"][k])
                if shown is not None and str(shown).strip() != str(item["key"][k]).strip():
                    raise NotSent(f"{rev.name} shows {k} {shown!r}, not the item's {item['key'][k]!r} -- nothing was "
                                  "saved")
        actions += [_set(stack.field(rev, a), v) for a, v in item["values"].items()]
    ok = stack.button(rev, "OK")
    # 4. The commit. From here on, anything but a clean read-back is unknown.
    assert_write_semantics({"action": "execute", "actionRequest": {"formOID": rev.oid, "formActions": actions + [
        {"command": "DoAction", "controlID": ok}]}}, allowed_fields=stack.allowed_fields,
        allowed_buttons=stack.allowed_buttons, allowed_forms=stack.forms)
    stack.committing = True
    try:
        after = stack.execute(rev, actions + [{"command": "DoAction", "controlID": ok}])
    except NotAWriteOfThisItem:
        raise
    except Exception as exc:  # noqa: BLE001 -- the commit may have reached JD Edwards
        return Outcome(True, f"the commit request was sent and did not answer cleanly: {exc}", log=log)
    return Outcome(True, f"{after.name} answered the commit" + (f" with errors: {'; '.join(after.errors)}"
                                                                 if after.errors else ""),
                   errors=after.errors, log=log)
