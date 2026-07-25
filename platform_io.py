"""
platform_io.py - ALL platform-specific code lives here

The rest of the codebase never checks what OS it is on. These helpers
no-op gracefully anywhere they don't apply (Windows, Ubuntu, macOS).
"""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("platform")


def is_raspberry_pi() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore")
        return "Raspberry Pi" in model
    except OSError:
        return False


def power_check(mode="auto") -> bool:
    """
    On a Raspberry Pi, verify clean power via vcgencmd get_throttled.
    Returns True if OK (or not applicable). Logs details on any problem.
    """
    if mode == "off":
        return True
    if not is_raspberry_pi():
        return True
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=5)
        value = out.stdout.strip()          # e.g. "throttled=0x0"
        log.info("Pi power check: %s", value)
        hexval = int(value.split("=")[1], 16)
        if hexval == 0:
            return True
        problems = []
        if hexval & 0x1:
            problems.append("UNDERVOLTAGE NOW")
        if hexval & 0x4:
            problems.append("THROTTLED NOW")
        if hexval & 0x10000:
            problems.append("undervoltage occurred")
        if hexval & 0x40000:
            problems.append("throttling occurred")
        log.warning("Pi power problems: %s", ", ".join(problems) or value)
        return False
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        log.warning("Pi power check failed to run: %s", e)
        return True


def service_hints():
    """Log a one-line reminder of how to run 24/7 on this platform."""
    if is_raspberry_pi():
        log.info("Tip: run as a systemd service with Restart=always; "
                 "enable the Pi hardware watchdog for auto-recovery.")
    else:
        log.info("Tip (Windows): use Task Scheduler 'At startup' + "
                 "'Restart on failure' to run collector.py 24/7.")
