"""process_details / service_info / Defender tool shape — against a real local box."""

from __future__ import annotations

import os

import pytest

from winrdp_mcp import ps
from winrdp_mcp.tools import admin, windows
from winrdp_mcp.transports import LocalTransport

pytestmark = pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


class _Ctx:
    def __init__(self):
        self.t = LocalTransport()

    def exec_json(self, body, host=None, elevated=False, timeout=120, **kw):
        return ps.parse_json(self.t.run_ps(ps.wrap_json(body), timeout=timeout).stdout)


@pytest.fixture
def tools():
    ctx = _Ctx()
    m = _FakeMCP()
    admin.register(m, ctx)
    windows.register(m, ctx)
    return m.fns


def test_process_details_has_parent_and_command_line(tools):
    r = tools["process_details"](pid=os.getpid())
    p = r["processes"][0]
    assert p["pid"] == os.getpid()
    assert p["command_line"] and p["executable"]
    assert p["ppid"] and p["parent_name"]
    assert p["cwd"] is None and "cwd_note" in r


def test_process_details_by_name_finds_matches(tools):
    r = tools["process_details"](name="python.exe")
    assert "processes" in r and r["processes"]
    assert all(p["name"].lower() == "python.exe" for p in r["processes"])


def test_service_info_exposes_the_nssm_parameter_set(tools):
    r = tools["service_info"]("WinRM")
    assert r["exists"] is True
    assert {"image_path", "path_name", "object_name", "app_directory", "app_parameters",
            "app_environment", "is_nssm"} <= set(r)


def test_defender_exclusion_remove_needs_a_target(tools):
    assert "provide path" in tools["defender_exclusion_remove"]()["error"].lower()


def test_defender_quarantine_restore_needs_a_target(tools):
    assert "provide threat_name" in tools["defender_quarantine_restore"]()["error"]


def test_new_tools_have_safe_annotations():
    from winrdp_mcp.server import DESTRUCTIVE, READONLY, _annotations_for

    for name in ("process_details", "service_info", "defender_exclusions",
                 "defender_quarantine"):
        assert name in READONLY
        assert _annotations_for(name).readOnlyHint is True
    for name in ("defender_exclusion_remove", "defender_quarantine_restore",
                 "defender_history_clear"):
        assert name in DESTRUCTIVE
        assert _annotations_for(name).destructiveHint is True
