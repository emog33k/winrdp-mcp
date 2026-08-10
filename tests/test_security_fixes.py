"""Regression tests locking in the 0.1.1 security/reliability fixes.

Each test maps to a finding from the code audit; a future change that reintroduces the bug
fails here (all local — no remote box needed, so they run in CI).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging

import pytest

from winrdp_mcp import log, ps
from winrdp_mcp.transports import _CHUNK, _MAX_INLINE_PS, ExecResult, Transport


# --- #1 chunked-upload recursion --------------------------------------------
def test_chunk_write_stays_under_inline_threshold():
    """A single chunk-write must fit inline; otherwise the WinRM chunk loop would cross the
    staging threshold and re-upload → infinite recursion."""
    from winrdp_mcp.config import REMOTE_TMP

    tmp = REMOTE_TMP + r"\winrdp_ps_0123456789ab.ps1.b64"  # the staging path upload() uses
    script = (f"Add-Content -LiteralPath {ps.ps_string(tmp)} "
              f"-Value {ps.ps_string('A' * _CHUNK)} -NoNewline")
    assert len(script) < _MAX_INLINE_PS, (
        f"chunk write is {len(script)} chars, not < _MAX_INLINE_PS ({_MAX_INLINE_PS})")


class _Recorder(Transport):
    """Base-Transport.upload driver that records which exec path each call took."""
    name = "rec"

    def __init__(self):
        self.inline: list[str] = []
        self.normal: list[str] = []

    def run_ps(self, script, timeout=120):
        self.normal.append(script)
        return ExecResult("", "", 0)

    def _run_ps_inline(self, script, timeout=120, idempotent=True):
        self.inline.append(script)
        return ExecResult("", "", 0)


def test_upload_chunks_use_non_staging_inline_path():
    """The chunk writes must go through _run_ps_inline (never the staging-capable run_ps),
    so an upload can't re-enter staging on a transport that stages large scripts."""
    r = _Recorder()
    Transport.upload(r, b"x" * (3 * _CHUNK), r"C:\tmp\f.bin")
    assert any("Add-Content" in s for s in r.inline)
    assert not any("Add-Content" in s for s in r.normal)


# --- #2 / #4 / #13 argument-injection validation ----------------------------
def test_enum_rejects_injection_but_accepts_valid():
    from winrdp_mcp.tools import _validate as V

    with pytest.raises(V.ValidationError):
        V.enum("Information;Start-Process calc", {"Information", "Warning", "Error"}, "level")
    assert V.enum("Warning", {"Information", "Warning", "Error"}, "level") == "Warning"


def test_token_rejects_quote_breakout():
    from winrdp_mcp.tools import _validate as V

    with pytest.raises(V.ValidationError):
        V.token("x';Start-Process calc;'", "auth_key")
    assert V.token("abc-123.key", "auth_key") == "abc-123.key"


def test_ui_find_control_type_quoting_escapes():
    # the fix routes control_type through ps_string; an embedded quote is doubled, not a breakout
    frag = ps.ps_string("*." + "x'evil")
    assert frag == "'*.x''evil'"


# --- #3 provisioning no longer weakens the box ------------------------------
def test_enable_script_omits_weakening_settings():
    from winrdp_mcp.provision import ENABLE_WINRM_PS

    assert "AllowUnencrypted" not in ENABLE_WINRM_PS
    assert "Auth\\Basic" not in ENABLE_WINRM_PS
    assert "TrustedHosts" not in ENABLE_WINRM_PS
    assert "LocalAccountTokenFilterPolicy" in ENABLE_WINRM_PS  # the necessary one stays


# --- #5 secrets never inlined into the script -------------------------------
class _FakeTransport:
    def __init__(self):
        self.uploads: list[tuple[bytes, str]] = []

    def run_ps(self, script, timeout=120, idempotent=True):
        return ExecResult("", "", 0)

    def upload(self, data, remote_path, timeout=300):
        self.uploads.append((data, remote_path))


def test_stage_secrets_keeps_password_out_of_script():
    from winrdp_mcp.context import Context

    ctx = Context()
    fake = _FakeTransport()
    prelude, paths = ctx._stage_secrets(fake, {"pw": "S3cr3tP@ss"})
    assert "S3cr3tP@ss" not in prelude               # plaintext never in the script body
    assert "FromBase64String" in prelude and "Get-Content" in prelude
    assert paths and all("winrdp_sec_" in p for p in paths)
    data, _ = fake.uploads[0]
    assert data == base64.b64encode(b"S3cr3tP@ss")    # only base64 is shipped, in a file
    assert b"S3cr3tP@ss" not in data


# --- #7 log redaction scrubs lazy %s args -----------------------------------
def test_redaction_scrubs_rendered_args():
    log.register_secret("Sup3rSecretPw")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(log._RedactFilter())
    lg = logging.getLogger("winrdp.test_redact")
    lg.handlers = [handler]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.debug("connecting with password %s to box", "Sup3rSecretPw")
    out = buf.getvalue()
    assert "Sup3rSecretPw" not in out and "***" in out


# --- #8 vault KDF is salted scrypt with legacy migration --------------------
def test_kdf_scrypt_salted_and_multifernet_decrypts_legacy():
    from cryptography.fernet import Fernet, MultiFernet

    from winrdp_mcp import vault

    passphrase = "test-passphrase-not-secret"  # matches conftest WINRDP_VAULT_KEY
    salt = vault._load_or_create_salt()
    scrypt_key = vault._scrypt_key(passphrase, salt)
    legacy_key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
    assert scrypt_key != legacy_key  # salted derivation != old unsalted single SHA-256

    f = vault._load_fernet()
    assert isinstance(f, MultiFernet)  # passphrase path builds a MultiFernet
    legacy_ct = Fernet(legacy_key).encrypt(b"old-inventory-data")
    assert f.decrypt(legacy_ct) == b"old-inventory-data"  # old ciphertext still decrypts
