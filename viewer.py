"""
RTSP Fullscreen Viewer
- Plays an RTSP stream fullscreen with no audio
- Auto-restarts on failure
- Launches on startup (see README)
- Windows only: hold the Windows button for 1 second to manually restart the stream
- macOS: requires pyobjc  (pip install pyobjc-framework-Cocoa)
"""

import sys
import time
import threading

IS_WINDOWS = sys.platform == "win32"
IS_MAC     = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")

# ── Windows-only imports ──────────────────────────────────────────────────────
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN     = 0x0100
    WM_SYSKEYDOWN  = 0x0104
    VK_LWIN        = 0x5B
    VK_RWIN        = 0x5C

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode",      wintypes.DWORD),
            ("scanCode",    wintypes.DWORD),
            ("flags",       wintypes.DWORD),
            ("time",        wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

# ── macOS-only imports ────────────────────────────────────────────────────────
if IS_MAC:
    try:
        from AppKit import (
            NSApplication, NSWindow, NSView, NSScreen,
            NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
            NSApplicationActivationPolicyRegular, NSColor,
        )
        from Foundation import NSRect, NSPoint, NSSize
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
CACHE_MS     = 1000  # Network/live cache in milliseconds
RETRY_SEC    = 5     # Seconds to wait before reconnecting on failure
WIN_HOLD_SEC = 1.0   # Seconds to hold the Windows button to trigger restart
# UDP: if no video frame arrives within this many seconds, force a restart
UDP_STALL_SEC = 5


# ── macOS Cocoa app ───────────────────────────────────────────────────────────

def run_mac():
    """Create a fullscreen Cocoa window, hand it to VLC, run the event loop."""

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # Full screen rect from main display
    screen      = NSScreen.mainScreen()
    screen_rect = screen.frame()

    # Borderless black window covering the whole screen
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen_rect,
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    window.setBackgroundColor_(NSColor.blackColor())
    window.setLevel_(25)          # Above everything (kCGMaximumWindowLevel-ish)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    # The content view is what we hand to VLC
    content_view = window.contentView()

    # ── VLC ───────────────────────────────────────────────────────────────
    vlc_args = [
        "--no-audio",
        "--network-caching={}".format(CACHE_MS),
        "--live-caching={}".format(CACHE_MS),
        "--no-video-title-show",
        "--quiet",
        "--vout=macosx",
        "--codec=avcodec,any",
        "--avcodec-hw=any",          # VideoToolbox on macOS
    ]
    instance = vlc.Instance(*vlc_args)
    player   = instance.media_player_new()

    # Give VLC the NSView pointer as an integer
    player.set_nsobject(objc.pyobjc_id(content_view))

    # ── State & threads ───────────────────────────────────────────────────
    running      = threading.Event()
    running.set()
    restart_lock = threading.Lock()

    def start_stream():
        try:
            player.stop()
        except Exception:
            pass
        media = instance.media_new(RTSP_URL)
        media.add_option(":network-caching={}".format(CACHE_MS))
        media.add_option(":live-caching={}".format(CACHE_MS))
        media.add_option(":no-audio")
        media.add_option(":rtsp-udp")        # UDP: drop frames, don't stall
        player.set_media(media)
        player.play()

    def restart_stream():
        if not restart_lock.acquire(blocking=False):
            return
        try:
            start_stream()
            time.sleep(8)
        finally:
            restart_lock.release()

    def watch_loop():
        time.sleep(8)
        last_time  = player.get_time()
        last_check = time.monotonic()
        while running.is_set():
            time.sleep(2)
            state = player.get_state()
            # Hard error / stop states
            if state in (
                vlc.State.Ended,
                vlc.State.Error,
                vlc.State.NothingSpecial,
                vlc.State.Stopped,
            ):
                time.sleep(RETRY_SEC)
                if running.is_set():
                    restart_stream()
                last_time  = -1
                last_check = time.monotonic()
                continue
            # UDP stall detection: playback clock frozen = stream dropped
            current_time = player.get_time()
            now          = time.monotonic()
            if current_time != last_time:
                last_time  = current_time
                last_check = now
            elif state == vlc.State.Playing and (now - last_check) > UDP_STALL_SEC:
                last_check = now
                if running.is_set():
                    restart_stream()

    threading.Thread(target=watch_loop, daemon=True).start()

    # Start playing
    start_stream()

    # Run the Cocoa event loop (handles window, keyboard, etc.)
    from AppKit import NSDate, NSRunLoop, NSDefaultRunLoopMode
    loop = NSRunLoop.currentRunLoop()
    while running.is_set():
        loop.runMode_beforeDate_(
            NSDefaultRunLoopMode,
            NSDate.dateWithTimeIntervalSinceNow_(0.1),
        )


# ── Windows runner ────────────────────────────────────────────────────────────

class WindowsViewer:
    def __init__(self):
        vlc_args = [
            "--no-audio",
            "--network-caching={}".format(CACHE_MS),
            "--live-caching={}".format(CACHE_MS),
            "--no-video-title-show",
            "--quiet",
            "--video-on-top",
            "--codec=avcodec,any",
            "--avcodec-hw=dxva2",    # DirectX Video Acceleration on Windows
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._running      = True
        self._restart_lock = threading.Lock()
        self._win_pressed_at = None
        self._win_hold_fired = False
        self._hook_id        = None
        self._hook_proc      = HOOKPROC(self._keyboard_hook)

        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._run_hook,   daemon=True).start()

        time.sleep(1.0)
        self._start_stream()
        self._main_loop()

    def _start_stream(self):
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
        time.sleep(1.5)
        self.player.set_fullscreen(True)

    def restart_stream(self):
        if not self._restart_lock.acquire(blocking=False):
            return
        try:
            self._start_stream()
            time.sleep(8)
        finally:
            self._restart_lock.release()

    def _main_loop(self):
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()

    def _watch_loop(self):
        time.sleep(8)
        last_time  = self.player.get_time()
        last_check = time.monotonic()
        while self._running:
            time.sleep(2)
            state = self.player.get_state()
            if state in (
                vlc.State.Ended,
                vlc.State.Error,
                vlc.State.NothingSpecial,
                vlc.State.Stopped,
            ):
                time.sleep(RETRY_SEC)
                if self._running:
                    self.restart_stream()
                last_time  = -1
                last_check = time.monotonic()
                continue
            current_time = self.player.get_time()
            now          = time.monotonic()
            if current_time != last_time:
                last_time  = current_time
                last_check = now
            elif state == vlc.State.Playing and (now - last_check) > UDP_STALL_SEC:
                last_check = now
                if self._running:
                    self.restart_stream()

    def _keyboard_hook(self, nCode, wParam, lParam):
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            if vk in (VK_LWIN, VK_RWIN):
                if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    now = time.monotonic()
                    if self._win_pressed_at is None:
                        self._win_pressed_at = now
                        self._win_hold_fired = False
                    elif not self._win_hold_fired:
                        if now - self._win_pressed_at >= WIN_HOLD_SEC:
                            self._win_hold_fired = True
                            threading.Thread(
                                target=self.restart_stream, daemon=True
                            ).start()
                    return 1
                else:
                    self._win_pressed_at = None
                    self._win_hold_fired = False
        return ctypes.windll.user32.CallNextHookEx(
            self._hook_id, nCode, wParam, lParam
        )

    def _run_hook(self):
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hMod     = kernel32.GetModuleHandleW(None)
        self._hook_id = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, hMod, 0
        )
        if not self._hook_id:
            return
        msg = wintypes.MSG()
        while self._running:
            ret = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if ret:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
        user32.UnhookWindowsHookEx(self._hook_id)

    def shutdown(self):
        self._running = False
        try:
            self.player.stop()
        except Exception:
            pass


