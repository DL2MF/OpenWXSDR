"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : signal_metrics.py
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
#  Rolling signal metrics (RSSI/SNR-like) computed from live IQ samples.
#
#  Provides SignalMetrics, a thread-safe rolling IQ power estimator that
#  computes RSSI (dBFS and gain-compensated dBm) and SNR from a sliding
#  window of instantaneous IQ power samples.
#
#  RSSI reported as estimated dBm using a gain-compensated dBFS mapping:
#    rssi_dbm ≈ rssi_dbfs − gain_db + calibration_db
#
#  SNR is estimated SPECTRALLY (in-band vs out-of-band) from an FFT of each
#  IQ chunk: the sonde is a narrowband signal centred in the wide (48 kHz)
#  rtl_fm channel, so it fills only a fraction of the FFT bins and the rest are
#  noise. The noise floor is the low percentile of the PSD bins (separation in
#  FREQUENCY), and SNR = in-band signal power over in-band noise power. The old
#  method took the noise floor from the rolling TOTAL-power history (separation
#  in TIME); for a continuously-transmitting sonde every history sample already
#  contained the signal, so the "floor" tracked the signal and SNR collapsed to
#  ~0 dB, twitching only when RSSI spiked (hence SNR mirrored RSSI).
#
#  Used by : AudioPipeline, AirspyPipeline, AirspyChannelizer
#
# =============================================================================
"""

from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Deque, Optional, Tuple

import numpy as np


@dataclass
class SignalMetrics:
    """Thread-safe rolling IQ power/SNR estimator."""

    window_size: int = 200
    gain_db: float = 0.0
    calibration_db: float = -14.0
    power_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=200))
    latest_rssi_dbfs: float = -140.0
    latest_rssi_dbm: float = -140.0
    latest_snr_db: float = 0.0
    # Recompute at most every min_interval_s. Pumps call update_iq once per audio
    # chunk — hundreds of times/s on the 10 MSPS Airspy channelizer — but per-frame
    # RSSI/SNR only needs ~1/s, so throttling to ~10/s cuts the metric CPU ~40x
    # with no visible loss. Set 0.0 to disable throttling.
    min_interval_s: float = 0.1

    def __post_init__(self):
        # Recreate deque to honor custom window_size when explicitly passed.
        self.power_hist = deque(self.power_hist, maxlen=max(20, int(self.window_size)))
        self._lock = threading.Lock()
        self._last_update = 0.0

    def update_iq(self, i_samples: np.ndarray, q_samples: np.ndarray) -> None:
        """Update rolling metrics from float IQ vectors (throttled to
        min_interval_s so a high-rate pump doesn't burn CPU on redundant recomputes)."""
        if i_samples is None or q_samples is None:
            return
        if len(i_samples) == 0 or len(q_samples) == 0:
            return
        if self.min_interval_s > 0.0:
            now = time.monotonic()
            if now - self._last_update < self.min_interval_s:
                return
            self._last_update = now

        i = i_samples.astype(np.float32, copy=False)
        q = q_samples.astype(np.float32, copy=False)

        # RSSI: mean instantaneous IQ power in linear scale → dBFS → dBm.
        p = np.mean(i * i + q * q)
        p = max(float(p), 1e-12)
        rssi_dbfs = 10.0 * math.log10(p)

        # SNR: spectral in-band vs out-of-band. FFT the chunk; the sonde occupies
        # the central bins while the majority of bins are noise. The noise floor
        # is a low percentile of the PSD (dominated by the noise bins), signal is
        # the bins that rise clearly above it, and SNR compares in-band signal
        # power to in-band noise power. Separating in FREQUENCY (not TIME) is what
        # makes this correct for a continuously-present signal. See module header.
        snr_db = self.latest_snr_db  # keep last value if a chunk is too short
        try:
            n = int(i.shape[0])
            if n >= 64:
                if n > 4096:                       # cap FFT cost (throttled ~10 Hz)
                    n = 4096
                c = (i[:n] + 1j * q[:n]).astype(np.complex64)
                win = np.hanning(n).astype(np.float32)
                spec = np.fft.fft(c * win)
                psd = spec.real * spec.real + spec.imag * spec.imag
                noise_bin = max(float(np.percentile(psd, 20.0)), 1e-12)
                sig_mask = psd > noise_bin * 4.0   # bins ≥ ~6 dB above the floor
                n_sig = int(np.count_nonzero(sig_mask))
                if n_sig > 0:
                    sig_excess = float(np.sum(psd[sig_mask] - noise_bin))
                    snr_db = 10.0 * math.log10(max(sig_excess, 1e-12) / (noise_bin * n_sig))
                    snr_db = max(0.0, snr_db)
                else:
                    snr_db = 0.0
        except Exception:
            pass

        with self._lock:
            self.power_hist.append(p)
            self.latest_rssi_dbfs = rssi_dbfs
            self.latest_rssi_dbm = rssi_dbfs - float(self.gain_db) + float(self.calibration_db)
            self.latest_snr_db = snr_db

    def snapshot(self) -> Tuple[Optional[float], Optional[float]]:
        with self._lock:
            return self.latest_rssi_dbm, self.latest_snr_db
