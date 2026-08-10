"""Unit tests for the PowerShell marshaling helpers."""

from __future__ import annotations

import base64

import pytest

from winrdp_mcp import ps


def test_encode_command_roundtrip():
    enc = ps.encode_command("Write-Output 42")
    assert base64.b64decode(enc).decode("utf-16-le") == "Write-Output 42"


def test_ps_string_escapes_single_quotes():
    assert ps.ps_string("it's a 'test'") == "'it''s a ''test'''"


def test_wrap_and_parse_json():
    body = "$result=@{a=1;b='x'}"
    wrapped = ps.wrap_json(body)
    assert ps.BEGIN in wrapped and ps.END in wrapped
    fake = f"noise\n{ps.BEGIN}\n{{\"a\":1,\"b\":\"x\"}}\n{ps.END}\nmore"
    assert ps.parse_json(fake) == {"a": 1, "b": "x"}


def test_parse_json_missing_raises():
    with pytest.raises(ValueError):
        ps.parse_json("no payload here")


def test_parse_json_empty_payload_is_none():
    assert ps.parse_json(f"{ps.BEGIN}\n\n{ps.END}") is None


def test_as_list_normalizes():
    assert ps.as_list(None) == []
    assert ps.as_list(5) == [5]
    assert ps.as_list([1, 2]) == [1, 2]
    assert ps.as_list({"a": 1}) == [{"a": 1}]
