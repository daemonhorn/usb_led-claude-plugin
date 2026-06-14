#!/usr/bin/env python3
"""
Patlite NE-USB + Claude Code hook installer.
Cross-platform: Windows, macOS, Linux.

Usage:
    python install.py           # install
    python install.py --uninstall  # remove hooks and plugin files
    python install.py --test    # test device without installing
"""

import sys
import os
import json
import shutil
import subprocess
import platform
import argparse
from pathlib import Path

PLUGIN_NAME = "patlite"
INSTALL_DIR = Path.home() / ".claude" / "plugins" / PLUGIN_NAME
VENV_DIR = INSTALL_DIR / ".venv"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
VENDOR_ID = 0x191A

HOOK_EVENTS = {
    "Notification": "notification",
    "Stop": "stop",
    "UserPromptSubmit": "working",
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "SessionEnd": "idle",
}

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"


# ── helpers ────────────────────────────────────────────────────────────────

def print_step(msg: str) -> None:
    print(f"\n{'─'*50}")
    print(f"  {msg}")
    print(f"{'─'*50}")


def print_ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def print_warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def print_err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


def run(cmd: list, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True)


# ── checks ─────────────────────────────────────────────────────────────────

def check_python() -> None:
    if sys.version_info < (3, 7):
        print_err(f"Python 3.7+ required (found {sys.version})")
        sys.exit(1)
    print_ok(f"Python {sys.version.split()[0]}")


def check_claude_config() -> None:
    settings_dir = Path.home() / ".claude"
    if not settings_dir.exists():
        print_warn("~/.claude not found — Claude Code may not be installed")
        print_warn("Install Claude Code first: https://claude.ai/code")
    else:
        print_ok("~/.claude directory found")


# ── dependencies ───────────────────────────────────────────────────────────

def _is_externally_managed() -> bool:
    """Return True when system Python is PEP 668 managed (Debian 12+, Ubuntu 23.04+)."""
    import sysconfig
    stdlib = sysconfig.get_path("stdlib")
    return bool(stdlib and Path(stdlib, "EXTERNALLY-MANAGED").exists())


def _get_python_exe() -> str:
    """Return posix path to the Python that has the plugin's packages installed."""
    venv_python = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python3")
    if venv_python.exists():
        return venv_python.as_posix()
    return Path(sys.executable).as_posix()


