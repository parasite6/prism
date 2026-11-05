# Shared by install-nautilus.sh and install-dolphin.sh (sourced, not run).
# Copies the repo to ~/.local/share/prism, so half-finished edits in the repo
# never reach the running code, then re-runs the calling installer from there.
# After that, sets up the PDF libraries the first time.
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/prism"

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
    # environment) are never deleted.
    rsync -a --delete --exclude /.git/ --exclude /.github/ --exclude /.pdf-venv/ \
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
