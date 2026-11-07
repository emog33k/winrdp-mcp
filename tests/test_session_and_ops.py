"""Session selection, alias collisions, audit log, cleanup profile and output cap."""

from __future__ import annotations

import json
import os

import pytest

from winrdp_mcp import config, log, tooling
from winrdp_mcp.context import Context
from winrdp_mcp.tools import hosts, system
from winrdp_mcp.transports import ExecResult, LocalTransport, TransportError
from winrdp_mcp.vault import Host

needs_windows = pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


# ---------------------------------------------------------------- sessions
@needs_windows
def test_list_sessions_has_id_user_state_and_active():
    from winrdp_mcp.elevation import list_sessions

    data = list_sessions(LocalTransport())
    assert data["active"] is True
    row = next(r for r in data["rows"] if r["active"])
    assert row["id"].isdigit() and row["user"] and row["state"] == "Active"


@needs_windows
def test_unknown_session_id_lists_the_available_ones():
    from winrdp_mcp.elevation import run_in_user_session

    with pytest.raises(TransportError) as ei:
        run_in_user_session(LocalTransport(), "$true", session_id=99999)
    assert "not found" in str(ei.value) and "available" in str(ei.value)


def test_session_id_requires_as_user():
    ctx = Context()
    try:
        ctx.vault.add(Host(alias="_sess_local", host="localhost", transport="local"))
        with pytest.raises(ValueError, match="session_id only applies with as_user"):
            ctx.exec_ps("$true", host="_sess_local", session_id=1)
    finally:
        ctx.vault.remove("_sess_local")
        ctx.close()


# ---------------------------------------------------------------- aliases
def test_add_host_warns_when_the_same_address_has_another_alias():
    ctx = Context()
    m = _FakeMCP()
    hosts.register(m, ctx)
    try:
        m.fns["add_host"]("_dup_a", "10.0.0.99")
        r = m.fns["add_host"]("_dup_b", "10.0.0.99", make_active=False)
        assert "_dup_a" in r["aliases_for_same_host"]
        assert "already registered" in r["warning"]
        lst = m.fns["list_hosts"]()
        assert set(lst["duplicate_hosts"]["10.0.0.99"]) == {"_dup_a", "_dup_b"}
    finally:
        for alias in ("_dup_a", "_dup_b"):
            ctx.vault.remove(alias)
        ctx.close()


# ---------------------------------------------------------------- audit
def test_audit_log_is_jsonl_and_redacts(tmp_path, monkeypatch):
    from winrdp_mcp import server

    monkeypatch.setenv("WINRDP_HOME", str(tmp_path))
    log.register_secret("super-secret-value")
    server._audit_write("run_powershell", "box", False, 12.3,
                        "failed with super-secret-value")
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    assert rec["tool"] == "run_powershell" and rec["host"] == "box"
    assert rec["ok"] is False and rec["duration_ms"] == 12.3
    assert "super-secret-value" not in json.dumps(rec)


# ---------------------------------------------------------------- cleanup profile
@needs_windows
def test_cleanup_dry_run_lists_without_deleting():
    t = LocalTransport()
    tooling.stage_script(t, "Write-Output hi", "_cleanup_probe.ps1")
    path = os.path.join(config.REMOTE_TOOLS, "_cleanup_probe.ps1")
    try:
        d = tooling.cleanup(t, "_cleanup_probe.ps1", dry_run=True)
        assert d["dry_run"] is True and d["count"] == 1 and d["bytes"] > 0
        assert os.path.exists(path)
        r = tooling.cleanup(t, "_cleanup_probe.ps1")
        assert r["dry_run"] is False and r["count"] == 1
        assert not os.path.exists(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------- output cap
def test_clip_is_a_noop_unless_a_limit_is_set():
    assert system._clip("abcdef", 0) == ("abcdef", False)
    assert system._clip("abcdef", 100) == ("abcdef", False)


def test_clip_reports_truncation():
    text, cut = system._clip("abcdef", 3)
    assert cut is True and text.startswith("abc") and "truncated" in text


def test_run_powershell_honours_max_output():
    class Ctx:
        def exec_ps(self, script, **kw):
            return ExecResult("abcdefgh", "", 0)

        def run_detached(self, script, host=None):
            return {}

    m = _FakeMCP()
    system.register(m, Ctx())
    r = m.fns["run_powershell"]("x", max_output=4)
    assert r["truncated"] is True and r["stdout"].startswith("abcd")
