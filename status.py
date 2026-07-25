"""
status.py - write a small health/status file and upload it to the cloud

Run by cron every 5 minutes. Reads the live database read-only (safe
alongside the collector thanks to WAL mode), writes a human-readable
status.txt, and rclone-copies it to the configured remote. Prints nothing
on success so the cron log stays quiet; errors go to stderr.

Reports:
  - per device: is data flowing? (age of newest record vs heartbeat interval)
  - last voltage event and last frequency event (time + magnitude extremes)
  - Pi boot time / uptime
  - data disk free space
  - Pi power health (vcgencmd get_throttled)
"""

import datetime
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import config as config_mod
import platform_io


def iso(epoch):
    if epoch is None:
        return "never"
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def age(epoch, now):
    if epoch is None:
        return "n/a"
    s = int(now - epoch)
    if s < 120:
        return f"{s} s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} days ago"


def q1(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return row if row else None


def main():
    ini = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    cfg = config_mod.load(ini)
    now = time.time()
    lines = []
    lines.append(f"PMU COLLECTOR STATUS - {cfg.station_name}")
    lines.append(f"Generated: {iso(now)}")
    lines.append("=" * 60)

    # ---- Pi / system ----
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        lines.append(f"Pi last boot:   {iso(now - uptime)}  "
                     f"(up {uptime / 86400:.1f} days)")
    except OSError:
        lines.append("Pi last boot:   n/a (not Linux)")

    du = shutil.disk_usage(cfg.data_dir)
    lines.append(f"Data disk free: {du.free / 1e9:.1f} GB of "
                 f"{du.total / 1e9:.1f} GB")

    if platform_io.is_raspberry_pi():
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"],
                                 capture_output=True, text=True, timeout=5)
            val = out.stdout.strip().split("=")[-1]
            verdict = "OK" if val == "0x0" else "PROBLEM - check power!"
            lines.append(f"Power health:   {val}  ({verdict})")
        except (OSError, subprocess.SubprocessError):
            pass

    # ---- database ----
    db_uri = f"file:{cfg.db_path}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        lines.append(f"DATABASE:       CANNOT OPEN ({e})")
        conn = None

    if conn:
        # ---- active settings group (station-wide) ----
        # The active group is the one with the most recent activation. Its
        # created_ts is the Pi wall-clock time the settings were FIRST put in
        # use; because an unchanged restart reuses the same group, this value
        # is stable across reboots and only moves when a setting actually
        # changes. Wrapped defensively so a pre-provenance database still works.
        try:
            r = q1(conn,
                   "SELECT a.settings_group_id, g.created_ts "
                   "FROM settings_activations a "
                   "JOIN settings_groups g ON g.id = a.settings_group_id "
                   "ORDER BY a.activated_at DESC LIMIT 1")
            lines.append("")
            if r:
                lines.append(f"Active settings: group {r[0]}  "
                             f"(in use since {iso(r[1])})")
            else:
                lines.append("Active settings: none recorded yet")
        except sqlite3.Error:
            lines.append("")
            lines.append("Active settings: n/a (pre-provenance database)")

        # freshness threshold: heartbeat interval + slack
        stale_after = cfg.heartbeat_sec * 2 + 30
        for (dev_id, key, name) in conn.execute(
                "SELECT id, key, name FROM devices"):
            newest = None
            for table in ("voltage_measurements", "frequency_measurements"):
                r = q1(conn, f"SELECT MAX(ts) FROM {table} WHERE device_id=?",
                       (dev_id,))
                if r and r[0] and (newest is None or r[0] > newest):
                    newest = r[0]
            if newest is None:
                state = "NO DATA YET"
            elif now - newest <= stale_after:
                state = "RECEIVING"
            else:
                state = "STALE - NOT RECEIVING?"
            lines.append("")
            lines.append(f"Device: {name}")
            lines.append(f"  Data:           {state} "
                         f"(newest record {age(newest, now)})")

            r = q1(conn, "SELECT trigger_start, min_magnitude, peak_magnitude "
                         "FROM voltage_events WHERE device_id=? "
                         "ORDER BY trigger_start DESC LIMIT 1", (dev_id,))
            if r:
                lines.append(f"  Last V event:   {iso(r[0])} "
                             f"({age(r[0], now)})  "
                             f"min {r[1]:.1f} V, peak {r[2]:.1f} V")
            else:
                lines.append("  Last V event:   none recorded")

            r = q1(conn, "SELECT trigger_start, min_frequency, peak_frequency "
                         "FROM frequency_events WHERE device_id=? "
                         "ORDER BY trigger_start DESC LIMIT 1", (dev_id,))
            if r:
                lines.append(f"  Last F event:   {iso(r[0])} "
                             f"({age(r[0], now)})  "
                             f"min {r[1]:.3f} Hz, peak {r[2]:.3f} Hz")
            else:
                lines.append("  Last F event:   none recorded")

            r = q1(conn, "SELECT MAX(ts) FROM voltage_measurements "
                         "WHERE device_id=? AND record_type='GAP'", (dev_id,))
            if r and r[0]:
                lines.append(f"  Last GAP mark:  {iso(r[0])} "
                             f"({age(r[0], now)}) - restart/outage")
        conn.close()

    text = "\n".join(lines) + "\n"

    status_dir = cfg.data_dir / "status"
    status_dir.mkdir(exist_ok=True)
    status_path = status_dir / "status.txt"
    status_path.write_text(text)

    # ---- upload (same rclone remote as backups) ----
    if cfg.rclone_remote:
        try:
            result = subprocess.run(
                [cfg.rclone_path, "copy", str(status_path), cfg.rclone_remote],
                capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                print(f"status upload failed: {result.stderr.strip()[:300]}",
                      file=sys.stderr)
                sys.exit(1)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"status upload error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
