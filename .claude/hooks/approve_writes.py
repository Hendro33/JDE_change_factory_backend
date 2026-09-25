#!/usr/bin/env python3
"""
PreToolUse approval hook -- Section 8.1 of the design document.

This is what makes "every write is human-approved" a technical control
rather than a documented expectation. Claude Code runs this script
before any tool call whose name matches the hook's matcher in
.claude/settings.json, feeding it a JSON description of the call on
stdin. This script decides whether the call is allowed to proceed.

For the pilot this is a blocking CLI confirmation, exactly as Section
8.1 describes. Swap `_confirm_on_cli()` for a Slack/Teams approval call
once there are real users -- the rest of this script (what counts as a
write, how the decision is communicated back to Claude Code) does not
need to change.

Claude Code's hook JSON protocol has changed across versions; treat the
input/output shapes below as a starting point and check them against
your installed Claude Code version's hook documentation before relying
on this in anything beyond local testing.
"""

import json
import sys

# Tool names (as seen by Claude Code -- typically
# "mcp__<server-name>__<tool-name>") that this hook treats as writes.
# Extend this as Technical Agent write tools are validated (Section 7.6)
# -- do not assume a new tool is safe just because it isn't listed here;
# start every new tool as a write requiring approval and only relax that
# once it's proven read-only.
WRITE_TOOLS = {
    "mcp__jde-change-factory__set_processing_option",
    # A test run is an action in JDE too (it can create orders or other
    # records), so it gets the same interactive confirmation.
    "mcp__jde-change-factory__run_orchestration",
}


def _confirm_on_cli(tool_name: str, tool_input: dict) -> bool:
    sys.stderr.write("\n=== JDE WRITE APPROVAL REQUIRED ===\n")
    sys.stderr.write(f"Tool: {tool_name}\n")
    sys.stderr.write(f"Arguments: {json.dumps(tool_input, indent=2)}\n")
    sys.stderr.write("Approve this write? [y/N]: ")
    sys.stderr.flush()
    try:
        with open("/dev/tty") as tty:
            answer = tty.readline().strip().lower()
    except OSError:
        # No controlling terminal (e.g. running unattended) -- fail
        # closed. A write with nobody able to approve it must not
        # proceed silently.
        sys.stderr.write("\nNo interactive terminal available -- denying by default.\n")
        return False
    return answer in ("y", "yes")


def main() -> None:
    raw = sys.stdin.read()
    event = json.loads(raw) if raw else {}
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})

    if tool_name not in WRITE_TOOLS:
        # Not a write we gate -- allow silently.
        print(json.dumps({"decision": "approve"}))
        return

    if _confirm_on_cli(tool_name, tool_input):
        print(json.dumps({"decision": "approve"}))
    else:
        print(json.dumps({
            "decision": "block",
            "reason": "Write rejected at human approval gate (Section 8.1). "
                      "No JDE change was made.",
        }))


if __name__ == "__main__":
    main()
