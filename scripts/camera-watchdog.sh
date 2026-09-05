#!/usr/bin/env bash
# Restarts camera-stream.service if mpv is alive but the picture has frozen.
# Detects a stall by watching whether playback position (time-pos) advances.

SOCK=/run/user/1000/mpv-camera.sock
INTERVAL=5          # seconds between checks
STALL_LIMIT=2       # consecutive frozen checks before restarting (~10s)
last=""
count=0

while sleep "$INTERVAL"; do
    pos=$(printf '{"command":["get_property","time-pos"]}\n' \
          | socat - "$SOCK" 2>/dev/null \
          | sed -n 's/.*"data":\([0-9.]*\).*/\1/p')

    if [ -z "$pos" ]; then
        # mpv not answering yet (starting or restarting) - Restart=always covers exits.
        last=""; count=0; continue
    fi

    if [ "$pos" = "$last" ]; then
        count=$((count + 1))
        if [ "$count" -ge "$STALL_LIMIT" ]; then
            logger -t camera-watchdog "stream frozen at ${pos}s - restarting"
            systemctl restart camera-stream.service
            last=""; count=0
            sleep 10        # give it time to come back before checking again
        fi
    else
        last="$pos"; count=0
    fi
done
