"""Symmetric encryption for user-provided secrets (SMTP password, Hunter key).

We use Fernet (AES-128-CBC + HMAC-SHA256) keyed by a single service-wide
`ENCRYPTION_KEY`. This is sufficient for the threat model: protect secrets at
rest against a database dump. It is NOT designed to defend against a full
application compromise \u2014 an attacker with code execution can read the key
from the environment.

Rotating the key invalidates every stored credential. Users must reconnect
SMTP / re-enter their Hunter key. A future migration could support multi-key
decryption (MultiFernet) for zero-downtime rotation.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


_KEY = os.environ.get("ENCRYPTION_KEY")
if not _KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY not set. Generate one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

_fernet = Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)


def encrypt(plaintext: str) -> bytes:
    """Encrypt a string. Returns ciphertext bytes safe to store in a LargeBinary column."""
    if plaintext is None:
        raise ValueError("encrypt() received None \u2014 use a nullable column instead")
    return _fernet.encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt bytes from the database. Raises InvalidToken if the key was rotated."""
    try:
        return _fernet.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise InvalidToken(
            "Credential decryption failed. ENCRYPTION_KEY may have been rotated; "
            "the user must reconnect their account."
        ) from e
