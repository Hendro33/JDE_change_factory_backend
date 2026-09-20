"""
The Architecture Review step: "how should this requirement best be
delivered in this customer's current JDE solution?"

Reuses the EXISTING architect subagent (.claude/agents/architect.md,
unmodified) and the EXISTING Claude Agent SDK integration pattern from
orchestration_driver.py / review_driver.py -- not a new agent, not a
new orchestration mechanism. Runs only against a story that has
already cleared Gate 1 (Application Manager authorised it for
delivery, which is also what clears backlog.py's own Gate 2 --
get_approved_story below refuses otherwise, exactly as designed).

The Architect calls resolve_without_change or propose_change ITSELF,
using its own real MCP tools -- this driver never calls either on the
agent's behalf. That is the actual gate (Section 3.5/15.3); this
module only captures the Architect's REASONING (ArchitectDecision +
Implementation Specification) into architecture_review_service.py,
since propose_change's own storage has no fields for it. The
authoritative proposed exact change and its approval stay exactly
where they already are, in approval.py -- this never duplicates them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..models.change import ArchitectDecision, ImplementationSpecification
from .architecture_review_service import ArchitectureReviewService
from .orchestration_driver import _coerce_enum, _extract_json

_ALLOWED_TOOLS = [
    "Task",
    "mcp__jde-change-factory__get_approved_story",
    "mcp__jde-change-factory__get_object",
    "mcp__jde-change-factory__get_version",
    "mcp__jde-change-factory__get_processing_options",
    "mcp__jde-change-factory__resolve_without_change",
    "mcp__jde-change-factory__propose_change",
]

_ROUTES = {"Functional Agent", "Technical Agent", "Mixed", "Human Implementation", "Resolve without Change"}

# Named so Admin > Agents can read the same values this driver actually
# runs with, rather than a second, independently-maintained copy.
PERMISSION_MODE = "dontAsk"
MAX_TURNS = 40

_SCHEMA_INSTRUCTIONS = """
After you have called either resolve_without_change or propose_change (exactly one of them, as your own instructions describe), respond with ONLY a single fenced json code block (nothing before or after it) with EXACTLY this shape -- no extra top-level keys, no commentary outside the block:
{
  "architect_decision": {
    "recommended_route": "Functional Agent" | "Technical Agent" | "Mixed" | "Human Implementation" | "Resolve without Change",
    "confidence": <0-1>,
    "existing_functionality_found": "<what standard functionality/configuration you found, or empty string>",
    "alternatives_considered": [{"approach": "...", "whyNot": "..."}],
    "objects_affected": ["<application/object ids actually confirmed via discovery>"],
    "dependencies_and_conflicts": ["<anything that could break, or empty if none found>"],
    "rollback_strategy": "<concrete rollback for the exact change, or 'not applicable' if no change was proposed>"
  },
  "implementation_spec": {
    "sequence": ["<ordered steps>"],
    "required_mcp_operations": ["<tool names the build step will call>"],
    "human_actions_required": ["<anything a person must do outside Jade>"],
    "validation_approach": "<how the change will be tested/validated>"
  }
}
Never report an object, version, or processing option you did not actually confirm via get_object/get_version/get_processing_options -- leave objects_affected honestly incomplete rather than guessing. If you are not confident in the recommended route, say so in existing_functionality_found or dependencies_and_conflicts rather than picking a route to fill the field.
""".strip()


def _build_prompt(story_id: str) -> str:
    return f"""Use the architect subagent to review approved story {story_id}, exactly as its own instructions describe: call get_approved_story first, work through the "why not?" sequence, use discovery tools to confirm anything you reference, and then call resolve_without_change (if existing functionality/configuration already satisfies the requirement) or propose_change (with the exact operation) -- never both, never neither.

story_id to use throughout, in every tool call: {story_id}

{_SCHEMA_INSTRUCTIONS}"""


def _alternatives_from(raw: list[dict]) -> list[dict]:
    out = []
    for a in raw or []:
        out.append({"approach": a.get("approach", ""), "whyNot": a.get("whyNot") or a.get("why_not", "")})
    return out


def _architect_decision_from_summary(raw: dict) -> ArchitectDecision:
    ad = raw.get("architect_decision") or {}
    try:
        confidence = float(ad.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return ArchitectDecision(
        recommended_route=_coerce_enum(ad.get("recommended_route"), _ROUTES, "Human Implementation"),  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, confidence)),
        existing_functionality_found=ad.get("existing_functionality_found") or "",
        alternatives_considered=_alternatives_from(ad.get("alternatives_considered") or []),
        objects_affected=list(ad.get("objects_affected") or []),
        dependencies_and_conflicts=list(ad.get("dependencies_and_conflicts") or []),
        rollback_strategy=ad.get("rollback_strategy") or "",
        decided_at=datetime.now(timezone.utc).isoformat(),
    )


def _implementation_spec_from_summary(raw: dict) -> ImplementationSpecification:
    spec = raw.get("implementation_spec") or {}
    return ImplementationSpecification(
        sequence=list(spec.get("sequence") or []),
        required_mcp_operations=list(spec.get("required_mcp_operations") or []),
        human_actions_required=list(spec.get("human_actions_required") or []),
        validation_approach=spec.get("validation_approach") or "",
    )


async def run_architecture_review(
    *, story_id: str, repo_root: str, run_service: ArchitectureReviewService, customer_id: Optional[str] = None
) -> None:
    """The whole Architecture Review step for one approved story.
    Intended to run as a background task -- never raises; all failure
    paths are recorded via run_service.fail(), same convention as
    run_enhancement (orchestration_driver.py)."""
    run_service.start(story_id)

    from .agent_registry_service import compute_agent_version
    from .registry import get_agent_run_service

    agent_run_service = get_agent_run_service()
    agent_run = agent_run_service.start(
        agent_name="architect",
        driver="architecture_driver",
        story_id=story_id,
        customer_id=customer_id,
        agent_version=compute_agent_version("architect", repo_root),
    )

    final_text: Optional[str] = None
    try:
        import claude_agent_sdk as sdk

        options = sdk.ClaudeAgentOptions(
            cwd=repo_root,
            permission_mode=PERMISSION_MODE,
            allowed_tools=_ALLOWED_TOOLS,
            max_turns=MAX_TURNS,
        )
        prompt = _build_prompt(story_id)

        async for message in sdk.query(prompt=prompt, options=options):
            if isinstance(message, sdk.ResultMessage):
                if message.is_error:
                    raise RuntimeError(f"architecture review ended in error: {getattr(message, 'result', None)}")
                final_text = getattr(message, "result", None)

        if final_text is None:
            raise RuntimeError("architecture review produced no final result")

        summary: dict[str, Any] = _extract_json(final_text)
        run_service.complete(
            story_id,
            architect_decision=_architect_decision_from_summary(summary),
            implementation_spec=_implementation_spec_from_summary(summary),
        )
        agent_run_service.complete(agent_run.run_id)
    except Exception as exc:  # noqa: BLE001 -- always recorded, never raised into the background task runner
        run_service.fail(story_id, str(exc))
        agent_run_service.fail(agent_run.run_id, str(exc))
