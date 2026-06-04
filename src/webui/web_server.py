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
import platform
import socket
import subprocess
import logging
import threading
import math
import re
import hmac
from flask import Flask, render_template, jsonify, request, send_file
from flask_cors import CORS
from typing import Dict, List, Set
from datetime import datetime

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from ..decoders.models import SondeTelemetry


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
        self.active_frequencies = set()  # Currently active frequencies
        self.lock = threading.Lock()
        
        # Per-sonde log files
        self.sonde_logfiles: Dict[str, str] = {}  # serial -> log file path
        
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
        
        # Apply initial logging levels from config
        self._apply_logging_levels()
        
        # External URL settings
        webui_config = config.get('webui', {})
        self.external_url_provider = str(webui_config.get('external_url_provider', 'openwx'))
        self.external_url_custom = str(webui_config.get('external_url_custom', ''))
        self.settings_password = str(webui_config.get('settings_password', '') or '')
        
        # References to other components for health monitoring
        self.spectrum_analyzer = None
        self.decoder_manager = None
        self.flux242_receiver = None
        self.mqtt_output = None
        self.sondehub_output = None
        
        # Get absolute paths for templates and static files
        # This ensures paths work regardless of working directory
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(os.path.join(current_dir, '../..'))
        templates_dir = os.path.join(project_root, 'templates')
        static_dir = os.path.join(project_root, 'static')
        
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
                                 callsign=station_cfg.get('callsign', ''),
                                 version='1.0.46')
        
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
                        'temp': latest.get('temp'),
                        'humidity': latest.get('humidity'),
                        'pressure': latest.get('pressure'),
                        'frame': latest.get('frame', 0),
                        'path': [[p.get('lat'), p.get('lon')] for p in data if p.get('lat') and p.get('lon')],
                        'timestamp': latest.get('timestamp')
                    }
                    sondes.append(sonde_info)
                
                return jsonify({
                    'sondes': sondes,
                    'count': len(sondes),
                    'total_frames': self.total_frames_received,
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
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
        
        @self.app.route('/api/status')
        def get_status():
            """Get system status"""
            with self.lock:
                active_sondes = len(self.sondes)
                total_frames = self.total_frames_received
                # Get unique active frequencies
                frequencies = list(self.active_frequencies)
            
            # Get system metrics
            system_metrics = self._get_system_metrics()
            
            return jsonify({
                'active_sondes': active_sondes,
                'total_frames': total_frames,
                'frequencies': frequencies,
                'cpu_percent': system_metrics['cpu_percent'],
                'memory_percent': system_metrics['memory_percent'],
                'memory_used_mb': system_metrics['memory_used_mb'],
                'memory_total_mb': system_metrics['memory_total_mb'],
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            })
        
        @self.app.route('/api/health')
        def get_health():
            """Get system health status"""
            with self.lock:
                # Get last frame time
                last_frame_time = None
                for serial, data in self.sondes.items():
                    if data and 'timestamp' in data[-1]:
                        frame_time = data[-1]['timestamp']
                        if last_frame_time is None or frame_time > last_frame_time:
                            last_frame_time = frame_time
                
                # Check if using flux242 mode
                if self.flux242_receiver is not None:
                    # Flux242 mode
                    decoder_status = 'flux242'
                    active_decoders = len(self.flux242_receiver.active_sondes) if hasattr(self.flux242_receiver, 'active_sondes') else 0
                    rtlsdr_status = 'flux242' if self.flux242_receiver.running else 'disconnected'
                else:
                    # Standard RTL-SDR / KA9Q mode
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
                    'last_frame_time': last_frame_time,
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                })
        
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

            # Check if using flux242 mode
            if self.flux242_receiver is not None:
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
                            'present':      present
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
        
        @self.app.route('/api/config')
        def get_config():
            """Get current configuration"""
            detection_cfg = self.config.get('detection', {})
            return jsonify({
                'sdr_type': self.config['sdr']['type'],
                'airspy_available': self.config.get('sdr', {}).get('airspy_support', False),
                'fixed_channels_enable': detection_cfg.get('fixed_channels_enable'),
                'success': True
            })

        @self.app.route('/api/reset_statistics', methods=['POST'])
        def reset_statistics():
            """Reset UI statistics counters and tracked active sondes/frequencies."""
            try:
                with self.lock:
                    self.sondes.clear()
                    self.total_frames_received = 0
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
                auth_error = self._require_settings_authorization()
                if auth_error is not None:
                    return auth_error
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
                    return jsonify({'success': True, 'device': airspy_id})

                if not hasattr(self.decoder_manager, '_workers'):
                    return jsonify({'success': False, 'error': 'Start scanning not supported for this SDR type'})

                from ..sdr.device_manager import DeviceWorker
                for worker in self.decoder_manager._workers:
                    if device_serial and worker.device_serial != device_serial:
                        continue
                    if worker.state in (DeviceWorker.STATE_IDLE, DeviceWorker.STATE_DECODING):
                        worker.stop_decode_and_scan()
                        self.logger.info(f"Triggered scanning on device {worker.device_serial}")
                        return jsonify({'success': True, 'device': worker.device_serial})
                    elif worker.state == DeviceWorker.STATE_SCANNING:
                        return jsonify({'success': True, 'device': worker.device_serial, 'message': 'Already scanning'})

                return jsonify({'success': False, 'error': 'Device not found'})

            except Exception as e:
                self.logger.error(f"Error starting scan: {e}", exc_info=True)
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/update_config', methods=['POST'])
        def update_config():
            """Update configuration and restart service"""
            try:
                import subprocess
                auth_error = self._require_settings_authorization()
                if auth_error is not None:
                    return auth_error
                
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
                auth_error = self._require_settings_authorization()
                if auth_error is not None:
                    return auth_error
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

        @self.app.route('/api/service_status')
        def get_service_status():
            """Return OPENWXSDR systemd status for the Service Status modal."""
            try:
                status_info = self._get_service_status_info(lines=8)
                host_info = self._get_host_info()

                return jsonify({
                    'success': True,
                    **status_info,
                    **host_info,
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
                return jsonify({'files': files})
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

    def _require_settings_authorization(self):
        """Return a Flask response tuple when settings write access is denied."""
        if not self.settings_password:
            return None

        provided_password = request.headers.get('X-Settings-Password', '')
        provided_bytes = str(provided_password).encode('utf-8')
        expected_bytes = self.settings_password.encode('utf-8')
        if hmac.compare_digest(provided_bytes, expected_bytes):
            return None

        self.logger.warning("Denied settings write request due to missing/invalid password")
        return jsonify({'success': False, 'error': 'Unauthorized settings access'}), 401
    
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
                       mqtt_output=None, sondehub_output=None):
        """Set references to other components for health monitoring"""
        self.spectrum_analyzer = spectrum_analyzer
        self.decoder_manager = decoder_manager
        self.flux242_receiver = flux242_receiver
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
            
            # Initialize list for new sonde
            if serial not in self.sondes:
                self.sondes[serial] = []
                
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
                                f.write(f"Sonde: {serial} ({telemetry.sonde_type})\n")
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
            self._sanitize_position_jump(serial, data)
            self.sondes[serial].append(data)
            
            # Write telemetry to log file
            if serial in self.sonde_logfiles:
                try:
                    with open(self.sonde_logfiles[serial], 'a') as f:
                        # Format telemetry data
                        f.write(f"Frame {telemetry.frame_number} - {data.get('timestamp', 'N/A')}\n")
                        if data.get('lat') is not None and data.get('lon') is not None:
                            f.write(f"  Position: {data['lat']:.5f}, {data['lon']:.5f}\n")
                        if data.get('alt') is not None:
                            f.write(f"  Altitude: {data['alt']:.1f} m\n")
                        if data.get('vel_h') is not None:
                            f.write(f"  Velocity H/V: {data['vel_h']:.1f}/{data.get('vel_v', 0):.1f} m/s\n")
                        if data.get('heading') is not None:
                            f.write(f"  Heading: {data['heading']:.0f}°\n")
                        if data.get('frequency'):
                            f.write(f"  Frequency: {data['frequency']:.3f} MHz\n")
                        if data.get('snr') is not None:
                            f.write(f"  SNR: {data['snr']:.1f} dB\n")
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
            
            # Check last update time
            last_frame = data[-1]
            if 'timestamp' in last_frame:
                try:
                    last_time = datetime.fromisoformat(last_frame['timestamp'].replace('Z', '+00:00'))
                    age_seconds = (current_time - last_time.replace(tzinfo=None)).total_seconds()
                    
                    # Use configured retention time instead of hardcoded value
                    if age_seconds > self.sonde_retention_time:
                        to_remove.append(serial)
                        self.logger.info(f"Removing sonde {serial} - no data for {age_seconds:.0f}s (retention: {self.sonde_retention_time}s)")
                        # Log last frame before removal
                        if serial in self.sonde_last_frames:
                            last_data = self.sonde_last_frames[serial]
                            self._log_action('sonde_stopped', {
                                'serial': serial,
                                'frequency_mhz': round(last_data.get('frequency', 0), 3),
                                'sonde_type': last_data.get('sonde_type', ''),
                                'last_frame': last_data.get('frame', 0),
                                'lat': round(last_data.get('lat', 0), 5) if last_data.get('lat') else None,
                                'lon': round(last_data.get('lon', 0), 5) if last_data.get('lon') else None,
                                'alt': round(last_data.get('alt', 0), 1) if last_data.get('alt') else None,
                                'reason': 'signal_lost'
                            })
                            del self.sonde_last_frames[serial]
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
        """Load historical telemetry points from existing log for track continuity."""
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
                'frequency': None,
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
                            'frequency': None,
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
                    elif line.startswith('Frequency:'):
                        try:
                            current['frequency'] = float(line.split(':', 1)[1].replace('MHz', '').strip())
                        except Exception:
                            pass

            if current.get('lat') is not None and current.get('lon') is not None:
                history.append(dict(current))

            if len(history) > self.max_track_points:
                history = history[-self.max_track_points:]

            if history:
                self.logger.info(f"Loaded {len(history)} historical points for {serial} from {logfile}")
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
            'hardware': self._detect_host_hardware(),
        }

    def _detect_host_hardware(self) -> str:
        """Best-effort hardware description for the host machine."""
        for path in ('/sys/firmware/devicetree/base/model', '/proc/device-tree/model'):
            try:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
                        model = handle.read().strip('\x00\n\r ')
                        if model:
                            return model
            except Exception:
                pass

        processor = platform.processor().strip()
        machine = platform.machine().strip()
        system_name = platform.system().strip()
        release = platform.release().strip()

        details = ' '.join(part for part in [processor, machine] if part)
        if not details:
            details = machine or system_name or 'Unknown hardware'
        if system_name or release:
            details = f"{details} ({system_name} {release})".strip()
        return details
