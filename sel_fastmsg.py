"""
sel_fastmsg.py - SEL Unsolicited Fast Message client for the SEL-734

The SEL-734 does NOT speak IEEE C37.118 on the wire. It streams
synchrophasors using SEL's open Fast Message binary protocol:
  0xA546 header | size | routing | status | func | seq | resp |
  PMADDR(4) | regcount(2) | sample#(2) | SOC(4) | freq(f32) |
  [mag(f32) ang(f32)] x N | digital(2) | CRC-16(2)

Frame sizes (Table 7.3): PMDATA=V1 -> 40 B (1 phasor),
V -> 64 B (VA,VB,VC,V1), A -> 96 B (adds IA,IB,IC,I1).
Angles are in degrees (+/-180). Frequency is IEEE-754 float, Hz.
Digital word: bit0 = PMDOK, bit1 = TSOK, bits 2-15 = SV03-SV16.
No ROCOF in this protocol.

Transport here is the meter's Ethernet port via Telnet (ETELNET := Y,
TCP port 23). Telnet requires 0xFF (IAC) handling: incoming IAC
sequences are negotiation (we refuse everything politely) and literal
0xFF data bytes arrive doubled; outgoing 0xFF must be doubled.
transport="tcp" is also available for raw serial-over-TCP converters.

Reference: SEL-734 Instruction Manual (Date Code 20260522), Section 7,
Tables 7.2-7.7.
"""

import logging
import socket
import struct
import time

log = logging.getLogger("sel_fastmsg")

# The SEL-734's SOC ("second of century") counts from 1900-01-01 UTC (the
# NTP epoch), NOT 1970 as in IEEE C37.118. Verified on real hardware
# 2026-07-10: raw SOC landed exactly 25,567 days in the future when read
# as unix time. We auto-detect per connection (nearest epoch to the local
# clock wins) so a future firmware that switches to unix time still works.
NTP_UNIX_OFFSET = 2208988800

# The stream reports voltage magnitudes in kV (the MET PM display shows V,
# the binary stream sends kilovolts - also verified on real hardware).
VOLTAGE_KV_TO_V = 1000.0

from c37118 import PhasorChannel   # reuse for channel naming/uniform API

HEADER = b"\xA5\x46"

# Table 7.4 - message rate byte, exactly as printed in the manual
RATE_BYTE = {20: 0x05, 10: 0x0A, 5: 0x16, 4: 0x19, 2: 0x32, 1: 0x64}

FRAME_PHASORS = {40: 1, 64: 4, 96: 8}    # frame size -> phasor count
CHANNEL_NAMES = {
    1: ["V1"],
    4: ["VA", "VB", "VC", "V1"],
    8: ["VA", "VB", "VC", "V1", "IA", "IB", "IC", "I1"],
}

# Telnet protocol bytes
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class SELFastMsgError(Exception):
    pass


def crc16(data: bytes, init=0x0000) -> int:
    """CRC-16 (reflected poly 0xA001). init=0x0000 is CRC-16/ARC."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


class Sample:
    """Duck-typed like c37118.DataSample for the collector."""
    __slots__ = ("timestamp", "stat", "phasors", "freq", "rocof", "idcode")

    def __init__(self):
        self.timestamp = 0.0
        self.stat = 0               # the 734 digital word
        self.phasors = []           # [(magnitude, angle_degrees)]
        self.freq = 0.0
        self.rocof = None           # not provided by this protocol
        self.idcode = 0

    @property
    def pmdok(self):
        return bool(self.stat & 0x0001)

    @property
    def tsok(self):
        return bool(self.stat & 0x0002)

    @property
    def stat_ok(self):
        return self.pmdok and self.tsok


class _TelnetSocket:
    """Minimal Telnet layer: refuse all options, unescape/escape IAC."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def send(self, data: bytes):
        self.sock.sendall(data.replace(bytes([IAC]), bytes([IAC, IAC])))

    def recv(self, n: int) -> bytes:
        """Return up to n de-telnetted data bytes (at least 1, or raise)."""
        while True:
            # First serve from buffer
            out = self._extract(n)
            if out:
                return out
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SELFastMsgError("Connection closed by meter")
            self.buf.extend(chunk)

    def _extract(self, n) -> bytes:
        out = bytearray()
        i = 0
        b = self.buf
        while i < len(b) and len(out) < n:
            if b[i] != IAC:
                out.append(b[i]); i += 1
                continue
            if i + 1 >= len(b):
                break                          # need more bytes for IAC seq
            cmd = b[i + 1]
            if cmd == IAC:                     # escaped literal 0xFF
                out.append(IAC); i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(b):
                    break
                opt = b[i + 2]
                reply = WONT if cmd in (DO, DONT) else DONT
                try:
                    self.sock.sendall(bytes([IAC, reply, opt]))
                except OSError:
                    pass
                i += 3
            elif cmd == SB:                    # subnegotiation: skip to IAC SE
                j = b.find(bytes([IAC, SE]), i + 2)
                if j < 0:
                    break
                i = j + 2
            else:                              # other 2-byte command
                i += 2
        del b[:i]
        return bytes(out)


