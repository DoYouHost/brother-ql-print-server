# Wraps the tree staged by build-rpm.sh; the bundled virtualenv must stay untouched
%global debug_package %{nil}
%global _build_id_links none
%global __os_install_post %{nil}
AutoReqProv: no

Name:       label-printer
Version:    %{pkgversion}
Release:    1
Summary:    Print server for Brother QL label printers
License:    AGPL-3.0-or-later
URL:        https://github.com/DoYouHost/brother-ql-print-server
Requires:   poppler-utils, mesa-libGL, glib2, avahi, systemd
Requires(pre): shadow-utils

%description
Web page and HTTP API that turn PDF, PNG, JPG and ZIP files into labels for a
Brother QL printer connected to a Raspberry Pi. Announces itself on the network
through Avahi (_labelprinter._tcp) so apps can find it.

%install
mkdir -p %{buildroot}
cp -a %{stagedir}/. %{buildroot}/

%files
/opt/label-printer
/usr/lib/systemd/system/label-printer.service
%config(noreplace) /etc/default/label-printer

%pre
getent passwd label-printer >/dev/null || \
    useradd --system --gid lp --no-create-home --home-dir /nonexistent --shell /sbin/nologin label-printer
exit 0

%post
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemctl enable label-printer.service >/dev/null 2>&1 || true
    systemctl restart label-printer.service || true
fi

%preun
if [ "$1" = 0 ] && [ -d /run/systemd/system ]; then
    systemctl stop label-printer.service || true
    systemctl disable label-printer.service >/dev/null 2>&1 || true
fi

%postun
if [ "$1" = 0 ]; then
    rm -f /etc/avahi/services/label-printer.service
    userdel label-printer >/dev/null 2>&1 || true
fi
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
