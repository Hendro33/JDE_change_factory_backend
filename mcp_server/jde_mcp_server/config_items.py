"""
Configuration change sets: what the Functional Agent proposes and people
approve, apply in DEV and record, item by item.

A real configuration change is rarely one value. A new order type, for
example, is a UDC value (00/DT), a document type (F40039), order activity
rules (F40203) and a processing option on the entry version. So an approved
change is an ORDERED SET of configuration items, each under one capability
of the catalogue, and each checked on its own against the universal rules
below and the customer's engagement scope -- when it is proposed, when it is
approved and before every recorded delivery step.

Item kinds (the catalogue's enforcement contract names the kind per
capability, and for set-up tables the table(s) it covers):

  processing_option        application, version, option, value
  udc_value                product_code, udc_type, code, action, values {DRDL01, DRDL02, DRSPHD}
  setup_row                table, key {alias: value}, action, values {alias: value}
  version_data_selection   application (batch), version, specification (as it will be entered)
  version_data_sequencing  application (batch), version, specification

Fields are JD Edwards data dictionary aliases (e.g. DRDL01, DCT4), exactly as
the customer's configuration manuals and standards name them. Actions are
"add" (the row must not exist yet) or "update" (it must exist); Jade never
proposes a delete.

A legacy single processing-option operation ({"tool": "set_processing_option",
...}) is treated as a set with one item, so every earlier change keeps working.
"""

from __future__ import annotations

import re
from typing import Any, Optional

CHANGE_SET_TOOL = "configuration_change_set"
LEGACY_TOOL = "set_processing_option"

KINDS = ("processing_option", "udc_value", "setup_row", "version_data_selection", "version_data_sequencing")
ROW_KINDS = ("udc_value", "setup_row")
VERSION_KINDS = ("version_data_selection", "version_data_sequencing")
ACTIONS = ("add", "update")
MAX_ITEMS = 30
UDC_FIELDS = ("DRDL01", "DRDL02", "DRSPHD")  # DRHRDC (hard-coded) is never changed by Jade
UDC_TABLE = "F0005"

_ALIAS = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
_TABLE = re.compile(r"^F[0-9A-Z]{3,9}$")
_ORACLE_OWNED_VERSION = re.compile(r"^(XJDE|ZJDE)", re.IGNORECASE)
_OBJECT = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


class ItemInvalid(ValueError):
    pass


def _text(raw: dict, key: str, *, required: bool = True, limit: int = 200, upper: bool = False) -> str:
    value = str(raw.get(key) if raw.get(key) is not None else "").strip()
    if required and not value:
        raise ItemInvalid(f"{key} is required")
    if len(value) > limit:
        raise ItemInvalid(f"{key} is longer than {limit} characters")
    return value.upper() if upper else value


def _aliases(raw: Any, what: str, *, allowed: Optional[tuple] = None, limit: int = 30) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ItemInvalid(f"{what} must name at least one field (data dictionary alias) and its value")
    if len(raw) > limit:
        raise ItemInvalid(f"{what}: at most {limit} fields")
    out = {}
    for k, v in raw.items():
        alias = str(k).strip().upper()
        if not _ALIAS.match(alias):
            raise ItemInvalid(f"{what}: {k!r} is not a data dictionary alias")
        if allowed is not None and alias not in allowed:
            raise ItemInvalid(f"{what}: {alias} cannot be changed here (allowed: {', '.join(allowed)})")
        value = "" if v is None else str(v)
        if len(value) > 200:
            raise ItemInvalid(f"{what}: the value of {alias} is longer than 200 characters")
        out[alias] = value
    return out


def _version(raw: dict) -> tuple[str, str]:
    application = _text(raw, "application", upper=True, limit=10)
    version = _text(raw, "version", upper=True, limit=10)
    if not _OBJECT.match(application):
        raise ItemInvalid(f"{application!r} is not a JD Edwards object name")
    if _ORACLE_OWNED_VERSION.match(version):
        raise ItemInvalid(f"{version} is an Oracle-owned template version (XJDE/ZJDE): it is never changed -- copy it "
                          "to a customer version first (a manual OMW step)")
    return application, version


