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

    # The repo root that orchestration_driver.py passes as the Claude
    # Agent SDK session's cwd, so it discovers the EXISTING
    # .claude/agents/*.md subagents and .mcp.json -- same directory a
    # human running `claude` from the repo root would use.
    repo_root: str = os.environ.get("JDE_API_REPO_ROOT", _REPO_ROOT_DEFAULT)

    # Jira demo mode. Default false: real mode, where each company's
    # connector is live only with a readable credential and a complete
    # configuration (Admin > Integrations > Jira), and otherwise reports
    # itself unavailable and blocks every Jira operation -- it never
    # falls back to the simulated gateway. Set JDE_JIRA_MOCK_MODE=true
    # only for an explicit demo or test deployment, where every company
    # uses the simulated Jira (registry.jira_mode).
    jira_mock_mode: bool = _env_bool("JDE_JIRA_MOCK_MODE", default=False)

    # The session cookie's Secure attribute -- browsers refuse to send
    # a Secure cookie over plain http, so this must be off for local
    # http dev and on for anything reachable over https. Default: on,
    # since the only case that needs it off is local development, which
    # opts out explicitly.
    cookie_secure: bool = _env_bool("JDE_COOKIE_SECURE", default=True)

    # "lax" works for same-origin/local dev. A deployment where the
    # frontend and this API are on DIFFERENT domains (e.g. a GitHub
    # Pages frontend calling a separately hosted backend) needs "none"
    # here -- browsers require Secure whenever SameSite=None, so that
    # combination also needs cookie_secure=True (the default already).
    cookie_samesite: str = os.environ.get("JDE_COOKIE_SAMESITE", "lax")

    # Unset (the default) makes it a host-only cookie -- fine for local
    # dev and for a same-host deployment. Set this when the frontend and
    # this API are on DIFFERENT subdomains of the SAME registrable
    # domain (e.g. api.consultiq.nl serving jade.consultiq.nl) --
    # ".consultiq.nl" makes the cookie valid on both, and lets
    # cookie_samesite stay "lax" (subdomains of the same registrable
    # domain are "same-site" to each other per the SameSite spec, so
    # this is both simpler AND safer than the cross-site "none" case).
    cookie_domain: str = os.environ.get("JDE_COOKIE_DOMAIN", "")

    # Controlled first-Admin creation (dependencies.py/services/
    # bootstrap_service.py): idempotent, applied on every startup, and
    # ONLY takes effect when both of these are set -- there is no public
    # admin-registration endpoint anywhere in this service. Intended for
    # local/dev use and the very first deployment; leave unset once a
    # real Admin already exists (services/bootstrap_service.py skips
    # entirely once its email is already registered).
    bootstrap_admin_email: str = os.environ.get("JDE_BOOTSTRAP_ADMIN_EMAIL", "")
    bootstrap_admin_password: str = os.environ.get("JDE_BOOTSTRAP_ADMIN_PASSWORD", "")
    bootstrap_admin_name: str = os.environ.get("JDE_BOOTSTRAP_ADMIN_NAME", "Admin")
    # Comma-separated company ids the bootstrap Admin is a member of
    # (all roles). Empty means every seeded company.
    bootstrap_admin_companies: list[str] = field(
        default_factory=lambda: _env_list("JDE_BOOTSTRAP_ADMIN_COMPANIES", "")
    )


settings = Settings()
