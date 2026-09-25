"""
The AIS server certificate (or its CA) an Admin uploads in Admin >
Integrations > JDE connection. Stored per company; the profile names the one
it uses by sha256. It only adds trust for that connection: certificate and
host-name/IP checks are never switched off, and a private key is refused.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection

MAX_PEM_BYTES = 64 * 1024
MAX_CERTIFICATES = 10


class InvalidCertificate(ValueError):
    pass


def _summarise(pem: str) -> dict[str, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtensionOID

    try:
        certs = x509.load_pem_x509_certificates(pem.encode())
    except ValueError as exc:
        raise InvalidCertificate("this is not a PEM certificate (-----BEGIN CERTIFICATE-----)") from exc
    if not certs:
        raise InvalidCertificate("no certificate found")
    if len(certs) > MAX_CERTIFICATES:
        raise InvalidCertificate(f"at most {MAX_CERTIFICATES} certificates")
    out = []
    for c in certs:
        try:
            san = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
            names = [str(n) for n in san.get_values_for_type(x509.DNSName)] + \
                    [str(n) for n in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            names = []
        try:
            is_ca = bool(c.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca)
        except x509.ExtensionNotFound:
            is_ca = False
        not_after = getattr(c, "not_valid_after_utc", None) or c.not_valid_after.replace(tzinfo=timezone.utc)
        out.append({
            "subject": c.subject.rfc4514_string(), "issuer": c.issuer.rfc4514_string(), "names": names,
            "not_after": not_after.isoformat(), "is_ca": is_ca,
            "fingerprint_sha256": c.fingerprint(hashes.SHA256()).hex(),
        })
    return {"certificates": out}


def validate(pem: str) -> tuple[str, str, dict[str, Any]]:
    """(normalised pem, sha256, summary) or InvalidCertificate."""
    pem = (pem or "").strip()
    if not pem:
        raise InvalidCertificate("no certificate was provided")
    if len(pem.encode()) > MAX_PEM_BYTES:
        raise InvalidCertificate(f"the file is larger than {MAX_PEM_BYTES // 1024} KB")
    if "PRIVATE KEY" in pem:
        raise InvalidCertificate("this file contains a private key; upload only the certificate, never a key")
    summary = _summarise(pem)
    try:
        ssl.create_default_context(cadata=pem + "\n")
    except (ssl.SSLError, ValueError) as exc:
        raise InvalidCertificate(f"the certificate cannot be used for verification ({type(exc).__name__})") from exc
    pem = pem + "\n"
    return pem, hashlib.sha256(pem.encode()).hexdigest(), summary


def store(company_id: str, pem: str, actor: str) -> dict[str, Any]:
    pem, sha, summary = validate(pem)
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jde_ca_certificates (company_id, sha256, pem, summary, uploaded_by, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (company_id, sha, pem, json.dumps(summary), actor, now))
    return get_summary(company_id, sha)  # type: ignore[return-value]


def get(company_id: str, sha: str) -> Optional[dict[str, Any]]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM jde_ca_certificates WHERE company_id = ? AND sha256 = ?",
                           (company_id, sha)).fetchone()
    return dict(row) if row else None


def get_summary(company_id: str, sha: str) -> Optional[dict[str, Any]]:
    row = get(company_id, sha)
    if row is None:
        return None
    return {"sha256": row["sha256"], "uploadedBy": row["uploaded_by"], "uploadedAt": row["uploaded_at"],
            **json.loads(row["summary"])}


def covers_host(summary: Optional[dict[str, Any]], host: str) -> bool:
    """Does some certificate in the upload name this host or IP? (A CA
    certificate need not; the server's own certificate must.)"""
    if not summary or not host:
        return False
    try:
        ip = str(ipaddress.ip_address(host))
    except ValueError:
        ip = None
    for c in summary.get("certificates", []):
        for n in c.get("names", []):
            if n.lower() == host.lower() or (ip and n == ip):
                return True
            if n.startswith("*.") and host.lower().endswith(n[1:].lower()) and host.count(".") == n.count("."):
                return True
    return False
