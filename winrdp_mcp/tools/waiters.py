"""Wait-for-condition tools — poll a box until a port/service/process/file reaches a
target state. The glue that makes multi-step automation reliable.

Polling happens controller-side with short calls (each survives a transient disconnect via
the transport self-heal), so any timeout is safe — no single long-held WinRM request.
"""

from __future__ import annotations

import time
from typing import Optional

from .. import ps


def register(mcp, ctx) -> None:
    def _poll(check, want, host, timeout, interval, extra=None):
        start = time.time()
        deadline = start + timeout
        while True:
            try:
                cur = check()
            except Exception as e:  # noqa: BLE001
                cur = {"_error": str(e)}
            if isinstance(cur, dict) and cur.get("_match") == want:
                out = {"reached": True, "waited_s": round(time.time() - start, 1)}
                out.update({k: v for k, v in cur.items() if not k.startswith("_")})
                return out
            if time.time() >= deadline:
                return {"reached": False, "waited_s": round(time.time() - start, 1),
                        "timeout_s": timeout, **(extra or {})}
            time.sleep(interval)

    @mcp.tool
    def wait_for_port(target: str, port: int, host: Optional[str] = None, state: str = "open",
                      timeout: int = 120, interval: int = 3) -> dict:
        """Wait until a TCP `port` on `target` is reachable *from the box*. state: open | closed."""
        want = state == "open"

        def check():
            r = ctx.exec_json(
                f"$t=Test-NetConnection {ps.ps_string(target)} -Port {int(port)} -WarningAction SilentlyContinue;"
                "$result=@{o=[bool]$t.TcpTestSucceeded}", host=host, timeout=30)
            return {"_match": bool(r.get("o")) == want, "open": bool(r.get("o"))}

        return _poll(check, True, host, timeout, interval, {"target": target, "port": port, "state": state})

    @mcp.tool
    def wait_for_service(name: str, host: Optional[str] = None, status: str = "Running",
                         timeout: int = 120, interval: int = 3) -> dict:
        """Wait until a service reaches a status. status: Running | Stopped | Paused."""
        def check():
            r = ctx.exec_json(
                f"$s=Get-Service -Name {ps.ps_string(name)} -ErrorAction SilentlyContinue;"
                "$result=@{st=if($s){$s.Status.ToString()}else{'Missing'}}", host=host, timeout=30)
            st = r.get("st")
            return {"_match": st == status, "status": st}

        return _poll(check, True, host, timeout, interval, {"service": name, "want": status})

    @mcp.tool
    def wait_for_process(name: str, host: Optional[str] = None, present: bool = True,
                         timeout: int = 120, interval: int = 3) -> dict:
        """Wait until a process is present (present=True) or gone (present=False)."""
        def check():
            r = ctx.exec_json(
                f"$p=@(Get-Process -Name {ps.ps_string(name)} -ErrorAction SilentlyContinue);"
                "$result=@{n=$p.Count}", host=host, timeout=30)
            here = int(r.get("n", 0)) > 0
            return {"_match": here == present, "count": int(r.get("n", 0))}

        return _poll(check, True, host, timeout, interval, {"process": name, "present": present})

    @mcp.tool
    def wait_for_file(path: str, host: Optional[str] = None, exists: bool = True,
                      timeout: int = 120, interval: int = 3) -> dict:
        """Wait until a file/directory exists (exists=True) or disappears (exists=False)."""
        def check():
            r = ctx.exec_json(
                f"$result=@{{e=[bool](Test-Path -LiteralPath {ps.ps_string(path)})}}", host=host, timeout=30)
            here = bool(r.get("e"))
            return {"_match": here == exists, "exists": here}

        return _poll(check, True, host, timeout, interval, {"path": path, "exists": exists})
