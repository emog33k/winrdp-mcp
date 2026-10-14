# Changelog

All notable changes to winrdp-mcp are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic versioning.

## [0.1.7] — 2026-10-14

### Fixed
- **Server failed to start on Python 3.10 / 3.11** (regression in 0.1.6). The `ExecResult`
  typed return used `typing.TypedDict`, which pydantic (via FastMCP's output-schema build)
  rejects on Python < 3.12 (`PydanticUserError: use typing_extensions.TypedDict`). Switched
  to `typing_extensions.TypedDict` and added `typing_extensions` as an explicit dependency.
  0.1.6 is affected on 3.10/3.11; upgrade to 0.1.7 (or run on 3.12+).
- **Pinned `fastmcp<4`.** FastMCP 4.0 is a new major with breaking changes to the
  elicitation middleware and annotation fields; the server is tested against the 3.x line.

## [0.1.6] — 2026-10-14

MCP 2026-spec alignment (dynamic discovery, structured output, progress, elicitation),
provisioning reliability fixes, and file-transfer/ops improvements. **147 tools, 6 prompts,
3 resources.** No tool signatures removed.

### Fixed
- **`download_file` now survives slow/large downloads.** It ran the fetch in a single WinRM
  request, so a transfer taking longer than the operation timeout dropped the session
  mid-download (Connection closed). It now runs in a detached SYSTEM task polled with short
  calls (like installs), tries WebClient then falls back to Invoke-WebRequest, negotiates
  TLS 1.2/1.3, and returns `{path, bytes, sha256}` only once the file is complete.
- **`file_upload` reports progress and prefers the fast channel.** The slow chunked-over-WinRM
  path now emits byte-level progress notifications (a 7–8 MB upload no longer looks hung), and
  SMB/SFTP is used in one shot whenever 445/22 is reachable (set up by `provision_host`).
- **`tail_file` gained `max_bytes` (default 20000).** Caps the returned text to the most
  recent bytes on the box, so a detached job's log full of a progress bar or stray binary
  output can't blow up the token budget; returns a `truncated` flag.
- **Provisioning no longer drops the RDP session.** The enable-WinRM script switched the
  network profile Public→Private (`Set-NetConnectionProfile`), which forces Windows to
  re-apply its per-profile firewall rules — on a fresh cloud box that dropped the live RDP
  connection and blocked reconnects for a while. The switch was also redundant
  (`Enable-PSRemoting -Force -SkipNetworkProfileCheck` already handles a Public network). It
  is removed, and the script now keeps RDP explicitly enabled (`fDenyTSConnections=0` + the
  Remote Desktop firewall group) before and after touching the firewall. This fixes both
  `provision_host` over an existing channel and the paste-once `bootstrap_oneliner`.
- **`provision_host`'s failure guidance no longer trusts the raw port scan.** A transparent
  proxy / VPN on the operator side can make a TCP connect succeed for every port, so the
  port scan can read all-open even when nothing is actually reachable. The Rung-4 message now
  derives from what actually happened (real transport probe / cold-start outcomes) instead of
  `reachable_ports`, so it recommends the right next step regardless of a misleading scan.

### Added
- **`WINRDP_DEFAULT_WINRM_AUTH` + `provision_host(enable_basic=…)` + `fix_connection` prompt.**
  Three conveniences for operators behind an NTLM-breaking proxy: set
  `WINRDP_DEFAULT_WINRM_AUTH=basic` so every `add_host` defaults to Basic without passing it
  each time; `provision_host(enable_basic=True)` turns Basic on across the whole ladder (SSH
  rung, WMI cold-start, harden, and the bootstrap one-liner) and connects via Basic; and the
  new `fix_connection` prompt walks `diagnose_host` → the right fix.
- **`diagnose_host` — a connectivity doctor.** Where `test_host` only says "No working
  transport", `diagnose_host` explains *why* and *what to do*: it detects an intercepting
  proxy/VPN on the operator side (a TCP connect to a guaranteed-dead IP that "succeeds"),
  reports which auth schemes the WinRM listener advertises, actually tries each transport and
  classifies the real error (credentials-rejected vs timeout vs proxy-mangled NTLM), probes
  SSH, and returns a verdict plus concrete fixes (e.g. "re-register with winrm_auth='basic'",
  "enable Basic", "open 5985 at the provider firewall"). Encodes the whole manual debugging
  session this came out of into one call.
- **Quieter SSH probing.** paramiko logged a full stack trace to stderr on every failed SSH
  probe (a closed port, a proxy mangling the banner — normal during transport selection); it's
  now silenced to CRITICAL unless `WINRDP_DEBUG` is set.
- **Opt-in Basic auth for proxy/VPN operators (`enable_basic`).** When the operator machine
  is behind a proxy/VPN that breaks connection-oriented NTLM (a rotating proxy: the multi-leg
  NTLM/Negotiate handshake needs one persistent TCP connection it doesn't preserve, so WinRM
  auth fails even with a correct password), `enable_winrm(basic=True)` and
  `rdp_bootstrap(enable_basic=True)` turn on WinRM Basic auth + AllowUnencrypted on the box
  (AllowUnencrypted via the policy registry key, so it works on a Public network profile
  without the `winrm set` firewall error). Register the host with `winrm_auth="basic"` and the
  stateless Basic scheme survives the proxy. Off by default (security tradeoff: over HTTP the
  password travels near-cleartext — scope WinRM to your operator IP at the provider firewall).
- **`winrdp://toolsets` resource** exposing the tool profiles and the on-demand toolset
  catalog, so a client can see the structure (and how `enable_toolset` works) without a call.
- **Bounded list tools.** `file_list` (`limit`, default 1000), `list_services` and
  `list_installed_software` (`limit`, default 500) now cap their output on the box so a huge
  directory / service list / software inventory can't flood the context. (`firewall_rules`,
  `list_tasks`, `list_processes`, `net_connections`, `event_log`, etc. already had caps.)
