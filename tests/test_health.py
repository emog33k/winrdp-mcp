"""Health-matrix tests: the false positives test_host/provision used to report.

No box is needed — transports are faked at the boundary, and the cases are the ones that
used to look healthy: rc=0 after a non-terminating error, Medium integrity, chunked-only
transfer, an SFTP subsystem that authenticates but does not work, and a session-less box.
"""

from __future__ import annotations

import json
import threading

import pytest

from winrdp_mcp import health, provision, transports
from winrdp_mcp.elevation import ElevatedResult
from winrdp_mcp.health import check_health
from winrdp_mcp.tools import hosts
from winrdp_mcp.transports import ExecResult, WinRMTransport
from winrdp_mcp.vault import Host

HEALTHY_INFO = {
    "hostname": "BOX", "user": "BOX\\admin", "is_admin": True, "is_system": False,
    "integrity": "High", "latfp": 1, "ps_version": "5.1.20348.1", "build": "20348",
}
HEALTHY_SESSION = {"active": True, "console": True, "rows": [{"session": "console"}]}


def _out(obj, rc=0):
    return ExecResult(f"{health.ps.BEGIN}\n{json.dumps(obj)}\n{health.ps.END}\n", "", rc)


class FakeTransport:
    name = "fake"

    def __init__(self, info=None, session=None, channel="local", fail_upload=False,
                 info_rc=0):
        self.info = HEALTHY_INFO if info is None else info
        self.session = HEALTHY_SESSION if session is None else session
        self.channel = channel
        self.fail_upload = fail_upload
        self.info_rc = info_rc
        self.files = {}

    def run_ps(self, script, timeout=120, idempotent=False):
        if "qwinsta" in script:
            return _out(self.session)
        if "LocalAccountTokenFilterPolicy" in script:
            return _out(self.info, self.info_rc)
        return ExecResult("", "", 0)

    def upload(self, data, remote_path, timeout=300, progress=None):
        if self.fail_upload:
            raise OSError("transfer exploded")
        self.files[remote_path] = data

    def download(self, remote_path, timeout=300):
        return self.files[remote_path]

    def file_channel(self):
        return self.channel


@pytest.fixture(autouse=True)
def _elevation_ok(monkeypatch):
    monkeypatch.setattr(
        health.elevation, "run_elevated",
        lambda t, script, timeout=60: ElevatedResult(
            "WINRDP_ELEV_OK\nWINRDP_ELEV:NT AUTHORITY\\SYSTEM\n", "", 0))


def _step(hc, name):
    return next(s for s in hc["steps"] if s["name"] == name)


def test_every_step_passes_on_a_healthy_box():
    hc = check_health(FakeTransport(), expected_name="BOX")
    assert hc["ok"] is True and hc["failed_step"] is None
    assert [s["name"] for s in hc["steps"]] == [
        "identity", "principal", "transfer", "elevation", "session", "platform"]
    assert hc["capabilities"] == {"as_user": True, "screenshot": True, "gui": True}


def test_identity_mismatch_fails_even_when_everything_else_works():
    hc = check_health(FakeTransport(), expected_name="OTHERBOX")
    assert hc["ok"] is False and hc["failed_step"] == "identity"
    assert "BOX" in _step(hc, "identity")["detail"]


def test_medium_integrity_fails_principal():
    """A filtered token answers commands (rc=0) but cannot administer the box."""
    info = {**HEALTHY_INFO, "integrity": "Medium"}
    hc = check_health(FakeTransport(info=info))
    assert hc["ok"] is False
    assert _step(hc, "principal")["ok"] is False
    assert "Medium" in _step(hc, "principal")["detail"]


def test_latfp_not_one_fails_principal():
    hc = check_health(FakeTransport(info={**HEALTHY_INFO, "latfp": 0}))
    assert _step(hc, "principal")["ok"] is False


def test_rc_zero_with_an_error_payload_still_fails():
    """The regression behind 'elevated returned rc=0 after an error': rc is not health."""
    hc = check_health(FakeTransport(info={"error": "boom"}, info_rc=0))
    assert hc["ok"] is False and hc["failed_step"] == "identity"
    assert "boom" in _step(hc, "principal")["detail"]


def test_chunked_transfer_is_a_warning_not_success():
    hc = check_health(FakeTransport(channel=None))
    tr = _step(hc, "transfer")
    assert tr["ok"] is False and tr["warning"] is True and tr["channel"] == "chunked"
    assert hc["ok"] is False and hc["failed_step"] == "transfer"


def test_hash_mismatch_fails_transfer():
    class Corrupt(FakeTransport):
        def download(self, remote_path, timeout=300):
            return b"not-the-same-bytes"

    hc = check_health(Corrupt())
    assert _step(hc, "transfer")["ok"] is False
    assert "MISMATCH" in _step(hc, "transfer")["detail"]


def test_no_active_session_marks_gui_unavailable_but_console_keeps_host_ok():
    hc = check_health(FakeTransport(session={"active": False, "console": True, "rows": []}))
    assert _step(hc, "session")["ok"] is True
    assert hc["capabilities"] == {"as_user": False, "screenshot": False, "gui": False}
    assert "unavailable" in _step(hc, "session")["detail"]


def test_no_session_at_all_fails_the_session_step():
    hc = check_health(FakeTransport(session={"active": False, "console": False, "rows": []}))
    assert hc["ok"] is False and hc["failed_step"] == "session"


def test_elevation_rc_zero_without_the_marker_fails(monkeypatch):
    """The elevated wrapper runs EAP=Continue and $LASTEXITCODE is null for a cmdlet error,
    so a broken task can exit 0. The full-token marker is the evidence, not rc."""
    monkeypatch.setattr(
        health.elevation, "run_elevated",
        lambda t, script, timeout=60: ElevatedResult("", "non-terminating error", 0))
    hc = check_health(FakeTransport())
    assert _step(hc, "elevation")["ok"] is False
    assert hc["failed_step"] == "elevation"


