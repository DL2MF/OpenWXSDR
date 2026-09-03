# OpenWX <img src="https://cdn.jsdelivr.net/npm/bootstrap-icons/icons/radar.svg" width="24"> SDR - Streamlined Radiosonde Decoder

A lightweight, efficient radiosonde decoder for Raspberry Pi and X86_64 gateways, designed to work with RTL-SDR, Airspy and KA9Q radio receivers. 
Using the excellent rs1729/RS decoders embedded into this framework.

<img width="820" height="558" alt="grafik" src="https://github.com/user-attachments/assets/3a453239-0042-430c-a2f8-44a2a7420c71" />


## Current Version:** 1.0.62 (August 09, 2026):

⚠ <u>Important notice:</u> From version 1.0.60 and higher upload of radiosonde telemetry to sondehub.org is now available for this sondetypes:
- RS41  (from v1.0.60)
- DFM06 (from v1.0.60)
- DFM09 (from v1.0.60)
- DFM17 (from v1.0.60)

- M10   (from v1.0.61)
- M20   (from v1.0.61)

- RS92  (from v1.0.62)

The RS92 data validation was successfully granted, it may take some time until sondehub makes the infrastructure update.

## ✨ What's New in v1.0.62

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

## RS92 GPS ephemeris

- Opt‑in downloader (`rs92.ephemeris_download`) fetches the current GPS‑day
  broadcast ephemeris into `data/rs92` and feeds `rs92mod -e` for a position
  solve. Default source is **BKG** (Germany, HTTPS, no login) RINEX3 mixed‑nav
  `BRDC00WRD_R_<yyyy><doy>0000_01D_MN.rnx.gz`. Runs in the background with
  retry/backoff (recovers a network‑not‑ready‑at‑boot failure) and re‑checks
  hourly for the UTC‑midnight day rollover. A **System Health "Ephemeris"** row
  shows ready/pending (only when enabled).

## 🔧 New Setup check tool integrated

  <img width="1629" height="966" alt="grafik" src="https://github.com/user-attachments/assets/80f564de-f1c5-4f95-a113-b5c0aa2f0164" />

Inline check of the installation and configuration of optional modules.

See changelog in the release section for all details.

#### 🔧 New configuration utility

<img width="765" height="481" alt="Screenshot 2026-08-03 135902" src="https://github.com/user-attachments/assets/4e0be656-368a-4b75-a62e-72e3519a64a3" />

For quickstart and easy setup from scratch the openwxsdr_config script is available now. It also contains a config backup and configuration check.

#### 🖥️ Integrated gateway receiver and radiosonde statistics 

<img width="1239" height="909" alt="grafik" src="https://github.com/user-attachments/assets/fcabd778-b537-44b6-b11a-f0c821970da1" />

The receiver statistics gives an comprehensive overview of your radiosonde detection and decoding performance


<img width="697" height="305" alt="Screenshot 2026-07-16 215225" src="https://github.com/user-attachments/assets/3d524eb0-6e36-42fc-8646-f227312415da" />

Received radiosonde frequencies are shown at a glance, you clearly see the most frequent radiosonde frequencies used in your area.


<img width="1285" height="891" alt="grafik" src="https://github.com/user-attachments/assets/6424c78a-b972-4093-8025-33d48157d42f" />

Telemetry and sensor data of each received radiosonde is available in the sonde statistics menu.


<img width="789" height="483" alt="Screenshot 2026-08-01 080802" src="https://github.com/user-attachments/assets/9a621bd8-2ceb-4033-9f40-b1d63b5057fd" />

In band sweep mode a single SDR checks the whole radiosonde spectrum and writes detected frequencies into Frequency Repository. Instantly manual decoder start is available from the repo manager for detected new frequencies.




## Features

- 🎯 **Automatic Signal Detection**: Scans spectrum and automatically detects radiosonde signals
- 🔧 **Multiple Sonde Types**: Supports RS41, RS92, DFM, M10, M20, iMet, LMS6, MRZ
- 📡 **Multi SDR Support**: Works with up to 4 RTL-SDR or Airspy R2, Airspy Mini USB dongles
- 🎛️ **Virtual Receivers**: Configurable parallel decoding of multiple sondes with KA9Q radio
- 🗺️ **Web Interface**: Clean Leaflet-based map showing real-time flight paths
- 📤 **OpenWX Integration**: MQTT & UDP JSON output for seamless data upload
- 📤 **Sondehub Integration**: seamless data upload to sondehub.org
- ⚡ **Lightweight**: Minimal overhead compared to several other decoder solutions
- 🔧 **One-step installer**: Easy installtion of all required packages 

## Hardware Requirements

