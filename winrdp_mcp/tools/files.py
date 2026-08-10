"""File-system tools: list, read, write, search, upload, download, delete, mkdir."""

from __future__ import annotations

from typing import Optional

from .. import ps


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
    def file_write(path: str, content: str, host: Optional[str] = None, append: bool = False) -> dict:
        """Write (or append) UTF-8 text to a file on a box."""
        t = ctx.transport_for(host)
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
