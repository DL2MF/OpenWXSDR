"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : audio_pipeline.py
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
#  RTL-SDR audio pipeline module for OpenWX.
#
#  Implements two pipeline classes:
#
#  AudioPipeline
#    rtl_fm (raw IQ, -M raw) → 48 kHz int16 IQ → rs1729 decoder stdin.
#    Single-channel pipeline per RTL-SDR device. Monitors IQ stream for
#    rolling RSSI/SNR estimation via SignalMetrics. No sox required.
#
#  MultiChannelAudioPipeline
#    Manages a pool of AudioPipeline instances across multiple RTL-SDR
#    devices. Auto-selects the least-loaded device for each new channel.
#    Tracks per-device capacity and cleans up dead pipelines automatically.
#
#  Decoder backend : rs1729 (RS41, DFM09, M10, iMet-C, ...)
#  Hardware        : RTL-SDR (any device supported by rtl_fm)
#
# =============================================================================
"""

import subprocess
import logging
import threading
import time
import os
try:
    import fcntl  # Linux only; used to enlarge pipe buffers (F_SETPIPE_SZ)
except ImportError:  # pragma: no cover - non-Linux
    fcntl = None
from typing import Optional

import numpy as np

from .signal_metrics import SignalMetrics


class AudioPipeline:
    """
    Manages rtl_fm in raw IQ mode
    rtl_fm (raw IQ, 48k) → decoder stdin
    """

    # dBFS→dBm mapping constant used with gain compensation.
    RSSI_CALIBRATION_DB = -14.0
    AUTO_GAIN_ESTIMATE_DB = 35.0
    
    def __init__(self, frequency: float, sample_rate: int = 48000, device_serial: str = "0", gain: int = 0, ppm_correction: int = 0,
                 enable_metrics: bool = False):
        """
        Initialize audio pipeline

        Args:
            frequency: Center frequency in Hz
            sample_rate: Output sample rate (default 48000 Hz for RS41)
            device_serial: RTL-SDR device serial number (or index as string, e.g., "0", "1")
            gain: Tuner gain (0 = auto, or specific value 0-50)
            ppm_correction: PPM frequency correction (default 0)
            enable_metrics: If True, insert a Python pump thread between rtl_fm
                and the decoder to compute live per-frame RSSI/SNR from the IQ
                stream. If False (default), the decoder reads rtl_fm's stdout
                DIRECTLY (V1.0.50 topology) — no Python thread in the signal path.
                The pump was the prime suspect for the V1.0.52→60 RS41 frame-yield
                regression: under CPU load it stalls, rtl_fm's stdout buffer
                fills, librtlsdr silently drops samples, and the decoder loses
                bit sync. v1.0.62 mitigates BOTH failure modes: the metric
                recompute is throttled (~10 Hz, see SignalMetrics.min_interval_s)
                and both pipe buffers are enlarged (F_SETPIPE_SZ) so a brief
                stall is absorbed by the kernel instead of dropping samples.
        """
        self.frequency = frequency
        self.sample_rate = sample_rate
        self.device_serial = device_serial
        self.gain = gain
        self.ppm_correction = ppm_correction
        self.enable_metrics = enable_metrics
        self.logger = logging.getLogger(f'AudioPipeline.{frequency/1e6:.3f}')
        
        self.rtl_process: Optional[subprocess.Popen] = None
        self._decoder_stream = None
        self._pump_thread: Optional[threading.Thread] = None
        self._pipe_read_fd: Optional[int] = None
        self._pipe_write_fd: Optional[int] = None
        effective_gain_db = float(self.gain) if float(self.gain) > 0.0 else self.AUTO_GAIN_ESTIMATE_DB
        self.metrics = SignalMetrics(
            gain_db=effective_gain_db,
            calibration_db=self.RSSI_CALIBRATION_DB,
        )
        self.running = False
    
    def start(self) -> bool:
        """
        Start rtl_fm in raw IQ mode
        Returns True if successful
        """
        try:
            # Start rtl_fm in raw IQ mode (verified working by user)
            # -d: Device selection (serial number or index)
            # -p: PPM correction
            # -M raw: Raw IQ mode (no demodulation)
            # -s 48k: 48 kHz sample rate (required by RS41 decoder)
            # -f: frequency in MHz
            # -g: Tuner gain (0 = auto, or specific value)
            # -E dc: DC blocking filter
            # Output: signed 16-bit IQ samples to stdout
            rtl_cmd = [
                'rtl_fm',
                '-d', str(self.device_serial),  # Device selection by serial or index
                '-p', str(self.ppm_correction),
                '-M', 'raw',
                '-s', f'{self.sample_rate//1000}k',
                '-f', f'{self.frequency/1e6:.2f}M',
            ]
            # CRITICAL: gain 0 means AUTO (per config docs: "gain: 0 = auto"),
            # but rtl_fm treats '-g 0' as MANUAL 0 dB — near-minimum gain, which
            # makes the decoder deaf and produces zero frames even on a strong
            # 30 dB+ signal (field: RTL00001/RTL00003 configured gain 0 decoded
            # nothing). rtl_fm only uses its automatic gain when -g is OMITTED
            # entirely. So pass -g only for a real manual gain; drop it for auto.
            try:
                gain_val = float(self.gain)
            except (TypeError, ValueError):
                gain_val = 0.0
            if gain_val > 0.0:
                rtl_cmd += ['-g', str(self.gain)]
            else:
                self.logger.info("Tuner gain 0 → using rtl_fm automatic gain (omitting -g)")
            rtl_cmd += ['-E', 'dc', '-']
            
            self.logger.info(f"Starting rtl_fm: {' '.join(rtl_cmd)}")
            
            # Start rtl_fm process
            self.rtl_process = subprocess.Popen(
                rtl_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Wait briefly to check if startup succeeded
            time.sleep(0.5)
            if self.rtl_process.poll() is not None:
                # Process exited immediately - startup failed
                rtl_code = self.rtl_process.poll()
                rtl_err = self.rtl_process.stderr.read().decode('utf-8', errors='ignore')[:200] if self.rtl_process.stderr else ""
                
                self.logger.error(f"Pipeline failed to start - rtl_fm exit: {rtl_code}")
                if rtl_err:
                    self.logger.error(f"rtl_fm error: {rtl_err}")
                
                self._cleanup()
                return False
            
            self.running = True

            if self.enable_metrics:
                # Metrics mode: monitored pipe + IQ pump thread (adds a Python
                # thread to the signal path — see enable_metrics docstring)
                self._pipe_read_fd, self._pipe_write_fd = os.pipe()
                self._decoder_stream = open(self._pipe_read_fd, 'rb', closefd=True)
                self._pipe_read_fd = None

                # Enlarge BOTH pipe buffers so a brief Python-thread stall is
                # absorbed by the kernel instead of backing up rtl_fm (whose full
                # stdout buffer makes librtlsdr silently drop samples — the old
                # frame-yield regression). At 48 kHz IQ (~192 KB/s) a few MB is
                # many seconds of slack, so the pump can lag without dropping.
                self._enlarge_pipe(self.rtl_process.stdout.fileno())
                self._enlarge_pipe(self._pipe_write_fd)

                self._pump_thread = threading.Thread(
                    target=self._pump_rtl_iq_to_decoder,
                    daemon=True,
                    name=f"AudioPump.{self.frequency/1e6:.3f}",
                )
                self._pump_thread.start()
            else:
                # Direct piping (V1.0.50 topology): decoder reads rtl_fm stdout
                # with zero Python involvement in the signal path
                self._decoder_stream = self.rtl_process.stdout

            # Monitor stderr
            self.rtl_stderr_thread = threading.Thread(
                target=self._monitor_stderr,
                args=(self.rtl_process.stderr, 'rtl_fm'),
                daemon=True
            )
            self.rtl_stderr_thread.start()

            self.logger.info(
                f"Audio pipeline started: rtl_fm raw IQ at {self.sample_rate} Hz "
                f"({'metrics pump' if self.enable_metrics else 'direct pipe'})"
            )
            return True
            
        except FileNotFoundError as e:
            self.logger.error(f"Required tool not found! Install rtl-sdr package. Error: {e}")
            self._cleanup()
            return False
        except Exception as e:
            self.logger.error(f"Failed to start audio pipeline: {e}")
            self._cleanup()
            return False
    
    def _cleanup(self):
        """Clean up processes"""
        self.running = False

        if self._decoder_stream is not None:
            try:
                self._decoder_stream.close()
            except Exception:
                pass
            self._decoder_stream = None

        if self._pipe_write_fd is not None:
            try:
                os.close(self._pipe_write_fd)
            except Exception:
                pass
            self._pipe_write_fd = None

        if self._pipe_read_fd is not None:
            try:
                os.close(self._pipe_read_fd)
            except Exception:
                pass
            self._pipe_read_fd = None

        if self._pump_thread and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=1.0)
        self._pump_thread = None

        if self.rtl_process:
            # CRITICAL: always confirm the process has actually been reaped
            # before discarding our reference. Previously, if rtl_fm didn't
            # exit within 2s of SIGTERM, we sent SIGKILL but never waited for
            # it to take effect — stop() could return and log "stopped" while
            # the kernel hadn't yet released rtl_fm's USB interface claim,
            # causing the *next* device open to fail with
            # "usb_claim_interface error -6" / LIBUSB_ERROR_BUSY.
            try:
                self.rtl_process.terminate()
                self.rtl_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    f"rtl_fm (PID {self.rtl_process.pid}) did not exit within 2s "
                    "of SIGTERM, sending SIGKILL"
                )
                self.rtl_process.kill()
                try:
                    self.rtl_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.logger.error(
                        f"rtl_fm (PID {self.rtl_process.pid}) still not reaped "
                        "after SIGKILL — USB interface may remain busy"
                    )
            except Exception as e:
                self.logger.debug(f"Error terminating rtl_fm: {e}")
            self.rtl_process = None
    
    def stop(self):
        """Stop rtl_fm"""
        self.running = False
        self._cleanup()
        self.logger.info("Audio pipeline stopped")
    
    def get_audio_stream(self):
        """
        Get IQ stream (stdout of rtl_fm)
        Returns file object that can be piped to decoder stdin
        """
        if self.rtl_process and self.running and self._decoder_stream is not None:
            return self._decoder_stream
        return None

    def get_signal_metrics_snapshot(self):
        """Return latest rolling (rssi_dbfs, snr_db) from live decoder IQ stream.
        Returns (None, None) in direct-pipe mode — callers already fall back
        to the scan-time signal strength."""
        if not self.enable_metrics:
            return (None, None)
        return self.metrics.snapshot()
    
    def is_alive(self) -> bool:
        """Check if rtl_fm is still running"""
        if not self.rtl_process:
            return False
        
        rtl_code = self.rtl_process.poll()
        
        if rtl_code is not None:
            if rtl_code != 0:
                self.logger.warning(f"rtl_fm process exited with code {rtl_code}")
            return False
        
        return True
    
    def _monitor_stderr(self, pipe, process_name: str):
        """Monitor process stderr for errors/warnings"""
        if not pipe:
            return
        
        try:
            for line in pipe:
                if not self.running:
                    break
                try:
                    line = line.decode('utf-8', errors='ignore').strip()
                    if line:
                        # Log errors and important messages
                        if 'Error' in line or 'Failed' in line or ' error' in line:
                            self.logger.warning(f"{process_name}: {line}")
                        elif 'Found' in line or 'Using' in line or 'Tuned' in line:
                            self.logger.debug(f"{process_name}: {line}")
                        else:
                            self.logger.debug(f"{process_name}: {line}")
                except Exception as e:
                    self.logger.debug(f"Error reading {process_name} stderr: {e}")
        except Exception as e:
            self.logger.error(f"Error in {process_name} stderr monitor: {e}")

    def _enlarge_pipe(self, fd: int, size: int = 4 * 1024 * 1024) -> None:
        """Best-effort enlarge a pipe's kernel buffer (Linux F_SETPIPE_SZ) so the
        metrics pump can stall briefly without rtl_fm backing up. The kernel
        clamps to /proc/sys/fs/pipe-max-size; any failure is harmless (we just
        keep the default buffer)."""
        if fcntl is None or not hasattr(fcntl, 'F_SETPIPE_SZ'):
            return
        try:
            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, int(size))
        except (OSError, ValueError):
            # Retry once at a smaller size in case the requested size exceeded
            # the system max.
            try:
                fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, 1024 * 1024)
            except (OSError, ValueError):
                pass

    def _pump_rtl_iq_to_decoder(self):
        """Forward rtl_fm IQ bytes to decoder pipe while computing rolling metrics."""
        if not self.rtl_process or not self.rtl_process.stdout:
            return

        source = self.rtl_process.stdout
        chunk_bytes = 16384  # Multiple of 4 bytes (int16 I/Q)

        try:
            while self.running:
                data = source.read(chunk_bytes)
                if not data:
                    break

                # Decode IQ chunk for metric estimation.
                sample_bytes = data[:len(data) - (len(data) % 4)]
                if sample_bytes:
                    try:
                        raw = np.frombuffer(sample_bytes, dtype=np.int16).reshape(-1, 2)
                        i = raw[:, 0].astype(np.float32) / np.float32(32768.0)
                        q = raw[:, 1].astype(np.float32) / np.float32(32768.0)
                        self.metrics.update_iq(i, q)
                    except Exception:
                        pass

                if self._pipe_write_fd is None:
                    break
                try:
                    os.write(self._pipe_write_fd, data)
                except OSError:
                    break
        except Exception as exc:
            if self.running:
                self.logger.debug(f"IQ pump stopped: {exc}")
        finally:
            if self._pipe_write_fd is not None:
                try:
                    os.close(self._pipe_write_fd)
                except Exception:
                    pass
                self._pipe_write_fd = None


class MultiChannelAudioPipeline:
    """
    Manages multiple audio pipelines for concurrent decoding
    Uses rtl_fm in raw IQ mode
    Supports multiple RTL-SDR devices
    """
    
    def __init__(self, max_channels: int = 4, sample_rate: int = 48000, device_configs: list = None):
        """
        Initialize multi-channel audio pipeline manager
        
        Args:
            max_channels: Maximum channels per device
            sample_rate: Audio sample rate (48000 Hz for RS41)
            device_configs: List of device configurations from config.yaml
                           Each config should have: serial, gain, ppm_error
        """
        self.max_channels = max_channels
        self.sample_rate = sample_rate
        self.device_configs = device_configs or []
        self.logger = logging.getLogger('MultiChannelAudio')
        self.pipelines = {}  # frequency -> AudioPipeline
        self.device_usage = {}  # device_serial -> list of frequencies
        
        # Initialize device usage tracking
        for device_config in self.device_configs:
            device_serial = device_config.get('serial', '0')
            self.device_usage[device_serial] = []
        
        if self.device_configs:
            self.logger.info(f"Initialized with {len(self.device_configs)} RTL-SDR device(s): "
                           f"{[d.get('serial', '0') for d in self.device_configs]}")
        else:
            self.logger.warning("No device configs provided, using default device 0")
    
    def create_pipeline(self, frequency: float, channel_id: int, device_serial: str = None, avoid_device: str = None) -> Optional[AudioPipeline]:
        """
        Create audio pipeline for specific frequency
        
        Args:
            frequency: Frequency in Hz
            channel_id: Channel identifier (for logging)
            device_serial: Specific device serial to use (None = auto-select least loaded)
            avoid_device: Device to avoid when auto-selecting (e.g., spectrum analyzer device)
            
        Returns:
            AudioPipeline instance or None if all devices at capacity
        """
        # Select device if not specified
        if device_serial is None:
            device_serial = self._select_device(avoid_device=avoid_device)
            if device_serial is None:
                self.logger.error("All RTL-SDR devices at maximum capacity")
                return None
        
        # Get device config
        device_config = self._get_device_config(device_serial)
        if device_config is None:
            self.logger.error(f"Device {device_serial} not found in configuration")
            return None
        
        # Check if this device has capacity
        if len(self.device_usage.get(device_serial, [])) >= self.max_channels:
            self.logger.warning(f"Device {device_serial} at maximum capacity ({self.max_channels} channels)")
            return None
        
        # Create pipeline
        pipeline = AudioPipeline(
            frequency=frequency,
            sample_rate=self.sample_rate,  # 48 kHz for RS41
            device_serial=device_serial,
            gain=device_config.get('gain', 0),
            ppm_correction=device_config.get('ppm_error', 0)
        )
        
        if pipeline.start():
            self.pipelines[frequency] = pipeline
            self.device_usage[device_serial].append(frequency)
            total_pipelines = sum(len(freqs) for freqs in self.device_usage.values())
            self.logger.info(f"Created pipeline for {frequency/1e6:.4f} MHz on device {device_serial} "
                           f"({len(self.device_usage[device_serial])}/{self.max_channels} channels, "
                           f"{total_pipelines} total pipelines)")
            return pipeline
        return None
    
    def _select_device(self, avoid_device: str = None) -> Optional[str]:
        """
        Select least loaded device
        
        Args:
            avoid_device: Device serial to avoid (e.g., when spectrum analyzer is using it)
        
        Returns:
            Device serial or None if all at capacity
        """
        if not self.device_configs:
            return "0"  # Default device
        
        # Find device with fewest active channels (excluding avoided device)
        min_load = self.max_channels + 1
        selected_device = None
        
        for device_config in self.device_configs:
            device_serial = device_config.get('serial', '0')
            
            # Skip avoided device
            if avoid_device and device_serial == avoid_device:
                continue
            
            current_load = len(self.device_usage.get(device_serial, []))
            if current_load < min_load:
                min_load = current_load
                selected_device = device_serial
        
        if min_load >= self.max_channels:
            return None  # All devices at capacity
        
        return selected_device
    
    def _get_device_config(self, device_serial: str) -> Optional[dict]:
        """Get configuration for specific device"""
        if not self.device_configs:
            return {'serial': '0', 'gain': 0, 'ppm_error': 0}
        
        for config in self.device_configs:
            if config.get('serial', '0') == device_serial:
                return config
        return None
    
    def remove_pipeline(self, frequency: float):
        """Stop and remove pipeline for frequency"""
        if frequency in self.pipelines:
            pipeline = self.pipelines[frequency]
            device_serial = pipeline.device_serial
            
            pipeline.stop()
            del self.pipelines[frequency]
            
            # Update device usage tracking
            if device_serial in self.device_usage and frequency in self.device_usage[device_serial]:
                self.device_usage[device_serial].remove(frequency)
            
            self.logger.info(f"Removed pipeline for {frequency/1e6:.4f} MHz from device {device_serial}")
    
    def get_pipeline(self, frequency: float) -> Optional[AudioPipeline]:
        """Get existing pipeline for frequency"""
        return self.pipelines.get(frequency)
    
    def cleanup_dead_pipelines(self):
        """Remove pipelines that have died"""
        dead = []
        for freq, pipeline in self.pipelines.items():
            if not pipeline.is_alive():
                dead.append(freq)
        
        for freq in dead:
            self.logger.warning(f"Pipeline for {freq/1e6:.4f} MHz died - cleaning up")
            self.remove_pipeline(freq)
        
        if dead:
            self.logger.info(f"Cleaned up {len(dead)} dead pipeline(s), {len(self.pipelines)}/{self.max_channels} channels now in use")
    
    def stop_all(self):
        """Stop all audio pipelines"""
        for frequency in list(self.pipelines.keys()):
            self.remove_pipeline(frequency)
