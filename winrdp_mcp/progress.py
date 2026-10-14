"""Progress-notification helper (MCP 2026 progress).

A tool declares an optional ``mcp_ctx: Context | None = None`` parameter — FastMCP injects
the live request Context there and hides it from the tool's input schema. Wrap it in
:class:`Progress` and call ``.report(...)`` at each milestone of a long operation.

Everything here is best-effort and safe to call unconditionally:
  * no Context (older FastMCP, or the client sent no progressToken) -> no-op;
  * ``report_progress`` is async and a sync tool runs in a worker thread, so we bridge the
    call back onto the event loop with ``anyio.from_thread.run``; if we're not in such a
    thread (or the bridge fails), we swallow it rather than break the tool.
"""

from __future__ import annotations

from typing import Optional

import anyio

try:  # the live request context type FastMCP injects
    from fastmcp import Context
except Exception:  # pragma: no cover
    Context = None  # type: ignore[assignment]


class Progress:
    """Best-effort progress reporter wrapping an injected FastMCP Context."""

    def __init__(self, mcp_ctx: Optional["Context"] = None, total: Optional[float] = None) -> None:
        self._ctx = mcp_ctx
        self.total = total

    @property
    def active(self) -> bool:
        return self._ctx is not None

    def report(self, progress: float, message: Optional[str] = None,
               total: Optional[float] = None) -> None:
        if self._ctx is None:
            return
        t = total if total is not None else self.total
        try:
            anyio.from_thread.run(self._ctx.report_progress, float(progress), t, message)
        except Exception:  # noqa: BLE001 — not in a worker-thread portal, or client has no token
            pass

    def heartbeat(self, message: str, start: float = 5.0, cap: float = 95.0):
        """Return a ``callback(elapsed_seconds)`` for a poll loop, so a long single blocking
        operation keeps emitting "still running — Ns" instead of looking hung. The reported
        value climbs ~1%/s from ``start`` and sticks at ``cap`` until the final report(100)."""
        def cb(elapsed: float) -> None:
            self.report(min(cap, start + elapsed), f"{message} — {int(elapsed)}s elapsed")
        return cb
