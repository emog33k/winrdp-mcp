"""LocalTransport tests — run real PowerShell; skipped off Windows."""

from __future__ import annotations

import os
import tempfile

import pytest

from winrdp_mcp import ps
from winrdp_mcp.transports import LocalTransport

pytestmark = pytest.mark.skipif(os.name != "nt", reason="LocalTransport needs Windows PowerShell")


def test_run_ps_returns_output():
    t = LocalTransport()
    r = t.run_ps("Write-Output (2+2)")
    assert r.ok
    assert "4" in r.stdout


def test_exec_json_roundtrip_via_wrap():
    t = LocalTransport()
    r = t.run_ps(ps.wrap_json("$result=@{a=1;b='hello'}"))
    assert ps.parse_json(r.stdout) == {"a": 1, "b": "hello"}


def test_json_error_path_is_structured():
    t = LocalTransport()
    r = t.run_ps(ps.wrap_json("$result = Get-Item 'Q:\\no\\such\\path'"))
    data = ps.parse_json(r.stdout)
    assert isinstance(data, dict) and "error" in data


def test_upload_download_roundtrip():
    t = LocalTransport()
    p = os.path.join(tempfile.gettempdir(), "winrdp_ut.bin")
    payload = b"binary\x00\x01\x02data"
    t.upload(payload, p)
    assert t.download(p) == payload
    os.remove(p)
