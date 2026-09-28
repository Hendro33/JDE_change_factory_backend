"""
Configuration for the JDE MCP server (the agents' tool server).

The tool server holds no JD Edwards connection and no credentials: agents
research JDE only through Jade's governed discovery (api_service), and a
change reaches JDE only through the recorded delivery route, verified live
through the customer's own connection (api_service delivery/).
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Where evidence records lived as files before they moved into Jade's
    # database; read once by the legacy import, never written.
    evidence_dir: str = os.environ.get("JDE_EVIDENCE_DIR", "./evidence")


settings = Settings()
