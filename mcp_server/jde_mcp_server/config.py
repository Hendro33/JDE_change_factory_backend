"""
Configuration for the JDE MCP server.

Everything here is read from environment variables so the same code runs
against a real AIS Server or in MOCK_MODE with zero changes. See the
README for the full list of variables and where each one comes from.

Design reference: Section 7 (JDE MCP Layer) and Section 8.2 (Service
account scoping) of the JDE AI-Driven Change Factory design document.
"""

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # When true, every tool returns canned fixture data instead of calling
    # a real AIS Server. Use this to build and test the agents and the
    # Claude Code wiring *before* JDE environment access is in place.
    mock_mode: bool = _env_bool("JDE_MCP_MOCK_MODE", default=True)

    # AIS Server connection. Per Section 8.2, this account should be
    # scoped to the narrowest security role that covers the pilot's
    # chosen versions/applications, and to the DEV pathcode only.
    ais_base_url: str = os.environ.get("JDE_AIS_BASE_URL", "")
    ais_username: str = os.environ.get("JDE_AIS_USERNAME", "")
    ais_password: str = os.environ.get("JDE_AIS_PASSWORD", "")
    ais_environment: str = os.environ.get("JDE_AIS_ENVIRONMENT", "")
    ais_role: str = os.environ.get("JDE_AIS_ROLE", "*ALL")

    # Where evidence records (Section 6, capture_evidence) are written.
    # A directory of JSON files is enough for the pilot; swap for a real
    # datastore once volume justifies it (Section 12.1).
    evidence_dir: str = os.environ.get("JDE_EVIDENCE_DIR", "./evidence")

    def require_live_config(self) -> None:
        """Raise a clear error if someone tries to go live without the
        AIS connection details filled in, instead of failing deep inside
        an HTTP client with a confusing error."""
        missing = [
            name
            for name, val in [
                ("JDE_AIS_BASE_URL", self.ais_base_url),
                ("JDE_AIS_USERNAME", self.ais_username),
                ("JDE_AIS_PASSWORD", self.ais_password),
                ("JDE_AIS_ENVIRONMENT", self.ais_environment),
            ]
            if not val
        ]
        if missing:
            raise RuntimeError(
                "JDE_MCP_MOCK_MODE is false but these AIS connection "
                f"variables are not set: {', '.join(missing)}. "
                "Either set them, or leave MOCK_MODE on while you build "
                "the rest of the pipeline (see README, Section 10.2 steps 1-2)."
            )


settings = Settings()
