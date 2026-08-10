"""File-system tools: list, read, write, search, upload, download, delete, mkdir."""

from __future__ import annotations

import binascii
import os
import shutil
import tempfile
from typing import Optional

from .. import ps
from ..config import REMOTE_TMP


def register(mcp, ctx) -> None:
    @mcp.tool
    def file_list(path: str, host: Optional[str] = None) -> list:
        """List a directory on a box (files and folders with size and mtime)."""
        body = (
            f"$result=@(Get-ChildItem -LiteralPath {ps.ps_string(path)} -Force|ForEach-Object{{@{{"
            "name=$_.Name;is_dir=$_.PSIsContainer;"
            "size=if($_.PSIsContainer){0}else{$_.Length};"
            "modified=$_.LastWriteTimeUtc.ToString('o');"
            "attributes=$_.Attributes.ToString()}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host))

    @mcp.tool
    def file_read(path: str, host: Optional[str] = None, max_bytes: int = 1_000_000) -> dict:
        """Read a text file from a box (UTF-8). Truncates at max_bytes."""
        body = (
            f"$p={ps.ps_string(path)};$fi=Get-Item -LiteralPath $p;"
            f"$bytes=[IO.File]::ReadAllBytes($p);"
            f"if($bytes.Length -gt {int(max_bytes)}){{$bytes=$bytes[0..{int(max_bytes)-1}];$trunc=$true}}else{{$trunc=$false}};"
            "$text=[Text.Encoding]::UTF8.GetString($bytes);"
            "$result=@{path=$fi.FullName;size=$fi.Length;truncated=$trunc;content=$text}"
        )
        return ctx.exec_json(body, host=host, timeout=120)

    @mcp.tool
    def file_write(path: str, content: str, host: Optional[str] = None, append: bool = False,
                   binary: bool = False) -> dict:
        """Write (or append) content to a file on a box.

        binary=True treats `content` as base64 and writes the decoded bytes (for arbitrary
        binary files); otherwise `content` is UTF-8 text. A non-append write streams the
        bytes over the fast file channel (SMB/SFTP) when available — bypassing the WinRM
        command channel entirely, with no size limit.
        """
        t = ctx.transport_for(host)
        if binary:
            import base64 as _b64
            data = _b64.b64decode(content)
            t.upload(data, path)
            return {"ok": True, "path": path, "bytes": len(data), "binary": True}
        if append:
            # Use .NET AppendAllText with BOM-less UTF-8 so appended bytes are exact and
            # match the non-append (raw upload) path — Add-Content -Encoding utf8 prepends
            # a BOM on first write in Windows PowerShell 5.1.
            r = t.run_ps(
                f"$p={ps.ps_string(path)};$d=Split-Path $p -Parent;"
                "if($d -and -not(Test-Path $d)){New-Item -ItemType Directory -Path $d -Force|Out-Null};"
                f"[IO.File]::AppendAllText($p,{ps.ps_string(content)},[Text.UTF8Encoding]::new($false))",
                timeout=120,
            )
            return {"ok": r.ok, "path": path, "stderr": r.stderr}
        t.upload(content.encode("utf-8"), path)
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}

    @mcp.tool
    def file_search(path: str, pattern: str = "*", host: Optional[str] = None,
                    recurse: bool = True, max_results: int = 200) -> list:
        """Find files by name pattern under a directory."""
        rec = "-Recurse " if recurse else ""
        body = (
            f"$result=@(Get-ChildItem -LiteralPath {ps.ps_string(path)} -Filter {ps.ps_string(pattern)} "
            f"{rec}-File -ErrorAction SilentlyContinue|Select-Object -First {int(max_results)}|ForEach-Object{{@{{"
            "path=$_.FullName;size=$_.Length;modified=$_.LastWriteTimeUtc.ToString('o')}})"
        )
        return ps.as_list(ctx.exec_json(body, host=host, timeout=180))

    @mcp.tool
    def file_upload(local_path: str, remote_path: str, host: Optional[str] = None) -> dict:
        """Upload a file from the operator machine to a box."""
        with open(local_path, "rb") as f:
            data = f.read()
        ctx.transport_for(host).upload(data, remote_path)
        return {"ok": True, "remote_path": remote_path, "bytes": len(data)}

    @mcp.tool
    def file_download(remote_path: str, local_path: str, host: Optional[str] = None) -> dict:
        """Download a file from a box to the operator machine."""
        data = ctx.transport_for(host).download(remote_path)
        with open(local_path, "wb") as f:
            f.write(data)
        return {"ok": True, "local_path": local_path, "bytes": len(data)}

    @mcp.tool
    def file_delete(path: str, host: Optional[str] = None, recurse: bool = False) -> dict:
        """Delete a file or directory on a box."""
        rec = "-Recurse " if recurse else ""
        body = (
            f"Remove-Item -LiteralPath {ps.ps_string(path)} {rec}-Force;"
            "$result=@{deleted=$true;path=" + ps.ps_string(path) + "}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def make_dir(path: str, host: Optional[str] = None) -> dict:
        """Create a directory (and parents) on a box."""
        body = (
            f"New-Item -ItemType Directory -Path {ps.ps_string(path)} -Force|Out-Null;"
            "$result=@{created=$true;path=" + ps.ps_string(path) + "}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def file_copy(source: str, dest: str, host: Optional[str] = None, recurse: bool = True) -> dict:
        """Copy a file or directory on a box (server-side, no round-trip through operator)."""
        rec = "-Recurse " if recurse else ""
        body = (
            f"Copy-Item -LiteralPath {ps.ps_string(source)} -Destination {ps.ps_string(dest)} {rec}-Force;"
            "$result=@{ok=$true;dest=" + ps.ps_string(dest) + "}"
        )
        return ctx.exec_json(body, host=host, timeout=300)

    @mcp.tool
    def file_move(source: str, dest: str, host: Optional[str] = None) -> dict:
        """Move or rename a file/directory on a box."""
        body = (
            f"Move-Item -LiteralPath {ps.ps_string(source)} -Destination {ps.ps_string(dest)} -Force;"
            "$result=@{ok=$true;dest=" + ps.ps_string(dest) + "}"
        )
        return ctx.exec_json(body, host=host, timeout=300)

    @mcp.tool
    def file_hash(path: str, host: Optional[str] = None, algorithm: str = "SHA256") -> dict:
        """Compute a file hash on a box. algorithm: SHA256|SHA1|MD5|SHA384|SHA512."""
        body = (
            f"$h=Get-FileHash -LiteralPath {ps.ps_string(path)} -Algorithm {algorithm};"
            "$result=@{path=$h.Path;algorithm=$h.Algorithm;hash=$h.Hash}"
        )
        return ctx.exec_json(body, host=host, timeout=180)

    @mcp.tool
    def zip_path(source: str, dest_zip: str, host: Optional[str] = None) -> dict:
        """Compress a file/folder into a .zip on a box."""
        body = (
            f"Compress-Archive -Path {ps.ps_string(source)} -DestinationPath {ps.ps_string(dest_zip)} -Force;"
            "$result=@{ok=$true;zip=" + ps.ps_string(dest_zip) + "}"
        )
        return ctx.exec_json(body, host=host, timeout=600)

    @mcp.tool
    def unzip_path(source_zip: str, dest_dir: str, host: Optional[str] = None) -> dict:
        """Extract a .zip archive on a box."""
        body = (
            f"Expand-Archive -Path {ps.ps_string(source_zip)} -DestinationPath {ps.ps_string(dest_dir)} -Force;"
            "$result=@{ok=$true;dest=" + ps.ps_string(dest_dir) + "}"
        )
        return ctx.exec_json(body, host=host, timeout=600)

    @mcp.tool
    def get_acl(path: str, host: Optional[str] = None) -> dict:
        """Read the owner and access rules (ACL) of a file/folder/registry path."""
        body = (
            f"$a=Get-Acl -LiteralPath {ps.ps_string(path)};"
            "$result=@{owner=$a.Owner;access=@($a.Access|ForEach-Object{@{"
            "identity=$_.IdentityReference.Value;rights=$_.FileSystemRights.ToString();"
            "type=$_.AccessControlType.ToString();inherited=$_.IsInherited}})}"
        )
        return ctx.exec_json(body, host=host)

    @mcp.tool
    def grant_acl(path: str, principal: str, rights: str = "FullControl",
                  host: Optional[str] = None, recurse: bool = False) -> dict:
        """Grant a principal rights on a path via icacls. rights: F|M|RX|R|W or a word like
        FullControl->F. Runs elevated. Use take_own first if access is denied."""
        rmap = {"fullcontrol": "F", "modify": "M", "read": "R", "readexecute": "RX", "write": "W"}
        g = rmap.get(rights.lower().replace(" ", ""), rights)
        rec = "/T " if recurse else ""
        r = ctx.exec_ps(
            f"icacls {ps.ps_string(path)} /grant {ps.ps_string(principal + ':' + g)} {rec}/C",
            host=host, elevated=True, timeout=300,
        )
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def take_own(path: str, host: Optional[str] = None, recurse: bool = False) -> dict:
        """Take ownership of a file/folder via takeown (needed before changing a locked
        ACL). Runs elevated."""
        rec = "/R /D Y " if recurse else ""
        r = ctx.exec_ps(f"takeown /F {ps.ps_string(path)} {rec}/A", host=host, elevated=True, timeout=300)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.rc}

    @mcp.tool
    def download_file(url: str, dest: str, host: Optional[str] = None, timeout: int = 600) -> dict:
        """Download a URL directly onto a box (server-side, TLS 1.2). Returns size + SHA-256."""
        body = (
            "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
            f"$d=Split-Path {ps.ps_string(dest)} -Parent;"
            "if($d -and -not(Test-Path $d)){New-Item -ItemType Directory -Path $d -Force|Out-Null};"
            f"(New-Object Net.WebClient).DownloadFile({ps.ps_string(url)},{ps.ps_string(dest)});"
            f"$fi=Get-Item {ps.ps_string(dest)};"
            "$result=@{path=$fi.FullName;bytes=$fi.Length;"
            "sha256=(Get-FileHash $fi.FullName -Algorithm SHA256).Hash}"
        )
        return ctx.exec_json(body, host=host, timeout=timeout)

    @mcp.tool
    def tail_file(path: str, host: Optional[str] = None, lines: int = 50) -> dict:
        """Return the last N lines of a text file on a box (snapshot)."""
        # Cast each line to a plain [string]: Get-Content decorates its output strings with
        # NoteProperties (PSPath/PSDrive/Provider/...) that ConvertTo-Json would otherwise
        # expand into megabytes of provider/assembly metadata.
        body = (
            f"$t=@(Get-Content -LiteralPath {ps.ps_string(path)} -Tail {int(lines)} -ErrorAction Stop|"
            "ForEach-Object{[string]$_});"
            "$result=@{path=" + ps.ps_string(path) + ";lines=$t}"
        )
        return ctx.exec_json(body, host=host, timeout=120)

    @mcp.tool
    def edit_file(path: str, find: str, replace: str, host: Optional[str] = None,
                  regex: bool = False, count_only: bool = False) -> dict:
        """Find/replace inside a text file on a box (literal by default, or `regex`).
        Returns the number of replacements; set count_only to preview without writing."""
        if regex:
            match = f"([regex]::Matches($c,{ps.ps_string(find)})).Count"
            new = f"[regex]::Replace($c,{ps.ps_string(find)},{ps.ps_string(replace)})"
        else:
            match = f"(($c.Length-$c.Replace({ps.ps_string(find)},'').Length)/[Math]::Max(1,{ps.ps_string(find)}.Length))"
            new = f"$c.Replace({ps.ps_string(find)},{ps.ps_string(replace)})"
        write = "" if count_only else (
            f"[IO.File]::WriteAllText({ps.ps_string(path)},$n,[Text.UTF8Encoding]::new($false));")
        body = (
            f"$c=[IO.File]::ReadAllText({ps.ps_string(path)});$cnt=[int]({match});$n={new};"
            + write +
            "$result=@{path=" + ps.ps_string(path) + ";replacements=$cnt;written=" +
            ("$false" if count_only else "$true") + "}"
        )
        return ctx.exec_json(body, host=host, timeout=120)

    @mcp.tool
    def sync_folder(local_path: str, remote_path: str, host: Optional[str] = None,
                    mirror: bool = False) -> dict:
        """Mirror a LOCAL operator folder to a box efficiently (zip → upload → expand).
        mirror=True wipes the destination first so it matches the source exactly."""
        if not os.path.isdir(local_path):
            return {"error": f"not a directory: {local_path}"}
        tmp_base = os.path.join(tempfile.gettempdir(), "winrdp_sync_" + binascii.hexlify(os.urandom(4)).decode())
        zip_path = shutil.make_archive(tmp_base, "zip", local_path)
        try:
            data = open(zip_path, "rb").read()
            rid = binascii.hexlify(os.urandom(4)).decode()
            remote_zip = f"{REMOTE_TMP}\\sync_{rid}.zip"
            t = ctx.transport_for(host)
            t.upload(data, remote_zip)
            clear = (f"if(Test-Path {ps.ps_string(remote_path)}){{Remove-Item -LiteralPath {ps.ps_string(remote_path)} "
                     "-Recurse -Force}};") if mirror else ""
            body = (
                clear +
                f"New-Item -ItemType Directory -Path {ps.ps_string(remote_path)} -Force|Out-Null;"
                f"Expand-Archive -Path {ps.ps_string(remote_zip)} -DestinationPath {ps.ps_string(remote_path)} -Force;"
                f"Remove-Item -LiteralPath {ps.ps_string(remote_zip)} -Force;"
                f"$n=@(Get-ChildItem -LiteralPath {ps.ps_string(remote_path)} -Recurse -File).Count;"
                "$result=@{dest=" + ps.ps_string(remote_path) + ";files=$n;mirror=" +
                ("$true" if mirror else "$false") + "}"
            )
            return ctx.exec_json(body, host=host, timeout=600)
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass

    @mcp.tool
    def transfer_between_hosts(src_host: str, src_path: str, dst_host: str, dst_path: str) -> dict:
        """Copy a file from one registered box to another, straight through the controller."""
        data = ctx.transport_for(src_host).download(src_path)
        ctx.transport_for(dst_host).upload(data, dst_path)
        return {"ok": True, "src": f"{src_host}:{src_path}", "dst": f"{dst_host}:{dst_path}", "bytes": len(data)}
