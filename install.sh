#!/bin/sh
# scp-select installer - run this after extracting the tarball to auto-configure
# the global command. It creates a launcher wrapper in ~/.local/bin and offers
# to add that dir to your PATH if needed.
#
#   tar xzf scp-select-1.0.0.tar.gz
#   cd scp-select-1.0.0
#   ./install.sh
#
# You can also skip this script and run the same logic directly:
#   python3 scp_select.py --install [alias]
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/scp_select.py"

if [ ! -f "$PY" ]; then
    echo "scp_select.py not found next to install.sh (expected: $PY)" >&2
    exit 1
fi

DEFAULT="scp-select"
printf "Command name (alias) to install [default: %s]: " "$DEFAULT"
read ALIAS
ALIAS="${ALIAS:-$DEFAULT}"

python3 "$PY" --install "$ALIAS"

# Offer to put ~/.local/bin on PATH if it isn't already.
on_path=0
case ":$PATH:" in
    *":$HOME/.local/bin:"*) on_path=1 ;;
esac

if [ "$on_path" = "0" ]; then
    RC_LINE='export PATH="$HOME/.local/bin:$PATH"'
    SHELL_RC=""
    if [ -n "$ZSH_VERSION" ] || [ -f "$HOME/.zshrc" ] && [ ! -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -n "$BASH_VERSION" ] || [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    fi
    if [ -n "$SHELL_RC" ]; then
        printf "Add '%s' to %s now? [Y/n] " "$RC_LINE" "$SHELL_RC"
        read ans
        ans="${ans:-Y}"
        case "$ans" in
            [Yy]*)
                printf '\n%s\n' "$RC_LINE" >> "$SHELL_RC"
                echo "Added to $SHELL_RC."
                echo "Run:  source $SHELL_RC   (or just open a new terminal)."
                ;;
            *)
                echo "Skipped. Add this line to your shell rc manually:"
                echo "  $RC_LINE"
                ;;
        esac
    else
        echo "Add this line to your shell rc:"
        echo "  $RC_LINE"
    fi
fi

echo
echo "Done. Run '$ALIAS' from any directory to launch scp-select."
echo "Change the alias later with:  $ALIAS --alias newname"
echo "Remove it with:              $ALIAS --uninstall"
