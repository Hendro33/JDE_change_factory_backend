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
    off = [a for a in agent_names if a in disabled_agents(company_id)]
    if off:
        names = ", ".join(AGENT_LABELS.get(a, a) for a in off)
        raise AgentDisabled(f"{names} {'is' if len(off) == 1 else 'are'} switched off for this customer "
                            "(Admin > Agents); nothing was started")


def save(company_id: str, disabled: list[str], expected_revision: Optional[int], actor: str) -> StoredSetting:
    unknown = sorted(set(disabled) - set(AGENT_LABELS))
    if unknown:
        raise ValueError(f"unknown agent(s): {', '.join(unknown)}")
    return _service.put(company_id, KEY, {"disabled": sorted(set(disabled))}, expected_revision, actor)
