#!/usr/bin/env bash
# OPTIONAL - only needed if you have NOT already set up the linux-surface kernel.
# Adds the linux-surface apt repo and installs its kernel. On the Surface 3 this
# fixes the stuck-backlight and (on "OEMB" units) the sound/WMI drivers.
# Run with: sudo ./setup-surface-kernel.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo ./setup-surface-kernel.sh" >&2
    exit 1
fi

echo "==> Importing linux-surface signing key"
wget -qO - https://raw.githubusercontent.com/linux-surface/linux-surface/master/pkg/keys/surface.asc \
    | gpg --dearmor | dd of=/etc/apt/trusted.gpg.d/linux-surface.gpg

echo "==> Adding linux-surface repository"
echo "deb [arch=amd64] https://pkg.surfacelinux.com/debian release main" \
    > /etc/apt/sources.list.d/linux-surface.list

echo "==> Installing the surface kernel"
apt update
apt install -y linux-image-surface linux-headers-surface
update-grub

echo
echo "Done. Reboot, then verify with:  uname -a   (should contain 'surface')"
echo "Check for the OEMB quirk with:   cat /sys/devices/virtual/dmi/id/product_name"
