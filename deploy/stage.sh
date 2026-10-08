# Sourced by build-deb.sh and build-rpm.sh (from the repo root): installs the app into a
# self-contained Python under a staging tree and leaves VERSION, STAGE and PKG set.
#
# The package carries its own interpreter (python-build-standalone, fetched by uv), so it
# does not depend on the distribution's Python version and one recipe serves every distro.
PYTHON_VERSION=3.13

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' label_printer/__init__.py)

# Work on the disk, not in /tmp: on Raspberry Pi OS /tmp is a small tmpfs and the OpenCV wheel alone does not fit
mkdir -p dist
STAGE=$(mktemp -d dist/.stage.XXXXXX)
STAGE=$(cd "$STAGE" && pwd)
trap 'rm -rf "$STAGE"' EXIT
export TMPDIR="$STAGE/tmp"
mkdir -p "$TMPDIR"
PKG="$STAGE/pkg"
APP="$PKG/opt/label-printer"
mkdir -p "$APP" "$PKG/etc/default"

if ! command -v uv >/dev/null 2>&1; then
    python3 -m venv "$STAGE/uv-tool"
    "$STAGE/uv-tool/bin/pip" install --quiet --no-cache-dir uv
    PATH="$STAGE/uv-tool/bin:$PATH"
fi
UV_PYTHON_INSTALL_DIR="$STAGE/py" uv python install --quiet "$PYTHON_VERSION"
# uv also leaves a minor-version symlink next to the real directory
mv "$(find "$STAGE/py" -mindepth 1 -maxdepth 1 -type d -name 'cpython-*')" "$APP/python"
PY="$APP/python/bin/python3"
# uv marks its pythons as externally managed; this tree is ours to modify
uv pip install --quiet --no-cache --break-system-packages --python "$PY" .

# Scripts were written with $PKG in their shebangs, but the tree runs from /opt/label-printer
grep -rlI "$PKG" "$APP/python/bin" | xargs -r sed -i "s|$PKG||g"
# Drop what a headless print server never loads: Tk and IDLE, pip, headers, docs, the libraries' own tests
LIB="$APP/python/lib"
rm -rf "$APP/python/include" "$APP/python/share" "$LIB"/tcl* "$LIB"/tk* "$LIB"/itcl* "$LIB"/thread* "$LIB"/libtcl* \
    "$LIB"/python*/idlelib "$LIB"/python*/tkinter "$LIB"/python*/turtledemo "$LIB"/python*/ensurepip \
    "$LIB"/python*/config-* "$LIB"/python*/site-packages/pip "$LIB"/python*/site-packages/pip-*
find "$LIB" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
# Recompile so the bytecode records the final paths instead of the staging ones
find "$APP/python" -name '*.pyc' -delete
"$PY" -m compileall -q -s "$PKG" "$APP/python/lib" >/dev/null || true
install -m 644 deploy/default "$PKG/etc/default/label-printer"