- **`task_stop(name)` — cancel a detached job cleanly.** Stops a scheduled task and kills
  its process tree (the `task` returned by `run_powershell(detach=True)` / `start_process`),
  then unregisters it. Previously the only option was `kill_process` by name, which is
  imprecise. `run_powershell(detach=True)`'s note now points at `task_stop`.
- **Progress notifications (MCP 2026 progress) across every long-running tool** — so nothing
  ever looks hung. `provision_host` reports each ladder rung; the `wait_for_*` family and
  `reboot_and_wait` report each poll with elapsed/timeout; and start/finish (or per-phase)
  progress on `install_software`, `windows_update`, `download_file`, `file_upload`,
  `sync_folder` + `transfer_between_hosts` (byte-level during the transfer), `pip_install`,
  `ensure_runtime`, `ensure_package_manager`, `enable_ssh`, `deploy_ui_agent`,
  `persist_as_service`, `install_rdp_wrapper`, `windows_features`, and `defender_scan` (21
  tools total). Implemented via an injected FastMCP `Context` (`winrdp_mcp/progress.py`) that
  is hidden from the tool input schema and bridges the async `report_progress` call from the
  sync tool's worker thread; it is a no-op when the client sends no progress token, so nothing
  breaks.
- **Heartbeat during long single-operations** — so even a multi-minute install/scan never
  looks idle between "start" and "finish". The detached scheduled-task poll loop
  (`run_elevated`/`run_long`) now invokes a progress callback on every poll (~1.5s), emitting
  "…— Ns elapsed". Wired into `install_software`, `download_file`, `ensure_runtime` (Python),
  `windows_update`, `defender_scan`, `windows_features`, `ensure_package_manager`,
  `persist_as_service`, and `install_rdp_wrapper` (the last four now take the detached poll
  path via the new `run_long_json`, so the heartbeat works even on an already-elevated
  session).
- **`rdp_bootstrap` tool for RDP-only boxes.** When a fresh box exposes only RDP (3389) and
  no WinRM/SSH/SMB, there is no remote-exec channel, so one manual paste is unavoidable.
  `rdp_bootstrap` makes it near one-click: it copies the enable command to the operator's
  local clipboard and launches mstsc with the saved credentials (clipboard redirection on),
  so you paste once in an elevated PowerShell on the box, then re-run `provision_host`.
  `provision_host`'s failure message now distinguishes "only RDP reachable" (→ `rdp_bootstrap`)
  from "SMB+DCOM open but impacket not installed" (→ `pip install 'winrdp-mcp[bootstrap]'`).
