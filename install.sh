#!/usr/bin/env bash
# Surface 3 RTSP camera kiosk - installer.
# Installs the streaming appliance (mpv under cage), the freeze watchdog,
# the on-screen/hardware refresh button, and wires everything to boot.
#
# Run with:  sudo ./install.sh
set -euo pipefail

# ---- the user the kiosk runs as (defaults to whoever invoked sudo) ----
KIOSK_USER="${SUDO_USER:-dhrumil}"
# -----------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo: sudo ./install.sh" >&2
    exit 1
fi

if ! id "$KIOSK_USER" >/dev/null 2>&1; then
    echo "User '$KIOSK_USER' does not exist. Edit KIOSK_USER at the top of this script." >&2
    exit 1
fi

USER_UID="$(id -u "$KIOSK_USER")"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
apt update
apt install -y mpv cage fonts-dejavu socat triggerhappy evtest

echo "==> Installing files"
install -D -m 0644 "$HERE/systemd/camera-stream.service"   /etc/systemd/system/camera-stream.service
install -D -m 0644 "$HERE/systemd/camera-watchdog.service" /etc/systemd/system/camera-watchdog.service
install -D -m 0644 "$HERE/systemd/net-watchdog.service"  /etc/systemd/system/net-watchdog.service
install -D -m 0644 "$HERE/pam/cage"                        /etc/pam.d/cage
install -D -m 0755 "$HERE/scripts/camera-watchdog.sh"      /usr/local/bin/camera-watchdog.sh
install -D -m 0755 "$HERE/scripts/net-watchdog.sh"    /usr/local/bin/net-watchdog.sh
install -D -m 0644 "$HERE/scripts/refresh-button.lua"      /usr/local/share/mpv-refresh/refresh-button.lua
install -D -m 0644 "$HERE/triggerhappy/refresh.conf"       /etc/triggerhappy/triggers.d/refresh.conf

# Create the credentials file if it does not exist yet (never overwrite it).
if [ ! -f /etc/camera-stream.env ]; then
    install -m 0600 "$HERE/camera-stream.env.example" /etc/camera-stream.env
    echo "    created /etc/camera-stream.env - EDIT IT and set CAMERA_URL"
    NEED_URL=1
else
    echo "    /etc/camera-stream.env already exists - leaving it untouched"
    NEED_URL=0
fi

# Patch the installed copies if this user / uid differs from the baked-in defaults.
if [ "$KIOSK_USER" != "dhrumil" ]; then
    sed -i "s/^User=dhrumil/User=$KIOSK_USER/" /etc/systemd/system/camera-stream.service
    echo "    (set User=$KIOSK_USER)"
fi
if [ "$USER_UID" != "1000" ]; then
    sed -i "s#/run/user/1000#/run/user/$USER_UID#g" \
        /etc/systemd/system/camera-stream.service \
        /usr/local/bin/camera-watchdog.sh
    echo "    (set runtime dir to /run/user/$USER_UID)"
fi

echo "==> Configuring user, groups, and lingering session"
usermod -aG video,render,input,tty "$KIOSK_USER"
loginctl enable-linger "$KIOSK_USER"

echo "==> Setting boot to console (no graphical login)"
systemctl set-default multi-user.target
systemctl disable lightdm 2>/dev/null || true
systemctl disable --now getty@tty1.service 2>/dev/null || true

echo "==> Enabling services"
systemctl daemon-reload
systemctl enable camera-stream.service camera-watchdog.service net-watchdog.service
systemctl restart triggerhappy.service 2>/dev/null || true

cat <<EOF

============================================================
Install complete. Two things to check before you rely on it:

  1. RTSP URL - set CAMERA_URL in:
        /etc/camera-stream.env      (chmod 600, not in git)

  2. Hardware button - confirm your key name:
        sudo evtest
     If the Windows button is not KEY_LEFTMETA, edit:
        /etc/triggerhappy/triggers.d/refresh.conf
     then:  sudo systemctl restart triggerhappy.service

Start it now without rebooting:
     sudo systemctl start camera-stream.service camera-watchdog.service

...or reboot to bring it all up automatically:
     sudo reboot
============================================================
EOF
