#!/usr/bin/env bash
# Reboot the machine if the gateway stays unreachable for ~5 minutes.
# Disable on the fly by creating /etc/net-watchdog.disable

GATEWAY="192.168.1.1"     # host that must stay reachable
INTERVAL=60               # seconds between checks
FAIL_LIMIT=5              # consecutive failures before rebooting (~5 min)

fails=0
sleep "$INTERVAL"         # grace period so the network can come up at boot

while true; do
    if ping -c 3 -W 2 "$GATEWAY" >/dev/null 2>&1; then
        [ "$fails" -gt 0 ] && logger -t net-watchdog "gateway back after $fails fail(s)"
        fails=0
    else
        fails=$((fails + 1))
        logger -t net-watchdog "gateway $GATEWAY unreachable ($fails/$FAIL_LIMIT)"
        if [ "$fails" -ge "$FAIL_LIMIT" ]; then
            if [ -e /etc/net-watchdog.disable ]; then
                logger -t net-watchdog "reboot suppressed (/etc/net-watchdog.disable present)"
            else
                logger -t net-watchdog "unreachable ~$((FAIL_LIMIT*INTERVAL))s - rebooting"
                sync
                systemctl reboot
                exit 0
            fi
            fails=0
        fi
    fi
    sleep "$INTERVAL"
done