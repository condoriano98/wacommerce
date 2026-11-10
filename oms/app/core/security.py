import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet

from app.core.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def issue_token(user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "iat": now,
        "exp": now + timedelta(minutes=s.jwt_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])


def _fernet() -> Fernet:
    """Encrypt with the current key; decrypt with current or previous keys (rotation, M5). Previous keys come from
    ``OMS_CREDENTIALS_PREVIOUS_KEYS`` (comma-separated) and are removed once ``reencrypt_credentials`` has run."""
    from cryptography.fernet import MultiFernet

    s = get_settings()
    keys = [s.credentials_master_key] + [k.strip() for k in s.credentials_previous_keys.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode()


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_meta_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Validate Meta's ``X-Hub-Signature-256: sha256=<hex>`` over the exact raw request bytes."""
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(header.removeprefix("sha256="), hmac_sha256_hex(app_secret, raw_body))


def sha256_normalized(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def normalize_id_phone(raw: str) -> str:
    """Normalize an Indonesian phone number to E.164 digits without '+', e.g. 0812... -> 62812..."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits
