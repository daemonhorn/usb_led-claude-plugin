#!/usr/bin/env python3
"""
USB LED / signal light controller for Claude Code hooks.
Usage: python usb_led.py <event>
Events: notification, stop, working, pre_tool, post_tool, idle, off
        touch_listen [--timeout N]   (background touch-to-approve daemon)
Supported drivers: patlite | luxafor | blink1  (set via config.yaml device.driver)
"""
import sys
import os
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")

# LED byte for Patlite: upper nibble = color, lower nibble = pattern
COLORS = {
    "off":    0x0,
    "red":    0x1,
    "green":  0x2,
    "amber":  0x3,
    "yellow": 0x3,
    "blue":   0x4,
    "purple": 0x5,
    "cyan":   0x6,
    "white":  0x7,
}
PATTERNS = {
    "off":    0x0,
    "solid":  0x1,
    "flash":  0x2,
    "flash2": 0x3,
    "pulse":  0x4,
    "pulse2": 0x5,
    "pulse3": 0x6,
    "pulse4": 0x7,
}

# Buzzer byte 3: bits[7:4] = repeat count (0=continuous, 1-14=N times, 0xF=keep)
#                bits[3:0] = sound pattern
# Buzzer byte 4: 0x0=mute, 0x1-0xA=volume steps, 0xF=keep current
BUZZER_PATTERNS = {
    "off":          0x0,
    "continuous":   0x1,
    "sweep":        0x2,
    "intermittent": 0x3,
    "weak":         0x4,
    "strong":       0x5,
    "star":         0x6,
    "london":       0x7,
    "keep":         0xF,
}

# RGB color table for devices that use raw RGB values (Luxafor, blink(1)).
RGB_COLORS = {
    "off":    (0,   0,   0),
    "red":    (255, 0,   0),
    "green":  (0,   200, 0),
    "amber":  (255, 80,  0),
    "yellow": (255, 180, 0),
    "blue":   (0,   0,   255),
    "purple": (128, 0,   128),
    "cyan":   (0,   200, 200),
    "white":  (255, 255, 255),
}

# Default VID/PID per driver — used when config.yaml omits device.vid/pid.
DRIVER_DEFAULTS = {
    "patlite": {"vid": 0x191A, "pid": 0x6001},
    "luxafor":  {"vid": 0x04D8, "pid": 0xF372},
    "blink1":   {"vid": 0x27B8, "pid": 0x01ED},
}

# GETSTATE command: asks the device to report current touch sensor state.
# Response: [status_byte, state_byte]; touch active when state_byte & 1 == 1.
_GETSTATE_CMD = [0x00, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

# Lock file prevents multiple simultaneous touch listeners.
_LOCK_FILE = os.path.join(tempfile.gettempdir(), "usb_led_touch_listen.pid")
# Cancel sentinel: written by any hook event that means "the prompt is gone".
_CANCEL_FILE = os.path.join(tempfile.gettempdir(), "usb_led_touch_cancel")


# ── HID compatibility layer ──────────────────────────────────────────────────
# pip 'hidapi' installs as module 'hid'; Debian's python3-hidapi installs as
# module 'hidapi' with a different API.  _get_hid() normalises both so the
# rest of the code uses a single interface.

def _get_hid():
    """Return a hid-compatible object, or None if no HID library is available."""
    try:
        import hid
        return hid
    except ImportError:
        pass
    try:
        import hidapi as _lib
        return _HidapiFacade(_lib)
    except ImportError:
        return None


class _HidapiFacade:
    """Adapts Debian's python3-hidapi (cffi) to match the pip 'hid' package API."""
    def __init__(self, lib):
        self._lib = lib

    def enumerate(self):
        return [
            {
                "vendor_id": d.vendor_id,
                "product_id": d.product_id,
                "manufacturer_string": d.manufacturer_string or "",
                "product_string": d.product_string or "",
            }
            for d in self._lib.enumerate()
        ]

    def device(self):
        return _HidapiDeviceFacade(self._lib)


class _HidapiDeviceFacade:
    """Wraps hidapi.Device to match hid.device() open/write/read/close semantics."""
    def __init__(self, lib):
        self._lib = lib
        self._dev = None

    def open(self, vid, pid):
        self._dev = self._lib.Device(vendor_id=vid, product_id=pid)

    def write(self, data):
        # pip hid: data[0] is the report_id, data[1:] is the payload.
        # Debian hidapi: write(payload_bytes, report_id=bytes([rid])).
        self._dev.write(bytes(data[1:]), report_id=bytes([data[0]]))

    def send_feature_report(self, data):
        # pip hid: data[0] is the report_id, data[1:] is the payload.
        # Debian hidapi cffi: report_id must be bytes of length 1 for buffer assignment.
        self._dev.send_feature_report(bytes(data[1:]), report_id=bytes([data[0]]))

    def get_feature_report(self, report_id, length):
        # Debian hidapi: get_feature_report(report_id_bytes, length) -> bytes WITHOUT report_id.
        # pip hid convention (what callers expect): result[0] == report_id.
        result = self._dev.get_feature_report(bytes([report_id]), length)
        if result is None:
            return []
        return [report_id] + list(result)

    def read(self, size, timeout_ms=0):
        result = self._dev.read(size, timeout_ms=timeout_ms)
        return list(result) if result is not None else []

    def close(self):
        if self._dev is not None:
            self._dev.close()
            self._dev = None


def load_config():
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"usb_led: failed to load config: {e}", file=sys.stderr)
        sys.exit(1)


