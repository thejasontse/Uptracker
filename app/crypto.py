"""Credential encryption at rest.

A Fernet key is generated on first run and stored in ``data/secret.key`` with
0600 permissions. Secrets are encrypted before being written to SQLite and are
never returned to the API in plaintext — the API only ever exposes whether a
credential exists and (optionally) its username.

The credential guard (see api.py) requires the operator to re-enter a secret
whenever a dataset's retrieval script/method/credential changes, so a stolen
or leaked session cannot silently repurpose stored credentials.
"""
from __future__ import annotations

import base64
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from . import db


def _load_or_create_key() -> bytes:
    db.ensure_dirs()
    if db.KEY_PATH.exists():
        key = db.KEY_PATH.read_bytes().strip()
        if key:
            return key
    # 32 random bytes, base64-encoded — the Fernet key format.
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    fd = os.open(db.KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class SecretBox:
    """Encrypt/decrypt secret strings with the on-disk keyfile."""

    def __init__(self) -> None:
        self._fernet = Fernet(_load_or_create_key())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - corrupted keyfile
            raise ValueError("Credential could not be decrypted (keyfile mismatch?)") from exc


_box: SecretBox | None = None


def secret_box() -> SecretBox:
    global _box
    if _box is None:
        _box = SecretBox()
    return _box
