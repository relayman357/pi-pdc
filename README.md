================================================================================
Pi PDC Quick Start Guide - QUICKSTART (Windows 11 or Raspberry Pi / Linux)
Revision: 7-25-2026
================================================================================
For complete documentation, theory of operation, and updates, see the 
project page: https://relayman.org/pdc/pipdc.html and the GitHub page
at https://github.com/relayman357/pi-pdc.

The same files run unchanged on both platforms; only the Python command,
paths, and the task scheduler differ. Conventions used throughout:

                      WINDOWS                    RASPBERRY PI / LINUX
  Python command      python                     python3
  Program directory   C:\Dev\pmu                 /home/pi/pmu
  data_dir (config)   ./data (default)           /mnt/pmu_data (external SSD)
  Database lands in   C:\Dev\pmu\data\db\pmu.db  /mnt/pmu_data/db/pmu.db
  Scheduler           Task Scheduler             cron

Steps below are written once with Windows syntax; on the Pi substitute
per the table. Where the platforms genuinely differ, both are shown.

REQUIREMENTS
  Python 3.9 or newer. Standard library only - nothing to pip install.
  Check:  python --version        (Pi: python3 --version)

FILES
  config.ini      <- THE ONLY FILE YOU EDIT. IPs, deadbands, paths, schedules.
  collector.py    <- main program (the PDC). Run this.
  c37118.py       <- IEEE C37.118 protocol client + frame parser
  deadband.py     <- deadband compression state machine
  dbwriter.py     <- SQLite schema + batched writer thread
  prune_old.py    <- Prunes data prior to a specified date.
  config.py       <- config.ini loader
  platform_io.py  <- Pi-specific helpers (power/watchdog; no-ops on Windows)
  backup.py       <- database snapshot + rclone cloud upload (run separately)
  sel_fastmsg.py  <- SEL Fast Message client (SEL-734 over Ethernet/Telnet)
  status.py       <- health/status file writer + cloud upload (every 5 min)
  sim_pmu.py      <- fake SEL-735 for testing with no hardware
  sim_734.py      <- fake SEL-734 for testing with no hardware

STEP 1 - SMOKE TEST WITH THE SIMULATOR (no meter needed, either platform)
  Terminal 1:   cd C:\Dev\pmu
                python sim_pmu.py
  Terminal 2:   cd C:\Dev\pmu
                python collector.py
  (config.ini ships pointing at a real meter IP, which won't work here -
   set ip = 127.0.0.1 temporarily, idcode = 1. The simulator injects a
   voltage sag every 45 s and a frequency dip every 120 s so you can
   watch triggers fire.)
  Stop with Ctrl+C. Data lands in <data_dir>\db\pmu.db (see table above).

STEP 2 - LOOK AT THE DATA
  Use the Viewer at https://relayman.org/pdc/pipdc.html or any SQLite
  browser works (e.g. "DB Browser for SQLite", free, both
  platforms; or the sqlite3 command-line tool on the Pi).
  Useful queries:
    SELECT record_type, COUNT(*) FROM voltage_measurements GROUP BY record_type;
    SELECT * FROM voltage_events;
    SELECT ts_utc, magnitude, record_type FROM voltage_measurements
      ORDER BY ts DESC LIMIT 50;

STEP 2b - OPTIONAL: SMOKE TEST THE SEL-734 PATH
  Terminal 1:   python sim_734.py          (listens on 127.0.0.1:2323)
  Terminal 2:   set the device section to protocol = sel_fastmsg,
                ip = 127.0.0.1, port = 2323, idcode = 1, data_rate = 20,
                then python collector.py

STEP 3 - POINT AT A REAL SEL-735 (or any compliant PMU)
  In config.ini set:
    ip     = <meter IP>
    idcode = <meter PMID setting>
    port   = <meter PMOTCP1 setting, default 4712>
  Meter side (see project notes for details): EPMIP:=1, PMOTS1:=TCP,
  PMOIPA1:=<this machine's IP>, MRATE:=60, PHDATAV:=PH, PHDATAI:=NA,
  NUMANA:=0.
  Then:  python collector.py

STEP 3b - POINT AT A REAL SEL-734 (Ethernet)
  Meter side: EPMU:=Y, TSTYPE:=IRIG or IEEE (per your IRIG source),
  PMDATA:=V, PMADDR:=<pick an ID>, and on the Ethernet port ETELNET:=Y,
  PROTO:=SEL. config.ini device section: protocol = sel_fastmsg,
  transport = telnet, port = 23, idcode = <PMADDR, hex ok>, data_rate = 20.
  Note: max rate is 20/s on the SEL-734, and it provides no ROCOF
  (the rocof column will be empty for these devices).
  The 734's Ethernet port allows 3 simultaneous Telnet sessions (6 total),
  so more than one collector machine may read the same meter - but see the
  IMPORTANT note in STEP 4 before pointing two machines at one cloud.

