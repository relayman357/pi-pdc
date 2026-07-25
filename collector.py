"""
collector.py - main PDC application

Run:  python collector.py            (uses config.ini in the same folder)
      python collector.py myconf.ini

One thread per device: connects to the meter over IEEE C37.118 (TCP),
feeds every frame into two deadband state machines (voltage + frequency),
which push records to the single database writer thread.

Ctrl+C for a clean shutdown (open events are closed, queue is flushed).

LIVE CONFIG RELOAD (Linux/Pi): send SIGHUP and the collector re-reads
config.ini WITHOUT restarting, so most tuning changes apply with no gap in
collection (see the RELOAD notes below and the WINDOWS RELOAD block).
    sudo systemctl reload pmu-collector      (needs ExecReload in the unit)
    kill -HUP $(systemctl show -p MainPID --value pmu-collector)
"""

import logging
import logging.handlers
import signal
import sys
import threading
import time

import config as config_mod
import platform_io
from c37118 import C37118Client, C37118Error, pick_voltage_index
from sel_fastmsg import SELFastMsgClient, SELFastMsgError
from dbwriter import DBWriter, iso_utc
from deadband import DeadbandRecorder

stop_flag = threading.Event()      # set once, station-wide, on shutdown
reload_flag = threading.Event()    # set by SIGHUP; serviced in the main loop


def _request_reload(signum, frame):
    """SIGHUP handler. Runs in the main thread between bytecodes, so it does
    the minimum safe thing - raise a flag the main loop polls. All the real
    work (re-reading config.ini, diffing, handing new settings to the device
    threads) happens in do_reload() on the main thread."""
    reload_flag.set()


# ---------------------------------------------------------------------------
# WINDOWS RELOAD (NOT IMPLEMENTED - documented for a future change)
# ---------------------------------------------------------------------------
# Windows has no SIGHUP, so the signal path above is Linux/Pi only. To add
# live reload on Windows later WITHOUT changing anything else, drive the same
# reload_flag from a file-watch instead of a signal:
#
#   * Poll config.ini's modification time in the main loop (below). When the
#     mtime advances, set reload_flag - do_reload() then does the rest,
#     unchanged. Debounce by ignoring a change younger than ~1 s so a reload
#     never fires mid-save while an editor is still writing the file.
#       import os
#       _cfg_mtime = os.path.getmtime(ini)
#       ... in the loop ...
#       m = os.path.getmtime(ini)
#       if m != _cfg_mtime and (time.time() - m) > 1.0:
#           _cfg_mtime = m; reload_flag.set()
#
#   * Or watch a sentinel file (e.g. reload.flag) the operator touches, if
#     you'd rather trigger reloads explicitly than on every ini save.
#
# Everything downstream of reload_flag is already cross-platform; only the
# TRIGGER differs by OS. Left for later per project decision.
# ---------------------------------------------------------------------------


def setup_logging(cfg):
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-12s %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    filelog = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "collector.log",
        maxBytes=cfg.log_max_mb * 1024 * 1024,
        backupCount=cfg.log_keep_files)
    filelog.setFormatter(fmt)
    root.addHandler(filelog)


