#!/usr/bin/env bash
# Installs or updates Prism for Strata. Run it from the repo after any change:
#   ./install-strata.sh
# or on its own, which fetches Prism first (PRISM_REF: a tag or branch):
#   curl -fsSL https://github.com/parasite6/prism/releases/latest/download/install-strata.sh | bash
# It copies the code to ~/.local/share/prism, sets up the PDF libraries the
# first time, and adds Strata custom actions: right-click > Actions > Convert
# to …, Compress, Rescale, Crop, Rotate, Remove background and Clean metadata,
# each shown only on files it applies to. No ring, keybinding or Hyprland
# change. Safe to re-run. Can be installed alongside the Nautilus and Dolphin parts.
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
[[ -n "$SELF" && -f "$(dirname "$(readlink -f "$SELF")")/install-common.sh" ]] || { prism_fetch "install-strata.sh"; exit 0; }

. "$(dirname "$(readlink -f "$SELF")")/install-common.sh"
prism_deps strata
prism_copy "$SELF"
prism_pdf
prism_model

# The actions are generated from convert.py's tables by menu.py, one folder per
# target. Our folders all start with dev.prism.; regenerate them so removed
# targets disappear too. Strata refuses a symlinked actions folder, so make a real one.
ACTIONS="${XDG_CONFIG_HOME:-$HOME/.config}/strata/actions"
if [[ -L "$ACTIONS" ]]; then
    echo "Refusing to write into $ACTIONS: it's a symlink, which Strata won't load." >&2
    exit 1
fi
mkdir -p -m 700 "$ACTIONS"
# Actions turned off in Settings > Actions stay off after an update.
disabled=()
for old in "$ACTIONS"/dev.prism.*/; do
    [[ -d "$old" && ! -L "${old%/}" ]] || continue
    grep -qx 'enabled = false' "$old/action.toml" 2>/dev/null && disabled+=("$(basename "$old")")
    rm -f -- "$old/action.toml" "$old/action.toml.tmp"
    rmdir -- "$old" 2>/dev/null || echo "Left $old in place: it holds files Prism didn't write." >&2
done
/usr/bin/python3 "$DIR/menu.py" --strata-actions "$ACTIONS" >/dev/null
for id in "${disabled[@]}"; do
    [[ -f "$ACTIONS/$id/action.toml" ]] && sed -i 's/^enabled = true$/enabled = false/' "$ACTIONS/$id/action.toml"
done
echo "Installed. In Strata: right-click a file > Actions > Convert to … / Compress / Rescale / Crop / Rotate / Remove background / Clean metadata."
echo "Strata picks up the actions the next time a menu opens (or see Settings > Actions)."
((${#disabled[@]})) && echo "Kept ${#disabled[@]} action(s) you turned off disabled."

# If the Nautilus ring is also installed and running, it's still on the old
# code: restart it (as install-dolphin.sh does; retried once for the same reason).
if pgrep -f '/(convert-ring|prism)/ring.py --daemon$' >/dev/null; then
    prism_restart_ring
    if "$DIR/ring-start" >/dev/null 2>&1 || { sleep 1; "$DIR/ring-start" >/dev/null 2>&1; }; then
        echo "Restarted the Nautilus ring on the new code."
    else
        echo "The Nautilus ring didn't restart; it starts on the next Hyprland reload or login." >&2
    fi
fi
command -v strata >/dev/null || echo "Strata isn't installed yet: the actions appear once it is." >&2
