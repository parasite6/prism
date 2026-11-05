#!/usr/bin/env bash
# Installs or updates Prism for Nautilus. Run it from the repo after any change:
#   ./install-nautilus.sh
# or on its own, which fetches Prism first (PRISM_REF: a tag or branch):
#   curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-nautilus.sh | bash
# It copies the code to ~/.local/share/prism, sets up the PDF libraries the
# first time, links Prism into Nautilus (Ctrl+Alt+C, Scripts > Prism) and
# Hyprland (hold Forward), adds "Prism: Set Shortcut" to the app launcher, and
# restarts the ring. Safe to re-run; also takes over from an install under the
# old name (Convert Ring). For Dolphin, run install-dolphin.sh (both can be installed).
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
[[ -n "$SELF" && -f "$(dirname "$(readlink -f "$SELF")")/install-common.sh" ]] || { prism_fetch "install-nautilus.sh"; exit 0; }

. "$(dirname "$(readlink -f "$SELF")")/install-common.sh"
prism_deps nautilus
prism_copy "$SELF"
prism_pdf
prism_model

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
prism_restart_ring  # Hyprland's reload starts it again (hypr-binds.lua)
hyprctl reload >/dev/null && echo "Hyprland reloaded: hold Forward on a selected file in Nautilus."
if [[ -d "${XDG_DATA_HOME:-$HOME/.local/share}/convert-ring" ]]; then
    echo "The old Convert Ring folder is no longer used: gio trash ${XDG_DATA_HOME:-$HOME/.local/share}/convert-ring"
fi
