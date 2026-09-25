"""
The ONE simulated DEV estate: what discovery reads and what simulated
execution changes, for one company and one JDE environment.

Before this module there were two simulations that did not share state:
discovery read an in-memory estate inside the API process, and the
execution gate's mock wrote a separate file (an unknown target read back as
"MOCK-INITIAL"). An approved simulated change was therefore never visible to
a later discovery read, and the two could contradict each other.

Now both read and write this estate:

  * scoped by company AND environment (one JSON file per pair under
    JDE_SIM_ESTATE_DIR), and within it by target (processing options by
    application|version, tables by name, technical objects by object id);
  * persisted with a file lock, so the API process and an MCP server process
    started for an agent run see the same state;
  * seeded from a fixed template on first use; every change is recorded in
    the estate's own history (who, why, what), including test conditions;
  * drift, failures and timeouts exist only as EXPLICIT test conditions
    (edit() with a reason, add_fault()) -- nothing random.

Everything here is labelled SIMULATION. Nothing in this module can reach a
customer system; it has no network code at all.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SIMULATION_LABEL = "SIMULATION -- simulated DEV estate, not the customer's JDE"
HISTORY_LIMIT = 300

# Fault modes a test condition may inject. Each is consumed once unless
# once=False. What each means for an attempt:
#   fail_before_send     -- the request never left: provably not applied
#   timeout_before_apply -- sent, then no answer; the target was NOT changed
#   timeout_after_apply  -- sent and applied, then no answer
#   fail                 -- an explicit error answer; nothing applied
FAULT_MODES = {"fail_before_send", "timeout_before_apply", "timeout_after_apply", "fail"}
FAULT_OPERATIONS = {"discovery_read", "po_write", "apply", "build", "verify"}

_TEMPLATE: dict[str, Any] = {
    "reachable": True,
    "accept_credentials": True,
    # Shaped like the documented defaultconfig response: SERVER defaults
    # only. It says nothing about which environment a session actually uses.
    "defaultconfig": {"aisVersion": "simulated-ais", "defaultEnvironment": "JDV920", "defaultRole": "*ALL",
                      "defaultJasServer": "http://sim-jas.invalid:8080", "capabilityList": ["dataservice", "poservice"]},
    # What the simulated token-request response reports about the session
    # (documented keys). report_context False simulates an AIS that omits
    # them; granted_* restricts what the simulated user may log in to.
    "session": {"report_context": True, "apps_release": "E920", "jasserver": "http://sim-jas.invalid:8080",
                "granted_environments": None, "granted_roles": None},
    "tables": {
        "F0005": [
            {"DRSY": "00", "DRRT": "DT", "DRKY": "SO", "DRDL01": "Sales Order", "DRSPHD": ""},
            {"DRSY": "00", "DRRT": "DT", "DRKY": "S3", "DRDL01": "Sales Order - Direct Ship", "DRSPHD": ""},
            {"DRSY": "00", "DRRT": "DT", "DRKY": "SQ", "DRDL01": "Sales Quote", "DRSPHD": ""},
        ],
        "F983051": [
            {"VRPID": "P4210", "VERS": "ZJDE0001", "JD": "Sales Order Entry (Oracle)", "VRCHKOUTSTS": "N"},
            {"VRPID": "P4210", "VERS": "CIQ0001", "JD": "Sales Order Entry - Webshop", "VRCHKOUTSTS": "N"},
        ],
        "F9860": [
            {"SIOBNM": "P4210", "SIFUNO": "APPL", "SISY": "42", "SIMD": "Sales Order Entry", "SIPKGNAME": ""},
            {"SIOBNM": "P554210", "SIFUNO": "APPL", "SISY": "55", "SIMD": "Custom Sales Order Review", "SIPKGNAME": ""},
            {"SIOBNM": "B5542001", "SIFUNO": "BSFN", "SISY": "55", "SIMD": "Custom Credit Check", "SIPKGNAME": ""},
        ],
        "F4211": [
            {"DOCO": "10001", "DCTO": "SO", "LNID": "1.000", "LITM": "BIKE-100", "UORG": "2", "LTTR": "540", "NXTR": "560"},
            {"DOCO": "10001", "DCTO": "SO", "LNID": "2.000", "LITM": "HELMET-7", "UORG": "1", "LTTR": "540", "NXTR": "560"},
            {"DOCO": "10002", "DCTO": "S3", "LNID": "1.000", "LITM": "BIKE-200", "UORG": "1", "LTTR": "520", "NXTR": "540"},
        ],
    },
    "processing_options": {
        "P4210|CIQ0001": {"PDOCTYPE": "S3", "PLNTY": "S", "PCREDCHK": "1"},
        "P4210|ZJDE0001": {"PDOCTYPE": "SO", "PLNTY": "S", "PCREDCHK": ""},
    },
    # Technical objects' ACTIVE DEV runtime state (technical_sim.py). Empty
    # until a test or demonstration seeds one.
    "objects": {},
    "faults": [],
    "history": [],
}


class SimEstateError(RuntimeError):
    pass


def estate_dir() -> str:
    return os.environ.get("JDE_SIM_ESTATE_DIR") or os.path.abspath("./sim_estate")


_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _norm_env(environment: str) -> str:
    env = (environment or "").strip().upper()
    if not _SAFE.match(env):
        raise SimEstateError(f"invalid environment id {environment!r}")
    return env


def _paths(company_id: str, environment: str) -> tuple[str, str]:
    if not _SAFE.match(company_id or ""):
        raise SimEstateError(f"invalid company id {company_id!r}")
    directory = os.path.join(estate_dir(), company_id)
    env = _norm_env(environment)
    return os.path.join(directory, f"{env}.json"), os.path.join(directory, f".{env}.lock")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(company_id: str, environment: str) -> dict:
    estate = copy.deepcopy(_TEMPLATE)
    estate.update({"company_id": company_id, "environment": _norm_env(environment), "label": SIMULATION_LABEL,
                   "revision": 1})
    estate["history"].append({"revision": 1, "at": _now_iso(), "actor": "simulation", "reason": "seeded from the template",
                              "change": "seed"})
    return estate


@contextmanager
def _lock(company_id: str, environment: str) -> Iterator[str]:
    path, lock = _paths(company_id, environment)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: str, company_id: str, environment: str) -> dict:
    if not os.path.exists(path):
        return _seed(company_id, environment)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path: str, estate: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estate, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load(company_id: str, environment: str) -> dict:
    """A snapshot of the estate (a copy: changing it changes nothing)."""
    with _lock(company_id, environment) as path:
        estate = _read(path, company_id, environment)
        if not os.path.exists(path):
            _write(path, estate)
        return estate


@contextmanager
def edit(company_id: str, environment: str, *, actor: str, reason: str) -> Iterator[dict]:
    """Change the estate atomically. Every edit is recorded in its history
    with who made it and why -- a simulated execution, a simulated human
    action, or an explicit test condition such as drift."""
    if not actor or not reason:
        raise SimEstateError("an estate change must say who made it and why")
    with _lock(company_id, environment) as path:
        estate = _read(path, company_id, environment)
        before = json.dumps({k: v for k, v in estate.items() if k not in ("history", "revision")}, sort_keys=True)
        yield estate
        after = json.dumps({k: v for k, v in estate.items() if k not in ("history", "revision")}, sort_keys=True)
        if before != after or not os.path.exists(path):
            estate["revision"] = int(estate.get("revision", 1)) + 1
            estate["history"].append({"revision": estate["revision"], "at": _now_iso(), "actor": actor,
                                      "reason": reason[:300]})
            estate["history"] = estate["history"][-HISTORY_LIMIT:]
            _write(path, estate)


def reset(company_id: Optional[str] = None) -> None:
    """Remove estates (tests and throwaway demonstrations only)."""
    root = estate_dir()
    if not os.path.isdir(root):
        return
    for company in os.listdir(root):
        if company_id and company != company_id:
            continue
        directory = os.path.join(root, company)
        for fn in os.listdir(directory):
            if fn.endswith(".json"):
                os.remove(os.path.join(directory, fn))


# ---------------------------------------------------------------------
# Processing options -- read by discovery (poservice) and by the
# Functional path's approved-target read; changed by simulated execution.
# ---------------------------------------------------------------------
def _po_key(application: str, version: str) -> str:
    return f"{application.strip().upper()}|{version.strip().upper()}"


def read_processing_option(company_id: str, environment: str, application: str, version: str,
                           option: str) -> Optional[str]:
    """The option's value; "" when the version exists without that option set;
    None when the simulated estate has no such application version."""
    values = load(company_id, environment)["processing_options"].get(_po_key(application, version))
    if values is None:
        return None
    return str(values.get(option.strip().upper(), ""))


def set_processing_option(estate: dict, application: str, version: str, option: str, value: str) -> None:
    """Inside edit(): set one option on an EXISTING version."""
    values = estate["processing_options"].get(_po_key(application, version))
    if values is None:
        raise SimEstateError(f"{application}|{version} does not exist in the simulated DEV estate")
    values[option.strip().upper()] = value


# ---------------------------------------------------------------------
# Test conditions
# ---------------------------------------------------------------------
def add_fault(company_id: str, environment: str, *, operation: str, target: str, mode: str, message: str = "",
              once: bool = True, actor: str = "test harness") -> None:
    if operation not in FAULT_OPERATIONS or mode not in FAULT_MODES:
        raise SimEstateError(f"unknown fault {operation}/{mode}")
    with edit(company_id, environment, actor=actor,
              reason=f"TEST CONDITION: {mode} on {operation} {target}") as estate:
        estate["faults"].append({"operation": operation, "target": target, "mode": mode, "message": message,
                                 "once": once, "added_at": time.time()})


def take_fault(estate: dict, operation: str, target: str) -> Optional[dict]:
    """Inside edit(): the fault that applies to this operation, consumed if once."""
    for i, fault in enumerate(estate.get("faults") or []):
        if fault["operation"] == operation and fault["target"] in (target, "*"):
            if fault.get("once", True):
                estate["faults"].pop(i)
            return fault
    return None


def peek_fault(company_id: str, environment: str, operation: str, target: str) -> Optional[dict]:
    for fault in load(company_id, environment).get("faults") or []:
        if fault["operation"] == operation and fault["target"] in (target, "*"):
            return fault
    return None
