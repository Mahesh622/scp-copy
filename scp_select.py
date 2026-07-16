#!/usr/bin/env python3
"""
scp-select - an interactive TUI for selecting files to push to / pull from a server.

Save "projects" that map a local directory to a remote host + directory, then browse
both sides, toggle-select files, and transfer them with a single keypress instead of
typing long scp paths by hand.
"""
import argparse
import curses
import os
import sys
import json
import re
import stat
import shutil
import tempfile
import time
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path
from typing import List, Optional

import paramiko

CONFIG_DIR = Path.home() / ".config" / "scp-select"
CONFIG_FILE = CONFIG_DIR / "projects.json"
KNOWN_HOSTS_FILE = CONFIG_DIR / "known_hosts"
VERSION = "1.1.0"
CONFIG_WARNING = ""

_COLORS = False


@dataclass
class Project:
    name: str
    host: str = ""
    port: int = 22
    local_dir: str = str(Path.home())
    remote_dir: str = ""
    identity_file: str = ""
    use_agent: bool = True


def project_from_dict(d: dict) -> Project:
    try:
        port = int(d.get("port", 22) or 22)
    except (TypeError, ValueError):
        port = 22
    return Project(
        name=d.get("name", ""),
        host=d.get("host", ""),
        port=port,
        local_dir=os.path.expanduser(d.get("local_dir", "") or str(Path.home())),
        remote_dir=d.get("remote_dir", ""),
        identity_file=os.path.expanduser(d.get("identity_file", "") or ""),
        use_agent=(
            d.get("use_agent", True) if isinstance(d.get("use_agent", True), bool)
            else str(d.get("use_agent", True)).strip().lower() in ("y", "yes", "1", "true")
        ),
    )


def project_to_dict(p: Project) -> dict:
    return asdict(p)


def load_config() -> dict:
    global CONFIG_WARNING
    CONFIG_WARNING = ""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("projects", []), list):
                raise ValueError("expected an object with a projects list")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.corrupt-{stamp}")
            try:
                shutil.copy2(CONFIG_FILE, backup)
                CONFIG_WARNING = f"Invalid config backed up to {backup.name}: {exc}"
            except OSError:
                CONFIG_WARNING = f"Invalid config could not be backed up: {exc}"
            data = {}
    else:
        data = {}
    data.setdefault("projects", [])
    return data


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".projects-", suffix=".tmp", dir=str(CONFIG_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def validate_project(project: Project) -> List[str]:
    errors = []
    if not project.name.strip():
        errors.append("Name is required.")
    host = project.host.rsplit("@", 1)[-1].strip()
    if not host or any(ch.isspace() for ch in host):
        errors.append("Host must be a hostname or user@hostname without spaces.")
    if not 1 <= project.port <= 65535:
        errors.append("Port must be between 1 and 65535.")
    local_dir = os.path.expanduser(project.local_dir)
    if not os.path.isdir(local_dir):
        errors.append("Local directory must exist and be a directory.")
    if project.identity_file and not os.path.isfile(os.path.expanduser(project.identity_file)):
        errors.append("Identity file does not exist.")
    if project.remote_dir and not project.remote_dir.startswith("/"):
        errors.append("Remote directory must be an absolute path.")
    return errors


def human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            if unit == "B":
                return f"{int(n)}{unit}"
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def cp(n: int):
    return curses.color_pair(n) if _COLORS else 0


def posix_join(a: str, b: str) -> str:
    if a.endswith("/"):
        return a + b
    return a + "/" + b


def posix_dirname(path: str) -> str:
    """Parent directory of a posix path. '/' -> '/'."""
    if not path or path == "/":
        return "/"
    stripped = path.rstrip("/")
    idx = stripped.rfind("/")
    if idx < 0:
        return "/"
    return stripped[:idx] or "/"


def posix_basename(path: str) -> str:
    if not path:
        return ""
    return path.rstrip("/").rsplit("/", 1)[-1]


def fuzzy_score(query: str, candidate: str) -> Optional[int]:
    # Lower scores represent tighter case-insensitive subsequence matches.
    query = query.casefold().strip()
    candidate_folded = candidate.casefold()
    if not query:
        return 0
    exact_at = candidate_folded.find(query)
    if exact_at >= 0:
        return exact_at * 10 + len(candidate) - len(query)

    positions = []
    start = 0
    for char in query:
        found = candidate_folded.find(char, start)
        if found < 0:
            return None
        positions.append(found)
        start = found + 1
    gaps = sum(b - a - 1 for a, b in zip(positions, positions[1:]))
    word_bonus = sum(
        1 for pos in positions
        if pos == 0 or candidate_folded[pos - 1] in " /_.-"
    )
    return 1000 + positions[0] * 10 + gaps * 4 + len(candidate) - word_bonus * 6


def local_suggest(text: str) -> List[str]:
    """Suggest local path completions for the typed text (dirs get trailing sep)."""
    t = os.path.expanduser(text or "")
    if not t:
        parent, prefix = str(Path.home()), ""
    elif t.endswith(os.sep):
        parent, prefix = t.rstrip(os.sep) or os.sep, ""
    else:
        parent, prefix = (os.path.dirname(t) or "."), os.path.basename(t)
    if not os.path.isdir(parent):
        return []
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return []
    ranked = []
    for name in names:
        score = fuzzy_score(prefix, name)
        if score is None:
            continue
        full = os.path.join(parent, name)
        if os.path.isdir(full):
            full += os.sep
        ranked.append((score, name.casefold(), full))
    return [full for _, _, full in sorted(ranked)]


def remote_suggest(remote: "Remote"):
    """Return a suggest(text) closure backed by a connected Remote (SFTP)."""
    def suggest(text: str) -> List[str]:
        if not text:
            parent, prefix = "/", ""
        elif text.endswith("/"):
            parent, prefix = text.rstrip("/") or "/", ""
        else:
            parent, prefix = posix_dirname(text), posix_basename(text)
        try:
            entries = remote.listdir(parent)
        except IOError:
            return []
        ranked = []
        for e in entries:
            if e.name in (".", ".."):
                continue
            score = fuzzy_score(prefix, e.name)
            if score is None:
                continue
            full = posix_join(parent, e.name)
            if e.is_dir:
                full += "/"
            ranked.append((score, e.name.casefold(), full))
        return [full for _, _, full in sorted(ranked)]
    return suggest


def _connect_for_suggest(stdscr, project: Project) -> Optional["Remote"]:
    """Open a connection (password retry via prompt) for live path suggestions."""
    r = Remote(project)
    pwd = None
    for _ in range(3):
        try:
            r.connect(password=pwd)
            return r
        except (paramiko.AuthenticationException, paramiko.SSHException):
            pwd = password_prompt(stdscr)
            if pwd is None:
                return None
        except Exception:
            return None
    return None


class Remote:
    def __init__(self, project: Project):
        self.project = project
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None
        self.connected = False

    def connect(self, password: Optional[str] = None) -> None:
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        KNOWN_HOSTS_FILE.touch(mode=0o600, exist_ok=True)
        try:
            KNOWN_HOSTS_FILE.chmod(0o600)
        except OSError:
            pass
        client.load_host_keys(str(KNOWN_HOSTS_FILE))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        host = self.project.host
        username = "root"
        port = self.project.port
        if "@" in host:
            username, host = host.split("@", 1)
        kwargs = {"hostname": host, "port": port, "username": username, "timeout": 15}
        if self.project.identity_file:
            kwargs["key_filename"] = self.project.identity_file
        if self.project.use_agent:
            kwargs["allow_agent"] = True
            kwargs["look_for_keys"] = True
        else:
            kwargs["allow_agent"] = False
            kwargs["look_for_keys"] = False
        if password:
            kwargs["password"] = password
        client.connect(**kwargs)
        self.client = client
        self.sftp = client.open_sftp()
        self.connected = True

    def exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
            return True
        except IOError:
            return False

    def close(self):
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.connected = False

    def listdir(self, path: str) -> List["Entry"]:
        out = []
        if path not in ("/", ""):
            out.append(Entry(name="..", is_dir=True, size=0,
                             path=os.path.dirname(path.rstrip("/")) or "/"))
        for attr in self.sftp.listdir_attr(path):
            is_dir = stat.S_ISDIR(attr.st_mode or 0)
            out.append(Entry(name=attr.filename, is_dir=is_dir, size=attr.st_size or 0,
                             path=posix_join(path, attr.filename), mtime=attr.st_mtime or 0))
        return out

    def is_dir(self, path: str) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(path).st_mode or 0)
        except Exception:
            return False

    def mkdir(self, path: str) -> None:
        try:
            self.sftp.mkdir(path)
        except IOError:
            pass

    def rename(self, path: str, new_name: str) -> str:
        target = posix_join(posix_dirname(path), new_name)
        if self.exists(target):
            raise FileExistsError(f"target already exists: {target}")
        self.sftp.rename(path, target)
        return target

    def rmtree(self, path: str) -> None:
        for e in self.listdir(path):
            if e.name in (".", ".."):
                continue
            if e.is_dir:
                self.rmtree(e.path)
            else:
                self.sftp.remove(e.path)
        self.sftp.rmdir(path)

