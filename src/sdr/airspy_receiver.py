"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : airspy_receiver.py
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
#  Airspy Mini / Airspy R2 receiver module for OpenWX.
#
#  Implements two receive pipelines:
#
#  AirspyPipeline  (legacy)
#    airspy_rx → sox resampling → 48 kHz int16 IQ → rs1729 decoder
#    Single-channel, backward-compatible pipeline using sox for
#    sample-rate conversion.
#
#  AirspyChannelizer  (recommended)
#    airspy_rx → Python DDC + polyphase decimation → N decoder channels
#    One airspy_rx instance feeds up to N simultaneous rs1729 decoders
#    via per-sonde phase-continuous digital down-conversion (DDC) and
#    scipy polyphase resampling to 48 kHz int16 IQ. No sox required.
#
#  AirspyReceiver  (high-level controller)
#    Scan → detect → decode state machine wrapping both pipelines.
#    Supports fixed channels, RX-scan mode, manual decoder control,
#    runtime configuration and spectrum/status API endpoints for the
#    OpenWX web interface.
#
#   Spectrum scan:
#    airspy_rx (time-limited capture) → numpy averaged FFT → DetectedSignal list
#
#  Supported hardware : Airspy Mini (3 / 6 MSPS)
#                       Airspy R2   (2.5 / 10 MSPS)
#  Decoder backend    : rs1729 (RS41, DFM09, M10, iMet-C, ...)
#
# =============================================================================
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from math import gcd
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy import signal as scipy_signal

from .rtlsdr_analyzer import DetectedSignal
from .signal_metrics import SignalMetrics
from ..decoders.rs1729_decoder import RS1729Decoder
from ..decoders.models import (
    SondeTelemetry, SondePosition, SondeVelocity, SondeEnvironment
)


# ---------------------------------------------------------------------------
# AirspyPipeline — airspy_rx | sox subprocess chain
# ---------------------------------------------------------------------------

