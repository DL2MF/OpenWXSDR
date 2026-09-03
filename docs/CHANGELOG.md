# OpenWXSDR Changelog

## v1.0.62 (2026-08-15u) - SNR spectral fix + rings + status extras
- FIX SNR: was noise-floor-from-rolling-total-power (separation in time) → ~0 dB
  for a continuous sonde and mirrored RSSI. Now spectral in-band/out-of-band via
  per-chunk FFT (separation in frequency) — responsive, non-zero, RSSI-decoupled.
  Absolute level may want an on-air calibration offset.
- Setup status: added Sonde retention time (Configuration) and MQTT server IP
  (MQTT upload detail).
- Landings: added 200 / 250 / 300 km range rings (+ marker colors).
- Files: src/sdr/signal_metrics.py, src/webui/web_server.py, templates/index.html.

## v1.0.62 (2026-08-15v) - Setup status: grouped layout + receiver dots
- Checks grouped into Installation / Configuration / Upload-Download sections
  with headers; modernized table styling.
- OK/WARN/FAIL legend moved to the footer (before version · timestamp).
- Configured receivers now show a big colored status dot (green decoding /
  blue scanning / grey idle / red not-detected) and the decoded sonde type
  when actively decoding, like the SDR Devices panel.
- Added UDP output (output.udp) row to Upload/Download.
- Files: templates/index.html, src/webui/web_server.py.

## v1.0.62 (2026-08-15w) - Setup status: rename + soft_decode/range/telemetry
- Menu: "Settings"→"Configuration"; "System configuration"→"Setup status"
  (modal title matches).
- Added soft_decode row (after sonde types; warns if ON but fsk_demod/softin
  missing), scan frequency range appended to Scanner mode, and Telemetry
  (install counter) as the final row — shows the current install ID from
  data/.install_id when enabled.
- Files: templates/index.html, src/webui/web_server.py.

## v1.0.62 (2026-08-15x) - System configuration modal: layout + more checks
- Table header row (Status | Setting | Details); station callsign appended to
  the modal title one size smaller (e.g. "System configuration - DL2MF-14").
- Configured receivers now listed per-device with a status badge (live worker
  state / RTL presence), like the SDR devices list.
- Added rows: Scanner mode (welch/rtl_power + fallback), Band sweep, USB
  recovery.
- Files: templates/index.html, src/webui/web_server.py.

## v1.0.62 (2026-08-15y) - System configuration diagnostics modal
- System menu: "Configuration" renamed to "Settings"; new "System configuration"
  item added after "Service".
- New modal + /api/system_config_check endpoint runs install/config diagnostics:
  installation, install_softchain (dft_detect/fsk_demod/softin), python venv,
  paho-mqtt, Airspy support, config.yaml validity, configured receivers, sonde
  types, RS41 fallback, live SNR, RS92 ephemeris, MQTT, SondeHub — with
  OK/WARN/FAIL/INFO badges.
- Files: templates/index.html, src/webui/web_server.py.

## v1.0.62 (2026-08-15z) - Sonde tile: elevation 0.1° precision
- Extended-mode sonde tile now shows elevation with one decimal (toFixed(1)).
- Battery in optimized stats verified correct end-to-end (endpoints, LTTB
  decimation, chart wiring) — empty battery seen on an older running build,
  resolved by deploying current source.
- File: templates/index.html.

## v1.0.62 (2026-08-09z) - Ephemeris: hide-when-off + retry on failure
- System Health "Ephemeris" row is hidden when rs92.ephemeris_download is false
  (only shown when enabled: ready/pending).
- Ephemeris download now retries with backoff (15s→10min) instead of a single
  boot-time attempt, so a network-not-ready-at-boot failure recovers on its own;
  re-checks hourly for the UTC-midnight GPS-day rollover.
- Files: templates/index.html, sdr/ephemeris.py.

## v1.0.62 (2026-08-09y) - RS92 ephemeris: correct BKG URL (RINEX3 MN)
- BKG serves no RINEX2 brdc; the brdcDDD0.YYn URL failed (connection reset).
  Default is now the actual BKG file BRDC00WRD_R_<yyyy><doy>0000_01D_MN.rnx.gz
  (RINEX3 mixed-nav; current rs92mod reads RINEX3, _MN has the GPS records).
- Files: sdr/ephemeris.py, config.yaml(.example).

## v1.0.62 (2026-08-09x) - RS92 ephemeris: startup diagnostic log
- Log the RS92 ephemeris state at startup whether enabled or not (incl. whether
  a rs92 config section was found + the URL), so a mis-nested config (dotted key
  vs nested YAML) is obvious.
- File: openwxsdr_app.py.

## v1.0.62 (2026-08-09w) - Stats layout + Ephemeris health
- Maximized chart header identity now bold + same font/size as the chart title.
- Sonde Statistics tiles reordered: Altitude|Elevation, HVel|VVel, Sats|Battery,
  SNR|RSSI.
- System Health: new "Ephemeris" row (RS92 download: ready/pending/disabled, via
  /api/health + ephemeris.status()); panel top/bottom padding tightened.
- Files: templates/index.html, web_server.py, sdr/ephemeris.py.

## v1.0.62 (2026-08-09v) - Fix stuck "Loading data" backdrop
- A fast landings/heatmap fetch called modal.hide() while the loading modal was
  still mid show-transition; Bootstrap ignores that, leaving the backdrop stuck.
  showLoading/hideLoading now defer the hide until 'shown.bs.modal' (or fire it
  immediately if already shown) and run a backdrop-cleanup safety net.
- File: templates/index.html.

## v1.0.62 (2026-08-09u) - Elevation chart: add distance line
- Elevation chart now plots a second line — slant Distance (km) from the station
  — on a right-hand axis, with the legend enabled. Both history endpoints return
  per-frame 'distance' (from _look_angles, same as elevation).
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09t) - Landings: path menu + restyled popup
- Right-click a landing marker → Show sondepath (loads the logfile track as a
  polyline with launch/landing dots) / Clear sondepath. /api/landings now
  returns filename; Clear (gateway menu) also clears the path.
- Landing popup restyled to match the live-sonde popup (.custom-popup): id
  header, Date/Time first as "YYYY-MM-DD - HH:MM:SSZ", distance last.
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09s) - RS92 GPS ephemeris download
- New opt-in RS92 broadcast-ephemeris downloader (src/sdr/ephemeris.py): fetches
  the current GPS-day RINEX nav file into data/rs92 and feeds rs92mod (-e) so
  RS92 can solve a position. Config rs92.ephemeris_download (default false),
  ephemeris_url (default BKG Germany RINEX2 brdcDDD0.YYn, HTTPS/no-login),
  ephemeris_dir (data/rs92). Downloads in the background at startup; the decoder
  self-discovers today's file, old files pruned. Only .gz/plain URLs (not CDDIS
  .Z). Absent/disabled => RS92 decodes as before.
- Files: sdr/ephemeris.py (new), openwxsdr_app.py, rs1729_decoder.py, config.yaml(.example).

## v1.0.62 (2026-08-09r) - Fix M10/M20 dashed-serial logfile loading
- M10/M20 serials contain dashes (e.g. 210-2-11234), so filename.split('-')[0]
  truncated them to "210" in the historic-track view and the logfile selector.
  Added logfileSerial() that strips only the trailing -YYYYMMDD-HHMMSS.log,
  keeping the full serial. Backend already used the anchored regex.
- File: templates/index.html.

## v1.0.62 (2026-08-09q) - Sonde-stats header identity (refine)
- Identity no longer shown on every chart tile — only on a maximized chart
  header (added on maximize, removed on minimize).
- Modal-title details now match the "Sonde Statistics" font/size and are bold.
- Separator between serial | type | freq | frames is now " | " (was comma).
- File: templates/index.html.

## v1.0.62 (2026-08-09p) - Sonde-stats header identity
- Fix: frequency was blank when opening stats via right-click on an active tile
  (the in-memory history endpoint now returns per-frame frequency).
- Modal title now reads "Sonde Statistics - <serial>, <type>, <freq>, <frames>"
  in the same font; the redundant detail row is hidden.
- Each chart card header (Altitude/VVel/HVel/RSSI/Sats/Battery/SNR/Elevation)
  now shows "<serial>, <type>, <freq>", visible normal and maximized.
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09o) - Landings/heatmap polish
- Range rings now thicker/brighter (weight 2.5, opacity 0.9) with a 4th ring at
  150 km; marker colour bands extended to match (<=150 red, beyond dark red).
- Heatmap gradient tuned for sparse data (150-250 logs): bigger blobs
  (radius 34/blur 24), low max so lone landings read green and clusters ramp
  green->yellow->orange->red.
- After Landings/Heatmap the map centers on home at zoom 9 (was fit-to-bounds).
- File: templates/index.html.

## v1.0.62 (2026-08-09n) - Gateway right-click: landings + heatmap
- Right-click the gateway marker for a context menu. "Landings" draws 25/50/100
  km range rings and a colour-coded marker at each sonde's last logged position,
  popup = id/type/freq/date/time/lat/lon/alt/vvel/distance. "Heatmap" overlays a
  density map (Leaflet.heat) of all last positions. A "Loading data ..." dialog
  shows during processing. New /api/landings + _landing_from_log parse the logs.
- Files: web_server.py, templates/index.html (adds leaflet.heat CDN).

## v1.0.62 (2026-08-09m) - RTL per-frame RSSI/SNR: safe to enable
- On RTL the tile/popup showed a static (scan-time) RSSI/SNR because the direct
  pipe has no per-frame source and rs41mod --IQ emits none. The metrics pump is
  the only source but was off (frame-yield regression). Now made safe: pipe
  buffers enlarged via F_SETPIPE_SZ so a brief pump stall can't make librtlsdr
  drop samples (throttle from (l) already fixed the CPU side). Enable per-frame
  RTL RSSI/SNR with decoders.live_signal_metrics: true (default still off;
  Airspy already has per-frame metrics).
- Files: audio_pipeline.py, config.yaml.example.

## v1.0.62 (2026-08-09l) - Faster stats charts + cheaper live metrics
- Sonde-statistics history endpoints now LTTB-decimate to ~1000 points server-
  side (?max_points=, 0=off), keeping burst/landing/spikes on a shared time axis
  — big drop in payload + Chart.js render time. Response adds total_frames.