- Raspberry Pi 4 / 5 / 400 / 500 (8GB recommended) or Intel x86_64 client
- Debian 13 "Trixie" or Raspberry Pi OS (64 Bit)
- RTL-SDR, Airspy R2, Airspy Mini dongle or KA9Q-compatible SDR
- Antenna tuned for 400-406 MHz
- Selective 400 MHz LNA
- SAW filter recommended

## Architecture

```
┌─────────────────┐
│  RTL-SDR/Airspy │
│   KA9Q Radio    │
└────────┬────────┘
         │
    ┌────▼──────────────┐
    │ Spectrum Analyzer │
    │ Signal Detector   │
    └────┬──────────────┘
         │
    ┌────▼─────────────────┐
    │   Decoder Manager    │
    │ (rs1729/RS decoders) │
    └────┬─────────────────┘
         │
    ┌────▼────────┬──────────┬─────────┐
    │             │          │         │
┌───▼────┐   ┌────▼─────┐  ┌─▼──────┐  │
│ Web UI │   │ UDP JSON │  │  Log   │  │
│ Flask  │   │ OpenWX   │  │  File  │  │
└────────┘   └──────────┘  └────────┘  │
                                       │ 
                                   [sondehub]
```

## Installation

### 1a. One-Step-Installer

OpenWXSDR offers an easy-to-use One-Step-Installer to install the necessary libraries, packages and OpenWXSDR package.

See the wiki for detailed installation instructions: https://github.com/DL2MF/OpenWXSDR/wiki/Installation

If you prefer manual installation of the package, continue below.

### 1b. Install System Dependencies

```bash
sudo apt-get update
sudo apt-get install -y git build-essential cmake pkg-config \
    libusb-1.0-0-dev python3-pip python3-venv rtl-sdr
```

### 2. Clone and Set Up rs1729/RS Decoders

```bash
mkdir -p decoders
cd decoders
git clone https://github.com/rs1729/RS.git rs1729
cd rs1729

# Build the decoders
cd demod && make
cd ../rs41 && make
cd ../rs92 && make
cd ../dfm && make
cd ../m10 && make
cd ../imet && make
cd ../lms6 && make
cd ..
```

### 3. Install OpenWXSDR

```bash
cd ~/!Develop_OpenWXSDR
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configure

Edit `config.yaml` to match your setup:
- Set SDR type and parameters
- Configure frequency ranges for your region
- Set receiver specific settings (span, gain, fft, treshold)
- Configure OpenWX UDP and external output destinations

## Configuration

Key configuration options in `config.yaml`:

- **sdr.type**: Choose 'rtlsdr', 'airspy', 'flux242' or 'ka9q'
- **receivers.max_concurrent**: Number of parallel decoders (1-8 recommended)
- **detection.freq_ranges**: Frequency ranges to scan
- **output.udp**: Configure OpenWX server destination


## UDP JSON Format

Data sent to OpenWX server:

```json
{
  "software_name": "OpenWXSDR",
  "software_version": "1.0.45",
  "uploader_callsign": "<your-call>",
  "time_received": "2026-04-30T12:34:56.789Z",
  "type": "RS41",
  "frame": 12345,
  "id": "T1234567",
  "datetime": "2026-04-30T12:34:56.000Z",
  "lat": 51.5074,
  "lon": -0.1278,
  "alt": 15420.5,
  "vel_h": 12.3,
  "vel_v": 5.2,
  "heading": 285.5,
  "temp": -45.2,
  "humidity": 25.5,
  "pressure": 150.25,
  "frequency": 402.700,
  "sats": 8,  
  "snr": 25.5
}
```

## Supported Radiosonde Types

| Type | Decoder   | Notes                   |
|------|-----------|-------------------------|
| RS41 | rs41mod   | Vaisala RS41-SG/SGP/SGM |
| RS92 | rs92mod   | Vaisala RS92-SGP/NGP    |
| DFM  | dfm09mod  | Graw DFM06/09/17        |
| M10  | m10mod    | Meteomodem M10          |
| M20  | m20mod    | Meteomodem M20          |
| iMet | imet54mod | InterMet iMet-54        |
| LMS6 | lms6mod   | Lockheed Martin LMS6    |
| MRZ  | mrzmod    | Meteo-Radiy MRZ         |


### 5. Run

```bash
python3 openwxsdr.py
```

Access the web UI at `http://raspberry-pi-ip:5000` 

Many configuration settings are also available in the WebUI and can be changed during operation:
- SNR treshold
- Scan interval
- Callsign and SSID
- SDR-Type and device settings
 - Center Frequency
 - Sample Rate
 - Gain
 - PPM correction
- MQTT and upload
 - Server IP / hostname
 - Port
 - User credentials


## Local WebUI on your device 

The local web UI is available at `http://yourdevice-ip:5000`