- **`dynamic` tool profile — load-on-demand tool discovery.** Set `WINRDP_PROFILE=dynamic`
  to start with a lean ~41-tool core plus three meta-tools (`list_toolsets`,
  `enable_toolset`, `disable_toolset`). The model pulls in a group (`rdp`, `gui`, `network`,
  `files`, `admin`, …) only when a task needs it, keeping the up-front tool list small per
  the 2026 MCP guidance on context bloat. FastMCP emits `tools/list_changed` so clients
  re-fetch automatically. Nothing is removed from the product — every tool is still
  reachable.
- **Structured output (`outputSchema`) for command results.** The seven tools that return a
  server-constructed `{stdout, stderr, rc}` now advertise a precise JSON Schema via the
  `ExecResult` type (`run_cmd`, `run_elevated`, `service_delete`, `grant_acl`, `take_own`,
  `gui_script`, `rdp_connect_to_console`). Live-data tools (`system_info`, `performance`,
  `rdp_status`, `ping`, `port_check`, `event_log`, `cim_query`, …) intentionally keep a loose
  `dict`/`list` return: FastMCP validates every response against the schema, and CIM/registry
  values vary per box and can be null, so a fixed per-field schema would reject otherwise-valid
  responses.
- **Optional server-side confirmation for destructive tools (`WINRDP_CONFIRM_DESTRUCTIVE=1`).**
  Uses MCP elicitation to ask the operator before a destructive tool runs — a safety net on
  top of client-side `destructiveHint` gating. Clients without elicitation support fall back
  to that gating (the call proceeds), so it never breaks automation.

### Changed
- **Richer tool annotations.** `idempotentHint` is now set on idempotent writers (so a
  client can safely retry after a dropped connection), the `title` is a human-readable
  display name (e.g. "Run PowerShell" instead of `run_powershell`), and `openWorldHint` is
  `false` for tools that stay inside the target box. Completes the four-hint set the 2026
  spec recommends.
- **Bumped minimum FastMCP to `>=2.11.0`** to guarantee structured-output support.
- Synced the version across `pyproject.toml`, `server.json`, and `dxt/manifest.json`.

## [0.1.5] — 2026-09-23

