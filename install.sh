#!/usr/bin/env bash
# Installs or updates Prism in one step. Run it from the repo after any change:
#   ./install.sh
# It copies the code to ~/.local/share/prism (so half-finished edits in the repo
# never reach the running ring), sets up the PDF libraries the first time, links
# Prism into Nautilus (Ctrl+Alt+C, Scripts > Prism) and Hyprland (hold Forward),
# and restarts the ring. Safe to re-run; also takes over from an install under
# the old name (Convert Ring).
set -euo pipefail
SRC="$(dirname "$(readlink -f "$0")")"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/prism"
if [[ "$SRC" != "$DIR" ]]; then
    command -v rsync >/dev/null || { echo "install.sh needs rsync (sudo dnf install rsync)" >&2; exit 1; }
    if [[ -d "$DIR" && ! -e "$DIR/ring.py" && -n "$(ls -A "$DIR")" ]]; then
        echo "Refusing to overwrite $DIR: it exists and isn't a Prism install" >&2
        exit 1
    fi
    mkdir -p "$DIR"
    # --delete drops files removed from the repo; excluded paths (the PDF
    # environment) are never deleted.
    rsync -a --delete --exclude /.git/ --exclude /.github/ --exclude /.pdf-venv/ \
        --exclude __pycache__/ --exclude '*.pyc' "$SRC/" "$DIR/"
    echo "Copied $SRC to $DIR"
    exec "$DIR/install.sh" "$@"
fi
if [[ ! -x "$DIR/.pdf-venv/bin/python" && -z "${PRISM_SKIP_PDF:-}" ]]; then
    echo "Setting up the PDF libraries (once; downloads ~450 MB from PyPI)..."
    "$DIR/setup-pdf.sh" || echo "PDF setup failed; everything but PDF conversions works. Retry: $DIR/setup-pdf.sh" >&2
fi
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nautilus"
mkdir -p "$DATA_DIR/scripts" "$CONFIG_DIR"
if [[ -e "$DATA_DIR/scripts/Prism" && ! -L "$DATA_DIR/scripts/Prism" ]]; then
    echo "Refusing to replace an existing Prism script: $DATA_DIR/scripts/Prism" >&2
    exit 1
fi
# The old name's Nautilus script, only if it is our symlink.
old="$DATA_DIR/scripts/Convert"
if [[ -L "$old" && "$(readlink "$old")" == */convert-ring/convert-ring ]]; then
    rm -f "$old"
fi
ln -sfn "$DIR/prism" "$DATA_DIR/scripts/Prism"
touch "$CONFIG_DIR/scripts-accels"
sed -i '/^<Control><Alt>c Convert$/d' "$CONFIG_DIR/scripts-accels"
grep -Fqx '<Control><Alt>c Prism' "$CONFIG_DIR/scripts-accels" || echo '<Control><Alt>c Prism' >> "$CONFIG_DIR/scripts-accels"
nautilus -q 2>/dev/null || true  # restarts on next open
# "Prism: Set Shortcut" in the app launcher (also: prism-bind).
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/dev.prism.Bind.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Prism: Set Shortcut
Comment=Choose the key or mouse button you hold to open Prism's ring
Exec="$DIR/prism-bind"
Icon=input-mouse
Terminal=false
Categories=Settings;Utility;
DESKTOP
echo "Installed. In Nautilus: select files, press Ctrl+Alt+C (or right-click > Scripts > Prism)."

# Hold Forward to open the ring, via Hyprland's custom.lua hook.
HYPR_CUSTOM="$HOME/.config/hypr/custom.lua"
LINE="dofile(\"$DIR/hypr-binds.lua\")"
mkdir -p "$(dirname "$HYPR_CUSTOM")"
touch "$HYPR_CUSTOM"
# Replace any entry from an earlier location or the old name.
sed -i -E '\#^dofile\(".*/(convert-ring|prism)/hypr-binds\.lua"\)$#d' "$HYPR_CUSTOM"
echo "$LINE" >> "$HYPR_CUSTOM"
# Restart the resident ring so it runs from this location. On SIGTERM it
# finishes running conversions first (up to 2 minutes): wait for that rather
# than cutting them off, but make sure it is really gone in the end (a stuck
# ring once survived a restart silently).
for pid in $(pgrep -f '/(convert-ring|prism)/ring.py --daemon$'); do
    kill "$pid" 2>/dev/null
    for i in $(seq 1300); do  # 130 s
        [ -e "/proc/$pid" ] || break
        [ "$i" = 10 ] && echo "Waiting for running conversions to finish (up to 2 minutes)..."
        sleep 0.1
    done
    [ -e "/proc/$pid" ] && kill -KILL "$pid" 2>/dev/null && echo "The old ring didn't stop; it was killed." >&2
done
rm -f "${XDG_RUNTIME_DIR:-/tmp}/convert-ring.pid" "${XDG_RUNTIME_DIR:-/tmp}/prism.pid"
hyprctl reload >/dev/null && echo "Hyprland reloaded: hold Forward on a selected file in Nautilus."
if [[ -d "${XDG_DATA_HOME:-$HOME/.local/share}/convert-ring" ]]; then
    echo "The old Convert Ring folder is no longer used: gio trash ${XDG_DATA_HOME:-$HOME/.local/share}/convert-ring"
fi
