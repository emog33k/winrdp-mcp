"""sync_folder / file_upload size guards + excludes, and task_status.

The guard arithmetic is tested directly (no box needed); the tool behaviour runs against a
real localhost box through the `local` transport, so it is skipped off Windows.
"""

from __future__ import annotations

import os
import time

import pytest

from winrdp_mcp.context import Context
from winrdp_mcp.tools import files, scheduling, system
from winrdp_mcp.tools.files import _size_limit_mb, _too_big
from winrdp_mcp.transports import TransportError, transfer_eta_seconds
from winrdp_mcp.vault import Host

needs_windows = pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")


# ----------------------------------------------------------------- guard arithmetic
def test_limit_auto_sizes_from_the_channel():
    # a fast one-shot channel gets a generous ceiling; chunked WinRM a tight one
    assert _size_limit_mb("smb", None) == 1024.0
    assert _size_limit_mb("sftp", None) == 1024.0
    assert _size_limit_mb("local", None) == 1024.0
    assert _size_limit_mb(None, None) == 25.0


def test_explicit_limit_overrides_and_zero_disables():
    assert _size_limit_mb(None, 500) == 500.0
    assert _size_limit_mb("smb", 10) == 10.0
    assert _size_limit_mb(None, 0) == 0.0  # callers treat <=0 as "no limit"


def test_chunked_eta_is_far_worse_than_smb():
    mb50 = 50 * 1024 * 1024
    assert transfer_eta_seconds(mb50, "smb") < transfer_eta_seconds(mb50, None) / 10


def test_refusal_explains_itself_and_carries_the_numbers():
    r = _too_big("big.bin", 60 * 1024 * 1024, None, 25.0, "do X", {"extra": 1})
    assert r["ok"] is False and r["needs_confirmation"] is True
    assert r["total_mb"] == 60.0 and r["limit_mb"] == 25.0
    assert r["channel"] == "chunked" and r["estimated_seconds"] > 0
    assert "nothing was transferred" in r["reason"]
    assert r["hint"] == "do X" and r["extra"] == 1


# ----------------------------------------------------------------- tool behaviour
class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


@pytest.fixture
def tools():
    ctx = Context()
    ctx.vault.add(Host(alias="_sy_local", host="localhost", transport="local"))
    m = _FakeMCP()
    for mod in (files, scheduling, system):
        mod.register(m, ctx)
    yield m.fns
    ctx.vault.remove("_sy_local")
    ctx.close()


@pytest.fixture
def src(tmp_path):
    def w(rel, size):
        p = tmp_path / "src" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    w("app.py", 100)
    w("node_modules/pkg/index.js", 20000)
    w("build/blob.bin", 30000)
    return str(tmp_path / "src")


@needs_windows
def test_sync_dry_run_measures_without_transferring(tools, src, tmp_path):
    dest = str(tmp_path / "dest")
    r = tools["sync_folder"](src, dest, host="_sy_local", dry_run=True)
    assert r["dry_run"] is True and r["files"] == 3
    assert r["total_bytes"] == 50100
    assert r["channel"] == "local" and r["within_limit"] is True
    assert r["largest_entries"][0]["path"] in {"build", "node_modules"}
    assert not os.path.exists(dest)  # nothing moved


@needs_windows
def test_sync_dry_run_reports_what_excludes_drop(tools, src, tmp_path):
    r = tools["sync_folder"](src, str(tmp_path / "d"), host="_sy_local", dry_run=True,
                             exclude=["node_modules", "*.bin"])
    assert r["files"] == 1 and r["total_bytes"] == 100
    assert r["excludes_applied"] == ["node_modules", "*.bin"]
    assert r["excluded"]["files"] == 1  # blob.bin; pruned dirs are counted as dirs
    assert "node_modules" in r["excluded"]["dirs"]


@needs_windows
def test_sync_refuses_oversized_folder_and_says_what_to_exclude(tools, src, tmp_path):
    dest = str(tmp_path / "dest")
    r = tools["sync_folder"](src, dest, host="_sy_local", max_mb=0.01)
    assert r["ok"] is False and r["needs_confirmation"] is True
    assert r["limit_mb"] == 0.01
    assert r["largest_entries"] and "exclude=[...]" in r["hint"]
    assert not os.path.exists(dest)


@needs_windows
def test_sync_force_overrides_the_guard(tools, src, tmp_path):
    dest = str(tmp_path / "dest")
    r = tools["sync_folder"](src, dest, host="_sy_local", max_mb=0.01, force=True)
    assert r["ok"] is True and r["files_sent"] == 3
    assert os.path.isfile(os.path.join(dest, "app.py"))