@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    path: str
    mtime: float = 0


def local_list(path: str) -> List[Entry]:
    out: List[Entry] = []
    if path.rstrip("/") != "":
        out.append(Entry(name="..", is_dir=True, size=0,
                         path=os.path.dirname(path.rstrip("/")) or "/"))
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return out
    for n in names:
        fp = os.path.join(path, n)
        try:
            st = os.lstat(fp)
            is_dir = stat.S_ISDIR(st.st_mode)
            size = 0 if is_dir else st.st_size
        except OSError:
            continue
        out.append(Entry(name=n, is_dir=is_dir, size=size, path=fp, mtime=st.st_mtime))
    return out


def local_rmtree(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.islink(path) or os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def local_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def validate_rename_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Name cannot be empty.")
    if name in (".", ".."):
        raise ValueError("Name cannot be '.' or '..'.")
    if "/" in name or "\0" in name or (os.sep != "/" and os.sep in name):
        raise ValueError("Name cannot contain path separators.")
    if os.altsep and os.altsep in name:
        raise ValueError("Name cannot contain path separators.")
    return name


def local_rename(path: str, new_name: str) -> str:
    target = os.path.join(os.path.dirname(path), new_name)
    if os.path.exists(target):
        raise FileExistsError(f"target already exists: {target}")
    os.rename(path, target)
    return target


class TransferCancelled(Exception):
    pass


class TransferProgress:
    def __init__(self, total_bytes: int, total_files: int, draw):
        self.total_bytes = total_bytes or 1
        self.total_files = total_files
        self.done_bytes = 0
        self.done_files = 0
        self.cur_name = ""
        self.cur_size = 0
        self.cur_done = 0
        self._draw = draw
        self.cancel = False
        self._last_draw = 0.0

    def start_file(self, name: str, size: int) -> None:
        self.cur_name = name
        self.cur_size = size
        self.cur_done = 0
        if self._draw:
            self._draw()

    def chunk(self, transferred: int, total: int) -> None:
        self.cur_done = transferred
        now = time.monotonic()
        if self._draw and (transferred >= total or now - self._last_draw >= 0.1):
            self._last_draw = now
            self._draw()
        if self.cancel:
            raise TransferCancelled()

    def finish_file(self) -> None:
        self.done_bytes += self.cur_size
        self.done_files += 1
        if self._draw:
            self._draw()


def plan_push(local_paths, remote_base: str, remote: "Remote"):
    files, dirs = [], []

    def walk(lp, rp):
        if os.path.isdir(lp) and not os.path.islink(lp):
            dirs.append(rp)
            try:
                for f in sorted(os.listdir(lp)):
                    walk(os.path.join(lp, f), posix_join(rp, f))
            except OSError:
                pass
        else:
            try:
                files.append((lp, rp, os.path.getsize(lp)))
            except OSError:
                pass

    for lp in local_paths:
        name = os.path.basename(lp.rstrip("/")) or lp
        walk(lp, posix_join(remote_base, name))
    return files, dirs


@dataclass
class TransferResult:
    completed: int = 0
    failed: int = 0
    cancelled: bool = False
    errors: List[str] = field(default_factory=list)


def do_push(remote, files, dirs, progress):
    result = TransferResult()
    for d in dirs:
        try:
            remote.sftp.mkdir(d)
        except IOError:
            pass
    for lp, rp, size in files:
        if progress.cancel:
            result.cancelled = True
            break
        progress.start_file(os.path.basename(lp), size)
        if progress.cancel:
            result.cancelled = True
            break
        try:
            remote.sftp.put(lp, rp, callback=progress.chunk)
            progress.finish_file()
            result.completed += 1
        except TransferCancelled:
            result.cancelled = True
            break
        except (OSError, paramiko.SSHException) as exc:
            result.failed += 1
            result.errors.append(f"{os.path.basename(lp)}: {exc}")
    return result


def plan_pull(remote_paths, local_base: str, remote: "Remote"):
    files, dirs = [], []

    def walk(rp, lp):
        if remote.is_dir(rp):
            dirs.append(lp)
            for e in remote.listdir(rp):
                if e.name in (".", ".."):
                    continue
                walk(e.path, os.path.join(lp, e.name))
        else:
            try:
                size = remote.sftp.stat(rp).st_size or 0
            except IOError:
                size = 0
            files.append((rp, lp, size))

    for rp in remote_paths:
        name = rp.rstrip("/").rsplit("/", 1)[-1] or rp
        walk(rp, os.path.join(local_base, name))
    return files, dirs


def do_pull(remote, files, dirs, progress):
    result = TransferResult()
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    for rp, lp, size in files:
        if progress.cancel:
            result.cancelled = True
            break
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        progress.start_file(os.path.basename(rp), size)
        partial = lp + ".scp-select.part"
        if progress.cancel:
            result.cancelled = True
            break
        try:
            remote.sftp.get(rp, partial, callback=progress.chunk)
            os.replace(partial, lp)
            progress.finish_file()
            result.completed += 1
        except TransferCancelled:
            try:
                os.remove(partial)
            except OSError:
                pass
            result.cancelled = True
            break
        except (OSError, paramiko.SSHException) as exc:
            try:
                os.remove(partial)
            except OSError:
                pass
            result.failed += 1
            result.errors.append(f"{os.path.basename(rp)}: {exc}")
    return result


def transfer_conflicts(direction, remote, files) -> List[str]:
    if direction == "push":
        return [rp for _, rp, _ in files if remote.exists(rp)]
    return [lp for _, lp, _ in files if os.path.exists(lp)]


# ---------- curses helpers ----------
def init_colors():
    global _COLORS
    _COLORS = curses.has_colors()
    if not _COLORS:
        return
    curses.start_color()
    bg = -1
    try:
        curses.use_default_colors()
    except Exception:
        bg = 0
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(3, curses.COLOR_GREEN, bg)
    curses.init_pair(4, curses.COLOR_YELLOW, bg)
    curses.init_pair(5, curses.COLOR_RED, bg)
    curses.init_pair(6, curses.COLOR_CYAN, bg)
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    # Selection / cursor-row highlight: medium grey background with bold white
    # text. Uses a 256-colour extended grey (238) when the terminal supports it;
    # falls back to the basic white bar on 16-colour terminals.
    if curses.COLORS >= 256:
        curses.init_pair(8, curses.COLOR_WHITE, 238)
    else:
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_WHITE)


