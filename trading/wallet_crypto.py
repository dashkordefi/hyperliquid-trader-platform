"""Шифрование ключа торговли на стороне сервера (Fernet, ключ из SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_trading_key(plain: str) -> str:
    return _fernet().encrypt(plain.strip().encode()).decode()


def decrypt_trading_key(blob: str) -> str:
    if not blob:
        return ""
    return _fernet().decrypt(blob.encode()).decode()