def normalise_item(raw: Any, index: int, enforcement_for) -> dict:
    """One item, validated against the universal rules. `enforcement_for`
    returns the capability's enforcement contract (or raises)."""
    if not isinstance(raw, dict):
        raise ItemInvalid(f"item {index + 1} must be an object")
    capability_id = _text(raw, "capability_id", limit=80)
    enforcement = enforcement_for(capability_id)
    kind = enforcement.get("item_kind")
    if kind not in KINDS:
        raise ItemInvalid(f"capability {capability_id} has no configuration item kind")
    if raw.get("kind") and raw.get("kind") != kind:
        raise ItemInvalid(f"capability {capability_id} changes {kind} items, not {raw.get('kind')}")
    item: dict[str, Any] = {"id": f"I{index + 1}", "capability_id": capability_id, "kind": kind,
                            "purpose": _text(raw, "purpose", required=False, limit=500)}
    if kind == "processing_option":
        item["application"], item["version"] = _version(raw)
        item["option"] = _text(raw, "option", limit=40)
        item["value"] = _text(raw, "value", required=False, limit=200)
        return item
    if kind in VERSION_KINDS:
        item["application"], item["version"] = _version(raw)
        if not item["application"].startswith("R"):
            raise ItemInvalid(f"{item['application']} is not a batch application (UBE): data selection and sequencing "
                              "belong to batch versions")
        item["specification"] = _text(raw, "specification", limit=2000)
        return item
    action = _text(raw, "action", limit=10).lower()
    if action not in ACTIONS:
        raise ItemInvalid(f"action must be add or update, not {action!r} (Jade never deletes configuration)")
    item["action"] = action
    if kind == "udc_value":
        item["table"] = UDC_TABLE
        item["key"] = {"DRSY": _text(raw, "product_code", upper=True, limit=4),
                       "DRRT": _text(raw, "udc_type", upper=True, limit=2),
                       "DRKY": _text(raw, "code", limit=10)}
        item["values"] = _aliases(raw.get("values"), "values", allowed=UDC_FIELDS)
        return item
    # setup_row
    table = _text(raw, "table", upper=True, limit=10)
    if not _TABLE.match(table):
        raise ItemInvalid(f"{table!r} is not a JD Edwards table name")
    tables = enforcement.get("tables")
    if tables and table not in tables:
        raise ItemInvalid(f"capability {capability_id} changes {', '.join(tables)} only, not {table}")
    item["table"] = table
    item["key"] = _aliases(raw.get("key"), "key", limit=6)
    item["values"] = _aliases(raw.get("values"), "values")
    overlap = set(item["key"]) & set(item["values"])
    if overlap:
        raise ItemInvalid(f"key fields cannot also be changed ({', '.join(sorted(overlap))})")
    return item


def normalise_change_set(operation: Any, enforcement_for) -> dict:
    """The operation Jade stores, hashes and people approve."""
    if not isinstance(operation, dict):
        raise ItemInvalid("the change set must be an object")
    raw_items = operation.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ItemInvalid("a configuration change set needs at least one item")
    if len(raw_items) > MAX_ITEMS:
        raise ItemInvalid(f"at most {MAX_ITEMS} items in one change set")
    items = [normalise_item(r, i, enforcement_for) for i, r in enumerate(raw_items)]
    targets = [target(i) for i in items]
    dupes = sorted({t for t in targets if targets.count(t) > 1})
    if dupes:
        raise ItemInvalid(f"the same target appears twice: {', '.join(dupes)}")
    out = {"tool": CHANGE_SET_TOOL, "summary": _text(operation, "summary", required=False, limit=1000), "items": items}
    if operation.get("test_orchestration"):
        out["test_orchestration"] = _text(operation, "test_orchestration", limit=60)
    return out


