"""
Reads the EXISTING .claude/agents/*.md subagent definitions (unmodified
-- this module only reads them) and combines that with each driver's
own, already-hardcoded ClaudeAgentOptions to answer "what is agent X
actually configured to do right now," for Admin > Agents.

This is deliberately NOT a prompt-editing surface: nothing here writes
to a .md file, and there is no authoring UI behind it. Design doc
Section 4.7's "primarily configuration and knowledge management" stays
a code-review-and-deploy process, not an in-app editor -- Section
14.1's Capture -> Analyse -> Improve -> Evaluate -> Approve -> Release
loop remains human/engineering-owned.

"Version" is a content hash of the .md file, computed at read time --
the smallest honest stand-in for Section 14.3's "every production-used
agent carries a version identifier" that doesn't require building a
real versioning/release workflow.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Optional

from ..models.agent_registry import AgentDefinition, AgentRuntimeConfig

_AGENTS_DIR = ".claude/agents"

# Static, honest map of which api_service driver (if any) currently
# invokes each subagent. Built from each driver's own module-level
# constants (imported below, never duplicated by hand) so this
# registry can never silently drift from what a driver actually does.
# functional-agent is deliberately absent: no driver here invokes it
# today -- it is still run directly via Claude Code, not orchestrated
# from this service (see architecture_driver.py's own docstring on
# what it does and does not automate).
def _driver_configs() -> dict[str, AgentRuntimeConfig]:
    from . import architecture_driver, orchestration_driver, review_driver

    orchestration = AgentRuntimeConfig(
        driver="orchestration_driver",
        model=None,
        permission_mode=orchestration_driver.PERMISSION_MODE,
        max_turns=orchestration_driver.MAX_TURNS,
        allowed_tools=list(orchestration_driver._ALLOWED_TOOLS),
    )
    review = AgentRuntimeConfig(
        driver="review_driver",
        model=None,
        permission_mode=review_driver.PERMISSION_MODE,
        max_turns=review_driver.MAX_TURNS,
        allowed_tools=list(orchestration_driver._ALLOWED_TOOLS),
    )
    architecture = AgentRuntimeConfig(
        driver="architecture_driver",
        model=None,
        permission_mode=architecture_driver.PERMISSION_MODE,
        max_turns=architecture_driver.MAX_TURNS,
        allowed_tools=list(architecture_driver._ALLOWED_TOOLS),
    )
    return {
        "receive-agent": orchestration,
        # improve-agent is invoked from two places: the initial
        # Receive/Improve/Check loop (orchestration_driver) and a
        # single refinement pass after a Domain Owner edit
        # (review_driver). The orchestration_driver entry is shown as
        # its primary/most frequent invocation.
        "improve-agent": orchestration,
        "check-agent": orchestration,
        "architect": architecture,
    }


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal, deliberately non-general frontmatter reader for this
    repo's own agent files -- not a YAML parser. Handles exactly the
    three single-line fields these files use (name/description/tools);
    tools may be empty (receive-agent's `tools:` line has no value)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def compute_agent_version(agent_name: str, repo_root: str) -> Optional[str]:
    """Small, dependency-free lookup a driver can call at run-start
    time to stamp an AgentRun with the .md file's content hash. Returns
    None if the file can't be found -- never raises, since a missing
    definition file here should not fail the agent run itself."""
    path = os.path.join(repo_root, _AGENTS_DIR, f"{agent_name}.md")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return _content_hash(f.read())


class AgentRegistryService:
    def __init__(self, repo_root: str) -> None:
        self._dir = os.path.join(repo_root, _AGENTS_DIR)

    def list_agents(self) -> list[AgentDefinition]:
        if not os.path.isdir(self._dir):
            return []
        configs = _driver_configs()
        out = []
        for fn in sorted(os.listdir(self._dir)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(self._dir, fn)
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            fields = _parse_frontmatter(text)
            name = fields.get("name") or fn[:-3]
            tools_raw = fields.get("tools", "")
            tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
            out.append(
                AgentDefinition(
                    name=name,
                    description=fields.get("description", ""),
                    declared_tools=tools,
                    version=_content_hash(text),
                    file_updated_at=mtime,
                    runtime=configs.get(name),
                )
            )
        return out

    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        for agent in self.list_agents():
            if agent.name == name:
                return agent
        return None
