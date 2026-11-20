"""Schoology T2 Support REST API client - a lightweight Postman replacement."""

__version__ = "1.1.0"
APP_NAME = "Schoology API Client"


def _use_system_certificates() -> None:
    """Trust the Windows/macOS certificate store (needed behind corporate SSL inspection).

    This must run before `requests` is imported anywhere, because requests builds its
    default SSL context at import time.
    """
    from .config import load_settings

    try:
        if not load_settings().use_system_certificates:
            return
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # truststore missing or unsupported platform -> fall back to certifi
        pass


_use_system_certificates()
