"""CLI entry point.

    winrdp-mcp serve                     # MCP over stdio (controller; add to Claude Code)
    winrdp-mcp serve --http --port 8765  # MCP over HTTP (remote clients)
    winrdp-mcp agent                     # run on the box itself (transport=local)
    winrdp-mcp bootstrap                  # print the paste-once enable-WinRM one-liner
    winrdp-mcp add-host ...              # register a box from the shell
    winrdp-mcp provision <alias>         # provision a registered box from the shell
"""

from __future__ import annotations

import json
import sys

import click

from . import __version__
from .provision import ENABLE_WINRM_PS, bootstrap_oneliner
from .server import build_server
from .vault import Host, Vault


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def cli() -> None:
    """winrdp-mcp — zero-config MCP server for Windows RDP administration."""


@cli.command()
@click.option("--http", "use_http", is_flag=True, help="Serve over HTTP instead of stdio.")
@click.option("--host", "bind", default="127.0.0.1", help="HTTP bind host.")
@click.option("--port", default=8765, type=int, help="HTTP port.")
@click.option("--local", is_flag=True, help="Also manage this machine as host 'local'.")
@click.option("--debug", is_flag=True, help="Verbose debug logging to stderr.")
def serve(use_http: bool, bind: str, port: int, local: bool, debug: bool) -> None:
    """Run the MCP server (controller mode)."""
    mcp, _ctx = build_server(local=local, debug=debug)
    if use_http:
        mcp.run(transport="http", host=bind, port=port)
    else:
        mcp.run()


@cli.command()
@click.option("--http", "use_http", is_flag=True, help="Serve over HTTP (for remote Claude).")
@click.option("--host", "bind", default="0.0.0.0", help="HTTP bind host.")
@click.option("--port", default=8765, type=int, help="HTTP port.")
@click.option("--debug", is_flag=True, help="Verbose debug logging to stderr.")
def agent(use_http: bool, bind: str, port: int, debug: bool) -> None:
    """Run on the box itself: manage this machine via the local transport."""
    seed = Host(alias="local", host="localhost", transport="local")
    mcp, _ctx = build_server(local=True, seed_host=seed, debug=debug)
    if use_http:
        mcp.run(transport="http", host=bind, port=port)
    else:
        mcp.run()


@cli.command()
def bootstrap() -> None:
    """Print the one-line command to enable WinRM on a box (paste into an RDP session)."""
    click.echo("# Paste this once into an elevated PowerShell / RDP session on the box:\n")
    click.echo(bootstrap_oneliner(Host(alias="_", host="_")))
    click.echo("\n# (equivalent readable script below)\n")
    click.echo(ENABLE_WINRM_PS.strip())


@cli.command("add-host")
@click.argument("alias")
@click.argument("host")
@click.option("--username", "-u", default="")
@click.option("--password", "-p", default="")
@click.option("--domain", "-d", default="")
@click.option("--transport", default="auto", type=click.Choice(["auto", "winrm", "ssh", "local"]))
@click.option("--ssl", is_flag=True)
def add_host_cmd(alias, host, username, password, domain, transport, ssl) -> None:
    """Register a box in the encrypted inventory (shared with the server)."""
    v = Vault()
    v.add(Host(alias=alias, host=host, username=username, password=password,
               domain=domain, transport=transport, use_ssl=ssl))
    click.echo(f"added '{alias}' -> {host} (active)")


@cli.command("list-hosts")
def list_hosts_cmd() -> None:
    """List registered boxes."""
    v = Vault()
    active = v.active.alias if v.active else None
    for h in v.all():
        mark = "*" if h.alias == active else " "
        click.echo(f"{mark} {h.alias:16} {h.host:24} {h.transport}")


@cli.command("provision")
@click.argument("alias", required=False)
def provision_cmd(alias) -> None:
    """Provision a registered box (or the active one) from the shell."""
    from .provision import provision as do_provision

    v = Vault()
    h = v.get(alias) if alias else v.active
    if not h:
        click.echo("no such host; add one with add-host", err=True)
        sys.exit(1)
    rep = do_provision(h)
    v.update(h)
    click.echo(json.dumps(rep.to_dict(), indent=2))


if __name__ == "__main__":
    cli()
