# winrdp-mcp

**A zero-config [MCP](https://modelcontextprotocol.io) server that provisions and fully administers any Windows RDP box — Windows 10/11 and Server 2016–2025 — for Claude and Claude Code.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/Target-Windows%2010%2F11%20%7C%20Server%202016--2025-0078D6.svg)](#)
[![MCP: FastMCP](https://img.shields.io/badge/MCP-FastMCP-6E56CF.svg)](https://github.com/jlowin/fastmcp)

You give it a host and admin credentials. It makes the box remotely manageable *by itself* — turning on WinRM, opening the Windows firewall, and fixing local-admin token filtering — regardless of the box's starting state or Windows version. Claude then gets **136 tools**: shell, files, registry, services, processes, scheduled tasks, users, firewall, event logs, software, networking, live RDP control, screenshots, real UAC elevation, and on-demand tool staging.

**Nothing is pre-installed on the target.** The controller reaches boxes over WinRM / SSH / SMB from wherever Claude Code runs, and manages one box or a whole fleet from a single server.

### Why it's different

- **Zero-config provisioning.** `provision_host` climbs a ladder — WinRM → SSH → SMB/WMI cold-start → paste-once bootstrap — and makes a fresh, locked-down box manageable with no manual WinRM setup.
- **Real UAC / elevation, not "please run as admin."** Over WinRM a local admin gets a high-integrity full token and elevated ops run directly; a filtered token falls back to a one-shot `SYSTEM` Scheduled Task. `as_user=True` runs inside the interactive RDP desktop.
- **136 tools across 13 modules**, every one with `readOnlyHint` / `destructiveHint` safety annotations so MCP clients can gate destructive actions automatically.
- **On-demand code execution.** `run_python` finds or installs Python, pip-installs deps, runs your code, and cleans up — same for Node, PowerShell, cmd, and batch. `stage_tool` pulls Sysinternals (or any URL/local file) onto the box mid-task.
- **Native GUI automation.** Drive the interactive RDP desktop — keystrokes, mouse, and UI Automation (find/click/read controls by name) — plus live screenshots, with no on-box agent.
- **First-class RDP** and an **encrypted multi-host inventory** (Fernet) with tags and parallel fan-out across the fleet.

### Two modes, one package

| Mode | Command | Runs where | Reaches the box via |
|------|---------|-----------|---------------------|
| **Controller** (default) | `winrdp-mcp serve` | wherever Claude Code lives | WinRM / SSH / SMB+DCOM |
| **Agent** | `winrdp-mcp agent` | on the box itself | local PowerShell |

Both build the *same* server; `python -m winrdp_mcp serve` is equivalent to the console script.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Tool groups](#tool-groups)
3. [Preparing a box](#preparing-a-box)
4. [Key capabilities](#key-capabilities)
5. [Configuration](#configuration)
6. [Security](#security)
7. [Documentation](#documentation)
8. [Architecture](#architecture)
9. [Attribution & license](#attribution--license)

---

## Quick start

### 1. Install

Python **3.10+** on the operator machine (Windows, macOS, or Linux). Targets are Windows.

```bash
pip install -e .                     # from a checkout of this repo
pip install -e ".[bootstrap]"        # + impacket, for SMB/WMI cold-start of boxes with WinRM AND SSH off
pip install -e ".[agent-ui]"         # + on-box interactive-desktop UI agent (click/type/OCR)
```

### 2. Register with Claude Code

Drop a project `.mcp.json` at your repo root:

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

Or register from the CLI:

```bash
claude mcp add winrdp -- winrdp-mcp serve
```

Set `WINRDP_VAULT_KEY` to a strong passphrase — it encrypts stored credentials at rest (see [Configuration](#configuration)).

### 3. The 30-second flow

Ask Claude to run these tools (arguments shown inline). Every tool takes an optional `host=` alias; omit it to hit the **active** host.

```text
add_host  alias="vps1"  host="203.0.113.10"  username="Administrator"  password="…"
provision_host                        # climbs the ladder → box is now manageable
system_info                           # OS, build, CPU, RAM, disks, IPs
run_powershell  script="Get-Service | Where Status -eq Running"
run_python  code="import platform; print(platform.platform())"
```

If the box has only RDP open, `provision_host` returns a `bootstrap_oneliner` to paste once into an RDP session — see [Preparing a box](#preparing-a-box).

---

## Tool groups

**136 tools** across thirteen modules. The full catalog — every signature, parameter, default, and safety class — is in **[docs/TOOLS.md](docs/TOOLS.md)**.

| Group | Module | # | What it covers |
|-------|--------|--:|----------------|
| **Hosts & Fleet** | `hosts.py` | 8 | `add_host`, `list_hosts`, `use_host`, `remove_host`, `test_host`, `provision_host`, `run_on_hosts` (parallel fan-out), `reboot_and_wait` |
| **Provisioning / UAC / Tooling** | `provisioning.py` | 10 | `enable_winrm`, `enable_ssh`, `run_elevated`, `uac_get`/`uac_set`, `stage_tool`, `stage_script`, `list_staged_tools`, `cleanup_staged`, `deploy_ui_agent` |
| **System** | `system.py` | 8 | `run_powershell`, `run_cmd`, `system_info`, `performance`, `event_log`, `screenshot`, `reboot`, `power_action` |
| **Scripting** | `scripting.py` | 6 | `run_python`, `run_python_file`, `pip_install`, `run_script` (auto-interpreter), `run_node`, `ensure_runtime` |
| **Files** | `files.py` | 21 | list/read/write/search/upload/download/delete, `make_dir`, copy/move, `file_hash`, zip/unzip, ACL, `download_file`, `tail_file`, `edit_file`, `sync_folder`, `transfer_between_hosts` |
| **Admin** | `admin.py` | 25 | registry, services (`service_control`/`service_create`/`service_delete`), processes, scheduled tasks, users/groups, firewall, clipboard, event-log write/clear |
| **RDP** | `rdp.py` | 12 | `rdp_status`/`rdp_enable`/`rdp_disable`/`rdp_set_port`, sessions, disconnect/logoff, `rdp_connect_to_console` (tscon), `rdp_connection_file`, `rdp_open`, `install_rdp_wrapper`, `rdp_allow_multiple_sessions` |
| **Software** | `software.py` | 4 | `install_software` (winget/choco/MSI/EXE-url), `uninstall_software`, `list_installed_software`, `ensure_package_manager` |
| **Network** | `network.py` | 8 | `net_info`, `ping`, `port_check`, `net_connections`, `port_proxy_add`/`list`/`delete` (tunneling), `set_dns` |
| **Windows** | `windows.py` | 11 | `cim_query` (any WQL), `windows_features`, `windows_update`, `hotfixes`, Defender (`status`/`realtime`/`exclusion_add`/`scan`), `env_get`/`env_set`, `list_startup` |
| **GUI** | `gui.py` | 15 | windows/keyboard/mouse (`send_keys`, `type_text`, `mouse_click`, `mouse_drag`), UI Automation (`ui_find`/`ui_invoke`/`ui_set_text`), `ocr_screen`, `find_and_click`, `wait_for_window`, `record_screen`, `gui_script` |
| **Waiters** | `waiters.py` | 4 | `wait_for_port`, `wait_for_service`, `wait_for_process`, `wait_for_file` — block until a condition holds |
| **Scheduling** | `scheduling.py` | 4 | `schedule_command`, `run_at_startup`, `persist_as_service` (NSSM auto-restart), `unpersist_service` |

Safety classification across all 136: **44 read-only**, **23 destructive**, **69 mutating**. Read-only tools are safe to auto-run; destructive tools carry `destructiveHint=True` so clients gate them behind confirmation.

---

## Preparing a box

The controller needs the target reachable on **one** management transport. In the best case (WinRM already up) `provision_host` does everything. The only two things you may have to do by hand on a brand-new cloud box are:

1. **Open one management port inbound in the provider firewall / security group** (5985 for WinRM-HTTP, or 5986 for HTTPS) — this is *outside* Windows and winrdp-mcp cannot do it for you.
2. **Turn on a transport once** — either paste the enable-WinRM one-liner into an RDP session, or let the SMB/WMI cold-start do it.

Print the paste-once one-liner any time:

```bash
winrdp-mcp bootstrap        # prints the -EncodedCommand one-liner + the readable script
```

Full walkthrough — provider-firewall specifics (AWS/Azure/GCP/Hetzner/…), the cold-start rungs, RDP hardening, verification, and a "new box in 3 minutes" runbook — in **[docs/PREPARE-SERVER.md](docs/PREPARE-SERVER.md)**.

---

## Key capabilities

### Provisioning ladder

`provision_host` tries the best rung first and stops at the first that works ([`winrdp_mcp/provision.py`](winrdp_mcp/provision.py)):

1. **WinRM** (5985/5986) reachable → use it, re-run the idempotent enable script to harden.
2. **SSH** (22) reachable → use it, and turn WinRM on over the SSH channel for the richer path.
3. **SMB (445) + DCOM (135)** only → stage the enable script over `ADMIN$` and trigger it fire-and-forget over WMI (`[bootstrap]` extra), then switch to WinRM.
4. **Nothing but RDP** → return a `bootstrap_oneliner` to paste once; then everything is remote.

The enable script opens the WinRM firewall rule, flips a `Public` network profile to `Private`, and sets `LocalAccountTokenFilterPolicy=1` so a non-builtin local admin gets a full token over the network.

### Elevated & interactive execution

```text
run_powershell  script="Stop-Service W3SVC"  elevated=true     # full unfiltered token
run_powershell  script="Add-Type -AssemblyName System.Windows.Forms; …"  as_user=true   # interactive RDP desktop
```

Over WinRM a full-token admin runs `elevated=True` **directly** (fast path); a filtered token (SSH / non-elevated local) falls back to a one-shot `SYSTEM` Scheduled Task. `as_user=True` runs inside the visible desktop session — needed for GUI, clipboard, and screenshots.

### Run any script in one call

```text
run_python  code="import psutil; print(psutil.cpu_percent())"  pip="psutil"   # auto-installs Python + psutil
run_script  content=<any code>  interpreter="auto"                            # python | node | powershell | cmd | vbscript
run_node    code="console.log(process.version)"
ensure_runtime  runtime="python"                                             # or "node"
```

`run_python` with `ensure_python=True` (default) locates Python or installs it detached (winget → choco → python.org), resolving the concrete `python.exe` by glob so it works the same session. Long installs run as a Scheduled Task and poll a done-marker, so a mid-install WinRM disconnect doesn't fail them.

### Parallel fan-out across the fleet

```text
add_host  alias="web1"  host="10.20.0.11"  username="Administrator"  password="…"  tags="prod,web"
run_on_hosts  script="(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"  tag="prod"
```

`run_on_hosts` runs one script across many boxes concurrently (default `max_parallel=8`) and returns per-host `{stdout, stderr, rc}` keyed by alias. Select by `aliases` (CSV), by `tag`, or omit both to hit every box.

### First-class RDP control

```text
rdp_status                            # enabled? NLA? port? firewall?
rdp_enable  nla=true
rdp_sessions                          # id / user / state via qwinsta
screenshot                            # live RDP desktop as a PNG
rdp_open                              # launch mstsc pre-authenticated (operator = Windows)
```

`rdp_connection_file` generates a `.rdp` and stores the password via `cmdkey` on the **operator** machine — it is never returned to the model or written into the `.rdp` file. `install_rdp_wrapper` enables concurrent sessions on client SKUs.

### On-demand tooling

```text
stage_tool  source="psexec"                       # preset (Sysinternals), a URL, or a local file
stage_tool  source="https://example.com/tool.exe"
list_staged_tools
cleanup_staged
```

Presets: `psexec, handle, procdump, autoruns, tcpview, pslist, accesschk, sigcheck`. Everything caches under `C:\ProgramData\winrdp-mcp\tools`; URLs and presets download **on the box**, local files are uploaded.

---

## Configuration

All configuration is via environment variables (set them in the `env` block of your `.mcp.json`).

| Variable | Purpose |
|----------|---------|
| `WINRDP_VAULT_KEY` | Passphrase (or raw Fernet key) that encrypts stored passwords. A passphrase is SHA-256-derived into a key. If unset, a machine-local `vault.key` (owner-only) is generated. **Set it.** |
| `WINRDP_HOME` | Override the data directory holding `inventory.json` and `vault.key` (default `%APPDATA%\winrdp-mcp`). |
| `WINRDP_DEBUG` | `1`/`true`/`yes` → verbose DEBUG logging to **stderr** (equivalent to `--debug`). |
| `WINRDP_ENABLED_TOOLS` | CSV allowlist — if set, **only** these tools are registered. |
| `WINRDP_DISABLED_TOOLS` | CSV blocklist — these tools are skipped (e.g. `reboot,file_delete`). |
| `WINRDP_WINRM_OP_TIMEOUT` | WinRM per-operation timeout in seconds (default `180`; read timeout is derived as `+30`). |

Logging goes to **stderr only** — on the stdio transport, stdout is the MCP JSON-RPC channel. Known secrets are redacted from logs by exact match plus structural patterns.

---

## Security

winrdp-mcp is admin tooling for boxes **you own or are authorized to manage**. It is a remote code-execution surface by design.

- **Permissive first contact.** Defaults favor zero-setup provisioning: `winrm_cert_validation="ignore"`, `ssh_host_key_policy="auto"` (trust-on-first-use), and the enable script sets `AllowUnencrypted` + `Basic` + `TrustedHosts=*`. On an untrusted network this allows an on-path attacker to MITM. **For production**, register with `add_host(..., use_ssl=true, winrm_port=5986, winrm_cert_validation="validate", ssh_host_key_policy="reject")`, scope `TrustedHosts`, and restrict the firewall to your operator IP.
- **NTLM encrypts the payload over HTTP 5985.** The default `winrm_auth="ntlm"` seals the message body even without TLS, so `AllowUnencrypted`/`Basic` only weaken things if you explicitly select Basic. The `AllowUnencrypted will not work … network connection is Public` warning is harmless with NTLM.
- **Secrets on the box are transient.** Elevated runs / `user_create` / `service_create` briefly place a secret on a command line or temp `.ps1` on the target; `cmdkey` stores the RDP password on the operator machine. None are ever returned to the model, and logs redact known secrets by exact match.
- **Least privilege.** Narrow the tool surface with `WINRDP_ENABLED_TOOLS` / `WINRDP_DISABLED_TOOLS`, and keep destructive-action confirmation on in your client.

Full threat model, credential-vault internals, log redaction, argument-injection defenses, and a hardening checklist: **[docs/SECURITY.md](docs/SECURITY.md)**.

---

## Documentation

| Doc | Contents |
|-----|----------|
| **[docs/PREPARE-SERVER.md](docs/PREPARE-SERVER.md)** | Taking a fresh cloud/VDS/dedicated box from locked-down to managed: provider firewalls, cold-start options, RDP hardening, verification, sizing. |
| **[docs/PRODUCTION.md](docs/PRODUCTION.md)** | Running safely at scale: install options, vault, transport hardening, allow/block lists, observability, reliability, fleet management, background-service setup, checklist. |
| **[docs/SECURITY.md](docs/SECURITY.md)** | Threat model, trust boundary, credential handling, transport/MITM, the enable-WinRM script, argument-injection defense, hardening checklist. |
| **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** | Symptom → cause → fix for the failure modes you actually hit (provision failures, connection drops, slow elevation, Python/Node install, SSH banner, 5986 certs, MCP registration). |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Internal map for contributors: transports → PowerShell marshaling → provisioning → elevation → context → vault → tooling → server assembly. |
| **[docs/TOOLS.md](docs/TOOLS.md)** | The complete reference for all 136 tools — signatures, parameters, defaults, and safety class. |

---

## Architecture

winrdp-mcp is a [FastMCP](https://github.com/jlowin/fastmcp) server: tools call a shared `Context` that resolves the target host from an encrypted vault, hands the PowerShell body to a cached `Transport` (WinRM / SSH / Local / SMB+WMI), and marshals delimited JSON back — nothing above the transport layer knows *how* the command reached the box. Full internals in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Attribution & license

Built on and gratefully crediting two MIT-licensed projects — see [NOTICE](NOTICE):

- **[winremote-mcp](https://github.com/dddabtc/winremote-mcp)** — basis for the on-box tool surface, the risk-tier model, and the optional `deploy_ui_agent` interactive-desktop path.
- **[windows-admin-mcp](https://github.com/Cosmicjedi/windows-admin-mcp)** — basis for the WinRM-primary / SSH-fallback administration approach.

winrdp-mcp's own additions: the zero-config provisioning ladder, real UAC/elevation via one-shot Scheduled Tasks, on-demand tool staging, the encrypted multi-host inventory, and first-class RDP control.

**MIT** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
