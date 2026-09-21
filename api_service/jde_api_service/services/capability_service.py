"""
Read-only service over mcp_server's capability_catalog.py -- see
models/capability.py's own docstring for why this stays a thin wrapper
rather than a second source of truth.
"""

from __future__ import annotations

from typing import Optional

from jde_mcp_server import capability_catalog

from ..models.capability import Capability, CapabilityCatalog


class CapabilityService:
    def list_catalog(self) -> CapabilityCatalog:
        return CapabilityCatalog(
            catalog_revision=capability_catalog.catalog_revision(),
            capabilities=[Capability.model_validate(c) for c in capability_catalog.list_capabilities()],
        )

    def get(self, capability_id: str) -> Optional[Capability]:
        cap = capability_catalog.get_capability(capability_id)
        return Capability.model_validate(cap) if cap is not None else None
