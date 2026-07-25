"""
prune_old.py - delete data older than a cutoff date from the LIVE database

Runs SAFELY while the collector is running - no service stop required. This is
possible because the database is WAL mode (see DBWriter._open): WAL allows the
collector's single writer thread plus other connections concurrently. This
script is simply a second writer, so it is written to never hold the write lock
long enough to disturb live collection:

  * deletes happen in small CHUNKS (short transactions), not one giant DELETE;
  * PRAGMA busy_timeout lets the two writers wait for each other instead of
    erroring with SQLITE_BUSY;
  * a tiny pause between chunks positively yields the write lock back to the
    collector so its 1-second batch commits never starve.

WHAT COUNTS AS "OLD"
--------------------
  voltage_measurements / frequency_measurements : ts < cutoff
  voltage_events / frequency_events             : trigger_end < cutoff
        (an event still open across the boundary is KEPT)
  correlated_events                             : any row pointing at a
        deleted event (cleaned up by hand - foreign_keys is not enabled in
        this project, so there is no ON DELETE CASCADE)

SETTINGS PROVENANCE IS PRESERVED BY DEFAULT
-------------------------------------------
The viewer resolves "settings in effect for measurement M on device D" as the
activation for D with the greatest activated_at <= M.ts. A config that has been
stable for a long time therefore has a SINGLE old activation that still governs
today's data. Deleting activations purely by age would orphan the provenance of
every RETAINED measurement. So:

  * By default this script does NOT touch settings_activations / settings_groups
    / settings_values at all. They are tiny (a handful of rows per config
    change) and they are the audit trail.
  * With --prune-settings, it still keeps - per device - the activation in force
    AT the cutoff (the greatest activated_at <= cutoff) plus everything after,
    and removes only genuinely superseded older activations. Orphaned groups
    (no surviving activation) are then removed too, but only if they were
    created before the cutoff, so a group the collector just created but has
    not yet stamped an activation against is never nuked.

devices is never pruned.

SPACE
-----
Deletes do not shrink the file; SQLite reuses the freed pages. VACUUM would
reclaim space but needs an exclusive lock and would block the collector, so it
is intentionally NOT run here. Given the project's ~20-60 MB/year growth this
is a non-issue. A passive WAL checkpoint at the end (never blocks) folds the
WAL back into the main file.

USAGE
-----
  python prune_old.py 7-5-2026                 # US M-D-YYYY (matches examples)
  python prune_old.py 2026-07-05               # ISO, unambiguous
  python prune_old.py 7-5-2026 --config /path/to/config.ini
  python prune_old.py 7-5-2026 --db /mnt/pmu_data/db/pmu.db
  python prune_old.py 7-5-2026 --dry-run       # preview counts, delete nothing
  python prune_old.py 7-5-2026 --yes           # skip the interactive confirm
  python prune_old.py 7-5-2026 --utc           # cutoff at UTC midnight
  python prune_old.py 7-5-2026 --prune-settings # also trim superseded provenance

The date is interpreted as LOCAL midnight by default (--utc for UTC midnight),
and rows with ts STRICTLY BEFORE that instant are deleted (the cutoff day is
kept). The resolved cutoff is printed in both local and UTC before anything is
deleted, and you must confirm unless --yes is given.
"""

import argparse
import datetime
import sqlite3
import sys
import time
from pathlib import Path

CHUNK_DEFAULT = 5000        # rows per delete transaction
PAUSE_SEC = 0.05           # yield to the collector between chunks


# ----------------------------- date handling --------------------------------

def parse_cutoff(date_str, utc):
    """Parse a date string into (epoch_seconds, aware_datetime_at_midnight).

    Accepts ISO 'YYYY-MM-DD' (unambiguous) or US 'M-D-YYYY' / 'M/D/YYYY'.
    The boundary is midnight - local by default, UTC with --utc.
    """
    s = date_str.strip().replace("/", "-")
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(f"unrecognized date: {date_str!r}")

    if len(parts[0]) == 4:                      # ISO YYYY-MM-DD
        y, m, d = (int(parts[0]), int(parts[1]), int(parts[2]))
    else:                                       # US M-D-YYYY
        m, d, y = (int(parts[0]), int(parts[1]), int(parts[2]))

    if utc:
        dt = datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc)
    else:
        # naive local midnight; .timestamp() applies the local tz (UTC on the
        # GPS-disciplined barn Pi, which is fine).
        dt = datetime.datetime(y, m, d).astimezone()
    return dt.timestamp(), dt