def _detect_driver(config) -> str:
    return str(config.get("device", {}).get("driver", "patlite")).lower()


# ── Patlite signal encoding ──────────────────────────────────────────────────

def build_led_byte(color_name, pattern_name) -> int:
    color = COLORS.get(str(color_name).lower(), 0x0)
    pattern = PATTERNS.get(str(pattern_name).lower(), 0x0)
    return (color << 4) | pattern


def build_buzzer_bytes(buzzer_name, volume) -> "tuple[int, int]":
    """
    Return (byte3, byte4) for the HID buzzer fields.

    byte3: (repeat << 4) | pattern  — repeat=0 means continuous; 0xFF = keep all
    byte4: 0x0=mute, 0x1–0xA=volume levels, 0xF=keep current
    """
    pat = BUZZER_PATTERNS.get(str(buzzer_name).lower(), 0xF)

    if pat == 0xF:              # "keep" — don't touch buzzer state
        bz_byte = 0xFF
        vol_byte = 0x0F
    elif pat == 0x0:            # "off" — silence, preserve volume
        bz_byte = 0x00
        vol_byte = 0x0F
    else:                       # active pattern, repeat=0 (continuous)
        bz_byte = pat           # (0x0 << 4) | pat
        if volume is None or str(volume).lower() == "keep":
            vol_byte = 0x0F
        elif str(volume).lower() == "off":
            vol_byte = 0x00
        else:
            try:
                vol_byte = max(0, min(10, int(volume)))
            except (ValueError, TypeError):
                vol_byte = 0x0F

    return bz_byte, vol_byte


def _send_patlite(dev, event_cfg):
    color = event_cfg.get("color", "off")
    pattern = event_cfg.get("pattern", "off")
    led_byte = build_led_byte(color, pattern)
    bz_byte, vol_byte = build_buzzer_bytes(
        event_cfg.get("buzzer", "keep"),
        event_cfg.get("volume", "keep"),
    )
    dev.write([0x00, 0x00, 0x00, bz_byte, vol_byte, led_byte, 0x00, 0x00, 0x00])


# ── Luxafor driver (VID 0x04D8 / PID 0xF372) ────────────────────────────────
# 9-byte Output Reports sent via dev.write().
# Byte layout: [report_id, cmd, target, R, G, B, arg1, arg2, arg3]
#   cmd 0x01 solid:  target=0xFF, RGB, unused×3
#   cmd 0x03 strobe: target=0xFF, RGB, speed(0=fast), unused, repeat(0=∞)
#   cmd 0x04 wave:   wave_type(1-5), RGB, unused, repeat(0=∞), speed(0=fast)

