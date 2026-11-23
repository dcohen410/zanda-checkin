"""Zero-access key hierarchy: the service operator can't decrypt practices' Zanda API keys at rest.

    Zanda API key ──AES-256-GCM──▶ encrypted with the practice's Organisation Master Key (OMK)
    OMK ──wrapped separately for each holder──▶
        • each staff account:   key derived from their password (scrypt, own salt)
        • each check-in iPad:   key derived from its device token (HKDF; the server keeps only a hash)
        • each signed-in admin: key derived from a random secret held only in that browser's cookie

Every wrapped blob is also sealed with the server's ENCRYPTION_KEY ("pepper"), so a stolen
database alone is useless. But the pepper alone opens nothing either: there's no copy of the
OMK, any password or any device token on the server. So the database, backups and server
settings together are not enough to recover a key.

The limit, stated plainly: while a practice's device is actually using the service, the server
has to unwrap the key in memory to call Zanda (Zanda doesn't allow calls straight from a web
page). Someone able to change the code running on the server could capture keys at that
moment. See SECURITY.md for what protects against that.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .security import KeyBox, scrypt

# scrypt cost for password-derived keys (same order as password hashing).
KDF_N, KDF_R, KDF_P = 2 ** 17, 8, 1


def new_omk() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def new_salt() -> str:
    return secrets.token_hex(16)


def kek_from_password(password: str, salt_hex: str) -> bytes:
    return scrypt(password.encode(), b"zc-kek:" + bytes.fromhex(salt_hex), KDF_N, KDF_R, KDF_P, dklen=32)


def kek_from_secret(secret: str, purpose: str) -> bytes:
    """For high-entropy secrets (device tokens, session secrets): HKDF is enough."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=f"zanda-checkin:{purpose}".encode()).derive(secret.encode())


def _seal(key: bytes, plaintext: bytes, aad: str) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext, aad.encode())).decode()


def _open(key: bytes, blob: str, aad: str) -> bytes:
    raw = base64.b64decode(blob)
    return AESGCM(key).decrypt(raw[:12], raw[12:], aad.encode())


class Keyring:
    def __init__(self, pepper: KeyBox):
        self.pepper = pepper

    # OMK wrapping ------------------------------------------------------------
    def wrap_omk(self, omk: bytes, kek: bytes, org_id: int, holder: str) -> str:
        return self.pepper.encrypt(_seal(kek, omk, f"omk:{org_id}:{holder}"))

    def unwrap_omk(self, wrapped: str, kek: bytes, org_id: int, holder: str) -> bytes:
        try:
            return _open(kek, self.pepper.decrypt(wrapped), f"omk:{org_id}:{holder}")
        except Exception as exc:  # wrong key, tampered blob or wrong holder
            raise ValueError("Key can't be unlocked") from exc

    # Zanda API keys ---------------------------------------------------------
    def encrypt_api_key(self, omk: bytes, api_key: str, org_id: int, site_id: int) -> str:
        return self.pepper.encrypt(_seal(omk, api_key.encode(), f"zanda:{org_id}:{site_id}"))

    def decrypt_api_key(self, omk: bytes, blob: str, org_id: int, site_id: int) -> str:
        try:
            return _open(omk, self.pepper.decrypt(blob), f"zanda:{org_id}:{site_id}").decode()
        except Exception as exc:
            raise ValueError("API key can't be decrypted") from exc
