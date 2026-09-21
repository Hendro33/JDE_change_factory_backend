#!/usr/bin/env python3
"""
Run this once, right after Step 7 of GETTING_STARTED.md, before doing
anything else with a real story.

    python3 prove_the_gate.py

It proves, in order, using its own throwaway story IDs and a temporary
test scope (your real scope.json is backed up and restored afterwards,
so this is safe to run before you've filled it in for real, and safe
to re-run any time):

  1. A story that hasn't been approved cannot be written to.
  2. An approved story with no approved change still cannot be written to.
  3. An approved change is refused if the operation is altered even
     slightly after approval (this is the single most important check).
  4. The exact approved operation succeeds.
  5. Evidence is captured and its tamper-evidence chain verifies.
  6. A story can be resolved with no JDE change at all.
  7. A change proposed against an unknown capability_id is refused.
  8. A change proposed for a non-DEV environment is refused.
  9. A capability at Needs spike cannot execute as an ordinary write --
     even with an otherwise-valid story/change/scope approval.
 10. The SAME Needs-spike capability CAN execute once (and only once)
     scope.json explicitly approves it as a bounded spike experiment.

If every line says PASS, the safety model this whole project depends
on is actually working on your machine, not just described in a
document.
"""

import json
import os
import shutil
import sys

os.environ["JDE_MCP_MOCK_MODE"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))

USE_COLOR = sys.stdout.isatty() and os.name != "nt"
PASS = "\033[92mPASS\033[0m" if USE_COLOR else "PASS"
FAIL = "\033[91mFAIL\033[0m" if USE_COLOR else "FAIL"
failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    if condition:
        print(f"  {PASS}  {label}")
    else:
        print(f"  {FAIL}  {label}")
        failures += 1


def _clean_test_files() -> None:
    for d in ("backlog", "changes", "evidence"):
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.startswith("GATE-TEST"):
                    os.remove(os.path.join(d, fn))


print("Setting up a temporary test scope (your real scope.json, if any, is untouched)...")
had_scope = os.path.exists("scope.json")
if had_scope:
    shutil.copy("scope.json", "scope.json.bak-before-gate-test")

test_scope = {
    "customer": "GATE-TEST",
    "tools_release": "TEST",
    "scope_revision": "GATE-TEST-1",
    "environment": {
        "dev_environment_id": "DV900TEST",
        "dev_path_code": "TEST",
        "ais_data_source_name": "TEST Data Source",
        "isolation_confirmed": True,
        "isolation_evidence": "prove_the_gate.py test fixture -- not a real isolation check",
    },
    "functional_agent": {
        "approved_versions": [
            {
                "capability_id": "processing_option_update",
                "capability_revision": "r1",
                "application": "P4210",
                "version": "TESTVER01",
                "options": ["PDOCTYPE"],
                "allowed_values": ["SO"],
                "notes": "prove_the_gate.py test entry -- not a real version",
            },
            {
                "capability_id": "processing_option_update",
                "capability_revision": "r1",
                "application": "P4210",
                "version": "TESTVER02",
                "options": ["PDOCTYPE"],
                "allowed_values": ["SO"],
                "notes": "prove_the_gate.py test entry for the Needs-spike gate (deliberately NOT in spike_experiments yet)",
            },
        ],
        # TESTVER01 is pre-approved as a bounded spike experiment so
        # steps 1-6 below (the original gate proof) keep demonstrating
        # a full successful write -- processing_option_update is Needs
        # spike in capability_catalog.json, so without this entry even
        # a fully-approved change would now be refused (step 9 proves
        # exactly that, using TESTVER02, which has no such entry).
        "spike_experiments": [{
            "capability_id": "processing_option_update",
            "capability_revision": "r1",
            "application": "P4210",
            "version": "TESTVER01",
            "option": "PDOCTYPE",
            "environment": "DEV",
            "approved_by": "Gate Test Runner",
            "approved_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "note": "prove_the_gate.py fixture spike approval",
        }],
        "never_touch_categories": [],
        "approvers": ["Gate Test Runner"],
    },
    "technical_agent": {"authorized_object_types": [], "reserved_product_code": "56", "naming_prefix": "TST", "approvers": []},
}
with open("scope.json", "w", encoding="utf-8") as f:
    json.dump(test_scope, f, indent=2)

_clean_test_files()

