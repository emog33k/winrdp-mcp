"""Filesystem paths and process-wide settings for winrdp-mcp."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Per-user data directory (inventory, vault key, staged-tool cache manifest)."""
    override = os.environ.get("WINRDP_HOME")
    if override:
        p = Path(override)
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        p = Path(base) / "winrdp-mcp"
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        p = Path(base) / "winrdp-mcp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def inventory_path() -> Path:
    return data_dir() / "inventory.json"


def key_path() -> Path:
    return data_dir() / "vault.key"


def scripts_dir() -> Path:
    """Directory holding bundled PowerShell bootstrap scripts."""
    # Installed layout: winrdp_mcp/_scripts ; dev layout: <repo>/scripts
    here = Path(__file__).resolve().parent
    candidates = [here / "_scripts", here.parent / "scripts"]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


# Remote staging locations on managed boxes.
REMOTE_ROOT = r"C:\ProgramData\winrdp-mcp"
REMOTE_TOOLS = REMOTE_ROOT + r"\tools"
REMOTE_TMP = REMOTE_ROOT + r"\tmp"

# Default WinRM ports.
WINRM_HTTP_PORT = 5985
WINRM_HTTPS_PORT = 5986
SSH_PORT = 22
RDP_PORT = 3389
SMB_PORT = 445
