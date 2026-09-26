"""
Which Jade agents are switched on for a company. An Admin manages this in
Admin > Agents; every place that starts an agent checks it, so a switched-off
agent never runs for that company. Stored in company_settings (revisioned,
attributed). Agent definitions, prompts and platform guardrails are not
editable here.
"""

from __future__ import annotations

from typing import Optional

from .company_settings_service import CompanySettingsService, StoredSetting

KEY = "agent_settings"
AGENT_LABELS = {
    "receive-agent": "Receive Agent",
    "improve-agent": "Improve Agent",
    "check-agent": "Requirements Agent",
    "architect": "Architect Agent",
    "functional-agent": "Functional Agent",
    "technical-agent": "Technical Agent",
    "process-analyst": "Process Analyst",
}

_service = CompanySettingsService()


class AgentDisabled(RuntimeError):
    pass


def stored(company_id: str) -> Optional[StoredSetting]:
    return _service.get(company_id, KEY)


def disabled_agents(company_id: Optional[str]) -> set[str]:
    if not company_id:
        return set()
    s = stored(company_id)
    return set((s.value.get("disabled") or []) if s else [])


def require_enabled(company_id: Optional[str], *agent_names: str) -> None:
    """Switched on for this customer AND runnable: the customer's own AI
    connection and an assigned, published Start-up Pack for each role
    (ai/runtime.require_ready). Either missing refuses the work up front."""
    off = [a for a in agent_names if a in disabled_agents(company_id)]
    if off:
        names = ", ".join(AGENT_LABELS.get(a, a) for a in off)
        raise AgentDisabled(f"{names} {'is' if len(off) == 1 else 'are'} switched off for this customer "
                            "(Admin > Agents); nothing was started")
    from ..ai import packs, runtime
    from ..ai.connection import AiNotConfigured

    roles = [a for a in agent_names if a in packs.ROLES]
    if roles:
        try:
            runtime.require_ready(company_id, roles, driver="api precheck")
        except AiNotConfigured as exc:
            raise AgentDisabled(f"{exc}; nothing was started") from None


def save(company_id: str, disabled: list[str], expected_revision: Optional[int], actor: str) -> StoredSetting:
    unknown = sorted(set(disabled) - set(AGENT_LABELS))
    if unknown:
        raise ValueError(f"unknown agent(s): {', '.join(unknown)}")
    return _service.put(company_id, KEY, {"disabled": sorted(set(disabled))}, expected_revision, actor)
