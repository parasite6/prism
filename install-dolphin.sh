#!/usr/bin/env bash
# Installs or updates Prism for Dolphin. Run it from the repo after any change:
#   ./install-dolphin.sh
# It copies the code to ~/.local/share/prism, sets up the PDF libraries the
# first time, and adds Dolphin's right-click menus: the submenus Prism Convert,
# Compress, Rescale and Edit, and the items Prism Remove Background and Prism
# Clean. No ring, keybinding or Hyprland change. Safe to re-run. For Nautilus,
# run install-nautilus.sh (both can be installed).
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/install-common.sh"
prism_copy "$0"
prism_pdf

# The menus are generated from convert.py's tables by menu.py. Our generated
# files all start with dev.prism.; regenerate them so removed targets disappear too.
MENUS="${XDG_DATA_HOME:-$HOME/.local/share}/kio/servicemenus"
mkdir -p "$MENUS"
rm -f "$MENUS"/dev.prism.Prism.desktop "$MENUS"/dev.prism.menu-*.desktop
/usr/bin/python3 "$DIR/menu.py" --dolphin-menus "$MENUS" >/dev/null
echo "Installed. In Dolphin: right-click > Prism Convert / Compress / Rescale / Edit / Remove Background / Clean."

# If the Nautilus ring is also installed and running, it's still on the old
# code: restart it.
# ring-start waits until the ring answers. Retried once: right after a Hyprland
# reload, the reload's own ring-start may still hold the start lock.
if pgrep -f '/(convert-ring|prism)/ring.py --daemon$' >/dev/null; then
    prism_restart_ring
    if "$DIR/ring-start" >/dev/null 2>&1 || { sleep 1; "$DIR/ring-start" >/dev/null 2>&1; }; then
        echo "Restarted the Nautilus ring on the new code."
    else
        echo "The Nautilus ring didn't restart; it starts on the next Hyprland reload or login." >&2
    fi
fi
command -v dolphin >/dev/null || echo "Dolphin isn't installed yet: the menus appear once it is." >&2
