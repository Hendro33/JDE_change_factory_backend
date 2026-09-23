#!/usr/bin/env python3
"""
Phase 2 — Backlog & Approval, and exact-change approval (design
document Sections 3.5, 15.3).

This is a human tool, run directly by a person, on purpose. There is no
MCP tool that does what this script does, and no subagent is given
tools that could approve or reject a story or a change. If you want
Phase 3 to be able to act on a story, run this and approve it here --
and separately, before any write actually executes, approve the exact
operation it proposes.

Usage:
    python3 backlog_review.py list
    python3 backlog_review.py show STORY_ID
    python3 backlog_review.py approve STORY_ID "your name" ["optional note"]
    python3 backlog_review.py reject STORY_ID "your name" "reason (required)"

    python3 backlog_review.py list-changes
    python3 backlog_review.py show-change CHANGE_ID

Exact changes are listed and shown here, but approved or rejected only
in Jade (Governance > Architecture Review): that decision needs an
authenticated approver whose role the company's approval policy
allows, which a local script cannot establish.
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))

from jde_mcp_server import backlog  # noqa: E402
from jde_mcp_server import approval  # noqa: E402


def _print_story(rec: dict) -> None:
    print(f"\n{'='*70}")
    print(f"story_id: {rec['story_id']}")
    print(f"status:   {rec['status']}")
    print(f"\n{rec['user_story']}\n")
    print("Business impact (Section 3.6):")
    for k, v in (rec.get("business_impact") or {}).items():
        print(f"  {k}: {v}")
    print(f"\nRough complexity signal (heuristic only): {rec.get('rough_complexity_signal')}")
    print(f"Source: {rec.get('source')}")
    print(f"{'='*70}\n")


def _print_change(rec: dict) -> None:
    print(f"\n{'='*70}")
    print(f"change_id: {rec['change_id']}  (story: {rec['story_id']})")
    print(f"status:    {rec['status']}")
    print(f"environment: {rec['environment']}")
    print("\nExact operation this will execute if approved:")
    print(json.dumps(rec["operation"], indent=2))
    print(f"\nchange_hash: {rec['change_hash']}")
    print("This hash is what set_processing_option/run_orchestration check against --")
    print("if the operation changes even slightly after you approve, the hash won't")
    print("match and the write will be refused.")
    print(f"{'='*70}\n")


def cmd_list():
    pending = backlog.list_pending()
    if not pending:
        print("No stories waiting for review.")
        return
    print(f"{len(pending)} story(ies) waiting for Phase 2 approval:\n")
    for rec in pending:
        print(f"  {rec['story_id']}  --  {rec['user_story'][:70]}")
    print("\nUse 'show STORY_ID' to see the full story before deciding.")


def cmd_show(story_id: str):
    for rec in backlog.list_pending():
        if rec["story_id"] == story_id:
            _print_story(rec)
            return
    print(f"'{story_id}' isn't pending (already decided, or doesn't exist).")


def cmd_approve(story_id: str, decided_by: str, note: str = ""):
    backlog.approve(story_id, decided_by, note)
    print(f"Approved {story_id} by {decided_by}. Phase 3 can now propose a change against it.")


def cmd_reject(story_id: str, decided_by: str, note: str):
    backlog.reject(story_id, decided_by, note)
    print(f"Rejected {story_id} by {decided_by}: {note}")
    print("This story is done -- if the need still stands, it should re-enter Phase 1 as a fresh draft, not be re-approved here.")


def cmd_list_changes():
    pending = approval.list_pending_changes()
    if not pending:
        print("No proposed changes waiting for review.")
        return
    print(f"{len(pending)} change(s) waiting for exact-change approval:\n")
    for rec in pending:
        print(f"  {rec['change_id']}  (story {rec['story_id']})  --  {json.dumps(rec['operation'])[:70]}")
    print("\nUse 'show-change CHANGE_ID' to see the exact operation before deciding.")


def cmd_show_change(change_id: str):
    for rec in approval.list_pending_changes():
        if rec["change_id"] == change_id:
            _print_change(rec)
            return
    print(f"'{change_id}' isn't pending (already decided, or doesn't exist).")


EXACT_CHANGE_DECISIONS_MOVED = (
    "Exact-change decisions need an authenticated approver whose role the company's approval policy allows. This local tool has no login, so it cannot make them: approve or reject the change in Jade (Governance > Architecture Review)."
)


def cmd_approve_change(*_args):
    raise approval.ChangeApprovalError(EXACT_CHANGE_DECISIONS_MOVED)


def cmd_reject_change(*_args):
    raise approval.ChangeApprovalError(EXACT_CHANGE_DECISIONS_MOVED)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "list":
            cmd_list()
        elif cmd == "show":
            cmd_show(*rest)
        elif cmd == "approve":
            cmd_approve(*rest)
        elif cmd == "reject":
            cmd_reject(*rest)
        elif cmd == "list-changes":
            cmd_list_changes()
        elif cmd == "show-change":
            cmd_show_change(*rest)
        elif cmd == "approve-change":
            cmd_approve_change(*rest)
        elif cmd == "reject-change":
            cmd_reject_change(*rest)
        else:
            print(__doc__)
            sys.exit(1)
    except TypeError:
        print(__doc__)
        sys.exit(1)
    except (backlog.BacklogError, approval.ChangeApprovalError) as e:
        print(f"Error: {e}")
        sys.exit(1)
