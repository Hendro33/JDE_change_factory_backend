"""
Startup recovery for agent runs that a restart interrupted.

Receive/Improve/Check and the Architect run as in-process background
tasks. If the process stops mid-run, the task is gone but its record
still says "receiving"/"analyzing"/"started" -- and the UI would show it
as running forever, while the retry endpoints refuse a run that looks
in progress. On startup nothing can still be running, so every such
record is marked failed with a plain reason, which makes it retryable.
Nothing is re-run automatically: a retry is a human decision.

Those runs never write to JDE, so retrying them is safe. A JDE write or
test run is different: if one was in flight, it may or may not have
reached JDE. Those are recorded as an UNKNOWN outcome instead
(execution.mark_interrupted_unknown), which blocks any retry until
someone reconciles it by checking the actual target state.
"""

from __future__ import annotations

from jde_mcp_server import execution

from .registry import get_agent_run_service, get_architecture_review_service, get_enhancement_run_service

INTERRUPTED = "Interrupted by a backend restart before it finished; nothing further ran. Retry it."

_ENHANCEMENT_IN_PROGRESS = {"receiving", "improving", "checking"}


def reconcile_interrupted_runs() -> dict[str, int]:
    counts = {"enhancement": 0, "architecture": 0, "agent": 0}
    counts["jde_attempts_unknown"] = execution.mark_interrupted_unknown()

    enhancement = get_enhancement_run_service()
    for run in enhancement.list_all():
        if run.stage in _ENHANCEMENT_IN_PROGRESS:
            enhancement.fail(run.request_id, INTERRUPTED)
            counts["enhancement"] += 1

    architecture = get_architecture_review_service()
    for run in architecture.list_all():
        if run.stage == "analyzing":
            architecture.fail(run.story_id, INTERRUPTED)
            counts["architecture"] += 1

    agents = get_agent_run_service()
    for run in agents.list_all():
        if run.stage == "started":
            agents.fail(run.run_id, INTERRUPTED)
            counts["agent"] += 1

    return counts
