"""
dbwriter.py - SQLite schema + batched writer thread

One writer thread owns the SQLite connection. Device threads push records
onto a queue; the writer commits them in one transaction per batch_seconds.
This keeps worst-case multi-device event load (hundreds of inserts/sec)
comfortable, and SQLite likes a single writer.

Timestamps are stored twice per row on purpose:
  ts      REAL  - unix epoch seconds (compact, precise, sortable, plottable)
  ts_utc  TEXT  - ISO-8601 UTC string (human-friendly in any db browser)

SETTINGS PROVENANCE
-------------------
Every measurement/event can be tied back to the exact configuration in effect
when it was captured, via two tables:

  settings_groups      - one row per DISTINCT effective config (deduplicated by
                         a content hash). A restart with identical settings
                         reuses the existing group; changing any setting creates
                         a new group. settings_values holds the full snapshot.

  settings_activations - one row per (group, device) edge. activated_at is
                         stamped from that DEVICE'S FIRST SAMPLE of the run, so
                         it lives in the SAME clock frame as every measurement
                         ts (the meter/IRIG frame - see the ~18 s GPS/UTC note
                         in the project notes). The viewer resolves "settings in
                         effect for measurement M on device D" as: the activation
                         for D with the greatest activated_at <= M.ts. Because
                         both sides are meter-frame, that boundary is exact.

"""

import datetime
import hashlib
import json
import queue
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,     -- config section, e.g. device:meter_1
    name        TEXT,
    ip          TEXT,
    port        INTEGER,
    idcode      INTEGER,
    station     TEXT                      -- PMSTN from the meter's CFG-2
);