def _send_luxafor(dev, color_name, pattern_name):
    r, g, b = RGB_COLORS.get(str(color_name).lower(), (0, 0, 0))
    pat = str(pattern_name).lower()

    if pat == "off" or str(color_name).lower() == "off":
        dev.write([0x00, 0x01, 0xFF, 0, 0, 0, 0, 0, 0])
    elif pat in ("flash", "flash2"):
        speed = 8 if pat == "flash" else 24
        dev.write([0x00, 0x03, 0xFF, r, g, b, speed, 0x00, 0])  # strobe, infinite
    elif pat in ("pulse", "pulse2", "pulse3", "pulse4"):
        speed = {"pulse": 48, "pulse2": 36, "pulse3": 24, "pulse4": 16}[pat]
        dev.write([0x00, 0x04, 1, r, g, b, 0x00, 0, speed])     # wave type 1, infinite
    else:  # solid
        dev.write([0x00, 0x01, 0xFF, r, g, b, 0, 0, 0])


def _luxafor_off(dev):
    dev.write([0x00, 0x01, 0xFF, 0, 0, 0, 0, 0, 0])


# ── blink(1) driver (VID 0x27B8 / PID 0x01ED) ───────────────────────────────
# 8-byte (mk1) or 9-byte (mk2) USB Feature Reports sent via send_feature_report().
# All reports start with report_id=0x01.
#
# 'n' (0x6e) fade_to_rgb: [0x01, 0x6e, R, G, B, th, tl, ledn]
#    th/tl = fade_ms//10 as big-endian uint16; ledn=0 → all LEDs
# 'P' (0x50) write_pattern_line:
#    mk1: [0x01, 0x50, th, tl, R, G, B, pos]
#    mk2: [0x01, 0x50, R, G, B, th, tl, pos, ledn]
# 'p' (0x70) play: [0x01, 0x70, play, start, end, count, 0, 0]
#    play=1 start / 0 stop; count=0 loops forever
# 'v' (0x76) version: [0x01, 0x76, 0, 0, 0, 0, 0, 0]
#    response bytes 3-4 are ASCII major/minor version digits

def _blink1_is_mk2(dev) -> bool:
    """Return True if the connected blink(1) is mk2 firmware (v2.0+)."""
    try:
        dev.send_feature_report([0x01, 0x76, 0, 0, 0, 0, 0, 0])
        resp = dev.get_feature_report(0x01, 9)
        if resp and len(resp) >= 5:
            return (resp[3] - ord('0')) >= 2
    except Exception:
        pass
    return False


def _blink1_write_pattern(dev, is_mk2, ms, r, g, b, pos):
    t = ms // 10
    th, tl = t >> 8, t & 0xFF
    if is_mk2:
        dev.send_feature_report([0x01, 0x50, r, g, b, th, tl, pos, 0])
    else:
        dev.send_feature_report([0x01, 0x50, th, tl, r, g, b, pos])


def _send_blink1(dev, color_name, pattern_name):
    r, g, b = RGB_COLORS.get(str(color_name).lower(), (0, 0, 0))
    pat = str(pattern_name).lower()

    if pat == "off" or str(color_name).lower() == "off":
        dev.send_feature_report([0x01, 0x70, 0, 0, 0, 0, 0, 0])   # stop pattern
        dev.send_feature_report([0x01, 0x6e, 0, 0, 0, 0, 0, 0])   # fade to off
        return

    is_mk2 = _blink1_is_mk2(dev)

    if pat in ("flash", "flash2", "pulse", "pulse2", "pulse3", "pulse4"):
        # Write a 2-frame loop into pattern RAM (color on → off) and play it.
        ms = {
            "flash":  300, "flash2": 600,
            "pulse":  800, "pulse2": 600, "pulse3": 400, "pulse4": 300,
        }[pat]
        _blink1_write_pattern(dev, is_mk2, ms, r, g, b, 0)         # frame 0: color
        _blink1_write_pattern(dev, is_mk2, ms, 0, 0, 0, 1)         # frame 1: off
        dev.send_feature_report([0x01, 0x70, 1, 0, 1, 0, 0, 0])    # play 0→1, infinite
    else:  # solid
        dev.send_feature_report([0x01, 0x70, 0, 0, 0, 0, 0, 0])    # stop any pattern
        dev.send_feature_report([0x01, 0x6e, r, g, b, 0, 0, 0])    # set color immediately


