# Security Model

winrdp-mcp is remote-administration tooling. By design it executes privileged commands on
Windows boxes over WinRM/SSH/SMB, stages tools onto them, elevates to `SYSTEM`, and holds
the credentials needed to do so. This document is the threat model and the hardening
reference: what the server protects, what it deliberately does not, where secrets live, and
how to tighten every permissive default before you point it at anything beyond a lab.

Everything here is stated against the code in `winrdp_mcp/`. Where a default is permissive
"so first contact just works," the exact setting and the exact way to close it are given.

## Table of contents

1. [Intended use and trust boundary](#1-intended-use-and-trust-boundary)
2. [Credential handling and the vault](#2-credential-handling-and-the-vault)
3. [Log redaction](#3-log-redaction)
4. [Secrets on the box and on the operator](#4-secrets-on-the-box-and-on-the-operator)
5. [Transport security and MITM](#5-transport-security-and-mitm)
6. [The enable-WinRM provisioning script](#6-the-enable-winrm-provisioning-script)
7. [Argument-injection defense](#7-argument-injection-defense)
8. [Reducing attack surface: allowlisting and safety hints](#8-reducing-attack-surface-allowlisting-and-safety-hints)
9. [Hardening checklist](#9-hardening-checklist)

---

## 1. Intended use and trust boundary

winrdp-mcp is admin tooling for Windows machines **you own or are explicitly authorized to
manage**. The whole product is a remote code-execution surface: tools such as
`run_powershell`, `run_python`, `run_script`, `install_software`, `service_create`,
`user_create`, and `reg_write` run arbitrary privileged commands on the target. It is not a
sandbox and does not attempt to constrain what an operator (or the model driving it) can do
on a box once a working transport exists.

The trust boundary is therefore:

- **Trusted:** the operator machine where `winrdp-mcp serve` runs (the controller), the
  process environment (`WINRDP_VAULT_KEY`, `WINRDP_HOME`), and the vault contents.
- **Trusted-with-verification:** the model / MCP client driving the tools. It is treated as
  capable of sending adversarial or hallucinated arguments, which is why command-line
  argument values are validated (Section 7) and destructive tools are annotated for client
  gating (Section 8) — but a client that can call `run_powershell` can run anything.
- **Managed, not trusted:** the target boxes. The server assumes you control them, but it
  does not assume they are uncompromised — secrets it must place there are minimized,
  time-bounded, and cleaned up (Section 4).

If you do not have authority over a target, none of the hardening below makes running these
tools against it acceptable.

## 2. Credential handling and the vault

### At rest: Fernet-encrypted inventory

Host passwords are the only field encrypted at rest. The inventory lives at
`WINRDP_HOME\inventory.json` (default `%APPDATA%\winrdp-mcp\inventory.json` on Windows) as
readable JSON — alias, host, port, username, transport hints, and tags are stored in the
clear so the file stays auditable. Each password is stored as `password_enc`, a Fernet token
(**AES-128-CBC + HMAC-SHA256**, authenticated encryption). See `winrdp_mcp/vault.py`.

### The key: `WINRDP_VAULT_KEY` vs. the on-disk `vault.key`

`Vault` resolves its Fernet key in this order (`_load_key`):

1. **`WINRDP_VAULT_KEY` environment variable**, if set. It accepts either a raw 32-byte
   urlsafe-base64 Fernet key, or an **arbitrary passphrase** — a passphrase is run through
   `SHA-256` and urlsafe-base64-encoded to derive the key. This is the recommended path: the
   key material never touches disk next to the ciphertext.
2. **Machine-local key file** `WINRDP_HOME\vault.key`, if it exists.
3. **First-run generation:** a new key is written to `vault.key`. It is created owner-only
   from the start with `os.open(..., O_CREAT|O_TRUNC, 0o600)` (no world-readable window on
   POSIX). On Windows, because `chmod` only toggles the read-only bit, the DACL is then
   tightened with:

   ```powershell
   icacls "%APPDATA%\winrdp-mcp\vault.key" /inheritance:r /grant:r "<you>:F"
   ```

   (run automatically), leaving the current user as the only principal with access.

### Ciphertext-only-theft caveat

When the key lives in `vault.key`, it sits in the same directory as `inventory.json`.
Encryption at rest then only protects against **ciphertext-only theft** — someone who copies
`inventory.json` alone (a backup, a synced folder, a stolen disk image that excludes the key,
a leaked snapshot) cannot read the passwords. Anyone who can read **both** files gets every
password. This is exactly why the first-run path logs:

```
using on-disk vault key <path> (encryption-at-rest only guards ciphertext-only theft);
set WINRDP_VAULT_KEY to a passphrase for stronger protection
```

For real separation of key and ciphertext, set `WINRDP_VAULT_KEY` from a secret store / CI
secret / prompt so the key is never persisted beside the inventory:

```powershell
$env:WINRDP_VAULT_KEY = (Read-Host -AsSecureString | ConvertFrom-SecureString -AsPlainText)
winrdp-mcp serve
```

Rotating the key: change `WINRDP_VAULT_KEY` (or delete `vault.key`) only after re-adding
hosts, since existing `password_enc` tokens are decryptable **only** with the key that wrote
them. A token that fails to decrypt is dropped to an empty password (`InvalidToken` is caught
in `Vault.load`), not surfaced as an error — so a mismatched key silently empties passwords.

### Passwords are never returned to the model

- In memory a `Host` holds its password in the clear (needed to authenticate), but
  `Host.redacted()` replaces it with `"***"`. Anything that serializes a host to the client —
  `list_hosts`, provisioning reports, host-detail tools — goes through `redacted()`.
- Passwords are registered for log scrubbing (Section 3) the moment they are loaded or added
  (`log.register_secret` in `Vault.load` / `Vault.add`).
- No tool returns a stored password in its result payload.

## 3. Log redaction

All logging goes to **stderr only** — stdout is the MCP protocol channel on the stdio
transport and must never be polluted (`winrdp_mcp/log.py`). `WINRDP_DEBUG=1` (or `--debug`)
raises the level to `DEBUG`; otherwise it is `INFO`.

Two layers scrub secrets from log records (`redact`):

1. **Exact-match scrubbing (strong path).** Every known plaintext secret registered via
   `register_secret` (vault passwords, and the `password` args of `user_create` and
   `service_create`) is stored in an in-process set and replaced with `***` by literal
   substring match. This is the reliable path because it catches passwords containing spaces,
   quotes, or shell metacharacters that pattern matching would miss. Values shorter than 3
   characters are not registered (too collision-prone).

2. **Structural fallback (best-effort).** For secrets never registered, regex patterns catch
   the common shapes:

   ```
   /pass:<value>
   /RP "<value>"
   password=<value> | password: <value>
   ConvertTo-SecureString '<value>'
   ```

A `_RedactFilter` runs `redact()` over each record's message before it is emitted. Note this
operates on the record message string; keep `WINRDP_DEBUG` **off** in shared or logged
environments as defense in depth — debug output includes truncated script bodies and
transport diagnostics, and redaction is a safety net, not a licence to log secrets. The
design intent is that secrets are not emitted in the first place; the two layers exist to
catch what slips through.

## 4. Secrets on the box and on the operator

Some operations must briefly place a secret somewhere outside the vault. This is a
**documented, accepted tradeoff** for boxes you control — a local administrator on the target
(or on the operator machine, respectively) could observe these during the short window they
exist. Enumerated exactly:

| Where | Which tools | What is placed, and for how long |
|---|---|---|
| **Temp `.ps1` on the target** | any elevated run via the scheduled-task path — `service_create`, `service_delete`, `install_software`/`ensure_runtime`/`run_python` (detached long runs), and any `elevated=True` op when the session token is *filtered* (SSH / non-elevated local) | `run_elevated` uploads a wrapper script to `C:\ProgramData\winrdp-mcp\tmp\winrdp_elev_<rand>.ps1`. If the script embeds a secret (e.g. `service_create` with `-Password`), that secret is on disk inside the wrapper while the one-shot Scheduled Task runs. The task, script, and its `.out`/`.err`/`.done` marker files are removed in a `finally` block. `run_in_user_session` (interactive-desktop ops via `as_user=True`) uses the same staged-`.ps1` pattern. |
| **Command line / transport payload on the target** | `user_create`, `service_create` | The password is spliced into the PowerShell body as `ConvertTo-SecureString '<pw>' -AsPlainText -Force`. Over WinRM it travels inside the (NTLM-encrypted, see Section 5) SOAP request and is executed; over the direct path it is not written to a file, but a local admin watching process/command-line creation on the box could observe it while the command runs. |
| **Windows Credential Manager on the operator** | `rdp_connection_file` (`store_credentials=True`), `rdp_open` | The stored RDP password is written to the **operator** machine (not the target) via `cmdkey /generic:TERMSRV/<host> /user:<user> /pass:<pw>` so `mstsc` connects without prompting. It persists in Credential Manager until removed (`cmdkey /delete:TERMSRV/<host>`). The plaintext is **never** placed in the generated `.rdp` file or returned in the tool result. |

Over WinRM against a **non-builtin local admin** with `LocalAccountTokenFilterPolicy=1`, the
session already holds a high-integrity full token, so `is_elevated()` returns true and
elevated ops run **directly** — no scheduled task and no staged `.ps1` at all. The temp-file
exposure above applies specifically to the filtered-token fallback (SSH, non-elevated local).

None of these secrets are ever returned to the model, and known values are scrubbed from logs
by exact match. To remove the operator-side RDP credential afterward:

```powershell
cmdkey /list                       # audit stored TERMSRV/* entries
cmdkey /delete:TERMSRV/<host>      # remove one
```

## 5. Transport security and MITM

The defaults favor frictionless first contact with a freshly provisioned box; they are
permissive and should be tightened for anything on an untrusted network.

### WinRM

- **Auth defaults to NTLM.** NTLM performs **message-level encryption of the payload even
  over plain HTTP on 5985** — credentials and command bodies are not sent in cleartext on the
  wire. What NTLM over HTTP does **not** provide is server authentication, so it does not by
  itself stop an on-path attacker who can also satisfy the auth challenge relay/MITM
  conditions. `open_transport` deliberately prefers HTTP 5985 unless the host opts into SSL,
  because NTLM already encrypts the payload and it is the fast standard path.
- **`Basic` auth is cleartext.** The provisioning script enables `Basic` as a fallback; over
  HTTP with `AllowUnencrypted`, Basic credentials are plaintext. The default `winrm_auth` is
  `ntlm`, so this only bites if you explicitly select `basic`. Do not use `basic` over HTTP.
- **Cert validation defaults to `ignore`** (`winrm_cert_validation="ignore"` on `Host`). Over
  HTTPS 5986 this accepts self-signed certs — convenient, but MITM-exploitable. The transport
  logs a warning when SSL is used with validation disabled:

  ```
  winrm <host>: HTTPS with cert validation DISABLED (MITM-exploitable);
  set the host's winrm_cert_validation='validate' in production
  ```

  For production, use HTTPS with a trusted certificate and validation on:

  ```powershell
  winrdp-mcp add-host prod01 10.0.0.5 -u Admin -p '***' --ssl
  # then set winrm_cert_validation="validate" on the host (validate against a trusted CA cert)
  ```

### SSH

- **Host-key policy defaults to `auto`** (`ssh_host_key_policy="auto"`), which is
  Paramiko's `AutoAddPolicy` — **trust-on-first-use**. The first connection accepts and
  records whatever host key the server presents. Pragmatic for fresh boxes, but the initial
  handshake is unauthenticated and MITM-able.
- For production set `ssh_host_key_policy="reject"` (`RejectPolicy`): only keys already in the
  operator's `known_hosts` are accepted; an unknown or changed key is refused. Pre-populate
  `known_hosts` out of band.

### Summary

| Setting | Default | Production |
|---|---|---|
| `winrm_auth` | `ntlm` (payload encrypted over HTTP) | `ntlm` over HTTPS, or `credssp`/`kerberos` in a domain |
| `winrm_cert_validation` | `ignore` | `validate` |
| SSH `ssh_host_key_policy` | `auto` (TOFU) | `reject` (known_hosts only) |
| WinRM transport | HTTP 5985 | HTTPS 5986 (`--ssl`) |

## 6. The enable-WinRM provisioning script

The zero-config provisioning ladder (`winrdp_mcp/provision.py`, `ENABLE_WINRM_PS`) runs an
idempotent script on the target to make it manageable. For frictionless first contact it
applies several settings that widen the box's remoting posture. Each, and how to tighten it:

| Setting the script applies | Why | How to tighten after provisioning |
|---|---|---|
| `Service\Auth\Basic = $true` | fallback auth for local accounts | disable Basic once NTLM/Kerberos is confirmed working: `Set-Item WSMan:\localhost\Service\Auth\Basic $false` |
| `Service\AllowUnencrypted = $true` | let Basic work over HTTP | only affects Basic (NTLM still encrypts). Set `$false` once off Basic: `Set-Item WSMan:\localhost\Service\AllowUnencrypted $false` |
| `Client\TrustedHosts = '*'` | let this box act as a WinRM **client** to any host | scope it: `Set-Item WSMan:\localhost\Client\TrustedHosts '10.0.0.0/24' -Force` (or specific hostnames). Not needed at all if you only manage *to* this box, never *from* it. |
| `LocalAccountTokenFilterPolicy = 1` | give non-builtin local admins a **full token over the network** (fixes "Access is denied" and enables direct elevation, Section 4) | this weakens UAC remote-token filtering. Revert with the value `0` if you require filtered remote tokens and accept the scheduled-task elevation fallback instead. |
| Firewall rule `WinRM-HTTP-In-5985` + `Windows Remote Management` group enabled | inbound WinRM | scope the rule's remote address to your management subnet, or move to 5986 and remove the 5985 rule |
| Network profile flipped `Public` → `Private` | `Enable-PSRemoting`/quickconfig refuses on a `Public` profile | leave `Private` only on trusted networks; the WinRM `AllowUnencrypted will not work ... network connection is Public` message is harmless with NTLM (it only concerns Basic) |

The same script is the payload of the paste-once bootstrap one-liner
(`bootstrap_oneliner`, base64 `-EncodedCommand`) and the SMB+WMI cold-start
(`_cold_start_wmi`), so these tightening steps apply regardless of which rung provisioned the
box.

## 7. Argument-injection defense

Most model-supplied values reach PowerShell as **quoted literals**: `ps.ps_string(value)`
emits a single-quoted PowerShell string with embedded `'` doubled (`'` → `''`), so a value
cannot break out of its quotes. This is the primary and preferred path and is used
pervasively (registry values, file paths, usernames, service binary paths, etc.).

Some values, however, are spliced **raw** onto a command line — a package id handed to
`winget`/`choco`, a bare `tscon /dest` token, a host/IP passed to `netsh`, an account or task
name — where quoting is not applied or not sufficient. Because some of those commands run
elevated, these values are validated against strict allowlists/charsets before use
(`winrdp_mcp/tools/_validate.py`), and an out-of-charset value raises `ValidationError`
rather than reaching the shell:

| Validator | Accepts (regex) | Used for |
|---|---|---|
| `enum(value, allowed, name)` | membership in an explicit set (case-sensitive) | `direction` (`Inbound`/`Outbound`), `protocol` (`TCP`/`UDP`), `action` (`Allow`/`Block`), service `start`, task `schedule`, Defender `scan_type`/`scope`, connection `state`, etc. |
| `host(value)` | `^[A-Za-z0-9._:\-]{1,255}$` | hosts/IPs going onto `netsh`/`portproxy` command lines |
| `package(value)` | `^[A-Za-z0-9._+/\-]{1,200}$` | winget/choco package ids on the install command line |
| `obj_name(value)` | `^[^"\r\n\t` + "`" + `|&;<>%]{1,256}$` (rejects double-quote, control chars, and shell meta `` ` | & ; < > % ``) | account, task, service, and registry value names |
| `token(value)` | `^[A-Za-z0-9._:\-]{1,64}$` | bare tokens such as a `tscon` `/dest` target and scheduled-task `start_time` |

This is defense in depth layered on top of the model being a semi-trusted caller — it does
not (and cannot) prevent a caller with access to `run_powershell` from running arbitrary
commands, but it does prevent a crafted argument from injecting into an otherwise-fixed,
possibly-elevated command line. Where a value is both quoted and validated (e.g. names in
`user_create`/`service_create`), both layers apply.

## 8. Reducing attack surface: allowlisting and safety hints

### Per-tool allowlist / blocklist

The 136 tools can be narrowed at startup via environment variables (`winrdp_mcp/server.py`):

```powershell
# Register ONLY these tools (everything else is not exposed to the client):
$env:WINRDP_ENABLED_TOOLS = "list_hosts,system_info,rdp_status,run_powershell"

# Or expose everything except a destructive subset:
$env:WINRDP_DISABLED_TOOLS = "reboot,power_action,user_delete,service_delete,uninstall_software"

winrdp-mcp serve
```

`WINRDP_ENABLED_TOOLS` (CSV) is an allowlist — if set, only those tools are registered.
`WINRDP_DISABLED_TOOLS` (CSV) is a blocklist applied on top. Use these to hand a client the
minimum surface it needs.

### Read-only / destructive annotations

Every tool carries MCP safety annotations. Read-only tools (e.g. `list_hosts`, `system_info`,
`file_read`, `rdp_status`) are marked `readOnlyHint=True, destructiveHint=False`. Destructive
tools (`remove_host`, `file_delete`, `reboot`, `reboot_and_wait`, `power_action`,
`kill_process`, `user_delete`, `service_delete`, `reg_delete`, `firewall_delete`,
`rdp_disable`, `clear_event_log`, `uninstall_software`, `defender_realtime`, `task_delete`,
`cleanup_staged`, `rdp_logoff_session`, `port_proxy_delete`, `install_rdp_wrapper`,
`uac_set`, `rdp_connect_to_console`, `rdp_set_port`) are marked `destructiveHint=True` so an
MCP client can gate them behind confirmation automatically. Mutating-but-not-destructive
tools (create/set/start) are neither. Keep destructive-action confirmation enabled in your
client.

## 9. Hardening checklist

Before pointing winrdp-mcp at anything beyond a disposable lab box:

- [ ] **Set `WINRDP_VAULT_KEY`** to a passphrase from a secret store, so the encryption key is
      never persisted next to `inventory.json`. Do not rely on the on-disk `vault.key` where
      ciphertext and key sit together.
- [ ] **Protect `WINRDP_HOME`** (`inventory.json`, and `vault.key` if used) with owner-only
      ACLs; treat the directory as a secret store. Exclude it from broad backups/sync.
- [ ] **Move WinRM to HTTPS 5986** (`add-host ... --ssl`) with a certificate from a trusted
      CA, and set `winrm_cert_validation="validate"` on each host.
- [ ] **Set `ssh_host_key_policy="reject"`** and pre-populate the operator's `known_hosts`
      for any SSH-managed host.
- [ ] **Never select `winrm_auth="basic"` over HTTP.** Keep NTLM (or use Kerberos/CredSSP in
      a domain).
- [ ] **After provisioning, tighten the box:** scope `Client\TrustedHosts` (or clear it if
      the box is only a management target), disable `Service\Auth\Basic` and
      `Service\AllowUnencrypted`, and scope the WinRM firewall rule to your management subnet.
- [ ] **Decide on `LocalAccountTokenFilterPolicy`.** `1` (set by provisioning) enables direct
      remote elevation but weakens UAC remote-token filtering; revert to `0` if your policy
      requires filtered remote tokens.
- [ ] **Narrow the tool surface** with `WINRDP_ENABLED_TOOLS` / `WINRDP_DISABLED_TOOLS` to
      the minimum the client needs.
- [ ] **Keep destructive-action confirmation on** in your MCP client (the `destructiveHint`
      annotations exist for this).
- [ ] **Keep `WINRDP_DEBUG` off** in shared/logged environments; log redaction is a safety
      net, not a reason to log secrets.
- [ ] **Clean up operator-side RDP credentials** you no longer need:
      `cmdkey /delete:TERMSRV/<host>`.
- [ ] **Confirm authorization.** Only manage boxes you own or are explicitly authorized to
      administer.

---

*See also: `README.md` (Security notes), `NOTICE` (attribution — winrdp-mcp builds on the
MIT-licensed [winremote-mcp](https://github.com/dddabtc/winremote-mcp) and
[windows-admin-mcp](https://github.com/Cosmicjedi/windows-admin-mcp)). MIT licensed.*
