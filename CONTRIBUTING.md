# Contributing to winrdp-mcp

Thanks for helping improve winrdp-mcp. This guide covers the dev setup, the quality bar,
and how to add a tool.

## Dev setup

```bash
git clone <your-fork>
cd Windows-RDP-MCP
python -m venv .venv
# Windows:  .venv\Scripts\activate      | POSIX: source .venv/bin/activate
pip install -e ".[dev,bootstrap]"
```

The controller runs on any OS (Windows/macOS/Linux); the managed targets are Windows.

## Quality bar

Before opening a PR, all of these must pass:

```bash
python -m compileall -q winrdp_mcp     # no syntax errors
ruff check winrdp_mcp tests            # lint (F, E, I, W)
pytest -q                              # unit tests (30+; Windows-only tests skip elsewhere)
```

- **Correctness first**, then clarity, then speed. Handle the unhappy paths.
- **Never** wrap a `pywinrm` call in a thread watchdog — the `Session` is not thread-safe
  and abandoning a call mid-flight corrupts the connection (see `transports.py`). The
  per-call bound is `read_timeout_sec`; a dropped/wedged connection is healed by the
  transport's reconnect-and-retry.
- **Quote or validate every model-supplied argument** that reaches a command line:
  `ps.ps_string()` for PowerShell literals, `tools/_validate.py` (enum/charset) for values
  spliced raw (paths, names, package ids, enums). Several tools run elevated.
- **Long operations** (minutes-scale installs) must go through `context.run_long()` so a
  mid-op WinRM disconnect doesn't fail them.
- **Never** return a secret in a tool result or log it (logs are stderr-only and redacted).

## Adding a tool

1. Add the function inside the relevant module's `register(mcp, ctx)` in `winrdp_mcp/tools/`.
2. Decorate with `@mcp.tool`; give it a clear docstring (that IS the model-facing API) and
   an optional `host: Optional[str] = None` first among the box selectors.
3. Build the PowerShell body with `ps.ps_string()` for any interpolated value; return
   structured data via `ctx.exec_json(...)` (assign `$result`) or `ctx.exec_ps(...)`.
4. Classify it in `winrdp_mcp/server.py`: add its name to `READONLY` or `DESTRUCTIVE` so it
   gets the right safety annotation.
5. If it's a new module, register it in `winrdp_mcp/tools/__init__.py`.
6. Add a unit test (pure logic) and, where practical, a Windows-guarded local test.
7. Document it in `docs/TOOLS.md`.

## Testing against a real box

Integration tests are opt-in. Point them at a box you own:

```bash
export WINRDP_TEST_HOST=... WINRDP_TEST_USER=... WINRDP_TEST_PASS=...
```

Never commit credentials. Use a throwaway VM; a >= 4 GB / 2 vCPU box avoids the
resource-thrash slowness small boxes exhibit under sustained WinRM load.

## Commit / PR

- Small, focused commits with clear messages.
- Describe the change, the reasoning, and how you verified it.
- MIT-licensed contributions only; keep the `NOTICE` attributions intact.
