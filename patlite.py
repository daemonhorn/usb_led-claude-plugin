#!/usr/bin/env python3
"""
Patlite NE-WT-USB controller for Claude Code hooks.
Usage: python patlite.py <event>
Events: notification, stop, working, pre_tool, post_tool, idle, off
"""
import sys
import os

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


def send_signal(event: str) -> None:
    config = load_config()

    event_cfg = config.get("events", {}).get(event)
    if event_cfg is None:
        print(f"patlite: unknown event '{event}'", file=sys.stderr)
        sys.exit(1)

    color = event_cfg.get("color", "off")
    pattern = event_cfg.get("pattern", "off")
    led_byte = build_led_byte(color, pattern)

    device_cfg = config.get("device", {})
    vid = int(str(device_cfg.get("vid", VENDOR_ID)), 16) if isinstance(device_cfg.get("vid"), str) else device_cfg.get("vid", VENDOR_ID)
    pid = device_cfg.get("pid")

    try:
        import hid
    except ImportError:
        print("patlite: hidapi not installed. Run: pip install hidapi", file=sys.stderr)
        sys.exit(1)

    if pid is None:
        found = [d for d in hid.enumerate() if d["vendor_id"] == vid]
        if not found:
            print(f"patlite: no Patlite device found (VID={hex(vid)})", file=sys.stderr)
            print("patlite: connected HID devices:", file=sys.stderr)
            for d in hid.enumerate():
                print(f"  VID={hex(d['vendor_id'])} PID={hex(d['product_id'])} {d['manufacturer_string']} {d['product_string']}", file=sys.stderr)
            sys.exit(1)
        pid = found[0]["product_id"]

    try:
        dev = hid.device()
        dev.open(vid, pid)

        # HID report: [report_id=0x00] + 8 data bytes
        # Data: [cmd_version=0x00, cmd_id=0x00, buzzer_ctrl=0xFF, buzzer_vol=0x0F, led_byte, 0x00, 0x00, 0x00]
        # buzzer 0xFF = keep current; 0x0F volume = keep current
        report = [0x00, 0x00, 0x00, 0xFF, 0x0F, led_byte, 0x00, 0x00, 0x00]
        dev.write(report)
        dev.close()
    except Exception as e:
        print(f"patlite: device error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <event>", file=sys.stderr)
        print("Events: notification, stop, working, pre_tool, post_tool, idle, off", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]

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