# ---------------------------------------------------------------- test_host wiring
class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class _FakeCtx:
    def __init__(self, transport, host):
        self.transport = transport
        self.host = host
        self.invalidated = []

    def resolve(self, host):
        return self.host

    def transport_for(self, host):
        return self.transport

    def invalidate(self, alias):
        self.invalidated.append(alias)


def _healthy_result():
    names = ("identity", "principal", "transfer", "elevation", "session", "platform")
    return {
        "ok": True, "failed_step": None, "failed_steps": [],
        "steps": [{"name": n, "ok": True, "detail": "ok"} for n in names],
        "capabilities": {"as_user": True, "screenshot": True, "gui": True},
        "hostname": "BOX",
    }


def _test_host_fixture(monkeypatch, *, intercepts, result=None, raises=None):
    h = Host(alias="a", host="1.2.3.4", transport="winrm", expected_name="BOX")
    ctx = _FakeCtx(FakeTransport(), h)
    m = _FakeMCP()
    hosts.register(m, ctx)
    monkeypatch.setattr(hosts, "port_open",
                        lambda target, port, timeout=2: intercepts)
    monkeypatch.setattr(hosts, "_scan", lambda host: {"winrm_http": True})
    if raises is not None:
        def boom(*a, **k):
            raise raises
        monkeypatch.setattr(health, "check_health", boom)
    else:
        monkeypatch.setattr(health, "check_health",
                            lambda t, expected_name=None: result or _healthy_result())
    return m.fns["test_host"](host="a"), ctx


def test_dead_ip_control_marks_the_port_scan_unreliable(monkeypatch):
    r, _ = _test_host_fixture(monkeypatch, intercepts=True)
    assert r["operator_network"] == "intercepting-proxy-or-vpn"
    assert r["ports_reliable"] is False
    assert "ports_note" in r
    # ports are still reported, but explicitly not as health
    assert r["ports"]["winrm_http"] is True and r["ok"] is True


def test_direct_network_reports_reliable_ports(monkeypatch):
    r, _ = _test_host_fixture(monkeypatch, intercepts=False)
    assert r["ports_reliable"] is True and r["operator_network"] == "direct"


def test_failed_health_still_returns_ok_and_failed_step(monkeypatch):
    bad = _healthy_result()
    bad["ok"] = False
    bad["failed_step"] = "principal"
    bad["failed_steps"] = ["principal"]
    r, ctx = _test_host_fixture(monkeypatch, intercepts=False, result=bad)
    assert r["ok"] is False and r["online"] is False
    assert r["failed_step"] == "principal" and r["steps"]
    assert "bootstrap_oneliner" in r
    assert "a" in ctx.invalidated


def test_probe_failure_invalidates_the_cached_transport(monkeypatch):
    r, ctx = _test_host_fixture(monkeypatch, intercepts=False,
                                raises=RuntimeError("wedged"))
    assert r["ok"] is False and r["failed_step"] == "transport"
    assert r["online"] is False and "wedged" in r["error"]
    assert ctx.invalidated == ["a"]
    assert "bootstrap_oneliner" in r


# ---------------------------------------------------------------- SFTP verification
def _winrm_without_connecting(monkeypatch, *, sftp_works):
    t = object.__new__(WinRMTransport)
    t._fchan = None
    t.host = "h"
    t._user = "u"
    t._password = "p"
    t._oplock = threading.RLock()

    monkeypatch.setattr(transports, "port_open",
                        lambda host, port, timeout=2: port == 22)

    class FakeSSH:
        def __init__(self, *a, **k):
            pass

        def verify_sftp(self):
            if not sftp_works:
                raise OSError("subsystem request failed")
            return True

        def close(self):
            pass

    monkeypatch.setattr(transports, "SSHTransport", FakeSSH)
    return t._fast_channel()


def test_sftp_authenticating_without_a_subsystem_is_not_claimed(monkeypatch):
    assert _winrm_without_connecting(monkeypatch, sftp_works=False) is None


def test_sftp_is_claimed_only_after_a_real_write(monkeypatch):
    ch = _winrm_without_connecting(monkeypatch, sftp_works=True)
    assert ch is not None and ch[0] == "sftp"


# ---------------------------------------------------------------- provision success
def _hc(principal=True, transfer=True):
    steps = [{"name": "identity", "ok": True, "detail": ""},
             {"name": "principal", "ok": principal, "detail": ""},
             {"name": "transfer", "ok": transfer, "detail": ""}]
    return {"ok": principal and transfer,
            "failed_step": None if principal and transfer else "principal",
            "failed_steps": [], "steps": steps, "capabilities": {}, "hostname": "H"}


@pytest.mark.parametrize("principal,transfer,harden,want_step", [
    (True, True, True, None),
    (False, True, True, "principal"),
    (True, False, True, "transfer"),
    (True, True, False, "harden"),
])
def test_provision_success_requires_principal_transfer_and_harden(
        monkeypatch, principal, transfer, harden, want_step):
    monkeypatch.setattr(health, "check_health",
                        lambda t, expected_name=None: _hc(principal, transfer))
    rep = provision.ProvisionReport(alias="a", host="h")
    ok = provision._verify_transport(object(), Host(alias="a", host="h"), rep,
                                     need_harden=True, harden_ok=harden)
    assert ok is (want_step is None)
    assert rep.success is False  # _verify_transport only reports; the caller sets it
    assert rep.failed_step == want_step
