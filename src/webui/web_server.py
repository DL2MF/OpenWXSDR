"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : web_server.py
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
#  Flask-based web interface and REST API server for OpenWX.
#
#  Provides the WebUI class which serves a real-time Leaflet map display,
#  tracks sonde telemetry history, and exposes a comprehensive JSON API
#  for frontend dashboards and external integrations.
#
#  Key REST API endpoints:
#    GET  /                    Interactive sonde tracking map (Leaflet)
#    GET  /api/sondes           Active sondes with full telemetry and tracks
#    GET  /api/sonde/<serial>   Per-sonde telemetry history
#    GET  /api/status           System frame counters and resource usage
#    GET  /api/health           SDR, decoder, MQTT, SondeHub health status
#    GET  /api/devices          SDR device assignments and decoder states
#    GET  /api/spectrum         Live spectrum data for waterfall display
#
#  Features: per-sonde CSV log files, GPS jump sanity filter, configurable
#  sonde retention time, systemd service status modal, runtime config API.
#
# =============================================================================
"""

import os
import sys
import socket
import subprocess
import shutil
import json
import time
import uuid

# Import version info from package
from .. import __version__, __build_date__
from ..hardware_info import detect_host_hardware
import logging
import threading
import math
import re
from flask import Flask, render_template, jsonify, request, send_file, send_from_directory, Response
from flask_cors import CORS
from typing import Dict, List, Set, Optional
from datetime import datetime, timedelta

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from ..decoders.models import SondeTelemetry


class _RxStatsError(Exception):
    """Carries an HTTP status code alongside the message for _compute_rx_statistics()."""
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class WebUI:
    """Flask-based web interface"""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger('WebUI')
        
        self.enabled = config['webui']['enabled']
        self.host = config['webui']['host']
        self.port = config['webui']['port']
        
        # Store telemetry data (keyed by serial number)
        self.sondes: Dict[str, List[dict]] = {}
        self.total_frames_received = 0  # Total frames ever received
        self.total_sondes_received = 0  # Total unique sondes ever received
        self._today_frames = 0          # Frames received on the current UTC day
        self._today_frames_date = datetime.utcnow().strftime('%Y-%m-%d')  # UTC day the counter belongs to
        self.active_frequencies = set()  # Currently active frequencies
        self.lock = threading.Lock()
        self.start_time = time.time()  # Track uptime
        
        # Per-sonde log files
        self.sonde_logfiles: Dict[str, str] = {}  # serial -> log file path

        # Background RX-statistics jobs (job_id -> progress/result dict), so the web UI
        # can poll for "processing file X/Y" progress on gateways with a large sonde log
        # history instead of the request just hanging with no feedback.
        self._rx_stats_jobs: Dict[str, dict] = {}
        self._rx_stats_jobs_lock = threading.Lock()

        # Persistent RX-statistics cache: historical sonde logfiles never change once a
        # session is over, and the activity log only ever grows by appending — so both are
        # cached and only the diff (new bytes / new-or-changed files) gets (re-)read on each
        # call, instead of rescanning the entire log history from scratch every time.
        self._rx_stats_cache_lock = threading.Lock()
        self._rx_stats_activity_cache: Dict[str, dict] = {}   # activity logfile name -> parsed events + byte offset
        self._rx_stats_file_cache: Dict[str, dict] = {}       # sonde logfile path -> frame/altitude scan result
        self._rx_stats_cache_path = os.path.join('data', 'logs', '.rx_stats_cache.json')
        self._load_rx_stats_cache()

        # Sonde retention time (seconds) - how long to keep sondes on map after last update
        self.sonde_retention_time = config.get('webui', {}).get('sonde_retention_time', 600)  # Default 10 minutes
        self.logger.info(f"Sonde retention time: {self.sonde_retention_time} seconds")

        # Keep significantly more points so long flights remain one continuous track.
        self.max_track_points = int(config.get('webui', {}).get('max_track_points', 20000))
        
        # Create log directory
        os.makedirs('data/logs', exist_ok=True)
        
        # Structured action logger
        self.action_log_path = os.path.abspath(f"data/logs/openwxsdr_{config.get('station', {}).get('callsign', 'unknown')}.log")
        self.action_logger = self._setup_action_logger()
        
        # Unified debug configuration - single log_level setting
        logging_cfg = config.get('logging', {})
        self.log_level = str(logging_cfg.get('log_level', 'INFO')).upper()  # INFO, WARNING, DEBUG
        self.debug_mode = bool(logging_cfg.get('debug_mode', self.log_level == 'DEBUG'))  # Auto-enable for DEBUG
        
        self.sonde_first_frames = {}  # Track first frame logged per sonde serial
        self.sonde_last_frames = {}  # Track last frame for each sonde
        self.sonde_frame_counts = {}  # Track total frame count per sonde serial
        
        # Apply initial logging levels from config
        self._apply_logging_levels()
        
        # External URL settings
        webui_config = config.get('webui', {})
        self.external_url_provider = str(webui_config.get('external_url_provider', 'openwx'))
        self.external_url_custom = str(webui_config.get('external_url_custom', ''))
        self.enable_config = bool(webui_config.get('enable_config', True))
        
        # References to other components for health monitoring
        self.spectrum_analyzer = None
        self.decoder_manager = None
        self.flux242_receiver = None
        self.ka9q_receiver = None
        self.mqtt_output = None
        self.sondehub_output = None
        
        # Get absolute paths for templates and static files
        # This ensures paths work regardless of working directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, '../..'))
        templates_dir = os.path.join(project_root, 'templates')
        static_dir = os.path.join(project_root, 'static')
        self.assets_dir = os.path.join(project_root, 'assets')  # Store assets path for route
        
        # Create Flask app with absolute paths
        self.app = Flask(__name__, 
                        template_folder=templates_dir,
                        static_folder=static_dir,
                        static_url_path='/static')
        CORS(self.app)
        
        # Configure routes
        self._setup_routes()
        
        self.server_thread = None
    
    def _setup_routes(self):
        """Setup Flask routes"""
        
        @self.app.route('/assets/<path:filename>')
        def serve_assets(filename):
            """Serve files from assets directory"""
            return send_from_directory(self.assets_dir, filename)
        
        @self.app.route('/')
        def index():
            """Main map page"""
            map_config = self.config['webui']['map']
            station_cfg = self.config.get('station', {})
            return render_template('index.html',
                                 default_lat=map_config['default_lat'],
                                 default_lon=map_config['default_lon'],
                                 default_zoom=map_config['default_zoom'],
                                 tile_server=map_config['tile_server'],
                                 station_lat=station_cfg.get('lat', map_config['default_lat']),
                                 station_lon=station_cfg.get('lon', map_config['default_lon']),
                                 station_alt=station_cfg.get('alt', 0),
                                 callsign=station_cfg.get('callsign', ''),
                                 version=__version__,
                                 build_date=__build_date__,
                                 static_js_v=self._static_js_version(),
                                 enable_config=self.enable_config)

        @self.app.route('/dashboard')
        def dashboard():
            """System dashboard overview page"""
            return render_template('dashboard.html',
                                 version=__version__,
                                 callsign=self.config['station']['callsign'],
                                 build_date=__build_date__,
                                 static_js_v=self._static_js_version(),
                                 enable_config=self.enable_config)

        @self.app.route('/testresult')
        @self.app.route('/testresults')
        def testresult():
            """Serve the decode test harness report (testscripts/rs_decode_test.py).
            The harness writes this self-contained HTML; here it is handed to the
            browser so results can be viewed with the service running. NOTE: during
            a live test the service is stopped to free the SDR, so for LIVE viewing
            use the harness's own built-in HTTP server (http_server in
            test_input.yaml). Path is configurable via webui.testresult_path."""
            cfg_path = self.config.get('webui', {}).get(
                'testresult_path', 'testscripts/testresults/testresult.html')
            path = cfg_path if os.path.isabs(cfg_path) else os.path.join(os.getcwd(), cfg_path)
            if not os.path.isfile(path):
                return (f"<h3>No test result yet</h3><p>Run "
                        f"<code>python3 testscripts/rs_decode_test.py</code> first.<br>"
                        f"Expected file: <code>{path}</code></p>"), 404
            return send_file(path)

        @self.app.route('/api/sondes')
        def get_sondes():
            """Get all active sondes with their telemetry"""
            with self.lock:
                sondes = []
                for serial, data in self.sondes.items():
                    if not data:
                        continue
                    
                    # Skip sondes with invalid/incomplete serial numbers
                    # Filter out: UNKNOWN, too short serials, serials with special chars (like "-+")
                    if not self._is_valid_serial(serial):
                        continue
                    
                    latest = data[-1]
                    sonde_info = {
                        'id': serial,  # Use 'id' to match frontend expectations
                        'serial': serial,  # Also include 'serial' for compatibility
                        'type': latest.get('type', 'Unknown'),
                        'subtype': latest.get('subtype'),
                        'lat': latest.get('lat'),
                        'lon': latest.get('lon'),
                        'alt': latest.get('alt'),
                        'vel_h': latest.get('vel_h'),
                        'vel_v': latest.get('vel_v'),
                        'heading': latest.get('heading'),
                        'frequency': latest.get('frequency'),
                        'rssi': latest.get('rssi'),
                        'snr': latest.get('snr'),
                        'battery': latest.get('batt'),  # battery voltage (M10/M20/DFM tiles show this instead of SNR)
                        'temp': latest.get('temp'),
                        'humidity': latest.get('humidity'),
                        'pressure': latest.get('pressure'),
                        'frame': latest.get('frame', 0),
                        'path': [[p.get('lat'), p.get('lon')] for p in data if p.get('lat') and p.get('lon')],
                        'timestamp': latest.get('timestamp'),
                        'reception_time': latest.get('reception_time', latest.get('timestamp'))  # Use reception_time for active detection
                    }
                    sondes.append(sonde_info)
                
                return jsonify({
                    'sondes': sondes,
                    'count': len(sondes),
                    'total_frames': self.total_frames_received,
                    'total_sondes': self.total_sondes_received,
                    'today_frames': self._get_today_frames(),
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                })

        @self.app.route('/api/gateway')
        def get_gateway():
            """Gateway self-info for the map's own-station marker popup:
            identity/hardware (like MQTT.openwx.de) plus the sondes this gateway
            received in the last hour with great-circle distance from the station.
            """
            station_cfg = self.config.get('station', {})
            lat = station_cfg.get('lat')
            lon = station_cfg.get('lon')
            alt = station_cfg.get('alt', 0)
            with self.lock:
                active = len([s for s in self.sondes if self._is_valid_serial(s)])
                total_frames = self.total_frames_received
            return jsonify({
                'name': station_cfg.get('callsign', '') or 'Gateway',
                'status': 'online',
                'receiver': station_cfg.get('receiver', 'Unknown'),
                'antenna': station_cfg.get('antenna', 'Unknown'),
                'hardware': detect_host_hardware(),
                'version': f"OpenWXSDR V{__version__}",
                'lat': lat,
                'lon': lon,
                'alt': alt,
                'active_sondes': active,
                'total_frames': total_frames,
                'recent_window_s': int(self.sonde_retention_time),
                'recent_sondes': self._recent_sondes(lat, lon, alt, self.sonde_retention_time),
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            })

        @self.app.route('/api/landings')
        def get_landings():
            """Landing (last-known) position of every sonde in the logs, with the
            metadata for the map markers/heatmap and the great-circle distance
            from the station (for the 25/50/100 km range rings). One entry per
            serial (the most recent log wins)."""
            station_cfg = self.config.get('station', {})
            slat = station_cfg.get('lat')
            slon = station_cfg.get('lon')
            log_dir = 'data/logs'
            seen: Dict[str, dict] = {}
            if os.path.isdir(log_dir):
                name_re = re.compile(r'^(.+?)-(\d{8})-(\d{6})\.log$')
                for fn in sorted(os.listdir(log_dir)):
                    m = name_re.match(fn)
                    if not m:
                        continue
                    serial = m.group(1)
                    info = self._landing_from_log(os.path.join(log_dir, fn))
                    if not info or info.get('lat') is None:
                        continue
                    entry = {
                        'serial': serial,
                        'type': info.get('type'),
                        'frequency': info.get('frequency'),
                        'timestamp': info.get('timestamp'),
                        'lat': info['lat'],
                        'lon': info['lon'],
                        'alt': info.get('alt'),
                        'vvel': info.get('vvel'),
                        'distance_km': self._haversine_km(slat, slon, info['lat'], info['lon']),
                        'filename': fn,   # lets the map load this sonde's full path
                    }
                    # sorted() ascending → later filenames (newer) overwrite.
                    seen[serial] = entry
            return jsonify({
                'station': {'lat': slat, 'lon': slon},
                'landings': list(seen.values()),
                'count': len(seen),
            })

        @self.app.route('/api/sonde/<serial>')
        def get_sonde(serial):
            """Get telemetry for specific sonde"""
            with self.lock:
                data = self.sondes.get(serial, [])
                return jsonify({
                    'serial': serial,
                    'telemetry': data
                })
        
        @self.app.route('/api/sonde/<serial>/history')
        def get_sonde_history(serial):
            """Get historical telemetry data for sonde statistics charts"""
            with self.lock:
                data = self.sondes.get(serial, [])
                
                if not data:
                    return jsonify({
                        'error': 'No data available for this sonde',
                        'serial': serial
                    }), 404
                
                # Extract relevant fields for charting
                _st = self.config.get('station', {})
                frames = []
                for point in data:
                    if not point.get('timestamp'):
                        continue

                    # Elevation angle + slant distance from the station to this
                    # fix (for the Elevation graph's second line).
                    elev = None
                    dist = None
                    if point.get('lat') is not None:
                        _look = self._look_angles(
                            _st.get('lat'), _st.get('lon'), _st.get('alt', 0),
                            point.get('lat'), point.get('lon'), point.get('alt'))
                        if _look:
                            elev = round(_look['elevation_deg'], 1)
                            dist = round(_look['slant_km'], 2)

                    frame = {
                        'timestamp': point['timestamp'],
                        'alt': point.get('alt'),
                        'vel_v': point.get('vel_v'),
                        'vel_h': point.get('vel_h'),
                        'rssi': point.get('rssi'),
                        'snr': point.get('snr'),
                        'sats': point.get('sats'),
                        'battery': point.get('batt'),  # API uses 'batt' key, charts expect 'battery'
                        'elevation': elev,
                        'distance': dist,
                        'frequency': point.get('frequency')  # for the stats header (right-click path)
                    }

                    # Only include frames with at least some data
                    if any(v is not None for k, v in frame.items() if k != 'timestamp'):
                        frames.append(frame)

            # LTTB decimation for fast charts (outside the lock — pure CPU).
            max_points = request.args.get('max_points', default=1000, type=int)
            total = len(frames)
            frames = self._decimate_frames(frames, max_points)
            return jsonify({
                'serial': serial,
                'frames': frames,
                'count': len(frames),
                'total_frames': total
            })
        
        @self.app.route('/api/status')
        def get_status():
            """Get system status"""
            with self.lock:
                active_sondes = len(self.sondes)
                total_frames = self.total_frames_received
                total_sondes = self.total_sondes_received
                # Get unique active frequencies
                frequencies = list(self.active_frequencies)
            
            # Get system metrics
            system_metrics = self._get_system_metrics()
            
            # Calculate uptime
            uptime_seconds = int(time.time() - self.start_time)
            
            return jsonify({
                'active_sondes': active_sondes,
                'total_frames': total_frames,
                'total_sondes': total_sondes,
                'total_sondes_received': total_sondes,  # Alias for dashboard
                'uptime_seconds': uptime_seconds,
                'frequencies': frequencies,
                'cpu_percent': system_metrics['cpu_percent'],
                'memory_percent': system_metrics['memory_percent'],
                'memory_used_mb': system_metrics['memory_used_mb'],
                'memory_total_mb': system_metrics['memory_total_mb'],
                'software_version': __version__,
                'build_date': __build_date__,
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            })
        
        @self.app.route('/api/health')
        def get_health():
            """Get system health status"""
            try:
                # Get last frame time (with lock for accessing self.sondes)
                last_frame_time = None
                with self.lock:
                    for serial, data in self.sondes.items():
                        if data and 'timestamp' in data[-1]:
                            frame_time = data[-1]['timestamp']
                            if last_frame_time is None or frame_time > last_frame_time:
                                last_frame_time = frame_time
                
                # Check SDR mode and get status (no lock needed - read-only component references)
                # Check if using flux242 mode
                if self.flux242_receiver is not None:
                    # Flux242 mode
                    decoder_status = 'flux242'
                    active_decoders = len(self.flux242_receiver.active_sondes) if hasattr(self.flux242_receiver, 'active_sondes') else 0
                    rtlsdr_status = 'flux242' if self.flux242_receiver.running else 'disconnected'
                # Check if using KA9Q mode
                elif self.ka9q_receiver is not None:
                    # KA9Q mode
                    decoder_status = 'running' if getattr(self.ka9q_receiver, 'running', False) else 'stopped'
                    active_decoders = self.ka9q_receiver.get_decoder_count() if hasattr(self.ka9q_receiver, 'get_decoder_count') else 0
                    stream_count = self.ka9q_receiver.get_stream_count() if hasattr(self.ka9q_receiver, 'get_stream_count') else 0
                    rtlsdr_status = 'decoding' if active_decoders > 0 else ('scanning' if stream_count > 0 else 'connected')
                else:
                    # Standard RTL-SDR mode
                    decoder_status = 'unknown'
                    active_decoders = 0
                    if self.decoder_manager is not None:
                        decoder_status = 'running' if self.decoder_manager.running else 'stopped'
                        with self.decoder_manager.lock:
                            active_decoders = len([
                                active for freq, active in self.decoder_manager.active_decoders.items()
                                if active.decoder.running
                            ])

                    # RTL-SDR hardware status
                    rtlsdr_status = 'unknown'
                    if active_decoders > 0:
                        rtlsdr_status = 'decoding'
                    elif self.decoder_manager is not None and hasattr(self.decoder_manager, 'get_worker_status'):
                        # New RTLSDRDeviceManager: check if any worker is scanning
                        scanning = any(
                            w['state'] == 'scanning'
                            for w in self.decoder_manager.get_worker_status()
                        )
                        rtlsdr_status = 'scanning' if scanning else 'connected'
                    elif self.spectrum_analyzer is not None:
                        if self.spectrum_analyzer.sdr is not None:
                            rtlsdr_status = 'connected'
                        else:
                            rtlsdr_status = 'disconnected'
                
                return jsonify({
                    'rtlsdr': {
                        'status': rtlsdr_status
                    },
                    'decoder_manager': {
                        'status': decoder_status,
                        'active_decoders': active_decoders
                    },
                    'mqtt': self._get_mqtt_health(),
                    'sondehub': self._get_sondehub_health(),
                    'ephemeris': self._get_ephemeris_health(),
                    'last_frame_time': last_frame_time,
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                })
            except Exception as e:
                self.logger.error(f"Error in /api/health: {e}", exc_info=True)
                return jsonify({'error': str(e), 'status': 'error'}), 500
        
        @self.app.route('/api/devices')
        def get_devices():
            """Get SDR device status and assignments"""
            devices = []

            # Only enumerate RTL-SDR serials in RTL-SDR mode;
            # Airspy/flux242/ka9q don't have RTL-SDR serial numbers.
            sdr_type = self.config.get('sdr', {}).get('type', 'rtlsdr')
            if sdr_type == 'rtlsdr':
                connected_serials = self._get_connected_rtlsdr_serials()
            else:
                connected_serials = None  # Non-RTL-SDR: always treat as present

            # Check if using KA9Q mode
            if self.ka9q_receiver is not None:
                # Get active streams from KA9Q receiver
                active_streams = self.ka9q_receiver.get_active_streams() if hasattr(self.ka9q_receiver, 'get_active_streams') else []
                active_decoders = self.ka9q_receiver.get_decoder_count() if hasattr(self.ka9q_receiver, 'get_decoder_count') else 0
                
                for stream in active_streams:
                    devices.append({
                        'serial': f'KA9Q-{stream.ssrc:08x}',
                        'status': 'decoding' if stream.ssrc in getattr(self.ka9q_receiver, 'active_decoders', {}) else 'idle',
                        'frequency': stream.frequency / 1e6 if stream.frequency else None,
                        'sonde_type': None,  # Updated when telemetry received
                        'active_sondes': 1 if stream.ssrc in getattr(self.ka9q_receiver, 'active_decoders', {}) else 0,
                        'present': True,
                        'ssrc': stream.ssrc,
                        'sample_rate': stream.sample_rate,
                        'packet_count': stream.packet_count
                    })
                
                # If no streams but receiver is running, show placeholder
                if not active_streams and getattr(self.ka9q_receiver, 'running', False):
                    devices.append({
                        'serial': 'KA9Q-Radio',
                        'status': 'scanning',
                        'frequency': None,
                        'sonde_type': 'Waiting for RTP streams',
                        'active_sondes': 0,
                        'present': True
                    })
            # Check if using flux242 mode
            elif self.flux242_receiver is not None:
                devices.append({
                    'serial': 'Flux242',
                    'status': 'decoding' if self.flux242_receiver.running else 'disconnected',
                    'frequency': self.config.get('sdr', {}).get('flux242', {}).get('center_freq', 0) / 1e6,
                    'sonde_type': 'Multi (5-6 sondes)',
                    'active_sondes': len(self.flux242_receiver.active_sondes) if hasattr(self.flux242_receiver, 'active_sondes') else 0,
                    'present': True
                })
            elif self.decoder_manager is not None:
                # Check for new RTLSDRDeviceManager (has per-worker status)
                if hasattr(self.decoder_manager, 'get_worker_status'):
                    worker_statuses = self.decoder_manager.get_worker_status()
                    for ws in worker_statuses:
                        device_serial = ws['serial']
                        present = (device_serial in connected_serials) if connected_serials is not None else True
                        state   = ws['state']    # 'idle' | 'scanning' | 'decoding'
                        freq_hz = ws['frequency']
                        devices.append({
                            'serial':       device_serial,
                            'status':       state if present else 'disconnected',
                            'frequency':    freq_hz if freq_hz else None,
                            'freq_label':   ws.get('freq_label'),
                            'sonde_type':   ws['sonde_type'],
                            'active_sondes': 1 if state == 'decoding' else 0,
                            'present':      present,
                            'scan_return_eta_s': ws.get('scan_return_eta_s'),
                            'decode_source': ws.get('decode_source'),
                            'sweep_enabled': ws.get('sweep_enabled', False),
                        })
                else:
                    # Legacy DecoderManager
                    device_configs = self.decoder_manager.device_configs
                    first_device   = self.decoder_manager.first_device_serial

                    for device_config in device_configs:
                        device_serial = device_config.get('serial', '0')
                        present = (device_serial in connected_serials) if connected_serials is not None else True

                        device_info = {
                            'serial':       device_serial,
                            'status':       'disconnected' if not present else 'idle',
                            'frequency':    None,
                            'sonde_type':   None,
                            'active_sondes': 0,
                            'present':      present
                        }

                        if present:
                            if device_serial == first_device and self.spectrum_analyzer is not None:
                                if self.spectrum_analyzer.running:
                                    device_info['status'] = 'scanning'
                                    device_info['frequency'] = device_config.get('center_freq', 0) / 1e6

                            with self.decoder_manager.lock:
                                for freq, active in self.decoder_manager.active_decoders.items():
                                    if active.device_serial == device_serial:
                                        device_info['status'] = 'decoding'
                                        device_info['frequency'] = freq / 1e6
                                        device_info['sonde_type'] = active.decoder.sonde_type if hasattr(active.decoder, 'sonde_type') else 'Unknown'
                                        device_info['active_sondes'] += 1
                                        break

                        devices.append(device_info)

            return jsonify({
                'devices': [d for d in devices if d.get('present', True)],
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            })

        @self.app.route('/api/frequency_repository')
        def get_frequency_repository():
            """Parse the newest logs/sdrfreq_*.log into a per-frequency summary
            for the repository modal. Aggregates rows by 10 kHz channel: a
            frequency is 'confirmed' if any decode produced telemetry there,
            else 'detected'. Returns nearest-first-agnostic list sorted by freq."""
            import glob, os
            try:
                files = sorted(glob.glob(os.path.join('logs', 'sdrfreq_*.log')))
                if not files:
                    return jsonify({'file': None, 'frequencies': []})
                path = files[-1]  # newest session
                agg = {}  # 10 kHz channel key -> dict
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or line.startswith('datetime_utc'):
                            continue
                        parts = line.split(',')
                        if len(parts) < 9:
                            continue
                        dt, event, freq_s, stype, serial, snr, rssi, alt, device = parts[:9]
                        try:
                            freq = float(freq_s)
                        except ValueError:
                            continue
                        key = round(freq * 100)  # 10 kHz bucket on MHz
                        e = agg.setdefault(key, {
                            'frequency_mhz': freq, 'status': 'detected', 'type': '',
                            'serial': '', 'snr_db': '', 'rssi_dbm': '', 'alt_m': '',
                            'device': '', 'first_seen': dt, 'last_seen': dt, 'count': 0,
                        })
                        e['count'] += 1
                        e['last_seen'] = dt
                        if device:
                            e['device'] = device
                        if snr:
                            e['snr_db'] = snr
                        if event == 'confirmed':
                            e['status'] = 'confirmed'
                            if stype:  e['type'] = stype
                            if serial: e['serial'] = serial
                            if rssi:   e['rssi_dbm'] = rssi
                            if alt:    e['alt_m'] = alt
                        elif not e['type'] and stype:
                            e['type'] = stype
                freqs = sorted(agg.values(), key=lambda x: x['frequency_mhz'])
                return jsonify({'file': os.path.basename(path), 'frequencies': freqs})
            except Exception as e:
                self.logger.error(f"Error in /api/frequency_repository: {e}", exc_info=True)
                return jsonify({'error': str(e), 'frequencies': []}), 500

        @self.app.route('/api/receivers')
        def get_receivers():
            """Get receiver status for dashboard"""
            try:
                receivers = []
                
                # KA9Q mode: show active streams as receivers
                if self.ka9q_receiver is not None:
                    active_streams = self.ka9q_receiver.get_active_streams() if hasattr(self.ka9q_receiver, 'get_active_streams') else []
                    active_decoders = getattr(self.ka9q_receiver, 'active_decoders', {})
                    
                    for stream in active_streams:
                        is_decoding = stream.ssrc in active_decoders
                        state = 'DECODING' if is_decoding else 'SCANNING'
                        
                        # Get sonde info if decoding
                        sonde_serial = None
                        sonde_type = None
                        if is_decoding and hasattr(active_decoders[stream.ssrc], 'decoder'):
                            decoder = active_decoders[stream.ssrc].decoder
                            sonde_serial = getattr(decoder, 'sonde_serial', None)
                            sonde_type = getattr(decoder, 'sonde_type', None)
                        
                        receivers.append({
                            'device_id': f'KA9Q-{stream.ssrc:08x}',
                            'state': state,
                            'frequency': stream.frequency,  # Already in Hz
                            'freq_label': f'{stream.frequency/1e6:.3f} MHz',
                            'sonde_type': sonde_type,
                            'sonde_serial': sonde_serial,
                            'decoder_mode': 'ka9q',
                            'channelizer_active': len(active_streams),
                            'channelizer_max': getattr(self.ka9q_receiver, 'max_channels', 0),
                        })
                    
                    # If no streams but receiver running, show waiting state
                    if not active_streams and getattr(self.ka9q_receiver, 'running', False):
                        receivers.append({
                            'device_id': 'KA9Q-Radio',
                            'state': 'IDLE',
                            'frequency': 0,
                            'freq_label': 'Waiting for RTP streams',
                            'sonde_type': None,
                            'sonde_serial': None,
                            'decoder_mode': 'ka9q',
                            'channelizer_active': 0,
                            'channelizer_max': getattr(self.ka9q_receiver, 'max_channels', 0),
                        })
                
                # RTL-SDR mode: show device workers
                elif self.decoder_manager is not None and hasattr(self.decoder_manager, 'get_worker_status'):
                    worker_statuses = self.decoder_manager.get_worker_status()
                    for ws in worker_statuses:
                        state = ws['state'].upper()  # IDLE, SCANNING, DECODING
                        freq_hz = ws.get('frequency')
                        # Convert MHz back to Hz if needed for display consistency
                        if freq_hz is not None:
                            freq_hz = freq_hz * 1e6  # API returns MHz, convert back to Hz
                        
                        # Step 4: Include channelizer info
                        receiver_info = {
                            'device_id': ws['serial'],
                            'state': state,
                            'frequency': freq_hz,
                            'freq_label': ws.get('freq_label'),  # Add scan range label
                            'sonde_type': ws['sonde_type'] or None,
                            'sonde_serial': ws.get('sonde_serial'),
                            'gain': ws.get('gain'),  # 0 = auto, else dB value
                            'decoder_mode': ws.get('decoder_mode', 'legacy'),  # 'legacy' or 'channelizer'
                            'channelizer_active': ws.get('channelizer_active', 0),  # Active channels
                            'channelizer_max': ws.get('channelizer_max', 0),  # Max channels
                            'sweep_enabled': ws.get('sweep_enabled', False),  # band-sweep active
                        }
                        receivers.append(receiver_info)
                
                return jsonify(receivers)
            except Exception as e:
                self.logger.error(f"Error in /api/receivers: {e}", exc_info=True)
                return jsonify({'error': str(e), 'receivers': []}), 500
        
        @self.app.route('/api/config')
        def get_config():
            """Get current configuration"""
            try:
                detection_cfg = self.config.get('detection', {})
                receivers_cfg = self.config.get('receivers', {})
                station_cfg = self.config.get('station', {})
                sdr_cfg = self.config.get('sdr', {})
                
                return jsonify({
                    'sdr_type': sdr_cfg.get('type'),
                    'airspy_available': sdr_cfg.get('airspy_support', False),
                    'fixed_channels_enable': detection_cfg.get('fixed_channels_enable'),
                    'sdr': sdr_cfg,  # Full SDR config for dashboard
                    'detection': detection_cfg,  # Full detection config
                    'receivers': receivers_cfg,  # Full receivers config
                    'station': station_cfg,  # Full station config
                    'success': True
                })
            except Exception as e:
                self.logger.error(f"Error in /api/config: {e}", exc_info=True)
                return jsonify({'error': str(e), 'success': False}), 500

        @self.app.route('/api/reset_statistics', methods=['POST'])
        def reset_statistics():
            """Reset UI statistics counters and tracked active sondes/frequencies."""
            try:
                with self.lock:
                    self.sondes.clear()
                    self.total_frames_received = 0
                    self.total_sondes_received = 0
                    self._today_frames = 0
                    self._today_frames_date = datetime.utcnow().strftime('%Y-%m-%d')
                    self.active_frequencies.clear()

                self.logger.info("System statistics reset requested from Web UI")
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Error resetting statistics: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/spectrum')
        def get_spectrum():
            """Return latest spectrum with receiver selection metadata."""
            dm = self.decoder_manager
            receiver_id = request.args.get('receiver', '').strip()

            if dm is None:
                return jsonify({
                    'freqs_mhz': [],
                    'power_db': [],
                    'signals': [],
                    'available_receivers': [],
                    'selected_receiver': '',
                })

            receivers = []
            if hasattr(dm, 'get_spectrum_receivers'):
                try:
                    receivers = dm.get_spectrum_receivers() or []
                except Exception:
                    receivers = []

            # Also expose configured receivers so the UI can switch between
            # RTL-SDR and Airspy selections when both are present in config.
            configured = []
            rtlsdr_cfg = self.config.get('sdr', {}).get('rtlsdr', {})
            for d in rtlsdr_cfg.get('devices', []) or []:
                serial = str(d.get('serial', '')).strip()
                if serial:
                    configured.append({'id': f'rtlsdr:{serial}', 'name': f'RTL-SDR {serial}'})

            airspy_cfg = self.config.get('sdr', {}).get('airspy', {})
            if airspy_cfg is not None:
                serial = str(airspy_cfg.get('serial', '') or 'airspy0').strip()
                configured.append({'id': f'airspy:{serial}', 'name': f'Airspy {serial}'})

            merged = {}
            for r in receivers + configured:
                rid = r.get('id')
                if rid:
                    merged[rid] = {'id': rid, 'name': r.get('name', rid)}
            receivers = list(merged.values())

            selected = receiver_id or (receivers[0]['id'] if receivers else '')

            if hasattr(dm, 'get_spectrum_for_receiver'):
                spec = dm.get_spectrum_for_receiver(selected) or {}
            elif hasattr(dm, 'get_spectrum'):
                spec = dm.get_spectrum() or {}
            else:
                spec = {}

            spec.setdefault('freqs_mhz', [])
            spec.setdefault('power_db', [])
            spec.setdefault('signals', [])
            spec['available_receivers'] = receivers
            spec['selected_receiver'] = selected

            # Backward compatibility: ensure a receiver label exists for modal title.
            if 'receiver_name' not in spec:
                if selected.startswith('rtlsdr:'):
                    spec['receiver_name'] = f"RTL-SDR {selected.split(':', 1)[1]}"
                elif selected.startswith('airspy:'):
                    spec['receiver_name'] = f"Airspy {selected.split(':', 1)[1]}"

            return jsonify(spec)

        @self.app.route('/api/runtime_config')
        def get_runtime_config():
            """Return runtime-mutable settings (debug_mode, snr_threshold, scan_interval, external_url)."""
            dm = self.decoder_manager
            if dm is not None and hasattr(dm, 'get_runtime_config'):
                result = {'success': True, **dm.get_runtime_config()}
            else:
                # Fallback: read from config.yaml
                det = self.config.get('detection', {})
                log = self.config.get('logging', {})
                result = {
                    'success': True,
                    'debug_mode': bool(self.debug_mode if hasattr(self, 'debug_mode') else log.get('debug_mode', False)),
                    'log_level': str(self.log_level if hasattr(self, 'log_level') else log.get('log_level', 'INFO')),
                    'snr_threshold': float(det.get('scan_threshold', 10.0)),
                    'scan_interval': int(self.config.get('receivers', {}).get('scan_interval', 15)),
                    'fixed_channel_scantime': int(det.get('fixed_channel_scantime', 60)),
                }
            # Add external URL settings
            result['external_url_provider'] = self.external_url_provider
            result['external_url_custom'] = self.external_url_custom
            return jsonify(result)

        @self.app.route('/api/runtime_config', methods=['POST'])
        def set_runtime_config():
            """Update debug_mode, snr_threshold, or scan_interval at runtime (no restart)."""
            try:
                data = request.get_json() or {}
                dm = self.decoder_manager
                changed = []

                if 'debug_mode' in data and dm is not None and hasattr(dm, 'set_debug_mode'):
                    debug_enabled = bool(data['debug_mode'])
                    dm.set_debug_mode(debug_enabled)
                    self.debug_mode = debug_enabled
                    changed.append('debug_mode')
                    self._log_action('config_change', {'setting': 'debug_mode', 'value': debug_enabled})
                    self._apply_logging_levels()
                
                if 'log_level' in data:
                    self.log_level = str(data['log_level']).upper()
                    changed.append('log_level')
                    self._log_action('config_change', {'setting': 'log_level', 'value': self.log_level})
                    self._apply_logging_levels()

                if 'snr_threshold' in data and dm is not None and hasattr(dm, 'set_snr_threshold'):
                    dm.set_snr_threshold(float(data['snr_threshold']))
                    changed.append('snr_threshold')

                if 'scan_interval' in data and dm is not None and hasattr(dm, 'set_scan_interval'):
                    dm.set_scan_interval(float(data['scan_interval']))
                    changed.append('scan_interval')
                
                if 'fixed_channel_scantime' in data and dm is not None and hasattr(dm, 'set_fixed_channel_scantime'):
                    dm.set_fixed_channel_scantime(int(data['fixed_channel_scantime']))
                    changed.append('fixed_channel_scantime')
                
                if 'external_url_provider' in data:
                    self.external_url_provider = str(data['external_url_provider'])
                    changed.append('external_url_provider')
                    self._log_action('config_change', {'setting': 'external_url_provider', 'value': self.external_url_provider})

                if not changed:
                    return jsonify({'success': False, 'error': 'No recognised fields or receiver not available'})
                return jsonify({'success': True, 'changed': changed})
            except Exception as e:
                self.logger.error(f"Error updating runtime config: {e}")
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/start_decoder', methods=['POST'])
        def start_decoder():
            """Start a decoder for a specific frequency, optionally on a specific device"""
            try:
                data = request.get_json()
                frequency    = float(data.get('frequency', 0)) * 1e6  # Convert MHz to Hz
                sonde_type   = data.get('sonde_type', 'RS41')
                device_serial = data.get('device_serial')  # None = auto
                duration_minutes = int(data.get('duration_minutes', 0))  # 0 = infinite
                
                self.logger.info(f"Manual decoder request: {frequency/1e6:.3f} MHz, type={sonde_type}, device={device_serial or 'auto'}, duration={duration_minutes}m")
                
                if frequency < 400e6 or frequency > 406e6:
                    self.logger.warning(f"Frequency {frequency/1e6:.3f} MHz out of range")
                    return jsonify({'success': False, 'error': 'Frequency out of range (400-406 MHz)'})
                
                if duration_minutes < 0 or duration_minutes > 1440:
                    self.logger.warning(f"Invalid duration: {duration_minutes} minutes")
                    return jsonify({'success': False, 'error': 'Duration must be between 0 and 1440 minutes'})
                
                if not self.decoder_manager:
                    self.logger.error("Decoder manager not available")
                    return jsonify({'success': False, 'error': 'Manual decoder start only available in RTL-SDR mode (not flux242/KA9Q)'})
                
                # RTLSDRDeviceManager supports optional device targeting
                if hasattr(self.decoder_manager, 'start_manual_decoder_on'):
                    # Pass duration_minutes if supported
                    import inspect
                    sig = inspect.signature(self.decoder_manager.start_manual_decoder_on)
                    if 'duration_seconds' in sig.parameters:
                        duration_seconds = duration_minutes * 60 if duration_minutes > 0 else None
                        success = self.decoder_manager.start_manual_decoder_on(
                            frequency, sonde_type, device_serial, duration_seconds=duration_seconds
                        )
                    else:
                        success = self.decoder_manager.start_manual_decoder_on(frequency, sonde_type, device_serial)
                else:
                    success = self.decoder_manager.start_manual_decoder(frequency, sonde_type)
                
                if success:
                    duration_label = 'infinite' if duration_minutes == 0 else f'{duration_minutes} minute{"s" if duration_minutes != 1 else ""}'
                    self.logger.info(f"Decoder started for {frequency/1e6:.3f} MHz ({duration_label})")
                    self._log_action('decoder_start', {
                        'frequency_mhz': round(frequency/1e6, 3),
                        'sonde_type': sonde_type,
                        'duration_minutes': duration_minutes,
                        'device': device_serial or 'auto'
                    })
                    return jsonify({'success': True, 'message': f'Decoder started for {frequency/1e6:.3f} MHz'})
                else:
                    return jsonify({'success': False, 'error': 'Failed to start decoder - check service logs for details'})
                    
            except Exception as e:
                self.logger.error(f"Error starting decoder: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/start_scanning', methods=['POST'])
        def start_scanning():
            """Trigger a specific idle/decoding device worker to start scanning"""
            try:
                data          = request.get_json() or {}
                device_serial = data.get('device_serial')

                if not self.decoder_manager:
                    return jsonify({'success': False, 'error': 'No decoder manager available'})

                # AirspyReceiver: stop decode and return to scan
                if hasattr(self.decoder_manager, 'stop_decode_and_scan') and hasattr(self.decoder_manager, '_state'):
                    airspy_id = getattr(self.decoder_manager, '_serial', '') or 'airspy0'
                    self.decoder_manager.stop_decode_and_scan()
                    self.logger.info(f"Triggered scanning on Airspy ({airspy_id})")
                    self._log_action('decoder_stop', {
                        'device': airspy_id,
                        'reason': 'manual_scan_requested'
                    })
                    return jsonify({'success': True, 'device': airspy_id})

                if not hasattr(self.decoder_manager, '_workers'):
                    return jsonify({'success': False, 'error': 'Start scanning not supported for this SDR type'})

                # Pick up any config.yaml detection-tuning edits (scan_check_time,
                # max_peaks, channel_spacing_hz, etc.) before the forced restart,
                # so "Start Scan" doubles as a lightweight apply-without-restart.
                if hasattr(self.decoder_manager, 'reload_detection_config'):
                    self.decoder_manager.reload_detection_config()

                for worker in self.decoder_manager._workers:
                    if device_serial and worker.device_serial != device_serial:
                        continue
                    # Force a clean restart regardless of current state
                    # (idle/scanning/decoding/error) — stops any active
                    # decode, discards any existing analyzer so the next
                    # scan cycle builds a fresh one with the reloaded config.
                    worker.force_clean_scan_restart()
                    self.logger.info(f"Forced clean scan restart on device {worker.device_serial}")
                    self._log_action('decoder_stop', {
                        'device': worker.device_serial,
                        'reason': 'manual_scan_requested'
                    })
                    return jsonify({'success': True, 'device': worker.device_serial})

                return jsonify({'success': False, 'error': 'Device not found'})

            except Exception as e:
                self.logger.error(f"Error starting scan: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/update_config', methods=['POST'])
        def update_config():
            """Update configuration and restart service"""
            try:
                import subprocess
                
                data = request.get_json()
                sdr_type = data.get('sdr_type')
                
                if sdr_type not in ['rtlsdr', 'flux242', 'ka9q', 'airspy']:
                    return jsonify({'success': False, 'error': 'Invalid SDR type'})
                
                # Read current config
                config_path = 'config.yaml'
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_text = f.read()

                config_text = self._update_mapping_keys_in_text(
                    config_text,
                    ['sdr'],
                    {'type': sdr_type}
                )

                with open(config_path, 'w', encoding='utf-8') as f:
                    f.write(config_text)
                
                # Restart service in background
                def restart_service():
                    import time
                    time.sleep(1)
                    try:
                        subprocess.run(['sudo', 'systemctl', 'restart', 'openwxsdr'], check=False)
                    except:
                        pass
                
                threading.Thread(target=restart_service, daemon=True).start()
                
                return jsonify({'success': True, 'message': 'Config updated, restarting service'})
            except Exception as e:
                self.logger.error(f"Error updating config: {e}")
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/config_sections')
        def get_config_sections():
            """Return relevant config sections for the Configuration modal tabs."""
            cfg = self.config
            station = cfg.get('station', {})
            receivers = cfg.get('receivers', {})
            detection = cfg.get('detection', {})
            sdr_cfg = cfg.get('sdr', {})
            rtlsdr_devices = cfg.get('sdr', {}).get('rtlsdr', {}).get('devices', [])
            airspy = cfg.get('sdr', {}).get('airspy', {})
            mqtt = cfg.get('openwx', {}).get('mqtt', {})
            sondehub = cfg.get('sondehub', {})
            import_api = cfg.get('import_api', {})

            available_receiver_bands = [
                {
                    'id': f"rtlsdr:{d.get('serial', i)}",
                    'label': f"RTL-SDR {d.get('serial', i)}",
                    'type': 'rtlsdr',
                    'center_freq_mhz': round(float(d.get('center_freq', 0)) / 1e6, 6),
                    'sample_rate_hz': int(d.get('sample_rate', 2400000)),
                }
                for i, d in enumerate(rtlsdr_devices)
                if d.get('center_freq') is not None
            ]

            if airspy.get('center_freq') is not None:
                available_receiver_bands.append({
                    'id': 'airspy:primary',
                    'label': 'Airspy Primary',
                    'type': 'airspy',
                    'center_freq_mhz': round(float(airspy.get('center_freq', 0)) / 1e6, 6),
                    'sample_rate_hz': int(airspy.get('sample_rate', 6000000)),
                })

            return jsonify({
                'success': True,
                'station': {
                    'callsign': station.get('callsign', ''),
                    'upload_position': bool(station.get('upload_position', False)),
                    'sdr_type': str(sdr_cfg.get('type', 'rtlsdr')),
                    'max_concurrent': int(receivers.get('max_concurrent', 1)),
                    'scan_interval': int(receivers.get('scan_interval', 15)),
                    'bandwidth': int(receivers.get('bandwidth', 12000)),
                    'min_signal_strength': float(receivers.get('min_signal_strength', -20)),
                    'use_dft_detect': bool(detection.get('use_dft_detect', True)),
                },
                'rtlsdr_devices': [
                    {
                        'serial': d.get('serial', ''),
                        'center_freq_mhz': round(d.get('center_freq', 404600000) / 1e6, 3),
                        'sample_rate': d.get('sample_rate', 2400000),
                        'gain': d.get('gain', 40),
                        'ppm_error': d.get('ppm_error', 0),
                    }
                    for d in rtlsdr_devices
                ],
                'airspy': {
                    'center_freq_mhz': round(airspy.get('center_freq', 404000000) / 1e6, 3),
                    'sample_rate': airspy.get('sample_rate', 6000000),
                    'decode_mode': airspy.get('decode_mode', 'legacy'),
                    'gain': airspy.get('gain', 14),
                    'scan_gain': airspy.get('scan_gain', 12),
                },
                'mqtt': {
                    'enabled': bool(mqtt.get('enabled', False)),
                    'server': mqtt.get('server', ''),
                    'port': int(mqtt.get('port', 1883)),
                    'username': mqtt.get('username', ''),
                    'password': mqtt.get('password', ''),
                },
                'sondehub': {
                    'enabled': bool(sondehub.get('enabled', False)),
                    'upload_url': sondehub.get('upload_url', ''),
                    'station_id': sondehub.get('station_id', ''),
                    'queue_mode': bool(sondehub.get('queue_mode', False)),
                    'queue_batch_max': int(sondehub.get('queue_batch_max', 200)),
                    'queue_max_size': int(sondehub.get('queue_max_size', 2000)),
                    'upload_rate_s': int(sondehub.get('upload_rate_s', 15)),
                },
                'import_api': {
                    'enabled': bool(import_api.get('enabled', False)),
                    'url': import_api.get('url', 'api.opnwx.de'),
                    'check_interval_s': int(import_api.get('check_interval_s', 300)),
                    'lat': float(import_api.get('lat', station.get('lat', 0.0))),
                    'lon': float(import_api.get('lon', station.get('lon', 0.0))),
                    'distance_km': int(import_api.get('distance_km', 500)),
                    'time_range_minutes': int(import_api.get('time_range_minutes', 240)),
                    'sonde_type': import_api.get('sonde_type', 'all'),
                    'max_sondes': int(import_api.get('max_sondes', 4)),
                    'station_lat': float(station.get('lat', 0.0)),
                    'station_lon': float(station.get('lon', 0.0)),
                },
                'detection': {
                    'fixed_channels_enable': detection.get('fixed_channels_enable', False),
                },
                'fixed_channels': detection.get('fixed_channels', []) or [],
                'sonde_types': detection.get('sonde_types', []) or [],
                'available_receiver_bands': available_receiver_bands,
            })

        @self.app.route('/api/save_config_sections', methods=['POST'])
        def save_config_sections():
            """Save one or more config sections to config.yaml (requires service restart)."""
            try:
                import yaml
                data = request.get_json() or {}
                config_path = 'config.yaml'

                with open(config_path, 'r', encoding='utf-8') as f:
                    config_text = f.read()

                config = yaml.safe_load(config_text) or {}

                if 'rtlsdr_device' in data:
                    dev = data['rtlsdr_device']
                    idx = int(dev.get('index', 0))
                    devices = config.get('sdr', {}).get('rtlsdr', {}).get('devices', [])
                    if 0 <= idx < len(devices):
                        device_serial = str(devices[idx].get('serial', '')).strip()
                        if device_serial:
                            config_text = self._update_rtlsdr_device_in_text(
                                config_text,
                                device_serial,
                                {
                                    'center_freq': int(round(float(dev['center_freq_mhz']) * 1e6)),
                                    'sample_rate': int(dev['sample_rate']),
                                    'gain': int(dev['gain']),
                                    'ppm_error': int(dev['ppm_error']),
                                }
                            )

                if 'airspy' in data:
                    a = data['airspy']
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['sdr', 'airspy'],
                        {
                            'center_freq': int(round(float(a['center_freq_mhz']) * 1e6)),
                            'sample_rate': int(a['sample_rate']),
                            'decode_mode': str(a['decode_mode']),
                            'gain': int(a['gain']),
                            'scan_gain': int(a['scan_gain']),
                        }
                    )

                if 'mqtt' in data:
                    m = data['mqtt']
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['openwx', 'mqtt'],
                        {
                            'enabled': bool(m['enabled']),
                            'server': str(m['server']),
                            'port': int(m['port']),
                            'username': str(m['username']),
                            'password': str(m['password']),
                        }
                    )

                if 'sondehub' in data:
                    s = data['sondehub']
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['sondehub'],
                        {
                            'enabled': bool(s['enabled']),
                            'upload_url': str(s['upload_url']),
                            'station_id': str(s['station_id']),
                            'queue_mode': bool(s.get('queue_mode', False)),
                            'queue_batch_max': int(s.get('queue_batch_max', 200)),
                            'queue_max_size': int(s.get('queue_max_size', 2000)),
                            'upload_rate_s': int(s['upload_rate_s']),
                        }
                    )

                if 'import_api' in data:
                    ia = data['import_api']
                    # Check if import_api section exists
                    if 'import_api' not in config:
                        # Create the section at the end of the file
                        config_text = config_text.rstrip() + '\n\n# Import API for automatic sonde detection\nimport_api:\n  enabled: false\n  url: api.opnwx.de\n  check_interval_s: 300\n  lat: 0.0\n  lon: 0.0\n  distance_km: 500\n  time_range_minutes: 240\n  sonde_type: all\n  max_sondes: 4\n'
                        # Re-parse to get updated config
                        config = yaml.safe_load(config_text)
                    
                    # Now update with actual values
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['import_api'],
                        {
                            'enabled': bool(ia['enabled']),
                            'url': str(ia['url']),
                            'check_interval_s': int(ia['check_interval_s']),
                            'lat': float(ia['lat']),
                            'lon': float(ia['lon']),
                            'distance_km': int(ia['distance_km']),
                            'time_range_minutes': int(ia['time_range_minutes']),
                            'sonde_type': str(ia['sonde_type']),
                            'max_sondes': int(ia['max_sondes']),
                        }
                    )

                if 'station' in data:
                    st = data['station']
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['station'],
                        {
                            'callsign': str(st['callsign']).strip(),
                            'upload_position': bool(st['upload_position']),
                        }
                    )
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['sdr'],
                        {'type': str(st['sdr_type']).strip().lower()}
                    )
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['receivers'],
                        {
                            'max_concurrent': int(st['max_concurrent']),
                            'scan_interval': int(st['scan_interval']),
                            'bandwidth': int(st['bandwidth']),
                            'min_signal_strength': float(st['min_signal_strength']),
                        }
                    )
                    config_text = self._update_mapping_keys_in_text(
                        config_text,
                        ['detection'],
                        {'use_dft_detect': bool(st['use_dft_detect'])}
                    )

                if 'fixed_channels' in data:
                    channels = data.get('fixed_channels') or []
                    normalized = []
                    for ch in channels:
                        try:
                            freq = float(ch.get('frequency', 0))
                            stype = str(ch.get('type', '')).strip()
                            if freq > 0 and stype:
                                entry = {
                                    'frequency': round(freq, 3), 
                                    'type': stype
                                }
                                # Add optional fields
                                if 'enabled' in ch:
                                    entry['enabled'] = bool(ch.get('enabled', False))
                                if 'rx_scan' in ch:
                                    entry['rx_scan'] = bool(ch.get('rx_scan', False))
                                if 'receiver_device' in ch:
                                    device = str(ch.get('receiver_device', '')).strip()
                                    if device:
                                        entry['receiver_device'] = device
                                normalized.append(entry)
                        except Exception:
                            continue
                    config_text = self._update_inline_fixed_channels(config_text, normalized)

                with open(config_path, 'w', encoding='utf-8') as f:
                    f.write(config_text)

                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Error saving config sections: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/preview_import_api', methods=['POST'])
        def preview_import_api():
            """Preview available sondes using Import API settings (no config save)."""
            try:
                from ..import_api.sonde_api_client import SondeApiClient
                
                # Get preview parameters from request
                params = request.get_json() or {}
                
                # Create temporary config for preview
                preview_config = {
                    'enabled': True,  # Enable for preview
                    'url': params.get('url', 'api.opnwx.de'),
                    'check_interval_s': params.get('check_interval_s', 300),
                    'lat': float(params.get('lat', 0.0)),
                    'lon': float(params.get('lon', 0.0)),
                    'distance_km': int(params.get('distance_km', 500)),
                    'time_range_minutes': int(params.get('time_range_minutes', 240)),
                    'sonde_type': params.get('sonde_type', 'all'),
                    'max_sondes': int(params.get('max_sondes', 4)),
                }
                
                # Create temporary API client
                api_client = SondeApiClient(preview_config)
                
                # Fetch sondes immediately
                sondes = api_client.fetch_sondes()
                
                return jsonify({
                    'success': True,
                    'sondes': sondes,
                    'count': len(sondes),
                })
                
            except Exception as e:
                self.logger.error(f"Error previewing Import API: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/kindle/dashboard_touch.png')
        def get_kindle_dashboard_touch():
            """Generate Kindle Touch dashboard image (600x800 grayscale PNG)."""
            try:
                # Test imports first
                try:
                    from ..kindle.dashboard_generator import generate_dashboard_image
                except ImportError as ie:
                    self.logger.error(f"Failed to import dashboard_generator: {ie}", exc_info=True)
                    return Response(f"Import error: {ie}", status=500)
                
                # Gather receiver status
                try:
                    receivers = self._get_receiver_status()
                    self.logger.info(f"Kindle dashboard: Got {len(receivers)} receivers")
                except Exception as e:
                    self.logger.error(f"Error getting receiver status: {e}", exc_info=True)
                    receivers = []
                
                # Gather active sondes
                try:
                    sondes = self._get_active_sondes_for_dashboard()
                    self.logger.info(f"Kindle dashboard: Got {len(sondes)} sondes")
                except Exception as e:
                    self.logger.error(f"Error getting sondes: {e}", exc_info=True)
                    sondes = []
                
                # Match sondes to receivers (add sonde_serial to decoding receivers)
                for rx in receivers:
                    if rx.get('state') == 'DECODING':
                        freq_hz = rx.get('frequency')
                        if freq_hz:
                            for s in sondes:
                                if abs(s.get('frequency', 0) - freq_hz) < 50000:  # 50 kHz tolerance
                                    rx['sonde_serial'] = s.get('serial', '')
                                    break
                
                # Gather system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station callsign from config
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_dashboard_image(
                        device_type='touch',
                        station_name=station_name,
                        receivers=receivers,
                        sondes=sondes,
                        system_info=system_info,
                        version=__version__
                    )
                except Exception as e:
                    self.logger.error(f"Error in generate_dashboard_image: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating Kindle Touch dashboard: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/kindle/dashboard_pw.png')
        def get_kindle_dashboard_paperwhite():
            """Generate Kindle Paperwhite dashboard image (758x1024 grayscale PNG)."""
            try:
                # Test imports first
                try:
                    from ..kindle.dashboard_generator import generate_dashboard_image
                except ImportError as ie:
                    self.logger.error(f"Failed to import dashboard_generator: {ie}", exc_info=True)
                    return Response(f"Import error: {ie}", status=500)
                
                # Gather receiver status
                try:
                    receivers = self._get_receiver_status()
                    self.logger.info(f"Kindle dashboard: Got {len(receivers)} receivers")
                except Exception as e:
                    self.logger.error(f"Error getting receiver status: {e}", exc_info=True)
                    receivers = []
                
                # Gather active sondes
                try:
                    sondes = self._get_active_sondes_for_dashboard()
                    self.logger.info(f"Kindle dashboard: Got {len(sondes)} sondes")
                except Exception as e:
                    self.logger.error(f"Error getting sondes: {e}", exc_info=True)
                    sondes = []
                
                # Match sondes to receivers (add sonde_serial to decoding receivers)
                for rx in receivers:
                    if rx.get('state') == 'DECODING':
                        freq_hz = rx.get('frequency')
                        if freq_hz:
                            for s in sondes:
                                if abs(s.get('frequency', 0) - freq_hz) < 50000:  # 50 kHz tolerance
                                    rx['sonde_serial'] = s.get('serial', '')
                                    break
                
                # Gather system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station callsign from config
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_dashboard_image(
                        device_type='paperwhite',
                        station_name=station_name,
                        receivers=receivers,
                        sondes=sondes,
                        system_info=system_info,
                        version=__version__
                    )
                except Exception as e:
                    self.logger.error(f"Error in generate_dashboard_image: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating Kindle Paperwhite dashboard: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/kindle/receiver/<receiver_id>/touch.png')
        def get_kindle_receiver_detail_touch(receiver_id):
            """Generate detailed Kindle Touch dashboard for a specific receiver."""
            try:
                from ..kindle.dashboard_generator import generate_receiver_detail_image
                
                # Get receiver status
                try:
                    receivers = self._get_receiver_status()
                    receiver = None
                    for rx in receivers:
                        if rx.get('device_id') == receiver_id:
                            receiver = rx
                            break
                    
                    if not receiver:
                        return Response(f"Receiver {receiver_id} not found", status=404)
                    
                    # Ensure spectrum data is included
                    if receiver.get('state') == 'SCANNING' and not receiver.get('spectrum'):
                        if hasattr(self.decoder_manager, 'get_spectrum_for_receiver'):
                            try:
                                spectrum = self.decoder_manager.get_spectrum_for_receiver(f"rtlsdr:{receiver_id}")
                                if spectrum:
                                    receiver['spectrum'] = spectrum
                            except Exception as e:
                                self.logger.debug(f"Could not get spectrum for {receiver_id}: {e}")
                    
                except Exception as e:
                    self.logger.error(f"Error getting receiver status: {e}", exc_info=True)
                    return Response(f"Error getting receiver status: {e}", status=500)
                
                # Get sonde if decoding
                sonde = None
                telemetry_history = None
                if receiver.get('state') == 'DECODING':
                    try:
                        sondes = self._get_active_sondes_for_dashboard()
                        # Find sonde on this receiver's frequency
                        freq_hz = receiver.get('frequency')
                        if freq_hz:
                            for s in sondes:
                                if abs(s.get('frequency', 0) - freq_hz) < 50000:  # 50 kHz tolerance
                                    sonde = s
                                    # Get telemetry history for this sonde
                                    serial = s.get('serial')
                                    if serial and serial in self.sondes:
                                        telemetry_history = self.sondes[serial][-100:]  # Last 100 points
                                    break
                    except Exception as e:
                        self.logger.error(f"Error getting sonde: {e}", exc_info=True)
                
                # Get system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station name
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_receiver_detail_image(
                        device_type='touch',
                        station_name=station_name,
                        receiver=receiver,
                        sonde=sonde,
                        system_info=system_info,
                        version=__version__,
                        telemetry_history=telemetry_history
                    )
                except Exception as e:
                    self.logger.error(f"Error generating receiver detail: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating receiver detail: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/kindle/receiver/<receiver_id>/pw.png')
        def get_kindle_receiver_detail_paperwhite(receiver_id):
            """Generate detailed Kindle Paperwhite dashboard for a specific receiver."""
            try:
                from ..kindle.dashboard_generator import generate_receiver_detail_image
                
                # Get receiver status
                try:
                    receivers = self._get_receiver_status()
                    receiver = None
                    for rx in receivers:
                        if rx.get('device_id') == receiver_id:
                            receiver = rx
                            break
                    
                    if not receiver:
                        return Response(f"Receiver {receiver_id} not found", status=404)
                    
                    # Ensure spectrum data is included
                    if receiver.get('state') == 'SCANNING' and not receiver.get('spectrum'):
                        if hasattr(self.decoder_manager, 'get_spectrum_for_receiver'):
                            try:
                                spectrum = self.decoder_manager.get_spectrum_for_receiver(f"rtlsdr:{receiver_id}")
                                if spectrum:
                                    receiver['spectrum'] = spectrum
                            except Exception as e:
                                self.logger.debug(f"Could not get spectrum for {receiver_id}: {e}")
                    
                except Exception as e:
                    self.logger.error(f"Error getting receiver status: {e}", exc_info=True)
                    return Response(f"Error getting receiver status: {e}", status=500)
                
                # Get sonde if decoding
                sonde = None
                telemetry_history = None
                if receiver.get('state') == 'DECODING':
                    try:
                        sondes = self._get_active_sondes_for_dashboard()
                        # Find sonde on this receiver's frequency
                        freq_hz = receiver.get('frequency')
                        if freq_hz:
                            for s in sondes:
                                if abs(s.get('frequency', 0) - freq_hz) < 50000:  # 50 kHz tolerance
                                    sonde = s
                                    # Get telemetry history for this sonde
                                    serial = s.get('serial')
                                    if serial and serial in self.sondes:
                                        telemetry_history = self.sondes[serial][-100:]  # Last 100 points
                                    break
                    except Exception as e:
                        self.logger.error(f"Error getting sonde: {e}", exc_info=True)
                
                # Get system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station name
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_receiver_detail_image(
                        device_type='paperwhite',
                        station_name=station_name,
                        receiver=receiver,
                        sonde=sonde,
                        system_info=system_info,
                        version=__version__,
                        telemetry_history=telemetry_history
                    )
                except Exception as e:
                    self.logger.error(f"Error generating receiver detail: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating receiver detail: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/kindle/sonde/<sonde_serial>/touch.png')
        def get_kindle_sonde_detail_touch(sonde_serial):
            """Generate detailed Kindle Touch dashboard for a specific sonde."""
            try:
                from ..kindle.dashboard_generator import generate_receiver_detail_image
                
                # Get sonde by serial
                sonde = None
                try:
                    # First try active sondes
                    sondes = self._get_active_sondes_for_dashboard()
                    for s in sondes:
                        if s.get('serial') == sonde_serial:
                            sonde = s
                            break
                    
                    # If not in active sondes, try to construct from historical data in memory
                    if not sonde and sonde_serial in self.sondes and len(self.sondes[sonde_serial]) > 0:
                        # Get last telemetry point
                        last_telem = self.sondes[sonde_serial][-1]
                        
                        # Construct sonde dict from historical data
                        sonde = {
                            'serial': sonde_serial,
                            'type': last_telem.get('type', 'Unknown'),
                            'frequency': last_telem.get('frequency', 0) * 1e6 if last_telem.get('frequency', 0) < 1000 else last_telem.get('frequency', 0),
                            'latitude': last_telem.get('lat', 0),
                            'longitude': last_telem.get('lon', 0),
                            'altitude': last_telem.get('alt', 0),
                            'velocity_h': last_telem.get('vel_h', 0),
                            'velocity_v': last_telem.get('vel_v', 0),
                            'heading': last_telem.get('heading', 0),
                            'rssi': last_telem.get('rssi', 0),
                            'snr': last_telem.get('snr', 0),
                            'sats': last_telem.get('sats', 0),
                            'battery': last_telem.get('batt', 0),
                            'frame': last_telem.get('frame', 0),
                            'last_update': time.time(),  # Current time as fallback
                        }
                        self.logger.info(f"Kindle view: Using in-memory historical data for sonde {sonde_serial}")
                    
                    # If still not found, try loading from log files
                    if not sonde:
                        sonde = self._load_sonde_from_logs(sonde_serial)
                    
                    if not sonde:
                        return Response(f"Sonde {sonde_serial} not found (not active, not in memory, no log files)", status=404)
                    
                except Exception as e:
                    self.logger.error(f"Error getting sonde: {e}", exc_info=True)
                    return Response(f"Error getting sonde: {e}", status=500)
                
                # Find receiver decoding this sonde
                receiver = None
                try:
                    receivers = self._get_receiver_status()
                    sonde_freq = sonde.get('frequency', 0)
                    for rx in receivers:
                        if rx.get('state') == 'DECODING':
                            rx_freq = rx.get('frequency', 0)
                            if abs(sonde_freq - rx_freq) < 50000:  # 50 kHz tolerance
                                receiver = rx
                                receiver['sonde_serial'] = sonde_serial
                                break
                    
                    # If no receiver found, create a minimal one
                    if not receiver:
                        receiver = {
                            'device_id': 'Unknown',
                            'state': 'DECODING',
                            'frequency': sonde_freq,
                            'freq_label': f"{sonde_freq/1e6:.3f} MHz",
                            'sonde_type': sonde.get('type', 'Unknown'),
                            'sonde_serial': sonde_serial
                        }
                except Exception as e:
                    self.logger.error(f"Error getting receivers: {e}", exc_info=True)
                    # Create minimal receiver
                    receiver = {
                        'device_id': 'Unknown',
                        'state': 'DECODING',
                        'frequency': sonde.get('frequency', 0),
                        'freq_label': f"{sonde.get('frequency', 0)/1e6:.3f} MHz",
                        'sonde_type': sonde.get('type', 'Unknown'),
                        'sonde_serial': sonde_serial
                    }
                
                # Get telemetry history for this sonde
                telemetry_history = None
                log_receiver_name = None
                if sonde_serial in self.sondes and len(self.sondes[sonde_serial]) > 0:
                    telemetry_history = self.sondes[sonde_serial][-100:]  # Last 100 points
                elif sonde and sonde.get('serial') == sonde_serial:
                    # If sonde was loaded from log files, load full history too
                    log_dir = 'data/logs'
                    if os.path.exists(log_dir):
                        sonde_logs = []
                        for fname in os.listdir(log_dir):
                            if fname.startswith(f"{sonde_serial}-") and fname.endswith('.log'):
                                sonde_logs.append(os.path.join(log_dir, fname))
                        if sonde_logs:
                            logfile = sorted(sonde_logs)[-1]
                            telemetry_history = self._load_history_from_log(logfile, sonde_serial, sonde.get('type', 'Unknown'))
                            # Try to extract receiver name from log header
                            try:
                                with open(logfile, 'r', encoding='utf-8', errors='ignore') as f:
                                    for line in f:
                                        if line.startswith('Receiver:'):
                                            log_receiver_name = line.split(':', 1)[1].strip()
                                            break
                                        # Stop after first few lines (header only)
                                        if line.startswith('==='):
                                            break
                            except Exception:
                                pass
                            if telemetry_history:
                                self.logger.info(f"Loaded {len(telemetry_history)} history points from log for Kindle view")
                
                # Update receiver device_id from log if found (prioritize log receiver name for historical sondes)
                if log_receiver_name:
                    receiver['device_id'] = log_receiver_name
                    self.logger.info(f"Updated receiver device_id to {log_receiver_name} from log file")
                
                # Get system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station name
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_receiver_detail_image(
                        device_type='touch',
                        station_name=station_name,
                        receiver=receiver,
                        sonde=sonde,
                        system_info=system_info,
                        version=__version__,
                        telemetry_history=telemetry_history
                    )
                except Exception as e:
                    self.logger.error(f"Error generating sonde detail: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating sonde detail: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/kindle/sonde/<sonde_serial>/pw.png')
        def get_kindle_sonde_detail_paperwhite(sonde_serial):
            """Generate detailed Kindle Paperwhite dashboard for a specific sonde."""
            try:
                from ..kindle.dashboard_generator import generate_receiver_detail_image
                
                # Get sonde by serial
                sonde = None
                try:
                    # First try active sondes
                    sondes = self._get_active_sondes_for_dashboard()
                    for s in sondes:
                        if s.get('serial') == sonde_serial:
                            sonde = s
                            break
                    
                    # If not in active sondes, try to construct from historical data in memory
                    if not sonde and sonde_serial in self.sondes and len(self.sondes[sonde_serial]) > 0:
                        # Get last telemetry point
                        last_telem = self.sondes[sonde_serial][-1]
                        
                        # Construct sonde dict from historical data
                        sonde = {
                            'serial': sonde_serial,
                            'type': last_telem.get('type', 'Unknown'),
                            'frequency': last_telem.get('frequency', 0) * 1e6 if last_telem.get('frequency', 0) < 1000 else last_telem.get('frequency', 0),
                            'latitude': last_telem.get('lat', 0),
                            'longitude': last_telem.get('lon', 0),
                            'altitude': last_telem.get('alt', 0),
                            'velocity_h': last_telem.get('vel_h', 0),
                            'velocity_v': last_telem.get('vel_v', 0),
                            'heading': last_telem.get('heading', 0),
                            'rssi': last_telem.get('rssi', 0),
                            'snr': last_telem.get('snr', 0),
                            'sats': last_telem.get('sats', 0),
                            'battery': last_telem.get('batt', 0),
                            'frame': last_telem.get('frame', 0),
                            'last_update': time.time(),  # Current time as fallback
                        }
                        self.logger.info(f"Kindle view: Using in-memory historical data for sonde {sonde_serial}")
                    
                    # If still not found, try loading from log files
                    if not sonde:
                        sonde = self._load_sonde_from_logs(sonde_serial)
                    
                    if not sonde:
                        return Response(f"Sonde {sonde_serial} not found (not active, not in memory, no log files)", status=404)
                    
                except Exception as e:
                    self.logger.error(f"Error getting sonde: {e}", exc_info=True)
                    return Response(f"Error getting sonde: {e}", status=500)
                
                # Find receiver decoding this sonde
                receiver = None
                try:
                    receivers = self._get_receiver_status()
                    sonde_freq = sonde.get('frequency', 0)
                    for rx in receivers:
                        if rx.get('state') == 'DECODING':
                            rx_freq = rx.get('frequency', 0)
                            if abs(sonde_freq - rx_freq) < 50000:  # 50 kHz tolerance
                                receiver = rx
                                receiver['sonde_serial'] = sonde_serial
                                break
                    
                    # If no receiver found, create a minimal one
                    if not receiver:
                        receiver = {
                            'device_id': 'Unknown',
                            'state': 'DECODING',
                            'frequency': sonde_freq,
                            'freq_label': f"{sonde_freq/1e6:.3f} MHz",
                            'sonde_type': sonde.get('type', 'Unknown'),
                            'sonde_serial': sonde_serial
                        }
                except Exception as e:
                    self.logger.error(f"Error getting receivers: {e}", exc_info=True)
                    # Create minimal receiver
                    receiver = {
                        'device_id': 'Unknown',
                        'state': 'DECODING',
                        'frequency': sonde.get('frequency', 0),
                        'freq_label': f"{sonde.get('frequency', 0)/1e6:.3f} MHz",
                        'sonde_type': sonde.get('type', 'Unknown'),
                        'sonde_serial': sonde_serial
                    }
                
                # Get telemetry history for this sonde
                telemetry_history = None
                log_receiver_name = None
                if sonde_serial in self.sondes and len(self.sondes[sonde_serial]) > 0:
                    telemetry_history = self.sondes[sonde_serial][-100:]  # Last 100 points
                elif sonde and sonde.get('serial') == sonde_serial:
                    # If sonde was loaded from log files, load full history too
                    log_dir = 'data/logs'
                    if os.path.exists(log_dir):
                        sonde_logs = []
                        for fname in os.listdir(log_dir):
                            if fname.startswith(f"{sonde_serial}-") and fname.endswith('.log'):
                                sonde_logs.append(os.path.join(log_dir, fname))
                        if sonde_logs:
                            logfile = sorted(sonde_logs)[-1]
                            telemetry_history = self._load_history_from_log(logfile, sonde_serial, sonde.get('type', 'Unknown'))
                            # Try to extract receiver name from log header
                            try:
                                with open(logfile, 'r', encoding='utf-8', errors='ignore') as f:
                                    for line in f:
                                        if line.startswith('Receiver:'):
                                            log_receiver_name = line.split(':', 1)[1].strip()
                                            break
                                        # Stop after first few lines (header only)
                                        if line.startswith('==='):
                                            break
                            except Exception:
                                pass
                            if telemetry_history:
                                self.logger.info(f"Loaded {len(telemetry_history)} history points from log for Kindle view")
                
                # Update receiver device_id from log if found (prioritize log receiver name for historical sondes)
                if log_receiver_name:
                    receiver['device_id'] = log_receiver_name
                    self.logger.info(f"Updated receiver device_id to {log_receiver_name} from log file")
                
                # Get system info
                try:
                    system_info = self._get_system_info()
                except Exception as e:
                    self.logger.error(f"Error getting system info: {e}", exc_info=True)
                    system_info = {'uptime_seconds': 0, 'cpu_percent': 0, 'memory_percent': 0}
                
                # Get station name
                station_callsign = self.config.get('station', {}).get('callsign', 'OpenWXSDR')
                station_name = f"OpenWX ● SDR - {station_callsign} Gateway"
                
                # Generate image
                try:
                    image_bytes = generate_receiver_detail_image(
                        device_type='paperwhite',
                        station_name=station_name,
                        receiver=receiver,
                        sonde=sonde,
                        system_info=system_info,
                        version=__version__,
                        telemetry_history=telemetry_history
                    )
                except Exception as e:
                    self.logger.error(f"Error generating sonde detail: {e}", exc_info=True)
                    return Response(f"Image generation error: {e}", status=500)
                
                return Response(image_bytes, mimetype='image/png')
                
            except Exception as e:
                self.logger.error(f"Error generating sonde detail: {e}", exc_info=True)
                return Response(f"Error: {e}", status=500)

        @self.app.route('/api/service_status')
        def get_service_status():
            """Return OPENWXSDR systemd status for the Service Status modal."""
            try:
                status_info = self._get_service_status_info(lines=8)
                host_info = self._get_host_info()
                mem_disk_info = self._get_memory_disk_info()

                return jsonify({
                    'success': True,
                    **status_info,
                    **host_info,
                    **mem_disk_info,
                    'version': __version__,
                    'build_date': __build_date__,
                    'station': self.config.get('station', {}).get('callsign', ''),
                })
            except Exception as e:
                self.logger.error(f"Error reading service status: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/service_console')
        def get_service_console():
            """Return OPENWXSDR systemd console log for the console modal."""
            try:
                status_info = self._get_service_status_info(lines=80)
                return jsonify({
                    'success': True,
                    'unit': status_info.get('unit', 'openwxsdr.service'),
                    'console_status': status_info.get('console_status', ''),
                    'summary': status_info.get('summary', ''),
                    'active': status_info.get('active', 'unknown'),
                })
            except Exception as e:
                self.logger.error(f"Error reading service console: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/service_control', methods=['POST'])
        def service_control():
            """Control OPENWXSDR systemd unit (stop/restart)."""
            try:
                data = request.get_json() or {}
                action = str(data.get('action', '')).strip().lower()
                if action not in ('stop', 'restart'):
                    return jsonify({'success': False, 'error': 'Invalid action'})

                cmd = ['sudo', 'systemctl', action, 'openwxsdr.service']
                subprocess.run(cmd, check=False)
                return jsonify({'success': True, 'action': action})
            except Exception as e:
                self.logger.error(f"Error controlling service: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/system_config_check')
        def get_system_config_check():
            """Aggregated installation / configuration diagnostics for the
            'System configuration' modal (install + softchain + config state)."""
            try:
                return jsonify({'success': True, **self._system_config_check()})
            except Exception as e:
                self.logger.error(f"Error in system_config_check: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/logfiles')
        def get_logfiles():
            """Get list of log files"""
            try:
                log_dir = 'data/logs'
                if not os.path.exists(log_dir):
                    return jsonify({'files': []})
                
                files = sorted(
                    [f for f in os.listdir(log_dir) if f.endswith('.log') or f.endswith('.txt')],
                    reverse=True
                )
                # Authoritative per-file sonde type (header + resolved subtype),
                # so the UI shows the correct type (incl. DFM17 etc.) instead of
                # guessing from the serial. Only for sonde logs (skip activity).
                types = {}
                for fn in files:
                    if fn.startswith('openwxsdr_') or not fn.endswith('.log'):
                        continue
                    t = self._logfile_type(os.path.join(log_dir, fn))
                    if t:
                        types[fn] = t
                return jsonify({'files': files, 'types': types})
            except Exception as e:
                self.logger.error(f"Error listing logfiles: {e}")
                return jsonify({'files': [], 'error': str(e)})
        
        @self.app.route('/api/logfile/<filename>')
        def get_logfile(filename):
            """Get content of a log file"""
            try:
                log_dir = 'data/logs'
                # Security: prevent directory traversal
                if '..' in filename or '/' in filename or '\\' in filename:
                    return "Invalid filename", 400
                
                filepath = os.path.join(log_dir, filename)
                if not os.path.exists(filepath):
                    return "File not found", 404
                
                with open(filepath, 'r') as f:
                    content = f.read()
                
                return content, 200, {'Content-Type': 'text/plain'}
            except Exception as e:
                self.logger.error(f"Error reading logfile: {e}")
                return str(e), 500
        
        @self.app.route('/api/logfile/<filename>/history')
        def get_logfile_history(filename):
            """Parse logfile and return historical telemetry data for statistics charts"""
            try:
                log_dir = 'data/logs'
                # Security: prevent directory traversal
                if '..' in filename or '/' in filename or '\\' in filename:
                    return jsonify({'error': 'Invalid filename'}), 400
                
                filepath = os.path.join(log_dir, filename)
                if not os.path.exists(filepath):
                    return jsonify({'error': 'File not found'}), 404
                
                # Extract serial from filename: SERIAL-YYYYMMDD-HHMMSS.log
                match = re.match(r'^(.+?)-(\d{8})-(\d{6})\.log$', filename)
                if not match:
                    return jsonify({'error': 'Invalid logfile format'}), 400
                
                serial = match.group(1)
                
                # Parse logfile
                frames = []
                with open(filepath, 'r') as f:
                    lines = f.readlines()
                
                current_frame = {}
                for line in lines:
                    line = line.strip()
                    
                    # New frame starts with "Frame X - timestamp"
                    if line.startswith('Frame '):
                        # Save previous frame if it has data
                        if current_frame and current_frame.get('timestamp'):
                            frames.append(current_frame)
                        
                        # Start new frame
                        parts = line.split(' - ', 1)
                        if len(parts) == 2:
                            current_frame = {'timestamp': parts[1]}
                        else:
                            current_frame = {}
                    
                    # Parse telemetry fields
                    elif line.startswith('Position:'):
                        # Position: 48.12345, 11.67890
                        try:
                            coords = line.split(':', 1)[1].strip()
                            lat, lon = coords.split(',')
                            current_frame['lat'] = float(lat.strip())
                            current_frame['lon'] = float(lon.strip())
                        except:
                            pass
                    
                    elif line.startswith('Elevation:'):
                        # Elevation: 12.3°  (gateway->sonde look angle)
                        try:
                            el_str = line.split(':', 1)[1].strip().replace('°', '')
                            current_frame['elevation'] = float(el_str)
                        except (ValueError, IndexError):
                            pass

                    elif line.startswith('Altitude:'):
                        # Altitude: 12345.0 m
                        try:
                            alt_str = line.split(':', 1)[1].strip().replace(' m', '')
                            current_frame['alt'] = float(alt_str)
                        except:
                            pass
                    
                    elif line.startswith('Velocity H/V:'):
                        # Velocity H/V: 5.2/3.4 m/s
                        try:
                            vel_str = line.split(':', 1)[1].strip().replace(' m/s', '')
                            vel_h, vel_v = vel_str.split('/')
                            current_frame['vel_h'] = float(vel_h.strip())
                            current_frame['vel_v'] = float(vel_v.strip())
                        except:
                            pass
                    
                    elif line.startswith('SNR:'):
                        # SNR: 15.3 dB
                        try:
                            snr_str = line.split(':', 1)[1].strip().replace(' dB', '')
                            current_frame['snr'] = float(snr_str)
                        except:
                            pass
                    
                    elif line.startswith('Frequency:'):
                        # Frequency: 405.700 MHz
                        try:
                            freq_str = line.split(':', 1)[1].strip().replace(' MHz', '')
                            current_frame['frequency'] = float(freq_str)
                        except:
                            pass
                    
                    elif line.startswith('RSSI:'):
                        # RSSI: -92.5 dB
                        try:
                            rssi_str = line.split(':', 1)[1].strip().replace(' dB', '')
                            current_frame['rssi'] = float(rssi_str)
                        except:
                            pass
                    
                    elif line.startswith('Satellites:'):
                        # Satellites: 8
                        try:
                            sat_str = line.split(':', 1)[1].strip()
                            current_frame['sats'] = int(sat_str)
                        except:
                            pass
                    
                    elif line.startswith('Battery:'):
                        # Battery: 3.45 V
                        try:
                            batt_str = line.split(':', 1)[1].strip().replace(' V', '')
                            current_frame['battery'] = float(batt_str)
                        except:
                            pass
                
                # Don't forget the last frame
                if current_frame and current_frame.get('timestamp'):
                    frames.append(current_frame)
                
                # Ensure all frames have the expected fields (with None if missing)
                _st = self.config.get('station', {})
                for frame in frames:
                    frame.setdefault('alt', None)
                    frame.setdefault('vel_v', None)
                    frame.setdefault('vel_h', None)
                    frame.setdefault('rssi', None)
                    frame.setdefault('snr', None)
                    frame.setdefault('sats', None)
                    frame.setdefault('battery', None)
                    # Elevation (prefer the logged value; compute for older logs)
                    # + slant distance for the Elevation graph's second line.
                    if frame.get('lat') is not None:
                        _look = self._look_angles(
                            _st.get('lat'), _st.get('lon'), _st.get('alt', 0),
                            frame.get('lat'), frame.get('lon'), frame.get('alt'))
                        if _look:
                            if frame.get('elevation') is None:
                                frame['elevation'] = round(_look['elevation_deg'], 1)
                            frame['distance'] = round(_look['slant_km'], 2)
                    frame.setdefault('elevation', None)
                    frame.setdefault('distance', None)
                
                # Server-side LTTB decimation for fast charts (default ~1000 pts,
                # override with ?max_points=; 0 = no decimation).
                max_points = request.args.get('max_points', default=1000, type=int)
                total = len(frames)
                frames = self._decimate_frames(frames, max_points)
                return jsonify({
                    'serial': serial,
                    'filename': filename,
                    'sonde_type': self._logfile_type(filepath),
                    'frames': frames,
                    'count': len(frames),
                    'total_frames': total
                })
                
            except Exception as e:
                self.logger.error(f"Error parsing logfile history: {e}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/logfile/<filename>/rx_statistics')
        def get_rx_statistics(filename):
            """Parse activity logfile and return receiver statistics (synchronous, no progress)"""
            try:
                days = request.args.get('days', type=int)
                return jsonify(self._compute_rx_statistics(filename, days=days))
            except _RxStatsError as e:
                return jsonify({'error': str(e)}), e.status_code
            except Exception as e:
                self.logger.error(f"Error parsing RX statistics: {e}")
                import traceback
                traceback.print_exc()
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/logfile/<filename>/rx_statistics/start', methods=['POST'])
        def start_rx_statistics_job(filename):
            """Kick off RX statistics computation in a background thread and return a job_id
            the client can poll via /api/rx_statistics_job/<job_id> for per-file progress —
            scanning every sonde logfile can take a while on a gateway with a long history."""
            days = request.args.get('days', type=int)
            job_id = uuid.uuid4().hex
            with self._rx_stats_jobs_lock:
                # Opportunistic cleanup of old finished/abandoned jobs so this dict doesn't
                # grow unbounded on a gateway whose web UI is polled/refreshed a lot.
                now = time.time()
                for stale_id in [jid for jid, j in self._rx_stats_jobs.items()
                                  if now - j.get('created_at', now) > 300]:
                    del self._rx_stats_jobs[stale_id]

                self._rx_stats_jobs[job_id] = {
                    'current': 0, 'total': 0, 'filename': '',
                    'done': False, 'error': None, 'result': None,
                    'created_at': now,
                }
            threading.Thread(
                target=self._run_rx_statistics_job, args=(job_id, filename, days), daemon=True
            ).start()
            return jsonify({'job_id': job_id})

        @self.app.route('/api/rx_statistics_job/<job_id>')
        def get_rx_statistics_job(job_id):
            """Poll progress/result of a background RX statistics job."""
            with self._rx_stats_jobs_lock:
                job = self._rx_stats_jobs.get(job_id)
                if job is None:
                    return jsonify({'error': 'Job not found'}), 404
                response = {
                    'current': job['current'],
                    'total': job['total'],
                    'filename': job['filename'],
                    'done': job['done'],
                }
                if job['done']:
                    if job['error']:
                        response['error'] = job['error']
                    else:
                        response['result'] = job['result']
            return jsonify(response)

        @self.app.route('/api/export_action_log')
        def export_action_log():
            """Export the structured action log for download"""
            try:
                if not os.path.exists(self.action_log_path):
                    return "Log file not found", 404
                
                return send_file(
                    self.action_log_path,
                    mimetype='application/json',
                    as_attachment=True,
                    download_name=os.path.basename(self.action_log_path)
                )
            except Exception as e:
                self.logger.error(f"Error exporting action log: {e}")
                return str(e), 500

    def _run_rx_statistics_job(self, job_id: str, filename: str, days: Optional[int] = None):
        """Background-thread target for /api/logfile/<filename>/rx_statistics/start."""
        def progress_cb(current, total, current_filename):
            with self._rx_stats_jobs_lock:
                job = self._rx_stats_jobs.get(job_id)
                if job is not None:
                    job['current'] = current
                    job['total'] = total
                    job['filename'] = current_filename

        try:
            result = self._compute_rx_statistics(filename, days=days, progress_cb=progress_cb)
            with self._rx_stats_jobs_lock:
                job = self._rx_stats_jobs.get(job_id)
                if job is not None:
                    job['done'] = True
                    job['result'] = result
        except Exception as e:
            self.logger.error(f"Error in RX statistics job {job_id}: {e}")
            with self._rx_stats_jobs_lock:
                job = self._rx_stats_jobs.get(job_id)
                if job is not None:
                    job['done'] = True
                    job['error'] = str(e)

    def _static_js_version(self) -> int:
        """mtime of rx-statistics-common.js, used as a cache-busting query string on the
        <script> tag. Without this, browsers happily keep serving a stale cached copy of
        this file after a deploy — the .html changes (so new markup like the range
        selector shows up), but clicks/selections silently keep running old JS logic that
        never sends the new request parameters, which looks exactly like "the server is
        ignoring my selection" even though the server-side code is correct."""
        try:
            return int(os.path.getmtime(os.path.join('static', 'js', 'rx-statistics-common.js')))
        except OSError:
            return 0

    def _load_rx_stats_cache(self):
        """Best-effort load of the persistent RX-statistics cache from disk."""
        try:
            with open(self._rx_stats_cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._rx_stats_activity_cache = data.get('activity', {})
            self._rx_stats_file_cache = data.get('files', {})
            self.logger.info(
                f"Loaded RX statistics cache: {len(self._rx_stats_file_cache)} sonde logfiles, "
                f"{len(self._rx_stats_activity_cache)} activity log(s)"
            )
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.warning(f"Failed to load RX statistics cache, starting fresh: {e}")

    def _save_rx_stats_cache(self):
        """Best-effort persist of the RX-statistics cache (atomic replace)."""
        try:
            with self._rx_stats_cache_lock:
                payload = {'activity': self._rx_stats_activity_cache, 'files': self._rx_stats_file_cache}
            tmp_path = self._rx_stats_cache_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp_path, self._rx_stats_cache_path)
        except Exception as e:
            self.logger.warning(f"Failed to save RX statistics cache: {e}")

    def _scan_sonde_logfile(self, log_path: str, need_first_alt: bool, need_last_alt: bool):
        """Single sequential pass over one sonde logfile: per-day frame counts, the sonde
        type header, and (if requested) the first/last altitude seen. Frame-day counting
        requires reading the whole file anyway, so first/last altitude are extracted from
        that same pass instead of the extra separate reads used previously."""
        frames_by_day = {}
        sonde_type = 'Unknown'
        first_alt = None
        last_alt = None
        in_frame = False
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('Sonde:') and '(' in line:
                        if sonde_type == 'Unknown':
                            try:
                                sonde_type = line.split('(')[1].split(')')[0].strip()
                            except Exception:
                                pass
                        continue
                    if line.startswith('Frame '):
                        in_frame = True
                        parts = line.split(' - ', 1)
                        if len(parts) == 2:
                            try:
                                dt = datetime.fromisoformat(parts[1].strip().rstrip('Z'))
                                if dt.year >= 2020:
                                    # (years < 2020 are excluded below in the caller's
                                    # merge step too; kept here so cached per-file results
                                    # never contain the glitch date in the first place)
                                    day_key = dt.strftime('%Y-%m-%d')
                                    frames_by_day[day_key] = frames_by_day.get(day_key, 0) + 1
                            except ValueError:
                                pass
                        continue
                    if in_frame and line.startswith('Altitude:'):
                        try:
                            alt = float(line.split(':')[1].replace('m', '').strip())
                            if need_first_alt and first_alt is None:
                                first_alt = alt
                            if need_last_alt:
                                last_alt = alt
                        except Exception:
                            pass
        except Exception as e:
            self.logger.warning(f"Error scanning sonde logfile {log_path}: {e}")
        return frames_by_day, sonde_type, first_alt, last_alt

    def _compute_rx_statistics(self, filename: str, days: Optional[int] = None, progress_cb=None) -> dict:
        """Parse an activity logfile and return receiver statistics.

        If progress_cb is given, it's called as progress_cb(current, total, filename)
        once per sonde logfile scanned, so a caller (e.g. a background job) can report
        "processing file X/Y" — scanning every sonde logfile for frame counts is the
        dominant cost on a gateway with a long tracking history.

        Historical sonde logfiles never change once a session is over, and the activity
        log only ever grows by appending — so both are cached (self._rx_stats_file_cache /
        self._rx_stats_activity_cache) and only new-or-changed files / new bytes get
        (re-)read on each call, instead of rescanning the entire history from scratch.

        If days is given, only data from the last `days` days is included in the result
        (and drives which sonde logfiles even need to be looked at) — this both narrows
        the returned charts to the requested range and, on a gateway with a long history,
        is the main lever for making an *uncached* first-ever request fast: a short range
        means far fewer serials/logfiles to touch.
        """
        log_dir = 'data/logs'
        # Security: prevent directory traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            raise _RxStatsError('Invalid filename', 400)

        filepath = os.path.join(log_dir, filename)
        if not os.path.exists(filepath):
            raise _RxStatsError('File not found', 404)

        # Must be an activity log
        if not filename.startswith('openwxsdr_'):
            raise _RxStatsError('Not an activity log', 400)

        # --- Parse JSON activity log, incrementally -------------------------------
        # The file is append-only (new events are only ever added at the end), so we
        # resume from the byte offset we stopped at last time and only parse new lines,
        # merging them onto the cached event lists from earlier calls.
        with self._rx_stats_cache_lock:
            cached_activity = self._rx_stats_activity_cache.get(filename)

        file_size = os.path.getsize(filepath)
        if cached_activity is not None and cached_activity.get('offset', 0) <= file_size:
            start_offset = cached_activity['offset']
            decoder_events = list(cached_activity.get('decoder_events', []))
            sonde_events = list(cached_activity.get('sonde_events', []))
            sonde_stopped_events = list(cached_activity.get('sonde_stopped_events', []))
            freq_to_sonde_type = dict(cached_activity.get('freq_to_sonde_type', {}))
        else:
            # No usable cache, or the file got shorter than our last-known offset
            # (e.g. rotated/truncated) — fall back to a full re-parse from scratch.
            start_offset = 0
            decoder_events = []
            sonde_events = []
            sonde_stopped_events = []
            freq_to_sonde_type = {}

        with open(filepath, 'rb') as f:
            f.seek(start_offset)
            committed_offset = start_offset
            for raw_line in f:
                if not raw_line.endswith(b'\n'):
                    # Partial line still being written — stop here and re-read it
                    # complete (from committed_offset) on the next call.
                    break
                committed_offset += len(raw_line)
                line = raw_line.decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event_type = event.get('action')  # Changed from 'event' to 'action'
                    timestamp = event.get('datetime')  # Changed from 'timestamp' to 'datetime'

                    # Decoder start/stop events
                    if event_type in ['decoder_start', 'decoder_stop']:
                        freq = event.get('data', {}).get('frequency_mhz')
                        stype = event.get('data', {}).get('sonde_type')

                        # Store frequency → sonde_type mapping from decoder_start.
                        # Key as a string (not float) so this dict round-trips cleanly
                        # through the JSON cache file without a key-type mismatch.
                        if event_type == 'decoder_start' and freq and stype:
                            freq_to_sonde_type[str(round(freq, 2))] = stype

                        decoder_events.append({
                            'timestamp': timestamp,
                            'event': event_type,
                            'frequency': freq,
                            'sonde_type': stype
                        })

                    # Sonde first frame events
                    elif event_type == 'sonde_first_frame':
                        data = event.get('data', {})
                        stype = data.get('sonde_type', '').strip()
                        freq = data.get('frequency_mhz')
                        serial = data.get('serial', '')

                        # If sonde_type is empty, try to determine from frequency mapping
                        if not stype and freq:
                            stype = freq_to_sonde_type.get(str(round(freq, 2)), '')

                        # If still empty, use serial pattern as fallback
                        # NOTE: Serial IDs no longer have M10-/M20-/DFM-/iMet- prefixes
                        if not stype and serial:
                            # M10/M20: Often contains hyphens like "310-2-02647"
                            if '-' in serial and serial[0].isdigit():
                                stype = 'M20'  # Default to M20 for hyphenated numeric serials
                            # DFM: 8 digits (no prefix anymore)
                            elif serial.isdigit() and len(serial) == 8:
                                stype = 'DFM'
                            # RS41: Starts with letter followed by 7-8 digits
                            elif serial[0].isalpha() and serial[1:].isdigit() and len(serial) in (8, 9):
                                stype = 'RS41'
                            # iMet: Often numeric or alphanumeric
                            elif serial.startswith('iMet') or serial.startswith('IMET'):
                                stype = 'iMet'
                            else:
                                stype = 'Unknown'

                        sonde_events.append({
                            'timestamp': timestamp,
                            'serial': serial,
                            'sonde_type': stype,
                            'frequency': freq,
                            'rssi': data.get('rssi'),
                            'snr': data.get('snr'),
                            'sats': data.get('sats'),
                            'alt': data.get('alt')  # Include altitude
                        })

                    # Sonde stopped events (for frame counts)
                    elif event_type == 'sonde_stopped':
                        data = event.get('data', {})
                        sonde_stopped_events.append({
                            'timestamp': timestamp,
                            'serial': data.get('serial'),
                            'total_frames': data.get('total_frames', 0)
                        })

                except json.JSONDecodeError:
                    continue

        # Retain at most ~110 days of event history in the cache — comfortably more than
        # the longest selectable range (3 months / ~92 days) the web UI ever requests, so
        # this bounds the cache's on-disk size indefinitely without affecting any range
        # actually shown. The underlying sonde logfiles on disk are untouched either way.
        cache_retention_cutoff = (datetime.now() - timedelta(days=110)).strftime('%Y-%m-%d %H:%M:%S')

        def _not_too_old(ts):
            return not ts or ts >= cache_retention_cutoff

        with self._rx_stats_cache_lock:
            self._rx_stats_activity_cache[filename] = {
                'offset': committed_offset,
                'decoder_events': [e for e in decoder_events if _not_too_old(e.get('timestamp'))],
                'sonde_events': [e for e in sonde_events if _not_too_old(e.get('timestamp'))],
                'sonde_stopped_events': [e for e in sonde_stopped_events if _not_too_old(e.get('timestamp'))],
                'freq_to_sonde_type': freq_to_sonde_type,
            }

        # --- Apply the requested time range (view-level filter over the full cached
        # history — the cache above always keeps ~110 days regardless of what's asked
        # for here, so switching the dropdown to a wider range never needs a re-scan). --
        if days is not None:
            cutoff_str = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

            def _in_range(ts):
                return bool(ts) and ts >= cutoff_str

            decoder_events = [e for e in decoder_events if _in_range(e.get('timestamp'))]
            sonde_events = [e for e in sonde_events if _in_range(e.get('timestamp'))]
            sonde_stopped_events = [e for e in sonde_stopped_events if _in_range(e.get('timestamp'))]

        # RSSI/sats values are derived from (already time-filtered) sonde_events rather
        # than accumulated during parsing, so they stay correct across cached/incremental
        # parses and different day-range selections without needing their own cache entry.
        rssi_values = [s['rssi'] for s in sonde_events if s.get('rssi') is not None]
        sats_values = [s['sats'] for s in sonde_events if s.get('sats') is not None]

        # Calculate statistics - count unique sondes by serial
        total_sondes = len(set(sonde['serial'] for sonde in sonde_events if sonde.get('serial')))
        avg_rssi = sum(rssi_values) / len(rssi_values) if rssi_values else 0
        avg_sats = sum(sats_values) / len(sats_values) if sats_values else 0

        # Group sondes by day for timeline (count unique sondes per day)
        sondes_by_day = {}
        for sonde in sonde_events:
            try:
                # Parse datetime in format: '2026-06-17 12:34:56'
                dt = datetime.strptime(sonde['timestamp'], '%Y-%m-%d %H:%M:%S')
                day_key = dt.strftime('%Y-%m-%d')
                if day_key not in sondes_by_day:
                    sondes_by_day[day_key] = set()
                sondes_by_day[day_key].add(sonde['serial'])
            except:
                pass

        # Convert sets to counts
        sonde_timeline = {day: len(serials) for day, serials in sondes_by_day.items()}

        # Calculate number of recorded days
        recorded_days = len(sondes_by_day)

        # Calculate max sondes per day with date
        max_sondes_per_day = 0
        max_sondes_date = None
        if sonde_timeline:
            max_day = max(sonde_timeline.items(), key=lambda x: (x[1], x[0]))  # Sort by count, then by date (latest)
            max_sondes_per_day = max_day[1]
            max_sondes_date = max_day[0]

        # 'total_frames_from_stopped' is a distinct, informational "completed
        # sessions" stat — kept exactly as before. It must NOT be used to build the
        # "Total Frames per Day" chart: 'sonde_stopped' events were only added to
        # the activity logger in a later software version, so older activity logs
        # have zero sonde_stopped events for weeks/months of otherwise-valid sonde
        # history. Relying on them for frames_by_day silently dropped every earlier
        # day from that chart while "Received Sondes per Day" (built from
        # 'sonde_first_frame' events, which always existed) kept showing the full
        # range. frames_by_day itself is computed below directly from each sonde's
        # logfile instead, so it always covers the same range.
        total_frames_from_stopped = sum(s.get('total_frames', 0) for s in sonde_stopped_events)
        frames_by_day = {}

        # Group sonde types by unique serials (not total events)
        sonde_types_serials = {}
        for sonde in sonde_events:
            stype = sonde.get('sonde_type', 'Unknown')
            serial = sonde.get('serial')
            if serial:
                if stype not in sonde_types_serials:
                    sonde_types_serials[stype] = set()
                sonde_types_serials[stype].add(serial)

        # Convert sets to counts
        sonde_types = {stype: len(serials) for stype, serials in sonde_types_serials.items()}

        # Get unique frequencies (rounded to 2 decimals) with dates
        from datetime import date as date_class
        today = date_class.today().strftime('%Y-%m-%d')
        frequencies_with_dates = {}
        for sonde in sonde_events:
            freq = sonde.get('frequency')
            timestamp = sonde.get('timestamp')
            if freq is not None and timestamp:
                freq_rounded = round(freq, 2)
                try:
                    dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
                    day_key = dt.strftime('%Y-%m-%d')
                    if freq_rounded not in frequencies_with_dates:
                        frequencies_with_dates[freq_rounded] = {'total': 0, 'today': 0}
                    frequencies_with_dates[freq_rounded]['total'] += 1
                    if day_key == today:
                        frequencies_with_dates[freq_rounded]['today'] += 1
                except:
                    pass

        # Get first frame altitude per sonde serial (for altitude chart)
        # Read ACTUAL first frame from sonde logfiles, not from activity log
        sonde_altitudes_dict = {}
        last_sonde_altitudes_dict = {}

        # Build set of unique serials from sonde_events
        unique_serials = set(s.get('serial') for s in sonde_events if s.get('serial'))

        if os.path.exists(log_dir):
            # CRITICAL: list the directory ONCE, not once per unique serial.
            # This endpoint re-listed data/logs inside the loop below for
            # every distinct sonde ever seen in the activity log — on a
            # gateway with a long tracking history (hundreds of serials),
            # that's hundreds of redundant directory listings and was the
            # main reason RX statistics got slower the longer a gateway
            # had been running.
            all_log_files = os.listdir(log_dir)

            # Resolve each serial's logfile list up front so total_log_files (used for
            # progress reporting) is known before the scan starts. Restricting to
            # unique_serials (already time-range filtered above) means a short range
            # naturally skips almost all of a large gateway's log history entirely.
            serial_logs_map = {}
            total_log_files = 0
            for serial in unique_serials:
                prefix = f"{serial}-"
                sonde_logs = [
                    os.path.join(log_dir, fname) for fname in all_log_files
                    if fname.startswith(prefix) and fname.endswith('.log')
                ]
                if sonde_logs:
                    serial_logs_map[serial] = sonde_logs
                    total_log_files += len(sonde_logs)

            file_index = 0
            for serial, sonde_logs in serial_logs_map.items():
                sorted_logs = sorted(sonde_logs)  # filename contains the session timestamp
                oldest_log = sorted_logs[0]
                newest_log = sorted_logs[-1]
                sonde_type = 'Unknown'

                for log_path in sorted_logs:
                    file_index += 1
                    if progress_cb is not None:
                        try:
                            progress_cb(file_index, total_log_files, os.path.basename(log_path))
                        except Exception:
                            pass

                    need_first_alt = (log_path == oldest_log)
                    need_last_alt = (log_path == newest_log)

                    try:
                        st = os.stat(log_path)
                        mtime, fsize = st.st_mtime, st.st_size
                    except OSError as e:
                        self.logger.warning(f"Error accessing sonde logfile {log_path}: {e}")
                        continue

                    with self._rx_stats_cache_lock:
                        cached = self._rx_stats_file_cache.get(log_path)

                    # A closed session's logfile never changes again, so an mtime+size
                    # match means the cached scan result is still accurate — skip the
                    # (potentially large) file read entirely. A currently-active sonde's
                    # newest logfile keeps growing between calls, so it naturally fails
                    # this check every time and gets rescanned fresh, without the closed
                    # sessions around it (typically almost all of them) paying that cost.
                    if cached is not None and cached.get('mtime') == mtime and cached.get('size') == fsize:
                        file_frames = cached.get('frames_by_day', {})
                        file_sonde_type = cached.get('sonde_type', 'Unknown')
                        first_alt = cached.get('first_alt')
                        last_alt = cached.get('last_alt')
                    else:
                        file_frames, file_sonde_type, first_alt, last_alt = self._scan_sonde_logfile(
                            log_path, need_first_alt=need_first_alt, need_last_alt=need_last_alt
                        )
                        with self._rx_stats_cache_lock:
                            self._rx_stats_file_cache[log_path] = {
                                'mtime': mtime, 'size': fsize,
                                'frames_by_day': file_frames,
                                'sonde_type': file_sonde_type,
                                'first_alt': first_alt,
                                'last_alt': last_alt,
                            }

                    for day, count in file_frames.items():
                        frames_by_day[day] = frames_by_day.get(day, 0) + count

                    if file_sonde_type and file_sonde_type != 'Unknown':
                        sonde_type = file_sonde_type

                    if need_first_alt and first_alt is not None:
                        sonde_altitudes_dict[serial] = {
                            'serial': serial, 'altitude': first_alt, 'sonde_type': sonde_type
                        }
                    if need_last_alt and last_alt is not None:
                        last_sonde_altitudes_dict[serial] = {
                            'serial': serial, 'altitude': last_alt, 'sonde_type': sonde_type
                        }

            # Persist the (possibly updated) file/activity cache for next time. Best-effort:
            # a failed save just means the next call re-scans a bit more, nothing is lost.
            self._save_rx_stats_cache()

        if days is not None:
            # Clip any day that leaked in from a session logfile spanning further back
            # than the requested range (e.g. a flight that started just before the
            # cutoff), so the chart's x-axis never extends past what was asked for.
            frames_by_day = {d: c for d, c in frames_by_day.items() if d >= cutoff_str[:10]}

        # Fallback: use altitude from activity log if logfile read failed
        # NOTE: Only fallback for FIRST frame altitude, not last
        # (last frame requires actual logfile data to be meaningful)
        for sonde in sonde_events:
            serial = sonde.get('serial')
            alt = sonde.get('alt')
            sonde_type = sonde.get('sonde_type', 'Unknown')
            if serial and alt is not None:
                # Add to first frame altitude if no logfile data
                if serial not in sonde_altitudes_dict:
                    sonde_altitudes_dict[serial] = {
                        'serial': serial,
                        'altitude': alt,
                        'sonde_type': sonde_type
                    }
                # DO NOT add to last frame altitude - only show sondes with logfiles
                # This prevents showing identical first/last values for sondes without logfiles

        # Convert to lists
        sonde_altitudes = list(sonde_altitudes_dict.values())
        last_sonde_altitudes = list(last_sonde_altitudes_dict.values())

        # Create RSSI timeline (RSSI values with timestamps)
        rssi_timeline = []
        for sonde in sonde_events:
            if sonde.get('rssi') is not None:
                rssi_timeline.append({
                    'timestamp': sonde.get('timestamp'),
                    'rssi': sonde.get('rssi'),
                    'serial': sonde.get('serial')
                })

        # Get today's frame count from frames_by_day (using 'today' calculated earlier)
        frames_today = frames_by_day.get(today, 0)

        return {
            'success': True,
            'decoder_events': decoder_events,
            'sonde_events': sonde_events,
            'sonde_timeline': sonde_timeline,
            'frames_timeline': frames_by_day,
            'rssi_values': rssi_values,
            'rssi_timeline': rssi_timeline,
            'sats_values': sats_values,
            'sonde_types': sonde_types,
            'frequencies': frequencies_with_dates,
            'sonde_altitudes': sonde_altitudes,
            'last_sonde_altitudes': last_sonde_altitudes,
            'statistics': {
                'total_sondes': total_sondes,
                'total_decoder_starts': len([e for e in decoder_events if e['event'] == 'decoder_start']),
                'avg_rssi': round(avg_rssi, 1),
                'avg_sats': round(avg_sats, 1),
                'total_frames': frames_today,
                'total_frames_from_stopped': total_frames_from_stopped,
                'recorded_days': recorded_days,
                'max_sondes_per_day': max_sondes_per_day,
                'max_sondes_date': max_sondes_date
            }
        }

    def _setup_action_logger(self):
        """Setup JSON action logger"""
        import json
        logger = logging.getLogger('ActionLog')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        
        # Remove existing handlers
        logger.handlers.clear()
        
        # JSON file handler with append mode
        handler = logging.FileHandler(self.action_log_path, mode='a')
        handler.setLevel(logging.INFO)
        
        # Custom formatter for JSON lines
        class JSONFormatter(logging.Formatter):
            def format(self, record):
                log_obj = {
                    'timestamp': record.created,
                    'datetime': self.formatTime(record, '%Y-%m-%d %H:%M:%S'),
                    'action': getattr(record, 'action', 'unknown'),
                    'data': getattr(record, 'data', {})
                }
                return json.dumps(log_obj)
        
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        return logger
    
    def _log_action(self, action: str, data: dict = None):
        """Log structured action to JSON log file"""
        try:
            record = self.action_logger.makeRecord(
                self.action_logger.name,
                logging.INFO,
                '',
                0,
                action,
                (),
                None
            )
            record.action = action
            record.data = data or {}
            self.action_logger.handle(record)
        except Exception as e:
            self.logger.error(f"Error logging action: {e}")
    
    def _apply_logging_levels(self):
        """Apply logging levels based on unified log_level setting.
        
        Supports three levels:
        - WARNING: Minimal logging (errors and warnings only)
        - INFO: Standard logging (info, warnings, errors)
        - DEBUG: Verbose logging (everything including debug messages)
        """
        try:
            # Get log level from config
            level_str = self.log_level if hasattr(self, 'log_level') else 'INFO'
            
            # Map string to logging level
            if level_str == 'DEBUG':
                level = logging.DEBUG
            elif level_str == 'WARNING':
                level = logging.WARNING
            else:  # INFO or unknown defaults to INFO
                level = logging.INFO
            
            # Set root logger and all application loggers
            logging.getLogger().setLevel(level)
            
            # Explicitly set main components to same level
            for logger_name in ['OpenWXSDR', 'WebUI', 'RTLSDRDeviceManager', 'Worker', 
                                'DftDetector', 'AudioPipeline', 'RS1729Decoder',
                                'SondeHubQueueOutput', 'MQTTOutput']:
                logging.getLogger(logger_name).setLevel(level)
            
            # For worker-specific loggers (Worker.NESDR001, etc.)
            for logger_name in logging.Logger.manager.loggerDict:
                if logger_name.startswith('Worker.'):
                    logging.getLogger(logger_name).setLevel(level)
            
            self.logger.info(f"Logging level set to: {level_str} (debug_mode={self.debug_mode})")
        except Exception as e:
            self.logger.error(f"Error applying logging levels: {e}")
    
    def set_components(self, spectrum_analyzer=None, decoder_manager=None, flux242_receiver=None,
                       ka9q_receiver=None, mqtt_output=None, sondehub_output=None):
        """Set references to other components for health monitoring"""
        self.spectrum_analyzer = spectrum_analyzer
        self.decoder_manager = decoder_manager
        self.flux242_receiver = flux242_receiver
        self.ka9q_receiver = ka9q_receiver
        self.mqtt_output = mqtt_output
        self.sondehub_output = sondehub_output
        self._connected_serials_cache: Set[str] = set()
        self._connected_serials_ts: float = 0

    def _get_mqtt_health(self) -> dict:
        """Return MQTT health status for WebUI."""
        if self.mqtt_output is None:
            return {'status': 'not_configured'}

        if hasattr(self.mqtt_output, 'get_status'):
            try:
                return self.mqtt_output.get_status()
            except Exception:
                return {'status': 'error'}

        return {'status': 'unknown'}

    def _get_sondehub_health(self) -> dict:
        """Return SondeHub health status for WebUI."""
        if self.sondehub_output is None:
            return {'status': 'not_configured'}

        if hasattr(self.sondehub_output, 'get_status'):
            try:
                return self.sondehub_output.get_status()
            except Exception:
                return {'status': 'error'}

        return {'status': 'unknown'}

    def _get_ephemeris_health(self) -> dict:
        """RS92 GPS-ephemeris download status for the System Health panel."""
        try:
            from ..sdr.ephemeris import status as _ephem_status
            return _ephem_status()
        except Exception:
            return {'enabled': False, 'available': False, 'state': 'disabled'}

    # ------------------------------------------------------------------ #
    #  System configuration diagnostics (System configuration modal)     #
    # ------------------------------------------------------------------ #
    def _bin_runs(self, path: str) -> bool:
        """True if an executable at `path` runs and emits any usage/help output
        (the field-broken dft_detect binary printed nothing at all)."""
        try:
            for args in (['--help'], []):
                r = subprocess.run([path] + args, capture_output=True,
                                   text=True, timeout=6)
                if (r.stdout or '') + (r.stderr or ''):
                    return True
            return False
        except Exception:
            return False

    def _decoder_dir(self) -> str:
        cfg = (self.config.get('decoders', {}) or {})
        d = str(cfg.get('rs1729_path') or './decoders/rs1729')
        return os.path.abspath(os.path.join(os.getcwd(), d))

    def _system_config_check(self) -> dict:
        """Collect the installation/config checks shown in the modal. Each entry
        is {label, status: ok|warn|fail|info, detail}. Never raises per-check —
        a failed probe becomes a 'warn'/'fail' row, not a 500."""
        cfg = self.config or {}
        dec_dir = self._decoder_dir()

        def entry(label, status, detail, kind=None, dot=None, sonde=None):
            e = {'label': label, 'status': status, 'detail': detail}
            if kind:
                e['kind'] = kind
            if dot:
                e['dot'] = dot
            if sonde:
                e['sonde'] = sonde
            return e

        g_install, g_config, g_upload = [], [], []

        # =====================================================================
        #  GROUP 1 — INSTALLATION
        # =====================================================================
        cfg_file = os.path.abspath(os.path.join(os.getcwd(), 'config.yaml'))
        have_cfg = os.path.isfile(cfg_file)
        have_decdir = os.path.isdir(dec_dir)
        in_venv = (sys.prefix != getattr(sys, 'base_prefix', sys.prefix))
        venv_dir = os.path.abspath(os.path.join(os.getcwd(), 'venv'))
        have_venv = in_venv or os.path.isdir(venv_dir)
        install_ok = have_cfg and have_decdir and have_venv
        g_install.append(entry(
            'Installation completed', 'ok' if install_ok else 'warn',
            'config.yaml, decoders/rs1729 and Python venv present'
            if install_ok else
            f"missing: {', '.join(x for x, ok in (('config.yaml', have_cfg), ('decoders/rs1729', have_decdir), ('venv', have_venv)) if not ok)}"))

        # install_softchain executed? (its binaries present + run)
        dft = os.path.join(dec_dir, 'dft_detect')
        fsk = os.path.join(dec_dir, 'fsk_demod')
        rs41 = os.path.join(dec_dir, 'rs41mod')
        dft_ok = os.path.isfile(dft) and self._bin_runs(dft)
        fsk_ok = os.path.isfile(fsk) and self._bin_runs(fsk)
        softchain_ok = dft_ok and fsk_ok
        g_install.append(entry(
            'install_softchain executed',
            'ok' if softchain_ok else ('warn' if (os.path.isfile(dft) or os.path.isfile(fsk)) else 'fail'),
            f"binaries in {os.path.relpath(dec_dir, os.getcwd())}"
            if softchain_ok else 'run scripts/install_softchain.sh'))
        g_install.append(entry(
            '  └ dft_detect', 'ok' if dft_ok else ('warn' if os.path.isfile(dft) else 'fail'),
            'runs and produces output' if dft_ok else
            ('present but no output (rebuild)' if os.path.isfile(dft) else 'not installed')))
        g_install.append(entry(
            '  └ fsk_demod', 'ok' if fsk_ok else ('warn' if os.path.isfile(fsk) else 'fail'),
            'runs and prints usage' if fsk_ok else
            ('present but no output (rebuild)' if os.path.isfile(fsk) else 'not installed')))
        softin = False
        if os.path.isfile(rs41):
            try:
                r = subprocess.run([rs41, '--help'], capture_output=True, text=True, timeout=6)
                softin = '--softin' in ((r.stdout or '') + (r.stderr or ''))
            except Exception:
                softin = False
        g_install.append(entry(
            '  └ softin support', 'ok' if softin else ('warn' if os.path.isfile(rs41) else 'fail'),
            'rs41mod accepts --softin (soft-bit chain usable)' if softin else
            ('rs41mod present but no --softin (old build)' if os.path.isfile(rs41) else 'rs41mod not installed')))

        # Python venv
        g_install.append(entry(
            'Python venv', 'ok' if have_venv else 'warn',
            f"running in venv ({sys.prefix})" if in_venv else
            (f"venv dir present ({os.path.relpath(venv_dir, os.getcwd())})" if os.path.isdir(venv_dir) else 'no venv detected')))

        # MQTT client library
        try:
            import paho.mqtt  # noqa: F401
            paho_ver = getattr(__import__('paho.mqtt', fromlist=['__version__']), '__version__', '?')
            g_install.append(entry('MQTT client library', 'ok', f"paho-mqtt {paho_ver} installed"))
        except Exception:
            g_install.append(entry('MQTT client library', 'warn', 'paho-mqtt not installed (pip install paho-mqtt)'))

        # config.yaml valid (parses)
        cfg_status, cfg_detail = 'ok', f"parsed OK ({os.path.relpath(cfg_file, os.getcwd())})"
        try:
            import yaml
            if have_cfg:
                with open(cfg_file, 'r', encoding='utf-8') as f:
                    yaml.safe_load(f)
            else:
                cfg_status, cfg_detail = 'warn', 'config.yaml not found in working directory'
        except Exception as e:
            cfg_status, cfg_detail = 'fail', f"YAML parse error: {e}"
        g_install.append(entry('config.yaml valid', cfg_status, cfg_detail))

        # Airspy support (install-time SDR capability)
        airspy_cfg = bool((cfg.get('sdr', {}) or {}).get('airspy_support', False))
        airspy_bin = shutil.which('airspy_rx') is not None
        g_install.append(entry(
            'Airspy support',
            'ok' if (airspy_cfg and airspy_bin) else ('warn' if (airspy_cfg or airspy_bin) else 'info'),
            f"sdr.airspy_support={airspy_cfg}; airspy_rx {'found' if airspy_bin else 'not found'} in PATH"))

        # =====================================================================
        #  GROUP 2 — CONFIGURATION
        # =====================================================================
        sdr = (cfg.get('sdr', {}) or {})
        sdr_type = str(sdr.get('type', 'rtlsdr'))
        det = (cfg.get('detection', {}) or {})

        # Per-device live worker status (state + decoded sonde + active freq).
        ws_map = {}
        dm = getattr(self, 'decoder_manager', None)
        if dm is not None and hasattr(dm, 'get_worker_status'):
            try:
                for ws in dm.get_worker_status():
                    ws_map[ws.get('serial')] = ws
            except Exception:
                pass
        connected = None
        if sdr_type == 'rtlsdr':
            try:
                connected = self._get_connected_rtlsdr_serials()
            except Exception:
                connected = None

        def _recv_row(serial, center_hz, mode):
            """Build a receiver entry with a colored status dot + decoded sonde,
            mirroring the SDR Devices panel."""
            ws = ws_map.get(serial) or {}
            state = ws.get('state')
            sonde = ws.get('sonde_type')
            present = (serial in connected) if connected is not None else True
            # Active-decode frequency label if we have one, else configured center.
            flabel = ws.get('freq_label')
            if not flabel and ws.get('frequency'):
                flabel = f"{ws['frequency']/1e6:.3f} MHz"
            if not flabel:
                flabel = f"{center_hz/1e6:.3f} MHz"
            if state == 'decoding':
                dot, st, sd = 'green', 'ok', 'decoding'
            elif state == 'scanning':
                dot, st, sd = 'blue', 'ok', 'scanning'
            elif state == 'idle':
                dot, st, sd = 'grey', 'info', 'idle'
            elif not present:
                dot, st, sd = 'red', 'warn', 'not detected'
            else:
                dot, st, sd = 'green', 'ok', 'connected'
            if state == 'decoding' and sonde:
                detail = f"{flabel} · {sonde} · {sd}"
            else:
                detail = f"{flabel} · {mode} · {sd}"
            return entry(f"  • {serial}", st, detail, kind='receiver', dot=dot,
                         sonde=(sonde if state == 'decoding' else None))

        recv_rows = []
        if sdr_type == 'rtlsdr':
            for d in ((sdr.get('rtlsdr', {}) or {}).get('devices', []) or []):
                recv_rows.append(_recv_row(str(d.get('serial', '?')),
                                           int(d.get('center_freq', 0)),
                                           d.get('decoder_mode', 'legacy')))
        elif sdr_type == 'airspy':
            a = (sdr.get('airspy', {}) or {})
            recv_rows.append(_recv_row(f"Airspy {a.get('serial') or '(auto)'}",
                                       int(a.get('center_freq', 0)),
                                       a.get('decode_mode', 'legacy')))
        elif sdr_type == 'ka9q':
            k = (sdr.get('ka9q', {}) or {})
            recv_rows.append(entry(
                f"  • KA9Q {k.get('radio_hostname', '?')}", 'ok',
                f"grp {k.get('multicast_group', '?')}:{k.get('port', '?')} · max {k.get('max_channels', '?')} ch",
                kind='receiver', dot='green'))
        elif sdr_type == 'flux242':
            fx = (sdr.get('flux242', {}) or {})
            recv_rows.append(entry("  • Flux242", 'ok',
                                   f"{int(fx.get('center_freq', 0))/1e6:.3f} MHz",
                                   kind='receiver', dot='green'))

        g_config.append(entry(f"Configured receivers ({sdr_type})",
                              'ok' if recv_rows else 'warn',
                              f"{len(recv_rows)} configured" if recv_rows else 'none configured'))
        g_config.extend(recv_rows)

        # Scanner mode / status (+ scan frequency range)
        scanner = (det.get('scanner', {}) or {})
        backend = str(scanner.get('backend', 'welch')).lower()
        if backend == 'rtl_power':
            lo, hi = scanner.get('band_start_hz'), scanner.get('band_stop_hz')
        else:
            fr = det.get('freq_ranges') or []
            lo = min((r[0] for r in fr if len(r) >= 2), default=None)
            hi = max((r[1] for r in fr if len(r) >= 2), default=None)
        rng = f" · range {lo/1e6:.2f} - {hi/1e6:.2f} MHz" if (lo and hi) else ''
        if backend == 'rtl_power':
            rp = str(scanner.get('rtl_power_path', 'rtl_power'))
            have_rp = shutil.which(rp) is not None
            g_config.append(entry(
                'Scanner mode', 'ok' if have_rp else 'warn',
                ('rtl_power full-band sweep' if have_rp
                 else f"rtl_power set but '{rp}' not found — falls back to welch") + rng))
        else:
            g_config.append(entry('Scanner mode', 'ok', 'welch per-device segment scan' + rng))
        if sdr_type == 'ka9q':
            sm = bool((sdr.get('ka9q', {}) or {}).get('scanning_mode', False))
            g_config.append(entry('  • KA9Q scanning', 'ok' if sm else 'info',
                                  'DFT spectrum scanning enabled' if sm else 'disabled (static channels)'))

        # Band sweep
        bs = (det.get('band_sweep', {}) or {})
        bs_on = bool(bs.get('enabled', False))
        if bs_on:
            bs_detail = (f"enabled {bs.get('band_min_hz', 0)/1e6:.1f}-{bs.get('band_max_hz', 0)/1e6:.1f} MHz, "
                         f"hop after {bs.get('dwell_empty_cycles', '?')} empty scans")
            if backend == 'rtl_power':
                bs_detail += ' (ignored in rtl_power mode)'
        else:
            bs_detail = 'disabled (static center per device)'
        g_config.append(entry('Band sweep', 'ok' if bs_on else 'info', bs_detail))

        # Supported sonde types
        try:
            from ..decoders.rs1729_decoder import RS1729Decoder
            type_map = RS1729Decoder.DECODER_MAP
        except Exception:
            type_map = {'RS41': 'rs41mod', 'RS92': 'rs92mod', 'DFM': 'dfm09mod',
                        'M10': 'm10mod', 'M20': 'm20mod', 'iMet': 'imet54mod',
                        'LMS6': 'lms6mod', 'MRZ': 'mrzmod'}
        present = [t for t, b in type_map.items() if os.path.isfile(os.path.join(dec_dir, b))]
        g_config.append(entry(
            'Supported sonde types',
            'ok' if present else 'warn',
            f"decoders present: {', '.join(present)}" if present else
            f"none of {', '.join(type_map)} found in {os.path.relpath(dec_dir, os.getcwd())}"))

        # soft_decode
        soft_on = bool((cfg.get('decoders', {}) or {}).get('soft_decode', False))
        if soft_on:
            usable = fsk_ok and softin
            g_config.append(entry(
                'soft_decode', 'ok' if usable else 'warn',
                'ON — fsk_demod soft-bit chain' if usable else
                'ON but fsk_demod/softin missing → falls back to direct --IQ'))
        else:
            g_config.append(entry('soft_decode', 'info', 'OFF — direct --IQ decode chain (default)'))

        # USB recovery
        rec = (cfg.get('recovery', {}) or {})
        usb_on = bool(rec.get('usb_reset_on_wedge', False))
        g_config.append(entry(
            'USB recovery', 'ok' if usb_on else 'info',
            (f"reset-on-wedge on (settle {rec.get('usb_reset_settle_s', '?')}s, "
             f"max {rec.get('usb_reset_max_attempts', '?')} attempts)")
            if usb_on else 'reset-on-wedge off'))

        # SNR live values
        live_snr = bool((cfg.get('decoders', {}) or {}).get('live_signal_metrics', False))
        airspy_active = (sdr_type == 'airspy')
        g_config.append(entry(
            'SNR live values',
            'ok' if (live_snr or airspy_active) else 'info',
            'active (decoders.live_signal_metrics: true)' if live_snr else
            ('active (Airspy provides per-frame RSSI/SNR)' if airspy_active else
             'off (decoders.live_signal_metrics: false — one-time scan value)')))

        # RS41 fallback (full dft_detect classification)
        fastpath = bool(det.get('rs41_fastpath', False))
        g_config.append(entry(
            'RS41 fallback (full dft_detect)',
            'ok' if not fastpath else 'info',
            'ACTIVE — every candidate classified by dft_detect (rs41_fastpath: false)'
            if not fastpath else 'INACTIVE — rs41_fastpath: true (bandwidth fast-path in use)'))

        # Sonde retention on map
        retention = (cfg.get('webui', {}) or {}).get('sonde_retention_time',
                     (cfg.get('output', {}) or {}).get('sonde_retention_time'))
        if retention is None:
            g_config.append(entry('Sonde retention time', 'info', 'not set (default)'))
        else:
            try:
                secs = int(retention)
                g_config.append(entry('Sonde retention time', 'ok',
                                      f"{secs} s ({secs/3600:.1f} h) kept on map after last frame"))
            except (TypeError, ValueError):
                g_config.append(entry('Sonde retention time', 'warn', f"invalid value: {retention!r}"))

        # =====================================================================
        #  GROUP 3 — UPLOAD / DOWNLOAD
        # =====================================================================
        # UDP JSON output
        udp = ((cfg.get('output', {}) or {}).get('udp', {}) or {})
        udp_on = bool(udp.get('enabled', False))
        g_upload.append(entry(
            'UDP output', 'ok' if udp_on else 'info',
            f"enabled → {udp.get('host', '127.0.0.1')}:{udp.get('port', '?')} (JSON)"
            if udp_on else 'disabled'))

        # MQTT upload
        mqtt_cfg = ((cfg.get('openwx', {}) or {}).get('mqtt', {}) or {})
        mqtt_enabled = bool(mqtt_cfg.get('enabled', False))
        mqtt_server = str(mqtt_cfg.get('server', '') or '')
        mqtt_port = mqtt_cfg.get('port', '')
        mqtt_state = self._get_mqtt_health().get('status', 'unknown')
        mqtt_target = f", server {mqtt_server}:{mqtt_port}" if mqtt_server else ''
        g_upload.append(entry(
            'MQTT upload',
            'ok' if (mqtt_enabled and mqtt_state in ('connected', 'ok', 'running')) else ('info' if not mqtt_enabled else 'warn'),
            f"enabled={mqtt_enabled}, status={mqtt_state}{mqtt_target}"))

        # SondeHub upload
        sh_enabled = bool((cfg.get('sondehub', {}) or {}).get('enabled', False))
        sh_state = self._get_sondehub_health().get('status', 'unknown')
        g_upload.append(entry(
            'SondeHub upload',
            'ok' if (sh_enabled and sh_state in ('connected', 'ok', 'running', 'active', 'uploading')) else ('info' if not sh_enabled else 'warn'),
            f"enabled={sh_enabled}, status={sh_state}"))

        # RS92 ephemeris
        eph = self._get_ephemeris_health()
        if not eph.get('enabled'):
            g_upload.append(entry('RS92 ephemeris', 'info', 'download disabled (rs92.ephemeris_download: false)'))
        else:
            g_upload.append(entry(
                'RS92 ephemeris',
                'ok' if eph.get('available') else 'warn',
                f"{eph.get('state', '?')}" + (f" — {eph.get('file')}" if eph.get('file') else '')))

        # Telemetry (anonymous install counter) — shows the current install ID
        tel = (cfg.get('telemetry', {}) or {})
        tel_on = bool(tel.get('enabled', False))
        if tel_on:
            install_id = ''
            try:
                iid_path = os.path.join(os.getcwd(), 'data', '.install_id')
                if os.path.isfile(iid_path):
                    with open(iid_path, 'r', encoding='utf-8') as f:
                        install_id = f.read().strip()
            except Exception:
                install_id = ''
            tel_detail = f"enabled — install ID {install_id}" if install_id else \
                'enabled — install ID not yet generated'
        else:
            tel_detail = 'disabled (no anonymous install counter)'
        g_upload.append(entry('Telemetry (install counter)',
                              'ok' if tel_on else 'info', tel_detail))

        groups = [
            {'name': 'Installation', 'checks': g_install},
            {'name': 'Configuration', 'checks': g_config},
            {'name': 'Upload / Download', 'checks': g_upload},
        ]
        checks = [c for g in groups for c in g['checks']]
        ok = sum(1 for c in checks if c['status'] == 'ok')
        warn = sum(1 for c in checks if c['status'] == 'warn')
        fail = sum(1 for c in checks if c['status'] == 'fail')
        return {
            'groups': groups,
            'checks': checks,   # flat list kept for backward compatibility
            'summary': {'ok': ok, 'warn': warn, 'fail': fail, 'total': len(checks)},
            'version': __version__,
            'station': str((cfg.get('station', {}) or {}).get('callsign', '') or ''),
            'generated': datetime.utcnow().isoformat() + 'Z',
        }

    def _get_connected_rtlsdr_serials(self) -> Set[str]:
        """Return set of serial numbers for physically connected RTL-SDR devices.
        Result is cached for 10 seconds to avoid hammering rtl_test."""
        import time
        now = time.time()
        if now - getattr(self, '_connected_serials_ts', 0) < 10:
            return self._connected_serials_cache
        try:
            result = subprocess.run(
                ['rtl_test', '-t'],
                capture_output=True, text=True, timeout=4
            )
            serials: Set[str] = set()
            for line in (result.stdout + result.stderr).splitlines():
                # Format: "  0:  RTLSDR3, DL2MF_SDR3 0.0ppm, SN: MF20003"
                if 'SN:' in line:
                    sn = line.split('SN:')[-1].strip()
                    if sn:
                        serials.add(sn)
            self._connected_serials_cache = serials
            self._connected_serials_ts = now
            return serials
        except Exception:
            # If rtl_test fails, fall back to showing all configured devices
            return None
    
    def _get_receiver_status(self) -> List[Dict]:
        """Gather current receiver status for Kindle dashboard.
        
        Returns:
            List of receiver status dicts with keys:
                - device_id: str (e.g. "RTL00001")
                - state: str ("SCANNING", "DECODING", "IDLE", etc.)
                - frequency: Optional[float] in Hz
                - freq_label: str (formatted frequency or range)
                - sonde_type: Optional[str] sonde type being decoded
                - spectrum: Optional[Dict] spectrum data if scanning
        """
        receivers = []
        
        # Check if using RTL-SDR device manager
        if self.decoder_manager and hasattr(self.decoder_manager, 'get_worker_status'):
            try:
                worker_statuses = self.decoder_manager.get_worker_status()
                for ws in worker_statuses:
                    state_map = {
                        'idle': 'IDLE',
                        'scanning': 'SCANNING',
                        'decoding': 'DECODING'
                    }
                    # Convert frequency from MHz to Hz if present
                    freq_hz = None
                    if ws.get('frequency'):
                        freq_hz = ws['frequency'] * 1e6  # MHz -> Hz
                    
                    state = state_map.get(ws.get('state', 'idle').lower(), 'IDLE')
                    device_id = ws.get('serial', 'Unknown')
                    
                    rx_dict = {
                        'device_id': device_id,
                        'state': state,
                        'frequency': freq_hz,
                        'freq_label': ws.get('freq_label', ''),
                        'sonde_type': ws.get('sonde_type'),
                    }
                    
                    # Add spectrum data if scanning
                    if state == 'SCANNING' and hasattr(self.decoder_manager, 'get_spectrum_for_receiver'):
                        try:
                            receiver_id = f"rtlsdr:{device_id}"
                            spectrum = self.decoder_manager.get_spectrum_for_receiver(receiver_id)
                            if spectrum and spectrum.get('freqs_mhz'):
                                rx_dict['spectrum'] = spectrum
                        except Exception as e:
                            self.logger.debug(f"Could not get spectrum for {device_id}: {e}")
                    
                    receivers.append(rx_dict)
            except Exception as e:
                self.logger.error(f"Error getting worker status: {e}")
        
        # Fallback: create placeholder receivers if none found
        if not receivers:
            for i in range(4):  # Assume 4 RTL-SDR devices
                receivers.append({
                    'device_id': f"RTL{i:05d}",
                    'state': 'IDLE',
                    'frequency': None,
                    'freq_label': '',
                    'sonde_type': None,
                })
        
        return receivers
    
    def _get_active_sondes_for_dashboard(self) -> List[Dict]:
        """Gather active sonde telemetry for Kindle dashboard.
        
        Returns:
            List of sonde dicts with simplified telemetry data
        """
        sondes = []
        now_iso = datetime.utcnow().isoformat() + 'Z'
        
        with self.lock:
            for serial, telemetry_list in self.sondes.items():
                if not telemetry_list:
                    continue
                
                # Get latest telemetry point
                latest = telemetry_list[-1]
                
                # Check if sonde is still active using reception_time
                reception_time_str = latest.get('reception_time', latest.get('timestamp', ''))
                if not reception_time_str:
                    continue
                
                try:
                    # Parse ISO format datetime
                    reception_dt = datetime.fromisoformat(reception_time_str.replace('Z', '+00:00'))
                    now_dt = datetime.fromisoformat(now_iso.replace('Z', '+00:00'))
                    age_seconds = (now_dt - reception_dt).total_seconds()
                    
                    # Skip if older than retention time
                    if age_seconds > self.sonde_retention_time:
                        continue
                        
                    last_update = time.time() - age_seconds
                except Exception:
                    # If datetime parsing fails, skip this sonde
                    continue
                
                # Extract relevant data using correct field names from to_dict()
                sondes.append({
                    'serial': serial,
                    'type': latest.get('type', '?'),
                    'frequency': latest.get('frequency', 0) * 1e6,  # Convert MHz to Hz
                    'altitude': latest.get('alt', 0),
                    'latitude': latest.get('lat', 0),
                    'longitude': latest.get('lon', 0),
                    'temperature': latest.get('temp'),
                    'humidity': latest.get('humidity'),
                    'pressure': latest.get('pressure'),
                    'velocity_v': latest.get('vel_v', 0),
                    'velocity_h': latest.get('vel_h', 0),
                    'heading': latest.get('heading', 0),
                    'battery': latest.get('batt', 0),
                    'sats': latest.get('sats', 0),
                    'snr': latest.get('snr', 0),
                    'rssi': latest.get('rssi', 0),
                    'last_update': last_update,
                })
        
        # Sort by most recent first
        sondes.sort(key=lambda s: s.get('last_update', 0), reverse=True)
        
        return sondes
    
    def _get_today_frames(self) -> int:
        """Frames received on the current UTC day, rolling over at UTC midnight.
        Rolls the counter here too so a quiet gateway still reports 0 (not
        yesterday's total) once the day changes without a new frame arriving."""
        today = datetime.utcnow().strftime('%Y-%m-%d')
        if today != self._today_frames_date:
            self._today_frames_date = today
            self._today_frames = 0
        return self._today_frames

    @staticmethod
    def _haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
        """Great-circle (surface) distance in km, or None if a coord is missing."""
        try:
            lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
        except (TypeError, ValueError):
            return None
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = (math.sin(dphi / 2) ** 2 +
             math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _look_angles(gw_lat, gw_lon, gw_alt, s_lat, s_lon, s_alt) -> Optional[dict]:
        """Gateway->sonde look geometry using a spherical-earth ENU conversion
        that accounts for BOTH altitudes. Returns a dict or None if lat/lon are
        missing:
          slant_km      true 3D line-of-sight distance (height-aware)
          ground_km     great-circle surface distance
          elevation_deg angle above the local horizon (negative = below horizon,
                        i.e. hidden by earth curvature)
          azimuth_deg   compass bearing 0..360 (0=N, 90=E, 180=S, 270=W)
        Missing altitudes default to 0 m so a fix still yields a bearing.
        """
        try:
            gw_lat = float(gw_lat); gw_lon = float(gw_lon)
            s_lat = float(s_lat);   s_lon = float(s_lon)
        except (TypeError, ValueError):
            return None
        try:
            gw_h = float(gw_alt)
        except (TypeError, ValueError):
            gw_h = 0.0
        try:
            s_h = float(s_alt)
        except (TypeError, ValueError):
            s_h = 0.0

        R = 6371000.0  # mean earth radius (m)

        def ecef(lat, lon, h):
            la, lo, r = math.radians(lat), math.radians(lon), R + h
            return (r * math.cos(la) * math.cos(lo),
                    r * math.cos(la) * math.sin(lo),
                    r * math.sin(la))

        gx, gy, gz = ecef(gw_lat, gw_lon, gw_h)
        sx, sy, sz = ecef(s_lat, s_lon, s_h)
        dx, dy, dz = sx - gx, sy - gy, sz - gz

        la, lo = math.radians(gw_lat), math.radians(gw_lon)
        east  = (-math.sin(lo), math.cos(lo), 0.0)
        north = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la))
        up    = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))

        e = dx * east[0]  + dy * east[1]  + dz * east[2]
        n = dx * north[0] + dy * north[1] + dz * north[2]
        u = dx * up[0]    + dy * up[1]    + dz * up[2]

        horiz = math.hypot(e, n)
        slant = math.sqrt(e * e + n * n + u * u)
        elevation = math.degrees(math.atan2(u, horiz)) if slant > 0 else 0.0
        azimuth = (math.degrees(math.atan2(e, n)) + 360.0) % 360.0

        return {
            'slant_km': slant / 1000.0,
            'ground_km': WebUI._haversine_km(gw_lat, gw_lon, s_lat, s_lon),
            'elevation_deg': elevation,
            'azimuth_deg': azimuth,
        }

    @staticmethod
    def _decimate_frames(frames: List[dict], max_points: int,
                         shape_key: str = 'alt') -> List[dict]:
        """Downsample statistics frames to ~max_points using LTTB (Largest-
        Triangle-Three-Buckets) on `shape_key`. LTTB keeps the visually
        important points (burst, landing, spikes) instead of blindly striding,
        and selects ONE shared index set so every series keeps a common time
        axis. Returns frames unchanged when already small enough."""
        n = len(frames)
        if not max_points or max_points < 3 or n <= max_points:
            return frames

        # Build the shape series; fall back to another numeric field if `alt`
        # is entirely missing (e.g. a log with no positions).
        def _num(fr, k):
            v = fr.get(k)
            return float(v) if isinstance(v, (int, float)) else None
        ys = [_num(f, shape_key) for f in frames]
        if all(v is None for v in ys):
            for k in ('rssi', 'snr', 'vel_v', 'vel_h', 'sats', 'battery', 'elevation'):
                cand = [_num(f, k) for f in frames]
                if any(v is not None for v in cand):
                    ys = cand
                    break
        # Forward-fill None so triangle areas are computable.
        last = 0.0
        filled = []
        for v in ys:
            if v is None:
                v = last
            else:
                last = v
            filled.append(v)
        ys = filled

        sampled = [0]                       # always keep the first point
        every = (n - 2) / (max_points - 2)
        a = 0
        for i in range(max_points - 2):
            avg_start = int((i + 1) * every) + 1
            avg_end = min(int((i + 2) * every) + 1, n)
            if avg_end <= avg_start:
                avg_end = min(avg_start + 1, n)
            avg_x = (avg_start + avg_end - 1) / 2.0
            avg_y = sum(ys[avg_start:avg_end]) / (avg_end - avg_start)
            rng_start = int(i * every) + 1
            rng_end = min(int((i + 1) * every) + 1, n)
            ay = ys[a]
            best_area = -1.0
            best_idx = rng_start
            for j in range(rng_start, rng_end):
                area = abs((a - avg_x) * (ys[j] - ay) - (a - j) * (avg_y - ay))
                if area > best_area:
                    best_area = area
                    best_idx = j
            sampled.append(best_idx)
            a = best_idx
        sampled.append(n - 1)               # always keep the last point
        return [frames[i] for i in sampled]

    def _recent_sondes(self, station_lat, station_lon, station_alt=0, window_s=3600) -> List[dict]:
        """Sondes this gateway received within the last `window_s` seconds, newest
        first (window comes from webui.sonde_retention_time — the "stale" horizon).

        Scans data/logs/<serial>-YYYYMMDD-HHMMSS.log, keeps files touched within
        the window (file mtime = last frame written), and reads the tail for the
        last known position + altitude + type so we can show the height-aware
        slant distance, elevation angle and bearing (course) from the station.
        One entry per serial (most recently active file wins).
        """
        results: Dict[str, dict] = {}
        log_dir = 'data/logs'
        if not os.path.isdir(log_dir):
            return []
        cutoff = time.time() - max(60, int(window_s))
        name_re = re.compile(r'^(.+?)-(\d{8})-(\d{6})\.log$')
        for fname in os.listdir(log_dir):
            m = name_re.match(fname)
            if not m:
                continue
            path = os.path.join(log_dir, fname)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            serial = m.group(1)
            # Keep only the most recently active file per serial.
            if serial in results and results[serial]['_mtime'] >= mtime:
                continue
            info = self._tail_last_position(path)
            lat = info.get('lat')
            lon = info.get('lon')
            look = self._look_angles(station_lat, station_lon, station_alt,
                                     lat, lon, info.get('alt'))
            results[serial] = {
                '_mtime': mtime,
                'serial': serial,
                'type': info.get('type'),
                'lat': lat,
                'lon': lon,
                'alt': info.get('alt'),
                'battery': info.get('battery'),
                'filename': fname,   # lets the popup load this sonde's history
                'last_seen': datetime.utcfromtimestamp(mtime).isoformat() + 'Z',
                # distance_km is now the height-aware 3D slant range.
                'distance_km': look['slant_km'] if look else None,
                'ground_km': look['ground_km'] if look else None,
                'elevation_deg': look['elevation_deg'] if look else None,
                'azimuth_deg': look['azimuth_deg'] if look else None,
            }
        out = sorted(results.values(), key=lambda r: r['_mtime'], reverse=True)
        for r in out:
            r.pop('_mtime', None)
        return out

    @staticmethod
    def _tail_last_position(path: str, max_bytes: int = 65536) -> dict:
        """Read the tail of a sonde log and return the last position/alt + type.
        Log blocks use lines like 'Position: <lat>, <lon>' and 'Altitude: <a> m';
        the header line 'Sonde: <serial> (<type>)' carries the type."""
        info: dict = {'lat': None, 'lon': None, 'alt': None, 'type': None,
                      'battery': None}
        try:
            with open(path, 'r', errors='ignore') as f:
                head = f.readline() + f.readline() + f.readline()
                mt = re.search(r'\(([^)]+)\)', head)
                if mt:
                    info['type'] = mt.group(1).strip()
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - max_bytes))
                except (OSError, ValueError):
                    f.seek(0)
                tail = f.read()
        except OSError:
            return info
        for pm in re.finditer(r'Position:\s*([+-]?\d+\.\d+),\s*([+-]?\d+\.\d+)', tail):
            try:
                info['lat'] = float(pm.group(1))
                info['lon'] = float(pm.group(2))
            except ValueError:
                pass
        for am in re.finditer(r'Altitude:\s*([+-]?\d+(?:\.\d+)?)\s*m', tail):
            try:
                info['alt'] = float(am.group(1))
            except ValueError:
                pass
        for bm in re.finditer(r'Battery:\s*([+-]?\d+(?:\.\d+)?)\s*V', tail):
            try:
                info['battery'] = float(bm.group(1))
            except ValueError:
                pass
        # Prefer the most specific per-frame 'Type:' line (e.g. DFM17) for the label.
        _tm = re.findall(r'^\s*Type:\s*(\S+)', tail, re.MULTILINE)
        if _tm:
            info['type'] = _tm[-1].strip()
        elif info['type'] is None:
            tm = re.search(r'\(([^)]+)\)', tail)
            if tm:
                info['type'] = tm.group(1).strip()
        return info

    @staticmethod
    def _logfile_type(path: str) -> Optional[str]:
        """Most specific sonde type for a log file: the latest per-frame
        'Type: <subtype>' line if present (e.g. DFM17), else the header
        'Sonde: <serial> (<type>)' base type (e.g. DFM). This is what makes a
        DFM log show DFM17 rather than the generic 'DFM' from the header, and
        fixes all-numeric DFM serials being mis-guessed as 'Unknown'."""
        header_type = None
        subtype = None
        try:
            with open(path, 'r', errors='ignore') as f:
                head = f.read(400)
                hm = re.search(r'Sonde:\s*\S+\s*\(([^)]+)\)', head)
                if hm:
                    header_type = hm.group(1).strip()
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 4096))
                except (OSError, ValueError):
                    f.seek(0)
                tail = f.read()
        except OSError:
            return None
        tm = re.findall(r'^\s*Type:\s*(\S+)', tail, re.MULTILINE)
        if tm:
            subtype = tm[-1].strip()
        # Prefer a subtype that refines (or is longer/more specific than) the base.
        if subtype and (not header_type
                        or subtype.upper().startswith(header_type.upper())
                        or len(subtype) >= len(header_type)):
            return subtype
        return header_type or subtype

    def _landing_from_log(self, path: str) -> Optional[dict]:
        """Extract a sonde's LANDING (last known) position + metadata from a log:
        the last frame that carries a Position, with its altitude, vertical
        velocity, frequency, timestamp and resolved type. Reads only the file
        tail (landings are near the end). Returns None if no position exists."""
        try:
            with open(path, 'r', errors='ignore') as f:
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 131072))   # 128 KB tail ≈ many final frames
                except (OSError, ValueError):
                    f.seek(0)
                tail = f.read()
        except OSError:
            return None

        # Split into per-frame blocks and scan from the end for the last fix.
        blocks = re.split(r'(?m)^Frame\s', tail)
        last = None
        for blk in reversed(blocks):
            pm = re.search(r'Position:\s*([+-]?\d+\.\d+),\s*([+-]?\d+\.\d+)', blk)
            if not pm:
                continue
            try:
                lat = float(pm.group(1))
                lon = float(pm.group(2))
            except ValueError:
                continue
            tsm = re.match(r'\s*\d+\s*-\s*(\S+)', blk)
            alt_m = re.search(r'Altitude:\s*([+-]?\d+(?:\.\d+)?)', blk)
            vv_m = re.search(r'Velocity H/V:\s*[+-]?\d+(?:\.\d+)?/([+-]?\d+(?:\.\d+)?)', blk)
            last = {
                'lat': lat, 'lon': lon,
                'timestamp': tsm.group(1) if tsm else None,
                'alt': float(alt_m.group(1)) if alt_m else None,
                'vvel': float(vv_m.group(1)) if vv_m else None,
            }
            break
        if last is None:
            return None
        # Frequency: last Frequency line anywhere in the tail (nearly constant).
        fr = re.findall(r'Frequency:\s*([+-]?\d+(?:\.\d+)?)', tail)
        last['frequency'] = float(fr[-1]) if fr else None
        last['type'] = self._logfile_type(path)
        return last

    def _load_sonde_from_logs(self, sonde_serial: str) -> Optional[Dict]:
        """Load sonde data from log files when not in active memory.

        Args:
            sonde_serial: Sonde serial number to search for
    
        Returns:
            Sonde dict with telemetry data, or None if not found
        """
        log_dir = 'data/logs'
        if not os.path.exists(log_dir):
            return None
        
        # Find log files for this sonde
        sonde_logs = []
        try:
            for fname in os.listdir(log_dir):
                if fname.startswith(f"{sonde_serial}-") and fname.endswith('.log'):
                    sonde_logs.append(os.path.join(log_dir, fname))
        except Exception as e:
            self.logger.error(f"Error reading log directory: {e}")
            return None
        
        if not sonde_logs:
            self.logger.debug(f"No log files found for sonde {sonde_serial}")
            return None
        
        # Use most recent log file
        logfile = sorted(sonde_logs)[-1]
        self.logger.info(f"Loading sonde {sonde_serial} from log file: {logfile}")
        
        # Parse log file to extract telemetry
        sonde_data = {
            'serial': sonde_serial,
            'type': 'Unknown',
            'frequency': 0,
            'latitude': 0,
            'longitude': 0,
            'altitude': 0,
            'velocity_h': 0,
            'velocity_v': 0,
            'heading': 0,
            'rssi': None,
            'snr': None,
            'sats': 0,
            'battery': 0,
            'frame': 0,
            'last_update': time.time(),
        }
        
        try:
            with open(logfile, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                
                # Extract sonde type from header
                for line in lines[:10]:
                    if line.startswith('Sonde:'):
                        match = re.search(r'\((\w+)\)', line)
                        if match:
                            sonde_data['type'] = match.group(1)
                            break
                
                # Parse last complete frame
                frame_count = 0
                for i, line in enumerate(lines):
                    line = line.strip()
                    
                    if line.startswith('Frame '):
                        frame_count += 1
                        sonde_data['frame'] = frame_count
                        
                    elif line.startswith('Position:'):
                        try:
                            parts = line.split(':', 1)[1].strip().split(',')
                            sonde_data['latitude'] = float(parts[0].strip())
                            sonde_data['longitude'] = float(parts[1].strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Altitude:'):
                        try:
                            sonde_data['altitude'] = float(line.split(':', 1)[1].replace('m', '').strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Velocity H/V:'):
                        try:
                            parts = line.split(':', 1)[1].strip().split('/')
                            sonde_data['velocity_h'] = float(parts[0].strip())
                            sonde_data['velocity_v'] = float(parts[1].replace('m/s', '').strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Heading:'):
                        try:
                            sonde_data['heading'] = float(line.split(':', 1)[1].replace('°', '').strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Frequency:'):
                        try:
                            freq_mhz = float(line.split(':', 1)[1].replace('MHz', '').strip())
                            sonde_data['frequency'] = freq_mhz * 1e6  # Convert to Hz
                        except Exception:
                            pass
                            
                    elif line.startswith('RSSI:'):
                        try:
                            sonde_data['rssi'] = float(line.split(':', 1)[1].replace('dB', '').strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('SNR:'):
                        try:
                            sonde_data['snr'] = float(line.split(':', 1)[1].replace('dB', '').strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Satellites:'):
                        try:
                            sonde_data['sats'] = int(line.split(':', 1)[1].strip())
                        except Exception:
                            pass
                            
                    elif line.startswith('Battery:'):
                        try:
                            sonde_data['battery'] = float(line.split(':', 1)[1].replace('V', '').strip())
                        except Exception:
                            pass
                
                # Check if we got valid position data
                if sonde_data['latitude'] == 0 and sonde_data['longitude'] == 0:
                    self.logger.warning(f"No valid position data found in log for {sonde_serial}")
                    return None
                    
                return sonde_data
                
        except Exception as e:
            self.logger.error(f"Error parsing log file {logfile}: {e}")
            return None
    
    def _get_system_info(self) -> Dict:
        """Gather system information for Kindle dashboard.
        
        Returns:
            Dict with uptime_seconds, cpu_percent, memory_percent
        """
        system_info = {}
        
        try:
            if PSUTIL_AVAILABLE:
                # Get system uptime
                boot_time = psutil.boot_time()
                system_info['uptime_seconds'] = time.time() - boot_time
                
                # Get CPU and memory usage
                system_info['cpu_percent'] = psutil.cpu_percent(interval=0.1)
                system_info['memory_percent'] = psutil.virtual_memory().percent
            else:
                # Fallback: read uptime from /proc (Linux only)
                try:
                    with open('/proc/uptime', 'r') as f:
                        uptime_seconds = float(f.readline().split()[0])
                        system_info['uptime_seconds'] = uptime_seconds
                except:
                    system_info['uptime_seconds'] = 0
                
                system_info['cpu_percent'] = 0
                system_info['memory_percent'] = 0
        except Exception as e:
            self.logger.error(f"Error gathering system info: {e}")
            system_info = {
                'uptime_seconds': 0,
                'cpu_percent': 0,
                'memory_percent': 0,
            }
        
        return system_info
    
    def start(self):
        """Start Flask server in background thread"""
        if not self.enabled:
            self.logger.info("Web UI disabled")
            return
        
        self.server_thread = threading.Thread(
            target=self._run_server,
            daemon=True
        )
        self.server_thread.start()
        
        self.logger.info(f"Web UI started at http://{self.host}:{self.port}")
    
    def _run_server(self):
        """Run Flask server"""
        # Disable Flask's default logger
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.WARNING)
        
        self.app.run(
            host=self.host,
            port=self.port,
            debug=self.config['webui']['debug'],
            use_reloader=False,
            threaded=True
        )
    
    def _is_valid_serial(self, serial: str) -> bool:
        """Check if serial number is valid and fully decoded"""
        if not serial or serial == 'UNKNOWN':
            return False
        
        # Filter out serials that are too short (less than 5 characters)
        if len(serial) < 5:
            return False
        
        # Filter out serials with suspicious special characters
        # Allow alphanumeric, underscore, and dash in middle only
        if serial.startswith('-') or serial.startswith('+') or serial.endswith('-') or serial.endswith('+'):
            return False
        
        # Filter out serials that are mostly non-alphanumeric
        alphanumeric_count = sum(c.isalnum() for c in serial)
        if alphanumeric_count < len(serial) / 2:
            return False
        
        return True
    
    def add_telemetry(self, telemetry: SondeTelemetry):
        """Add new telemetry data"""
        with self.lock:
            serial = telemetry.serial
            
            # Increment frame count for this sonde
            if serial not in self.sonde_frame_counts:
                self.sonde_frame_counts[serial] = 0
            self.sonde_frame_counts[serial] += 1
            
            # Initialize list for new sonde
            if serial not in self.sondes:
                self.sondes[serial] = []
                self.total_sondes_received += 1  # Increment unique sonde counter
                
                # Only log and create file for valid serials
                if self._is_valid_serial(serial):
                    self.logger.info(f"New sonde detected: {serial} ({telemetry.sonde_type})")
                    
                    # Log first frame to structured action log (will be completed below when data is appended)
                    self.sonde_first_frames[serial] = False  # Mark as pending
                
                    # Log filename format: <sondeid>-YYYYMMDD-HHMMSS.log
                    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                    logfile = f"data/logs/{serial}-{timestamp}.log"
                    
                    # Check if a log file already exists for this sonde ID
                    existing_logs = []
                    log_dir = 'data/logs'
                    if os.path.exists(log_dir):
                        for fname in os.listdir(log_dir):
                            if fname.startswith(f"{serial}-") and fname.endswith('.log'):
                                existing_logs.append(os.path.join(log_dir, fname))
                    
                    # If log exists, append to the most recent one
                    if existing_logs:
                        logfile = sorted(existing_logs)[-1]  # Use most recent
                        self.logger.info(f"Appending to existing log file: {logfile}")
                        # Restore historical track points so path remains continuous.
                        if not self.sondes[serial]:
                            self.sondes[serial] = self._load_history_from_log(
                                logfile, serial, telemetry.sonde_type
                            )
                    
                    self.sonde_logfiles[serial] = logfile
                    
                    # Write header to log file (only if new file)
                    if not existing_logs:
                        try:
                            with open(logfile, 'w') as f:
                                f.write(f"OpenWXSDR Sonde Log\n")
                                # Prefer the specific subtype (e.g. DFM17, RS41-SGP)
                                # when it's already resolved at first frame.
                                _hdr_type = getattr(telemetry, 'subtype', None) or telemetry.sonde_type
                                f.write(f"Sonde: {serial} ({_hdr_type})\n")
                                receiver_name = telemetry.receiver_device if telemetry.receiver_device else 'Unknown'
                                f.write(f"Receiver: {receiver_name}\n")
                                f.write(f"Started: {datetime.now().isoformat()}\n")
                                f.write(f"{'='*80}\n\n")
                        except Exception as e:
                            self.logger.error(f"Error creating log file: {e}")
                    else:
                        # Append session separator
                        try:
                            with open(logfile, 'a') as f:
                                f.write(f"\n{'='*80}\n")
                                f.write(f"Session resumed: {datetime.now().isoformat()}\n")
                                f.write(f"{'='*80}\n\n")
                        except Exception as e:
                            self.logger.error(f"Error appending to log file: {e}")
            
            # Convert to dict and validate position continuity before storing.
            data = telemetry.to_dict()
            # Add reception time (current system time) for retention logic
            # This is separate from 'timestamp' which is the decoded GPS time from the sonde
            data['reception_time'] = datetime.utcnow().isoformat() + 'Z'
            self._sanitize_position_jump(serial, data)
            self.sondes[serial].append(data)
            
            # Write telemetry to log file
            if serial in self.sonde_logfiles:
                try:
                    with open(self.sonde_logfiles[serial], 'a') as f:
                        # Format telemetry data
                        f.write(f"Frame {telemetry.frame_number} - {data.get('timestamp', 'N/A')}\n")
                        # Record the resolved type/subtype per frame so the exact
                        # DFM/RS subtype (e.g. DFM17) can be recovered from the log
                        # later even though it isn't known when the header is written.
                        _ftype = data.get('subtype') or data.get('type')
                        if _ftype:
                            f.write(f"  Type: {_ftype}\n")
                        if data.get('lat') is not None and data.get('lon') is not None:
                            f.write(f"  Position: {data['lat']:.5f}, {data['lon']:.5f}\n")
                            # Elevation angle from this gateway to the sonde (needs
                            # a fix; uses station lat/lon/alt). Written per frame so
                            # it's available in the log and the statistics graph.
                            _st = self.config.get('station', {})
                            _look = self._look_angles(
                                _st.get('lat'), _st.get('lon'), _st.get('alt', 0),
                                data.get('lat'), data.get('lon'), data.get('alt'))
                            if _look is not None:
                                f.write(f"  Elevation: {_look['elevation_deg']:.1f}°\n")
                        if data.get('alt') is not None:
                            f.write(f"  Altitude: {data['alt']:.1f} m\n")
                        # CRITICAL: Write vel_v only if it exists, don't default to 0
                        if data.get('vel_h') is not None:
                            vel_v_str = f"{data['vel_v']:.1f}" if data.get('vel_v') is not None else "N/A"
                            f.write(f"  Velocity H/V: {data['vel_h']:.1f}/{vel_v_str} m/s\n")
                        if data.get('heading') is not None:
                            f.write(f"  Heading: {data['heading']:.0f}°\n")
                        if data.get('frequency'):
                            f.write(f"  Frequency: {data['frequency']:.3f} MHz\n")
                        if data.get('rssi') is not None:
                            f.write(f"  RSSI: {data['rssi']:.1f} dB\n")
                        if data.get('snr') is not None:
                            f.write(f"  SNR: {data['snr']:.1f} dB\n")
                        if data.get('sats') is not None:
                            f.write(f"  Satellites: {data['sats']}\n")
                        if data.get('batt') is not None:
                            f.write(f"  Battery: {data['batt']:.2f} V\n")
                        if data.get('temp') is not None:
                            f.write(f"  Temperature: {data['temp']:.1f}°C\n")
                        if data.get('humidity') is not None:
                            f.write(f"  Humidity: {data['humidity']:.1f}%\n")
                        if data.get('pressure') is not None:
                            f.write(f"  Pressure: {data['pressure']:.1f} hPa\n")
                        f.write("\n")
                except Exception as e:
                    self.logger.error(f"Error writing to log file: {e}")
            
            # Increment total frame counter
            self.total_frames_received += 1
            # Increment today's (UTC) frame counter, rolling over at UTC midnight.
            _utc_day = datetime.utcnow().strftime('%Y-%m-%d')
            if _utc_day != self._today_frames_date:
                self._today_frames_date = _utc_day
                self._today_frames = 0
            self._today_frames += 1
            
            # Log first complete telemetry frame to structured action log
            if serial in self.sonde_first_frames and not self.sonde_first_frames[serial]:
                if data.get('lat') and data.get('lon') and data.get('frame'):
                    self.sonde_first_frames[serial] = True  # Mark as logged
                    self.sonde_last_frames[serial] = data  # Track for final frame logging
                    self._log_action('sonde_first_frame', {
                        'serial': serial,
                        'frequency_mhz': round(data.get('frequency', 0), 3),
                        'sonde_type': data.get('sonde_type', ''),
                        'lat': round(data.get('lat', 0), 5),
                        'lon': round(data.get('lon', 0), 5),
                        'alt': round(data.get('alt', 0), 1) if data.get('alt') else None,
                        'frame': data.get('frame', 0),
                        'total_frames': self.sonde_frame_counts.get(serial, 0),
                        'sats': data.get('sats', 0),
                        'rssi': round(data.get('rssi', 0), 1) if data.get('rssi') else None,
                        'snr': round(data.get('snr', 0), 1) if data.get('snr') else None,
                        'temp': round(data.get('temp', 0), 1) if data.get('temp') else None,
                        'humidity': round(data.get('humidity', 0), 1) if data.get('humidity') else None,
                        'pressure': round(data.get('pressure', 0), 1) if data.get('pressure') else None
                    })
            
            # Update last frame for stopping log
            if serial in self.sonde_last_frames:
                self.sonde_last_frames[serial] = data
            
            # Track active frequency
            if telemetry.frequency:
                self.active_frequencies.add(telemetry.frequency)
            
            # Keep a long continuous path for full-flight rendering.
            if len(self.sondes[serial]) > self.max_track_points:
                self.sondes[serial] = self.sondes[serial][-self.max_track_points:]
            
            # Remove old sondes (no data for 1 hour)
            self._cleanup_old_sondes()
    
    def _cleanup_old_sondes(self):
        """Remove sondes with no recent data (configurable retention time)"""
        current_time = datetime.utcnow()
        to_remove = []
        
        for serial, data in self.sondes.items():
            if not data:
                to_remove.append(serial)
                continue
            
            # Check last update time using reception_time (system time when received)
            # NOT timestamp (decoded GPS time from sonde, which may be from archive)
            last_frame = data[-1]
            time_field = 'reception_time' if 'reception_time' in last_frame else 'timestamp'
            
            if time_field in last_frame:
                try:
                    last_time = datetime.fromisoformat(last_frame[time_field].replace('Z', '+00:00'))
                    age_seconds = (current_time - last_time.replace(tzinfo=None)).total_seconds()
                    
                    # Use configured retention time instead of hardcoded value
                    if age_seconds > self.sonde_retention_time:
                        to_remove.append(serial)
                        self.logger.info(f"Removing sonde {serial} - no data for {age_seconds:.0f}s (retention: {self.sonde_retention_time}s)")
                        # Log last frame before removal
                        if serial in self.sonde_last_frames:
                            last_data = self.sonde_last_frames[serial]
                            # Calculate total frames for this sonde
                            total_frames = self.sonde_frame_counts.get(serial, 0)
                            self._log_action('sonde_stopped', {
                                'serial': serial,
                                'frequency_mhz': round(last_data.get('frequency', 0), 3),
                                'sonde_type': last_data.get('sonde_type', ''),
                                'last_frame': last_data.get('frame', 0),
                                'total_frames': total_frames,
                                'lat': round(last_data.get('lat', 0), 5) if last_data.get('lat') else None,
                                'lon': round(last_data.get('lon', 0), 5) if last_data.get('lon') else None,
                                'alt': round(last_data.get('alt', 0), 1) if last_data.get('alt') else None,
                                'reason': 'signal_lost'
                            })
                            del self.sonde_last_frames[serial]
                            # Also remove frame count when sonde is removed
                            if serial in self.sonde_frame_counts:
                                del self.sonde_frame_counts[serial]
                except:
                    pass
        
        for serial in to_remove:
            del self.sondes[serial]

    @staticmethod
    def _yaml_scalar(value):
        """Render scalar for in-place YAML line updates."""
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value)
        return "'" + s.replace("'", "''") + "'"

    @staticmethod
    def _replace_scalar_line(line: str, key: str, value) -> str:
        """Replace the scalar value of `key:` in one YAML line, preserving comments."""
        newline = ''
        core = line
        if line.endswith('\r\n'):
            core = line[:-2]
            newline = '\r\n'
        elif line.endswith('\n'):
            core = line[:-1]
            newline = '\n'

        pattern = re.compile(rf'^(\s*{re.escape(key)}\s*:\s*)([^#\r\n]*?)(\s*(#.*))?$')
        match = pattern.match(core)
        if not match:
            return line

        prefix = match.group(1)
        comment = match.group(3) or ''
        return f"{prefix}{WebUI._yaml_scalar(value)}{comment}{newline}"

    @staticmethod
    def _replace_key_value_raw(line: str, key: str, rendered_value: str) -> str:
        """Replace key value using pre-rendered YAML fragment while preserving comments."""
        newline = ''
        core = line
        if line.endswith('\r\n'):
            core = line[:-2]
            newline = '\r\n'
        elif line.endswith('\n'):
            core = line[:-1]
            newline = '\n'

        pattern = re.compile(rf'^(\s*{re.escape(key)}\s*:\s*)([^#\r\n]*?)(\s*(#.*))?$')
        match = pattern.match(core)
        if not match:
            return line

        prefix = match.group(1)
        comment = match.group(3) or ''
        return f"{prefix}{rendered_value}{comment}{newline}"

    @staticmethod
    def _line_indent(line: str) -> int:
        return len(line) - len(line.lstrip(' '))

    @staticmethod
    def _is_blank_or_comment(line: str) -> bool:
        stripped = line.strip()
        return stripped == '' or stripped.startswith('#')

    def _find_block_bounds(self, lines: List[str], path: List[str]):
        """Find YAML block bounds for a path like ['openwx','mqtt']."""
        search_start = 0
        parent_indent = -2
        key_line_idx = None

        for key in path:
            target_indent = parent_indent + 2
            pattern = re.compile(rf'^{" " * target_indent}{re.escape(key)}\s*:\s*(?:#.*)?$')
            found_idx = None
            for i in range(search_start, len(lines)):
                raw = lines[i].rstrip('\r\n')
                if pattern.match(raw):
                    found_idx = i
                    break
            if found_idx is None:
                return None
            key_line_idx = found_idx
            parent_indent = target_indent
            search_start = found_idx + 1

        if key_line_idx is None:
            return None

        end_idx = len(lines)
        for i in range(key_line_idx + 1, len(lines)):
            raw = lines[i].rstrip('\r\n')
            if self._is_blank_or_comment(raw):
                continue
            if self._line_indent(raw) <= parent_indent:
                end_idx = i
                break

        return (key_line_idx + 1, end_idx, parent_indent + 2)

    def _update_mapping_keys_in_text(self, config_text: str, path: List[str], updates: Dict[str, object]) -> str:
        """Update existing scalar keys in a YAML mapping block without reordering file content."""
        lines = config_text.splitlines(keepends=True)
        bounds = self._find_block_bounds(lines, path)
        if not bounds:
            return config_text

        start_idx, end_idx, key_indent = bounds
        pending = dict(updates)

        for i in range(start_idx, end_idx):
            raw = lines[i].rstrip('\r\n')
            if self._is_blank_or_comment(raw):
                continue
            if self._line_indent(raw) != key_indent:
                continue

            for key in list(pending.keys()):
                if re.match(rf'^\s*{re.escape(key)}\s*:', raw):
                    lines[i] = self._replace_scalar_line(lines[i], key, pending.pop(key))
                    break

            if not pending:
                break

        return ''.join(lines)

    def _update_rtlsdr_device_in_text(self, config_text: str, serial: str, updates: Dict[str, object]) -> str:
        """Update one RTL-SDR device entry by serial without rewriting the full YAML file."""
        lines = config_text.splitlines(keepends=True)
        bounds = self._find_block_bounds(lines, ['sdr', 'rtlsdr', 'devices'])
        if not bounds:
            return config_text

        start_idx, end_idx, _ = bounds
        device_start = None
        device_indent = None

        for i in range(start_idx, end_idx):
            raw = lines[i].rstrip('\r\n')
            match = re.match(r'^\s*-\s*serial\s*:\s*["\']?([^"\'#\r\n]+)["\']?(?:\s*#.*)?$', raw)
            if match and match.group(1).strip() == serial:
                device_start = i + 1
                device_indent = self._line_indent(raw)
                break

        if device_start is None or device_indent is None:
            return config_text

        device_end = end_idx
        for i in range(device_start, end_idx):
            raw = lines[i].rstrip('\r\n')
            if self._is_blank_or_comment(raw):
                continue
            if self._line_indent(raw) <= device_indent and raw.lstrip().startswith('- '):
                device_end = i
                break

        key_indent = device_indent + 2
        pending = dict(updates)
        for i in range(device_start, device_end):
            raw = lines[i].rstrip('\r\n')
            if self._is_blank_or_comment(raw):
                continue
            if self._line_indent(raw) != key_indent:
                continue

            for key in list(pending.keys()):
                if re.match(rf'^\s*{re.escape(key)}\s*:', raw):
                    lines[i] = self._replace_scalar_line(lines[i], key, pending.pop(key))
                    break

            if not pending:
                break

        return ''.join(lines)

    def _update_inline_fixed_channels(self, config_text: str, channels: List[dict]) -> str:
        """Update detection.fixed_channels inline list value in-place."""
        lines = config_text.splitlines(keepends=True)
        bounds = self._find_block_bounds(lines, ['detection'])
        if not bounds:
            return config_text

        start_idx, end_idx, key_indent = bounds
        rendered_items = []
        for ch in channels:
            parts = [f"frequency: {float(ch['frequency']):.3f}"]
            parts.append(f"type: {self._yaml_scalar(ch['type'])}")
            if 'enabled' in ch:
                parts.append(f"enabled: {str(ch['enabled']).lower()}")
            if 'rx_scan' in ch:
                parts.append(f"rx_scan: {str(ch['rx_scan']).lower()}")
            if 'receiver_device' in ch:
                parts.append(f"receiver_device: {self._yaml_scalar(ch['receiver_device'])}")
            rendered_items.append('{' + ', '.join(parts) + '}')
        
        rendered = '[' + ', '.join(rendered_items) + ']' if rendered_items else '[]'

        for i in range(start_idx, end_idx):
            raw = lines[i].rstrip('\r\n')
            if self._is_blank_or_comment(raw):
                continue
            if self._line_indent(raw) != key_indent:
                continue
            if re.match(r'^\s*fixed_channels\s*:', raw):
                lines[i] = self._replace_key_value_raw(lines[i], 'fixed_channels', rendered)
                break

        return ''.join(lines)

    def _load_history_from_log(self, logfile: str, serial: str, sonde_type: str) -> List[dict]:
        """Load historical telemetry points from existing log for track continuity and Kindle graphs."""
        history: List[dict] = []
        try:
            if not os.path.exists(logfile):
                return history

            current = {
                'serial': serial,
                'type': sonde_type,
                'lat': None,
                'lon': None,
                'alt': None,
                'vel_h': None,
                'vel_v': None,
                'heading': None,
                'frequency': None,
                'rssi': None,
                'snr': None,
                'sats': None,
                'batt': None,
                'timestamp': None,
            }

            with open(logfile, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('Frame '):
                        if current.get('lat') is not None and current.get('lon') is not None:
                            history.append(dict(current))
                        # New frame block
                        ts = None
                        if ' - ' in line:
                            ts = line.split(' - ', 1)[1].strip()
                        current = {
                            'serial': serial,
                            'type': sonde_type,
                            'lat': None,
                            'lon': None,
                            'alt': None,
                            'vel_h': None,
                            'vel_v': None,
                            'heading': None,
                            'frequency': None,
                            'rssi': None,
                            'snr': None,
                            'sats': None,
                            'batt': None,
                            'timestamp': ts,
                        }
                    elif line.startswith('Position:'):
                        try:
                            payload = line.split(':', 1)[1].strip()
                            lat_s, lon_s = [x.strip() for x in payload.split(',')]
                            current['lat'] = float(lat_s)
                            current['lon'] = float(lon_s)
                        except Exception:
                            pass
                    elif line.startswith('Altitude:'):
                        try:
                            current['alt'] = float(line.split(':', 1)[1].replace('m', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('Velocity H/V:'):
                        try:
                            parts = line.split(':', 1)[1].strip().split('/')
                            current['vel_h'] = float(parts[0].strip())
                            current['vel_v'] = float(parts[1].replace('m/s', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('Heading:'):
                        try:
                            current['heading'] = float(line.split(':', 1)[1].replace('°', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('Frequency:'):
                        try:
                            current['frequency'] = float(line.split(':', 1)[1].replace('MHz', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('RSSI:'):
                        try:
                            current['rssi'] = float(line.split(':', 1)[1].replace('dB', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('SNR:'):
                        try:
                            current['snr'] = float(line.split(':', 1)[1].replace('dB', '').strip())
                        except Exception:
                            pass
                    elif line.startswith('Satellites:'):
                        try:
                            current['sats'] = int(line.split(':', 1)[1].strip())
                        except Exception:
                            pass
                    elif line.startswith('Battery:'):
                        try:
                            current['batt'] = float(line.split(':', 1)[1].replace('V', '').strip())
                        except Exception:
                            pass

            if current.get('lat') is not None and current.get('lon') is not None:
                history.append(dict(current))
            
            # Add reception_time to all historical frames (set to current time when loaded)
            # This prevents immediate cleanup of archive data with old GPS timestamps
            load_time = datetime.utcnow().isoformat() + 'Z'
            for frame in history:
                frame['reception_time'] = load_time

            if len(history) > self.max_track_points:
                history = history[-self.max_track_points:]

            if history:
                self.logger.info(f"Loaded {len(history)} historical points for {serial} from {logfile} (with full telemetry)")
        except Exception as e:
            self.logger.warning(f"Failed to load history from {logfile}: {e}")
        return history

    def _sanitize_position_jump(self, serial: str, data: dict):
        """Reject implausible GPS jumps to avoid artificial gaps/spikes on the map."""
        lat = data.get('lat')
        lon = data.get('lon')
        if lat is None or lon is None:
            return

        history = self.sondes.get(serial, [])
        prev = None
        for item in reversed(history):
            if item.get('lat') is not None and item.get('lon') is not None:
                prev = item
                break
        if prev is None:
            return

        prev_lat = float(prev.get('lat'))
        prev_lon = float(prev.get('lon'))
        dist_m = self._haversine_m(prev_lat, prev_lon, float(lat), float(lon))

        # Estimate maximum plausible displacement using elapsed seconds.
        max_m = 3000.0  # 3 km baseline tolerance for sparse timestamps.
        try:
            t0_raw = prev.get('timestamp')
            t1_raw = data.get('timestamp')
            if t0_raw and t1_raw:
                t0 = datetime.fromisoformat(str(t0_raw).replace('Z', '+00:00'))
                t1 = datetime.fromisoformat(str(t1_raw).replace('Z', '+00:00'))
                dt = max(0.1, abs((t1 - t0).total_seconds()))
                # Radiosondes are far below this speed; keep generous headroom.
                max_m = max(max_m, 250.0 * dt)
        except Exception:
            pass

        if dist_m > max_m:
            self.logger.warning(
                f"Ignoring implausible position jump for {serial}: "
                f"{dist_m/1000:.2f} km > {max_m/1000:.2f} km"
            )
            data['lat'] = None
            data['lon'] = None

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Distance between two lat/lon coordinates in meters."""
        r = 6371000.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
        return 2.0 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))
    
    def _get_system_metrics(self) -> dict:
        """Get CPU and memory usage metrics"""
        metrics = {
            'cpu_percent': 0.0,
            'memory_percent': 0.0,
            'memory_used_mb': 0.0,
            'memory_total_mb': 0.0
        }
        
        if PSUTIL_AVAILABLE:
            try:
                # Get CPU percentage (non-blocking)
                metrics['cpu_percent'] = psutil.cpu_percent(interval=0)
                
                # Get memory info
                memory = psutil.virtual_memory()
                metrics['memory_percent'] = memory.percent
                metrics['memory_used_mb'] = memory.used / (1024 * 1024)
                metrics['memory_total_mb'] = memory.total / (1024 * 1024)
            except Exception as e:
                self.logger.error(f"Error getting system metrics: {e}")
        
        return metrics

    def _get_service_status_info(self, lines: int = 8) -> dict:
        """Read systemd status details for the service status modal."""
        unit = 'openwxsdr.service'
        active = subprocess.run(
            ['systemctl', 'is-active', unit],
            capture_output=True, text=True, check=False
        ).stdout.strip()
        enabled = subprocess.run(
            ['systemctl', 'is-enabled', unit],
            capture_output=True, text=True, check=False
        ).stdout.strip()
        status_lines = subprocess.run(
            ['systemctl', 'status', unit, '--no-pager', f'--lines={lines}'],
            capture_output=True, text=True, check=False
        ).stdout.strip().splitlines()
        summary = status_lines[0] if status_lines else ''

        loaded_line = ''
        active_line = ''
        main_pid_line = ''
        tasks_line = ''
        cpu_line = ''
        console_lines = []

        for line in status_lines:
            stripped = line.rstrip()
            if stripped:
                console_lines.append(stripped)
            text = stripped.strip()
            if text.startswith('Loaded:'):
                loaded_line = text
            elif text.startswith('Active:'):
                active_line = text
            elif text.startswith('Main PID:'):
                main_pid_line = text
            elif text.startswith('Tasks:'):
                tasks_line = text
            elif text.startswith('CPU:'):
                cpu_line = text

        return {
            'unit': unit,
            'active': active or 'unknown',
            'enabled': enabled or 'unknown',
            'summary': summary,
            'loaded_line': loaded_line,
            'active_line': active_line,
            'main_pid_line': main_pid_line,
            'tasks_line': tasks_line,
            'cpu_line': cpu_line,
            'console_status': '\n'.join(console_lines),
        }

    @staticmethod
    def _human_bytes(num_bytes) -> str:
        """Format a byte count as a compact human-readable string (e.g. 7.7 GB)."""
        try:
            n = float(num_bytes)
        except (TypeError, ValueError):
            return 'n/a'
        for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
            if abs(n) < 1024.0:
                return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} EB"

    def _get_memory_disk_info(self) -> dict:
        """Total RAM plus root-filesystem disk total/free, human-readable.
        Uses psutil for RAM when available (falls back to /proc/meminfo); disk
        uses stdlib shutil.disk_usage so it works without psutil."""
        ram_total = None
        if PSUTIL_AVAILABLE:
            try:
                ram_total = psutil.virtual_memory().total
            except Exception:
                ram_total = None
        if ram_total is None:
            try:
                with open('/proc/meminfo') as fh:
                    for line in fh:
                        if line.startswith('MemTotal:'):
                            ram_total = int(line.split()[1]) * 1024  # kB → bytes
                            break
            except Exception:
                ram_total = None

        disk_total = disk_free = None
        try:
            usage = shutil.disk_usage('/')
            disk_total, disk_free = usage.total, usage.free
        except Exception:
            pass

        return {
            'ram_total': self._human_bytes(ram_total) if ram_total else 'n/a',
            'disk_total': self._human_bytes(disk_total) if disk_total else 'n/a',
            'disk_free': self._human_bytes(disk_free) if disk_free else 'n/a',
        }

    def _get_host_info(self) -> dict:
        """Get host hardware and network identity for the service modal."""
        hostname = socket.gethostname()
        ip_address = '127.0.0.1'

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(('8.8.8.8', 80))
                ip_address = sock.getsockname()[0]
        except Exception:
            if PSUTIL_AVAILABLE:
                try:
                    for addrs in psutil.net_if_addrs().values():
                        for addr in addrs:
                            if getattr(addr, 'family', None) == socket.AF_INET and addr.address and not addr.address.startswith('127.'):
                                ip_address = addr.address
                                raise StopIteration
                except StopIteration:
                    pass
                except Exception:
                    pass

        return {
            'hostname': hostname,
            'ip_address': ip_address,
            'hardware': detect_host_hardware(),
        }
