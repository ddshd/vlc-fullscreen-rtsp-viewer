# Surface 3 RTSP Camera Kiosk

A "set-and-forget" appliance that boots straight into a fullscreen RTSP camera
feed and keeps it running: it auto-restarts on crashes, detects silent freezes,
and offers a refresh button (on-screen tap, the tablet's volume/Windows buttons,
or the `r` key).

Built for a **Microsoft Surface 3** (Atom x7-Z8700, 2 GB RAM) running **Debian 13
"trixie"**, but it works on any Debian/Ubuntu box with minor edits.

## Why mpv and not VLC

Debian and Ubuntu ship VLC **without RTSP support** (the `live555` module is
stripped out by policy), so `rtsp://` camera URLs fail there with
`satip stream error: Failed to setup RTSP session` even though the same URL plays
in VLC on Windows. mpv decodes RTSP through FFmpeg instead, so it is unaffected -
and it is lighter, which suits 2 GB of RAM. (If you must use VLC, the Flatpak
build includes RTSP.)

## What's in here

```
surface3-camera-kiosk/
├── install.sh                     # one-shot installer (run with sudo)
├── README.md                      # this file
├── camera-stream.env.example      # template for your (git-ignored) camera URL
├── .gitlab-ci.yml                 # CI job that builds the release ZIP
├── .gitignore                     # keeps secrets + build output out of git
├── .gitattributes                 # keeps CI/git plumbing out of the ZIP
├── systemd/
│   ├── camera-stream.service      # cage + mpv, auto-restart, boots on tty1
│   └── camera-watchdog.service    # runs the freeze watchdog
├── scripts/
│   ├── camera-watchdog.sh         # restarts the stream if the picture freezes
│   └── refresh-button.lua         # on-screen + key + volume-button refresh
├── pam/
│   └── cage                       # PAM session so cage can access GPU/input
├── triggerhappy/
│   └── refresh.conf               # maps a hardware button to a stream restart
└── optional/
    └── setup-surface-kernel.sh    # linux-surface kernel (only if not done yet)
```

## Quick start

```
sudo ./install.sh
sudo reboot
```

It installs the packages, drops each file in place, points the box at a console
boot (no desktop), and enables the services. On reboot the camera feed comes up
fullscreen with no login.

## Credentials (kept out of git)

The camera URL - which contains your username and password - is **not** stored in
any committed file. `camera-stream.service` reads it from
`/etc/camera-stream.env`, which lives only on the device (root-owned, `chmod 600`)
and is git-ignored. The repo ships only `camera-stream.env.example` as a template.

`install.sh` creates `/etc/camera-stream.env` from the template on first run (and
never overwrites an existing one). Set your URL there:

```
sudo nano /etc/camera-stream.env       # set CAMERA_URL=rtsp://user:pass@host:554/path
sudo systemctl restart camera-stream.service
```

If your user/pass contains `@ : / #`, percent-encode them (`@` -> `%40`, etc.).

## Two other things to check

1. **Username** - files assume `dhrumil`. `install.sh` auto-substitutes the user
   that ran `sudo`, so usually you don't need to touch this.
2. **UID** - files assume uid `1000` (`/run/user/1000`). `install.sh` fixes this
   automatically if your uid differs. Check with `id -u <user>`.

## Prerequisites (you have probably done these already)

- **Debian 13** installed, minimal (no desktop needed).
- **linux-surface kernel** - optional but recommended on the Surface 3; it fixes
  the stuck-backlight issue and, on "OEMB" units, the sound/WMI drivers. If you
  haven't set it up: `sudo optional/setup-surface-kernel.sh`.
- **Hardware video decode** is intentionally OFF (`--hwdec=no`). The Reolink
  sub-stream is low-res and decodes fine in software on this CPU, which is more
  reliable. If you switch to the full `_main` stream and CPU load climbs, install
  `i965-va-driver vainfo`, set `LIBVA_DRIVER_NAME=i965`, and change `--hwdec=no`
  to `--hwdec=vaapi` in the service (note: packaged mpv's VA-API on this old Intel
  GPU can be finicky).

## How it stays up

- **Crashes / exits / camera drop** -> `Restart=always` in the service relaunches
  mpv after 3 s. `StartLimitIntervalSec=0` means it never stops retrying.
- **Dead connection that hangs** -> `--demuxer-lavf-o=timeout=5000000` makes the
  FFmpeg demuxer give up after 5 s of no data, so mpv exits and gets restarted.
- **Silent freeze (process alive, picture frozen)** -> `camera-watchdog.sh` watches
  mpv's playback position over its IPC socket and restarts the stream if it stops
  advancing for ~10 s.

## Refreshing manually

Any of these reload the stream:
- **Tap** the on-screen REFRESH button (top-right).
- **Volume up / down** on the tablet.
- **Windows/Home button** (via triggerhappy; restarts the service).
- Press **`r`** if a keyboard is attached.

The on-screen tap and the volume/`r` bindings reload in-process (instant, no black
flash). The hardware Windows button does a full service restart (~2-3 s black
screen) unless you switch it to the in-process form noted in `refresh.conf`.

## Everyday commands

```
# watch what the stream is doing
journalctl -u camera-stream.service -f

# watch the watchdog
journalctl -t camera-watchdog -f

# restart / stop by hand
sudo systemctl restart camera-stream.service
sudo systemctl stop camera-stream.service camera-watchdog.service

# turn the whole thing off and get a normal login back
sudo systemctl disable --now camera-stream.service camera-watchdog.service
sudo systemctl set-default graphical.target   # if you want a desktop back
```

## Testing it works

```
# Simulate a frozen feed - the watchdog should restart within ~10-15 s:
sudo iptables -A OUTPUT -d <CAMERA_IP> -j DROP
journalctl -t camera-watchdog -f          # watch for the "restarting" line
sudo iptables -D OUTPUT -d <CAMERA_IP> -j DROP   # restore the camera
```

## Building the ZIP / GitLab CI

The `.gitlab-ci.yml` pipeline builds a downloadable `surface3-camera-kiosk.zip`
on every branch and tag. It uses `git archive`, so:

- only **committed** files go in - a local `/etc/camera-stream.env` or any
  untracked secret can never end up in the artifact;
- files marked `export-ignore` in `.gitattributes` (the CI/git plumbing) are
  excluded automatically.

Grab the ZIP from the pipeline's job artifacts in GitLab. To build the same
archive locally:

```
git archive --format=zip --prefix=surface3-camera-kiosk/ -o surface3-camera-kiosk.zip HEAD
```

## Notes / gotchas

- If taps land offset from the button, the touchscreen needs a libinput
  calibration matrix under Wayland - fixable, ask if you hit it.
- If the refresh glyph shows as an empty box, delete the `\226\159\179` bytes in
  `refresh-button.lua` and keep just "REFRESH".
- Don't bind `KEY_POWER` for refresh - logind treats it as shutdown/suspend.
