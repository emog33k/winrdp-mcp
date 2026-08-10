# Troubleshooting & FAQ

Symptom → cause → fix for the failure modes you actually hit running `winrdp-mcp`
against real Windows boxes (Win10/11, Server 2016–2025). Most transient errors are
self-healing; this page is for the ones that need you to change something.

Terminology used below:

- **Controller** — `winrdp-mcp serve` (stdio) running where Claude Code lives; reaches
  boxes over WinRM/SSH/SMB. Nothing is pre-installed on the target.
- **Agent** — `winrdp-mcp agent` running *on* the box (`transport=local`).
- **Provisioning ladder** — `provision_host` climbs WinRM(5985/5986) → SSH(22) →
  SMB(445)+DCOM(135) cold-start → paste-once bootstrap. See `winrdp_mcp/provision.py`.
- **Self-heal** — a dropped/wedged WinRM connection is transparently reconnected and the
  call retried once. See `winrdp_mcp/transports.py`.

## Table of contents

1. [`provision_host` fails / "No working transport"](#1-provision_host-fails--no-working-transport)
2. ["AllowUnencrypted will not work … network connection is Public"](#2-allowunencrypted-will-not-work--network-connection-is-public)
3. ["Access is denied" over WinRM for a non-builtin local admin](#3-access-is-denied-over-winrm-for-a-non-builtin-local-admin)
4. [Connection drops / RemoteDisconnected / HTTP 400 during heavy ops](#4-connection-drops--remotedisconnected--http-400-during-heavy-ops)
5. [Very slow `elevated` or `as_user` operations](#5-very-slow-elevated-or-as_user-operations)
6. [`run_python` slow, or "Python not found"](#6-run_python-slow-or-python-not-found)
7. [`run_node` fails](#7-run_node-fails)
8. [SSH "Error reading SSH protocol banner"](#8-ssh-error-reading-ssh-protocol-banner)
9. [HTTPS / WinRM 5986 certificate errors](#9-https--winrm-5986-certificate-errors)
10. [MCP server not showing up in Claude / Claude Code](#10-mcp-server-not-showing-up-in-claude--claude-code)
11. [Collecting debug info](#collecting-debug-info)
12. [Production sizing & hardening quick reference](#production-sizing--hardening-quick-reference)

---

## 1. `provision_host` fails / "No working transport"

**Symptom.** `provision_host` returns `success: false` with a message like *"Could not
auto-enable remoting. Paste bootstrap_oneliner into an RDP/console session …"*, or a later
tool call raises `No working transport for <alias> (<host>). Run provision() first.`

**Cause.** The ladder found no reachable management port. In practice the box's WinRM is
off **and** the cloud/hypervisor firewall (security group / NSG / `netsh` profile) blocks
the ports the ladder needs. `provision()` scans `winrm_http(5985)`, `winrm_https(5986)`,
`ssh(22)`, `rdp(3389)`, `smb(445)`, `dcom(135)` — if none but RDP answer, it can only hand
you the paste-once one-liner (Rung 4). The SMB+WMI cold-start (Rung 3) additionally needs
**445 and 135** open and the optional `impacket` dependency.

**Fix — checklist (top to bottom):**

1. Confirm what is actually reachable. `reachable_ports` in the provision report tells you;
   or run `test_host` (see [§11](#collecting-debug-info)). From your shell:

   ```powershell
   Test-NetConnection <host> -Port 5985   # WinRM HTTP
   Test-NetConnection <host> -Port 22     # SSH
   Test-NetConnection <host> -Port 445    # SMB (for cold-start)
   Test-NetConnection <host> -Port 135    # DCOM/RPC (for cold-start)
   ```

2. **Open the ports in the provider firewall**, not just Windows Firewall. AWS security
   group / Azure NSG / GCP firewall rules must allow inbound 5985 (or 5986) from your
   controller's IP. The provisioning script opens the *Windows* firewall for you; it cannot
   touch the provider's edge.

3. If only **RDP (3389)** is reachable, use the paste-once bootstrap. Get the one-liner and
   run it **once** in an elevated PowerShell inside your existing RDP/console session:

   ```powershell
   # On the controller — print the one-liner (and the readable script it wraps):
   winrdp-mcp bootstrap
   # or:  python -m winrdp_mcp bootstrap
   ```

   It emits a single self-contained command of the form:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand <base64…>
   ```

   Paste that into the box (elevated), then re-run `provision_host`. The decoded script is
   idempotent: it enables PSRemoting, sets WinRM to Automatic + starts it, runs
   `winrm quickconfig`, sets `LocalAccountTokenFilterPolicy=1`, and opens the 5985 firewall
   rule. (It does not touch Basic/AllowUnencrypted/TrustedHosts — NTLM needs none of them.)

4. If **445 and 135** are open but the cold-start rung was skipped with
   `wmi-bootstrap-unavailable`, install the optional extra on the controller:

   ```powershell
   pip install "winrdp-mcp[bootstrap]"   # pulls impacket for the DCOM/WMI trigger
   ```

5. If **SSH (22)** is reachable, provisioning uses it to turn WinRM on automatically — no
   action needed beyond making sure the creds in `add_host` are correct.

> Re-run `provision_host` after any change. It is safe to run repeatedly; each rung is
> idempotent and the WinRM config is re-hardened on every successful connect.

---

## 2. "AllowUnencrypted will not work … network connection is Public"

**Symptom.** During enable/provision you see a warning:
*"WinRM firewall exception will not work … network connection … is currently Public"* or
*"Set-Item … AllowUnencrypted … will not work because the network connection type is
Public."*

**Cause.** WinRM refuses to relax `AllowUnencrypted` while the active network profile is
**Public**. This only affects **Basic** auth. `winrdp-mcp` uses **NTLM** by default, and
NTLM encrypts the SOAP payload even over plain HTTP 5985 — so the warning is **harmless**
and remoting works regardless.

**Fix (optional).** If you want the warning gone (or you intend to use Basic auth), set the
profile to Private. The provisioning script already attempts this
(`Set-NetConnectionProfile -NetworkCategory Private`); do it by hand if a specific adapter
was still Public:

```powershell
Get-NetConnectionProfile |
  Where-Object { $_.NetworkCategory -eq 'Public' } |
  Set-NetConnectionProfile -NetworkCategory Private
```

Then re-run enable/provision. No change is needed if you are on NTLM and only saw the
warning — the connection is already encrypted.

---

## 3. "Access is denied" over WinRM for a non-builtin local admin

**Symptom.** WinRM connects but privileged operations fail with *Access is denied*
(HTTP 401/500-ish), even though the account is a member of the local Administrators group.
Common with a *second* local admin account (anything other than the built-in `Administrator`).

**Cause.** UAC **remote token filtering**. Over the network, a non-builtin local admin
receives a *filtered* (standard-user) token, so admin rights are stripped. The built-in
`Administrator` is exempt; your custom admin is not.

**Fix.** Set `LocalAccountTokenFilterPolicy = 1`, which grants non-builtin local admins a
full token over the network. **`provision_host` sets this for you** as part of the enable
script:

```
HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System
    LocalAccountTokenFilterPolicy = 1   (DWORD)
```

If provisioning could not run (e.g. you enabled WinRM by other means), set it manually on
the box and reconnect:

```powershell
New-ItemProperty `
  -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' `
  -Name LocalAccountTokenFilterPolicy -Value 1 -PropertyType DWord -Force
```

No reboot is required for this value. After it is set, a local admin over WinRM holds a
high-integrity token and elevated ops run **directly** (no scheduled-task detour — see
[§5](#5-very-slow-elevated-or-as_user-operations)). Domain accounts are unaffected by this
policy.

---

## 4. Connection drops / RemoteDisconnected / HTTP 400 during heavy ops

**Symptom.** During a package install or other sustained load you see, in debug logs,
`recoverable error (RemoteDisconnected)` / `Code 400` / `Code 500`, ops become very slow,
and calls occasionally fail after the retry.

**Cause.** The box is **thrashing**. Confirmed on a live 2 GB / low-vCPU VM: right after a
package install, **Windows Defender (`MsMpEng.exe`) plus `TiWorker.exe`** (component-based
servicing) peg CPU to ~100% and drop free RAM to ~300 MB. Under that load the remote WSMan
shell times out and the socket closes (`RemoteDisconnected`), or the shell wedges and
answers every subsequent request with HTTP 400.

**What happens automatically.** The WinRM transport self-heals: `_is_recoverable` classifies
these as transient, tears down the pywinrm `Session`, reconnects, and retries the call
**once** (`winrdp_mcp/transports.py`). A single blip is invisible to you; a sustained storm
just makes everything slow. Long installs additionally run **detached** via
`run_long` → a one-shot SYSTEM Scheduled Task that is polled with short calls, so the
install survives a mid-install disconnect (`install_software`, `run_python`'s auto-install,
`ensure_runtime`).

> Design note baked into the code: pywinrm's `Session` (a `requests.Session` + the open
> WSMan shell) is **not thread-safe**. There is deliberately **no** thread-based watchdog
> around WinRM calls — abandoning one mid-flight corrupts the shell so every later request
> returns HTTP 400. The per-call bound is `read_timeout_sec`, and recovery is the
> reconnect-and-retry above. Do not add a watchdog thread.

**Fix.**

- **Size up.** Use **≥ 4 GB RAM / 2 vCPU** for smooth operation. A 2 GB box works but
  crawls under sustained WinRM load.
- **Wait out the post-install storm.** Give Defender/`TiWorker` a minute or two to settle
  after a big install before firing more heavy ops. Watch it:

  ```powershell
  # via a quick run_on_hosts / run_script call, or in an RDP session on the box:
  Get-Process MsMpEng, TiWorker -ErrorAction SilentlyContinue |
    Sort-Object CPU -Descending |
    Select-Object Name, CPU, @{n='WS_MB';e={[int]($_.WS/1MB)}}
  ```

- **Raise the per-op timeout** if a legitimately long *single* call keeps hitting the read
  timeout on a slow box:

  ```powershell
  $env:WINRDP_WINRM_OP_TIMEOUT = "300"   # seconds; default 180. read_timeout = this + 30
  ```

  Set it in the MCP server's environment (e.g. the `env` block of your `.mcp.json`) and
  restart the server.

---

## 5. Very slow `elevated` or `as_user` operations

**Symptom.** A tool call with `elevated=True` or `as_user=True` takes many seconds to a
minute, far longer than a normal call.

**Cause.** Both go through a **Scheduled Task round-trip**, and every step is a separate
WinRM request (register task → start → poll a done-marker → read out/err/done → unregister),
so WinRM latency multiplies:

- `elevated=True` → if the session token is *filtered* (SSH, or non-elevated local), the
  command runs through a one-shot **SYSTEM Scheduled Task** with `RunLevel Highest`
  (`winrdp_mcp/elevation.py:run_elevated`).
- `as_user=True` → always runs inside the **interactive console user's** session via a task
  bound to that user (needed for GUI/clipboard/screenshot). This is inherently heavy and
  polls for completion (`run_in_user_session`).

**Fix / expectations.**

- **Use WinRM with a full token to make elevation cheap.** `Context.exec_ps` checks
  `transport.is_elevated()`; over WinRM a local admin with
  `LocalAccountTokenFilterPolicy=1` ([§3](#3-access-is-denied-over-winrm-for-a-non-builtin-local-admin))
  already holds a high-integrity token, so `elevated=True` runs **directly** with no task
  detour. If your elevated calls are slow, you are probably on SSH or a filtered token —
  switch that box to WinRM and ensure the token-filter policy is set.
- **`as_user` is unavoidably slow** — it exists to touch the visible desktop (clipboard,
  `screenshot`, GUI launch). Expect scheduled-task overhead; it is not a fast path. Use it
  only when you genuinely need the interactive session, and keep `timeout` generous.
- Don't wrap these in your own tight retry loop; on a busy box let the single self-heal
  retry and the task poller do their job.

---

## 6. `run_python` slow, or "Python not found"

**Symptom.** First `run_python` on a fresh box takes minutes; or it returns
`{"error": "Python not found and could not be installed. Call ensure_runtime or install
manually."}`.

**Cause.** With `ensure_python=True` (the default), a missing interpreter triggers an
**auto-install that runs detached** through `run_long` (SYSTEM Scheduled Task + poll), so it
survives a mid-install disconnect. The installer chain is
**winget (`Python.Python.3.12`, machine scope) → choco (`python`) → direct python.org
`.exe` (3.12.4)** (`winrdp_mcp/tools/scripting.py:_install_python`). That download+install is
genuinely multi-minute on a small/slow box. `_find_python` then resolves the concrete
`python.exe` path by globbing standard install dirs (not just PATH), so it works in the same
session even though a fresh WinRM shell may still have a stale PATH.

**Fix.**

- **Give it internet + admin.** The install needs outbound access to winget/choco/python.org
  and runs elevated (machine scope). No package manager and no internet → it fails; that's
  the error above.
- **Pre-install once** to make later calls instant:

  ```
  ensure_runtime(runtime="python")
  ```

  or install Python on the box yourself (any of the standard locations
  `C:\Program Files\Python3*`, `%LOCALAPPDATA%\Programs\Python\Python3*`, `py.exe` are found).
- **Be patient on the first call** — raise `run_python`'s `timeout` (default 600 s) if a slow
  box needs longer to download and install.
- Note: `_find_python` deliberately **ignores the `WindowsApps` Python stub**; the App
  Execution Alias won't satisfy it.

---

## 7. `run_node` fails

**Symptom.** `run_node` (or `run_script interpreter=node`) errors with something like
*'node' is not recognized* / command-not-found.

**Cause.** Unlike Python, **Node is not auto-installed** by `run_node`. The runner just calls
`& node …`; if Node isn't on the box (and on PATH for the session), it fails.

**Fix.** Install the Node runtime first, then retry:

```
ensure_runtime(runtime="node")
```

That installs via **winget (`OpenJS.NodeJS`) → choco (`nodejs`)**, elevated
(`winrdp_mcp/tools/scripting.py`). If neither package manager is present it returns
`{"installed": false, "error": "no package manager; call ensure_package_manager"}` — install
a package manager (or Node manually) and retry. As with Python, a brand-new WinRM shell may
have a stale PATH; opening a fresh call/session after install picks Node up.

---

## 8. SSH "Error reading SSH protocol banner"

**Symptom.** Provision/connect over SSH raises paramiko's
`Error reading SSH protocol banner` (or it hangs then times out), even though port 22 shows
open.

**Cause.** Something is listening on 22 but it is **not the Windows OpenSSH server** — no
valid SSH banner is sent. Typical culprits: a port-forward/proxy, a different service bound
to 22, or a half-installed `sshd` that never started.

**Fix.**

- **Prefer WinRM.** It is the primary, richer transport; SSH is a fallback rung. If WinRM is
  reachable, just use it (`transport="winrm"` or leave `transport="auto"`).
- **Actually enable OpenSSH** on the box. Over an existing WinRM/RDP session:

  ```
  enable_ssh(host="<alias>")
  ```

  This installs the `OpenSSH.Server` capability, sets `sshd` to Automatic + starts it, opens
  the 22 firewall rule, and sets PowerShell as the default SSH shell
  (`winrdp_mcp/tools/provisioning.py` / `ENABLE_SSH_PS`). Verify on the box:

  ```powershell
  Get-Service sshd
  Get-NetTCPConnection -LocalPort 22 -State Listen |
    Select-Object OwningProcess, @{n='Proc';e={(Get-Process -Id $_.OwningProcess).Name}}
  ```

  You want the listener owned by `sshd.exe`. If it's owned by something else, that other
  service is why the banner read fails.

---

## 9. HTTPS / WinRM 5986 certificate errors

**Symptom.** Connecting to 5986 throws a TLS/certificate validation error, **or** (the
opposite worry) you see a log warning that HTTPS is running with cert validation **disabled**
and MITM-exploitable.

**Cause.** By default a host is created with `winrm_cert_validation="ignore"`, so
self-signed certs are accepted and 5986 "just works" — but that is insecure for production.
When you (correctly) set validation to `validate` and the box only has a self-signed / wrong
CN / expired cert, the TLS handshake fails.

**Fix.** Choose per host:

- **Quick/lab (default):** `winrm_cert_validation="ignore"` accepts any cert. You'll see a
  one-time log warning that validation is disabled — expected.
- **Production:** register the box with proper validation and a trusted cert:

  ```
  add_host(
    alias="prod-web",
    host="10.0.0.5",
    username="svc-admin",
    password="…",
    use_ssl=true,
    winrm_port=5986,
    winrm_cert_validation="validate",
    ssh_host_key_policy="reject"
  )
  ```

  Then ensure the WinRM HTTPS listener presents a certificate your controller trusts (issued
  by a CA in the controller's trust store, correct CN/SAN = the host you connect to, not
  expired). If you must keep a self-signed cert, either import it into the controller's
  trusted store or fall back to `ignore` (understanding the risk). Note the default endpoint
  preference is HTTP 5985 (NTLM already encrypts the payload); HTTPS is tried first only when
  the host sets `use_ssl`.

---

## 10. MCP server not showing up in Claude / Claude Code

**Symptom.** After adding `winrdp-mcp` to your MCP config, the `winrdp` tools don't appear
in Claude Code, or the server shows as failed.

**Cause.** The client reads MCP config at startup; a running client won't pick up a
newly-added `.mcp.json`. Or the launch command itself is wrong (package not installed in the
interpreter the config points at, wrong args, bad `cwd`).

**Fix.**

1. **Verify the server runs standalone** in the exact interpreter your config uses:

   ```powershell
   python -m winrdp_mcp --help
   python -m winrdp_mcp serve --help
   ```

   If that errors, the package isn't installed for that Python. Install it (editable dev
   install from the repo root):

   ```powershell
   pip install -e .
   ```

2. **Confirm the config** is a project `.mcp.json` with a correct entry:

   ```json
   {
     "mcpServers": {
       "winrdp": {
         "command": "python",
         "args": ["-m", "winrdp_mcp", "serve"],
         "env": {
           "WINRDP_VAULT_KEY": "change-me-to-a-long-random-passphrase"
         }
       }
     }
   }
   ```

   Or register it via the CLI: `claude mcp add`.

3. **Restart Claude Code** (fully quit and relaunch) so it re-reads the config and spawns the
   server. This is the single most common fix.

4. If it still fails, launch with debug and read stderr (see below) — an import error or a
   bad `WINRDP_VAULT_KEY` will surface there. Remember: on stdio, **stdout is the protocol
   channel** — all logs go to stderr, so nothing the server logs can corrupt the MCP stream.

---

## Collecting debug info

When you file an issue or need to see what's actually happening, gather these.

**1. Turn on debug logging (stderr).** Either env var or flag:

```powershell
# Env var (works via the MCP config `env` block too):
$env:WINRDP_DEBUG = "1"          # accepts 1 / true / yes
python -m winrdp_mcp serve

# Or the flag:
python -m winrdp_mcp serve --debug
```

Debug logs go to **stderr** with per-call timing (`exec <alias> [mode via transport] <secs>`),
self-heal warnings (`recoverable error … reconnecting + retrying once`), and transport
setup. Secrets are scrubbed: known passwords are redacted by exact match and structural
patterns (`/pass:`, `password=…`, `ConvertTo-SecureString`) are masked to `***`
(`winrdp_mcp/log.py`).

**2. Probe a specific box** with the built-in diagnostic tool — it scans the management ports
and verifies the live transport in one shot:

```
test_host(host="<alias>")
```

Returns `{alias, host, ports:{winrm_http, winrm_https, ssh, rdp, smb, dcom}, transport,
online, computername}` (or `online:false` + `error`). This is the fastest way to see *which
transport actually works* and *which ports are open*.

**3. Re-run provisioning for the full ladder report:**

```
provision_host(host="<alias>")
```

The report includes `reachable_ports`, the chosen `transport`, `winrm_enabled`/`ssh_enabled`,
the `actions` taken (e.g. `winrm-hardened`, `bootstrap-staged-over-smb`,
`wmi-bootstrap-failed: …`), and the `bootstrap_oneliner` fallback.

**4. From the shell**, the same is available without a client:

```powershell
python -m winrdp_mcp list-hosts
python -m winrdp_mcp provision <alias>     # prints the JSON report
python -m winrdp_mcp bootstrap             # the paste-once enable-WinRM one-liner
```

Include: the debug stderr around the failure, the `test_host` / `provision` JSON, the
Windows build of the target, and the box's RAM/vCPU (thrashing is size-related — see §4).

---

## Production sizing & hardening quick reference

| Concern | Recommendation |
| --- | --- |
| **VM size** | **≥ 4 GB RAM / 2 vCPU.** 2 GB thrashes under sustained WinRM load (Defender + TiWorker peg CPU/RAM after installs). |
| **Transport** | WinRM over **HTTPS 5986** with `winrm_cert_validation="validate"` and a trusted cert. |
| **SSH** | `ssh_host_key_policy="reject"` (known_hosts only) instead of trust-on-first-use. |
| **TrustedHosts / AllowUnencrypted / Basic** | Not set by provisioning (0.1.1+) — NTLM encrypts the payload without them. If you provisioned with ≤ 0.1.0, undo them: `Set-Item WSMan:\localhost\Service\Auth\Basic $false`, `…\AllowUnencrypted $false`, `Clear-Item WSMan:\localhost\Client\TrustedHosts -Force`. |
| **Vault key** | Set `WINRDP_VAULT_KEY` to a strong passphrase; it encrypts stored passwords (Fernet). Without it a machine-local `vault.key` is used. |
| **Per-op timeout** | `WINRDP_WINRM_OP_TIMEOUT` (seconds, default 180; read timeout = +30). Raise for slow boxes. |
| **Tool surface** | Restrict with `WINRDP_ENABLED_TOOLS` (CSV allowlist) / `WINRDP_DISABLED_TOOLS` (CSV blocklist, e.g. `reboot,file_delete`). |
| **Secrets on the box** | Elevated runs / `user_create` / `service_create` briefly place a secret on a command line or temp `.ps1`; `cmdkey` stores the RDP password on the **operator** machine. Secrets are never returned to the model and logs redact them. |
