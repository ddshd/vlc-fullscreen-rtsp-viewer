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

    WM_LBUTTONDBLCLK       = 0x0203
    WM_APPCOMMAND          = 0x0319
    APPCOMMAND_VOLUME_UP   = 10
    APPCOMMAND_VOLUME_DOWN = 9

    WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

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

CACHE_MS      = 1000  # Network/live cache in milliseconds
RETRY_SEC     = 5     # Seconds to wait before reconnecting on failure
UDP_STALL_SEC = 5     # Seconds of frozen playback clock before forcing restart


# ── macOS Cocoa app ───────────────────────────────────────────────────────────

def run_mac():
    """Create a fullscreen Cocoa window, hand it to VLC, run the event loop."""

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # Prevent display sleep and screensaver on macOS
    try:
        import ctypes as _ctypes
        _iokit = _ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        _iokit.IOPMAssertionCreateWithName.restype  = _ctypes.c_uint32
        _iokit.IOPMAssertionCreateWithName.argtypes = [
            _ctypes.c_void_p, _ctypes.c_uint32, _ctypes.c_void_p, _ctypes.POINTER(_ctypes.c_uint32)
        ]
        from CoreFoundation import CFStringCreateWithCString, kCFStringEncodingUTF8
        _assertion_name = CFStringCreateWithCString(
            None, b"RTSPViewer preventing sleep", kCFStringEncodingUTF8
        )
        _assertion_type = CFStringCreateWithCString(
            None, b"PreventUserIdleDisplaySleep", kCFStringEncodingUTF8
        )
        _assertion_id   = _ctypes.c_uint32(0)
        _iokit.IOPMAssertionCreateWithName(
            _assertion_type, 255, _assertion_name,
            _ctypes.byref(_assertion_id)
        )
    except Exception:
        pass  # Non-fatal — viewer still works without this

    screen      = NSScreen.mainScreen()
    screen_rect = screen.frame()

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen_rect,
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
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
        "--no-mouse-events",         # Disable double-tap/click fullscreen toggle
    ]
    instance = vlc.Instance(*vlc_args)
    player   = instance.media_player_new()
    player.set_nsobject(objc.pyobjc_id(content_view))

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
        media.add_option(":rtsp-udp")
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
    start_stream()

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
            "--avcodec-hw=dxva2",
            "--no-mouse-events",
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._running          = True
        self._restart_lock     = threading.Lock()
        self._wnd_proc_ref     = None   # keep WNDPROCTYPE ref alive
        self._orig_wnd_proc    = None   # original VLC WndProc (for CallWindowProc)
        self._vlc_hwnd         = None

        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )

        threading.Thread(target=self._watch_loop, daemon=True).start()

        time.sleep(1.0)
        self._start_stream()

        # Subclass VLC's window once it exists so we intercept its messages
        self._subclass_vlc_window()

        self._main_loop()

    # ── Stream control ────────────────────────────────────────────────────

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
        # Re-subclass after each restart since VLC recreates its window
        if self._vlc_hwnd:
            self._subclass_vlc_window()

    def restart_stream(self):
        if not self._restart_lock.acquire(blocking=False):
            return
        try:
            self._start_stream()
            time.sleep(8)
        finally:
            self._restart_lock.release()

    # ── Find and subclass VLC's window ────────────────────────────────────

    def _find_vlc_hwnd(self):
        """Find VLC's top-level video window by enumerating windows."""
        user32    = ctypes.windll.user32
        found     = ctypes.c_ulong(0)
        pid       = ctypes.windll.kernel32.GetCurrentProcessId()

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum(hwnd, _):
            wp = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
            if wp.value == pid:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                # VLC's DirectX video window class name
                if buf.value in ("DirectDrawDeviceWnd", "VLC DirectX", "VLC video output"):
                    found.value = hwnd
                    return False  # stop enumeration
            return True

        cb = WNDENUMPROC(_enum)
        user32.EnumWindows(cb, 0)
        return wintypes.HWND(found.value) if found.value else None

    def _subclass_vlc_window(self):
        """Replace VLC's WndProc with ours so we intercept WM_APPCOMMAND etc."""
        user32 = ctypes.windll.user32

        # Wait up to 5 s for VLC to create its window
        for _ in range(50):
            hwnd = self._find_vlc_hwnd()
            if hwnd:
                break
            time.sleep(0.1)
        else:
            return  # Couldn't find it — non-fatal

        self._vlc_hwnd      = hwnd
        self._wnd_proc_ref  = WNDPROCTYPE(self._vlc_wnd_proc)

        # SetWindowLongPtrW(GWLP_WNDPROC = -4) replaces the window proc
        GWLP_WNDPROC = -4
        user32.SetWindowLongPtrW.restype  = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        orig = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._wnd_proc_ref)
        self._orig_wnd_proc = ctypes.c_void_p(orig)

    # ── Subclassed window proc ────────────────────────────────────────────

    def _vlc_wnd_proc(self, hwnd, msg, wParam, lParam):
        user32 = ctypes.windll.user32

        # Volume buttons → restart stream
        if msg == WM_APPCOMMAND:
            cmd = (lParam >> 16) & 0xFFF
            if cmd in (APPCOMMAND_VOLUME_UP, APPCOMMAND_VOLUME_DOWN):
                threading.Thread(target=self.restart_stream, daemon=True).start()
                return 1  # Suppress — don't change system volume

        # Double-click → suppress (don't let VLC toggle fullscreen)
        if msg == WM_LBUTTONDBLCLK:
            return 0

        # All other messages → pass to original VLC proc
        if self._orig_wnd_proc:
            return user32.CallWindowProcW(
                self._orig_wnd_proc, hwnd, msg, wParam, ctypes.c_long(lParam)
            )
        return user32.DefWindowProcW(hwnd, msg, wParam, ctypes.c_long(lParam))

    # ── Loops ─────────────────────────────────────────────────────────────

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

    def shutdown(self):
        self._running = False
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
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
            "--avcodec-hw=vaapi",
            "--no-mouse-events",         # Disable double-click fullscreen toggle
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._running      = True
        self._restart_lock = threading.Lock()

        # Prevent display sleep on Linux — try common inhibitors
        self._inhibit_proc = None
        try:
            import subprocess
            # systemd-inhibit keeps sleep/idle blocked for our lifetime
            self._inhibit_proc = subprocess.Popen([
                "systemd-inhibit",
                "--what=idle:sleep:handle-lid-switch",
                "--who=RTSPViewer",
                "--why=Displaying RTSP stream",
                "--mode=block",
                "sleep", "infinity"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass  # Non-fatal

        threading.Thread(target=self._watch_loop, daemon=True).start()

        time.sleep(0.5)
        self._start_stream()
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.player.stop()
            if self._inhibit_proc:
                self._inhibit_proc.terminate()

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