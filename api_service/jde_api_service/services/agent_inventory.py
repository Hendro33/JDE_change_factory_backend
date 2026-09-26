"""
The ONE canonical inventory of Jade's agents.

Admin > Agents & AI shows this list and nothing else, so the roster, the
on/off switches and the AI configuration can never list different agents.
It joins, by the same key, what already exists:

  * agent_settings.AGENT_LABELS -- the agents a customer can switch on/off
  * ai.packs.ROLES             -- the agents that run inside Jade with a
                                  customer's AI connection and Start-up Pack
  * .claude/agents/*.md        -- the agents' own definition files
"""

from __future__ import annotations

from typing import Optional

from ..models.base import ApiModel

GROUPS = {
    "receive-agent": "Requirements", "improve-agent": "Requirements", "check-agent": "Requirements",
    "process-analyst": "Requirements", "architect": "Solution", "functional-agent": "Delivery",
    "technical-agent": "Delivery",
}
PURPOSE = {
    "receive-agent": "Turns every incoming request into Jade's standard requirement shape.",
    "improve-agent": "Adds business context, acceptance criteria, rules, assumptions and a test script.",
    "check-agent": "Checks the story is clear, complete and testable before it goes to review.",
    "process-analyst": "Places the story in the business process framework and finds missing requirements.",
    "architect": "Researches the JD Edwards environment and proposes how the story should be delivered.",
    "functional-agent": "Applies approved configuration changes in JD Edwards DEV and verifies them.",
    "technical-agent": "Prepares the exact technical package for an approved design; applied only through the gate.",
}


class AgentInventoryEntry(ApiModel):
    key: str
    label: str
    group: str
    purpose: str
    enabled: bool
    # Runs inside Jade on the customer's AI connection (has a Start-up Pack role).
    runs_in_jade: bool
    # Name of its definition file in .claude/agents, if it has one.
    definition: Optional[str] = None
    note: str = ""


def inventory(company_id: str, repo_root: str) -> list[AgentInventoryEntry]:
    from ..ai import packs
    from . import agent_settings
    from .agent_registry_service import AgentRegistryService

    disabled = agent_settings.disabled_agents(company_id)
    defs = {a.name: a for a in AgentRegistryService(repo_root).list_agents()}
    out = []
    for key, label in agent_settings.AGENT_LABELS.items():
        runs = key in packs.ROLES
        out.append(AgentInventoryEntry(
            key=key, label=label, group=GROUPS.get(key, "Other"), purpose=PURPOSE.get(key, ""),
            enabled=key not in disabled, runs_in_jade=runs, definition=key if key in defs else None,
            note="" if runs else "Runs outside Jade today (through Claude Code), under the same execution gate.",
        ))
    return out
