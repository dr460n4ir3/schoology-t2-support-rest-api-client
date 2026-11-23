"""Encrypted files in the app folder (~/SchoologyAPIClient): environments, recent lookups and Bulk Runner results.

The files are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package). The key is
made once per computer and kept in the Mac Keychain / Windows Credential Manager (see secret_store.py), so the
files can only be read by this user on this computer. Copied somewhere else, they are unreadable.

Files written by older versions are plain text; read() accepts both, and they are re-written encrypted the next
time they are saved (environments are converted when the app loads them).

When no password store is available (rare: a Linux box without a keyring, or RESTCLIENT_KEYCHAIN=0 in tests)
files are written as plain text, as before.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from . import secret_store

log = logging.getLogger(__name__)

MAGIC = b"RESTCLIENT-ENCRYPTED-1\n"  # first line of every encrypted file
KEY_ACCOUNT = "file-encryption-key"
_lock = threading.Lock()
_fernet = None


class VaultError(RuntimeError):
    pass


def available() -> bool:
    return secret_store.available()


def _cipher():
    """The Fernet cipher, creating and storing this computer's key the first time."""
    global _fernet
    with _lock:
        if _fernet is None:
            from cryptography.fernet import Fernet

            key = secret_store.get(KEY_ACCOUNT)
            if not key:
                key = Fernet.generate_key().decode()
                secret_store.put(KEY_ACCOUNT, key)
            _fernet = Fernet(key.encode())
        return _fernet


def reset_cache() -> None:
    global _fernet
    with _lock:
        _fernet = None


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt(text: str) -> bytes:
    return MAGIC + _cipher().encrypt(text.encode("utf-8"))


def decrypt(data: bytes) -> str:
    """Encrypted or plain (older) file contents -> text."""
    if not is_encrypted(data):
        return data.decode("utf-8-sig")
    from cryptography.fernet import InvalidToken

    try:
        return _cipher().decrypt(data[len(MAGIC):].strip()).decode("utf-8")
    except InvalidToken as exc:
        raise VaultError("This file was encrypted on another computer or by another user, so it can't be "
                         "opened here.") from exc
    except secret_store.SecretStoreError as exc:
        raise VaultError(str(exc)) from exc


def read_text(path: Path) -> str:
    return decrypt(Path(path).read_bytes())


def write_text(path: Path, text: str, encrypt_file: bool = True) -> bool:
    """Write the file encrypted (plain text only when there is no password store, or encrypt_file=False: the
    "Keep keys in the keychain" setting is off). Atomic. Returns True when the file was encrypted."""
    path = Path(path)
    data, encrypted = text.encode("utf-8"), False
    if encrypt_file and available():
        try:
            data, encrypted = encrypt(text), True
        except secret_store.SecretStoreError as exc:
            log.warning("Could not encrypt %s (%s); writing it as plain text", path.name, exc)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return encrypted


def encrypt_in_place(path: Path) -> bool:
    """Convert a plain-text file from an older version. Returns True when it was converted."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if is_encrypted(data) or not available():
        return False
    write_text(path, data.decode("utf-8-sig"))
    return True