- SignalMetrics.update_iq throttled to ~10/s (min_interval_s): the always-on
  Airspy channelizer pump called it ~400/s; per-frame RSSI/SNR only needs ~1/s,
  so metric CPU drops ~40x. (Airspy already attaches per-frame RSSI/SNR; RTL stays
  direct-pipe by default — its pump is the known frame-yield regression.)
- Files: web_server.py, signal_metrics.py.

## v1.0.62 (2026-08-09k) - Elevation in logs + sonde-stats graph
- Sonde logs now record the gateway->sonde Elevation angle per frame (from
  station lat/lon/alt). Both history endpoints return elevation (computed for
  older logs without it). Added an Elevation chart to the Sonde Statistics modal
  (index.html). dashboard.html has no per-sonde stats view, so nothing to add.
- Files: src/webui/web_server.py, templates/index.html

## v1.0.62 (2026-08-09j) - IQ dump via config + data/logs path
- The env-var IQ dump never fired (systemd doesn't pass shell env; PrivateTmp
  hides /tmp). Now enabled by sdr.airspy.debug_iq_dump: true and written to
  data/logs/ch_dump_<freq>.raw (visible in the app folder). Env var still works.
- Files: src/sdr/airspy_receiver.py, config.yaml.example

## v1.0.62 (2026-08-09i) - Channelizer decode diagnostics
- Resampler overlap-save fix (h) is confirmed running but did NOT restore decode,
  so transients weren't the cause. Added instrumentation to isolate DDC-output vs
  real-time/pipe: env OPENWXSDR_IQ_DUMP=1 tees the exact decoder-input IQ to
  /tmp/openwxsdr_ch_<freq>.raw (test offline with rs41mod), and a dropped-output-
  block counter now WARNs on channelizer queue overflow (real-time overload =
  IQ gaps = no FSK lock) instead of failing silently.
- File: src/sdr/airspy_receiver.py

## v1.0.62 (2026-08-09h) - Fix Airspy channelizer decode (stateless resampler)
- Root cause of channelizer zero-frames: AirspyChannelizer ran resample_poly
  statelessly per chunk, so its anti-alias filter restarted from zero every
  ~2.5ms — a transient corrupting ~1/3 of every block, shredding the FSK (legacy
  decodes because sox resamples continuously). Made the resampler stateful via
  overlap-save (per-channel warm-up history + 1-chunk lookahead, drift-free).
  Verified numerically: 0.0 error vs a continuous resample (was 59%).
- File: src/sdr/airspy_receiver.py
- The v1.0.62 Airspy dft_detect classification opened a second airspy_rx per
  candidate. In channelizer mode (which holds one airspy_rx open continuously)
  the extra back-to-back device open left the Airspy degraded — DDC RMS ~0.002,
  decoder healthy but zero frames. Skip dft classification in channelizer mode
  (bandwidth fallback, as pre-1.0.62); add an 0.8s device-settle after the
  legacy clip so libusb releases before the pipeline reopens.
- File: src/sdr/airspy_receiver.py
- Station popup sonde rows now lead with time, then distance, elevation, course,
  and add last altitude + battery. _tail_last_position parses battery and the
  per-frame subtype; _recent_sondes adds battery + filename.
- Each sonde is a link: focuses it if still active, else loads its logfile
  history (openRecentSonde).
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09e) - DFM type from logs + badge/overlap fixes
- Loading a DFM log no longer shows "Unknown": type comes from the log header
  (all-numeric serials = DFM). Logs record the subtype per frame ("Type: DFM17");
  /api/logfiles returns a types map + history returns sonde_type, so list, badge
  and details show the subtype.
- Badge colours via baseType() so subtypes (DFM17, RS41-SGP) colour right.
- Inactive tile 3-dot menu no longer overlapped (removed opacity stacking
  context + :has() z-index lift).
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09d) - Tile polish + logfile tiles + popup window
- CPU/RAM Graph hides Frequencies tile + spans a full row so the chart fits;
  badge padding 4px/font 11px to fit beside the menu.
- Inactive (from-logfile) sondes render the full live tile (shared
  renderSondeCard, last log record); ×-button -> Close in the 3-dot menu.
- Station popup 'received' window now from webui.sonde_retention_time, labels the
  range (e.g. "last 4h"); _recent_sondes(window_s).
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09c) - Sonde tile modes + CPU/RAM graph
- Sonde tiles get a 3-dot menu (Standard/Extended). Extended second row shows
  Distance / Elevation / Battery (client-side height-aware look angle from
  station; needs station_alt, now passed to template). Open-menu guard stops the
  2s refresh closing it.
- CPU/RAM tile gets a 3-dot menu (Live/Graph); Graph draws a 60-min CPU+RAM
  Chart.js line chart from a client-side ring buffer.
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09b) - Gateway->sonde look angles (height-aware)
- Gateway popup distance now the 3D slant range (uses station.alt + sonde alt),
  via a spherical-earth ENU conversion, plus elevation angle (negative = below
  horizon by curvature) and course (azimuth° + compass point). New _look_angles();
  /api/gateway recent_sondes gains ground_km/elevation_deg/azimuth_deg.
- Files: web_server.py, templates/index.html.

## v1.0.62 (2026-08-09) - UI: stats menus, battery tiles, gateway marker
- Active-Sondes tile: 3-dot menu (Active/Total). Frames tile menu now Total/
  Today's Frames (new today_frames counter, UTC rollover).
- Sonde tile: M10/M20/DFM show battery(V) not SNR; header time now "..Z".
- Map: own-gateway marker; click shows identity/HW + last-hour sondes with
  great-circle distance (new /api/gateway, parses data/logs). /api/sondes +battery.
- Files: __init__.py, web_server.py, templates/index.html.

## v1.0.61-dev (2026-08-08b) - auto_rx-grade classification (3 changes)
- Pin source: install_softchain.sh clones auto_rx @ v1.8.2 (was --depth 1 HEAD),
  so all gateways build identical dft_detect/fsk_demod/mod. Override AUTORX_REF.
- RS41 fast-path now DEFAULT OFF (detection.rs41_fastpath): every candidate is
  classified purely by dft_detect (fixes DFM-as-RS41). ~15s slower/RS41; re-enable
  per gateway.
- Airspy now classifies via dft_detect too: captures a 48k int16 IQ clip per
  candidate → shared DftDetector (was bandwidth-only). Gated by
  detection.airspy_use_dft_detect. New classify_iq_file() in dft_detector.py.
- Files: install_softchain.sh, device_manager.py, airspy_receiver.py,
  dft_detector.py, config.yaml(.example).

## v1.0.61-dev (2026-08-08) - Airspy: strong-narrow birdie reject
- Airspy started decoders on 401.x MHz "ghosts" (12-17 dB but only 2.4-3.1 kHz
  wide). Real sondes spread wide above the detection floor; a strong-yet-narrow
  peak is a CW/spur birdie. AirspyScanner now rejects SNR>=birdie_reject_snr_db
  AND BW<birdie_reject_bw_hz. Configurable: sdr.airspy.scan_min_bw_hz (2400),
  birdie_reject_snr_db (12), birdie_reject_bw_hz (4500). Adds birdie= to the
  Detection log line. Files: src/sdr/airspy_receiver.py, config.yaml(.example).

## v1.0.61-dev (2026-08-01) - rtl_power scanner: SIGINT flush + parse-on-stop
- Field finding: rtl_power on the R820T produces a valid full-band sweep (2 hop
  rows) but `-1` (single shot) often does NOT self-exit — it hangs after writing
  the sweep. The scanner previously used subprocess.run(timeout) which SIGKILLed
  it (no flush) AND discarded the CSV, so a good sweep was thrown away and the
  device looked "hung". Fix: run rtl_power under Popen, let it collect one sweep
  (~integration_s*hops+8 s, capped by wall_timeout_s), then stop it with SIGINT
  (rtl_power's own handler flushes the CSV + exits cleanly, exactly like Ctrl-C),
  and parse the CSV regardless of how it ended. Also honors a manual-decode
  abort mid-scan. This makes rtl_power viable on dongles where `-1` won't exit.
- File: src/sdr/rtl_power_scanner.py, config.yaml(.example) comment

## v1.0.61-dev (2026-07-29b) - rtl_power full-band scan backend (Phase 1)
- New detection.scanner.backend (default 'welch' = unchanged). Set 'rtl_power'
  to scan the whole 402-406 MHz band each pass via rtl_power instead of the
  pyrtlsdr Welch 2.4 MHz segment scan — one dongle sees the full band every
  scan (no band-sweep gaps), catching just-launched low sondes sooner. Works on
  a single SDR (time-shared with decoding). New src/sdr/rtl_power_scanner.py
  (subprocess wrapper + CSV parser, hard wall timeout, no libusb-wedge class).
  device_manager: pluggable backend in _scan_cycle (shared _process_scan_signals
  reused by both paths), band-sweep + capture watchdog gated off under
  rtl_power, auto-fallback to welch if the binary is missing. detect_signals()
  and detection_threshold apply identically to both backends.
- Files: src/sdr/rtl_power_scanner.py (new), device_manager.py, config.yaml(.example)

## v1.0.61-dev (2026-07-29) - install_softchain: busy binaries + iq_dec
- install_softchain.sh failed with "Text file busy" (ETXTBSY) when a target
  binary (e.g. fsk_demod) was being executed by the running service, and set -e
  aborted before iq_dec was installed. Now copies to <name>.new and atomically
  mv's into place (replaces the name even while the old inode stays live for the
  running process), and each install is guarded with || true.
