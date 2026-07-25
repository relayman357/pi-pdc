"""
backup.py - snapshot the live database and upload to the cloud

Run separately from the collector (Task Scheduler on Windows, cron on
Pi/Ubuntu), or manually:  python backup.py

Uses SQLite's online backup API, so the collector NEVER pauses - the
snapshot is consistent even while writes continue (WAL mode).
Never point rclone at the live pmu.db directly.
"""

import gzip
import logging
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import config as config_mod
from dbwriter import iso_utc

log = logging.getLogger("backup")

# Rolling 24h snapshots live in <backups>/_24h locally and are uploaded to a
# matching _24h folder under the configured rclone_remote.
SNAP24H_DIRNAME = "_24h"


def snapshot(live_db: Path, backup_dir: Path, compress: bool) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snap = backup_dir / f"pmu_{stamp}.db"

    src = sqlite3.connect(str(live_db))
    dst = sqlite3.connect(str(snap))
    with dst:
        src.backup(dst)             # consistent copy, collector keeps running
    dst.close()
    src.close()
    log.info("Snapshot created: %s (%.1f MB)", snap.name,
             snap.stat().st_size / 1e6)

    if compress:
        gz = snap.with_suffix(".db.gz")
        with open(snap, "rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        snap.unlink()
        log.info("Compressed: %s (%.1f MB)", gz.name, gz.stat().st_size / 1e6)
        return gz
    return snap


def upload(path: Path, rclone_path: str, remote: str) -> bool:
    if not remote:
        log.info("No rclone_remote configured - keeping local snapshot only")
        return True
    cmd = [rclone_path, "copy", str(path), remote]
    log.info("Uploading: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=1800)
        if result.returncode == 0:
            log.info("Upload OK")
            return True
        log.error("rclone failed (rc=%d): %s", result.returncode,
                  result.stderr.strip()[:500])
    except FileNotFoundError:
        log.error("rclone not found at '%s' - install it or set rclone_path",
                  rclone_path)
    except subprocess.SubprocessError as e:
        log.error("rclone error: %s", e)
    return False


def _subremote(remote: str, sub: str) -> str:
    """Append a subfolder to an rclone remote (e.g. dropbox:pmu_project ->
    dropbox:pmu_project/_24h). Returns '' unchanged if no remote is set, so
    upload() takes its usual keep-local-only path."""
    if not remote:
        return ""
    return remote.rstrip("/") + "/" + sub


def prune(backup_dir: Path, keep: int):
    snaps = sorted(backup_dir.glob("pmu_*.db*"))
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink()
        log.info("Pruned old snapshot: %s", old.name)


# ----------------------------- 24h rolling snapshots -------------------------
#
# Same file structure as a normal (compressed) backup, but containing only the
# last 24 h of data - everything with a timestamp older than 24 h before the
# NEWEST sample in the db is removed, then the file is VACUUMed to minimum size
# and gzipped. Because the snapshot is a throwaway copy (never the live db), we
# can VACUUM freely, which prune_old.py deliberately avoids on the live file.
#
# What is pruned mirrors prune_old.py exactly:
#     voltage/frequency_measurements : ts < cutoff
#     voltage/frequency_events       : trigger_end < cutoff  (open events kept)
#     correlated_events              : rows pointing at a removed event
# Settings provenance (settings_groups / _values / _activations) and devices
# are KEPT untouched: the viewer resolves the settings in force for a retained
# measurement via the newest activation with activated_at <= ts, and that
# governing activation is often far older than 24 h, so pruning it by age would
# orphan the provenance of the very data we keep. They are tiny anyway.


def _global_max_ts(conn):
    """Latest timestamp recorded anywhere in the db (meter frame), or None.

    ts_utc is just iso_utc(ts), so MAX(ts) is exactly the last UTC time
    recorded. Events are included so a db that only holds event rows still
    yields a sensible cutoff.
    """
    newest = None
    for table, col in (("voltage_measurements", "ts"),
                       ("frequency_measurements", "ts"),
                       ("voltage_events", "trigger_end"),
                       ("frequency_events", "trigger_end")):
        row = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
        if row and row[0] is not None:
            newest = row[0] if newest is None else max(newest, row[0])
    return newest


def _prune_older_than(conn, cutoff: float):
    """Delete everything older than cutoff from a snapshot copy (see notes)."""
    # correlated_events first - they reference the events we're about to remove
    conn.execute(
        "DELETE FROM correlated_events WHERE "
        "voltage_event_id IN (SELECT id FROM voltage_events "
        "  WHERE trigger_end < ?) OR "
        "frequency_event_id IN (SELECT id FROM frequency_events "
        "  WHERE trigger_end < ?)",
        (cutoff, cutoff))
    conn.execute("DELETE FROM voltage_events   WHERE trigger_end < ?", (cutoff,))
    conn.execute("DELETE FROM frequency_events WHERE trigger_end < ?", (cutoff,))
    conn.execute("DELETE FROM voltage_measurements   WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM frequency_measurements WHERE ts < ?", (cutoff,))


def snapshot_24h(live_db: Path, out_dir: Path):
    """Write a gzipped snapshot holding only the last 24 h of data.

    Returns the .gz Path, or None if the db has no data yet.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = out_dir / f"pmu_24h_{stamp}.db"       # temp working copy

    # Consistent copy of the live db (collector keeps running), same online
    # backup API and destination journal mode as snapshot() above.
    src = sqlite3.connect(str(live_db))
    dst = sqlite3.connect(str(work))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

    conn = sqlite3.connect(str(work))
    try:
        newest = _global_max_ts(conn)
        if newest is None:
            log.info("24h snapshot: db has no data yet - skipped")
            conn.close()
            work.unlink(missing_ok=True)
            return None
        cutoff = newest - 86400.0                # 24 h before the newest sample
        _prune_older_than(conn, cutoff)
        conn.commit()
        conn.execute("VACUUM")                   # shrink to minimum (offline copy)
    finally:
        conn.close()

    gz = work.with_suffix(".db.gz")
    with open(work, "rb") as fin, gzip.open(gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    work.unlink()
    log.info("24h snapshot: %s (%.1f MB, data >= %s)",
             gz.name, gz.stat().st_size / 1e6, iso_utc(cutoff))
    return gz


def prune_24h(out_dir: Path, keep: int):
    """Keep the newest `keep` rolling 24h snapshots, delete the rest."""
    if keep <= 0:
        return
    snaps = sorted(out_dir.glob("pmu_24h_*.db.gz"))
    for old in snaps[:-keep]:
        old.unlink()
        log.info("Pruned old 24h snapshot: %s", old.name)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    ini = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    cfg = config_mod.load(ini)

    if not cfg.backup_enabled:
        log.info("[backup] enabled = false in %s - nothing to do "
                 "(set enabled = true to activate scheduled backups)", ini)
        sys.exit(0)

    if not cfg.db_path.exists():
        sys.exit(f"Live database not found: {cfg.db_path}")

    snap = snapshot(cfg.db_path, cfg.backup_dir, cfg.backup_compress)
    ok = upload(snap, cfg.rclone_path, cfg.rclone_remote)
    prune(cfg.backup_dir, cfg.backup_keep_local)

    if cfg.backup_24h_snaps > 0:
        snap24_dir = cfg.backup_dir / SNAP24H_DIRNAME
        snap24 = snapshot_24h(cfg.db_path, snap24_dir)
        if snap24:
            remote24 = _subremote(cfg.rclone_remote, SNAP24H_DIRNAME)
            ok = upload(snap24, cfg.rclone_path, remote24) and ok
            prune_24h(snap24_dir, cfg.backup_24h_snaps)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
