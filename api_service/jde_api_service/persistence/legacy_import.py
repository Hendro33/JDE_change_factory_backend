"""
One-time import of records an earlier Jade kept as JSON files, one file
per record in a directory per kind, into the database's document table
(jde_mcp_server.docstore). Runs at every start-up and is idempotent: a
directory already imported is recorded and skipped; a record already in
the database is never overwritten. The files are left where they are.
"""

from __future__ import annotations

import logging
import os

from jde_mcp_server import approval, backlog, config as mcp_config, docstore

from ..config import settings

logger = logging.getLogger(__name__)

# Kinds the API keeps (the directory under the data directory is the kind).
API_KINDS = (
    "change_requests", "customer_links", "enhancement_runs", "business_domains", "domain_reviews",
    "delivery_queue", "architecture_reviews", "engagement_scope", "decision_feedback", "agent_runs",
)


def sources() -> list[tuple[str, str]]:
    out = [(kind, os.path.join(settings.data_dir, kind)) for kind in API_KINDS]
    out.append(("design_baselines", os.environ.get("JDE_DESIGN_BASELINE_DIR")
                or os.path.join(settings.data_dir, "design_baselines")))
    out.append((backlog.KIND, os.environ.get("JDE_BACKLOG_DIR") or backlog.BACKLOG_DIR))
    out.append((approval.KIND, os.environ.get("JDE_CHANGE_DIR") or approval.CHANGE_DIR))
    out.append(("evidence", os.environ.get("JDE_EVIDENCE_DIR") or mcp_config.settings.evidence_dir))
    return out


def import_legacy_files() -> dict[str, int]:
    imported: dict[str, int] = {}
    for kind, directory in sources():
        count = docstore.import_directory(kind, directory)
        if count:
            imported[kind] = imported.get(kind, 0) + count
    if imported:
        logger.info("Imported records from an earlier file-based installation: %s", imported)
    return imported
