#!/usr/bin/env python3
"""
Run this once, right after Step 7 of GETTING_STARTED.md, before doing
anything else with a real story.

    python3 prove_the_gate.py

It proves, in order, using its own throwaway story IDs and a temporary
test company whose scope and story links live in a temporary directory
(nothing of yours is read or changed, so it is safe to re-run any time):

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
 10. The SAME Needs-spike capability CAN execute once the company's
     scope explicitly approves it as a dated spike experiment.
 11. An expired spike experiment allows nothing.
 12. A story not linked to a company cannot even be proposed.
 13. A company with no saved scope cannot approve anything.
 14. With no approval policy, nobody can approve an exact change.
 15. An approver whose role the policy does not allow is refused.
 16. An approval past its policy-set validity cannot execute or run tests.
 17. An applied change never runs a second time.
 18. A write whose outcome is unknown blocks any retry until reconciled.
 19. A write is refused when the company has not allowed its mechanism.
 20. A write is refused when the target's option category is protected
     or on the company's never-touch list (free-text notes do not count).
 21. A test is refused unless it is an approved test whose declared side
     effects the capability permits.
 22. An approval stops being usable the moment its approver loses the
     role (or membership) that allowed it -- checked inside the attempt
     lock, immediately before dispatch.

If every line says PASS, the safety model this whole project depends
on is actually working on your machine, not just described in a
document.
"""

import json
import os
import sys
import tempfile
import time

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
                if fn.lstrip(".").startswith("GATE-TEST"):  # includes per-change lock files
                    os.remove(os.path.join(d, fn))


print("Setting up a temporary test company (nothing of yours is read or changed)...")
_tmp = tempfile.TemporaryDirectory(prefix="jade-gate-test-")
SCOPE_DIR = os.path.join(_tmp.name, "engagement_scope")
LINK_DIR = os.path.join(_tmp.name, "customer_links")
os.makedirs(SCOPE_DIR)
os.makedirs(LINK_DIR)
# Set before jde_mcp_server is imported below, which reads them once.
os.environ["JDE_COMPANY_SCOPE_DIR"] = SCOPE_DIR
os.environ["JDE_STORY_COMPANY_DIR"] = LINK_DIR
os.environ["JDE_MOCK_JDE_STATE_FILE"] = os.path.join(_tmp.name, "mock_jde_state.json")
COMPANY = "GATE-TEST-CO"
APPROVER_ROLES = {"product_manager"}
APPROVER_ID = "u-gate-test"

# The gate re-reads the approver's CURRENT roles from the membership
# database before every dispatch. A minimal stand-in with just the three
# tables it reads, holding one approver with the Product Manager role.
AUTH_DB = os.path.join(_tmp.name, "auth.sqlite3")
os.environ["JDE_AUTH_DB_PATH"] = AUTH_DB


def set_approver_roles(roles: list[str], membership_status: str = "active") -> None:
    import sqlite3

    conn = sqlite3.connect(AUTH_DB)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, is_active INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS company_memberships (id TEXT PRIMARY KEY, user_id TEXT, company_id TEXT, status TEXT);"
        "CREATE TABLE IF NOT EXISTS membership_roles (membership_id TEXT, role TEXT);"
        "DELETE FROM users; DELETE FROM company_memberships; DELETE FROM membership_roles;"
    )
    conn.execute("INSERT INTO users VALUES (?, 1)", (APPROVER_ID,))
    conn.execute("INSERT INTO company_memberships VALUES ('m-gate', ?, ?, ?)", (APPROVER_ID, COMPANY, membership_status))
    conn.executemany("INSERT INTO membership_roles VALUES ('m-gate', ?)", [(r,) for r in roles])
    conn.commit()
    conn.close()


set_approver_roles(["product_manager"])


def write_scope(scope: dict) -> None:
    with open(os.path.join(SCOPE_DIR, f"{COMPANY}.json"), "w", encoding="utf-8") as f:
        json.dump(scope, f, indent=2)


