"""Execution transports: WinRM, SSH, Local, SMB.

Every transport exposes the same tiny surface:

    run_ps(script, timeout)      -> ExecResult      # run PowerShell, return stdout/stderr/rc
    run_cmd(command, timeout)    -> ExecResult      # run a cmd.exe command line
    upload(data, remote_path)    -> None            # push bytes to the box
    download(remote_path)        -> bytes           # pull bytes from the box
    probe()                      -> bool            # is this transport usable right now?
    close()                      -> None

Structured tools build a PowerShell body with :mod:`winrdp_mcp.ps`, call ``run_ps`` and
parse the delimited JSON. Nothing above the transport layer knows *how* the command got
to the box — that is the whole point.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import shlex
import socket
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from . import log, ps
from .config import REMOTE_TMP

_log = log.get("transport")

# WinRM per-operation timeouts (seconds). read_timeout must exceed operation_timeout.
WINRM_OP_TIMEOUT = int(os.environ.get("WINRDP_WINRM_OP_TIMEOUT", "180"))
WINRM_READ_TIMEOUT = WINRM_OP_TIMEOUT + 30


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    rc: int

    def __post_init__(self) -> None:
        # Tidy PowerShell CLIXML streams: real Error/Warning text stays in stderr,
        # Write-Host/Information output is recovered into stdout, progress noise is dropped.
        # No-op for plain (non-CLIXML) stderr.
        self.stdout, self.stderr = ps.split_ps_streams(self.stdout, self.stderr)

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def raise_for_status(self, what: str = "command") -> "ExecResult":
        if not self.ok:
            # Box output can echo a secret (a command line, a credential in an error). This
            # message becomes `error: str(e)` handed to the model — scrub known secrets first.
            msg = log.redact((self.stderr or self.stdout or "").strip())
            raise TransportError(f"{what} failed (rc={self.rc}): {msg[:2000]}")
        return self


class TransportError(RuntimeError):
    pass


def port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Upload chunk size (base64 chars). Each chunk rides inside a run_ps whose script pywinrm
# re-encodes to UTF-16LE base64 (~2.7x growth) onto a single command line; keep the chunk
# small enough that the encoded command stays well under the WSMan/cmd command-line limit.
# MUST stay well below _MAX_INLINE_PS: the chunk write is `Add-Content ... -Value '<chunk>'`
# (~110 chars of scaffolding + path), and if that script crossed _MAX_INLINE_PS the WinRM
# transport would try to STAGE it — which uploads via this very chunk loop → infinite
# recursion. The chunk loop uses _run_ps_inline (never stages), and this headroom is the
# second guard.
_CHUNK = 1600

# A run_ps script longer than this is staged to a temp .ps1 and run via `-File` instead of
# being packed onto one command line. pywinrm runs `powershell -EncodedCommand <base64>`,
# and the script re-encodes to UTF-16LE base64 (~2.7x). Keep the encoded command well under
# the conservative 8191 cmd-line limit some boxes enforce: 2400 text -> ~6.4k encoded.
# Staging uses the fast file channel (SMB/SFTP), so it stays cheap.
_MAX_INLINE_PS = 2400


class Transport:
    """Abstract transport."""

    name = "base"
    _elevated: Optional[bool] = None  # cached: does this session already hold a full token?

    def is_elevated(self) -> bool:
        """True if the current session already runs with a full (elevated) admin token.

        Over WinRM a local admin with LocalAccountTokenFilterPolicy=1 already gets a
        high-integrity token, so scheduled-task SYSTEM elevation is unnecessary — callers
        can run privileged commands directly. Result is cached per transport.
        """
        if self._elevated is None:
            try:
                r = self.run_ps(
                    "if(([Security.Principal.WindowsPrincipal]"
                    "[Security.Principal.WindowsIdentity]::GetCurrent())."
                    "IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
                    "{'ELEV_YES'}else{'ELEV_NO'}",
                    timeout=30,
                )
                self._elevated = "ELEV_YES" in r.stdout
            except Exception:
                self._elevated = False
        return self._elevated

    def run_ps(self, script: str, timeout: int = 120) -> ExecResult:  # pragma: no cover
        raise NotImplementedError

    def _run_ps_inline(self, script: str, timeout: int = 120, idempotent: bool = True) -> ExecResult:
        """Run a short script WITHOUT the large-script staging path.

        The base :meth:`upload` chunk loop calls this so a transport that stages large
        scripts (WinRM) can never re-enter staging from inside an upload (which would
        recurse forever). Transports that never stage just run it directly.
        """
        return self.run_ps(script, timeout=timeout)

    def run_cmd(self, command: str, timeout: int = 120) -> ExecResult:  # pragma: no cover
        raise NotImplementedError

    def upload(self, data: bytes, remote_path: str, timeout: int = 300) -> None:
        """Default upload: stream base64 chunks through PowerShell (works everywhere)."""
        self.run_ps(
            f"$p={ps.ps_string(remote_path)};"
            "$d=Split-Path $p -Parent;"
            "if($d -and -not(Test-Path $d)){New-Item -ItemType Directory -Path $d -Force|Out-Null};"
            "if(Test-Path $p){Remove-Item -LiteralPath $p -Force}",
            timeout=timeout,
        ).raise_for_status("prepare upload")

        if not data:  # zero-byte file: create it directly, skip the chunk loop
            self.run_ps(
                f"[IO.File]::WriteAllBytes({ps.ps_string(remote_path)},([byte[]]@()))",
                timeout=timeout,
            ).raise_for_status("write empty file")
            return

        b64 = base64.b64encode(data).decode("ascii")
        tmp = remote_path + ".b64"
        # write the base64 text in chunks
        self._run_ps_inline(
            f"if(Test-Path {ps.ps_string(tmp)}){{Remove-Item -LiteralPath {ps.ps_string(tmp)} -Force}}",
            idempotent=False,
        ).raise_for_status()
        for i in range(0, len(b64), _CHUNK):
            chunk = b64[i:i + _CHUNK]
            # _run_ps_inline (not run_ps): a chunk write must never trip the staging
            # threshold — that would upload via this same loop and recurse. idempotent=False:
            # a blind reconnect-retry would append the chunk twice and corrupt the file.
            self._run_ps_inline(
                f"Add-Content -LiteralPath {ps.ps_string(tmp)} -Value {ps.ps_string(chunk)} -NoNewline",
                timeout=timeout, idempotent=False,
            ).raise_for_status("write chunk")
        self.run_ps(
            f"$b=[Convert]::FromBase64String((Get-Content -LiteralPath {ps.ps_string(tmp)} -Raw));"
            f"[IO.File]::WriteAllBytes({ps.ps_string(remote_path)},$b);"
            f"Remove-Item -LiteralPath {ps.ps_string(tmp)} -Force",
            timeout=timeout,
        ).raise_for_status("finalize upload")

    def download(self, remote_path: str, timeout: int = 300) -> bytes:
        """Default download: base64-encode on the box, decode locally."""
        r = self.run_ps(
            f"[Convert]::ToBase64String([IO.File]::ReadAllBytes({ps.ps_string(remote_path)}))",
            timeout=timeout,
        ).raise_for_status("download")
        return base64.b64decode(r.stdout.strip())

    def probe(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# WinRM
# ---------------------------------------------------------------------------
class WinRMTransport(Transport):
    name = "winrm"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        use_ssl: bool = False,
        port: Optional[int] = None,
        auth: str = "ntlm",
        domain: str = "",
        cert_validation: str = "ignore",
    ) -> None:
        self.host = host
        self._use_ssl = use_ssl
        self.port = port or (5986 if use_ssl else 5985)
        proto = "https" if use_ssl else "http"
        self._endpoint = f"{proto}://{host}:{self.port}/wsman"
        self._user = f"{domain}\\{username}" if domain else username
        self._password = password
        self._auth = auth
        self._cert_validation = cert_validation
        self._fchan = None  # lazy fast file channel: None=untried, False=none, else (kind,obj)
        # pywinrm's Session (requests.Session + the open WSMan shell) is NOT thread-safe:
        # two concurrent calls on one host would interleave and wedge the shell (HTTP 400
        # cascade). Serialize all use of this session; different hosts hold different locks,
        # so cross-host fan-out still runs in parallel. Reentrant: upload() calls run_ps().
        self._oplock = threading.RLock()
        self._connect()
        if use_ssl and cert_validation == "ignore":
            _log.warning("winrm %s: HTTPS with cert validation DISABLED (MITM-exploitable); "
                         "set the host's winrm_cert_validation='validate' in production", host)

    def _connect(self) -> None:
        import winrm  # imported lazily so core import stays cheap

        self._session = winrm.Session(
            self._endpoint,
            auth=(self._user, self._password),
            transport=self._auth,
            server_cert_validation=self._cert_validation,
            operation_timeout_sec=WINRM_OP_TIMEOUT,
            read_timeout_sec=WINRM_READ_TIMEOUT,
        )
        _log.debug("winrm session -> %s (%s auth)", self._endpoint, self._auth)

    @staticmethod
    def _is_recoverable(exc: Exception) -> bool:
        """A dropped/wedged connection (transient) vs. a real remote error.

        A remote OperationTimeout / overload closes the socket (RemoteDisconnected) or
        leaves the WSMan shell in a state that answers every later request with HTTP 400.
        Both are cured by tearing down the pywinrm Session and reconnecting.
        """
        import requests

        if isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.Timeout)):
            return True
        # pywinrm WinRMTransportError carries the HTTP code; 400/500 on a previously-good
        # session means the shell/connection is wedged.
        code = getattr(exc, "code", None)
        if code in (400, 500):
            return True
        msg = str(exc)
        return "Code 400" in msg or "Code 500" in msg or "RemoteDisconnected" in msg

    def _run(self, fn, *, idempotent: bool = True):
        """Run a pywinrm call, reconnecting + retrying once on a recoverable error.

        The retry is only safe for idempotent calls (reads/probes): a remote OperationTimeout
        can close the socket AFTER the command already executed, so a blind retry of a
        mutating call (create user, append a file chunk) would double-apply it. Mutating
        callers pass idempotent=False — a recoverable error still reconnects the session for
        the NEXT call, but the failing operation propagates instead of silently re-running.
        """
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if not self._is_recoverable(e):
                raise
            if not idempotent:
                _log.warning("winrm %s: recoverable error (%s) on a non-idempotent call; "
                             "reconnecting for next call, not retrying", self.host, type(e).__name__)
                try:
                    self._connect()
                except Exception:  # noqa: BLE001
                    pass
                raise
            _log.warning("winrm %s: recoverable error (%s); reconnecting + retrying once",
                         self.host, type(e).__name__)
            self._connect()
            return fn()  # a second failure propagates

    def _apply_timeout(self, timeout: int) -> None:
        """Best-effort: bound this call by the caller's timeout instead of only the
        construction-time default, so a wedged connection can't block for the full
        read-timeout regardless of a short per-call budget (e.g. a waiter poll)."""
        try:
            op = max(20, min(int(timeout), WINRM_OP_TIMEOUT))
            self._session.protocol.operation_timeout_sec = op
            self._session.protocol.transport.read_timeout_sec = op + 30
        except Exception:  # noqa: BLE001
            pass

    def run_ps(self, script: str, timeout: int = 120, idempotent: bool = True) -> ExecResult:
        # NOTE: no thread-based watchdog here. pywinrm's Session (requests.Session + the open
        # WSMan shell) is not thread-safe; abandoning a call mid-flight corrupts it and every
        # later request returns HTTP 400. Bounding comes from read_timeout_sec; a dropped or
        # wedged connection is healed by _run reconnecting and retrying once. All session use
        # is serialized by _oplock (reentrant, so the staging path below re-enters safely).
        if len(script) > _MAX_INLINE_PS:
            # pywinrm packs run_ps as `powershell -EncodedCommand <base64>` on ONE command
            # line; a big script overflows the length limit. Stage it and run via -File.
            return self._run_ps_via_file(script, timeout)
        _log.debug("winrm run_ps %s: %s", self.host, script[:120].replace("\n", " "))
        with self._oplock:
            self._apply_timeout(timeout)
            r = self._run(lambda: self._session.run_ps(script), idempotent=idempotent)
        return ExecResult(
            stdout=r.std_out.decode("utf-8", "replace"),
            stderr=r.std_err.decode("utf-8", "replace"),
            rc=r.status_code,
        )

    def _run_ps_inline(self, script: str, timeout: int = 120, idempotent: bool = True) -> ExecResult:
        # Same as run_ps but NEVER stages to a file: used by the base upload chunk loop so an
        # upload can't re-enter staging (which uploads → recurses). Chunks are < _MAX_INLINE_PS.
        with self._oplock:
            self._apply_timeout(timeout)
            r = self._run(lambda: self._session.run_ps(script), idempotent=idempotent)
        return ExecResult(
            stdout=r.std_out.decode("utf-8", "replace"),
            stderr=r.std_err.decode("utf-8", "replace"),
            rc=r.status_code,
        )

    def _run_ps_via_file(self, script: str, timeout: int) -> ExecResult:
        rid = binascii.hexlify(os.urandom(6)).decode()
        remote = f"{REMOTE_TMP}\\winrdp_ps_{rid}.ps1"
        # Hold the session lock across upload + exec + cleanup so another thread can't
        # interleave on this shell mid-stage. Reentrant: upload() re-acquires it.
        with self._oplock:
            # UTF-8 BOM so `-File` decodes correctly on 5.1 and 7; upload chunks stay small.
            self.upload(script.encode("utf-8-sig"), remote)
            try:
                self._apply_timeout(timeout)
                # Executing a staged script is not idempotent — don't blind-retry it.
                r = self._run(lambda: self._session.run_cmd(
                    "powershell", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", remote]),
                    idempotent=False)
                return ExecResult(
                    stdout=r.std_out.decode("utf-8", "replace"),
                    stderr=r.std_err.decode("utf-8", "replace"),
                    rc=r.status_code,
                )
            finally:
                try:
                    self._run(lambda: self._session.run_ps(
                        f"Remove-Item -LiteralPath {ps.ps_string(remote)} -Force -ErrorAction SilentlyContinue"),
                        idempotent=False)
                except Exception:
                    pass

    def run_cmd(self, command: str, timeout: int = 120, idempotent: bool = False) -> ExecResult:
        # A cmd.exe command line is assumed mutating (shutdown, netsh, schtasks, sc): don't
        # blind-retry it on a mid-flight drop. Callers with a read-only command can opt in.
        with self._oplock:
            self._apply_timeout(timeout)
            r = self._run(lambda: self._session.run_cmd("cmd.exe", ["/c", command]), idempotent=idempotent)
        return ExecResult(
            stdout=r.std_out.decode("utf-8", "replace"),
            stderr=r.std_err.decode("utf-8", "replace"),
            rc=r.status_code,
        )

    def _fast_channel(self):
        """Pick (once, cached) the fastest file channel to the box: SMB admin share, else
        SFTP over SSH, else None (caller falls back to chunked base64 over run_ps).

        Chunked run_ps is many round-trips; SMB/SFTP move a file in one shot. SMB needs no
        install (445 + ADMIN$); SFTP needs OpenSSH (which provisioning can enable)."""
        if self._fchan is not None:
            return self._fchan or None
        from .config import SMB_PORT, SSH_PORT
        dom = self._user.split("\\")[0] if "\\" in self._user else ""
        usr = self._user.split("\\")[-1]
        # 1) SMB admin share — no install required
        try:
            if port_open(self.host, SMB_PORT, timeout=2):
                smb = SMBFiles(self.host, usr, self._password, dom)
                if smb.probe():
                    self._fchan = ("smb", smb)
                    _log.debug("winrm %s: fast file channel = SMB", self.host)
                    return self._fchan
        except Exception as e:  # noqa: BLE001
            _log.debug("winrm %s: SMB channel unavailable: %s", self.host, e)
        # 2) SFTP over SSH
        try:
            if port_open(self.host, SSH_PORT, timeout=2):
                ssh = SSHTransport(self.host, usr, self._password, port=SSH_PORT)
                self._fchan = ("sftp", ssh)
                _log.debug("winrm %s: fast file channel = SFTP", self.host)
                return self._fchan
        except Exception as e:  # noqa: BLE001
            _log.debug("winrm %s: SFTP channel unavailable: %s", self.host, e)
        self._fchan = False
        return None

    def _drop_fast_channel(self) -> None:
        """Close and forget the current fast file channel (so it isn't leaked when it fails
        and we fall back to chunked base64)."""
        ch = self._fchan
        self._fchan = False
        if isinstance(ch, tuple):
            try:
                ch[1].close()
            except Exception:  # noqa: BLE001
                pass

    def upload(self, data: bytes, remote_path: str, timeout: int = 300) -> None:
        with self._oplock:
            ch = self._fast_channel()
            if ch:
                kind, obj = ch
                try:
                    obj.write(data, remote_path) if kind == "smb" else obj.upload(data, remote_path, timeout)
                    return
                except Exception as e:  # noqa: BLE001
                    _log.debug("winrm %s: fast upload failed (%s); chunked fallback", self.host, e)
                    self._drop_fast_channel()
            super().upload(data, remote_path, timeout=timeout)

    def download(self, remote_path: str, timeout: int = 300) -> bytes:
        with self._oplock:
            ch = self._fast_channel()
            if ch:
                kind, obj = ch
                try:
                    return obj.read(remote_path) if kind == "smb" else obj.download(remote_path, timeout)
                except Exception as e:  # noqa: BLE001
                    _log.debug("winrm %s: fast download failed (%s); base64 fallback", self.host, e)
                    self._drop_fast_channel()
            return super().download(remote_path, timeout=timeout)

    def close(self) -> None:
        # Close whichever fast channel we opened — SFTP (paramiko client + socket + thread)
        # OR SMB (a process-global smbclient session). Both were leaked before.
        if isinstance(self._fchan, tuple):
            try:
                self._fchan[1].close()
            except Exception:  # noqa: BLE001
                pass
            self._fchan = None

    def probe(self) -> bool:
        try:
            r = self.run_ps("$true", timeout=15)
            return r.ok
        except Exception:
            return False


# ---------------------------------------------------------------------------
# SSH (Windows OpenSSH)
# ---------------------------------------------------------------------------
class SSHTransport(Transport):
    name = "ssh"

    def __init__(
        self,
        host: str,
        username: str,
        password: str = "",
        *,
        port: int = 22,
        key_filename: Optional[str] = None,
        host_key_policy: str = "auto",
    ) -> None:
        import paramiko

        self.host = host
        self.port = port
        self._oplock = threading.RLock()  # serialize channel/SFTP use (paramiko isn't safe under races)
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        # host_key_policy: "auto" = trust-on-first-use (pragmatic for fresh boxes),
        # "reject" = only accept keys already in known_hosts (production).
        policy = paramiko.RejectPolicy() if host_key_policy == "reject" else paramiko.AutoAddPolicy()
        self._client.set_missing_host_key_policy(policy)
        self._client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password or None,
            key_filename=key_filename,
            look_for_keys=bool(key_filename),
            allow_agent=bool(key_filename),
            timeout=15,
        )

    def run_ps(self, script: str, timeout: int = 120) -> ExecResult:
        if len(script) > _MAX_INLINE_PS:
            return self._run_ps_via_file(script, timeout)
        enc = ps.encode_command(script)
        return self._exec(f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}", timeout)

    def _run_ps_via_file(self, script: str, timeout: int) -> ExecResult:
        rid = binascii.hexlify(os.urandom(6)).decode()
        remote = f"{REMOTE_TMP}\\winrdp_ps_{rid}.ps1"
        self.upload(script.encode("utf-8-sig"), remote)  # SFTP — no length limit
        try:
            return self._exec(
                f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{remote}"', timeout)
        finally:
            try:
                self._exec(f'powershell -NoProfile -Command "Remove-Item -LiteralPath '
                           f'\'{remote}\' -Force -ErrorAction SilentlyContinue"', 30)
            except Exception:
                pass

    def run_cmd(self, command: str, timeout: int = 120) -> ExecResult:
        # The SSH DefaultShell is set to PowerShell by provisioning, so a bare command
        # would run under PowerShell (wrong quoting/`$` interpolation for cmd builtins like
        # schtasks/shutdown/netsh). Force real cmd.exe semantics by invoking it explicitly.
        return self.run_ps(f"& $env:ComSpec /c {ps.ps_string(command)}", timeout)

    def _exec(self, command: str, timeout: int) -> ExecResult:
        with self._oplock:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        return ExecResult(out, err, rc)

    def upload(self, data: bytes, remote_path: str, timeout: int = 300) -> None:
        # Ensure parent dir, then SFTP the bytes — far faster than base64 chunking.
        self.run_ps(
            f"$d=Split-Path {ps.ps_string(remote_path)} -Parent;"
            "if($d -and -not(Test-Path $d)){New-Item -ItemType Directory -Path $d -Force|Out-Null}"
        ).raise_for_status("mkdir for upload")
        with self._oplock:
            sftp = self._client.open_sftp()
            try:
                sftp.putfo(io.BytesIO(data), remote_path.replace("\\", "/"))
            finally:
                sftp.close()

    def download(self, remote_path: str, timeout: int = 300) -> bytes:
        with self._oplock:
            sftp = self._client.open_sftp()
            try:
                buf = io.BytesIO()
                sftp.getfo(remote_path.replace("\\", "/"), buf)
                return buf.getvalue()
            finally:
                sftp.close()

    def probe(self) -> bool:
        try:
            return self.run_ps("$true", timeout=15).ok
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local (agent mode / managing the box we run on)
# ---------------------------------------------------------------------------
class LocalTransport(Transport):
    name = "local"

    def __init__(self, host: str = "localhost") -> None:
        self.host = host

    def _powershell_exe(self) -> str:
        return "powershell.exe" if os.name == "nt" else "pwsh"

    def run_ps(self, script: str, timeout: int = 120) -> ExecResult:
        if len(script) > _MAX_INLINE_PS:
            return self._run_ps_via_file(script, timeout)
        enc = ps.encode_command(script)
        proc = subprocess.run(
            [self._powershell_exe(), "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            capture_output=True,
            timeout=timeout,
        )
        return ExecResult(
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            proc.returncode,
        )

    def _run_ps_via_file(self, script: str, timeout: int) -> ExecResult:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".ps1")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(script.encode("utf-8-sig"))
        try:
            proc = subprocess.run(
                [self._powershell_exe(), "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-File", path],
                capture_output=True, timeout=timeout,
            )
            return ExecResult(
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"),
                proc.returncode,
            )
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def run_cmd(self, command: str, timeout: int = 120) -> ExecResult:
        if os.name == "nt":
            proc = subprocess.run(["cmd.exe", "/c", command], capture_output=True, timeout=timeout)
        else:
            proc = subprocess.run(shlex.split(command), capture_output=True, timeout=timeout)
        return ExecResult(
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            proc.returncode,
        )

    def upload(self, data: bytes, remote_path: str, timeout: int = 300) -> None:
        os.makedirs(os.path.dirname(remote_path) or ".", exist_ok=True)
        with open(remote_path, "wb") as f:
            f.write(data)

    def download(self, remote_path: str, timeout: int = 300) -> bytes:
        with open(remote_path, "rb") as f:
            return f.read()

    def probe(self) -> bool:
        return os.name == "nt"


# ---------------------------------------------------------------------------
# SMB file transport (no exec) — used to stage the cold-start bootstrap
# ---------------------------------------------------------------------------
class SMBFiles:
    """File push/pull over the admin share (``ADMIN$`` / ``C$``) using smbprotocol.

    This has *no* command execution; it is the file half of a cold-start where WinRM and
    SSH are both off. Execution of the staged bootstrap is done by :mod:`winrdp_mcp.provision`
    via DCOM/WMI (impacket) or a scheduled-task trigger.
    """

    def __init__(self, host: str, username: str, password: str, domain: str = "",
                 connection_timeout: int = 8) -> None:
        import smbclient

        self.host = host
        self._smbclient = smbclient
        user = f"{domain}\\{username}" if domain else username
        # Bound the SMB negotiation: register_session defaults to a 60s timeout, so a box with
        # 445 open at the TCP layer but SMB filtered (common on cloud VDS) would hang the whole
        # fast-channel probe for a minute before falling back to SFTP/base64. Fail fast instead.
        smbclient.register_session(host, username=user, password=password,
                                   connection_timeout=connection_timeout)

    def _unc(self, windows_path: str) -> str:
        # C:\ProgramData\x -> \\host\C$\ProgramData\x
        drive, _, rest = windows_path.partition(":")
        rest = rest.lstrip("\\")
        return rf"\\{self.host}\{drive}$\{rest}"

    def write(self, data: bytes, windows_path: str) -> None:
        unc = self._unc(windows_path)
        parent = unc.rsplit("\\", 1)[0]
        try:
            self._smbclient.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with self._smbclient.open_file(unc, mode="wb") as f:
            f.write(data)

    def read(self, windows_path: str) -> bytes:
        with self._smbclient.open_file(self._unc(windows_path), mode="rb") as f:
            return f.read()

    def probe(self) -> bool:
        try:
            self._smbclient.listdir(rf"\\{self.host}\C$")
            return True
        except Exception:
            return False

    def close(self) -> None:
        # register_session registers a PROCESS-GLOBAL connection; without this it lives until
        # process exit (a leak when used as a WinRM fast channel or for cold-start staging).
        try:
            self._smbclient.delete_session(self.host)
        except Exception:  # noqa: BLE001
            pass


def wmi_exec(host: str, username: str, password: str, command: str, domain: str = "") -> None:
    """Fire-and-forget command execution over DCOM/WMI (Win32_Process.Create).

    Requires the optional ``impacket`` dependency and TCP 135 + high RPC ports open.
    Used only to bootstrap WinRM on a box where nothing else is reachable.
    """
    from impacket.dcerpc.v5.dcom import wmi
    from impacket.dcerpc.v5.dcomrt import DCOMConnection
    from impacket.dcerpc.v5.dtypes import NULL

    dcom = DCOMConnection(host, username, password, domain, "", "", None, oxidResolver=True)
    try:
        iinterface = dcom.CoCreateInstanceEx(wmi.CLSID_WbemLevel1Login, wmi.IID_IWbemLevel1Login)
        login = wmi.IWbemLevel1Login(iinterface)
        services = login.NTLMLogin("//./root/cimv2", NULL, NULL)
        login.RemRelease()
        win32process, _ = services.GetObject("Win32_Process")
        win32process.Create(command, r"C:\Windows\System32", None)
    finally:
        dcom.disconnect()
