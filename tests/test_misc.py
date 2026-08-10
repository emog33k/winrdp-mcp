"""Unit tests for interpreter guessing, log redaction, and provisioning helpers."""

from __future__ import annotations

from winrdp_mcp import log
from winrdp_mcp.provision import bootstrap_oneliner
from winrdp_mcp.tools.scripting import _guess_interpreter
from winrdp_mcp.vault import Host


def test_guess_interpreter():
    assert _guess_interpreter("import os\nprint(os.getcwd())") == "python"
    assert _guess_interpreter("#!/usr/bin/env python3\nx=1") == "python"
    assert _guess_interpreter("console.log('hi')") == "node"
    assert _guess_interpreter("const x = require('fs')") == "node"
    assert _guess_interpreter("@echo off\ndir") == "cmd"
    assert _guess_interpreter("Get-Process | Sort-Object CPU") == "powershell"


def test_log_redacts_secrets():
    assert "hunter2" not in log.redact("cmdkey /pass:hunter2")
    assert "topsecret" not in log.redact("password=topsecret")
    assert "sup3r" not in log.redact("ConvertTo-SecureString 'sup3r' -AsPlainText")


def test_bootstrap_oneliner_is_encoded_command():
    line = bootstrap_oneliner(Host(alias="_", host="_"))
    assert line.startswith("powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand ")
    import base64

    blob = line.rsplit(" ", 1)[1]
    decoded = base64.b64decode(blob).decode("utf-16-le")
    assert "WINRDP_WINRM_ENABLED" in decoded
