"""Connectivity doctor — explain *why* a box won't connect, and what to do about it.

`test_host` only says "No working transport". This runs the checks a human would otherwise
do by hand (the ones that took a whole debugging session once): detect an intercepting
proxy/VPN on the operator side, see which auth schemes the box offers, actually try each
transport and classify the real error (bad credentials vs unreachable vs proxy-mangled NTLM),
then give a concrete recommendation.
"""

from __future__ import annotations

from typing import Optional

from . import config, log
from .transports import SSHTransport, port_open
from .vault import Host

_log = log.get("diag")

# RFC 5737 TEST-NET-1 — guaranteed non-routable. A successful TCP connect here means the
# operator's network accepts connects for ANY address (a transparent proxy / VPN), which
# makes port scans meaningless and commonly breaks connection-oriented NTLM.
_DEAD_IP = "192.0.2.1"


def _www_authenticate(host: str, port: int, timeout: float = 6.0) -> Optional[str]:
    """Return the WinRM endpoint's advertised auth schemes (the 401 WWW-Authenticate), or None."""
    try:
        import requests
        r = requests.post(f"http://{host}:{port}/wsman", data="", timeout=timeout)
        return r.headers.get("WWW-Authenticate")
    except Exception as e:  # noqa: BLE001
        return f"(no response: {type(e).__name__})"


def _try_winrm_auth(h: Host, auth: str, timeout: float = 15.0) -> dict:
    """Attempt a real WinRM call with a specific auth scheme; classify the outcome."""
    try:
        import winrm
        scheme = "https" if h.use_ssl else "http"
        port = h.winrm_https_port if h.use_ssl else h.winrm_port
        user = f"{h.domain}\\{h.username}" if h.domain else h.username
        s = winrm.Session(
            f"{scheme}://{h.host}:{port}/wsman",
            auth=(user, h.password), transport=auth,
            server_cert_validation="ignore",
        )
        r = s.run_ps("$env:COMPUTERNAME")
        if r.status_code == 0:
            return {"auth": auth, "ok": True, "computername": r.std_out.decode(errors="replace").strip()}
        return {"auth": auth, "ok": False, "error": r.std_err.decode(errors="replace")[:200]}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        low = msg.lower()
        if "invalidcredentials" in low or "rejected" in low:
            kind = "credentials-rejected"
        elif "timed out" in low or "timeout" in low:
            kind = "timeout"
        elif "refused" in low or "actively refused" in low:
            kind = "connection-refused"
        elif "basic" in low and "unencrypted" in low:
            kind = "basic-needs-allowunencrypted"
        else:
            kind = "error"
        return {"auth": auth, "ok": False, "kind": kind, "error": msg[:200]}


def diagnose(h: Host) -> dict:
    """Run the connectivity checks and return a structured report with a verdict + fixes."""
    rep: dict = {"alias": h.alias, "host": h.host, "checks": [], "recommendations": []}

    # 1) Operator-side interception (the thing that fooled us for a whole session).
    try:
        intercepts = port_open(_DEAD_IP, config.WINRM_HTTP_PORT, timeout=2)
    except Exception:  # noqa: BLE001
        intercepts = False
    rep["operator_network"] = "intercepting-proxy-or-vpn" if intercepts else "direct"
    if intercepts:
        rep["checks"].append("A TCP connect to a guaranteed-dead IP succeeded → the operator "
                             "network accepts connects for any address (transparent proxy/VPN, "
                             "e.g. Clash TUN). Port scans are therefore unreliable here.")
        rep["recommendations"].append(
            "Operator network intercepts connections. This commonly breaks NTLM (its multi-leg "
            "handshake needs one persistent TCP connection a rotating proxy won't preserve). Fix: "
            "route the box IP DIRECT in your proxy, OR enable Basic auth on the box "
            "(enable_winrm(basic=True) / rdp_bootstrap(enable_basic=True)) and register with "
            "winrm_auth='basic'.")

    # 2) Raw port reachability (advisory — unreliable when intercepts is true).
    ports = {
        "winrm_http": port_open(h.host, h.winrm_port, timeout=3),
        "winrm_https": port_open(h.host, h.winrm_https_port, timeout=3),
        "ssh": port_open(h.host, h.ssh_port, timeout=3),
        "rdp": port_open(h.host, h.rdp_port, timeout=3),
        "smb": port_open(h.host, config.SMB_PORT, timeout=3),
    }
    rep["reachable_ports"] = ports
    rep["reachable_ports_note"] = ("unreliable — operator proxy accepts any connect"
                                   if intercepts else "raw TCP connect results")

    # 3) What auth schemes does the WinRM listener advertise?
    offered = _www_authenticate(h.host, h.winrm_port)
    rep["winrm_offers"] = offered
    offers_basic = bool(offered and "Basic" in offered)

    # 4) Actually try to authenticate. Try the configured scheme, then the other, so we can say
    #    which one WOULD work.
    attempts = []
    primary = (h.winrm_auth or "ntlm").lower()
    order = [primary] + [a for a in ("ntlm", "basic") if a != primary]
    winrm_ok = False
    for auth in order:
        res = _try_winrm_auth(h, auth)
        attempts.append(res)
        if res.get("ok"):
            winrm_ok = True
            break
    rep["winrm_attempts"] = attempts

    # 5) SSH reachability (real banner/handshake).
    try:
        s = SSHTransport(h.host, h.username, h.password, port=h.ssh_port,
                         host_key_policy=getattr(h, "ssh_host_key_policy", "auto"))
        rep["ssh"] = {"ok": bool(s.probe())}
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        rep["ssh"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # 6) Verdict + targeted recommendation.
    if winrm_ok:
        good = next(a["auth"] for a in attempts if a.get("ok"))
        rep["verdict"] = f"WinRM works with auth='{good}'."
        if good != primary:
            rep["recommendations"].append(
                f"Re-register the host with winrm_auth='{good}' (add_host ... winrm_auth='{good}').")
    else:
        rejected = [a for a in attempts if a.get("kind") == "credentials-rejected"]
        ntlm_rej = any(a["auth"] == "ntlm" and a.get("kind") == "credentials-rejected" for a in attempts)
        if ntlm_rej and intercepts:
            rep["verdict"] = ("WinRM rejects NTLM while the operator network intercepts "
                              "connections — the proxy is almost certainly breaking the NTLM "
                              "handshake (the password itself may be fine).")
            if not offers_basic:
                rep["recommendations"].append(
                    "Enable Basic on the box: enable_winrm(basic=True) (or paste "
                    "rdp_bootstrap(enable_basic=True)'s command), then add_host(winrm_auth='basic').")
            else:
                rep["recommendations"].append(
                    "The box already offers Basic — re-register with winrm_auth='basic'.")
        elif rejected:
            rep["verdict"] = "Credentials were rejected by the box (check username/password)."
            rep["recommendations"].append(
                "Verify the password; on the box: a ValidateCredentials check / `net user <name>` "
                "(watch for lockout from repeated failed logons / brute force).")
        elif all(a.get("kind") in ("timeout", "connection-refused") for a in attempts):
            rep["verdict"] = "WinRM is not reachable (timeout/refused)."
            rep["recommendations"].append(
                "Open 5985 (WinRM) inbound at the provider firewall / security group, and make "
                "sure WinRM is enabled on the box (provision_host / the bootstrap one-liner).")
        else:
            rep["verdict"] = "WinRM did not authenticate; see winrm_attempts for the raw errors."
    return rep
