"""Tool modules. Each exposes ``register(mcp, ctx)`` to attach its tools to the server."""

from __future__ import annotations

from . import (admin, files, gui, hosts, network, ops, provisioning, rdp, scheduling,
               scripting, software, system, tunnel, waiters, windows)

MODULES = [hosts, provisioning, system, scripting, files, admin, rdp, software, network,
           windows, gui, waiters, scheduling, tunnel, ops]


def _short(mod) -> str:
    return mod.__name__.rsplit(".", 1)[-1]


# Curated tool profiles — which tool modules to expose. `WINRDP_PROFILE` selects one; the
# default 'full' exposes everything. A leaner profile keeps the model's tool list focused.
PROFILES = {
    "full": None,  # everything
    "core": {"hosts", "provisioning", "system", "scripting", "files", "admin", "ops", "waiters"},
    "admin": {"hosts", "provisioning", "system", "scripting", "files", "admin", "network",
              "windows", "software", "ops", "waiters", "scheduling", "tunnel"},
    "rdp": {"hosts", "provisioning", "system", "files", "admin", "rdp", "gui", "ops"},
}


def register_all(mcp, ctx, profile: str = "full") -> None:
    allowed = PROFILES.get(profile)
    for mod in MODULES:
        if allowed is None or _short(mod) in allowed:
            mod.register(mcp, ctx)
