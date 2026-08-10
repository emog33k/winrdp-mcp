"""Execution context shared by every tool.

Holds the vault, resolves which host a call targets, caches open transports, and exposes
the three primitives tools actually use:

    exec_ps(body, host=, elevated=, as_user=)     -> raw ExecResult
    exec_json(body, host=, elevated=)             -> parsed JSON (dict/list/scalar)
    transport_for(host=)                          -> the live Transport

`host` is optional everywhere: omit it to target the *active* host (set with use_host).
"""

from __future__ import annotations

import base64
import binascii
import os
import threading
import time
from typing import Any, Optional

from . import elevation, log, ps
from .config import REMOTE_SECURE
from .provision import open_transport, provision
from .transports import ExecResult, Transport
from .vault import Host, Vault

_log = log.get("context")


class Context:
    def __init__(self, vault: Optional[Vault] = None, *, default_local: bool = False) -> None:
        self.vault = vault or Vault()
        self._transports: dict[str, Transport] = {}
        self._lock = threading.RLock()
        if default_local and self.vault.active is None:
            self.vault.add(Host(alias="local", host="localhost", transport="local"))

    # ---- host resolution -------------------------------------------------
    def resolve(self, host: Optional[str]) -> Host:
        if host:
            h = self.vault.get(host)
            if not h:
                raise KeyError(f"unknown host alias '{host}'. Use list_hosts / add_host.")
            return h
        h = self.vault.active
        if not h:
            raise KeyError("no active host. Add one with add_host and select it with use_host.")
        return h

    # ---- transport cache -------------------------------------------------
    def transport_for(self, host: Optional[str] = None) -> Transport:
        h = self.resolve(host)
        with self._lock:
            t = self._transports.get(h.alias)
            if t is not None:
                return t
        # Open outside the lock so concurrent first-connects (run_on_hosts fan-out) to
        # different boxes actually run in parallel instead of serializing.
        opened = open_transport(h)
        with self._lock:
            existing = self._transports.get(h.alias)
            if existing is not None:  # another thread won the race; keep theirs
                try:
                    opened.close()
                except Exception:
                    pass
                return existing
            self._transports[h.alias] = opened
            self.vault.update(h)  # persist resolved_transport
            return opened

    def invalidate(self, alias: str) -> None:
        with self._lock:
            t = self._transports.pop(alias, None)
        if t:
            try:
                t.close()
            except Exception:
                pass

    def provision(self, host: Optional[str] = None, **kw) -> dict:
        h = self.resolve(host)
        self.invalidate(h.alias)
        rep = provision(h, **kw)
        self.vault.update(h)
        return rep.to_dict()

    # ---- execution primitives -------------------------------------------
    def run_detached(self, script: str, *, host: Optional[str] = None) -> dict:
        """Launch a script fire-and-forget via a Scheduled Task — survives session close
        (outside the WinRM Job Object) and has loopback access. Returns task + log paths."""
        return elevation.run_detached(self.transport_for(host), script)

    def exec_ps(
        self,
        body_or_script: str,
        *,
        host: Optional[str] = None,
        wrap: bool = False,
        elevated: bool = False,
        as_user: bool = False,
        loopback: bool = False,
        timeout: int = 120,
    ) -> ExecResult:
        h = self.resolve(host)
        t = self.transport_for(host)
        script = ps.wrap_plain(body_or_script) if wrap else body_or_script
        mode = "as_user" if as_user else ("elevated" if elevated else "normal")
        start = time.time()
        try:
            if as_user:
                r = elevation.run_in_user_session(t, script, timeout=timeout)
                return r
            # loopback=True forces the Scheduled Task path even when the WinRM session is
            # already elevated: the network-logon token blocks outbound 127.0.0.1, but the
            # task's service/batch logon does not.
            if loopback or (elevated and not t.is_elevated()):
                er = elevation.run_elevated(t, script, timeout=timeout, run_as="SYSTEM")
                return ExecResult(er.stdout, er.stderr, er.rc)
            # Either not elevated, or the session already holds a full admin token — run
            # directly (over WinRM this is the fast, common case).
            return t.run_ps(script, timeout=timeout)
        except Exception:
            _log.warning("exec failed on %s [%s] after %.1fs", h.alias, mode, time.time() - start)
            raise
        finally:
            _log.debug("exec %s [%s via %s] %.1fs", h.alias, mode, t.name, time.time() - start)

    def _stage_secrets(self, t: Transport, secrets: dict) -> tuple[str, list[str]]:
        """Upload each secret to an admin-only temp file on the box and return a PS prelude
        that reads it back into a variable, plus the file paths to delete afterward.

        This keeps a plaintext secret (a new account password) OUT of the script body — and
        therefore off the target's process command line / Event 4688, where an inline
        ``ConvertTo-SecureString '<pw>'`` would otherwise persist. The file lives in
        REMOTE_TMP (locked to SYSTEM+Administrators by ensure_remote_dirs) and is deleted in
        a finally. Residual: over pure WinRM with no SMB/SFTP fast channel, the upload itself
        transits a command line transiently; a fast channel avoids even that.
        """
        # Ensure the secure dir exists and is locked to SYSTEM + Administrators only (SIDs are
        # locale-independent). ProgramData is world-readable by default; this dir is not.
        try:
            t.run_ps(
                f"$s={ps.ps_string(REMOTE_SECURE)};"
                "if(-not(Test-Path $s)){New-Item -ItemType Directory -Path $s -Force|Out-Null};"
                "& icacls $s /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' 2>$null|Out-Null",
                timeout=30,
            )
        except Exception:  # noqa: BLE001 — worst case the dir keeps default (still deleted after)
            pass
        prelude: list[str] = []
        paths: list[str] = []
        for name, value in secrets.items():
            log.register_secret(str(value))
            rid = binascii.hexlify(os.urandom(6)).decode()
            p = f"{REMOTE_SECURE}\\winrdp_sec_{rid}.b64"
            b64 = base64.b64encode(str(value).encode("utf-8")).decode("ascii")
            t.upload(b64.encode("ascii"), p)
            paths.append(p)
            prelude.append(
                f"${name}=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
                f"(Get-Content -LiteralPath {ps.ps_string(p)} -Raw)));"
            )
        return "".join(prelude), paths

    def _cleanup_paths(self, t: Transport, paths: list[str]) -> None:
        if not paths:
            return
        lits = ",".join(ps.ps_string(p) for p in paths)
        try:
            t.run_ps(f"Remove-Item -LiteralPath {lits} -Force -ErrorAction SilentlyContinue", timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def exec_json(
        self,
        body: str,
        *,
        host: Optional[str] = None,
        elevated: bool = False,
        depth: int = 6,
        timeout: int = 120,
        secrets: Optional[dict] = None,
    ) -> Any:
        """Run a JSON-emitting body (assigns ``$result``) and return parsed data.

        ``secrets`` (name -> plaintext) are staged to admin-only temp files on the box and
        exposed to the body as ``$name`` variables, so a password never appears inline in the
        script / command line. They are deleted after the run.
        """
        if secrets:
            t = self.transport_for(host)
            prelude, paths = self._stage_secrets(t, secrets)
            try:
                return self._exec_json(prelude + body, host=host, elevated=elevated,
                                       depth=depth, timeout=timeout)
            finally:
                self._cleanup_paths(t, paths)
        return self._exec_json(body, host=host, elevated=elevated, depth=depth, timeout=timeout)

    def _exec_json(self, body: str, *, host: Optional[str], elevated: bool, depth: int,
                   timeout: int) -> Any:
        script = ps.wrap_json(body, depth=depth)
        if elevated and not self.transport_for(host).is_elevated():
            t = self.transport_for(host)
            r = elevation.run_elevated(t, script, timeout=timeout)
            return ps.parse_json(r.stdout)
        # already-elevated session (or elevated not needed): run directly
        r = self.exec_ps(script, host=host, timeout=timeout)
        return ps.parse_json(r.stdout)

    def run_long(self, script: str, *, host: Optional[str] = None, timeout: int = 1800) -> ExecResult:
        """Run a long (minutes-scale) command detached, then poll for completion.

        A multi-minute command (a package install, a Windows Update) cannot be held open
        on a single WinRM request — a busy/small box drops the connection. This runs it in
        an independent one-shot Scheduled Task (SYSTEM) and polls a done-marker with short
        calls that survive a mid-install disconnect (the transport self-heals). Use for
        software installs and other long operations instead of a plain elevated exec.
        """
        t = self.transport_for(host)
        er = elevation.run_elevated(t, script, timeout=timeout, run_as="SYSTEM")
        return ExecResult(er.stdout, er.stderr, er.rc)

    def close(self) -> None:
        with self._lock:
            for t in self._transports.values():
                try:
                    t.close()
                except Exception:
                    pass
            self._transports.clear()