def safe_addstr(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    s = s[: max(0, w - x - 1)]
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def text_input(stdscr, prompt, initial="", hidden=False, suggest_fn=None):
    buf = list(initial)
    cur = len(buf)
    matches: List[str] = []
    match_idx = 0
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            safe_addstr(stdscr, 0, 0, prompt[: w - 1], cp(4) | curses.A_BOLD)
            disp = ("*" * len(buf)) if hidden else "".join(buf)
            line = disp + " " * max(1, w - 1 - len(disp))
            safe_addstr(stdscr, 1, 0, line[: w - 1], cp(2))
            if suggest_fn and not hidden:
                try:
                    matches = suggest_fn(disp)
                except Exception as e:
                    matches = []
                    safe_addstr(stdscr, 3, 0, f"(suggestions unavailable: {str(e)[: w - 40]})", cp(5))
                else:
                    if match_idx >= len(matches):
                        match_idx = 0
                    if matches:
                        max_show = max(1, h - 5)
                        for i, m in enumerate(matches[:max_show]):
                            attr = (cp(3) | curses.A_BOLD) if i == match_idx else 0
                            marker = ">" if i == match_idx else " "
                            safe_addstr(stdscr, 3 + i, 0, (marker + " " + m)[: w - 1], attr)
                        more = len(matches) - max_show
                        if more > 0:
                            safe_addstr(stdscr, 3 + max_show, 0, f"  ...+{more} more", cp(4))
                        hint = "Tab=complete  Up/Dn=pick  Enter=OK  Esc=cancel"
                    else:
                        hint = "(no matches)  Enter=OK  Esc=cancel"
            else:
                hint = "Enter=OK   Esc=Cancel"
            safe_addstr(stdscr, h - 1, 0, hint[: w - 1], cp(7) | curses.A_BOLD)
            try:
                stdscr.move(1, min(cur, w - 2))
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, curses.KEY_ENTER):
                return "".join(buf)
            if ch == 27:
                return None
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    cur -= 1
                    buf.pop(cur)
                match_idx = 0
            elif ch == 9:  # Tab - accept highlighted suggestion
                if matches:
                    buf = list(matches[match_idx])
                    cur = len(buf)
                match_idx = 0
                continue
            elif ch == curses.KEY_UP:
                if matches:
                    match_idx = (match_idx - 1) % len(matches)
                continue
            elif ch == curses.KEY_DOWN:
                if matches:
                    match_idx = (match_idx + 1) % len(matches)
                continue
            elif ch == curses.KEY_LEFT:
                cur = max(0, cur - 1)
            elif ch == curses.KEY_RIGHT:
                cur = min(len(buf), cur + 1)
            elif ch == curses.KEY_HOME:
                cur = 0
            elif ch == curses.KEY_END:
                cur = len(buf)
            elif 32 <= ch <= 126:
                buf.insert(cur, chr(ch))
                cur += 1
                match_idx = 0
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass



