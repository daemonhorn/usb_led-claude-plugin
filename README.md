# patlite_usb-claude-plugin

Physical feedback for Claude Code via a [Patlite NE-USB](https://www.patlite.com/product/detail0000000762.html) signal tower. Lights up with different colors and patterns for each Claude Code lifecycle event — so you know at a glance whether Claude is working, done, or waiting for your attention.

## Supported devices

Any Patlite NE-USB series device (VID `0x191A`, PID `0x6001`):

| Model | Colors | Sound |
|-------|--------|-------|
| NE-WT-USB | Multicolor LED | No |
| NE-SN-USB | Multicolor LED | Yes (buzzer) |
| NE-ST-USB | Multicolor LED | No |
| NE-WN-USB | Multicolor LED | No |

> **Note:** The available colors on your specific unit depend on its LED configuration. Experiment with colors in `config.yaml` — unsupported ones silently fall back to the closest available color.

---

## Quick start

### Prerequisites

- Python 3.7+
- [Claude Code](https://claude.ai/code) installed
- Patlite NE-USB device plugged in via USB

### Install

```bash
git clone git@github.com:daemonhorn/patlite_usb-claude-plugin.git
cd patlite_usb-claude-plugin
python install.py
```

The installer:
1. Installs `hidapi` and `pyyaml` via pip
2. Copies `patlite.py` and `config.yaml` to `~/.claude/plugins/patlite/`
3. Adds hooks to `~/.claude/settings.json`
4. Runs a quick light test (green → blue → off)

**Restart Claude Code** to activate the hooks.

### Linux only — USB permissions

The installer will attempt to install a udev rule (requires `sudo`). If it fails, create the rule manually:

```bash
sudo tee /etc/udev/rules.d/99-patlite.rules <<'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", MODE="0660", GROUP="plugdev"
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

Removes all hooks from `~/.claude/settings.json` and deletes `~/.claude/plugins/patlite/`.

---

## Default event mapping

| Claude Code event | Light | Meaning |
|-------------------|-------|---------|
| `UserPromptSubmit` | 🔵 Blue solid | You sent a prompt — Claude is working |
| `PreToolUse` | 🩵 Cyan pulse | Claude is executing a tool |
| `PostToolUse` | 🔵 Blue solid | Tool done, Claude still working |
| `Stop` | 🟢 Green solid | Claude finished — come check |
| `Notification` | 🟡 Amber flash | Claude needs your attention |

---

## Configuration

Edit `~/.claude/plugins/patlite/config.yaml` to customize behavior. Changes take effect immediately — no restart needed.

```yaml
device:
  vid: 0x191A   # Patlite vendor ID — do not change
  pid: null     # null = auto-detect; set to e.g. 0x6001 to pin a specific device

events:
  notification:
    color: amber
    pattern: flash

  stop:
    color: green
    pattern: solid

  working:
    color: blue
    pattern: solid

  pre_tool:
    color: cyan
    pattern: pulse

  post_tool:
    color: blue
    pattern: solid

  idle:
    color: "off"
    pattern: "off"
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

### Disabling an event

Set both fields to `"off"` for any event you don't want to trigger the light:

```yaml
events:
  pre_tool:
    color: "off"
    pattern: "off"
```

---

## Manual control

Run `patlite.py` directly from any terminal to test signals or build automations:

```bash
# From the installed location
python ~/.claude/plugins/patlite/patlite.py <event>

# From the repo
python patlite.py <event>
```

Available events: `notification`, `stop`, `working`, `pre_tool`, `post_tool`, `idle`, `off`

The `off` argument always turns the light off regardless of config.

---

## How it works

### USB protocol

The Patlite NE-USB is a USB HID class device (no custom driver required). The plugin sends 9-byte HID output reports:

```
Byte 0:  Report ID       = 0x00
Byte 1:  Command version = 0x00
Byte 2:  Command ID      = 0x00 (LED/buzzer control)
Byte 3:  Buzzer control  = 0xFF (keep current)
Byte 4:  Buzzer volume   = 0x0F (keep current)
Byte 5:  LED control     = (color_nibble << 4) | pattern_nibble
Bytes 6–8: Padding       = 0x00
```

Source: [PATLITE-Corporation/NE-USB_linux_python_example](https://github.com/PATLITE-Corporation/NE-USB_linux_python_example)

### Claude Code hooks

The installer adds entries to `~/.claude/settings.json`. Each hook invokes `patlite.py <event>` as a shell command. All hooks use `"allowFailure": true` so Claude Code continues normally if the device is unplugged.

| Hook | Trigger |
|------|---------|
| `UserPromptSubmit` | User sends a message |
| `PreToolUse` | Before any tool call |
| `PostToolUse` | After any tool call |
| `Stop` | Claude finishes generating |
| `Notification` | Claude sends a system notification |

---

## Troubleshooting

**Light doesn't respond after install**
- Restart Claude Code to reload `settings.json`
- Verify: open `~/.claude/settings.json` and confirm the `hooks` section is present
- Run `python install.py --test` to test the device independently

**Hooks fire with a mangled path on Windows**
- This happens when hooks are added via the `/hooks` dialog (bash eats backslashes in the command)
- Fix: `python fix_hooks.py` then restart Claude Code

**`No Patlite device found` error**
- Confirm the device is plugged in:
  ```bash
  python -c "import hid; [print(d) for d in hid.enumerate() if d['vendor_id'] == 0x191A]"
  ```
- On Linux: check udev rules and `plugdev` group membership

**`ImportError: hidapi`**
- Run `pip install hidapi` and retry
- Verify: `python -c "import hid; print('ok')"`

**Light stuck on a color**
- Run: `python ~/.claude/plugins/patlite/patlite.py off`

**Too many flickers during tool-heavy responses**
- Disable `pre_tool`/`post_tool` by setting both to `color: "off"` in `config.yaml`

**Wrong Python used by hooks**
- Re-run `python install.py --uninstall` then `python install.py` with the correct Python interpreter

---

## Platform notes

| Platform | Notes |
|----------|-------|
| **Windows** | Works out of the box — Windows HID driver provides access |
| **macOS** | Works out of the box — IOHIDManager provides access |
| **Linux** | Requires udev rules — installer handles this; see [Linux section](#linux-only--usb-permissions) |

---

## License

MIT