class DeviceThread(threading.Thread):
    """Owns the connection and both state machines for one meter."""

    def __init__(self, dev_cfg, device_id, writer, heartbeat_sec, last_ts,
                 settings_group_id, run_started):
        super().__init__(name=dev_cfg.key, daemon=True)
        self.cfg = dev_cfg
        self.device_id = device_id
        self.writer = writer
        self.heartbeat_sec = heartbeat_sec
        self.log = logging.getLogger(dev_cfg.key)
        self.last_sample_ts = None
        # Settings provenance: stamp one activation edge per run, at this
        # device's first sample, so the boundary is in the meter clock frame.
        # A live reload that changes the config re-arms this (see _apply_pending).
        self.settings_group_id = settings_group_id
        self.run_started = run_started
        self._activation_recorded = False

        # Live-reload plumbing. The main thread stashes a new config in
        # _pending under the lock; THIS thread applies it between samples so
        # it never races its own recorders. _stop_self ends just this device
        # (used when a reload removes/disables it), independent of stop_flag.
        self._stop_self = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending = None       # (new_dev_cfg, new_group, new_hb, reload_ts)

        self.v_rec = DeadbandRecorder(
            "voltage", dev_cfg.voltage_band, dev_cfg.voltage_post_trig_sec,
            heartbeat_sec, writer.put_measurement, writer.put_event, device_id,
            baseline_time_const=dev_cfg.rolling_baseline_time_const,
            max_event_sec=dev_cfg.max_event_sec,
            pre_trigger_sec=dev_cfg.voltage_pre_trig_sec,
            capture_all=dev_cfg.capture_all_voltage_samples)
        self.f_rec = DeadbandRecorder(
            "frequency", dev_cfg.freq_band_hz, dev_cfg.freq_post_trig_sec,
            heartbeat_sec, writer.put_measurement, writer.put_event, device_id,
            baseline_time_const=dev_cfg.rolling_baseline_time_const,
            max_event_sec=dev_cfg.max_event_sec,
            pre_trigger_sec=dev_cfg.freq_pre_trig_sec,
            capture_all=dev_cfg.capture_all_freq_samples)

        # Make capture-all conspicuous: it changes storage volume by orders of
        # magnitude and makes pre_trig/heartbeat moot for that signal.
        self._log_capture_all_warnings(dev_cfg)

        # GAP detection: if the archive already has data for this device,
        # write a GAP marker so the unverified period is flagged.
        self._write_gap_markers(last_ts.get("voltage"), last_ts.get("frequency"),
                                "previous data ends before this run started")

    def _log_capture_all_warnings(self, cfg, prev=None):
        """Warn (once) when capture_all is ON for a signal. With prev given
        (a reload), warn only on an OFF->ON transition, so a reload that
        leaves it already-on stays quiet."""
        for sig, on, was_on in (
                ("voltage", cfg.capture_all_voltage_samples,
                 prev.capture_all_voltage_samples if prev else False),
                ("frequency", cfg.capture_all_freq_samples,
                 prev.capture_all_freq_samples if prev else False)):
            if on and (prev is None or not was_on):
                self.log.warning(
                    "%s: capture_all is ON for %s - storing EVERY sample "
                    "(~%.1fM rows/day at %d/s); heartbeats stop and "
                    "pre_trig_sec is ignored for this signal",
                    cfg.key, sig, cfg.data_rate * 86400 / 1e6, cfg.data_rate)

    def _write_gap_markers(self, prev_v, prev_f, reason):
        """Flag an unverified period per signal (startup or reload-reconnect).
        prev_* are the last known ts for each signal, or None to skip."""
        now = time.time()
        for signal_name, prev in (("voltage", prev_v), ("frequency", prev_f)):
            if prev is not None:
                self.log.warning(
                    "%s: %s (%s) - writing GAP marker; the period since then "
                    "is unverified", signal_name, reason, iso_utc(prev))
                self.writer.put_measurement({
                    "device_id": self.device_id, "signal": signal_name,
                    "ts": now, "value": 0.0, "record_type": "GAP"})

    # ---------- called from the MAIN thread ----------

    def apply_reload(self, new_dev_cfg, new_group, new_heartbeat_sec, reload_ts):
        """Hand new settings to this thread. Non-blocking: the actual apply
        happens in _apply_pending() on this device's own thread."""
        with self._pending_lock:
            self._pending = (new_dev_cfg, new_group, new_heartbeat_sec, reload_ts)

    def stop_self(self):
        """End just this device's thread (reload removed/disabled it)."""
        self._stop_self.set()

    # ---------- runs on THIS thread ----------

    def _make_client(self):
        if self.cfg.protocol == "sel_fastmsg":
            return SELFastMsgClient(self.cfg.ip, self.cfg.port,
                                    self.cfg.idcode, self.cfg.data_rate,
                                    self.cfg.connect_timeout_sec,
                                    self.cfg.transport)
        return C37118Client(self.cfg.ip, self.cfg.port, self.cfg.idcode,
                            self.cfg.connect_timeout_sec)

    def _apply_pending(self, client):
        """Apply a pending live reload. Returns True if the CONNECTION must be
        rebuilt (a connection-level setting changed), in which case the caller
        breaks the stream so run() reconnects with the new settings. Detection
        settings (bands, nominals, heartbeat, pre/post-trig, baseline const,
        max-event, capture_all) are pushed into the recorders here with no gap."""
        with self._pending_lock:
            pending, self._pending = self._pending, None
        if pending is None:
            return False
        new_cfg, new_group, new_hb, reload_ts = pending
        old = self.cfg

        # Connection identity - any change means we must reconnect this device.
        reconnect = (new_cfg.protocol != old.protocol or
                     new_cfg.transport != old.transport or
                     new_cfg.ip != old.ip or
                     new_cfg.port != old.port or
                     new_cfg.idcode != old.idcode or
                     new_cfg.data_rate != old.data_rate)

        # Tier-1: detection settings, applied live to both recorders. Validate
        # BOTH first (apply=False), then commit, so a rejected value leaves the
        # whole device untouched - old cfg, old recorders, no reconnect, no
        # regroup. config.load() already validated these upstream, so this is
        # belt-and-suspenders, but it keeps a bad reload strictly atomic.
        v_kw = dict(band=new_cfg.voltage_band,
                    post_trigger_sec=new_cfg.voltage_post_trig_sec,
                    heartbeat_sec=new_hb,
                    baseline_time_const=new_cfg.rolling_baseline_time_const,
                    max_event_sec=new_cfg.max_event_sec,
                    pre_trigger_sec=new_cfg.voltage_pre_trig_sec,
                    capture_all=new_cfg.capture_all_voltage_samples)
        f_kw = dict(band=new_cfg.freq_band_hz,
                    post_trigger_sec=new_cfg.freq_post_trig_sec,
                    heartbeat_sec=new_hb,
                    baseline_time_const=new_cfg.rolling_baseline_time_const,
                    max_event_sec=new_cfg.max_event_sec,
                    pre_trigger_sec=new_cfg.freq_pre_trig_sec,
                    capture_all=new_cfg.capture_all_freq_samples)
        try:
            self.v_rec.reconfigure(apply=False, **v_kw)
            self.f_rec.reconfigure(apply=False, **f_kw)
        except ValueError as e:
            self.log.error("reload: rejected settings for %s (%s); keeping "
                           "previous config entirely", self.cfg.key, e)
            return False

        # All valid - commit atomically.
        self.cfg = new_cfg
        self.heartbeat_sec = new_hb
        changed = ([("voltage", *c) for c in self.v_rec.reconfigure(**v_kw)] +
                   [("frequency", *c) for c in self.f_rec.reconfigure(**f_kw)])
        for sig, name, o, n in changed:
            self.log.info("reload: %s %s %s -> %s", sig, name, o, n)
        # If capture_all just switched on for a signal, shout about the volume.
        self._log_capture_all_warnings(new_cfg, prev=old)

        # Provenance: if the whole-config group changed, stamp a fresh
        # activation at the next sample (meter-frame boundary) and refresh the
        # audit run_started so status.txt shows when this config came into use.
        if new_group is not None and new_group != self.settings_group_id:
            self.settings_group_id = new_group
            self.run_started = reload_ts
            self._activation_recorded = False
            self.log.info("reload: now recording under settings group %d",
                          new_group)

        if reconnect:
            self.log.info("reload: connection settings changed "
                          "(%s:%s idcode %s %s) - reconnecting this device",
                          new_cfg.ip, new_cfg.port, new_cfg.idcode,
                          new_cfg.protocol)
        return reconnect

    def run(self):
        while not stop_flag.is_set() and not self._stop_self.is_set():
            client = self._make_client()
            reload_reconnect = False
            try:
                self.log.info("Connecting to %s:%s (%s, idcode %s)...",
                              self.cfg.ip, self.cfg.port, self.cfg.protocol,
                              self.cfg.idcode)
                client.connect()
                pmu = client.pmu_cfg
                v_idx = pick_voltage_index(pmu, self.cfg.voltage_channel)
                self.log.info(
                    "Connected. Station '%s', %d phasors %s, recording %s, "
                    "fnom %g Hz, rate %d/s",
                    pmu.station, len(pmu.phasors), pmu.phasors,
                    pmu.phasors[v_idx].name, pmu.fnom,
                    client.stream_cfg.data_rate)
                DBWriter.update_station(self.writer.db_path, self.device_id,
                                        pmu.station)
                if client.stream_cfg.data_rate != self.cfg.data_rate:
                    self.log.warning(
                        "Meter data rate %d != config data_rate %d "
                        "(check MRATE)", client.stream_cfg.data_rate,
                        self.cfg.data_rate)

                reload_reconnect = self._stream(client, v_idx)

            except (OSError, C37118Error, SELFastMsgError) as e:
                self.log.error("Connection lost/failed: %s", e)
            finally:
                client.close()

            if stop_flag.is_set() or self._stop_self.is_set():
                break
            if reload_reconnect:
                # A deliberate, brief gap for THIS device only. Flag it (so
                # integrity stays provable) and reconnect immediately with the
                # new connection settings - no reconnect_delay wait.
                self._write_gap_markers(
                    self.v_rec.last_record_ts, self.f_rec.last_record_ts,
                    "reconnecting to apply new connection settings")
                continue
            self.log.info("Reconnecting in %.0f s...",
                          self.cfg.reconnect_delay_sec)
            stop_flag.wait(self.cfg.reconnect_delay_sec)

        # Clean shutdown: close any open events at the last known time
        ts = self.last_sample_ts or time.time()
        self.v_rec.flush(ts)
        self.f_rec.flush(ts)
        self.log.info("Stopped.")

    def _stream(self, client, v_idx):
        """Stream samples until stop, self-stop, or a reload that needs a
        reconnect. Returns True only in that last case (caller reconnects)."""
        bad_stat_logged = False
        for s in client.samples():
            if stop_flag.is_set() or self._stop_self.is_set():
                return False

            # Apply a pending live reload at a safe point (between samples).
            if self._pending is not None:
                if self._apply_pending(client):
                    return True                     # connection must rebuild
                # A non-reconnect reload may have changed voltage_channel;
                # re-resolve the phasor index. Keep the old one if the new
                # channel name isn't present on this meter.
                try:
                    v_idx = pick_voltage_index(client.pmu_cfg,
                                               self.cfg.voltage_channel)
                except Exception as e:
                    self.log.warning("reload: voltage_channel %r not usable "
                                     "(%s); keeping previous channel",
                                     self.cfg.voltage_channel, e)

            self.last_sample_ts = s.timestamp

            # First sample of this run (or first after a group change): record
            # which settings group is active, stamped with the meter-frame
            # timestamp (same frame as every ts we store).
            if not self._activation_recorded:
                self.writer.put_activation({
                    "settings_group_id": self.settings_group_id,
                    "device_id": self.device_id,
                    "activated_at": s.timestamp,
                    "run_started": self.run_started,
                    "source": self.cfg.key,
                })
                self._activation_recorded = True

            if not s.stat_ok:
                if not bad_stat_logged:
                    self.log.warning(
                        "STAT=0x%04X indicates a data problem in the meter "
                        "(will log once until it clears)", s.stat)
                    bad_stat_logged = True
            elif bad_stat_logged:
                self.log.info("STAT cleared - data good again")
                bad_stat_logged = False

            mag, ang = s.phasors[v_idx]
            self.v_rec.process(s.timestamp, mag,
                               {"angle": ang, "stat": s.stat})
            self.f_rec.process(s.timestamp, s.freq,
                               {"rocof": s.rocof, "stat": s.stat})
        return False


