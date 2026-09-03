# OpenWXSDR — Update V1.0.61 → V1.0.62

A large release focused on **decode reliability**, the **Airspy channelizer**, a
substantially expanded **web UI / map**, and new **RS92 GPS‑ephemeris** support.

Update is safe to install over an existing V1.0.61 gateway: it replaces
application files but never your `config.yaml` or the `data/` and `logs/`
folders. Review `config.yaml.example` for the new options listed below.

---

## Decoding & detection

- **Pinned decoder source.** `scripts/install_softchain.sh` now clones
  `radiosonde_auto_rx` at a fixed tag (`v1.8.2`, override with `AUTORX_REF`), so
  every gateway builds *identical* `dft_detect` / `fsk_demod` / `iq_dec` / `*mod`
  binaries — no more per‑install behaviour drift.
- **RS41 bandwidth fast‑path is now OFF by default.** Every candidate is
  classified purely by `dft_detect` correlation (auto_rx's method), which removes
  the bandwidth‑guess that let a narrow‑measuring DFM start as RS41. Re‑enable per
  gateway with `detection.rs41_fastpath: true` (≈15 s faster RS41 start).
- **dft_detect classification hardened:** M10/M20 negative‑correlation now compared
  by magnitude (fixes M10/M20 being mis‑typed), MRZ + iMet variants + build
  suffixes (RS41SG) added to the parser, and unreachable "no type passed
  threshold" logging fixed (now reports each type's score for calibration).
  Thresholds are config‑driven via `detection.dft_thresholds`.
- **DFM decode fix:** `--dist/--ptu/-ID` are gated on `--help` only (field
  `dfm09mod` rejects them → exit 255 → dead DFM). M10/M20 flags passed
  unconditionally. Soft‑chain params aligned with auto_rx (`--mask 5000
  --nsym=300 -p 5`, DFM `-i`). `datetime.utcnow()` deprecation removed.
- **iq_dec inline DC removal** (auto_rx method) available in both the `--IQ` and
  soft chains via `decoders.iq_dc_block`.

## Airspy

- **Ghost / "birdie" reject.** The scanner now rejects strong‑but‑narrow peaks
  (CW/spurs) that were starting phantom decoders — configurable via
  `sdr.airspy.birdie_reject_snr_db` / `birdie_reject_bw_hz` / `scan_min_bw_hz`.
- **Channelizer decode fixed (major).** The Python resampler ran `resample_poly`
  *statelessly per chunk*, re‑initialising its anti‑alias filter every ~2.5 ms and
  corrupting the FSK, so the channelizer never decoded (legacy worked because
  `sox` resamples continuously). It's now a stateful **overlap‑save** resampler —
  verified numerically at 0.0 error vs a continuous resample (was 59 %).
- **Airspy classification** can route through `dft_detect` in legacy mode
  (`detection.airspy_use_dft_detect`); skipped in channelizer mode by design.
- **Diagnostics:** `sdr.airspy.debug_iq_dump` tees the exact decoder‑input IQ to
  `data/logs/ch_dump_<freq>.raw` for offline `rs41mod` testing; a dropped‑block
  counter warns on real‑time overload.

## Web UI & map

- **System Statistics tiles:** Active‑Sondes 3‑dot menu (Active / Total); Frames
  menu (Total / Today's, new UTC‑day counter); CPU/RAM Live / Graph (60‑min
  chart, full‑row when graphed).
- **Sonde tiles:** Standard / Extended 3‑dot menu (Extended row shows Distance /
  Elevation / Battery, height‑aware); M10/M20/DFM show **battery** instead of SNR;
  header time as `…Z`; correct **DFM subtype** badges (DFM06/09/17) resolved from
  the log with correct colours.
- **Own‑gateway map marker.** Click for identity/hardware + the sondes received in
  the last `sonde_retention_time` window, each with height‑aware **slant distance,
  elevation and course**. Right‑click → **Landings** (25/50/100/150 km range
  rings + last‑position markers) / **Heatmap** (last positions) / **Clear**, with a
  "Loading data …" dialog.
- **Landings:** live‑styled popups (id, date/time, position, altitude, v‑vel,
  frequency, distance); right‑click a landing → **Show / Clear sonde path**
  (polyline loaded from the logfile).
- **Sonde Statistics modal:** **server‑side LTTB decimation** (huge speed‑up on
  long flights, `?max_points`); new **Elevation** chart with a second **Distance**
  line on a right axis; tiles reordered (Altitude|Elevation, HVel|VVel,
  Sats|Battery, SNR|RSSI); sonde identity in the title and maximized headers;
  fixed missing frequency when opened via right‑click on an active tile.
- **Gateway details** modal shows Hostname/IP, version, memory/disk.
- Fixed a stuck "Loading data …" modal backdrop (Bootstrap show/hide race), and
  M10/M20 dashed‑serial (`210‑2‑11234‑…`) logfile loading being truncated to
  `210`.

## RS92 GPS ephemeris (new)

- Opt‑in downloader (`rs92.ephemeris_download`) fetches the current GPS‑day
  broadcast ephemeris into `data/rs92` and feeds `rs92mod -e` for a position
  solve. Default source is **BKG** (Germany, HTTPS, no login) RINEX3 mixed‑nav
  `BRDC00WRD_R_<yyyy><doy>0000_01D_MN.rnx.gz`. Runs in the background with
  retry/backoff (recovers a network‑not‑ready‑at‑boot failure) and re‑checks
  hourly for the UTC‑midnight day rollover. A **System Health "Ephemeris"** row
  shows ready/pending (only when enabled).

## Other

- **RTL per‑frame RSSI/SNR:** the metrics pump is now cheap (throttled ~10 Hz) and
  stall‑safe (enlarged pipe buffers to avoid the old frame‑yield regression).
  Enable with `decoders.live_signal_metrics: true`. Airspy already provides
  per‑frame metrics.
- **USB‑reset auto‑recovery** for wedged RTL dongles (opt‑in
  `recovery.usb_reset_on_wedge`).
- **rtl_power full‑band scan backend** (Phase 1) for single‑SDR full‑band
  detection (`detection.scanner.backend: rtl_power`), with SIGINT flush and
  parse‑on‑stop.
- `/api/gateway`, `/api/landings`, per‑frame `battery`/`elevation`/`distance` in
  the history endpoints, `today_frames`.
- Version → **1.0.62**.

## New config keys (see `config.yaml.example`)

`detection.rs41_fastpath`, `detection.dft_thresholds`, `detection.scanner.*`,
`detection.airspy_use_dft_detect`; `sdr.airspy.scan_min_bw_hz`,
`birdie_reject_snr_db`, `birdie_reject_bw_hz`, `debug_iq_dump`;
`decoders.iq_dc_block`, `decoders.live_signal_metrics`;
`recovery.*`; `rs92.ephemeris_download`, `ephemeris_url`, `ephemeris_dir`.

> If your decoders were rebuilt, re‑run `scripts/install_softchain.sh` so the
> pinned `dft_detect`/`fsk_demod`/`iq_dec` binaries match this release.
