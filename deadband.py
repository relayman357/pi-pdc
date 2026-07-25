"""
deadband.py - deadband compression state machine

One DeadbandRecorder instance per signal (voltage, frequency) per device.
Pure logic, no I/O - fully testable with fake data.

States:
  NORMAL    - within band; only heartbeats are written
  TRIGGERED - out of band, or inside the post-trigger window;
              every sample is written

Rules (see project notes):
  - The "baseline" is a rolling exponential moving average of the signal,
    updated on every in-band sample with time constant
    rolling_baseline_time_const. It follows slow quiescent drift (the
    service level wandering over hours) but cannot chase a fast edge.
  - Trigger levels are baseline +/- band. The band half-width is fixed
    (voltage_band_pct x voltage_nominal), only its center moves.
  - On trigger, the baseline FREEZES at its pre-event value. "Back in
    band" during an event means the value returned to within band of
    that frozen baseline - never of the disturbed values themselves.
  - The post-trigger countdown starts at band return and restarts if
    the signal breaks band again, so a sag and its recovery are captured
    as one event even if they are seconds apart.
  - If the signal settles at a NEW level and never returns to band
    (e.g. a tap change), the event is force-closed after max_event_sec.
  - When an event closes, the baseline is re-seeded with the mean of the
    last reseed_window_sec (default 1 s) of stored samples, so recording
    resumes centered on wherever the signal actually landed. This is why
    post_trigger_sec must be greater than reseed_window_sec.
  - PRE-TRIGGER CONTEXT: while NORMAL, the last pre_trigger_sec of samples
    is held in a small rolling buffer (capped at MAX_PRE_TRIGGER_SEC = 10 s;
    ~200 tuples at 20/s). On trigger, the buffer is flushed to storage as
    ordinary SAMPLE records, so an event capture begins up to
    pre_trigger_sec BEFORE trigger_start. No new record type: the rows
    carry their true timestamps, and ts < trigger_start marks them as
    pre-trigger context. Event summaries (trigger_start, peak, min,
    samples_stored) are NOT affected by pre-trigger samples. A sample
    already written as a HEARTBEAT is skipped on flush (no duplicate row).
    pre_trigger_sec = 0 disables the buffer entirely.

Record types:
  START, HEARTBEAT, TRIGGER_START, SAMPLE, BAND_RETURN, TRIGGER_END, GAP
"""

import math
from collections import deque

NORMAL = "NORMAL"
TRIGGERED = "TRIGGERED"

RESEED_WINDOW_SEC = 1.0
MAX_PRE_TRIGGER_SEC = 10.0   # cap on the pre-trigger buffer (memory bound)


