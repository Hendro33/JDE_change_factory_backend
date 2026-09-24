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
    from jde_api_service.technical import tools as technical_tools

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
            elif md.name == "technical-agent.md":
                assert tool in technical_tools.ALLOWED_TOOLS, (md.name, tool)
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


def test_the_architect_runtime_is_denied_every_other_project_tool():
    from jde_api_service.services import architecture_driver

    registered = _registered_tools()
    assert set(architecture_driver.PROJECT_SERVER_TOOLS) == registered  # the list is complete
    allowed = {t.rsplit("__", 1)[-1] for t in architecture_driver._ALLOWED_TOOLS if t.startswith("mcp__jde-change-factory__")}
    disallowed = {t.rsplit("__", 1)[-1] for t in architecture_driver._DISALLOWED_TOOLS}
    assert allowed | disallowed == registered and not allowed & disallowed
    assert {"set_processing_option", "run_orchestration", "capture_evidence", "read_approved_target"} <= disallowed


def test_every_agent_driver_uses_the_restricted_runtime():
    """Only Task as a built-in tool, and no credential/execution secrets in the
    agent process -- for every driver, with no way around the helper."""
    from jde_api_service.services import agent_runtime

    opts = agent_runtime.options(cwd="/tmp", permission_mode="dontAsk", allowed_tools=[])
    assert opts.tools == ["Task"]
    for name in ("JDE_CREDENTIAL_KEY", "JDE_CREDENTIAL_KEY_PREVIOUS", "JDE_AIS_PASSWORD"):
        assert opts.env[name] == ""
    services = REPO / "api_service" / "jde_api_service" / "services"
    for py in services.glob("*.py"):
        if py.name != "agent_runtime.py":
            assert "ClaudeAgentOptions(" not in py.read_text(), py.name


def test_the_architect_is_told_every_catalogue_capability_id():
    """propose_change fails closed on an unknown capability_id, so the
    Architect's prompt carries the catalogue's ids rather than leaving it to guess."""
    from jde_mcp_server import capability_catalog
    from jde_api_service.services import architecture_driver

    prompt = architecture_driver._build_prompt("S-X")
    for cap in capability_catalog.list_capabilities():
        assert cap["capability_id"] in prompt
    assert '"tool": "set_processing_option"' in prompt


# ---------------------------------------------------------------------
# Delegation: a subagent can never hold more than the run's own runtime
# ---------------------------------------------------------------------
def _agent_tools(name: str) -> list[str]:
    front = (REPO / ".claude" / "agents" / f"{name}.md").read_text().split("---")[1]
    line = next((l for l in front.splitlines() if l.startswith("tools:")), "tools:")
    return [t.strip() for t in line[len("tools:"):].split(",") if t.strip()]


def test_the_technical_runtime_is_task_plus_its_own_run_bound_tools_only():
    from jde_api_service.services import architecture_driver
    from jde_api_service.technical import driver as technical_driver
    from jde_api_service.technical import tools as technical_tools

    assert technical_driver.ALLOWED == ["Task", *technical_tools.ALLOWED_TOOLS]
    assert set(technical_driver.DISALLOWED) == {f"mcp__jde-change-factory__{t}" for t in architecture_driver.PROJECT_SERVER_TOOLS}
    # No technical tool approves, records a CNC activation, reads credentials or reaches a network or shell.
    names = [t.rsplit("__", 1)[-1] for t in technical_tools.ALLOWED_TOOLS]
    assert not [n for n in names if n.startswith(("approve", "reject", "record")) or "cnc" in n]
    for forbidden in ("credential", "shell", "bash", "http", "sql", "fetch", "write_file", "exec"):
        assert not any(forbidden in n.lower() for n in names), forbidden
    # The subagent definition asks for nothing its runtime does not grant.
    assert set(_agent_tools("technical-agent")) <= set(technical_driver.ALLOWED)


def test_no_subagent_definition_can_widen_its_drivers_runtime():
    """A subagent's tools come from the session that delegates to it: every
    tool an agent definition lists must be in the allowlist of the driver
    that runs it, and the runtime's built-ins are Task only -- so Task
    cannot hand a subagent Bash, WebFetch, file access or another server."""
    from jde_api_service.services import architecture_driver, conversation_driver, orchestration_driver
    from jde_api_service.technical import driver as technical_driver

    drivers = {
        "architect": architecture_driver._ALLOWED_TOOLS,
        "technical-agent": technical_driver.ALLOWED,
        "improve-agent": orchestration_driver._ALLOWED_TOOLS,
    }
    for agent, allowed in drivers.items():
        extra = set(_agent_tools(agent)) - set(allowed)
        assert not extra, (agent, extra)
    # The functional-agent has no driver in this increment: its write, test and
    # evidence tools are only reachable through the gate, never granted here.
    assert not any(set(_agent_tools("functional-agent")) & set(a) for a in
                   (architecture_driver._ALLOWED_TOOLS, technical_driver.ALLOWED, conversation_driver._SOLUTION_ALLOWED_TOOLS))


def test_the_technical_driver_builds_its_options_through_the_restricted_runtime(monkeypatch):
    """The options the technical driver actually passes to the runtime."""
    import asyncio

    import claude_agent_sdk as sdk

    from jde_api_service.technical import driver as technical_driver

    captured = {}

    class _Stop(Exception):
        pass

    class _FakeTools:
        outcome, calls = {}, []

        def __init__(self, **kwargs):
            pass

        def sdk_server(self):
            return sdk.create_sdk_mcp_server("jade-technical", tools=[])

    async def fake_query(prompt, options):
        captured["options"] = options
        captured["prompt"] = prompt
        raise _Stop()
        yield  # pragma: no cover

    monkeypatch.setattr(technical_driver, "TechnicalAgentTools", _FakeTools)
    monkeypatch.setattr(technical_driver.store, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(technical_driver.store, "finish_run", lambda *a, **k: None)
    asyncio.run(technical_driver.run_technical_agent(company_id="vdb", story_id="S-X", run_id="TR-0000000000",
                                                     repo_root=str(REPO), purpose="prepare", observer=fake_query))
    opts = captured["options"]
    assert opts.tools == ["Task"]
    assert list(opts.mcp_servers) == ["jade-technical"]
    assert opts.allowed_tools == technical_driver.ALLOWED
    assert set(opts.disallowed_tools) == set(technical_driver.DISALLOWED)
    for secret in ("JDE_CREDENTIAL_KEY", "JDE_AIS_USERNAME", "JDE_AIS_PASSWORD", "JDE_BOOTSTRAP_ADMIN_PASSWORD"):
        assert opts.env[secret] == ""
    assert "technical-agent subagent" in captured["prompt"]
