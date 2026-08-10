"""SSH port-forwarding — reach a service bound to a box's 127.0.0.1 from the operator.

Processes launched over a WinRM network logon can't see the interactive user's loopback,
which blocks "test the box's localhost HTTP" workflows. An SSH local tunnel solves it: it
opens a port on the operator machine that forwards, through the box's SSH, to the box's
127.0.0.1:<remote_port>. Requires OpenSSH on the box (provisioning can install it).
"""

from __future__ import annotations

import atexit
import select
import socketserver
import threading
from typing import Optional

from .. import log

_log = log.get("tunnel")
_TUNNELS: dict[int, "_Tunnel"] = {}
_LOCK = threading.RLock()


@atexit.register
def _close_all_tunnels() -> None:
    """Stop every open SSH tunnel (server thread + paramiko client) on process exit."""
    with _LOCK:
        tunnels = list(_TUNNELS.values())
        _TUNNELS.clear()
    for t in tunnels:
        try:
            t.stop()
        except Exception:  # noqa: BLE001
            pass


class _Handler(socketserver.BaseRequestHandler):
    ssh_transport = None
    chain_host = "127.0.0.1"
    chain_port = 0

    def handle(self):
        try:
            chan = self.ssh_transport.open_channel(
                "direct-tcpip", (self.chain_host, self.chain_port), self.request.getpeername())
        except Exception as e:  # noqa: BLE001
            _log.debug("tunnel channel open failed: %s", e)
            return
        if chan is None:
            return
        try:
            while True:
                r, _, _ = select.select([self.request, chan], [], [])
                if self.request in r:
                    data = self.request.recv(4096)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(4096)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            chan.close()
            self.request.close()


class _Tunnel:
    def __init__(self, alias, host, username, password, remote_host, remote_port, local_port,
                 ssh_port=22):
        import paramiko

        self.alias = alias
        self.remote = f"{remote_host}:{remote_port}"
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(hostname=host, port=ssh_port, username=username,
                             password=password or None, look_for_keys=False,
                             allow_agent=False, timeout=15)
        try:
            transport = self._client.get_transport()

            class _H(_Handler):
                ssh_transport = transport
                chain_host = remote_host
                chain_port = remote_port

            self._server = socketserver.ThreadingTCPServer(("127.0.0.1", local_port), _H)
            self._server.daemon_threads = True
            self._server.allow_reuse_address = True
            self.local_port = self._server.server_address[1]
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        except Exception:
            # e.g. local_port already bound — don't leak the SSH connection/socket/thread.
            try:
                self._client.close()
            except Exception:
                pass
            raise

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass


def register(mcp, ctx) -> None:
    @mcp.tool
    def port_forward(remote_port: int, host: Optional[str] = None, local_port: int = 0,
                     remote_host: str = "127.0.0.1") -> dict:
        """Open an SSH local tunnel: operator 127.0.0.1:<local_port> → box's
        <remote_host>:<remote_port>. Use it to reach a service bound to the box's loopback
        (which WinRM-launched processes can't). local_port=0 picks a free port. Requires
        OpenSSH on the box (provision installs it when SMB isn't available)."""
        h = ctx.resolve(host)
        try:
            t = _Tunnel(h.alias, h.host, h.username, h.password, remote_host, int(remote_port),
                        int(local_port), ssh_port=getattr(h, "ssh_port", 22))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"tunnel failed: {e} — is OpenSSH running on the box? "
                                          "run enable_ssh or provision_host."}
        with _LOCK:
            _TUNNELS[t.local_port] = t
        return {"ok": True, "local_url": f"http://127.0.0.1:{t.local_port}",
                "local_port": t.local_port, "forwards_to": f"{h.alias}:{t.remote}"}

    @mcp.tool
    def port_forward_list() -> list:
        """List active SSH tunnels opened with port_forward."""
        with _LOCK:
            return [{"local_port": p, "host": t.alias, "forwards_to": t.remote}
                    for p, t in _TUNNELS.items()]

    @mcp.tool
    def port_forward_stop(local_port: int) -> dict:
        """Close an SSH tunnel by its local port."""
        with _LOCK:
            t = _TUNNELS.pop(int(local_port), None)
        if not t:
            return {"ok": False, "error": f"no tunnel on local port {local_port}"}
        t.stop()
        return {"ok": True, "closed_local_port": int(local_port)}
