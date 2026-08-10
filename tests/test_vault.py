"""Unit tests for the encrypted host inventory."""

from __future__ import annotations

from winrdp_mcp.vault import Host, Vault


def test_password_encrypts_and_roundtrips():
    v = Vault()
    v.add(Host(alias="a", host="1.2.3.4", username="Administrator", password="s3cret!"))
    v2 = Vault()  # fresh load from disk
    h = v2.get("a")
    assert h is not None
    assert h.password == "s3cret!"
    v2.remove("a")


def test_password_is_encrypted_on_disk():
    from winrdp_mcp import config

    v = Vault()
    v.add(Host(alias="enc", host="10.0.0.1", password="PlainSecret123"))
    raw = config.inventory_path().read_text(encoding="utf-8")
    assert "PlainSecret123" not in raw  # never stored in cleartext
    assert "password_enc" in raw
    v.remove("enc")


def test_redacted_hides_password():
    h = Host(alias="r", host="x", password="hunter2")
    assert h.redacted()["password"] == "***"


def test_active_host_switching():
    v = Vault()
    v.add(Host(alias="h1", host="a"), make_active=True)
    v.add(Host(alias="h2", host="b"), make_active=False)
    assert v.active.alias == "h1"
    assert v.set_active("h2")
    assert v.active.alias == "h2"
    assert not v.set_active("nope")
    v.remove("h1")
    v.remove("h2")


def test_remove_updates_active():
    v = Vault()
    v.add(Host(alias="only", host="a"))
    assert v.active.alias == "only"
    v.remove("only")
    assert v.active is None
