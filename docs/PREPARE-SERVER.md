# Preparing a Windows Server / VDS / Dedicated Box

This guide takes a **fresh** Windows box — Windows 10/11, or Windows Server 2016 through
2025, from any cloud, VDS, or dedicated-hosting provider — and makes it manageable by
`winrdp-mcp`. The controller reaches boxes over **WinRM, SSH, or SMB**; nothing is
installed on the target. In the best case (WinRM already reachable) there is nothing to do
here at all — `provision_host` handles it. This document covers the cases where the box
starts fully locked down: a fresh cloud image with only RDP open, or a provider firewall in
front of it.

The two things you almost always have to do by hand on a brand-new box are:

1. **Open one management port inbound in the provider's firewall / security group** (the
   cloud panel — this is *outside* Windows and `winrdp-mcp` cannot do it for you).
2. **Turn on a transport once** inside Windows — either paste the enable-WinRM block into
   an RDP session, or let `provision_host` cold-start it.

After that, everything is automatic and repeatable.

---

## Contents

- [1. What winrdp needs on the target](#1-what-winrdp-needs-on-the-target)
- [2. The one-time bootstrap (turn on WinRM)](#2-the-one-time-bootstrap-turn-on-winrm)
- [3. Provider-side firewall / security group](#3-provider-side-firewall--security-group)
- [4. Cold-start options (when only RDP is open)](#4-cold-start-options-when-only-rdp-is-open)
- [5. Enabling and hardening RDP itself](#5-enabling-and-hardening-rdp-itself)
- [6. Verification](#6-verification)
- [7. Sizing: 4 GB / 2 vCPU minimum](#7-sizing-4-gb--2-vcpu-minimum)
- [8. New box in 3 minutes (end-to-end)](#8-new-box-in-3-minutes-end-to-end)

---

## 1. What winrdp needs on the target

**Nothing installed.** No Python, no agent, no MSI. The target only has to be *reachable*
on one management transport from the operator machine (where Claude Code / `winrdp-mcp
serve` runs). The controller speaks WinRM first, then SSH, then SMB+DCOM for cold-start.

Ports the controller may use, defined in `winrdp_mcp/config.py`:

| Port | Protocol | Used for | Who opens it |
|------|----------|----------|--------------|
| **5985** | WinRM over HTTP | Primary transport. NTLM still encrypts the payload. | Enable script + provider firewall |
| **5986** | WinRM over HTTPS | Production transport (TLS). Use with `winrm_cert_validation="validate"`. | You (cert + listener) + provider firewall |
| **22**   | SSH | Alternate transport (Windows OpenSSH Server). | `enable_ssh` / provider firewall |
| **3389** | RDP | Interactive desktop; the paste-once bootstrap path. | Usually open by default on cloud images |
| **445**  | SMB | Cold-start staging of the enable script over the admin share. | Provider firewall (optional) |
| **135**  | DCOM/RPC | Cold-start trigger via WMI `Win32_Process.Create`. | Provider firewall (optional) |

You do **not** need all of these. In practice you open exactly one primary management port
(5985 for HTTP, or 5986 for HTTPS) plus RDP 3389 for the initial hands-on step. 445/135 are
only relevant to the fully-remote SMB/WMI cold-start (see [section 4](#4-cold-start-options-when-only-rdp-is-open)).

> The Windows firewall is handled for you — the enable script opens the WinRM rule. The
> **provider** firewall (security group / NSG) is a separate layer you must open yourself;
> see [section 3](#3-provider-side-firewall--security-group).

---

## 2. The one-time bootstrap (turn on WinRM)

Pick one of the two options below. Both run the same idempotent script and are safe to
re-run.

### Option A — RDP in and paste the enable-WinRM block

1. RDP to the box (`mstsc /v:<ip>`), log in as a local **Administrator**.
2. Open **PowerShell as Administrator** (right-click → Run as administrator).
3. Paste the block below. This is the exact contents of
   [`scripts/enable-winrm.ps1`](../scripts/enable-winrm.ps1):

```powershell
# enable-winrm.ps1 — make a Windows box remotely manageable over WinRM.
# Paste into an elevated PowerShell (or RDP) session once. Idempotent.
$ErrorActionPreference = 'Stop'

# Public network profile blocks WinRM quickconfig; make active connections Private.
try {
    Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } |
        Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
} catch {}

Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
winrm quickconfig -quiet -force 2>$null

# NOTE: Basic auth, AllowUnencrypted, and TrustedHosts=* are intentionally NOT set — the
# default NTLM transport encrypts the payload even over HTTP 5985, so they're unnecessary
# and would only weaken the box.

# Give non-builtin local admins a full token over the network.
$p = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -Path $p -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force | Out-Null

Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
netsh advfirewall firewall add rule name='WinRM-HTTP-In-5985' dir=in action=allow protocol=TCP localport=5985 2>$null

Write-Output 'WINRDP_WINRM_ENABLED'
```

Success is the literal line `WINRDP_WINRM_ENABLED` at the end. What each part does:

- **Network profile → Private** — a `Public` profile makes `winrm quickconfig` refuse to
  create the listener. The script flips active connections to `Private` first. See the
  gotcha below.
- **`Enable-PSRemoting` + service Automatic + `quickconfig`** — creates the HTTP listener
  on 5985 and starts WinRM at boot.
- **No `Basic` / `AllowUnencrypted` / `TrustedHosts=*`** — the tool connects with NTLM (the
  default), which encrypts the payload without them; see the NTLM note below.
- **`LocalAccountTokenFilterPolicy = 1`** — the important one. It gives a **non-builtin**
  local admin a *full, high-integrity token over the network*, which is what lets elevated
  operations run directly over WinRM instead of getting "Access is denied" from a filtered
  token.
- **Firewall rule** — opens 5985 inbound in the Windows firewall.

### Option B — print the paste-once one-liner with `winrdp-mcp bootstrap`

Run this on the **operator** machine (it only prints text; it does not touch the box):

```powershell
winrdp-mcp bootstrap
# equivalently:
python -m winrdp_mcp bootstrap
```

It prints a single self-contained command whose form is:

```text
powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand <base64-of-the-enable-script>
```

...followed by the readable script from Option A. Copy the one-liner, RDP to the box, paste
it into an elevated PowerShell, press Enter. Same result, one line, nothing to format.

### Gotcha: the Public network-profile error

On a fresh cloud image the NIC is usually classified **Public**, and you'll see WinRM
refuse to configure with a message like:

```text
WinRM firewall exception will not work since one of the network connection types on this
machine is set to Public.
```

The enable script fixes this by setting active connections to `Private`
(`Set-NetConnectionProfile -NetworkCategory Private`) *before* `quickconfig`. If you ever
configure WinRM by hand, do the same, or use `Enable-PSRemoting -SkipNetworkProfileCheck`.

### Note: NTLM needs no `AllowUnencrypted` / `Basic`

`winrdp-mcp` connects with **NTLM by default** (`add_host(..., winrm_auth="ntlm")`), which
**encrypts the message payload even over HTTP 5985**. That's why the enable script does not
turn on `AllowUnencrypted` or `Basic` — they'd only weaken the box without helping the normal
path. (≤ 0.1.0 did set them; undo with `Set-Item WSMan:\localhost\Service\Auth\Basic $false`
and `…\AllowUnencrypted $false`.) You may still see a harmless "AllowUnencrypted will not
work ... the network connection is Public" warning from `quickconfig` — it's only about
Basic and does not block NTLM WinRM. If you want it gone, set the profile
to Private (the enable script already does). For real transport encryption use HTTPS 5986
(see [section 5](#5-enabling-and-hardening-rdp-itself) and production hardening below).

---

## 3. Provider-side firewall / security group

**This is the step people miss.** Enabling WinRM inside Windows is not enough if the cloud
provider's firewall drops the packet before it reaches the OS. You must open the management
port **inbound** in the provider console, in addition to (and independently of) the Windows
firewall rule the enable script creates.

**Scope it to your operator's public IP** wherever possible. WinRM/RDP exposed to
`0.0.0.0/0` is a standing target for credential-stuffing and RDP brute force.

### Generic checklist

- [ ] Open **inbound TCP 5985** (WinRM-HTTP) — *or* **5986** (WinRM-HTTPS) if you use TLS.
- [ ] Keep **inbound TCP 3389** (RDP) open for the initial hands-on bootstrap and for
      `rdp_open` / `screenshot`.
- [ ] (Cold-start only) open **445** and **135** inbound — see [section 4](#4-cold-start-options-when-only-rdp-is-open).
- [ ] Restrict the source to **your operator IP / CIDR**, not `Any`.
- [ ] Apply to the correct object: some providers attach rules per-instance, others
      per-subnet/VPC.
- [ ] Confirm there is no *second* firewall (host-level provider agent + network ACL).

### Where the setting lives, by provider

| Provider | Where to open the port |
|----------|------------------------|
| **AWS EC2** | Security Group → Inbound rules → add Custom TCP 5985 (source = your IP) |
| **Azure** | Network Security Group (NSG) on the NIC/subnet → Inbound security rules |
| **Google Cloud** | VPC network → Firewall → Create rule, Ingress, Allow tcp:5985, target by tag |
| **Hetzner Cloud** | Firewalls → Inbound rules (attach the firewall to the server) |
| **Oracle Cloud (OCI)** | VCN → Security List *or* Network Security Group → Ingress rules |
| **DigitalOcean** | Networking → Firewalls → Inbound rules |
| **Vultr / Linode / OVH** | Firewall / Security group section of the instance's networking panel |
| **Contabo / Kamatera / generic VPS** | Provider firewall panel if present; otherwise only the Windows firewall applies (the enable script already opened 5985) |

Some bare "Windows VPS" resellers have **no** network firewall in front of the box — in
that case the Windows firewall rule from the enable script is the only gate, and the port is
reachable as soon as WinRM is on. Treat that as *more* exposed, not less: harden per the
production notes and prefer 5986.

---

## 4. Cold-start options (when only RDP is open)

`provision_host` climbs a ladder (`winrdp_mcp/provision.py`) and stops at the first rung
that works:

1. **WinRM reachable** → use it and re-run the enable script to harden.
2. **SSH reachable** → use it, and turn WinRM on over the SSH channel for the richer path.
3. **SMB(445) + DCOM(135) reachable** → stage the enable script over the admin share and
   trigger it fire-and-forget via WMI, then switch to WinRM.
4. **Nothing but RDP** → return `bootstrap_oneliner` for you to paste once.

### Case A — only RDP (3389) is open

This is the common fresh-image state. There is no fully-remote path in; you must touch the
box once. `provision_host` will come back with `success: false` and a `bootstrap_oneliner`:

```json
{
  "success": false,
  "message": "Could not auto-enable remoting. Paste bootstrap_oneliner into an RDP/console session on the box once, then re-run provision().",
  "bootstrap_oneliner": "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand <base64>"
}
```

RDP in, paste that one line into an elevated PowerShell (or use `winrdp-mcp bootstrap`),
then re-run `provision_host`. From then on it's fully remote.

### Case B — WinRM and SSH both off, but SMB + DCOM reachable

If 445 and 135 are open (e.g. a domain image, or you opened them in the provider firewall),
`winrdp-mcp` can cold-start **without any RDP step**. It writes the enable script to the
admin share and launches it over WMI `Win32_Process.Create`. This uses **impacket**, which
is an opt-in extra:

```bash
pip install "winrdp-mcp[bootstrap]"
```

Then just provision normally — the ladder uses SMB/WMI automatically:

```text
add_host  alias="vps1"  host="203.0.113.10"  username="Administrator"  password="..."
provision_host          # allow_wmi_bootstrap defaults to True
```

The report's `actions` will show `bootstrap-staged-over-smb` and
`bootstrap-launched-over-wmi`, then WinRM comes up and it switches to it. If impacket isn't
installed you'll see `wmi-bootstrap-unavailable (pip install 'winrdp-mcp[bootstrap]')` and
it falls through to the paste-once one-liner.

> The admin creds must be able to reach `ADMIN$` and instantiate WMI — a real local/domain
> admin with `LocalAccountTokenFilterPolicy` not blocking it. Fresh workgroup boxes often
> need Case A first.

### Case C — SSH is available

If OpenSSH Server is already installed and 22 is open, the ladder uses it and turns WinRM on
over the SSH channel. You can also enable SSH later from Claude with `enable_ssh` (installs
the Windows OpenSSH Server capability, opens 22, sets PowerShell as the default shell).

---

## 5. Enabling and hardening RDP itself

RDP is usually already on for cloud Windows images (that's how you get in). If it isn't, or
you want to change its posture, use the RDP tools after provisioning
(`winrdp_mcp/tools/rdp.py`):

```text
rdp_status                          # enabled? NLA required? port? firewall rules?
rdp_enable  nla=true                # allow connections, open the 'Remote Desktop' group, force NLA
rdp_set_port  port=33890            # move off 3389; opens the new port, restarts TermService
rdp_allow_multiple_sessions         # lift per-user single-session (see note)
rdp_disable                         # block incoming RDP entirely
```

Hardening recommendations for a box on the public internet:

- **Require NLA** — `rdp_enable(nla=true)` (the default). NLA forces authentication before a
  full session is created.
- **Move RDP off 3389** — `rdp_set_port(port=<high port>)` cuts the bulk of automated
  scanning. Reconnect on the new port; update the provider firewall rule to match.
- **Scope 3389 (or your custom port) to your IP** in the provider firewall — same principle
  as the management port.
- **Concurrent sessions**: `rdp_allow_multiple_sessions` flips `fSingleSessionPerUser`, but
  *true* multi-session on client SKUs (Win10/11) needs `install_rdp_wrapper` with a RDPWrap
  installer URL. Server SKUs support multiple admin sessions natively.

Operator-side helpers once the box is registered: `rdp_connection_file` generates a `.rdp`
and stores the password via `cmdkey` (never returned to the model), and `rdp_open` launches
`mstsc` pre-authenticated.

---

## 6. Verification

### 6.1 From the operator, check the port is actually reachable

Before involving Claude, confirm the provider firewall + Windows firewall + listener all
line up. On a Windows operator:

```powershell
Test-NetConnection -ComputerName 203.0.113.10 -Port 5985
```

Expected on success:

```text
ComputerName     : 203.0.113.10
RemoteAddress    : 203.0.113.10
RemotePort       : 5985
InterfaceAlias   : Ethernet
SourceAddress    : 10.0.0.5
TcpTestSucceeded : True
```

`TcpTestSucceeded : False` means something upstream is closed — almost always the **provider
firewall** ([section 3](#3-provider-side-firewall--security-group)) or WinRM not started. On
a non-Windows operator:

```bash
nc -vz 203.0.113.10 5985     # or: python -c "import socket; socket.create_connection(('203.0.113.10',5985),5)"
```

### 6.2 Operator prerequisites (once)

```bash
pip install -e .                       # or: pip install winrdp-mcp
export WINRDP_VAULT_KEY='a-long-random-passphrase'   # encrypts stored credentials
```

Register the server with Claude Code via a project `.mcp.json`:

```json
{
  "mcpServers": {
    "winrdp": {
      "command": "python",
      "args": ["-m", "winrdp_mcp", "serve"]
    }
  }
}
```

...or `claude mcp add winrdp -- winrdp-mcp serve`.

### 6.3 Register, provision, and test in Claude

Ask Claude to run these tools (arguments shown inline):

```text
add_host  alias="vps1"  host="203.0.113.10"  username="Administrator"  password="..."  tags="prod"
provision_host
test_host
```

`add_host` returns:

```json
{ "added": "vps1", "active": "vps1" }
```

`provision_host` on a box whose WinRM you already enabled returns (abridged):

```json
{
  "alias": "vps1",
  "host": "203.0.113.10",
  "reachable_ports": {
    "winrm_http": true, "winrm_https": false, "ssh": false,
    "rdp": true, "smb": false, "dcom": false
  },
  "transport": "winrm",
  "winrm_enabled": true,
  "ssh_enabled": false,
  "actions": ["winrm-already-reachable", "winrm-hardened"],
  "success": true,
  "message": "WinRM reachable; hardened and ready."
}
```

`test_host` confirms a live session:

```json
{
  "alias": "vps1",
  "host": "203.0.113.10",
  "ports": { "winrm_http": true, "winrm_https": false, "ssh": false, "rdp": true, "smb": false, "dcom": false },
  "transport": "winrm",
  "online": true,
  "computername": "WIN-VPS1"
}
```

`online: true` with the box's real `computername` means it's fully manageable. Follow up
with `system_info` or `run_powershell` to do real work.

---

## 7. Sizing: 4 GB / 2 vCPU minimum

**Recommendation: at least 4 GB RAM and 2 vCPU for smooth operation.**

A 2 GB / single-low-CPU box thrashes under sustained WinRM load. Observed on a live test:
immediately after a package install, **Windows Defender (`MsMpEng`)** scanning the new files
plus **`TiWorker`** (component servicing) peg the CPU to **100%** and drive free RAM down to
**~300 MB**. Under that pressure the WinRM connection drops mid-operation. The transport
self-heal in `winrdp_mcp/transports.py` reconnects and retries once, so operations still
*complete* — but everything crawls, and long installs are at real risk of stalling.

Mitigations if you're stuck on a small box:

- Prefer running long installs detached — `install_software`, `run_python(ensure_python=…)`,
  and `ensure_runtime` already run as a scheduled task and poll, so a multi-minute install
  survives a mid-install disconnect.
- Raise the per-operation ceiling with `WINRDP_WINRM_OP_TIMEOUT` (seconds; default 180) so
  slow calls don't time out under load:

```powershell
$env:WINRDP_WINRM_OP_TIMEOUT = '600'
```

- Add a Defender exclusion for the staging dir once the box is up
  (`defender_exclusion_add "C:\ProgramData\winrdp-mcp"`) to cut the post-install scan storm.
- Right-size the box: 4 GB / 2 vCPU removes the thrash entirely in testing.

---

## 8. New box in 3 minutes (end-to-end)

A fresh cloud Windows box, only RDP open, from zero to managed.

**Provider console (once):**

1. Open inbound **TCP 5985** scoped to your operator's public IP.
2. Leave **TCP 3389** open (also scope it to your IP) for the one paste step.

**On the box (once, ~30 s):**

3. RDP in as Administrator, open **PowerShell (Admin)**.
4. On your operator, `winrdp-mcp bootstrap`, copy the one-liner, paste it on the box, Enter.
   Watch for `WINRDP_WINRM_ENABLED`. *(Or paste the full block from
   [section 2](#2-the-one-time-bootstrap-turn-on-winrm).)*

**On the operator:**

5. Confirm the path is open:

```powershell
Test-NetConnection -ComputerName 203.0.113.10 -Port 5985   # expect TcpTestSucceeded : True
```

6. In Claude Code (with the `winrdp` MCP server registered and `WINRDP_VAULT_KEY` set):

```text
add_host  alias="vps1"  host="203.0.113.10"  username="Administrator"  password="..."  tags="prod"
provision_host      # -> transport: "winrm", success: true
test_host           # -> online: true, computername: "WIN-VPS1"
system_info         # -> OS, build, CPU, RAM, disks, IPs
```

Done — the box is fully manageable. Everything from here is remote and repeatable.

### Production hardening (do this before you trust it on the internet)

- **Use HTTPS 5986**, not HTTP 5985: install a WinRM HTTPS listener with a real cert, open
  5986 in the provider firewall, and register with
  `add_host(..., use_ssl=true, winrm_cert_validation="validate", ssh_host_key_policy="reject")`.
  The defaults (`winrm_cert_validation="ignore"`, `ssh_host_key_policy="auto"`) are
  permissive so first contact just works, which allows an on-path attacker to MITM on an
  untrusted network — tighten them once the cert/known_hosts are in place.
- **Scope `TrustedHosts`** to specific hosts instead of `*` after bootstrap.
- **Restrict every management/RDP port to your operator IP** in the provider firewall.
- **Set `WINRDP_VAULT_KEY`** to a strong passphrase so stored credentials are encrypted at
  rest (otherwise a machine-local `vault.key` is used).
- Secrets on the box are transient: elevated runs, `user_create`, and `service_create`
  briefly place a secret on a command line or temp `.ps1` on the target; `cmdkey` stores the
  RDP password on the **operator** machine. Secrets are never returned to the model and logs
  redact known secrets by exact match.
