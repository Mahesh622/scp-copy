#!/usr/bin/env python3
"""Non-interactive self-test for scp_select core logic (no SSH server needed)."""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scp_select as S


class FakeSFTP:
    def __init__(self, tree):
        self.tree = tree  # dict path -> True(file) or dict(dir)

    def stat(self, path):
        class A:
            st_size = 123
        return A()


class FakeRemote:
    """Mimics the remote listing/stat for plan_pull using a nested in-memory tree.
    Tree shape: {name: True (file) | dict (subdir)}, rooted at '/'."""

    def __init__(self, tree):
        self.tree = tree
        self.sftp = FakeSFTP(tree)

    def _resolve(self, path):
        if path in ("/", ""):
            return self.tree
        node = self.tree
        for part in [p for p in path.split("/") if p]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def is_dir(self, path):
        return isinstance(self._resolve(path), dict)

    def listdir(self, path):
        out = []
        if path not in ("/", ""):
            out.append(S.Entry(name="..", is_dir=True, size=0,
                               path=os.path.dirname(path.rstrip("/")) or "/"))
        node = self._resolve(path)
        if isinstance(node, dict):
            for name, val in sorted(node.items()):
                is_dir = isinstance(val, dict)
                out.append(S.Entry(name=name, is_dir=is_dir, size=0 if is_dir else 50,
                                   path=S.posix_join(path, name)))
        return out


def test_human_size():
    assert S.human_size(0) == "0B"
    assert S.human_size(512) == "512B"
    assert S.human_size(2048) == "2.0K"
    assert S.human_size(1048576) == "1.0M"
    assert S.human_size(1073741824) == "1.0G"
    print("ok  human_size")


def test_config_roundtrip(tmp_path):
    cfg_dir = tmp_path / ".config" / "scp-select"
    cfg_file = cfg_dir / "projects.json"
    S.CONFIG_DIR = cfg_dir
    S.CONFIG_FILE = cfg_file
    cfg = S.load_config()
    assert cfg["projects"] == []
    p = S.Project(name="demo", host="user@1.2.3.4", port=2222,
                  local_dir="/tmp/local", remote_dir="/var/www", use_agent=True)
    cfg["projects"].append(S.project_to_dict(p))
    S.save_config(cfg)
    cfg2 = S.load_config()
    assert len(cfg2["projects"]) == 1
    p2 = S.project_from_dict(cfg2["projects"][0])
    assert p2.name == "demo"
    assert p2.host == "user@1.2.3.4"
    assert p2.port == 2222
    assert p2.local_dir == "/tmp/local"
    assert p2.remote_dir == "/var/www"
    print("ok  config roundtrip")


def test_local_list(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")
    entries = S.local_list(str(tmp_path))
    names = {e.name for e in entries}
    assert "a.txt" in names
    assert "sub" in names
    sub = next(e for e in entries if e.name == "sub")
    assert sub.is_dir is True
    a = next(e for e in entries if e.name == "a.txt")
    assert a.is_dir is False and a.size == 5
    print("ok  local_list")


def test_plan_push(tmp_path):
    root = tmp_path / "src"
    (root / "d1").mkdir(parents=True)
    (root / "d1" / "x.txt").write_text("xxxxx")
    (root / "d1" / "d2").mkdir()
    (root / "d1" / "d2" / "y.txt").write_text("yy")
    (root / "top.txt").write_text("t")
    files, dirs = S.plan_push([str(root / "d1"), str(root / "top.txt")], "/remote", None)
    rpaths = {rp for _, rp, _ in files}
    assert "/remote/d1/x.txt" in rpaths
    assert "/remote/d1/d2/y.txt" in rpaths
    assert "/remote/top.txt" in rpaths
    assert "/remote/d1" in dirs
    assert "/remote/d1/d2" in dirs
    total = sum(s for _, _, s in files)
    assert total == 5 + 2 + 1
    print("ok  plan_push (recursive, dirs + files)")


def test_plan_pull(tmp_path):
    tree = {
        "srv": {
            "app": {
                "main.py": True,
                "lib": {"util.py": True},
            },
            "readme.md": True,
        }
    }
    fr = FakeRemote(tree)
    files, dirs = S.plan_pull(["/srv/app", "/srv/readme.md"], str(tmp_path / "dst"), fr)
    lpaths = {lp for _, lp, _ in files}
    assert (str(tmp_path / "dst" / "app" / "main.py")) in lpaths
    assert (str(tmp_path / "dst" / "app" / "lib" / "util.py")) in lpaths
    assert (str(tmp_path / "dst" / "readme.md")) in lpaths
    assert (str(tmp_path / "dst" / "app")) in dirs
    assert (str(tmp_path / "dst" / "app" / "lib")) in dirs
    print("ok  plan_pull (recursive, dirs + files)")


def test_do_pull_writes_files(tmp_path):
    # Build a real local source tree, push it into an in-memory remote is not trivial;
    # instead test do_pull by faking sftp.get to copy from a real source.
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "f.txt").write_text("payload")
    tree = {"r": {"a": {"f.txt": True}}}

    class SFTPGet(FakeSFTP):
        def get(self, rp, lp, callback=None):
            # map remote path back to local source for the test
            local_src = str(src) + rp[len("/r"):]
            shutil.copyfile(local_src, lp)
            if callback:
                callback(os.path.getsize(local_src), os.path.getsize(local_src))

    class R(FakeRemote):
        pass
    r = R(tree)
    r.sftp = SFTPGet(tree)
    files, dirs = S.plan_pull(["/r/a"], str(tmp_path / "dst"), r)
    prog = S.TransferProgress(sum(s for _, _, s in files), len(files), None)
    S.do_pull(r, files, dirs, prog)
    out = tmp_path / "dst" / "a" / "f.txt"
    assert out.read_text() == "payload"
    assert prog.done_files == 1
    print("ok  do_pull writes files + progress")


