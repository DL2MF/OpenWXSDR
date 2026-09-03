"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : dft_detector.py
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
#  DFT-based radiosonde type detector for OpenWX.
#
#  Uses the dft_detect tool from the rs1729/RS repository to perform
#  correlation-based sonde identification against known sonde signatures.
#  This approach is significantly more accurate than bandwidth-based
#  detection and can reliably distinguish between RS41, RS92, DFM, M10
#  and iMet sondes with type-specific correlation thresholds.
#
#  Pipeline:
#    rtl_fm (FM demodulation, 48 kHz) → temp .raw file → dft_detect
#
#  Supported sonde types : RS41, RS92, DFM, M10, M20, iMet, LMS6
#  External dependency   : dft_detect (rs1729/RS, github.com/rs1729/RS)
#
# =============================================================================
"""

import subprocess
import logging
import tempfile
import os
import re
import time
import math
from typing import Optional, Dict, Tuple
from dataclasses import dataclass


@dataclass
class CorrelationResult:
    """Correlation detection result from dft_detect"""
    sonde_type: str
    correlation: float
    frequency: float
    bandwidth: float


class DftDetector:
    """
    Sonde type detector using dft_detect correlation analysis.
    
    This is much more accurate than bandwidth-based detection because it:
    - Compares signals against known sonde signatures
    - Uses correlation thresholds optimized to prevent false positives
    - Can distinguish between sondes with similar bandwidths
    
    dft_detect from rs1729/RS repository (https://github.com/rs1729/RS/tree/master/scan)
    
    Correlation thresholds:
    - RS41: ~0.53
    - RS92: ~0.54
    - DFM: ~0.62
    - M10: ~0.75
    """
    
    # Correlation thresholds from radiosonde_auto_rx's 2019 calibration study
    # (auto_rx/test/notes/2019-03-01_dft_detect_optimization.md). Those values
    # were derived by correlating each header against real noise captures and
    # against calibrated-SNR sonde samples, then setting the threshold just
    # above the highest "unwanted type" score. They are a good STARTING point,
    # not a universal truth: the noise-floor correlation depends on the
    # receiver front end, so a station with a different dongle/LNA/RF
    # environment can need different values. Override per station via
    # config.yaml `detection.dft_thresholds` (see DEFAULT_THRESHOLDS use in
    # __init__) rather than editing this table.
    DEFAULT_THRESHOLDS = {
        'RS41': 0.53,
        'RS92': 0.54,
        'DFM': 0.62,
        'M10': 0.75,
        'M20': 0.75,
        'iMet': 0.65,  # Estimated — not covered by the auto_rx study, calibrate locally
        'LMS6': 0.60,  # Estimated — auto_rx had no LMS6 IQ samples at study time
        'MRZ': 0.60,   # Estimated — calibrate locally
    }

    # Backwards-compatible alias: older code/tests referenced DftDetector.THRESHOLDS
    # as a class attribute. Instance-level self.thresholds is authoritative.
    THRESHOLDS = DEFAULT_THRESHOLDS

    # radiosonde_auto_rx uses a narrower IF/capture bandwidth for narrowband
    # sonde types during the detect step specifically to raise correlation
    # SNR (less noise power let through) — and a wider one for types whose
    # signal itself is wider (M10/M20/iMet, ~9-22 kHz). We only support the
    # 400-406 MHz band (no 1680 MHz RS92-NGP/LMS6 support), so this picks
    # between two rates using the coarse 3dB bandwidth already measured by
    # the spectrum scan, rather than blindly reusing auto_rx's own numbers
    # (which are tuned for its fsk_demod-based capture chain) — narrowing
    # blindly risks clipping a genuinely wideband candidate.
    NARROWBAND_SAMPLE_RATE_HZ = 24_000
    WIDEBAND_SAMPLE_RATE_HZ = 48_000
    WIDEBAND_BANDWIDTH_THRESHOLD_HZ = 16_000
    
    def __init__(self, dft_detect_path: str = 'dft_detect', sample_duration: float = 5.0,
                 thresholds: Optional[Dict[str, float]] = None):
        """
        Initialize DFT detector

        Args:
            dft_detect_path: Path to dft_detect binary (default: 'dft_detect' in PATH)
            sample_duration: Duration of IQ capture in seconds (default: 5.0s)
            thresholds: Optional per-type correlation threshold overrides, merged
                over DEFAULT_THRESHOLDS. Supplied from config.yaml
                `detection.dft_thresholds` so a station can be calibrated
                without a code change. Unknown keys are kept (a locally built
                dft_detect may emit types this file doesn't list); non-numeric
                values are ignored with a warning.
        """
        self.dft_detect_path = dft_detect_path
        self.sample_duration = sample_duration
        self.logger = logging.getLogger('DftDetector')

        self.thresholds = dict(self.DEFAULT_THRESHOLDS)
        if thresholds:
            for _type, _value in thresholds.items():
                try:
                    self.thresholds[str(_type)] = float(_value)
                except (TypeError, ValueError):
                    self.logger.warning(
                        f"Ignoring non-numeric dft threshold for '{_type}': {_value!r}"
                    )
            _overrides = {
                k: v for k, v in self.thresholds.items()
                if self.DEFAULT_THRESHOLDS.get(k) != v
            }
            if _overrides:
                self.logger.info(f"Using per-station dft_detect thresholds: {_overrides}")
        self.debug_mode = False  # Can be enabled for detailed correlation parsing logs

        # CRITICAL: install.sh clones rs1729/RS unpinned (plain `git clone` / `git pull`,
        # no fixed commit or tag), so the exact dft_detect CLI convention actually
        # installed on a given host is unknown at code-time. Older builds expect
        # `dft_detect <file> <rate> 16 --iq 0.0 --dc`; newer ("Vigor's fork") builds
        # expect `dft_detect --dc --iq 0.0 - <rate> 16` reading the samples from
        # stdin instead of a filename argument. Passing the wrong convention doesn't
        # error cleanly — the binary tries to parse an argument as if it were sample
        # data, which is exactly consistent with the "exit code 206 (corrupted input
        # data)" / "error: wav header" failures seen on every single invocation in
        # the field. We probe both formats on first use and cache whichever actually
        # produces parseable output, so this self-adapts to whatever is installed.
        self._working_format: Optional[str] = None  # 'legacy' or 'modern' once known
        
        # Check if dft_detect is available
        self.available = self._check_availability()
        if not self.available:
            self.logger.warning(
                "dft_detect not found! Falling back to bandwidth-based detection. "
                "Run install.sh to build dft_detect from rs1729/RS for accurate sonde identification."
            )
    
    def _check_availability(self) -> bool:
        """Check if dft_detect is available"""
        try:
            result = subprocess.run(
                [self.dft_detect_path, '--help'],
                capture_output=True,
                timeout=2.0
            )
            return result.returncode in [0, 1]  # Some versions return 1 for --help
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    
    def detect_sonde_type(
        self,
        frequency: float,
        device_serial: str = "0",
        sample_rate: Optional[int] = None,
        bandwidth: Optional[float] = None
    ) -> Optional[Tuple[str, float]]:
        """
        Detect sonde type using correlation analysis.

        Process:
        1. Capture short burst of IQ samples at detected frequency
        2. Run dft_detect to correlate against known sonde signatures
        3. Return sonde type with highest correlation above threshold

        Args:
            frequency: Center frequency in Hz
            device_serial: RTL-SDR device serial number or index
            sample_rate: Sample rate in Hz. If None (default), auto-selected
                from `bandwidth` — narrower for narrowband candidates to raise
                correlation SNR, wider for candidates that are already wide.
            bandwidth: Optional bandwidth hint in Hz — used both for the
                bandwidth-based fallback AND to auto-select the IF capture
                rate above.

        Returns:
            Tuple of (sonde_type, frequency_offset_hz) or None if no match
            Example: ('RS41', -125.0) means RS41 detected 125 Hz below center
        """
        if not self.available:
            self.logger.debug("dft_detect not available, skipping correlation detection")
            return None

        if sample_rate is None:
            # ALWAYS capture wide (48 kHz). The narrowband auto-selection was
            # driven by the scan's 3 dB-bandwidth estimate, which badly
            # underestimates M10/M20 (two-humped spectrum → the 3 dB walker
            # stops at the first hump: field log showed "BW 2.3 kHz" for a
            # genuine M20 that was actually ~12 kHz wide and 4 kHz off-center).
            # The resulting 24 kHz capture clipped the signal and dft_detect
            # failed 6 consecutive times (~25 s wasted per attempt) before one
            # lucky identification. The theoretical SNR benefit of narrowing
            # never materialized in the field; wide capture identifies on the
            # first attempt.
            sample_rate = self.WIDEBAND_SAMPLE_RATE_HZ

        self.logger.info(f"Running correlation analysis at {frequency/1e6:.4f} MHz")

        # Capture FM-demodulated audio (rtl_fm -M fm output is the correct input for dft_detect)
        iq_file = self._capture_fm_audio(frequency, device_serial, sample_rate)
        if not iq_file:
            return None
        
        try:
            # Run dft_detect on captured samples
            results = self._run_dft_detect(iq_file, sample_rate)
            
            if results:
                # Find best match above threshold
                best_match = self._select_best_match(results)
                if best_match:
                    self.logger.info(
                        f"Detected {best_match.sonde_type} with correlation {best_match.correlation:.3f} "
                        f"(threshold: {self.thresholds.get(best_match.sonde_type, 0.6):.3f}), "
                        f"frequency offset: {best_match.frequency:.1f} Hz"
                    )
                    # Return tuple: (sonde_type, frequency_offset) for frequency correction
                    return (best_match.sonde_type, best_match.frequency)
                # NOTE: the "nothing passed threshold" case is logged inside
                # _select_best_match(), which has the per-type scores to report.
                # (A log call used to sit here AFTER the return above, so it was
                # unreachable and this case logged nothing at all.)
            else:
                self.logger.warning("dft_detect returned no results")
            
            return None
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(iq_file)
            except:
                pass
    
    def classify_iq_file(
        self, iq_file: str, sample_rate: int
    ) -> Optional[Tuple[str, float]]:
        """Classify an ALREADY-captured baseband IQ clip (signed-16 int, 2-ch)
        via dft_detect correlation, decoupled from the RTL/rtl_fm capture in
        detect_sonde_type().

        This lets a non-RTL front-end (e.g. the Airspy path, which produces its
        own 48 kHz int16 IQ via airspy_rx→sox) reuse the exact same correlation
        engine, per-type calibrated thresholds and CLI-convention self-adaption
        as the RTL path — so both front-ends classify identically instead of the
        Airspy path relying on a bandwidth guess.

        Returns (sonde_type, frequency_offset_hz) or None.
        """
        if not self.available:
            return None
        try:
            results = self._run_dft_detect(iq_file, sample_rate)
        except Exception as exc:  # noqa: BLE001 - never let classification crash a scan
            self.logger.warning(f"classify_iq_file: dft_detect error: {exc}")
            return None
        if not results:
            return None
        best = self._select_best_match(results)
        if best:
            self.logger.info(
                f"IQ-file correlation: {best.sonde_type} corr={best.correlation:.3f} "
                f"(threshold {self.thresholds.get(best.sonde_type, 0.6):.3f}), "
                f"offset {best.frequency:+.1f} Hz"
            )
            return (best.sonde_type, best.frequency)
        return None

    def _capture_fm_audio(
        self,
        frequency: float,
        device_serial: str,
        sample_rate: int,
        retry_count: int = 0
    ) -> Optional[str]:
        """
        Capture FM-demodulated audio using rtl_fm.

        This matches how radiosonde_auto_rx uses dft_detect:
        - rtl_fm handles internal decimation so 48 kHz output works reliably
        - FM-demodulated audio is the correct input format for dft_detect
        - rtl_fm accepts serial numbers directly (-d serial), no index lookup needed

        Args:
            frequency: Center frequency in Hz
            device_serial: RTL-SDR device serial number (used directly with rtl_fm -d)
            sample_rate: Output sample rate in Hz (48000 recommended)
            retry_count: Current retry attempt (for PLL lock failures)

        Returns:
            Path to temporary file containing FM audio samples, or None on failure
        """
        # CRITICAL: Add USB settling delay before opening device
        # This prevents "[R82XX] PLL not locked!" errors after scanner closes
        if retry_count == 0:
            self.logger.debug(f"Waiting 2s for USB device {device_serial} to settle before rtl_fm...")
            time.sleep(2.0)
        
        fd, audio_file = tempfile.mkstemp(suffix='.raw', prefix='openwxsdr_dft_')
        os.close(fd)

        # rtl_fm parameters:
        # -d serial  : device by serial number (no index lookup needed)
        # -M raw      : FM demodulation — produces signed 16-bit audio
        # -s rate    : output sample rate (48 kHz works with rtl_fm internal decimation)
        # -f freq    : tune frequency
        # -g gain    : gain (40 dB typical)
        # -E dc      : DC offset removal
        # -         : write to stdout (captured to file)
        cmd = [
            'rtl_fm',
            '-d', device_serial,
            '-M', 'raw',
            '-s', str(sample_rate),
            '-f', str(int(frequency)),
            '-g', '40',
            '-E', 'dc',
            '-'
        ]

        self.logger.debug(f"Capturing FM audio from device {device_serial}: {' '.join(cmd)}")

        proc = None
        try:
            with open(audio_file, 'wb') as outfile:
                proc = subprocess.Popen(
                    cmd,
                    stdout=outfile,
                    stderr=subprocess.PIPE
                )
                time.sleep(self.sample_duration)
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            # Check stderr for PLL lock failures
            stderr_out = b''
            if proc and proc.stderr:
                try:
                    stderr_out = proc.stderr.read()
                except Exception:
                    pass
            
            stderr_text = stderr_out.decode('utf-8', errors='ignore')
            
            # Detect PLL lock failure
            if 'PLL not locked' in stderr_text or 'usb_claim_interface' in stderr_text:
                if retry_count < 2:  # Allow up to 2 retries
                    self.logger.warning(
                        f"RTL-SDR PLL lock failure (attempt {retry_count + 1}/3), "
                        f"retrying after 3s cooldown..."
                    )
                    os.unlink(audio_file)
                    time.sleep(3.0)  # Longer cooldown for hardware recovery
                    return self._capture_fm_audio(frequency, device_serial, sample_rate, retry_count + 1)
                else:
                    self.logger.error(
                        f"RTL-SDR PLL lock failure after {retry_count + 1} attempts, giving up"
                    )
                    os.unlink(audio_file)
                    return None

            # Verify file has usable data
            file_size = os.path.getsize(audio_file) if os.path.exists(audio_file) else 0
            if file_size < 1000:
                self.logger.error(
                    f"FM capture file too small ({file_size} bytes): "
                    f"{stderr_text[:200]}"
                )
                os.unlink(audio_file)
                return None

            self.logger.debug(f"Captured {file_size} bytes of FM audio")
            return audio_file

        except Exception as e:
            self.logger.error(f"Failed to capture FM audio: {e}")
            if proc:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            try:
                os.unlink(audio_file)
            except Exception:
                pass
            return None
    
    def _build_dft_cmd_legacy(self, iq_file: str, sample_rate: int) -> list:
        """Older rs1729/RS build: dft_detect <file> <rate> 16 --iq 0.0 --dc"""
        return [
            self.dft_detect_path,
            iq_file,
            str(sample_rate),
            '16',
            '--iq',
            '0.0',
            '--dc',
        ]

    def _build_dft_cmd_modern(self, sample_rate: int) -> list:
        """Newer ("Vigor's fork") build: dft_detect --dc --iq 0.0 - <rate> 16,
        reading samples from stdin (the '-' placeholder) instead of a filename."""
        return [
            self.dft_detect_path,
            '--dc',
            '--iq',
            '0.0',
            '-',
            str(sample_rate),
            '16',
        ]

    def _build_dft_cmd_advanced(self, iq_file: str, sample_rate: int) -> list:
        """Third observed build convention: --iq is a bare flag (no numeric
        offset argument) — dft_detect --dc --iq - <rate> 16 <iq_file>.
        Passing "0.0" right after --iq on this build makes dft_detect treat
        "0.0" as its input-file positional argument instead, which fails with
        "error: open 0.0" — exactly what was observed in the field. The file
        is passed as the last positional argument, not via stdin."""
        return [
            self.dft_detect_path,
            '--dc',
            '--iq',
            '-',
            str(sample_rate),
            '16',
            iq_file,
        ]

    def _run_dft_detect_once(self, iq_file: str, sample_rate: int, fmt: str):
        """Run a single dft_detect attempt with the given CLI convention.
        Returns (raw_output, results_dict). A non-empty results_dict is the
        only reliable signal that we used the CLI convention this binary
        expects — a clean returncode alone doesn't guarantee that, and a
        206/non-zero exit code alone doesn't rule it out."""
        if fmt == 'modern':
            cmd = self._build_dft_cmd_modern(sample_rate)
            stdin_file = open(iq_file, 'rb')
        elif fmt == 'advanced':
            cmd = self._build_dft_cmd_advanced(iq_file, sample_rate)
            stdin_file = None
        else:
            cmd = self._build_dft_cmd_legacy(iq_file, sample_rate)
            stdin_file = None

        self.logger.debug(f"Running dft_detect ({fmt}): {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                stdin=stdin_file,
                capture_output=True,
                timeout=10.0,
                text=True,
            )
        finally:
            if stdin_file:
                stdin_file.close()

        if result.returncode != 0:
            if result.returncode == 206:
                self.logger.debug(
                    f"dft_detect ({fmt}) exit code 206 (corrupted input data / "
                    f"CLI convention mismatch)"
                )
            else:
                self.logger.debug(f"dft_detect ({fmt}) returned non-zero exit code: {result.returncode}")
            if result.stderr:
                self.logger.debug(f"dft_detect ({fmt}) stderr: {result.stderr[:200]}")

        output = result.stdout + result.stderr
        results = self._parse_dft_output(output)
        return output, results

    def _run_dft_detect(self, iq_file: str, sample_rate: int) -> Dict[str, Tuple[float, float]]:
        """
        Run dft_detect on captured IQ samples, self-adapting to whichever CLI
        convention (legacy filename-arg vs. modern stdin) the installed binary
        actually expects — see the note in __init__ for why this is necessary.

        Args:
            iq_file: Path to IQ sample file
            sample_rate: Sample rate in Hz

        Returns:
            Dictionary of {sonde_type: (correlation_score, frequency_offset)}
        """
        formats_to_try = (
            [self._working_format] if self._working_format
            else ['legacy', 'modern', 'advanced']
        )

        try:
            output = ""
            results: Dict[str, Tuple[float, float]] = {}
            for fmt in formats_to_try:
                output, results = self._run_dft_detect_once(iq_file, sample_rate, fmt)
                if results:
                    if self._working_format != fmt:
                        self.logger.info(
                            f"dft_detect CLI convention detected: '{fmt}' "
                            f"(caching for subsequent calls)"
                        )
                        self._working_format = fmt
                    break

            self.logger.info(f"Correlation output: {output}")
            if results:
                self.logger.info(f"Correlation results: {results}")
            elif not output.strip():
                # Completely empty output from every CLI convention is not a
                # "weak signal" result — the binary itself is not working
                # (field: every detection then falls through to the unreliable
                # bandwidth guess, causing phantom/wrong-type decoders).
                try:
                    fsize = os.path.getsize(iq_file)
                except OSError:
                    fsize = -1
                self.logger.warning(
                    f"dft_detect produced NO output at all (formats tried: "
                    f"{', '.join(formats_to_try)}; capture file: {fsize} bytes). "
                    f"The dft_detect binary at '{self.dft_detect_path}' appears "
                    f"broken on this system — test it manually and rebuild from "
                    f"rs1729/RS (scan/dft_detect.c) if needed. Falling back to "
                    f"bandwidth-based type detection until then."
                )
            else:
                self.logger.warning(
                    "dft_detect returned no parseable results "
                    f"(tried format(s): {', '.join(formats_to_try)})"
                )

            return results

        except subprocess.TimeoutExpired:
            self.logger.error("dft_detect timed out")
            return {}
        except Exception as e:
            self.logger.error(f"Failed to run dft_detect: {e}")
            return {}
    
    def _parse_dft_output(self, output: str) -> Dict[str, Tuple[float, float]]:
        """
        Parse all dft_detect output lines and keep the strongest result per sonde type.

        Accepts variants like:
            RS41: 0.653
            RS41: 0.653, -1250
            RS41: 0.653, -1250Hz
            RS41: 0.653, -1250.0 Hz
        """
        results: Dict[str, Tuple[float, float]] = {}
        # Different dft_detect builds label the same sonde types differently
        # (e.g. "DFM9"/"IMET4" instead of "DFM"/"iMet") — accept both and
        # normalize below so real correlation output isn't silently dropped
        # just because it doesn't match our expected spelling.
        # MRZ was missing here even though config.yaml `detection.sonde_types`
        # lists it and dft_detect emits it — every MRZ correlation line was
        # silently discarded, so an MRZ could never be identified by DFT and
        # always fell through to the bandwidth guess. IMET1/IMET5 and the
        # RS41/RS92 sub-labels some builds emit (e.g. "RS41SG") are accepted
        # too; \w* absorbs any build-specific suffix before the colon.
        line_pattern = re.compile(
            r'^\s*(RS41|RS92|DFM9?|M10|M20|IMET4|IMET5|IMET1|iMet|LMS6|MRZ)\w*\s*:\s*'
            r'([+-]?\d+(?:\.\d+)?)'
            r'(?:\s*[,;]\s*([+-]?\d+(?:\.\d+)?)(?:\s*Hz)?)?\s*$',
            re.IGNORECASE,
        )

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = line_pattern.match(line)
            if not match:
                if self.debug_mode:
                    self.logger.debug(f"Ignoring unparsed dft_detect line: {line}")
                continue

            sonde_type = match.group(1)
            _t = sonde_type.lower()
            if _t in ('imet', 'imet4', 'imet1', 'imet5'):
                # Collapse the iMet variants onto the single type the rest of
                # the pipeline (decoder path lookup, config sonde_types) knows.
                sonde_type = 'iMet'
            elif _t in ('dfm', 'dfm9'):
                sonde_type = 'DFM'
            elif _t == 'mrz':
                sonde_type = 'MRZ'
            elif _t == 'lms6':
                sonde_type = 'LMS6'
            elif _t == 'rs41':
                sonde_type = 'RS41'
            elif _t == 'rs92':
                sonde_type = 'RS92'
            correlation = float(match.group(2))
            freq_offset = float(match.group(3)) if match.group(3) is not None else 0.0

            current = results.get(sonde_type)
            if current is None or correlation > current[0]:
                results[sonde_type] = (correlation, freq_offset)
                if self.debug_mode:
                    self.logger.debug(
                        f"Parsed candidate {sonde_type}: corr={correlation:.3f}, offset={freq_offset:.1f} Hz"
                    )
            elif self.debug_mode:
                self.logger.debug(
                    f"Discarded weaker {sonde_type}: corr={correlation:.3f}, offset={freq_offset:.1f} Hz"
                )

        return results

    def _select_best_match(self, results: Dict[str, Tuple[float, float]]) -> Optional[CorrelationResult]:
        """Select the strongest match above the per-type threshold."""
        best_match: Optional[CorrelationResult] = None

        for sonde_type, (correlation, freq_offset) in results.items():
            threshold = self.thresholds.get(sonde_type, 0.6)
            # CRITICAL: Use math.fabs() to handle negative correlations (M10/M20 phase inversion)
            if math.fabs(correlation) < threshold:
                if self.debug_mode:
                    self.logger.debug(
                        f"Rejecting {sonde_type}: corr={correlation:.3f} < threshold={threshold:.3f}, offset={freq_offset:.1f} Hz"
                    )
                continue

            candidate = CorrelationResult(
                sonde_type=sonde_type,
                correlation=correlation,
                frequency=freq_offset,
                bandwidth=0.0,
            )

            if self.debug_mode:
                self.logger.debug(
                    f"Acceptable candidate {sonde_type}: corr={correlation:.3f}, threshold={threshold:.3f}, offset={freq_offset:.1f} Hz"
                )

            # FIX: compare candidates by correlation MAGNITUDE, not signed value.
            # The threshold test above already uses math.fabs() because M10/M20
            # legitimately correlate NEGATIVELY (phase inversion) — a genuine M10
            # scores about -0.95. The old comparison (`candidate.correlation >
            # best_match.correlation`) was signed, so that -0.95 M10 lost to any
            # marginal positive score that also cleared its own threshold (e.g. a
            # 0.55 RS41), and the sonde was started with the wrong decoder. Using
            # fabs() on both sides makes "best" mean "strongest match", which is
            # what the threshold test already assumed.
            if best_match is None or math.fabs(candidate.correlation) > math.fabs(best_match.correlation):
                best_match = candidate

        if best_match:
            self.logger.info(
                f"select_best_match best={best_match.sonde_type} corr={best_match.correlation:.3f} offset={best_match.frequency:.1f} Hz"
            )
        elif results:
            # Promoted from debug_mode-only: when dft_detect DID produce scores
            # but none cleared its threshold, the caller silently drops to the
            # bandwidth fallback. Without this line the logs give no indication
            # of how close the correlation got, which is exactly the information
            # needed to tell "threshold slightly too high for this station" apart
            # from "no sonde there". Shows each type's score vs. its threshold.
            _detail = ", ".join(
                f"{t}={c:+.3f}/thr {self.thresholds.get(t, 0.6):.2f}"
                for t, (c, _o) in sorted(results.items())
            )
            self.logger.info(
                f"No sonde type exceeded its correlation threshold ({_detail}) "
                f"— falling back to bandwidth-based type ID"
            )

        return best_match