CREATE TABLE IF NOT EXISTS voltage_measurements (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    ts          REAL NOT NULL,
    ts_utc      TEXT NOT NULL,
    magnitude   REAL NOT NULL,
    angle       REAL,
    stat        INTEGER,
    record_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vm_dev_ts ON voltage_measurements(device_id, ts);

CREATE TABLE IF NOT EXISTS frequency_measurements (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    ts          REAL NOT NULL,
    ts_utc      TEXT NOT NULL,
    frequency   REAL NOT NULL,
    rocof       REAL,
    stat        INTEGER,
    record_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fm_dev_ts ON frequency_measurements(device_id, ts);

CREATE TABLE IF NOT EXISTS voltage_events (
    id               INTEGER PRIMARY KEY,
    device_id        INTEGER NOT NULL REFERENCES devices(id),
    trigger_start    REAL NOT NULL,
    band_return      REAL,
    trigger_end      REAL NOT NULL,
    peak_magnitude   REAL,
    min_magnitude    REAL,
    samples_stored   INTEGER,
    band_volts       REAL,
    post_trigger_sec REAL
);

CREATE TABLE IF NOT EXISTS frequency_events (
    id               INTEGER PRIMARY KEY,
    device_id        INTEGER NOT NULL REFERENCES devices(id),
    trigger_start    REAL NOT NULL,
    band_return      REAL,
    trigger_end      REAL NOT NULL,
    peak_frequency   REAL,
    min_frequency    REAL,
    samples_stored   INTEGER,
    band_hz          REAL,
    post_trigger_sec REAL
);

CREATE TABLE IF NOT EXISTS correlated_events (
    id                 INTEGER PRIMARY KEY,
    device_id          INTEGER NOT NULL,
    voltage_event_id   INTEGER REFERENCES voltage_events(id),
    frequency_event_id INTEGER REFERENCES frequency_events(id)
);

-- One row per DISTINCT effective configuration (deduplicated by content hash).
CREATE TABLE IF NOT EXISTS settings_groups (
    id          INTEGER PRIMARY KEY,
    hash        TEXT UNIQUE NOT NULL,     -- sha256 of the sorted snapshot
    created_ts  REAL NOT NULL,            -- wall clock when first seen (info only)
    created_utc TEXT NOT NULL
);

-- The full config.ini snapshot, one row per key, belonging to a group.
CREATE TABLE IF NOT EXISTS settings_values (
    id                INTEGER PRIMARY KEY,
    settings_group_id INTEGER NOT NULL REFERENCES settings_groups(id),
    section           TEXT NOT NULL,
    key               TEXT NOT NULL,
    value             TEXT
);
CREATE INDEX IF NOT EXISTS idx_sv_group ON settings_values(settings_group_id);

-- Per-device activation edges. activated_at is the device's first-sample
-- timestamp (meter frame) - directly comparable to every measurement ts.
CREATE TABLE IF NOT EXISTS settings_activations (
    id                INTEGER PRIMARY KEY,
    settings_group_id INTEGER NOT NULL REFERENCES settings_groups(id),
    device_id         INTEGER NOT NULL REFERENCES devices(id),
    activated_at      REAL NOT NULL,      -- meter-frame ts of the first sample
    activated_utc     TEXT NOT NULL,
    run_started       REAL NOT NULL,      -- Pi wall clock at collector start (audit)
    source            TEXT NOT NULL       -- device key that stamped it
);
CREATE INDEX IF NOT EXISTS idx_act_dev ON settings_activations(device_id, activated_at);
"""


def iso_utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------- settings-group helpers (module-level, take a conn) ----------

def settings_hash(rows) -> str:
    """Stable sha256 of a settings snapshot [(section, key, value), ...].

    Dedup is over the WHOLE snapshot, so any change - even a cosmetic one like
    log_level - starts a new group (a complete audit trail). If cosmetic churn
    ever fragments the record, hash only the detection-relevant keys here.
    """
    payload = json.dumps(
        sorted([str(s), str(k), "" if v is None else str(v)] for s, k, v in rows),
        separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_or_create_group(conn, rows, created_ts=None) -> int:
    """Return the settings_groups.id for this snapshot, creating it (and its
    settings_values rows) only if this exact configuration is not yet stored."""
    h = settings_hash(rows)
    row = conn.execute("SELECT id FROM settings_groups WHERE hash=?", (h,)).fetchone()
    if row:
        return row[0]
    if created_ts is None:
        created_ts = time.time()
    cur = conn.execute(
        "INSERT INTO settings_groups (hash, created_ts, created_utc) VALUES (?,?,?)",
        (h, created_ts, iso_utc(created_ts)))
    gid = cur.lastrowid
    conn.executemany(
        "INSERT INTO settings_values (settings_group_id, section, key, value) "
        "VALUES (?,?,?,?)",
        [(gid, s, k, v) for s, k, v in rows])
    return gid


class DBWriter(threading.Thread):
    """Owns the SQLite connection. put() is thread-safe."""

    _SENTINEL = object()

    def __init__(self, db_path, batch_seconds=1.0, log=None):
        super().__init__(name="dbwriter", daemon=True)
        self.db_path = str(db_path)
        self.batch_seconds = batch_seconds
        self.q = queue.Queue(maxsize=100000)
        self.log = log

    # ---------- called from other threads ----------

    def put_measurement(self, rec):
        """rec comes from DeadbandRecorder.record_sink."""
        self.q.put(("measurement", rec))

    def put_event(self, ev):
        """ev comes from DeadbandRecorder.event_sink."""
        self.q.put(("event", ev))

    def put_activation(self, act):
        """act = settings-activation edge, stamped at a device's first sample.
        Keys: settings_group_id, device_id, activated_at, run_started, source."""
        self.q.put(("activation", act))

    def stop(self):
        self.q.put(self._SENTINEL)

    # ---------- setup helpers (called from main thread, own connection) ----------

    @staticmethod
    def _open(db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @classmethod
    def initialize(cls, db_path, run_started, settings_rows, devices):
        """
        Create schema, upsert devices, and resolve this run's settings group.

        Returns:
          device_ids        {device_key: device_id}
          last_ts           {device_id: {"voltage": ts|None, "frequency": ts|None}}
                            (last heartbeat per signal, for GAP detection)
          settings_group_id id of the group matching this run's config; device
                            threads stamp an activation against it at first sample
        """
        conn = cls._open(db_path)
        conn.executescript(SCHEMA)

        ids = {}
        for dev in devices:
            cur = conn.execute("SELECT id FROM devices WHERE key=?", (dev.key,))
            row = cur.fetchone()
            if row:
                ids[dev.key] = row[0]
                conn.execute(
                    "UPDATE devices SET name=?, ip=?, port=?, idcode=? WHERE id=?",
                    (dev.name, dev.ip, dev.port, dev.idcode, row[0]))
            else:
                cur = conn.execute(
                    "INSERT INTO devices (key,name,ip,port,idcode) VALUES (?,?,?,?,?)",
                    (dev.key, dev.name, dev.ip, dev.port, dev.idcode))
                ids[dev.key] = cur.lastrowid

        # Resolve (find-or-create) the settings group for the config now in force.
        settings_group_id = find_or_create_group(
            conn, settings_rows, created_ts=run_started)

        last_ts = {}
        for dev_id in ids.values():
            last_ts[dev_id] = {}
            for signal, table in (("voltage", "voltage_measurements"),
                                  ("frequency", "frequency_measurements")):
                cur = conn.execute(
                    f"SELECT MAX(ts) FROM {table} WHERE device_id=?", (dev_id,))
                last_ts[dev_id][signal] = cur.fetchone()[0]

        conn.commit()
        conn.close()
        return ids, last_ts, settings_group_id

    @staticmethod
    def update_station(db_path, device_id, station):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE devices SET station=? WHERE id=?",
                     (station, device_id))
        conn.commit()
        conn.close()

    # ---------- writer thread ----------

    def run(self):
        conn = self._open(self.db_path)
        pending = 0
        try:
            while True:
                try:
                    item = self.q.get(timeout=self.batch_seconds)
                except queue.Empty:
                    if pending:
                        conn.commit()
                        pending = 0
                    continue

                if item is self._SENTINEL:
                    break

                kind, payload = item
                try:
                    if kind == "measurement":
                        self._insert_measurement(conn, payload)
                    elif kind == "event":
                        self._insert_event(conn, payload)
                    else:  # "activation"
                        self._insert_activation(conn, payload)
                    pending += 1
                except sqlite3.Error as e:
                    if self.log:
                        self.log.error("DB insert failed: %s (%s)", e, payload)

                if pending >= 500:
                    conn.commit()
                    pending = 0
        finally:
            conn.commit()
            conn.close()

    @staticmethod
    def _insert_measurement(conn, r):
        if r["signal"] == "voltage":
            conn.execute(
                "INSERT INTO voltage_measurements "
                "(device_id, ts, ts_utc, magnitude, angle, stat, record_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["device_id"], r["ts"], iso_utc(r["ts"]), r["value"],
                 r.get("angle"), r.get("stat"), r["record_type"]))
        else:
            conn.execute(
                "INSERT INTO frequency_measurements "
                "(device_id, ts, ts_utc, frequency, rocof, stat, record_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["device_id"], r["ts"], iso_utc(r["ts"]), r["value"],
                 r.get("rocof"), r.get("stat"), r["record_type"]))

    @staticmethod
    def _insert_activation(conn, a):
        conn.execute(
            "INSERT INTO settings_activations (settings_group_id, device_id, "
            "activated_at, activated_utc, run_started, source) "
            "VALUES (?,?,?,?,?,?)",
            (a["settings_group_id"], a["device_id"], a["activated_at"],
             iso_utc(a["activated_at"]), a["run_started"], a["source"]))

    @staticmethod
    def _insert_event(conn, e):
        if e["signal"] == "voltage":
            cur = conn.execute(
                "INSERT INTO voltage_events "
                "(device_id, trigger_start, band_return, trigger_end, "
                " peak_magnitude, min_magnitude, samples_stored, band_volts, "
                " post_trigger_sec) VALUES (?,?,?,?,?,?,?,?,?)",
                (e["device_id"], e["trigger_start"], e["band_return"],
                 e["trigger_end"], e["peak"], e["min"], e["samples_stored"],
                 e["band"], e["post_trigger_sec"]))
            new_id, table, other = cur.lastrowid, "voltage", "frequency_events"
        else:
            cur = conn.execute(
                "INSERT INTO frequency_events "
                "(device_id, trigger_start, band_return, trigger_end, "
                " peak_frequency, min_frequency, samples_stored, band_hz, "
                " post_trigger_sec) VALUES (?,?,?,?,?,?,?,?,?)",
                (e["device_id"], e["trigger_start"], e["band_return"],
                 e["trigger_end"], e["peak"], e["min"], e["samples_stored"],
                 e["band"], e["post_trigger_sec"]))
            new_id, table, other = cur.lastrowid, "frequency", "voltage_events"

        # Correlate with any overlapping event of the other signal
        cur = conn.execute(
            f"SELECT id FROM {other} WHERE device_id=? AND "
            "trigger_start <= ? AND trigger_end >= ?",
            (e["device_id"], e["trigger_end"], e["trigger_start"]))
        for (other_id,) in cur.fetchall():
            v_id = new_id if table == "voltage" else other_id
            f_id = other_id if table == "voltage" else new_id
            conn.execute(
                "INSERT INTO correlated_events "
                "(device_id, voltage_event_id, frequency_event_id) VALUES (?,?,?)",
                (e["device_id"], v_id, f_id))
