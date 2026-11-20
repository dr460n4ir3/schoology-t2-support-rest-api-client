"""Keep secrets out of anything that is copied, saved, exported, logged or shown to an AI.

Every secret the app knows about (secret environment values, auth secrets after {{variables}} are filled in,
OAuth 2.0 tokens it fetched) is registered here. redact() then hides them, plus anything that looks like a
credential (Authorization headers, "access_token": "...", oauth_signature=...), in any piece of text.
The screen itself still shows the real response: this is for text that leaves the window.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Iterable

HIDDEN = "[hidden]"
MIN_LENGTH = 4  # shorter values would hide ordinary text ("1", "abc")

_secrets: set[str] = set()
_lock = threading.Lock()

_PATTERNS = [
    # Authorization: Bearer xyz / Basic xyz / OAuth oauth_consumer_key="..." ...
    (re.compile(r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)(bearer|basic|oauth|token)\s+[^\"'\r\n]+"),
     lambda m: f"{m.group(1)}{m.group(2)} {HIDDEN}"),
    # "access_token": "xyz"  /  client_secret=xyz  /  'password': 'xyz'
    (re.compile(r"(?i)([\"']?\b(?:access_token|refresh_token|id_token|client_secret|consumer_secret|token_secret|"
                r"oauth_signature|password|api_key|apikey)[\"']?\s*[:=]\s*[\"']?)([^\"'&\s,;}<]+)"),
     lambda m: f"{m.group(1)}{HIDDEN}"),
]


def register(*values: str | None) -> None:
    """Remember secret values so redact() can hide them. Blank, short and {{template}} values are ignored."""
    with _lock:
        for value in values:
            v = (value or "").strip()
            if len(v) >= MIN_LENGTH and "{{" not in v and not v.upper().startswith("PASTE-"):
                _secrets.add(v)


def forget_all() -> None:
    with _lock:
        _secrets.clear()


def redact(text: str | None, extra: Iterable[str] = ()) -> str:
    if not text:
        return text or ""
    with _lock:
        known = set(_secrets)
    known |= {v.strip() for v in extra if v and len(v.strip()) >= MIN_LENGTH and "{{" not in v}
    for secret in sorted(known, key=len, reverse=True):  # longest first, so a secret inside another is handled
        if secret in text:
            text = text.replace(secret, HIDDEN)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter: secrets never reach the log file."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken log call should not break the app
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def install_log_filter() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