def iso_local(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z")


def iso_utc(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ----------------------------- db helpers -----------------------------------

def open_db(db_path, busy_ms=15000):
    """Open the live db read-write, WAL preserved, with a busy timeout so the
    two writers politely wait for each other."""
    conn = sqlite3.connect(str(db_path), timeout=busy_ms / 1000.0)
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA foreign_keys=OFF")     # explicit; matches the project
    return conn


def count(conn, sql, params):
    return conn.execute(sql, params).fetchone()[0]


def chunked_delete(conn, table, where_sql, params, chunk, pause):
    """Delete rows matching where_sql from table, chunk rows per transaction.

    Uses the portable `rowid IN (SELECT ... LIMIT n)` pattern (stock SQLite
    builds don't support DELETE ... LIMIT). Commits every chunk and briefly
    sleeps so the collector's writer can slip in between chunks.
    """
    total = 0
    while True:
        before = conn.total_changes
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {table} WHERE {where_sql} LIMIT ?)",
            (*params, chunk))
        conn.commit()
        n = conn.total_changes - before
        total += n
        if n < chunk:
            break
        if pause:
            time.sleep(pause)
    return total


# ----------------------------- preview --------------------------------------

def preview(conn, cutoff, prune_settings):
    """Return an ordered list of (label, count) of what WOULD be deleted."""
    rows = [
        ("voltage_measurements",
         count(conn, "SELECT COUNT(*) FROM voltage_measurements WHERE ts < ?",
               (cutoff,))),
        ("frequency_measurements",
         count(conn, "SELECT COUNT(*) FROM frequency_measurements WHERE ts < ?",
               (cutoff,))),
        ("voltage_events",
         count(conn, "SELECT COUNT(*) FROM voltage_events "
                     "WHERE trigger_end < ?", (cutoff,))),
        ("frequency_events",
         count(conn, "SELECT COUNT(*) FROM frequency_events "
                     "WHERE trigger_end < ?", (cutoff,))),
        ("correlated_events",
         count(conn,
               "SELECT COUNT(*) FROM correlated_events WHERE "
               "voltage_event_id IN (SELECT id FROM voltage_events "
               "  WHERE trigger_end < ?) OR "
               "frequency_event_id IN (SELECT id FROM frequency_events "
               "  WHERE trigger_end < ?)", (cutoff, cutoff))),
    ]
    if prune_settings:
        # superseded activations only (boundary + future are kept per device)
        supersed = count(
            conn,
            "SELECT COUNT(*) FROM settings_activations sa WHERE activated_at < "
            "(SELECT MAX(activated_at) FROM settings_activations s "
            " WHERE s.device_id = sa.device_id AND s.activated_at <= ?)",
            (cutoff,))
        rows.append(("settings_activations (superseded)", supersed))
    return rows


# ----------------------------- delete phases --------------------------------

def prune_measurements(conn, cutoff, chunk):
    v = chunked_delete(conn, "voltage_measurements", "ts < ?", (cutoff,),
                       chunk, PAUSE_SEC)
    f = chunked_delete(conn, "frequency_measurements", "ts < ?", (cutoff,),
                       chunk, PAUSE_SEC)
    return v, f


def prune_events(conn, cutoff, chunk):
    # correlations first (they reference the events we're about to remove)
    c = chunked_delete(
        conn, "correlated_events",
        "voltage_event_id IN (SELECT id FROM voltage_events "
        "  WHERE trigger_end < ?) OR "
        "frequency_event_id IN (SELECT id FROM frequency_events "
        "  WHERE trigger_end < ?)",
        (cutoff, cutoff), chunk, PAUSE_SEC)
    v = chunked_delete(conn, "voltage_events", "trigger_end < ?", (cutoff,),
                       chunk, PAUSE_SEC)
    f = chunked_delete(conn, "frequency_events", "trigger_end < ?", (cutoff,),
                       chunk, PAUSE_SEC)
    return v, f, c


