"""
Wire models for company-level settings (services/company_settings_service.py).
"""

from __future__ import annotations

from typing import Optional

from pydantic import model_validator

from .base import ApiModel

DEFAULT_WARN_AT = 10
DEFAULT_CRITICAL_AT = 25


class DashboardThresholds(ApiModel):
    """KPI alert colours on the dashboard, shared by every user of the
    company. configured=False means the defaults are in use and nothing
    has been saved yet (revision 0)."""

    warn_at: int = DEFAULT_WARN_AT
    critical_at: int = DEFAULT_CRITICAL_AT
    configured: bool = False
    revision: int = 0
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class DashboardThresholdsUpdate(ApiModel):
    warn_at: int
    critical_at: int
    expected_revision: Optional[int] = None

    @model_validator(mode="after")
    def _check_order(self) -> "DashboardThresholdsUpdate":
        if self.warn_at < 0 or self.critical_at < 0:
            raise ValueError("thresholds must not be negative")
        if self.critical_at < self.warn_at:
            raise ValueError("the red (critical) threshold must be at least the orange (warning) threshold")
        return self