try:
    from jde_mcp_server.ais_client import client
    from jde_mcp_server.approval import ChangeApprovalError, approve_change, propose_change
    from jde_mcp_server.backlog import (
        StoryNotApproved,
        approve,
        propose_to_backlog,
        resolve_without_change,
    )
    from jde_mcp_server.capability_catalog import CapabilityError
    from jde_mcp_server.evidence import capture_evidence, verify_chain

    print("\n1. A story that has NOT been approved...")
    propose_to_backlog("GATE-TEST-1", "gate test story", {"financial_impact": "Low"}, "Low")
    try:
        client.set_processing_option("GATE-TEST-1", "no-such-change", "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("cannot be written to", False)
    except StoryNotApproved:
        check("cannot be written to", True)

    print("\n2. An approved story with NO approved change yet...")
    approve("GATE-TEST-1", "Gate Test Runner", "approving for the gate test")
    operation = {
        "tool": "set_processing_option", "story_id": "GATE-TEST-1",
        "application": "P4210", "version": "TESTVER01", "option": "PDOCTYPE", "value": "SO",
    }
    change = propose_change("GATE-TEST-1", operation, "processing_option_update")
    try:
        client.set_processing_option("GATE-TEST-1", change["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("still cannot be written to", False)
    except ChangeApprovalError:
        check("still cannot be written to", True)

    print("\n3. Approving the change, then trying a DIFFERENT value than what was approved...")
    approve_change(change["change_id"], "Gate Test Runner", note="approving for the gate test")
    try:
        client.set_processing_option("GATE-TEST-1", change["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SV")
        check("a tampered operation is refused", False)
    except ChangeApprovalError:
        check("a tampered operation is refused", True)

    print("\n4. Running the EXACT operation that was approved...")
    try:
        result = client.set_processing_option("GATE-TEST-1", change["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("the exact approved operation succeeds", result.get("tool") == "set_processing_option")
    except Exception as e:  # noqa: BLE001
        check(f"the exact approved operation succeeds (unexpected error: {e})", False)

    print("\n5. Evidence and tamper-evidence...")
    capture_evidence("GATE-TEST-1", {"stage": "test", "detail": "gate test write"})
    chain = verify_chain("GATE-TEST-1")
    check("evidence was captured and the tamper-evidence chain verifies", chain.get("valid") is True)

    print("\n6. Resolving a story with no JDE change at all...")
    propose_to_backlog("GATE-TEST-2", "gate test story 2", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-2", "Gate Test Runner", "approving for the gate test")
    rec = resolve_without_change("GATE-TEST-2", "Existing configuration already covers this (test).", "Gate Test Runner")
    check("a story can be resolved without touching JDE", rec.get("status") == "resolved_without_change")

    print("\n7. Proposing a change against an unknown capability_id...")
    try:
        propose_change("GATE-TEST-1", operation, "not-a-real-capability")
        check("an unknown capability_id is refused", False)
    except CapabilityError:
        check("an unknown capability_id is refused", True)

    print("\n8. Proposing a change for a non-DEV environment...")
    try:
        propose_change("GATE-TEST-1", operation, "processing_option_update", environment="PROD")
        check("a non-DEV environment is refused", False)
    except ChangeApprovalError:
        check("a non-DEV environment is refused", True)

    print("\n9. A Needs-spike capability WITHOUT an approved spike experiment...")
    propose_to_backlog("GATE-TEST-3", "gate test story 3", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-3", "Gate Test Runner", "approving for the gate test")
    op2 = {
        "tool": "set_processing_option", "story_id": "GATE-TEST-3",
        "application": "P4210", "version": "TESTVER02", "option": "PDOCTYPE", "value": "SO",
    }
    change2 = propose_change("GATE-TEST-3", op2, "processing_option_update")
    approve_change(change2["change_id"], "Gate Test Runner", note="approving for the gate test")
    try:
        client.set_processing_option("GATE-TEST-3", change2["change_id"], "P4210", "TESTVER02", "PDOCTYPE", "SO")
        check("a fully-approved change on a Needs-spike capability is still refused without a spike experiment", False)
    except CapabilityError:
        check("a fully-approved change on a Needs-spike capability is still refused without a spike experiment", True)

    print("\n10. The SAME operation, once scope.json approves it as a bounded spike experiment...")
    test_scope["functional_agent"]["spike_experiments"].append({
        "capability_id": "processing_option_update",
        "capability_revision": "r1",
        "application": "P4210",
        "version": "TESTVER02",
        "option": "PDOCTYPE",
        "environment": "DEV",
        "approved_by": "Gate Test Runner",
        "approved_at": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "note": "prove_the_gate.py step 10 -- approving the same target as a spike experiment",
    })
    with open("scope.json", "w", encoding="utf-8") as f:
        json.dump(test_scope, f, indent=2)
    try:
        result = client.set_processing_option("GATE-TEST-3", change2["change_id"], "P4210", "TESTVER02", "PDOCTYPE", "SO")
        check("the same operation succeeds once explicitly approved as a spike experiment", result.get("tool") == "set_processing_option")
    except Exception as e:  # noqa: BLE001
        check(f"the same operation succeeds once explicitly approved as a spike experiment (unexpected error: {e})", False)

finally:
    print("\nCleaning up test data and restoring your scope.json...")
    _clean_test_files()
    if had_scope:
        shutil.move("scope.json.bak-before-gate-test", "scope.json")
    else:
        os.remove("scope.json")
        print("(No scope.json existed before this test, so none was left behind.")
        print(" Copy scope.example.json to scope.json and fill it in before running real stories.)")

print()
if failures == 0:
    print("ALL CHECKS PASSED -- the safety gate works as designed on this machine.")
    sys.exit(0)
else:
    print(f"{failures} CHECK(S) FAILED -- do not proceed with real stories until this is fixed.")
    sys.exit(1)