def test_posix_dirname_basename():
    assert S.posix_dirname("/") == "/"
    assert S.posix_dirname("") == "/"
    assert S.posix_dirname("/var") == "/"
    assert S.posix_dirname("/var/log") == "/var"
    assert S.posix_dirname("/var/log/") == "/var"
    assert S.posix_dirname("relative") == "/"
    assert S.posix_basename("") == ""
    assert S.posix_basename("/") == ""
    assert S.posix_basename("/var") == "var"
    assert S.posix_basename("/var/log") == "log"
    assert S.posix_basename("/var/log/") == "log"
    print("ok  posix_dirname / posix_basename")


def test_local_suggest(tmp_path):
    sub = tmp_path / "suggest_test"
    sub.mkdir()
    (sub / "alpha.txt").write_text("x")
    (sub / "alpha_dir").mkdir()
    (sub / "beta.txt").write_text("y")
    base = str(sub)
    # prefix filter
    res = S.local_suggest(base + "/al")
    names = [os.path.basename(r.rstrip(os.sep)) for r in res]
    assert "alpha.txt" in names and "alpha_dir" in names and "beta.txt" not in names
    # directories get a trailing separator so you can drill in
    assert any(r.endswith(os.sep) for r in res)
    # trailing slash -> list contents of that dir, empty prefix
    res2 = S.local_suggest(base + os.sep)
    assert len(res2) == 3
    # nonexistent parent -> no crash, empty list
    assert S.local_suggest("/no/such/parent/xyz") == []
    print("ok  local_suggest (prefix filter, dir trailing sep, missing parent)")


def test_remote_suggest():
    tree = {
        "srv": {
            "app": {"main.py": True, "config.yml": True},
            "archive": True,
            "readme.md": True,
        }
    }
    fr = FakeRemote(tree)
    suggest = S.remote_suggest(fr)

    def base_name(r):
        return r.rstrip("/").rsplit("/", 1)[-1]

    # list /srv entries
    res = suggest("/srv/")
    names = [base_name(r) for r in res]
    assert "app" in names and "archive" in names and "readme.md" in names
    # dirs carry trailing slash
    assert any(r.endswith("/") for r in res)
    # prefix filter on /srv/a
    res2 = suggest("/srv/a")
    basenames = {base_name(r) for r in res2}
    assert "app" in basenames and "archive" in basenames
    assert "readme.md" not in basenames
    # drill into a subdir with trailing slash
    res3 = suggest("/srv/app/")
    inner = {base_name(r) for r in res3}
    assert "main.py" in inner and "config.yml" in inner
    # empty input lists root
    assert isinstance(suggest(""), list)
    print("ok  remote_suggest (prefix filter, drill-in, trailing slash, root)")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = __import__("pathlib").Path(d)
        test_human_size()
        test_posix_dirname_basename()
        test_config_roundtrip(tmp)
        test_local_list(tmp)
        test_local_suggest(tmp)
        test_remote_suggest()
        test_plan_push(tmp)
        test_plan_pull(tmp)
        test_do_pull_writes_files(tmp)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
