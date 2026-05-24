"""
RTSP Fullscreen Viewer
- Plays an RTSP stream fullscreen with no audio
- Auto-restarts on stream failure or stall
- Scheduled hourly restart
- Logs all events to stdout with local timestamps
- macOS: requires pyobjc  (pip install pyobjc-framework-Cocoa)
"""

import sys
import time
import threading
import datetime

IS_WINDOWS = sys.platform == "win32"
IS_MAC     = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# ── Windows-only imports ──────────────────────────────────────────────────────
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

# ── macOS-only imports ────────────────────────────────────────────────────────
if IS_MAC:
    try:
        from AppKit import (
            NSApplication, NSWindow, NSScreen,
            NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
            NSApplicationActivationPolicyRegular, NSColor,
        )
        import objc
    except ImportError:
        print(
            "ERROR: pyobjc-framework-Cocoa is required on macOS.\n"
            "Run: pip install pyobjc-framework-Cocoa",
            file=sys.stderr,
        )
        sys.exit(1)

try:
    import vlc
except ImportError:
    msg = (
        "python-vlc is not installed.\n\n"
        "Run: pip install python-vlc\n\n"
        "Also ensure VLC media player is installed on this machine."
    )
    if IS_WINDOWS:
        import ctypes as _ct
        _ct.windll.user32.MessageBoxW(0, msg, "Missing Dependency", 0x10)
    else:
        print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)

import os
RTSP_URL = os.environ.get("RTSP_URL")
if not RTSP_URL:
    print("ERROR: RTSP_URL environment variable is not set.", file=sys.stderr)
    print("Example: export RTSP_URL=rtsp://user:pass@192.168.1.1:554/stream", file=sys.stderr)
    sys.exit(1)

CACHE_MS          = 1000   # Network/live cache in milliseconds
RETRY_SEC         = 5      # Seconds to wait before reconnecting on failure
UDP_STALL_SEC     = 5      # Seconds of frozen playback clock before forcing restart
HOURLY_RESTART_S  = 3600   # Scheduled restart interval in seconds

VLC_BRIGHTNESS    = 1.2    # VLC video brightness (1.0 = normal, 1.2 = +20%)
BRIGHT_DAY        = 1.0    # Display brightness 05:00–00:00  (100%)
BRIGHT_NIGHT      = 0.7    # Display brightness 00:00–05:00  (70%)
NIGHT_START_H     = 0      # Hour night-dim begins (0 = midnight)
NIGHT_END_H       = 5      # Hour night-dim ends   (5 = 5am)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Display brightness ────────────────────────────────────────────────────────

def _target_brightness():
    """Return the brightness level that should be active right now."""
    h = datetime.datetime.now().hour
    if NIGHT_START_H <= h < NIGHT_END_H:
        return BRIGHT_NIGHT
    return BRIGHT_DAY


def _set_display_brightness_windows(level):
    """Set display brightness on Windows via WMI (0–100)."""
    try:
        import subprocess
        pct = int(level * 100)
        subprocess.run(
            ["powershell", "-Command",
             f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
             f".WmiSetBrightness(1,{pct})"],
            capture_output=True
        )
    except Exception as e:
        log(f"[BRIGHTNESS] Windows set failed: {e}")


def _set_display_brightness_mac(level):
    """Set display brightness on macOS via IOKit DisplayServices."""
    try:
        import ctypes
        ds = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        # IODisplaySetFloatParameter for brightness
        # Simpler: use osascript
        import subprocess
        pct = int(level * 100)
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set brightness of display 1 to {level}'],
            capture_output=True
        )
    except Exception as e:
        log(f"[BRIGHTNESS] macOS set failed: {e}")


def _set_display_brightness_linux(level):
    """Set display brightness on Linux via xrandr or brightnessctl."""
    try:
        import subprocess
        # Try brightnessctl first (works on most modern setups)
        result = subprocess.run(
            ["brightnessctl", "set", f"{int(level * 100)}%"],
            capture_output=True
        )
        if result.returncode != 0:
            raise RuntimeError("brightnessctl failed")
    except Exception:
        try:
            import subprocess
            subprocess.run(
                ["xrandr", "--output", "LVDS-1", "--brightness", str(level)],
                capture_output=True
            )
        except Exception as e:
            log(f"[BRIGHTNESS] Linux set failed: {e}")


def set_display_brightness(level):
    if IS_WINDOWS:
        _set_display_brightness_windows(level)
    elif IS_MAC:
        _set_display_brightness_mac(level)
    else:
        _set_display_brightness_linux(level)


class BrightnessManager:
    """Checks every minute whether the display brightness needs to change."""

    def __init__(self):
        self._current = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            target = _target_brightness()
            if target != self._current:
                label = "DAY (100%)" if target == BRIGHT_DAY else "NIGHT (70%)"
                log(f"[BRIGHTNESS] Setting display brightness → {label}")
                set_display_brightness(target)
                self._current = target
            time.sleep(60)


# ── Shared stream logic ───────────────────────────────────────────────────────

