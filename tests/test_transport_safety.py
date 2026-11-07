"""Transport safety regressions: no blind retry of mutating scripts, EAP=Stop, upload resume."""

from __future__ import annotations

import inspect
import os

import pytest

from winrdp_mcp.tools import system
from winrdp_mcp.transports import ExecResult, LocalTransport, Transport, WinRMTransport

needs_windows = pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class _Ctx:
    def __init__(self):
        self.kwargs = {}

    def exec_ps(self, script, **kwargs):
        self.kwargs = kwargs
        return ExecResult(stdout="", stderr="", rc=0)

    def run_detached(self, script, host=None):
        return {}


def test_run_powershell_applies_the_stop_prologue():
    ctx = _Ctx()
    m = _FakeMCP()
    system.register(m, ctx)
    m.fns["run_powershell"]("Write-Output hi")
    assert ctx.kwargs.get("wrap") is True


def test_winrm_run_ps_is_not_idempotent_by_default():
    assert inspect.signature(WinRMTransport.run_ps).parameters["idempotent"].default is False


def test_recoverable_error_is_retried_only_when_idempotent():
    import requests

    t = object.__new__(WinRMTransport)
    t.host = "h"
    t._connect = lambda: None
    calls = []

    def boom():
        calls.append(1)
        raise requests.exceptions.ConnectionError("dropped")

    with pytest.raises(requests.exceptions.ConnectionError):
        t._run(boom, idempotent=False)
    assert len(calls) == 1, "a mutating call must not be retried"

    calls.clear()
    with pytest.raises(requests.exceptions.ConnectionError):
        t._run(boom, idempotent=True)
    assert len(calls) == 2, "a read may retry once"


class _ChunkedLocal(Transport):
    """Base Transport.upload over a real local PowerShell, with a simulated mid-flight drop."""

    name = "chunked-local"

    def __init__(self, fail_after=None):
        self.fail_after = fail_after
        self.calls = 0
        self._local = LocalTransport()

    def _call(self, script, timeout):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise OSError("simulated connection drop")
        self.calls += 1
        return self._local.run_ps(script, timeout)

    def run_ps(self, script, timeout=120, idempotent=False):
        return self._call(script, timeout)

    def _run_ps_inline(self, script, timeout=120, idempotent=False):
        return self._call(script, timeout)


@needs_windows
def test_chunked_upload_resumes_after_a_drop(tmp_path):
    data = os.urandom(64 * 1024)
    remote = str(tmp_path / "resume.bin")
    first = _ChunkedLocal(fail_after=5)
    with pytest.raises(OSError):
        first.upload(data, remote)
    assert not os.path.exists(remote)
    assert os.path.exists(remote + ".gz.b64") and os.path.exists(remote + ".gz.state")

    second = _ChunkedLocal()
    second.upload(data, remote)
    assert open(remote, "rb").read() == data
    assert not os.path.exists(remote + ".gz.b64")
    assert not os.path.exists(remote + ".gz.state")


@needs_windows
def test_chunked_upload_starts_over_when_the_payload_changes(tmp_path):
    remote = str(tmp_path / "changed.bin")
    with pytest.raises(OSError):
        _ChunkedLocal(fail_after=5).upload(os.urandom(64 * 1024), remote)
    # a different payload must not append to the old temp
    new = os.urandom(64 * 1024)
    _ChunkedLocal().upload(new, remote)
    assert open(remote, "rb").read() == new
