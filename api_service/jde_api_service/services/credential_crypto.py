"""
Encryption of stored third-party credentials (today: Jira API tokens).

The key is NEVER stored with the data. It comes only from the server's
environment (JDE_CREDENTIAL_KEY, set as a secret on the host), so the
SQLite file and every backup of it hold ciphertext that is useless
without the key. Consequences, stated plainly:

  * Losing the key loses the stored tokens (nothing else). Recovery is
    to set a new key and have an Admin re-enter each company's token.
  * Restoring a backup needs the key that was current when the backup
    was taken (or keep it available as JDE_CREDENTIAL_KEY_PREVIOUS).
  * Rotation: set the new key as JDE_CREDENTIAL_KEY and the old one as
    JDE_CREDENTIAL_KEY_PREVIOUS; on the next start every stored token is
    re-encrypted under the new key, after which the old key can go.

Without a key, saving a credential is refused (fail closed) rather than
falling back to plaintext. Tokens saved in plaintext before this existed
are still read, reported as "plaintext (legacy)", and encrypted on the
next start once a key is set.

Format: "enc:v1:<key id>:<Fernet token>". Fernet is AES-128-CBC with an
HMAC-SHA256 tag, from the maintained `cryptography` package -- no
hand-rolled cryptography. The key id is the first 8 hex characters of
the key's SHA-256, used only to report which key a value needs.

Generate a key:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

KEY_ENV = "JDE_CREDENTIAL_KEY"
PREVIOUS_KEY_ENV = "JDE_CREDENTIAL_KEY_PREVIOUS"
PREFIX = "enc:v1:"


class CredentialKeyMissing(RuntimeError):
    """No usable JDE_CREDENTIAL_KEY on this server."""


class CredentialUnreadable(RuntimeError):
    """A stored value cannot be decrypted with the keys available."""


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _load_keys() -> list[bytes]:
    keys = []
    for name in (KEY_ENV, PREVIOUS_KEY_ENV):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            Fernet(raw.encode())
        except (ValueError, TypeError) as exc:
            raise CredentialKeyMissing(f"{name} is set but is not a valid key ({exc}); see credential_crypto.py") from exc
        keys.append(raw.encode())
    return keys


def is_configured() -> bool:
    try:
        return bool((os.environ.get(KEY_ENV) or "").strip()) and bool(_load_keys())
    except CredentialKeyMissing:
        return False


def current_key_id() -> Optional[str]:
    raw = (os.environ.get(KEY_ENV) or "").strip()
    return _key_id(raw.encode()) if raw else None


def is_encrypted(stored: str) -> bool:
    return stored.startswith(PREFIX)


def stored_key_id(stored: str) -> Optional[str]:
    return stored[len(PREFIX):].split(":", 1)[0] if is_encrypted(stored) else None


def encrypt(plaintext: str) -> str:
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        raise CredentialKeyMissing(
            f"saving credentials is disabled: {KEY_ENV} is not configured on this server, and credentials "
            "are never stored unencrypted"
        )
    key = raw.encode()
    _load_keys()  # validates both variables
    return f"{PREFIX}{_key_id(key)}:{Fernet(key).encrypt(plaintext.encode()).decode()}"


def decrypt(stored: str) -> str:
    """Plaintext for an encrypted value; a legacy plaintext value is
    returned as it is (the caller reports it as legacy)."""
    if not is_encrypted(stored):
        return stored
    key_id, token = stored[len(PREFIX):].split(":", 1)
    keys = _load_keys()
    if not keys:
        raise CredentialUnreadable(f"this credential is encrypted (key {key_id}) but no {KEY_ENV} is configured")
    try:
        return MultiFernet([Fernet(k) for k in keys]).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialUnreadable(
            f"this credential was encrypted with key {key_id}, which is not available on this server -- "
            f"restore that key (as {KEY_ENV} or {PREVIOUS_KEY_ENV}) or re-enter the credential"
        ) from exc


def needs_reencryption(stored: str) -> bool:
    """True for legacy plaintext, or a value under a key other than the current one."""
    current = current_key_id()
    if current is None:
        return False
    return (not is_encrypted(stored)) or stored_key_id(stored) != current