def password_prompt(stdscr):
    return text_input(stdscr, "Password (key/agent auth failed):", hidden=True)


def confirm_dialog(stdscr, message):
    h, w = stdscr.getmaxyx()
    bw = min(max(len(message) + 4, 18), w - 2)
    bh = 5
    by = h // 2 - bh // 2
    bx = (w - bw) // 2
    win = curses.newwin(bh, bw, by, bx)
    win.attron(cp(5) | curses.A_BOLD)
    win.border()
    win.attroff(cp(5) | curses.A_BOLD)
    safe_addstr(win, 1, 2, message[: bw - 4], cp(5))
    safe_addstr(win, 3, 2, "y = yes    n / Esc = no", cp(4))
    win.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord('y'), ord('Y')):
            return True
        if ch in (ord('n'), ord('N'), 27):
            return False


def _message(stdscr, msg):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, 0, 0, msg[: w - 1], cp(5) | curses.A_BOLD)
    safe_addstr(stdscr, 2, 0, "Press any key to continue...", cp(4))
    stdscr.refresh()
    stdscr.getch()


def draw_header(stdscr, project, status=""):
    h, w = stdscr.getmaxyx()
    line = f" scp-select  [{project.name}]  {project.host}:{project.port}"
    safe_addstr(stdscr, 0, 0, line[: w - 1], cp(1) | curses.A_BOLD)
    if status:
        safe_addstr(stdscr, 0, max(0, w - len(status) - 1), status[: w - 1], cp(4))


def draw_help_bar(stdscr, hints):
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, h - 1, 0, hints[: w - 1], cp(1) | curses.A_BOLD)


def help_overlay(stdscr):
    h, w = stdscr.getmaxyx()
    lines = [
        "scp-select  -  key bindings",
        "",
        "Tab             switch LOCAL / REMOTE pane",
        "Up/Down  k/j    move cursor",
        "Enter / Right   open dir  (or toggle a file)",
        "Left            go up one directory",
        "Space           toggle selection",
        "a               select all in current pane",
        "c               clear selections in current pane",
        "/               fuzzy-find entries by name",
        "r               refresh current pane",
        "s               cycle pane sorting",
        "d               delete current item (confirm)",
        "m               make a new directory",
        "R               rename current item",
        "p               PUSH  selected local  -> remote",
        "l               PULL  selected remote -> local",
        "b               back to project list",
        "q / Esc         quit (confirm)",
        "?               this help",
    ]
    bw = min(max(len(l) for l in lines) + 4, w - 2)
    bh = min(len(lines) + 2, h - 2)
    by = (h - bh) // 2
    bx = (w - bw) // 2
    win = curses.newwin(bh, bw, by, bx)
    win.attron(cp(6))
    win.border()
    win.attroff(cp(6))
    for i, l in enumerate(lines[: bh - 2]):
        attr = cp(3) | curses.A_BOLD if i == 0 else 0
        safe_addstr(win, i + 1, 2, l[: bw - 4], attr)
    safe_addstr(win, bh - 1, 2, "press any key to close", cp(4))
    win.refresh()
    stdscr.getch()


def run_transfer(stdscr, title, total_bytes, total_files, work_fn):
    progress = TransferProgress(total_bytes, total_files, None)

    def draw():
        ch = stdscr.getch()
        if ch == 27:
            progress.cancel = True
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        done = progress.done_bytes + progress.cur_done
        pct = 100.0 * done / progress.total_bytes
        bar_w = min(40, w - 6)
        filled = int(bar_w * done / progress.total_bytes)
        filled = max(0, min(bar_w, filled))
        bar = "#" * filled + "-" * (bar_w - filled)
        y = h // 2 - 2
        safe_addstr(stdscr, 0, 0, f" {title}", cp(1) | curses.A_BOLD)
        safe_addstr(stdscr, y, 2, f"file {progress.done_files}/{progress.total_files}", cp(4) | curses.A_BOLD)
        safe_addstr(stdscr, y + 1, 2, f"{human_size(done)} / {human_size(progress.total_bytes)}   {pct:5.1f}%", cp(3) | curses.A_BOLD)
        safe_addstr(stdscr, y + 2, 2, "[" + bar + "]", cp(6) | curses.A_BOLD)
        safe_addstr(stdscr, y + 3, 2, progress.cur_name[: w - 4], 0)
        safe_addstr(stdscr, h - 1, 0, "Transferring...  Esc = cancel", cp(5) | curses.A_BOLD)
        stdscr.refresh()

    progress._draw = draw
    progress.result = TransferResult()
    stdscr.nodelay(True)
    try:
        progress.result = work_fn(progress)
    finally:
        stdscr.nodelay(False)
        stdscr.erase()
        stdscr.refresh()
    return progress


