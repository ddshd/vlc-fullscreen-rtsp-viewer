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

    WH_MOUSE_LL      = 14
    WM_LBUTTONDBLCLK = 0x0203
    WM_APPCOMMAND    = 0x0319
    # APPCOMMAND values (high word of lParam >> 4)
    APPCOMMAND_VOLUME_UP   = 10
    APPCOMMAND_VOLUME_DOWN = 9

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt",          wintypes.POINT),
            ("mouseData",   wintypes.DWORD),
            ("flags",       wintypes.DWORD),
            ("time",        wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    HOOKPROC    = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    WNDPROCTYPE = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

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
            "--no-mouse-events",         # Disable double-click fullscreen toggle
        ]
        self.instance = vlc.Instance(*vlc_args)
        self.player   = self.instance.media_player_new()
        self.player.set_fullscreen(True)

        self._running         = True
        self._restart_lock    = threading.Lock()
        self._hook_id_mouse   = None
        self._hook_proc_mouse = HOOKPROC(self._mouse_hook)
        self._wnd_proc_ref    = None  # keep WNDPROCTYPE alive

        ES_CONTINUOUS       = 0x80000000
        ES_SYSTEM_REQUIRED  = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )

        threading.Thread(target=self._watch_loop, daemon=True).start()
        threading.Thread(target=self._run_hooks,  daemon=True).start()

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

    # ── Mouse hook: suppress double-clicks ───────────────────────────────
    def _mouse_hook(self, nCode, wParam, lParam):
        if nCode >= 0 and wParam == WM_LBUTTONDBLCLK:
            return 1
        return ctypes.windll.user32.CallNextHookEx(
            self._hook_id_mouse, nCode, wParam, lParam
        )

    # ── Window proc: catches WM_APPCOMMAND (volume buttons) ──────────────
    def _wnd_proc(self, hwnd, msg, wParam, lParam):
        if msg == WM_APPCOMMAND:
            cmd = (lParam >> 16) & 0xFFF
            if cmd in (APPCOMMAND_VOLUME_UP, APPCOMMAND_VOLUME_DOWN):
                threading.Thread(target=self.restart_stream, daemon=True).start()
                return 1  # Suppress — don't change system volume
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wParam, lParam)

    def _run_hooks(self):
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hMod     = kernel32.GetModuleHandleW(None)

        # Low-level mouse hook — suppress double-click fullscreen toggle
        self._hook_id_mouse = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._hook_proc_mouse, hMod, 0
        )

        # Register a minimal window class to receive WM_APPCOMMAND
        self._wnd_proc_ref = WNDPROCTYPE(self._wnd_proc)

        class _WNDCLASSEX(ctypes.Structure):
            _fields_ = [
                ("cbSize",        wintypes.UINT),
                ("style",         wintypes.UINT),
                ("lpfnWndProc",   WNDPROCTYPE),
                ("cbClsExtra",    ctypes.c_int),
                ("cbWndExtra",    ctypes.c_int),
                ("hInstance",     wintypes.HANDLE),
                ("hIcon",         wintypes.HANDLE),
                ("hCursor",       wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName",  ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm",       wintypes.HANDLE),
            ]

        wc              = _WNDCLASSEX()
        wc.cbSize       = ctypes.sizeof(_WNDCLASSEX)
        wc.lpfnWndProc  = self._wnd_proc_ref
        wc.hInstance    = hMod
        wc.lpszClassName = "RTSPAppCmd"
        user32.RegisterClassExW(ctypes.byref(wc))

        # HWND_MESSAGE (-3) = message-only window, invisible, no taskbar entry
        HWND_MESSAGE = ctypes.cast(-3, wintypes.HWND)
        hwnd = user32.CreateWindowExW(
            0, "RTSPAppCmd", None, 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hMod, None
        )

        msg = wintypes.MSG()
        while self._running:
            ret = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if ret:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)

        if self._hook_id_mouse:
            user32.UnhookWindowsHookEx(self._hook_id_mouse)
        if hwnd:
            user32.DestroyWindow(hwnd)

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