<img width="1656" height="1119" alt="Screenshot 2026-08-05 203845" src="https://github.com/user-attachments/assets/4e1a2eb4-8f7b-482a-aad0-0082870409ce" />


For each configured receiver the frequency spectrum is available:

<img width="1361" height="824" alt="Screenshot 2026-05-18 132137" src="https://github.com/user-attachments/assets/157818ec-ce90-4e98-ba48-08f29d6dd2c5" />


## Telemetry data upload to external OpenWX.de

If enabled in config.yaml your device will upload radiosonde telemetry and sensor ptu to OpenWX.de `http://map.openwx.de`:

Upload to OpenWX.de is preferable via MQTT configurable. Generate your API-key in your OpenWX-Account and configure your credentials in config.yaml.

```
  # MQTT upload to OpenWX.de broker
  mqtt:
    enabled: true
    server: '<server-ip>'                   # MQTT broker hostname or IP
    port: 1883                              # MQTT broker port (1883 = plain, 8883 = TLS)
    username: '<username>'                  # MQTT username (leave empty if not required)
    password: '<password>'                  # MQTT password (leave empty if not required)
    topic_prefix: 'OPENWXSDR/<your-call>/'  # MQTT topic prefix (e.g. OPENWXSDR/<callsign>/<serial>)
    client_id: '<your-call>-15'             # MQTT client ID
    keepalive: 60                           # MQTT keepalive in seconds
    connect_timeout: 10                     # Wait this many seconds for initial CONNACK
    tls_enabled: true                       # Set false for username/password brokers on plain TCP (usually port 1883)
    tls_insecure: true                      # Accept brokers by IP/self-signed cert without CA validation
    tls_ca_certs: ''                        # Optional CA bundle path for strict TLS validation
    transport: 'tcp'                        # MQTT transport
```


<img width="1529" height="879" alt="grafik" src="https://github.com/user-attachments/assets/44f5b697-20b4-4619-8ef0-88396e06eaf9" />



## Telemetry data upload to external sondehub.org

If enabled in config.yaml your device will upload radiosonde telemetry and sensor ptu to sondehub.org:

```
# ============================================================
# SondeHub Upload
# ============================================================
sondehub:
  enabled: true                                            # Since V1.0.60 upload to sondehub.org is granted
  queue_mode: true                                         # false = direct upload mode, true = queued batch upload mode
  queue_batch_max: 50                                      # Max telemetry objects uploaded per queued request (10-fast 50-robust)
  queue_max_size: 1000                                     # Max queued  objects before oldest/new drops may occur (200-fast 1000-robust)
  upload_url: 'https://api.v2.sondehub.org/sondes/telemetry'
  listeners_url: 'https://api.v2.sondehub.org/listeners'   # Listener metadata endpoint
  station_id: '<your-call>->ssid>'                         # Station ID (use callsign/SSID style)
  uploader_callsign: '<your-call>'                         # Receiver callsign shown in SondeHub
  uploader_antenna: '1/4 wave UHF vertical'                # Receiver antenna
  uploader_radio: 'Airspy Mini + rs1729'                   # Optional receiver hardware/radio description
  contact_email: '<mail>@domain.com'                       # Optional contact email for listener metadata
  uploader_lat: 52.00                                      # Must match station.lat above
  uploader_lon: 10.00                                      # Must match station.lon above
  uploader_alt: 100                                        # Must match station.alt above
  upload_rate_s: 10                                        # Upload interval in seconds (1-5 for queue mode -10 recommended for single tracking)
  listener_upload_interval_s: 21600                        # Listener metadata upload interval in seconds (6h / 4 times/day recommend)
```


<img width="1376" height="943" alt="Screenshot 2026-05-19 152207" src="https://github.com/user-attachments/assets/1be3ef61-1a51-4f64-9e72-7c77dae0eca6" />




# Grafana dashboard from Sondehub will show your receiver in the statistics:

<img width="796" height="492" alt="Screenshot 2026-08-02 142152" src="https://github.com/user-attachments/assets/68ac0a4d-b4cd-4813-9b51-f28c71b1a5e8" />


## License

GNU GPLv3 License - See [(https://github.com/DL2MF/OpenWXSDR/blob/main/LICENSE)](LICENSE) file

## Credits

- rs1729/RS decoders: https://github.com/rs1729/RS
- Inspired by Project Horus radiosonde_auto_rx: https://github.com/projecthorus/radiosonde_auto_rx/
- Built for the OpenWX.de network community

## Support

[![paypal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/donate?token=8zPp8MT2DKdshzvmRxDi6yJhCdXGJSb_wIulhbD73TTYGuveGIrCGbGb0jhV9m4Tpj3D2ijR2JXltlGC)

For issues and questions, please open an issue on GitHub.
