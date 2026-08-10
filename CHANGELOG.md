# Changelog

All notable changes to winrdp-mcp are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.0] — 2026-08-10

Initial release. A zero-config MCP server that provisions and fully administers Windows
RDP boxes (Win10/11, Server 2016–2025) for Claude & Claude Code.

### Added
- **144 tools** across 15 groups: hosts/fleet, provisioning & UAC, system, scripting,
  files, admin, RDP, software, network, deeper Windows management, native GUI automation,
  wait-for-condition helpers, scheduling/persistence, SSH tunneling, and high-level ops.
- **MCP prompts** (`prompts.py`) — 5 user-invoked workflows: `provision_and_harden`,
  `diagnose_box`, `security_audit`, `setup_dev_box`, `open_service_locally`.
- **MCP resources** (`resources.py`) — `winrdp://hosts` (inventory) and
  `winrdp://host/{alias}/info` (compact live box summary).
- **Tool profiles** — `WINRDP_PROFILE=full|admin|rdp|core` exposes a curated tool set.
- **Ops tools** (`ops.py`) — `health_report`, `apply_baseline`, `whoami_priv`,
  `failed_logons`, `list_open_ports`.
- **SSH tunneling** (`tunnel.py`) — `port_forward`/`port_forward_list`/`port_forward_stop`
  to reach a box's loopback service from the operator machine.
- **Packaging** — installable from PyPI (`pipx install winrdp-mcp`), with manifests for
  Smithery (`smithery.yaml`), the MCP registry (`server.json`), and a Claude Desktop
  extension (`dxt/`), plus CI + trusted-publishing GitHub Actions.
- **Native GUI automation** (`gui.py`) — drive the interactive RDP desktop with no on-box
  agent: `send_keys`, `type_text`, `mouse_move`/`mouse_click`/`mouse_drag`, `list_windows`,
  `focus_window`, UI Automation (`ui_find`/`ui_invoke`/`ui_set_text`), built-in OCR
  (`ocr_screen`/`find_and_click`), `wait_for_window`, `record_screen` (animated GIF), and
  `gui_script` (a single-call multi-step sequence with mouse/UIA helpers pre-loaded).
- **Wait-for-condition helpers** (`waiters.py`) — `wait_for_port`, `wait_for_service`,
  `wait_for_process`, `wait_for_file`; polled controller-side so any timeout is safe.
- **File convenience** — `download_file`, `tail_file`, `edit_file` (find/replace),
  `sync_folder` (zip→upload→expand a local folder), `transfer_between_hosts`.
- **Scheduling & persistence** (`scheduling.py`) — `schedule_command`, `run_at_startup`,
  and `persist_as_service` (a resilient auto-restarting service via NSSM) / `unpersist_service`.
- **Zero-config provisioning ladder** — `provision_host` climbs WinRM → SSH → SMB/WMI
  cold-start → paste-once bootstrap, enabling WinRM, opening the firewall, and fixing
  local-admin token filtering on any Windows version.
- **Real elevation** — full-token execution over WinRM detected via `is_elevated` (direct,
  fast) with a one-shot SYSTEM Scheduled Task fallback for filtered tokens; `as_user` runs
  inside the interactive RDP desktop.
- **On-demand scripting** — `run_python` (auto-installs Python + pip deps), `run_script`
  (auto interpreter), `run_node`, `pip_install`, `ensure_runtime`.
- **On-demand tooling** — `stage_tool` (Sysinternals presets / URL / local file),
  `stage_script`, staged-tool cache management.
- **First-class RDP** — enable/disable, NLA, custom port, session list/disconnect/logoff,
  `tscon` handoff, multi-session, `.rdp` generation, `mstsc` launch, RDP Wrapper, live
  desktop screenshot.
- **Encrypted multi-host inventory** (Fernet) with tags, active-host targeting, parallel
  `run_on_hosts` fan-out, and `reboot_and_wait`.
- **Safety annotations** (`readOnlyHint`/`destructiveHint`) on every tool and a tool
  allowlist (`WINRDP_ENABLED_TOOLS` / `WINRDP_DISABLED_TOOLS`).