class _RawSocket:
    def __init__(self, sock):
        self.sock = sock

    def send(self, data):
        self.sock.sendall(data)

    def recv(self, n):
        data = self.sock.recv(n)
        if not data:
            raise SELFastMsgError("Connection closed by meter")
        return data


class SELFastMsgClient:
    """
    Same shape as C37118Client so collector.py can use either:
        connect() / close() / samples() / .pmu_cfg / .stream_cfg
    """

    def __init__(self, ip, port, pmaddr, data_rate=20, timeout=10.0,
                 transport="telnet"):
        if data_rate not in RATE_BYTE:
            raise SELFastMsgError(
                f"data_rate {data_rate} invalid for SEL-734; "
                f"choose one of {sorted(RATE_BYTE)}")
        self.ip = ip
        self.port = port
        self.pmaddr = pmaddr & 0xFFFFFFFF
        self.data_rate = data_rate
        self.timeout = timeout
        self.transport_kind = transport
        self.sock = None
        self.link = None
        self.crc_init = 0xFFFF       # real SEL-734s use the 0xFFFF-init
                                     # CRC-16 (verified on hardware); the
                                     # 0x0000 variant is tried as fallback
        self.epoch_offset = None     # SOC epoch, auto-detected at 1st frame
        self.pmu_cfg = _FakePMUCfg()
        self.stream_cfg = _FakeStreamCfg(data_rate)
        self._rxbuf = bytearray()

    # ---------------- connection ----------------

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port),
                                             timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.link = (_TelnetSocket(self.sock) if self.transport_kind == "telnet"
                     else _RawSocket(self.sock))
        # Give any Telnet negotiation a moment to be absorbed
        time.sleep(0.2)

        # Enable streaming. SEL documents a CRC-16 check word; if the meter
        # ignores the first enable (CRC variant mismatch), retry with the
        # 0xFFFF-init variant, then lock onto whichever produced data.
        for init in (0xFFFF, 0x0000):
            self.crc_init = init
            self.link.send(self._enable_packet(init))
            if self._await_first_frame(4.0):
                # Read channel names from the first frame's size
                return
        raise SELFastMsgError(
            "Meter did not stream after enable message - check ETELNET:=Y, "
            "PROTO:=SEL, EPMU:=Y, PMDATA, and that the IRIG clock is locked")

    def close(self):
        if self.sock:
            try:
                self.link.send(self._disable_packet(self.crc_init))
                time.sleep(0.1)
            except (OSError, SELFastMsgError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _enable_packet(self, crc_init) -> bytes:
        # Table 7.6: A546, size 0x12 (18), routing 5x00, status 00 (no ack),
        # func 01 (enable), seq C0, resp 00, app 20, resv 00 00, rate, CRC
        body = bytes([0xA5, 0x46, 0x12, 0, 0, 0, 0, 0, 0x00, 0x01, 0xC0,
                      0x00, 0x20, 0x00, 0x00, RATE_BYTE[self.data_rate]])
        return body + struct.pack(">H", crc16(body, crc_init))

    def _disable_packet(self, crc_init) -> bytes:
        # Table 7.7: size 0x10 (16), func 02 (disable), app 20, resv 00
        body = bytes([0xA5, 0x46, 0x10, 0, 0, 0, 0, 0, 0x00, 0x02, 0xC0,
                      0x00, 0x20, 0x00])
        return body + struct.pack(">H", crc16(body, crc_init))

    # ---------------- frame stream ----------------

    def _await_first_frame(self, seconds) -> bool:
        deadline = time.time() + seconds
        self.sock.settimeout(0.5)
        try:
            while time.time() < deadline:
                try:
                    frame = self._next_frame()
                except socket.timeout:
                    continue
                if frame is not None:
                    self._configure_from_frame(frame)
                    self._pending_first = frame
                    return True
            return False
        finally:
            self.sock.settimeout(self.timeout)

    def _configure_from_frame(self, frame):
        n = FRAME_PHASORS[frame[2]]
        self.pmu_cfg.phasors = [
            PhasorChannel(name, name.startswith("V"), 1.0)
            for name in CHANNEL_NAMES[n]]

    def _next_frame(self):
        """Return one validated frame (bytes) or None if CRC fails."""
        # Hunt for header
        while True:
            while len(self._rxbuf) < 3:
                self._rxbuf.extend(self.link.recv(4096))
            idx = self._rxbuf.find(HEADER)
            if idx < 0:
                # keep last byte in case it's the start of a header
                del self._rxbuf[:-1]
                continue
            if idx > 0:
                del self._rxbuf[:idx]
            if len(self._rxbuf) < 3:
                continue
            size = self._rxbuf[2]
            if size not in FRAME_PHASORS:
                del self._rxbuf[:2]          # false header; keep hunting
                continue
            while len(self._rxbuf) < size:
                self._rxbuf.extend(self.link.recv(4096))
            frame = bytes(self._rxbuf[:size])
            del self._rxbuf[:size]
            crc_rx = struct.unpack(">H", frame[-2:])[0]
            if crc_rx == crc16(frame[:-2], self.crc_init):
                return frame
            # try the other CRC variant once per frame before discarding
            other = 0xFFFF if self.crc_init == 0x0000 else 0x0000
            if crc_rx == crc16(frame[:-2], other):
                self.crc_init = other
                return frame
            return None

    def samples(self):
        # First frame was consumed while confirming the enable worked
        first = getattr(self, "_pending_first", None)
        if first is not None:
            self._pending_first = None
            s = self._parse(first)
            if s:
                yield s
        while True:
            frame = self._next_frame()
            if frame is None:
                continue
            s = self._parse(frame)
            if s:
                yield s

    def _parse(self, frame) -> Sample:
        n = FRAME_PHASORS[frame[2]]
        try:
            pmaddr, regcount, sample_num, soc = struct.unpack(
                ">IHHI", frame[12:24])
            values = struct.unpack(f">{1 + 2 * n}f",
                                   frame[24:24 + 4 * (1 + 2 * n)])
            (digital,) = struct.unpack(
                ">H", frame[24 + 4 * (1 + 2 * n):26 + 4 * (1 + 2 * n)])
        except struct.error:
            return None
        if pmaddr != self.pmaddr:
            return None                      # frame from a different meter
        s = Sample()
        s.idcode = pmaddr
        raw_ts = soc + sample_num / self.data_rate
        if self.epoch_offset is None:
            now = time.time()
            self.epoch_offset = (NTP_UNIX_OFFSET
                                 if abs((raw_ts - NTP_UNIX_OFFSET) - now)
                                 < abs(raw_ts - now) else 0)
            epoch = "1900 (NTP)" if self.epoch_offset else "1970 (unix)"
            log.info("SOC epoch detected: %s", epoch)
            skew = raw_ts - self.epoch_offset - now
            if abs(skew) > 5:
                log.warning("Meter time differs from system clock by "
                            "%.1f s - check IRIG/UTC settings", skew)
        s.timestamp = raw_ts - self.epoch_offset
        s.freq = values[0]
        names = CHANNEL_NAMES[n]
        for i in range(n):
            mag, ang = values[1 + 2 * i], values[2 + 2 * i]
            if names[i].startswith("V"):
                mag *= VOLTAGE_KV_TO_V          # stream is kV; store volts
            s.phasors.append((mag, ang))
        s.stat = digital
        return s


class _FakePMUCfg:
    """Just enough of c37118.PMUConfig for the collector's log line."""
    def __init__(self):
        self.station = "SEL-734"
        self.phasors = []
        self.fnom = 60.0


class _FakeStreamCfg:
    def __init__(self, rate):
        self.data_rate = rate
