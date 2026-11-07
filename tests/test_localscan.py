"""Exclude matching + folder scanning — the preflight behind sync_folder."""

from __future__ import annotations

import os
import zipfile

import pytest

from winrdp_mcp.localscan import ExcludeMatcher, build_zip, scan


@pytest.fixture
def tree(tmp_path):
    """A small tree with the shapes people actually want excluded."""
    def w(rel, size=10):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    w("app.py", 100)
    w("README.md", 50)
    w("src/main.py", 200)
    w("src/main.py.map", 400)
    w("node_modules/pkg/index.js", 5000)
    w("node_modules/pkg/nested/deep.js", 5000)
    w("presets/a.json", 300)
    w("presets/sub/b.json", 300)
    w("build/out/blob.bin", 9000)
    w("sub/node_modules/x.js", 7000)
    return tmp_path


# ---------------------------------------------------------------- matcher
def test_bare_pattern_matches_basename_at_any_depth():
    m = ExcludeMatcher(["node_modules"])
    assert m.match("node_modules", is_dir=True)
    assert m.match("sub/deep/node_modules", is_dir=True)
    assert not m.match("src", is_dir=True)


def test_glob_on_basename():
    m = ExcludeMatcher(["*.bin", "*.map"])
    assert m.match("build/out/blob.bin", is_dir=False)
    assert m.match("src/main.py.map", is_dir=False)
    assert not m.match("src/main.py", is_dir=False)


def test_path_pattern_matches_relative_path_only():
    m = ExcludeMatcher(["build/out/*.bin"])
    assert m.match("build/out/blob.bin", is_dir=False)
    assert not m.match("other/out/blob.bin", is_dir=False)


def test_double_star_pattern_also_prunes_its_own_directory():
    m = ExcludeMatcher(["presets/**"])
    assert m.match("presets", is_dir=True)          # pruned as a directory
    assert m.match("presets/sub/b.json", is_dir=False)


def test_trailing_slash_restricts_to_directories():
    m = ExcludeMatcher(["cache/"])
    assert m.match("a/cache", is_dir=True)
    assert not m.match("a/cache", is_dir=False)


def test_matching_is_case_insensitive_and_slash_agnostic():
    m = ExcludeMatcher([r"Build\Out\*.BIN"])
    assert m.match("build/out/blob.bin", is_dir=False)


def test_empty_patterns_match_nothing():
    m = ExcludeMatcher(["", "  ", None and "x"])
    assert not m
    assert not m.match("anything", is_dir=False)


# ---------------------------------------------------------------- scan
def test_scan_without_excludes_sees_everything(tree):
    s = scan(str(tree))
    assert s.file_count == 10
    assert s.total_bytes == 100 + 50 + 200 + 400 + 5000 + 5000 + 300 + 300 + 9000 + 7000
    assert s.excluded_files == 0


def test_scan_prunes_excluded_dirs_at_every_depth(tree):
    s = scan(str(tree), ["node_modules"])
    rels = {e.rel_path for e in s.files}
    assert not any("node_modules" in r for r in rels)
    assert "sub/node_modules" in s.excluded_dirs
    assert s.file_count == 7


def test_scan_combines_dir_and_file_patterns(tree):
    s = scan(str(tree), ["node_modules", "*.bin", "presets/**"])
    assert {e.rel_path for e in s.files} == {"app.py", "README.md", "src/main.py", "src/main.py.map"}
    assert s.excluded_files == 1          # blob.bin (pruned dirs' files are not counted)
    assert s.excluded_bytes == 9000


def test_scan_reports_where_the_weight_is(tree):
    s = scan(str(tree))
    dirs, files = s.heaviest(top=3)
    assert dirs[0]["path"] == "node_modules"
    assert files[0]["path"] == "build/out/blob.bin"
    man = s.manifest()
    assert man["files"] == 10 and man["total_mb"] == s.total_mb
    assert man["largest_entries"][0]["path"] == "node_modules"


def test_scan_rel_paths_are_posix_and_sorted(tree):
    s = scan(str(tree))
    rels = [e.rel_path for e in s.files]
    assert rels == sorted(rels)
    assert all("\\" not in r for r in rels)


def test_scan_keeps_empty_dirs(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "a.txt").write_text("hi")
    s = scan(str(tmp_path))
    assert s.empty_dirs == ["logs"]


# ---------------------------------------------------------------- zip
def test_build_zip_contains_exactly_the_scanned_files(tree, tmp_path):
    s = scan(str(tree), ["node_modules", "*.bin"])
    dest = str(tmp_path.parent / "out.zip")
    seen = []
    size = build_zip(s, dest, progress=lambda d, t: seen.append((d, t)))
    assert size > 0 and os.path.getsize(dest) == size
    with zipfile.ZipFile(dest) as z:
        names = {n for n in z.namelist() if not n.endswith("/")}
    assert names == {e.rel_path for e in s.files}
    assert seen and seen[-1][0] == s.total_bytes


def test_build_zip_preserves_empty_dirs(tmp_path):
    src = tmp_path / "src"
    (src / "logs").mkdir(parents=True)
    (src / "a.txt").write_text("hi")
    s = scan(str(src))
    dest = str(tmp_path / "out.zip")
    build_zip(s, dest)
    with zipfile.ZipFile(dest) as z:
        assert "logs/" in z.namelist()
