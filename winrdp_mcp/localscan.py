"""Local folder scanning with glob excludes — the preflight behind ``sync_folder``.

``sync_folder`` used to zip a folder blind: a 2 GB tree with ``node_modules`` in it became
minutes of silence with no way to say "not that directory". This module gives it eyes:

* :class:`ExcludeMatcher` — git-ignore-flavoured glob matching, pruning whole subtrees.
* :func:`scan` — walk the folder once, returning the exact file list that will be shipped,
  the byte total, and *where the weight is* (largest top-level dirs and files) so a caller
  that refuses an oversized sync can say what to exclude instead of just "too big".
* :func:`build_zip` — zip exactly that file list (``shutil.make_archive`` cannot exclude).

Everything here is pure local filesystem work: no transport, no PowerShell, fully testable.
"""

from __future__ import annotations

import fnmatch
import os
import zipfile
from dataclasses import dataclass, field

MB = 1024 * 1024


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip()


class ExcludeMatcher:
    """Glob excludes for a folder walk.

    Rules (deliberately close to .gitignore, and case-insensitive since the targets are
    Windows boxes):

    * a pattern with **no** ``/`` matches the **basename** of any file or directory at any
      depth — ``node_modules``, ``*.bin``, ``.git``;
    * a pattern **with** ``/`` matches the **relative path** from the sync root —
      ``presets/**``, ``build/out/*.bin``, ``src/**/*.map``;
    * a trailing ``/`` restricts the pattern to directories — ``cache/``;
    * matching a directory prunes the whole subtree (it is never walked, so excluding
      ``node_modules`` is also what makes the scan fast).
    """

    def __init__(self, patterns: list[str] | None = None) -> None:
        self.patterns: list[str] = []
        self._names: list[tuple[str, bool]] = []  # (pattern, dir_only)
        self._paths: list[tuple[str, bool]] = []
        for raw in patterns or []:
            if not isinstance(raw, str):  # a client may put a null in the list
                continue
            p = _norm(raw)
            if not p:
                continue
            self.patterns.append(raw)
            dir_only = p.endswith("/")
            p = p.rstrip("/")
            if not p or p == ".":
                continue
            p = p.lower().lstrip("./")
            (self._paths if "/" in p else self._names).append((p, dir_only))

    def __bool__(self) -> bool:
        return bool(self._names or self._paths)

    @staticmethod
    def _trim_tail(pat: str) -> str:
        """``presets/**`` / ``presets/*`` also name the directory itself, so a walk can
        prune it instead of descending and excluding every file one by one."""
        for tail in ("/**", "/*"):
            if pat.endswith(tail):
                return pat[: -len(tail)]
        return pat

    def match(self, rel_path: str, *, is_dir: bool) -> bool:
        rel = _norm(rel_path).lstrip("./").lower()
        name = rel.rsplit("/", 1)[-1]
        for pat, dir_only in self._names:
            if dir_only and not is_dir:
                continue
            if fnmatch.fnmatchcase(name, pat):
                return True
        for pat, dir_only in self._paths:
            if dir_only and not is_dir:
                continue
            if fnmatch.fnmatchcase(rel, pat):
                return True
            if is_dir and fnmatch.fnmatchcase(rel, self._trim_tail(pat)):
                return True
        return False


@dataclass
class Entry:
    abs_path: str
    rel_path: str  # posix, relative to the scan root
    size: int


@dataclass
class Scan:
    root: str
    files: list[Entry] = field(default_factory=list)
    empty_dirs: list[str] = field(default_factory=list)
    total_bytes: int = 0
    excluded_files: int = 0
    excluded_bytes: int = 0
    excluded_dirs: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / MB, 2)

    def heaviest(self, top: int = 5) -> tuple[list[dict], list[dict]]:
        """(largest top-level entries, largest individual files) — what to exclude next."""
        by_top: dict[str, int] = {}
        for e in self.files:
            head = e.rel_path.split("/", 1)[0] if "/" in e.rel_path else e.rel_path
            by_top[head] = by_top.get(head, 0) + e.size
        dirs = [{"path": k, "mb": round(v / MB, 2)}
                for k, v in sorted(by_top.items(), key=lambda kv: -kv[1])[:top]]
        files = [{"path": e.rel_path, "mb": round(e.size / MB, 2)}
                 for e in sorted(self.files, key=lambda e: -e.size)[:top]]
        return dirs, files

    def manifest(self, top: int = 5) -> dict:
        """Compact, model-readable summary of what a sync would ship."""
        dirs, files = self.heaviest(top)
        m = {
            "files": self.file_count,
            "total_mb": self.total_mb,
            "total_bytes": self.total_bytes,
            "largest_entries": dirs,
            "largest_files": files,
        }
        if self.excluded_files or self.excluded_dirs:
            m["excluded"] = {
                "files": self.excluded_files,
                "mb": round(self.excluded_bytes / MB, 2),
                "dirs": self.excluded_dirs[:20],
            }
        if self.unreadable:
            m["unreadable"] = self.unreadable[:10]
        return m


def scan(root: str, exclude: list[str] | None = None) -> Scan:
    """Walk ``root`` once and return exactly what a sync would ship.

    Excluded directories are pruned (not descended), so a correct ``exclude`` makes the
    scan itself cheap. Unreadable entries are collected rather than raised: one locked file
    must not abort a 10 000-file sync.
    """
    m = ExcludeMatcher(exclude)
    s = Scan(root=os.path.abspath(root))
    for dirpath, dirnames, filenames in os.walk(s.root):
        rel_dir = _norm(os.path.relpath(dirpath, s.root))
        if rel_dir == ".":
            rel_dir = ""
        kept_dirs = []
        for d in dirnames:
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if m.match(rel, is_dir=True):
                s.excluded_dirs.append(rel)
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs  # prune in place — os.walk will not descend
        had_file = False
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                s.unreadable.append(rel)
                continue
            if m.match(rel, is_dir=False):
                s.excluded_files += 1
                s.excluded_bytes += size
                continue
            s.files.append(Entry(full, rel, size))
            s.total_bytes += size
            had_file = True
        if rel_dir and not had_file and not kept_dirs:
            s.empty_dirs.append(rel_dir)
    s.files.sort(key=lambda e: e.rel_path)
    return s


def build_zip(s: Scan, dest_zip: str, *, progress=None) -> int:
    """Zip exactly the files in ``s`` (preserving empty directories) and return zip bytes.

    ``progress`` (optional ``callable(done_bytes, total_bytes)``) is called as files are
    added, so a caller can surface "zipping 40%" on a big tree.
    """
    done = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for e in s.files:
            try:
                z.write(e.abs_path, e.rel_path)
            except OSError:
                s.unreadable.append(e.rel_path)
                continue
            done += e.size
            if progress is not None:
                try:
                    progress(done, s.total_bytes)
                except Exception:  # noqa: BLE001 — progress must never break the zip
                    pass
        for d in s.empty_dirs:
            z.writestr(d.rstrip("/") + "/", b"")
    return os.path.getsize(dest_zip)
