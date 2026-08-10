"""Encrypted host inventory / credential vault.

Passwords are encrypted at rest with Fernet (AES-128-CBC + HMAC). The key comes from
``$WINRDP_VAULT_KEY`` if set, otherwise a machine-local key file created 0600 on first
use. Everything else (host, port, user, transport hints) is stored in clear so the
inventory stays human-readable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from . import config, log


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


def _load_key() -> bytes:
    env = os.environ.get("WINRDP_VAULT_KEY")
    if env:
        # Accept either a raw Fernet key or an arbitrary passphrase.
        try:
            Fernet(env.encode())
            return env.encode()
        except (ValueError, Exception):
            digest = hashlib.sha256(env.encode("utf-8")).digest()
            return base64.urlsafe_b64encode(digest)

    kp = config.key_path()
    if kp.exists():
        return kp.read_bytes().strip()

    key = Fernet.generate_key()
    # Create owner-only from the start (avoids a world-readable window on POSIX).
    fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    if os.name == "nt":
        # chmod only toggles the read-only bit on Windows; tighten the DACL to owner-only.
        try:
            import getpass
            import subprocess

            subprocess.run(["icacls", str(kp), "/inheritance:r", "/grant:r",
                            f"{getpass.getuser()}:F"], capture_output=True, timeout=15)
        except Exception:
            pass
    log.get("vault").warning(
        "using on-disk vault key %s (encryption-at-rest only guards ciphertext-only theft); "
        "set WINRDP_VAULT_KEY to a passphrase for stronger protection", kp)
    return key


class Vault:
    """Reads/writes the host inventory with transparent password encryption."""

    def __init__(self) -> None:
        self._fernet = Fernet(_load_key())
        self._hosts: dict[str, Host] = {}
        self._active: Optional[str] = None
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
        hosts = []
        for h in self._hosts.values():
            d = asdict(h)
            pw = d.pop("password", "")
            d["password_enc"] = self._fernet.encrypt(pw.encode("utf-8")).decode() if pw else ""
            hosts.append(d)
        payload = {"active": self._active, "hosts": hosts}
        path = config.inventory_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ---- CRUD ------------------------------------------------------------
    def add(self, host: Host, make_active: bool = True) -> None:
        if host.password:
            log.register_secret(host.password)
        self._hosts[host.alias] = host
        if make_active or self._active is None:
            self._active = host.alias
        self.save()

    def remove(self, alias: str) -> bool:
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
        return list(self._hosts.values())

    @property
    def active(self) -> Optional[Host]:
        if self._active and self._active in self._hosts:
            return self._hosts[self._active]
        return None

    def set_active(self, alias: str) -> bool:
        if alias in self._hosts:
            self._active = alias
            self.save()
            return True
        return False

    def update(self, host: Host) -> None:
        self._hosts[host.alias] = host
        self.save()
