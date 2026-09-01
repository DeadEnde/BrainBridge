"""BrainBridge — at-rest encryption for stored user sessions.

User session states (the Google tokens) are the crown jewels. With
BRAINBRIDGE_SECRET set, every stored per-user state is encrypted with
Fernet (AES128-CBC + HMAC) before it hits KV/Blob/files; without it we
fall back to plain base64 (dev only — log a warning).

Payload format:  "enc:<fernet token>"   (auto-detected on read)
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]


def _fernet() -> "Fernet | None":
    secret = os.environ.get("BRAINBRIDGE_SECRET", "").strip()
    if not secret:
        return None
    if Fernet is None:
        raise RuntimeError("cryptography not installed (pip install cryptography)")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def secrets_enabled() -> bool:
    return bool(os.environ.get("BRAINBRIDGE_SECRET", "").strip())


def encrypt_state(data: bytes) -> str:
    """data -> stored string (encrypted when BRAINBRIDGE_SECRET is set)."""
    f = _fernet()
    if f is None:
        if os.environ.get("VERCEL"):
            print("[secret] WARNING: BRAINBRIDGE_SECRET not set — storing user "
                  "tokens WITHOUT encryption on Vercel. Set it now.",
                  file=sys.stderr)
        return base64.b64encode(data).decode()
    return "enc:" + f.encrypt(data).decode()


def decrypt_state(payload: str) -> bytes:
    """stored string -> original bytes."""
    if payload.startswith("enc:"):
        f = _fernet()
        if f is None:
            raise ValueError(
                "This record is encrypted but BRAINBRIDGE_SECRET is not set "
                "(or changed). Set the same secret in the env to decrypt it.")
        try:
            return f.decrypt(payload[4:].encode())
        except InvalidToken as e:
            raise ValueError("Decryption failed (wrong BRAINBRIDGE_SECRET?)") from e
    return base64.b64decode(payload)
