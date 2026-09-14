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

secure_boot=unavailable
for variable in /sys/firmware/efi/efivars/SecureBoot-*; do
    if [ -r "$variable" ]; then
        # efivarfs prefixes the payload with four little-endian attribute
        # bytes. SecureBoot itself is the following single byte.
        value=$(od -An -t u1 -j 4 -N 1 "$variable" | tr -d ' ')
        if [ "$value" = 1 ]; then
            secure_boot=enabled
        else
            secure_boot=disabled
        fi
        break
    fi
done

ima=unavailable
if [ -d /sys/kernel/security/ima ]; then
    ima=enabled
    case " $(cat /proc/cmdline) " in
        *" ima_appraise=enforce "*)
            if [ -r /sys/kernel/security/ima/policy ] &&
                    grep -q '^appraise ' /sys/kernel/security/ima/policy; then
                ima=enforcing
            fi
            ;;
    esac
fi

echo "BUCKOS_VERIFY flavor=$flavor version=${VERSION_ID:-unknown} arch=$arch pid1=$pid1 failed=$failed selinux=$selinux avc=$avc secure_boot=$secure_boot ima=$ima"
systemctl poweroff --force --force
