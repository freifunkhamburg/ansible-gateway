#!/bin/sh
# Validate a staged (not yet installed) unbound.conf for a chrooted setup.
#
# The config declares `chroot: /var/lib/unbound` and unbound-checkconf
# really enters that chroot, so the staged file and the conf.d includes have
# to exist inside it — the same staging the unit's ExecStartPre
# (unbound-helper chroot_setup) performs at service start.
set -e
STAGED="$1"
CHROOT=/var/lib/unbound
STAGED_DIR="$CHROOT$(dirname "$STAGED")"

mkdir -p "$CHROOT" "$STAGED_DIR" "$CHROOT/etc/unbound"
tar -C "$(dirname "$STAGED")" -c . | tar -C "$STAGED_DIR" -x
if [ -d /etc/unbound ]; then
  tar -C /etc/unbound -c . 2>/dev/null | tar -C "$CHROOT/etc/unbound" -x || true
fi
/usr/sbin/unbound-checkconf "$CHROOT$STAGED"
rc=$?
# do not accumulate the per-run staging copies inside the chroot
rm -rf "${STAGED_DIR%/.*}" 2>/dev/null || true
rm -rf "$STAGED_DIR"
exit $rc
