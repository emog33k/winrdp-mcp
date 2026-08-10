# Production Deployment

How to run **winrdp-mcp** safely and reliably in a production or shared-team setting. This
document assumes you have read the top-level `README.md` and understand the two run modes
(`serve` controller vs. `agent` on-box). Everything below is verified against the source in
`winrdp_mcp/` — commands, env vars, defaults, and paths are real and copy-pasteable.

winrdp-mcp is admin tooling that holds credentials to and executes arbitrary code on
Windows hosts you own or are authorized to manage. Treat the operator host and the vault
key with the same care as those credentials.

## Table of contents

1. [Install options](#1-install-options)
2. [Secrets and the credential vault](#2-secrets-and-the-credential-vault)
3. [Transport hardening (WinRM / SSH)](#3-transport-hardening-winrm--ssh)
4. [Least privilege: tool allow/block lists](#4-least-privilege-tool-allowblock-lists)
5. [Observability and logging](#5-observability-and-logging)
6. [Reliability: self-heal, timeouts, detached installs](#6-reliability-self-heal-timeouts-detached-installs)
7. [Fleet at scale](#7-fleet-at-scale)
8. [Resource sizing and performance](#8-resource-sizing-and-performance)
9. [Running the controller as a background service](#9-running-the-controller-as-a-background-service)
10. [Production checklist](#10-production-checklist)

---

## 1. Install options

The controller (`serve`) runs wherever Claude Code lives (Windows, macOS, Linux); targets
are Windows. Python **3.10+** is required. Do **not** install into the system interpreter —
use an isolated environment so the dependency set (pywinrm, paramiko, smbprotocol,
cryptography, fastmcp) stays pinned and reproducible.

### Option A — virtualenv (recommended for a project/repo checkout)

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -e .                     # from a checkout of this repo
# or, once published to an index:
# pip install winrdp-mcp
```

Optional extras (opt-in; they pull heavier dependencies):

```bash
pip install -e ".[bootstrap]"   # impacket — SMB/WMI cold-start of boxes with WinRM AND SSH off
pip install -e ".[agent-ui]"    # winremote-mcp — interactive desktop (click/type/OCR) via on-box agent
pip install -e ".[dev]"         # ruff + pytest
```

### Option B — pipx (recommended for a global operator CLI)

pipx gives you the `winrdp-mcp` / `winrdp` console scripts on `PATH` in their own venv:

```bash
pipx install winrdp-mcp
# with an extra:
pipx install "winrdp-mcp[bootstrap]"
```

Both `winrdp-mcp` and `winrdp` are entry points for `winrdp_mcp.__main__:cli`. Invoking the
module directly works identically and needs no console script on `PATH`:

```bash
python -m winrdp_mcp serve
```

### Registering with Claude Code

Claude Code launches the MCP server as a **stdio subprocess** — you do not normally start
it yourself (see §9 for the HTTP-service exception). There are two registration paths.

**`claude mcp add`** — writes the server into your Claude config. Pick the scope
deliberately:

```bash
# local (default): only you, only in the current project directory
claude mcp add winrdp -- winrdp-mcp serve

# project: shared with the team via a committed .mcp.json in the repo root
claude mcp add -s project winrdp -- winrdp-mcp serve

# user: available to you across every project on this machine
claude mcp add -s user winrdp -- winrdp-mcp serve
```

**Project `.mcp.json`** — check a file into the repo so everyone on the team gets the same
server. The form that needs no console script on `PATH`:

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

If you install the console scripts (pipx / venv on `PATH`), you can instead use the form in
`claude-config.example.json`:

```json
{
  "mcpServers": {
    "winrdp": {
      "command": "winrdp-mcp",
      "args": ["serve"],
      "env": {
        "WINRDP_VAULT_KEY": "change-me-to-a-long-random-passphrase"
      }
    }
  }
}
```

**Scope guidance for production:** use **project** scope for a team that manages a shared
fleet from one repo, **user** scope for a single operator's workstation. Never put a real
`WINRDP_VAULT_KEY` (or any secret) into a `.mcp.json` you commit — see §2.

---

## 2. Secrets and the credential vault

Host passwords are encrypted at rest with **Fernet** (AES-128-CBC + HMAC-SHA256). The rest
of each host record — hostname, port, username, transport hints, tags — is stored in clear
so the inventory stays human-readable and diffable. Files live in the data directory
(§7): `inventory.json` (encrypted passwords under `password_enc`) and, if you don't supply
a key, `vault.key`.

### Set `WINRDP_VAULT_KEY`

The Fernet key is resolved by `winrdp_mcp/vault.py::_load_key()` in this order:

1. **`WINRDP_VAULT_KEY` env var** — accepted either as a raw 32-byte urlsafe-base64 Fernet
   key, or as an **arbitrary passphrase**. A passphrase is run through SHA-256 and
   urlsafe-base64-encoded to derive the key. Use a long, random passphrase:

   ```bash
   # generate a strong passphrase
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   ```powershell
   # PowerShell equivalent
   [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
   ```

   Provide it via your process environment or a secrets manager — **not** a committed file:

   ```bash
   export WINRDP_VAULT_KEY='…long-random-value…'
   ```

2. **On-disk `vault.key` fallback** — if the env var is unset, a random Fernet key is
   generated on first use and written owner-only (`0600`; on Windows the DACL is tightened
   with `icacls /inheritance:r /grant:r <user>:F`). The server logs a warning when it falls
   back to this path.

### Why the passphrase matters — the fallback's weakness

The on-disk `vault.key` sits **in the same directory as `inventory.json`**. Encryption-at-
rest with a co-located key only defends against *ciphertext-only* theft — someone who
exfiltrates `inventory.json` alone. Anyone who can read both files (a backup that captured
the whole data dir, a compromised operator account, a snapshot) can decrypt every stored
password. Setting `WINRDP_VAULT_KEY` from a secrets manager moves the key **off the disk**
that holds the ciphertext, which is the posture you want in production.

### Never commit secrets

- **Do not commit** `vault.key`, `inventory.json`, or any `.mcp.json` / config that contains
  a real `WINRDP_VAULT_KEY`. Add them to `.gitignore`.
- `list_hosts` and `Host.redacted()` always mask passwords (`***`); the plaintext password
  is never returned to the model.
- Rotating the key: because passwords are held decrypted in memory once loaded, a rotation
  is: stop the server → set the new `WINRDP_VAULT_KEY` → re-add hosts (or re-save the vault
  under the new key). A ciphertext encrypted under the old key will silently decrypt to an
  empty password under a new key (`InvalidToken` is swallowed to `""`).

---

## 3. Transport hardening (WinRM / SSH)

### Understand the default permissive posture

winrdp-mcp is built to make a box manageable **from any starting state with zero manual
setup**, so first contact is deliberately permissive. Two things to know:

- **Per-host security fields default to permissive** (`winrdp_mcp/vault.py`):
  `winrm_cert_validation="ignore"` and `ssh_host_key_policy="auto"` (trust-on-first-use).
- **The provisioning enable script** (`ENABLE_WINRM_PS` in `winrdp_mcp/provision.py`) sets,
  on the target: `AllowUnencrypted=$true`, `Auth\Basic=$true`, `Client\TrustedHosts='*'`,
  and `LocalAccountTokenFilterPolicy=1`, opens the firewall for 5985, and flips any `Public`
  network profile to `Private`.

This is what lets `provision_host` "just work" on a fresh box. On an **untrusted network it
allows an on-path attacker to MITM the session** — acceptable for a lab or a box you reach
over a trusted link, not for production over the open internet.

Important nuance: over HTTP 5985 with **NTLM** auth, the *payload is still encrypted* by
NTLM's message sealing. `AllowUnencrypted` / `Basic` only weaken things if you actually use
**Basic** auth (which sends credentials in the clear). Keep `winrm_auth="ntlm"` (the
default) and the 5985 path is confidential, if not authenticated against MITM.

### Production WinRM: HTTPS on 5986 with certificate validation

Stand up a WinRM HTTPS listener on the target with a certificate your operator host trusts
(internal CA or a real cert), then register the host to **require validation**:

```
add_host  alias="prod-web1"  host="web1.corp.example"  username="svc-admin"  password="…"
          use_ssl=true  winrm_port=5986  winrm_auth="ntlm"
          winrm_cert_validation="validate"
          ssh_host_key_policy="reject"
```

Behavior this drives (`winrdp_mcp/transports.py::WinRMTransport`):

- `use_ssl=True` → endpoint `https://host:5986/wsman`, default port 5986.
- `cert_validation="validate"` → pywinrm verifies the server certificate chain. With
  `"ignore"` on an HTTPS host the transport logs a **MITM-exploitable** warning at connect
  time — treat that warning as a production defect.

### Production SSH: reject unknown host keys

`ssh_host_key_policy="reject"` maps to paramiko's `RejectPolicy` (only keys already in the
operator's `known_hosts` are accepted); the default `"auto"` is `AutoAddPolicy`
(trust-on-first-use). Pre-populate `known_hosts` out of band, then set `"reject"` so a
swapped host key fails the connection instead of being silently trusted.

### Scope `TrustedHosts` and the firewall

The bootstrap sets `TrustedHosts='*'` on the **target** for frictionless first contact. Once
the box is reachable, tighten it to only the operator hosts that legitimately connect, and
restrict the 5985/5986 firewall rule to the operator's source IPs. Example, run on the box
via `run_powershell(elevated=True)` or in an RDP session:

```powershell
# Restrict which clients this box will accept over WinRM
Set-Item WSMan:\localhost\Client\TrustedHosts -Value 'operator1.corp.example,10.20.0.5' -Force

# If you moved to HTTPS-only, disable unencrypted and Basic again:
Set-Item WSMan:\localhost\Service\AllowUnencrypted -Value $false
Set-Item WSMan:\localhost\Service\Auth\Basic       -Value $false

# Scope the inbound rule to your operator subnet
Set-NetFirewallRule -Name 'WINRM-HTTPS-In-5986' -RemoteAddress 10.20.0.0/24
```

---

## 4. Least privilege: tool allow/block lists

The server registers **118 tools**. In production, expose only what the operator actually
needs. Two CSV env vars gate registration (`winrdp_mcp/server.py::_install_tool_wrapper`):

| Variable | Semantics |
|----------|-----------|
| `WINRDP_ENABLED_TOOLS` | **Allowlist.** If set, **only** these tools are registered; everything else is hidden. |
| `WINRDP_DISABLED_TOOLS` | **Blocklist.** These tools are skipped even if otherwise enabled. |

A skipped tool is **never registered with the MCP client**, so the model cannot see or call
it (the underlying Python function stays importable internally). Whitespace around names is
trimmed. If both are set, a tool must be in `ENABLED` **and** not in `DISABLED`.

**Block the destructive edge** (keep everything else) — a good default for a shared fleet:

```bash
export WINRDP_DISABLED_TOOLS="reboot,reboot_and_wait,power_action,file_delete,uninstall_software,clear_event_log,rdp_disable,rdp_set_port,user_delete,service_delete,remove_host"
```

**Curate a read-mostly diagnostic profile** with an explicit allowlist:

```bash
export WINRDP_ENABLED_TOOLS="list_hosts,use_host,test_host,system_info,performance,event_log,screenshot,list_services,list_processes,net_info,ping,port_check,defender_status,rdp_status,rdp_sessions,run_powershell"
```

Set these in the same `env` block as the server registration (per-project `.mcp.json`) so
they apply every time Claude Code launches the subprocess:

```json
{
  "mcpServers": {
    "winrdp": {
      "command": "python",
      "args": ["-m", "winrdp_mcp", "serve"],
      "env": {
        "WINRDP_DISABLED_TOOLS": "reboot,power_action,file_delete,uninstall_software"
      }
    }
  }
}
```

### Safety annotations and client-side gating

Independently of the allowlist, every tool carries MCP `ToolAnnotations`
(`readOnlyHint` / `destructiveHint` / `openWorldHint`) set in `server.py`:

- **`readOnlyHint=True`** — never mutates box state. Safe to auto-run. Examples:
  `list_hosts`, `system_info`, `performance`, `event_log`, `screenshot`, `file_read`,
  `file_list`, `list_services`, `rdp_status`, `rdp_sessions`, `net_info`, `defender_status`,
  `list_installed_software`.
- **`destructiveHint=True`** — deletes data, kills processes, cuts access, or reboots.
  Examples: `reboot`, `power_action`, `reboot_and_wait`, `file_delete`, `kill_process`,
  `user_delete`, `service_delete`, `reg_delete`, `firewall_delete`, `rdp_disable`,
  `clear_event_log`, `uninstall_software`, `remove_host`, `install_rdp_wrapper`,
  `rdp_connect_to_console`.
- Everything else is mutating-but-not-destructive (create/set/start).

MCP clients (including Claude Code) use `destructiveHint` to **gate those tools behind a
confirmation prompt** rather than auto-running them. Allowlisting and annotations are
complementary: annotations ask the human before a dangerous call; the allowlist removes the
call entirely.

---

## 5. Observability and logging

### stderr only — and why

All logging goes to **stderr** (`winrdp_mcp/log.py`). In stdio mode, **stdout is the MCP
JSON-RPC protocol channel** — writing anything else to it corrupts the framing and breaks
the session. Never add `print()` or route logs to stdout. Log line format:

```
%(asctime)s winrdp [%(levelname)s] %(name)s: %(message)s     (time as %H:%M:%S)
```

### Turn on debug logging

```bash
export WINRDP_DEBUG=1        # or --debug on the serve/agent command
```

`WINRDP_DEBUG` accepts `1`/`true`/`yes`. Default level is INFO.

**What gets logged:**

- **INFO** — server ready + host count; per-exec failures (`exec failed on <alias> …`);
  WinRM recoverable-error reconnect warnings; provisioning actions; the vault on-disk-key
  fallback warning.
- **DEBUG** — every `run_ps` (script truncated to 120 chars, newlines flattened); per-exec
  timing and which transport served it; the WinRM endpoint/auth on connect.

### Secret redaction

Logs scrub secrets two ways (`log.py`):

1. **Exact-match registration** — every password loaded from the vault is registered with
   `register_secret()` and replaced by `***` wherever it appears in a log line. This is the
   strong path (handles passwords with spaces/quotes that patterns miss).
2. **Structural patterns** — a best-effort regex fallback for secrets that weren't captured:
   `/pass:…`, `password=…`, `ConvertTo-SecureString '…'`, and `/RP "…"`.

Secrets are **never returned to the model**. Note the on-box caveat (from the README/NOTICE):
elevated runs and `user_create` / `service_create` briefly place a secret on a command line
or a temp `.ps1` **on the target**, visible to a local admin on that box while the command
runs; `rdp_open` / `rdp_connection_file` store the RDP password via `cmdkey` on the
**operator** machine. These are inherent to the operations, not logged, and acceptable for
boxes you control — know they exist.

---

## 6. Reliability: self-heal, timeouts, detached installs

### WinRM transport self-heal

A busy or small box will drop or wedge a WinRM connection under load. `WinRMTransport._run`
(`winrdp_mcp/transports.py`) detects a **recoverable** failure — `requests`
`ConnectionError` / `ChunkedEncodingError` / `Timeout`, an HTTP **400/500** on a
previously-good session, or `RemoteDisconnected` — tears down the pywinrm session,
**reconnects, and retries the call once**. A second failure propagates.

**Design constraint, do not "fix" this:** there is deliberately **no thread-based watchdog**
around pywinrm calls. pywinrm's `Session` (a `requests.Session` plus the open WSMan shell)
is **not thread-safe**; abandoning a call mid-flight corrupts the shell so every later
request returns HTTP 400. The per-call time bound comes from `read_timeout_sec`, and a
dropped/wedged connection is healed by the reconnect-and-retry above.

### Tune `WINRDP_WINRM_OP_TIMEOUT`

```bash
export WINRDP_WINRM_OP_TIMEOUT=180   # seconds; default 180
```

This sets the WinRM per-operation timeout. The read timeout is derived as
`operation_timeout + 30` (`read_timeout` must exceed `operation_timeout`). Raise it for
boxes that respond slowly under load (see §8); a too-low value turns slow-but-fine
operations into spurious `Timeout` retries.

### Detached long-running installs

A multi-minute install cannot be held open on a single WinRM request — a busy/small box
will drop the connection mid-install. `Context.run_long` (`winrdp_mcp/context.py`) runs such
work **detached**: it launches the command in an independent one-shot **SYSTEM Scheduled
Task** and then **polls a done-marker with short calls** that individually survive a
mid-install disconnect (the transport self-heals between polls). This is why a long install
completes even if the connection blips.

This detached path backs `install_software`, `ensure_runtime`, and `run_python` when it must
auto-install Python (`ensure_python=True`). Prefer these tools over a plain
`run_powershell(elevated=True)` for anything that takes minutes.

### Elevation, briefly

Over WinRM a local admin with `LocalAccountTokenFilterPolicy=1` already holds a
**high-integrity full token**, so `elevated=True` operations run **directly** (the fast,
common path). On a filtered token (SSH, or non-elevated local), elevation falls back to a
one-shot **SYSTEM Scheduled Task** via `Register-ScheduledTask`. `as_user=True` runs inside
the interactive RDP desktop session. This is transparent to the caller.

---

## 7. Fleet at scale

### Encrypted inventory + tags

Register each box once; credentials are encrypted at rest (§2). Tag boxes for grouping:

```
add_host  alias="web1"  host="10.20.0.11"  username="Administrator"  password="…"  tags="prod,web"
add_host  alias="web2"  host="10.20.0.12"  username="Administrator"  password="…"  tags="prod,web"
add_host  alias="db1"   host="10.20.0.21"  username="Administrator"  password="…"  tags="prod,db"
provision_host host="web1"      # once per box, right after add_host
```

`use_host` sets the active target; every tool takes an optional `host=` alias and defaults
to the active one.

### Parallel fan-out

`run_on_hosts` runs one PowerShell script across many boxes concurrently
(`ThreadPoolExecutor`, `max_parallel` default **8**) and returns per-host
`{stdout, stderr, rc}` keyed by alias:

```
run_on_hosts  script="(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"  tag="prod"
run_on_hosts  script="Get-Hotfix | Select -First 1"  aliases="web1,web2"  elevated=true  timeout=120
```

Select targets by `aliases` (comma-separated), by `tag`, or omit both to hit every
registered box. First-connects to different boxes open **outside the transport-cache lock**,
so a fan-out genuinely parallelizes instead of serializing on the first connection.

### Reboot and wait

`reboot_and_wait` restarts a box and blocks until WinRM/SSH is back (probing the relevant
port, then verifying `$env:COMPUTERNAME`) — essential for automation that must continue
across a restart:

```
reboot_and_wait  host="web1"  timeout=300  force=true
```

### Inventory location and backups

The data directory holds `inventory.json` and (if used) `vault.key`. Resolution
(`winrdp_mcp/config.py`):

- `WINRDP_HOME` if set (use this to place the inventory on a controlled/backed-up volume);
- else Windows: `%APPDATA%\winrdp-mcp\`;
- else POSIX: `$XDG_DATA_HOME/winrdp-mcp` or `~/.local/share/winrdp-mcp`.

```bash
export WINRDP_HOME=/srv/winrdp        # inventory + key live here
```

**Backup strategy:** back up `inventory.json` on a schedule so a lost operator host doesn't
lose the fleet definition. If you set `WINRDP_VAULT_KEY` from a secrets manager, you only
need to back up `inventory.json` (the key lives elsewhere) — this is the recommended split.
If you rely on the on-disk `vault.key`, backing it up alongside `inventory.json` recreates
the ciphertext-plus-co-located-key weakness in your backups too; store them separately.

---

## 8. Resource sizing and performance

**Recommended target: ≥ 4 GB RAM and 2 vCPU** for smooth operation.

This comes from live testing. On a **2 GB / low-CPU** box under sustained WinRM load, after
a package install **Windows Defender (`MsMpEng`) and `TiWorker`** peg CPU to ~100% and drive
free RAM down to ~300 MB. The box then drops WinRM connections: the transport self-heal (§6)
recovers each drop, but operations become **very slow**. On a right-sized box this storm is
brief and non-disruptive.

Practical mitigations on small/busy targets:

- Give installs the **detached path** (`install_software`, `ensure_runtime`,
  `run_python(ensure_python=True)`) so they survive the disconnects rather than failing.
- Raise `WINRDP_WINRM_OP_TIMEOUT` (§6) so slow-but-fine calls don't spuriously time out.
- During heavy install windows, consider a Defender exclusion for the staging directory
  `C:\ProgramData\winrdp-mcp` via `defender_exclusion_add` (re-evaluate afterward per your
  security policy).

**Harmless warning to ignore:** WinRM may emit *"AllowUnencrypted will not work … the
network connection is Public."* This only affects **Basic** auth; with NTLM the payload is
still encrypted, so it does not break management. The enable script already flips a `Public`
profile to `Private`; if you still see it, set the profile Private explicitly:

```powershell
Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' |
  Set-NetConnectionProfile -NetworkCategory Private
```

---

## 9. Running the controller as a background service

**Default and preferred model:** in stdio mode the MCP server is a **subprocess Claude Code
starts and stops for you** — you should *not* run a standalone `serve` (stdio) process. It
speaks JSON-RPC on stdin/stdout and has no meaning outside its parent client.

Run a persistent process **only for HTTP mode**, where multiple/remote clients connect to
one long-lived server:

```bash
winrdp-mcp serve --http --host 127.0.0.1 --port 8765
```

Security for HTTP mode:

- The HTTP transport has **no built-in authentication**. **Bind to `127.0.0.1`** (the
  `serve` default) and reach it via an SSH tunnel or a reverse proxy that adds auth/TLS.
  Do **not** bind `0.0.0.0` on an untrusted network.
- A health endpoint is exposed for watchdogs: `GET /health` →
  `{"status":"ok","version":…,"hosts":<count>}`.

  ```bash
  curl -s http://127.0.0.1:8765/health
  ```

### Windows — Scheduled Task at logon

Run the HTTP server under a dedicated operator account, with the vault key from the
environment (never on the command line):

```powershell
$py = "C:\srv\winrdp\.venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $py `
  -Argument "-m winrdp_mcp serve --http --host 127.0.0.1 --port 8765"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\winrdp-op" `
  -LogonType Password -RunLevel Limited
Register-ScheduledTask -TaskName "winrdp-mcp-http" -Action $action -Trigger $trigger `
  -Principal $principal -Description "winrdp-mcp HTTP controller"
```

Set `WINRDP_VAULT_KEY` (and any `WINRDP_*` config) as a **user or machine environment
variable** for that account rather than passing it as an argument, so it never appears in the
task definition or process command line.

### Linux — systemd unit

```ini
# /etc/systemd/system/winrdp-mcp.service
[Unit]
Description=winrdp-mcp HTTP controller
After=network-online.target

[Service]
Type=simple
User=winrdp
Environment=WINRDP_HOME=/srv/winrdp
EnvironmentFile=/etc/winrdp-mcp/env      # holds WINRDP_VAULT_KEY=… (chmod 600, root:winrdp)
ExecStart=/srv/winrdp/.venv/bin/winrdp-mcp serve --http --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now winrdp-mcp
curl -s http://127.0.0.1:8765/health
```

Keep `WINRDP_VAULT_KEY` in the `EnvironmentFile` (mode `600`), not inline in the unit.

---

## 10. Production checklist

- [ ] **Isolated install** — venv or pipx, Python 3.10+, dependencies pinned; not the system interpreter.
- [ ] **`WINRDP_VAULT_KEY` set** from a secrets manager to a long random passphrase; **not** relying on the on-disk `vault.key` co-located with `inventory.json`.
- [ ] **No secrets in VCS** — `vault.key`, `inventory.json`, and any config with a real `WINRDP_VAULT_KEY` are `.gitignore`d; committed `.mcp.json` carries no secrets.
- [ ] **Registration scope chosen** — project scope for a shared team fleet, user scope for a single operator.
- [ ] **Transport hardened** for internet-facing/untrusted links: HTTPS WinRM (`use_ssl=true`, port 5986, `winrm_cert_validation="validate"`) with a trusted cert; `ssh_host_key_policy="reject"` with a pre-populated `known_hosts`.
- [ ] **`TrustedHosts` scoped** on targets (not `*`) and the 5985/5986 firewall rule limited to operator source IPs; `AllowUnencrypted`/`Basic` disabled once on HTTPS.
- [ ] **Least privilege** — `WINRDP_ENABLED_TOOLS` / `WINRDP_DISABLED_TOOLS` set so only needed tools are exposed; destructive tools removed unless required.
- [ ] **Client confirmation on** for `destructiveHint` tools (default in Claude Code); verified for `reboot`, `file_delete`, `uninstall_software`, etc.
- [ ] **Logging to stderr only**; `WINRDP_DEBUG=1` available for diagnosis; confirmed no output goes to stdout.
- [ ] **`WINRDP_WINRM_OP_TIMEOUT` tuned** for the fleet's responsiveness under load.
- [ ] **Long installs use the detached tools** (`install_software`, `ensure_runtime`, `run_python(ensure_python=True)`), not raw elevated execs.
- [ ] **Targets sized ≥ 4 GB / 2 vCPU**; Defender/TiWorker post-install storm accounted for on smaller boxes.
- [ ] **`WINRDP_HOME` on a backed-up volume**; `inventory.json` backed up on a schedule (with the key stored separately).
- [ ] **HTTP mode (if used) bound to `127.0.0.1`**, fronted by SSH tunnel/authenticated proxy, health-checked via `GET /health`, and run as a service with the key supplied via the environment/EnvironmentFile.