def _install_into_venv(req_file: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print_err("Could not create virtual environment:")
        print_err(result.stderr.strip())
        print_warn("On Debian/Ubuntu, install the venv package first:")
        print_warn("  sudo apt install python3-venv")
        print_warn("Or install dependencies via apt instead:")
        print_warn("  sudo apt install python3-hidapi python3-yaml python3-pynput")
        sys.exit(1)

    venv_python = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python3")
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(req_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print_err("pip install into venv failed:")
        print_err(result.stderr.strip())
        sys.exit(1)
    print_ok(f"Dependencies installed into virtualenv ({VENV_DIR})")
    print_ok("Hook commands will use the virtualenv Python automatically")


def install_deps() -> None:
    print_step("Installing Python dependencies")
    repo_dir = Path(__file__).parent
    req_file = repo_dir / "requirements.txt"

    result = run([sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                 capture=True)
    if result.returncode == 0:
        print_ok("hidapi installed")
        print_ok("pyyaml installed")
        print_ok("pynput installed")
        return

    # PEP 668: Debian 12+, Ubuntu 23.04+ block system-wide pip installs.
    if _is_externally_managed() or "externally-managed-environment" in result.stderr:
        print_warn("System Python is externally managed (PEP 668).")
        print_warn("Creating a virtual environment to hold plugin dependencies...")
        _install_into_venv(req_file)
    else:
        print_err("pip install failed:")
        print(result.stderr, file=sys.stderr)
        sys.exit(1)


# ── file install ───────────────────────────────────────────────────────────

def install_files() -> None:
    print_step(f"Installing plugin files → {INSTALL_DIR}")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    repo_dir = Path(__file__).parent
    for fname in ("patlite.py", "config.yaml"):
        src = repo_dir / fname
        dst = INSTALL_DIR / fname
        if dst.exists() and fname == "config.yaml":
            print_warn(f"config.yaml already exists at {dst} — skipping (your settings preserved)")
            continue
        shutil.copy2(src, dst)
        print_ok(f"Copied {fname}")


# ── udev rules (Linux) ─────────────────────────────────────────────────────

def install_udev() -> None:
    rules_path = Path("/etc/udev/rules.d/99-patlite.rules")
    rules = (
        '# Patlite NE-USB signal tower — allow current user group access\n'
        'SUBSYSTEM=="usb", ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", '
        'MODE="0660", GROUP="plugdev"\n'
        'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="191a", ATTRS{idProduct}=="6001", '
        'MODE="0660", GROUP="plugdev"\n'
    )
    if rules_path.exists():
        print_ok("udev rules already installed")
        return

    try:
        import tempfile, subprocess as sp
        with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as f:
            f.write(rules)
            tmp = f.name
        result = sp.run(["sudo", "cp", tmp, str(rules_path)], capture_output=True)
        os.unlink(tmp)
        if result.returncode == 0:
            sp.run(["sudo", "udevadm", "control", "--reload-rules"])
            sp.run(["sudo", "udevadm", "trigger"])
            print_ok("udev rules installed — reconnect the device if already plugged in")
        else:
            raise PermissionError(result.stderr.decode())
    except Exception as e:
        print_warn(f"Could not install udev rules automatically: {e}")
        print_warn("Manually create /etc/udev/rules.d/99-patlite.rules with:")
        print()
        print(rules)
        print_warn("Then run: sudo udevadm control --reload-rules && sudo udevadm trigger")


# ── settings.json hooks ────────────────────────────────────────────────────

def _make_hook_entry(event: str, cmd_arg: str) -> dict:
    # Use forward slashes so the command works correctly when run via bash on Windows.
    python_exe = _get_python_exe()
    script_path = (INSTALL_DIR / "patlite.py").as_posix()
    command = f"{python_exe} {script_path} {cmd_arg}"
    return {
        "matcher": ".*",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 5,
                "allowFailure": True,
            }
        ],
    }


def _hook_already_present(entries: list) -> bool:
    for entry in entries:
        for hook in entry.get("hooks", []):
            if "patlite.py" in hook.get("command", ""):
                return True
    return False


def update_settings() -> None:
    print_step(f"Updating Claude Code hooks → {SETTINGS_PATH}")

    # Load existing settings
    settings = {}
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            print_err(f"settings.json is not valid JSON: {e}")
            print_err("Fix the file manually, then re-run the installer.")
            sys.exit(1)

    hooks = settings.setdefault("hooks", {})
    added = 0
    skipped = 0

    for event, cmd_arg in HOOK_EVENTS.items():
        entries = hooks.setdefault(event, [])
        if _hook_already_present(entries):
            print_warn(f"{event}: hook already registered — skipped")
            skipped += 1
        else:
            entries.append(_make_hook_entry(event, cmd_arg))
            print_ok(f"{event}: hook added")
            added += 1

    # Backup then write
    if added > 0:
        backup = SETTINGS_PATH.with_suffix(".json.bak")
        if SETTINGS_PATH.exists():
            shutil.copy2(SETTINGS_PATH, backup)
            print_ok(f"Backed up existing settings → {backup.name}")

        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        print_ok(f"settings.json updated ({added} hooks added, {skipped} skipped)")
    else:
        print_ok("All hooks already present — no changes needed")


# ── device test ────────────────────────────────────────────────────────────

def test_device() -> None:
    print_step("Testing Patlite device")
    try:
        import hid
    except ImportError:
        print_warn("hidapi not importable — re-run the installer or install manually:")
        print_warn("  Debian/Ubuntu: sudo apt install python3-hidapi")
        print_warn("  Other:         pip install hidapi")
        return

    devices = [d for d in hid.enumerate() if d["vendor_id"] == VENDOR_ID]
    if not devices:
        print_warn("No Patlite device detected (VID=0x191A). Is it plugged in?")
        print_warn("The hooks are installed — they will activate when the device is connected.")
        return

    d = devices[0]
    print_ok(f"Device found: {d['manufacturer_string']} {d['product_string']} "
             f"(VID={hex(d['vendor_id'])} PID={hex(d['product_id'])})")

    # Quick light test: cycle through a few colors
    print("  Running light test (green → blue → off)…")
    try:
        import time
        dev = hid.device()
        dev.open(VENDOR_ID, d["product_id"])
        for led_byte in (0x21, 0x41, 0x00):   # green solid, blue solid, off
            dev.write([0x00, 0x00, 0x00, 0xFF, 0x0F, led_byte, 0x00, 0x00, 0x00])
            time.sleep(1)
        dev.close()
        print_ok("Light test passed")
    except Exception as e:
        print_warn(f"Light test failed: {e}")
        if IS_LINUX:
            print_warn("On Linux you may need udev rules — re-run with: python install.py")


# ── uninstall ──────────────────────────────────────────────────────────────

def uninstall() -> None:
    print_step("Uninstalling Patlite Claude Code plugin")

    # Remove hooks from settings.json
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            settings = json.load(f)

        hooks = settings.get("hooks", {})
        removed = 0
        for event in list(HOOK_EVENTS):
            entries = hooks.get(event, [])
            before = len(entries)
            entries[:] = [
                e for e in entries
                if not _hook_already_present([e])
            ]
            removed += before - len(entries)
            if not entries:
                hooks.pop(event, None)

        if not hooks:
            settings.pop("hooks", None)

        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        print_ok(f"Removed {removed} hooks from settings.json")

    # Remove plugin directory
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
        print_ok(f"Removed {INSTALL_DIR}")

    print_ok("Uninstall complete")


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patlite NE-USB + Claude Code installer"
    )
    parser.add_argument("--uninstall", action="store_true", help="Remove plugin and hooks")
    parser.add_argument("--test", action="store_true", help="Test device only")
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════╗")
    print("║  Patlite NE-USB  ×  Claude Code  Installer  ║")
    print("╚══════════════════════════════════════════════╝")

    if args.uninstall:
        uninstall()
        return

    if args.test:
        test_device()
        return

    print_step("Checking prerequisites")
    check_python()
    check_claude_config()

    install_deps()
    install_files()

    if IS_LINUX:
        install_udev()

    update_settings()
    test_device()

    print()
    print("╔══════════════════════════════════════════════╗")
    print("║  Installation complete!                      ║")
    print("║                                              ║")
    print("║  Restart Claude Code to activate hooks.      ║")
    print("║  Edit ~/.claude/plugins/patlite/config.yaml  ║")
    print("║  to customize colors and patterns.           ║")
    print("╚══════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
