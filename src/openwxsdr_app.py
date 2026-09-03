"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : openwxsdr_app.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; version 2 of the License.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# -----------------------------------------------------------------------------
#  Description
# -----------------------------------------------------------------------------
#
#  Main application entry point and top-level orchestrator for OpenWX.
#
#  The OpenWXSDR class initializes, wires together, and manages the lifecycle
#  of all subsystems based on the active SDR backend type configured in
#  config.yaml. Supported backends: rtlsdr, airspy, ka9q, flux242.
#
#  Component lifecycle:
#    initialize() ? start() ? _main_loop() / _flux242_main_loop() ? stop()
#
#  Subsystems managed:
#    SDR backends    : RTLSDRDeviceManager, AirspyReceiver, KA9QReceiver,
#                      Flux242Receiver
#    Decoder backend : DecoderManager (rs1729)
#    Output plugins  : UDPOutput, MQTTOutput, HttpOutput,
#                      SondeHubOutput / SondeHubQueueOutput
#    Web interface   : WebUI (Flask + Leaflet map)
#
# =============================================================================
"""

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING


class FrequencyRepository:
    """Per-session log of radiosonde band activity → logs/sdrfreq_<ts>.log.

    Two row types (per the "Both" design):
      * detected  — a sonde-like peak the scanner found (freq/SNR, no serial)
      * confirmed — a decode produced real telemetry (freq/type/serial/SNR/RSSI)
    Deduped so the file stays compact: one 'detected' row per 10 kHz channel and
    one 'confirmed' row per sonde serial per session. The web UI reads the file
    back (glob newest sdrfreq_*.log) to show the repository modal.
    """

    HEADER = "datetime_utc,event,frequency_mhz,type,serial,snr_db,rssi_dbm,alt_m,device\n"

    def __init__(self, logdir: str = "logs"):
        self.logger = logging.getLogger("FreqRepo")
        self._logdir = logdir
        self._lock = threading.Lock()
        self._path: Optional[str] = None
        self._detected_keys = set()       # 10 kHz channel buckets
        self._confirmed_serials = set()
        self._filename = f"sdrfreq_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    @staticmethod
    def _key(freq_hz: float) -> int:
        return round(freq_hz / 10_000.0)   # 10 kHz channel bucket

    def _row(self, event, freq_hz, sonde_type="", serial="", snr=None, rssi=None, alt=None, device=""):
        def num(v, spec):
            try:
                return spec.format(v) if (v is not None and v != 0.0) else ""
            except Exception:
                return ""
        dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (f"{dt},{event},{freq_hz/1e6:.3f},{sonde_type or ''},{serial or ''},"
                f"{num(snr, '{:.1f}')},{num(rssi, '{:.1f}')},{num(alt, '{:.0f}')},{device or ''}\n")

    def _append(self, line: str):
        try:
            with self._lock:
                if self._path is None:
                    os.makedirs(self._logdir, exist_ok=True)
                    self._path = os.path.join(self._logdir, self._filename)
                    with open(self._path, "a", encoding="utf-8") as f:
                        f.write("# OpenWXSDR frequency repository (session)\n")
                        f.write(self.HEADER)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception as e:
            self.logger.debug(f"freq repo write failed: {e}")

    def record_detected(self, freq_hz: float, snr=None, device=""):
        k = self._key(freq_hz)
        with self._lock:
            if k in self._detected_keys:
                return
            self._detected_keys.add(k)
        self._append(self._row("detected", freq_hz, snr=snr, device=device))
        self.logger.info(f"Freq repo: detected {freq_hz/1e6:.3f} MHz")

    def record_confirmed(self, telemetry):
        serial = getattr(telemetry, "serial", "") or ""
        if not serial:
            return
        with self._lock:
            if serial in self._confirmed_serials:
                return
            self._confirmed_serials.add(serial)
        alt = telemetry.position.altitude if getattr(telemetry, "position", None) else None
        self._append(self._row(
            "confirmed", telemetry.frequency, sonde_type=telemetry.sonde_type, serial=serial,
            snr=getattr(telemetry, "snr", None), rssi=getattr(telemetry, "rssi", None),
            alt=alt, device=getattr(telemetry, "receiver_device", "") or ""))
        self.logger.info(
            f"Freq repo: confirmed {telemetry.sonde_type} {serial} @ "
            f"{telemetry.frequency/1e6:.3f} MHz")

from .sdr.rtlsdr_analyzer import SpectrumAnalyzer
from .sdr.ka9q_receiver import KA9QReceiver
from .sdr.flux242_receiver import Flux242Receiver, Flux242Config
from .sdr.device_manager import RTLSDRDeviceManager
from .decoders.decoder_manager import DecoderManager
from .decoders.models import SondeTelemetry
from .output.udp_output import UDPOutput
from .output.mqtt_output import MQTTOutput
from .output.http_output import HttpOutput
from .output.sondehub_output import SondeHubOutput
from .output.sondehub_queue import SondeHubQueueOutput
from .output.channelizer_status import ChannelizerStatusOutput
from .telemetry.telemetry import InstallPing
from .webui.web_server import WebUI

if TYPE_CHECKING:
    from .sdr.airspy_receiver import AirspyReceiver


class OpenWXSDR:
    """Main application coordinator"""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('OpenWXSDR')
        self.running = False
        
        # Components
        self.spectrum_analyzer: Optional[SpectrumAnalyzer] = None
        self.ka9q_receiver: Optional[KA9QReceiver] = None
        self.flux242_receiver: Optional[Flux242Receiver] = None
        self.decoder_manager: Optional[DecoderManager] = None
        self.device_manager: Optional[RTLSDRDeviceManager] = None
        self.airspy_receiver: Optional['AirspyReceiver'] = None
        self.udp_output: Optional[UDPOutput] = None
        self.mqtt_output: Optional[MQTTOutput] = None
        self.http_output: Optional[HttpOutput] = None
        self.sondehub_output: Optional[object] = None
        self.channelizer_status_output: Optional[ChannelizerStatusOutput] = None
        self.webui: Optional[WebUI] = None
        self.install_ping: Optional[InstallPing] = None

        # Per-session repository of detected + confirmed radiosonde frequencies
        # (logs/sdrfreq_<ts>.log). Populated by the scanner (detected) and the
        # telemetry handlers (confirmed); read back by the web UI modal.
        self.frequency_repository = FrequencyRepository()

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def initialize(self) -> bool:
        """Initialize all components"""
        self.logger.info("Initializing OpenWXSDR...")
        
        try:
            # Initialize channelizer status output early (needed by device_manager)
            self.logger.info("Initializing channelizer status output...")
            self.channelizer_status_output = ChannelizerStatusOutput(self.config)

            # RS92 GPS broadcast-ephemeris downloader (opt-in via
            # rs92.ephemeris_download). Fetches the current GPS-day RINEX file
            # into data/rs92 in the background so rs92mod can use it (-e).
            from .sdr.ephemeris import configure as _configure_rs92_ephemeris
            self.rs92_ephemeris = _configure_rs92_ephemeris(self.config)
            if self.rs92_ephemeris.enabled:
                self.logger.info(
                    "RS92 ephemeris download ENABLED — fetching current GPS-day "
                    f"file into {self.rs92_ephemeris.out_dir} (url: "
                    f"{self.rs92_ephemeris.url_template}) ...")
                self.rs92_ephemeris.start_background_refresh()
            else:
                # Logged so a mis-nested config is obvious. Must be nested YAML:
                #   rs92:
                #     ephemeris_download: true
                # NOT a dotted key 'rs92.ephemeris_download: true'.
                self.logger.info(
                    "RS92 ephemeris download disabled "
                    f"(config has rs92 section: {'rs92' in self.config})")

            # Initialize SDR
            sdr_type = self.config['sdr']['type']
            
            if sdr_type == 'rtlsdr':
                self.logger.info("Initializing RTL-SDR device manager...")
                self.device_manager = RTLSDRDeviceManager(
                    self.config, self._handle_telemetry, self.channelizer_status_output,
                    frequency_repository=self.frequency_repository
                )
                if not self.device_manager.initialize():
                    self.logger.error("Failed to initialize RTL-SDR device manager")
                    return False
            
            elif sdr_type == 'ka9q':
                self.logger.info("Initializing KA9Q receiver...")
                self.ka9q_receiver = KA9QReceiver(self.config, self._handle_ka9q_telemetry)
                if not self.ka9q_receiver.initialize():
                    self.logger.error("Failed to initialize KA9Q receiver")
                    return False
            
            elif sdr_type == 'flux242':
                self.logger.info("Initializing Flux242 receiver (receivemultisonde.sh)...")
                # For flux242, we don't need decoder_manager or spectrum_analyzer
                # The flux242 script handles everything internally
                flux_cfg = self.config['sdr']['flux242']
                flux242_config = Flux242Config(
                    center_freq=flux_cfg.get('center_freq', 403405000),
                    sample_rate=flux_cfg.get('sample_rate', 2400000),
                    gain=flux_cfg.get('gain', 40),
                    ppm_error=flux_cfg.get('ppm_error', 0),
                    threshold=flux_cfg.get('threshold', 4),
                    udp_port=flux_cfg.get('udp_port', 5678),
                    power_port=flux_cfg.get('power_port', 5676),
                    debug_port=flux_cfg.get('debug_port', 5675),
                    script_path=flux_cfg.get('script_path', './radiosonde/scripts/receivemultisonde.sh')
                )
                self.flux242_receiver = Flux242Receiver(flux242_config, self._handle_flux242_telemetry)
                
                # Skip decoder manager for flux242 mode
                self.decoder_manager = None
            
            elif sdr_type == 'airspy':
                self.logger.info("Initializing Airspy receiver...")
                try:
                    from .sdr.airspy_receiver import AirspyReceiver
                except Exception as e:
                    self.logger.error(f"Failed to import AirspyReceiver: {e}", exc_info=True)
                    return False
                self.airspy_receiver = AirspyReceiver(
                    self.config, self._handle_telemetry
                )
                if not self.airspy_receiver.initialize():
                    self.logger.error("Failed to initialize Airspy receiver")
                    return False

            else:
                self.logger.error(f"Unknown SDR type: {sdr_type}")
                return False
            
            # Initialize decoder manager (only for ka9q mode; rtlsdr uses DeviceManager)
            if sdr_type == 'ka9q':
                self.logger.info("Initializing decoder manager...")
                self.decoder_manager = DecoderManager(
                    self.config,
                    self._handle_telemetry,
                    spectrum_analyzer=None,
                    ka9q_receiver=self.ka9q_receiver  # Pass KA9Q receiver for status queries
                )
            
            # Initialize output
            self.logger.info("Initializing UDP output...")
            self.udp_output = UDPOutput(self.config)

            # Initialize MQTT output (optional, enabled via openwx.mqtt.enabled)
            self.logger.info("Initializing MQTT output...")
            self.mqtt_output = MQTTOutput(self.config)

            # Initialize HTTP output (optional, enabled via openwx.http.enabled)
            self.logger.info("Initializing HTTP output...")
            self.http_output = HttpOutput(self.config)

            # Initialize SondeHub output (optional, enabled via sondehub.enabled)
            self.logger.info("Initializing SondeHub output...")
            sondehub_cfg = self.config.get('sondehub', {})
            if bool(sondehub_cfg.get('queue_mode', False)):
                self.logger.info("SondeHub uploader mode: queue")
                self.sondehub_output = SondeHubQueueOutput(self.config)
            else:
                self.logger.info("SondeHub uploader mode: direct")
                self.sondehub_output = SondeHubOutput(self.config)
            
            # Anonymous, opt-out install counter (see telemetry.py docstring
            # for exactly what is/isn't sent — no callsign/location/credentials)
            self.logger.info("Initializing anonymous install counter...")
            self.install_ping = InstallPing(self.config)

            # Initialize web UI
            self.logger.info("Initializing web UI...")
            self.webui = WebUI(self.config)
            
            # Set component references for health monitoring
            if self.webui:
                self.webui.set_components(
                    spectrum_analyzer=None,
                    decoder_manager=(
                        self.airspy_receiver or
                        self.device_manager or
                        self.decoder_manager
                    ),
                    flux242_receiver=self.flux242_receiver,
                    ka9q_receiver=self.ka9q_receiver,
                    mqtt_output=self.mqtt_output,
                    sondehub_output=self.sondehub_output
                )
            
            self.logger.info("Initialization complete!")
            return True
            
        except Exception as e:
            self.logger.error(f"Initialization failed: {e}", exc_info=True)
            return False
    
    def start(self):
        """Start all components"""
        self.logger.info("Starting OpenWXSDR...")
        self.running = True
        
        try:
            # Start web UI
            if self.webui:
                self.webui.start()

            # Start anonymous install ping (no-op if telemetry.enabled: false)
            if self.install_ping:
                self.install_ping.start()
            
            # Start decoder manager (only for rtlsdr/ka9q modes)
            if self.decoder_manager:
                self.decoder_manager.start()
            
            # Start appropriate SDR
            sdr_type = self.config['sdr']['type']
            
            if sdr_type == 'rtlsdr':
                # DeviceWorkers handle everything; just start them
                if self.device_manager:
                    self.device_manager.start()
            
            elif sdr_type == 'ka9q':
                if self.ka9q_receiver:
                    self.ka9q_receiver.start_receiving()
            
            elif sdr_type == 'flux242':
                # Start flux242 receiver
                if self.flux242_receiver:
                    if not self.flux242_receiver.start():
                        self.logger.error("Failed to start flux242 receiver!")
                        self.stop()
                        return

            elif sdr_type == 'airspy':
                if self.airspy_receiver:
                    self.airspy_receiver.start()

            self.logger.info("OpenWXSDR started successfully!")
            self.logger.info(f"Web UI available at http://localhost:{self.config['webui']['port']}")
            
            # Main loop (different for flux242 vs others)
            if sdr_type == 'flux242':
                self._flux242_main_loop()
            else:
                self._main_loop()  # airspy, rtlsdr, ka9q all use the same idle main loop
            
        except Exception as e:
            self.logger.error(f"Error during operation: {e}", exc_info=True)
            self.stop()
    
    def _main_loop(self):
        """Main application loop for rtlsdr/ka9q modes"""
        while self.running:
            try:
                # RTL-SDR mode: DeviceWorkers manage scan/decode internally
                # KA9Q mode: decoder_manager handles signals from ka9q_receiver
                if self.spectrum_analyzer and self.decoder_manager:
                    signals = self.spectrum_analyzer.get_detected_signals()
                    if signals:
                        self.decoder_manager.update_signals(signals)

                time.sleep(1)

            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(5)
    
    def _flux242_main_loop(self):
        """Main application loop for flux242 mode"""
        # flux242 runs autonomously, we just need to keep process alive
        # and monitor health
        while self.running:
            try:
                if self.flux242_receiver:
                    status = self.flux242_receiver.get_status()
                    if not status['running'] or not status['process_alive']:
                        self.logger.error("Flux242 receiver died, stopping...")
                        self.running = False
                        break
                
                time.sleep(5)
                
            except Exception as e:
                self.logger.error(f"Error in flux242 main loop: {e}", exc_info=True)
                time.sleep(5)
    
    def stop(self):
        """Stop all components"""
        self.logger.info("Stopping OpenWXSDR...")
        self.running = False
        
        # Stop components in reverse order
        if self.install_ping:
            self.install_ping.stop()

        if self.device_manager:
            self.device_manager.stop()

        if self.decoder_manager:
            self.decoder_manager.stop()
        
        if self.spectrum_analyzer:
            self.spectrum_analyzer.stop_scanning()
            self.spectrum_analyzer.close()
        
        if self.ka9q_receiver:
            self.ka9q_receiver.stop_receiving()
            self.ka9q_receiver.close()
        
        if self.flux242_receiver:
            self.flux242_receiver.stop()

        if self.airspy_receiver:
            self.airspy_receiver.stop()
        
        if self.udp_output:
            self.udp_output.close()

        if self.mqtt_output:
            self.mqtt_output.close()

        if self.http_output:
            self.http_output.close()

        if self.sondehub_output:
            self.sondehub_output.close()
        
        self.logger.info("OpenWXSDR stopped")
    
    def _check_priority_frequency(self):
        """
        Check priority frequency before starting scanner
        Waits for configured timeout to see if signal can be decoded
        """
        priority_freq_mhz = self.config.get('detection', {}).get('priority_frequency')
        timeout = self.config.get('detection', {}).get('priority_check_timeout', 30)
        
        # Skip if no priority frequency configured
        if not priority_freq_mhz or priority_freq_mhz <= 0:
            return
        
        priority_freq = priority_freq_mhz * 1e6  # Convert MHz to Hz
        
        # Get optional sonde type hint
        sonde_type_hint = self.config.get('detection', {}).get('priority_sonde_type')
        
        # Determine bandwidth based on sonde type
        # Different sonde types have characteristic bandwidths:
        bandwidth_map = {
            'RS41': 4500,   # 4-5 kHz
            'RS92': 2800,   # 2.6-3 kHz  
            'DFM': 7500,    # 6-9 kHz (DFM needs wider BW!)
            'M10': 9000,    # 9-15 kHz
            'M20': 20000,   # 18-22 kHz
            'iMet': 12000   # 10-15 kHz
        }
        
        if sonde_type_hint and sonde_type_hint.upper() in bandwidth_map:
            bandwidth = bandwidth_map[sonde_type_hint.upper()]
            self.logger.info(f"Checking priority frequency: {priority_freq_mhz:.3f} MHz as {sonde_type_hint} for {timeout}s")
        else:
            # Default to middle-range bandwidth that won't bias detection
            bandwidth = 7000  # Neutral value between RS41 and DFM
            self.logger.info(f"Checking priority frequency: {priority_freq_mhz:.3f} MHz (auto-detect) for {timeout}s before starting scanner")
        
        try:
            # Create a signal for the priority frequency
            from .sdr.rtlsdr_analyzer import DetectedSignal
            priority_signal = DetectedSignal(
                frequency=priority_freq,
                strength=25.0,  # Assume good signal
                bandwidth=bandwidth,
                timestamp=time.time()
            )
            
            # Try to start decoder for priority frequency
            if self.decoder_manager:
                # Inject the priority signal
                self.decoder_manager.update_signals([priority_signal])
                
                # Wait for timeout to see if frames are decoded
                start_time = time.time()
                frames_received = False
                no_progress_timeout = 10  # Abort if no frames after 10s (faster than full timeout)
                
                while time.time() - start_time < timeout:
                    # Check if we're receiving frames
                    if self.webui and len(self.webui.sondes) > 0:
                        frames_received = True
                        self.logger.info(f"Priority frequency is decoding successfully - keeping decoder active")
                        break
                    
                    # Early abort if no frames after 10 seconds - likely PLL/hardware issue
                    if time.time() - start_time >= no_progress_timeout and not frames_received:
                        self.logger.warning(
                            f"Priority frequency check: No frames decoded after {no_progress_timeout}s. "
                            f"Likely RTL-SDR PLL failure or weak signal. Aborting to free device for scanning."
                        )
                        # Force cleanup of stuck decoder
                        if hasattr(self.device_manager, 'stop_all_decoders'):
                            self.device_manager.stop_all_decoders()
                        break
                    
                    time.sleep(1)
                
                if not frames_received:
                    self.logger.info(f"No frames decoded on priority frequency after {int(time.time() - start_time)}s - will start scanner")
                    # The decoder manager will handle cleanup of idle decoders
            
        except Exception as e:
            self.logger.error(f"Error checking priority frequency: {e}", exc_info=True)
    
    @staticmethod
    def _format_telemetry_log(telemetry) -> str:
        """One-line telemetry summary. Optional fields (T/P/H, SNR, RSSI) print
        N/A when the sonde/receiver supplied no real value — never a fabricated
        default. Temperature 0 °C is a real value (only None → N/A); SNR/RSSI of
        None or exactly 0.0 (the unset default) → N/A."""
        def env(v, spec):
            return spec.format(v) if v is not None else "N/A"
        def sig(v, spec):
            return spec.format(v) if (v is not None and v != 0.0) else "N/A"

        pos = telemetry.position
        pos_str = ""
        if pos:
            pos_str = (f"{pos.latitude:.5f} {pos.longitude:.5f} "
                       f"{pos.altitude:.0f}m ")
        vel_str = ""
        if telemetry.velocity:
            vel_str = f"Vv:{telemetry.velocity.vertical_speed:+.1f}m/s "

        e = telemetry.environment
        temp = e.temperature if e else None
        hum = e.humidity if e else None
        pres = e.pressure if e else None
        return (
            f"{telemetry.sonde_type} {telemetry.serial} "
            f"{telemetry.frequency/1e6:.3f}MHz "
            f"#{telemetry.frame_number} "
            f"{pos_str}{vel_str}"
            f"T:{env(temp, '{:.1f}C')} P:{env(pres, '{:.1f}hPa')} H:{env(hum, '{:.0f}%')} "
            f"SNR:{sig(telemetry.snr, '{:.1f}dB')} RSSI:{sig(telemetry.rssi, '{:.1f}dBm')}"
        )

    def _handle_telemetry(self, telemetry: SondeTelemetry):
        """Handle decoded telemetry from decoders (rtlsdr/ka9q modes)"""
        try:
            self.logger.debug(f"[TELEMETRY] Received: serial={telemetry.serial}, type={telemetry.sonde_type}")
            self.logger.info(self._format_telemetry_log(telemetry))

            # Frequency repository: mark this frequency confirmed (once per serial)
            self.frequency_repository.record_confirmed(telemetry)

            # Send to web UI
            if self.webui:
                self.webui.add_telemetry(telemetry)
            
            # Send to OpenWX via UDP
            if self.udp_output:
                self.udp_output.send_telemetry(telemetry)

            # Publish via MQTT
            if self.mqtt_output:
                self.mqtt_output.send_telemetry(telemetry)

            # Upload via HTTP
            if self.http_output:
                self.http_output.send_telemetry(telemetry)

            # Upload to SondeHub
            if self.sondehub_output:
                self.logger.debug(f"[TELEMETRY] Routing to SondeHub output for {telemetry.serial}")
                self.sondehub_output.send_telemetry(telemetry)
            else:
                self.logger.debug(f"[TELEMETRY] SondeHub output not initialized")
            
        except Exception as e:
            self.logger.error(f"Error handling telemetry: {e}", exc_info=True)
    
    def _handle_flux242_telemetry(self, telemetry_dict: dict):
        """Handle decoded telemetry from flux242 receiver (dict format)"""
        try:
            from .decoders.models import SondePosition, SondeVelocity, SondeEnvironment
            from datetime import datetime
            
            # Convert dict to SondeTelemetry object for compatibility
            position = None
            velocity = None
            environment = None
            
            # Parse datetime from ISO format (e.g., "2026-05-04T12:17:31.992Z")
            dt = None
            if telemetry_dict.get('datetime'):
                try:
                    dt = datetime.fromisoformat(telemetry_dict['datetime'].replace('Z', '+00:00'))
                except:
                    dt = datetime.utcnow()
            else:
                dt = datetime.utcnow()
            
            if telemetry_dict.get('lat') and telemetry_dict.get('lon'):
                position = SondePosition(
                    latitude=telemetry_dict['lat'],
                    longitude=telemetry_dict['lon'],
                    altitude=telemetry_dict.get('alt', 0),
                    datetime=dt
                )
            
            if telemetry_dict.get('vel_h') is not None:
                velocity = SondeVelocity(
                    horizontal_speed=telemetry_dict['vel_h'],
                    vertical_speed=telemetry_dict.get('vel_v', 0),
                    heading=telemetry_dict.get('heading', 0)
                )
            
            # Environmental data
            if telemetry_dict.get('temp') or telemetry_dict.get('humidity') or telemetry_dict.get('pressure'):
                environment = SondeEnvironment(
                    temperature=telemetry_dict.get('temp'),
                    humidity=telemetry_dict.get('humidity'),
                    pressure=telemetry_dict.get('pressure')
                )
            
            # Get frequency (flux242_receiver already converted to MHz)
            frequency_mhz = telemetry_dict.get('frequency', 0.0)
            rssi_value = telemetry_dict.get('rssi')
            if rssi_value is None:
                rssi_value = telemetry_dict.get('power_db', telemetry_dict.get('signal_db'))
            snr_value = telemetry_dict.get('snr')
            if snr_value is None:
                snr_value = telemetry_dict.get('signal_strength')
            
            telemetry = SondeTelemetry(
                serial=telemetry_dict.get('serial', 'UNKNOWN'),
                sonde_type=telemetry_dict.get('type', 'Unknown'),
                frame_number=telemetry_dict.get('frame', 0),
                position=position,
                velocity=velocity,
                environment=environment,
                satellites=telemetry_dict.get('sats'),
                frequency=frequency_mhz * 1e6,  # Convert MHz back to Hz for SondeTelemetry
                rssi=rssi_value,
                snr=snr_value,
            )
            
            # Log telemetry
            if telemetry.position:
                self.logger.info(
                    f"Flux242: {telemetry.sonde_type} {telemetry.serial} "
                    f"F{telemetry.frame_number} "
                    f"{telemetry.position.latitude:.5f},{telemetry.position.longitude:.5f} "
                    f"Alt:{telemetry.position.altitude:.0f}m "
                    f"Freq:{frequency_mhz:.3f}MHz"
                )
            else:
                self.logger.debug(f"Flux242: {telemetry.sonde_type} {telemetry.serial} F{telemetry.frame_number}")
            
            # Send to web UI
            if self.webui:
                self.webui.add_telemetry(telemetry)
            
            # Send to OpenWX via UDP
            if self.udp_output:
                self.udp_output.send_telemetry(telemetry)

            # Publish via MQTT
            if self.mqtt_output:
                self.mqtt_output.send_telemetry(telemetry)

            # Upload via HTTP
            if self.http_output:
                self.http_output.send_telemetry(telemetry)

            # Upload to SondeHub
            if self.sondehub_output:
                self.logger.debug(f"[TELEMETRY-Flux242] Routing to SondeHub output for {telemetry.serial}")
                self.sondehub_output.send_telemetry(telemetry)
            else:
                self.logger.debug(f"[TELEMETRY-Flux242] SondeHub output not initialized")
            
        except Exception as e:
            self.logger.error(f"Error handling flux242 telemetry: {e}", exc_info=True)

    def _handle_ka9q_telemetry(self, telemetry_dict: dict):
        """Handle decoded telemetry from the KA9Q receiver's fsk_demod→decoder
        pipeline. The rs1729 decoder JSON uses 'id' for the serial and carries
        no frequency (that comes from the RTP stream/SSRC and is injected by
        ka9q_receiver before this callback). Mirrors _handle_flux242_telemetry."""
        try:
            from .decoders.models import SondePosition, SondeVelocity, SondeEnvironment
            from datetime import datetime

            serial = str(telemetry_dict.get('id') or telemetry_dict.get('serial') or 'UNKNOWN').strip()
            # Strip family prefixes the way the RTL path does
            for prefix in ('RS41-', 'M10-', 'M20-', 'DFM-', 'iMet-', 'IMET-', 'LMS6-', 'MRZ-'):
                if serial.startswith(prefix):
                    serial = serial[len(prefix):]
                    break
            if not serial or serial == 'UNKNOWN':
                return  # No usable serial → not a real telemetry frame

            dt = None
            if telemetry_dict.get('datetime'):
                try:
                    dt = datetime.fromisoformat(str(telemetry_dict['datetime']).replace('Z', '+00:00'))
                except Exception:
                    dt = datetime.utcnow()
            else:
                dt = datetime.utcnow()

            position = None
            lat = telemetry_dict.get('lat')
            lon = telemetry_dict.get('lon')
            alt = telemetry_dict.get('alt')
            if lat is not None and lon is not None:
                # Reject obviously invalid fixes (0,0 placeholder / out of range)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0.0 and lon == 0.0):
                    return
                position = SondePosition(latitude=lat, longitude=lon,
                                         altitude=alt if alt is not None else 0, datetime=dt)

            velocity = None
            if telemetry_dict.get('vel_h') is not None:
                velocity = SondeVelocity(
                    horizontal_speed=telemetry_dict['vel_h'],
                    vertical_speed=telemetry_dict.get('vel_v', 0),
                    heading=telemetry_dict.get('heading', 0)
                )

            # PTU (temp/humidity/pressure) from the rs41mod --ptu2 JSON. Use
            # `is not None` (not truthiness): a genuine 0.0 °C temperature or
            # 0.0 % humidity is falsy and would otherwise be dropped.
            environment = None
            _temp = telemetry_dict.get('temp')
            _hum = telemetry_dict.get('humidity')
            _pres = telemetry_dict.get('pressure')
            if _temp is not None or _hum is not None or _pres is not None:
                environment = SondeEnvironment(
                    temperature=_temp,
                    humidity=_hum,
                    pressure=_pres
                )

            frequency_hz = float(telemetry_dict.get('frequency', 0.0))  # injected by ka9q_receiver (Hz)

            telemetry = SondeTelemetry(
                serial=serial,
                sonde_type=telemetry_dict.get('type', 'RS41'),
                frame_number=telemetry_dict.get('frame', 0),
                position=position,
                velocity=velocity,
                environment=environment,
                satellites=telemetry_dict.get('sats'),
                frequency=frequency_hz,
                rssi=telemetry_dict.get('rssi'),
                snr=telemetry_dict.get('snr'),
            )

            if telemetry.position:
                self.logger.info(self._format_telemetry_log(telemetry))

            self.frequency_repository.record_confirmed(telemetry)

            if self.webui:
                self.webui.add_telemetry(telemetry)
            if self.udp_output:
                self.udp_output.send_telemetry(telemetry)
            if self.mqtt_output:
                self.mqtt_output.send_telemetry(telemetry)
            if self.http_output:
                self.http_output.send_telemetry(telemetry)
            if self.sondehub_output:
                self.sondehub_output.send_telemetry(telemetry)

        except Exception as e:
            self.logger.error(f"Error handling KA9Q telemetry: {e}", exc_info=True)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)
