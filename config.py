"""
config.py - loads and validates config.ini

Everything user-settable lives in config.ini. This module turns it into
typed objects the rest of the code uses. No other module reads the ini file.
"""

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DeviceConfig:
    key: str                    # section name, e.g. "device:meter_1"
    name: str
    protocol: str               # "c37118" (SEL-735) or "sel_fastmsg" (SEL-734)
    transport: str              # sel_fastmsg only: "telnet" or "tcp"
    ip: str
    port: int
    idcode: int
    data_rate: int
    connect_timeout_sec: float
    reconnect_delay_sec: float
    voltage_channel: str        # "auto" or channel name like "VA"
    voltage_nominal: float
    voltage_band_pct: float
    voltage_post_trig_sec: float
    voltage_pre_trig_sec: float   # rolling pre-trigger buffer (0 = off, max 10)
    capture_all_voltage_samples: bool  # store every voltage sample (no deadband skip)
    freq_nominal: float
    freq_band_hz: float
    freq_post_trig_sec: float
    freq_pre_trig_sec: float      # rolling pre-trigger buffer (0 = off, max 10)
    capture_all_freq_samples: bool     # store every frequency sample
    rolling_baseline_time_const: float  # EMA time constant (s), both signals
    max_event_sec: float        # force-close events that never return to band

    @property
    def voltage_band(self) -> float:
        """Deadband half-width in volts."""
        return self.voltage_nominal * self.voltage_band_pct / 100.0


@dataclass
class Config:
    station_name: str
    data_dir: Path
    log_level: str
    log_max_mb: int
    log_keep_files: int
    db_filename: str
    batch_seconds: float
    heartbeat_sec: float
    devices: list = field(default_factory=list)
    backup_enabled: bool = False
    backup_compress: bool = True
    backup_keep_local: int = 7
    backup_24h_snaps: int = 0   # rolling last-24h snapshots to keep (0 = off)
    rclone_remote: str = ""
    rclone_path: str = "rclone"
    power_check: str = "auto"
    watchdog: str = "auto"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / self.db_filename

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