def _blink1_off(dev):
    dev.send_feature_report([0x01, 0x70, 0, 0, 0, 0, 0, 0])    # stop pattern
    dev.send_feature_report([0x01, 0x6e, 0, 0, 0, 0, 0, 0])    # fade to off


# ── device open ──────────────────────────────────────────────────────────────

def _parse_vid_pid(raw) -> int:
    """Parse a VID/PID value that may be an int or a hex string like '0x191A'."""
    return int(str(raw), 16) if isinstance(raw, str) else int(raw)


def _open_device(config):
    """Open and return the HID device, resolving VID/PID from config or driver defaults."""
    hid = _get_hid()
    device_cfg = config.get("device", {})
    driver = str(device_cfg.get("driver", "patlite")).lower()
    defaults = DRIVER_DEFAULTS.get(driver, DRIVER_DEFAULTS["patlite"])

    vid = _parse_vid_pid(device_cfg.get("vid", defaults["vid"]))

    pid_raw = device_cfg.get("pid", defaults["pid"])
    if pid_raw is None:
        found = [d for d in hid.enumerate() if d["vendor_id"] == vid]
        if not found:
            print(f"usb_led: no device found (VID={hex(vid)})", file=sys.stderr)
            sys.exit(1)
        pid = found[0]["product_id"]
    else:
        pid = _parse_vid_pid(pid_raw)

    dev = hid.device()
    dev.open(vid, pid)
    return dev


# ── signal dispatch ──────────────────────────────────────────────────────────

