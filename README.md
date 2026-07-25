# Pi PDC

**Pi PDC** is a lightweight Python phasor data collector for Raspberry Pi, Windows, and other systems running Python 3.9 or newer.

It records IEEE C37.118 synchrophasor streams and SEL Fast Message data in an SQLite database. During steady-state conditions it stores periodic heartbeat records; during disturbances it records every sample, including configurable pre-trigger and post-trigger data.

## Features

- Standard-library Python only
- Runs on Windows and Raspberry Pi/Linux
- IEEE C37.118 support
- SEL Fast Message support for the SEL-734
- SQLite database
- Event-based deadband recording
- Optional full-rate recording
- Simulators for testing without hardware
- Tested with SEL-734, SEL-735, and SEL-351A devices

## Getting Started

1. Install Python 3.9 or newer.
2. Download or clone this repository.
3. Edit `config.ini`.
4. Run:

```bash
python collector.py
```

On Raspberry Pi/Linux:

```bash
python3 collector.py
```

The included simulators allow the collector to be tested without a physical PMU.

## Documentation

Complete setup instructions, operating notes, and design information are available at:

https://relayman.org/pdc/pipdc.html

## Main Files

| File | Purpose |
|---|---|
| `collector.py` | Main Pi PDC program |
| `config.ini` | Device, recording, path, and backup settings |
| `c37118.py` | IEEE C37.118 client and frame parser |
| `sel_fastmsg.py` | SEL Fast Message client |
| `deadband.py` | Deadband recording logic |
| `dbwriter.py` | SQLite database writer |
| `backup.py` | Database snapshot and cloud backup |
| `prune_old.py` | Prune data older than input date | 
| `status.py` | Health and status reporting |
| `sim_pmu.py` | IEEE C37.118 PMU simulator |
| `sim_734.py` | SEL-734 Fast Message simulator |

## Project Status

**Beta**

This software is intended for monitoring, testing, research, and engineering applications. It has not been certified for protective relaying, revenue metering, grid control, or other safety-critical applications. Users are responsible for validating its suitability and accuracy for their application.
