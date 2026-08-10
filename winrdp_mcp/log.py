"""Logging that is safe for an stdio MCP server.

All logs go to **stderr** — stdout is the MCP protocol channel and must not be polluted.
`WINRDP_DEBUG=1` (or `--debug`) raises the level to DEBUG. Passwords are never logged;
:func:`redact` scrubs anything that looks like a credential before it hits a record.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_CONFIGURED = False

# Exact secret values registered at runtime (the vault knows the real passwords). This is
# the strong redaction path — pattern matching alone misses passwords with spaces/quotes.
_SECRETS: set[str] = set()

# Structural fallback for secrets we did not capture (best-effort).
_REDACT_PATTERNS = [
    re.compile(r"(/pass:)\S+", re.IGNORECASE),
    re.compile(r"(/RP\s+\")[^\"]*(\")", re.IGNORECASE),
    re.compile(r"(password[=:\s]+)\S+", re.IGNORECASE),
    re.compile(r"(ConvertTo-SecureString\s+')[^']*(')", re.IGNORECASE),
]


def register_secret(value: str) -> None:
    """Register a plaintext secret so it is scrubbed by exact match from every log line."""
    if value and len(value) >= 3:
        _SECRETS.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for secret in _SECRETS:
        if secret in out:
            out = out.replace(secret, "***")
    for pat in _REDACT_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "***" + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""), out)
    return out


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def setup(debug: bool | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        if debug:
            logging.getLogger("winrdp").setLevel(logging.DEBUG)
        return
    level = logging.DEBUG if (debug or os.environ.get("WINRDP_DEBUG", "").lower() in ("1", "true", "yes")) else logging.INFO
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s winrdp [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    handler.addFilter(_RedactFilter())
    root = logging.getLogger("winrdp")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get(name: str = "winrdp") -> logging.Logger:
    if not _CONFIGURED:
        setup()
    return logging.getLogger(name if name.startswith("winrdp") else f"winrdp.{name}")
