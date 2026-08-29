#!/bin/sh

set -eu

# multi-user.target can become active while late device-triggered services are
# still settling. Give them a bounded window before taking the failure/AVC
# snapshot so the result does not depend on boot scheduling.
sleep 2

. /etc/os-release
flavor=${ID:-unknown}
if command -v rpm >/dev/null 2>&1 && rpm -q centos-release-hyperscale >/dev/null 2>&1; then
    flavor=centos-hyperscale
fi

arch=$(uname -m)
pid1=$(cat /proc/1/comm)
systemctl --failed --no-legend --plain | sed 's/^/BUCKOS_FAILED /'
failed=$(systemctl --failed --no-legend --plain | awk 'NF { count++ } END { print count + 0 }')
selinux=not-installed
if command -v getenforce >/dev/null 2>&1; then
    selinux=$(getenforce)
elif [ -r /sys/fs/selinux/enforce ]; then
    if [ "$(cat /sys/fs/selinux/enforce)" = 1 ]; then
        selinux=Enforcing
    else
        selinux=Permissive
    fi
fi
avc=$(dmesg 2>/dev/null | grep -c 'avc:  denied' || true)

echo "BUCKOS_VERIFY flavor=$flavor version=${VERSION_ID:-unknown} arch=$arch pid1=$pid1 failed=$failed selinux=$selinux avc=$avc"
systemctl poweroff --force --force