### Fixed
- **`screenshot` / `as_user` / every interactive GUI op works again (`qwinsta` parse).**
  Session discovery sliced the `USERNAME` column up to `STATE`, but the `ID` column sits
  between them, so a short username kept the trailing session-id digits (e.g. `admin` became
  `"admin                     1"`). That value failed the name-to-SID lookup in
  `Register-ScheduledTask` (`ERROR_NONE_MAPPED`), breaking `screenshot`,
  `start_process(as_user=True)`, `run_powershell(as_user=True)`, mouse/keys, UI Automation
  and OCR. The slice now stops at the `ID` column, which also skips blank-username rows
  (services/listeners) and preserves local usernames that contain spaces. (#1)
- **Interactive-session task no longer flashes a console window.** The scheduled task that
  runs GUI / `as_user` work in the desktop session launched `powershell.exe` without
  `-WindowStyle Hidden`, briefly showing a console on the target desktop. Added it. The two
  `SYSTEM` tasks run in session 0 and never render a window, so they are unchanged. (#2)

### Added
- **Windows Home interactive sessions.** When `qwinsta.exe` is missing (as on some Windows 11
  Home boxes), session discovery and `rdp_sessions()` fall back to the console user reported
  by `Win32_ComputerSystem`, confirmed live by that user's `explorer.exe`, instead of
  reporting no interactive session. Full editions keep the `qwinsta` path. (#3, thanks
  @karas1999)

## [0.1.4] — 2026-08-10

### Fixed
- **Base64 WinRM upload fallback is robust and faster.** This path only runs when a box
  exposes *only* WinRM (both SMB/445 and SSH/22 unreachable, so the fast SMB/SFTP channels
  can't be used). Two fixes:
  - Chunks are written with `[IO.File]::AppendAllText` (opens→appends→closes atomically per
    call) instead of `Add-Content`, which could fail *"Stream was not readable"* when a prior
    handle wasn't released.
  - The payload is **gzip-compressed** before chunking — a staged `.ps1` (the usual payload)
    compresses several-fold, cutting the number of round-trips proportionally. The box
    decompresses on finalize.
  - Chunk size stays well under the real limit: pywinrm ships each write as `powershell
    -EncodedCommand <utf16le-base64>` (~2.67× expansion), so the command line for a full
    chunk is ~4.9k — comfortably under ~8192. Locked in by a regression test.

  (Reachable SSH still uses SFTP directly, and reachable SMB uses the admin share — both
  single-shot; the chunked path is only the last resort for WinRM-only boxes.)

## [0.1.3] — 2026-08-10

### Added
- Listed on the official **MCP Registry** (`io.github.emog33k/winrdp-mcp`). Requires an
  `mcp-name:` ownership marker in the package README, added here; `server.json` moved to the
  current 2025-12-11 schema.

## [0.1.2] — 2026-08-10

### Fixed
- **SMB fast-channel probe now fails fast.** `smbprotocol` defaults to a 60s connection
  timeout, so a box with 445 open at the TCP layer but SMB filtered (common on cloud VDS)
  would hang the first upload for a minute before falling back to SFTP/base64. Bounded to 8s.
- **DXT build (`dxt/build.ps1`) now produces a valid bundle.** Two Windows PowerShell 5.1
  bugs fixed: pip's stderr advisory no longer aborts the vendoring step, and the archive is
  written with forward-slash entry paths (Compress-Archive emitted backslashes, violating the
  ZIP spec and breaking the extension loader). Verified: 23 MB self-contained `.dxt`.

### Added
- Regression test suite (`tests/test_security_fixes.py`) locking in every 0.1.1 fix: the
  chunk-write inline invariant + non-staging routing (recursion guard), injection validation,
  provisioning omits the weakening settings, secrets stay out of the script body, log
  redaction of `%s` args, and scrypt KDF + legacy-ciphertext migration.

### Changed
- Graceful shutdown: `serve` / `agent` close cached transports on exit, and open SSH tunnels
  are torn down via an `atexit` hook.

## [0.1.1] — 2026-08-10

Security & reliability hardening from a full-code audit. No tool signatures removed;
one behavioral change (provisioning no longer enables Basic/AllowUnencrypted — see below).

### Fixed — reliability
- **Chunked upload could recurse forever** over a WinRM-only box with no SMB/SSH fast channel
  (`_CHUNK` == `_MAX_INLINE_PS`): a chunk write crossed the staging threshold and re-entered
  the uploader. Chunk writes now use a non-staging inline path with headroom (`_CHUNK=1600`).
- **pywinrm session was shared across threads** (fan-out to a duplicate alias, or a waiter
  polling while another call runs) → HTTP 400 cascade. Each transport now serializes its own
  use with a reentrant lock; cross-host parallelism is unaffected. `run_on_hosts` dedups aliases.
- **Reconnect-retry no longer double-executes non-idempotent calls** (a lost response after a
  command ran would re-run it). Retry is limited to idempotent reads.
- **Per-call `timeout` is now honored on WinRM** (a wedged connection no longer blocks for the
  full read-timeout regardless of the caller's budget).
- Session/leak fixes: `close()` now tears down the SMB fast channel (not just SFTP); a failed
  fast channel is closed before falling back; `port_forward` no longer leaks the SSH client on
  a bind failure and honors the host's `ssh_port`; elevated-task cleanup can't mask the result.
- `Vault` mutations/saves are now locked and use a unique temp file (no torn inventory under
  fan-out).

### Fixed — security
- **PowerShell injection in `write_event` (`level`) that ran as SYSTEM** — now validated.
  Also validated `file_hash` (`algorithm`) and `ui_find` (`control_type`).
- **`deploy_ui_agent` bound the desktop-control agent to `0.0.0.0` with an optional auth key.**
  Now binds `127.0.0.1` by default (reach it via `port_forward`); a non-loopback bind requires
  a validated `auth_key`.
- **New-user / service-account passwords no longer land on the target's process command line**
  (Event 4688). They are staged to an admin-only file and read on the box (`exec_json(secrets=)`).
- **Provisioning no longer enables Basic auth, `AllowUnencrypted`, or `TrustedHosts=*`** — the
  default NTLM transport encrypts the payload without them, so they only weakened the box.
- **Vault passphrase KDF is now scrypt + a persisted per-install salt** (was unsalted single-pass
  SHA-256). Old inventories still decrypt and re-encrypt to the strong key on next save.
- Owner-only ACLs: the vault key is hardened before its bytes are written (no open window);
  the encrypted inventory gets an owner-only DACL; on-box secret files live in a SYSTEM+Admins
  locked directory.
- Log redaction now scrubs the **rendered** message (secrets passed as `%s` args were leaking);
  `TransportError` text handed to the model is redacted.

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
