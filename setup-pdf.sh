#!/usr/bin/env bash
set -euo pipefail
DIR="$(dirname "$(readlink -f "$0")")"
env -u PYTHONHOME -u PYTHONPATH /usr/bin/python3 -m venv "$DIR/.pdf-venv"
env -u PYTHONHOME -u PYTHONPATH "$DIR/.pdf-venv/bin/python" -m pip install -r "$DIR/requirements-pdf.txt"
