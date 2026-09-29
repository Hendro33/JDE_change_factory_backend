"""
The agents' governed hand-off tools ("jde-change-factory"): backlog hand-off,
approved story, resolve without change, exact-change proposals, capability
status, design baselines and evidence.

They run INSIDE the API process as an in-process SDK tool server, built per
agent run and bound to that run's story -- the same pattern as the discovery,
technical and process tools. Previously the agent CLI started a separate
Python process from .mcp.json; that depended on which `python3` was on the
PATH (not Jade's own environment on a Mac) and on the environment reaching
it, and was never exercised by the tests. Now the tools use exactly the
database, configuration and code the API uses.

Every tool call for another story is refused: the company is taken from the
story's own records, never from the model.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

SERVER_NAME = "jde-change-factory"


class StoryMismatch(RuntimeError):
    pass


class ProjectTools:
    def __init__(self, *, company_id: str, story_id: Optional[str]) -> None:
        self.company_id, self.story_id = company_id, story_id
        self.calls: list[str] = []

    def _bound(self, story_id: str) -> str:
        if not self.story_id:
            raise StoryMismatch("this run is not working on a story")
        if str(story_id or "").strip() != self.story_id:
            raise StoryMismatch(f"this run works on story {self.story_id} only, not {story_id!r}")
        return self.story_id

    # -- the tools ----------------------------------------------------------------
    def propose_to_backlog(self, a: dict) -> dict:
        from jde_mcp_server.backlog import propose_to_backlog

        return propose_to_backlog(self._bound(a.get("story_id")), a.get("user_story", ""), a.get("business_impact") or {},
                                  a.get("rough_complexity_signal", ""), a.get("source", ""))

    def get_approved_story(self, a: dict) -> dict:
        from jde_mcp_server.backlog import get_approved_story

        return get_approved_story(self._bound(a.get("story_id")))

    def resolve_without_change(self, a: dict) -> dict:
        from jde_mcp_server.backlog import resolve_without_change

        return resolve_without_change(self._bound(a.get("story_id")), a.get("resolution_note", ""),
                                      a.get("resolved_by", "architect"))

    def propose_change(self, a: dict) -> dict:
        from jde_mcp_server.approval import propose_change

        return propose_change(self._bound(a.get("story_id")), a.get("operation") or {}, a.get("capability_id", ""),
                              a.get("environment") or "DEV")

    def get_capability_status(self, a: dict) -> dict:
        from jde_mcp_server.capability_catalog import CapabilityError, get_capability

        cap = get_capability(a.get("capability_id", ""))
        if cap is None:
            raise CapabilityError(f"{a.get('capability_id')!r} is not a registered capability")
        return cap

    def get_engagement_scope(self, a: dict) -> dict:
        from jde_mcp_server.scope import company_for_story, describe_scope

        story = self._bound(a.get("story_id"))
        if company_for_story(story) != self.company_id:
            raise StoryMismatch("the story belongs to another customer")
        return describe_scope(self.company_id)

    def capture_evidence(self, a: dict) -> dict:
        from jde_mcp_server.evidence import capture_evidence

        return capture_evidence(self._bound(a.get("story_id")), a.get("payload") or {})

    def verify_evidence_chain(self, a: dict) -> dict:
        from jde_mcp_server.evidence import verify_chain

        return verify_chain(self._bound(a.get("story_id")))

    def get_design_baseline(self, a: dict) -> dict:
        from jde_mcp_server.design_baseline import get_design_baseline

        return get_design_baseline(self._bound(a.get("story_id")))

    def call(self, name: str, fn: Callable[[dict], Any], args: dict) -> dict:
        self.calls.append(name)
        try:
            return {"content": [{"type": "text", "text": json.dumps(fn(args or {}), default=str)}]}
        except Exception as exc:  # noqa: BLE001 -- a refusal is reported to the agent, never raised
            return {"content": [{"type": "text", "text": json.dumps({"error": str(exc), "refused": True})}],
                    "is_error": True}

    # -- the in-process server -------------------------------------------------------
    def sdk_server(self):
        import claude_agent_sdk as sdk

        s = {"story_id": {"type": "string"}}
        obj = {"type": "object"}
        specs = [
            ("propose_to_backlog",
             "Hand a story that passed EVERY quality criterion to Story Review. It does not approve it: an Application "
             "Manager places it in a business domain and its Domain Owner approves it. business_impact carries the five "
             "criteria (financial_impact, operational_reach, risk_compliance, strategic_alignment, urgency), each with "
             "the evidence it was traced from, or empty.",
             {"type": "object", "properties": {**s, "user_story": {"type": "string"}, "business_impact": obj,
                                               "rough_complexity_signal": {"type": "string"}, "source": {"type": "string"}},
              "required": ["story_id", "user_story", "business_impact", "rough_complexity_signal"]},
             self.propose_to_backlog),
            ("get_approved_story",
             "The approved story record. Refuses a story that people have not approved for delivery.",
             {"type": "object", "properties": s, "required": ["story_id"]}, self.get_approved_story),
            ("resolve_without_change",
             "End the story: existing JD Edwards functionality or configuration already satisfies it. Name exactly what "
             "does. Nothing in JD Edwards is touched.",
             {"type": "object", "properties": {**s, "resolution_note": {"type": "string"}, "resolved_by": {"type": "string"}},
              "required": ["story_id", "resolution_note"]}, self.resolve_without_change),
            ("propose_change",
             "Propose the exact configuration change for people to approve in Architecture Review. It does not approve "
             "anything; after approval the agents apply each item in DEV (a person only what JD Edwards cannot "
             "accommodate through AIS or the web client) and Jade reads every item back live.\n"
             + _format_help(),
             {"type": "object", "properties": {**s, "operation": obj, "capability_id": {"type": "string"},
                                               "environment": {"type": "string"}},
              "required": ["story_id", "operation", "capability_id"]}, self.propose_change),
            ("get_capability_status",
             "One catalogue capability: its status, what it may change, and its enforcement rules.",
             {"type": "object", "properties": {"capability_id": {"type": "string"}}, "required": ["capability_id"]},
             self.get_capability_status),
            ("get_engagement_scope",
             "What this customer allows to be proposed, read-only: the DEV environment, approved processing-option "
             "versions and values, approved configuration (UDC types, set-up tables, document and line types, order "
             "activity rules, batch versions -- with their fields, actions and allowed values), approved tests, "
             "never-touch and protected categories. Check it BEFORE proposing; the gate re-checks every item.",
             {"type": "object", "properties": s, "required": ["story_id"]}, self.get_engagement_scope),
            ("capture_evidence", "Append an immutable, hash-chained evidence record for the story.",
             {"type": "object", "properties": {**s, "payload": obj}, "required": ["story_id", "payload"]},
             self.capture_evidence),
            ("verify_evidence_chain", "Recompute the story's evidence chain and report whether it is intact.",
             {"type": "object", "properties": s, "required": ["story_id"]}, self.verify_evidence_chain),
            ("get_design_baseline",
             "The approved design for the story with the exact evidence manifest it was based on, and the exact change "
             "it proposed with its approval state.",
             {"type": "object", "properties": s, "required": ["story_id"]}, self.get_design_baseline),
        ]

        def make(name, fn):
            async def handler(args):
                return self.call(name, fn, args)
            return handler

        tools = [sdk.tool(name, desc, schema)(make(name, fn)) for name, desc, schema, fn in specs]
        return sdk.create_sdk_mcp_server(SERVER_NAME, tools=tools)


TOOL_NAMES = ["propose_to_backlog", "get_approved_story", "resolve_without_change", "propose_change",
              "get_capability_status", "get_engagement_scope", "capture_evidence", "verify_evidence_chain",
              "get_design_baseline"]


def _format_help() -> str:
    from jde_mcp_server.config_items import FORMAT_HELP

    return FORMAT_HELP