def do_reload(ini, threads, writer, log):
    """Re-read config.ini and apply it live. Runs on the main thread.

    Never fatal: a bad config.ini is rejected and the running config kept.
    Existing devices get new settings handed to their threads (applied with
    no gap unless a connection setting changed); removed/disabled devices are
    stopped; newly added/enabled devices get a fresh thread.
    """
    # config.load() calls sys.exit() on any validation error; catch that so a
    # typo in config.ini can never take the running collector down.
    try:
        new_cfg = config_mod.load(ini)
        new_rows = config_mod.settings_snapshot(ini)
    except SystemExit as e:
        log.error("reload: config.ini rejected (%s) - keeping running config",
                  e)
        return

    reload_ts = time.time()
    # Reuse initialize(): upserts device rows (updates ip/name/etc, inserts any
    # new device), resolves the settings group for the new snapshot (new group
    # only if something changed), and returns fresh ids/last_ts. Its own
    # short-lived WAL connection is safe alongside the writer thread.
    device_ids, last_ts, new_group = DBWriter.initialize(
        writer.db_path, reload_ts, new_rows, new_cfg.devices)
    log.info("reload: config re-read; active settings group %d", new_group)

    new_by_key = {d.key: d for d in new_cfg.devices}
    old_keys, new_keys = set(threads), set(new_by_key)

    for key in old_keys - new_keys:
        log.info("reload: device %s removed/disabled - stopping its thread", key)
        threads[key].stop_self()
        threads[key].join(timeout=10)
        del threads[key]

    for key in old_keys & new_keys:
        threads[key].apply_reload(new_by_key[key], new_group,
                                  new_cfg.heartbeat_sec, reload_ts)

    for key in new_keys - old_keys:
        dev = new_by_key[key]
        dev_id = device_ids[key]
        log.info("reload: device %s added/enabled - starting its thread", key)
        t = DeviceThread(dev, dev_id, writer, new_cfg.heartbeat_sec,
                         last_ts.get(dev_id, {}), new_group, reload_ts)
        t.start()
        threads[key] = t


