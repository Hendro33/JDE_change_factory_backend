"""
Change Sets -- design update Section 4. NOT IMPLEMENTED.

A Change Set is several independently-committed operations that only
together produce the intended outcome (e.g. create a document type,
create its line type, then establish activity rules). Section 4 is
explicit that a Change Set must NOT be presented as an atomic
transaction: an orchestration may commit earlier steps before a later
one fails, so it needs its own manifest, checkpoint evidence, and a
governed compensation/recovery procedure -- none of which exists here
yet.

This module exists so that limitation is a real, callable thing that
fails closed, not a paragraph an agent might talk itself past. Nothing
in mcp_server/jde_mcp_server/server.py exposes a Change Set tool to any
agent -- there is deliberately no MCP surface for this at all yet.
Multi-step outcomes are handled today the only way this codebase
actually supports: propose_change / approve_change /
require_exact_change, called once per operation, in sequence, each a
fully independent exact-change approval (design update Section 5.2's
"Implement single-operation execution first").
"""

from __future__ import annotations


class ChangeSetNotSupportedError(RuntimeError):
    """Raised unconditionally by propose_change_set below. Distinct from
    every other *Error in this package -- this one never depends on
    approval, scope, or capability status, because there is no
    execution path here to gate at all."""


def propose_change_set(*args, **kwargs) -> dict:
    raise ChangeSetNotSupportedError(
        "Change Set execution is not implemented (design update Section 4). "
        "A Change Set's outcome depends on multiple independently-committed "
        "operations, and this codebase does not yet provide atomic "
        "multi-operation commit, checkpoint evidence, or governed "
        "compensation/recovery -- presenting sequential writes as atomic "
        "would misrepresent what actually happens if a later step fails "
        "after earlier ones already committed. Propose and get approval for "
        "each operation individually via approval.propose_change instead, "
        "and treat each one as a fully independent exact-change approval."
    )
