"""Native GUI automation — drive the interactive RDP desktop without a heavy agent.

Everything here runs inside the logged-on user's session (``as_user``) via .NET: SendKeys
for keyboard, user32 P/Invoke for the mouse, and UI Automation to find/click/read controls
by name. Requires an active/connected interactive session (a disconnected RDP session has
no composed desktop). Each call spawns a scheduled task in the session, so it carries the
usual ``as_user`` latency — for multi-step sequences, prefer one ``run_powershell(as_user=
True)`` block, or ``gui_script`` here which pre-loads the mouse/UIA helpers.
"""

from __future__ import annotations

from typing import Optional

from .. import ps

# Inline user32 mouse helper, compiled per (fresh) session process.
_MOUSE_TYPE = (
    "Add-Type @'\n"
    "using System;using System.Runtime.InteropServices;\n"
    "public class WinRDPMouse{\n"
    " [DllImport(\"user32.dll\")] public static extern bool SetCursorPos(int x,int y);\n"
    " [DllImport(\"user32.dll\")] public static extern void mouse_event(uint f,uint dx,uint dy,uint d,int e);\n"
    " public const uint MOVE=0x0001,LDOWN=0x0002,LUP=0x0004,RDOWN=0x0008,RUP=0x0010,MDOWN=0x0020,MUP=0x0040;\n"
    "}\n'@\n"
)

_BTN = {
    "left": ("LDOWN", "LUP"),
    "right": ("RDOWN", "RUP"),
    "middle": ("MDOWN", "MUP"),
}


def _sendkeys_escape(text: str) -> str:
    """Escape literal text for SendKeys (its metacharacters are {}()+^%~[])."""
    out = []
    for ch in text:
        if ch in "{}()+^%~[]":
            out.append("{" + ch + "}")
        elif ch == "\r":
            continue
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    return "".join(out)


