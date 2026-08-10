"""Input validation helpers for tool arguments interpolated into command lines.

Structured tools that splice a model-supplied value into a PowerShell/cmd command (rather
than quoting it with :func:`winrdp_mcp.ps.ps_string`) use these to reject values that
would break out of the intended argument — an extra layer, since some of those commands
run elevated.
"""

from __future__ import annotations

import re


class ValidationError(ValueError):
    pass


def enum(value: str, allowed: set[str], name: str) -> str:
    """Return ``value`` if it is one of ``allowed`` (case-sensitive), else raise."""
    if value not in allowed:
        raise ValidationError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return value


# Host / IP: letters, digits, dot, colon (IPv6), hyphen, underscore. No spaces or shell meta.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")
# Package ids (winget/choco): add plus and slash. Still no whitespace or shell meta.
_PKG_RE = re.compile(r"^[A-Za-z0-9._+/\-]{1,200}$")
# Task/service/account names: printable, but never a double quote or control/`&|;` meta.
_NAME_RE = re.compile(r'^[^"\r\n\t`|&;<>%]{1,256}$')
# tscon /dest target and similar bare tokens.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")


def host(value: str, name: str = "host") -> str:
    if not _HOST_RE.match(value or ""):
        raise ValidationError(f"{name} contains illegal characters: {value!r}")
    return value


def package(value: str, name: str = "name") -> str:
    if not _PKG_RE.match(value or ""):
        raise ValidationError(f"{name} contains illegal characters: {value!r}")
    return value


def obj_name(value: str, name: str = "name") -> str:
    if not _NAME_RE.match(value or ""):
        raise ValidationError(f"{name} contains illegal characters (quotes/control/shell meta): {value!r}")
    return value


def token(value: str, name: str = "value") -> str:
    if not _TOKEN_RE.match(value or ""):
        raise ValidationError(f"{name} contains illegal characters: {value!r}")
    return value
