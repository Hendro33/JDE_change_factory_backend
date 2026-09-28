from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jde_api_service.models.change import Change
from jde_api_service.services.metrics_service import MetricsService
from .conftest import headers


@pytest.mark.parametrize("period,days", [("week", 7), ("month", 30), ("year", 365)])
def test_period_boundaries_and_all_aggregates(period, days):
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    times = [now, now - timedelta(days=days), now - timedelta(days=days, seconds=1), now + timedelta(seconds=1)]
    rows = [Change(id=str(i), customer_id="bwm", title="Story", source="Business", state="RECEIVED",
                   created_at=t.isoformat(), updated_at=now.isoformat()) for i,t in enumerate(times)]
    requested = []
    def scoped(customer):
        requested.append(customer)
        return rows
    svc = MetricsService(SimpleNamespace(list_for_customer=scoped), delivered_at=lambda customer: {})
    metrics = svc.metrics_for_customer("bwm", period, now=now)
    assert set(requested) == {"bwm"}
    assert metrics.performance.change_volume == 2
    assert sum(x.count for x in metrics.change_types) == 2
    assert next(x.value for x in metrics.totals if x.key == "incoming_requests") == 2
    assert svc.metrics_for_customer("bwm").performance.change_volume == 4


def test_period_api_is_validated_and_customer_scoped(client, monkeypatch):
    from jde_api_service.services.registry import get_metrics_service
    service = get_metrics_service()
    from jde_api_service.routers import changes
    monkeypatch.setattr(changes, "get_metrics_service", lambda: service)
    original = service.metrics_for_customer
    calls = []
    def observed(customer_id, period="lifetime"):
        calls.append((customer_id, period))
        return original(customer_id, period)
    monkeypatch.setattr(service, "metrics_for_customer", observed)
    assert client.get("/metrics?period=nonsense", headers=headers()).status_code == 422
    for period in ["week", "month", "year", "lifetime"]:
        response = client.get(f"/metrics?period={period}", headers=headers(customer="bwm"))
        assert response.status_code == 200
        assert calls[-1] == ("bwm", period)
        assert response.json()["performance"]["changeVolume"] == original("bwm", period).performance.change_volume


def test_cycle_time_and_trends_come_from_recorded_timestamps():
    """Cycle time = request received -> as-built finalised, for stories
    delivered in the period; every figure's delta compares it with the
    previous period of the same length."""
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def story(i, created_days_ago):
        return Change(id=f"S{i}", customer_id="bwm", title="Story", source="Business", state="RECEIVED",
                      created_at=(now - timedelta(days=created_days_ago)).isoformat(), updated_at=now.isoformat())

    rows = [story(1, 3), story(2, 5), story(3, 10), story(4, 12), story(5, 13)]
    delivered = {"S1": now - timedelta(days=1),    # 2 days, delivered this week
                 "S3": now - timedelta(days=2),    # 8 days, delivered this week (created last week)
                 "S4": now - timedelta(days=9)}    # 3 days, delivered last week
    svc = MetricsService(SimpleNamespace(list_for_customer=lambda c: rows), delivered_at=lambda c: delivered)
    week = svc.metrics_for_customer("bwm", "week", now=now)
    assert week.performance.delivered_count == 2 and week.performance.average_cycle_time_days == 5.0
    assert week.performance.average_cycle_time_delta == 2.0          # 5.0 this week vs 3.0 last week
    assert week.performance.change_volume == 2 and week.performance.change_volume_delta == -1  # 2 vs 3 created
    incoming = next(t for t in week.totals if t.key == "incoming_requests")
    assert (incoming.value, incoming.delta) == (2, -1)
    lifetime = svc.metrics_for_customer("bwm")
    assert lifetime.performance.delivered_count == 3 and lifetime.performance.average_cycle_time_days == 4.3