def send_signal(event: str) -> None:
    config = load_config()

    event_cfg = config.get("events", {}).get(event)
    if event_cfg is None:
        print(f"usb_led: unknown event '{event}'", file=sys.stderr)
        sys.exit(1)

    if _get_hid() is None:
        print("usb_led: hidapi not installed.", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install python3-hidapi", file=sys.stderr)
        print("  Other:         pip install hidapi", file=sys.stderr)
        sys.exit(1)

    driver = _detect_driver(config)

    try:
        dev = _open_device(config)
        if driver == "luxafor":
            _send_luxafor(dev, event_cfg.get("color", "off"), event_cfg.get("pattern", "off"))
        elif driver == "blink1":
            _send_blink1(dev, event_cfg.get("color", "off"), event_cfg.get("pattern", "off"))
        else:  # patlite (default)
            _send_patlite(dev, event_cfg)
        dev.close()
    except Exception as e:
        print(f"usb_led: device error: {e}", file=sys.stderr)
        sys.exit(1)

    # Touch sensor is Patlite-only (NE-WT-USB / NE-ST-USB).
    if event == "notification":
        if driver == "patlite":
            _spawn_touch_listener(config)
    else:
        _cancel_listen()


# ── touch sensor ────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False


def _acquire_lock() -> bool:
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                return False  # another listener already running
        except (ValueError, OSError):
            pass  # stale lock
    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock():
    try:
        os.unlink(_LOCK_FILE)
    except OSError:
        pass


def _cancel_listen() -> None:
    """Signal the running touch listener to stop — prompt was dismissed."""
    try:
        open(_CANCEL_FILE, "w").close()
    except OSError:
        pass


def _check_cancelled() -> bool:
    return os.path.exists(_CANCEL_FILE)


def _find_controlling_tty() -> "tuple[str | None, int | None]":
    """
    Walk the /proc process tree (Linux only) from the current hook invocation
    upward.  Returns (tty_path, terminal_emulator_pid).

    Logic: skip ancestors with no PTY; record the PTY once we find it; the
    first ancestor that drops back to a non-PTY stdin is the terminal emulator.
    """
    if sys.platform != "linux":
        return None, None
    pid = os.getpid()
    seen: set = set()
    tty: "str | None" = None
    in_pty_section = False
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            fd0 = os.readlink(f"/proc/{pid}/fd/0")
            is_pty = fd0.startswith("/dev/pts/") or fd0.startswith("/dev/tty")
        except OSError:
            fd0, is_pty = None, False
        if is_pty and tty is None:
            tty = fd0
            in_pty_section = True
        if in_pty_section and not is_pty:
            return tty, pid   # first non-PTY ancestor after PTY section
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except OSError:
            break
    return tty, None


# ── platform-specific window focus helpers ───────────────────────────────────

def _try_focus_x11_pid(pid: int) -> bool:
    """
    Find the X11 window whose _NET_WM_PID matches pid and request activation
    via EWMH _NET_ACTIVE_WINDOW.  Returns True if a window was found and the
    message was sent (the WM may still deny the raise on Wayland/XWayland).
    Requires python3-xlib; silently returns False if unavailable.
    """
    try:
        from Xlib import display as xdisplay, X
        from Xlib.protocol import event as xevent
        d = xdisplay.Display()
        root = d.screen().root
        NET_WM_PID        = d.intern_atom("_NET_WM_PID")
        NET_CLIENT_LIST   = d.intern_atom("_NET_CLIENT_LIST")
        NET_ACTIVE_WINDOW = d.intern_atom("_NET_ACTIVE_WINDOW")
        client_list = root.get_full_property(NET_CLIENT_LIST, X.AnyPropertyType)
        if not client_list:
            return False
        window = None
        for wid in client_list.value:
            try:
                w = d.create_resource_object("window", wid)
                prop = w.get_full_property(NET_WM_PID, X.AnyPropertyType)
                if prop and len(prop.value) > 0 and prop.value[0] == pid:
                    window = w
                    break
            except Exception:
                continue
        if window is None:
            return False
        ev = xevent.ClientMessage(
            window=window,
            client_type=NET_ACTIVE_WINDOW,
            data=(32, [2, X.CurrentTime, 0, 0, 0]),
        )
        root.send_event(
            ev,
            event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
        )
        d.sync()
        time.sleep(0.05)
        return True
    except Exception:
        return False


def _try_focus_win32_pid(pid: int) -> bool:
    """
    Windows: enumerate visible top-level windows, find one belonging to pid,
    restore and bring it to the foreground.  Returns True if a window was found.
    """
    try:
        import ctypes
        import ctypes.wintypes
        found: list = [None]

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _cb(hwnd: "ctypes.wintypes.HWND", _lp: "ctypes.wintypes.LPARAM") -> bool:
            pid_buf = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
            if (pid_buf.value == pid
                    and ctypes.windll.user32.IsWindowVisible(hwnd)
                    and found[0] is None):
                found[0] = hwnd
                return False
            return True

        ctypes.windll.user32.EnumWindows(_cb, 0)
        if found[0]:
            ctypes.windll.user32.ShowWindow(found[0], 9)   # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(found[0])
            time.sleep(0.05)
            return True
    except Exception:
        pass
    return False


def _try_focus_macos_terminal() -> bool:
    """
    macOS: find the first running terminal app (Terminal, iTerm2, etc.) and
    activate it.  Returns True if a known terminal app was activated.
    """
    import subprocess
    for app in ("Terminal", "iTerm2", "iTerm", "Warp", "Alacritty", "kitty", "Hyper"):
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to '
                 f'(name of processes) contains "{app}"'],
                capture_output=True, text=True, timeout=2,
            )
            if result.stdout.strip() == "true":
                subprocess.run(
                    ["osascript", "-e", f'tell application "{app}" to activate'],
                    capture_output=True, timeout=2,
                )
                time.sleep(0.1)
                return True
        except Exception:
            continue
    return False