def register(mcp, ctx) -> None:
    def _json_as_user(body: str, host, timeout=90):
        r = ctx.exec_ps(ps.wrap_json(body), host=host, as_user=True, timeout=timeout)
        return ps.parse_json(r.stdout)

    # ------------------------------------------------------------------ windows
    @mcp.tool
    def list_windows(host: Optional[str] = None) -> list:
        """List the top-level windows on the interactive desktop (title, pid, handle, bounds)."""
        body = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$result=@(Get-Process|Where-Object{$_.MainWindowHandle -ne 0 -and $_.MainWindowTitle}|"
            "ForEach-Object{@{title=$_.MainWindowTitle;process=$_.ProcessName;pid=$_.Id;"
            "handle=[int64]$_.MainWindowHandle}})"
        )
        return ps.as_list(_json_as_user(body, host))

    @mcp.tool
    def focus_window(title: str, host: Optional[str] = None) -> dict:
        """Bring a window to the foreground by (partial) title so keystrokes land in it."""
        body = (
            f"$w=New-Object -ComObject WScript.Shell;$ok=$w.AppActivate({ps.ps_string(title)});"
            "Start-Sleep -Milliseconds 300;$result=@{activated=$ok;title=" + ps.ps_string(title) + "}"
        )
        return _json_as_user(body, host)

    # ------------------------------------------------------------------ keyboard
    @mcp.tool
    def send_keys(keys: str, host: Optional[str] = None, window: Optional[str] = None) -> dict:
        """Send keystrokes to the active window using SendKeys syntax.

        Examples: '^s' (Ctrl+S), '%{F4}' (Alt+F4), '{ENTER}', '{TAB}Hello'. Use `window`
        to focus a window by title first. For literal text prefer type_text.
        """
        pre = f"$w=New-Object -ComObject WScript.Shell;[void]$w.AppActivate({ps.ps_string(window)});Start-Sleep -Milliseconds 300;" if window else ""
        body = (
            "Add-Type -AssemblyName System.Windows.Forms;" + pre +
            f"[System.Windows.Forms.SendKeys]::SendWait({ps.ps_string(keys)});"
            "$result=@{sent=$true}"
        )
        return _json_as_user(body, host)

    @mcp.tool
    def type_text(text: str, host: Optional[str] = None, window: Optional[str] = None) -> dict:
        """Type literal text into the active window (SendKeys metacharacters auto-escaped).
        Use `window` to focus a window by title first."""
        pre = f"$w=New-Object -ComObject WScript.Shell;[void]$w.AppActivate({ps.ps_string(window)});Start-Sleep -Milliseconds 300;" if window else ""
        body = (
            "Add-Type -AssemblyName System.Windows.Forms;" + pre +
            f"[System.Windows.Forms.SendKeys]::SendWait({ps.ps_string(_sendkeys_escape(text))});"
            "$result=@{typed=$true;length=" + str(len(text)) + "}"
        )
        return _json_as_user(body, host)

    # ------------------------------------------------------------------ mouse
    @mcp.tool
    def mouse_move(x: int, y: int, host: Optional[str] = None) -> dict:
        """Move the mouse cursor to screen coordinates (x, y)."""
        body = _MOUSE_TYPE + f"[WinRDPMouse]::SetCursorPos({int(x)},{int(y)})|Out-Null;$result=@{{x={int(x)};y={int(y)}}}"
        return _json_as_user(body, host)

    @mcp.tool
    def mouse_click(x: int, y: int, host: Optional[str] = None, button: str = "left",
                    double: bool = False) -> dict:
        """Click at screen coordinates (x, y). button: left | right | middle."""
        if button not in _BTN:
            return {"error": "button must be left|right|middle"}
        down, up = _BTN[button]
        one = f"[WinRDPMouse]::mouse_event([WinRDPMouse]::{down},0,0,0,0);[WinRDPMouse]::mouse_event([WinRDPMouse]::{up},0,0,0,0);"
        clicks = one + ("Start-Sleep -Milliseconds 80;" + one if double else "")
        body = (
            _MOUSE_TYPE +
            f"[WinRDPMouse]::SetCursorPos({int(x)},{int(y)})|Out-Null;Start-Sleep -Milliseconds 60;"
            + clicks +
            f"$result=@{{x={int(x)};y={int(y)};button='{button}';double=${str(double).lower()}}}"
        )
        return _json_as_user(body, host)

    # ------------------------------------------------------------------ UI Automation
    @mcp.tool
    def ui_find(name: Optional[str] = None, control_type: Optional[str] = None,
                host: Optional[str] = None, top: int = 60) -> list:
        """Find UI elements on the desktop via UI Automation.

        Filter by (partial) `name` and/or `control_type` (e.g. Button, Edit, MenuItem,
        CheckBox, Text, ComboBox). Returns name, type, automation id, and bounding rect —
        use the rect center with mouse_click, or ui_invoke by name.
        """
        conds = []
        if control_type:
            conds.append(f"$_.Current.ControlType.ProgrammaticName -like '*.{control_type}'")
        if name:
            conds.append(f"$_.Current.Name -like {ps.ps_string('*' + name + '*')}")
        where = ("|Where-Object{" + " -and ".join(conds) + "}") if conds else ""
        body = (
            "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            "$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,"
            "[System.Windows.Automation.Condition]::TrueCondition);"
            f"$result=@(@($all){where}|Select-Object -First {int(top)}|ForEach-Object{{"
            "$r=$_.Current.BoundingRectangle;@{name=$_.Current.Name;"
            "type=$_.Current.ControlType.ProgrammaticName -replace 'ControlType\\.','';"
            "automation_id=$_.Current.AutomationId;enabled=$_.Current.IsEnabled;"
            "x=[int]($r.X+$r.Width/2);y=[int]($r.Y+$r.Height/2);"
            "left=[int]$r.X;top=[int]$r.Y;width=[int]$r.Width;height=[int]$r.Height}})"
        )
        return ps.as_list(_json_as_user(body, host, timeout=120))

    @mcp.tool
    def ui_invoke(name: str, host: Optional[str] = None) -> dict:
        """Find a control by (partial) name and activate it — Invoke (buttons), else Toggle,
        else click its center. The reliable way to press a button without pixel math."""
        body = (
            "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            f"$c=New-Object System.Windows.Automation.PropertyCondition("
            "[System.Windows.Automation.AutomationElement]::NameProperty,"
            f"{ps.ps_string(name)});"
            "$e=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$c);"
            "if(-not $e){$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,"
            "[System.Windows.Automation.Condition]::TrueCondition);"
            f"$e=@($all)|Where-Object{{$_.Current.Name -like {ps.ps_string('*' + name + '*')}}}|Select-Object -First 1}};"
            "if(-not $e){$result=@{ok=$false;error='element not found'}}else{"
            "$ip=$null;if($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$ip)){"
            "$ip.Invoke();$result=@{ok=$true;action='invoke';name=$e.Current.Name}}"
            "elseif($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern,[ref]$ip)){"
            "$ip.Toggle();$result=@{ok=$true;action='toggle';name=$e.Current.Name}}"
            "else{$r=$e.Current.BoundingRectangle;$result=@{ok=$true;action='click';"
            "x=[int]($r.X+$r.Width/2);y=[int]($r.Y+$r.Height/2);name=$e.Current.Name}}}"
        )
        return _json_as_user(body, host, timeout=120)

    @mcp.tool
    def ui_set_text(name: str, text: str, host: Optional[str] = None) -> dict:
        """Set the text of an input control found by (partial) name (UIA ValuePattern)."""
        body = (
            "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            "$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,"
            "[System.Windows.Automation.Condition]::TrueCondition);"
            f"$e=@($all)|Where-Object{{$_.Current.Name -like {ps.ps_string('*' + name + '*')}}}|Select-Object -First 1;"
            "if(-not $e){$result=@{ok=$false;error='element not found'}}else{"
            "$vp=$null;if($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$vp)){"
            f"$vp.SetValue({ps.ps_string(text)});$result=@{{ok=$true;name=$e.Current.Name}}}}"
            "else{$result=@{ok=$false;error='no ValuePattern (not an editable control)'}}}"
        )
        return _json_as_user(body, host, timeout=120)

    # ------------------------------------------------------------------ batch
    @mcp.tool
    def gui_script(script: str, host: Optional[str] = None, timeout: int = 180) -> dict:
        """Run a PowerShell block in the interactive session with GUI helpers pre-loaded —
        the one-call way to do a multi-step GUI sequence without paying per-action latency.

        Pre-loaded: System.Windows.Forms (SendKeys), the [WinRDPMouse] class
        (SetCursorPos + mouse_event with LDOWN/LUP/RDOWN/RUP/MDOWN/MUP consts), and
        UIAutomationClient/Types. Your script's last expression is returned as text.
        """
        preload = (
            "Add-Type -AssemblyName System.Windows.Forms,UIAutomationClient,UIAutomationTypes;"
            + _MOUSE_TYPE
        )
        r = ctx.exec_ps(preload + script, host=host, as_user=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}
