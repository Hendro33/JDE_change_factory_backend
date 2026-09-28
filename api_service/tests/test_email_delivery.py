"""
Invitations and password resets are e-mailed through SMTP when the server
has a mail server configured (services/email_service.py), and every caller
says truthfully what happened. The mail server itself is replaced at the
SMTP boundary by a recording double; everything above it is real.
"""

from __future__ import annotations

import smtplib

import pytest

from .conftest import headers

SENT: list = []


class _RecordingSmtp:
    """Stands in for the mail server connection (smtplib.SMTP)."""

    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.started_tls, self.login_as = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.login_as = user

    def send_message(self, msg):
        if _RecordingSmtp.fail:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        SENT.append({"to": msg["To"], "from": msg["From"], "subject": msg["Subject"], "body": msg.get_content(),
                     "tls": self.started_tls, "login": self.login_as, "server": (self.host, self.port)})


@pytest.fixture()
def smtp(monkeypatch):
    SENT.clear()
    _RecordingSmtp.fail = False
    monkeypatch.setattr(smtplib, "SMTP", _RecordingSmtp)
    for k, v in {"JDE_SMTP_HOST": "smtp.azurecomm.net", "JDE_SMTP_USERNAME": "jade-sender",
                 "JDE_SMTP_PASSWORD": "not-a-real-password", "JDE_MAIL_FROM": "jade@consultiq.example",
                 "JDE_PUBLIC_URL": "https://jade.consultiq.example"}.items():
        monkeypatch.setenv(k, v)
    return SENT


def _invite(client, email="new.person@customer.example"):
    return client.post("/admin/users/invite", headers=headers("vdb"),
                       json={"email": email, "roles": ["domain_owner"], "domainIds": []})


def test_an_invitation_is_emailed_over_starttls_and_the_link_is_not_shown(client, smtp):
    r = _invite(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["emailSent"] is True and body["previewUrl"] is None
    assert len(smtp) == 1
    mail = smtp[0]
    assert mail["to"] == "new.person@customer.example" and mail["from"] == "jade@consultiq.example"
    assert mail["tls"] is True and mail["login"] == "jade-sender" and mail["server"] == ("smtp.azurecomm.net", 587)
    assert "https://jade.consultiq.example?acceptInvitation=" in mail["body"]


def test_a_failed_delivery_is_reported_and_the_admin_gets_the_link_to_hand_over(client, smtp):
    _RecordingSmtp.fail = True
    body = _invite(client, "unknown@customer.example").json()
    assert body["emailSent"] is False and "could not be sent" in body["emailDetail"]
    assert "acceptInvitation=" in body["previewUrl"]


def test_without_a_mail_server_nothing_claims_to_be_sent(client, monkeypatch):
    monkeypatch.delenv("JDE_SMTP_HOST", raising=False)
    body = _invite(client).json()
    assert body["emailSent"] is False and "not set up" in body["emailDetail"] and body["previewUrl"]
    members = client.get("/admin/users", headers=headers("vdb")).json()["members"]
    ellen = next(m for m in members if m["email"] == "ellen@test.local")
    r = client.post(f"/admin/users/{ellen['membershipId']}/password-reset-link", headers=headers("vdb"))
    assert r.status_code == 200 and r.json()["sent"] is False and "resetToken=" in r.json()["previewUrl"]
    r = client.post("/auth/forgot-password", json={"email": "ellen@test.local"})
    assert r.json() == {"ok": True, "previewUrl": None, "emailDelivery": False}


def test_self_service_reset_is_emailed_without_revealing_whether_the_address_exists(client, smtp):
    known = client.post("/auth/forgot-password", json={"email": "ellen@test.local"}).json()
    unknown = client.post("/auth/forgot-password", json={"email": "nobody@customer.example"}).json()
    assert known == unknown == {"ok": True, "previewUrl": None, "emailDelivery": True}
    assert [m["to"] for m in smtp] == ["ellen@test.local"]
    assert "https://jade.consultiq.example?resetToken=" in smtp[0]["body"]


def test_an_admin_issued_reset_is_emailed_when_possible(client, smtp):
    members = client.get("/admin/users", headers=headers("vdb")).json()["members"]
    ellen = next(m for m in members if m["email"] == "ellen@test.local")
    r = client.post(f"/admin/users/{ellen['membershipId']}/password-reset-link", headers=headers("vdb"))
    assert r.json()["sent"] is True and r.json()["previewUrl"] is None
    assert smtp[-1]["to"] == "ellen@test.local"
