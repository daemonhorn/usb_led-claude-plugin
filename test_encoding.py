#!/usr/bin/env python3
"""
Byte-array unit tests for Patlite, Luxafor, and blink(1) HID encoding.
Run with: python3 test_encoding.py
No hardware required — uses a fake dev that records writes.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from usb_led import (
    build_led_byte, build_buzzer_bytes,
    _send_patlite, _send_luxafor, _blink1_write_pattern, _send_blink1,
    RGB_COLORS,
)


class _FakeDev:
    def __init__(self, mk2=False):
        self.writes = []
        self.feature_reports = []
        self._mk2 = mk2
        # Pre-load a version response so _blink1_is_mk2 works:
        # resp[0]=report_id, resp[1]=cmd_echo('v'), resp[2]=0,
        # resp[3]=major ASCII ('2' or '1'), resp[4]=minor ASCII
        major = ord('2') if mk2 else ord('1')
        self._version_resp = [0x01, ord('v'), 0, major, ord('0'), 0, 0, 0, 0]
        self._next_get_resp = None

    def write(self, data):
        self.writes.append(list(data))

    def send_feature_report(self, data):
        self.feature_reports.append(list(data))
        # Prime the version response for the next get_feature_report call.
        if data[1] == 0x76:
            self._next_get_resp = self._version_resp

    def get_feature_report(self, report_id, length):
        if self._next_get_resp is not None:
            resp = self._next_get_resp
            self._next_get_resp = None
            return resp
        return []

    def close(self):
        pass


def check(cond, msg):
    if not cond:
        print(f"  FAIL: {msg}")
        return False
    print(f"  pass: {msg}")
    return True


passed = failed = 0

def test(cond, msg):
    global passed, failed
    if check(cond, msg):
        passed += 1
    else:
        failed += 1


# ── Patlite LED byte encoding ────────────────────────────────────────────────
print("Patlite LED byte:")
test(build_led_byte("amber", "flash") == 0x32, "amber/flash == 0x32")
test(build_led_byte("green", "solid") == 0x21, "green/solid == 0x21")
test(build_led_byte("off", "off")    == 0x00, "off/off == 0x00")
test(build_led_byte("blue", "pulse") == 0x44, "blue/pulse == 0x44")

# ── Patlite buzzer byte encoding ─────────────────────────────────────────────
print("Patlite buzzer bytes:")
bz, vol = build_buzzer_bytes("keep", "keep")
test(bz == 0xFF and vol == 0x0F, "keep/keep -> 0xFF, 0x0F")

bz, vol = build_buzzer_bytes("off", "keep")
test(bz == 0x00 and vol == 0x0F, "off/keep -> 0x00, 0x0F")

bz, vol = build_buzzer_bytes("intermittent", 5)
test(bz == 0x03 and vol == 5,    "intermittent/5 -> 0x03, 5")

bz, vol = build_buzzer_bytes("sweep", "off")
test(bz == 0x02 and vol == 0,    "sweep/off -> 0x02, 0")

# ── Patlite HID write ────────────────────────────────────────────────────────
print("Patlite HID write:")
dev = _FakeDev()
_send_patlite(dev, {"color": "amber", "pattern": "flash", "buzzer": "off", "volume": "keep"})
test(len(dev.writes) == 1, "one write() call")
w = dev.writes[0]
test(w[0] == 0x00, "report_id == 0")
test(w[3] == 0x00, "bz_byte=0 (buzzer off)")
test(w[4] == 0x0F, "vol_byte=0x0F (keep)")
test(w[5] == 0x32, "led_byte=0x32 (amber/flash)")
test(len(w) == 9,  "9-byte report")

# ── Luxafor solid ────────────────────────────────────────────────────────────
print("Luxafor solid:")
dev = _FakeDev()
_send_luxafor(dev, "red", "solid")
w = dev.writes[0]
test(w[1] == 0x01, "cmd=0x01 (solid)")
test(w[2] == 0xFF, "target=0xFF (all LEDs)")
test(w[3:6] == [255, 0, 0], "RGB=(255,0,0)")

# ── Luxafor flash (strobe) ───────────────────────────────────────────────────
print("Luxafor flash (strobe):")
dev = _FakeDev()
_send_luxafor(dev, "amber", "flash")
r, g, b = RGB_COLORS["amber"]
w = dev.writes[0]
test(w[1] == 0x03,      "cmd=0x03 (strobe)")
test(w[3:6] == [r,g,b], "RGB matches amber")
test(w[6] == 8,         "speed=8 (flash)")
test(w[8] == 0,         "repeat=0 (infinite)")

# ── Luxafor flash2 (slow strobe) ─────────────────────────────────────────────
print("Luxafor flash2:")
dev = _FakeDev()
_send_luxafor(dev, "blue", "flash2")
test(dev.writes[0][6] == 24, "speed=24 (flash2)")

# ── Luxafor pulse (wave) ─────────────────────────────────────────────────────
print("Luxafor pulse (wave):")
dev = _FakeDev()
_send_luxafor(dev, "green", "pulse")
w = dev.writes[0]
test(w[1] == 0x04, "cmd=0x04 (wave)")
test(w[2] == 1,    "wave_type=1")
test(w[7] == 0,    "repeat=0 (infinite)")
test(w[8] == 48,   "speed=48 (pulse)")

# ── Luxafor off ──────────────────────────────────────────────────────────────
print("Luxafor off:")
dev = _FakeDev()
_send_luxafor(dev, "off", "off")
w = dev.writes[0]
test(w[1] == 0x01,        "cmd=0x01 (solid)")
test(w[3:6] == [0, 0, 0], "RGB=(0,0,0)")

# ── blink(1) mk1 pattern write ───────────────────────────────────────────────
print("blink(1) mk1 write_pattern:")
dev = _FakeDev(mk2=False)
_blink1_write_pattern(dev, is_mk2=False, ms=300, r=255, g=80, b=0, pos=0)
w = dev.feature_reports[0]
# mk1 format: [0x01, 0x50, th, tl, R, G, B, pos]
t = 300 // 10
th, tl = t >> 8, t & 0xFF
test(w[1] == 0x50,        "cmd='P' (write_pattern)")
test(w[2] == th,          "mk1: byte2=th")
test(w[3] == tl,          "mk1: byte3=tl")
test(w[4:7] == [255,80,0],"mk1: bytes4-6=RGB")
test(w[7] == 0,           "mk1: pos=0")

# ── blink(1) mk2 pattern write ───────────────────────────────────────────────
print("blink(1) mk2 write_pattern:")
dev = _FakeDev(mk2=True)
_blink1_write_pattern(dev, is_mk2=True, ms=300, r=255, g=80, b=0, pos=0)
w = dev.feature_reports[0]
# mk2 format: [0x01, 0x50, R, G, B, th, tl, pos, ledn]
test(w[1] == 0x50,        "cmd='P' (write_pattern)")
test(w[2:5] == [255,80,0],"mk2: bytes2-4=RGB")
test(w[5] == th,          "mk2: byte5=th")
test(w[6] == tl,          "mk2: byte6=tl")
test(w[7] == 0,           "mk2: pos=0")
test(w[8] == 0,           "mk2: ledn=0")

# ── blink(1) solid (mk1) ─────────────────────────────────────────────────────
print("blink(1) solid (mk1):")
dev = _FakeDev(mk2=False)
_send_blink1(dev, "green", "solid")
cmds = [r[1] for r in dev.feature_reports]
test(0x70 in cmds, "stops pattern (0x70)")
test(0x6e in cmds, "fades to color (0x6e)")
fade = [r for r in dev.feature_reports if r[1] == 0x6e][0]
r2, g2, b2 = RGB_COLORS["green"]
test(fade[2:5] == [r2, g2, b2], "fade RGB matches green")

# ── blink(1) flash loop (mk1) ────────────────────────────────────────────────
print("blink(1) flash loop (mk1):")
dev = _FakeDev(mk2=False)
_send_blink1(dev, "amber", "flash")
cmds = [r[1] for r in dev.feature_reports]
# expect: 'v' version probe, two 'P' writes, one 'p' play
test(cmds.count(0x50) == 2, "two write_pattern calls")
test(0x70 in cmds,          "play command issued (0x70)")
play = [r for r in dev.feature_reports if r[1] == 0x70 and r[2] == 1][0]
test(play[2] == 1, "play=1 (start)")
test(play[3] == 0, "start=0")
test(play[4] == 1, "end=1")
test(play[5] == 0, "count=0 (infinite)")

# ── blink(1) off ─────────────────────────────────────────────────────────────
print("blink(1) off:")
dev = _FakeDev()
_send_blink1(dev, "off", "off")
cmds = [r[1] for r in dev.feature_reports]
test(0x70 in cmds, "stop pattern (0x70)")
test(0x6e in cmds, "fade to off (0x6e)")
fade = [r for r in dev.feature_reports if r[1] == 0x6e][0]
test(fade[2:5] == [0, 0, 0], "fade to black")

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
