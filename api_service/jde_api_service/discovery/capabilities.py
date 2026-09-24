"""
The discovery capabilities Jade can offer the Architect -- a closed,
code-defined list. Every one is a READ. Nothing here can write, submit a
batch, run a business function, change an object, build or deploy.

Each capability has a base status:

  * unavailable -- AIS does not provide it (source code, event rules,
    full object specifications). The Architect is told so and pointed at
    the technical baseline import instead.
  * unverified  -- Jade has an adapter, but its request/response shape
    has not been confirmed against THIS customer's AIS. It becomes
    "supported" for a profile revision only after an approved sample read
    succeeds for it (DiscoveryService.sample_read).

The request is built here from typed parameters. The model never supplies
a URL, a path, SQL, a form action or an orchestration name. Semantics are
validated, not just the HTTP method: a dataservice call is a POST, and it
is a read only because dataServiceType is BROWSE (assert_read_semantics).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

CapabilityStatus = Literal["supported", "unverified", "unavailable"]
DataClass = Literal["configuration", "business_data"]

# The only paths the discovery transport will ever call.
AUTH_ENDPOINTS = {
    "token_request": ("POST", "/jderest/v2/tokenrequest"),
    "logout": ("POST", "/jderest/v2/tokenrequest/logout"),
}
READ_ENDPOINTS = {
    "defaultconfig": ("GET", "/jderest/defaultconfig"),
    "dataservice": ("POST", "/jderest/v2/dataservice"),
    "poservice": ("POST", "/jderest/v2/poservice"),
}
FILTER_OPERATORS = {"=": "EQUAL", "<>": "NOT_EQUAL", "<": "LESS", ">": "GREATER",
                    "<=": "LESS_EQUAL", ">=": "GREATER_EQUAL", "begins_with": "STR_START_WITH"}
HARD_MAX_RECORDS = 10


class NotARead(RuntimeError):
    """A planned request is not a read by its semantics -- never sent."""


@dataclass(frozen=True)
class DiscoveryCapability:
    capability_id: str
    title: str
    description: str
    base_status: CapabilityStatus
    data_class: DataClass
    # What a target names, e.g. "table", "application|version", "product_code/type".
    target_kind: str = ""
    endpoint: Optional[str] = None
    # Fixed table for capabilities that always read the same one.
    fixed_table: Optional[str] = None
    fixed_fields: tuple[str, ...] = ()
    unavailable_reason: str = ""
    alternative: str = ""


CAPABILITIES: dict[str, DiscoveryCapability] = {c.capability_id: c for c in (
    DiscoveryCapability(
        "environment_info", "AIS server defaults",
        "The AIS server's documented defaultconfig: AIS version and the server's DEFAULT environment, role and "
        "HTML server. These are server defaults -- not proof of the environment, release or data routing a "
        "session actually uses.",
        "unverified", "configuration", target_kind="none", endpoint="defaultconfig",
    ),
    DiscoveryCapability(
        "table_browse", "Read rows of an approved table",
        "Up to the profile's record limit (never more than 10) from one approved table, approved columns only, "
        "optionally filtered on approved columns. No joins, no paging, no SQL.",
        "unverified", "business_data", target_kind="table", endpoint="dataservice",
    ),
    DiscoveryCapability(
        "udc_values", "User defined code values",
        "Values of one approved UDC (product code/type), e.g. 00/DT document types.",
        "unverified", "configuration", target_kind="product_code/type", endpoint="dataservice",
        fixed_table="F0005", fixed_fields=("DRSY", "DRRT", "DRKY", "DRDL01", "DRSPHD"),
    ),
    DiscoveryCapability(
        "version_list", "Versions of an application",
        "The versions defined for one approved interactive or batch application (F983051).",
        "unverified", "configuration", target_kind="application", endpoint="dataservice",
        fixed_table="F983051", fixed_fields=("VRPID", "VERS", "JD", "VRCHKOUTSTS"),
    ),
    DiscoveryCapability(
        "object_librarian", "Object librarian entry",
        "Name, type, system code and description of one approved object (F9860). Identifies customisations "
        "(55-59 system codes) but does not return the object's definition or source.",
        "unverified", "configuration", target_kind="object", endpoint="dataservice",
        fixed_table="F9860", fixed_fields=("SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"),
    ),
    DiscoveryCapability(
        "processing_option_values", "Processing option values of a version",
        "The saved processing option values of one approved application|version.",
        "unverified", "configuration", target_kind="application|version", endpoint="poservice",
    ),
    DiscoveryCapability(
        "source_code", "Business function source code",
        "C business function source.", "unavailable", "configuration",
        unavailable_reason="AIS does not expose business function source code.",
        alternative="Import the relevant source as a technical baseline artifact, with its repository/commit.",
    ),
    DiscoveryCapability(
        "event_rules", "Event rules (ER / NER)",
        "Event rules of an application, report or named event rule.", "unavailable", "configuration",
        unavailable_reason="AIS does not expose event rules.",
        alternative="Import an event rules print/export as a technical baseline artifact.",
    ),
    DiscoveryCapability(
        "object_specifications", "Full object specifications",
        "Table, business view, form and data structure definitions.", "unavailable", "configuration",
        unavailable_reason="AIS does not return full object specifications as structured data.",
        alternative="Import an OMW/object specification export as a technical baseline artifact.",
    ),
)}


def get(capability_id: str) -> Optional[DiscoveryCapability]:
    return CAPABILITIES.get(capability_id)


@dataclass
class ReadPlan:
    """A fully built, deterministic request. Nothing in it comes from the
    model except values that were validated against the approved scope."""

    capability_id: str
    endpoint: str
    method: str
    path: str
    body: Optional[dict[str, Any]] = None
    fields: list[str] = field(default_factory=list)
    max_records: int = HARD_MAX_RECORDS


def build_plan(cap: DiscoveryCapability, target: str, fields: list[str], filters: list[dict], max_records: int,
               *, environment: str) -> ReadPlan:
    method, path = READ_ENDPOINTS[cap.endpoint]  # type: ignore[index]
    if cap.endpoint == "defaultconfig":
        return ReadPlan(cap.capability_id, cap.endpoint, method, path, None, [], 1)
    if cap.endpoint == "poservice":
        application, _, version = target.partition("|")
        return ReadPlan(cap.capability_id, cap.endpoint, method, path,
                        {"applicationName": application, "version": version, "deviceName": "JadeDiscovery"},
                        fields, max_records)
    # dataservice BROWSE
    table = cap.fixed_table or target
    conditions = [
        {"controlId": f"{table}.{f['field']}", "operator": FILTER_OPERATORS[f["op"]],
         "value": [{"content": str(f["value"]), "specialValueId": "LITERAL"}]}
        for f in filters
    ]
    if cap.capability_id == "udc_values":
        sy, _, rt = target.partition("/")
        conditions = [
            {"controlId": "F0005.DRSY", "operator": "EQUAL", "value": [{"content": sy, "specialValueId": "LITERAL"}]},
            {"controlId": "F0005.DRRT", "operator": "EQUAL", "value": [{"content": rt, "specialValueId": "LITERAL"}]},
        ] + conditions
    elif cap.capability_id == "version_list":
        conditions = [{"controlId": "F983051.VRPID", "operator": "EQUAL",
                       "value": [{"content": target, "specialValueId": "LITERAL"}]}] + conditions
    elif cap.capability_id == "object_librarian":
        conditions = [{"controlId": "F9860.SIOBNM", "operator": "EQUAL",
                       "value": [{"content": target, "specialValueId": "LITERAL"}]}] + conditions
    body = {
        "targetName": table,
        "targetType": "table",
        "dataServiceType": "BROWSE",
        "returnControlIDs": "|".join(f"{table}.{f}" for f in fields),
        "maxPageSize": str(max_records),
        "enableNextPageProcessing": "false",
        "outputType": "GRID_DATA",
    }
    if conditions:
        body["query"] = {"autoFind": True, "matchType": "MATCH_ALL", "condition": conditions}
    return ReadPlan(cap.capability_id, cap.endpoint, method, path, body, fields, max_records)


def assert_read_semantics(plan: ReadPlan) -> None:
    """The last check before dispatch: the endpoint is a known read and the
    body can only read. HTTP method alone proves nothing."""
    if (plan.method, plan.path) not in READ_ENDPOINTS.values():
        raise NotARead(f"{plan.method} {plan.path} is not an allowed discovery read endpoint")
    if plan.max_records > HARD_MAX_RECORDS:
        raise NotARead(f"at most {HARD_MAX_RECORDS} records per query")
    body = plan.body or {}
    if plan.endpoint == "dataservice":
        if body.get("dataServiceType") != "BROWSE":
            raise NotARead(f"dataServiceType {body.get('dataServiceType')!r} is not a read")
        if str(body.get("enableNextPageProcessing", "false")).lower() != "false":
            raise NotARead("automatic pagination is not allowed")
    forbidden = {"actionRequest", "formActions", "formServiceAction", "orchestration", "batchJob", "submit",
                 "reportName", "bsfnName", "gridUpdates", "gridInserts"}
    present = forbidden & set(body)
    if present:
        raise NotARead(f"request carries write/interactive fields: {', '.join(sorted(present))}")
