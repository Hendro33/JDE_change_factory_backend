"""
The runtime every Jade agent run gets, in one place.

  * Built-in tools: only Task (to reach the named subagent). Without this the
    Claude Code runtime offers its whole built-in set -- Bash, Read, Write,
    WebFetch, scheduling and more -- and some of those run without a
    permission prompt even in dontAsk mode. Subagents get their MCP tools
    from their own definitions (.claude/agents/*.md).
  * Environment: secrets the agent runtime never needs are blanked in the
    CLI process (and so in the project MCP server it starts): the credential
    encryption keys, the execution AIS credential and the bootstrap password.
    The discovery tools run in THIS process, not the agent's, so they keep
    working.
"""

from __future__ import annotations

BASE_TOOLS = ["Task"]
SCRUBBED_ENV = {name: "" for name in (
    "JDE_CREDENTIAL_KEY", "JDE_CREDENTIAL_KEY_PREVIOUS", "JDE_AIS_USERNAME", "JDE_AIS_PASSWORD",
    "JDE_BOOTSTRAP_ADMIN_PASSWORD",
)}


def options(**kwargs):
    import claude_agent_sdk as sdk

    env = {**SCRUBBED_ENV, **kwargs.pop("env", {})}
    return sdk.ClaudeAgentOptions(tools=list(BASE_TOOLS), env=env, **kwargs)