- iq_dec was never installed: auto_rx's top-level build doesn't reliably emit it
  (and rs1729/RS master's demod/mod Makefile has no iq_dec target at all). Now
  built explicitly via `make -C demod/mod iq_dec` before install. Restart the
  service to pick up the new binaries.
- File: scripts/install_softchain.sh

## v1.0.61-dev (2026-07-28b) - Fix RS41 fast-path misclassifying DFM
- A just-starting DFM-17 whose 3 dB width measured abnormally narrow (3.5-4.7 kHz,
  vs its real ~8 kHz) landed in the RS41 fast-path BW window (3500-7000 Hz), so
  the fast-path skipped dft_detect, ran rs41mod on a DFM → 0 frames → birdie
  block. (Same sonde/antenna decoded fine on another gateway that measured 8.2
  kHz → dft_detect → DFM.) Fix: when a fast-path RS41 decode makes 0 frames, the
  frequency is flagged so the NEXT detection skips the BW fast-path and uses
  dft_detect for a proper type ID (self-corrects to DFM). Keeps the fast-path's
  speed for real RS41s. New RTLSDRDeviceManager.note_fastpath_failed /
  should_skip_fastpath.
- File: device_manager.py

## v1.0.61-dev (2026-07-28) - Detection cascade fixes (band-sweep + birdie block)
- (C) Band-sweep no longer abandons a segment that still has a detected sonde:
  the empty-scan counter resets whenever a real (non-blacklisted, not
  already-decoding) signal is present, even one skipped this pass. Fixes SDRs
  sweeping away from a sonde that was momentarily in cooldown.
- (B) 0-frame "birdie" block is now an ESCALATING backoff instead of a flat
  15-min lockout: first miss blocks failed_decode_base_cooldown_s (60s),
  doubling per repeat up to failed_decode_cooldown_s. It also clears early when
  the frequency reappears failed_decode_snr_rise_db (3 dB) stronger than when it
  failed — so a weak, ascending sonde is retried and decodes promptly instead of
  being locked out while it strengthens (observed: 404.500 decoding ~8 min late).
- Files: device_manager.py, config.yaml

## v1.0.61-dev (2026-07-27c) - Optional USB-reset auto-recovery for wedged SDRs
- New recovery.usb_reset_on_wedge (default false): when the watchdog quarantines
  a device whose capture_spectrum() libusb read is stuck, reset that dongle's USB
  port (matched by serial via sysfs, then USBDEVFS_RESET) to abort the stuck read,
  then un-quarantine it so the worker reopens and resumes — instead of the device
  staying dead until a full restart. Attempt-limited per device
  (usb_reset_max_attempts, default 3); needs root/udev perms for /dev/bus/usb.
  Falls back to the existing permanent-quarantine behaviour if reset fails.
  Field-validated: two wedged dongles self-healed in ~10s, no restart. The
  expected LIBUSB_ERROR_NO_DEVICE from the deliberate reset is now logged calmly
  (INFO, no traceback) and not counted as an open failure (_recovering flag).
- Files: device_manager.py, config.yaml(.example)

## v1.0.61-dev (2026-07-27b) - Fix DFM decode broken by --dist/--ptu/-ID
- dfm09mod exited 255 immediately (DFM decode fully dead since ~2026-07-21) on
  every field build: --dist/--ptu/-ID are rejected by these binaries but
  _probe_flags_accepted false-positived them (they neither print usage nor exit
  cleanly). Now gated on --help (decoder_caps) ONLY for both the --IQ and soft
  DFM branches — no probe — so they're added only when the binary documents
  them. Restores the known-good 2026-07-17 command '--auto -vv --IQ 0.0 --ecc
  --json'. (DFM PTU/serial-unmask need a newer dfm09mod that lists those flags.)
- File: rs1729_decoder.py

## v1.0.61-dev (2026-07-27) - Soft-chain weak-signal fix + harness log fix
- Soft chain was deaf below ~30 dB (harness: RS41 softin 2/30 vs --IQ 18/30).
  Cause: fsk_demod used auto_rx's PRE-2025-08-26 params. Adopted current auto_rx
  invocations: RS41 '--mask 5000 --nsym=300 -p 5' (was '--mask 4800'); DFM '-i'
  inverted soft bits, no mask (was none). Needs fsk_demod built from current
  auto_rx (install_softchain.sh). Files: rs1729_decoder.py, ka9q_receiver.py
  (KA9Q soft chain updated to the same RS41 params for consistency).
- Test harness: scheduled-start countdown spammed thousands of identical lines
  in the final second (sleep floored at ~0). Sleep now floored to 1s; countdown
  logged once per 5-min bucket + final minute. File: testscripts/rs_decode_test.py.
- Harness: replaced deprecated datetime.utcnow() with timezone-aware
  datetime.now(timezone.utc) (silences DeprecationWarning on Python 3.12+);
  timestamp format unchanged.

## v1.0.61-dev (2026-07-26i) - Test harness: quiet live-report HTTP server
- The built-in report server no longer dumps a ConnectionResetError/BrokenPipe
  traceback to the console when a browser refreshes/closes mid-request (custom
  handle_error swallows benign disconnects; do_GET guards writes). Server keeps
  serving; genuinely unexpected errors still print one line.
- Files: testscripts/rs_decode_test.py

## v1.0.61-dev (2026-07-26h) - Test harness: multi-SDR (compare + failover)
- device.serials: [A, B] + device.multi_mode. 'compare' runs the full matrix on
  each SDR (report gets a per-SDR frames matrix, per-SDR comparison table, best
  SDR/antenna, and an SDR column in the step log). 'failover' uses the primary
  and switches to the next SDR on a USB-claim/init/start error (sticky; a SDR
  erroring 3x is marked dead), so a wedged dongle no longer ends the run.
  device.serial (single) still works unchanged.
- Files: testscripts/rs_decode_test.py, testscripts/test_input.yaml

## v1.0.61-dev (2026-07-26g) - Test harness scan: direct target SNR
- rs_decode_test.py scan step now measures SNR AT the known test frequency
  (peak within ±3 bins minus median noise floor) instead of relying on the
  production peak-detector + threshold, which reported 'target not detected'
  for signals below detection_threshold that the decoder still decodes. Any
  threshold-crossing peaks are still logged to the repository-style scan log.
  Fixed the doubled "scan: scan:" console prefix.
- Files: testscripts/rs_decode_test.py

## v1.0.61-dev (2026-07-26f) - Test harness: scheduled start + live report server
- test_input.yaml loop.start_time: wait until a local HH:MM before starting
  (e.g. begin overnight when area sondes are up); end_time now counted from the
  actual start so both compose. Rotates modes still applies.
- Built-in HTTP server (http_server.enabled/port, default :8099) serves the live
  testresult.html to any LAN browser DURING the run — the main web UI is offline
  then (service stopped to free the SDR).
- web_server.py: new /testresult (+/testresults) route serves the harness report
  once the service is back up; path via webui.testresult_path.
- Files: testscripts/rs_decode_test.py, testscripts/test_input.yaml, web_server.py

## v1.0.61-dev (2026-07-26e) - Decode test harness + iq_dec in soft chain
- iq_dc_block now also applies to the soft chain (rtl_fm → iq_dec → fsk_demod →
  --softin), not just --IQ; enables the softin+iq_dec test mode. Off by default.
- New testscripts/rs_decode_test.py + test_input.yaml: reads config.yaml, uses
  one configured RTL-SDR, scans each test frequency (repository-style scan log),
  then decodes with 4 modes (iq / softin / iqdec / softin+iqdec) for a dwell,
  loops over passes with pauses until retries or end_time. Logs to console + CSV,
  live-refreshes testresult.html, prints a summary ranked by frames decoded.
  --dry-run simulates without hardware. Reuses RS1729Decoder/AudioPipeline/
  SpectrumAnalyzer (real code paths). Adds repo root+src to sys.path so it runs
  as testscripts/rs_decode_test.py; scan step auto-skips (one warning, decode
  continues via rtl_fm) when pyrtlsdr is absent, or with --no-scan. Per-pass
  mode-order rotation (rotate_mode_order, default true) removes the cold-device
  first-slot bias; summary shows multiplier vs baseline and auto-flags likely
  --IQ DC-spike deafness. First live run confirmed plain --IQ can be deaf while
  iq_dec/soft chain decode fully.
- Files: rs1729_decoder.py, config.yaml(.example), testscripts/*

## v1.0.61-dev (2026-07-26d) - Service Status / About modal fields
- Service Status modal: new 'Memory / Disk' row below Hardware (total RAM +
  root disk free/total) and a 'Version' row below Service (version, build date,
  station). /api/service_status now returns ram_total/disk_total/disk_free
  (psutil→/proc/meminfo for RAM, stdlib shutil.disk_usage for disk) + version.
- About modal (index + dashboard, EN+DE): new 'Hostname / IP' row, filled from
  /api/service_status on open.
- Files: web_server.py, templates/index.html, templates/dashboard.html

## v1.0.61-dev (2026-07-26c) - Inline iq_dec DC removal (--IQ chain)
- New decoders.iq_dc_block (default false): routes rtl_fm -M raw cs16 IQ through
  rs1729 iq_dec (--bo 16, +--IFbw for >80 kHz) before the --IQ decoder, matching
  auto_rx's dc_block. Strips the RTL centre-spike DC offset off the sonde tone;
  no effect on the fsk_demod soft chain. Graceful no-op if iq_dec binary absent.
  install_softchain.sh now also installs iq_dec.
- Files: rs1729_decoder.py, device_manager.py, config.yaml(.example),
  scripts/install_softchain.sh

## v1.0.61-dev (2026-07-26b) - Frequency repository listing refinements
- Weak signals now listed: the repository uses a separate, lower
  detection.repository_threshold_db (default 8) so sub-decode-threshold sondes
  (e.g. 404.100/405.300) get logged for manual selection while auto-decode still
  uses detection_threshold. detect_signals() gained a threshold_db override.
- Segment-aware grid filter for unconfirmed entries: accept the coarse 100 kHz
  channel grid in every segment, PLUS the fine 10 kHz grid only inside
  repository_dense_ranges (default 403.0-404.0, the DFM/military band that uses
  10 kHz channels like 403.13/403.55). Elsewhere the 10 kHz grid is rejected so
  RTL spurs (404.040/405.660 …, also on the 10 kHz grid) don't clutter the list.
  Decoded sondes are still logged 'confirmed' regardless of grid.
- Files: rtlsdr_analyzer.py, device_manager.py, config.yaml(.example)

## v1.0.61-dev (2026-07-26) - Frequency repository (log + modal)
- New per-session log logs/sdrfreq_<ts>.log recording radiosonde band activity:
  a 'detected' row per 10 kHz channel the scanner finds, upgraded to 'confirmed'
  when a decode produces telemetry (freq/type/serial/SNR/RSSI/alt). Written by
  the scanner (detected) and telemetry handlers (confirmed), deduped.
- New GET /api/frequency_repository parses the newest sdrfreq_*.log into a
  per-frequency summary. New "Frequencies" button + modal in the web UI lists
  the channels (confirmed/detected) with a quick "decode this frequency" action.
- Files: openwxsdr_app.py, device_manager.py, web_server.py, index.html

## v1.0.61-dev (2026-07-25d) - Telemetry log format + no fake SNR/RSSI
- Telemetry log line: dropped the "Telemetry: " prefix; added T:/P:/H:/SNR:/
  RSSI:. Optional fields show N/A when the sonde/receiver gave no real value
  (temp 0 °C is kept as real; SNR/RSSI None or 0.0 → N/A). Both rtlsdr and KA9Q
  log sites share one formatter.
- Removed the fabricated 25 dB/25 dBm default: manual/imported decodes build a
  synthetic signal with a placeholder strength and no measured power — that no
  longer surfaces as SNR/RSSI; it's N/A until real values arrive.
- Files: openwxsdr_app.py, device_manager.py

## v1.0.61-dev (2026-07-25c) - Band-sweep resilience (opt-in)
- Added detection.band_sweep (default OFF): a device starts at its configured
  center_freq and, after dwell_empty_cycles empty scans, retunes to the next
  segment center spanning band_min_hz..band_max_hz. Any single SDR can then
  cover the whole band over time, so if one dongle fails the survivors close
  its gap. Segments auto-tile the band with overlap (402.1-405.9 → 2 segments
  at ~403.12/404.88 MHz for 2.4 MSPS). Documented a DC-safe 4-device static
  center plan (402.775/403.625/404.425/405.225). Static scan path unchanged
  when disabled.
- Files: device_manager.py, config.yaml

## v1.0.61-dev (2026-07-25) - center_freq crash + soft-chain 0-frames
- KeyError 'center_freq' crash-loop: a device entry in config.yaml missing
  'center_freq' made SpectrumAnalyzer raise on EVERY scan cycle, spamming a
  traceback every ~5s and leaving that device permanently dead. Manager now
  validates/repairs device configs (fills center_freq/sample_rate + warns once);
  SpectrumAnalyzer uses .get() defaults as defense-in-depth.
- Soft chain 0 frames: the rtl_fm→fsk_demod→rs41mod --softin chain produced
  healthy-but-frameless decodes on RTL clients while direct --IQ decoded the
  same signal fine. decoders.soft_decode now defaults OFF (use reliable --IQ);
  opt back in once fsk_demod is verified. KA9Q's separate soft chain unaffected.
- Files: device_manager.py, rtlsdr_analyzer.py, rs1729_decoder.py, config.yaml

## v1.0.61-dev (2026-07-24f) - RSSI == SNR fix
- Active-sonde RSSI (dBm) and SNR (dB) displayed the identical value. Cause:
  on the direct-pipe --IQ chain there are no live IQ metrics, and both rssi and
  snr fell back to the same scan value (_cur_signal_strength_db, which is an
  SNR). Fix: DetectedSignal now carries the absolute peak power (power_dbfs)
  and noise floor; RSSI falls back to the power, SNR to the SNR, so they differ.
  Same fix applied to the channelizer path. Note: without live_signal_metrics,
  RSSI is dBFS peak power (relative), not a calibrated dBm; enable
  decoders.live_signal_metrics for true live RSSI/SNR.
- Files: rtlsdr_analyzer.py, device_manager.py, decoder_manager.py
- "PTU degraded mode" fired on every RS41 frame and PTU came from the less
  accurate text path. Two bugs: (1) JSON gate required ALL of temp+humidity+
  pressure to be truthy, so a missing field (non-SGP have no pressure) or a
  0.0 value forced text fallback and discarded good JSON temp/humidity;
  (2) in --ptu2 mode rs41mod prints "RH2=" (not "RH="), so the text parser
  never captured humidity (RH=None). Fix: PTU is now JSON-first per-field
  (is-not-None, 0.0 valid), text only fills genuinely-absent fields; text
  parser accepts _RH/RH/RH2. Verified via rs1729 source (--ptu2 = ptu mode 2,
  RH2). SondeHub/HTTP/UDP/MQTT now carry accurate JSON PTU. Warns only if JSON
  truly emits no PTU (flag/build problem).
- Files: rs1729_decoder.py

## v1.0.61-dev (2026-07-24d) - Fix gain:0 deafness (0 frames)
- Devices with configured gain 0 ("0 = auto") decoded 0 frames even on 30 dB+
  signals: rtl_fm was launched with '-g 0', which is MANUAL near-minimum gain,
  not auto. rtl_fm only auto-gains when -g is omitted. Now gain>0 → '-g N',
  gain 0 → omit -g (true auto). Fixes DL2MF-14 RTL00001/RTL00003 decoding
  nothing while the old build (gain 20) worked.
- Files: audio_pipeline.py

## v1.0.61-dev (2026-07-24) - Wedged-capture watchdog + quarantine
- Overnight: RTL00001 stuck inside capture_spectrum() for hours (a libusb read
  hung); every Import-API assignment aborted, 0 frames all night while its 3
  siblings were healthy. The SIGBUS guard prevented the crash but left the
  device dead forever. Added a per-device watchdog thread: if a capture is
  in-flight >CAPTURE_HANG_TIMEOUT_S (45s, well above the 8s wall cap), the read
  is wedged (uninterruptible in-process) → QUARANTINE the device (mark ERROR +
  _wedged, park the worker) so Import-API/scan skip it and assign nearby sondes
  to a healthy receiver. Safety net: if ALL devices wedge, os._exit(1) for a
  clean systemd restart. Note: RTL00001 wedging repeatedly points to a bad
  dongle/USB port/PLL on that Pi.
- Files: device_manager.py

## v1.0.61-dev (2026-07-23c) - Fix stale PTU log messages
- The "does not support --softin ... text PTU fallback ... install Auto_RX
  decoders for full PTU" WARNING was obsolete after --sat removal (the --IQ
  chain now emits full JSON PTU). Demoted to an accurate INFO.
- The "--ptu2 will likely be unavailable" WARNING fired whenever --help didn't
  list --ptu2, but --help under-reports — now probe-verified, so it only warns
  when --ptu2 is genuinely rejected.
- Files: rs1729_decoder.py

## v1.0.61-dev (2026-07-23b) - RS41 detection fast-path
- RS41 "deaf" fix: V1.0.60 ran the ~15s dft_detect step (close/settle/5s
  capture/correlate/reopen) on EVERY signal, and weak RS41 sometimes missed the
  0.53 correlation → slow + occasionally undetected. Added a bandwidth fast-path
  in _identify_sonde_type: a 3 dB width of 3.5-7.0 kHz (unambiguously RS41 — RS92
  is narrower, DFM/M10/M20/iMet wider) decodes immediately, restoring V1.0.50's
  instant reliable RS41 start. dft_detect still runs for wider/ambiguous signals
  (DFM subtype, M10 vs M20, iMet) — the Vigor-patch gains are preserved.
  Config: detection.rs41_fastpath (+bw_min/max).
- detection_threshold 18→14 dB (catch weaker RS41 like V1.0.50; birdies now
  filtered by dc_notch_hz + failed_decode_cooldown_s).
- Files: device_manager.py, config.yaml

## v1.0.61-dev (2026-07-23) - Multi-SDR stability (V1.0.60 regression fix)
- CRITICAL: removed the threaded test-read probe in device_manager._scan_cycle.
  On timeout it set self._analyzer=None WITHOUT close(), leaking the RtlSdr
  handle → permanent LIBUSB_ERROR_BUSY → 10-failure os._exit restart loop
  (field: 144 restarts, "4 SDR always lost, self-healing dead"). Now a
  successful open() proceeds directly and the first capture_spectrum() is the
  real read; failures go through _teardown_scan() which close()s properly (no
  leak, no SIGSEGV since no detached thread). Matches V1.0.50 behavior.
- STATE_ERROR backoff capped 300s→30s (reopens now succeed, no need to park).
- SIGBUS fix on manual decode (v2): the _capturing guard alone wasn't enough —
  on a contended 4-dongle Pi capture_spectrum() ran 25s+ (a nominal 5s dwell
  inflates under CPU/USB contention), so the guard's "proceed cautiously" still
  closed mid-read → SIGBUS. Two changes: (1) capture_spectrum() now has a hard
  wall-clock cap (detection.scan_max_wall_s, default 8s) and returns a partial
  average instead of running for minutes — this also fixes "first scan never
  completes"; (2) the manual path now ABORTS the decode (leaves device scanning,
  returns False) if still capturing after the wait, NEVER closing mid-read.
- Files: device_manager.py, rtlsdr_analyzer.py

## v1.0.61-dev (2026-07-21c) - Import API distance for SondeHub
- api.v2.sondehub.org's /sondes omits "distance", so every imported sonde got
  distance_km=0 (broke nearest-first sort + receiver assignment). Now computed
  via haversine from the station position when the URL is sondehub.org (or when
  any source returns 0/missing distance but the sonde has a real fix).
- Files: import_api/sonde_api_client.py

## v1.0.61-dev (2026-07-21b) - RS41 PTU fix
- CRITICAL: removed --sat from all RS41 rs41mod commands (soft chain + --IQ +
  default). rs41mod only emits JSON PTU from the ec>=0 branch, where get_PTU()
  is guarded by `!sat` (rs41mod.c ~2279) — so --sat silently disabled
  temp/humidity/pressure in JSON. The JSON "sats" field is numSV from the GPS
  decode and does NOT need --sat, so nothing is lost.
- KA9Q PTU: environment condition changed from truthiness to `is not None`
  (a real 0.0 °C / 0.0 % reading was being dropped).
- Files: rs1729_decoder.py, openwxsdr_app.py

## v1.0.61-dev (2026-07-21) - SondeHub payload fixes
- rs41_mainboard_fw now submitted as string (was int).
- tx_frequency now converted kHz→MHz (405700.0 → 405.7); it's stored in kHz,
  not Hz — fixes sondehub_output.py + sondehub_queue.py.
- Added uploader_alt (float) to telemetry payload.
- PTU (temp/humidity/pressure) already correctly gated + submitted.
- Files: sondehub_output.py, sondehub_queue.py

## v1.0.61-dev (2026-07-17) - Soft-Decision Decode Chain (P1)

### Added
- fsk_demod soft-bit decode chain (auto_rx method) for RS41 + DFM in the RTL
  legacy path: rtl_fm -M raw → fsk_demod → decoder --softin. ~2 dB sensitivity
  gain, kHz-level mistuning tolerance, PTU now direct in JSON. Auto-fallback
  to --IQ chain when fsk_demod/--softin/--json unavailable. New config:
  `decoders.soft_decode` (default true). DFM soft chain captures at 50 kHz
  (fsk_demod needs integer samples/symbol). fsk_demod EbNodB tracked from
  --stats and exposed in decoder stats.
- Empirical flag probing: --help under-reports flags on many rs1729 builds
  (field log: rs41mod ran --ptu2 fine while not listing it). Soft-chain flags
  (--softin/--json/--ecc/...) are now verified by spawning the decoder with
  the flag + empty stdin when --help says no. Enables the soft chain on
  existing field installs without rebuilding decoders.
- Landed-sonde guard: imported-sonde decodes that end idle now block re-
  assignment — 6 h if last frames showed low altitude + descent (landed),
  10 min for signal loss / zero frames (freq-keyed if no serial). Stops the
  assign→idle(600s)→teardown→re-assign loop on landed sondes. New config:
  import_api.reassign_cooldown_s / landed_alt_m / landed_cooldown_s.
- DC-spur guard: scan peaks within detection.dc_notch_hz (default 25 kHz) of
  a device's center frequency are rejected — RTL-SDR DC/1-f spurs caused
  phantom decodes (field: "DFM" at 404.5813, 18.7 kHz below 404.600 center).
- Negative-result cache: auto decodes ending with 0 frames block that
  frequency (±10 kHz) from re-detection for detection.failed_decode_cooldown_s
  (default 900 s). Breaks the detect→decode-nothing→rescan birdie loop.
- dft_detect: empty output from all CLI formats now logs an actionable
  WARNING (broken binary) instead of a generic no-results message.
- scan_check_time default 20 → 5 s (field: 4-device contention inflated first
  scan dwells to 2-8 min, starving decoders); now also in config.yaml.
- Empty-spectrum web UI polls no longer spam WARNINGs (rate-limited INFO).
- New scripts/install_softchain.sh: builds fsk_demod + working dft_detect
  (+ optionally auto_rx decoder builds) from pinned radiosonde_auto_rx.
- KA9Q log cleanup: the full fsk_demod stats JSON (EbNodB + eye_diagram +
  samp_fft, per channel every 5s) is no longer logged — replaced by a compact
  throttled "fsk_demod <freq>: EbNodB=NN dB" line (config ka9q.ebnodb_log_
  interval_s, default 30). Per-packet I/Q-write, per-spawn command echoes,
  reader-started, and routine decoder stderr demoted to DEBUG.
- KA9Q telemetry now reaches outputs: the decoder-output reader had a TODO
  where JSON frames were logged but never parsed/forwarded, and KA9QReceiver
  was constructed with no callback. Now wired to _handle_ka9q_telemetry
  (mirrors flux242) → web UI / UDP / MQTT / SondeHub. Channel frequency is
  injected from the RTP/SSRC mapping (decoder JSON carries none).
- CRITICAL KA9Q fix: RTP samples from ka9q-radio are big-endian but fsk_demod
  reads native little-endian — raw payload was byte-swapped garbage, so EbNodB
  stayed at noise floor and ZERO frames decoded even on strong sondes. Now
  byte-swapped before fsk_demod (config ka9q.rtp_byte_swap, default true,
  auto-disabled on big-endian hosts). This is why KA9Q never produced frames.
- KA9Q: missing fsk_demod now logs ONE actionable startup error (pointing to
  install_softchain.sh) instead of a per-stream FileNotFoundError traceback
  every scan cycle. Binary paths resolved + verified once at construction.
- RS41 --IQ chain now adds probe-verified --ecc2/--ecc (Reed-Solomon):
  recovers weak frames and rejects corrupted ones (field: no-GPS desk sonde
  emitted random 400 km/frame coordinates — CRC-failed data passed through).
  DFM --IQ flags (--ecc/--json/--dist/--ptu/-ID) now caps-or-probe gated too.
- CRITICAL: worker loop had no STATE_ERROR branch — one failed USB open set
  Error (for UI) and the loop busy-spun at 100% CPU, never retrying (field:
  3/4 devices stuck in Error 24 h; V1.0.50 retried forever). Now: backoff
  (30s×failures, max 5 min) → retry scan; service-restart safety net kept.
- dft_detect capture now always 48 kHz: the 3 dB-BW estimate underestimates
  M10/M20 ("2.3 kHz" for a ~12 kHz M20) → narrow 24 kHz capture clipped the
  signal, 6 consecutive failed identifications in the field.
- New decoders.live_signal_metrics (default false): direct rtl_fm → decoder
  piping (V1.0.50 topology, no Python pump in signal path — suspected RS41
  frame-yield regression cause). true restores live RSSI/SNR pump.
- Python 3.8 compatibility: builtin-generic annotations (tuple[...]) crashed
  fresh 3.8 installs at import — replaced with typing.Tuple in
  decoder_manager.py + airspy_receiver.py; full src/ sweep found no other
  3.9+ constructs.
- Files: rs1729_decoder.py, device_manager.py, decoder_manager.py,
  airspy_receiver.py, rtlsdr_analyzer.py, dft_detector.py, config.yaml,
  scripts/install_softchain.sh

## v1.0.52.002 (2026-07-07) - Version Consolidation & M10/M20 Decoder Safety [TEST BUILD]

### Reconciliation Build
Merged the working `src/` tree (with USB/RTL-SDR/sonde-detection fixes) against the
parallel `OpenWXSDR_v1.0.52_RS41_DFM17_KA9Q_basic` snapshot (tested for several days).
Confirmed the working tree's device_manager.py (thread-based USB test-read timeout,
20 kHz registry tolerance, DFT frequency-offset correction), dft_detector.py (frequency
offset + phase-inversion `math.fabs()` fix for M10/M20), and openwxsdr_app.py (safer
telemetry logging) are all strict improvements over the snapshot. All other `src/`
modules (ka9q_receiver, ka9q_control, flux242_receiver, channelizer, airspy_receiver,
webui/web_server, decoder_manager, models, sondehub_output/queue, http_output,
signal_metrics, audio_pipeline, rtlsdr_analyzer, import_api) were byte-identical
between the two trees.

### Fixed
- **Single source of truth for version info**: `openwxsdr.py` console banner/`--version`,
  `mqtt_output.py` (previously parsed a non-existent `Version:` line in MANIFEST.md and
  silently fell back to a hardcoded string), `udp_output.py` (hardcoded `'1.0.0'`), and
  `kindle/dashboard_generator.py` default parameters now all import `__version__` from
  `src/__init__.py` instead of hardcoding version strings. Web UI and SondeHub outputs
  already did this correctly.
- **M10/M20 decoder crash regression**: the working tree had started unconditionally
  appending `--dc`, `--ptu`, `--json`, `--lpIQ` to the m10mod/m20mod command line. This
  is the exact failure mode documented in `EMERGENCY_FIX_v1.0.46a.md` (older decoder
  binaries reject unrecognized flags → immediate crash → zero frames decoded). These
  flags are now gated behind capability probing (`decoder_caps`), the same safe pattern
  already used for DFM. Extended `_detect_decoder_capabilities()` to probe for `--dc`
  and `--lpIQ` support in addition to the existing flags.
- Documented `Pillow` as an optional dependency in `requirements.txt` for the Kindle
  dashboard feature (`/api/kindle/*` endpoints), which had no dependency declaration
  anywhere despite requiring PIL.

### Fixed (found during first test deployment)
- **`build_python_update.sh`/`.ps1` never shipped `openwxsdr.py`, `src/kindle/`, or
  `src/import_api/`**: the update-package script only staged `src/{openwxsdr_app,
  __init__}.py` plus `decoders/output/sdr/webui`. The root entry point (console
  banner/version) and two active feature directories were silently excluded from every
  Python-only update — explains the console log still showing "Version 1.0.50" after
  deployment even though `src/__init__.py` was updated. Both scripts (and their embedded
  installers) now stage/deploy/backup all five locations.
- **Multi-device startup could hang for hours on `priority_frequency`**: `RTLSDRDeviceManager.start()`
  called `_check_priority_frequency()` synchronously for the first worker (idx 0), which
  blocks in a loop for up to `priority_check_timeout` seconds (config default 30s, but
  configurable to hours) waiting for a decoded frame. Because this ran inline in the
  same for-loop that starts every device worker, workers for all *other* devices never
  had `.start()` called until the first device's priority check finished or timed out —
  on a 4-device setup with a multi-hour timeout, devices 2–4 would never scan at all
  (symptom: web UI spectrum view shows data for device 1 only, others stuck on "No
  spectrum data yet"). Fixed by running the wait-for-USB-init + priority check in a
  background thread (`_run_priority_frequency_check`) so the remaining workers start
  immediately.

## v1.0.50 (2026-06-16) - PTU Merging Improvements [RELEASE CANDIDATE]

### PTU Data Extraction Improvements
- **Improved PTU text fallback merging**: Changed from exact frame number matching to recency-based matching
  - **Problem**: PTU data from text output not merging reliably due to frame number misalignment
  - **Solution**: Match PTU data by proximity (+/- 5 frames) instead of exact frame numbers
  - **Result**: More reliable PTU data capture from verbose text output
  
- **Added --softin detection**: Decoder capabilities now auto-detected at startup
  - Probes `rs41mod --help` to check for --softin flag support
  - Logs clear warning if --softin unavailable, explaining IQ mode fallback
  - Preserves working IQ pipeline as primary path
  
- **Conditional environment population**: Only create environment block when PTU data exists
  - MQTT/SondeHub uploads only include PTU when actual measurements available
  - Prevents empty environment blocks from polluting telemetry
  
- **Timestamp-based PTU cache cleanup**: Improved cache management
  - Removes oldest entries by timestamp rather than arbitrary sorting
  - Maintains last 100 PTU entries for recency matching

### Pipeline Architecture
**Current (Working) Pipeline:**
```
rtl_fm -M raw (raw IQ samples) → rs41mod --IQ 0.0 (IQ input mode)
                                ↓
                    PTU from verbose text output (-vv --ptu2)
                                ↓
                    Recency-based merge into JSON frames
```

**Future Enhancement (requires Auto_RX-compatible decoders):**
```
fsk_demod (soft symbols) → rs41mod --softin -i --ptu2 --json
```

### Modified Files
- **src/decoders/rs1729_decoder.py**:
  - Added `_detect_softin_support()` class method to probe decoder capabilities
  - Improved `_extract_ptu_from_text()` with timestamp tracking
  - Changed PTU merging from exact frame match to recency-based (+/- 5 frames)
  - Added clear warnings when --softin not available
  - Maintains IQ mode (`--IQ 0.0`) as working default

- **src/decoders/decoder_manager.py**:
  - Improved environment conditional: only create when at least one PTU field has value
  - Ensures MQTT/SondeHub only carry PTU when real measurements exist

- **src/sdr/audio_pipeline.py**:
  - Maintains rtl_fm `-M raw` mode (verified working)
  - Ready for future fsk_demod integration

### Technical Details
- **Recency Matching Window**: +/- 5 frames for PTU merge
- **Cache Size**: Maintains last 100 PTU entries
- **Detection**: Runs once per decoder binary at first use
- **Fallback**: IQ mode with text PTU parsing when --softin unavailable

### Expected Behavior
With current IQ pipeline + improved merging:
- ✅ PTU data extracted from verbose text output (`-vv --ptu2`)
- ✅ Recency-based matching handles frame number misalignment
- ✅ Environment only populated when measurements exist
- ✅ Clear warnings if --softin unavailable

### Testing Checklist
1. Launch RS41 sonde, verify PTU data appears in logs
2. Check for "PTU Merged by recency" messages in decoder logs
3. Verify environment block only present when PTU data exists
4. Monitor decoder startup for --softin detection messages
5. Confirm MQTT/SondeHub uploads include PTU when available

## v1.0.32 (2026-05-05) - DFT Device Selection Fix and Flux242 Logging

### Critical Fixes
- **Fixed DFT device selection**: DFT correlation now uses the correct device for IQ sampling
  - Device selection now happens BEFORE sonde type identification
  - Passes selected device serial to `_identify_sonde_type()` method
  - Eliminates "usb_claim_interface error -6" when spectrum analyzer is running
  - Result: Accurate sonde type detection (RS41 vs DFM vs M10 vs M20)

- **Enhanced Flux242 logging**: Added INFO-level logging for received frames
  - UDP data reception now logged: "Received X bytes from address"
  - Decoded frames logged: "Decoded DFM frame from serial at frequency"
  - Helps diagnose JSON frame reception issues

### New Features
- **RTL-SDR device status table in web UI**: System Health section now displays table of all RTL-SDR devices
  - Shows device serial, status (scanning/decoding/idle/disconnected), frequency, and sonde type
  - Color-coded status indicators: green (active), yellow (idle), red (disconnected)
  - Updates every 5 seconds with current device assignments
  - Works in both RTL-SDR and Flux242 modes

### Technical Details
- Refactored `_start_decoder_for_signal()` in decoder_manager.py
- Device selection moved before `_identify_sonde_type()` call
- Selected device serial passed to DFT detector for correct IQ capture
- Flux242 UDP listener now logs all received data at INFO level
- New `/api/devices` endpoint returns device array with status information
- Web UI JavaScript fetches device data and populates table dynamically

### Bug Analysis
Previous versions called `_identify_sonde_type(signal)` without device_serial parameter,
causing DFT to default to device 0 (spectrum analyzer) which was already busy.
Fix: Pre-select device, then pass it to DFT detector before decoder starts.

---

## v1.0.31 (2026-05-05) - Configurable Sonde Retention

### New Features
- **Configurable sonde retention time**: Sondes now stay visible on map for configurable duration after last update
- **New config setting**: `webui.sonde_retention_time` (default: 600 seconds / 10 minutes)
  - Set to 0 to remove immediately when decoder stops
  - Set to high value (e.g., 3600) to keep tracks visible for 1+ hours
  - Prevents premature track removal during brief signal loss

### Configuration
- Added `sonde_retention_time: 600` to webui section in config.yaml
- Default 10 minutes provides good balance between current tracking and historical data

### Technical Details
- Web UI now reads retention time from config instead of hardcoded 1 hour
- Cleanup logs show retention timeout for transparency: "Removing sonde X3812402 - no data for 720s (retention: 600s)"
- Retention applies to all sonde types (RS41, DFM, M10, etc.)

---

## v1.0.30 (2026-05-05) - UI Filtering and Device Allocation Fixes

### Critical Fixes
- **Fixed incomplete sonde display in UI**: Sondes with invalid serial numbers (like "-+", "(+)", etc.) no longer appear in active list
- **Fixed device allocation bug**: System now checks device capacity before trying to start decoders, prevents RTL-SDR reuse errors
- **Changed log filename format**: Now uses `<sondeid>-YYYYMMDD-HHMMSS.log` instead of `<timestamp>-<sondeid>.txt`
- **Added log file append**: When same sonde reappears, appends to existing log file instead of creating new one

### New Features
- **Smart serial validation**: `_is_valid_serial()` method filters out incomplete/malformed serial numbers
  - Rejects serials shorter than 5 characters
  - Rejects serials starting/ending with special characters
  - Rejects serials that are mostly non-alphanumeric
- **Device capacity pre-check**: For single-device setups, verifies capacity before attempting decoder start
  - Logs active frequencies already using the device
  - Prevents "device busy" errors from rtl_fm

### Technical Details
- Enhanced sonde filtering in `/api/sondes` endpoint (web_server.py)
- Log filename format changed from `20260505-140158-X3812402.txt` to `X3812402-20260505-140158.log`
- Log files now check for existing sonde logs and append with session separator
- Device allocation check added before calling `create_pipeline()` in decoder_manager.py

---

## v1.0.29 (2026-05-05) - Critical DFT Device Selection Fix

### Critical Fixes
- **Fixed DFT detection device selection**: rtl_sdr doesn't support serial numbers like rtl_fm does
- **Added device enumeration**: Map serial numbers to device indices before calling rtl_sdr
- **Resolves "usb_claim_interface error -6"**: DFT now uses correct device instead of always device 0
- **Added RTL-SDR udev rules**: Included rtl-sdr.rules in package for proper device permissions

### Technical Details
- New `_get_device_index_from_serial()` method in dft_detector.py
- Uses rtl_test to enumerate devices and map serial→index
- rtl_sdr requires device INDEX (0, 1, 2...), not serial numbers
- Fixes issue where DFT always tried to open device 0 (spectrum analyzer) regardless of parameter

### Deployment Issues Resolved
- DFT detection now works correctly in multi-device setups
- Third and subsequent decoders can start successfully
- No more device conflicts between spectrum analyzer and DFT detection
- Udev rules properly installed for non-root RTL-SDR access

---

## v1.0.28 (2026-05-05) - DFT Correlation-Based Sonde Detection

### Major Feature: DFT-Based Sonde Identification
- **Integrated dft_detect from rs1729/RS for accurate sonde type detection**
- **Uses correlation analysis against known sonde signatures instead of unreliable bandwidth detection**
- **Automatic fallback to bandwidth-based detection if dft_detect not available**
- **Built automatically during install.sh execution**

### New Components
- `src/sdr/dft_detector.py`: DFT correlation detection module
  - Captures short IQ sample bursts at detected signals
  - Runs dft_detect correlation analysis
  - Parses correlation scores and applies type-specific thresholds
  - Correlation thresholds optimized for each sonde type:
    * RS41: ~0.53
    * RS92: ~0.54
    * DFM: ~0.62
    * M10: ~0.75

### Configuration
- Added `detection.use_dft_detect` setting (enabled by default)
- Added `detection.dft_detect_path` for custom dft_detect binary location
- Added `detection.dft_sample_duration` for IQ capture duration (default: 5.0s)

### Improvements
- Two-stage sonde detection: DFT correlation (primary) → Bandwidth heuristics (fallback)
- More accurate sonde identification, especially for RS41 vs DFM disambiguation
- Reduces false positives from bandwidth variability
- Graceful degradation when dft_detect not installed
- Consistent decoder source: Both decoders and dft_detect from rs1729/RS repository

### Installation
dft_detect is built automatically from rs1729/RS during install.sh:
```bash
# dft_detect compiled from rs1729/RS/scan/dft_detect.c
# Compilation: gcc -O2 dft_detect.c -lm -o dft_detect
# Installed to /usr/local/bin/ for system-wide access
# No separate installation required!
```

### Architecture Benefits
- **More accurate**: Correlation against actual signal signatures vs simple bandwidth check
- **Proven system**: Uses same detection as widely-deployed radiosonde_auto_rx
- **Optional**: Auto-fallback maintains compatibility without requiring dft_detect
- **Fast**: 5-second IQ capture and analysis before decoder start

## v1.0.27 (2026-05-04) - Sonde Detection Improvements

### Bug Fixes
- **Fixed RS41 misidentification**: RS41 sondes with 3-5 kHz bandwidth now correctly identified (were being misidentified as DFM)
- **Fixed spectrum analyzer coverage**: Adjusted center frequency from 404.6 MHz to 403.6 MHz to properly cover 403.23 MHz DFM signals
- **Reduced false positives**: Increased detection threshold from 15 dB to 18 dB to reduce transient noise detections

### Improvements
- Rewrote `_identify_sonde_type()` with accurate bandwidth classifications:
  - RS41: 3.0-5.5 kHz (most common radiosonde type)
  - DFM: ≥5.5 kHz (wider bandwidth)
  - RS92: 2.0-3.0 kHz (narrow bandwidth)
- Enhanced logging for sonde type detection with bandwidth information
- Spectrum analyzer now covers 402.4-404.8 MHz range (previously 403.4-405.8 MHz)

### Multi-Device Support
- Continued refinement of 4-device RTL-SDR support from v1.0.24-v1.0.26
- Device conflict prevention working correctly
- Automatic decoder distribution across available devices

## v1.0.22 (2026-05-04) - Flux242/Radiosonde Integration

### Major Feature: Multi-Sonde Reception
- **Integrated flux242/radiosonde framework for professional multi-sonde tracking**
- **Track 5-6 radiosondes simultaneously with single RTL-SDR!**
- **New SDR mode: `type: 'flux242'` in config.yaml**
- **Proven external decoder toolchain (receivemultisonde.sh + iq_server)**

### New Components
- `src/sdr/flux242_receiver.py`: Complete flux242 integration
  - UDP listener for decoded JSON frames (port 5678)
  - Process health monitoring
  - JSON telemetry parser and converter
- `install_flux242.sh`: Automated installation script
  - Installs dependencies (rtl-sdr, gawk, socat, jq, sox)
  - Clones and compiles flux242/radiosonde project
  - Compiles decoders (rs41mod, dfm09mod, m10mod, etc.)
  - Compiles iq_server channelizer

### Configuration
- Added complete `flux242` section in config.yaml
- **FIXED: script_path now uses absolute path** (was relative './radiosonde/...')
- Default: `/home/pi/radiosonde/scripts/receivemultisonde.sh`
- Configurable: center_freq, sample_rate, gain, threshold, UDP ports

### Documentation
- `FLUX242_QUICKSTART.md`: Quick start guide for users
- `docs/FLUX242_INTEGRATION.md`: Complete technical documentation
- `FLUX242_IMPLEMENTATION.md`: Technical implementation summary
- `FLUX242_PACKAGE_VERIFICATION.md`: Verification checklist

### Application Integration
- `src/openwxsdr_app.py`: Full flux242 mode support
  - Detects `sdr.type == 'flux242'` and initializes Flux242Receiver
  - New `_flux242_main_loop()` for process monitoring
  - New `_handle_flux242_telemetry()` for JSON conversion
  - Skips decoder_manager in flux242 mode (external script handles decoding)

### Architecture
- **Three SDR modes now supported:**
  1. `rtlsdr` - Built-in single-sonde decoder (1 sonde at a time)
  2. `flux242` - External multi-sonde decoder (5-6 sondes simultaneously) **RECOMMENDED**
  3. `ka9q` - Network-based SDR (future)

### Performance
- CPU usage: ~15-20% per sonde on Raspberry Pi 4
- Raspberry Pi 3B+: 3-4 concurrent sondes
- Raspberry Pi 4: 4-5 concurrent sondes
- Intel Celeron N5000: 5-6 concurrent sondes

### Breaking Changes
- **config.yaml flux242.script_path changed from relative to absolute path**
  - Old: `'./radiosonde/scripts/receivemultisonde.sh'`
  - New: `'/home/pi/radiosonde/scripts/receivemultisonde.sh'`
  - **Users upgrading must update their config.yaml!**

### Migration Notes
- Existing rtlsdr mode users: No changes required
- New flux242 users: Run `./install_flux242.sh` and configure absolute script_path
- See FLUX242_QUICKSTART.md for complete setup instructions

## v1.0.21 (2026-05-02) - rtl_fm Raw IQ Mode [WORKING!]

### USER VERIFIED: Successful Frame Decoding! 🎉
- **User tested command works: frames are being decoded!**
- **Sonde ID: DL2MF-11 successfully identified**
- **Pipeline: rtl_fm (raw mode, 48k) → rs41mod (IQ mode)**
- **No sox needed! Direct piping works perfectly**

### Working Configuration
```bash
rtl_fm -p 0 -M raw -s 48k -f 405.7M -g 0 -E dc | \
  rs41mod -v --ptu2 --IQ 0.0 - 48000 16
```

### Technical Changes
- `src/sdr/audio_pipeline.py`: rtl_fm in raw IQ mode
  - Command: `rtl_fm -p 0 -M raw -s 48k -f 405.7M -g 0 -E dc`
  - `-M raw`: Raw IQ mode (no FM demodulation)
  - `-s 48k`: 48 kHz sample rate (matches decoder requirement)
  - `-g 0`: Auto gain
  - `-E dc`: DC blocking filter
  - **Removed sox entirely - not needed!**
  
- `src/decoders/rs1729_decoder.py`: IQ decoding mode
  - Command: `rs41mod -v --ptu2 --IQ 0.0 - 48000 16`
  - `-v`: Verbose frame output
  - `--ptu2`: Decode PTU sensors
  - `--IQ 0.0`: IQ mode with threshold 0.0
  - `-`: Read from stdin
  - `48000 16`: 48 kHz sample rate, 16-bit depth

### Why This Works
1. **rtl_fm raw mode**: Outputs raw IQ samples (no demodulation)
2. **48 kHz directly**: No resampling needed, matches decoder
3. **Simple pipeline**: rtl_fm → decoder, no intermediate tools
4. **Decoder IQ mode**: Processes raw IQ samples directly
5. **User verified**: Real frames decoded, sonde ID extracted

### Output Example
```
[   20] (DL2MF-11)  Sat 2026-05-02 07:19:05.000  lat: 0.00000  lon: 0.00000  alt: 0.00
[   22] (DL2MF-11)  Sat 2026-05-02 07:19:07.000  lat: 0.00000  lon: 0.00000  alt: 0.00
```
*Note: Zeros indicate GPS not locked or test signal, but frames are decoding!*

## v1.0.20 (2026-05-02) - rtl_fm with Audio Decoding [DID NOT DECODE]

### Switch from rtl_sdr to rtl_fm
- **rtl_fm doesn't terminate unexpectedly** (verified by user testing)
- **Uses FM demodulation for GFSK signals**
- **Pipeline: rtl_fm (15k) → sox (48k WAV + lowpass) → rs41mod (audio)**

### Technical Changes
- `src/sdr/audio_pipeline.py`: Switch to rtl_fm
  - Command: `rtl_fm -f 405.45M -M fm -s 15k -g 40`
  - Sox resamples 15k → 48k with lowpass 2800 Hz filter
  - Outputs WAV format to decoder stdin
  
- `src/decoders/rs1729_decoder.py`: Audio decoding mode
  - Changed from `--iq0.3` (raw IQ) to `-vx --crc --ptu` (audio)
  - Decoder processes FM demodulated GFSK audio
  - Reads WAV format from stdin (48 kHz sample rate)

### Why rtl_fm Instead of rtl_sdr
1. **Stability**: rtl_fm doesn't terminate unexpectedly
2. **FM demodulation**: Built-in FM demod for GFSK signals
3. **Proven**: User verified rtl_fm runs continuously
4. **Simpler**: No raw IQ complexity, standard audio processing

### Pipeline Architecture
```
┌─────────┐  15 kHz s16   ┌──────────┐  48 kHz WAV    ┌─────────┐
│rtl_fm   │────stdout────►│sox       │────stdout──────►│rs41mod  │
│FM demod │               │resample  │   +lowpass     │-vx --crc│
└─────────┘               │+filter   │                └─────────┘
                          └──────────┘
```

### Sample Rate Requirements
- rtl_fm output: 15 kHz (s16 format)
- Sox resamples to: 48 kHz (WAV format) - required for RS41 decoder
- Lowpass filter: 2800 Hz (keeps GFSK signal at ~2.3 kHz BW)

## v1.0.19 (2026-05-01) - Raw Format Direct Piping Fix

### Critical Fix: Abandon WAV Format, Use Raw Format with Direct Piping
- **WAV format fundamentally incompatible with streaming pipes**
  - WAV headers require file size (impossible with streaming)
  - Sox cannot write complete WAV headers to FIFOs
  - Decoder crashes with "error: wav header" (exit code 255)
  - **Solution: Revert to direct stdout→stdin piping with raw format**

### Technical Changes
- `src/sdr/audio_pipeline.py`: **Removed named pipes, restored direct piping**
  - Removed os.mkfifo() and FIFO path handling
  - Sox outputs raw format to stdout: `-t raw -r 48000 -e unsigned -b 8 -c 2 -`
  - Returns sox.stdout file object instead of file path
  - Direct piping: rtl_sdr → sox → raw stdout → decoder stdin
  
- `src/decoders/rs1729_decoder.py`: **Changed decoder flags for raw stdin**
  - Removed `--IQ <file>` flag (WAV format)
  - Uses `--iq0.3 -` for raw unsigned 8-bit IQ from stdin
  - `--iq0.3`: threshold 0.3 for signal detection
  - `-`: read from stdin (not file)
  - Decoder reads directly from sox stdout pipe
  
- `src/decoders/decoder_manager.py`: **Updated to pass stdout file object**
  - Changed from `decoder.start(audio_source=fifo_path)`
  - Changed to `decoder.start(audio_stream=audio_stream)`
  - Passes sox.stdout for stdin piping

### Why This Fix Works
1. **No file headers needed** - Raw IQ is headerless binary data
2. **Direct piping proven** - Standard Unix pattern, used in SDR tools
3. **Decoder compatibility** - `--iq0.x` flags designed for stdin
4. **Streaming friendly** - No file size requirements

### What Went Wrong with v1.0.17-18
- v1.0.17: Tried WAV format via named pipes (FIFO)
- v1.0.18: Correct flags but WAV still broken
- **Root cause**: WAV format requires complete header with file size
- Sox writes data to FIFO but header is incomplete
- Decoder rejects: "error: wav header", exit code 255

### Why Raw Format is Correct
- rs41mod `--iq0.x` flags expect raw IQ stdin
- Format: unsigned 8-bit, 2 channels (I+Q), no headers
- Matches sox output format exactly
- Proven pattern in radiosonde decoding community

## v1.0.18 (2026-05-01) - Decoder Flags Fix for WAV Input [BROKEN]

### Critical Fix: Correct Decoder Flags
- **Changed from `--iq0` to `--IQ` for WAV file input**
  - `--iq0` is for raw IQ from rtl_fm stdin
  - `--IQ` is for IQ data from WAV files (including FIFOs)
  - Added demodulation flags: `--lpIQ`, `--dc`, `--lpbw 10`
  - Decoder now properly demodulates RS41 signals

### Why This Fix
1. **Decoder was running but not decoding frames**
2. **WAV files need different flags than raw stdin**
3. **`--IQ` requires demodulation flags for GFSK**
4. **Low-pass filtering essential for clean signal**

### Technical Changes
- `src/decoders/rs1729_decoder.py`: Updated start() method
  - Detects WAV file input vs stdin
  - Uses `--IQ <file> --lpIQ --dc --lpbw 10` for WAV
  - Keeps `--iq0` for legacy raw stdin (not currently used)

## v1.0.17 (2026-05-01) - Named Pipe WAV Format Streaming

### Critical Fix: WAV Format via Named Pipes
- **Decoder requires WAV headers, not raw pipes**
  - Test revealed: "error: wav header" when piping raw data
  - rs41mod and similar decoders expect WAV file format
  - Named pipes (FIFOs) allow streaming WAV format in real-time
  - Pattern proven in dxlAPRS and similar SDR projects

### Technical Changes
- `src/sdr/audio_pipeline.py`: Named pipe (FIFO) implementation
  - Creates named pipe in /tmp for each frequency
  - Sox outputs WAV format to named pipe: `sox ... -r 48000 /tmp/pipe.wav`
  - Decoder opens named pipe as WAV file: `rs41mod --iq0 /tmp/pipe.wav`
  - Pipeline: rtl_sdr (2.4M) → sox → WAV FIFO → decoder
- `src/decoders/decoder_manager.py`: Pass FIFO path instead of stdin
  - Uses decoder.start(fifo_path) instead of start_with_audio_stream()
  - Decoder opens named pipe as if it's a regular WAV file

### Why Named Pipes
1. **Decoder compatibility** - Works with tools expecting WAV files
2. **Real-time streaming** - Continuous data flow like a file
3. **Industry standard** - Used by dxlAPRS and similar projects
4. **Simple integration** - No changes needed to decoder binaries

## v1.0.16 (2026-05-01) - rtl_sdr + sox Resampling Solution

### Critical Fix: Use rtl_sdr with sox Resampling
- **rtl_sdr at 2.4 MSPS + sox resampling to 48 kHz**
  - rtl_sdr uses same sample rate as spectrum analyzer (from config.yaml)
  - sox handles the resampling from 2.4 MSPS down to 48 kHz
  - Avoids rtl_fm which exits with code 255 
  - Pipeline: rtl_sdr (2.4M) → sox → rs41mod (48k)

### Why This Solution
1. **rtl_fm exits with code 255** - User reported rtl_fm test failed
2. **rtl_sdr needs resampling** - Cannot do 48 kHz directly (min ~225 kHz)
3. **sox can resample IQ data** - Handles unsigned 8-bit stereo (IQ) resampling
4. **Uses config sample rate** - Matches spectrum analyzer (2.4 MSPS)

### Technical Changes
- `src/sdr/audio_pipeline.py`: Complete rewrite for rtl_sdr + sox pipeline
  - AudioPipeline now manages two subprocesses (rtl_sdr | sox)
  - rtl_sample_rate parameter from config (default 2400000)
  - sox resamples: 2.4 MSPS → 48 kHz
- `src/decoders/decoder_manager.py`: Pass rtl_sample_rate from config to audio pipeline
- Removed rtl_fm completely

### Pipeline Command
```bash
rtl_sdr -f [freq] -s 2400000 -g 40 - | \
  sox -t raw -r 2400000 -e unsigned -b 8 -c 2 - \
      -t raw -r 48000 -e unsigned -b 8 -c 2 - | \
  rs41mod -v --iq0 -
```

### Requirements
- rtl-sdr package (for rtl_sdr)
- sox package (for resampling)
- Install: `sudo apt-get install rtl-sdr sox`

## v1.0.13 (2026-05-01) - IQ Mode Filter Fix

### Critical Fix: Decoder Exit Code 255 Crash
- **Removed -F 9 (FIR filter) flag in raw IQ mode** (audio_pipeline.py)
  - FIR filters are designed for FM demodulation, not raw IQ samples
  - Filter was corrupting IQ data, causing decoder to crash after 3-4 seconds
  - Raw IQ samples now pass through unfiltered to decoder
  - Signal detection working, decoder invocation correct, but crashed due to malformed IQ data

### Technical Changes
- `src/sdr/audio_pipeline.py`: Removed '-F', '9' from rtl_fm command when using -M raw
- Added diagnostic scripts: check_decoder_flags.sh, debug_decoder_crash.sh, test_iq_modes.sh
- Fixed templates/ directory missing from package (web UI now functional)

## v1.0.12 (2026-05-01) - IQ Mode Fix

### Critical Fix: RS41 GFSK Decoding
- **Changed rtl_fm modulation mode from FM to RAW** (audio_pipeline.py)
  - RS41 radiosondes use GFSK (Gaussian Frequency Shift Keying) digital modulation
  - FM demodulation destroyed the FSK structure, causing decoder failure
  - Raw IQ mode preserves signal structure for proper GFSK decoding
  
- **Added --iq0 flag for RS41 decoder** (rs1729_decoder.py)
  - rs41mod requires IQ samples, not audio
  - Pipeline: rtl_fm -M raw → unsigned 8-bit IQ → rs41mod --iq0
  - Decoder now successfully decodes RS41 telemetry

### Technical Changes
- `src/sdr/audio_pipeline.py`: Changed '-M', 'fm' to '-M', 'raw'
- `src/sdr/audio_pipeline.py`: Removed '-E', 'dc' flag (not applicable to raw mode)
- `src/decoders/rs1729_decoder.py`: Added conditional --iq0 flag for RS41 sonde type

## v1.0.11 (2026-05-01) - Binary Mode Fix

### Fixed: Binary/Text Mode Corruption
- **Removed `universal_newlines=True` from subprocess.Popen** (rs1729_decoder.py)
  - rtl_fm outputs binary data, universal_newlines corrupted it
  - stdin now stays in binary mode for clean data passthrough
  - stdout/stderr manually decoded in _read_output()

- **Updated _read_output() to use readline()** (rs1729_decoder.py)
  - Binary mode requires explicit readline() instead of iteration
  - Added manual UTF-8 decoding with error handling

- **Removed bufsize=1 from Popen** (rs1729_decoder.py)
  - Line buffering incompatible with binary mode
  - Eliminated RuntimeWarning messages

## v1.0.9 (2026-05-01) - Invalid Flags Fix

### Fixed: Decoder Command Line Arguments
- **Removed invalid --json and --ecc flags** (rs1729_decoder.py)
  - rs41mod only supports: -v, -vx, -vv, -r/--raw, -i/--invert, --ths, --iq0,2,3
  - Changed decoder command from `rs41mod --json --ecc -` to `rs41mod -v -`
  - Fixed exit code 255 caused by invalid arguments

## v1.0.8 (2026-05-01) - Channel Management & Detection

### Fixed: Channel Leak (Maximum Channels Reached)
- **Added cleanup_dead_pipelines() calls** (decoder_manager.py)
  - Zombie rtl_fm processes were blocking all decoder slots
  - Now cleans up dead processes before starting new decoders
  - Called in _check_decoder_health() and _start_decoder_for_signal()

### Enhanced: RS41 Detection for European Band
- **Improved _identify_sonde_type()** (decoder_manager.py)
  - Better detection for 400-406 MHz European RS41 band
  - Prefers RS41 for narrow bandwidth (< 9 kHz) in this range
  - Reduced false positives for iMet/RS92 identification

### Configuration Changes
- **Changed max_concurrent from 8 to 1** (config.yaml)
  - Critical for single RTL-SDR setups
  - Prevents multiple decoders from attempting simultaneous RTL-SDR access

### Installation Improvements  
- **Fixed requirements.txt detection** (install.sh)
  - Auto-detects requirements.txt in multiple locations
  - Handles tarball extraction variations
  - Searches: current dir, OpenWXSDR subdir, parent dir

## v1.0.0 (2026-04-30) - Initial Release

### Features
- Multi-sonde decoder support via rs1729 decoder suite
  - RS41, RS92, DFM09, M10, M20, iMet, LMS6, MRZ
- RTL-SDR spectrum analyzer with automatic signal detection
- Multi-channel audio pipeline management
- UDP telemetry output (SondeHub compatible)
- Web dashboard for monitoring
- Systemd service integration

### Components
- `src/sdr/rtlsdr_analyzer.py`: RTL-SDR spectrum scanning and signal detection
- `src/sdr/audio_pipeline.py`: rtl_fm process management
- `src/decoders/decoder_manager.py`: Decoder orchestration and lifecycle
- `src/decoders/rs1729_decoder.py`: RS1729 decoder wrapper
- `src/output/udp_output.py`: UDP telemetry transmission
- `src/webui/web_server.py`: Flask-based web interface

### Dependencies
- Python 3.8+
- rtl-sdr (built from source)
- pyrtlsdr
- rs1729 decoder suite (auto-downloaded)
- Flask (web UI)
- NumPy, SciPy (signal processing)

---

## Migration Notes

### From v1.0.8 to v1.0.12
**Breaking change:** Decoder command line structure changed significantly.

Users upgrading from v1.0.8 or earlier **must**:
1. Update `src/sdr/audio_pipeline.py` (rtl_fm -M raw)
2. Update `src/decoders/rs1729_decoder.py` (--iq0 flag, binary mode)
3. Remove any custom --raw flags manually added
4. Restart service: `sudo systemctl restart openwxsdr`

### From v1.0.0 to v1.0.8
Users upgrading from v1.0.0 **must**:
1. Update config.yaml: Set `max_concurrent: 1` for single RTL-SDR
2. Update decoder_manager.py for cleanup fixes
3. Rebuild rs1729 decoders if using outdated binaries
4. Restart service

---

## Known Issues

### v1.0.12
- Other sonde types (RS92, DFM, M10, M20) may also need --iq0 flag (untested)
- Web UI lacks real-time telemetry display (planned for v1.1)
- No automatic frequency tuning for Doppler shift

### All Versions
- Single RTL-SDR device only (no multi-device support)
- No support for direct sampling mode
- Requires manual rs1729 decoder build on Pi

---

## Roadmap

### v1.1 (Planned)
- Real-time web-based telemetry map
- Automatic Doppler correction
- Multiple RTL-SDR device support
- Enhanced signal detection algorithms

### v1.2 (Planned)
- SondeHub uploader integration
- Automatic chase prediction
- Email/Telegram notifications
- Landing prediction

---

For full documentation, see README.md
