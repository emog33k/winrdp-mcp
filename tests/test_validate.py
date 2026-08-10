"""Argument validation used to block command-line injection in structured tools."""

from __future__ import annotations

import pytest

from winrdp_mcp.tools import _validate as V


def test_enum():
    assert V.enum("Inbound", {"Inbound", "Outbound"}, "direction") == "Inbound"
    with pytest.raises(V.ValidationError):
        V.enum("In; evil", {"Inbound", "Outbound"}, "direction")


def test_host_rejects_shell_meta():
    assert V.host("10.0.0.5") == "10.0.0.5"
    assert V.host("box-01.corp.local")
    for bad in ["10.0.0.5; calc", "$(whoami)", "a b", "x`y", "1.2.3.4|nc"]:
        with pytest.raises(V.ValidationError):
            V.host(bad)


def test_obj_name_rejects_quotes_and_meta():
    assert V.obj_name("MyTask_1")
    for bad in ['a"b', "a\r\nb", "a|b", "a&b", "a;b", "a`b", "a%b"]:
        with pytest.raises(V.ValidationError):
            V.obj_name(bad)


def test_package_charset():
    assert V.package("Google.Chrome")
    assert V.package("googlechrome")
    with pytest.raises(V.ValidationError):
        V.package("pkg; rm -rf /")


def test_token():
    assert V.token("console")
    with pytest.raises(V.ValidationError):
        V.token("a b")
