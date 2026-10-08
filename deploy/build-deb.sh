#!/bin/sh
# Builds dist/label-printer_<version>_<arch>.deb for the machine it runs on.
#
# The package bundles its own Python, so it only has to match the CPU architecture:
# build on the Pi (or any arm64 / amd64 Debian machine). Needs python3-venv (to fetch
# uv), dpkg-deb and network access.
set -eu
cd "$(dirname "$0")/.."

ARCH=$(dpkg --print-architecture)
if command -v git >/dev/null 2>&1; then
    MAINTAINER=$(printf '%s <%s>' "$(git config user.name || echo unknown)" "$(git config user.email || echo unknown@localhost)")
else
    MAINTAINER="unknown <unknown@localhost>"
fi

. deploy/stage.sh
mkdir -p "$PKG/lib/systemd/system" "$PKG/DEBIAN"
install -m 644 deploy/label-printer.service "$PKG/lib/systemd/system/label-printer.service"
install -m 755 deploy/debian/postinst deploy/debian/prerm deploy/debian/postrm "$PKG/DEBIAN/"
echo /etc/default/label-printer > "$PKG/DEBIAN/conffiles"

SIZE=$(du -sk "$PKG" | cut -f1)
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: label-printer
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $SIZE
Depends: poppler-utils, libgl1, libglib2.0-0, avahi-daemon, adduser
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
