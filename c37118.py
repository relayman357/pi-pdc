"""
c37118.py - minimal IEEE C37.118.2 client for a single PMU stream

Implements just what a small PDC needs:
  - TCP connection to the PMU (the SEL-735 with PMOTS1 := TCP)
  - Command frames: stop / start / send CFG-2
  - CFG-2 parsing (channel names, formats, scale factors, nominal freq)
  - Data frame parsing (phasors, FREQ, DFREQ/ROCOF, STAT)
  - CRC-CCITT verification on every frame

References: IEEE C37.118.2-2011; SEL-735 Instruction Manual Appendix I.
Pure standard library - runs identically on Windows / Pi / Ubuntu.
"""

import math
import socket
import struct
import time

# Frame types (bits 4-6 of the second SYNC byte)
TYPE_DATA = 0
TYPE_HEADER = 1
TYPE_CFG1 = 2
TYPE_CFG2 = 3
TYPE_CMD = 4
TYPE_CFG3 = 5

# Command codes
CMD_STOP = 1
CMD_START = 2
CMD_SEND_HDR = 3
CMD_SEND_CFG1 = 4
CMD_SEND_CFG2 = 5


def crc_ccitt(data: bytes) -> int:
    """CRC-CCITT (X^16 + X^12 + X^5 + 1), initial value 0xFFFF, per C37.118."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


class PhasorChannel:
    """One phasor channel as described by CFG-2."""
    def __init__(self, name, is_voltage, scale):
        self.name = name            # e.g. "VA"
        self.is_voltage = is_voltage
        self.scale = scale          # volts/amps per LSB (integer format only)

    def __repr__(self):
        kind = "V" if self.is_voltage else "I"
        return f"<{kind}:{self.name}>"


class PMUConfig:
    """Parsed CFG-2 for one PMU inside the stream."""
    def __init__(self):
        self.station = ""
        self.idcode = 0
        self.freq_dfreq_float = False
        self.analogs_float = False
        self.phasors_float = False
        self.phasors_polar = False
        self.phasors = []           # list[PhasorChannel]
        self.num_analogs = 0
        self.num_digitals = 0       # number of 16-bit digital words
        self.fnom = 60.0
        self.cfgcnt = 0

    def data_block_size(self) -> int:
        """Bytes this PMU occupies in each data frame (after common header)."""
        size = 2  # STAT
        size += len(self.phasors) * (8 if self.phasors_float else 4)
        size += 8 if self.freq_dfreq_float else 4      # FREQ + DFREQ
        size += self.num_analogs * (4 if self.analogs_float else 2)
        size += self.num_digitals * 2
        return size


class StreamConfig:
    """Parsed CFG-2 for the whole stream."""
    def __init__(self):
        self.time_base = 1000000
        self.pmus = []              # list[PMUConfig]
        self.data_rate = 60


class DataSample:
    """One PMU's measurements from one data frame."""
    __slots__ = ("timestamp", "stat", "phasors", "freq", "rocof",
                 "time_quality", "idcode")

    def __init__(self):
        self.timestamp = 0.0        # unix epoch, float seconds (UTC)
        self.stat = 0
        self.phasors = []           # list of (magnitude, angle_degrees)
        self.freq = 0.0             # Hz
        self.rocof = 0.0            # Hz/s
        self.time_quality = 0
        self.idcode = 0

    @property
    def stat_ok(self) -> bool:
        """True when STAT indicates good data (upper 2 bits == 00)."""
        return (self.stat & 0xC000) == 0


class C37118Error(Exception):
    pass


