#!/usr/bin/env bash
# Installs or updates Prism for Dolphin. Run it from the repo after any change:
#   ./install-dolphin.sh
# or on its own, which fetches Prism first (PRISM_REF: a tag or branch):
#   curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-dolphin.sh | bash
# It copies the code to ~/.local/share/prism, sets up the PDF libraries the
# first time, and adds Dolphin's right-click menus: the submenus Prism Convert,
# Compress, Rescale and Edit, and the items Prism Remove Background and Prism
# Clean. No ring, keybinding or Hyprland change. Safe to re-run. For Nautilus,
# run install-nautilus.sh (both can be installed).
set -euo pipefail
# Run on its own (curl | bash, or a downloaded copy): fetch Prism into a temp
# folder that is always removed, and run this installer from there. Defined
# first and called on one line, so a download cut short mid-pipe runs nothing.
prism_fetch() {
    local repo="${PRISM_REPO:-https://github.com/parasite6/prism.git}" ref="${PRISM_REF:-sanctuary}"
    [[ $EUID -ne 0 ]] || { echo "Run this as yourself, not root (no sudo): Prism installs into your home folder." >&2; exit 1; }
    command -v git >/dev/null || { echo "The installer needs git: sudo dnf install git" >&2; exit 1; }
    PRISM_WORK=$(mktemp -d "${TMPDIR:-/tmp}/prism-install.XXXXXX")
    trap 'rm -rf -- "$PRISM_WORK"' EXIT
    trap 'exit 130' INT TERM
    echo ":: Fetching Prism ($ref)..."
    git clone --quiet --depth 1 --branch "$ref" -- "$repo" "$PRISM_WORK/prism" ||
        { echo "Couldn't download Prism from $repo (ref: $ref). Check your connection and try again." >&2; exit 1; }
    [[ -f "$PRISM_WORK/prism/$1" ]] || { echo "Ref '$ref' has no $1: it isn't a Prism release with it." >&2; exit 1; }
    # stdin: under curl | bash it's the rest of this script (questions use /dev/tty).
    bash "$PRISM_WORK/prism/$1" </dev/null || { echo "Installation failed (see above)." >&2; exit 1; }
}
SELF="${BASH_SOURCE[0]:-}"
[[ -n "$SELF" && -f "$(dirname "$(readlink -f "$SELF")")/install-common.sh" ]] || { prism_fetch "install-dolphin.sh"; exit 0; }

. "$(dirname "$(readlink -f "$SELF")")/install-common.sh"
prism_deps dolphin
prism_copy "$SELF"
prism_pdf
prism_model

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