def prune_settings(conn, cutoff):
    """Remove superseded provenance only. Keeps, per device, the activation in
    force at the cutoff plus everything after, so retained measurements can
    still resolve their settings. Then drops groups no surviving activation
    references (guarded by created_ts < cutoff to spare a just-created group).
    """
    removed_act = 0
    for (dev_id,) in conn.execute(
            "SELECT DISTINCT device_id FROM settings_activations"):
        before = conn.total_changes
        conn.execute(
            "DELETE FROM settings_activations "
            "WHERE device_id = ? AND activated_at < "
            "(SELECT MAX(activated_at) FROM settings_activations "
            " WHERE device_id = ? AND activated_at <= ?)",
            (dev_id, dev_id, cutoff))
        removed_act += conn.total_changes - before
    conn.commit()

    conn.execute(
        "DELETE FROM settings_values WHERE settings_group_id IN ("
        " SELECT g.id FROM settings_groups g "
        " WHERE g.created_ts < ? AND NOT EXISTS "
        "  (SELECT 1 FROM settings_activations a "
        "   WHERE a.settings_group_id = g.id))",
        (cutoff,))
    before = conn.total_changes
    conn.execute(
        "DELETE FROM settings_groups WHERE created_ts < ? AND id NOT IN "
        "(SELECT DISTINCT settings_group_id FROM settings_activations)",
        (cutoff,))
    removed_grp = conn.total_changes - before
    conn.commit()
    return removed_act, removed_grp


# ----------------------------- main -----------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Delete data older than a cutoff date from the live "
                    "collector database (safe while the service runs).")
    ap.add_argument("date", help="cutoff date: M-D-YYYY (US) or YYYY-MM-DD (ISO)")
    ap.add_argument("--config", default="config.ini",
                    help="path to config.ini (default: config.ini)")
    ap.add_argument("--db", default=None,
                    help="path to the .db, bypassing config.ini")
    ap.add_argument("--utc", action="store_true",
                    help="interpret the date as UTC midnight (default: local)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be deleted, then exit")
    ap.add_argument("--prune-settings", action="store_true",
                    help="also trim superseded settings provenance")
    ap.add_argument("--chunk", type=int, default=CHUNK_DEFAULT,
                    help=f"rows per delete transaction (default {CHUNK_DEFAULT})")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="skip the passive WAL checkpoint at the end")
    args = ap.parse_args()

    try:
        cutoff, dt = parse_cutoff(args.date, args.utc)
    except (ValueError, OverflowError) as e:
        sys.exit(f"Bad date {args.date!r}: {e}")

    if args.db:
        db_path = Path(args.db)
    else:
        import config as config_mod   # only needed when resolving via config.ini
        cfg = config_mod.load(args.config)
        db_path = cfg.db_path
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    now = time.time()
    print(f"Database: {db_path}")
    print(f"Cutoff:   {iso_local(cutoff)}  ({iso_utc(cutoff)})")
    print(f"          deleting rows with ts < {cutoff:.0f}")
    if cutoff >= now:
        print("\n*** WARNING: the cutoff is in the future - this would delete "
              "essentially ALL data. ***")
        if not args.yes:
            sys.exit("Refusing without --yes.")

    conn = open_db(db_path)
    try:
        rows = preview(conn, cutoff, args.prune_settings)
        total = sum(n for _, n in rows)
        print("\nWould delete:")
        for label, n in rows:
            print(f"  {label:<34} {n:>12,}")
        print(f"  {'TOTAL':<34} {total:>12,}")

        if total == 0:
            print("\nNothing to prune.")
            return
        if args.dry_run:
            print("\n--dry-run: no changes made.")
            return

        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit("\nNot a terminal; re-run with --yes to proceed.")
            reply = input("\nType 'yes' to delete the rows above: ").strip()
            if reply != "yes":
                print("Aborted.")
                return

        print("\nPruning (live-safe, chunked)...")
        vm, fm = prune_measurements(conn, cutoff, args.chunk)
        ve, fe, ce = prune_events(conn, cutoff, args.chunk)
        print(f"  voltage_measurements    deleted {vm:,}")
        print(f"  frequency_measurements  deleted {fm:,}")
        print(f"  voltage_events          deleted {ve:,}")
        print(f"  frequency_events        deleted {fe:,}")
        print(f"  correlated_events       deleted {ce:,}")
        if args.prune_settings:
            ra, rg = prune_settings(conn, cutoff)
            print(f"  settings_activations    deleted {ra:,}")
            print(f"  settings_groups         deleted {rg:,}")

        if not args.no_checkpoint:
            # PASSIVE never blocks: it folds back whatever the collector isn't
            # currently reading and returns immediately.
            try:
                busy, log_frames, ckpt = conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                print(f"  WAL checkpoint          "
                      f"{'busy' if busy else 'ok'} "
                      f"({ckpt}/{log_frames} frames folded)")
            except sqlite3.Error as e:
                print(f"  WAL checkpoint          skipped ({e})")

        print("\nDone. (File size unchanged by design - freed pages are reused; "
              "VACUUM is intentionally not run against the live db.)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
