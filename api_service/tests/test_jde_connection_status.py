"""The one plain connection status the Administrator sees, derived from the
separate checks of the last Test Connection and sample read."""

from jde_api_service.discovery.models import CheckResult
from jde_api_service.discovery.profile_service import connection_status


def _c(state, detail=""):
    return CheckResult(state=state, detail=detail)


def _status(**checks):
    return connection_status({k: v for k, v in checks.items()})["state"]


def test_each_failure_is_named_for_what_failed():
    assert _status() == "not_tested"
    assert _status(reachability=_c("stale")) == "not_tested"
    assert _status(reachability=_c("failed", "TLS: the certificate presented does not match the host name 1.2.3.4")) \
        == "certificate_problem"
    assert _status(reachability=_c("failed", "timed out reaching 1.2.3.4 from the backend machine")) \
        == "network_unavailable"
    assert _status(reachability=_c("ok"), authentication=_c("failed", "AIS refused the request (HTTP 401)")) \
        == "authentication_failed"
    assert _status(reachability=_c("ok"), authentication=_c("ok"), environment=_c("failed")) == "environment_mismatch"


def test_connected_says_whether_a_read_only_request_succeeded():
    base = dict(reachability=_c("ok"), authentication=_c("ok"), environment=_c("ok"))
    assert connection_status(base)["label"] == "Connected"
    assert "sample read" in connection_status(base)["detail"]
    ok = connection_status({**base, "approved_read": _c("ok")})
    assert (ok["state"], ok["label"]) == ("connected", "Connected") and "read-only request succeeded" in ok["detail"]
    assert connection_status({**base, "approved_read": _c("failed", "x")})["label"] == "Connected, sample read failed"
