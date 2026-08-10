"""PowerShell script building and JSON marshaling helpers.

All structured tools funnel through here so their output is deterministic, machine
parseable, and robust to PowerShell's habit of emitting bare scalars / single objects
where an array is expected.
"""

from __future__ import annotations

import base64
import json
from typing import Any


def encode_command(script: str) -> str:
    """Return a base64 UTF-16LE blob for ``powershell -EncodedCommand``.

    This sidesteps every quoting/escaping problem when shipping a script over SSH or a
    local subprocess where we control the full command line.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# Prologue that makes scripts fail loudly and predictably.
_PROLOGUE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
)

# Sentinels bracket the JSON payload so we can extract it even if a cmdlet prints
# stray text (banners, warnings) around it.
BEGIN = "<<<WINRDP_JSON_BEGIN>>>"
END = "<<<WINRDP_JSON_END>>>"


def wrap_json(body: str, depth: int = 6) -> str:
    """Wrap a script body so its final expression is emitted as delimited JSON.

    ``body`` should assign its result to ``$result`` (any object/array/scalar).
    """
    return (
        _PROLOGUE
        + "try{"
        + body
        + f";$__j=($result|ConvertTo-Json -Depth {depth} -Compress);"
        + f"Write-Output '{BEGIN}';Write-Output $__j;Write-Output '{END}'"
        + "}catch{"
        + f"Write-Output '{BEGIN}';"
        + "Write-Output (@{error=$_.Exception.Message;"
        + "category=$_.CategoryInfo.Category.ToString();"
        + "target=$_.CategoryInfo.TargetName}|ConvertTo-Json -Compress);"
        + f"Write-Output '{END}'}}"
    )


def wrap_plain(body: str) -> str:
    """Wrap a raw shell body (no JSON): just apply the safety prologue."""
    return _PROLOGUE + body


def parse_json(stdout: str) -> Any:
    """Extract and decode the JSON payload produced by :func:`wrap_json`."""
    start = stdout.find(BEGIN)
    end = stdout.find(END)
    if start == -1 or end == -1:
        raise ValueError(f"No JSON payload in output:\n{stdout.strip()[:2000]}")
    blob = stdout[start + len(BEGIN):end].strip()
    if not blob:
        return None
    return json.loads(blob)


def as_list(value: Any) -> list:
    """PowerShell collapses single-element arrays to a scalar; normalize back to list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def ps_string(value: str) -> str:
    """Quote a Python string as a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def ensure_remote_dirs() -> str:
    """Snippet ensuring the winrdp working directories exist on the box."""
    from .config import REMOTE_ROOT, REMOTE_TMP, REMOTE_TOOLS

    return (
        f"foreach($d in @({ps_string(REMOTE_ROOT)},{ps_string(REMOTE_TOOLS)},"
        f"{ps_string(REMOTE_TMP)})){{if(-not(Test-Path $d)){{"
        "New-Item -ItemType Directory -Path $d -Force|Out-Null}}}"
    )
