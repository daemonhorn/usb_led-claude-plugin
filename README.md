# usb_led-claude-plugin

Physical feedback for Claude Code via a USB LED / signal light. Lights up with different colors and patterns for each Claude Code lifecycle event — so you know at a glance whether Claude is working, done, or waiting for your attention. Supports [Patlite NE-USB](https://www.patlite.com/product/detail0000000762.html) signal towers, [Luxafor](https://luxafor.com/) Flag/Orb/Mute, and [ThingM blink(1)](https://blink1.thingm.com/) mk1/mk2/mk3.

## Supported devices

### Patlite NE-USB (default)

All NE-USB series models share the same USB VID/PID and the same HID protocol:

| Model | Colors | Touch sensor | Buzzer |
|-------|--------|-------------|--------|
| NE-WT-USB | Multicolor LED | Yes | Yes |
| NE-WN-USB | Multicolor LED | No  | Yes |
| NE-ST-USB | Multicolor LED | Yes | Yes |
| NE-SN-USB | Multicolor LED | No  | Yes |

VID: `0x191A` / PID: `0x6001` (all models)

> **Note:** The available colors on your specific unit depend on its LED configuration. Experiment with colors in `config.yaml` — unsupported ones silently fall back to the closest available color.

Protocol reference: [PATLITE-Corporation/NE-USB\_linux\_python\_example](https://github.com/PATLITE-Corporation/NE-USB_linux_python_example)

### Luxafor Flag / Orb / Mute (`driver: luxafor`)

| VID | PID | Colors | Patterns | Buzzer | Touch |
|-----|-----|--------|----------|--------|-------|
| `0x04D8` | `0xF372` | Full RGB | solid, flash, flash2, pulse–pulse4 | No | No |

Patterns use native Luxafor hardware commands: solid color (cmd `0x01`), strobe (cmd `0x03`), and breathing wave (cmd `0x04`). All LEDs are targeted simultaneously (`0xFF`).

### ThingM blink(1) mk1 / mk2 / mk3 (`driver: blink1`)

| VID | PID | Colors | Patterns | Buzzer | Touch |
|-----|-----|--------|----------|--------|-------|
| `0x27B8` | `0x01ED` | Full RGB | solid, flash, flash2, pulse–pulse4 | No | No |

Flash and pulse patterns are implemented by writing a 2-frame loop into the device's onboard pattern RAM (`P` command) and playing it (`p` command). mk1 vs mk2 firmware format is auto-detected via the `v` command. Solid color stops any running pattern and sets the color immediately.

---

## Quick start

### Prerequisites

- Python 3.7+
- [Claude Code](https://claude.ai/code) installed
- Patlite NE-USB device plugged in via USB

### Install

```bash
git clone git@github.com:daemonhorn/usb_led-claude-plugin.git
cd usb_led-claude-plugin
python3 install.py
```

The installer:
1. Installs Python dependencies (see below for platform notes)
2. Auto-detects your connected device and pre-fills `driver:` in `config.yaml`
3. Copies `usb_led.py` and `config.yaml` to `~/.claude/plugins/usb_led/`
4. Adds hooks to `~/.claude/settings.json`
5. Runs a quick light test on every detected device (green → blue → off)

### Debian / Ubuntu

Debian 12+ and Ubuntu 23.04+ use a [PEP 668](https://peps.python.org/pep-0668/) managed Python environment that blocks system-wide `pip install`. The installer detects this automatically and falls back to one of two paths:

**Option A — let the installer create a virtualenv (recommended):**

```bash
sudo apt install python3-venv   # needed once
python3 install.py              # creates ~/.claude/plugins/usb_led/.venv automatically
```

The hooks are configured to call the virtualenv Python, so everything works transparently.

**Option B — install dependencies via apt:**

```bash
sudo apt install python3-hidapi python3-yaml python3-pynput python3-xlib
python3 install.py
```

`python3-hidapi` installs under the module name `hidapi` rather than `hid` — the plugin handles this automatically via a compatibility shim. `python3-xlib` is optional but enables automatic terminal window focus on X11/XWayland before injecting touch-sensor keypresses.

**Restart Claude Code** to activate the hooks.

### Linux only — USB permissions

The installer will attempt to install a udev rule (requires `sudo`). If it fails, create the rule manually:

```bash
sudo tee /etc/udev/rules.d/99-patlite.rules <<'EOF'
# Patlite NE-USB signal tower (VID 191A / PID 6001)
SUBSYSTEM=="usb",    ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", MODE="0660", GROUP="plugdev"
# Luxafor Flag / Orb / Mute (VID 04D8 / PID F372)
SUBSYSTEM=="usb",    ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="f372", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="f372", MODE="0660", GROUP="plugdev"
# ThingM blink(1) mk1/mk2/mk3 (VID 27B8 / PID 01ED)
SUBSYSTEM=="usb",    ATTRS{idVendor}=="27b8", ATTRS{idProduct}=="01ed", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="27b8", ATTRS{idProduct}=="01ed", MODE="0660", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then add yourself to the `plugdev` group if not already a member:

```bash
sudo usermod -aG plugdev $USER   # log out and back in after this
```

### Troubleshooting the install

If hooks were added with broken paths (e.g., after using the `/hooks` dialog on Windows), run:

```bash
python fix_hooks.py
```

Then restart Claude Code.

### Uninstall

```bash
python install.py --uninstall
```

Removes all hooks from `~/.claude/settings.json` and deletes `~/.claude/plugins/usb_led/`.

---

## Touch sensor (NE-WT-USB / NE-ST-USB)

Models with a **T** in the name have a capacitive touch sensor on the body. When Claude Code shows a permission prompt (e.g., "allow this bash command?"), the tower flashes amber. Touching the sensor injects **Enter** to confirm the highlighted option — no keyboard required.

### How it works

1. Claude Code fires the `Notification` hook
2. `usb_led.py` sets the amber-flash LED, then spawns a detached background listener
3. The listener polls the touch sensor via USB every ~100 ms for up to `approval_timeout` seconds
4. **Early exit:** any subsequent hook event (`PreToolUse`, `PostToolUse`, `Stop`, etc.) signals that the prompt was already answered — the listener exits immediately via a cancel sentinel file
5. When touch is detected, the listener attempts to focus Claude Code's terminal window before injecting Enter (see [injection strategy](#keystroke-injection-strategy) below)
6. Only one listener runs at a time (PID lock file in `/tmp`)

### Keystroke injection strategy

The listener walks the Linux `/proc` process tree at spawn time to identify the terminal emulator PID (e.g. `gnome-terminal`, `kitty`), then uses a platform-appropriate method to bring that window to the foreground before injecting Enter via `pynput`:

| Platform | Focus method | Notes |
|---|---|---|
| **Linux / X11** | `python3-xlib` — EWMH `_NET_ACTIVE_WINDOW` | Works when terminal runs on X11 or XWayland |
| **Linux / Wayland** | *(none — Wayland blocks cross-process input)* | pynput still fires; terminal must be focused |
| **macOS** | `osascript` — activates Terminal / iTerm2 / Warp / etc. | Searches running terminal apps by name |
| **Windows** | `ctypes` — `EnumWindows` + `SetForegroundWindow` | Targets the terminal PID's visible window |

### Caveats

- **Wayland:** targeted window focus is not possible without extra tools (`ydotool`, `wtype`). The Enter keystroke fires via `pynput` regardless, so it goes to whichever window is currently focused. Touch only when you see the amber flash and your Claude Code terminal is in the foreground.
- The listener starts on every `Notification` event, not only permission prompts. If the notification was informational (no dialog), the listener exits at the next hook event or after `approval_timeout` seconds — whichever comes first.

### Configuration

```yaml
touch:
  enabled: true            # set to false to disable on non-T models
  approval_timeout: 30     # seconds to wait for touch after each notification
```

### Dependencies

`pynput` is required for keystroke injection (all platforms). On Linux, `python3-xlib` enables targeted window focus on X11 systems. Both are included in `requirements.txt` and installed automatically by the installer.

Manual install if needed:

```bash
# Debian/Ubuntu
sudo apt install python3-pynput python3-xlib

# Other platforms
pip install pynput
```

---

## Default event mapping

| Claude Code event | Light | Meaning |
|-------------------|-------|---------|
| `UserPromptSubmit` | 🔵 Blue solid | You sent a prompt — Claude is working |
| `PreToolUse` | 🩵 Cyan pulse | Claude is executing a tool |
| `PostToolUse` | 🔵 Blue solid | Tool done, Claude still working |
| `Stop` | 🟢 Green solid | Claude finished — come check |
| `Notification` | 🟡 Amber flash | Claude needs your attention |
| `SessionEnd` | ⚫ Off | Session exited — light clears automatically |

Sound is **off** by default for all events. Enable it per-event in `config.yaml`.

---

## Configuration

Edit `~/.claude/plugins/usb_led/config.yaml` to customize behavior. Changes take effect immediately — no restart needed.

Ready-made configs for each device family are in the [`examples/`](examples/) directory — copy the right one over `~/.claude/plugins/usb_led/config.yaml`.

```yaml
device:
  driver: patlite   # Options: patlite | luxafor | blink1
  # vid/pid are auto-resolved from the driver; uncomment to override:
  # vid: 0x191A    # patlite / 0x04D8 luxafor / 0x27B8 blink1
  # pid: 0x6001    # patlite / 0xF372 luxafor / 0x01ED blink1

events:
  notification:
    color: amber
    pattern: flash
    buzzer: "off"    # silent by default
    volume: keep

  stop:
    color: green
    pattern: solid
    buzzer: "off"
    volume: keep

  working:
    color: blue
    pattern: solid
    buzzer: "off"
    volume: keep

  pre_tool:
    color: cyan
    pattern: pulse
    buzzer: "off"
    volume: keep

  post_tool:
    color: blue
    pattern: solid
    buzzer: "off"
    volume: keep

  idle:
    color: "off"
    pattern: "off"
    buzzer: "off"
    volume: keep
```

### Colors

| Value | Light |
|-------|-------|
| `red` | Red |
| `amber` / `yellow` | Amber/yellow |
| `green` | Green |
| `blue` | Blue |
| `cyan` | Cyan/sky blue |
| `purple` | Purple/magenta |
| `white` | White |
| `"off"` | Off (no light) |

> **Important:** Use `"off"` in quotes — bare `off` is parsed as boolean `False` by YAML.

### Patterns

| Value | Behavior |
|-------|----------|
| `solid` | Steady on |
| `flash` | Fast blink |
| `flash2` | Slower blink |
| `pulse` | Smooth pulse |
| `pulse2` – `pulse4` | Pulse variants |
| `"off"` | Off |

### Buzzer sounds

All NE-USB models include a buzzer. Set `buzzer:` per event to any of these values:

| Value | Sound |
|-------|-------|
| `"off"` | Silence — stops any playing sound |
| `continuous` | Steady tone |
| `sweep` | Rising/falling sweep |
| `intermittent` | Short repeating beeps |
| `weak` | Soft caution chime |
| `strong` | Loud attention chime |
| `star` | Shining-star melody |
| `london` | London Bridge melody |
| `keep` | Leave buzzer in its current state (default) |

> **Important:** Use `"off"` in quotes — bare `off` is parsed as boolean `False` by YAML.

### Volume

Set `volume:` to control playback level when an active buzzer pattern is selected:

| Value | Meaning |
|-------|---------|
| `1` – `10` | Volume steps (1 = quiet, 10 = loudest) |
| `"off"` | Mute (play pattern silently — useful to stop a loop without the noise) |
| `keep` | Leave volume at its current hardware setting |

`volume` is only applied when `buzzer` is an active pattern. If `buzzer` is `"off"` or `keep`, `volume` is ignored.

**Example — play a chime when Claude finishes:**

```yaml
events:
  stop:
    color: green
    pattern: solid
    buzzer: strong
    volume: 5
```

### Disabling an event

Set both fields to `"off"` for any event you don't want to trigger the light:

```yaml
events:
  pre_tool:
    color: "off"
    pattern: "off"
    buzzer: "off"
    volume: keep
```

---

## Manual control

Run `usb_led.py` directly from any terminal to test signals or build automations:

```bash
# From the installed location
python ~/.claude/plugins/usb_led/usb_led.py <event>

# From the repo
python usb_led.py <event>
```

Available events: `notification`, `stop`, `working`, `pre_tool`, `post_tool`, `idle`, `off`

The `off` argument always turns the light off and silences the buzzer regardless of config.

---

## How it works

### USB protocol

All supported devices are USB HID class devices — no custom driver required on any platform. Protocol details by driver:

#### Patlite (`driver: patlite`)

Sends 9-byte HID **output** reports:

```
Byte 0:  Report ID       = 0x00
Byte 1:  Command version = 0x00
Byte 2:  Command ID      = 0x00 (LED/buzzer control)
Byte 3:  Buzzer control  = (repeat << 4) | pattern
           0x00 = off/silence
           0x01–0x07 = continuous play of pattern 1–7
           0xFF = keep current state
Byte 4:  Buzzer volume   = 0x00 (mute) .. 0x0A (max) .. 0x0F (keep current)
Byte 5:  LED control     = (color_nibble << 4) | pattern_nibble
Bytes 6–8: Padding       = 0x00
```

Source: [PATLITE-Corporation/NE-USB\_linux\_python\_example](https://github.com/PATLITE-Corporation/NE-USB_linux_python_example)

#### Luxafor (`driver: luxafor`)

Sends 9-byte HID **output** reports:

```
Byte 0:  Report ID  = 0x00
Byte 1:  Command    = 0x01 solid / 0x03 strobe / 0x04 wave
Byte 2:  LED target = 0xFF (all)
Bytes 3–5: R, G, B (0–255)
Bytes 6–8: command-specific args (speed, repeat)
```

#### ThingM blink(1) (`driver: blink1`)

Sends 8-byte (mk1) or 9-byte (mk2) USB **feature** reports:

```
Byte 0:  Report ID   = 0x01
Byte 1:  Command     = 0x6e fade_to_rgb / 0x50 write_pattern / 0x70 play / 0x76 version
Bytes 2+: command-specific payload (RGB, fade time in 10ms units, pattern position)
```

Flash and pulse patterns write a 2-frame color→black loop into the device's onboard pattern RAM and start the looping playback engine. Solid stops playback and sets color immediately.

Source: [todbot/blink1](https://github.com/todbot/blink1)

### Claude Code hooks

The installer adds entries to `~/.claude/settings.json`. Each hook invokes `usb_led.py <event>` as a shell command. All hooks use `"allowFailure": true` so Claude Code continues normally if the device is unplugged.

| Hook | Trigger |
|------|---------|
| `UserPromptSubmit` | User sends a message |
| `PreToolUse` | Before any tool call |
| `PostToolUse` | After any tool call |
| `Stop` | Claude finishes generating |
| `Notification` | Claude sends a system notification |
| `SessionEnd` | Claude Code session exits (turns light off) |

---

## Troubleshooting

**Light doesn't respond after install**
- Restart Claude Code to reload `settings.json`
- Verify: open `~/.claude/settings.json` and confirm the `hooks` section is present
- Run `python install.py --test` to test the device independently

**Hooks fire with a mangled path on Windows**
- This happens when hooks are added via the `/hooks` dialog (bash eats backslashes in the command)
- Fix: `python fix_hooks.py` then restart Claude Code

**Device not detected**
- Confirm the device is plugged in and run `python3 install.py --test` to check all families
- Enumerate all HID devices to find your device's VID/PID:
  ```bash
  # pip hid package
  python3 -c "import hid; [print(hex(d['vendor_id']), hex(d['product_id']), d['product_string']) for d in hid.enumerate()]"
  # Debian python3-hidapi package
  python3 -c "import hidapi; [print(d) for d in hidapi.enumerate()]"
  ```
- On Linux: check udev rules and `plugdev` group membership

**`ImportError: No module named 'hid'` or `'hidapi'`**

On standard platforms, re-run the installer — it installs dependencies automatically.

On Debian/Ubuntu, install via apt:
```bash
sudo apt install python3-hidapi python3-yaml python3-pynput
```
Or install `python3-venv` and re-run `python3 install.py` to let the installer create a virtualenv.

**`hidapi` import fails even after installing it (non-Debian)**
- There are two pip packages that both expose `import hid`: the ctypes-based `hid` package and the Cython-based `hidapi` package. If both are installed the wrong one may load first.
- Fix: `pip uninstall hid` — then only `hidapi` remains.
- Check which you have: `pip list | grep -i hid`

**Light stuck on a color**
- Run: `python ~/.claude/plugins/usb_led/usb_led.py off`

**Buzzer keeps playing after Claude finishes**
- The `keep` buzzer value (default) leaves whatever the device is doing. If a previous event started the buzzer, set a later event's `buzzer: "off"` to stop it. For example, setting `stop.buzzer: "off"` will silence the buzzer whenever Claude finishes.

**Too many flickers during tool-heavy responses**
- Disable `pre_tool`/`post_tool` by setting both to `color: "off"` in `config.yaml`

**Touch sensor doesn't inject Enter / `pynput` error in logs**
- Install pynput: `sudo apt install python3-pynput` (Debian/Ubuntu) or `pip install pynput` (other)
- Verify: `python3 -c "from pynput.keyboard import Key, Controller"`
- On Linux/Wayland: targeted window focus isn't supported without extra tools — pynput fires to whatever window is focused. Keep the Claude Code terminal in the foreground when expecting a permission prompt, or install `ydotool`/`wtype` and set `touch.enabled: false` if it causes accidental keypresses elsewhere
- On Linux/X11: install `python3-xlib` (`sudo apt install python3-xlib`) to enable automatic terminal focus before injection

**Wrong Python used by hooks**
- Re-run `python3 install.py --uninstall` then `python3 install.py` with the correct Python interpreter

---

## Platform notes

| Platform | Notes |
|----------|-------|
| **Windows** | Works out of the box — Windows HID driver provides access |
| **macOS** | Works out of the box — IOHIDManager provides access |
| **Linux** | Requires udev rules — installer handles this; see [Linux section](#linux-only--usb-permissions) |
| **Debian 12+ / Ubuntu 23.04+** | PEP 668 managed Python — installer auto-creates a venv, or use `apt install python3-hidapi python3-yaml python3-pynput python3-xlib`; see [Debian section](#debian--ubuntu) |
| **Linux / Wayland** | Touch-sensor Enter injection works but targets the focused window — no cross-process input injection without `ydotool`/`wtype`; see [Caveats](#caveats) |

---

## License

BSD 2-Clause