class StreamManager:
    """
    Platform-agnostic stream management:
    - start / restart with reason logging
    - failure/stall watcher
    - hourly scheduled restart
    """

    RESTART_REASON_LABELS = {
        "startup":  "STARTUP",
        "error":    "ERROR RECOVERY",
        "stall":    "STALL RECOVERY",
        "hourly":   "SCHEDULED HOURLY RESTART",
    }

    def __init__(self, instance, player):
        self.instance      = instance
        self.player        = player
        self._lock         = threading.Lock()
        self._running      = True
        self._start_time   = None   # monotonic time of last successful start

        threading.Thread(target=self._watch_loop,   daemon=True).start()
        threading.Thread(target=self._hourly_loop,  daemon=True).start()

    def start(self, reason="startup"):
        label = self.RESTART_REASON_LABELS.get(reason, reason.upper())
        if reason == "startup":
            log(f"[{label}] Starting stream → {RTSP_URL}")
        else:
            elapsed = self._elapsed_since_start()
            log(f"[{label}] Restarting stream (was up for {elapsed}) → {RTSP_URL}")

        try:
            self.player.stop()
        except Exception:
            pass

        media = self.instance.media_new(RTSP_URL)
        media.add_option(":network-caching={}".format(CACHE_MS))
        media.add_option(":live-caching={}".format(CACHE_MS))
        media.add_option(":no-audio")
        media.add_option(":rtsp-udp")
        self.player.set_media(media)
        self.player.play()
        self._start_time = time.monotonic()
        # Apply brightness boost to VLC video output
        self.player.video_set_adjust_int(vlc.VideoAdjust.Enable, 1)
        self.player.video_set_adjust_float(vlc.VideoAdjust.Brightness, VLC_BRIGHTNESS)
        log(f"  → Player started (state: {self.player.get_state()}, "
            f"VLC brightness: {VLC_BRIGHTNESS})")

    def restart(self, reason):
        """Thread-safe restart. Skips if one is already in progress."""
        if not self._lock.acquire(blocking=False):
            log(f"  [SKIP] Restart already in progress, ignoring '{reason}' trigger")
            return
        try:
            self.start(reason=reason)
            time.sleep(8)   # Cooldown before watcher can fire again
        finally:
            self._lock.release()

    def stop(self):
        self._running = False
        try:
            self.player.stop()
        except Exception:
            pass

    def _elapsed_since_start(self):
        if self._start_time is None:
            return "unknown"
        secs = int(time.monotonic() - self._start_time)
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _watch_loop(self):
        """Detects stream errors and UDP stalls, restarts as needed."""
        time.sleep(8)
        last_vlc_time = self.player.get_time()
        last_change   = time.monotonic()

        while self._running:
            time.sleep(2)
            state = self.player.get_state()

            # Hard error / unexpected stop
            if state in (
                vlc.State.Ended,
                vlc.State.Error,
                vlc.State.NothingSpecial,
                vlc.State.Stopped,
            ):
                log(f"[ERROR RECOVERY] Stream stopped unexpectedly (state: {state}), "
                    f"waiting {RETRY_SEC}s then restarting...")
                time.sleep(RETRY_SEC)
                if self._running:
                    self.restart("error")
                last_vlc_time = -1
                last_change   = time.monotonic()
                continue

            # UDP stall detection: playback clock frozen while "Playing"
            current_vlc_time = self.player.get_time()
            now              = time.monotonic()
            if current_vlc_time != last_vlc_time:
                last_vlc_time = current_vlc_time
                last_change   = now
            elif state == vlc.State.Playing and (now - last_change) > UDP_STALL_SEC:
                log(f"[STALL RECOVERY] Playback clock frozen for >{UDP_STALL_SEC}s "
                    f"(state: {state}), restarting...")
                last_change = now
                if self._running:
                    self.restart("stall")

    def _hourly_loop(self):
        """Fires a clean restart every HOURLY_RESTART_S seconds."""
        time.sleep(HOURLY_RESTART_S)
        while self._running:
            next_restart = datetime.datetime.now() + datetime.timedelta(seconds=HOURLY_RESTART_S)
            log(f"[SCHEDULED HOURLY RESTART] Triggering restart now. "
                f"Next scheduled restart at {next_restart.strftime('%H:%M:%S')}")
            self.restart("hourly")
            time.sleep(HOURLY_RESTART_S)


# ── macOS runner ──────────────────────────────────────────────────────────────

