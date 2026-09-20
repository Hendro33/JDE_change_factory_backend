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
    # jira_sync_service.py). This is the only deployment-level Jira
    # setting left, and it is now a FORCE-MOCK override only -- default
    # false, so a customer whose email + API token are configured under
    # Admin > Integrations > Jira goes live purely from that Admin
    # action, with no .env or backend file edit required
    # (registry.get_jira_gateway is what actually applies this: a
    # customer with no credentials configured still gets JiraMockGateway
    # regardless of this flag, so the connector stays fully exercisable
    # before real credentials exist). Set JDE_JIRA_MOCK_MODE=true only
    # to force the whole deployment to mock regardless of what any
    # customer has configured -- e.g. a shared demo/staging environment.
    # Credentials/config themselves are customer-scoped, entered through
    # Admin > Integrations > Jira and stored via JiraCredentialsService /
    # JiraIntegrationService -- see models/jira_integration.py's own
    # docstring for the explicit pilot/production distinction this
    # follows.
    jira_mock_mode: bool = _env_bool("JDE_JIRA_MOCK_MODE", default=False)


settings = Settings()
