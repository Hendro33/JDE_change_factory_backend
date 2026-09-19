from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MCP_SERVER_SRC = _HERE.parent.parent / "mcp_server"
if str(_MCP_SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER_SRC))


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets its own throwaway directories for both this
    service's own data (change requests, customer links) and
    mcp_server's stores (backlog, changes, evidence) -- no test can
    see another test's or a developer's real local data."""
    import dataclasses

    from jde_api_service.config import settings as api_settings
    from jde_mcp_server import backlog as backlog_module
    from jde_mcp_server import approval as approval_module
    from jde_mcp_server import config as mcp_config_module

    api_data_dir = tmp_path / "api_data"
    backlog_dir = tmp_path / "backlog"
    change_dir = tmp_path / "changes"
    evidence_dir = tmp_path / "evidence"

    monkeypatch.setattr(api_settings, "data_dir", str(api_data_dir))
    monkeypatch.setattr(backlog_module, "BACKLOG_DIR", str(backlog_dir))
    monkeypatch.setattr(approval_module, "CHANGE_DIR", str(change_dir))
    # mcp_server's Settings is frozen (by design, and it isn't ours to
    # modify) -- rebind the module-level `settings` name to a fresh
    # instance instead of mutating the existing one. change_service.py
    # reads `mcp_config.settings.evidence_dir` fresh on every call
    # (rather than capturing the settings object at import time), so it
    # sees this rebind.
    monkeypatch.setattr(
        mcp_config_module,
        "settings",
        dataclasses.replace(mcp_config_module.settings, evidence_dir=str(evidence_dir)),
    )

    return {
        "api_data_dir": api_data_dir,
        "backlog_dir": backlog_dir,
        "change_dir": change_dir,
        "evidence_dir": evidence_dir,
    }


@pytest.fixture()
def client(isolated_dirs):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    # `with` triggers FastAPI's startup lifecycle (pilot-dataset
    # seeding included) the same way a real `uvicorn` run does --
    # without it, tests would see different behaviour than production.
    with TestClient(app) as c:
        yield c


def headers(user: str = "u-hendro", customer: str | None = "vdb") -> dict:
    h = {"X-Demo-User-Id": user}
    if customer is not None:
        h["X-Customer-Id"] = customer
    return h
