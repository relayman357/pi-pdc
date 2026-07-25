"""
sim_pmu.py - fake SEL-735 for testing the collector with no hardware

Streams IEEE C37.118 frames (CFG-2 + data) like an SEL-735 configured with
PHDATAV := PH, PHDATAI := NA, MRATE := 60. Serves one TCP client at a time.

Run:   python sim_pmu.py [port] [idcode]
       (defaults: port 4712, idcode 1)

Behavior: steady 120.0 V with small noise, 60.000 Hz with small noise.
Every 45 seconds it injects a 3-second voltage sag to 100 V, and every
120 seconds a frequency dip to 59.90 Hz - so you can watch the deadband
logic trigger, record, and recover.

Test with the collector's default config (voltage_nominal 120, band 1%).
"""

import math
import random
import socket
import struct
import sys
import time

from c37118 import crc_ccitt, TYPE_CFG2, TYPE_DATA, TYPE_CMD, CMD_START, \
    CMD_STOP, CMD_SEND_CFG2

STATION = b"SIM SEL-735     "        # 16 bytes
CHANNELS = [b"VA              ", b"VB              ", b"VC              "]
TIME_BASE = 1000000
DATA_RATE = 60


def build_cfg2(idcode):
    soc = int(time.time())
    body = struct.pack(">HHHII", 0xAA00 | (TYPE_CFG2 << 4) | 1, 0, idcode,
                       soc, 0)
    body += struct.pack(">IH", TIME_BASE, 1)          # TIME_BASE, NUM_PMU
    body += STATION
    # FORMAT: bit0 polar=1, bit1 phasor float=1, bit3 freq float=1 -> 0x000B
    body += struct.pack(">HHHHH", idcode, 0x000B, len(CHANNELS), 0, 0)
    for name in CHANNELS:
        body += name
    for _ in CHANNELS:                                # PHUNIT: voltage, scale 1
        body += struct.pack(">I", (0 << 24) | 100000)
    body += struct.pack(">HH", 0, 0)                  # FNOM=60Hz, CFGCNT
    body += struct.pack(">h", DATA_RATE)
    body = body[:2] + struct.pack(">H", len(body) + 2) + body[4:]
    return body + struct.pack(">H", crc_ccitt(body))


def build_data(idcode, t, va_mag, freq):
    soc = int(t)
    frac = int((t - soc) * TIME_BASE)
    body = struct.pack(">HHHII", 0xAA00 | (TYPE_DATA << 4) | 1, 0, idcode,
                       soc, frac)
    body += struct.pack(">H", 0)                      # STAT = good
    angle = -2.30 * math.pi / 180.0
    for i, mag in enumerate((va_mag, va_mag, 0.5)):   # VB mirrors VA, VC ~ 0
        body += struct.pack(">ff", mag,
                            angle - i * 2 * math.pi / 3)
    body += struct.pack(">ff", freq, 0.0)             # FREQ, DFREQ
    body = body[:2] + struct.pack(">H", len(body) + 2) + body[4:]
    return body + struct.pack(">H", crc_ccitt(body))


def signal_values(t0):
    """Return (va_mag, freq) for the current moment, with injected events."""
    elapsed = time.time() - t0
    va = 120.0 + random.gauss(0, 0.05)
    freq = 60.0 + random.gauss(0, 0.002)
    if 0 <= (elapsed % 45.0) < 3.0 and elapsed > 10:      # voltage sag
        va = 100.0 + random.gauss(0, 0.3)
    if 0 <= (elapsed % 120.0) < 5.0 and elapsed > 20:     # frequency dip
        freq = 59.90 + random.gauss(0, 0.003)
    return va, freq


def serve_client(conn, idcode):
    conn.settimeout(0.0)   # non-blocking reads for commands
    streaming = False
    t0 = time.time()
    next_frame = time.time()
    print("Client connected")
    while True:
        # Handle any incoming command frames
        try:
            data = conn.recv(4096)
            if data == b"":
                print("Client disconnected")
                return
            for off in range(0, len(data) - 17, 18):
                sync, size, cid, soc, frac, cmd = struct.unpack(
                    ">HHHIIH", data[off:off + 16])
                if ((sync >> 4) & 7) != TYPE_CMD:
                    continue
                if cmd == CMD_SEND_CFG2:
                    conn.sendall(build_cfg2(idcode))
                    print("Sent CFG-2")
                elif cmd == CMD_START:
                    streaming = True
                    next_frame = time.time()
                    print("Streaming started")
                elif cmd == CMD_STOP:
                    streaming = False
                    print("Streaming stopped")
        except BlockingIOError:
            pass
        except OSError:
            print("Client disconnected")
            return

        if streaming and time.time() >= next_frame:
            va, freq = signal_values(t0)
            try:
                conn.sendall(build_data(idcode, next_frame, va, freq))
            except OSError:
                print("Client disconnected")
                return
            next_frame += 1.0 / DATA_RATE
        else:
            time.sleep(0.002)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4712
    idcode = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    print(f"Simulated SEL-735 on 127.0.0.1:{port}, idcode {idcode}")
    print("Voltage sag every 45 s, frequency dip every 120 s. Ctrl+C to quit.")
    while True:
        conn, addr = srv.accept()
        try:
            serve_client(conn, idcode)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
