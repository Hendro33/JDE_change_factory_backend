"""
Settings for the API service, and the bootstrap that makes the
sibling MCP server package importable.

This service deliberately does NOT vendor or reimplement backlog.py /
approval.py / evidence.py -- it imports jde_mcp_server directly (same
package the Claude Code agents use) and only reads from it, the same
way review_ui.py already does. That import has to work whether or not
jde_mcp_server has been `pip install -e`'d, so we fall back to putting
../mcp_server on sys.path, exactly like backlog_review.py and
review_ui.py already do at the repo root.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MCP_SERVER_SRC = os.path.normpath(os.path.join(_THIS_DIR, "..", "..", "mcp_server"))
_REPO_ROOT_DEFAULT = os.path.normpath(os.path.join(_THIS_DIR, "..", ".."))

if _MCP_SERVER_SRC not in sys.path:
    try:
        import jde_mcp_server  # noqa: F401
    except ImportError:
        sys.path.insert(0, _MCP_SERVER_SRC)


def _env_list(name: str, default: str) -> list[str]:
    val = os.environ.get(name, default)
    return [v.strip() for v in val.split(",") if v.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # NOT frozen: tests point data_dir at a temp directory via
    # monkeypatch.setattr(settings, "data_dir", ...) rather than
    # juggling environment variables per test.
    # CORS: the Vite dev server's default origin, plus anything explicitly configured.
    allowed_origins: list[str] = field(
        default_factory=lambda: _env_list(
            "JDE_API_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        )
    )

    # Where this service keeps its OWN data -- change requests and the
    # customer-link sidecar. Deliberately separate from mcp_server's
    # backlog/changes/evidence directories, which stay entirely owned
    # and controlled by that package.
    data_dir: str = os.environ.get("JDE_API_DATA_DIR", "./api_data")

    # The header that stands in for real authentication in this phase
    # (Section 15.10 NFR: identity/authorisation is a documented
    # target-architecture requirement, not built yet). Whatever this
    # header names, the ENTITLEMENT LIST is always resolved server-side
    # from customer_service's identity table -- this header selects an
    # identity, it never supplies permissions directly.
    demo_identity_header: str = "X-Demo-User-Id"
    demo_customer_header: str = "X-Customer-Id"
    default_identity_id: str = os.environ.get("JDE_API_DEFAULT_IDENTITY", "u-hendro")

    # The repo root that orchestration_driver.py passes as the Claude
    # Agent SDK session's cwd, so it discovers the EXISTING
    # .claude/agents/*.md subagents and .mcp.json -- same directory a
    # human running `claude` from the repo root would use.
    repo_root: str = os.environ.get("JDE_API_REPO_ROOT", _REPO_ROOT_DEFAULT)

    # Jira Service Management hand-off connector (jira_gateway.py /
    # jira_sync_service.py). The CREDENTIAL is deployment-level, exactly
    # like JDE_AIS_USERNAME/PASSWORD already are -- one Jira service
    # account for the whole deployment today, not yet per-customer (see
    # models/jira_integration.py's own docstring for that explicit
    # limitation). Everything else about the connection (site URL,
    # project key, status names, field ids) is customer-scoped
    # configuration, stored via JiraIntegrationService, not here.
    #
    # Mirrors JDE_MCP_MOCK_MODE: defaults on, so the connector is fully
    # exercisable (Admin UI, sync, tests) against JiraMockGateway before
    # any real Jira credential exists.
    jira_mock_mode: bool = _env_bool("JDE_JIRA_MOCK_MODE", default=True)
    jira_email: str = os.environ.get("JIRA_EMAIL", "")
    jira_api_token: str = os.environ.get("JIRA_API_TOKEN", "")

    def require_jira_live_config(self) -> None:
        """Same shape as mcp_server's Settings.require_live_config --
        fail clearly here rather than deep inside an httpx call."""
        missing = [name for name, val in [("JIRA_EMAIL", self.jira_email), ("JIRA_API_TOKEN", self.jira_api_token)] if not val]
        if missing:
            raise RuntimeError(
                "JDE_JIRA_MOCK_MODE is false but these Jira credential variables are not set: "
                f"{', '.join(missing)}. Either set them, or leave mock mode on."
            )


settings = Settings()
