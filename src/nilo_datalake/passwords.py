"""Password hashing for the console account.

The settings file stores a scrypt hash and a random session secret. The
plaintext password is not written to disk.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_N = 2**14
_R = 8
_P = 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=32,
        )
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    if len(digest) != len(expected):
        return False
    return hmac.compare_digest(digest, expected)


def new_session_secret() -> str:
    return secrets.token_hex(32)