# ---------------------------------------------------------------------
# Reading a stored change
# ---------------------------------------------------------------------
def is_change_set(record_or_operation: dict) -> bool:
    op = record_or_operation.get("operation", record_or_operation) if "operation" in record_or_operation else record_or_operation
    return (op or {}).get("tool") == CHANGE_SET_TOOL


def items_of(record: dict) -> list[dict]:
    """The items of a stored change: a change set's own, or the one item a
    legacy processing-option operation is."""
    op = record.get("operation") or {}
    if op.get("tool") == CHANGE_SET_TOOL:
        return list(op.get("items") or [])
    if op.get("tool") == LEGACY_TOOL:
        return [{"id": "I1", "capability_id": record.get("capability_id"), "kind": "processing_option",
                 "application": str(op.get("application", "")).upper(), "version": str(op.get("version", "")).upper(),
                 "option": str(op.get("option", "")), "value": str(op.get("value", "")), "purpose": ""}]
    return []


def item(record: dict, item_id: str) -> dict:
    for it in items_of(record):
        if it["id"] == item_id:
            return it
    raise ItemInvalid(f"change {record.get('change_id')} has no item {item_id}")


def target(it: dict) -> str:
    """A stable identity for what the item changes."""
    kind = it["kind"]
    if kind == "processing_option":
        return f"po:{it['application']}|{it['version']}|{it['option'].upper()}"
    if kind in VERSION_KINDS:
        return f"{'vds' if kind == 'version_data_selection' else 'vsq'}:{it['application']}|{it['version']}"
    key = ";".join(f"{k}={v}" for k, v in sorted(it["key"].items()))
    return f"row:{it['table']}:{key}"


def label(it: dict) -> str:
    """How people read the item."""
    kind = it["kind"]
    if kind == "processing_option":
        return f"{it['application']} version {it['version']}: processing option {it['option']} = {it['value']!r}"
    if kind in VERSION_KINDS:
        what = "data selection" if kind == "version_data_selection" else "data sequencing"
        return f"{it['application']} version {it['version']}: {what}"
    values = ", ".join(f"{k} = {v!r}" for k, v in it["values"].items())
    if kind == "udc_value":
        k = it["key"]
        return f"{'Add' if it['action'] == 'add' else 'Update'} UDC {k['DRSY']}/{k['DRRT']} code {k['DRKY']!r}: {values}"
    key = ", ".join(f"{k} {v!r}" for k, v in it["key"].items())
    return f"{'Add' if it['action'] == 'add' else 'Update'} {it['table']} row ({key}): {values}"


def approved_values(it: dict) -> Any:
    """What must be in JD Edwards after the person applied the item."""
    if it["kind"] == "processing_option":
        return it["value"]
    if it["kind"] in ROW_KINDS:
        return {"exists": True, "values": dict(it["values"])}
    return {"specification": it["specification"]}


def read_fields(it: dict) -> list[str]:
    return sorted(it.get("values") or {})


# ---------------------------------------------------------------------
# Engagement scope (the customer's approved configuration)
# ---------------------------------------------------------------------
def parse_target(kind: str, text: str) -> dict:
    """Scope entry targets: udc "SY/RT"; setup row "TABLE" or
    "TABLE:FIELD=V1|V2;FIELD2=V3"; batch version "APP|VERSION"."""
    text = (text or "").strip()
    if kind == "udc_value":
        sy, _, rt = text.partition("/")
        if not sy or not rt:
            raise ItemInvalid(f"UDC target {text!r} must be product code/type, e.g. 00/DT")
        return {"DRSY": sy.strip().upper(), "DRRT": rt.strip().upper()}
    if kind in VERSION_KINDS:
        app, _, ver = text.partition("|")
        if not app or not ver:
            raise ItemInvalid(f"version target {text!r} must be application|version, e.g. R42565|CIQ0001")
        return {"application": app.strip().upper(), "version": ver.strip().upper()}
    table, _, keys = text.partition(":")
    out: dict[str, Any] = {"table": table.strip().upper(), "key_values": {}}
    if not _TABLE.match(out["table"]):
        raise ItemInvalid(f"{text!r} does not start with a JD Edwards table name")
    for part in [p for p in keys.split(";") if p.strip()]:
        field, _, values = part.partition("=")
        out["key_values"][field.strip().upper()] = [v.strip() for v in values.split("|") if v.strip()]
    return out


