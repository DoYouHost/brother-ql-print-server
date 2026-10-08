#!/bin/sh
# Builds dist/label-printer_<version>_<arch>.deb for the machine it runs on.
#
# Run it on the architecture and Debian release you are targeting (for a Raspberry
# Pi OS 64-bit: on a Pi 3/4/5/Zero 2 W): the bundled virtualenv holds compiled
# wheels and is tied to this Python. Needs python3-venv and dpkg-deb, plus network
# access for pip.
set -eu
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' label_printer/__init__.py)
ARCH=$(dpkg --print-architecture)
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYNEXT=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor + 1}")')
MAINTAINER=$(printf '%s <%s>' "$(git config user.name || echo unknown)" "$(git config user.email || echo unknown@localhost)")

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/pkg"
mkdir -p "$PKG/opt/label-printer" "$PKG/lib/systemd/system" "$PKG/etc/default" "$PKG/DEBIAN" dist

python3 -m venv "$PKG/opt/label-printer/venv"
"$PKG/opt/label-printer/venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check .
# The venv was built under $PKG but runs from /opt/label-printer: fix the paths it recorded
grep -rlI "$PKG" "$PKG/opt/label-printer/venv/bin" "$PKG/opt/label-printer/venv/pyvenv.cfg" | xargs -r sed -i "s|$PKG||g"
find "$PKG/opt/label-printer/venv" -name '*.pyc' -delete

install -m 644 deploy/label-printer.service "$PKG/lib/systemd/system/label-printer.service"
install -m 644 deploy/default "$PKG/etc/default/label-printer"
install -m 755 deploy/debian/postinst deploy/debian/prerm deploy/debian/postrm "$PKG/DEBIAN/"
echo /etc/default/label-printer > "$PKG/DEBIAN/conffiles"

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: label-printer
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $SIZE
Depends: python3 (>= $PYVER), python3 (<< $PYNEXT), poppler-utils, libgl1, libglib2.0-0, avahi-daemon, adduser
Section: net
Priority: optional
Description: Print server for Brother QL label printers
 Web page and HTTP API that turn PDF, PNG, JPG and ZIP files into labels for a
 Brother QL printer connected to a Raspberry Pi. Announces itself on the network
 through Avahi (_labelprinter._tcp) so apps can find it.
CONTROL

OUT="dist/label-printer_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "built $OUT ($(du -h "$OUT" | cut -f1))"
