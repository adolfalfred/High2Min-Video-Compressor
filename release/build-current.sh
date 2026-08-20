#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PYTHON=${PYTHON:-python3}
OUTPUT=${OUTPUT:-"$PROJECT_DIR/releases"}
TEMPORARY=$(mktemp -d "${TMPDIR:-/tmp}/adt-video-native-build.XXXXXX")
cleanup() {
    rm -rf -- "$TEMPORARY"
}
trap cleanup EXIT INT TERM

"$PYTHON" -c "import tkinter" || {
    echo "Tk is required to build the desktop UI. Install the native Python Tk package first." >&2
    exit 1
}
"$PYTHON" -m venv "$TEMPORARY"
VENV_PYTHON="$TEMPORARY/bin/python"
"$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$SCRIPT_DIR/requirements-build.txt"

set -- "$SCRIPT_DIR/build_release.py" --output "$OUTPUT" "$@"
"$VENV_PYTHON" "$@"