def matches(entry: dict, it: dict) -> bool:
    """Does this scope entry cover the item's target?"""
    if entry.get("capability_id") != it["capability_id"]:
        return False
    try:
        t = parse_target(it["kind"], entry.get("target", ""))
    except ItemInvalid:
        return False
    kind = it["kind"]
    if kind == "udc_value":
        return t["DRSY"] == it["key"]["DRSY"] and t["DRRT"] == it["key"]["DRRT"]
    if kind in VERSION_KINDS:
        return t["application"] == it["application"] and t["version"] == it["version"]
    if t["table"] != it["table"]:
        return False
    for field, allowed in t["key_values"].items():
        if allowed and str(it["key"].get(field, "")).strip() not in allowed:
            return False
    return True


def check_entry(entry: dict, it: dict) -> None:
    """The entry's actions, fields and allowed values (ScopeViolation-style
    messages; the caller raises its own exception type)."""
    if it["kind"] in ROW_KINDS:
        actions = [a.lower() for a in entry.get("actions") or []]
        if it["action"] not in actions:
            raise ItemInvalid(f"{label(it)}: the customer's scope allows {', '.join(actions) or 'no'} action(s) here, "
                              f"not {it['action']}")
        fields = {f.upper() for f in entry.get("fields") or []}
        extra = sorted(set(it["values"]) - fields)
        if extra:
            raise ItemInvalid(f"{label(it)}: {', '.join(extra)} may not be changed under the customer's scope "
                              f"(approved fields: {', '.join(sorted(fields)) or 'none'})")
        allowed = {k.upper(): v for k, v in (entry.get("allowed_values") or {}).items()}
        for field, value in it["values"].items():
            if field in allowed and allowed[field] and value not in allowed[field]:
                raise ItemInvalid(f"{label(it)}: {value!r} is not an allowed value of {field} "
                                  f"({', '.join(allowed[field])})")


FORMAT_HELP = """A configuration change set, proposed with propose_change(story_id, operation, capability_id="configuration_change_set"):
operation = {"tool": "configuration_change_set", "summary": "<what the set achieves>",
             "test_orchestration": "<optional: one of the customer's approved tests>",
             "items": [ <ordered items, in the order a person applies them in DEV> ]}
Each item names its capability_id and a one-line "purpose", then by kind:
- processing_option_update: {"application", "version", "option", "value"}
- udc_value_maintenance: {"product_code", "udc_type", "code", "action": "add"|"update", "values": {"DRDL01", "DRDL02", "DRSPHD"}}
- document_type_definition (F40039), line_type_definition (F40205), order_activity_status_rules (F40203),
  constants_and_setup_master_data (tables the customer lists): {"table", "key": {alias: value}, "action": "add"|"update",
  "values": {alias: value}} -- fields as JD Edwards data dictionary aliases
- batch_version_data_selection / batch_version_data_sequencing: {"application" (R...), "version", "specification": "<the exact
  selection or sequence as it will be entered>"}
Universal rules: never XJDE/ZJDE versions, never a delete, never the UDC hard-coded flag (DRHRDC). Every item must be inside
the customer's engagement scope (get_engagement_scope): its target listed for its capability, its action and fields allowed,
its values allowed, its category not protected or never-touch -- otherwise the whole proposal is refused with the reason."""
