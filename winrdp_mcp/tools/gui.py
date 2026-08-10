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

try:  # rich image return for record_screen when available
    from fastmcp.utilities.types import Image
except Exception:  # pragma: no cover
    Image = None

# Capture the whole virtual desktop to a temp PNG; expose the virtual-screen origin so OCR
# word rectangles can be mapped back to absolute screen coordinates.
_CAPTURE = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;$vx=$b.X;$vy=$b.Y;"
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
    "([System.Drawing.Graphics]::FromImage($bmp)).CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
    "$png=Join-Path $env:TEMP 'winrdp_ocr.png';"
    "$bmp.Save($png,[System.Drawing.Imaging.ImageFormat]::Png);"
)

# Run the built-in Windows OCR engine (WinRT) over $png; produce $ocr (OcrResult) and
# $words (per-word text + absolute-screen center/bounds).
_OCR = (
    "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null;"
    "[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]|Out-Null;"
    "[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]|Out-Null;"
    "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
    "$as=([System.WindowsRuntimeSystemExtensions].GetMethods()|"
    "Where-Object{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and "
    "$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'})[0];"
    "function Await($op,$t){$task=$as.MakeGenericMethod($t).Invoke($null,@($op));$task.Wait(-1)|Out-Null;$task.Result};"
    "$sf=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png)) ([Windows.Storage.StorageFile]);"
    "$st=Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream]);"
    "$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder]);"
    "$sb=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap]);"
    "$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages();"
    "if(-not $eng){throw 'Windows OCR engine unavailable on this box'};"
    "$ocr=Await ($eng.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult]);"
    "$st.Dispose();Remove-Item $png -Force -ErrorAction SilentlyContinue;"
    "$words=@();foreach($ln in $ocr.Lines){foreach($w in $ln.Words){$r=$w.BoundingRect;"
    "$words+=@{text=$w.Text;x=[int]($vx+$r.X+$r.Width/2);y=[int]($vy+$r.Y+$r.Height/2);"
    "left=[int]($vx+$r.X);top=[int]($vy+$r.Y);width=[int]$r.Width;height=[int]$r.Height}}};"
)

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

    # ------------------------------------------------------------------ drag
    @mcp.tool
    def mouse_drag(x1: int, y1: int, x2: int, y2: int, host: Optional[str] = None,
                   button: str = "left", steps: int = 20) -> dict:
        """Press at (x1,y1), drag to (x2,y2), release. button: left | right | middle."""
        if button not in _BTN:
            return {"error": "button must be left|right|middle"}
        down, up = _BTN[button]
        n = max(1, int(steps))
        body = (
            _MOUSE_TYPE +
            f"[WinRDPMouse]::SetCursorPos({int(x1)},{int(y1)})|Out-Null;Start-Sleep -Milliseconds 50;"
            f"[WinRDPMouse]::mouse_event([WinRDPMouse]::{down},0,0,0,0);Start-Sleep -Milliseconds 50;"
            f"$x1={int(x1)};$y1={int(y1)};$x2={int(x2)};$y2={int(y2)};"
            f"for($i=1;$i -le {n};$i++){{"
            f"$xx=[int]($x1+($x2-$x1)*$i/{n});$yy=[int]($y1+($y2-$y1)*$i/{n});"
            "[WinRDPMouse]::SetCursorPos($xx,$yy)|Out-Null;Start-Sleep -Milliseconds 15};"
            f"Start-Sleep -Milliseconds 50;[WinRDPMouse]::mouse_event([WinRDPMouse]::{up},0,0,0,0);"
            f"$result=@{{ok=$true;from=@({int(x1)},{int(y1)});to=@({int(x2)},{int(y2)})}}"
        )
        return _json_as_user(body, host)

    # ------------------------------------------------------------------ waits (screen)
    @mcp.tool
    def wait_for_window(title: str, host: Optional[str] = None, timeout: int = 60,
                        interval: int = 2) -> dict:
        """Wait until a window whose title contains `title` appears on the desktop.
        Polls in-session in a single call (cheap despite the as_user path)."""
        n = max(1, int(timeout) // max(1, int(interval)))
        body = (
            f"$found=$null;for($i=0;$i -lt {n};$i++){{"
            f"$w=Get-Process|Where-Object{{$_.MainWindowTitle -like {ps.ps_string('*' + title + '*')}}}|Select-Object -First 1;"
            f"if($w){{$found=@{{title=$w.MainWindowTitle;process=$w.ProcessName;pid=$w.Id}};break}};"
            f"Start-Sleep -Seconds {int(interval)}}};"
            "$result=if($found){@{reached=$true;window=$found}}else{@{reached=$false}}"
        )
        return _json_as_user(body, host, timeout=int(timeout) + 30)

    # ------------------------------------------------------------------ OCR / vision-lite
    @mcp.tool
    def ocr_screen(host: Optional[str] = None) -> dict:
        """Read text off the live desktop via the built-in Windows OCR engine (Win10+).
        Returns the full text plus per-word screen coordinates (center x/y + bounding box).
        Pairs with mouse_click / find_and_click. (Claude can also just read a screenshot.)"""
        body = _CAPTURE + _OCR + (
            "$result=@{text=$ocr.Text;words=@($words)}"
        )
        return _json_as_user(body, host, timeout=120)

    @mcp.tool
    def find_and_click(text: str, host: Optional[str] = None, button: str = "left",
                       double: bool = False, occurrence: int = 1) -> dict:
        """OCR the desktop, find on-screen `text`, and click its center — vision-lite
        clicking for UIs that UI Automation can't see. occurrence picks the Nth match."""
        if button not in _BTN:
            return {"error": "button must be left|right|middle"}
        down, up = _BTN[button]
        one = f"[WinRDPMouse]::mouse_event([WinRDPMouse]::{down},0,0,0,0);[WinRDPMouse]::mouse_event([WinRDPMouse]::{up},0,0,0,0);"
        clicks = one + ("Start-Sleep -Milliseconds 80;" + one if double else "")
        body = _MOUSE_TYPE + _CAPTURE + _OCR + (
            f"$m=@(@($words)|Where-Object{{$_.text -like {ps.ps_string('*' + text + '*')}}});"
            f"if($m.Count -lt {int(occurrence)}){{$result=@{{ok=$false;error='text not found';"
            "matches=$m.Count;seen=@(@($words)|ForEach-Object{$_.text}|Select-Object -First 40)}}else{"
            f"$t=$m[{int(occurrence) - 1}];[WinRDPMouse]::SetCursorPos($t.x,$t.y)|Out-Null;Start-Sleep -Milliseconds 60;"
            + clicks +
            "$result=@{ok=$true;text=$t.text;x=$t.x;y=$t.y}}"
        )
        return _json_as_user(body, host, timeout=120)

    # ------------------------------------------------------------------ record
    @mcp.tool
    def record_screen(host: Optional[str] = None, seconds: int = 5, fps: int = 4,
                      max_width: int = 960):
        """Record the desktop for a few seconds and return it as an animated GIF.

        Requires a connected interactive session. Frames are downscaled to `max_width` and
        the GIF is shipped base64 over WinRM, so keep it short (a long/large recording can
        exceed transport limits)."""
        frames = max(1, min(int(seconds) * max(1, int(fps)), 60))
        delay_ms = int(1000 / max(1, int(fps)))
        cs = max(1, delay_ms // 10)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing,PresentationCore,WindowsBase;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            f"$sw=[int][Math]::Min($b.Width,{int(max_width)});$sc=$sw/$b.Width;$sh=[int]($b.Height*$sc);"
            "$enc=New-Object System.Windows.Media.Imaging.GifBitmapEncoder;"
            f"for($i=0;$i -lt {frames};$i++){{"
            "$full=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "([System.Drawing.Graphics]::FromImage($full)).CopyFromScreen($b.X,$b.Y,0,0,$full.Size);"
            "$bmp=New-Object System.Drawing.Bitmap $sw,$sh;$g2=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g2.DrawImage($full,0,0,$sw,$sh);$full.Dispose();$g2.Dispose();"
            "$ms=New-Object System.IO.MemoryStream;$bmp.Save($ms,[System.Drawing.Imaging.ImageFormat]::Png);$ms.Position=0;$bmp.Dispose();"
            "$src=New-Object System.Windows.Media.Imaging.BitmapImage;"
            "$src.BeginInit();$src.CacheOption='OnLoad';$src.StreamSource=$ms;$src.EndInit();"
            "$enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($src));"
            f"Start-Sleep -Milliseconds {delay_ms}}};"
            "$out=Join-Path $env:TEMP 'winrdp_rec.gif';"
            "$fs=[System.IO.File]::Create($out);$enc.Save($fs);$fs.Close();"
            "$bytes=[System.IO.File]::ReadAllBytes($out);"
            # patch every Graphic Control Extension delay (0x21 0xF9 0x04 ... [delay lo,hi])
            f"$cs={cs};for($i=0;$i -lt $bytes.Length-8;$i++){{"
            "if($bytes[$i] -eq 0x21 -and $bytes[$i+1] -eq 0xF9 -and $bytes[$i+2] -eq 0x04){"
            "$bytes[$i+4]=[byte]($cs -band 0xFF);$bytes[$i+5]=[byte](($cs -shr 8) -band 0xFF)}};"
            "[System.IO.File]::WriteAllBytes($out,$bytes);"
            "Remove-Item $out -Force -ErrorAction SilentlyContinue;"
            "[Convert]::ToBase64String($bytes)"
        )
        r = ctx.exec_ps(script, host=host, as_user=True, timeout=max(90, frames * 2 + 60))
        import base64
        b64 = "".join(l.strip() for l in r.stdout.splitlines() if l.strip())
        if not b64:
            return {"error": "no recording captured (is an interactive session connected?)", "stderr": r.stderr}
        raw = base64.b64decode(b64)
        if Image is not None:
            return Image(data=raw, format="gif")
        return {"gif_base64": b64, "bytes": len(raw)}