class DeadbandRecorder:
    def __init__(self, signal, band, post_trigger_sec, heartbeat_sec,
                 record_sink, event_sink, device_id,
                 baseline_time_const=60.0, max_event_sec=120.0,
                 pre_trigger_sec=0.0, capture_all=False):
        """
        signal:              "voltage" or "frequency" (used in records)
        band:                half-width of the deadband, in signal units
        post_trigger_sec:    recording continues this long after band return;
                             must be > RESEED_WINDOW_SEC (1 s) because the
                             last 1 s of the event re-seeds the baseline
        pre_trigger_sec:     rolling buffer of in-band samples flushed to
                             storage (as SAMPLE records) when a trigger fires;
                             0 disables; capped at MAX_PRE_TRIGGER_SEC
        capture_all:         store EVERY sample as a SAMPLE record regardless
                             of state. The state machine still runs (triggers,
                             events, special record types all unchanged);
                             this just back-fills whatever it skipped.
                             Heartbeats naturally stop (never a gap to prove)
                             and the pre-trigger buffer is bypassed (nothing
                             missing to flush).
        baseline_time_const: EMA time constant (s) of the rolling baseline
        max_event_sec:       force-close an event that never returns to band
        record_sink:         callable(record_dict) - receives every stored record
        event_sink:          callable(event_dict)  - receives event summaries
        """
        if not (0.0 <= pre_trigger_sec <= MAX_PRE_TRIGGER_SEC):
            raise ValueError(
                f"{signal}: pre_trigger_sec must be between 0 and "
                f"{MAX_PRE_TRIGGER_SEC} s, got {pre_trigger_sec}")
        if post_trigger_sec <= RESEED_WINDOW_SEC:
            raise ValueError(
                f"{signal}: post_trigger_sec must be > {RESEED_WINDOW_SEC} s "
                f"(the last {RESEED_WINDOW_SEC} s of each event re-seeds the "
                f"baseline), got {post_trigger_sec}")
        if baseline_time_const <= 0:
            raise ValueError(
                f"{signal}: rolling_baseline_time_const must be > 0, "
                f"got {baseline_time_const}")
        if max_event_sec <= post_trigger_sec:
            raise ValueError(
                f"{signal}: max_event_sec ({max_event_sec}) must be > "
                f"post_trigger_sec ({post_trigger_sec})")

        self.signal = signal
        self.band = band
        self.post_trigger_sec = post_trigger_sec
        self.pre_trigger_sec = pre_trigger_sec
        self.capture_all = capture_all
        self.heartbeat_sec = heartbeat_sec
        self.baseline_time_const = baseline_time_const
        self.max_event_sec = max_event_sec
        self.record_sink = record_sink
        self.event_sink = event_sink
        self.device_id = device_id

        self.state = NORMAL
        self.baseline = None        # rolling EMA; frozen while TRIGGERED
        self.last_record_ts = None
        self.in_band_since = None
        self.event = None           # accumulating event summary
        self.started = False
        self._prev_ts = None        # previous sample time (EMA dt)
        self._tail = deque()        # (ts, value) trailing samples in an event
        self._pre = deque()         # (ts, value, extra) rolling pre-trigger
                                    # buffer, filled only in NORMAL state

    # ---------------- public API ----------------

    def process(self, ts, value, extra=None):
        """
        Feed one sample. ts = unix epoch seconds (from the PMU frame),
        value = signal value, extra = dict of additional columns
        (e.g. {"angle": ...} or {"rocof": ...}).
        """
        extra = extra or {}

        if not self.started:
            self._write(ts, value, "START", extra)
            self.baseline = value
            self.started = True
            self._prev_ts = ts
            return

        if self.state == NORMAL:
            self._process_normal(ts, value, extra)
        else:
            self._process_triggered(ts, value, extra)

        # capture_all: the state machine stored this sample iff it stamped
        # last_record_ts with this exact ts; back-fill anything it skipped.
        if self.capture_all and self.last_record_ts != ts:
            self._write(ts, value, "SAMPLE", extra)

        self._prev_ts = ts

    def flush(self, ts):
        """Call on shutdown: close any open event."""
        if self.state == TRIGGERED and self.event:
            self.event["trigger_end"] = ts
            self.event_sink(self.event)
            self.event = None

    def reconfigure(self, *, band=None, post_trigger_sec=None,
                    heartbeat_sec=None, baseline_time_const=None,
                    max_event_sec=None, pre_trigger_sec=None,
                    capture_all=None, apply=True):
        """Live-update detection settings on a running recorder (no restart).

        Call from the OWNING device thread only (so it never races process()).
        Arguments left as None are unchanged. The pre/post_trigger_sec,
        max_event_sec and baseline_time_const invariants are re-checked
        against the resulting combination, matching __init__; on a bad
        combination this raises ValueError and changes nothing. Returns a list
        of (name, old, new) for fields that actually changed, for logging.
        apply=False validates and returns that list WITHOUT mutating, so a
        caller can pre-check several recorders and apply only if all pass.

        Applying mid-event is safe: new thresholds take effect for the rest of
        the ongoing return-to-band / timeout tests, while the event SUMMARY
        keeps the band/post_trigger_sec captured at trigger time. A change to
        pre_trigger_sec clears the rolling pre-trigger buffer (it re-fills
        within the new window) so a later trigger never flushes context that
        predates the new setting. Toggling capture_all takes effect on the
        next sample - no reset needed.
        """
        new_post = self.post_trigger_sec if post_trigger_sec is None \
            else post_trigger_sec
        new_max = self.max_event_sec if max_event_sec is None else max_event_sec
        new_btc = self.baseline_time_const if baseline_time_const is None \
            else baseline_time_const
        new_pre = self.pre_trigger_sec if pre_trigger_sec is None \
            else pre_trigger_sec
        if not (0.0 <= new_pre <= MAX_PRE_TRIGGER_SEC):
            raise ValueError(
                f"{self.signal}: pre_trigger_sec must be between 0 and "
                f"{MAX_PRE_TRIGGER_SEC} s, got {new_pre}")
        if new_post <= RESEED_WINDOW_SEC:
            raise ValueError(
                f"{self.signal}: post_trigger_sec must be > {RESEED_WINDOW_SEC}")
        if new_btc <= 0:
            raise ValueError(
                f"{self.signal}: baseline_time_const must be > 0")
        if new_max <= new_post:
            raise ValueError(
                f"{self.signal}: max_event_sec must be > post_trigger_sec")

        changes = []
        for name, new in (("band", band),
                          ("post_trigger_sec", post_trigger_sec),
                          ("heartbeat_sec", heartbeat_sec),
                          ("baseline_time_const", baseline_time_const),
                          ("max_event_sec", max_event_sec),
                          ("pre_trigger_sec", pre_trigger_sec),
                          ("capture_all", capture_all)):
            if new is not None and getattr(self, name) != new:
                changes.append((name, getattr(self, name), new))
                if apply:
                    setattr(self, name, new)
        if apply and any(c[0] == "pre_trigger_sec" for c in changes):
            self._pre.clear()
        return changes

    # ---------------- internals ----------------

    def _process_normal(self, ts, value, extra):
        if abs(value - self.baseline) > self.band:
            # Out of band - open an event. Baseline freezes here.
            # First, flush the pre-trigger buffer: up to pre_trigger_sec of
            # in-band context, written as plain SAMPLE rows (true timestamps,
            # all < trigger_start) so they land ahead of TRIGGER_START in
            # chronological order. They do not touch the event summary.
            for p_ts, p_val, p_extra in self._pre:
                self._write(p_ts, p_val, "SAMPLE", p_extra)
            self._pre.clear()
            self.state = TRIGGERED
            self.in_band_since = None
            self._tail.clear()
            self.event = {
                "device_id": self.device_id,
                "signal": self.signal,
                "trigger_start": ts,
                "band_return": None,
                "trigger_end": None,
                "peak": value,
                "min": value,
                "samples_stored": 0,
                "band": self.band,
                "post_trigger_sec": self.post_trigger_sec,
            }
            self._store_event_sample(ts, value, "TRIGGER_START", extra)
            return

        # In band: the rolling baseline follows the quiescent level.
        dt = ts - self._prev_ts
        if dt > 0:
            alpha = 1.0 - math.exp(-dt / self.baseline_time_const)
            self.baseline += alpha * (value - self.baseline)

        wrote_heartbeat = False
        if (ts - self.last_record_ts) >= self.heartbeat_sec:
            self._write(ts, value, "HEARTBEAT", extra)
            wrote_heartbeat = True

        # Maintain the rolling pre-trigger buffer. A sample just written as
        # a HEARTBEAT is already in the database, so it is not buffered -
        # otherwise a later flush would store the same instant twice.
        if self.pre_trigger_sec > 0 and not self.capture_all:
            if not wrote_heartbeat:
                self._pre.append((ts, value, extra))
            cutoff = ts - self.pre_trigger_sec
            while self._pre and self._pre[0][0] < cutoff:
                self._pre.popleft()

    def _process_triggered(self, ts, value, extra):
        rec_type = "SAMPLE"

        # Band return is judged against the FROZEN pre-event baseline.
        if abs(value - self.baseline) > self.band:
            self.in_band_since = None          # restart countdown
        elif self.in_band_since is None:
            self.in_band_since = ts
            rec_type = "BAND_RETURN"
            if self.event["band_return"] is None:
                self.event["band_return"] = ts

        returned = (self.in_band_since is not None and
                    (ts - self.in_band_since) >= self.post_trigger_sec)
        timed_out = (ts - self.event["trigger_start"]) >= self.max_event_sec
        if returned or timed_out:
            rec_type = "TRIGGER_END"

        self._store_event_sample(ts, value, rec_type, extra)

        if returned or timed_out:
            self.event["trigger_end"] = ts
            self.event_sink(self.event)
            self.event = None
            self.state = NORMAL
            self.in_band_since = None
            # Re-seed the baseline from the last RESEED_WINDOW_SEC of the
            # event, so recording resumes centered on the settled level.
            self.baseline = (sum(v for _, v in self._tail)
                             / len(self._tail))
            self._tail.clear()

    def _store_event_sample(self, ts, value, rec_type, extra):
        self._write(ts, value, rec_type, extra)
        self._tail.append((ts, value))
        cutoff = ts - RESEED_WINDOW_SEC
        while self._tail and self._tail[0][0] < cutoff:
            self._tail.popleft()
        self.event["samples_stored"] += 1
        self.event["peak"] = max(self.event["peak"], value)
        self.event["min"] = min(self.event["min"], value)

    def _write(self, ts, value, rec_type, extra):
        rec = {
            "device_id": self.device_id,
            "signal": self.signal,
            "ts": ts,
            "value": value,
            "record_type": rec_type,
        }
        rec.update(extra)
        self.record_sink(rec)
        self.last_record_ts = ts
