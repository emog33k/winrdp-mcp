"""Interactive-session discovery and launch for GUI / as_user ops.

Guards two regressions:
  * the qwinsta USERNAME slice must not keep the trailing session-id digits, or the
    value fails the SID lookup in Register-ScheduledTask (ERROR_NONE_MAPPED);
  * the interactive-session task must launch powershell.exe with -WindowStyle Hidden
    so it does not flash a console window on the target desktop.
"""

from __future__ import annotations

import os

import pytest

from winrdp_mcp import elevation
from winrdp_mcp.transports import ExecResult


def _run_parse(sample_lines):
    """Run the real session-parse PowerShell against a canned qwinsta sample."""
    from winrdp_mcp.transports import LocalTransport

    rows = "$rows=@(" + ",".join("'" + ln + "'" for ln in sample_lines) + ");"
    out = LocalTransport().run_ps(rows + elevation._SESSION_PARSE_PS, timeout=30)
    return out.stdout.strip().splitlines()[-1].strip()


@pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")
def test_session_parse_strips_session_id_for_short_username():
    # Real qwinsta layout: the ID column sits between USERNAME and STATE, so a short
    # username's slice would otherwise absorb the id ("admin                     1").
    sample = [
        " SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE ",
        " services                                    0  Disc                        ",
        ">console           admin                     1  Active                      ",
    ]
    assert _run_parse(sample) == "ACTIVE:admin"


@pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")
def test_session_parse_keeps_username_with_space():
    # Local Windows account names may contain spaces; cutting at the ID column keeps them.
    sample = [
        " SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE ",
        ">console           John Doe                  1  Active                      ",
    ]
    assert _run_parse(sample) == "ACTIVE:John Doe"


@pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")
def test_session_parse_reports_disconnected():
    sample = [
        " SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE ",
        " rdp-tcp#3         alice                     3  Disc                        ",
    ]
    assert _run_parse(sample) == "DISC:alice"


@pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")
def test_session_parse_reports_none_when_no_user():
    sample = [
        " SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE ",
        " services                                    0  Disc                        ",
    ]
    assert _run_parse(sample) == "NONE"


class _InteractiveTransport:
    """Drives run_in_user_session's happy path without touching a real box."""

    def __init__(self):
        self.calls = []
        self.uploads = []
        self.register_script = ""

    def run_ps(self, script, timeout=120):
        self.calls.append(script)
        if "qwinsta" in script:
            return ExecResult("ACTIVE:admin\n", "", 0)
        if "Register-ScheduledTask" in script:
            self.register_script = script
            return ExecResult("", "", 0)
        if "Get-Content -LiteralPath" in script:  # _safe_read of .done / .out
            return ExecResult("rc=0\n" if ".done" in script else "ok\n", "", 0)
        if script.startswith("Test-Path -LiteralPath"):  # completion poll
            return ExecResult("True\n", "", 0)
        return ExecResult("", "", 0)

    def upload(self, data, remote_path, timeout=300):
        self.uploads.append((data, remote_path))


def test_interactive_task_hidden_window_and_clean_userid():
    transport = _InteractiveTransport()
    result = elevation.run_in_user_session(transport, "'ok'", timeout=1)

    assert result.rc == 0
    # Issue #2: the interactive-session task must not flash a console window.
    assert "-WindowStyle Hidden" in transport.register_script
    # Issue #1: the parsed username reaches -UserId intact (no session-id suffix).
    assert "-UserId 'admin'" in transport.register_script
    assert any("qwinsta" in c for c in transport.calls)