def link(story_id: str, company: str = COMPANY) -> None:
    with open(os.path.join(LINK_DIR, f"{story_id}.json"), "w", encoding="utf-8") as f:
        json.dump({"story_id": story_id, "customer_id": company}, f)


test_scope = {
    "customer_id": COMPANY,
    "tools_release": "TEST",
    "revision": 1,
    "approval_policy": {"policy_version": 1, "exact_change_approver_roles": ["product_manager"], "approval_valid_hours": 24},
    "mechanisms_allowed": ["ais_form_service_request", "ais_orchestration"],
    "test_scope": {"approved_tests": [
        {"orchestration": "ORCH_GATE_TEST", "side_effects": ["creates_dev_transaction"], "note": "prove_the_gate.py fixture"},
    ]},
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
                "option_category": "document_and_order_types",
                "options": ["PDOCTYPE"],
                "allowed_values": ["SO"],
                "notes": "prove_the_gate.py test entry -- not a real version",
            },
            {
                "capability_id": "processing_option_update",
                "capability_revision": "r1",
                "application": "P4210",
                "version": "TESTVER02",
                "option_category": "document_and_order_types",
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
write_scope(test_scope)
for sid in ("GATE-TEST-1", "GATE-TEST-2", "GATE-TEST-3", "GATE-TEST-4"):
    link(sid)

_clean_test_files()

try:
    from jde_mcp_server.ais_client import client
    from jde_mcp_server.approval import ApproverNotAuthorised, ChangeApprovalError, approve_change, propose_change
    from jde_mcp_server import approval as approval_module
    from jde_mcp_server.scope import ScopeViolation
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
    approve_change(change["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID, note="approving for the gate test")
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
    approve_change(change2["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID, note="approving for the gate test")
    try:
        client.set_processing_option("GATE-TEST-3", change2["change_id"], "P4210", "TESTVER02", "PDOCTYPE", "SO")
        check("a fully-approved change on a Needs-spike capability is still refused without a spike experiment", False)
    except CapabilityError:
        check("a fully-approved change on a Needs-spike capability is still refused without a spike experiment", True)

    print("\n10. The SAME operation, once the company scope approves it as a dated spike experiment...")
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
    write_scope(test_scope)
    try:
        result = client.set_processing_option("GATE-TEST-3", change2["change_id"], "P4210", "TESTVER02", "PDOCTYPE", "SO")
        check("the same operation succeeds once explicitly approved as a spike experiment", result.get("tool") == "set_processing_option")
    except Exception as e:  # noqa: BLE001
        check(f"the same operation succeeds once explicitly approved as a spike experiment (unexpected error: {e})", False)

    print("\n11. A new approved change on the same target, once the spike's expiry has passed...")
    test_scope["functional_agent"]["spike_experiments"][-1]["expires_at"] = "2020-01-01T00:00:00Z"
    write_scope(test_scope)
    change2b = propose_change("GATE-TEST-3", op2, "processing_option_update")
    approve_change(change2b["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
    try:
        client.set_processing_option("GATE-TEST-3", change2b["change_id"], "P4210", "TESTVER02", "PDOCTYPE", "SO")
        check("an expired spike experiment allows nothing", False)
    except CapabilityError:
        check("an expired spike experiment allows nothing", True)

    print("\n12. A story that is not linked to any company...")
    propose_to_backlog("GATE-TEST-UNLINKED", "gate test story (no company)", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-UNLINKED", "Gate Test Runner", "approving for the gate test")
    try:
        propose_change("GATE-TEST-UNLINKED", {**operation, "story_id": "GATE-TEST-UNLINKED"}, "processing_option_update")
        check("an unattributed story cannot even be proposed", False)
    except ScopeViolation:
        check("an unattributed story cannot even be proposed", True)

    print("\n13. A story belonging to a company with no saved scope...")
    propose_to_backlog("GATE-TEST-OTHERCO", "gate test story (other company)", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-OTHERCO", "Gate Test Runner", "approving for the gate test")
    link("GATE-TEST-OTHERCO", "GATE-TEST-OTHER-CO")
    other = propose_change("GATE-TEST-OTHERCO", {**operation, "story_id": "GATE-TEST-OTHERCO"}, "processing_option_update")
    try:
        approve_change(other["change_id"], "Gate Test Runner", company_id="GATE-TEST-OTHER-CO", approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
        check("another company's scope never stands in for this one", False)
    except ScopeViolation:
        check("another company's scope never stands in for this one", True)

    print("\n14. With no approval policy saved for the company...")
    propose_to_backlog("GATE-TEST-4", "gate test story 4", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-4", "Gate Test Runner", "approving for the gate test")
    op4 = {**operation, "story_id": "GATE-TEST-4"}
    change4 = propose_change("GATE-TEST-4", op4, "processing_option_update")
    saved_policy = test_scope.pop("approval_policy")
    write_scope(test_scope)
    try:
        approve_change(change4["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
        check("nobody can approve an exact change", False)
    except ScopeViolation:
        check("nobody can approve an exact change", True)
    test_scope["approval_policy"] = saved_policy
    write_scope(test_scope)

    print("\n15. An approver whose role the policy does not allow...")
    try:
        approve_change(change4["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles={"domain_owner"}, approver_user_id=APPROVER_ID)
        check("an approver without an allowed role is refused", False)
    except ApproverNotAuthorised:
        check("an approver without an allowed role is refused", True)

    print("\n16. An approval past its validity window...")
    approve_change(change4["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
    rec = approval_module._load(change4["change_id"])
    rec["expires_at"] = time.time() - 1
    approval_module._save(change4["change_id"], rec)
    try:
        client.set_processing_option("GATE-TEST-4", change4["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("an expired approval cannot execute", False)
    except ChangeApprovalError:
        check("an expired approval cannot execute", True)
    try:
        client.run_orchestration("GATE-TEST-4", change4["change_id"], "", {})
        check("an expired approval cannot run its test either", False)
    except ChangeApprovalError:
        check("an expired approval cannot run its test either", True)

finally:
    from jde_mcp_server import ais_client as ais_module, execution
    import httpx

    print("\n17. Running an already-applied change again...")
    try:
        client.set_processing_option("GATE-TEST-1", change["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("an applied change never runs a second time", False)
    except execution.ExecutionBlocked:
        check("an applied change never runs a second time", True)

    print("\n18. A write interrupted after it was sent (outcome unknown), then retried...")
    propose_to_backlog("GATE-TEST-5", "gate test story 5", {"financial_impact": "Low"}, "Low")
    approve("GATE-TEST-5", "Gate Test Runner", "approving for the gate test")
    link("GATE-TEST-5")
    op5 = {**operation, "story_id": "GATE-TEST-5"}
    change5 = propose_change("GATE-TEST-5", op5, "processing_option_update")
    approve_change(change5["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
    real_submit = ais_module._mock_submit

    def timed_out(*args):
        raise httpx.ReadTimeout("no response (gate test)")

    ais_module._mock_submit = timed_out
    try:
        client.set_processing_option("GATE-TEST-5", change5["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
    except httpx.ReadTimeout:
        pass
    finally:
        ais_module._mock_submit = real_submit
    check("the interrupted write is recorded as unknown",
          execution.effective_state(approval_module._load(change5["change_id"])) == "unknown")
    try:
        client.set_processing_option("GATE-TEST-5", change5["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        check("a blind retry is refused until the target is reconciled", False)
    except execution.ExecutionBlocked:
        check("a blind retry is refused until the target is reconciled", True)

    def _write_blocked(story: str) -> bool:
        propose_to_backlog(story, f"gate test {story}", {"financial_impact": "Low"}, "Low")
        approve(story, "Gate Test Runner", "approving for the gate test")
        link(story)
        ch = propose_change(story, {**operation, "story_id": story}, "processing_option_update")
        approve_change(ch["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
        try:
            client.set_processing_option(story, ch["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
            return False
        except ScopeViolation:
            return True

    print("\n19. A write whose mechanism the company has not allowed...")
    test_scope["mechanisms_allowed"] = ["ais_orchestration"]
    write_scope(test_scope)
    check("a write is refused when its mechanism is not allowed", _write_blocked("GATE-TEST-6"))
    test_scope["mechanisms_allowed"] = ["ais_form_service_request", "ais_orchestration"]

    print("\n20. A write to a protected or never-touch option category...")
    entry = test_scope["functional_agent"]["approved_versions"][0]
    entry["option_category"] = "pricing"
    write_scope(test_scope)
    check("a protected option category is refused", _write_blocked("GATE-TEST-7"))
    entry["option_category"] = "document_and_order_types"
    test_scope["functional_agent"]["never_touch_categories"] = ["document_and_order_types"]
    write_scope(test_scope)
    check("a never-touch option category is refused", _write_blocked("GATE-TEST-8"))
    test_scope["functional_agent"]["never_touch_categories"] = []
    test_scope["functional_agent"]["never_touch_notes"] = ["document types (free text only)"]
    write_scope(test_scope)
    check("free-text notes are not enforcement: they neither block nor unblock", not _write_blocked("GATE-TEST-9"))

    print("\n21. A test that is not approved, or whose side effects are not permitted...")
    for story, tests in (
        ("GATE-TEST-10", []),
        ("GATE-TEST-11", [{"orchestration": "ORCH_GATE_TEST", "side_effects": ["posting"]}]),
    ):
        test_scope["test_scope"]["approved_tests"] = tests
        write_scope(test_scope)
        propose_to_backlog(story, f"gate test {story}", {"financial_impact": "Low"}, "Low")
        approve(story, "Gate Test Runner", "approving for the gate test")
        link(story)
        ch = propose_change(story, {**operation, "story_id": story, "test_orchestration": "ORCH_GATE_TEST"},
                            "processing_option_update")
        approve_change(ch["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
        client.set_processing_option(story, ch["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
        try:
            client.run_orchestration(story, ch["change_id"], "ORCH_GATE_TEST", {})
            ok = False
        except ScopeViolation:
            ok = True
        label = "an unapproved test is refused" if not tests else "a test with a forbidden side effect is refused"
        check(label, ok)

    print("\n22. The approver loses their role after approving...")
    test_scope["test_scope"]["approved_tests"] = [
        {"orchestration": "ORCH_GATE_TEST", "side_effects": ["creates_dev_transaction"]}]
    write_scope(test_scope)
    for story, change_roles, status, label in (
        ("GATE-TEST-12", [], "active", "a role revoked after approval blocks dispatch"),
        ("GATE-TEST-13", ["product_manager"], "inactive", "a deactivated membership blocks dispatch"),
    ):
        propose_to_backlog(story, f"gate test {story}", {"financial_impact": "Low"}, "Low")
        approve(story, "Gate Test Runner", "approving for the gate test")
        link(story)
        ch = propose_change(story, {**operation, "story_id": story}, "processing_option_update")
        approve_change(ch["change_id"], "Gate Test Runner", company_id=COMPANY, approver_roles=APPROVER_ROLES, approver_user_id=APPROVER_ID)
        set_approver_roles(change_roles, status)
        try:
            client.set_processing_option(story, ch["change_id"], "P4210", "TESTVER01", "PDOCTYPE", "SO")
            check(label, False)
        except ChangeApprovalError as e:
            check(label, "no longer holds" in str(e))
        set_approver_roles(["product_manager"])

    print("\nCleaning up test data...")
    _clean_test_files()
    _tmp.cleanup()

print()
if failures == 0:
    print("ALL CHECKS PASSED -- the safety gate works as designed on this machine.")
    sys.exit(0)
else:
    print(f"{failures} CHECK(S) FAILED -- do not proceed with real stories until this is fixed.")
    sys.exit(1)