class C37118Client:
    """
    Blocking TCP client for one PMU stream.

    Usage:
        client = C37118Client(ip, port, idcode, timeout)
        client.connect()          # connects, fetches CFG-2, starts data
        for sample in client.samples():
            ...
        client.close()
    """

    def __init__(self, ip, port, idcode, timeout=10.0):
        self.ip = ip
        self.port = port
        self.idcode = idcode
        self.timeout = timeout
        self.sock = None
        self.stream_cfg = None
        self.pmu_cfg = None         # the PMU we care about

    # ---------------- connection / commands ----------------

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port),
                                             timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        # Be polite: stop any stream a previous client left running
        self._send_command(CMD_STOP)
        self._drain(0.3)
        self._send_command(CMD_SEND_CFG2)
        self.stream_cfg = self._wait_for_cfg2()
        self.pmu_cfg = self._select_pmu(self.stream_cfg)
        self._send_command(CMD_START)

    def close(self):
        if self.sock:
            try:
                self._send_command(CMD_STOP)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send_command(self, cmd):
        soc = int(time.time())
        body = struct.pack(">HHHIIH",
                           0xAA00 | (TYPE_CMD << 4) | 1,  # SYNC, version 1
                           18,                            # FRAMESIZE
                           self.idcode, soc, 0, cmd)
        frame = body + struct.pack(">H", crc_ccitt(body))
        self.sock.sendall(frame)

    def _drain(self, seconds):
        """Discard whatever arrives for a moment (e.g. after STOP)."""
        self.sock.settimeout(seconds)
        try:
            while True:
                if not self.sock.recv(4096):
                    break
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(self.timeout)

    # ---------------- frame reception ----------------

    def _recv_exact(self, n) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise C37118Error("Connection closed by PMU")
            buf += chunk
        return buf

    def _read_frame(self):
        """Return (frame_type, full_frame_bytes) with CRC verified."""
        header = self._recv_exact(4)
        sync, framesize = struct.unpack(">HH", header)
        if (sync >> 8) != 0xAA:
            raise C37118Error(f"Bad SYNC byte: 0x{sync:04X} (stream out of sync)")
        if not (18 <= framesize <= 65535):
            raise C37118Error(f"Implausible frame size: {framesize}")
        rest = self._recv_exact(framesize - 4)
        frame = header + rest
        crc_rx = struct.unpack(">H", frame[-2:])[0]
        if crc_rx != crc_ccitt(frame[:-2]):
            raise C37118Error("CRC check failed")
        return (sync >> 4) & 0x7, frame

    def _wait_for_cfg2(self, max_frames=50):
        for _ in range(max_frames):
            ftype, frame = self._read_frame()
            if ftype == TYPE_CFG2:
                return parse_cfg2(frame)
        raise C37118Error("PMU never sent CFG-2")

    def _select_pmu(self, stream_cfg):
        for pmu in stream_cfg.pmus:
            if pmu.idcode == self.idcode:
                return pmu
        # Fall back to the first PMU but warn via exception message context
        if stream_cfg.pmus:
            return stream_cfg.pmus[0]
        raise C37118Error("CFG-2 contained no PMU blocks")

    def samples(self):
        """Generator yielding DataSample objects. Blocks between frames."""
        while True:
            ftype, frame = self._read_frame()
            if ftype == TYPE_DATA:
                yield parse_data(frame, self.stream_cfg, self.pmu_cfg)
            elif ftype in (TYPE_CFG1, TYPE_CFG2, TYPE_CFG3):
                # Meter re-sent config (settings change) - re-parse and continue
                if ftype == TYPE_CFG2:
                    self.stream_cfg = parse_cfg2(frame)
                    self.pmu_cfg = self._select_pmu(self.stream_cfg)
            # header frames and anything else: ignore


# ---------------- parsers (module-level, unit-testable) ----------------