# ---------- project list & form ----------
def project_list_screen(stdscr, cfg):
    projects = [project_from_dict(p) for p in cfg.get("projects", [])]
    cursor = 0
    scroll = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        safe_addstr(stdscr, 0, 0, " scp-select  -  choose a project", cp(1) | curses.A_BOLD)
        safe_addstr(stdscr, 1, 0, " n:new  e:edit  d:delete  Enter:open  q:quit", cp(7) | curses.A_BOLD)
        n = len(projects)
        if cursor >= n:
            cursor = max(0, n - 1)
        view_h = h - 5
        if cursor < scroll:
            scroll = cursor
        if cursor >= scroll + view_h:
            scroll = cursor - view_h + 1
        if n == 0:
            safe_addstr(stdscr, 3, 0, "No projects yet.  Press 'n' to create one.", cp(5))
        for i in range(view_h):
            idx = scroll + i
            if idx >= n:
                break
            p = projects[idx]
            sel = (idx == cursor)
            tag = ">" if sel else " "
            line = f" {tag} {p.name:<18} {(p.host or '(no host)'):<26} {p.local_dir} -> {p.remote_dir or '(none)'}"
            attr = (cp(8) | curses.A_BOLD) if sel else 0
            safe_addstr(stdscr, 3 + i, 0, line[: w - 1], attr)
        safe_addstr(stdscr, h - 1, 0, f" {n} project(s)   |   config: {CONFIG_FILE}", cp(1) | curses.A_BOLD)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            return None
        elif ch in (curses.KEY_UP, ord('k')) and n:
            cursor = (cursor - 1) % n
        elif ch in (curses.KEY_DOWN, ord('j')) and n:
            cursor = (cursor + 1) % n
        elif ch in (10, curses.KEY_ENTER) and n:
            return projects[cursor]
        elif ch == ord('n'):
            p = edit_project_form(stdscr, Project(name="new-project"))
            if p:
                projects.append(p)
                cfg["projects"] = [project_to_dict(x) for x in projects]
                save_config(cfg)
                cursor = len(projects) - 1
        elif ch == ord('e') and n:
            p = edit_project_form(stdscr, projects[cursor])
            if p:
                projects[cursor] = p
                cfg["projects"] = [project_to_dict(x) for x in projects]
                save_config(cfg)
        elif ch == ord('d') and n:
            if confirm_dialog(stdscr, f"Delete '{projects[cursor].name}'?"):
                projects.pop(cursor)
                cfg["projects"] = [project_to_dict(x) for x in projects]
                save_config(cfg)
                if cursor >= len(projects):
                    cursor = max(0, len(projects) - 1)


def _apply_field(project, key, value):
    if key == "port":
        try:
            setattr(project, key, int(value) or 22)
        except ValueError:
            pass
    elif key == "use_agent":
        setattr(project, key, value.strip().lower() in ("y", "yes", "1", "true"))
    else:
        setattr(project, key, value)