class AirspyPipeline:
    """
    Manages airspy_rx → sox resampling pipeline.
    Produces 16-bit signed IQ at 48 kHz for RS1729 decoder input.

    airspy_rx v1.0.x outputs signed 16-bit integer (INT16_IQ) samples.
    sox downsamples and keeps int16 format; rs1729 decoders use --IQ 0.0
    to handle FM demodulation themselves.
    """

    # Valid sample rates: Airspy Mini = 3_000_000 or 6_000_000
    #                     Airspy R2   = 2_500_000 or 10_000_000
    # Gain limits: LNA 0-14, Mixer 0-15, VGA 0-15
    LNA_GAIN_MAX   = 14
    MIXER_GAIN_MAX = 15
    VGA_GAIN_MAX   = 15
    RSSI_CALIBRATION_DB = -14.0

    def __init__(self, frequency: float, sample_rate: int = 3_000_000,
                 serial: str = '',
                 lna_gain: int = 8, mixer_gain: int = 8, vga_gain: int = 8,
                 ppm_correction: int = 0):
        self.frequency      = frequency
        self.sample_rate    = sample_rate
        self.serial         = serial
        # Clamp gains to hardware limits
        self.lna_gain       = min(max(lna_gain, 0), self.LNA_GAIN_MAX)
        self.mixer_gain     = min(max(mixer_gain, 0), self.MIXER_GAIN_MAX)
        self.vga_gain       = min(max(vga_gain, 0), self.VGA_GAIN_MAX)
        self.ppm_correction = ppm_correction
        self.logger         = logging.getLogger(f'AirspyPipeline.{frequency/1e6:.3f}')

        self.airspy_proc: Optional[subprocess.Popen] = None
        self.sox_proc:    Optional[subprocess.Popen] = None
        self._decoder_stream = None
        self._pipe_read_fd: Optional[int] = None
        self._pipe_write_fd: Optional[int] = None
        self._pump_thread: Optional[threading.Thread] = None
        total_gain_db = float(self.lna_gain + self.mixer_gain + self.vga_gain)
        self.metrics = SignalMetrics(
            gain_db=total_gain_db,
            calibration_db=self.RSSI_CALIBRATION_DB,
        )
        self.running = False

    def start(self) -> bool:
        """Start airspy_rx and sox subprocess chain."""
        try:
            # Apply PPM correction to the frequency
            adj_freq = self.frequency * (1.0 + self.ppm_correction / 1e6)
            freq_mhz = adj_freq / 1e6

            airspy_cmd = [
                'airspy_rx',
                '-f', f'{freq_mhz:.6f}',
                '-a', str(self.sample_rate),
                '-r', '-',              # raw int16 IQ to stdout (airspy_rx v1.0.x default)
                '-l', str(self.lna_gain),
                '-m', str(self.mixer_gain),
                '-v', str(self.vga_gain),
            ]
            # Select specific device by serial number if configured
            if self.serial:
                airspy_cmd += ['-p', str(self.serial)]

            self.logger.info(f"Starting: {' '.join(airspy_cmd)}")
            self.airspy_proc = subprocess.Popen(
                airspy_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            # sox: int16 IQ at sample_rate → int16 IQ at 48 kHz
            # airspy_rx v1.0.x outputs signed 16-bit integers (INT16_IQ).
            # rs1729 decoders use --IQ 0.0 so they handle FM demodulation.
            sox_cmd = [
                'sox',
                '-t', 'raw', '-e', 'signed-integer', '-b', '16',
                '-r', str(self.sample_rate), '-c', '2',
                '-',        # stdin from airspy_rx
                '-t', 'raw', '-e', 'signed-integer', '-b', '16',
                '-r', '48000', '-c', '2',
                '-'         # stdout to decoder
            ]
            self.logger.info(f"Starting: {' '.join(sox_cmd)}")
            self.sox_proc = subprocess.Popen(
                sox_cmd,
                stdin=self.airspy_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            # Close parent's copy of airspy stdout so sox detects EOF when airspy dies
            self.airspy_proc.stdout.close()

            # Give processes a moment to start
            time.sleep(0.8)

            if self.airspy_proc.poll() is not None:
                err = self.airspy_proc.stderr.read(500).decode('utf-8', errors='ignore')
                self.logger.error(f"airspy_rx exited immediately: {err}")
                self._cleanup()
                return False

            if self.sox_proc.poll() is not None:
                err = self.sox_proc.stderr.read(500).decode('utf-8', errors='ignore')
                self.logger.error(f"sox exited immediately: {err}")
                self._cleanup()
                return False

            self.running = True

            # Create monitored decoder stream for per-frame RSSI/SNR.
            self._pipe_read_fd, self._pipe_write_fd = os.pipe()
            self._decoder_stream = open(self._pipe_read_fd, 'rb', closefd=True)
            self._pipe_read_fd = None

            self._pump_thread = threading.Thread(
                target=self._pump_sox_iq_to_decoder,
                daemon=True,
                name=f"AirspyLegacyPump.{self.frequency/1e6:.3f}",
            )
            self._pump_thread.start()

            # Drain stderr in background to avoid pipe buffer stalls
            for proc, name in [(self.airspy_proc, 'airspy_rx'), (self.sox_proc, 'sox')]:
                threading.Thread(
                    target=self._drain_stderr, args=(proc.stderr, name), daemon=True
                ).start()

            return True

        except FileNotFoundError as exc:
            self.logger.error(
                f"Tool not found: {exc}. "
                "Install with: sudo apt-get install -y airspy sox"
            )
            return False
        except Exception as exc:
            self.logger.error(f"Pipeline start failed: {exc}", exc_info=True)
            self._cleanup()
            return False

    def get_audio_stream(self):
        """Return sox stdout — 48 kHz int16 IQ stream for decoder input."""
        return self._decoder_stream

    def get_signal_metrics_snapshot(self):
        return self.metrics.snapshot()

    def stop(self):
        self._cleanup()

    def is_alive(self) -> bool:
        return (
            self.running and
            self.airspy_proc is not None and
            self.airspy_proc.poll() is None and
            self.sox_proc is not None and
            self.sox_proc.poll() is None
        )

    def _cleanup(self):
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

        for proc in (self.sox_proc, self.airspy_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self.sox_proc = None
        self.airspy_proc = None

    @staticmethod
    def _drain_stderr(stream, name: str):
        logger = logging.getLogger(f'AirspyPipeline.{name}')
        try:
            for line in stream:
                msg = line.rstrip(b'\n').decode('utf-8', errors='ignore')
                if msg:
                    logger.debug(f"[{name}] {msg}")
        except Exception:
            pass

    def _pump_sox_iq_to_decoder(self):
        """Forward sox IQ stream to decoder pipe while updating rolling metrics."""
        if self.sox_proc is None or self.sox_proc.stdout is None:
            return

        source = self.sox_proc.stdout
        chunk_bytes = 16384  # Multiple of 4 bytes (int16 I/Q)

        try:
            while self.running:
                data = source.read(chunk_bytes)
                if not data:
                    break

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
                self.logger.debug(f"Legacy IQ pump stopped: {exc}")
        finally:
            if self._pipe_write_fd is not None:
                try:
                    os.close(self._pipe_write_fd)
                except Exception:
                    pass
                self._pipe_write_fd = None


# ---------------------------------------------------------------------------
# _ChannelizerChannel / AirspyChannelizer — Python DDC multi-channel pipeline
# ---------------------------------------------------------------------------

@dataclass
class _ChannelizerChannel:
    """Per-sonde state stored in AirspyChannelizer._channels."""
    write_fd:  int    # write end of os.pipe() — channelizer writes 48 kHz int16 IQ
    offset:    float  # Hz, sonde frequency relative to Airspy center (after PPM)
    phase_acc: float  # radians, maintained across chunks for phase-continuous DDC
    metrics:   SignalMetrics = field(default_factory=SignalMetrics)
    queue:     object = None  # threading.Queue for output chunks (set in __post_init__)
    # Streaming (overlap-save) resampler state — see _reader_loop. Without this,
    # resample_poly ran per chunk from a zero filter state, re-introducing a large
    # anti-alias transient every chunk that corrupted the FSK (decoder never
    # locked). res_prev = previous chunk's DDC baseband (emitted one chunk late so
    # both the warm-up history AND a lookahead are real samples); res_hist = the
    # warm-up samples immediately preceding res_prev.
    res_prev_r: object = None
    res_prev_i: object = None
    res_hist_r: object = None
    res_hist_i: object = None

    def __post_init__(self):
        import queue as _queue
        self.queue = _queue.Queue(maxsize=8)  # ~8 × 20 ms = 160 ms buffering


class AirspyChannelizer:
    """
    Single airspy_rx → Python DDC + polyphase decimation → N decoder channels.

    One airspy_rx captures at center_freq / sample_rate.  For each registered
    sonde frequency F the reader thread applies:
      1. Phase-continuous DDC: mix by exp(-j·2π·(F−Fc)/Fs) → baseband IQ
      2. scipy.signal.resample_poly (polyphase): sample_rate → 48 kHz
      3. Write int16 stereo IQ to that channel's os.pipe()

    The RS1729Decoder reads from the pipe's read end as its stdin.
    """

    # Chunk size: multiple of (sample_rate / 48000) to get exact integer output count.
    # 6 MSPS → 125:1 decimation → must be a multiple of 125.
    # 25,000 int16 IQ samples ≈ 4.2 ms per chunk → ~1000 output samples (4 kB on pipe).
    CHUNK_SAMPLES = 25_000
    RSSI_CALIBRATION_DB = -14.0

    def __init__(self, center_freq: int, sample_rate: int = 6_000_000,
                 lna_gain: int = 14, mixer_gain: int = 14, vga_gain: int = 14,
                 serial: str = '', ppm_correction: int = 0,
                 airspy_qi_order: bool = False, debug_iq_dump: bool = False):
        self.debug_iq_dump  = bool(debug_iq_dump)
        self.center_freq    = center_freq
        self.sample_rate    = sample_rate
        self.lna_gain       = min(max(lna_gain,   0), AirspyPipeline.LNA_GAIN_MAX)
        self.mixer_gain     = min(max(mixer_gain, 0), AirspyPipeline.MIXER_GAIN_MAX)
        self.vga_gain       = min(max(vga_gain,   0), AirspyPipeline.VGA_GAIN_MAX)
        self.serial         = serial
        self.ppm_correction = ppm_correction
        self.airspy_qi_order = bool(airspy_qi_order)
        self.logger         = logging.getLogger('AirspyChannelizer')

        # Rational resampling factors: sample_rate / 48000 Hz
        # 6 MSPS → 1:125  (exact integer, most efficient)
        # 3 MSPS → 2:125  (rational)
        # 2.5 MSPS → 48:2500 → simplified via gcd
        _g             = gcd(48_000, sample_rate)
        self._res_up   = 48_000 // _g
        self._res_down = sample_rate // _g

        # Overlap-save history length (input samples) for the streaming resampler.
        # resample_poly's internal Kaiser filter is 2*10*max(up,down)+1 taps at the
        # up-sampled rate; its one-sided reach in INPUT samples is that/(2*up). We
        # round UP to a whole multiple of res_down so every resampled block has an
        # exact integer output length (no sample-rate drift over time).
        _filt_len   = 2 * 10 * max(self._res_up, self._res_down) + 1
        _one_sided  = _filt_len // (2 * self._res_up) + 1
        self._res_hist_len = ((_one_sided + self._res_down - 1)
                              // self._res_down) * self._res_down

        self._channels:  Dict[float, _ChannelizerChannel] = {}
        self._ch_lock    = threading.Lock()
        self._proc:      Optional[subprocess.Popen] = None
        self._reader_th: Optional[threading.Thread] = None
        self._running    = False
        self._drops      = 0   # count of dropped output blocks (queue full)

    def start(self) -> bool:
        """Launch airspy_rx at center_freq and start the reader/channelizer thread."""
        adj_cf = self.center_freq * (1.0 + self.ppm_correction / 1e6)
        cmd = [
            'airspy_rx',
            '-f', f'{adj_cf / 1e6:.6f}',
            '-a', str(self.sample_rate),
            '-r', '-',
            '-l', str(self.lna_gain),
            '-m', str(self.mixer_gain),
            '-v', str(self.vga_gain),
        ]
        if self.serial:
            cmd += ['-p', str(self.serial)]
        self.logger.info(f"Starting: {' '.join(cmd)}")
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except FileNotFoundError:
            self.logger.error("airspy_rx not found")
            return False

        time.sleep(0.8)
        if self._proc.poll() is not None:
            err = self._proc.stderr.read(300).decode('utf-8', errors='ignore')
            self.logger.error(f"airspy_rx exited immediately: {err}")
            return False

        self._running = True
        self._reader_th = threading.Thread(
            target=self._reader_loop, name='AirspyChannelizer', daemon=True
        )
        self._reader_th.start()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,), daemon=True
        ).start()
        self.logger.info(
            f"AirspyChannelizer on {self.center_freq/1e6:.3f} MHz, "
            f"{self.sample_rate/1e6:.1f} MSPS, "
            f"resample {self._res_down}:{self._res_up}"
        )
        return True

    def add_channel(self, frequency: float):
        """
        Register a sonde at `frequency` Hz.
        Returns a readable binary file object (48 kHz int16 IQ) for an RS1729Decoder.
        """
        adj_cf   = self.center_freq  * (1.0 + self.ppm_correction / 1e6)
        adj_freq = frequency         * (1.0 + self.ppm_correction / 1e6)
        offset   = adj_freq - adj_cf
        r_fd, w_fd = os.pipe()
        total_gain_db = float(self.lna_gain + self.mixer_gain + self.vga_gain)
        ch = _ChannelizerChannel(
            write_fd=w_fd,
            offset=offset,
            phase_acc=0.0,
            metrics=SignalMetrics(
                gain_db=total_gain_db,
                calibration_db=self.RSSI_CALIBRATION_DB,
            ),
        )
        with self._ch_lock:
            self._channels[frequency] = ch
        # Start a dedicated pipe-writer thread so the channelizer loop never blocks
        threading.Thread(
            target=self._pipe_writer, args=(frequency, ch),
            name=f'PipeWriter.{frequency/1e6:.3f}', daemon=True
        ).start()
        self.logger.info(
            f"Channel added: {frequency/1e6:.4f} MHz (offset {offset/1e3:+.1f} kHz)"
        )
        # open(r_fd) returns an io.FileIO; the OS delivers EOF when write_fd is closed
        return open(r_fd, 'rb', closefd=True)

    def remove_channel(self, frequency: float):
        """Signal the writer thread to stop and close the write end of the pipe."""
        with self._ch_lock:
            ch = self._channels.pop(frequency, None)
        if ch:
            # Sentinel None tells the writer thread to exit
            try:
                ch.queue.put_nowait(None)
            except Exception:
                pass
            self.logger.info(f"Channel removed: {frequency/1e6:.4f} MHz")

    def get_channel_metrics_snapshot(self, frequency: float) -> Tuple[Optional[float], Optional[float]]:
        """Return latest (rssi_dbm, snr_db) for a channel frequency."""
        with self._ch_lock:
            ch = self._channels.get(frequency)
            if ch is None:
                return None, None
            return ch.metrics.snapshot()

    def is_alive(self) -> bool:
        return (self._running and
                self._proc is not None and
                self._proc.poll() is None)

    def stop(self):
        self._running = False
        with self._ch_lock:
            freqs = list(self._channels.keys())
        for f in freqs:
            self.remove_channel(f)
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        if self._reader_th:
            self._reader_th.join(timeout=4)
        self.logger.info("AirspyChannelizer stopped")

    # ------------------------------------------------------------------
    # Internal reader / channelizer loop
    # ------------------------------------------------------------------

    @staticmethod
    def _pipe_writer(frequency: float, ch: '_ChannelizerChannel'):
        """Dedicated thread: drains ch.queue and writes to the pipe.

        Diagnostic: enable with sdr.airspy.debug_iq_dump: true (or env
        OPENWXSDR_IQ_DUMP=1) to also tee the EXACT bytes fed to the decoder to
        data/logs/ch_dump_<freq>.raw (in the app folder — no /tmp, so it's
        visible even under systemd PrivateTmp). Test it offline with:
          decoders/rs1729/rs41mod -vv --ptu2 --json --ecc2 --IQ 0.0 \
              data/logs/ch_dump_<freq>.raw 48000 16
        Decodes there => channelizer IQ is good (issue is real-time/pipe). No
        decode => the DDC output itself is the problem.
        """
        import queue as _queue
        logger = logging.getLogger(f'AirspyCh.{frequency/1e6:.3f}')
        dump = None
        if self.debug_iq_dump or os.environ.get('OPENWXSDR_IQ_DUMP'):
            try:
                os.makedirs('data/logs', exist_ok=True)
                dump_path = os.path.abspath(
                    f"data/logs/ch_dump_{frequency/1e6:.3f}.raw")
                dump = open(dump_path, 'wb')
                logger.info(f"IQ dump enabled -> {dump_path} (48 kHz int16 IQ)")
            except OSError as exc:
                logger.warning(f"IQ dump could not open file: {exc}")
                dump = None
        while True:
            try:
                item = ch.queue.get(timeout=2.0)
            except _queue.Empty:
                continue
            if item is None:   # sentinel — time to exit
                break
            try:
                os.write(ch.write_fd, item)
            except OSError:
                break  # decoder closed read-end
            if dump is not None:
                try:
                    dump.write(item)
                except OSError:
                    pass
        try:
            os.close(ch.write_fd)
        except OSError:
            pass
        if dump is not None:
            try:
                dump.close()
            except OSError:
                pass
        logger.debug(f"PipeWriter exited for {frequency/1e6:.4f} MHz")

    def _reader_loop(self):
        chunk_bytes = self.CHUNK_SAMPLES * 4   # int16 I/Q interleaved (order depends on airspy_qi_order)
        inv_32768 = np.float32(1.0 / 32768.0)
        _chunk_count = 0
        _DIAG_CHUNKS = 5   # log IQ stats for the first N chunks

        while self._running:
            # Accumulate a full chunk (may require multiple reads)
            buf = bytearray()
            while len(buf) < chunk_bytes and self._running:
                try:
                    more = self._proc.stdout.read(chunk_bytes - len(buf))
                except Exception:
                    break
                if not more:
                    break
                buf.extend(more)

            if len(buf) < chunk_bytes:
                break   # airspy_rx died or short read

            # Parse int16 IQ -> float32 using configured interleave order.
            raw   = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)
            if self.airspy_qi_order:
                iq_i = raw[:, 0].astype(np.float32) * inv_32768
                iq_r = raw[:, 1].astype(np.float32) * inv_32768
            else:
                iq_r = raw[:, 0].astype(np.float32) * inv_32768
                iq_i = raw[:, 1].astype(np.float32) * inv_32768
            n     = len(iq_r)

            _chunk_count += 1
            if _chunk_count <= _DIAG_CHUNKS:
                self.logger.info(
                    f"IQ sanity [chunk {_chunk_count}]: "
                    f"I min={iq_r.min():.4f} max={iq_r.max():.4f} std={iq_r.std():.4f} | "
                    f"Q min={iq_i.min():.4f} max={iq_i.max():.4f} std={iq_i.std():.4f}"
                )
            elif _chunk_count % 2400 == 0:   # every ~10 s at 240 chunks/s
                self.logger.info(
                    f"Channelizer alive: {_chunk_count} chunks processed "
                    f"({_chunk_count * self.CHUNK_SAMPLES / self.sample_rate:.0f} s)"
                )

            # Snapshot active channels (avoids holding lock during heavy computation).
            # ch is the live object; only this reader thread mutates its DDC/resampler
            # state, so reading/updating it outside the membership lock is safe.
            with self._ch_lock:
                snapshot = {
                    f: (ch.offset, ch.phase_acc, ch.queue, ch)
                    for f, ch in self._channels.items()
                }

            for freq, (offset, phase_acc, ch_queue, ch_obj) in snapshot.items():
                try:
                    # Log phase accumulator at chunk start (first 2 chunks for > 1 MHz offset)
                    if _chunk_count <= _DIAG_CHUNKS and offset > 1000000:
                        self.logger.info(
                            f"Chunk start [{freq/1e6:.4f} MHz, chunk {_chunk_count}]: "
                            f"phase_acc_in={phase_acc:.6f} rad"
                        )

                    # ---- Phase-continuous DDC (float32 phasors, float64 accumulator) ----
                    step   = 2.0 * np.pi * offset / self.sample_rate   # float64 scalar
                    
                    # Diagnostics for problematic channels (large positive offset, chunk 1-2 only)
                    if _chunk_count <= 2 and offset > 1000000:
                        self.logger.info(
                            f"DDC setup [f={freq/1e6:.4f} MHz, chunk {_chunk_count}]: "
                            f"offset={offset/1e3:.1f} kHz, phase_acc={phase_acc:.4f} rad, "
                            f"step={step:.6f} rad, step*n={step*n:.1f} rad (cycles={(step*n)/(2*np.pi):.1f})"
                        )
                    
                    # Keep phase values bounded for trig precision and long-session stability.
                    phases = ((phase_acc + step * np.arange(n)) % (2.0 * np.pi)).astype(np.float32)
                    cos_p  = np.cos(phases)        # float32 NEON-vectorised on ARM; cos/sin are 2π-periodic
                    sin_p  = np.sin(phases)
                    # Wrap the accumulator itself to prevent unbounded growth/drift.
                    new_acc = float((phase_acc + step * n) % (2.0 * np.pi))

                    # (I + jQ) × exp(-jφ) = (I cosφ + Q sinφ) + j(Q cosφ − I sinφ)
                    mixed_r = iq_r * cos_p + iq_i * sin_p
                    mixed_i = iq_i * cos_p - iq_r * sin_p

                    # ---- Streaming (overlap-save) polyphase resampling ----------
                    # resample_poly is stateless, so calling it per chunk restarts
                    # its long anti-alias filter from zero every chunk → a transient
                    # that corrupts the FSK (decoder never locks). Instead we emit
                    # the PREVIOUS chunk resampled inside a buffer that has REAL
                    # samples on both sides — res_hist (warm-up) before it and this
                    # chunk's head as lookahead after it — so both filter edges are
                    # settled. One-chunk (~2.5 ms) latency; sample-rate is drift-free
                    # because history and chunk are whole multiples of res_down.
                    H = self._res_hist_len
                    if ch_obj.res_prev_r is None:
                        # Prime: stash this chunk, emit nothing yet.
                        with self._ch_lock:
                            if freq in self._channels:
                                self._channels[freq].phase_acc = new_acc
                                self._channels[freq].res_prev_r = mixed_r
                                self._channels[freq].res_prev_i = mixed_i
                                self._channels[freq].res_hist_r = np.zeros(H, np.float32)
                                self._channels[freq].res_hist_i = np.zeros(H, np.float32)
                        continue

                    prev_r, prev_i = ch_obj.res_prev_r, ch_obj.res_prev_i
                    hist_r, hist_i = ch_obj.res_hist_r, ch_obj.res_hist_i
                    look = min(H, len(mixed_r))
                    buf_r = np.concatenate((hist_r, prev_r, mixed_r[:look]))
                    buf_i = np.concatenate((hist_i, prev_i, mixed_i[:look]))
                    full_r = scipy_signal.resample_poly(
                        buf_r.astype(np.float64), self._res_up, self._res_down,
                        window=('kaiser', 8.0)).astype(np.float32)
                    full_i = scipy_signal.resample_poly(
                        buf_i.astype(np.float64), self._res_up, self._res_down,
                        window=('kaiser', 8.0)).astype(np.float32)
                    drop_pre = H * self._res_up // self._res_down
                    out_len  = len(prev_r) * self._res_up // self._res_down
                    dec_r = full_r[drop_pre:drop_pre + out_len]
                    dec_i = full_i[drop_pre:drop_pre + out_len]
                    # New warm-up = tail of the chunk we just emitted; carry current.
                    new_hist_r = (prev_r[-H:] if len(prev_r) >= H
                                  else np.concatenate((hist_r, prev_r))[-H:])
                    new_hist_i = (prev_i[-H:] if len(prev_i) >= H
                                  else np.concatenate((hist_i, prev_i))[-H:])

                    # Log DDC output amplitude for first few chunks to verify signal
                    if _chunk_count <= _DIAG_CHUNKS:
                        rms = float(np.sqrt(np.mean(dec_r**2 + dec_i**2)))
                        self.logger.info(
                            f"DDC RMS [{freq/1e6:.4f} MHz, chunk {_chunk_count}]: {rms:.6f}"
                        )

                    # Advance DDC + resampler state.
                    with self._ch_lock:
                        if freq in self._channels:
                            self._channels[freq].phase_acc = new_acc
                            self._channels[freq].res_prev_r = mixed_r
                            self._channels[freq].res_prev_i = mixed_i
                            self._channels[freq].res_hist_r = new_hist_r
                            self._channels[freq].res_hist_i = new_hist_i
                            self._channels[freq].metrics.update_iq(dec_r, dec_i)
                            if _chunk_count <= _DIAG_CHUNKS and offset > 1000000:
                                self.logger.info(
                                    f"Chunk end [{freq/1e6:.4f} MHz, chunk {_chunk_count}]: "
                                    f"phase_acc_out={new_acc:.6f} rad (wrapped, +{step*n:.1f} rad)"
                                )

                    # Convert to int16 stereo IQ (I, Q interleaved)
                    n_out = len(dec_r)
                    out = np.empty(n_out * 2, dtype=np.int16)
                    out[0::2] = np.clip(dec_r * np.float32(32767.0),
                                        -32768, 32767).astype(np.int16)
                    out[1::2] = np.clip(dec_i * np.float32(32767.0),
                                        -32768, 32767).astype(np.int16)

                    # Non-blocking enqueue; drop if queue is full (slow decoder).
                    # Dropped output = a GAP in the decoder's IQ stream, which
                    # breaks FSK sync — log it so a real-time overload is visible
                    # instead of silently producing zero frames.
                    try:
                        ch_queue.put_nowait(out.tobytes())
                    except Exception:
                        self._drops += 1
                        if self._drops == 1 or self._drops % 200 == 0:
                            self.logger.warning(
                                f"Channelizer output queue full — dropped "
                                f"{self._drops} block(s); decoder IQ has gaps "
                                f"(real-time overload?)"
                            )

                except Exception as exc:
                    self.logger.debug(f"Channel {freq/1e6:.4f} MHz DDC error: {exc}")

        self._running = False
        self.logger.info("Channelizer reader loop ended")

    @staticmethod
    def _drain_stderr(stream):
        logger = logging.getLogger('AirspyChannelizer.stderr')
        try:
            for line in stream:
                msg = line.rstrip(b'\n').decode('utf-8', errors='ignore')
                if msg:
                    logger.debug(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# AirspyScanner — short IQ capture + FFT signal detection
# ---------------------------------------------------------------------------

class AirspyScanner:
    """
    Captures IQ data for a fixed duration then detects radiosonde signal peaks
    using an averaged Hann-windowed FFT for better sensitivity.

    Key design choices:
    - Time-based capture (no -n flag) is robust against airspy_rx v1.0.x quirks.
    - Capture duration >= 1.5 s guarantees at least one RS41 burst (530 ms on,
      470 ms off) is covered regardless of phase.
    - Averaging N FFT chunks reduces the noise variance by sqrt(N), giving
      ~7-8 dB extra SNR with 1.5 s of data at 3 MSPS (≈34 chunks at 32768).
    """

    def __init__(self, center_freq: int, sample_rate: int = 3_000_000,
                 lna_gain: int = 8,
                 capture_seconds: float = 1.5,
                 fft_chunk_size: int = 32768,
                 detection_threshold_db: float = 8.0,
                 freq_ranges: list = None,
                 frequency_blacklist: list = None,
                 debug_mode: bool = False,
                 airspy_qi_order: bool = False,
                 min_bw_hz: int = 2_400,
                 birdie_snr_db: float = 12.0,
                 birdie_min_bw_hz: int = 4_500):
        self.center_freq     = center_freq
        self.sample_rate     = sample_rate
        self.lna_gain        = min(max(lna_gain, 0), AirspyPipeline.LNA_GAIN_MAX)
        self.capture_seconds = capture_seconds
        self.fft_chunk_size  = fft_chunk_size
        self.threshold_db    = detection_threshold_db
        self.freq_ranges     = freq_ranges or [(400_000_000, 406_000_000)]
        self.blacklist_hz    = frequency_blacklist or []
        self.debug_mode      = debug_mode
        self.airspy_qi_order = bool(airspy_qi_order)
        # Bandwidth gating (see _detect_signals):
        #   min_bw_hz        — absolute floor; anything narrower is never a sonde.
        #   birdie_snr_db /  — a STRONG-but-NARROW peak is a CW/spur birdie, not a
        #   birdie_min_bw_hz    sonde: a real RS41/DFM at high SNR spreads WIDE
        #                       above the detection floor, a birdie stays a sharp
        #                       spike. Reject when SNR>=birdie_snr_db AND
        #                       BW<birdie_min_bw_hz. Targets exactly the observed
        #                       401.x MHz ghosts (12-17 dB / 2.4-3.1 kHz).
        self.min_bw_hz        = int(min_bw_hz)
        self.birdie_snr_db    = float(birdie_snr_db)
        self.birdie_min_bw_hz = int(birdie_min_bw_hz)
        self.logger          = logging.getLogger('AirspyScanner')
        # Last spectrum snapshot: set by _detect_signals, read by AirspyReceiver
        self.last_spectrum: dict = {}
        # Percent of int16 IQ samples near full-scale; used to detect front-end clipping.
        self.last_clip_ratio_pct: float = 0.0
        # True if the last scan() call couldn't get any usable IQ at all (e.g.
        # airspy_open() failed / device not found) — distinct from a healthy
        # capture that simply found zero signals. AirspyReceiver uses this to
        # tell "device is broken" apart from "band is quiet" and surface a
        # proper error state instead of showing "Scanning" forever.
        self.last_capture_failed: bool = False

    def scan(self) -> List[DetectedSignal]:
        """Capture IQ data and return detected signal peaks."""
        try:
            iq = self._capture_iq(self.capture_seconds)
            if iq is None or len(iq) < self.fft_chunk_size:
                self.logger.warning(
                    f"Capture too short ({len(iq) if iq is not None else 0} samples) "
                    f"for {self.fft_chunk_size}-point FFT — scan skipped"
                )
                self.last_capture_failed = True
                return []
            self.last_capture_failed = False
            return self._detect_signals(iq)
        except Exception as exc:
            self.logger.error(f"Scan failed: {exc}", exc_info=True)
            self.last_capture_failed = True
            return []

    def _capture_iq(self, duration_s: float) -> Optional[np.ndarray]:
        """
        Run airspy_rx (continuous, no -n flag) and drain stdout in a background
        thread while sleeping for duration_s.  A background reader is essential:
        at 3 MSPS the kernel pipe buffer (64 KB) fills in ~5 ms, blocking
        airspy_rx before communicate() is ever called.
        """
        cmd = [
            'airspy_rx',
            '-f', f'{self.center_freq / 1e6:.6f}',
            '-a', str(self.sample_rate),
            '-r', '-',
            '-l', str(self.lna_gain),
        ]
        proc = None
        data_chunks: list = []

        def _drain(pipe):
            """Read stdout in large blocks, accumulate into data_chunks."""
            try:
                while True:
                    chunk = pipe.read(131072)   # 128 KB per read
                    if not chunk:
                        break
                    data_chunks.append(chunk)
            except Exception:
                pass

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            reader = threading.Thread(target=_drain, args=(proc.stdout,), daemon=True)
            reader.start()

            # Let airspy_rx collect samples for the requested duration
            time.sleep(duration_s)
            proc.terminate()
            reader.join(timeout=4)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

            data = b''.join(data_chunks)
            stderr_bytes = b''
            try:
                stderr_bytes = proc.stderr.read(500)
            except Exception:
                pass
            proc = None

            if len(data) < 1024:
                self.logger.warning(
                    f"airspy_rx returned only {len(data)} bytes. "
                    f"stderr: {stderr_bytes[:200].decode('utf-8', errors='ignore')}"
                )
                return None

            raw = np.frombuffer(data, dtype=np.int16)
            if len(raw) > 0:
                clipped = int(np.count_nonzero(np.abs(raw) >= 32000))
                self.last_clip_ratio_pct = 100.0 * clipped / float(len(raw))
                if self.last_clip_ratio_pct >= 0.2:
                    self.logger.info(
                        f"Scan clipping detected: {self.last_clip_ratio_pct:.2f}% "
                        f"of IQ samples near full-scale (gain={self.lna_gain})"
                    )
            # Discard first 100 ms to skip hardware settling transients
            skip = min(int(0.1 * self.sample_rate * 2), len(raw) // 8)
            raw = raw[skip:]
            if len(raw) < 2:
                return None
            raw_f = raw.astype(np.float64) / 32768.0
            n = (len(raw_f) // 2) * 2
            if self.airspy_qi_order:
                # Q,I interleaved
                q = raw_f[:n:2]
                i = raw_f[1:n:2]
            else:
                # I,Q interleaved
                i = raw_f[:n:2]
                q = raw_f[1:n:2]
            iq = i + 1j * q
            self.logger.info(
                f"Captured {len(iq)/self.sample_rate*1e3:.0f} ms "
                f"({len(iq):,} complex samples)"
            ) if self.debug_mode else self.logger.debug(
                f"Captured {len(iq)/self.sample_rate*1e3:.0f} ms "
                f"({len(iq):,} complex samples)"
            )
            return iq

        except FileNotFoundError:
            self.logger.error("airspy_rx not found. Install: sudo apt-get install -y airspy")
            return None
        except Exception as exc:
            self.logger.error(f"IQ capture failed: {exc}", exc_info=True)
            return None
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _detect_signals(self, iq: np.ndarray) -> List[DetectedSignal]:
        """
        Averaged Hann-windowed FFT peak detection.
        Splitting the IQ buffer into chunks and averaging reduces noise variance
        by sqrt(n_chunks), giving several dB of extra sensitivity.
        """
        chunk = self.fft_chunk_size
        n_chunks = len(iq) // chunk
        if n_chunks == 0:
            return []

        window    = np.hanning(chunk)
        avg_power = np.zeros(chunk, dtype=np.float64)
        for k in range(n_chunks):
            block = iq[k * chunk:(k + 1) * chunk]
            fft_block = np.fft.fftshift(np.fft.fft(block * window))
            avg_power += np.abs(fft_block) ** 2
        avg_power /= n_chunks

        freqs = (self.center_freq +
                 np.fft.fftshift(np.fft.fftfreq(chunk, d=1.0 / self.sample_rate)))

        with np.errstate(divide='ignore'):
            power_db = 10.0 * np.log10(avg_power + 1e-30)
        noise_floor = np.percentile(power_db, 20)
        if not np.isfinite(noise_floor):
            return []
        snr = power_db - noise_floor

        peak_snr = float(np.nanmax(snr))
        peak_idx  = int(np.nanargmax(snr))
        peak_freq = float(freqs[peak_idx])
        _log_fn = self.logger.info if self.debug_mode else self.logger.debug
        _log_fn(
            f"Averaged {n_chunks} FFT chunks × {chunk} pts; "
            f"noise_floor={noise_floor:.1f} dB, peak_snr={peak_snr:.1f} dB @ "
            f"{peak_freq/1e6:.4f} MHz, threshold={self.threshold_db:.1f} dB"
        )

        # Downsample spectrum to ~2000 points for efficient web transfer
        _ds = max(1, chunk // 2000)
        self.last_spectrum = {
            'freqs_mhz':   (freqs[::_ds] / 1e6).tolist(),
            'power_db':    power_db[::_ds].tolist(),
            'noise_floor': float(noise_floor),
            'threshold_db': float(self.threshold_db),
            'timestamp':   datetime.utcnow().isoformat() + 'Z',
        }

        signals: List[DetectedSignal] = []
        MIN_SEP  = 50_000        # Hz — minimum separation between distinct peaks
        MIN_BW   = self.min_bw_hz  # Hz — radiosonde minimum: RS41 ≈ 2.4 kHz, DFM ≈ 4 kHz
        DC_GUARD = 100_000       # Hz — exclude LO/DC leakage near center

        n_above_thr = 0
        n_dc_guard  = 0
        n_oor       = 0
        n_narrow    = 0
        n_birdie    = 0

        for i in range(1, len(snr) - 1):
            if np.isnan(snr[i]) or snr[i] < self.threshold_db:
                continue
            if snr[i] < snr[i - 1] or snr[i] < snr[i + 1]:
                continue   # not a local maximum

            n_above_thr += 1
            freq = float(freqs[i])

            # Exclude DC / LO leakage around the center frequency
            if abs(freq - self.center_freq) < DC_GUARD:
                n_dc_guard += 1
                self.logger.debug(
                    f"DC-guard reject: {freq/1e6:.4f} MHz "
                    f"(offset {(freq-self.center_freq)/1e3:.0f} kHz, SNR {snr[i]:.1f} dB)"
                )
                continue
            # Range check
            if not any(lo <= freq <= hi for lo, hi in self.freq_ranges):
                n_oor += 1
                self.logger.debug(
                    f"Out-of-range reject: {freq/1e6:.4f} MHz "
                    f"(ranges={[(lo/1e6,hi/1e6) for lo,hi in self.freq_ranges]})"
                )
                continue
            # Blacklist check (±2.5 kHz tolerance)
            if any(abs(freq - bl) < 2_500 for bl in self.blacklist_hz):
                continue
            # De-duplicate nearby peaks
            if any(abs(freq - s.frequency) < MIN_SEP for s in signals):
                continue

            bw = self._estimate_bandwidth(snr, freqs, i, floor_db=self.threshold_db)

            # Reject signals narrower than a real radiosonde
            if bw < MIN_BW:
                n_narrow += 1
                _log_fn(
                    f"Narrow-BW reject: {freq/1e6:.4f} MHz "
                    f"(SNR {snr[i]:.1f} dB, BW {bw/1e3:.2f} kHz < min {MIN_BW/1e3:.1f} kHz)"
                )
                continue

            # Reject STRONG-but-NARROW peaks as CW/spur birdies. A real sonde at
            # this SNR spreads WIDE above the detection floor; a birdie stays a
            # sharp spike. This surgically drops the observed 401.x MHz ghosts
            # (12-17 dB / 2.4-3.1 kHz) without touching weak/narrow real sondes.
            if snr[i] >= self.birdie_snr_db and bw < self.birdie_min_bw_hz:
                n_birdie += 1
                _log_fn(
                    f"Strong-narrow birdie reject: {freq/1e6:.4f} MHz "
                    f"(SNR {snr[i]:.1f} dB ≥ {self.birdie_snr_db:.0f}, "
                    f"BW {bw/1e3:.2f} kHz < {self.birdie_min_bw_hz/1e3:.1f} kHz)"
                )
                continue

            signals.append(DetectedSignal(
                frequency=freq,
                strength=float(snr[i]),
                bandwidth=bw,
                timestamp=time.time()
            ))

        if n_above_thr > 0 or not signals:
            self.logger.info(
                f"Detection: {n_above_thr} peaks >{self.threshold_db:.0f} dB "
                f"(DC-guard={n_dc_guard}, out-of-range={n_oor}, "
                f"narrow-BW={n_narrow}, birdie={n_birdie}, accepted={len(signals)})"
            )

        return sorted(signals, key=lambda s: s.strength, reverse=True)

    @staticmethod
    def _estimate_bandwidth(snr: np.ndarray, freqs: np.ndarray, peak_idx: int,
                            floor_db: float = 0.0) -> float:
        """Estimate bandwidth: width of peak above the detection floor.

        Using the detection threshold as floor separates real sondes (SNR well
        above threshold → wide above-floor footprint) from ghost/CW signals
        (SNR barely above threshold → near-zero above-floor width).
        """
        left = peak_idx
        right = peak_idx
        while left > 0 and snr[left] > floor_db:
            left -= 1
        while right < len(snr) - 1 and snr[right] > floor_db:
            right += 1
        return float(abs(freqs[right] - freqs[left]))


# ---------------------------------------------------------------------------
# AirspyReceiver — main class, compatible with RTLSDRDeviceManager interface
# ---------------------------------------------------------------------------

class AirspyReceiver:
    """
    Airspy Mini / Airspy 2 receiver with scan → detect → decode state machine.

    Interface is compatible with RTLSDRDeviceManager so web_server.py and
    openwxsdr_app.py work without modification beyond adding the 'airspy' branch.

    Per-device: one receiver manages one connected Airspy dongle.
    """

    STATE_IDLE     = 'idle'
    STATE_SCANNING = 'scanning'
    STATE_DECODING = 'decoding'
    STATE_ERROR    = 'error'  # airspy_rx can't open the device; not actually scanning

    # A handful of retries (~1-2 min at the ~15s scan cadence) before we stop
    # quietly retrying forever and surface a clear error to the web UI —
    # matches RTLSDRDeviceManager's DeviceWorker.MAX_CONSECUTIVE_OPEN_FAILURES
    # in spirit, but WITHOUT the self-restart escalation: unlike RTL-SDR's
    # LIBUSB_ERROR_BUSY (a leaked USB claim only a process exit releases),
    # AIRSPY_ERROR_NOT_FOUND means the device genuinely isn't there/claimable
    # right now — restarting the process wouldn't fix that, so we just keep
    # retrying in the background while showing the true state.
    MAX_CONSECUTIVE_SCAN_FAILURES = 4

    def __init__(self, config: dict,
                 telemetry_callback: Callable[[SondeTelemetry], None]):
        self.config             = config
        self.telemetry_callback = telemetry_callback
        self.logger             = logging.getLogger('AirspyReceiver')
        self.running            = False
        self.lock               = threading.Lock()   # web_server.py compatibility
        self._consecutive_scan_failures = 0

        airspy_cfg = config.get('sdr', {}).get('airspy', {})
        det_cfg    = config.get('detection', {})
        rx_cfg     = config.get('receivers', {})
        dec_cfg    = config.get('decoders', {})
        log_cfg    = config.get('logging', {})

        self._serial        = str(airspy_cfg.get('serial', ''))
        self._center_freq   = int(airspy_cfg.get('center_freq', 404_000_000))
        self._sample_rate   = int(airspy_cfg.get('sample_rate', 2_500_000))
        self._decode_mode   = str(airspy_cfg.get('decode_mode', 'legacy')).strip().lower()
        self._airspy_qi_order = bool(airspy_cfg.get('airspy_qi_order', False))
        self._legacy_snap_hz = int(airspy_cfg.get('legacy_snap_hz', 10_000))
        self._legacy_probe_offsets_hz = [
            int(v) for v in airspy_cfg.get(
                'legacy_probe_offsets_hz',
                [0, 1000, -1000, 2000, -2000, 5000, -5000],
            )
        ]
        self._gain          = int(airspy_cfg.get('gain', 8))
        self._scan_gain     = int(airspy_cfg.get('scan_gain', self._gain))
        self._scan_gain_fallback = int(
            airspy_cfg.get('scan_gain_fallback', max(0, self._scan_gain - 6))
        )
        self._scan_clip_trigger_pct = float(
            airspy_cfg.get('scan_clip_trigger_pct', 0.2)
        )
        # Ghost/birdie rejection thresholds passed to AirspyScanner._detect_signals.
        self._scan_min_bw_hz = int(airspy_cfg.get('scan_min_bw_hz', 2_400))
        self._birdie_snr_db  = float(airspy_cfg.get('birdie_reject_snr_db', 12.0))
        self._birdie_min_bw_hz = int(airspy_cfg.get('birdie_reject_bw_hz', 4_500))
        # Diagnostic: tee channelizer decoder-input IQ to data/logs/ch_dump_*.raw
        self._debug_iq_dump  = bool(airspy_cfg.get('debug_iq_dump', False))
        self._ppm           = int(airspy_cfg.get('ppm_correction', 0))
        self._scan_interval = det_cfg.get('airspy_scan_interval',
                              rx_cfg.get('scan_interval', 15))
        self._idle_timeout  = dec_cfg.get('max_idle_time', 300)   # seconds without frames → back to scan
        self._startup_timeout = int(dec_cfg.get('startup_timeout', 10))

        # Runtime-mutable: can be changed from the web UI without restart
        self._debug_mode    = bool(log_cfg.get('debug_mode', False))
        self._snr_threshold = float(det_cfg.get('scan_threshold', 10.0))

        # dft_detect correlation classification for the Airspy path. Previously
        # the Airspy path classified auto-detected signals purely by bandwidth
        # (_bandwidth_fallback), while the RTL path used dft_detect correlation —
        # so the two front-ends could type the same sonde differently. Here we
        # give Airspy the SAME correlation classifier: for each auto-detected
        # candidate we capture a short 48 kHz int16 IQ clip (airspy_rx→sox, the
        # exact format dft_detect --iq consumes) and correlate it. Falls back to
        # the bandwidth guess if dft_detect is unavailable or returns no match.
        # Default follows detection.use_dft_detect; override per station with
        # detection.airspy_use_dft_detect. Fixed/manual channels keep their
        # explicit type and never enter this path.
        self._use_dft = bool(det_cfg.get('airspy_use_dft_detect',
                                         det_cfg.get('use_dft_detect', True)))
        self._dft = None
        if self._use_dft:
            try:
                from .dft_detector import DftDetector
                self._dft = DftDetector(
                    dft_detect_path=det_cfg.get('dft_detect_path', 'dft_detect'),
                    thresholds=det_cfg.get('dft_thresholds') or None,
                )
                if not self._dft.available:
                    self.logger.warning(
                        "Airspy: dft_detect not available — classifying by "
                        "bandwidth. Run scripts/install_softchain.sh to build it."
                    )
                    self._dft = None
            except Exception as exc:  # noqa: BLE001 - classification is optional
                self.logger.warning(
                    f"Airspy: DftDetector init failed ({exc}) — classifying by bandwidth"
                )
                self._dft = None

        # Latest spectrum snapshot: dict with keys freqs_mhz, power_db, noise_floor,
        # threshold_db, signals, timestamp — served by /api/spectrum
        self._spectrum: dict = {}
        self._spectrum_lock  = threading.Lock()

        # Fixed channels to start immediately at boot (up to 8)
        raw_fixed = det_cfg.get('fixed_channels', []) or []
        self._fixed_channels: List[dict] = list(raw_fixed[:8])
        self._fixed_start_done = (len(self._fixed_channels) == 0)
        
        # RX Scan state
        self._rx_scan_thread: Optional[threading.Thread] = None
        self._rx_scan_running = False
        self._rx_scan_channels: List[dict] = []

        freq_ranges_raw = det_cfg.get('freq_ranges', [[400_000_000, 406_000_000]])
        self._freq_ranges = [tuple(r) for r in freq_ranges_raw]
        bl = det_cfg.get('frequency_blacklist', [])
        self._blacklist = [f * 1e6 for f in bl]
        # Use a dedicated scan_threshold for Airspy FFT peak detection.
        # Do NOT fall back to detection_threshold (18 dB) — that is tuned for
        # the RTL-SDR's coarse 2048-pt single-shot FFT.  The Airspy scanner
        # averages ~245 FFT chunks, inherently improving SNR by ~12 dB, so a
        # lower threshold of 8–10 dB is appropriate.
        self._scan_threshold = self._snr_threshold

        self._max_decoders  = int(airspy_cfg.get('max_decoders', 4))

        # Clamp adaptive scan-gain settings to valid hardware limits.
        self._scan_gain = min(max(self._scan_gain, 0), AirspyPipeline.LNA_GAIN_MAX)
        self._scan_gain_fallback = min(
            max(self._scan_gain_fallback, 0), AirspyPipeline.LNA_GAIN_MAX
        )
        self._scan_clip_trigger_pct = max(0.0, min(self._scan_clip_trigger_pct, 50.0))

        # Multi-channel decode state
        self._state:        str   = self.STATE_IDLE
        self._channelizer:  Optional[AirspyChannelizer]        = None
        self._legacy_pipeline: Optional[AirspyPipeline]        = None
        self._decoders:     Dict[float, RS1729Decoder]          = {}
        self._dec_types:    Dict[float, str]                    = {}
        self._dec_starts:   Dict[float, float]                  = {}
        self._dec_strengths_db: Dict[float, float]              = {}
        self._manual_decoders: Dict[float, Optional[float]]     = {}  # freq -> duration_seconds (None=infinite)

        if self._decode_mode not in ('legacy', 'channelizer'):
            self.logger.warning(
                f"Unknown Airspy decode_mode '{self._decode_mode}', falling back to legacy"
            )
            self._decode_mode = 'legacy'
        if self._decode_mode == 'legacy':
            self._max_decoders = 1

        # Legacy single-channel compat attrs (updated to last added channel)
        self._cur_freq:     Optional[float] = None
        self._cur_type:     Optional[str]   = None
        self._decode_start: float           = 0.0
        self._last_frame_t: float           = 0.0
        self._thread:       Optional[threading.Thread] = None
        self._transitioning = False
        self._legacy_probe_state: Optional[dict] = None

        # RTLSDRDeviceManager-compatible attributes used by web_server.py
        _id = self._serial or 'airspy0'
        self.device_configs      = [{'serial': _id, 'center_freq': self._center_freq}]
        self.first_device_serial = _id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Verify airspy_rx is installed (sox no longer required — Python channelizer)."""
        try:
            subprocess.run(['airspy_rx', '--help'], capture_output=True, timeout=3)
        except FileNotFoundError:
            self.logger.error(
                "airspy_rx not found. "
                "Install with: sudo apt-get install -y airspy"
            )
            return False
        except subprocess.TimeoutExpired:
            pass  # tool exists but --help hangs; that's fine

        self.logger.info(
            f"AirspyReceiver ready: center={self._center_freq/1e6:.3f} MHz, "
            f"rate={self._sample_rate/1e6:.1f} MSPS, "
            f"gain={self._gain}, ppm={self._ppm}, "
            f"max_decoders={self._max_decoders}, "
            f"iq_order={'QI' if self._airspy_qi_order else 'IQ'}, "
            f"decode_mode={self._decode_mode}, "
            f"legacy_snap_hz={self._legacy_snap_hz}, "
            f"startup_timeout={self._startup_timeout}s"
        )
        return True

    def start(self):
        self.running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='AirspyReceiver'
        )
        self._thread.start()
        self.logger.info("AirspyReceiver started")
        # Start any fixed channels immediately
        if self._fixed_channels:
            threading.Thread(
                target=self._start_fixed_channels, daemon=True, name='FixedChannels'
            ).start()

    # ------------------------------------------------------------------
    # Runtime configuration API (used by web UI)
    # ------------------------------------------------------------------

    def set_debug_mode(self, enabled: bool):
        """Enable or disable verbose scanner INFO logging at runtime."""
        self._debug_mode = bool(enabled)
        self.logger.info(f"Debug mode {'enabled' if self._debug_mode else 'disabled'}")

    def set_snr_threshold(self, threshold_db: float):
        """Update the SNR detection threshold (dB above noise floor) at runtime."""
        self._snr_threshold = max(3.0, min(30.0, float(threshold_db)))
        self._scan_threshold = self._snr_threshold
        self.logger.info(f"SNR threshold set to {self._snr_threshold:.1f} dB")

    def set_scan_interval(self, seconds: float):
        """Update the idle scan interval at runtime."""
        self._scan_interval = max(5, int(seconds))
        self.logger.info(f"Scan interval set to {self._scan_interval} s")

    def get_runtime_config(self) -> dict:
        """Return runtime-mutable settings (served by /api/runtime_config)."""
        return {
            'debug_mode': self._debug_mode,
            'snr_threshold': self._snr_threshold,
            'scan_interval': self._scan_interval,
        }

    def get_spectrum(self) -> dict:
        """Return latest spectrum snapshot (served by /api/spectrum)."""
        with self._spectrum_lock:
            return dict(self._spectrum)

    def get_spectrum_receivers(self) -> List[dict]:
        """Return selectable spectrum receiver list for web UI."""
        serial = self._serial or 'airspy0'
        return [{'id': f'airspy:{serial}', 'name': f'Airspy {serial}'}]

    def get_spectrum_for_receiver(self, receiver_id: str) -> dict:
        """Return latest spectrum for selected Airspy receiver id."""
        serial = self._serial or 'airspy0'
        expected = f'airspy:{serial}'
        if receiver_id and receiver_id != expected:
            return {
                'receiver_id': expected,
                'receiver_name': f'Airspy {serial}',
                'freqs_mhz': [],
                'power_db': [],
                'signals': [],
                'timestamp': datetime.utcnow().isoformat() + 'Z',
            }
        spec = self.get_spectrum()
        if spec:
            return spec
        return {
            'receiver_id': expected,
            'receiver_name': f'Airspy {serial}',
            'freqs_mhz': [],
            'power_db': [],
            'signals': [],
            'timestamp': datetime.utcnow().isoformat() + 'Z',
        }

    # ------------------------------------------------------------------
    # Fixed-channel startup
    # ------------------------------------------------------------------

    def _start_fixed_channels(self):
        """Decode the fixed_channels list at startup, before any scanning."""
        time.sleep(1.5)   # brief delay so the main loop is alive
        try:
            # Filter for enabled channels only
            enabled_channels = [ch for ch in self._fixed_channels if ch.get('enabled', False)]
            
            # Separate into instant decode and RX scan channels
            instant_decode = [ch for ch in enabled_channels if not ch.get('rx_scan', False)]
            rx_scan = [ch for ch in enabled_channels if ch.get('rx_scan', False)]
            
            # Start instant decode channels immediately
            if instant_decode:
                fixed_entries = instant_decode
                if self._decode_mode == 'legacy' and len(fixed_entries) > 1:
                    self.logger.warning(
                        "Airspy legacy decode_mode supports one active decoder; using only the first fixed channel"
                    )
                    fixed_entries = fixed_entries[:1]

                for entry in fixed_entries:
                    try:
                        freq_hz = float(entry['frequency']) * 1e6
                        stype   = str(entry.get('type', 'RS41'))
                        sig = DetectedSignal(
                            frequency=freq_hz, strength=30.0, bandwidth=5000,
                            timestamp=time.time()
                        )
                        self.logger.info(
                            f"Fixed channel (instant): starting {stype} decoder at {freq_hz/1e6:.3f} MHz"
                        )
                        self._add_channel(sig, override_type=stype)
                    except Exception as exc:
                        self.logger.warning(f"Fixed channel entry error: {exc}")
            
            # Start RX Scan thread if there are scan channels
            if rx_scan:
                self._rx_scan_channels = rx_scan
                self._rx_scan_running = True
                self._rx_scan_thread = threading.Thread(
                    target=self._rx_scan_loop, daemon=True, name='RXScan'
                )
                self._rx_scan_thread.start()
                self.logger.info(f"RX Scan enabled with {len(rx_scan)} channels (15s each)")
                
        finally:
            self._fixed_start_done = True

    def _rx_scan_loop(self):
        """Cycle through RX Scan channels, decoding each for 15 seconds."""
        scan_duration = 15  # seconds per channel
        
        while self.running and self._rx_scan_running:
            for idx, entry in enumerate(self._rx_scan_channels):
                if not self.running or not self._rx_scan_running:
                    break
                    
                try:
                    freq_hz = float(entry['frequency']) * 1e6
                    stype = str(entry.get('type', 'RS41'))
                    
                    self.logger.info(
                        f"RX Scan: channel {idx+1}/{len(self._rx_scan_channels)} - "
                        f"{stype} at {freq_hz/1e6:.3f} MHz for {scan_duration}s"
                    )
                    
                    # Stop any current decoders
                    self._teardown_decode()
                    
                    # Start decoder on this channel
                    sig = DetectedSignal(
                        frequency=freq_hz, strength=30.0, bandwidth=5000,
                        timestamp=time.time()
                    )
                    self._add_channel(sig, override_type=stype)
                    
                    # Wait for scan duration
                    time.sleep(scan_duration)
                    
                except Exception as exc:
                    self.logger.warning(f"RX Scan channel error: {exc}")
                    time.sleep(1)  # Brief pause before continuing
                    
            # Brief pause before restarting cycle
            if self.running and self._rx_scan_running:
                time.sleep(1)

    def stop(self):
        self.running = False
        self._rx_scan_running = False  # Stop RX Scan thread
        self._teardown_decode()
        if self._thread:
            self._thread.join(timeout=10)
        if self._rx_scan_thread:
            self._rx_scan_thread.join(timeout=5)
        self._state = self.STATE_IDLE
        self.logger.info("AirspyReceiver stopped")

    def stop_decode_and_scan(self):
        """Stop any active decode and return to scanning (called from web UI)."""
        if self._state == self.STATE_DECODING:
            self.logger.info("Stopping decode, returning to scan mode")
            self._teardown_decode()
        self._state = self.STATE_SCANNING

    # ------------------------------------------------------------------
    # web_server.py / openwxsdr_app.py compatible API
    # ------------------------------------------------------------------

    @property
    def active_decoders(self) -> Dict:
        """Return {freq: ActiveDecoder} snapshot — compatible with RTLSDRDeviceManager."""
        if self._state != self.STATE_DECODING or not self._decoders:
            return {}
        from .device_manager import ActiveDecoder
        active_pipeline = self._channelizer or self._legacy_pipeline
        result = {}
        for freq, decoder in self._decoders.items():
            start_t = self._dec_starts.get(freq, 0.0)
            last_t  = (
                decoder.last_frame_time.timestamp()
                if decoder.last_frame_time else start_t
            )
            result[freq] = ActiveDecoder(
                decoder=decoder,
                signal=DetectedSignal(
                    frequency=freq,
                    strength=20.0,
                    bandwidth=5000,
                    timestamp=start_t,
                ),
                start_time=start_t,
                last_update=last_t,
                audio_pipeline=active_pipeline,
                device_serial=self._serial or 'airspy0',
            )
        return result

    def get_worker_status(self) -> List[dict]:
        """Per-device status list for web UI /api/devices."""
        if self._state == self.STATE_DECODING and self._decoders:
            return [
                {
                    'serial':     self._serial or 'airspy0',
                    'state':      self.STATE_DECODING,
                    'sonde_type': self._dec_types.get(freq),
                    'frequency':  freq,
                    'freq_label': f'{freq/1e6:.3f} MHz',
                }
                for freq in self._decoders
            ]
        return [{
            'serial':     self._serial or 'airspy0',
            'state':      self._state,
            'sonde_type': None,
            'frequency':  None,
            'freq_label': (f'{self._center_freq/1e6:.1f} MHz '
                           f'±{self._sample_rate/2e6:.1f} MHz'),
        }]

    def start_manual_decoder(self, frequency: float, sonde_type: str,
                           duration_seconds: Optional[float] = None) -> bool:
        return self.start_manual_decoder_on(frequency, sonde_type, duration_seconds=duration_seconds)

    def start_manual_decoder_on(self, frequency: float, sonde_type: str,
                                device_serial: str = None,
                                duration_seconds: Optional[float] = None) -> bool:
        """Manually start/replace a decoder for a specific frequency (from web UI)."""
        self._transitioning = True
        # If already decoding this exact frequency, restart it
        try:
            if frequency in self._decoders:
                self.logger.info(f"Restarting decoder for {frequency/1e6:.3f} MHz")
                dec = self._decoders.pop(frequency, None)
                if dec:
                    dec.stop()
                
                # Clean up legacy pipeline if it exists (holds device lock)
                if self._legacy_pipeline:
                    try:
                        self._legacy_pipeline.stop()
                    except Exception as e:
                        self.logger.warning(f"Error stopping legacy pipeline: {e}")
                    self._legacy_pipeline = None
                
                if self._channelizer:
                    self._channelizer.remove_channel(frequency)
                self._dec_types.pop(frequency, None)
                self._dec_starts.pop(frequency, None)
                self._dec_strengths_db.pop(frequency, None)
                self._manual_decoders.pop(frequency, None)
                time.sleep(1.5)
            sig = DetectedSignal(
                frequency=frequency, strength=30.0, bandwidth=5000, timestamp=time.time()
            )
            return self._add_channel(sig, override_type=sonde_type, manual_duration_seconds=duration_seconds)
        finally:
            self._transitioning = False

    # ------------------------------------------------------------------
    # Main state-machine loop
    # ------------------------------------------------------------------

    def _run(self):
        while self.running:
            try:
                if not self._fixed_start_done:
                    # Avoid scanner/device contention while fixed channels are created.
                    time.sleep(0.2)
                    continue
                if self._transitioning:
                    time.sleep(0.1)
                    continue
                if self._state in (self.STATE_IDLE, self.STATE_SCANNING, self.STATE_ERROR):
                    self._scan_cycle()
                elif self._state == self.STATE_DECODING:
                    self._decode_cycle()
            except Exception as exc:
                self.logger.error(f"Receiver loop error: {exc}", exc_info=True)
                time.sleep(5)

    def _scan_cycle(self):
        if self._decoders or self._transitioning:
            self._state = self.STATE_DECODING
            return

        self._state = self.STATE_SCANNING

        scanner = AirspyScanner(
            center_freq=self._center_freq,
            sample_rate=self._sample_rate,
            lna_gain=self._scan_gain,
            capture_seconds=1.5,
            fft_chunk_size=32768,
            detection_threshold_db=self._snr_threshold,
            freq_ranges=self._freq_ranges,
            frequency_blacklist=self._blacklist,
            debug_mode=self._debug_mode,
            airspy_qi_order=self._airspy_qi_order,
            min_bw_hz=self._scan_min_bw_hz,
            birdie_snr_db=self._birdie_snr_db,
            birdie_min_bw_hz=self._birdie_min_bw_hz,
        )
        self.logger.info(
            f"Scanning {self._center_freq/1e6:.1f} MHz "
            f"±{self._sample_rate/2e6:.1f} MHz"
        )
        signals = scanner.scan()

        if scanner.last_capture_failed:
            self._consecutive_scan_failures += 1
            if self._consecutive_scan_failures >= self.MAX_CONSECUTIVE_SCAN_FAILURES:
                self._state = self.STATE_ERROR
                self.logger.error(
                    f"Airspy device not reachable after {self._consecutive_scan_failures} "
                    "consecutive scan failures (airspy_open() failed / not found) — "
                    "showing error state in the web UI, will keep retrying in the background"
                )
            time.sleep(self._scan_interval)
            return

        if self._consecutive_scan_failures >= self.MAX_CONSECUTIVE_SCAN_FAILURES:
            self.logger.info("Airspy device recovered — resuming normal scanning")
        self._consecutive_scan_failures = 0

        # A decoder may have been started while this scan was in progress.
        if self._decoders or self._transitioning:
            self._state = self.STATE_DECODING
            return

        # If front-end clipping is detected, run a second scan at lower gain.
        if (
            self._scan_gain_fallback != self._scan_gain and
            scanner.last_clip_ratio_pct >= self._scan_clip_trigger_pct
        ):
            self.logger.info(
                f"Re-scanning at lower gain {self._scan_gain_fallback} "
                f"(clip={scanner.last_clip_ratio_pct:.2f}% >= "
                f"{self._scan_clip_trigger_pct:.2f}%)"
            )
            scanner_low = AirspyScanner(
                center_freq=self._center_freq,
                sample_rate=self._sample_rate,
                lna_gain=self._scan_gain_fallback,
                capture_seconds=1.5,
                fft_chunk_size=32768,
                detection_threshold_db=self._snr_threshold,
                freq_ranges=self._freq_ranges,
                frequency_blacklist=self._blacklist,
                debug_mode=self._debug_mode,
                airspy_qi_order=self._airspy_qi_order,
                min_bw_hz=self._scan_min_bw_hz,
                birdie_snr_db=self._birdie_snr_db,
                birdie_min_bw_hz=self._birdie_min_bw_hz,
            )
            low_signals = scanner_low.scan()
            signals = self._merge_detected_signals(signals, low_signals)
            # Prefer lower-gain spectrum if it produced candidates.
            if low_signals and scanner_low.last_spectrum:
                scanner = scanner_low

        # Store spectrum snapshot for /api/spectrum
        if scanner.last_spectrum:
            _spec = scanner.last_spectrum.copy()
            _spec['signals'] = [
                {'freq_mhz': s.frequency / 1e6, 'snr_db': s.strength, 'bw_khz': s.bandwidth / 1e3}
                for s in signals
            ]
            with self._spectrum_lock:
                self._spectrum = _spec
        for sig in signals:
            sig = self._normalize_detected_signal(sig)
            self.logger.info(
                f"Signal at {sig.frequency/1e6:.4f} MHz "
                f"(SNR {sig.strength:.1f} dB, BW {sig.bandwidth/1e3:.1f} kHz)"
            )
            if any(abs(sig.frequency - f) < 50_000 for f in self._decoders):
                continue  # already decoding this frequency
            if len(self._decoders) >= self._max_decoders:
                self.logger.info(
                    f"Max decoders ({self._max_decoders}) reached — "
                    f"skipping {sig.frequency/1e6:.4f} MHz"
                )
                break
            # Classify by dft_detect correlation (same engine as the RTL path)
            # before committing a decoder; None → _add_channel uses the
            # bandwidth fallback.
            #
            # ONLY in legacy mode. In channelizer mode a single airspy_rx is held
            # open continuously by the channelizer, so opening a SECOND airspy_rx
            # here for the classification clip is architecturally wrong: the
            # back-to-back scan→clip→channelizer device opens leave the Airspy in
            # a degraded state (observed: DDC RMS collapses to ~0.002, decoder
            # healthy but zero frames). Channelizer mode keeps the pre-1.0.62
            # bandwidth classification.
            override = None
            if self._decode_mode != 'channelizer':
                override = self._classify_with_dft(sig)
            self._add_channel(sig, override_type=override)

        if self._decoders:
            return  # state already set to DECODING by _add_channel

        time.sleep(self._scan_interval)

    @staticmethod
    def _merge_detected_signals(primary: List[DetectedSignal],
                                secondary: List[DetectedSignal]) -> List[DetectedSignal]:
        """Merge two detection lists and keep strongest peak per 50 kHz bucket."""
        if not primary:
            return list(secondary)
        if not secondary:
            return list(primary)

        merged: List[DetectedSignal] = list(primary)
        for sig in secondary:
            replaced = False
            for idx, cur in enumerate(merged):
                if abs(sig.frequency - cur.frequency) < 50_000:
                    if sig.strength > cur.strength:
                        merged[idx] = sig
                    replaced = True
                    break
            if not replaced:
                merged.append(sig)

        return sorted(merged, key=lambda s: s.strength, reverse=True)

    def _normalize_detected_signal(self, sig: DetectedSignal) -> DetectedSignal:
        """Snap auto-detected frequencies to a sane decode grid in legacy mode."""
        if self._decode_mode != 'legacy' or self._legacy_snap_hz <= 0:
            return sig

        snapped = round(sig.frequency / self._legacy_snap_hz) * self._legacy_snap_hz
        snapped = float(snapped)
        if abs(snapped - sig.frequency) >= 1.0:
            self.logger.info(
                f"Legacy auto-tune snap: {sig.frequency/1e6:.4f} MHz -> {snapped/1e6:.4f} MHz"
            )
        return DetectedSignal(
            frequency=snapped,
            strength=sig.strength,
            bandwidth=sig.bandwidth,
            timestamp=sig.timestamp,
        )

    def _decode_cycle(self):
        if not self._decoders:
            self._cleanup_all()
            return

        if self._decode_mode == 'legacy' and self._legacy_probe_state:
            for freq, decoder in list(self._decoders.items()):
                start_t = self._dec_starts.get(freq, time.time())
                if decoder.frame_count == 0 and (time.time() - start_t) >= self._startup_timeout:
                    next_freq = self._next_legacy_probe_frequency()
                    if next_freq is not None:
                        sonde_type = self._dec_types.get(freq, 'RS41')
                        probe_state = dict(self._legacy_probe_state)
                        self.logger.info(
                            f"Legacy startup probe: no frames after {self._startup_timeout}s at "
                            f"{freq/1e6:.4f} MHz, trying {next_freq/1e6:.4f} MHz"
                        )
                        self._transitioning = True
                        try:
                            self._cleanup_all()
                            self._add_channel(
                                DetectedSignal(
                                    frequency=next_freq,
                                    strength=0.0,
                                    bandwidth=5000,
                                    timestamp=time.time(),
                                ),
                                override_type=sonde_type,
                                probe_state=probe_state,
                            )
                        finally:
                            self._transitioning = False
                        return

                    self.logger.info(
                        f"Legacy startup probe exhausted around {self._legacy_probe_state['base_frequency']/1e6:.4f} MHz"
                    )
                    self._cleanup_all()
                    return

        # Prune dead or idle channels
        dead = []
        for freq, decoder in list(self._decoders.items()):
            if not decoder.is_alive():
                self.logger.info(
                    f"Decoder ended on {freq/1e6:.4f} MHz — removing channel"
                )
                dead.append(freq)
            elif freq in self._manual_decoders:
                # Manual decoders: duration=None/0 means infinite, skip idle check
                # Duration>0 is handled by timer expiration (not idle timeout)
                pass
            elif decoder.is_idle(idle_threshold=self._idle_timeout):
                self.logger.info(
                    f"Decoder idle >{self._idle_timeout}s on "
                    f"{freq/1e6:.4f} MHz — removing channel"
                )
                decoder.stop()
                dead.append(freq)

        for freq in dead:
            if self._channelizer:
                self._channelizer.remove_channel(freq)
            self._decoders.pop(freq, None)
            self._dec_types.pop(freq, None)
            self._dec_starts.pop(freq, None)
            self._dec_strengths_db.pop(freq, None)
            self._manual_decoders.pop(freq, None)

        if not self._decoders:
            self._cleanup_all()
            return

        # Check that the underlying hardware process is still alive
        active_pipeline = self._channelizer or self._legacy_pipeline
        if active_pipeline and not active_pipeline.is_alive():
            self.logger.warning("Airspy decode pipeline died — returning to scan")
            self._cleanup_all()
            return

        time.sleep(2)

    # ------------------------------------------------------------------
    # Scan → Decode transition
    # ------------------------------------------------------------------

    def _build_legacy_probe_state(self, frequency: float, sonde_type: str) -> dict:
        base_frequency = float(frequency)
        seen = set()
        candidates = []
        for offset_hz in self._legacy_probe_offsets_hz:
            candidate = float(base_frequency + int(offset_hz))
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
        if base_frequency not in seen:
            candidates.insert(0, base_frequency)

        return {
            'base_frequency': base_frequency,
            'sonde_type': sonde_type,
            'candidates': candidates,
            'next_index': 1,
        }

    def _next_legacy_probe_frequency(self) -> Optional[float]:
        if not self._legacy_probe_state:
            return None

        candidates = self._legacy_probe_state['candidates']
        next_index = self._legacy_probe_state['next_index']
        if next_index >= len(candidates):
            return None

        self._legacy_probe_state['next_index'] = next_index + 1
        return float(candidates[next_index])

    def _classify_with_dft(self, sig: DetectedSignal) -> Optional[str]:
        """Classify an auto-detected candidate via dft_detect correlation.

        Captures a short IQ clip from the Airspy at sig.frequency and runs the
        shared DftDetector. Returns the sonde type on a confident match, else
        None (caller falls back to the bandwidth guess). The dft frequency
        offset is logged but not applied — the legacy probe already refines the
        tune, and the reliability win here is the correct TYPE.
        """
        if self._dft is None:
            return None
        iq_file = self._capture_iq_clip(sig.frequency, self._dft.sample_duration)
        if not iq_file:
            return None
        try:
            result = self._dft.classify_iq_file(iq_file, 48_000)
        finally:
            try:
                os.unlink(iq_file)
            except OSError:
                pass
        if result:
            sonde_type, offset = result
            self.logger.info(
                f"Airspy DFT identified {sonde_type} at {sig.frequency/1e6:.4f} MHz "
                f"(offset {offset:+.0f} Hz)"
            )
            return sonde_type
        self.logger.info(
            f"Airspy DFT: no confident match at {sig.frequency/1e6:.4f} MHz "
            f"— using bandwidth classification"
        )
        return None

    def _capture_iq_clip(self, frequency: float, seconds: float) -> Optional[str]:
        """Capture ~`seconds` of 48 kHz signed-16 int IQ at `frequency` to a temp
        .raw file (airspy_rx→sox), the exact format dft_detect --iq expects.

        Runs only while the receiver is between scans (device is free), mirroring
        the RTL path's close-scan → capture → reopen-decoder sequence. Returns the
        file path, or None on failure. Caller deletes the file.
        """
        adj_freq = frequency * (1.0 + self._ppm / 1e6)
        lna   = min(self._gain, AirspyPipeline.LNA_GAIN_MAX)
        mixer = min(self._gain, AirspyPipeline.MIXER_GAIN_MAX)
        vga   = min(self._gain, AirspyPipeline.VGA_GAIN_MAX)

        airspy_cmd = [
            'airspy_rx',
            '-f', f'{adj_freq/1e6:.6f}',
            '-a', str(self._sample_rate),
            '-r', '-',
            '-l', str(lna), '-m', str(mixer), '-v', str(vga),
        ]
        if self._serial:
            airspy_cmd += ['-p', str(self._serial)]
        sox_cmd = [
            'sox',
            '-t', 'raw', '-e', 'signed-integer', '-b', '16',
            '-r', str(self._sample_rate), '-c', '2', '-',
            '-t', 'raw', '-e', 'signed-integer', '-b', '16',
            '-r', '48000', '-c', '2', '-',
        ]

        fd, path = tempfile.mkstemp(suffix='.raw', prefix='openwxsdr_airspy_dft_')
        os.close(fd)
        airspy_proc = sox_proc = None
        try:
            with open(path, 'wb') as out:
                airspy_proc = subprocess.Popen(
                    airspy_cmd, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0)
                sox_proc = subprocess.Popen(
                    sox_cmd, stdin=airspy_proc.stdout, stdout=out,
                    stderr=subprocess.DEVNULL, bufsize=0)
                airspy_proc.stdout.close()
                time.sleep(max(1.0, float(seconds)))
                for p in (sox_proc, airspy_proc):
                    if p and p.poll() is None:
                        try:
                            p.terminate()
                            p.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            p.kill()
                            p.wait()
            # Let libusb fully release the Airspy before the decode pipeline
            # reopens it — a back-to-back airspy_rx open otherwise risks a
            # degraded (low-gain) capture on the same device.
            time.sleep(0.8)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size < 4096:
                self.logger.warning(
                    f"Airspy DFT clip too small ({size} bytes) at "
                    f"{frequency/1e6:.4f} MHz — skipping correlation"
                )
                os.unlink(path)
                return None
            return path
        except FileNotFoundError as exc:
            self.logger.error(
                f"Airspy DFT clip capture: tool not found ({exc}). "
                "Install with: sudo apt-get install -y airspy sox"
            )
        except Exception as exc:  # noqa: BLE001 - classification is optional
            self.logger.warning(f"Airspy DFT clip capture failed: {exc}")
            for p in (sox_proc, airspy_proc):
                if p and p.poll() is None:
                    try:
                        p.kill()
                        p.wait()
                    except Exception:
                        pass
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    def _add_channel(self, sig: DetectedSignal,
                     override_type: Optional[str] = None,
                     probe_state: Optional[dict] = None,
                     manual_duration_seconds: Optional[float] = -1) -> bool:
        """Start a decoder for sig using the configured Airspy decode pipeline.
        
        Args:
            manual_duration_seconds: -1 (default) = auto-detected, None/0 = manual infinite, >0 = manual timed
        """
        sonde_type = override_type or self._bandwidth_fallback(sig)

        lna   = min(self._gain, AirspyPipeline.LNA_GAIN_MAX)
        mixer = min(self._gain, AirspyPipeline.MIXER_GAIN_MAX)
        vga   = min(self._gain, AirspyPipeline.VGA_GAIN_MAX)

        if self._decode_mode == 'legacy':
            if self._decoders and sig.frequency not in self._decoders:
                self.logger.info(
                    f"Airspy legacy mode retuning to {sig.frequency/1e6:.4f} MHz "
                    f"from {next(iter(self._decoders))/1e6:.4f} MHz"
                )
                self._cleanup_all()
                # Wait for device to be fully released before starting new pipeline
                time.sleep(1.0)

            # Retry loop to handle device contention
            pipeline_started = False
            max_retries = 3
            for attempt in range(max_retries):
                self._legacy_pipeline = AirspyPipeline(
                    frequency=sig.frequency,
                    sample_rate=self._sample_rate,
                    serial=self._serial,
                    lna_gain=lna,
                    mixer_gain=mixer,
                    vga_gain=vga,
                    ppm_correction=self._ppm,
                )
                if self._legacy_pipeline.start():
                    pipeline_started = True
                    break
                else:
                    self.logger.warning(
                        f"Airspy legacy pipeline start failed (attempt {attempt+1}/{max_retries})"
                    )
                    self._legacy_pipeline = None
                    if attempt < max_retries - 1:
                        time.sleep(0.5)  # Wait before retry
            
            if not pipeline_started:
                self.logger.error("Airspy legacy pipeline failed to start after retries")
                return False

            stream = self._legacy_pipeline.get_audio_stream()
            if stream is None:
                self.logger.error("Airspy legacy pipeline did not provide an audio stream")
                self._legacy_pipeline.stop()
                self._legacy_pipeline = None
                return False

            _freq = sig.frequency
            _type = sonde_type
            decoder = RS1729Decoder(frequency=sig.frequency, sonde_type=sonde_type)
            decoder.set_frame_callback(
                lambda data: self._on_frame(data, decoder_freq=_freq, decoder_type=_type)
            )
            if not decoder.start(audio_stream=stream):
                self.logger.error(
                    f"RS1729 decoder failed to start for {sig.frequency/1e6:.4f} MHz"
                )
                self._legacy_pipeline.stop()
                self._legacy_pipeline = None
                return False

            self._legacy_probe_state = probe_state or self._build_legacy_probe_state(
                sig.frequency, sonde_type
            )

            self._decoders[sig.frequency]   = decoder
            self._dec_types[sig.frequency]  = sonde_type
            self._dec_starts[sig.frequency] = time.time()
            self._dec_strengths_db[sig.frequency] = float(sig.strength)
            # Track manual decoders: -1 = auto-detected, None/0 = infinite, >0 = timed
            if manual_duration_seconds != -1:
                self._manual_decoders[sig.frequency] = manual_duration_seconds
            self._cur_freq     = sig.frequency
            self._cur_type     = sonde_type
            self._decode_start = self._dec_starts[sig.frequency]
            self._last_frame_t = 0.0
            self._state        = self.STATE_DECODING

            self.logger.info(
                f"Decoding {sonde_type} at {sig.frequency/1e6:.4f} MHz "
                f"(Airspy legacy pipeline)"
            )
            return True

        # Start channelizer if not already running
        if self._channelizer is None:
            self._channelizer = AirspyChannelizer(
                center_freq=self._center_freq,
                sample_rate=self._sample_rate,
                lna_gain=lna, mixer_gain=mixer, vga_gain=vga,
                serial=self._serial,
                ppm_correction=self._ppm,
                airspy_qi_order=self._airspy_qi_order,
                debug_iq_dump=self._debug_iq_dump,
            )
            if not self._channelizer.start():
                self.logger.error("AirspyChannelizer failed to start")
                self._channelizer = None
                return False

        stream = self._channelizer.add_channel(sig.frequency)
        if stream is None:
            return False

        # Capture per-channel values for the callback closure
        _freq = sig.frequency
        _type = sonde_type
        decoder = RS1729Decoder(frequency=sig.frequency, sonde_type=sonde_type)
        decoder.set_frame_callback(
            lambda data: self._on_frame(data, decoder_freq=_freq, decoder_type=_type)
        )
        if not decoder.start(audio_stream=stream):
            self.logger.error(
                f"RS1729 decoder failed to start for {sig.frequency/1e6:.4f} MHz"
            )
            self._channelizer.remove_channel(sig.frequency)
            return False

        self._decoders[sig.frequency]   = decoder
        self._dec_types[sig.frequency]  = sonde_type
        self._dec_starts[sig.frequency] = time.time()
        self._dec_strengths_db[sig.frequency] = float(sig.strength)
        # Track manual decoders: -1 = auto-detected, None/0 = infinite, >0 = timed
        if manual_duration_seconds != -1:
            self._manual_decoders[sig.frequency] = manual_duration_seconds

        # Keep legacy compat attrs pointing at the most-recently added channel
        self._cur_freq     = sig.frequency
        self._cur_type     = sonde_type
        self._decode_start = self._dec_starts[sig.frequency]
        self._last_frame_t = 0.0
        self._state        = self.STATE_DECODING

        self.logger.info(
            f"Decoding {sonde_type} at {sig.frequency/1e6:.4f} MHz "
            f"(Airspy, {len(self._decoders)} active channel(s))"
        )
        return True

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _cleanup_all(self):
        """Stop all active decoders and the channelizer; return to IDLE."""
        for decoder in list(self._decoders.values()):
            try:
                decoder.stop()
            except Exception:
                pass
        self._decoders.clear()
        self._dec_types.clear()
        self._dec_starts.clear()
        self._dec_strengths_db.clear()
        self._manual_decoders.clear()
        self._legacy_probe_state = None
        if self._legacy_pipeline:
            try:
                self._legacy_pipeline.stop()
            except Exception:
                pass
            self._legacy_pipeline = None
        if self._channelizer:
            try:
                self._channelizer.stop()
            except Exception:
                pass
            self._channelizer = None
        self._cur_freq = None
        self._cur_type = None
        self._state    = self.STATE_IDLE

    def _teardown_decode(self):
        """Backward-compat alias used by stop(), stop_decode_and_scan()."""
        self._cleanup_all()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _bandwidth_fallback(self, sig: DetectedSignal) -> str:
        """Estimate sonde type from signal bandwidth."""
        bw   = sig.bandwidth
        freq = sig.frequency
        if 400e6 <= freq <= 406e6:
            if bw >= 22_000: return 'M20'
            if bw >= 16_000: return 'iMet'
            if bw >= 14_000: return 'M10'
            if bw >= 10_000: return 'DFM'
            return 'RS41'
        return 'RS41'

    def _on_frame(self, frame_data: dict,
                  decoder_freq: Optional[float] = None,
                  decoder_type: Optional[str]  = None):
        """Convert rs1729 frame dict → SondeTelemetry and forward upstream."""
        self._last_frame_t = time.time()
        try:
            sonde_id     = frame_data.get('sonde_id', 'UNKNOWN')
            frequency_hz = frame_data.get('frequency', decoder_freq or self._cur_freq or 0.0)

            def _parse_db(val) -> Optional[float]:
                if val is None:
                    return None
                if isinstance(val, (int, float)):
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return None
                if isinstance(val, str):
                    match = re.search(r'(-?\d+(?:\.\d+)?)', val)
                    if match:
                        try:
                            return float(match.group(1))
                        except (TypeError, ValueError):
                            return None
                return None

            signal_db = self._dec_strengths_db.get(frequency_hz)
            if signal_db is None and decoder_freq is not None:
                signal_db = self._dec_strengths_db.get(decoder_freq)

            live_rssi = None
            live_snr = None
            if decoder_freq is not None and self._channelizer is not None:
                live_rssi, live_snr = self._channelizer.get_channel_metrics_snapshot(decoder_freq)
            elif self._legacy_pipeline is not None and hasattr(self._legacy_pipeline, 'get_signal_metrics_snapshot'):
                live_rssi, live_snr = self._legacy_pipeline.get_signal_metrics_snapshot()

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

            if rssi_db is None:
                rssi_db = signal_db
            if snr_db is None:
                snr_db = signal_db

            if live_rssi is not None:
                rssi_db = live_rssi
            if live_snr is not None:
                snr_db = live_snr

            frame_number = 0
            raw = frame_data.get('raw_line', '')
            if '[' in raw and ']' in raw:
                try:
                    frame_number = int(raw[raw.find('[') + 1:raw.find(']')].strip())
                except ValueError:
                    pass

            position = None
            if 'lat' in frame_data and 'lon' in frame_data and 'alt' in frame_data:
                position = SondePosition(
                    latitude=frame_data['lat'],
                    longitude=frame_data['lon'],
                    altitude=frame_data['alt'],
                    datetime=datetime.utcnow()
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
                sonde_type=frame_data.get('sonde_type', decoder_type or self._cur_type or 'RS41'),
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
                timestamp=datetime.utcnow(),
                decoder_name='rs1729',
                decoder_version='rs1729'
            )

            if self.telemetry_callback:
                self.telemetry_callback(telemetry)

        except Exception as exc:
            self.logger.error(f"Frame conversion error: {exc}", exc_info=True)
