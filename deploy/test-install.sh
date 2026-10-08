#!/bin/sh
# Builds the package and installs it in a clean Debian, then exercises it.
# Run it inside a container, as root, with the repo mounted read-only at /src:
#   docker run --rm -v "$PWD":/src:ro debian:13 sh /src/deploy/test-install.sh
# systemd is not running in a container, so the unit itself is not exercised;
# everything the unit would run is, as the service user.
set -eu
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAILED: $*" >&2; exit 1; }

step "tools"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv dpkg-dev curl >/dev/null
cp -r /src /work && cd /work

step "build"
sh deploy/build-deb.sh
DEB=$(ls dist/*.deb)
dpkg-deb -f "$DEB" Package Version Architecture Depends

step "install (apt resolves the dependencies)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "./$DEB" >/dev/null
getent passwd label-printer | grep -q ':/usr/sbin/nologin$' || fail "service user missing"
[ "$(id -gn label-printer)" = lp ] || fail "service user is not in group lp"
[ -f /etc/default/label-printer ] && [ -f /lib/systemd/system/label-printer.service ] || fail "files missing"
head -1 /opt/label-printer/venv/bin/label-printer | grep -qx '#!/opt/label-printer/venv/bin/python3' || fail "venv paths not fixed"
/opt/label-printer/venv/bin/python -m label_printer --version
/opt/label-printer/venv/bin/python -m label_printer check

step "announcement: install and withdraw"
/opt/label-printer/venv/bin/python -m label_printer announce install
grep -q '<type>_labelprinter._tcp</type>' /etc/avahi/services/label-printer.service || fail "no announcement"
grep -E 'port|txt-record' /etc/avahi/services/label-printer.service
/opt/label-printer/venv/bin/python -m label_printer announce remove
[ ! -e /etc/avahi/services/label-printer.service ] || fail "announcement not withdrawn"

step "run as the service user, with the settings read from the environment"
su -s /bin/sh label-printer -c 'PRINTER_IDENTIFIER=file:///dev/null LABEL_PRINTER_PORT=8123 /opt/label-printer/venv/bin/python -m label_printer' >/tmp/server.log 2>&1 &
for i in $(seq 1 60); do curl -sf -o /dev/null http://127.0.0.1:8123/info && break; sleep 1; done
curl -sf http://127.0.0.1:8123/info || { cat /tmp/server.log; fail "server did not answer"; }
echo
for path in / /web/tokens.css /web/fonts/manrope-latin.woff2; do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8123$path")" = 200 ] || fail "$path not served"
done
echo "web page, tokens and fonts served"
kill %1 2>/dev/null || true

step "a wrong setting stops the service with a message instead of a traceback"
if PRINTER_LABEL=29x90 /opt/label-printer/venv/bin/python -m label_printer check 2>/tmp/err; then fail "bad label accepted"; fi
cat /tmp/err

step "remove and purge"
DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq label-printer >/dev/null
[ -f /etc/default/label-printer ] || fail "settings must survive a remove"
DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq label-printer >/dev/null
[ ! -e /opt/label-printer ] && [ ! -e /etc/default/label-printer ] && ! getent passwd label-printer >/dev/null || fail "purge left files or the user behind"
echo "clean"

printf '\nALL CHECKS PASSED\n'