- Hybrid deployment: `serve` (controller over stdio) and `agent` (on the box); a CLI with
  `bootstrap`, `add-host`, `list-hosts`, `provision`.
- Encrypted-at-rest credentials, stderr logging with secret redaction, per-host transport
  security knobs (`winrm_cert_validation`, `ssh_host_key_policy`).

### Hardened (from an adversarial code review + a live end-to-end run against a real VDS)
- Removed a thread-based WinRM timeout watchdog that corrupted the non-thread-safe pywinrm
  session and cascaded HTTP 400s across all subsequent calls.
- Added transport **self-heal**: reconnect + retry once on a dropped/wedged WinRM
  connection (`RemoteDisconnected` / HTTP 400).
- Long installs now run **detached** (`run_long` → scheduled task + poll) so a mid-install
  disconnect on a small/busy box doesn't fail them.
- `list_tasks` made lightweight (the per-task `Get-ScheduledTaskInfo` N+1 could hang and
  drop the connection); details available via `detailed=True`.
- HTTP-first WinRM ordering (NTLM-encrypted, faster; HTTPS only when `use_ssl`).
- Argument validation/quoting across all tools; `rdp_connection_file` no longer returns the
  plaintext password; robust `qwinsta` session parsing; BOM-free file appends.
- Large-script / large-file handling: a `run_ps` script over ~6 KB is staged to a temp
  `.ps1` and run via `-File` (avoids the WSMan/cmd command-line length limit that pywinrm's
  `-EncodedCommand` packing hits, on all transports); the upload chunk was cut to a size
  that stays under that limit after re-encoding.
- Fast file channel: WinRM uploads/downloads of any size now transparently use a cached
  **SMB** (`ADMIN$`, no install) channel when 445 is reachable, else **SFTP** over SSH,
  falling back to chunked base64 only when neither is available — instant transfers instead
  of many round-trips. `provision_host(fast_transfer=True)` (default) ensures the channel:
  it uses SMB when 445 is open, otherwise installs OpenSSH so SFTP is available.

### From production-use feedback
- `run_powershell`/`run_ps` staging threshold lowered to 2.4 KB so scripts stay under the
  worst-case command-line limit some boxes enforce (staging is cheap over the fast channel).
- `file_write(binary=True)` writes decoded base64 bytes for arbitrary binary files.
- `start_process(wait=False)` redirects the background process's stdout/stderr to log files
  (returned as `stdout_log`/`stderr_log`) so silent background failures are diagnosable.
- `as_user`/GUI ops now detect a Disconnected session and fail with an actionable message
  (reconnect RDP or `rdp_connect_to_console`) instead of running on a non-composed desktop.
- New `port_forward` / `port_forward_list` / `port_forward_stop`: SSH local tunnels to reach
  a service bound to a box's 127.0.0.1 (which WinRM-launched processes cannot).
- `run_powershell(detach=True)` / `start_process(detach=True)`: launch a background process
  in a Scheduled Task so it runs OUTSIDE the WinRM Job Object and survives the session close
  (a normal launch dies with the shell — that's why a background server only lived ~1 min).
- `run_powershell(loopback=True)`: route through a Scheduled Task so the script can reach
  127.0.0.1 — the WinRM network-logon token blocks outbound loopback, the task's logon does
  not. (`file_write` already streams over SFTP/SMB, bypassing the WinRM command channel.)
- Fixed three latent PowerShell brace-balance / output bugs surfaced by a brace-balance
  audit and live runs: `ensure_remote_dirs` (broke all tool-staging), `tail_file` (returned
  megabytes of provider metadata), and `find_and_click`.

- Readable output: PowerShell serializes its progress/information streams into stderr as
  a CLIXML blob. Results are now tidied on every call — real Error/Warning text stays in
  stderr, `Write-Host`/Information output is recovered into stdout (no duplication, no
  loss), and only the progress-bar noise is dropped.

### Attribution
Builds on the MIT-licensed [winremote-mcp](https://github.com/dddabtc/winremote-mcp) and
[windows-admin-mcp](https://github.com/Cosmicjedi/windows-admin-mcp) — see `NOTICE`.