def parse_cfg2(frame: bytes) -> StreamConfig:
    cfg = StreamConfig()
    (sync, framesize, idcode, soc, fracsec, time_base,
     num_pmu) = struct.unpack(">HHHIIIH", frame[:20])
    cfg.time_base = time_base & 0x00FFFFFF
    if cfg.time_base == 0:
        cfg.time_base = 1000000
    off = 20
    for _ in range(num_pmu):
        pmu = PMUConfig()
        pmu.station = frame[off:off + 16].decode("ascii", "replace").strip()
        off += 16
        (pmu.idcode, fmt, phnmr, annmr, dgnmr) = struct.unpack(
            ">HHHHH", frame[off:off + 10])
        off += 10
        pmu.phasors_polar = bool(fmt & 0x0001)
        pmu.phasors_float = bool(fmt & 0x0002)
        pmu.analogs_float = bool(fmt & 0x0004)
        pmu.freq_dfreq_float = bool(fmt & 0x0008)
        pmu.num_analogs = annmr
        pmu.num_digitals = dgnmr

        names = []
        for _ in range(phnmr + annmr + 16 * dgnmr):
            names.append(frame[off:off + 16].decode("ascii", "replace").strip())
            off += 16

        for i in range(phnmr):
            (phunit,) = struct.unpack(">I", frame[off:off + 4])
            off += 4
            is_voltage = (phunit >> 24) == 0
            scale = (phunit & 0x00FFFFFF) * 1e-5   # V or A per LSB (int fmt)
            pmu.phasors.append(PhasorChannel(names[i], is_voltage, scale))

        off += 4 * annmr        # ANUNIT - skip (we don't use analogs)
        off += 4 * dgnmr        # DIGUNIT - skip

        (fnom_word, pmu.cfgcnt) = struct.unpack(">HH", frame[off:off + 4])
        off += 4
        pmu.fnom = 50.0 if (fnom_word & 0x0001) else 60.0
        cfg.pmus.append(pmu)

    (cfg.data_rate,) = struct.unpack(">h", frame[off:off + 2])
    return cfg


def parse_data(frame: bytes, stream: StreamConfig, wanted: PMUConfig) -> DataSample:
    (sync, framesize, idcode, soc, fracsec) = struct.unpack(">HHHII", frame[:14])
    sample = DataSample()
    sample.time_quality = (fracsec >> 24) & 0xFF
    sample.timestamp = soc + (fracsec & 0x00FFFFFF) / stream.time_base

    off = 14
    for pmu in stream.pmus:
        if pmu is not wanted:
            off += pmu.data_block_size()
            continue

        sample.idcode = pmu.idcode
        (sample.stat,) = struct.unpack(">H", frame[off:off + 2])
        off += 2

        for ch in pmu.phasors:
            if pmu.phasors_float:
                a, b = struct.unpack(">ff", frame[off:off + 8])
                off += 8
                if pmu.phasors_polar:
                    mag, ang = a, math.degrees(b)
                else:
                    mag = math.hypot(a, b)
                    ang = math.degrees(math.atan2(b, a))
            else:
                if pmu.phasors_polar:
                    m_raw, a_raw = struct.unpack(">Hh", frame[off:off + 4])
                    off += 4
                    mag = m_raw * ch.scale
                    ang = math.degrees(a_raw / 10000.0)
                else:
                    re_raw, im_raw = struct.unpack(">hh", frame[off:off + 4])
                    off += 4
                    re = re_raw * ch.scale
                    im = im_raw * ch.scale
                    mag = math.hypot(re, im)
                    ang = math.degrees(math.atan2(im, re))
            sample.phasors.append((mag, ang))

        if pmu.freq_dfreq_float:
            freq_raw, dfreq_raw = struct.unpack(">ff", frame[off:off + 8])
            off += 8
            sample.freq = freq_raw          # float format = actual Hz
            sample.rocof = dfreq_raw        # Hz/s
        else:
            freq_raw, dfreq_raw = struct.unpack(">hh", frame[off:off + 4])
            off += 4
            sample.freq = pmu.fnom + freq_raw / 1000.0   # int = mHz deviation
            sample.rocof = dfreq_raw / 100.0             # int = Hz/s x 100

        off += pmu.num_analogs * (4 if pmu.analogs_float else 2)
        off += pmu.num_digitals * 2

    return sample


def pick_voltage_index(pmu: PMUConfig, wanted_name: str) -> int:
    """
    Choose which phasor channel to record.
    wanted_name "auto" -> first voltage channel.
    Otherwise match by channel name (case-insensitive, substring ok).
    """
    if wanted_name and wanted_name.lower() != "auto":
        for i, ch in enumerate(pmu.phasors):
            if ch.is_voltage and wanted_name.upper() in ch.name.upper():
                return i
    for i, ch in enumerate(pmu.phasors):
        if ch.is_voltage:
            return i
    raise C37118Error(
        "No voltage phasor in the stream - check PHDATAV setting in the meter")
