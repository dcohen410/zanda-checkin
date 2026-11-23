"""Passwords, device tokens, API-key encryption and simple rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken

# scrypt parameters (OWASP-recommended minimums: N=2^17, r=8, p=1). Each run uses ~128 MB,
# so at most KDF_SLOTS run at once: a flood of sign-in attempts queues instead of exhausting memory.
_N, _R, _P = 2 ** 17, 8, 1
KDF_SLOTS = threading.BoundedSemaphore(4)
MIN_PASSWORD = 12


def scrypt(password: bytes, salt: bytes, n: int, r: int, p: int, dklen: int = 64) -> bytes:
    with KDF_SLOTS:
        return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=dklen)


def password_problem(password: str, *context: str) -> str | None:
    """None if the password is strong enough, otherwise a plain-English reason.

    Passwords protect each practice's master key, and someone holding the database could try
    to guess them offline, so we need genuinely hard-to-guess ones, not just long ones.
    """
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. A few unrelated words works well."
    if len(password) > 1024:
        return "That password is too long."
    from zxcvbn import zxcvbn
    result = zxcvbn(password[:100], user_inputs=[c for c in context if c])
    if result["score"] < 3:
        hint = (result.get("feedback") or {}).get("warning") or "It would be too easy to guess."
        return f"Choose a harder-to-guess password. {hint}".strip()
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = scrypt(password.encode(), salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        check = scrypt(password.encode(), bytes.fromhex(salt), int(n), int(r), int(p))
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


# A real hash to compare against when the email doesn't exist, so timing doesn't reveal accounts.
_DUMMY_HASH = None


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("not-a-real-password")
    verify_password(password, _DUMMY_HASH)


def new_device_token() -> tuple[str, str]:
    """Returns (token to give the device, hash to store)."""
    token = "zc_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class KeyBox:
    """Encrypts Zanda API keys at rest (Fernet: AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, key: str):
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored API key can't be decrypted (was ENCRYPTION_KEY changed?)") from exc


class RateLimiter:
    """In-memory sliding window: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            self._hits[key] = hits
            return len(hits) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
