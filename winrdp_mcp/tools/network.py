"""Networking tools: adapters/DNS, ping, port check, connections, port-proxy, set DNS."""

from __future__ import annotations

from typing import Optional

from .. import ps
from . import _validate as V


def register(mcp, ctx) -> None:
    @mcp.tool
    def net_info(host: Optional[str] = None) -> dict:
        """Network configuration of a box: adapters, IPv4, gateway, DNS servers, MAC."""
        body = (
            "$result=@(Get-NetIPConfiguration -ErrorAction SilentlyContinue|ForEach-Object{@{"
            "interface=$_.InterfaceAlias;"
            "ipv4=@($_.IPv4Address.IPAddress);"
            "gateway=$_.IPv4DefaultGateway.NextHop;"
            "dns=@($_.DNSServer.ServerAddresses);"
            "mac=$_.NetAdapter.MacAddress;"
            "status=$_.NetAdapter.Status}})"
        )
        return {"adapters": ps.as_list(ctx.exec_json(body, host=host))}

    @mcp.tool
    def ping(target: str, host: Optional[str] = None, count: int = 4) -> dict:
        """Ping a target from the box."""
        body = (
            f"$r=Test-Connection -ComputerName {ps.ps_string(target)} -Count {int(count)} "
            "-ErrorAction SilentlyContinue;"
            "$result=@{target=" + ps.ps_string(target) + ";replies=@($r).Count;"
            "avg_ms=if($r){[math]::Round((@($r)|Measure-Object -Property ResponseTime -Average).Average,1)}else{$null}}"
        )
        return ctx.exec_json(body, host=host, timeout=60)

    @mcp.tool
    def port_check(target: str, port: int, host: Optional[str] = None) -> dict:
        """Test whether a TCP port on `target` is reachable *from the box*."""
        body = (
            f"$t=Test-NetConnection -ComputerName {ps.ps_string(target)} -Port {int(port)} "
            "-WarningAction SilentlyContinue;"
            "$result=@{target=" + ps.ps_string(target) + f";port={int(port)};"
            "open=$t.TcpTestSucceeded;remote_ip=$t.RemoteAddress.IPAddressToString}"
        )
        return ctx.exec_json(body, host=host, timeout=60)

    @mcp.tool
    def net_connections(host: Optional[str] = None, state: str = "Listen", top: int = 200) -> list:
        """List TCP connections on a box. state: Listen|Established|* (all)."""
        if state != "*":
            V.enum(state, {"Listen", "Established", "TimeWait", "CloseWait", "SynSent",
                           "SynReceived", "FinWait1", "FinWait2", "Closing", "LastAck",
                           "Closed", "Bound"}, "state")
        flt = "" if state == "*" else f"-State {state} "
        body = (
            f"$result=@(Get-NetTCPConnection {flt}-ErrorAction SilentlyContinue|"
            f"Select-Object -First {int(top)}|ForEach-Object{{"
            "$p=(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName;@{"
            "local=\"$($_.LocalAddress):$($_.LocalPort)\";"
            "remote=\"$($_.RemoteAddress):$($_.RemotePort)\";"
            "state=$_.State.ToString();pid=$_.OwningProcess;process=$p}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def port_proxy_add(listen_port: int, connect_host: str, connect_port: int,
                       host: Optional[str] = None, listen_address: str = "0.0.0.0",
                       open_firewall: bool = True) -> dict:
        """Add a netsh portproxy on a box (v4->v4). Great for tunneling RDP/other services
        through a reachable box to one behind NAT. Runs elevated."""
        V.host(connect_host, "connect_host")
        V.host(listen_address, "listen_address")
        fw = (
            f"New-NetFirewallRule -DisplayName 'winrdp-proxy-{int(listen_port)}' -Direction Inbound "
            f"-Protocol TCP -LocalPort {int(listen_port)} -Action Allow|Out-Null;" if open_firewall else ""
        )
        body = (
            f"netsh interface portproxy add v4tov4 listenport={int(listen_port)} "
            f"listenaddress={listen_address} connectport={int(connect_port)} "
            f"connectaddress={connect_host}|Out-Null;" + fw +
            "$result=@{ok=$true;"
            f"rule='{listen_address}:{int(listen_port)} -> {connect_host}:{int(connect_port)}'}}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def port_proxy_list(host: Optional[str] = None) -> dict:
        """List active netsh portproxy rules on a box."""
        r = ctx.transport_for(host).run_cmd("netsh interface portproxy show all", timeout=30)
        return {"stdout": r.stdout, "rc": r.rc}

    @mcp.tool
    def port_proxy_delete(listen_port: int, host: Optional[str] = None,
                          listen_address: str = "0.0.0.0") -> dict:
        """Delete a netsh portproxy rule by listen port. Runs elevated."""
        V.host(listen_address, "listen_address")
        body = (
            f"netsh interface portproxy delete v4tov4 listenport={int(listen_port)} "
            f"listenaddress={listen_address}|Out-Null;$result=@{{ok=$true}}"
        )
        return ctx.exec_json(body, host=host, elevated=True)

    @mcp.tool
    def set_dns(interface: str, servers: str, host: Optional[str] = None) -> dict:
        """Set DNS servers on an interface. servers: comma-separated (e.g. '1.1.1.1,8.8.8.8').
        Use net_info to get interface aliases. Runs elevated."""
        arr = ",".join(ps.ps_string(s.strip()) for s in servers.split(",") if s.strip())
        body = (
            f"Set-DnsClientServerAddress -InterfaceAlias {ps.ps_string(interface)} "
            f"-ServerAddresses @({arr});$result=@{{ok=$true;interface={ps.ps_string(interface)}}}"
        )
        return ctx.exec_json(body, host=host, elevated=True)
