#!/bin/sh
set -eu

# Builds the native Linux artifact from an Ubuntu WSL installation without
# changing its system packages. All apt packages and Python build tools are
# extracted into a temporary directory and removed on exit.

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT=${OUTPUT:-"$PROJECT_DIR/releases"}
TEMPORARY=$(mktemp -d /tmp/adt-video-wsl-build.XXXXXX)
cleanup() {
    rm -rf -- "$TEMPORARY"
}
trap cleanup EXIT INT TERM

cd "$TEMPORARY"
apt download \
    blt \
    libtcl8.6 \
    libtk8.6 \
    libxft2 \
    libxss1 \
    python3-pip \
    python3-setuptools \
    python3-tk \
    python3-wheel \
    tk8.6-blt2.5 >/dev/null 2>&1

mkdir package-root
for package in ./*.deb; do
    dpkg-deb -x "$package" package-root
done

PIP_PACKAGES="$TEMPORARY/package-root/usr/lib/python3/dist-packages"
BUILD_PACKAGES="$TEMPORARY/build-packages"
PYTHONPATH="$PIP_PACKAGES" python3 -m pip install \
    --disable-pip-version-check \
    --target "$BUILD_PACKAGES" \
    -r "$SCRIPT_DIR/requirements-build.txt"

export PYTHONPATH="$BUILD_PACKAGES:$TEMPORARY/package-root/usr/lib/python3.12:$TEMPORARY/package-root/usr/lib/python3.12/lib-dynload:$PIP_PACKAGES"
export LD_LIBRARY_PATH="$TEMPORARY/package-root/usr/lib:$TEMPORARY/package-root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export TCL_LIBRARY="$TEMPORARY/package-root/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$TEMPORARY/package-root/usr/share/tcltk/tk8.6"

python3 -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.destroy()"
python3 "$SCRIPT_DIR/build_release.py" --output "$OUTPUT" "$@"
