"""
No entry point can expose an unrestricted JDE read.

The former get_object / get_version / get_processing_options MCP tools read
any object through the execution AIS connection with no company scope. They
are removed from the .mcp.json server (the server every Claude Code run in
this repository loads), from ais_client, from every agent definition and
from every driver allowlist. JDE research goes only through the governed,
company-bound discovery tools; the Functional Agent reads only its own
change's target.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re

import pytest

from .conftest import headers
from .test_stage1_execution_safeguards import _approve, _approved_story, _full_scope, _propose, _save_scope

REPO = pathlib.Path(__file__).resolve().parents[2]
UNRESTRICTED = {"get_object", "get_version", "get_processing_options"}


def _registered_tools() -> set[str]:
    from jde_mcp_server import server

    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_the_mcp_json_server_registers_no_unrestricted_read():
    names = _registered_tools()
    assert not names & UNRESTRICTED
    assert {"read_approved_target", "get_design_baseline", "set_processing_option"} <= names
    mcp_json = json.loads((REPO / ".mcp.json").read_text())
    assert list(mcp_json["mcpServers"]) == ["jde-change-factory"]  # the only project MCP server


def test_the_execution_client_has_no_unrestricted_read_methods():
    from jde_mcp_server.ais_client import AISClient

    assert not UNRESTRICTED & set(dir(AISClient))


def test_no_agent_definition_or_driver_allowlist_names_an_unrestricted_read():
    from jde_api_service.discovery import architect_tools
    from jde_api_service.services import architecture_driver, conversation_driver, orchestration_driver, review_driver

    registered = _registered_tools()
    for md in (REPO / ".claude" / "agents").glob("*.md"):
        front = md.read_text().split("---")[1]
        line = next((l for l in front.splitlines() if l.startswith("tools:")), "tools:")
        tools = [t.strip() for t in line[len("tools:"):].split(",") if t.strip()]
        for tool in tools:
            server, _, name = tool.removeprefix("mcp__").partition("__")
            assert name not in UNRESTRICTED, (md.name, tool)
            if server == "jde-change-factory":
                assert name in registered, (md.name, tool)
            else:
                assert server == architect_tools.SERVER_NAME and tool in architect_tools.ALLOWED_TOOLS, (md.name, tool)
    allowlists = {
        "architecture": architecture_driver._ALLOWED_TOOLS,
        "orchestration": orchestration_driver._ALLOWED_TOOLS,
        "conversation(solution)": conversation_driver._SOLUTION_ALLOWED_TOOLS,
        "review": getattr(review_driver, "_ALLOWED_TOOLS", []),
    }
    for name, tools in allowlists.items():
        assert not any(t.rsplit("__", 1)[-1] in UNRESTRICTED for t in tools), name
    # Only the Architect gets JDE discovery, and only the governed tools.
    assert [t for t in allowlists["architecture"] if "jade-discovery" in t] == architect_tools.ALLOWED_TOOLS
    for name in ("orchestration", "conversation(solution)", "review"):
        assert not [t for t in allowlists[name] if "jade-discovery" in t], name


def test_hooks_and_settings_do_not_pre_approve_raw_reads():
    settings = (REPO / ".claude" / "settings.json").read_text()
    assert not re.search("get_object|get_version|get_processing_options", settings)


def test_read_approved_target_reads_only_the_changes_own_target(client):
    from jde_mcp_server.approval import ChangeApprovalError
    from jde_mcp_server.approved_target import read_approved_target

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-TS-1")
    change = _propose("S-TS-1")
    out = read_approved_target("S-TS-1", change["change_id"])
    assert out["available"] is True and out["target"] == {"application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE"}
    _approved_story("S-TS-2")
    with pytest.raises(ChangeApprovalError):
        read_approved_target("S-TS-2", change["change_id"])  # another story's change
    from jde_mcp_server import approval

    approval.reject_change(change["change_id"], "Hendro", "no", company_id="vdb")
    with pytest.raises(ChangeApprovalError, match="rejected"):
        read_approved_target("S-TS-1", change["change_id"])