@needs_windows
def test_sync_excludes_are_honoured_on_the_box(tools, src, tmp_path):
    dest = str(tmp_path / "dest")
    r = tools["sync_folder"](src, dest, host="_sy_local",
                             exclude=["node_modules", "*.bin"])
    assert r["ok"] is True and r["files_sent"] == 1
    assert os.path.isfile(os.path.join(dest, "app.py"))
    assert not os.path.exists(os.path.join(dest, "node_modules"))
    assert not os.path.exists(os.path.join(dest, "build", "blob.bin"))


@needs_windows
def test_sync_reports_timing_and_channel(tools, src, tmp_path):
    r = tools["sync_folder"](src, str(tmp_path / "dest"), host="_sy_local")
    assert r["channel"] == "local"
    assert set(r["seconds"]) == {"zip", "upload", "expand"}
    assert r["bytes_local"] == 50100 and r["bytes_zip"] > 0
    assert 0 < r["compression"] <= 1.5


@needs_windows
def test_sync_everything_excluded_is_an_error_not_an_empty_success(tools, src, tmp_path):
    r = tools["sync_folder"](src, str(tmp_path / "dest"), host="_sy_local", exclude=["*"])
    assert r["ok"] is False and "nothing to sync" in r["error"]


@needs_windows
def test_sync_rejects_a_non_directory(tools, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    r = tools["sync_folder"](str(f), str(tmp_path / "d"), host="_sy_local")
    assert r["ok"] is False and "not a directory" in r["error"]


@needs_windows
def test_file_upload_refuses_an_oversized_file(tools, tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 40000)
    dest = str(tmp_path / "out.bin")
    r = tools["file_upload"](str(big), dest, host="_sy_local", max_mb=0.01)
    assert r["ok"] is False and r["needs_confirmation"] is True
    assert r["total_mb"] > 0 and not os.path.exists(dest)


@needs_windows
def test_file_upload_reports_channel_and_throughput(tools, tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 2048)
    r = tools["file_upload"](str(f), str(tmp_path / "out.bin"), host="_sy_local")
    assert r["ok"] is True and r["bytes"] == 2048
    assert r["channel"] == "local" and r["seconds"] >= 0 and r["mbps"] >= 0
    assert r["verified"] is True and len(r["sha256"]) == 64


@needs_windows
def test_file_upload_rejects_a_missing_file(tools, tmp_path):
    r = tools["file_upload"](str(tmp_path / "nope.bin"), "x", host="_sy_local")
    assert r["ok"] is False and "not a file" in r["error"]


@needs_windows
def test_task_status_of_an_unknown_task_is_not_found(tools):
    r = tools["task_status"]("winrdp_no_such_task_xyz", host="_sy_local")
    assert r["status"] == "not_found"
    assert r["exists"] is False and r["running"] is False
    assert r["pids"] == [] and r["stdout"] is None
    assert "no task" in r["hint"]


@needs_windows
def test_kill_on_timeout_actually_kills_the_remote_process(tools, tmp_path):
    """The whole point: a timeout must not leave the work running.

    The script sleeps well past the deadline and only then writes `marker`. If the kill
    worked, the file never appears — and the call must unblock near the deadline rather
    than when the abandoned process happens to finish.
    """
    marker = tmp_path / "survived.txt"
    script = f"Start-Sleep -Seconds 25; Set-Content -LiteralPath '{marker}' -Value 'SURVIVED'"
    t0 = time.time()
    with pytest.raises(TransportError) as ei:
        tools["run_powershell"](script, host="_sy_local", timeout=3, kill_on_timeout=True)
    elapsed = time.time() - t0
    assert "kill_on_timeout=True" in str(ei.value)
    assert elapsed < 20, f"did not unblock near the deadline ({elapsed:.1f}s)"
    time.sleep(2)
    assert not marker.exists(), "the remote process survived the timeout"


@needs_windows
def test_without_kill_on_timeout_the_error_is_a_plain_timeout(tools):
    """Default behaviour is unchanged — and must not claim anything was killed."""
    with pytest.raises(Exception) as ei:
        tools["run_powershell"]("Start-Sleep -Seconds 20", host="_sy_local", timeout=2)
    assert "kill_on_timeout" not in str(ei.value)


def test_kill_on_timeout_is_rejected_for_as_user():
    ctx = Context()
    try:
        ctx.vault.add(Host(alias="_ku_local", host="localhost", transport="local"))
        with pytest.raises(ValueError, match="not supported with as_user"):
            ctx.exec_ps("$true", host="_ku_local", as_user=True, kill_on_timeout=True)
    finally:
        ctx.vault.remove("_ku_local")
        ctx.close()


@needs_windows
def test_task_status_rejects_injection(tools):
    from winrdp_mcp.tools import _validate as V
    with pytest.raises(V.ValidationError):
        tools["task_status"]('x"; shutdown /r')
