#!/usr/bin/env bash
# Links Prism into Nautilus (Ctrl+Alt+C, Scripts > Prism) and Hyprland (hold Forward).
# Safe to re-run; also takes over from an install under the old name (Convert Ring).
set -euo pipefail
DIR="$(dirname "$(readlink -f "$0")")"
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
# Restart the resident ring so it runs from this location.
# Make sure the old one is really gone (a stuck ring once survived this silently).
for pid in $(pgrep -f '/(convert-ring|prism)/ring.py --daemon$'); do
    kill "$pid" 2>/dev/null
    for _ in $(seq 20); do [ -e "/proc/$pid" ] || break; sleep 0.05; done
    [ -e "/proc/$pid" ] && kill -KILL "$pid" 2>/dev/null
done
rm -f "${XDG_RUNTIME_DIR:-/tmp}/convert-ring.pid" "${XDG_RUNTIME_DIR:-/tmp}/prism.pid"
hyprctl reload >/dev/null && echo "Hyprland reloaded: hold Forward on a selected file in Nautilus."
if [[ ! -x "$DIR/.pdf-venv/bin/python" ]]; then
    echo "PDF conversions need their Python environment once: $DIR/setup-pdf.sh"
fi
