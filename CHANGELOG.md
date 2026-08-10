# Changelog

All notable changes to winrdp-mcp are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.0] — 2026-08-10

Initial release. A zero-config MCP server that provisions and fully administers Windows
RDP boxes (Win10/11, Server 2016–2025) for Claude & Claude Code.

### Added
- **108 tools** across 10 groups: hosts/fleet, provisioning & UAC, system, scripting,
  files, admin, RDP, software, network, and deeper Windows management.
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

### Attribution
Builds on the MIT-licensed [winremote-mcp](https://github.com/dddabtc/winremote-mcp) and
[windows-admin-mcp](https://github.com/Cosmicjedi/windows-admin-mcp) — see `NOTICE`.