def load(path="config.ini") -> Config:
    ini_path = Path(path)
    if not ini_path.exists():
        sys.exit(f"Config file not found: {ini_path.resolve()}\n"
                 f"Copy config.ini next to collector.py and edit it.")

    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.read(ini_path)

    g = cp["general"]
    db = cp["database"]
    hb = cp["heartbeat"]

    cfg = Config(
        station_name=g.get("station_name", "Station"),
        data_dir=Path(g.get("data_dir", "./data")),
        log_level=g.get("log_level", "INFO").upper(),
        log_max_mb=g.getint("log_max_mb", 10),
        log_keep_files=g.getint("log_keep_files", 5),
        db_filename=db.get("filename", "pmu.db"),
        batch_seconds=db.getfloat("batch_seconds", 1.0),
        heartbeat_sec=hb.getfloat("interval_sec", 60.0),
    )

    if cp.has_section("backup"):
        b = cp["backup"]
        cfg.backup_enabled = b.getboolean("enabled", False)
        cfg.backup_compress = b.getboolean("compress", True)
        cfg.backup_keep_local = b.getint("keep_local_copies", 7)
        # Rolling 24h snapshots: how many gzipped last-24h files to keep.
        # Clamp to a minimum of 0 (0 = feature off, no snapshots created).
        cfg.backup_24h_snaps = max(0, b.getint("number_of_24h_snaps", 0))
        cfg.rclone_remote = b.get("rclone_remote", "")
        cfg.rclone_path = b.get("rclone_path", "rclone")

    if cp.has_section("platform"):
        p = cp["platform"]
        cfg.power_check = p.get("power_check", "auto")
        cfg.watchdog = p.get("watchdog", "auto")

    for section in cp.sections():
        if not section.startswith("device:"):
            continue
        d = cp[section]
        if not d.getboolean("enabled", True):
            continue
        protocol = d.get("protocol", "c37118").strip().lower()
        default_port = 23 if protocol == "sel_fastmsg" else 4712
        cfg.devices.append(DeviceConfig(
            key=section,
            name=d.get("name", section),
            protocol=protocol,
            transport=d.get("transport", "telnet").strip().lower(),
            ip=d.get("ip"),
            port=d.getint("port", default_port),
            idcode=int(d.get("idcode", "1"), 0),   # 0x... hex ok (PMADDR)
            data_rate=d.getint("data_rate", 60),
            connect_timeout_sec=d.getfloat("connect_timeout_sec", 10.0),
            reconnect_delay_sec=d.getfloat("reconnect_delay_sec", 5.0),
            voltage_channel=d.get("voltage_channel", "auto").strip(),
            voltage_nominal=d.getfloat("voltage_nominal", 120.0),
            voltage_band_pct=d.getfloat("voltage_band_pct", 1.0),
            voltage_post_trig_sec=d.getfloat("voltage_post_trig_sec", 10.0),
            voltage_pre_trig_sec=d.getfloat("voltage_pre_trig_sec", 0.0),
            capture_all_voltage_samples=d.getboolean(
                "capture_all_voltage_samples", False),
            freq_nominal=d.getfloat("freq_nominal", 60.0),
            freq_band_hz=d.getfloat("freq_band_hz", 0.05),
            freq_post_trig_sec=d.getfloat("freq_post_trig_sec", 30.0),
            freq_pre_trig_sec=d.getfloat("freq_pre_trig_sec", 0.0),
            capture_all_freq_samples=d.getboolean(
                "capture_all_freq_samples", False),
            rolling_baseline_time_const=d.getfloat(
                "rolling_baseline_time_const", 60.0),
            max_event_sec=d.getfloat("max_event_sec", 120.0),
        ))

    if not cfg.devices:
        sys.exit("No enabled [device:*] sections found in config.ini")

    # Basic validation
    for dev in cfg.devices:
        if not dev.ip:
            sys.exit(f"{dev.key}: ip is required")
        if dev.voltage_band_pct <= 0 or dev.freq_band_hz <= 0:
            sys.exit(f"{dev.key}: deadbands must be > 0")
        if dev.voltage_post_trig_sec <= 1.0 or dev.freq_post_trig_sec <= 1.0:
            sys.exit(f"{dev.key}: post_trig_sec values must be > 1 s "
                     f"(the last 1 s of each event re-seeds the baseline)")
        for label, v in (("voltage_pre_trig_sec", dev.voltage_pre_trig_sec),
                         ("freq_pre_trig_sec", dev.freq_pre_trig_sec)):
            if not (0.0 <= v <= 10.0):
                sys.exit(f"{dev.key}: {label} must be between 0 and 10 s "
                         f"(0 disables; capped to bound the sample buffer), "
                         f"got {v}")
        if dev.rolling_baseline_time_const <= 0:
            sys.exit(f"{dev.key}: rolling_baseline_time_const must be > 0")
        if dev.max_event_sec <= max(dev.voltage_post_trig_sec,
                                    dev.freq_post_trig_sec):
            sys.exit(f"{dev.key}: max_event_sec must be greater than both "
                     f"post_trig_sec values")
        if dev.protocol not in ("c37118", "sel_fastmsg"):
            sys.exit(f"{dev.key}: protocol must be c37118 or sel_fastmsg")
        if dev.protocol == "sel_fastmsg" and dev.data_rate not in (1, 2, 4, 5, 10, 20):
            sys.exit(f"{dev.key}: SEL-734 data_rate must be 1, 2, 4, 5, 10, or 20")

    # Make sure directories exist
    for sub in ("db", "logs", "backups"):
        (cfg.data_dir / sub).mkdir(parents=True, exist_ok=True)

    return cfg


def settings_snapshot(path="config.ini"):
    """Return [(section, key, value)] of the entire ini for archiving in the db."""
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cp.read(path)
    rows = []
    for section in cp.sections():
        for key, value in cp[section].items():
            rows.append((section, key, value))
    return rows
