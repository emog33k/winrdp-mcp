"""Tool behaviour against a real localhost box — skipped off Windows.

These exercise the hardening fixes end-to-end: BOM-less append, qwinsta parsing, and
injection rejection through the actual registered tool functions.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from winrdp_mcp.context import Context
from winrdp_mcp.tools import admin, files, rdp
from winrdp_mcp.tools import _validate as V
from winrdp_mcp.vault import Host

pytestmark = pytest.mark.skipif(os.name != "nt", reason="needs Windows PowerShell")


class _FakeMCP:
    def __init__(self):
        self.fns = {}

    def tool(self, fn):
        self.fns[fn.__name__] = fn
        return fn


@pytest.fixture
def tools():
    ctx = Context()
    ctx.vault.add(Host(alias="_tl_local", host="localhost", transport="local"))
    m = _FakeMCP()
    for mod in (files, admin, rdp):
        mod.register(m, ctx)
    yield m.fns
    ctx.vault.remove("_tl_local")  # don't pollute the shared test inventory
    ctx.close()


def test_file_append_has_no_bom(tools):
    p = os.path.join(tempfile.gettempdir(), "winrdp_test_append.txt")
    if os.path.exists(p):
        os.remove(p)
    try:
        tools["file_write"](p, "AB", append=True)
        tools["file_write"](p, "CD", append=True)
        raw = open(p, "rb").read()
        assert raw == b"ABCD"
        assert raw[:3] != b"\xef\xbb\xbf"
    finally:
        if os.path.exists(p):
            os.remove(p)


def test_rdp_sessions_parses_blank_username(tools):
    rows = tools["rdp_sessions"]()
    assert isinstance(rows, list) and rows
    # every row must have all four keys; ids are numeric strings
    for r in rows:
        assert set(r) == {"session", "user", "id", "state"}
    # at least one listener/disconnected row has a numeric id and a state token
    assert any(r["id"].isdigit() and r["state"] for r in rows)


def test_task_delete_rejects_injection(tools):
    with pytest.raises(V.ValidationError):
        tools["task_delete"]('x"; shutdown /r')
