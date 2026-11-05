# Shared by install-nautilus.sh and install-dolphin.sh (sourced, not run).
# Copies the repo to ~/.local/share/prism, so half-finished edits in the repo
# never reach the running code, then re-runs the calling installer from there.
# After that, sets up the PDF libraries the first time.
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/prism"

# What Prism needs, as command -> Fedora package. Everything missing is reported
# at once, with one command to install it. $1: nautilus or dolphin.
prism_deps() {
    [[ -n "${PRISM_SKIP_DEPS:-}" ]] && return 0
    [[ $EUID -ne 0 ]] || { echo "Run this as yourself, not root (no sudo): Prism installs into your home folder." >&2; exit 1; }
    local -A need=(
        [git]=git [rsync]=rsync [magick]=ImageMagick [ffmpeg]=ffmpeg-free [ffprobe]=ffmpeg-free
        [pandoc]=pandoc-cli [soffice]="libreoffice-writer libreoffice-calc libreoffice-impress"
        [bwrap]=bubblewrap [pdftotext]=poppler-utils [pdftoppm]=poppler-utils [heif-enc]=libheif-tools
        [heif-dec]=libheif-tools [wl-copy]=wl-clipboard [notify-send]=libnotify [flock]=util-linux-core
        [systemd-run]=systemd
    )
    if [[ "$1" == nautilus ]]; then
        need+=([nautilus]=nautilus [hyprctl]=hyprland)
    else
        need+=([dolphin]=dolphin)
    fi
    local missing=() cmd
    for cmd in "${!need[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("${need[$cmd]}")
    done
    [[ -x /usr/bin/python3 ]] || missing+=(python3)
    /usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' \
        >/dev/null 2>&1 || missing+=(python3-gobject gtk4)
    /usr/bin/python3 -c 'import cairo' >/dev/null 2>&1 || missing+=(python3-cairo)
    # REMOVE BG (flat graphics, and around the GPU model).
    /usr/bin/python3 -c 'import numpy' >/dev/null 2>&1 || missing+=(python3-numpy)
    /usr/bin/python3 -c 'import scipy' >/dev/null 2>&1 || missing+=(python3-scipy)
    /usr/bin/python3 -c 'import PIL' >/dev/null 2>&1 || missing+=(python3-pillow)
    [[ "$1" != nautilus || -e /usr/lib64/libgtk4-layer-shell.so.0 ]] || missing+=(gtk4-layer-shell)
    if ((${#missing[@]})); then
        local unique
        unique=$(printf '%s\n' "${missing[@]}" | tr ' ' '\n' | sort -u | tr '\n' ' ')
        echo "Missing dependencies. Install them, then run this again:
    sudo dnf install ${unique% }" >&2
        exit 1
    fi
}

prism_copy() {  # $1: the calling installer's path
    local src
    src="$(dirname "$(readlink -f "$1")")"
    [[ "$src" == "$DIR" ]] && return 0
    command -v rsync >/dev/null || { echo "The installer needs rsync (sudo dnf install rsync)" >&2; exit 1; }
    if [[ -d "$DIR" && ! -e "$DIR/ring.py" && -n "$(ls -A "$DIR")" ]]; then
        echo "Refusing to overwrite $DIR: it exists and isn't a Prism install" >&2
        exit 1
    fi
    mkdir -p "$DIR"
    # --delete drops files removed from the repo; excluded paths (the PDF
    # and model environments) are never deleted.
    rsync -a --delete --exclude /.git/ --exclude /.github/ --exclude /.pdf-venv/ --exclude /.model-venv/ \
        --exclude __pycache__/ --exclude '*.pyc' "$src/" "$DIR/"
    echo "Copied $src to $DIR"
    exec "$DIR/$(basename "$1")"
}

prism_pdf() {
    if [[ ! -x "$DIR/.pdf-venv/bin/python" && -z "${PRISM_SKIP_PDF:-}" ]]; then
        echo "Setting up the PDF libraries (once; downloads ~450 MB from PyPI)..."
        "$DIR/setup-pdf.sh" || echo "PDF setup failed; everything but PDF conversions works. Retry: $DIR/setup-pdf.sh" >&2
    fi
}

# REMOVE BG's GPU model: checks the GPU, then fetches PyTorch (if missing) and the
# weights, asking first on a terminal. Never fails the install: without the
# model, flat graphics still work, and the reason is saved for the refusal.
prism_model() {
    /usr/bin/python3 "$DIR/setup-model.py" || echo "The model setup failed; retry: $DIR/setup-model.py" >&2
}

# Restart the resident ring (Nautilus only) so it runs the code just copied.
# On SIGTERM it finishes running conversions first (up to 2 minutes): wait for
# that rather than cutting them off, but make sure it is really gone in the end
# (a stuck ring once survived a restart silently). No ring running: nothing to do.
prism_restart_ring() {
    local pid i
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
}
