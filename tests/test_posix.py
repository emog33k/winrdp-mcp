"""POSIX SSH support: run_sh, shebang detection, and the POSIX health matrix."""

from __future__ import annotations

from winrdp_mcp.health import check_health
from winrdp_mcp.tools import scripting
from winrdp_mcp.tools.scripting import _guess_interpreter
from winrdp_mcp.transports import ExecResult, SSHTransport
from winrdp_mcp.vault import Host


class FakePosix:
    name = "ssh"

    def __init__(self, uid="0", sudo=False, channel="sftp"):
        self.uid = uid
        self.sudo = sudo
        self.channel = channel
        self.files = {}

    def is_posix(self):
        return True

    def run_sh(self, script, timeout=120):
        if "hostname; id -un" in script:
            return ExecResult(f"vps-payload\nroot\n{self.uid}\nLinux 6.1 x86_64\n", "", 0)
        if "sudo -n true" in script:
            return ExecResult("WINRDP_SUDO_OK\n" if self.sudo else "",
                              "" if self.sudo else "sudo: a password is required", 0 if self.sudo else 1)
        return ExecResult("", "", 0)

    def upload(self, data, remote_path, timeout=300, progress=None):
        self.files[remote_path] = data

    def download(self, remote_path, timeout=300):
        return self.files[remote_path]

    def file_channel(self):
        return self.channel


def _step(hc, name):
    return next(s for s in hc["steps"] if s["name"] == name)


def test_posix_health_passes_for_root():
    hc = check_health(FakePosix(), expected_name="vps-payload")
    assert hc["ok"] is True, hc
    assert hc["kind"] == "posix"
    assert _step(hc, "principal")["ok"] is True
    assert _step(hc, "transfer")["ok"] is True
    assert _step(hc, "session")["ok"] is True and "not applicable" in _step(hc, "session")["detail"]
    assert hc["capabilities"] == {"as_user": False, "screenshot": False, "gui": False}


def test_posix_health_fails_without_root_or_sudo():
    hc = check_health(FakePosix(uid="1000", sudo=False))
    assert hc["ok"] is False
    assert _step(hc, "principal")["ok"] is False
    assert _step(hc, "elevation")["ok"] is False


def test_posix_sudo_counts_as_admin():
    hc = check_health(FakePosix(uid="1000", sudo=True))
    assert _step(hc, "principal")["ok"] is True
    assert hc["ok"] is True


def test_posix_identity_mismatch_fails():
    hc = check_health(FakePosix(), expected_name="other")
    assert hc["failed_step"] == "identity"


def test_posix_chunked_is_not_a_success():
    hc = check_health(FakePosix(channel=None))
    assert _step(hc, "transfer")["ok"] is False
    assert _step(hc, "transfer")["warning"] is True


def test_shebang_detection_for_posix():
    assert _guess_interpreter("#!/bin/bash\nls\n") == "bash"
    assert _guess_interpreter("#!/bin/sh\nls\n") == "sh"


def test_ssh_is_posix_detection_is_cached():
    t = object.__new__(SSHTransport)
    t._posix = None
    calls = []
    t._exec = lambda cmd, timeout: (calls.append(1),
                                    ExecResult("WINRDP_POSIX\n", "", 0))[1]
    assert t.is_posix() is True and t.is_posix() is True
    assert len(calls) == 1


def test_ssh_is_posix_false_when_sh_is_missing():
    t = object.__new__(SSHTransport)
    t._posix = None
    t._exec = lambda cmd, timeout: ExecResult("", "sh: not found", 127)
    assert t.is_posix() is False


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class _WindowsTransport:
    name = "winrm"


class _Ctx:
    def __init__(self):
        self.transport = _WindowsTransport()
        self.host = Host(alias="a", host="h")

    def resolve(self, host):
        return self.host

    def transport_for(self, host):
        return self.transport


def test_run_sh_rejects_a_windows_host():
    m = _FakeMCP()
    scripting.register(m, _Ctx())
    r = m.fns["run_sh"]("uname -a", host="a")
    assert "no POSIX shell" in r["error"]