def _pynput_inject() -> None:
    """Inject Enter via pynput — goes to whichever window currently has focus."""
    try:
        from pynput.keyboard import Key, Controller
        kb = Controller()
        kb.press(Key.enter)
        kb.release(Key.enter)
    except ImportError:
        print("usb_led: pynput not installed.", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install python3-pynput", file=sys.stderr)
        print("  Other:         pip install pynput", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"usb_led: keystroke injection failed: {e}", file=sys.stderr)
        sys.exit(1)


def _inject_enter(terminal_pid: "int | None" = None) -> None:
    """
    Inject Enter into Claude Code's terminal.

    Strategy (best-effort, degrades gracefully):
      Linux   — try python3-xlib EWMH focus (X11 only; no-op on native Wayland),
                then pynput
      macOS   — try osascript to activate the parent terminal app, then pynput
      Windows — try SetForegroundWindow on the terminal PID's window, then pynput
      all     — pynput as the guaranteed fallback
    """
    if sys.platform == "linux" and terminal_pid:
        _try_focus_x11_pid(terminal_pid)          # no-op on Wayland, works on X11
    elif sys.platform == "darwin":
        _try_focus_macos_terminal()
    elif sys.platform == "win32" and terminal_pid:
        _try_focus_win32_pid(terminal_pid)
    _pynput_inject()


def touch_listen(timeout_s: int = 30, terminal_pid: "int | None" = None) -> None:
    """
    Poll for touch sensor input and inject Enter when detected.
    Spawned as a detached background process by the notification handler.
    Single-instance: exits immediately if another listener is already running.
    Exits early if another hook fires (cancel sentinel written by send_signal).
    """
    if not _acquire_lock():
        return

    if _get_hid() is None:
        _release_lock()
        return

    dev = None
    try:
        config = load_config()
        dev = _open_device(config)
        deadline = time.monotonic() + timeout_s
        last_touched = False

        while time.monotonic() < deadline:
            if _check_cancelled():
                return

            dev.write(_GETSTATE_CMD)
            resp = dev.read(8, timeout_ms=200)
            touched = bool(resp and len(resp) > 1 and (resp[1] & 1))

            if touched and not last_touched:
                # Rising edge — close device before injecting so LED writes can reopen it
                dev.close()
                dev = None
                _inject_enter(terminal_pid)
                return

            last_touched = touched
            time.sleep(0.1)

    except Exception:
        pass
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
        try:
            os.unlink(_CANCEL_FILE)
        except OSError:
            pass
        _release_lock()


def _spawn_touch_listener(config: dict) -> None:
    """Spawn touch_listen as a detached background process (fire-and-forget)."""
    touch_cfg = config.get("touch", {})
    if not touch_cfg.get("enabled", True):
        return
    timeout = int(touch_cfg.get("approval_timeout", 30))

    # Clear any stale cancel sentinel from the previous notification cycle.
    try:
        os.unlink(_CANCEL_FILE)
    except OSError:
        pass

    script = os.path.abspath(__file__)
    cmd = [sys.executable, script, "touch_listen", "--timeout", str(timeout)]

    # Discover the terminal emulator PID now (while still in the hook process
    # tree) and pass it to the detached listener for window focus targeting.
    _tty, terminal_pid = _find_controlling_tty()
    if terminal_pid:
        cmd += ["--terminal-pid", str(terminal_pid)]

    try:
        import subprocess
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                cmd,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(cmd, start_new_session=True, close_fds=True)
    except Exception as e:
        print(f"usb_led: could not start touch listener: {e}", file=sys.stderr)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <event>", file=sys.stderr)
        print("Events: notification, stop, working, pre_tool, post_tool, idle, off", file=sys.stderr)
        print("        touch_listen [--timeout N] [--terminal-pid N]", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]

    if event == "touch_listen":
        timeout = 30
        terminal_pid = None
        if "--timeout" in sys.argv:
            idx = sys.argv.index("--timeout")
            timeout = int(sys.argv[idx + 1])
        if "--terminal-pid" in sys.argv:
            idx = sys.argv.index("--terminal-pid")
            terminal_pid = int(sys.argv[idx + 1])
        touch_listen(timeout_s=timeout, terminal_pid=terminal_pid)
        return

    # "off" is a built-in alias that always turns the light off
    if event == "off":
        import yaml
        cfg = {}
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            pass

        _cancel_listen()
        driver = _detect_driver(cfg)
        hid = _get_hid()
        if hid is not None:
            device_cfg = cfg.get("device", {})
            defaults = DRIVER_DEFAULTS.get(driver, DRIVER_DEFAULTS["patlite"])
            vid = _parse_vid_pid(device_cfg.get("vid", defaults["vid"]))
            found = [d for d in hid.enumerate() if d["vendor_id"] == vid]
            if found:
                try:
                    dev = hid.device()
                    dev.open(vid, found[0]["product_id"])
                    if driver == "luxafor":
                        _luxafor_off(dev)
                    elif driver == "blink1":
                        _blink1_off(dev)
                    else:
                        dev.write([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                    dev.close()
                except Exception as e:
                    print(f"usb_led: device error: {e}", file=sys.stderr)
        return

    send_signal(event)


if __name__ == "__main__":
    main()
