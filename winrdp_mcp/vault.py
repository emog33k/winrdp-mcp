"""Encrypted host inventory / credential vault.

Passwords are encrypted at rest with Fernet (AES-128-CBC + HMAC). The key comes from
``$WINRDP_VAULT_KEY`` if set, otherwise a machine-local key file created 0600 on first
use. Everything else (host, port, user, transport hints) is stored in clear so the
inventory stays human-readable.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import config, log

# scrypt work factor for passphrase -> key derivation (n=2^15, r=8, p=1 ≈ 32 MiB).
_SCRYPT_N = 2 ** 15


def _harden_acl(path) -> None:
    """Restrict a file to the current user only (Windows). Best-effort but LOUD on failure —
    this guards the vault key / inventory."""
    if os.name != "nt":
        return
    try:
        import getpass
        import subprocess

        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                            f"{getpass.getuser()}:F"], capture_output=True, timeout=15)
        if r.returncode != 0:
            log.get("vault").warning("could not restrict ACL on %s (rc=%s): %s",
                                     path, r.returncode, r.stderr.decode("utf-8", "replace")[:200])
    except Exception as e:  # noqa: BLE001
        log.get("vault").warning("could not restrict ACL on %s: %s", path, e)


def _scrypt_key(passphrase: str, salt: bytes) -> bytes:
    raw = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1).derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _load_or_create_salt() -> bytes:
    sp = config.data_dir() / "vault.salt"
    if sp.exists():
        return sp.read_bytes()
    salt = os.urandom(16)
    fd = os.open(str(sp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt  # a salt is not secret; no ACL hardening needed


@dataclass
class Host:
    """A managed Windows box."""

    alias: str
    host: str
    username: str = ""
    password: str = ""  # kept in-memory clear; persisted encrypted
    domain: str = ""
    # transport preference: "auto" | "winrm" | "ssh" | "local"
    transport: str = "auto"
    winrm_port: int = config.WINRM_HTTP_PORT
    winrm_https_port: int = config.WINRM_HTTPS_PORT
    use_ssl: bool = False
    winrm_auth: str = "ntlm"  # ntlm | basic | credssp | kerberos
    ssh_port: int = config.SSH_PORT
    rdp_port: int = config.RDP_PORT
    # transport security posture (see transports.py):
    #   winrm_cert_validation: "ignore" (self-signed) | "validate" (production HTTPS)
    #   ssh_host_key_policy:    "auto" (trust-on-first-use) | "reject" (known_hosts only)
    winrm_cert_validation: str = "ignore"
    ssh_host_key_policy: str = "auto"
    # populated by provision(): which transport actually works right now
    resolved_transport: str = ""
    notes: str = ""
    tags: list = field(default_factory=list)  # for fan-out grouping

    def redacted(self) -> dict:
        d = asdict(self)
        d["password"] = "***" if self.password else ""
        return d


def _write_key_file(kp, key: bytes) -> None:
    """Write the Fernet key file such that the key bytes never exist on disk under the
    inherited (admin/world-readable) DACL: create empty, harden the ACL, THEN write."""
    fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name == "nt":
        os.close(fd)              # empty file only — no key material yet
        _harden_acl(kp)           # owner-only DACL before the key lands
        with open(kp, "wb") as f:
            f.write(key)
    else:
        try:
            os.write(fd, key)     # 0o600 at create-time is already owner-only on POSIX
        finally:
            os.close(fd)


def _load_fernet():
    """Return a Fernet (or MultiFernet) for the vault.

    Passphrase in WINRDP_VAULT_KEY is stretched with scrypt + a persisted per-install salt;
    a MultiFernet also carries the legacy unsalted-SHA256 key so inventories encrypted by an
    older version still decrypt (and re-encrypt to the strong key on the next save).
    """
    env = os.environ.get("WINRDP_VAULT_KEY")
    if env:
        eb = env.encode()
        try:
            return Fernet(eb)  # already a raw 32-byte urlsafe-b64 Fernet key: use verbatim
        except Exception:  # noqa: BLE001 — not a raw key → treat as a passphrase
            pass
        salt = _load_or_create_salt()
        primary = Fernet(_scrypt_key(env, salt))
        legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(eb).digest()))
        return MultiFernet([primary, legacy])  # decrypt: primary→legacy; encrypt: primary

    kp = config.key_path()
    if kp.exists():
        return Fernet(kp.read_bytes().strip())

    key = Fernet.generate_key()
    _write_key_file(kp, key)
    log.get("vault").warning(
        "using on-disk vault key %s (encryption-at-rest only guards ciphertext-only theft); "
        "set WINRDP_VAULT_KEY to a passphrase for stronger protection", kp)
    return Fernet(key)


class Vault:
    """Reads/writes the host inventory with transparent password encryption."""

    def __init__(self) -> None:
        self._fernet = _load_fernet()
        self._hosts: dict[str, Host] = {}
        self._active: Optional[str] = None
        self._lock = threading.RLock()  # guards _hosts / _active / save() across fan-out threads
        self.load()

    # ---- persistence -----------------------------------------------------
    def load(self) -> None:
        path = config.inventory_path()
        if not path.exists():
            self._hosts = {}
            self._active = None
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._active = raw.get("active")
        self._hosts = {}
        for item in raw.get("hosts", []):
            enc = item.pop("password_enc", "")
            h = Host(**{k: v for k, v in item.items() if k in Host.__dataclass_fields__})
            if enc:
                try:
                    h.password = self._fernet.decrypt(enc.encode()).decode("utf-8")
                except InvalidToken:
                    h.password = ""
            if h.password:
                log.register_secret(h.password)  # scrub it from any log line, exact match
            self._hosts[h.alias] = h

    def save(self) -> None:
        with self._lock:
            hosts = []
            for h in self._hosts.values():
                d = asdict(h)
                pw = d.pop("password", "")
                d["password_enc"] = self._fernet.encrypt(pw.encode("utf-8")).decode() if pw else ""
                hosts.append(d)
            payload = {"active": self._active, "hosts": hosts}
            path = config.inventory_path()
            # Unique temp name so two concurrent saves don't write the same file and race the
            # rename into a half-written inventory.
            tmp = path.with_suffix(f".{binascii.hexlify(os.urandom(4)).decode()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
            _harden_acl(path)  # owner-only DACL on the (encrypted) inventory

    # ---- CRUD ------------------------------------------------------------
    def add(self, host: Host, make_active: bool = True) -> None:
        with self._lock:
            if host.password:
                log.register_secret(host.password)
            self._hosts[host.alias] = host
            if make_active or self._active is None:
                self._active = host.alias
            self.save()

    def remove(self, alias: str) -> bool:
        with self._lock:
            if alias in self._hosts:
                del self._hosts[alias]
                if self._active == alias:
                    self._active = next(iter(self._hosts), None)
                self.save()
                return True
        return False

    def get(self, alias: str) -> Optional[Host]:
        return self._hosts.get(alias)

    def all(self) -> list[Host]:
        with self._lock:  # snapshot so a concurrent add/remove can't change the dict mid-iterate
            return list(self._hosts.values())

    @property
    def active(self) -> Optional[Host]:
        if self._active and self._active in self._hosts:
            return self._hosts[self._active]
        return None

    def set_active(self, alias: str) -> bool:
        with self._lock:
            if alias in self._hosts:
                self._active = alias
                self.save()
                return True
            return False

    def update(self, host: Host) -> None:
        with self._lock:
            self._hosts[host.alias] = host
            self.save()
