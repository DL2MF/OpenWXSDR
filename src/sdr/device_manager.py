"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : device_manager.py
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
#  RTL-SDR device manager module for OpenWX.
#
#  Each physical RTL-SDR device operates as an independent scan → detect →
#  decode worker. There is no fixed scanner or decoder role: any free device
#  scans for signals, and upon detection switches autonomously to decoding.
#  When a sonde disappears or the decoder goes stale, the device returns to
#  scanning.
#
#  Architecture:
#
#  RTLSDRDeviceManager
#    ├── SondeRegistry   – thread-safe "which frequencies are already being decoded"
#    ├── DeviceWorker[0] – serial RTL00001, state: SCANNING / DECODING
#    ├── DeviceWorker[1] – serial RTL00002, …
#    ├── DeviceWorker[2] – serial RTL00003, …	
#    └── DeviceWorker[3] – serial RTL00004, …	
#
#  DeviceWorker state machine
#  --------------------------
#    IDLE ──► SCANNING ──► DECODING ──► SCANNING ──► …
#                │               │
#                └───────────────┘  (decoder dies / stale)
#  Classes:
#
#  SondeRegistry     Thread-safe frequency claim registry (±50 kHz tolerance).
#  ActiveDecoder     Snapshot dataclass; compatible with web_server.py API.
#  DeviceWorker      Per-device IDLE → SCANNING → DECODING state machine.
#  RTLSDRDeviceManager  Top-level manager; spawns workers, exposes web API.
#
#  Decoder backend   : rs1729 (RS41, DFM09, M10, iMet-C, ...)
#  Sonde detection   : SpectrumAnalyzer (pyrtlsdr FFT) + DftDetector
#
# =============================================================================
"""

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .rtlsdr_analyzer import DetectedSignal, SpectrumAnalyzer
from .rtl_power_scanner import RtlPowerScanner
from .audio_pipeline import AudioPipeline
from .dft_detector import DftDetector
from ..decoders.rs1729_decoder import RS1729Decoder
from ..decoders.models import (
    SondeTelemetry, SondePosition, SondeVelocity, SondeEnvironment
)
from ..import_api.sonde_api_client import SondeApiClient


# ---------------------------------------------------------------------------
# Shared sonde frequency registry
# ---------------------------------------------------------------------------

class SondeRegistry:
    """
    Thread-safe set of frequencies that are currently being decoded.
    Uses a tolerance window so small FFT drift doesn't cause double-decoding.
    """

    TOLERANCE_HZ = 20_000   # 20 kHz minimum gap between decoders

    def __init__(self):
        self._entries: Dict[float, bool] = {}
        self._lock = threading.Lock()

    def _find_near(self, freq_hz: float) -> Optional[float]:
        for f in self._entries:
            if abs(f - freq_hz) < self.TOLERANCE_HZ:
                return f
        return None

    def is_active(self, freq_hz: float) -> bool:
        with self._lock:
            return self._find_near(freq_hz) is not None

    def register(self, freq_hz: float) -> bool:
        """Atomically claim freq_hz.  Returns False if already claimed."""
        with self._lock:
            if self._find_near(freq_hz) is not None:
                return False
            self._entries[freq_hz] = True
            return True

    def unregister(self, freq_hz: float):
        with self._lock:
            key = self._find_near(freq_hz)
            if key is not None:
                del self._entries[key]

    def active_frequencies(self) -> List[float]:
        with self._lock:
            return list(self._entries.keys())


# ---------------------------------------------------------------------------
# ActiveDecoder – web_server.py-compatible snapshot
# ---------------------------------------------------------------------------

@dataclass
class ActiveDecoder:
    """Snapshot of a running decoder, compatible with web_server.py expectations."""
    decoder: object        # RS1729Decoder  (.running, .sonde_type)
    signal: object         # DetectedSignal (.frequency)
    start_time: float
    last_update: float
    audio_pipeline: object  # AudioPipeline
    device_serial: str


# ---------------------------------------------------------------------------
# Per-device worker
# ---------------------------------------------------------------------------

class DeviceWorker:
    """
    Manages one RTL-SDR device through independent scan → detect → decode cycles.

    SCANNING state: pyrtlsdr is open; spectrum is captured repeatedly to find
                    new radiosonde signals not already claimed by another worker.

    DECODING state: pyrtlsdr is closed; rtl_fm + rs1729 decoder are running for
                    the detected sonde.  Returns to SCANNING when the decoder
                    dies or goes silent.
    """

    STATE_IDLE     = 'idle'
    STATE_SCANNING = 'scanning'
    STATE_DECODING = 'decoding'
    STATE_ERROR    = 'error'  # Device open/test-read failed; not actually scanning

    # ~2-3 minutes of retries (mix of 10s/15s backoffs) before giving up on a
    # device and self-restarting the whole service. LIBUSB_ERROR_BUSY has never
    # been observed to self-recover within the process.
    MAX_CONSECUTIVE_OPEN_FAILURES = 10
    # If capture_spectrum() stays in-flight longer than this, a libusb read is
    # wedged (unrecoverable in-process). Well above any healthy capture: the
    # scan_max_wall_s cap (default 8s) bounds a normal averaged capture, so only
    # a genuine hung read reaches 45s.
    CAPTURE_HANG_TIMEOUT_S = 45.0

    def __init__(self, device_config: dict, app_config: dict,
                 sonde_registry: SondeRegistry,
                 telemetry_callback: Callable[[SondeTelemetry], None],
                 device_index: int = 0,
                 manager=None):
        self.device_config   = device_config
        self.device_serial   = device_config['serial']
        self.device_index    = device_index  # Used for staggered USB initialization
        self.app_config      = app_config
        self.registry        = sonde_registry
        self.telemetry_cb    = telemetry_callback
        self._manager        = manager  # Reference to RTLSDRDeviceManager for fixed_channels check
        self.logger          = logging.getLogger(f'Worker.{self.device_serial}')

        # RS41 bandwidth fast-path (see _identify_sonde_type). Skips the ~15s
        # dft_detect step for signals whose 3 dB width is unambiguously RS41,
        # restoring V1.0.50's instant RS41 start. DEFAULT OFF: it trades the
        # correlation check for a bandwidth guess, which is exactly what let a
        # narrow-measuring DFM be started as RS41 (the misclassification we kept
        # chasing). With it off, every candidate is classified purely by
        # dft_detect (auto_rx's method) — more reliable, ~15s slower per RS41.
        # Re-enable per gateway with detection.rs41_fastpath: true where the
        # speed matters and DFM confusion isn't a problem.
        det_cfg = app_config.get('detection', {})
        self.RS41_FASTPATH_ENABLED = bool(det_cfg.get('rs41_fastpath', False))
        self.RS41_FASTPATH_BW_MIN = float(det_cfg.get('rs41_fastpath_bw_min_hz', 3500))
        self.RS41_FASTPATH_BW_MAX = float(det_cfg.get('rs41_fastpath_bw_max_hz', 7000))

        # Scan backend (Phase 1): 'welch' (pyrtlsdr + Welch, per-2.4 MHz-segment,
        # default) or 'rtl_power' (full-band rtl_power sweep — one dongle sees the
        # whole band every scan, no band-sweep gaps). rtl_power is time-shared
        # with decoding on the same device; the detect/dispatch path downstream is
        # identical. Falls back to 'welch' automatically if the binary is missing.
        scanner_cfg = det_cfg.get('scanner', {}) or {}
        self._scan_backend = str(scanner_cfg.get('backend', 'welch')).strip().lower()
        if self._scan_backend not in ('welch', 'rtl_power'):
            self._scan_backend = 'welch'
        self._rtl_power_cfg = scanner_cfg
        self._rtl_power_scanner: Optional[RtlPowerScanner] = None
        self._rtl_power_fail_streak = 0   # consecutive rtl_power scan failures
        # A device-less SpectrumAnalyzer used ONLY for detect_signals/config on
        # the rtl_power arrays (never opens a pyrtlsdr handle).
        self._detect_analyzer: Optional[SpectrumAnalyzer] = None

        # Band-sweep (opt-in): a device starts at its configured center_freq and,
        # after dwell_empty_cycles consecutive scans with no decode, retunes to
        # the next segment center covering the whole radiosonde band. This lets
        # ANY single device eventually cover the entire band, so if another SDR
        # fails the survivors close its coverage gap on their own. Off by default
        # so the tested static-scan path is unchanged until explicitly enabled.
        self._sweep_cfg = det_cfg.get('band_sweep', {}) or {}
        self._sweep_enabled = bool(self._sweep_cfg.get('enabled', False))
        self._sweep_dwell = max(1, int(self._sweep_cfg.get('dwell_empty_cycles', 3)))
        self._sweep_centers = []        # built lazily once sample_rate is known
        self._sweep_index = 0
        self._empty_scan_count = 0

        # Frequency-repository listing: a lower threshold than the decode
        # threshold so weak candidates get LISTED (not auto-decoded), plus a
        # channel-grid filter so unconfirmed RTL spurs on odd (non-100 kHz)
        # frequencies aren't listed. Decoded sondes are logged as 'confirmed'
        # regardless of grid.
        self._repo_threshold_db = float(det_cfg.get('repository_threshold_db', 8.0))
        # Coarse channel grid accepted in EVERY segment (100 kHz sonde channels).
        self._repo_grid_hz = float(det_cfg.get('repository_grid_hz', 100_000))
        self._repo_grid_tol_hz = float(det_cfg.get('repository_grid_tol_hz', 5_000))
        # Fine grid (10 kHz) accepted only inside dense segments — the 403.0-404.0
        # DFM/military band commonly uses 10 kHz channels (403.13, 403.55, …). The
        # 404-406/401-403 segments use 10 kHz only rarely, so there we keep to the
        # coarse grid to reject the RTL's 10 kHz-grid spurs (x.x40/x.x60 …).
        self._repo_dense_grid_hz = float(det_cfg.get('repository_dense_grid_hz', 10_000))
        self._repo_dense_grid_tol_hz = float(det_cfg.get('repository_dense_grid_tol_hz', 3_000))
        self._repo_dense_ranges = [
            (float(lo), float(hi)) for lo, hi in
            det_cfg.get('repository_dense_ranges', [[403_000_000, 404_000_000]])
        ]

        self._state          = self.STATE_SCANNING  # Start in SCANNING state (will open USB on first cycle)
        self._running        = False
        self._thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._first_usb_init = True  # Flag to track first USB device open
        self._device_lock = threading.Lock()  # CRITICAL: Prevent USB race conditions
        # Consecutive failed device-open attempts (init failure, test-read timeout/
        # error/zero-samples). Once a device hits LIBUSB_ERROR_BUSY it has never been
        # observed to recover within the process — the USB interface claim is leaked
        # by a thread stuck in the C extension and can only be released by the kernel
        # when the process exits. Tracked so we can self-restart via systemd instead
        # of polling a permanently dead device forever. Reset on any successful open.
        self._consecutive_open_failures = 0
        self._manual_decode_pending = threading.Event()  # Signals scan cycle to abort early
        # True while the worker thread is inside capture_spectrum() (an UNLOCKED
        # libusb read). Any code that closes the analyzer from another thread
        # (manual decode teardown) must wait for this to clear first — closing
        # the device while a read transfer is in flight crashes with SIGBUS.
        self._capturing = False
        # Wall-clock time capture_spectrum() was entered. A watchdog thread uses
        # this to detect a WEDGED capture: on flaky USB, a single libusb read
        # inside capture_spectrum() can block forever (the scan_max_wall_s cap is
        # checked BETWEEN reads, so it can't break a hung read). Python can't
        # interrupt a blocked C read, and the SIGBUS guard (correctly) refuses to
        # close the device mid-read — so without this watchdog the device stays
        # dead for the whole process lifetime (observed overnight: RTL00001 stuck
        # in capture for hours, every Import-API assignment aborting). The
        # watchdog escalates to os._exit(1) so systemd restarts and releases the
        # stuck interface — the same clean recovery the old SIGBUS used to force.
        self._capture_started_at = 0.0
        # Set once the watchdog quarantines this device (permanently wedged read).
        # Guards the run loop so the abandoned worker never resumes scanning even
        # if the stuck read eventually returns.
        self._wedged = False
        # True only during a deliberate USB-reset recovery: the in-flight read
        # will error with LIBUSB_ERROR_NO_DEVICE (expected), so the scan-cycle
        # handler logs it calmly and does NOT count it as an open failure.
        self._recovering = False

        # Active scanning components
        self._analyzer: Optional[SpectrumAnalyzer] = None
        self._last_spectrum: dict = {}
        self._spectrum_lock = threading.Lock()

        # Active decoding components
        self._pipeline: Optional[AudioPipeline] = None
        self._decoder:  Optional[RS1729Decoder] = None
        self._cur_freq: Optional[float] = None
        self._cur_type: Optional[str]  = None
        # True when the current decode's type came from the RS41 BW fast-path
        # (not dft_detect). If such a decode yields 0 frames, the manager forces
        # dft_detect on the next detection of that frequency (fixes DFM misID).
        self._cur_used_fastpath = False
        self._cur_serial: Optional[str] = None
        self._cur_signal_strength_db: Optional[float] = None   # SNR (dB over noise)
        self._cur_signal_power_dbfs: Optional[float] = None     # absolute peak power → RSSI
        self._decode_start   = 0.0
        self._last_frame_t   = 0.0
        self._last_state     = self.STATE_IDLE  # Track previous state for transition delays

        # Timing
        cfg_rx = app_config.get('receivers', {})
        cfg_dec = app_config.get('decoders', {})
        self._scan_interval  = cfg_rx.get('scan_interval', 15)
        self._idle_timeout   = cfg_dec.get('max_idle_time', 300)   # seconds without frames → back to scan
        # CRITICAL: manual/imported decoders with duration=None ("decode until
        # sonde lost") previously had NO staleness check at all — only the
        # decoder subprocess dying released the device. If the process keeps
        # running without ever producing another valid frame (sonde landed,
        # out of range, etc.), the device stayed stuck in DECODING forever.
        # Give these a much longer, separately configurable idle timeout
        # instead of none at all.
        self._manual_idle_timeout = cfg_dec.get('manual_idle_time', 1800)  # 30 min default

        # Optional USB-reset auto-recovery for a WEDGED device (default OFF).
        # A wedged capture_spectrum() is a stuck libusb read that nothing
        # in-process can interrupt — only a USB port reset frees it. When
        # enabled, the watchdog resets the dongle's USB port (matched by serial
        # via sysfs, then USBDEVFS_RESET), which aborts the stuck read, then
        # un-quarantines the device so its worker reopens it and resumes scanning
        # instead of staying dead until a full service restart. Attempt-limited
        # so a chronically-bad dongle can't reset-loop forever.
        cfg_recovery = app_config.get('recovery', {})
        self._usb_reset_on_wedge = bool(cfg_recovery.get('usb_reset_on_wedge', False))
        self._usb_reset_settle_s = float(cfg_recovery.get('usb_reset_settle_s', 8.0))
        self._usb_reset_max_attempts = int(cfg_recovery.get('usb_reset_max_attempts', 3))
        self._usb_reset_attempts = 0
        self._decode_expiration_time: Optional[float] = None  # For duration-limited decoding
        self._is_manual_decoder: bool = False  # Manual decoders ignore the short auto-detect idle timeout
        # How this device's current (or most recent) decode was started —
        # 'auto' (spectrum scan), 'manual' (web UI entry or Import API
        # assignment), 'priority' (detection.priority_frequency), or
        # 'fixed_channel' (detection.fixed_channels table). Used only to
        # color-code the web UI's SDR Devices table; has no behavioral effect.
        self._decode_source: Optional[str] = None

        # DFT detector for sonde type identification
        det_cfg = app_config.get('detection', {})
        if det_cfg.get('use_dft_detect', True):
            # detect_confirm_time is the short, per-candidate correlation
            # check duration — dft_sample_duration is kept as a fallback so
            # existing configs don't need editing.
            self._dft = DftDetector(
                dft_detect_path=det_cfg.get('dft_detect_path', 'dft_detect'),
                sample_duration=det_cfg.get('detect_confirm_time', det_cfg.get('dft_sample_duration', 5.0)),
                # Per-station correlation threshold overrides. Optional: when
                # absent, DftDetector.DEFAULT_THRESHOLDS (auto_rx's calibrated
                # values) apply, so existing configs keep working unchanged.
                thresholds=det_cfg.get('dft_thresholds') or None,
            )
        else:
            self._dft = None

        # Frequency blacklist (Hz)
        bl = det_cfg.get('frequency_blacklist', [])
        self._blacklist = [f * 1e6 for f in bl]
        
        # RX Scan cycling (Phase 2)
        self._rx_scan_enabled = False
        self._rx_scan_channels: List[dict] = []
        self._rx_scan_index = 0
        self._fixed_channel_scantime = int(det_cfg.get('fixed_channel_scantime', 60))

        # Channelizer mode (Step 3: Device Manager Integration)
        self._decoder_mode = device_config.get('decoder_mode', 'legacy')
        self._channelizer: Optional['IqDecChannelizer'] = None  # Will be instantiated if mode='channelizer'
        self._channelizer_manual_requests = queue.Queue()  # Manual decode requests for channelizer mode
        if self._decoder_mode == 'channelizer':
            # Import here to avoid circular dependency
            from .channelizer import IqDecChannelizer
            self._channelizer = IqDecChannelizer(
                device_config, app_config, telemetry_callback, self.device_serial, device_index
            )
            self.logger.info(f"Channelizer mode enabled (max {self._channelizer.max_channels} channels)")
        else:
            self.logger.info(f"Legacy mode enabled (single-channel)")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'Worker-{self.device_serial}'
        )
        self._thread.start()
        # Wedged-capture watchdog: runs on its own thread (the worker thread is
        # the one that can get stuck inside a hung read, so it can't watch
        # itself). See CAPTURE_HANG_TIMEOUT_S / _capture_started_at.
        self._watchdog_thread = threading.Thread(
            target=self._capture_watchdog, daemon=True,
            name=f'Watchdog-{self.device_serial}'
        )
        self._watchdog_thread.start()
        self.logger.info(f"Started (device {self.device_serial})")

    def _capture_watchdog(self):
        """Quarantine a device whose capture_spectrum() has wedged.

        A single libusb read inside capture_spectrum() can block forever on
        flaky USB. That read is a C call the worker thread is stuck in, so
        nothing in-process can interrupt it, and the SIGBUS guard rightly refuses
        to close the device mid-read. Rather than kill the whole service (which
        would take the other, healthy dongles down with it and thrash-restart on
        a chronically bad device — observed: RTL00001 wedged all night while its
        3 siblings were fine), we QUARANTINE just this device: mark it ERROR and
        set _wedged so it's excluded from Import-API assignment and never
        re-scanned. The stuck worker thread is abandoned (its USB interface is
        leaked until the next full restart), but the other devices keep decoding
        and the Import API assigns nearby sondes to a healthy receiver.

        Safety net: if EVERY device has wedged there's nothing left to decode
        with, so fall back to os._exit(1) and let systemd bring it all back."""
        while self._running:
            time.sleep(5.0)
            if not self._capturing or self._wedged:
                continue
            age = time.time() - self._capture_started_at
            if age <= self.CAPTURE_HANG_TIMEOUT_S:
                continue

            self._wedged = True
            self._state = self.STATE_ERROR
            self.logger.critical(
                f"Device {self.device_serial} WEDGED: capture_spectrum() in-flight "
                f"{age:.0f}s (>{self.CAPTURE_HANG_TIMEOUT_S:.0f}s) — a libusb read is "
                "stuck and cannot be interrupted. Quarantining this device (marked "
                "ERROR, excluded from assignment); other devices keep running."
            )

            # Optional USB-reset auto-recovery: reset the dongle's USB port to
            # free the stuck read, then un-quarantine it. Keep the watchdog alive
            # (continue, don't return) so a re-wedge after recovery is caught
            # again, up to the attempt limit.
            if self._usb_reset_on_wedge and \
                    self._usb_reset_attempts < self._usb_reset_max_attempts:
                self._usb_reset_attempts += 1
                self.logger.warning(
                    f"USB-reset recovery attempt {self._usb_reset_attempts}/"
                    f"{self._usb_reset_max_attempts} for wedged {self.device_serial}…"
                )
                threading.Thread(
                    target=self._attempt_usb_recovery, daemon=True,
                    name=f"USBRecover.{self.device_serial}"
                ).start()
                continue

            # If all sibling workers are also wedged/errored, nothing can decode —
            # restart the whole service to release every stuck interface.
            try:
                workers = getattr(self._manager, '_workers', None) or []
                if workers and all(
                    getattr(w, '_wedged', False) or w._state == self.STATE_ERROR
                    for w in workers
                ):
                    self.logger.critical(
                        "All devices wedged/errored — restarting service "
                        "(systemd Restart=on-failure will bring it back up)."
                    )
                    os._exit(1)
            except Exception:
                pass
            return  # device abandoned; nothing more to watch

    def _attempt_usb_recovery(self):
        """Recover a WEDGED device by resetting its USB port, then un-quarantine
        it. Spawned by the watchdog in its own thread. The USB reset aborts the
        stuck libusb read (freeing the abandoned worker); after a settle we drop
        the analyzer (forcing a clean reopen) and clear _wedged so the run loop
        resumes scanning. If reset fails, the device stays quarantined (existing
        behaviour)."""
        serial = self.device_serial
        # Set BEFORE the reset so the read that errors out (NO_DEVICE) is logged
        # calmly by the scan-cycle handler rather than as a failure+traceback.
        self._recovering = True
        try:
            try:
                ok = self._usb_reset_by_serial(serial)
            except Exception as e:
                self.logger.error(f"USB reset raised for {serial}: {e}")
                ok = False
            if not ok:
                self.logger.error(
                    f"USB reset failed for {serial}; device stays quarantined "
                    "(retries on next wedge, or clears on full restart)."
                )
                return
            # Let the aborted read propagate through the stuck worker and the OS
            # re-enumerate the dongle before the worker reopens it.
            time.sleep(self._usb_reset_settle_s)
            try:
                with self._device_lock:
                    self._teardown_scan()
            except Exception as e:
                self.logger.debug(f"teardown during USB recovery of {serial}: {e}")
            # Un-quarantine: the run loop reopens the device and resumes scanning.
            self._capturing = False
            self._capture_started_at = 0.0
            self._wedged = False
            self._state = self.STATE_SCANNING
            self.logger.warning(
                f"USB-reset recovery complete for {serial} — un-quarantined, resuming "
                f"scan (attempt {self._usb_reset_attempts}/{self._usb_reset_max_attempts})."
            )
        finally:
            self._recovering = False

    @staticmethod
    def _usb_reset_by_serial(serial: str) -> bool:
        """Issue USBDEVFS_RESET to the RTL-SDR whose EEPROM serial == `serial`.
        Locates the device via sysfs (NO libusb handle needed — the wedged
        worker still holds the pyrtlsdr handle), so it works while the device is
        'open': the port reset forces the stuck bulk transfer to error out.
        Returns True on a successful reset. Needs write access to
        /dev/bus/usb/<bus>/<dev> (root, or the rtl-sdr udev rules)."""
        import glob
        import fcntl
        USBDEVFS_RESET = 0x5514  # _IO('U', 20)
        _log = logging.getLogger('USBReset')

        node = None
        for dev_dir in glob.glob('/sys/bus/usb/devices/*'):
            try:
                with open(os.path.join(dev_dir, 'serial')) as fh:
                    if fh.read().strip() != serial:
                        continue
                with open(os.path.join(dev_dir, 'busnum')) as fh:
                    busnum = int(fh.read().strip())
                with open(os.path.join(dev_dir, 'devnum')) as fh:
                    devnum = int(fh.read().strip())
                node = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
                break
            except (FileNotFoundError, ValueError, OSError):
                continue  # not a USB device with a serial file, or transient

        if node is None:
            _log.error(f"No USB device with serial '{serial}' in sysfs — cannot reset")
            return False

        fd = None
        try:
            fd = os.open(node, os.O_WRONLY)
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            _log.warning(f"USBDEVFS_RESET issued to {serial} at {node}")
            return True
        except PermissionError:
            _log.error(f"Permission denied resetting {node} for {serial} — run the "
                       "service as root or add udev permissions.")
            return False
        except OSError as e:
            _log.error(f"USB reset ioctl failed for {serial}: {e}")
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass

    def stop(self):
        self._running = False
        # Cleanup based on decoder mode
        if self._decoder_mode == 'channelizer' and self._channelizer:
            self.logger.info("Stopping channelizer")
            self._channelizer.stop()
        else:
            # Legacy mode cleanup
            self._teardown_decode()
            self._teardown_scan()
        if self._thread:
            self._thread.join(timeout=10)
        self._state = self.STATE_IDLE

    # ------------------------------------------------------------------
    # Public read-only state
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_freq(self) -> Optional[float]:
        return self._cur_freq

    @property
    def current_sonde_type(self) -> Optional[str]:
        return self._cur_type

    @property
    def current_sonde_serial(self) -> Optional[str]:
        return self._cur_serial

    @property
    def decode_source(self) -> Optional[str]:
        """'auto', 'manual', 'import_api', 'priority', or 'fixed_channel' —
        how the current decode was started, or None while idle/scanning.
        Web UI color-coding only."""
        return self._decode_source

    @property
    def decoder_mode(self) -> str:
        """Return decoder mode: 'legacy' or 'channelizer'"""
        return self._decoder_mode

    @property
    def channelizer_active_channels(self) -> int:
        """Return number of active channelizer channels (0 if legacy mode)"""
        if self._channelizer:
            return self._channelizer.get_channel_count()
        return 0

    @property
    def channelizer_max_channels(self) -> int:
        """Return max channelizer channels (0 if legacy mode)"""
        if self._channelizer:
            return self._channelizer.max_channels
        return 0
    
    def get_channelizer_channel_details(self) -> List[dict]:
        """
        Get detailed info about active channelizer channels for status output.
        
        Returns:
            List of dicts with: frequency, sonde_type, sonde_serial, snr
        """
        if not self._channelizer:
            return []
        
        channels = self._channelizer.get_active_channels()
        result = []
        for ch in channels:
            result.append({
                'frequency': ch.frequency,
                'sonde_type': ch.sonde_type,
                'sonde_serial': ch.sonde_serial or 'N/A',
                'snr': 0.0  # TODO: Add SNR tracking to ChannelInfo
            })
        return result

    def get_scan_return_eta_s(self) -> Optional[float]:
        """Seconds remaining until this device returns to scanning if no more
        valid frames arrive, or None if not currently decoding. Mirrors the
        exact timeout branches in _decode_cycle() so the web UI countdown
        matches what the worker will actually do:
          - duration-limited decode (_decode_expiration_time set): hard
            deadline, independent of idle time.
          - manual/imported decoder with no duration (decode until lost):
            idle-based, using the longer _manual_idle_timeout.
          - auto-detected decoder: idle-based, using _idle_timeout.
        """
        if self._state != self.STATE_DECODING:
            return None

        now = time.time()

        if self._decode_expiration_time is not None:
            return max(0.0, self._decode_expiration_time - now)

        timeout = self._manual_idle_timeout if self._is_manual_decoder else self._idle_timeout
        last_activity = self._last_frame_t if self._last_frame_t else self._decode_start
        elapsed = now - last_activity
        return max(0.0, timeout - elapsed)

    def get_active_decoder(self) -> Optional[ActiveDecoder]:
        """Return a snapshot if currently decoding, else None."""
        if self._state == self.STATE_DECODING and self._decoder and self._cur_freq:
            return ActiveDecoder(
                decoder=self._decoder,
                signal=DetectedSignal(
                    frequency=self._cur_freq,
                    strength=20.0,
                    bandwidth=5000,
                    timestamp=self._decode_start
                ),
                start_time=self._decode_start,
                last_update=self._last_frame_t or self._decode_start,
                audio_pipeline=self._pipeline,
                device_serial=self.device_serial
            )
        return None

    def get_spectrum(self) -> dict:
        """Return latest spectrum snapshot for this RTL-SDR worker."""
        with self._spectrum_lock:
            has_data = bool(self._last_spectrum)
            if has_data:
                self.logger.debug(f"get_spectrum() returning data: {len(self._last_spectrum.get('freqs_mhz', []))} points")
            else:
                # No snapshot yet is NORMAL while decoding (scanner torn down
                # before the first dwell completed) — and the web UI polls this
                # every second, so log quietly and rate-limited instead of the
                # per-poll WARNING spam observed in the field.
                now = time.time()
                if now - getattr(self, '_last_empty_spectrum_log', 0.0) > 60.0:
                    self._last_empty_spectrum_log = now
                    self.logger.info(
                        f"No spectrum snapshot yet for {self.device_serial} "
                        f"(state={self._state}) — first scan dwell not completed before decode"
                    )
            return dict(self._last_spectrum)

    def _update_spectrum_snapshot(self, freqs, power_db, signals: List[DetectedSignal]):
        """Build a compact spectrum payload for the web UI."""
        try:
            import numpy as np

            if freqs is None or power_db is None or len(freqs) == 0:
                self.logger.warning(f"_update_spectrum_snapshot() called with empty data: freqs={freqs is not None}, power_db={power_db is not None}, len={len(freqs) if freqs is not None else 0}")
                return

            noise_floor = float(np.percentile(power_db, 20))
            threshold_db = float(self._analyzer.detection_threshold if self._analyzer else 10.0)

            ds = max(1, len(freqs) // 2000)
            spec = {
                'freqs_mhz': (freqs[::ds] / 1e6).tolist(),
                'power_db': power_db[::ds].tolist(),
                'noise_floor': noise_floor,
                'threshold_db': threshold_db,
                'signals': [
                    {
                        'freq_mhz': float(s.frequency / 1e6),
                        'snr_db': float(s.strength),
                    }
                    for s in signals
                ],
                'timestamp': datetime.utcnow().isoformat() + 'Z',
                'receiver_id': f"rtlsdr:{self.device_serial}",
                'receiver_name': f"RTL-SDR {self.device_serial}",
            }
            with self._spectrum_lock:
                self._last_spectrum = spec
            self.logger.debug(f"Spectrum snapshot updated: {len(spec['freqs_mhz'])} points, {len(signals)} signals, receiver_id={spec['receiver_id']}")
        except Exception as exc:
            self.logger.error(f"Failed to update spectrum snapshot: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # RX Scan cycling (Phase 2)
    # ------------------------------------------------------------------

    def enable_rx_scan(self, channels: List[dict]):
        """Enable RX Scan mode with a list of channels to cycle through.
        
        Args:
            channels: List of channel dicts, each with: {frequency: MHz, type: str, enabled: bool}
        """
        self._rx_scan_channels = [ch for ch in channels if ch.get('enabled', False)]
        self._rx_scan_index = 0
        self._rx_scan_enabled = len(self._rx_scan_channels) > 0
        
        if self._rx_scan_enabled:
            self.logger.info(
                f"RX Scan enabled with {len(self._rx_scan_channels)} channels, "
                f"scantime={self._fixed_channel_scantime}s"
            )
    
    def disable_rx_scan(self):
        """Disable RX Scan cycling mode."""
        self._rx_scan_enabled = False
        self._rx_scan_channels = []
        self._rx_scan_index = 0
        self.logger.info("RX Scan disabled")
    
    def _start_next_rx_scan_channel(self) -> bool:
        """Start the next channel in RX Scan rotation. Returns True if successful."""
        if not self._rx_scan_enabled or not self._rx_scan_channels:
            return False
        
        # Get current channel (wrap around)
        channel = self._rx_scan_channels[self._rx_scan_index]
        current_idx = self._rx_scan_index  # Save for logging
        self._rx_scan_index = (self._rx_scan_index + 1) % len(self._rx_scan_channels)
        
        freq_hz = float(channel['frequency']) * 1e6
        stype = str(channel.get('type', 'RS41'))
        
        self.logger.info(
            f"RX Scan [{current_idx + 1}/{len(self._rx_scan_channels)}]: "
            f"{stype} at {freq_hz/1e6:.3f} MHz for {self._fixed_channel_scantime}s"
        )
        
        # Start decode with duration limit (will auto-cycle when time expires)
        sig = DetectedSignal(
            frequency=freq_hz, strength=25.0,
            bandwidth=7000, timestamp=time.time()
        )
        self._is_manual_decoder = False  # RX Scan decoders are NOT manual
        self._decode_expiration_time = time.time() + self._fixed_channel_scantime

        return self._start_decode(sig, override_type=stype, decode_source='fixed_channel')

    def start_manual_decode(self, frequency: float, sonde_type: str,
                           duration_seconds: Optional[float] = None,
                           source: str = 'manual') -> bool:
        """Force-start decoding a specific frequency (from web UI, Import API,
        fixed_channels, or a priority-frequency check).

        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            duration_seconds: If set, auto-return to scanning after this many seconds.
                            None or 0 = infinite decoding.
            source: 'manual' (web UI entry, the default), 'import_api',
                    'priority', or 'fixed_channel' — used only for the web
                    UI's SDR Devices table color-coding, no behavioral effect.
        """
        # Channelizer mode: Add manual decode request to queue
        if self._decoder_mode == 'channelizer':
            self.logger.info(
                f"Manual decode (channelizer): {sonde_type} at {frequency/1e6:.3f} MHz"
            )
            try:
                self._channelizer_manual_requests.put({
                    'frequency': frequency,
                    'sonde_type': sonde_type,
                    'duration_seconds': duration_seconds
                }, timeout=1.0)
                return True
            except queue.Full:
                self.logger.error("Manual decode queue full (channelizer mode)")
                return False
        
        # Legacy mode: Use state machine approach
        # Signal the scan cycle to stop ASAP so we can acquire the lock quickly
        self._manual_decode_pending.set()
        self.logger.info(f"Manual decode: waiting for device lock on {self.device_serial}...")

        # CRITICAL: _manual_decode_pending must stay set for the ENTIRE manual-decode
        # sequence (lock wait, settle sleeps, _start_decode's own teardown/DFT/rtl_fm
        # startup) — not just until we first grab the lock. _scan_cycle() checks this
        # flag at several points to back off; clearing it early lets an in-flight scan
        # cycle race _start_decode() against ours for the same physical USB device
        # (observed as concurrent rtl_fm/dft_detect processes fighting over the same
        # RTL-SDR dongle, causing "rtl_fm exit: 1" / "dft_detect exit code 206").
        try:
            # CRITICAL: Acquire device lock to prevent race with worker thread.
            # Use a timeout so we get an error log instead of blocking forever if the
            # scan cycle is stuck (e.g. read_samples() hang on a USB error).
            if not self._device_lock.acquire(timeout=20.0):
                self.logger.error(
                    f"Manual decode: could not acquire device lock on {self.device_serial} "
                    f"after 20 s — scan cycle may be stuck"
                )
                return False

            try:
                if self._state == self.STATE_DECODING:
                    self.logger.warning(f"Device {self.device_serial} already decoding, cannot start manual decode")
                    return False

                # Set state to IDLE immediately to signal worker thread to stop scanning
                # Worker thread will see IDLE state and skip further scan cycles
                if self._state == self.STATE_SCANNING:
                    self._state = self.STATE_IDLE  # Stop worker thread from scanning
                    self.logger.info(f"Manual decode: stopping scanner on device {self.device_serial}")
            finally:
                self._device_lock.release()

            # CRITICAL (SIGBUS fix): the worker's capture_spectrum() runs UNLOCKED
            # and holds an in-flight libusb read. Closing the analyzer while that
            # read is active frees the device context under the transfer and
            # crashes the whole process with SIGBUS (status=7/BUS, seen in the
            # field). We therefore NEVER close/open while _capturing is True.
            # capture_spectrum() now has a hard wall-clock cap (scan_max_wall_s),
            # so this clears within a few seconds; wait for it, and if it somehow
            # doesn't clear, ABORT the manual decode (leave the device scanning)
            # rather than closing mid-read — a failed manual decode the user can
            # retry is vastly better than a crash + full restart.
            wait_start = time.time()
            while self._capturing and (time.time() - wait_start) < 20.0:
                time.sleep(0.1)
            if self._capturing:
                self.logger.error(
                    f"Manual decode ABORTED: worker on {self.device_serial} is still "
                    f"inside capture_spectrum() after 20s — refusing to close the device "
                    f"mid-read (would SIGBUS). Retry shortly."
                )
                # Let the worker resume scanning; do NOT touch the device.
                with self._device_lock:
                    if self._state == self.STATE_IDLE:
                        self._state = self.STATE_SCANNING
                return False

            # Wait for USB device to be fully released before starting rtl_fm
            # Conservative 5-second delay for USB hub stability (increased from 3s for long-running sessions)
            self.logger.info(f"Manual decode: waiting 5s for USB device {self.device_serial} to settle")
            time.sleep(5.0)

            # Re-acquire lock for decoder setup
            with self._device_lock:
                sig = DetectedSignal(
                    frequency=frequency, strength=25.0,
                    bandwidth=7000, timestamp=time.time()
                )
                self._decode_expiration_time = None
                # Always mark as manual decoder (None/0 = infinite, >0 = timed)
                self._is_manual_decoder = True
                if duration_seconds and duration_seconds > 0:
                    self._decode_expiration_time = time.time() + duration_seconds

                # _start_decode() will handle teardown_scan() and USB initialization
                # Manual decodes use force_override=True to take over from auto-detect
                success = self._start_decode(sig, override_type=sonde_type, force_override=True,
                                              decode_source=source)
                if success:
                    self.logger.info(
                        f"Manual decode started: {sonde_type} at {frequency/1e6:.3f} MHz "
                        f"on device {self.device_serial}"
                    )
                else:
                    self.logger.error(
                        f"Manual decode failed: {sonde_type} at {frequency/1e6:.3f} MHz "
                        f"on device {self.device_serial}"
                    )
                return success
        finally:
            self._manual_decode_pending.clear()

    def stop_decode_and_scan(self) -> bool:
        """Stop the current decode (if any) and return to scanning."""
        if self._state == self.STATE_DECODING:
            self._teardown_decode()   # sets _state = IDLE
        self._state = self.STATE_SCANNING
        self.logger.info(f"Device {self.device_serial}: returning to scan")
        return True

    def force_clean_scan_restart(self):
        """Force a completely clean scan restart, regardless of current
        state (idle/scanning/decoding/error) — used by the web UI's 'Start
        Scan' button. Stops any active decode, closes and discards any
        existing SpectrumAnalyzer (so the next scan cycle constructs a
        brand-new one, picking up detection-tuning changes applied via
        RTLSDRDeviceManager.reload_detection_config() — scan_check_time,
        max_peaks, channel_spacing_hz, etc. — without a full service
        restart), and clears any stuck error backoff state."""
        with self._device_lock:
            if self._state == self.STATE_DECODING:
                self._teardown_decode()
            self._teardown_scan()
            self._consecutive_open_failures = 0
            self._state = self.STATE_SCANNING
        self.logger.info(f"Device {self.device_serial}: forced clean scan restart")

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    def _run(self):
        """Main worker loop - dispatches to legacy or channelizer mode."""
        # Step 3: Check decoder mode and dispatch accordingly
        if self._decoder_mode == 'channelizer':
            self.logger.info(f"Starting channelizer mode for {self.device_serial}")
            self._run_channelizer()
        else:
            self.logger.info(f"Starting legacy mode for {self.device_serial}")
            self._run_legacy()

    def _run_legacy(self):
        """Legacy single-channel scan → decode loop (original behavior)."""
        idle_start = None  # Track when we entered IDLE state
        
        while self._running:
            try:
                # Quarantined: the watchdog found this device's read wedged and
                # abandoned it. If that stuck read ever returns, the worker must
                # NOT resume scanning (it would un-quarantine a known-bad device
                # and hand it back to the Import API). Park permanently, no spin.
                if self._wedged:
                    self._state = self.STATE_ERROR
                    time.sleep(5.0)
                    continue

                # Track state transitions
                prev_state = self._last_state
                self._last_state = self._state

                if self._state == self.STATE_SCANNING:
                    idle_start = None  # Reset idle timer
                    self._scan_cycle()
                elif self._state == self.STATE_DECODING:
                    idle_start = None  # Reset idle timer
                    self._decode_cycle()
                elif self._state == self.STATE_IDLE:
                    # IDLE state: waiting for manual decode to start or transition back to scanning
                    if idle_start is None:
                        idle_start = time.time()
                    elif time.time() - idle_start > 15:
                        # Been idle for 15 seconds, transition back to scanning
                        self.logger.info(f"IDLE timeout - returning to SCANNING")
                        self._state = self.STATE_SCANNING
                        idle_start = None
                    else:
                        # Just wait a bit
                        time.sleep(0.5)
                elif self._state == self.STATE_ERROR:
                    # CRITICAL FIX: this loop previously had NO branch for
                    # STATE_ERROR — a single failed device open set the state
                    # (for UI visibility) and the loop then matched nothing,
                    # busy-spinning this thread at 100% CPU with no retry, no
                    # sleep, forever. Field impact: 3 of 4 devices stuck in
                    # "Error" for 24 h (V1.0.50 retried forever because it
                    # never entered an error state), with the spinning threads
                    # starving the surviving device's decoder pipe.
                    # Recovery: back off, then return to SCANNING and retry the
                    # open. Now that devices are closed (not leaked) on failure,
                    # reopens usually succeed, so a long backoff just wastes
                    # airtime — cap it low (10s base, 30s max) so a device that
                    # can recover isn't parked for minutes. The
                    # MAX_CONSECUTIVE_OPEN_FAILURES → service-restart safety net
                    # still applies if retries keep genuinely failing.
                    backoff = min(10.0 * max(1, self._consecutive_open_failures), 30.0)
                    self.logger.warning(
                        f"Device {self.device_serial} in error state "
                        f"({self._consecutive_open_failures} consecutive open failures) "
                        f"— retrying in {backoff:.0f}s"
                    )
                    t0 = time.time()
                    while self._running and time.time() - t0 < backoff:
                        if self._manual_decode_pending.is_set():
                            break  # Let a manual decode try to claim the device
                        time.sleep(1.0)
                    if self._running:
                        self.logger.info(
                            f"Device {self.device_serial}: retrying scan after error backoff"
                        )
                        self._state = self.STATE_SCANNING
                        idle_start = None
                else:
                    # Unknown state — never busy-spin
                    time.sleep(1.0)

            except Exception as exc:
                self.logger.error(f"Worker loop error: {exc}", exc_info=True)
                time.sleep(5)

    def _run_channelizer(self):
        """
        Channelizer mode: Multi-channel scan and decode using iq_server/iq_client.
        
        This replaces the legacy scan→decode state machine with a persistent
        iq_server process that handles multiple sondes simultaneously via
        iq_client | iq_fm | decoder pipelines.
        
        Status: STEP 5 - Full iq_server integration with manual decode support
        """
        self.logger.info("Channelizer mode starting")
        
        if not self._channelizer:
            self.logger.error("Channelizer not initialized, falling back to idle")
            time.sleep(10)
            return
        
        # Start iq_server process
        if not self._channelizer.start():
            self.logger.error("Failed to start channelizer, returning to idle")
            time.sleep(10)
            return
        
        self.logger.info(f"Channelizer started: {self._channelizer.max_channels} max channels")
        
        try:
            while self._running:
                try:
                    # Check for manual decode requests
                    try:
                        request = self._channelizer_manual_requests.get(block=False)
                        frequency = request['frequency']
                        sonde_type = request['sonde_type']
                        duration_seconds = request.get('duration_seconds')
                        
                        self.logger.info(
                            f"Processing manual decode: {sonde_type} at {frequency/1e6:.3f} MHz"
                        )
                        
                        # Check if channel has capacity
                        if not self._channelizer.has_capacity():
                            self.logger.warning("Channelizer at capacity, cannot start manual decode")
                        else:
                            # Start channel
                            if self._channelizer.start_channel(frequency, sonde_type):
                                self.logger.info(
                                    f"Manual decode started: {sonde_type} at {frequency/1e6:.3f} MHz"
                                )
                            else:
                                self.logger.error(
                                    f"Failed to start manual decode: {sonde_type} at {frequency/1e6:.3f} MHz"
                                )
                    
                    except queue.Empty:
                        pass  # No manual requests pending
                    
                    # Monitor active channels
                    active_channels = self._channelizer.get_active_channels()
                    if active_channels:
                        self.logger.debug(
                            f"Channelizer: {len(active_channels)}/{self._channelizer.max_channels} channels active"
                        )
                    
                    # TODO (Future steps):
                    # - Automatic spectrum scanning for signal detection
                    # - Auto-start channels for detected sondes
                    # - Monitor channel health and stop inactive channels
                    # - Implement channel timeout/rotation logic
                    
                    # Update interval: check status every 2 seconds
                    time.sleep(2)
                    
                except Exception as e:
                    self.logger.error(f"Channelizer loop error: {e}", exc_info=True)
                    time.sleep(5)
        
        finally:
            # Clean shutdown
            self.logger.info("Channelizer shutting down")
            self._channelizer.stop()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _note_open_failure(self):
        """Record a failed device-open/test-read attempt. Once a device has been
        stuck for MAX_CONSECUTIVE_OPEN_FAILURES retries in a row, self-restart the
        whole service instead of polling a permanently dead device forever — the
        leaked USB interface claim can only be released by the kernel when this
        process exits. systemd (Restart=on-failure, RestartSec=10) brings it back
        up cleanly and releases every device, not just this one."""
        self._consecutive_open_failures += 1
        # CRITICAL: _state defaults to STATE_SCANNING at construction and was
        # otherwise only ever written on a *successful* open, so a device stuck
        # failing to open forever kept reporting "Scanning" (green) to the web UI
        # with no way to tell it apart from a genuinely healthy scanner. Surface
        # the failure so /api/devices and other w.state consumers see it.
        self._state = self.STATE_ERROR
        if self._consecutive_open_failures >= self.MAX_CONSECUTIVE_OPEN_FAILURES:
            self.logger.critical(
                f"Device {self.device_serial} failed to open "
                f"{self._consecutive_open_failures} times in a row and is not "
                "recovering — restarting the whole service to release the stuck "
                "USB interface (systemd Restart=on-failure will bring it back up)"
            )
            os._exit(1)

    def _note_open_success(self):
        self._consecutive_open_failures = 0

    def _scan_cycle(self):
        """Open the RTL-SDR, capture one spectrum, look for new signals."""
        # CRITICAL: Acquire device lock only for state changes and analyzer init
        # Do NOT hold lock during spectrum capture or signal processing
        
        # Check if manual decode is pending before doing any work
        if self._manual_decode_pending.is_set():
            time.sleep(0.5)
            return

        # Phase-1 pluggable scan backend: the rtl_power full-band sweep replaces
        # the pyrtlsdr Welch segment scan (time-shared with decoding on this
        # device). Detect/dispatch downstream is identical.
        if self._scan_backend == 'rtl_power':
            return self._scan_cycle_rtl_power()

        # Initialize analyzer if needed (with lock protection)
        if self._analyzer is None:
            # If we just transitioned from DECODING, wait for USB device to be fully released
            # Increased from 3s to 5s for better USB/PLL stability on some Raspberry Pi systems
            # Check flag instead of _last_state because state goes DECODING → IDLE → SCANNING
            if hasattr(self, '_usb_reopen_after_decode') and self._usb_reopen_after_decode:
                self.logger.debug("Waiting 5s for USB device to settle after DECODING")
                time.sleep(5.0)
                self._usb_reopen_after_decode = False
            
            # CRITICAL: Stagger first USB device open to prevent simultaneous access
            # This prevents "[R82XX] PLL not locked!" errors from USB bus contention.
            # Device 0 previously got the SHORTEST delay ((index+1)*2.5 = 2.5s) even
            # though it's the one opened earliest — right when the USB subsystem is
            # least settled after a fresh service (re)start. Observed in the field:
            # on 4x RTL-SDR systems (even with a powered hub), device 0 consistently
            # failed its test read while devices 1-3 (5s/7.5s/10s) succeeded reliably
            # — this was fully deterministic, not random flakiness, so it reproduced
            # on every single restart (the auto-restart-after-N-failures safety net
            # doesn't help here since restarting hits the exact same race again).
            # Give every device the same baseline settle time device 1 already had,
            # then stagger on top of that.
            if self._first_usb_init:
                stagger_delay = 5.0 + self.device_index * 2.5  # 5s for device 0, 7.5s for device 1, etc.
                self.logger.info(f"First USB init: waiting {stagger_delay:.1f}s for USB/PLL stabilization")
                time.sleep(stagger_delay)
                self._first_usb_init = False
            
            # Acquire lock for analyzer initialization
            if not self._device_lock.acquire(blocking=False):
                self.logger.debug(f"Device lock held (manual decoder active), skipping scan cycle")
                time.sleep(0.5)
                return
            
            try:
                self._analyzer = SpectrumAnalyzer(self.app_config, self.device_config, self._blacklist)
                if not self._analyzer.initialize():
                    self.logger.error("Cannot open RTL-SDR — retrying in 15 s")
                    self._analyzer = None
                    self._note_open_failure()
                    # Release lock BEFORE long sleep
                    self._device_lock.release()
                    time.sleep(15)
                    return

                # V1.0.60 REGRESSION FIX: the old code ran a probing read_samples()
                # in a DETACHED daemon thread with a 5s timeout, then on timeout set
                # self._analyzer = None WITHOUT close() — leaking the RtlSdr handle,
                # which kept the USB interface claimed. Every subsequent open then
                # failed with LIBUSB_ERROR_BUSY, and the only escape was the 10-
                # failure os._exit() service restart. Field logs showed 144 restarts
                # and BUSY devices that NEVER recovered without a full restart —
                # "4 SDR always lost, self-healing not working". V1.0.50 had no such
                # probe: it just opened and let the first spectrum capture be the
                # real read, recovering via a normal close/reopen on any hiccup.
                #
                # We now do the same: a successful open() is enough to proceed;
                # capture_spectrum() below is the first real read, and its exception
                # handler tears the scanner down through _teardown_scan() which
                # properly close()s the device (no leak). Because the read now runs
                # in THIS worker thread (not a detached one), there is no concurrent
                # access to the handle, so the SIGSEGV that motivated the old
                # leak-instead-of-close cannot occur.
                self._note_open_success()
                self._state = self.STATE_SCANNING
                self.logger.info(
                    f"Scanning {self.device_config['center_freq']/1e6:.1f} MHz "
                    f"±{self.device_config['sample_rate']/2e6:.1f} MHz"
                )
            finally:
                # Release lock after analyzer init
                if self._device_lock.locked():
                    self._device_lock.release()

        # Capture spectrum WITHOUT holding the lock - this can now take up to
        # scan_check_time seconds (default 20s) for the averaged multi-chunk
        # capture. Manual decode can interrupt by setting _manual_decode_pending;
        # pass it through as abort_check so a pending manual/priority decode
        # doesn't have to wait out the full dwell time before being noticed.
        # CRITICAL: hold a local reference to the analyzer for the rest of this
        # capture/analyze pass. capture_spectrum() runs unlocked and can take
        # up to scan_check_time seconds — if a manual/imported decode request
        # arrives during that window, its _teardown_scan() call sets
        # self._analyzer = None concurrently. Re-reading self._analyzer after
        # capture_spectrum() returns raced that None-out in the field:
        # "AttributeError: 'NoneType' object has no attribute 'detect_signals'".
        # detect_signals()/filter_signals_in_ranges() only operate on the
        # already-captured freqs/power_db arrays (never touch self.sdr), so
        # using this local reference is safe even if the real analyzer gets
        # torn down/closed concurrently.
        analyzer = self._analyzer
        # Mark the in-flight libusb read window. _capturing stays True only for
        # the capture_spectrum() call itself (the sole place that reads the
        # device); detect_signals()/snapshot below operate on arrays and touch
        # no USB. A concurrent close() (manual decode teardown) waits for this
        # to clear — see start_manual_decode() — so the device is never closed
        # while a read transfer is in flight (would SIGBUS).
        self._capture_started_at = time.time()
        self._capturing = True
        try:
            self.logger.debug(f"Starting capture_spectrum() for {self.device_serial}...")
            freqs, power_db = analyzer.capture_spectrum(
                abort_check=self._manual_decode_pending.is_set
            )
            self.logger.debug(f"capture_spectrum() completed for {self.device_serial}: {len(freqs) if freqs is not None else 0} points")
        except Exception as exc:
            self._capturing = False
            # During a deliberate USB-reset recovery the in-flight read errors
            # with LIBUSB_ERROR_NO_DEVICE — that's expected, not a hardware open
            # failure. Log it calmly and do NOT count it toward the restart
            # safety net (the recovery thread handles teardown + reopen).
            if self._recovering:
                self.logger.info(
                    f"{self.device_serial}: in-flight read aborted by USB-reset "
                    f"recovery (expected: {type(exc).__name__}) — worker will reopen"
                )
                with self._device_lock:
                    self._teardown_scan()
                time.sleep(1)
                return
            self.logger.error(f"Spectrum capture failed for {self.device_serial}: {exc}", exc_info=True)
            # Tear down (this close()s the device properly — no leak) and count
            # the failure so a genuinely dead device still eventually hits the
            # os._exit safety net. Because the device is now CLOSED (not leaked),
            # the next open normally succeeds, so this does NOT create the old
            # BUSY→restart loop — escalation only fires for truly stuck hardware.
            with self._device_lock:
                self._teardown_scan()
            self._note_open_failure()
            time.sleep(5)
            return

        # Capture returned — the device read is done, so it's now safe for a
        # concurrent close() to proceed. Clear the guard BEFORE the rest of the
        # pass (detect_signals/snapshot touch no USB).
        self._capturing = False

        # A manual/imported decode may have claimed this device while
        # capture_spectrum() was running (or aborted it early) — don't bother
        # analyzing/publishing signals that are about to be discarded anyway.
        if self._manual_decode_pending.is_set():
            return

        self._process_scan_signals(freqs, power_db, analyzer)

    # ------------------------------------------------------------------
    #  Scan backend: rtl_power full-band sweep (Phase 1)
    # ------------------------------------------------------------------
    def _scan_cycle_rtl_power(self):
        """One time-shared rtl_power full-band scan pass, then the shared
        detect/dispatch. No pyrtlsdr device is opened (rtl_power owns the dongle
        for the pass and releases it on exit), so there is no _capturing/libusb
        wedge window here — the scanner uses its own subprocess wall timeout."""
        # Lazily build the scanner and the device-less detect analyzer.
        if self._rtl_power_scanner is None:
            cfg = self._rtl_power_cfg
            self._rtl_power_scanner = RtlPowerScanner(
                device_serial=self.device_serial,
                gain=self.device_config.get('gain', 0),
                ppm=self.device_config.get('ppm_error', 0),
                band_start_hz=int(cfg.get('band_start_hz', 402_000_000)),
                band_stop_hz=int(cfg.get('band_stop_hz', 406_000_000)),
                step_hz=int(cfg.get('step_hz', 800)),
                integration_s=float(cfg.get('integration_s', 8)),
                crop_percent=int(cfg.get('crop_percent', 25)),
                rtl_power_path=str(cfg.get('rtl_power_path', 'rtl_power')),
                wall_timeout_s=float(cfg.get('wall_timeout_s', 30)),
            )
            # Missing binary → permanent fallback to Welch for this worker.
            if not self._rtl_power_scanner.available():
                self.logger.error(
                    "rtl_power not installed — falling back to the Welch scan "
                    "backend for this device (install the rtl-sdr package to use "
                    "detection.scanner.backend: rtl_power)."
                )
                self._scan_backend = 'welch'
                return
        if self._detect_analyzer is None:
            # Config-only SpectrumAnalyzer: provides detect_signals/
            # filter_signals_in_ranges (identical to the Welch path) without ever
            # opening a pyrtlsdr handle.
            self._detect_analyzer = SpectrumAnalyzer(
                self.app_config, self.device_config, self._blacklist)

        self._state = self.STATE_SCANNING
        # rtl_fm held the device during the previous decode; give the kernel a
        # moment to release it before rtl_power opens it (mirrors the Welch path).
        if getattr(self, '_usb_reopen_after_decode', False):
            time.sleep(2.0)
            self._usb_reopen_after_decode = False

        if self._first_usb_init:
            time.sleep(5.0 + self.device_index * 2.5)
            self._first_usb_init = False

        result = self._rtl_power_scanner.scan(
            abort_check=self._manual_decode_pending.is_set)
        if self._manual_decode_pending.is_set():
            return
        if result is None:
            # Transient scan failure (timeout/empty). Don't feed the pyrtlsdr
            # open-failure→restart safety net. If rtl_power keeps failing, fall
            # back to Welch so the device never dead-scans (a scan pass that
            # always exceeds wall_timeout_s, a hung binary, etc.).
            self._rtl_power_fail_streak += 1
            if self._rtl_power_fail_streak >= 3:
                self.logger.error(
                    "rtl_power scan failed 3x in a row (timeout/empty) — falling "
                    "back to the Welch scan backend for this device. Check "
                    "detection.scanner timing (raise wall_timeout_s / lower "
                    "step_hz / lower integration_s) or rtl_power health."
                )
                self._scan_backend = 'welch'
                self._rtl_power_fail_streak = 0
            time.sleep(self._scan_interval + 2)
            return

        self._rtl_power_fail_streak = 0
        freqs, power_db = result
        self._note_open_success()
        self._process_scan_signals(freqs, power_db, self._detect_analyzer)

    def _process_scan_signals(self, freqs, power_db, analyzer):
        """Shared post-acquisition path for BOTH scan backends: detect signals,
        update the spectrum snapshot, log repository candidates, then dispatch a
        decode for the strongest decodable signal. `analyzer` supplies
        detect_signals/filter_signals_in_ranges + config (never touches USB)."""
        signals = analyzer.detect_signals(freqs, power_db)
        signals = analyzer.filter_signals_in_ranges(signals)
        self._update_spectrum_snapshot(freqs, power_db, signals)

        # Frequency repository: log sonde-like candidates as 'detected' rows. Use
        # a SEPARATE, lower threshold than the decode path so weak sondes get
        # listed (for manual selection) even though they won't be auto-decoded,
        # and require the frequency to sit near the 100 kHz channel grid so the
        # RTL's odd-frequency spurs (x.x40/x.x60 …) aren't listed. A real decode
        # still logs 'confirmed' (in the telemetry handler) regardless of grid.
        _repo = getattr(self._manager, 'frequency_repository', None)
        if _repo is not None:
            repo_signals = analyzer.detect_signals(freqs, power_db, threshold_db=self._repo_threshold_db)
            repo_signals = analyzer.filter_signals_in_ranges(repo_signals)
            for _s in repo_signals:
                if self._is_blacklisted(_s.frequency):
                    continue
                if not self._repo_grid_ok(_s.frequency):
                    continue
                _repo.record_detected(_s.frequency, snr=_s.strength, device=self.device_serial)

        # Abort if manual decode was requested during spectrum capture
        if self._manual_decode_pending.is_set():
            return

        # Process signals WITHOUT holding the lock
        # Sort by strength descending; skip blacklisted / already-decoded freqs.
        # (C) Track whether this segment holds a REAL (non-blacklisted, not
        # already-decoded-elsewhere) sonde signal, even one we skip this pass —
        # so the band-sweep never abandons a segment that has a sonde in it.
        signal_present = False
        for sig in sorted(signals, key=lambda s: s.strength, reverse=True):
            if self._is_blacklisted(sig.frequency):
                continue
            if self.registry.is_active(sig.frequency):
                continue

            # A real, non-blacklisted, not-currently-decoded signal is here.
            signal_present = True

            # Skip frequencies whose last auto decode produced zero frames
            # (negative-result cache — birdies, DC spurs, misclassifications).
            # (B) is_auto_decode_blocked gets the current SNR: a signal that
            # reappears clearly STRONGER than when it failed (an ascending sonde)
            # clears its own block and is retried now instead of staying locked
            # out for the full cooldown.
            if self._manager is not None and \
                    self._manager.is_auto_decode_blocked(sig.frequency, sig.strength):
                self.logger.debug(
                    f"Skipping {sig.frequency/1e6:.4f} MHz - in failed-decode cooldown"
                )
                continue

            # CRITICAL: Skip signals that are assigned to fixed_channels
            # Let fixed_channels start them with the correct type, not auto-detection
            if self._is_fixed_channel_frequency(sig.frequency):
                self.logger.debug(
                    f"Skipping {sig.frequency/1e6:.4f} MHz - reserved for fixed_channel with specified type"
                )
                continue

            # Check one more time before starting decode
            if self._manual_decode_pending.is_set():
                return

            self.logger.info(
                f"New signal at {sig.frequency/1e6:.4f} MHz "
                f"(SNR {sig.strength:.1f} dB, BW {sig.bandwidth/1e3:.1f} kHz)"
            )
            self._empty_scan_count = 0   # found something → stay on this segment
            self._start_decode(sig)
            return   # worker will re-enter loop in DECODING state

        # Nothing decoded this pass.
        if signal_present:
            # (C) A sonde IS present (in cooldown, reserved for a fixed channel,
            # etc.) — do NOT let the band-sweep count this as an empty scan.
            self._empty_scan_count = 0
        else:
            # Genuinely empty segment: count it and hop once the dwell is reached.
            # (No-op under the rtl_power backend, which already covers the band.)
            self._maybe_sweep_hop()
        time.sleep(self._scan_interval)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def _decode_cycle(self):
        """Monitor the running decoder; return to scanning when it ends."""
        if not self._decoder or not self._decoder.is_alive():
            self.logger.info(
                f"Decoder ended on {self._cur_freq/1e6:.4f} MHz — back to scan"
            )
            self._teardown_decode()
            return

        if self._is_manual_decoder:
            # Manual/imported decoders with an explicit duration (e.g. priority
            # frequency checks) are governed purely by their expiration timer
            # below, regardless of idle time — unchanged from before.
            # Decoders with duration=None ("decode until sonde lost", e.g.
            # Import API assignments) have no expiration timer at all, so they
            # need their OWN staleness check here — otherwise a decoder whose
            # subprocess keeps running without ever producing another frame
            # ties up the device forever.
            if self._decode_expiration_time is None and self._decoder.is_idle(self._manual_idle_timeout):
                self.logger.info(
                    f"Manual/imported decoder idle for >{self._manual_idle_timeout}s — back to scan"
                )
                self._teardown_decode()
                return
        elif self._decoder.is_idle(self._idle_timeout):
            self.logger.info(
                f"Decoder idle for >{self._idle_timeout}s — back to scan"
            )
            self._teardown_decode()
            return

        # Check if duration-limited decode has expired
        if self._decode_expiration_time is not None:
            remaining = self._decode_expiration_time - time.time()
            if remaining <= 0:
                self.logger.info(
                    f"Decoder duration limit expired on {self._cur_freq/1e6:.4f} MHz — back to scan"
                )
                self._teardown_decode()
                return

        time.sleep(2)

    # ------------------------------------------------------------------
    # Scan → Decode transition
    # ------------------------------------------------------------------

    def _start_decode(self, sig: DetectedSignal,
                      override_type: Optional[str] = None,
                      force_override: bool = False,
                      decode_source: str = 'auto') -> bool:
        # Atomically claim the frequency in the shared registry
        # For manual decodes with force_override, unregister first to allow takeover
        if force_override:
            self.logger.info(f"Manual decode force override: unclaiming {sig.frequency/1e6:.4f} MHz")
            self.registry.unregister(sig.frequency)

            # CRITICAL: unregister() only removes the shared registry
            # bookkeeping — it does NOT stop whatever OTHER physical device
            # might actually be decoding that (or a nearby) frequency right
            # now. Without this, a manual/priority override for a frequency
            # already claimed by a DIFFERENT RTL-SDR silently steals the
            # registry slot while that other dongle keeps running its own
            # rtl_fm+decoder pipeline on virtually the same real signal —
            # two adjacent dongles fighting over one carrier. Observed in
            # the field: a manual DFM request stole a nearby frequency from
            # a device auto-decoding the SAME physical sonde (bandwidth-
            # fallback had misclassified it), and neither device could
            # decode afterward.
            if self._manager is not None:
                for other in getattr(self._manager, '_workers', []):
                    if other is self:
                        continue
                    other_freq = other.current_freq
                    if (other.state == self.STATE_DECODING and other_freq is not None
                            and abs(other_freq - sig.frequency) < self.registry.TOLERANCE_HZ):
                        self.logger.info(
                            f"Manual decode force override: stopping conflicting decode "
                            f"on {other.device_serial} ({other_freq/1e6:.4f} MHz)"
                        )
                        with other._device_lock:
                            other.stop_decode_and_scan()

        # Store original frequency for registry updates
        original_freq = sig.frequency
        
        if not self.registry.register(original_freq):
            self.logger.info(
                f"{sig.frequency/1e6:.4f} MHz already claimed (within ±{self.registry.TOLERANCE_HZ/1000:.0f} kHz) — continuing scan"
            )
            return False

        # Close pyrtlsdr so rtl_fm can open the same USB device
        self._teardown_scan()
        # Wait for USB device to be fully released before DFT detection
        # This prevents "[R82XX] PLL not locked!" errors
        # Conservative 3-second delay for USB hub stability with multiple devices
        self.logger.debug(f"Waiting 3s for USB device {self.device_serial} to settle...")
        time.sleep(3.0)

        # Identify sonde type (this will use rtl_fm internally for DFT detection)
        sonde_offset = 0.0  # Frequency offset from DFT detection
        if override_type:
            sonde_type = override_type
            self.logger.info(f"Manual decode: using override type {sonde_type} at {sig.frequency/1e6:.4f} MHz")
        else:
            # CRITICAL: Check for manual decode before DFT (which takes 7+ seconds)
            # This allows import API or user requests to interrupt auto-detection
            if hasattr(self, '_manual_decode_pending') and self._manual_decode_pending.is_set():
                self.logger.info(f"Aborting auto-detection at {sig.frequency/1e6:.4f} MHz - manual decode pending")
                self.registry.unregister(sig.frequency)
                return False
            
            # _identify_sonde_type returns (sonde_type, frequency_offset)
            result = self._identify_sonde_type(sig)
            if isinstance(result, tuple) and len(result) > 1:
                sonde_type, sonde_offset = result
            else:
                # Legacy single return value
                sonde_type = result
                sonde_offset = 0.0
            
            if not sonde_type:
                self.logger.error(f"Failed to identify sonde type at {sig.frequency/1e6:.4f} MHz")
                self.registry.unregister(sig.frequency)
                return False

        # CRITICAL: only trust DFT's frequency-offset sub-measurement for
        # types that actually need tight tuning to lock (M10/M20 — narrow
        # sync window, needed a real -5434.5 Hz correction in the field to
        # decode at all). For RS41 it has been observed to make things
        # WORSE: a confirmed-strong, confirmed-genuine RS41 at raw-detected
        # 405.6992 MHz (already within 1.2 kHz of the independently-verified
        # true 405.698 MHz) got "corrected" by a reported +1801.2 Hz offset
        # to 405.701 MHz — 3 kHz off, and never decoded a single frame.
        # RS41/DFM/RS92/iMet aren't frequency-critical the way M10/M20 are
        # (matches operational experience: RS41 decoded reliably before
        # dft_detect ever returned a usable offset at all), so for those
        # types keep the type ID but ignore the offset and fall back to
        # plain 1 kHz quantization of the raw scan-detected frequency.
        FREQUENCY_OFFSET_SENSITIVE_TYPES = ('M10', 'M20')
        if sonde_offset != 0.0 and sonde_type not in FREQUENCY_OFFSET_SENSITIVE_TYPES:
            self.logger.debug(
                f"Ignoring DFT offset {sonde_offset:+.1f} Hz for {sonde_type} "
                "(not frequency-offset-sensitive) — using raw scan frequency"
            )
            sonde_offset = 0.0

        # Apply frequency correction from DFT detection (critical for M10/M20)
        # Quantize to 1 kHz to avoid rtl_fm frequency jitter
        if sonde_offset != 0.0:
            corrected_freq = round((sig.frequency + sonde_offset) / 1000.0) * 1000.0
            self.logger.info(
                f"Applying DFT frequency correction: {sig.frequency/1e6:.4f} MHz "
                f"+ {sonde_offset:+.1f} Hz → {corrected_freq/1e6:.4f} MHz"
            )
            sig.frequency = corrected_freq
        else:
            # No DFT offset, just quantize to 1 kHz
            sig.frequency = round(sig.frequency / 1000.0) * 1000.0
        
        # CRITICAL: If frequency changed after correction, move the registry claim
        # from original_freq to the corrected frequency. Unregister BEFORE checking/
        # registering the corrected value — checking is_active() while still holding
        # the original_freq claim is a self-collision bug: the correction (even plain
        # 1 kHz quantization with no real DFT offset) is almost always well within
        # TOLERANCE_HZ (20 kHz) of original_freq, so is_active() kept finding this
        # call's own just-registered entry and aborting essentially every auto-detected
        # decode that received any correction at all.
        if sig.frequency != original_freq:
            self.registry.unregister(original_freq)
            if not self.registry.register(sig.frequency):
                self.logger.warning(
                    f"After DFT correction, {sig.frequency/1e6:.4f} MHz is already being decoded "
                    f"(within ±{self.registry.TOLERANCE_HZ/1000:.0f} kHz) — aborting"
                )
                return False
            self.logger.debug(f"Updated registry: {original_freq/1e6:.4f} → {sig.frequency/1e6:.4f} MHz")
        
        # Check decoder cooldown to prevent tight failure loops
        decoder_path = self._get_decoder_path(sonde_type)
        if decoder_path and not RS1729Decoder.should_retry_decoder(decoder_path, sonde_type, cooldown_seconds=60):
            self.logger.warning(
                f"Decoder {sonde_type} in cooldown after recent failures, "
                f"skipping {sig.frequency/1e6:.4f} MHz"
            )
            self.registry.unregister(sig.frequency)
            return False

        # Add brief delay after DFT detection before starting AudioPipeline
        # DFT detection just used rtl_fm, so give device time to settle
        # Skip this delay if override_type was provided (DFT wasn't run)
        if not override_type:
            self.logger.debug(f"Waiting 1s after DFT detection before starting AudioPipeline...")
            time.sleep(1.0)

        # Construct decoder FIRST (no subprocess spawned yet): it decides the
        # decode chain (fsk_demod soft-bit vs. legacy --IQ) and therefore the
        # required capture sample rate (e.g. DFM soft chain needs 50 kHz).
        # The AudioPipeline is then created at exactly that rate.
        # Default OFF: the rtl_fm→fsk_demod→rs41mod --softin chain produced 0
        # frames in the field on RTL clients (healthy decoder, no telemetry)
        # while the direct --IQ chain decodes the same signal reliably. --IQ is
        # now the default; set decoders.soft_decode: true to opt back into the
        # ~2 dB soft-decision chain once it's verified on your hardware. (The
        # KA9Q receiver has its own, separate, working soft chain — unaffected.)
        soft_decode = bool(self.app_config.get('decoders', {}).get('soft_decode', False))
        # Optional auto_rx-style inline DC removal via iq_dec on the --IQ chain.
        # Default OFF: harmless where the iq_dec binary is absent (graceful
        # no-op), but keep it opt-in so a new stage never silently changes
        # decode behaviour across gateways until validated per client.
        iq_dc_block = bool(self.app_config.get('decoders', {}).get('iq_dc_block', False))
        decoder = RS1729Decoder(
            frequency=sig.frequency,
            sonde_type=sonde_type,
            soft_decode=soft_decode,
            iq_dc_block=iq_dc_block,
            allow_rate_change=True
        )
        decoder.set_frame_callback(self._on_frame)

        # Start rtl_fm audio pipeline at the decoder's required input rate
        self.logger.info(
            f"Starting AudioPipeline for {sonde_type} at {sig.frequency/1e6:.4f} MHz "
            f"on {self.device_serial} ({decoder.sample_rate} Hz, chain={decoder.decode_chain})"
        )
        pipeline = AudioPipeline(
            frequency=sig.frequency,
            sample_rate=decoder.sample_rate,
            device_serial=self.device_serial,
            gain=self.device_config.get('gain', 40),
            ppm_correction=self.device_config.get('ppm_error', 0),
            enable_metrics=bool(self.app_config.get('decoders', {}).get('live_signal_metrics', False))
        )
        if not pipeline.start():
            self.logger.error(f"AudioPipeline failed to start for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            self.registry.unregister(sig.frequency)
            return False

        # Reset per-decode telemetry tracking (landed-sonde guard) BEFORE the
        # decoder starts — frames can arrive during its startup checks
        self._last_alt_m = None
        self._last_vv_ms = None

        # Start rs1729 decoder
        self.logger.info(f"Starting RS1729 decoder for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
        audio_stream = pipeline.get_audio_stream()
        if not audio_stream:
            self.logger.error(f"AudioPipeline audio stream is None for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            pipeline.stop()
            self.registry.unregister(sig.frequency)
            return False
        if not decoder.start(audio_stream=audio_stream):
            self.logger.error(f"RS1729 decoder failed to start for {sonde_type} at {sig.frequency/1e6:.4f} MHz")
            pipeline.stop()
            self.registry.unregister(sig.frequency)
            return False

        self._pipeline     = pipeline
        self._decoder      = decoder
        self._cur_freq     = sig.frequency
        self._cur_type     = sonde_type
        # Manual/imported decodes build a SYNTHETIC DetectedSignal with a
        # placeholder strength (e.g. 25.0) and no measured power_dbfs — there is
        # no real scan measurement for them. Detect that (power_dbfs unset) and
        # report NO scan SNR/RSSI rather than fabricating 25 dB / 25 dBm; the log
        # and UI then show N/A until the decoder / live metrics supply real
        # values. Genuine scan signals always carry a real (negative) power_dbfs.
        _pwr = getattr(sig, 'power_dbfs', None)
        _synthetic = _pwr in (None, 0.0)
        self._cur_signal_strength_db = None if _synthetic else float(sig.strength)
        self._cur_signal_power_dbfs = None if _synthetic else float(_pwr)
        self._decode_start = time.time()
        self._last_frame_t = 0.0
        self._decode_source = decode_source
        self._state        = self.STATE_DECODING
        self.logger.info(
            f"Decoding {sonde_type} at {sig.frequency/1e6:.4f} MHz "
            f"(device {self.device_serial})"
        )
        return True

    # ------------------------------------------------------------------
    # Teardown helpers
    # ------------------------------------------------------------------

    def _teardown_scan(self):
        if self._analyzer:
            try:
                self._analyzer.close()
            except Exception:
                pass
            self._analyzer = None

    # ------------------------------------------------------------------
    # Band-sweep (opt-in coverage rotation) — see __init__.
    # ------------------------------------------------------------------

    def _build_sweep_centers(self) -> list:
        """Segment centers that tile the configured band. Each device rotates
        through these when idle so one SDR can cover the whole band over time.
        Outer centers are inset by ~usable half-bandwidth so the band edges are
        reachable; segments overlap by design (step < usable width)."""
        c = self._sweep_cfg
        band_min = float(c.get('band_min_hz', 402_100_000))
        band_max = float(c.get('band_max_hz', 405_900_000))
        sr = float(self.device_config.get('sample_rate', 2_400_000))
        # ~85% of Nyquist half is reliably usable (tuner/filter roll off near
        # the ±sample_rate/2 edges).
        usable_half = (sr / 2.0) * 0.85
        step = float(c.get('step_hz', sr * 0.8))
        lo = band_min + usable_half
        hi = band_max - usable_half
        if hi <= lo:
            return [round((band_min + band_max) / 2.0)]
        n = max(2, int(round((hi - lo) / step)) + 1)
        return [round(lo + i * (hi - lo) / (n - 1)) for i in range(n)]

    def _maybe_sweep_hop(self):
        """Called after a scan cycle that started no decode. Counts empty
        cycles and, past the dwell threshold, retunes this device to the next
        band segment (reopened by the next scan cycle at the new center)."""
        if self._scan_backend == 'rtl_power':
            return  # rtl_power already sweeps the whole band each scan — no hop
        if not self._sweep_enabled:
            return
        if not self._sweep_centers:
            self._sweep_centers = self._build_sweep_centers()
            # Start from the segment nearest this device's configured center so
            # the first hop moves to a segment it hasn't just scanned.
            cur = self.device_config.get('center_freq', 0) or 0
            self._sweep_index = min(
                range(len(self._sweep_centers)),
                key=lambda i: abs(self._sweep_centers[i] - cur)
            )
            self.logger.info(
                f"Band sweep enabled for {self.device_serial}: segments "
                f"{[round(x/1e6, 3) for x in self._sweep_centers]} MHz, "
                f"hop after {self._sweep_dwell} empty scans"
            )
        if len(self._sweep_centers) < 2:
            return  # whole band fits one segment — nothing to sweep

        self._empty_scan_count += 1
        if self._empty_scan_count < self._sweep_dwell:
            return
        self._empty_scan_count = 0
        self._sweep_index = (self._sweep_index + 1) % len(self._sweep_centers)
        new_center = self._sweep_centers[self._sweep_index]
        self.logger.info(
            f"Band sweep: no sonde after {self._sweep_dwell} scans — retuning "
            f"{self.device_serial} to {new_center/1e6:.3f} MHz "
            f"(segment {self._sweep_index + 1}/{len(self._sweep_centers)})"
        )
        self.device_config['center_freq'] = new_center
        # Close the analyzer so the next scan cycle reopens at the new center.
        with self._device_lock:
            self._teardown_scan()

    def _teardown_decode(self):
        # Capture decode outcome BEFORE stopping — needed for the manager's
        # landed-sonde re-assignment guard below
        ended_source = self._decode_source
        ended_serial = self._cur_serial
        ended_freq   = self._cur_freq
        frames_decoded = self._decoder.frame_count if self._decoder else 0

        if self._decoder:
            try:
                self._decoder.stop()
            except Exception:
                pass
            self._decoder = None
        if self._pipeline:
            try:
                self._pipeline.stop()
            except Exception:
                pass
            self._pipeline = None
        if self._cur_freq:
            self.registry.unregister(self._cur_freq)
        self._cur_freq  = None
        self._cur_type  = None
        self._cur_serial = None
        self._cur_signal_strength_db = None
        self._cur_signal_power_dbfs = None
        self._decode_source = None
        was_manual = self._is_manual_decoder
        self._is_manual_decoder = False
        self._state     = self.STATE_IDLE

        # Notify the manager so Import API doesn't immediately re-assign a
        # landed/lost sonde back onto this (or another) device
        if ended_source == 'import_api' and self._manager is not None:
            try:
                self._manager.note_imported_decode_ended(
                    serial=ended_serial,
                    frequency=ended_freq,
                    last_alt_m=getattr(self, '_last_alt_m', None),
                    last_vv_ms=getattr(self, '_last_vv_ms', None),
                    frames_decoded=frames_decoded
                )
            except Exception as exc:
                self.logger.debug(f"Landed-sonde guard notification failed: {exc}")

        # Zero-frame auto decode → negative-result cache: stop the scanner
        # from re-picking the same birdie/phantom every cycle
        if (ended_source == 'auto' and frames_decoded == 0 and ended_freq
                and self._manager is not None):
            try:
                self._manager.note_auto_decode_failed(
                    ended_freq, snr=self._cur_signal_strength_db)
                # If this 0-frame decode came from the RS41 BW fast-path, the type
                # ID was probably wrong (a narrow-measuring DFM etc.) — force
                # dft_detect on the next detection so it's classified correctly.
                if self._cur_used_fastpath:
                    self._manager.note_fastpath_failed(ended_freq)
            except Exception as exc:
                self.logger.debug(f"Auto-decode failure notification failed: {exc}")

        # Mark that we need to wait before next USB reopen (prevents PLL lock failures)
        self._usb_reopen_after_decode = True
        
        # Critical: Wait for USB device to fully release after rtl_fm/decoder stop
        # Increased from 2s to 5s for better USB/PLL stability on some Raspberry Pi systems
        # Without this delay, reopening RTL-SDR too quickly causes PLL lock failures
        # and corrupted IQ data (exit code 206 in dft_detect)
        self.logger.debug("Waiting 5s for USB device to settle after decoder stop...")
        time.sleep(5.0)
        
        # Phase 2: If RX Scan is enabled and this was NOT a manual decoder, start next channel
        if self._rx_scan_enabled and not was_manual:
            self.logger.info("RX Scan: decode complete, cycling to next channel...")
            # Conservative 3-second USB delay before starting next decode
            time.sleep(3.0)
            # Start next channel (will set state to DECODING if successful)
            if not self._start_next_rx_scan_channel():
                self.logger.warning("RX Scan: failed to start next channel, returning to SCANNING")
                self._state = self.STATE_SCANNING

    # ------------------------------------------------------------------
    # Sonde-type identification
    # ------------------------------------------------------------------

    def _identify_sonde_type(self, sig: DetectedSignal) -> tuple:
        """
        Identify sonde type, returning (sonde_type, frequency_offset).
        
        Returns:
            Tuple of (sonde_type, offset_hz) where offset_hz is frequency correction from DFT
            or (sonde_type, 0.0) if using bandwidth fallback
        """
        sonde_offset = 0.0
        bw = sig.bandwidth
        self._cur_used_fastpath = False

        # If a previous fast-path RS41 decode at this frequency produced 0 frames,
        # the BW-based ID was likely wrong (e.g. a narrow-measuring DFM) — skip
        # the fast-path this time and let dft_detect decide.
        force_dft = (self._manager is not None
                     and self._manager.should_skip_fastpath(sig.frequency))

        # ---- RS41 fast-path (restores V1.0.50 behaviour) --------------------
        # A signal whose 3 dB width sits squarely in the RS41 band (3.5-7.0 kHz)
        # is RS41 with very high confidence: RS92 is narrower (2-3 kHz), DFM is
        # wider (>=7.5 kHz), M10/M20/iMet are much wider (9-24 kHz). For these we
        # SKIP dft_detect entirely and start decoding immediately.
        #
        # Why: the dft_detect step costs ~15 s of zero decoding per signal (close
        # device, 3 s settle, 5 s rtl_fm capture, correlate, 1 s, reopen) AND on
        # weak RS41 sometimes fails to reach the 0.53 correlation threshold, so
        # RS41 was both slow to start and occasionally missed entirely in V1.0.60
        # ("deaf", fewer frames). V1.0.50 identified RS41 by bandwidth instantly
        # and decoded rock-solid. dft_detect stays in the loop for wider/ambiguous
        # signals (DFM subtype, M10 vs M20, iMet) where it genuinely earns its
        # cost and where the Vigor patch improved things.
        if self.RS41_FASTPATH_ENABLED and not force_dft \
                and 400e6 <= sig.frequency <= 406e6 \
                and self.RS41_FASTPATH_BW_MIN <= bw <= self.RS41_FASTPATH_BW_MAX:
            self.logger.info(
                f"RS41 fast-path: BW {bw/1e3:.1f} kHz is unambiguously RS41 — "
                f"skipping dft_detect, decoding immediately (offset ignored for RS41)"
            )
            self._cur_used_fastpath = True
            return 'RS41', 0.0
        if force_dft and 400e6 <= sig.frequency <= 406e6 \
                and self.RS41_FASTPATH_BW_MIN <= bw <= self.RS41_FASTPATH_BW_MAX:
            self.logger.info(
                f"BW {bw/1e3:.1f} kHz is in RS41 fast-path range but a prior fast-path "
                f"decode here made 0 frames — using dft_detect to re-check the type"
            )
        # ---------------------------------------------------------------------

        if self._dft and self._dft.available:
            try:
                result = self._dft.detect_sonde_type(
                    frequency=sig.frequency,
                    device_serial=self.device_serial,
                    # sample_rate intentionally omitted: DftDetector auto-selects
                    # a narrower or wider IF capture rate from sig.bandwidth.
                    bandwidth=sig.bandwidth
                )
                if result:
                    # DFT returns tuple: (sonde_type, frequency_offset)
                    if isinstance(result, tuple) and len(result) > 1:
                        sonde_type, sonde_offset = result[0], result[1]
                        self.logger.info(
                            f"DFT identified {sonde_type} at {sig.frequency/1e6:.4f} MHz "
                            f"(offset: {sonde_offset:+.1f} Hz)"
                        )
                        return sonde_type, sonde_offset
                    else:
                        # Legacy single-value return
                        self.logger.info(
                            f"DFT identified {result} at {sig.frequency/1e6:.4f} MHz"
                        )
                        return result, 0.0
                self.logger.info("DFT: no confident match — using bandwidth fallback")
            except Exception as exc:
                self.logger.warning(f"DFT detection error: {exc}")
        
        # Bandwidth fallback returns no offset
        return self._bandwidth_fallback(sig), 0.0

    def _bandwidth_fallback(self, sig: DetectedSignal) -> str:
        """Classify sonde type by bandwidth with confidence-aware ambiguous zone handling."""
        bw   = sig.bandwidth
        freq = sig.frequency
        
        if 400e6 <= freq <= 406e6:
            # Clear boundaries with high confidence
            if bw >= 22000:   return 'M20'
            if bw >= 16000:   return 'iMet'
            if bw >= 14000:   return 'M10'
            if bw >= 12000:
                self.logger.info(f"BW {bw/1e3:.1f} kHz → RS92 (confident)")
                return 'RS92'

            # CRITICAL: previously this whole 6.5-10 kHz band defaulted to RS41,
            # which made the 'bw >= 10000: DFM' branch below only reachable for a
            # narrow 10-12 kHz sliver — but DFM's own typical range (per the note
            # below) is 7.5-8.5 kHz, i.e. squarely inside the old RS41 default.
            # That meant a real DFM signal got misclassified as RS41 almost every
            # time dft_detect failed to return a confident correlation match
            # (which was happening on ~100% of calls due to a CLI-argument-format
            # mismatch with the installed dft_detect build — see dft_detector.py).
            # RS41 typically 4.8-6 kHz, can drift to 7-7.5 kHz.
            # DFM typically 7.5-8.5 kHz, can vary up toward 10 kHz.
            # Split the ambiguous zone at 7.5 kHz so each type's *typical* range
            # is favored, instead of defaulting the whole band to one type.
            if 6500 <= bw < 7500:
                self.logger.warning(
                    f"BW {bw/1e3:.1f} kHz in ambiguous zone (6.5-7.5 kHz) "
                    f"→ RS41 (typical drift range, may be DFM)"
                )
                return 'RS41'

            if 7500 <= bw < 10000:
                self.logger.warning(
                    f"BW {bw/1e3:.1f} kHz in ambiguous zone (7.5-10 kHz) "
                    f"→ DFM (typical range, may be drifted RS41)"
                )
                return 'DFM'

            if bw >= 10000:
                # Strong DFM indicator above 10 kHz
                self.logger.info(f"BW {bw/1e3:.1f} kHz → DFM (high confidence)")
                return 'DFM'

            # Below 6.5 kHz: clear RS41
            self.logger.info(f"BW {bw/1e3:.1f} kHz → RS41 (confident)")
            return 'RS41'
        
        return 'RS41'

    def _is_blacklisted(self, freq_hz: float) -> bool:
        """Check if frequency is blacklisted (±2.5 kHz tolerance)"""
        return any(abs(freq_hz - b) < 2_500 for b in self._blacklist)

    @staticmethod
    def _near_grid(freq_hz: float, grid_hz: float, tol_hz: float) -> bool:
        """True if freq_hz is within tol_hz of a grid_hz multiple."""
        r = freq_hz % grid_hz
        return r <= tol_hz or (grid_hz - r) <= tol_hz

    def _repo_grid_ok(self, freq_hz: float) -> bool:
        """Whether an UNCONFIRMED candidate belongs in the frequency repository.

        Accept the coarse 100 kHz sonde-channel grid in every segment; inside a
        dense segment (403.0-404.0 DFM band) also accept the fine 10 kHz grid
        (403.13/403.55/…). Elsewhere the 10 kHz grid is rejected so the RTL's
        10 kHz-grid spurs (404.040/405.660/…) don't clutter the list. A decoded
        sonde is logged 'confirmed' separately, regardless of this filter."""
        if self._repo_grid_hz <= 0:
            return True  # filter disabled
        if self._near_grid(freq_hz, self._repo_grid_hz, self._repo_grid_tol_hz):
            return True
        if self._repo_dense_grid_hz > 0:
            for lo, hi in self._repo_dense_ranges:
                if lo <= freq_hz <= hi:
                    return self._near_grid(freq_hz, self._repo_dense_grid_hz,
                                           self._repo_dense_grid_tol_hz)
        return False
    
    def _get_decoder_path(self, sonde_type: str) -> Optional[str]:
        """Get decoder path for a sonde type."""
        # Normalize subtype labels like "DFM17"/"DFM09" back to the base
        # family ("DFM") first — otherwise this cooldown-path lookup picks
        # the wrong binary (rs41mod default) for imported/manual decodes
        # that report a specific DFM variant instead of the base type.
        normalized = RS1729Decoder.normalize_sonde_type(sonde_type)
        decoder_binary = RS1729Decoder.DECODER_MAP.get(normalized, 'rs41mod')
        # Delegate to RS1729Decoder's own path resolution (single source of
        # truth — this used to be a separately maintained, slightly-divergent
        # copy of the same lookup list).
        return RS1729Decoder.resolve_decoder_path(decoder_binary)

    def _is_fixed_channel_frequency(self, freq_hz: float) -> bool:
        """Check if frequency matches a configured fixed_channel (within 10 kHz tolerance)."""
        # Get fixed_channels from manager (they're stored at manager level)
        try:
            if not hasattr(self, '_manager') or not self._manager:
                self.logger.debug(f"_is_fixed_channel_frequency: No manager reference")
                return False
                
            if not hasattr(self._manager, '_fixed_channels'):
                self.logger.debug(f"_is_fixed_channel_frequency: Manager has no _fixed_channels attribute")
                return False
            
            fixed_channels = self._manager._fixed_channels
            if not fixed_channels:
                self.logger.debug(f"_is_fixed_channel_frequency: fixed_channels list is empty")
                return False
            
            self.logger.debug(f"_is_fixed_channel_frequency: Checking {freq_hz/1e6:.3f} MHz against {len(fixed_channels)} fixed channel(s)")
            
            for ch in fixed_channels:
                if not ch.get('enabled', False):
                    self.logger.debug(f"  Channel {ch.get('frequency')} MHz: disabled, skipping")
                    continue
                    
                ch_freq_mhz = ch.get('frequency', 0)
                ch_freq_hz = float(ch_freq_mhz) * 1e6
                freq_diff_khz = abs(freq_hz - ch_freq_hz) / 1e3
                
                self.logger.debug(
                    f"  Channel {ch_freq_mhz} MHz: diff={freq_diff_khz:.1f} kHz, "
                    f"type={ch.get('type')}, device={ch.get('receiver_device')}"
                )
                
                if abs(freq_hz - ch_freq_hz) < 10_000:  # Within 10 kHz
                    self.logger.info(
                        f"Frequency {freq_hz/1e6:.3f} MHz matches fixed_channel {ch_freq_mhz} MHz "
                        f"(type={ch.get('type')}) - will skip scanning"
                    )
                    return True
                    
            self.logger.debug(f"_is_fixed_channel_frequency: No match for {freq_hz/1e6:.3f} MHz")
            return False
            
        except Exception as exc:
            self.logger.error(f"_is_fixed_channel_frequency error: {exc}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Telemetry / frame conversion
    # ------------------------------------------------------------------

    def _on_frame(self, frame_data: dict):
        """Convert raw rs1729 frame dict → SondeTelemetry and forward upstream."""
        self._last_frame_t = time.time()
        try:
            sonde_id     = frame_data.get('sonde_id', 'UNKNOWN')
            frequency_hz = frame_data.get('frequency', self._cur_freq or 0.0)
            
            # Track current sonde serial for dashboard display
            if sonde_id and sonde_id != 'UNKNOWN':
                self._cur_serial = sonde_id

            # Track last altitude / vertical speed for the landed-sonde
            # re-assignment guard (manager.note_imported_decode_ended)
            try:
                if frame_data.get('alt') is not None:
                    self._last_alt_m = float(frame_data.get('alt'))
                if frame_data.get('velocity_vertical') is not None:
                    self._last_vv_ms = float(frame_data.get('velocity_vertical'))
            except (TypeError, ValueError):
                pass

            def _parse_db(val) -> Optional[float]:
                if val is None:
                    return None
                if isinstance(val, (int, float)):
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return None
                if isinstance(val, str):
                    m = re.search(r'(-?\d+(?:\.\d+)?)', val)
                    if m:
                        try:
                            return float(m.group(1))
                        except (TypeError, ValueError):
                            return None
                return None

            rssi_db = None
            for key in ('rssi', 'power_db', 'signal_db', 'signal_strength'):
                rssi_db = _parse_db(frame_data.get(key))
                if rssi_db is not None:
                    break

            snr_db = None
            for key in ('snr', 'signal_db', 'signal_strength'):
                snr_db = _parse_db(frame_data.get(key))
                if snr_db is not None:
                    break

            live_rssi = None
            live_snr = None
            if self._pipeline is not None:
                try:
                    live_rssi, live_snr = self._pipeline.get_signal_metrics_snapshot()
                except Exception:
                    live_rssi, live_snr = None, None

            # RSSI and SNR must come from DIFFERENT sources. Previously both fell
            # back to _cur_signal_strength_db (the scan SNR) when live metrics and
            # frame values were absent (the default direct-pipe chain has no live
            # metrics), so RSSI and SNR displayed the identical value. RSSI now
            # falls back to the scan's absolute peak power (dBFS); SNR to the scan
            # SNR. They only coincide if the analyzer couldn't supply a power.
            if live_rssi is not None:
                rssi_db = live_rssi
            elif rssi_db is None:
                rssi_db = self._cur_signal_power_dbfs
                if rssi_db is None:  # no absolute power available — last resort
                    rssi_db = self._cur_signal_strength_db

            if live_snr is not None:
                snr_db = live_snr
            elif snr_db is None:
                snr_db = self._cur_signal_strength_db

            # Frame number from parsed frame_data or fallback to raw line "[  361] …"
            frame_number = frame_data.get('frame_number', 0)
            if frame_number == 0:
                # Try fallback parsing from raw line
                raw = frame_data.get('raw_line', '')
                if '[' in raw and ']' in raw:
                    try:
                        frame_number = int(raw[raw.find('[')+1:raw.find(']')].strip())
                    except ValueError:
                        pass
            
            # Skip upload if frame_number is still 0 or None (invalid/failed decode)
            if not frame_number or frame_number == 0:
                self.logger.debug(
                    f"Skipping frame with invalid frame_number={frame_number} for {sonde_id} "
                    f"(likely incomplete decode)"
                )
                return
            
            # Get decoded datetime from sonde (NOT gateway time!)
            decoded_datetime = frame_data.get('decoded_datetime')
            if not decoded_datetime:
                # Fallback to UTC now only if no decoded time available
                decoded_datetime = datetime.utcnow()
                self.logger.warning(
                    f"No decoded_datetime available for {sonde_id} frame {frame_number}, "
                    f"using gateway time as fallback"
                )

            position = None
            if 'lat' in frame_data and 'lon' in frame_data and 'alt' in frame_data:
                position = SondePosition(
                    latitude=frame_data['lat'],
                    longitude=frame_data['lon'],
                    altitude=frame_data['alt'],
                    datetime=decoded_datetime  # Use decoded sonde time!
                )

            velocity = None
            if 'velocity_horizontal' in frame_data:
                velocity = SondeVelocity(
                    horizontal_speed=frame_data.get('velocity_horizontal', 0.0),
                    vertical_speed=frame_data.get('velocity_vertical', 0.0),
                    heading=frame_data.get('heading', 0.0)
                )

            environment = None
            if any(k in frame_data for k in ('temp', 'humidity', 'pressure')):
                environment = SondeEnvironment(
                    temperature=frame_data.get('temp'),
                    humidity=frame_data.get('humidity'),
                    pressure=frame_data.get('pressure')
                )

            telemetry = SondeTelemetry(
                sonde_type=frame_data.get('sonde_type', self._cur_type or 'RS41'),
                serial=sonde_id,
                frame_number=frame_number,
                subtype=frame_data.get('subtype'),
                dfmcode=frame_data.get('dfmcode'),  # DFM type code (e.g., "0xC")
                position=position,
                velocity=velocity,
                environment=environment,
                frequency=frequency_hz,
                snr=snr_db,
                rssi=rssi_db,
                satellites=frame_data.get('sats'),
                battery=frame_data.get('battery'),
                burst_timer=frame_data.get('burst_timer'),
                rs41_mainboard=frame_data.get('rs41_mainboard'),
                rs41_mainboard_fw=frame_data.get('rs41_mainboard_fw'),
                ref_datetime=frame_data.get('ref_datetime'),
                ref_position=frame_data.get('ref_position'),
                tx_frequency=frame_data.get('tx_frequency'),
                timestamp=decoded_datetime,  # Use decoded sonde time!
                decoder_name='rs1729',
                decoder_version='rs1729',
                receiver_device=self.device_serial
            )

            if self.telemetry_cb:
                self.telemetry_cb(telemetry)

        except Exception as exc:
            self.logger.error(f"Frame conversion error: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class RTLSDRDeviceManager:
    """
    Creates one DeviceWorker per configured RTL-SDR device and starts them all.
    Workers discover devices by serial number (USB order-independent) and
    independently scan → detect → decode in parallel.

    Exposes attributes compatible with web_server.py:
      .running           bool
      .lock              threading.Lock
      .active_decoders   dict {freq_hz: ActiveDecoder}
      .device_configs    list[dict]
      .first_device_serial   str
      .start_manual_decoder(freq, type) → bool
    """

    def __init__(self, config: dict,
                 telemetry_callback: Callable[[SondeTelemetry], None],
                 channelizer_status_output=None,
                 frequency_repository=None):
        self.config             = config
        self.telemetry_callback = telemetry_callback
        self.channelizer_status_output = channelizer_status_output
        # Optional per-session detected/confirmed frequency log (set by the app).
        self.frequency_repository = frequency_repository
        self.logger             = logging.getLogger('RTLSDRDeviceManager')
        self.running            = False
        self.lock               = threading.Lock()   # web_server compatibility

        self._registry = SondeRegistry()
        self._workers: List[DeviceWorker] = []
        self._status_thread: Optional[threading.Thread] = None  # Channelizer status sender

        # Build device list from config
        rtlsdr_cfg = config.get('sdr', {}).get('rtlsdr', {})
        if 'devices' in rtlsdr_cfg:
            self.device_configs = rtlsdr_cfg['devices']
            # Validate/repair per-device configs. A device entry missing
            # 'center_freq' or 'sample_rate' previously crashed SpectrumAnalyzer
            # on EVERY scan cycle (KeyError: 'center_freq'), spinning that
            # worker's log with a traceback every ~5 s and leaving the device
            # permanently dead. Fill sane defaults and warn once so one typo in
            # config.yaml degrades one device gracefully instead of wedging it.
            _default_center = rtlsdr_cfg.get('center_freq', 404_000_000)
            _default_rate = rtlsdr_cfg.get('sample_rate', 2_400_000)
            for _dev in self.device_configs:
                _serial = _dev.get('serial', '?')
                if _dev.get('center_freq') is None:
                    _dev['center_freq'] = _default_center
                    self.logger.warning(
                        f"Device {_serial} config missing 'center_freq' — "
                        f"defaulting to {_default_center/1e6:.3f} MHz. Set a "
                        f"'center_freq' for this device in config.yaml to control "
                        f"which part of the band it scans."
                    )
                if _dev.get('sample_rate') is None:
                    _dev['sample_rate'] = _default_rate
                    self.logger.warning(
                        f"Device {_serial} config missing 'sample_rate' — "
                        f"defaulting to {_default_rate/1e6:.1f} MSPS."
                    )
        else:
            # Legacy single-device format
            self.device_configs = [{
                'serial':      str(rtlsdr_cfg.get('device_index', 0)),
                'center_freq': rtlsdr_cfg.get('center_freq', 403_000_000),
                'sample_rate': rtlsdr_cfg.get('sample_rate', 2_400_000),
                'gain':        rtlsdr_cfg.get('gain', 40),
                'ppm_error':   rtlsdr_cfg.get('ppm_error', 0),
            }]

        self.first_device_serial = (
            self.device_configs[0]['serial'] if self.device_configs else '0'
        )
        self.logger.info(
            f"Configured {len(self.device_configs)} device(s): "
            f"{[d['serial'] for d in self.device_configs]}"
        )

        # Fixed channels support (up to 12 max for 3+ RTL-SDRs)
        det_cfg = config.get('detection', {})
        self._fixed_channels_enabled = det_cfg.get('fixed_channels_enable', False)
        self._fixed_channel_scantime = int(det_cfg.get('fixed_channel_scantime', 60))
        raw_fixed = det_cfg.get('fixed_channels', []) or []
        max_fixed = min(len(self.device_configs) * 4, 12)
        self._fixed_channels: List[dict] = list(raw_fixed[:max_fixed])
        self._fixed_start_done = (len(self._fixed_channels) == 0 or not self._fixed_channels_enabled)
        
        # Diagnostic logging for fixed_channels configuration
        self.logger.info(f"Fixed channels config: enabled={self._fixed_channels_enabled}, count={len(self._fixed_channels)}")
        if self._fixed_channels:
            for idx, ch in enumerate(self._fixed_channels):
                self.logger.info(
                    f"  Fixed channel {idx+1}: {ch.get('frequency')} MHz, type={ch.get('type')}, "
                    f"enabled={ch.get('enabled')}, device={ch.get('receiver_device')}"
                )
        
        # Priority frequency configuration
        self._priority_freq = det_cfg.get('priority_frequency')  # MHz
        self._priority_sonde_type = det_cfg.get('priority_sonde_type')  # RS41, DFM, etc.
        self._priority_timeout = det_cfg.get('priority_check_timeout', 30)  # seconds

        # Import API configuration
        import_api_cfg = config.get('import_api', {})
        self._import_api_enabled = import_api_cfg.get('enabled', False)
        if self._import_api_enabled:
            try:
                self._api_client = SondeApiClient(import_api_cfg)
                self.logger.info(f"Import API initialized: {import_api_cfg.get('url', 'api.opnwx.de')}")
            except Exception as e:
                self.logger.error(f"Failed to initialize Import API: {e}")
                self._import_api_enabled = False
                self._api_client = None
        else:
            self._api_client = None
            self.logger.debug("Import API disabled in configuration")

        # Landed-sonde reassignment guard: after an imported-sonde decode ends
        # idle, block that sonde from immediate re-assignment. Field-observed
        # churn: a sonde landed at ~13:41, the API kept listing it, and the
        # manager re-assigned it every poll — each time occupying a device for
        # the full manual_idle_time (600 s) decoding nothing.
        self._import_reassign_cooldown_s = float(import_api_cfg.get('reassign_cooldown_s', 600))
        self._import_landed_alt_m = float(import_api_cfg.get('landed_alt_m', 3000))
        self._import_landed_cooldown_s = float(import_api_cfg.get('landed_cooldown_s', 21600))
        self._import_blocked: Dict[str, tuple] = {}        # serial → (blocked_until, reason)
        self._import_blocked_freqs: Dict[float, tuple] = {}  # freq_hz → (blocked_until, reason)
        self._import_block_lock = threading.Lock()

        # Auto-decode failure cooldown (negative-result cache, auto_rx-style
        # temporary block list): a scan-detected frequency whose decoder
        # produced ZERO frames is very likely a birdie/DC spur/misclassified
        # signal — don't let the scanner re-pick it every cycle (field: a
        # phantom "DFM" at 404.5813 MHz occupied a device 300 s per cycle in
        # an endless detect→decode-nothing→rescan loop). Manual and Import
        # API decodes are NOT affected by this block.
        det_guard_cfg = config.get('detection', {})
        self._failed_decode_cooldown_s = float(det_guard_cfg.get('failed_decode_cooldown_s', 900))
        # (B) The block now uses an ESCALATING backoff instead of a flat cooldown,
        # and clears early when the signal reappears meaningfully stronger. This
        # stops a real (weak, ascending) sonde from being locked out for the full
        # cooldown while it strengthens: the FIRST 0-frame gets only a short
        # block (re-attempt soon if still detected); repeated 0-frames on the same
        # frequency escalate toward the cap (throttling a true birdie).
        self._failed_decode_base_cooldown_s = float(
            det_guard_cfg.get('failed_decode_base_cooldown_s', 60))
        # A signal reappearing this many dB above the SNR it failed at is treated
        # as an ascending/strengthening sonde → its block is cleared and retried.
        self._failed_decode_snr_rise_db = float(
            det_guard_cfg.get('failed_decode_snr_rise_db', 3.0))
        # freq_hz → {'until': ts, 'snr': dB|None, 'fails': n}
        self._auto_decode_failures: Dict[float, dict] = {}
        self._auto_fail_lock = threading.Lock()

        # RS41 fast-path self-correction: if a fast-path RS41 decode produces 0
        # frames, the BW-based ID was likely wrong (e.g. a just-starting DFM that
        # measured abnormally narrow and landed in the RS41 BW window). Record the
        # frequency so the NEXT detection skips the fast-path and uses dft_detect
        # for a proper type ID. TTL-bounded (reuses failed_decode_cooldown_s).
        self._fastpath_failed: Dict[float, float] = {}  # freq_hz → skip_until

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        for idx, dev_cfg in enumerate(self.device_configs):
            worker = DeviceWorker(
                device_config=dev_cfg,
                app_config=self.config,
                sonde_registry=self._registry,
                telemetry_callback=self.telemetry_callback,
                device_index=idx,  # Pass index for staggered USB init
                manager=self  # Pass manager reference for fixed_channels check
            )
            self._workers.append(worker)
        self.logger.info(f"Initialized {len(self._workers)} worker(s)")
        return True

    def start(self):
        self.running = True
        
        # Start workers with delays to prevent USB bus contention
        # Each worker will open its USB device soon after starting
        for idx, w in enumerate(self._workers):
            w.start()
            self.logger.info(f"Started worker {idx+1}/{len(self._workers)}: {w.device_serial}")
            
            # Check priority frequency on first worker after it initializes.
            # CRITICAL: Run in a background thread — priority_check_timeout can be
            # configured up to hours long (e.g. 14400s), and this loop must not block
            # startup of the remaining workers (NESDR002+ would never call w.start()
            # until the priority check finished or timed out).
            if idx == 0 and self._priority_freq and self._priority_freq > 0:
                threading.Thread(
                    target=self._run_priority_frequency_check,
                    args=(w,),
                    daemon=True,
                    name='PriorityFreqCheck'
                ).start()

            # CRITICAL: Delay between starting workers to prevent USB conflicts
            # This gives each worker time to initialize and open its USB device
            # before the next worker starts
            if idx < len(self._workers) - 1:  # Don't delay after last worker
                delay = 2.0  # 2 seconds between worker starts
                self.logger.debug(f"Waiting {delay}s before starting next worker")
                time.sleep(delay)
        self.logger.info("All device workers started")
        
        # Start Import API polling if enabled. Runs its own warm-up wait
        # (_start_import_api_polling → _wait_for_workers_ready) in a
        # background thread before the first poll/assignment, so an
        # immediate Import API match can't land a device on decode duty
        # while sibling devices are still mid-USB-open/PLL-negotiation.
        if self._import_api_enabled and self._api_client:
            threading.Thread(
                target=self._start_import_api_polling, daemon=True, name='ImportAPI-Warmup'
            ).start()
        
        # Start fixed channels if enabled (wait for workers to stabilize)
        self.logger.debug(f"Checking fixed_channels startup: enabled={self._fixed_channels_enabled}, channels={len(self._fixed_channels)}")
        
        if self._fixed_channels_enabled and self._fixed_channels:
            self.logger.info(f"Fixed Channels enabled: {len(self._fixed_channels)} channels configured")
            threading.Thread(
                target=self._start_fixed_channels, daemon=True, name='FixedChannels-RTL'
            ).start()
        elif self._fixed_channels:
            self.logger.info("Fixed Channels configured but disabled (fixed_channels_enable: false)")
        else:
            self.logger.debug("No fixed channels configured")
        
        # Start channelizer status sender if enabled
        if self.channelizer_status_output and self.channelizer_status_output.enabled:
            self._status_thread = threading.Thread(
                target=self._send_channelizer_status_loop, daemon=True, name='ChannelizerStatus'
            )
            self._status_thread.start()
            self.logger.info("Channelizer status sender started")

    def stop(self):
        self.running = False
        
        # Stop Import API polling
        if self._api_client:
            self._api_client.stop()
        
        # Stop channelizer status sender
        if self._status_thread:
            self._status_thread.join(timeout=2)
            self._status_thread = None
        
        for w in self._workers:
            w.stop()
        self.logger.info("All device workers stopped")

    def stop_all_decoders(self):
        """Force stop all active decoders and return devices to scanning/idle state.
        
        Used for emergency cleanup when decoders are stuck (e.g., PLL failures during priority check).
        """
        stopped_count = 0
        for w in self._workers:
            if w._state == w.STATE_DECODING:
                self.logger.info(f"Force stopping decoder on device {w.device_serial}")
                w.stop_decode_and_scan()
                stopped_count += 1
        
        if stopped_count > 0:
            self.logger.info(f"Force stopped {stopped_count} decoder(s)")
        else:
            self.logger.debug("No active decoders to stop")
    
    def _send_channelizer_status_loop(self):
        """
        Background thread that periodically sends channelizer status updates via UDP.
        
        Collects active channel info from all workers and sends formatted status
        similar to receivemultisonde's slot status output.
        """
        while self.running:
            try:
                if self.channelizer_status_output and self.channelizer_status_output.should_send_update():
                    # Collect status from all workers
                    device_statuses = {}
                    for worker in self._workers:
                        device_statuses[worker.device_serial] = {
                            'decoder_mode': worker.decoder_mode,
                            'channelizer_active': worker.get_channelizer_channel_details()
                        }
                    
                    # Send aggregated status
                    self.channelizer_status_output.send_status(device_statuses)
                
                # Sleep for 1 second between checks (actual send controlled by update_interval)
                time.sleep(1)
            
            except Exception as e:
                self.logger.error(f"Error in channelizer status sender: {e}", exc_info=True)
                time.sleep(5)
    
    def _run_priority_frequency_check(self, w):
        """Background-thread entry point: wait for worker USB init, then run the
        (potentially long-running) priority frequency check without blocking start()."""
        max_wait = 15  # seconds to wait for USB init
        waited = 0
        while waited < max_wait and w._analyzer is None:
            time.sleep(0.5)
            waited += 0.5

        if w._analyzer is not None:
            # USB initialized successfully, now check priority frequency
            self._check_priority_frequency()
        else:
            self.logger.warning("First worker USB initialization timeout, skipping priority check")

    def _check_priority_frequency(self):
        """Check priority frequency on first available worker before starting normal scanning."""
        if not self._priority_freq or self._priority_freq <= 0:
            return
        
        priority_freq_hz = self._priority_freq * 1e6
        sonde_type = self._priority_sonde_type or 'RS41'
        timeout = self._priority_timeout
        
        self.logger.info(
            f"Checking priority frequency: {self._priority_freq:.3f} MHz "
            f"as {sonde_type} for {timeout}s before starting scanner"
        )
        
        # Use first worker for priority check
        worker = self._workers[0]
        
        # Start manual decoder on priority frequency
        success = worker.start_manual_decode(
            frequency=priority_freq_hz,
            sonde_type=sonde_type,
            duration_seconds=timeout,
            source='priority'
        )
        
        if not success:
            self.logger.warning("Failed to start priority frequency decoder")
            return

        # CRITICAL: Blacklist this frequency on every other worker for as long as
        # the priority decode is active. Without this, any other worker whose scan
        # range overlaps the priority frequency re-detects it on every cycle and
        # runs the full teardown + 3s settle + DFT correlation dance, only to
        # abort once the registry catches the duplicate — wasting USB/CPU in a
        # tight loop indefinitely instead of just skipping it up front.
        # CRITICAL: reassign w._blacklist to a NEW list rather than mutating the
        # existing one in place. This runs on a background thread while each
        # worker's own thread concurrently reads self._blacklist in
        # _is_blacklisted() (a `for b in self._blacklist` loop) — mutating a
        # list another thread may be mid-iteration over is a real (if rare)
        # race. Swapping the attribute to a fresh list is race-free: any
        # in-flight iteration keeps seeing the old list object to completion,
        # and every subsequent attribute read gets the updated one.
        other_workers = [w for w in self._workers if w is not worker]
        for w in other_workers:
            w._blacklist = w._blacklist + [priority_freq_hz]

        # Wait for priority check timeout
        start_time = time.time()
        frames_received = False

        while time.time() - start_time < timeout:
            # Check if frames are being decoded
            decoder = worker._decoder
            if decoder and decoder.frame_count > 0:
                frames_received = True
                self.logger.info(
                    f"Priority frequency is decoding successfully "
                    f"({decoder.frame_count} frames) - keeping active"
                )
                # Keep decoder running, don't stop it — and keep the frequency
                # blacklisted on other workers for as long as it's decoding
                return

            time.sleep(1.0)

        # Timeout reached without successful decode
        if not frames_received:
            self.logger.info(
                f"Priority frequency check timeout ({timeout}s) - "
                f"no frames decoded, returning to scan mode"
            )
            # Frequency is free again - let other workers detect it normally.
            # Same copy-on-write reasoning as above: rebuild a new list rather
            # than mutating w._blacklist in place.
            for w in other_workers:
                if priority_freq_hz in w._blacklist:
                    w._blacklist = [f for f in w._blacklist if f != priority_freq_hz]
            # Force return to scanning
            worker.stop_decode_and_scan()
    
    def note_imported_decode_ended(self, serial: Optional[str], frequency: Optional[float],
                                   last_alt_m: Optional[float], last_vv_ms: Optional[float],
                                   frames_decoded: int):
        """Called by a DeviceWorker when an Import-API-sourced decode ends
        (idle timeout / stop). Registers a re-assignment block so the next
        API poll doesn't immediately re-occupy a device with the same sonde.

        Heuristics:
        - Last decoded altitude below landed_alt_m while descending → the
          sonde is on the ground (or about to be): long cooldown.
        - Otherwise (lost high, or never decoded a single frame): short
          cooldown — enough to break the assign→idle→assign loop while still
          allowing re-acquisition of a sonde that comes back over the horizon.
        - Zero frames ever decoded → no serial known: block by frequency.
        """
        now = time.time()
        if (last_alt_m is not None and last_alt_m < self._import_landed_alt_m
                and (last_vv_ms is None or last_vv_ms < 0)):
            cooldown = self._import_landed_cooldown_s
            reason = (f"landed (last alt {last_alt_m:.0f} m, "
                      f"vV {last_vv_ms if last_vv_ms is not None else '?'} m/s)")
        else:
            cooldown = self._import_reassign_cooldown_s
            if frames_decoded > 0:
                reason = (f"signal lost (last alt "
                          f"{f'{last_alt_m:.0f} m' if last_alt_m is not None else 'unknown'})")
            else:
                reason = "no frames decoded"

        with self._import_block_lock:
            if serial:
                self._import_blocked[serial] = (now + cooldown, reason)
            elif frequency:
                self._import_blocked_freqs[frequency] = (now + cooldown, reason)

        target = serial or (f"{frequency/1e6:.3f} MHz" if frequency else "unknown")
        self.logger.info(
            f"Import API: blocking re-assignment of {target} for "
            f"{cooldown/60:.0f} min — {reason} ({frames_decoded} frames decoded)"
        )

    def note_fastpath_failed(self, frequency: float):
        """A fast-path RS41 decode produced 0 frames — the BW-based ID was likely
        wrong (a DFM/other that measured narrow). Force dft_detect on the next
        detection of this frequency (±10 kHz) for the failed_decode_cooldown_s TTL."""
        with self._auto_fail_lock:
            self._fastpath_failed[frequency] = time.time() + self._failed_decode_cooldown_s
        self.logger.info(
            f"Fast-path RS41 at {frequency/1e6:.4f} MHz produced 0 frames — will use "
            f"dft_detect (not the BW fast-path) next time to re-check the sonde type"
        )

    def should_skip_fastpath(self, frequency: float) -> bool:
        """True if a recent fast-path RS41 decode at this frequency (±10 kHz)
        produced 0 frames, so the RS41 BW fast-path should be skipped in favour
        of dft_detect."""
        now = time.time()
        with self._auto_fail_lock:
            for f in [f for f, until in self._fastpath_failed.items() if until <= now]:
                del self._fastpath_failed[f]
            return any(abs(f - frequency) < 10_000 for f in self._fastpath_failed)

    def note_auto_decode_failed(self, frequency: float, snr: Optional[float] = None):
        """A scan-triggered (auto) decode ended with zero frames — block this
        frequency from auto re-detection. (B) Uses an escalating backoff: the
        Nth consecutive 0-frame near this frequency blocks for
        base * 2^(N-1), capped at failed_decode_cooldown_s. So the first miss on
        a weak-but-real sonde only pauses briefly (it's retried soon and decodes
        once it strengthens), while a true birdie that keeps producing 0 frames
        is throttled toward the long cap. Records the SNR so is_auto_decode_blocked
        can clear the block early if the signal reappears stronger."""
        now = time.time()
        with self._auto_fail_lock:
            # Carry the consecutive-fail count from a recent entry near this freq.
            fails = 1
            for f, e in list(self._auto_decode_failures.items()):
                if abs(f - frequency) < 10_000:
                    fails = int(e.get('fails', 1)) + 1
                    del self._auto_decode_failures[f]
            dur = min(self._failed_decode_base_cooldown_s * (2 ** (fails - 1)),
                      self._failed_decode_cooldown_s)
            self._auto_decode_failures[frequency] = {
                'until': now + dur, 'snr': snr, 'fails': fails}
        snr_s = f" (SNR {snr:.1f} dB)" if snr is not None else ""
        self.logger.info(
            f"Auto-decode produced 0 frames at {frequency/1e6:.4f} MHz{snr_s} — "
            f"blocking auto re-detection for {dur/60:.1f} min (fail #{fails}; clears "
            f"early if it reappears ≥{self._failed_decode_snr_rise_db:.0f} dB stronger)"
        )

    def is_auto_decode_blocked(self, frequency: float,
                               current_snr: Optional[float] = None) -> bool:
        """True if this frequency is in its failed-decode cooldown (±10 kHz).
        (B) If current_snr is given and the signal has reappeared at least
        failed_decode_snr_rise_db above the SNR it failed at, the block is
        CLEARED and False is returned — an ascending/strengthening sonde is
        retried immediately instead of waiting out the cooldown."""
        now = time.time()
        with self._auto_fail_lock:
            for f in [f for f, e in self._auto_decode_failures.items()
                      if e.get('until', 0) <= now]:
                del self._auto_decode_failures[f]
            for f, e in list(self._auto_decode_failures.items()):
                if abs(f - frequency) >= 10_000:
                    continue
                blk_snr = e.get('snr')
                if current_snr is not None and blk_snr is not None and \
                        current_snr >= blk_snr + self._failed_decode_snr_rise_db:
                    del self._auto_decode_failures[f]
                    self.logger.info(
                        f"{frequency/1e6:.4f} MHz reappeared at {current_snr:.1f} dB "
                        f"(failed at {blk_snr:.1f} dB) — clearing block, retrying"
                    )
                    return False
                return True
            return False

    def _get_import_block_reason(self, serial: Optional[str],
                                 frequency: Optional[float]) -> Optional[str]:
        """Return the block reason if this sonde is under re-assignment
        cooldown, else None. Expired entries are purged."""
        now = time.time()
        with self._import_block_lock:
            for table in (self._import_blocked, self._import_blocked_freqs):
                expired = [k for k, (until, _) in table.items() if until <= now]
                for k in expired:
                    del table[k]

            if serial and serial in self._import_blocked:
                until, reason = self._import_blocked[serial]
                return f"{reason} (retry in {(until - now)/60:.0f} min)"
            if frequency:
                for freq, (until, reason) in self._import_blocked_freqs.items():
                    if abs(freq - frequency) < 20_000:
                        return f"{reason} (retry in {(until - now)/60:.0f} min)"
        return None

    def _on_imported_sondes(self, sondes: List[Dict]):
        """Callback for Import API: assign detected sondes to available SDR receivers.
        
        Args:
            sondes: List of sonde dicts with keys: serial, frequency, type, distance_km, lat, lon, alt
        """
        if not self.running or not sondes:
            return
        
        self.logger.info(f"Import API detected {len(sondes)} nearby sondes")
        
        # Find available workers (IDLE or SCANNING, not DECODING, and not already
        # mid-flight into a manual decode e.g. priority frequency startup — that
        # state transiently looks IDLE before its own _start_decode() runs, so
        # without this check we can double-book the same device and the imported
        # sonde's decode ends up winning the race and evicting the manual one)
        available_workers = [
            w for w in self._workers
            if w._state in (w.STATE_IDLE, w.STATE_SCANNING)
            and not w._manual_decode_pending.is_set()
        ]
        
        if not available_workers:
            self.logger.info("No available SDR receivers for imported sondes (all busy decoding)")
            return
        
        self.logger.info(f"Found {len(available_workers)} available receivers")
        
        # Assign sondes to available workers (prioritized by distance - nearest first)
        assigned_count = 0
        for sonde in sondes:
            if assigned_count >= len(available_workers):
                self.logger.info(f"All available receivers assigned, skipping remaining sondes")
                break
            
            serial = sonde['serial']
            frequency = sonde['frequency']  # Hz
            sonde_type = sonde['type']
            distance = sonde['distance_km']
            
            # Check if this frequency is already being decoded
            if self._registry.is_active(frequency):
                self.logger.debug(
                    f"Skipping imported sonde {serial} @ {frequency/1e6:.3f} MHz: "
                    f"already being decoded"
                )
                continue

            # Landed-sonde / churn guard: skip sondes whose previous decode
            # attempt ended idle (landed or signal lost) and are in cooldown
            block_reason = self._get_import_block_reason(serial, frequency)
            if block_reason:
                self.logger.info(
                    f"Skipping imported sonde {serial} @ {frequency/1e6:.3f} MHz: "
                    f"{block_reason}"
                )
                continue

            # Get next available worker
            worker = available_workers[assigned_count]
            
            self.logger.info(
                f"Assigning imported sonde {serial} ({sonde_type}) @ {frequency/1e6:.3f} MHz "
                f"({distance:.1f}km) to device {worker.device_serial}"
            )
            
            # Start manual decode on this worker
            # Use unlimited duration (None) so it keeps decoding until sonde disappears
            success = worker.start_manual_decode(
                frequency=frequency,
                sonde_type=sonde_type,
                duration_seconds=None,  # Decode until sonde lost
                source='import_api'
            )
            
            if success:
                assigned_count += 1
                self.logger.info(
                    f"Successfully started decoder for imported sonde {serial} "
                    f"on device {worker.device_serial}"
                )
            else:
                self.logger.warning(
                    f"Failed to start decoder for imported sonde {serial} "
                    f"on device {worker.device_serial}"
                )
        
        if assigned_count > 0:
            self.logger.info(
                f"Import API: assigned {assigned_count}/{len(sondes)} sondes to available receivers"
            )
        else:
            self.logger.debug("Import API: no new sondes assigned (all already being decoded)")

    # ------------------------------------------------------------------
    # Fixed-channel startup
    # ------------------------------------------------------------------

    def _wait_for_workers_ready(self, label: str, min_wait: float = 20, max_wait: float = 40,
                                 final_buffer: float = 2.0):
        """Block until all workers have opened their RTL-SDR device and reached
        SCANNING/DECODING at least once (or max_wait elapses). Any startup path
        that immediately assigns a device to a decode (fixed_channels, Import
        API) needs this — without it, the assignment can land in the middle of
        the multi-device USB-open/PLL-negotiation storm that the workers'
        internal staggered-init delays (5s/7.5s/10s/12.5s...) spread out over,
        producing a "healthy" decoder that silently never receives clean IQ
        data (observed in the field as RS41/DFM decoders staying alive with
        zero frames when an Import API assignment landed within the first
        ~15s of process start, while sibling devices were still mid-PLL-retry)."""
        self.logger.info(f"{label}: waiting for all workers to complete first scan cycle...")
        start_wait = time.time()

        # First, wait for minimum time
        while time.time() - start_wait < min_wait:
            time.sleep(1.0)

        elapsed = time.time() - start_wait
        self.logger.info(f"{label}: minimum wait complete ({elapsed:.1f}s)")

        # Then verify all workers are in SCANNING or DECODING state
        while time.time() - start_wait < max_wait:
            all_scanning = True
            for w in self._workers:
                if w.state not in (DeviceWorker.STATE_SCANNING, DeviceWorker.STATE_DECODING):
                    all_scanning = False
                    break

            if all_scanning:
                elapsed = time.time() - start_wait
                self.logger.info(f"{label}: all workers ready after {elapsed:.1f}s total")
                break

            time.sleep(1.0)
        else:
            # Timeout reached
            self.logger.warning(
                f"{label}: timeout waiting for workers (some may still be initializing)"
            )

        # Final buffer to ensure USB devices are fully settled
        self.logger.info(f"{label}: adding {final_buffer:.0f}s final buffer for USB stability...")
        time.sleep(final_buffer)

    def _start_import_api_polling(self):
        """Warm-up wrapper around SondeApiClient.start(): waits for all workers
        to be past their initial USB-open/PLL-negotiation storm before the
        first Import API poll can assign (and immediately start decoding on)
        a device — see _wait_for_workers_ready() docstring for the failure
        mode this avoids. Runs in its own thread so RTLSDRDeviceManager.start()
        itself stays non-blocking."""
        self._wait_for_workers_ready("Import API")
        self.logger.info("Starting Import API polling...")
        self._api_client.start(self._on_imported_sondes)

    def _start_fixed_channels(self):
        """Decode fixed_channels list at startup with RX Scan cycling (Phase 2).

        Phase 2 Implementation:
        - Groups channels by device
        - Enables RX Scan cycling on each device
        - Each device cycles through its assigned channels every fixed_channel_scantime seconds
        - Conservative 3-second USB delays between device stops and starts
        """
        self._wait_for_workers_ready("Fixed Channels")

        try:
            self.logger.debug("Fixed Channels: starting channel assignment phase")
            
            # Filter for enabled channels only
            enabled_channels = [ch for ch in self._fixed_channels if ch.get('enabled', False)]
            
            if not enabled_channels:
                self.logger.info("No enabled Fixed Channels to start")
                return
            
            # Group channels by device
            device_channels = {}
            for ch in enabled_channels:
                device_id = ch.get('receiver_device', '')
                device_serial = device_id.split(':')[-1] if ':' in device_id else device_id
                if device_serial not in device_channels:
                    device_channels[device_serial] = []
                device_channels[device_serial].append(ch)
            
            # Check which channels have rx_scan enabled
            rx_scan_channels = [ch for ch in enabled_channels if ch.get('rx_scan', False)]
            continuous_channels = [ch for ch in enabled_channels if not ch.get('rx_scan', False)]
            
            self.logger.info(
                f"Fixed Channels: {len(enabled_channels)} total - "
                f"{len(rx_scan_channels)} RX Scan (cycling), "
                f"{len(continuous_channels)} continuous"
            )
            
            # Start RX Scan cycling or continuous decode per device
            success_count = 0
            skipped_count = 0
            
            for worker in self._workers:
                worker_channels = device_channels.get(worker.device_serial, [])
                
                if not worker_channels:
                    self.logger.debug(f"Device {worker.device_serial}: no fixed channels assigned")
                    continue
                
                self.logger.debug(
                    f"Device {worker.device_serial}: {len(worker_channels)} channel(s) assigned, "
                    f"current state={worker.state}"
                )
                
                # Check if ANY channel for this device has rx_scan enabled
                has_rx_scan = any(ch.get('rx_scan', False) for ch in worker_channels)
                rx_scan_count = sum(1 for ch in worker_channels if ch.get('rx_scan', False))
                continuous_count = len(worker_channels) - rx_scan_count
                
                if has_rx_scan:
                    # Phase 2: RX Scan cycling mode (at least one channel has rx_scan=true)
                    # Only include channels with rx_scan=true in rotation
                    cycling_channels = [ch for ch in worker_channels if ch.get('rx_scan', False)]
                    
                    self.logger.info(
                        f"Device {worker.device_serial}: RX Scan mode with "
                        f"{len(cycling_channels)} cycling channel(s), "
                        f"scantime={self._fixed_channel_scantime}s"
                    )
                    
                    if continuous_count > 0:
                        self.logger.warning(
                            f"Device {worker.device_serial}: {continuous_count} channel(s) with "
                            "rx_scan=false will be SKIPPED (device will cycle through rx_scan=true channels)"
                        )
                    
                    worker.enable_rx_scan(cycling_channels)
                    # Start first channel (will auto-cycle after scantime expires)
                    if worker._start_next_rx_scan_channel():
                        success_count += 1
                    else:
                        self.logger.warning(
                            f"Device {worker.device_serial}: Failed to start RX Scan"
                        )
                        skipped_count += 1
                        
                elif len(worker_channels) == 1:
                    # Phase 1: Single channel with rx_scan=false - continuous decode
                    ch = worker_channels[0]
                    freq_hz = float(ch['frequency']) * 1e6
                    stype = str(ch.get('type', 'RS41'))
                    
                    self.logger.info(
                        f"Device {worker.device_serial}: continuous decode of {stype} at "
                        f"{freq_hz/1e6:.3f} MHz (single channel, rx_scan=false)"
                    )
                    
                    try:
                        self.logger.debug(
                            f"Calling start_manual_decode() for {worker.device_serial}: "
                            f"freq={freq_hz/1e6:.3f} MHz, type={stype}"
                        )
                        result = worker.start_manual_decode(freq_hz, stype, duration_seconds=None,
                                                             source='fixed_channel')
                        self.logger.debug(
                            f"start_manual_decode() returned: {result} for {worker.device_serial}"
                        )
                        
                        if result:
                            success_count += 1
                        else:
                            self.logger.warning(
                                f"Device {worker.device_serial}: Failed to start decoder (returned False)"
                            )
                            skipped_count += 1
                    except Exception as e:
                        self.logger.error(
                            f"Device {worker.device_serial}: Exception starting decoder: {e}",
                            exc_info=True
                        )
                        skipped_count += 1
                        
                else:
                    # Multiple channels, all with rx_scan=false - CONFLICT!
                    # Can only decode one frequency at a time - start first, skip rest
                    ch = worker_channels[0]
                    freq_hz = float(ch['frequency']) * 1e6
                    stype = str(ch.get('type', 'RS41'))
                    
                    self.logger.warning(
                        f"Device {worker.device_serial}: {len(worker_channels)} channels configured "
                        "with rx_scan=false, but can only decode ONE at a time. "
                        f"Starting first channel ({freq_hz/1e6:.3f} MHz), "
                        f"skipping {len(worker_channels)-1} other(s). "
                        "Set rx_scan=true to enable cycling."
                    )
                    
                    try:
                        self.logger.debug(
                            f"Calling start_manual_decode() for {worker.device_serial}: "
                            f"freq={freq_hz/1e6:.3f} MHz, type={stype}"
                        )
                        result = worker.start_manual_decode(freq_hz, stype, duration_seconds=None,
                                                             source='fixed_channel')
                        self.logger.debug(
                            f"start_manual_decode() returned: {result} for {worker.device_serial}"
                        )
                        
                        if result:
                            success_count += 1
                            skipped_count += len(worker_channels) - 1
                        else:
                            self.logger.warning(
                                f"Device {worker.device_serial}: Failed to start decoder (returned False)"
                            )
                            skipped_count += len(worker_channels)
                    except Exception as e:
                        self.logger.error(
                            f"Device {worker.device_serial}: Exception starting decoder: {e}",
                            exc_info=True
                        )
                        skipped_count += len(worker_channels)
                
                # CRITICAL: 3-second delay between device starts to prevent USB conflicts
                time.sleep(3.0)
            
            self.logger.info(
                f"Fixed Channels startup complete: {success_count}/{len(self._workers)} "
                f"device(s) started, {skipped_count} channel(s) skipped"
            )
        
        except Exception as e:
            self.logger.error(
                f"Fatal error in Fixed Channels startup: {e}",
                exc_info=True
            )
                
        finally:
            self._fixed_start_done = True



    # ------------------------------------------------------------------
    # web_server.py compatible API
    # ------------------------------------------------------------------

    @property
    def active_decoders(self) -> Dict[float, ActiveDecoder]:
        """Return {frequency: ActiveDecoder} for all currently-decoding workers."""
        result = {}
        for w in self._workers:
            ad = w.get_active_decoder()
            if ad:
                result[ad.signal.frequency] = ad
        return result

    def start_manual_decoder(self, frequency: float, sonde_type: str,
                           duration_seconds: Optional[float] = None) -> bool:
        """Start a manual decoder on the first non-decoding worker (web UI).
        
        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            duration_seconds: If set, auto-return to scanning after this many seconds.
        """
        # Prefer a worker already in SCANNING state (SDR already warm)
        candidates = sorted(
            self._workers,
            key=lambda w: 0 if w.state == DeviceWorker.STATE_SCANNING else 1
        )
        for w in candidates:
            if w.state != DeviceWorker.STATE_DECODING:
                duration_label = 'infinite' if not duration_seconds or duration_seconds <= 0 else f'{int(duration_seconds)}s'
                self.logger.info(
                    f"Manual {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"({duration_label}) → device {w.device_serial}"
                )
                return w.start_manual_decode(frequency, sonde_type, duration_seconds)
        self.logger.warning("No available worker for manual decoder")
        return False

    def start_manual_decoder_on(self, frequency: float, sonde_type: str,
                                device_serial: str = None,
                                duration_seconds: Optional[float] = None) -> bool:
        """Start a manual decoder targeting a specific device (or auto-select).
        
        Args:
            frequency: Target frequency in Hz
            sonde_type: Sonde type (RS41, RS92, etc.)
            device_serial: Target device serial (None = auto-select first available)
            duration_seconds: If set, auto-return to scanning after this many seconds.
        """
        if not device_serial:
            return self.start_manual_decoder(frequency, sonde_type, duration_seconds)
        for w in self._workers:
            if w.device_serial == device_serial:
                if w.state == DeviceWorker.STATE_DECODING:
                    self.logger.warning(
                        f"Device {device_serial} already decoding; "
                        "cannot start another manual decoder on it"
                    )
                    return False
                duration_label = 'infinite' if not duration_seconds or duration_seconds <= 0 else f'{int(duration_seconds)}s'
                self.logger.info(
                    f"Manual {sonde_type} at {frequency/1e6:.3f} MHz "
                    f"({duration_label}) → device {device_serial} (explicit)"
                )
                return w.start_manual_decode(frequency, sonde_type, duration_seconds)
        self.logger.warning(f"Device {device_serial} not found in worker list")
        return False

    def get_worker_status(self) -> List[dict]:
        """Per-worker state summary for web UI /api/devices."""
        result = []
        for w in self._workers:
            if w.state == 'decoding' and w.current_freq:
                freq_mhz   = w.current_freq / 1e6
                freq_label = f"{freq_mhz:.3f} MHz"
            elif w.state == 'scanning':
                cf         = w.device_config.get('center_freq', 0)
                sr         = w.device_config.get('sample_rate', 2_400_000)
                low_mhz    = (cf - sr / 2) / 1e6
                high_mhz   = (cf + sr / 2) / 1e6
                freq_mhz   = cf / 1e6
                freq_label = f"{low_mhz:.1f}-{high_mhz:.1f} MHz"
            else:
                freq_mhz   = None
                freq_label = None
            
            # Step 4: Add channelizer status info
            result.append({
                'serial':                w.device_serial,
                'state':                 w.state,
                'frequency':             freq_mhz,
                'freq_label':            freq_label,
                'sonde_type':            w.current_sonde_type,
                'sonde_serial':          w.current_sonde_serial,
                'decode_source':         w.decode_source,  # 'auto'/'manual'/'priority'/'fixed_channel'/None
                'gain':                  w.device_config.get('gain', 40),  # Current gain setting
                'decoder_mode':          w.decoder_mode,  # 'legacy' or 'channelizer'
                'channelizer_active':    w.get_channelizer_channel_details(),  # Active channel details
                'channelizer_max':       w.channelizer_max_channels,     # Max channels (0 for legacy)
                'scan_return_eta_s':     w.get_scan_return_eta_s(),  # Seconds until back to scanning, or None
                'sweep_enabled':         bool(getattr(w, '_sweep_enabled', False)),  # band-sweep active for this device
            })
        return result

    def get_spectrum_receivers(self) -> List[dict]:
        """Return selectable spectrum receiver list for web UI."""
        return [
            {
                'id': f"rtlsdr:{w.device_serial}",
                'name': f"RTL-SDR {w.device_serial}",
            }
            for w in self._workers
        ]

    def get_spectrum_for_receiver(self, receiver_id: str) -> dict:
        """Return latest spectrum for selected receiver id (rtlsdr:<serial>)."""
        target = receiver_id or ''
        if not target.startswith('rtlsdr:'):
            target = self.get_spectrum_receivers()[0]['id'] if self._workers else ''

        serial = target.split(':', 1)[1] if ':' in target else ''
        self.logger.debug(f"get_spectrum_for_receiver({receiver_id}) looking for serial='{serial}'")
        for w in self._workers:
            if w.device_serial == serial:
                self.logger.debug(f"Found worker with serial={serial}, state={w.state}")
                spec = w.get_spectrum()
                if spec:
                    self.logger.debug(f"Returning spectrum with {len(spec.get('freqs_mhz', []))} points")
                    return spec
                self.logger.debug(f"Worker {serial} returned empty spectrum, returning fallback")
                return {
                    'receiver_id': target,
                    'receiver_name': f"RTL-SDR {serial}",
                    'freqs_mhz': [],
                    'power_db': [],
                    'signals': [],
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                }

        self.logger.error(f"Worker with serial={serial} NOT FOUND in {len(self._workers)} workers")
        return {}
    
    # ------------------------------------------------------------------
    # Runtime configuration (for web UI)
    # ------------------------------------------------------------------
    
    def set_debug_mode(self, enabled: bool):
        """Enable/disable debug logging at runtime."""
        # Propagate to all workers
        for w in self._workers:
            # Workers inherit logger from parent, no need to propagate
            pass
    
    def set_snr_threshold(self, threshold_db: float):
        """Update SNR detection threshold at runtime (currently not used by RTL-SDR mode)."""
        self.logger.info(f"SNR threshold updated to {threshold_db} dB (note: RTL-SDR uses fixed detection threshold)")
    
    def set_scan_interval(self, seconds: float):
        """Update scan interval at runtime (currently not dynamically adjustable)."""
        self.logger.info(f"Scan interval updated to {seconds}s (note: requires restart to apply)")
    
    def set_fixed_channel_scantime(self, seconds: int):
        """Update Fixed Channel scan time (Phase 2: RX Scan cycling duration)."""
        self._fixed_channel_scantime = seconds
        self.logger.info(f"Fixed Channel scantime set to {seconds}s (will be used in Phase 2 RX Scan)")

    def reload_detection_config(self) -> bool:
        """Re-read the `detection:` section from config.yaml on disk and
        apply it in place to the shared app config dict, so the web UI's
        'Start Scan' (force clean restart) button can pick up freshly-
        edited scan tuning — scan_check_time, max_peaks, channel_spacing_hz,
        detect_confirm_time, etc. — without a full service restart.
        Mutates self.config in place: every DeviceWorker.app_config is the
        SAME dict object, so this takes effect for all workers immediately.
        """
        try:
            import yaml
            with open('config.yaml', 'r', encoding='utf-8') as f:
                fresh = yaml.safe_load(f) or {}
            fresh_detection = fresh.get('detection', {})
            self.config.setdefault('detection', {}).update(fresh_detection)
            self.logger.info("Reloaded detection config from config.yaml for forced scan restart")
            return True
        except Exception as e:
            self.logger.warning(f"Failed to reload detection config from config.yaml: {e}")
            return False


    def get_runtime_config(self) -> dict:
        """Return current runtime configuration."""
        det_cfg = self.config.get('detection', {})
        rcv_cfg = self.config.get('receivers', {})
        log_cfg = self.config.get('logging', {})
        
        return {
            'debug_mode': bool(log_cfg.get('debug_mode', False)),
            'debug_level': str(log_cfg.get('debug_level', 'basic')),
            'snr_threshold': float(det_cfg.get('detection_threshold', 18.0)),
            'scan_interval': int(rcv_cfg.get('scan_interval', 15)),
            'fixed_channel_scantime': getattr(self, '_fixed_channel_scantime', 
                                             int(det_cfg.get('fixed_channel_scantime', 60))),
        }
