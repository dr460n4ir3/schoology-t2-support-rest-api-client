"""Keys and secrets in the operating system's password store instead of plain-text files.

Mac: the login Keychain (Keychain Access app, entries named "Schoology API Client").
Windows: Credential Manager. Linux: the Secret Service (GNOME Keyring / KWallet) when there is one.

Environment files then keep everything except the secret values, so they are safe to back up or share.
If no password store is available, the app keeps using the files as before.
Set RESTCLIENT_KEYCHAIN=0 to turn this off (tests and scripted use).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SERVICE = "Schoology API Client"
AUTH_SECRET_FIELDS = ("token", "password", "key_value", "consumer_secret", "token_secret", "client_secret")

_available: bool | None = None


class SecretStoreError(RuntimeError):
    pass


def available() -> bool:
    """True when a real password store is usable (not keyring's 'fail' / 'null' placeholders)."""
    global _available
    if os.environ.get("RESTCLIENT_KEYCHAIN", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if _available is None:
        try:
            import keyring

            backend = keyring.get_keyring()
            name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
            _available = not any(bad in name for bad in ("fail", "null", "plaintext"))
            if hasattr(backend, "backends"):  # keyring's "chainer": usable only if it chains a real backend
                _available = _available and bool(getattr(backend, "backends"))
        except Exception as exc:  # keyring missing or broken: fall back to files
            log.info("No password store available: %s", exc)
            _available = False
    return _available


def reset_cache() -> None:
    global _available
    _available = None


def store_name() -> str:
    import sys

    return {"darwin": "Mac Keychain", "win32": "Windows Credential Manager"}.get(sys.platform, "system keyring")


def ref(env_id: str, field: str) -> str:
    """Account name for one secret, e.g. '3f2a.../var:schoology_consumer_secret'."""
    return f"{env_id}/{field}"


def get(account: str) -> str | None:
    import keyring

    try:
        return keyring.get_password(SERVICE, account)
    except Exception as exc:
        raise SecretStoreError(f"Couldn't read from the {store_name()}: {exc}") from exc


def put(account: str, value: str) -> None:
    import keyring

    try:
        keyring.set_password(SERVICE, account, value)
    except Exception as exc:
        raise SecretStoreError(f"Couldn't save to the {store_name()}: {exc}") from exc


def delete(account: str) -> None:
    import keyring

    try:
        keyring.delete_password(SERVICE, account)
    except Exception:  # already gone
        pass
