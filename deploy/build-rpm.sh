#!/bin/sh
# Builds dist/label-printer-<version>-1.<arch>.rpm for the machine it runs on.
#
# Like build-deb.sh, the package bundles its own Python and only has to match the CPU
# architecture. Needs python3, rpm-build and network access.
set -eu
cd "$(dirname "$0")/.."

. deploy/stage.sh
mkdir -p "$PKG/usr/lib/systemd/system"
install -m 644 deploy/label-printer.service "$PKG/usr/lib/systemd/system/label-printer.service"

rpmbuild -bb deploy/rpm/label-printer.spec --quiet \
    --define "_topdir $STAGE/rpm" \
    --define "stagedir $PKG" \
    --define "pkgversion $VERSION"
OUT=$(find "$STAGE/rpm/RPMS" -name '*.rpm')
mv "$OUT" dist/
echo "built dist/$(basename "$OUT") ($(du -h "dist/$(basename "$OUT")" | cut -f1))"
