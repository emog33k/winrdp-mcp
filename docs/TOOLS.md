# Tool Reference

The complete catalog of every tool exposed by the `winrdp-mcp` FastMCP server. Each entry is taken directly from the tool's `@mcp.tool` definition in `winrdp_mcp/tools/*.py`; parameter lists, types, and defaults are the actual Python signatures.

**Every tool accepts an optional `host=` argument** (except the four inventory tools whose target *is* the host: `add_host`, `list_hosts`, `use_host`, `remove_host`). When `host=` is omitted, the tool targets the **active host** (set with `use_host`, or the most recently added host). Pass an alias to hit a specific box. This lets you administer many boxes from one server.

## Contents

- [Safety classification](#safety-classification)
- [Summary](#summary)
- [Hosts & Fleet](#hosts--fleet) — `winrdp_mcp/tools/hosts.py`
- [Provisioning / UAC / Tooling](#provisioning--uac--tooling) — `winrdp_mcp/tools/provisioning.py`
- [System](#system) — `winrdp_mcp/tools/system.py`
- [Scripting](#scripting) — `winrdp_mcp/tools/scripting.py`
- [Files](#files) — `winrdp_mcp/tools/files.py`
- [Admin](#admin) — `winrdp_mcp/tools/admin.py`
- [RDP](#rdp) — `winrdp_mcp/tools/rdp.py`
- [Software](#software) — `winrdp_mcp/tools/software.py`
- [Network](#network) — `winrdp_mcp/tools/network.py`
- [Windows](#windows) — `winrdp_mcp/tools/windows.py`

## Safety classification

Every tool carries an MCP `ToolAnnotations` safety hint, assigned from the `READONLY` and `DESTRUCTIVE` sets in `winrdp_mcp/server.py`. All tools also get `openWorldHint=True` (they act on external systems). MCP clients (Claude / Claude Code) use these hints to decide what to auto-run versus gate behind confirmation.

| Class | Annotation | Meaning |
| --- | --- | --- |
| **read-only** | `readOnlyHint=True, destructiveHint=False` | Never mutates box state; safe to auto-run. |
| **destructive** | `readOnlyHint=False, destructiveHint=True` | Deletes data, kills processes, cuts access, or reboots. Clients should gate behind confirmation. |
| **mutating** | `readOnlyHint=False, destructiveHint=False` | Creates / sets / starts state, but is not destructive. |

Tool visibility can be narrowed at startup: `WINRDP_ENABLED_TOOLS` (CSV allow-list — only these register) and `WINRDP_DISABLED_TOOLS` (CSV block-list) are read by `_install_tool_wrapper`.

## Summary

| Group | Module | Tools | read-only | destructive | mutating |
| --- | --- | --: | --: | --: | --: |
| Hosts & Fleet | `hosts.py` | 8 | 2 | 2 | 4 |
| Provisioning / UAC / Tooling | `provisioning.py` | 10 | 2 | 2 | 6 |
| System | `system.py` | 8 | 4 | 2 | 2 |
| Scripting | `scripting.py` | 6 | 0 | 0 | 6 |
| Files | `files.py` | 16 | 6 | 1 | 9 |
| Admin | `admin.py` | 25 | 6 | 7 | 12 |
| RDP | `rdp.py` | 12 | 3 | 5 | 4 |
| Software | `software.py` | 4 | 1 | 1 | 2 |
| Network | `network.py` | 8 | 5 | 1 | 2 |
| Windows | `windows.py` | 11 | 5 | 1 | 5 |
| **Total** | | **108** | **34** | **22** | **52** |

---

## Hosts & Fleet

Multi-host inventory: add / list / select / remove / test / provision, plus parallel fan-out. Credentials are stored encrypted at rest (`winrdp_mcp/vault.py`, Fernet).

#### `add_host` — mutating
Register a Windows box in the encrypted inventory (`transport`: `auto`/`winrm`/`ssh`/`local`; creds encrypted at rest).
```python
add_host(alias: str, host: str, username: str = "", password: str = "", domain: str = "",
         transport: str = "auto", use_ssl: bool = False, winrm_auth: str = "ntlm",
         winrm_port: int = 5985, ssh_port: int = 22, rdp_port: int = 3389,
         winrm_cert_validation: str = "ignore", ssh_host_key_policy: str = "auto",
         tags: str = "", make_active: bool = True)
```

#### `list_hosts` — read-only
List all registered boxes (passwords redacted) and which one is active.
```python
list_hosts()
```

#### `use_host` — mutating
Make a registered box the active target for subsequent tool calls.
```python
use_host(alias: str)
```

#### `remove_host` — destructive
Remove a box from the inventory.
```python
remove_host(alias: str)
```

#### `test_host` — read-only
Scan reachable management ports and verify the live transport for a box.
```python
test_host(host: Optional[str] = None)
```

#### `provision_host` — mutating
Zero-config provision: climb the WinRM → SSH → SMB/WMI → bootstrap ladder to make a box remotely manageable no matter its state.
```python
provision_host(host: Optional[str] = None, enable_ssh: bool = False,
               allow_wmi_bootstrap: bool = True)
```

#### `run_on_hosts` — mutating
Run one PowerShell script across many boxes in parallel and collect per-host `{stdout, stderr, rc}`. Select by `aliases` (CSV) or `tag`; omit both to hit every box.
```python
run_on_hosts(script: str, aliases: str = "", tag: str = "", elevated: bool = False,
             timeout: int = 120, max_parallel: int = 8)
```

#### `reboot_and_wait` — destructive
Reboot a box and block until it is reachable again (WinRM/SSH back up).
```python
reboot_and_wait(host: Optional[str] = None, timeout: int = 300, force: bool = True)
```

---

## Provisioning / UAC / Tooling

On-demand tooling staging, UAC/token policy, elevation, remoting enablement, and the interactive-desktop UI agent. Staged tools cache at `C:\ProgramData\winrdp-mcp\tools`.

#### `stage_tool` — mutating
Pull a helper tool onto a box on demand: a preset (`psexec`, `handle`, `procdump`, `autoruns`, `tcpview`, `pslist`, `accesschk`, `sigcheck`), an `http(s)` URL (downloaded on the box), or a local operator-machine file (uploaded).
```python
stage_tool(source: str, name: Optional[str] = None, host: Optional[str] = None)
```

#### `stage_script` — mutating
Write a script (PowerShell/batch/py/text) into the box's tool cache and return its remote path.
```python
stage_script(content: str, name: str, host: Optional[str] = None)
```

#### `list_staged_tools` — read-only
List tools/scripts staged on a box.
```python
list_staged_tools(host: Optional[str] = None)
```

#### `cleanup_staged` — destructive
Remove a staged tool by name, or wipe the whole tool cache if `name` is omitted.
```python
cleanup_staged(name: Optional[str] = None, host: Optional[str] = None)
```

#### `run_elevated` — mutating
Run PowerShell with a full elevated token via a one-shot Scheduled Task (`run_as='SYSTEM'` needs no password). The real UAC-bypass path when a normal logon gets a filtered token.
```python
run_elevated(script: str, host: Optional[str] = None, run_as: str = "SYSTEM",
             timeout: int = 300)
```

#### `uac_get` — read-only
Read the box's UAC policy (`EnableLUA`, admin prompt behaviour, token filter).
```python
uac_get(host: Optional[str] = None)
```

#### `uac_set` — destructive
Adjust UAC policy. `disable_token_filter=True` lets non-builtin local admins get a full token over the network (`LocalAccountTokenFilterPolicy=1`). `EnableLUA` changes need a reboot.
```python
uac_set(host: Optional[str] = None, enable_lua: Optional[bool] = None,
        disable_admin_prompt: bool = False, disable_token_filter: bool = False)
```

#### `enable_winrm` — mutating
(Re)run the WinRM enablement script on a box through the current transport.
```python
enable_winrm(host: Optional[str] = None)
```

#### `enable_ssh` — mutating
Install and start the Windows OpenSSH server on a box, open the firewall, and set PowerShell as the default SSH shell.
```python
enable_ssh(host: Optional[str] = None)
```

#### `deploy_ui_agent` — mutating
Deploy the interactive-desktop UI agent (`winremote-mcp`) onto a box for click/type/OCR control and start it as an HTTP MCP endpoint. Requires Python + pip on the box.
```python
deploy_ui_agent(host: Optional[str] = None, port: int = 8765,
                auth_key: Optional[str] = None)
```

---

## System

Shell execution, system facts, live performance, event log, live-desktop screenshot, and power control.

#### `run_powershell` — mutating
Run a PowerShell script on a box — the universal escape hatch. `elevated=True` runs with a full unfiltered token (Scheduled Task); `as_user=True` runs inside the interactive RDP desktop session.
```python
run_powershell(script: str, host: Optional[str] = None, elevated: bool = False,
               as_user: bool = False, timeout: int = 120)
```

#### `run_cmd` — mutating
Run a `cmd.exe` command line on a box. Returns `{stdout, stderr, rc}`.
```python
run_cmd(command: str, host: Optional[str] = None, timeout: int = 120)
```

#### `system_info` — read-only
Comprehensive box facts: OS name/version/build/edition, hostname, domain, uptime, CPU, memory, disks, IPs, server-SKU flag.
```python
system_info(host: Optional[str] = None)
```

#### `performance` — read-only
Live CPU %, memory, per-disk free space, and the top processes by memory.
```python
performance(host: Optional[str] = None, top: int = 8)
```

#### `event_log` — read-only
Read recent Windows event-log entries. `log`: System|Application|Security|…; `level`: Error|Warning|Information|Critical.
```python
event_log(log: str = "System", count: int = 50, level: Optional[str] = None,
          source: Optional[str] = None, host: Optional[str] = None)
```

#### `screenshot` — read-only
Capture the interactive RDP desktop of a box and return it as a PNG image (runs in the logged-on user's session; requires a connected RDP session).
```python
screenshot(host: Optional[str] = None, monitor: int = 0)
```

#### `reboot` — destructive
Reboot a box after a short delay.
```python
reboot(host: Optional[str] = None, delay_seconds: int = 5, force: bool = True)
```

#### `power_action` — destructive
Power control. `action`: shutdown | reboot | logoff | cancel.
```python
power_action(action: str = "shutdown", host: Optional[str] = None, delay_seconds: int = 5)
```

---

## Scripting

One-shot script execution: the runtime is located, installed on demand if missing, dependencies pip-installed, code staged and executed, and `stdout`/`stderr`/`rc` returned. All six are mutating.

#### `run_python` — mutating
Run inline Python code on a box. `pip` installs packages first; `ensure_python=True` auto-installs Python (winget) if missing; `elevated`/`as_user` for full-token / interactive-desktop execution.
```python
run_python(code: str, host: Optional[str] = None, args: str = "", pip: str = "",
           ensure_python: bool = True, elevated: bool = False, as_user: bool = False,
           timeout: int = 600)
```

#### `run_python_file` — mutating
Upload a `.py` file from the operator machine and run it on a box.
```python
run_python_file(local_path: str, host: Optional[str] = None, args: str = "",
                pip: str = "", elevated: bool = False, timeout: int = 600)
```

#### `pip_install` — mutating
pip-install one or more packages on a box (comma/space separated).
```python
pip_install(packages: str, host: Optional[str] = None, timeout: int = 600)
```

#### `run_script` — mutating
Run a script in any interpreter on a box. `interpreter`: auto | python | node | powershell | cmd | bat | vbscript (`auto` guesses from shebang/syntax, defaulting to PowerShell).
```python
run_script(content: str, interpreter: str = "auto", host: Optional[str] = None,
           args: str = "", elevated: bool = False, as_user: bool = False, timeout: int = 600)
```

#### `run_node` — mutating
Run inline Node.js code on a box (Node must be installed).
```python
run_node(code: str, host: Optional[str] = None, args: str = "", timeout: int = 300)
```

#### `ensure_runtime` — mutating
Ensure a language runtime is installed on a box. `runtime`: python | node. Installs via winget (falls back to choco); runs elevated.
```python
ensure_runtime(runtime: str = "python", host: Optional[str] = None, timeout: int = 900)
```

---

## Files

File-system operations on a box, plus ACL/ownership. Uploads/downloads move bytes between the operator machine and the box; copy/move/zip run server-side.

#### `file_list` — read-only
List a directory on a box (files and folders with size and mtime).
```python
file_list(path: str, host: Optional[str] = None)
```

#### `file_read` — read-only
Read a text file from a box (UTF-8). Truncates at `max_bytes`.
```python
file_read(path: str, host: Optional[str] = None, max_bytes: int = 1_000_000)
```

#### `file_write` — mutating
Write (or append) UTF-8 text to a file on a box.
```python
file_write(path: str, content: str, host: Optional[str] = None, append: bool = False)
```

#### `file_search` — read-only
Find files by name pattern under a directory.
```python
file_search(path: str, pattern: str = "*", host: Optional[str] = None,
            recurse: bool = True, max_results: int = 200)
```

#### `file_upload` — mutating
Upload a file from the operator machine to a box.
```python
file_upload(local_path: str, remote_path: str, host: Optional[str] = None)
```

#### `file_download` — read-only
Download a file from a box to the operator machine.
```python
file_download(remote_path: str, local_path: str, host: Optional[str] = None)
```

#### `file_delete` — destructive
Delete a file or directory on a box.
```python
file_delete(path: str, host: Optional[str] = None, recurse: bool = False)
```

#### `make_dir` — mutating
Create a directory (and parents) on a box.
```python
make_dir(path: str, host: Optional[str] = None)
```

#### `file_copy` — mutating
Copy a file or directory on a box (server-side, no round-trip through operator).
```python
file_copy(source: str, dest: str, host: Optional[str] = None, recurse: bool = True)
```

#### `file_move` — mutating
Move or rename a file/directory on a box.
```python
file_move(source: str, dest: str, host: Optional[str] = None)
```

#### `file_hash` — read-only
Compute a file hash on a box. `algorithm`: SHA256|SHA1|MD5|SHA384|SHA512.
```python
file_hash(path: str, host: Optional[str] = None, algorithm: str = "SHA256")
```

#### `zip_path` — mutating
Compress a file/folder into a `.zip` on a box.
```python
zip_path(source: str, dest_zip: str, host: Optional[str] = None)
```

#### `unzip_path` — mutating
Extract a `.zip` archive on a box.
```python
unzip_path(source_zip: str, dest_dir: str, host: Optional[str] = None)
```

#### `get_acl` — read-only
Read the owner and access rules (ACL) of a file/folder/registry path.
```python
get_acl(path: str, host: Optional[str] = None)
```

#### `grant_acl` — mutating
Grant a principal rights on a path via `icacls`. `rights`: F|M|RX|R|W or a word like FullControl. Runs elevated.
```python
grant_acl(path: str, principal: str, rights: str = "FullControl",
          host: Optional[str] = None, recurse: bool = False)
```

#### `take_own` — mutating
Take ownership of a file/folder via `takeown` (needed before changing a locked ACL). Runs elevated.
```python
take_own(path: str, host: Optional[str] = None, recurse: bool = False)
```

---

## Admin

Registry, services, processes, scheduled tasks, users/groups, firewall, clipboard, and event-log write/clear.

#### `reg_read` — read-only
Read a registry key (all values) or a single value. `path` like `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion`.
```python
reg_read(path: str, name: Optional[str] = None, host: Optional[str] = None)
```

#### `reg_write` — mutating
Write a registry value. `type`: String|DWord|QWord|Binary|ExpandString|MultiString. Creates the key if missing.
```python
reg_write(path: str, name: str, value: str, host: Optional[str] = None, type: str = "String")
```

#### `reg_delete` — destructive
Delete a registry value (if `name` given) or an entire key.
```python
reg_delete(path: str, name: Optional[str] = None, host: Optional[str] = None)
```

#### `list_services` — read-only
List Windows services (name, display name, status, start mode).
```python
list_services(host: Optional[str] = None, filter: Optional[str] = None)
```

#### `service_control` — mutating
Control a service. `action`: start | stop | restart | enable | disable | manual | auto.
```python
service_control(name: str, action: str, host: Optional[str] = None)
```

#### `list_processes` — read-only
List running processes with pid, memory, and CPU seconds.
```python
list_processes(host: Optional[str] = None, name: Optional[str] = None, top: int = 100)
```

#### `kill_process` — destructive
Kill a process by pid or by name (all matching).
```python
kill_process(pid: Optional[int] = None, name: Optional[str] = None, host: Optional[str] = None)
```

#### `start_process` — mutating
Launch a program/command on a box. `elevated=True` for full token, `as_user=True` to launch in the interactive RDP desktop session.
```python
start_process(command: str, host: Optional[str] = None, elevated: bool = False,
              as_user: bool = False, wait: bool = False)
```

#### `list_tasks` — read-only
List scheduled tasks (name, path, state). `detailed=True` also fetches last-run time/result (slower, N+1 CIM calls).
```python
list_tasks(host: Optional[str] = None, folder: str = "\\", detailed: bool = False,
           top: int = 500)
```

#### `task_create` — mutating
Create a scheduled task. `schedule`: ONCE|DAILY|HOURLY|ONLOGON|ONSTART. `run_as` SYSTEM needs no password; `highest=True` runs with a full token.
```python
task_create(name: str, command: str, host: Optional[str] = None, schedule: str = "ONCE",
            start_time: str = "23:59", run_as: str = "SYSTEM", highest: bool = True)
```

#### `task_delete` — destructive
Delete a scheduled task by name.
```python
task_delete(name: str, host: Optional[str] = None)
```

#### `task_run` — mutating
Run a scheduled task now.
```python
task_run(name: str, host: Optional[str] = None)
```

#### `list_users` — read-only
List local user accounts (enabled state, last logon, admin membership).
```python
list_users(host: Optional[str] = None)
```

#### `user_create` — mutating
Create a local user; optionally add to Administrators and Remote Desktop Users. (The password is briefly placed on the target command line and scrubbed from debug logs.)
```python
user_create(username: str, password: str, host: Optional[str] = None,
            administrator: bool = False, rdp: bool = True, never_expires: bool = True)
```

#### `user_delete` — destructive
Delete a local user account.
```python
user_delete(username: str, host: Optional[str] = None)
```

#### `group_add_member` — mutating
Add a user to a local group (e.g. 'Administrators', 'Remote Desktop Users').
```python
group_add_member(group: str, member: str, host: Optional[str] = None)
```

#### `firewall_rules` — read-only
List firewall rules for a direction.
```python
firewall_rules(host: Optional[str] = None, direction: str = "Inbound",
               enabled_only: bool = True, top: int = 200)
```

#### `firewall_add` — mutating
Open (or block) a port in Windows Firewall.
```python
firewall_add(name: str, port: int, host: Optional[str] = None, protocol: str = "TCP",
             direction: str = "Inbound", action: str = "Allow")
```

#### `firewall_delete` — destructive
Delete firewall rule(s) by display name.
```python
firewall_delete(name: str, host: Optional[str] = None)
```

#### `service_create` — mutating
Create a Windows service. `start`: auto|demand|disabled. Optionally run under a specific account. Runs elevated.
```python
service_create(name: str, bin_path: str, host: Optional[str] = None,
               display_name: Optional[str] = None, start: str = "auto",
               run_as: Optional[str] = None, password: Optional[str] = None)
```

#### `service_delete` — destructive
Delete a Windows service (via `sc.exe delete`). Runs elevated.
```python
service_delete(name: str, host: Optional[str] = None)
```

#### `clipboard_get` — mutating
Read the clipboard text of the interactive session on a box.
```python
clipboard_get(host: Optional[str] = None)
```

#### `clipboard_set` — mutating
Set the clipboard text of the interactive session on a box.
```python
clipboard_set(text: str, host: Optional[str] = None)
```

#### `write_event` — mutating
Write an entry to a Windows event log (creates the source if needed). Runs elevated.
```python
write_event(message: str, host: Optional[str] = None, log: str = "Application",
            source: str = "winrdp-mcp", event_id: int = 1000, level: str = "Information")
```

#### `clear_event_log` — destructive
Clear a Windows event log. Runs elevated.
```python
clear_event_log(log: str, host: Optional[str] = None)
```

---

## RDP

First-class Remote Desktop control: enable/disable, NLA, port, sessions, `tscon` handoff, `.rdp` generation, `mstsc` launch, and concurrent-session enablement.

#### `rdp_status` — read-only
Report RDP configuration: enabled, NLA, port, max connections, firewall.
```python
rdp_status(host: Optional[str] = None)
```

#### `rdp_enable` — mutating
Enable RDP: allow connections, open the firewall group, set NLA. Idempotent.
```python
rdp_enable(host: Optional[str] = None, nla: bool = True)
```

#### `rdp_disable` — destructive
Disable incoming RDP connections.
```python
rdp_disable(host: Optional[str] = None)
```

#### `rdp_set_port` — destructive
Change the RDP listening port and (optionally) open it in the firewall. Takes effect after a reboot or TermService restart.
```python
rdp_set_port(port: int, host: Optional[str] = None, open_firewall: bool = True)
```

#### `rdp_sessions` — read-only
List RDP/console sessions (id, user, state) via `qwinsta`.
```python
rdp_sessions(host: Optional[str] = None)
```

#### `rdp_disconnect_session` — mutating
Disconnect an RDP session by id (keeps it running).
```python
rdp_disconnect_session(session_id: int, host: Optional[str] = None)
```

#### `rdp_logoff_session` — destructive
Log off an RDP session by id (ends it).
```python
rdp_logoff_session(session_id: int, host: Optional[str] = None)
```

#### `rdp_connect_to_console` — destructive
Redirect a session to the console with `tscon` (session steal/handoff). Requires SYSTEM; runs elevated.
```python
rdp_connect_to_console(session_id: int, host: Optional[str] = None, target: str = "console")
```

#### `rdp_connection_file` — read-only
Generate a ready-to-use `.rdp` file on the operator machine for a registered box. If `store_credentials` and the operator is Windows, the password is stored via `cmdkey` (never returned to the model).
```python
rdp_connection_file(host: Optional[str] = None, save_path: Optional[str] = None,
                    fullscreen: bool = True, multimon: bool = False,
                    store_credentials: bool = True)
```

#### `rdp_open` — mutating
Launch `mstsc` from the operator machine to a registered box, pre-storing the password with `cmdkey` so it connects without prompting. Operator must be Windows.
```python
rdp_open(host: Optional[str] = None)
```

#### `install_rdp_wrapper` — destructive
Install RDP Wrapper to allow concurrent RDP sessions on client SKUs (Win10/11). Provide `url` to a RDPWrap installer zip; without a url, flips the supported policy and returns guidance.
```python
install_rdp_wrapper(host: Optional[str] = None, url: Optional[str] = None, timeout: int = 600)
```

#### `rdp_allow_multiple_sessions` — mutating
Toggle the per-user single-session restriction (`fSingleSessionPerUser`) so several users can RDP at once.
```python
rdp_allow_multiple_sessions(host: Optional[str] = None, enable: bool = True)
```

---

## Software

Package management via winget / Chocolatey / direct MSI-EXE installers, plus uninstall and package-manager bootstrap. Installs run detached (scheduled task + poll) so a mid-install disconnect doesn't fail them.

#### `install_software` — mutating
Install a program on a box. Provide `name` (a winget id or choco package) or `url` to a `.msi`/`.exe` installer. `manager='auto'` prefers winget then choco.
```python
install_software(name: Optional[str] = None, url: Optional[str] = None,
                 host: Optional[str] = None, manager: str = "auto",
                 silent_args: str = "/S", timeout: int = 1200)
```

#### `uninstall_software` — destructive
Uninstall a program by display name or package id. Tries winget, then choco, then the registry uninstall string (silent). Runs elevated.
```python
uninstall_software(name: str, host: Optional[str] = None, timeout: int = 600)
```

#### `list_installed_software` — read-only
List installed programs (from the uninstall registry, 32/64-bit + per-user).
```python
list_installed_software(host: Optional[str] = None, filter: Optional[str] = None)
```

#### `ensure_package_manager` — mutating
Install a package manager on a box. `manager`: 'choco' (Chocolatey) or 'winget' (App Installer). Runs elevated.
```python
ensure_package_manager(host: Optional[str] = None, manager: str = "choco", timeout: int = 600)
```

---

## Network

Adapter/DNS info, ping, port checks, connection listing, `netsh` port-proxy (tunneling), and DNS configuration. Ping/port-check run *from the box*.

#### `net_info` — read-only
Network configuration of a box: adapters, IPv4, gateway, DNS servers, MAC.
```python
net_info(host: Optional[str] = None)
```

#### `ping` — read-only
Ping a target from the box.
```python
ping(target: str, host: Optional[str] = None, count: int = 4)
```

#### `port_check` — read-only
Test whether a TCP port on `target` is reachable *from the box*.
```python
port_check(target: str, port: int, host: Optional[str] = None)
```

#### `net_connections` — read-only
List TCP connections on a box. `state`: Listen|Established|* (all).
```python
net_connections(host: Optional[str] = None, state: str = "Listen", top: int = 200)
```

#### `port_proxy_add` — mutating
Add a `netsh` portproxy on a box (v4→v4) — tunnel RDP/other services through a reachable box to one behind NAT. Runs elevated.
```python
port_proxy_add(listen_port: int, connect_host: str, connect_port: int,
               host: Optional[str] = None, listen_address: str = "0.0.0.0",
               open_firewall: bool = True)
```

#### `port_proxy_list` — read-only
List active `netsh` portproxy rules on a box.
```python
port_proxy_list(host: Optional[str] = None)
```

#### `port_proxy_delete` — destructive
Delete a `netsh` portproxy rule by listen port. Runs elevated.
```python
port_proxy_delete(listen_port: int, host: Optional[str] = None,
                  listen_address: str = "0.0.0.0")
```

#### `set_dns` — mutating
Set DNS servers on an interface (`servers` comma-separated, e.g. `1.1.1.1,8.8.8.8`). Runs elevated.
```python
set_dns(interface: str, servers: str, host: Optional[str] = None)
```

---

## Windows

Deeper management: arbitrary CIM/WMI queries, features/roles, Windows Update (COM API), Defender, environment variables, and startup entries.

#### `cim_query` — read-only
Run an arbitrary CIM/WMI query — the escape hatch for any WMI data. Provide a WQL `query` or a `class_name`. (Values are flattened to strings.)
```python
cim_query(query: Optional[str] = None, class_name: Optional[str] = None,
          namespace: str = "root/cimv2", host: Optional[str] = None, top: int = 100)
```

#### `windows_features` — mutating
Manage Windows features/roles. `action`: list | install | remove. Auto-detects Server (`Install-WindowsFeature`) vs client (`Enable/Disable` optional feature). Install/remove run elevated.
```python
windows_features(action: str = "list", name: Optional[str] = None,
                 host: Optional[str] = None, timeout: int = 900)
```

#### `windows_update` — mutating
Windows Update via the built-in COM API. `action`: check (list pending) | install (download+install all). Runs elevated.
```python
windows_update(action: str = "check", host: Optional[str] = None, timeout: int = 1800)
```

#### `hotfixes` — read-only
List installed hotfixes/updates (KB, description, install date).
```python
hotfixes(host: Optional[str] = None, top: int = 40)
```

#### `defender_status` — read-only
Microsoft Defender status: realtime protection, signature age, last scan.
```python
defender_status(host: Optional[str] = None)
```

#### `defender_realtime` — destructive
Enable or disable Defender realtime protection. Runs elevated. (Tamper Protection may block disabling.)
```python
defender_realtime(enable: bool, host: Optional[str] = None)
```

#### `defender_exclusion_add` — mutating
Add a path to Defender's exclusion list. Runs elevated.
```python
defender_exclusion_add(path: str, host: Optional[str] = None)
```

#### `defender_scan` — mutating
Run a Defender scan. `scan_type`: QuickScan | FullScan. Runs elevated.
```python
defender_scan(host: Optional[str] = None, scan_type: str = "QuickScan", timeout: int = 1800)
```

#### `env_get` — read-only
Read environment variables. `scope`: Machine | User | Process.
```python
env_get(host: Optional[str] = None, scope: str = "Machine")
```

#### `env_set` — mutating
Set an environment variable. `scope`: Machine | User. Machine scope runs elevated.
```python
env_set(name: str, value: str, host: Optional[str] = None, scope: str = "Machine")
```

#### `list_startup` — read-only
List autostart entries (Run keys, Startup folders, WMI startup commands).
```python
list_startup(host: Optional[str] = None)
```
