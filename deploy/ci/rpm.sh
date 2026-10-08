#!/bin/sh
# Runs inside a fedora container with the repo mounted at /src: builds the .rpm
# and checks that it installs and starts importing.
set -eu
dnf install -y -q python3 python3-pip rpm-build >/dev/null
sh /src/deploy/build-rpm.sh
dnf install -y -q /src/dist/*.rpm >/dev/null
/opt/label-printer/python/bin/python3 -c "import label_printer.server"
/opt/label-printer/python/bin/python3 -m label_printer --version