def edit_project_form(stdscr, project):
    project = replace(project)
    fields = [
        ("Name", "name"),
        ("Host (user@ip)", "host"),
        ("Port", "port"),
        ("Local directory", "local_dir"),
        ("Remote directory", "remote_dir"),
        ("Identity file (blank=none)", "identity_file"),
        ("Use SSH agent (y/n)", "use_agent"),
    ]
    cur = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        safe_addstr(stdscr, 0, 0, " Edit project", cp(1) | curses.A_BOLD)
        safe_addstr(stdscr, 1, 0, " Up/Down:move  Enter:edit field  S:save  Esc:cancel", cp(7) | curses.A_BOLD)
        for i, (label, key) in enumerate(fields):
            val = getattr(project, key)
            if isinstance(val, bool):
                val = "yes" if val else "no"
            line = f"  {label:<28}: {val}"
            attr = (cp(8) | curses.A_BOLD) if i == cur else 0
            safe_addstr(stdscr, 3 + i, 0, line[: w - 1], attr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == 27:
            return None
        elif ch in (curses.KEY_UP, ord('k')):
            cur = (cur - 1) % len(fields)
        elif ch in (curses.KEY_DOWN, ord('j')):
            cur = (cur + 1) % len(fields)
        elif ch in (10, curses.KEY_ENTER):
            label, key = fields[cur]
            current = getattr(project, key)
            if isinstance(current, bool):
                current = "yes" if current else "no"
            if key in ("local_dir", "identity_file"):
                res = text_input(stdscr, f"{label}:", str(current), suggest_fn=local_suggest)
            elif key == "remote_dir":
                if not project.host.strip():
                    res = text_input(stdscr, f"{label}:", str(current))
                else:
                    stdscr.erase()
                    h, w = stdscr.getmaxyx()
                    safe_addstr(stdscr, 0, 0, f"Connecting to {project.host} for path suggestions...", cp(4))
                    safe_addstr(stdscr, 1, 0, "(Esc cancels the password prompt; you can still type the path manually)", cp(6))
                    stdscr.refresh()
                    remote = _connect_for_suggest(stdscr, project)
                    suggest = remote_suggest(remote) if remote else None
                    try:
                        res = text_input(stdscr, f"{label}:", str(current), suggest_fn=suggest)
                    finally:
                        if remote:
                            remote.close()
            else:
                res = text_input(stdscr, f"{label}:", str(current))
            if res is not None:
                _apply_field(project, key, res)
        elif ch in (ord('s'), ord('S')):
            errors = validate_project(project)
            if not errors:
                project.local_dir = os.path.abspath(os.path.expanduser(project.local_dir))
                project.identity_file = os.path.expanduser(project.identity_file)
                return project
            _message(stdscr, errors[0])


# ---------- browser ----------
class Pane:
    def __init__(self, kind, path, remote=None):
        self.kind = kind
        self.path = path
        self.remote = remote
        self.entries = []
        self.view = []
        self.selected = set()
        self.cursor = 0
        self.scroll = 0
        self.filter = ""
        self.sort_mode = "name"
        self.load()

    def load(self):
        if self.kind == "local":
            self.entries = local_list(self.path)
        else:
            try:
                self.entries = self.remote.listdir(self.path)
            except IOError as e:
                self.entries = [Entry(name=f"(cannot list: {str(e)[:30]})", is_dir=False, size=0, path="")]
        self.apply_filter()

    def apply_filter(self):
        dotdot = [] if self.filter else [e for e in self.entries if e.name == ".."]
        ranked = []
        for entry in self.entries:
            if entry.name == "..":
                continue
            score = fuzzy_score(self.filter, entry.name)
            if score is not None:
                ranked.append((score, self._sort_key(entry), entry))
        self.view = dotdot + [entry for _, _, entry in sorted(ranked)]
        if self.cursor >= len(self.view):
            self.cursor = max(0, len(self.view) - 1)

    def _sort_key(self, entry):
        if self.sort_mode == "type":
            return (not entry.is_dir, Path(entry.name).suffix.casefold(), entry.name.casefold())
        if self.sort_mode == "size":
            return (not entry.is_dir, entry.size, entry.name.casefold())
        if self.sort_mode == "modified":
            return (not entry.is_dir, -entry.mtime, entry.name.casefold())
        return (not entry.is_dir, entry.name.casefold())

    def cycle_sort(self):
        modes = ("name", "type", "size", "modified")
        self.sort_mode = modes[(modes.index(self.sort_mode) + 1) % len(modes)]
        self.apply_filter()
        self.cursor = 0

    def current(self):
        if 0 <= self.cursor < len(self.view):
            return self.view[self.cursor]
        return None

    def toggle(self):
        e = self.current()
        if not e or e.name == "..":
            return
        if e.path in self.selected:
            self.selected.discard(e.path)
        else:
            self.selected.add(e.path)

    def replace_selected(self, old_path, new_path):
        if old_path in self.selected:
            self.selected.discard(old_path)
            self.selected.add(new_path)


def draw_pane(stdscr, pane, x, width, height, active, top_y=2):
    title = " LOCAL " if pane.kind == "local" else " REMOTE "
    attr = (cp(1) | curses.A_BOLD) if active else cp(6)
    safe_addstr(stdscr, top_y, x, title.ljust(width)[:width], attr)
    plabel = pane.path
    if pane.filter:
        plabel = f"{pane.path}  [fuzzy: {pane.filter}]"
    plabel += f"  [sort: {pane.sort_mode}]"
    safe_addstr(stdscr, top_y + 1, x, plabel[:width], cp(6))
    list_top = top_y + 2
    list_h = height - 2
    if pane.cursor < pane.scroll:
        pane.scroll = pane.cursor
    if pane.cursor >= pane.scroll + list_h:
        pane.scroll = pane.cursor - list_h + 1
    for i in range(list_h):
        idx = pane.scroll + i
        if idx >= len(pane.view):
            break
        e = pane.view[idx]
        sel = e.path in pane.selected
        cur = (idx == pane.cursor) and active
        marker = ">" if cur else " "
        check = "*" if sel else " "
        slash = "/" if e.is_dir else " "
        name = e.name + ("/" if e.is_dir else "")
        size_str = "" if e.is_dir else human_size(e.size)
        text = f"{marker}{check} {slash}{name}"
        room = width - len(text) - 1
        if room > len(size_str):
            text = text + " " * (room - len(size_str)) + size_str
        a = 0
        if e.is_dir:
            a |= curses.A_BOLD
        if sel:
            a |= cp(3)
        if cur:
            a = cp(8) | curses.A_BOLD
        safe_addstr(stdscr, list_top + i, x, text[:width], a)
    cnt = len(pane.selected)
    if cnt:
        safe_addstr(stdscr, top_y + 1, x + width - 14, f"{cnt} selected", cp(3))


def _do_transfer(stdscr, project, remote, local_pane, remote_pane, direction):
    if direction == "push":
        src = local_pane
        if not src.selected:
            return "Nothing selected on LOCAL. (Space to select)"
        files, dirs = plan_push(list(src.selected), remote_pane.path, remote)
        total = sum(s for _, _, s in files)
        title = f"PUSH  ->  {project.host}:{remote_pane.path}"

        def work(p):
            do_push(remote, files, dirs, p)
    else:
        src = remote_pane
        if not src.selected:
            return "Nothing selected on REMOTE. (Space to select)"
        files, dirs = plan_pull(list(src.selected), local_pane.path, remote)
        total = sum(s for _, _, s in files)
        title = f"PULL  ->  {local_pane.path}"

        def work(p):
            do_pull(remote, files, dirs, p)
    if not files and not dirs:
        return "No readable files or directories in the selection."
    conflicts = transfer_conflicts(direction, remote, files)
    verb = "Push" if direction == "push" else "Pull"
    prompt = f"{verb} {len(files)} file(s), {human_size(total)}"
    if conflicts:
        prompt += f", overwrite {len(conflicts)}"
    if not confirm_dialog(stdscr, prompt + "?"):
        return "Transfer cancelled before starting."
    prog = run_transfer(stdscr, title, total, len(files), work)
    result = prog.result
    if not result.cancelled and not result.failed:
        src.selected.clear()
    if result.cancelled:
        return f"Cancelled: {result.completed} transferred, {result.failed} failed."
    if result.failed:
        detail = result.errors[0] if result.errors else "unknown error"
        return f"Partial: {result.completed} transferred, {result.failed} failed ({detail})"
    return f"Done: {result.completed} file(s) transferred."


def live_fuzzy_filter(stdscr, pane, draw_browser):
    original = pane.filter
    buf = list(original)
    while True:
        pane.filter = "".join(buf)
        pane.apply_filter()
        pane.cursor = 0
        pane.scroll = 0
        query = pane.filter or "(type to search; Ctrl-U clears)"
        draw_browser(f" Fuzzy: {query}   Enter:keep  Esc:cancel  Backspace:edit")
        ch = stdscr.getch()
        if ch in (10, curses.KEY_ENTER):
            return True
        if ch == 27:
            pane.filter = original
            pane.apply_filter()
            pane.cursor = 0
            pane.scroll = 0
            return False
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif ch == 21:  # Ctrl-U
            buf.clear()
        elif ch == curses.KEY_RESIZE:
            continue
        elif 32 <= ch <= 126:
            buf.append(chr(ch))


def _focus_path(pane, path):
    for index, entry in enumerate(pane.view):
        if entry.path == path:
            pane.cursor = index
            pane.scroll = 0
            return


def _rename_current_item(stdscr, pane, remote):
    e = pane.current()
    if not e or e.name == "..":
        return "No item to rename."
    new_name = text_input(stdscr, f"Rename {pane.kind.upper()} '{e.name}' to:", e.name)
    if new_name is None:
        return "Rename cancelled."
    try:
        new_name = validate_rename_name(new_name)
        if new_name == e.name:
            return "Rename unchanged."
        if pane.kind == "local":
            new_path = local_rename(e.path, new_name)
        else:
            new_path = remote.rename(e.path, new_name)
        pane.replace_selected(e.path, new_path)
        pane.load()
        _focus_path(pane, new_path)
        return f"Renamed {e.name} to {new_name}"
    except Exception as ex:
        return f"Rename failed: {ex}"


def browser_screen(stdscr, project, remote):
    local_pane = Pane("local", project.local_dir)
    remote_pane = Pane("remote", project.remote_dir or "/", remote=remote)
    panes = {"local": local_pane, "remote": remote_pane}
    active = "local"
    status = "Connected.  Press ? for help."
    default_help = " Tab:switch Space:sel p:push l:pull /:fuzzy s:sort r:refresh d:del m:mkdir R:rename ?:help b:back q:quit"

    def draw_browser(help_text=None):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 10 or w < 60:
            safe_addstr(stdscr, 0, 0, "Terminal too small. Resize to at least 60x10.",
                        cp(5) | curses.A_BOLD)
            stdscr.refresh()
            return
        draw_header(stdscr, project, status)
        half = max(20, w // 2)
        pane_h = h - 4
        draw_pane(stdscr, local_pane, 0, half, pane_h, active == "local")
        draw_pane(stdscr, remote_pane, half, w - half, pane_h, active == "remote")
        draw_help_bar(stdscr, help_text or default_help)
        stdscr.refresh()

    while True:
        draw_browser()
        pane = panes[active]
        ch = stdscr.getch()
        if ch in (ord('q'), 27):
            if confirm_dialog(stdscr, "Quit scp-select?"):
                return "quit"
            continue
        if ch == ord('b'):
            return "back"
        if ch == ord('?'):
            help_overlay(stdscr)
            continue
        if ch == 9:
            active = "remote" if active == "local" else "local"
            continue
        if ch == ord('p'):
            status = _do_transfer(stdscr, project, remote, local_pane, remote_pane, "push")
            local_pane.load()
            remote_pane.load()
            continue
        if ch == ord('l'):
            status = _do_transfer(stdscr, project, remote, local_pane, remote_pane, "pull")
            local_pane.load()
            remote_pane.load()
            continue
        if ch in (curses.KEY_UP, ord('k')):
            pane.cursor = max(0, pane.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord('j')):
            pane.cursor = min(len(pane.view) - 1, pane.cursor + 1)
        elif ch in (10, curses.KEY_ENTER, curses.KEY_RIGHT):
            e = pane.current()
            if e:
                if e.is_dir:
                    pane.path = e.path
                    pane.cursor = 0
                    pane.scroll = 0
                    pane.filter = ""
                    pane.load()
                else:
                    pane.toggle()
        elif ch == curses.KEY_LEFT:
            if pane.path.rstrip("/") != "":
                pane.path = os.path.dirname(pane.path.rstrip("/")) or "/"
                pane.cursor = 0
                pane.scroll = 0
                pane.filter = ""
                pane.load()
        elif ch == ord(' '):
            pane.toggle()
        elif ch == ord('a'):
            for e in pane.view:
                if e.name != "..":
                    pane.selected.add(e.path)
        elif ch == ord('c'):
            pane.selected.clear()
        elif ch == ord('r'):
            pane.load()
        elif ch == ord('s'):
            pane.cycle_sort()
            status = f"{pane.kind.upper()} sorted by {pane.sort_mode}."
        elif ch == ord('d'):
            e = pane.current()
            if e and e.name != "..":
                if confirm_dialog(stdscr, f"Delete {'dir' if e.is_dir else 'file'} '{e.name}' on {pane.kind.upper()}?"):
                    try:
                        if pane.kind == "local":
                            local_rmtree(e.path)
                        else:
                            if e.is_dir:
                                remote.rmtree(e.path)
                            else:
                                remote.sftp.remove(e.path)
                        pane.load()
                        status = f"Deleted {e.name}"
                    except Exception as ex:
                        status = f"Delete failed: {ex}"
        elif ch == ord('m'):
            name = text_input(stdscr, f"New directory in {pane.kind.upper()}:")
            if name:
                try:
                    if pane.kind == "local":
                        local_mkdir(os.path.join(pane.path, name))
                    else:
                        remote.mkdir(posix_join(pane.path, name))
                    pane.load()
                    status = f"Created {name}"
                except Exception as ex:
                    status = f"mkdir failed: {ex}"
        elif ch == ord('R'):
            status = _rename_current_item(stdscr, pane, remote)
        elif ch == ord('/'):
            accepted = live_fuzzy_filter(stdscr, pane, draw_browser)
            status = (
                f"{pane.kind.upper()} fuzzy filter: {pane.filter or 'cleared'}"
                if accepted else "Fuzzy filter unchanged."
            )


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()
    cfg = load_config()
    if CONFIG_WARNING:
        _message(stdscr, CONFIG_WARNING)
    while True:
        project = project_list_screen(stdscr, cfg)
        if project is None:
            break
        remote = Remote(project)
        pwd = None
        connected = False
        attempts = 0
        while not connected and attempts < 3:
            try:
                remote.connect(password=pwd)
                connected = True
            except paramiko.BadHostKeyException as ex:
                _message(stdscr, f"Host key changed for {ex.hostname}. Connection blocked.")
                break
            except (paramiko.AuthenticationException, paramiko.SSHException):
                attempts += 1
                if attempts >= 3:
                    _message(stdscr, "Authentication failed (3 attempts).")
                    break
                pwd = password_prompt(stdscr)
                if pwd is None:
                    break
            except Exception as ex:
                _message(stdscr, f"Connection failed: {ex}")
                break
        if not connected:
            continue
        try:
            result = browser_screen(stdscr, project, remote)
        finally:
            remote.close()
        if result == "quit":
            break


WRAPPER_DIR = Path.home() / ".local" / "bin"


def _wrapper_path(alias: str) -> Path:
    return WRAPPER_DIR / alias


def _write_wrapper(alias: str, script_path: str) -> Path:
    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    wp = _wrapper_path(alias)
    content = (
        "#!/bin/sh\n"
        f"# {alias} - launcher for scp-select (auto-generated by 'scp-select --alias')\n"
        f'exec python3 "{script_path}" "$@"\n'
    )
    wp.write_text(content)
    try:
        wp.chmod(0o755)
    except OSError:
        pass
    return wp


def validate_alias(alias: str) -> str:
    alias = (alias or "scp-select").strip() or "scp-select"
    if alias in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias):
        raise ValueError("alias must contain only letters, numbers, '.', '_' or '-'")
    return alias


def install_command(alias: str = "scp-select"):
    """Install/refresh the global launcher wrapper for the given alias.
    Removes the previously-installed wrapper if the alias changed. Returns
    (wrapper_path, on_path_bool)."""
    alias = validate_alias(alias)
    script_path = os.path.realpath(__file__)
    cfg = load_config()
    old_alias = cfg.get("command_alias")
    if old_alias and old_alias != alias:
        old_wp = _wrapper_path(old_alias)
        try:
            if old_wp.exists() and "scp-select" in old_wp.read_text():
                old_wp.unlink()
        except OSError:
            pass
    wp = _write_wrapper(alias, script_path)
    cfg["command_alias"] = alias
    cfg["script_path"] = script_path
    save_config(cfg)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    on_path = str(WRAPPER_DIR) in path_dirs
    return wp, on_path


def _print_path_hint():
    print(f"  NOTE: {WRAPPER_DIR} is not on your PATH.")
    print("  Add this line to your shell rc (~/.bashrc or ~/.zshrc) and open a new terminal:")
    print(f'    export PATH="{WRAPPER_DIR}:$PATH"')


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scp-select",
        description="Browse and transfer files over SFTP in a two-pane terminal UI.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="list saved projects and exit")
    actions.add_argument("--install", nargs="?", const="scp-select", metavar="ALIAS",
                         help="install a launcher, optionally using ALIAS")
    actions.add_argument("--alias", nargs="?", const="scp-select", metavar="NAME",
                         help="set or change the launcher alias")
    actions.add_argument("--uninstall", action="store_true", help="remove the installed launcher")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def cli(argv=None):
    args = build_parser().parse_args(argv)
    if args.list:
        cfg = load_config()
        if CONFIG_WARNING:
            print(f"Warning: {CONFIG_WARNING}", file=sys.stderr)
        projects = cfg.get("projects", [])
        if not projects:
            print("No saved projects.")
            return 0
        print(f"{len(projects)} project(s) in {CONFIG_FILE}:")
        for project_data in projects:
            project = project_from_dict(project_data)
            print(f"  {project.name:<20} {project.host}:{project.port}  "
                  f"{project.local_dir} -> {project.remote_dir or '(none)'}")
        current = cfg.get("command_alias")
        if current:
            print(f"  active command alias: {current}  ->  {_wrapper_path(current)}")
        return 0

    requested_alias = args.install or args.alias
    if requested_alias:
        try:
            wp, on_path = install_command(requested_alias)
        except (OSError, ValueError) as exc:
            print(f"Could not install command: {exc}", file=sys.stderr)
            return 2
        action = "Installed command" if args.install else "Command alias is now"
        print(f"{action} '{requested_alias}':")
        print(f"  wrapper: {wp}")
        if not on_path:
            _print_path_hint()
        print(f"  Run '{requested_alias}' to launch.")
        return 0

    if args.uninstall:
        cfg = load_config()
        alias = cfg.get("command_alias", "scp-select")
        wp = _wrapper_path(alias)
        try:
            if wp.exists():
                wp.unlink()
                print(f"Removed '{wp}'.")
        except OSError as exc:
            print(f"Could not remove '{wp}': {exc}", file=sys.stderr)
            return 1
        cfg.pop("command_alias", None)
        cfg.pop("script_path", None)
        save_config(cfg)
        return 0

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    cli()
