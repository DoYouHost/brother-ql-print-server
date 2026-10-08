#!/bin/sh
# Runs inside a debian:13 container with the repo mounted at /src: builds the .deb
# and checks that it installs and starts importing.
set -eu
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv dpkg-dev ca-certificates >/dev/null
sh /src/deploy/build-deb.sh
apt-get install -y -qq /src/dist/*.deb >/dev/null
/opt/label-printer/python/bin/python3 -c "import label_printer.server"
/opt/label-printer/python/bin/python3 -m label_printer --version