# ── Linux runner ──────────────────────────────────────────────────────────────

class LinuxViewer:
    def __init__(self):
        vlc_args = [
            "--no-audio",
            "--network-caching={}".format(CACHE_MS),
            "--live-caching={}".format(CACHE_MS),
            "--no-video-title-show",
            "--quiet",
            "--video-on-top",
            "--fullscreen",
            "--codec=avcodec,any",
            "--avcodec-hw=vaapi",    # VA-API on Linux
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._running      = True
        self._restart_lock = threading.Lock()

        threading.Thread(target=self._watch_loop, daemon=True).start()

        time.sleep(0.5)
        self._start_stream()
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.player.stop()

    def _start_stream(self):
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

    def restart_stream(self):
        if not self._restart_lock.acquire(blocking=False):
            return
        try:
            self._start_stream()
            time.sleep(8)
        finally:
            self._restart_lock.release()

    def _watch_loop(self):
        time.sleep(8)
        last_time  = self.player.get_time()
        last_check = time.monotonic()
        while self._running:
            time.sleep(2)
            state = self.player.get_state()
            if state in (
                vlc.State.Ended,
                vlc.State.Error,
                vlc.State.NothingSpecial,
                vlc.State.Stopped,
            ):
                time.sleep(RETRY_SEC)
                if self._running:
                    self.restart_stream()
                last_time  = -1
                last_check = time.monotonic()
                continue
            current_time = self.player.get_time()
            now          = time.monotonic()
            if current_time != last_time:
                last_time  = current_time
                last_check = now
            elif state == vlc.State.Playing and (now - last_check) > UDP_STALL_SEC:
                last_check = now
                if self._running:
                    self.restart_stream()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if IS_MAC:
        run_mac()
    elif IS_WINDOWS:
        WindowsViewer()
    else:
        LinuxViewer()