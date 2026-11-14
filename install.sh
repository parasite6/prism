#!/usr/bin/env bash
# Kept for Prism 0.1's one-line installer (and older instructions), which run
# install.sh from the repo. That used to install the Nautilus and Dolphin parts
# together; each now has its own installer. This runs the Nautilus one, plus the
# Dolphin and Strata ones if their menus were installed before. New installs: run
# install-nautilus.sh, install-dolphin.sh or install-strata.sh directly.
set -euo pipefail
here="$(dirname "$(readlink -f "$0")")"
bash "$here/install-nautilus.sh"
if compgen -G "${XDG_DATA_HOME:-$HOME/.local/share}/kio/servicemenus/dev.prism.menu-*.desktop" >/dev/null; then
    bash "$here/install-dolphin.sh"
fi
if compgen -G "${XDG_CONFIG_HOME:-$HOME/.config}/strata/actions/dev.prism.*/action.toml" >/dev/null; then
    bash "$here/install-strata.sh"
fi