STEP 4 - CLOUD BACKUP (optional)
  Install rclone (rclone.org; on the Pi: sudo apt install rclone), run
  "rclone config" once to create a remote, set [backup] enabled = true
  and rclone_remote in config.ini, then schedule
    python backup.py
  backup.py takes ONE snapshot per invocation and exits, so the
  scheduler's cadence IS the backup cadence; the [backup] section
  controls whether it runs (enabled is the master switch - false makes
  backup.py exit doing nothing, a kill switch that needs no schedule
  edit) and how (compress, keep_local_copies, rclone_remote/rclone_path
  - the same remote status.py uploads to).

  Scheduling, Windows (Task Scheduler, daily 3 AM):
    Program:  C:\Path\To\python.exe
    Args:     backup.py
    Start in: C:\Dev\pmu
  Scheduling, Pi (crontab -e, daily 3 AM):
    0 3 * * *  cd /home/pi/pmu && /usr/bin/python3 backup.py >> /mnt/pmu_data/logs/backup.log 2>&1

  IMPORTANT if you run MORE THAN ONE collector machine: each machine's
  rclone_remote must point to its OWN cloud folder, not one already used
  by another machine (e.g. dropbox:pmu_project vs dropbox:pmu_project_pi2)
  - otherwise the machines overwrite each other's status.txt every 5 min.
  Same rclone account/remote name is fine; only the folder must differ.

  The collector never pauses - backups snapshot the live database safely.
  After scheduling, run "python backup.py" once by hand: you should see
  snapshot/upload log lines, not the "enabled = false" message.

STEP 5 - REMOTE MONITORING (optional)
  Run status.py every 5 minutes to write and upload a small status.txt
  (data freshness, last events, uptime, disk; power health is Pi-only -
  those checks are no-ops on Windows via platform_io.py).
  Pi (crontab -e):
    */5 * * * *  cd /home/pi/pmu && /usr/bin/python3 status.py >> /mnt/pmu_data/logs/status.log 2>&1
  Windows: Task Scheduler, repeat every 5 minutes, same Program/Args/
  Start-in pattern as STEP 4 with Args: status.py
  Checking from anywhere = open status.txt in the cloud folder. If
  "Generated" is older than ~10 min, the machine/internet is down; if
  it's fresh but a device shows STALE, the meter link is down.

PI-SPECIFIC DEPLOYMENT NOTES
  Everything above works on the Pi as-is with the table's substitutions.
  For a permanent, headless Pi installation, pmu_project_notes.txt covers
  the extra steps that have no Windows equivalent: external SSD mount via
  /etc/fstab (use the nofail option - see the LESSON LEARNED note there),
  running collector.py as a systemd service for start-on-boot and
  auto-restart, the hardware watchdog, and UPS/power-health monitoring.
  On the Pi you can also apply most config.ini edits WITHOUT a restart or
  data gap - "sudo systemctl reload pmu-collector" (SIGHUP). See the "LIVE
  CONFIG RELOAD" section in pmu_project_notes.txt for what applies live vs.
  what still needs a restart. (Windows: no SIGHUP; edit and restart.)

RECORD TYPES CHEAT SHEET
  START          first record on collector startup
  HEARTBEAT      periodic proof the signal stayed in band
  TRIGGER_START  first sample outside the deadband (band is centered on a
                 rolling baseline that FREEZES for the duration of the event)
  SAMPLE         every sample (at the meter's stream rate) while an event
                 is active - plus, when pre_trig_sec is set, the rolling
                 pre-trigger buffer flushed at the moment of trigger
                 (those rows have ts BEFORE trigger_start)
  BAND_RETURN    signal back within band of the frozen pre-event baseline;
                 post-trigger countdown began (restarts if band breaks again)
  TRIGGER_END    event closed: post-trigger window expired, or the
                 max_event_sec force-close fired because the signal settled
                 at a new level and never returned to band (band_return
                 stays NULL in the events table in that case)
  GAP            collector restarted; period since last record unverified

  On every event close the baseline re-seeds from the mean of the last 1 s
  of stored samples, which is why both post_trig_sec settings must be > 1.
  New config keys (2026-07-11): rolling_baseline_time_const, max_event_sec.
  New config keys (2026-07-16): voltage_pre_trig_sec, freq_pre_trig_sec -
  0-10 s of full-rate in-band context stored ahead of every trigger
  (0 = off). No schema or viewer changes; legacy pmu_settings migration
  code was also removed from dbwriter.py the same day (dev-phase db wipe).
  Also 2026-07-16: capture_all_voltage_samples / capture_all_freq_samples
  (default false) - per device+signal, store EVERY sample; triggers and
  event summaries still work; heartbeats stop and pre_trig is ignored on
  that signal. WATCH DATABASE/BACKUP SIZE: ~1.7M rows/day at 20 samples/s.
  Full details and the pre-2026-07-11 data caveats: pmu_project_notes.txt,
  "DATA STORAGE STRATEGY" section.
================================================================================