def main():
    ini = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    cfg = config_mod.load(ini)
    setup_logging(cfg)
    log = logging.getLogger("main")

    log.info("=== PMU Data Collector starting - station '%s' ===",
             cfg.station_name)
    platform_io.power_check(cfg.power_check)
    platform_io.service_hints()

    run_started = time.time()
    device_ids, last_ts, settings_group_id = DBWriter.initialize(
        cfg.db_path, run_started, config_mod.settings_snapshot(ini),
        cfg.devices)
    log.info("Database ready: %s (settings group %d)",
             cfg.db_path, settings_group_id)

    writer = DBWriter(cfg.db_path, cfg.batch_seconds,
                      logging.getLogger("dbwriter"))
    writer.start()

    threads = {}
    for dev in cfg.devices:
        dev_id = device_ids[dev.key]
        t = DeviceThread(dev, dev_id, writer, cfg.heartbeat_sec,
                         last_ts.get(dev_id, {}), settings_group_id,
                         run_started)
        t.start()
        threads[dev.key] = t

    # Live reload via SIGHUP (Linux/Pi). No-op on OSes without SIGHUP; see the
    # WINDOWS RELOAD note above for the file-watch alternative.
    reload_enabled = hasattr(signal, "SIGHUP")
    if reload_enabled:
        signal.signal(signal.SIGHUP, _request_reload)
    log.info("%d device thread(s) running. Ctrl+C to stop%s.",
             len(threads),
             "; SIGHUP (systemctl reload) to apply config.ini live"
             if reload_enabled else "")

    try:
        while any(t.is_alive() for t in threads.values()):
            if reload_flag.is_set():
                reload_flag.clear()
                log.info("reload: SIGHUP received - re-reading %s", ini)
                try:
                    do_reload(ini, threads, writer, log)
                except Exception:
                    log.exception("reload: failed - keeping running config")
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Shutdown requested...")
        stop_flag.set()
        for t in threads.values():
            t.join(timeout=10)

    writer.stop()
    writer.join(timeout=10)
    log.info("=== Collector stopped ===")


if __name__ == "__main__":
    main()
