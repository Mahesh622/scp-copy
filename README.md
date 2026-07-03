# scp-select

An interactive **TUI** for selecting files to push to / pull from a server over SSH,
with **saved projects** that map a local directory to a remote host + directory so you
never have to retype paths.

Instead of:

```
scp -P 2222 ./src/main.py ./src/util.py user@myserver:/var/www/app/src/
```

you browse both sides, press `Space` to tick the files you want, and hit `p` (push) or
`l` (pull). Directories transfer recursively, with a live progress bar.

## Requirements

- Python 3.8+
- `paramiko`  (`pip install -r requirements.txt`)
- The `scp`/`ssh` stack is *not* required — transfers use SFTP via paramiko.

## Install

Option A — run from source:

```bash
cd scp-copy
pip install -r requirements.txt
chmod +x scp_select.py
# optional: put it on your PATH
ln -s "$PWD/scp_select.py" ~/.local/bin/scp-select
```

Option B — install as a package (provides the `scp-select` command):

```bash
cd scp-copy
pip install .
scp-select            # launch the TUI
```

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
| `/`                 | Filter entries by name |
| `r`                 | Refresh current pane |
| `d`                 | Delete current item (with confirm) |
| `m`                 | Make a new directory in current pane |
| `p`                 | **Push** selected LOCAL items -> REMOTE |
| `l`                 | **Pull** selected REMOTE items -> LOCAL |
| `b`                 | Back to project list |
| `q` / `Esc`         | Quit (with confirm) |

Push copies the selected local items into the *current remote directory*; pull copies
the selected remote items into the *current local directory*. Existing files are
overwritten (standard scp behaviour).

## Notes / limitations

- Host keys are auto-accepted (like `ssh -o StrictHostKeyChecking=accept-new`). Tighten
  this in `Remote.connect` if you prefer strict checking.
- Passwords are never stored; you are prompted (masked) only if key/agent auth fails.
- Transfers are sequential and overwrite on conflict.
