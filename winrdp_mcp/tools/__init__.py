"""Tool modules. Each exposes ``register(mcp, ctx)`` to attach its tools to the server."""

from __future__ import annotations

from . import admin, files, hosts, network, provisioning, rdp, scripting, software, system, windows

MODULES = [hosts, provisioning, system, scripting, files, admin, rdp, software, network, windows]


def register_all(mcp, ctx) -> None:
    for mod in MODULES:
        mod.register(mcp, ctx)
