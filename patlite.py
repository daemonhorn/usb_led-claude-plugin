#!/usr/bin/env python3
"""
Patlite NE-WT-USB controller for Claude Code hooks.
Usage: python patlite.py <event>
Events: notification, stop, working, pre_tool, post_tool, idle, off
        touch_listen [--timeout N]   (background touch-to-approve daemon)
"""
import sys
import os
import tempfile
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")

VENDOR_ID = 0x191A

# LED byte: upper nibble = color, lower nibble = pattern
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

# GETSTATE command: asks the device to report current touch sensor state.
# Response: [status_byte, state_byte]; touch active when state_byte & 1 == 1.
_GETSTATE_CMD = [0x00, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]

# Lock file prevents multiple simultaneous touch listeners.
_LOCK_FILE = os.path.join(tempfile.gettempdir(), "patlite_touch_listen.pid")


def load_config():
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"patlite: failed to load config: {e}", file=sys.stderr)
        sys.exit(1)


def build_led_byte(color_name, pattern_name) -> int:
    color = COLORS.get(str(color_name).lower(), 0x0)
    pattern = PATTERNS.get(str(pattern_name).lower(), 0x0)
    return (color << 4) | pattern


def _open_device(config):
    """Open and return the HID device, resolving PID from config or auto-detect."""
    import hid
    device_cfg = config.get("device", {})
    vid_raw = device_cfg.get("vid", VENDOR_ID)
    vid = int(str(vid_raw), 16) if isinstance(vid_raw, str) else vid_raw
    pid = device_cfg.get("pid")

    if pid is None:
        found = [d for d in hid.enumerate() if d["vendor_id"] == vid]
        if not found:
            print(f"patlite: no Patlite device found (VID={hex(vid)})", file=sys.stderr)
            for d in hid.enumerate():
                print(f"  VID={hex(d['vendor_id'])} PID={hex(d['product_id'])} "
                      f"{d['manufacturer_string']} {d['product_string']}", file=sys.stderr)
            sys.exit(1)
        pid = found[0]["product_id"]

    dev = hid.device()
    dev.open(vid, pid)
    return dev


def send_signal(event: str) -> None:
    config = load_config()

    event_cfg = config.get("events", {}).get(event)
    if event_cfg is None:
        print(f"patlite: unknown event '{event}'", file=sys.stderr)
        sys.exit(1)

    color = event_cfg.get("color", "off")
    pattern = event_cfg.get("pattern", "off")
    led_byte = build_led_byte(color, pattern)

    try:
        import hid
    except ImportError:
        print("patlite: hidapi not installed.", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install python3-hidapi", file=sys.stderr)
        print("  Other:         pip install hidapi", file=sys.stderr)
        sys.exit(1)

    try:
        dev = _open_device(config)
        # HID report: [report_id=0x00] + 8 data bytes
        # buzzer 0xFF = keep current; 0x0F volume = keep current
        dev.write([0x00, 0x00, 0x00, 0xFF, 0x0F, led_byte, 0x00, 0x00, 0x00])
        dev.close()
    except Exception as e:
        print(f"patlite: device error: {e}", file=sys.stderr)
        sys.exit(1)

    if event == "notification":
        _spawn_touch_listener(config)


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


def _inject_enter():
    try:
        from pynput.keyboard import Key, Controller
        kb = Controller()
        kb.press(Key.enter)
        kb.release(Key.enter)
    except ImportError:
        print("patlite: pynput not installed.", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install python3-pynput", file=sys.stderr)
        print("  Other:         pip install pynput", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"patlite: keystroke injection failed: {e}", file=sys.stderr)
        sys.exit(1)


def touch_listen(timeout_s: int = 30) -> None:
    """
    Poll for touch sensor input and inject Enter when detected.
    Spawned as a detached background process by the notification handler.
    Single-instance: exits immediately if another listener is already running.
    """
    if not _acquire_lock():
        return

    try:
        import hid
    except ImportError:
        _release_lock()
        return

    dev = None
    try:
        config = load_config()
        dev = _open_device(config)
        deadline = time.monotonic() + timeout_s
        last_touched = False

        while time.monotonic() < deadline:
            dev.write(_GETSTATE_CMD)
            resp = dev.read(8, timeout_ms=200)
            touched = bool(resp and len(resp) > 1 and (resp[1] & 1))

            if touched and not last_touched:
                # Rising edge — close device before injecting so LED writes can reopen it
                dev.close()
                dev = None
                _inject_enter()
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
        _release_lock()


def _spawn_touch_listener(config: dict) -> None:
    """Spawn touch_listen as a detached background process (fire-and-forget)."""
    touch_cfg = config.get("touch", {})
    if not touch_cfg.get("enabled", True):
        return
    timeout = int(touch_cfg.get("approval_timeout", 30))

    script = os.path.abspath(__file__)
    cmd = [sys.executable, script, "touch_listen", "--timeout", str(timeout)]

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
        print(f"patlite: could not start touch listener: {e}", file=sys.stderr)


# ── main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <event>", file=sys.stderr)
        print("Events: notification, stop, working, pre_tool, post_tool, idle, off", file=sys.stderr)
        print("        touch_listen [--timeout N]", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]

    if event == "touch_listen":
        timeout = 30
        if "--timeout" in sys.argv:
            idx = sys.argv.index("--timeout")
            timeout = int(sys.argv[idx + 1])
        touch_listen(timeout_s=timeout)
        return

    # "off" is a built-in alias that always turns the light off
    if event == "off":
        import yaml
        cfg = {"device": {}, "events": {"off": {"color": "off", "pattern": "off"}}}
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f)
            cfg.setdefault("events", {})["off"] = {"color": "off", "pattern": "off"}
        except Exception:
            pass
        import hid
        found = [d for d in hid.enumerate() if d["vendor_id"] == VENDOR_ID]
        if found:
            dev = hid.device()
            dev.open(VENDOR_ID, found[0]["product_id"])
            dev.write([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
            dev.close()
        return

    send_signal(event)


if __name__ == "__main__":
    main()