def run_mac():
    log("[STARTUP] Platform: macOS")

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # Prevent display sleep
    try:
        import ctypes as _ct
        _iokit = _ct.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
        _iokit.IOPMAssertionCreateWithName.restype  = _ct.c_uint32
        _iokit.IOPMAssertionCreateWithName.argtypes = [
            _ct.c_void_p, _ct.c_uint32, _ct.c_void_p, _ct.POINTER(_ct.c_uint32)
        ]
        from CoreFoundation import CFStringCreateWithCString, kCFStringEncodingUTF8
        _name = CFStringCreateWithCString(None, b"RTSPViewer active", kCFStringEncodingUTF8)
        _type = CFStringCreateWithCString(None, b"PreventUserIdleDisplaySleep", kCFStringEncodingUTF8)
        _aid  = _ct.c_uint32(0)
        _iokit.IOPMAssertionCreateWithName(_type, 255, _name, _ct.byref(_aid))
        log("[STARTUP] Display sleep prevention active (IOPMAssertion)")
    except Exception as e:
        log(f"[STARTUP] Could not prevent display sleep: {e}")

    screen      = NSScreen.mainScreen()
    screen_rect = screen.frame()

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen_rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )
    window.setBackgroundColor_(NSColor.blackColor())
    window.setLevel_(25)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    content_view = window.contentView()

    vlc_args = [
        "--no-audio",
        "--network-caching={}".format(CACHE_MS),
        "--live-caching={}".format(CACHE_MS),
        "--no-video-title-show",
        "--quiet",
        "--vout=macosx",
        "--codec=avcodec,any",
        "--avcodec-hw=any",
        "--no-mouse-events",
    ]
    instance = vlc.Instance(*vlc_args)
    player   = instance.media_player_new()
    player.set_nsobject(objc.pyobjc_id(content_view))

    mgr = StreamManager(instance, player)
    BrightnessManager()
    mgr.start(reason="startup")

    from AppKit import NSDate, NSRunLoop, NSDefaultRunLoopMode
    loop = NSRunLoop.currentRunLoop()
    try:
        while True:
            loop.runMode_beforeDate_(
                NSDefaultRunLoopMode,
                NSDate.dateWithTimeIntervalSinceNow_(0.1),
            )
    except KeyboardInterrupt:
        log("[SHUTDOWN] Interrupted by user")
        mgr.stop()


# ── Windows runner ────────────────────────────────────────────────────────────

class WindowsViewer:
    def __init__(self):
        log("[STARTUP] Platform: Windows")

        vlc_args = [
            "--no-audio",
            "--network-caching={}".format(CACHE_MS),
            "--live-caching={}".format(CACHE_MS),
            "--no-video-title-show",
            "--quiet",
            "--video-on-top",
            "--codec=avcodec,any",
            "--avcodec-hw=dxva2",
            "--no-mouse-events",
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        log("[STARTUP] Display sleep prevention active (SetThreadExecutionState)")

        self._boost_brightness()

        self.mgr = StreamManager(self.instance, self.player)
        BrightnessManager()

        time.sleep(1.0)
        self.mgr.start(reason="startup")
        time.sleep(1.5)
        self.player.set_fullscreen(True)

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log("[SHUTDOWN] Interrupted by user")
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            self.mgr.stop()

    def _boost_brightness(self):
        """Read current brightness via WMI and set it to current + 20%, capped at 100."""
        try:
            import subprocess
            # Read current brightness
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"],
                capture_output=True, text=True, timeout=5
            )
            current = int(result.stdout.strip())
            target  = min(100, current + 20)
            # Set new brightness
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
                 f".WmiSetBrightness(1,{target})"],
                capture_output=True, timeout=5
            )
            log(f"[STARTUP] Brightness: {current}% → {target}% (+20%)")
        except Exception as e:
            log(f"[STARTUP] Could not adjust brightness: {e}")


# ── Linux runner ──────────────────────────────────────────────────────────────

class LinuxViewer:
    def __init__(self):
        log("[STARTUP] Platform: Linux")

        vlc_args = [
            "--no-audio",
            "--network-caching={}".format(CACHE_MS),
            "--live-caching={}".format(CACHE_MS),
            "--no-video-title-show",
            "--quiet",
            "--video-on-top",
            "--fullscreen",
            "--codec=avcodec,any",
            "--avcodec-hw=vaapi",
            "--no-mouse-events",
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._inhibit_proc = None
        try:
            import subprocess
            self._inhibit_proc = subprocess.Popen([
                "systemd-inhibit",
                "--what=idle:sleep:handle-lid-switch",
                "--who=RTSPViewer",
                "--why=Displaying RTSP stream",
                "--mode=block",
                "sleep", "infinity"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("[STARTUP] Display sleep prevention active (systemd-inhibit)")
        except Exception as e:
            log(f"[STARTUP] Could not prevent display sleep: {e}")

        self.mgr = StreamManager(self.instance, self.player)
        BrightnessManager()

        time.sleep(0.5)
        self.mgr.start(reason="startup")

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log("[SHUTDOWN] Interrupted by user")
            self.mgr.stop()
            if self._inhibit_proc:
                self._inhibit_proc.terminate()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log(f"[STARTUP] RTSP Viewer starting — stream: {RTSP_URL}")
    log(f"[STARTUP] Cache: {CACHE_MS}ms | Stall timeout: {UDP_STALL_SEC}s | "
        f"Hourly restart: every {HOURLY_RESTART_S//60}min")

    if IS_MAC:
        run_mac()
    elif IS_WINDOWS:
        WindowsViewer()
    else:
        LinuxViewer()