# scp-select

An interactive **TUI** for selecting files to push to / pull from a server over SSH,
with **saved projects** that map a local directory to a remote host + directory so you
never have to retype paths.

Instead of:

```
scp -P 2222 ./src/main.py ./src/util.py user@myserver:/var/www/app/src/
```

you browse both sides, press `Space` to tick the files you want, and hit `p` (push) or
`l` (pull). Directories transfer recursively, with a live progress bar. You can also rename items directly in either pane.

## Requirements

- Python 3.8+
- `paramiko`  (`pip install -r requirements.txt`)
- The `scp`/`ssh` stack is *not* required — transfers use SFTP via paramiko.

## Install

**Option 1 — from the release tarball (recommended for sharing):**

```bash
tar xzf scp-select-1.0.0.tar.gz
cd scp-select-1.0.0
pip install -r requirements.txt   # installs paramiko
./install.sh
```

`install.sh` is interactive: it asks for the **command name** (default `scp-select`),
creates a launcher in `~/.local/bin/`, and offers to add that dir to your `PATH` if it
isn't already. After that you can run `scp-select` from any directory.

**Option 2 — from the git repo, run from source:**

```bash
cd scp-copy
pip install -r requirements.txt
python3 scp_select.py --install        # auto-configure the global command
```

**Option 3 — install as a Python package:**

```bash
cd scp-copy
pip install .                          # provides the `scp-select` command
```

### Changing the command alias

You can pick any name for the command (or change it later) without reinstalling:

```bash
scp-select --alias scpp          # the command is now 'scpp'
scpp                             # launch the TUI
scp-select --uninstall           # remove the launcher wrapper
```

The chosen alias is remembered in `~/.config/scp-select/projects.json`, and switching
aliases automatically removes the old wrapper.

## Run

```bash
./scp_select.py            # launch the TUI
./scp_select.py --list     # print saved projects and exit (non-interactive)
```

### First run

1. On the project list screen press **`n`** to create a project.
2. Fill in: `Name`, `Host` (`user@ip`), `Port`, `Local directory`,
   `Remote directory`, optional `Identity file`, and `Use SSH agent`.
3. Press **`S`** to save, then **Enter** to open it.

**Path suggestions / autocomplete.** When editing the `Local directory` or
`Identity file` fields you get live completions from the local filesystem. When
editing the `Remote directory` field **and a `Host` is set**, scp-select opens
an SSH connection (using your key/agent, or a masked password prompt if those
fail) and suggests remote directories as you type. Inside the path input:

| Key | Action |
|-----|--------|
| `Tab` | Accept the highlighted suggestion (directories keep a trailing `/` so you can drill in) |
| `Up` / `Down` | Move the highlight between suggestions |
| type | Narrow the list by prefix |
| `Enter` | Confirm the typed/accepted value |
| `Esc` | Cancel (for the password prompt this falls back to manual path entry) |

If the connection can't be established, the field falls back to plain manual
entry — no crash, you just type the path yourself.

Projects are stored in `~/.config/scp-select/projects.json`.

## Key bindings

### Project list

| Key | Action |
|-----|--------|
| `Up`/`Down` or `k`/`j` | Move selection |
| `Enter`              | Open project |
| `n`                  | New project |
| `e`                  | Edit project |
| `d`                  | Delete project |
| `q` / `Esc`          | Quit |

### Browser (two-pane: LOCAL | REMOTE)

| Key | Action |
|-----|--------|
| `Tab`               | Switch between LOCAL and REMOTE pane |
| `Up`/`Down` `k`/`j` | Move cursor |
| `Enter` / `Right`   | Open directory / toggle a file |
| `Space`             | Toggle selection on current item |
| `a`                 | Select all in current pane |
| `c`                 | Clear selections in current pane |
| `/`                 | Fuzzy-find entries by name |
| `s`                 | Cycle sorting: name, type, size, modified |
| `r`                 | Refresh current pane |
| `d`                 | Delete current item (with confirm) |
| `m`                 | Make a new directory in current pane |
| `R`                 | Rename current item in current pane |
| `p`                 | **Push** selected LOCAL items -> REMOTE |
| `l`                 | **Pull** selected REMOTE items -> LOCAL |
| `b`                 | Back to project list |
| `q` / `Esc`         | Quit (with confirm) |

Push copies the selected local items into the *current remote directory*; pull copies
the selected remote items into the *current local directory*. Before each transfer,
scp-select shows the file count and total size and asks once before overwriting conflicts.

Fuzzy finding updates the active pane live as you type and uses case-insensitive
subsequence matching, so a query such as `cfg` can match `project_config.yml`.
Press Enter to keep the query, Esc to restore the previous query, or Ctrl-U to clear it.
Exact substrings rank ahead of looser matches. Sorting is independent for each pane and
directories remain grouped before files.

## Notes / limitations

- New host keys are trusted on first use and stored in
  `~/.config/scp-select/known_hosts`. A changed key is blocked.
- Passwords are never stored; you are prompted (masked) only if key/agent auth fails.
- Transfers are sequential. Conflicting destination files require batch confirmation.
- Failed downloads use a temporary partial file and do not replace a valid destination.
