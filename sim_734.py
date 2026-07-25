"""
sim_734.py - fake SEL-734 for testing (SEL Fast Message over Telnet)

Simulates an SEL-734 with PMDATA := V (VA, VB, VC, V1), streaming at the
rate requested by the client's enable message. Speaks enough Telnet to be
realistic: sends an initial DO/WILL negotiation and escapes literal 0xFF
bytes (IAC doubling) - exercising the collector's Telnet handling.

Run:   python sim_734.py [port] [pmaddr]
       (defaults: port 2323 to avoid needing admin rights for port 23,
        pmaddr 1)

Injects a voltage sag every 45 s and a frequency dip every 120 s.
Digital word has PMDOK + TSOK set (bits 0 and 1).
"""

import random
import socket
import struct
import sys
import time

from sel_fastmsg import crc16, RATE_BYTE, NTP_UNIX_OFFSET

IAC, DO, WILL, SGA = 255, 253, 251, 3


def build_data(pmaddr, soc, sample_num, va, freq):
    soc += NTP_UNIX_OFFSET          # real 734s count SOC from 1900 (NTP)
    va /= 1000.0                    # real 734s stream voltage in kV
    phasors = [(va, -2.30), (va, -122.30), (0.0005, 117.70),
               (va / 3.0, -2.30)]                      # VA VB VC V1
    body = bytes([0xA5, 0x46, 64, 0, 0, 0, 0, 0, 0x00, 0x20, 0xC0, 0x00])
    body += struct.pack(">IHHI", pmaddr, 0x0016, sample_num, soc)
    body += struct.pack(">f", freq)
    for mag, ang in phasors:
        body += struct.pack(">ff", mag, ang)
    body += struct.pack(">H", 0x0003)                  # PMDOK | TSOK
    return body + struct.pack(">H", crc16(body, 0xFFFF))  # real-734 CRC


def telnet_escape(data: bytes) -> bytes:
    return data.replace(bytes([IAC]), bytes([IAC, IAC]))


def signal_values(t0):
    elapsed = time.time() - t0
    va = 120.0 + random.gauss(0, 0.05)
    freq = 60.0 + random.gauss(0, 0.002)
    if 0 <= (elapsed % 45.0) < 3.0 and elapsed > 10:
        va = 100.0 + random.gauss(0, 0.3)
    if 0 <= (elapsed % 120.0) < 5.0 and elapsed > 20:
        freq = 59.90 + random.gauss(0, 0.003)
    return va, freq


def serve(conn, pmaddr):
    conn.settimeout(0.0)
    # Realistic Telnet server behavior: negotiate at connect
    conn.sendall(bytes([IAC, WILL, SGA, IAC, DO, SGA]))
    rate = None
    rxbuf = bytearray()
    t0 = time.time()
    next_frame = None
    print("Client connected")
    while True:
        try:
            data = conn.recv(4096)
            if data == b"":
                print("Client disconnected"); return
            rxbuf.extend(data)
        except BlockingIOError:
            pass
        except OSError:
            print("Client disconnected"); return

        # Strip telnet replies (IAC x y) then look for enable/disable packets
        while True:
            i = rxbuf.find(bytes([IAC]))
            if i < 0 or len(rxbuf) - i < 3:
                break
            if rxbuf[i + 1] == IAC:
                del rxbuf[i + 1:i + 2]      # unescape literal FF
            else:
                del rxbuf[i:i + 3]
        i = rxbuf.find(b"\xA5\x46")
        if i >= 0 and len(rxbuf) >= i + 3:
            size = rxbuf[i + 2]
            if len(rxbuf) >= i + size:
                pkt = bytes(rxbuf[i:i + size])
                del rxbuf[:i + size]
                crc_rx = struct.unpack(">H", pkt[-2:])[0]
                if crc_rx not in (crc16(pkt[:-2], 0xFFFF),
                                  crc16(pkt[:-2], 0x0000)):
                    continue
                func = pkt[9]
                if func == 0x01 and size == 18:
                    nn = pkt[15]
                    for r, byte in RATE_BYTE.items():
                        if byte == nn or (nn == 0 and r == 20):
                            rate = r
                    print(f"Enable received, rate byte 0x{nn:02X} "
                          f"-> {rate}/s")
                    next_frame = time.time()
                elif func == 0x02:
                    print("Disable received")
                    rate = None

        if rate and time.time() >= next_frame:
            soc = int(next_frame)
            sample_num = int(round((next_frame - soc) * rate)) % rate
            va, freq = signal_values(t0)
            try:
                conn.sendall(telnet_escape(
                    build_data(pmaddr, soc, sample_num, va, freq)))
            except OSError:
                print("Client disconnected"); return
            next_frame += 1.0 / rate
        else:
            time.sleep(0.002)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2323
    pmaddr = int(sys.argv[2], 0) if len(sys.argv) > 2 else 1
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    print(f"Simulated SEL-734 (Telnet) on 127.0.0.1:{port}, PMADDR {pmaddr}")
    while True:
        conn, _ = srv.accept()
        try:
            serve(conn, pmaddr)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
