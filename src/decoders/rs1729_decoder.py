"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : rs1729_decoder.py
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
#  Subprocess wrapper around the rs1729 radiosonde decoder binaries.
#
#  RS1729Decoder spawns and manages a single rs1729 decoder child process
#  (rs41mod, dfm09mod, m10mod, m20mod, rs92mod, imet54mod, lms6mod, mrzmod)
#  fed with raw 48 kHz / 16-bit signed IQ audio piped directly from rtl_fm
#  or the Airspy channelizer via stdin.
#
#  Decoder binary selection:
#    RS41 → rs41mod    DFM → dfm09mod    M10 → m10mod
#    M20  → m20mod     RS92 → rs92mod    iMet → imet54mod
#
#  Output parsing handles both JSON (--json flag, DFM) and plain-text
#  (RS41/M10/RS92) decoder output formats, including side-channel PTU
#  and GPS satellite count lines merged into subsequent frame callbacks.
#  Uses stdbuf -oL (when available) to force line-buffered child stdout.
#
# =============================================================================
"""

import shutil
import subprocess
import logging
import threading
import time
import json
import re
import os
from typing import Optional, Callable
from datetime import datetime


class RS1729Decoder:
    """
    Manages rs1729 decoder processes for various radiosonde types
    Decodes raw IQ data from stdin (48 kHz, 16-bit signed)
    Detects --softin support and uses appropriate decoder flags
    """
    
    # Mapping of sonde types to decoder binaries
    DECODER_MAP = {
        'RS41': 'rs41mod',
        'RS92': 'rs92mod',
        'DFM': 'dfm09mod',
        'M10': 'm10mod',
        'M20': 'm20mod',
        'iMet': 'imet54mod',
        'LMS6': 'lms6mod',
        'MRZ': 'mrzmod'
    }

    # External sources (Import API, manual web UI entry) may report a
    # specific sonde variant/subtype (e.g. "DFM17", "DFM09", "DFM06")
    # instead of the base family name used as the DECODER_MAP key.
    # DECODER_MAP.get('DFM17', 'rs41mod') falls through to its default —
    # observed in the field silently launching rs41mod (wrong decoder,
    # wrong flags) for a real DFM17 signal. Normalize known variants back
    # to their base family before decoder-binary selection.
    SONDE_TYPE_ALIASES = {
        'DFM06': 'DFM', 'DFM09': 'DFM', 'DFM17': 'DFM',
    }

    # m10mod/m20mod share one binary family and report which variant was
    # actually decoded via a raw hex type code in the JSON "subtype" field
    # (e.g. "0x20") instead of a friendly name the way DFM does
    # ("0xC:DFM17") — translate known codes to human-readable labels so the
    # web UI doesn't show a bare hex code where DFM shows "DFM17".
    M_SERIES_TYPE_CODES = {
        '0x20': 'M20',
        '0x9F': 'M10',
    }

    # Soft-decision decode chain parameters (auto_rx method):
    #   rtl_fm -M raw (cs16 IQ) → fsk_demod (soft symbols) → decoder --softin
    # This is ~2 dB more sensitive than direct --IQ decoding and tolerant of
    # several kHz mistuning (fsk_demod tracks the tone frequencies itself).
    # NOTE: fsk_demod requires sample_rate to be an integer multiple of the
    # baud rate — hence DFM uses 50000 (20 sps), not 48000 (19.2 sps).
    # M10/M20 intentionally NOT listed yet: their --softin support across
    # unpinned rs1729/RS builds is unverified, and their --IQ path was only
    # recently field-fixed — add them here once validated (M10 would need
    # 48080 Hz, which AudioPipeline's '<n>k' rtl_fm rate formatting can't
    # express yet).
    # fsk_extra mirrors radiosonde_auto_rx's CURRENT fsk_demod invocations
    # (autorx/decode.py, generate_decoder_command_experimental). The earlier
    # params here ('--mask 4800' for RS41, nothing for DFM) were auto_rx's
    # PRE-2025-08-26 settings — field/bench testing with this harness showed
    # that RS41 soft chain was deaf below ~30 dB SNR with them while direct --IQ
    # decoded the same signals fine. auto_rx's 2025-08-26 update ("bump mask
    # estimator to 5000 Hz, increase timing estimator duration --nsym=300, and
    # change oversampling -p 5 … improves weak signal performance") is adopted
    # below. DFM uses inverted soft bits (-i, needed for dfm09mod subtype
    # detection) and NO mask estimator (auto_rx: DFMs decode better without it).
    # Requires an fsk_demod built from current auto_rx (scripts/install_softchain.sh)
    # that accepts --nsym/-p; on an older binary the soft chain simply falls
    # back to --IQ.
    SOFT_CHAIN_PARAMS = {
        'RS41': {'sample_rate': 48000, 'baud': 4800,
                 'fsk_extra': ['--mask', '5000', '--nsym=300', '-p', '5'],
                 'freq_lower': -5000, 'freq_upper': 5000},
        'DFM':  {'sample_rate': 50000, 'baud': 2500,
                 'fsk_extra': ['-i'],
                 'freq_lower': -5000, 'freq_upper': 5000},
    }

    # Class-level cache for decoder capabilities
    _decoder_caps = {}
    _decoder_failures = {}  # Track failures per (path, type) for cooldown
    _flag_probe_cache = {}  # (path, flags) → bool from empirical flag probing

    @classmethod
    def normalize_sonde_type(cls, sonde_type: str) -> str:
        """Uppercase and map known subtype variants (e.g. 'DFM17', 'DFM09')
        back to their base family key ('DFM') used in DECODER_MAP — single
        source of truth, used both here and by device_manager.py's own
        decoder-path/cooldown lookup so the two never drift out of sync."""
        normalized = sonde_type.upper() if sonde_type else 'RS41'
        return cls.SONDE_TYPE_ALIASES.get(normalized, normalized)

    @classmethod
    def resolve_decoder_path(cls, decoder_binary: str) -> Optional[str]:
        """Locate a decoder binary: relative to the working directory first
        (matches the systemd unit's WorkingDirectory=<install dir>), then via
        PATH. Single source of truth for decoder path resolution — previously
        this list was duplicated (and had drifted slightly out of sync,
        including host-specific absolute paths like /home/pi/OpenWXSDR/...)
        between here and device_manager.py's own _get_decoder_path(); that one
        now delegates here too."""
        relative_path = f'decoders/rs1729/{decoder_binary}'
        full_path = os.path.join(os.getcwd(), relative_path)
        if os.path.isfile(full_path) or os.path.isfile(relative_path):
            return relative_path

        which_path = shutil.which(decoder_binary)
        if which_path:
            return which_path

        return None

    @classmethod
    def _detect_decoder_capabilities(cls, decoder_path: str) -> dict:
        """
        Detect decoder binary capabilities by probing --help output
        
        Args:
            decoder_path: Path to decoder binary
            
        Returns:
            Dict with capability flags: softin, json, ID, ptu, ecc, dist, sat
        """
        # Check cache first
        if decoder_path in cls._decoder_caps:
            return cls._decoder_caps[decoder_path]
        
        caps = {
            'softin': False,
            'json': False,
            'ID': False,
            'ptu': False,
            'ptu2': False,
            'ecc': False,
            'dist': False,
            'sat': False,
            'IQ': False,
            'dc': False,
            'lpIQ': False,
            'jsnsubfrm1': False,
        }
        
        try:
            # Run decoder with --help and parse supported flags
            result = subprocess.run(
                [decoder_path, '--help'],
                capture_output=True,
                text=True,
                timeout=2
            )
            help_text = result.stdout + result.stderr
            
            # Check for each capability
            caps['softin'] = '--softin' in help_text
            caps['json'] = '--json' in help_text
            caps['ID'] = '-ID' in help_text
            caps['ptu'] = '--ptu' in help_text
            caps['ptu2'] = '--ptu2' in help_text
            caps['ecc'] = '--ecc' in help_text
            caps['dist'] = '--dist' in help_text
            caps['sat'] = '--sat' in help_text
            caps['IQ'] = '--IQ' in help_text
            caps['dc'] = '--dc' in help_text
            caps['lpIQ'] = '--lpIQ' in help_text
            caps['jsnsubfrm1'] = '--jsnsubfrm1' in help_text
            
            cls._decoder_caps[decoder_path] = caps
            return caps
        except Exception:
            # If probe fails, return minimal safe capabilities
            cls._decoder_caps[decoder_path] = caps
            return caps
    
    @classmethod
    def _probe_flags_accepted(cls, decoder_path: str, flags: list, timeout: float = 3.0) -> bool:
        """
        Empirically test whether a decoder binary ACCEPTS the given flags.

        Why: rs1729 --help output under-reports supported flags on many
        builds — field-verified on this very project: rs41mod ran fine with
        --ptu2 while not listing it in --help (and m10mod/m20mod never list
        --IQ/--json/--dc at all). Trusting --help therefore wrongly disables
        features on working builds.

        Method: spawn the decoder with the flags and /dev/null on stdin.
        A build that accepts the flags reads stdin, hits immediate EOF and
        exits quietly. A build that rejects them errors out mentioning the
        option / printing usage before reading any input.
        """
        key = (decoder_path, tuple(flags))
        if key in cls._flag_probe_cache:
            return cls._flag_probe_cache[key]

        accepted = False
        try:
            with open(os.devnull, 'rb') as devnull:
                result = subprocess.run(
                    [decoder_path] + list(flags),
                    stdin=devnull,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
            output = (result.stdout + result.stderr).lower()
            rejected = (
                'unknown option' in output
                or 'invalid option' in output
                or 'unrecognized' in output
                or 'not supported' in output
                or 'usage:' in output
                or ('error' in output and 'option' in output)
            )
            accepted = not rejected
        except FileNotFoundError:
            accepted = False
        except subprocess.TimeoutExpired:
            # Kept reading stdin past EOF or hung — treat as accepted:
            # a flag-rejecting build errors out instantly, it never hangs.
            accepted = True
        except Exception:
            accepted = False

        cls._flag_probe_cache[key] = accepted
        return accepted

    @classmethod
    def _detect_softin_support(cls, decoder_path: str) -> bool:
        """
        Detect if decoder supports --softin flag (legacy method)
        
        Args:
            decoder_path: Path to decoder binary
            
        Returns:
            True if --softin is supported
        """
        caps = cls._detect_decoder_capabilities(decoder_path)
        return caps.get('softin', False)
    
    def __init__(self, frequency: float, sonde_type: str = 'RS41', decoder_path: str = None,
                 sample_rate: int = 48000, soft_decode: bool = False,
                 allow_rate_change: bool = False, iq_dc_block: bool = False):
        """
        Initialize decoder

        Args:
            frequency: Signal frequency in Hz
            sonde_type: Type of radiosonde (RS41, DFM, M10, etc.)
            decoder_path: Path to decoder executable (default: auto-detect based on sonde_type)
            sample_rate: IQ input sample rate in Hz — must match the AudioPipeline
                capturing at the same rate. Default 48000 works for RS41/DFM/RS92/iMet
                (their baud rates are low enough for 5-6x oversampling at 48 kHz), but
                M10/M20's ~9600 baud only gets 5.0 samples/symbol at 48 kHz, which
                m20mod itself flags as marginal ("note: sample rate low (5.0 sps)") —
                caller should pass a higher rate (e.g. 96000) for those types.
            soft_decode: Prefer the fsk_demod soft-bit chain (auto_rx method) when
                the sonde type is in SOFT_CHAIN_PARAMS, the decoder binary supports
                --softin and --json, and fsk_demod is installed. Falls back to the
                direct --IQ chain otherwise.
            allow_rate_change: If True, the constructor may CHANGE self.sample_rate
                to the soft chain's required rate (e.g. 50000 for DFM) — the caller
                must then create its capture pipeline using self.sample_rate AFTER
                construction (device_manager does this). If False (default), the
                soft chain is only used when its required rate equals sample_rate,
                so callers with a fixed-rate stream (Airspy, decoder_manager) are
                never fed a rate mismatch.
            iq_dc_block: If True, insert rs1729's iq_dec as an inline DC-removal
                stage ahead of the decoder (auto_rx method). In the --IQ chain:
                rtl_fm -M raw (cs16 IQ) → iq_dec → decoder --IQ. In the soft
                chain it precedes fsk_demod: rtl_fm → iq_dec → fsk_demod →
                decoder --softin. iq_dec strips the residual DC offset (the
                RTL-SDR centre spike) that sits on the sonde's centre tone.
                Silently skipped (no DC stage) when the iq_dec binary is absent.
        """
        self.frequency = frequency
        self.sonde_type = self.normalize_sonde_type(sonde_type)
        self.sample_rate = sample_rate
        
        # Get decoder binary name for this sonde type
        decoder_binary = self.DECODER_MAP.get(self.sonde_type, 'rs41mod')
        
        # Auto-detect decoder path if not specified
        if decoder_path is None:
            decoder_path = self.resolve_decoder_path(decoder_binary) or decoder_binary
        
        self.decoder_path = decoder_path
        self.logger = logging.getLogger(f'{self.sonde_type}Decoder.{frequency/1e6:.3f}')
        
        # Detect decoder capabilities comprehensively
        self.decoder_caps = self._detect_decoder_capabilities(self.decoder_path)
        self.has_softin = self.decoder_caps.get('softin', False)
        
        # Log detected capabilities for debugging
        caps_str = ', '.join(f"{k}={v}" for k, v in self.decoder_caps.items() if v)
        if caps_str:
            self.logger.info(f"Detected decoder capabilities: {caps_str}")
        
        if not self.has_softin and self.sonde_type in ['RS41', 'DFM']:
            # INFO, not WARNING: the --IQ chain is a fully-supported path and,
            # since --sat was removed from the RS41 command, it emits complete
            # PTU (temp/pressure, and humidity once the sonde's cal subframes
            # are collected) DIRECTLY in the JSON — no "text fallback", no need
            # to install different decoders. The soft chain (fsk_demod +
            # --softin) is only a ~2 dB sensitivity upgrade, not a PTU
            # requirement. This message previously scared users into thinking
            # PTU was degraded on --IQ, which is no longer true.
            self.logger.info(
                f"{self.DECODER_MAP.get(self.sonde_type)} --help does not list "
                f"--softin; using the --IQ decode chain (full JSON PTU supported). "
                f"Install fsk_demod (scripts/install_softchain.sh) only if you want "
                f"the extra ~2 dB soft-decision sensitivity."
            )

        # ------------------------------------------------------------------
        # Decode-chain selection: 'softin' (rtl_fm → fsk_demod → decoder
        # --softin, the auto_rx method) or 'iq' (direct --IQ, legacy path).
        # The soft chain requires: type in SOFT_CHAIN_PARAMS, decoder binary
        # with --softin AND --json (our frame parser needs JSON output), and
        # an fsk_demod binary. JSON gating matters because with --softin the
        # decoder's text output format differs from the --IQ text format the
        # legacy parser knows.
        # ------------------------------------------------------------------
        self.decode_chain = 'iq'
        self.fsk_process: Optional[subprocess.Popen] = None
        self.fsk_demod_path = self.resolve_decoder_path('fsk_demod')

        # Inline iq_dec DC-removal stage (auto_rx method) for the --IQ chain.
        # Resolved eagerly so start() can decide without re-probing; only used
        # when iq_dc_block is set AND the binary is present (graceful no-op).
        self.iq_dc_block = bool(iq_dc_block)
        self.iqdec_path = self.resolve_decoder_path('iq_dec')
        self.iqdec_process: Optional[subprocess.Popen] = None
        if soft_decode and self.sonde_type in self.SOFT_CHAIN_PARAMS:
            params = self.SOFT_CHAIN_PARAMS[self.sonde_type]
            # --help under-reports flags on many rs1729 builds (field-verified:
            # rs41mod accepted --ptu2 while not listing it). When --help says
            # no, verify empirically before giving up on the soft chain.
            softin_ok = self.has_softin and self.decoder_caps.get('json', False)
            if not softin_ok:
                probe_flags = ['--softin', '--json']
                if self.sonde_type == 'DFM':
                    probe_flags.append('--auto')
                softin_ok = self._probe_flags_accepted(self.decoder_path, probe_flags)
                if softin_ok:
                    self.logger.info(
                        f"{os.path.basename(self.decoder_path)} accepts "
                        f"{' '.join(probe_flags)} despite --help not listing them "
                        f"(empirical probe) — soft chain enabled"
                    )
            if not softin_ok:
                self.logger.info(
                    f"Soft decode chain unavailable for {self.sonde_type}: decoder "
                    f"rejects --softin/--json (verified by probe) — using --IQ chain"
                )
            elif not self.fsk_demod_path:
                self.logger.info(
                    "Soft decode chain unavailable: fsk_demod binary not found "
                    "(expected in decoders/rs1729/ or PATH) — using --IQ chain"
                )
            elif params['sample_rate'] != self.sample_rate and not allow_rate_change:
                self.logger.info(
                    f"Soft decode chain for {self.sonde_type} needs {params['sample_rate']} Hz "
                    f"input but stream is fixed at {self.sample_rate} Hz — using --IQ chain"
                )
            else:
                self.decode_chain = 'softin'
                self.sample_rate = params['sample_rate']
                self.logger.info(
                    f"Using fsk_demod soft-bit decode chain for {self.sonde_type} "
                    f"({self.sample_rate} Hz / {params['baud']} baud)"
                )

        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.frame_callback: Optional[Callable] = None
        self.last_frame_time: Optional[datetime] = None
        self.frame_count = 0
        self._start_time: Optional[float] = None
        self.debug_json_ptu = os.environ.get("OPENWX_JSON_PTU_DEBUG", "0").lower() in ("1", "true", "yes", "on")
        self.ptu_cache = {}  # Cache PTU data from text lines, keyed by (serial, frame_num)
        self.ptu_cache_timestamps = {}  # Track PTU data timestamps for freshness check
        self.startup_failure_count = 0  # Track immediate startup failures
        self.last_failure_time = None  # Track last failure for cooldown
        self.last_ebno_db: Optional[float] = None  # From fsk_demod --stats (soft chain only)
        self._logged_ptu_degraded_mode = False  # One-time warning for PTU fallback mode
        self._logged_ptu_source = False  # One-time INFO/WARN reporting where PTU comes from
    
    def set_frame_callback(self, callback: Callable[[dict], None]):
        """Set callback for decoded frames"""
        self.frame_callback = callback
    
    def start(self, audio_stream) -> bool:
        """
        Start decoder with IQ stream from stdin.

        Dispatches to the fsk_demod soft-bit chain (auto_rx method) when
        selected in the constructor, with automatic fallback to the direct
        --IQ chain if the soft chain fails to spawn.

        Args:
            audio_stream: Audio stream file object (rtl_fm stdout / pipeline pipe)

        Returns:
            True if decoder started successfully
        """
        if not audio_stream:
            self.logger.error("No audio stream provided")
            return False

        if self.decode_chain == 'softin':
            if self._start_softin_chain(audio_stream):
                return True
            # Fall back to direct --IQ decoding on the same live stream. The
            # capture sample rate already matches (pipeline was created from
            # self.sample_rate), so the --IQ decoder gets a consistent rate.
            self.logger.warning(
                "Soft decode chain failed to start — falling back to --IQ chain"
            )
            self._stop_fsk_process()
            # If an iq_dec DC stage was started ahead of fsk_demod, stop it too
            # so _start_iq_chain can spawn a fresh one on the same live stream.
            self._stop_iqdec()
            self.decode_chain = 'iq'

        return self._start_iq_chain(audio_stream)

    def _start_softin_chain(self, audio_stream) -> bool:
        """
        Spawn the auto_rx-style soft-decision pipeline:
            audio_stream (cs16 IQ) → [iq_dec] → fsk_demod → decoder --softin

        Same fsk_demod invocation as the field-tested KA9Q path
        (ka9q_receiver.py) and radiosonde_auto_rx. When iq_dc_block is set, an
        iq_dec DC-removal stage is inserted ahead of fsk_demod (harmless no-op
        if the binary is absent) — this is the mode-4 combination the test
        harness exercises (softin + iq_dec).
        """
        params = self.SOFT_CHAIN_PARAMS[self.sonde_type]

        fsk_cmd = [
            self.fsk_demod_path,
            '--cs16',                          # complex signed 16-bit IQ input
            '-b', str(params['freq_lower']),   # tone search lower bound (Hz)
            '-u', str(params['freq_upper']),   # tone search upper bound (Hz)
            '-s',                              # soft-decision output
        ] + params['fsk_extra'] + [
            '--stats=5',                       # JSON stats (EbNodB) every 5 s on stderr
            '2',                               # 2FSK
            str(self.sample_rate),
            str(params['baud']),
            '-', '-'                           # stdin → stdout
        ]

        cmd = [self.decoder_path]
        if self.sonde_type == 'RS41':
            # Matches the KA9Q path / auto_rx: soft bits are inverted → -i.
            # JSON output carries PTU directly in this mode (no text fallback
            # merging needed).
            cmd.extend(['--softin', '-i', '--json', '--ptu2'])
            # Optional enhancement flags: trust --help when it says yes,
            # otherwise verify empirically (--help under-reports on many
            # builds; --ecc in particular matters for weak-signal yield).
            # CRITICAL: do NOT add --sat here. In rs41mod, JSON PTU is only
            # emitted from the ec>=0 branch, where get_PTU() is guarded by
            # `!sat` (rs41mod.c line ~2279). Passing --sat silently disables
            # temp/humidity/pressure in the JSON. The JSON "sats" field comes
            # from numSV in the GPS decode and does NOT need --sat.
            for flag, cap_key in (('--jsnsubfrm1', 'jsnsubfrm1'),
                                  ('--ecc', 'ecc')):
                if self.decoder_caps.get(cap_key, False) or \
                        self._probe_flags_accepted(self.decoder_path, [flag]):
                    cmd.append(flag)
        elif self.sonde_type == 'DFM':
            # --auto handles DFM06/09/17 subtype + inversion detection.
            cmd.extend(['--auto', '--softin', '--json'])
            # --ecc: keep probe fallback (harmless, and it's in --help anyway).
            if self.decoder_caps.get('ecc', False) or \
                    self._probe_flags_accepted(self.decoder_path, ['--ecc']):
                cmd.append('--ecc')
            # --dist/--ptu/-ID: --help ONLY, NO probe — field dfm09mod builds
            # reject them (exit 255) and the probe false-positives them, which
            # killed every DFM decode from 2026-07-21 on. See the matching note
            # in _start_iq_chain's DFM branch.
            for flag, cap_key in (('--dist', 'dist'), ('--ptu', 'ptu'), ('-ID', 'ID')):
                if self.decoder_caps.get(cap_key, False):
                    cmd.append(flag)
        else:
            # Type not in SOFT_CHAIN_PARAMS — shouldn't happen (constructor
            # gates decode_chain), but never crash into it.
            return False

        stdbuf = shutil.which('stdbuf')
        if stdbuf:
            cmd = [stdbuf, '-oL', '-eL'] + cmd

        self.logger.info(f"Starting fsk_demod: {' '.join(fsk_cmd)}")
        self.logger.info(f"Starting decoder (softin): {' '.join(cmd)}")

        try:
            # Optional iq_dec DC stage feeds fsk_demod; passthrough if disabled.
            fsk_stdin = self._maybe_start_iqdec(audio_stream)
            self.fsk_process = subprocess.Popen(
                fsk_cmd,
                stdin=fsk_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            self.process = subprocess.Popen(
                cmd,
                stdin=self.fsk_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                universal_newlines=False
            )
            # Close fsk_demod stdout in the parent so the decoder sees EOF
            # when fsk_demod exits (same pattern as ka9q_receiver.py).
            self.fsk_process.stdout.close()
            # Likewise close iq_dec's stdout in the parent so fsk_demod sees EOF
            # when iq_dec exits.
            if self.iqdec_process is not None:
                try:
                    self.iqdec_process.stdout.close()
                except Exception:
                    pass
        except FileNotFoundError as e:
            self.logger.error(f"Soft chain binary not found: {e}")
            self._stop_iqdec()
            return False
        except Exception as e:
            self.logger.error(f"Failed to spawn soft decode chain: {e}")
            self._stop_iqdec()
            return False

        ok = self._confirm_startup(cmd)
        if not ok:
            self._stop_iqdec()
        return ok

    def _stop_fsk_process(self):
        """Terminate the fsk_demod process, if any."""
        if self.fsk_process:
            try:
                self.fsk_process.terminate()
                self.fsk_process.wait(timeout=2)
            except Exception:
                try:
                    self.fsk_process.kill()
                    self.fsk_process.wait(timeout=2)
                except Exception:
                    pass
            self.fsk_process = None

    def _maybe_start_iqdec(self, audio_stream):
        """Optionally insert rs1729's iq_dec as an inline DC-removal stage ahead
        of the --IQ decoder (auto_rx method):

            rtl_fm -M raw (cs16 IQ) → iq_dec → decoder --IQ

        iq_dec removes the residual DC offset in the 16-bit IQ (the RTL-SDR
        centre spike) that otherwise lands right on the sonde's centre tone.
        Returns the stream the decoder should read from: iq_dec's stdout when
        the stage is active, otherwise the original audio_stream unchanged.

        Command mirrors auto_rx's get_sdr_iq_cmd(dc_block=True):
            iq_dec --bo 16 [--IFbw <rate_kHz>] - <sample_rate> 16
        --IFbw is only added for wideband streams (>80 kHz), matching auto_rx —
        our RS41/DFM rates (48/50 kHz) are narrowband so it is omitted.
        """
        if not self.iq_dc_block:
            return audio_stream
        if not self.iqdec_path:
            self.logger.info(
                "iq_dc_block enabled but iq_dec binary not found (expected in "
                "decoders/rs1729/ or PATH) — using direct --IQ without DC removal"
            )
            return audio_stream

        cmd = [self.iqdec_path, '--bo', '16']
        if self.sample_rate > 80000:
            cmd += ['--IFbw', str(self.sample_rate // 1000)]
        cmd += ['-', str(self.sample_rate), '16']

        self.logger.info(f"Starting iq_dec DC-removal stage: {' '.join(cmd)}")
        try:
            self.iqdec_process = subprocess.Popen(
                cmd,
                stdin=audio_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,   # matches auto_rx '2>/dev/null'
                bufsize=0,
            )
        except FileNotFoundError:
            self.logger.warning(
                f"iq_dec not spawnable at {self.iqdec_path} — using direct --IQ"
            )
            self.iqdec_process = None
            return audio_stream
        except Exception as e:
            self.logger.warning(f"Failed to start iq_dec ({e}) — using direct --IQ")
            self.iqdec_process = None
            return audio_stream

        return self.iqdec_process.stdout

    def _stop_iqdec(self):
        """Terminate the iq_dec DC-removal process, if any."""
        if self.iqdec_process:
            try:
                self.iqdec_process.terminate()
                self.iqdec_process.wait(timeout=2)
            except Exception:
                try:
                    self.iqdec_process.kill()
                    self.iqdec_process.wait(timeout=2)
                except Exception:
                    pass
            self.iqdec_process = None

    def _start_iq_chain(self, audio_stream) -> bool:
        """Start the legacy direct --IQ decoder chain (decoder reads raw IQ)."""
        try:
            # Build decoder command based on sonde type
            # Different decoders have different command-line options
            # NOTE: RS/demod/mod decoders (rs41mod, dfm09mod) support --json with full telemetry
            cmd = [self.decoder_path]
            
            # Add decoder-specific flags
            if self.sonde_type == 'RS41':
                # RS41: rs41mod with --json and --ptu2 for full telemetry including PTU
                # -vv: VERY verbose (needed to get PTU in text when using --json)
                # --ptu2: PTU sensor data (temp/humidity/pressure)
                # --json: JSON output with position/velocity
                # CRITICAL: --sat REMOVED. In rs41mod the JSON PTU block calls
                # get_PTU() only under `!sat` (rs41mod.c ~2279), so --sat
                # silently disabled temp/humidity/pressure in JSON — the exact
                # cause of "no PTU" in the field logs. The JSON "sats" field is
                # numSV from the GPS decode and does NOT depend on --sat.
                cmd.extend(['-vv', '--ptu2', '--json'])
                # Error correction (Reed-Solomon): recovers weak-but-repairable
                # frames AND rejects corrupted ones. Field evidence for the
                # latter: a desk RS41 without GPS lock produced random Atlantic
                # coordinates jumping 400 km/frame — CRC-failed GPS data passed
                # straight through because --ecc was never added (--help
                # under-reports it). Probe empirically: prefer --ecc2
                # (stronger), fall back to --ecc.
                if self._probe_flags_accepted(self.decoder_path, ['--ecc2']):
                    cmd.append('--ecc2')
                elif self.decoder_caps.get('ecc', False) or \
                        self._probe_flags_accepted(self.decoder_path, ['--ecc']):
                    cmd.append('--ecc')
                else:
                    self.logger.warning(
                        "rs41mod accepts neither --ecc2 nor --ecc — corrupted "
                        "frames will pass through undetected and weak frames "
                        "won't be recovered. Update decoders/rs1729 binaries."
                    )
                cmd.extend(['--IQ', '0.0', '-', str(self.sample_rate), '16'])
                # Only warn if --ptu2 is GENUINELY unsupported — verified by an
                # empirical probe, not by --help (which under-reports: rs41mod
                # runs --ptu2 fine on builds whose --help never lists it, so the
                # old "not in --help → warn" check cried wolf on every normal
                # install). We still pass --ptu2 above unconditionally; this is
                # purely a diagnostic for the rare truly-old binary.
                if not self.decoder_caps.get('ptu2', False) and \
                        not self._probe_flags_accepted(self.decoder_path, ['--ptu2']):
                    self.logger.warning(
                        "rs41mod rejects --ptu2 (verified by probe) — PTU "
                        "(temp/humidity/pressure) will be unavailable. Update the "
                        "decoders/rs1729 binaries (scripts/install_softchain.sh)."
                    )
            elif self.sonde_type == 'DFM':
                # DFM: dfm09mod with IQ input mode
                # --auto: Automatic DFM subtype detection (DFM06/DFM09/DFM17) -
                # CRITICAL for correct detection. --auto and --IQ work on all
                # field builds even though dfm09mod --help under-reports them.
                cmd.extend(['--auto', '-vv', '--IQ', '0.0'])

                # --ecc/--json ARE listed by --help on the field builds; keep the
                # probe fallback for the rare under-reporting binary.
                for flag, cap_key in (('--ecc', 'ecc'), ('--json', 'json')):
                    if self.decoder_caps.get(cap_key, False) or \
                            self._probe_flags_accepted(self.decoder_path, [flag]):
                        cmd.append(flag)

                # CRITICAL: --dist/--ptu/-ID are gated on --help (decoder_caps)
                # ONLY — NO empirical probe. Field dfm09mod builds REJECT these
                # (exit 255 → DFM decode completely dead, observed 2026-07-21
                # onward), yet _probe_flags_accepted false-positived them: the
                # binary neither prints "usage"/"unknown option" for them nor
                # exits cleanly, so the probe wrongly judged them accepted and
                # every DFM decode died at startup. The known-good command on
                # 2026-07-17 was exactly '--auto -vv --IQ 0.0 --ecc --json'.
                # Only add these when the binary explicitly documents them.
                for flag, cap_key in (('--dist', 'dist'), ('--ptu', 'ptu'), ('-ID', 'ID')):
                    if self.decoder_caps.get(cap_key, False):
                        cmd.append(flag)
                if not self.decoder_caps.get('ID', False):
                    self.logger.info("dfm09mod --help lacks -ID; serial may be masked "
                                     "(not probed — probing it breaks this build)")

                # Add verbosity and input parameters
                cmd.extend(['-', str(self.sample_rate), '16'])
            elif self.sonde_type in ('M10', 'M20'):
                # M10/M20: m10mod/m20mod.
                # CRITICAL: --dc/--ptu/--json/--IQ/--lpIQ are all undocumented on
                # this decoder family — `m10mod --help` only ever lists
                # `-r, --raw` / `-c, --color`, the same way `--IQ` itself never
                # shows up there either despite being required and working.
                # The old capability probe (decoder_caps.get('dc'/'ptu'/'json'/
                # 'lpIQ', False)) therefore NEVER found these flags "supported"
                # and silently dropped all of them on every install, leaving
                # only the bare '-v --IQ 0.0 - <rate> 16' — this is very likely
                # why M20 produced zero frames in the field despite a strong,
                # confirmed-genuine signal. Field-proven fix (multiple gateway
                # operators, incl. several French M10/M20 stations): always
                # pass these flags unconditionally, matching rs41mod/dfm09mod
                # which DO document their extra flags via --help and keep
                # capability gating.
                cmd.extend(['-v', '--dc', '--ptu', '--json', '--IQ', '0.0', '--lpIQ',
                            '-', str(self.sample_rate), '16'])
            elif self.sonde_type == 'RS92':
                # RS92: rs92mod -v [-e <ephemeris>] --IQ 0.0 - 48000 16
                # RS92 needs the current GPS-day broadcast ephemeris (RINEX) to
                # solve a position. If the opt-in downloader has today's file
                # (config rs92.ephemeris_download, see src/sdr/ephemeris.py) pass
                # it with -e; otherwise decode as before (position may be absent).
                try:
                    from ..sdr.ephemeris import rs92_today_file
                    _ephem = rs92_today_file()
                except Exception:
                    _ephem = None
                cmd.append('-v')
                if _ephem:
                    cmd.extend(['-e', _ephem])
                    self.logger.info(f"RS92: using GPS ephemeris {_ephem}")
                cmd.extend(['--IQ', '0.0', '-', str(self.sample_rate), '16'])
            elif self.sonde_type == 'iMet':
                # iMet: imet54mod -v --IQ 0.0 - 48000 16
                cmd.extend(['-v', '--IQ', '0.0', '-', str(self.sample_rate), '16'])
            else:
                # Default: assume RS41-like syntax (no --sat — it disables JSON PTU)
                cmd.extend(['-vv', '--ptu2', '--json', '--IQ', '0.0', '-', str(self.sample_rate), '16'])
            
            # Wrap with stdbuf (if available) to force line-buffered stdout AND stderr
            # on the child process.  Without this, libc switches to block-buffering
            # when stdout/stderr are pipes → PTU data on stderr arrives too late!
            # -oL: line-buffered stdout (for JSON frames)
            # -eL: line-buffered stderr (for PTU text lines - CRITICAL!)
            stdbuf = shutil.which('stdbuf')
            if stdbuf:
                cmd = [stdbuf, '-oL', '-eL'] + cmd

            self.logger.info(f"Starting decoder: {' '.join(cmd)}")

            # Optionally route rtl_fm's raw cs16 IQ through iq_dec first for
            # auto_rx-style inline DC removal. Returns audio_stream untouched
            # when the stage is disabled/unavailable, so the direct-pipe
            # topology is preserved by default.
            decoder_stdin = self._maybe_start_iqdec(audio_stream)

            # Start decoder with stdin piped from rtl_fm / iq_dec / Airspy channelizer
            # bufsize=0 (unbuffered binary) is critical: the decoder reads raw int16 IQ
            # bytes; universal_newlines must be False to keep stdin in binary mode.
            self.process = subprocess.Popen(
                cmd,
                stdin=decoder_stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                universal_newlines=False
            )

            # If iq_dec is in the chain, close its stdout in the parent so the
            # decoder sees EOF when iq_dec exits (same pattern as fsk_demod).
            if self.iqdec_process is not None:
                try:
                    self.iqdec_process.stdout.close()
                except Exception:
                    pass

            ok = self._confirm_startup(cmd)
            if not ok:
                # Tear down the iq_dec stage on decoder startup failure so it
                # doesn't linger holding the pipe / CPU.
                self._stop_iqdec()
            return ok

        except FileNotFoundError:
            self.logger.error(f"Decoder not found: {self.decoder_path}. Install rs1729 decoder tools.")
            self._stop_iqdec()
            return False
        except Exception as e:
            self.logger.error(f"Failed to start decoder: {e}")
            self._stop_iqdec()
            return False

    def _record_startup_failure(self, exit_code: int, cmd: list, phase: str):
        """Common bookkeeping for a decoder that died during startup."""
        self.startup_failure_count += 1
        self.last_failure_time = time.time()
        self.logger.error(
            f"Decoder {phase} with code {exit_code} (failure #{self.startup_failure_count})"
        )
        self.logger.error(f"Failed command: {' '.join(cmd)}")

        # If the soft chain was involved, surface fsk_demod's stderr — the
        # decoder often dies with a generic code when fsk_demod is the real
        # culprit (bad args, unsupported build).
        if self.fsk_process:
            fsk_exit = self.fsk_process.poll()
            if fsk_exit is not None:
                try:
                    fsk_err = self.fsk_process.stderr.read(500).decode('utf-8', errors='replace')
                except Exception:
                    fsk_err = ''
                self.logger.error(
                    f"fsk_demod also exited (code {fsk_exit}): {fsk_err.strip()[:300]}"
                )

        # Track failure for cooldown
        failure_key = (self.decoder_path, self.sonde_type)
        if failure_key not in self._decoder_failures:
            self._decoder_failures[failure_key] = []
        self._decoder_failures[failure_key].append(time.time())

    def _confirm_startup(self, cmd: list) -> bool:
        """Shared post-spawn health checks + monitor-thread startup for both
        decode chains. Returns True when the decoder survives 2.5 s."""
        # Wait briefly to check startup
        time.sleep(0.5)
        if self.process.poll() is not None:
            self._record_startup_failure(self.process.poll(), cmd, 'exited immediately')
            return False

        self.running = True
        self._start_time = time.time()

        # Start threads to monitor stdout and stderr
        self.stdout_thread = threading.Thread(
            target=self._monitor_stdout,
            daemon=True
        )
        self.stderr_thread = threading.Thread(
            target=self._monitor_stderr,
            daemon=True
        )

        self.stdout_thread.start()
        self.stderr_thread.start()

        if self.fsk_process is not None:
            self.fsk_stderr_thread = threading.Thread(
                target=self._monitor_fsk_stderr,
                daemon=True
            )
            self.fsk_stderr_thread.start()

        self.logger.info(f"Decoder started - processing {self.frequency/1e6:.4f} MHz")

        # Monitor for early crashes
        time.sleep(2.0)
        if self.process.poll() is not None:
            self.running = False
            self._record_startup_failure(self.process.poll(), cmd, 'crashed early')
            return False

        self.logger.info(f"Decoder healthy after 2s startup, PID={self.process.pid}")

        return True

    def _monitor_fsk_stderr(self):
        """Monitor fsk_demod stderr: log startup lines, extract EbNodB from
        the periodic --stats JSON for signal-quality tracking."""
        if not self.fsk_process or not self.fsk_process.stderr:
            return
        line_count = 0
        try:
            for raw_line in self.fsk_process.stderr:
                if not self.running:
                    break
                line = raw_line.decode('utf-8', errors='replace').strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line:
                    continue
                line_count += 1
                if line.startswith('{'):
                    # Periodic stats JSON: {"secs":..,"EbNodB": 12.3,"ppm":..}
                    try:
                        stats = json.loads(line)
                        ebno = stats.get('EbNodB')
                        if ebno is not None:
                            self.last_ebno_db = float(ebno)
                            self.logger.debug(f"fsk_demod EbNodB={ebno}, ppm={stats.get('ppm')}")
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                elif line_count <= 5:
                    self.logger.info(f"fsk_demod stderr [{line_count}]: {line}")
                else:
                    self.logger.debug(f"fsk_demod stderr: {line}")
        except Exception as e:
            if self.running:
                self.logger.debug(f"fsk_demod stderr monitor stopped: {e}")
    
    def stop(self):
        """Stop decoder (and fsk_demod, if the soft chain is active)"""
        self.running = False

        # Stop upstream stages (fsk_demod / iq_dec) first so the decoder sees
        # EOF on stdin and can flush/exit cleanly before being terminated.
        self._stop_fsk_process()
        self._stop_iqdec()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            self.process = None

        self.logger.info("Decoder stopped")
    
    def is_alive(self) -> bool:
        """Check if decoder is still running"""
        if not self.process:
            return False
        
        exit_code = self.process.poll()
        if exit_code is not None:
            if exit_code != 0:
                self.logger.warning(f"Decoder exited with code {exit_code}")
            return False
        
        return True
    
    def is_idle(self, idle_threshold: int = 300) -> bool:
        """Check if decoder hasn't received frames recently."""
        if not self.last_frame_time:
            # No frames ever received — idle if the decoder has been running
            # longer than the idle threshold (start_time tracked via frame_count==0)
            if not hasattr(self, '_start_time') or self._start_time is None:
                return False
            return (time.time() - self._start_time) > idle_threshold

        # Frames were received before; check how long ago the last one was
        time_since_last = (datetime.now() - self.last_frame_time).total_seconds()
        return time_since_last > idle_threshold
    
    def get_frame_stats(self) -> dict:
        """Get decoder statistics"""
        return {
            'frequency': self.frequency,
            'frame_count': self.frame_count,
            'last_frame': self.last_frame_time.isoformat() if self.last_frame_time else None,
            'running': self.running and self.is_alive(),
            'startup_failures': self.startup_failure_count,
            'last_failure': self.last_failure_time,
            'decode_chain': self.decode_chain,
            'ebno_db': self.last_ebno_db
        }
    
    @classmethod
    def should_retry_decoder(cls, decoder_path: str, sonde_type: str, cooldown_seconds: int = 60) -> bool:
        """
        Check if decoder should be retried based on recent failure history
        
        Args:
            decoder_path: Path to decoder binary
            sonde_type: Type of radiosonde
            cooldown_seconds: Minimum seconds between retry attempts
            
        Returns:
            True if decoder can be retried, False if in cooldown
        """
        failure_key = (decoder_path, sonde_type)
        if failure_key not in cls._decoder_failures:
            return True
        
        failures = cls._decoder_failures[failure_key]
        if not failures:
            return True
        
        # Check if last failure is outside cooldown window
        last_failure = failures[-1]
        time_since_failure = time.time() - last_failure
        
        if time_since_failure < cooldown_seconds:
            return False
        
        # Clean up old failures (keep last 10)
        cls._decoder_failures[failure_key] = failures[-10:]
        return True
    
    def _monitor_stdout(self):
        """Monitor decoder stdout. Prefer JSON frames, use text lines only as PTU fallback/debug."""
        if not self.process or not self.process.stdout:
            self.logger.error("stdout monitoring: no process or stdout available")
            return

        line_count = 0
        try:
            for raw_line in self.process.stdout:
                if not self.running:
                    self.logger.info(f"stdout monitoring: stopped (running=False) after {line_count} lines")
                    break

                line = raw_line.decode('utf-8', errors='replace').strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line:
                    continue
                
                line_count += 1

                if line.startswith('{') and line.endswith('}'):
                    try:
                        json_data = json.loads(line)
                        # Basic validation: ensure critical fields exist
                        if not isinstance(json_data, dict):
                            continue
                        frame = self._parse_json_frame(json_data)
                        if frame and self.frame_callback:
                            self.frame_count += 1
                            self.last_frame_time = datetime.now()
                            self.frame_callback(frame)
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"Invalid JSON from decoder: {e}")
                    except Exception as e:
                        self.logger.error(f"Error processing decoder frame: {e}")
                    continue

                # Non-JSON lines: extract PTU data
                if self.debug_json_ptu:
                    self.logger.info(f"Decoder stdout (non-JSON): {line}")
                try:
                    self._extract_ptu_from_text(line)
                except Exception as e:
                    self.logger.debug(f"PTU text fallback parse failed: {e}")
        except Exception as e:
            if self.running:
                self.logger.error(f"Error monitoring decoder stdout: {e}")
    def _monitor_stderr(self):
        """Monitor decoder stderr mainly for debug and legacy PTU text fallback."""
        if not self.process or not self.process.stderr:
            self.logger.warning("stderr monitoring: no process or stderr available")
            return
        
        stderr_line_count = 0
        try:
            for raw_line in self.process.stderr:
                if not self.running:
                    self.logger.info(f"stderr monitoring stopped after {stderr_line_count} lines")
                    break
                
                line = raw_line.decode('utf-8', errors='replace').strip() if isinstance(raw_line, bytes) else raw_line.strip()
                if not line:
                    continue
                
                stderr_line_count += 1
                
                # Always log the first 10 stderr lines for debugging
                if stderr_line_count <= 10:
                    self.logger.info(f"Decoder stderr [{stderr_line_count}]: {line}")
                
                # Try to extract PTU data from this line
                try:
                    self._extract_ptu_from_text(line)
                except Exception as e:
                    if self.debug_json_ptu:
                        self.logger.debug(f"PTU extraction failed: {e}")
                
                # Log all stderr lines when debug mode is on
                if self.debug_json_ptu and stderr_line_count > 10:
                    self.logger.info(f"Decoder stderr: {line}")
        
        except Exception as e:
            self.logger.error(f"Error monitoring decoder stderr: {e}", exc_info=True)

    def _extract_ptu_from_text(self, line: str):
        """Legacy PTU fallback from verbose text output, cached with serial + timestamp.
        
        RS41 text format with -vv --ptu2:
        [ 5644] (W4060809)  Mon 2026-06-08 05:06:05.997  lat: 52.89519 lon: 7.89611 alt: 24350.9  vH: 10.4 D: 294.0 vV: 6.3  T=-47.6°C RH=5.8% P=24.38hPa
        
        Caches by (serial, frame_num) to prevent cross-contamination when multiple sondes are decoded.
        """
        # Extract frame number
        frame_match = re.search(r'\[\s*(\d+)\]', line)
        if not frame_match:
            return
        frame_num = int(frame_match.group(1))
        
        # Extract sonde serial (in parentheses)
        serial_match = re.search(r'\(([A-Z0-9]+)\)', line)
        if not serial_match:
            return  # Need serial for proper cache keying
        sonde_serial = serial_match.group(1)
        
        ptu = {}

        # Match T=-47.6°C or T=-47.6
        m = re.search(r'T=([+-]?\d+(?:\.\d+)?)', line)
        if m:
            ptu['temp'] = float(m.group(1))
        # Match humidity. CRITICAL: in --ptu2 mode rs41mod prints "RH2=5.8%"
        # (the improved humidity) and SUPPRESSES the plain "_RH=" line, so a
        # regex for "RH=" alone never matches and humidity is lost. Accept
        # _RH=, RH=, and RH2= (prefer whichever is present).
        m = re.search(r'(?:_?RH2?)=(\d+(?:\.\d+)?)', line)
        if m:
            ptu['humidity'] = float(m.group(1))
        # Match P=24.38hPa or P=24.38
        m = re.search(r'P=(\d+(?:\.\d+)?)', line)
        if m:
            ptu['pressure'] = float(m.group(1))

        if ptu:
            # Store with current timestamp, keyed by (serial, frame) to prevent cross-contamination
            now = time.time()
            cache_key = (sonde_serial, frame_num)
            self.ptu_cache[cache_key] = ptu
            self.ptu_cache_timestamps[cache_key] = now
            
            # Cleanup old cache entries aggressively to prevent memory growth
            if len(self.ptu_cache) > 100:  # Higher limit for multi-sonde scenarios
                # Remove entries older than 10 seconds
                cutoff_time = now - 10.0
                expired_keys = [k for k, t in self.ptu_cache_timestamps.items() if t < cutoff_time]
                for k in expired_keys:
                    self.ptu_cache.pop(k, None)
                    self.ptu_cache_timestamps.pop(k, None)
                    
            if self.debug_json_ptu:
                self.logger.info(f"Cached fallback PTU for {sonde_serial} frame {frame_num}: {ptu}")
    
    def _parse_json_frame(self, json_data: dict) -> Optional[dict]:
        """Parse decoder JSON output. JSON is the primary source for PTU and navigation data."""
        try:
            sonde_id = str(json_data.get('id') or json_data.get('serial') or '').strip()
            if not sonde_id:
                return None

            lat = json_data.get('lat')
            lon = json_data.get('lon')
            alt = json_data.get('alt')
            frame_num = json_data.get('frame')
            if lat is None or lon is None or alt is None or frame_num is None:
                return None

            # Normalize sonde type (handle hex values from decoder output)
            sonde_type = str(json_data.get('type') or self.sonde_type).strip()
            if sonde_type.startswith('0x'):
                # Decoder returned hex type code - use configured sonde_type
                sonde_type = self.sonde_type
            
            # Strip any existing prefixes from sonde_id (M10-, M20-, DFM-, iMet-, etc.)
            # and keep only the actual serial number for all output streams
            for prefix in ['M10-', 'M20-', 'DFM-', 'iMet-', 'IMET-', 'LMS6-', 'MRZ-']:
                if sonde_id.startswith(prefix):
                    sonde_id = sonde_id[len(prefix):]
                    break
            
            # For DFM: also strip leading 'D' if the rest is numeric
            if sonde_type == 'DFM' and sonde_id.startswith('D') and sonde_id[1:].isdigit():
                sonde_id = sonde_id[1:]

            # CRITICAL: a newly-detected DFM reports an all-'x' placeholder
            # serial ("xxxxxxxx") for the first several frames — the real
            # serial is spread across multiple sub-frames and isn't fully
            # decoded yet. Forwarding this as telemetry creates a phantom
            # "xxxxxxxx" entry in the Active Radiosondes panel that only
            # gets replaced once the real serial resolves. Log it (position/
            # altitude may already be valid) but don't emit it as telemetry.
            if sonde_type == 'DFM' and re.fullmatch(r'[xX]+', sonde_id):
                self.logger.info(
                    f"DFM frame #{frame_num}: serial not yet decoded (placeholder "
                    f"'{sonde_id}'), lat={json_data.get('lat')} lon={json_data.get('lon')} "
                    f"alt={json_data.get('alt')} — not submitting as telemetry until ID resolves"
                )
                return None

            decoded_datetime = None
            dt_raw = json_data.get('datetime')
            if dt_raw:
                try:
                    dt_str = dt_raw.rstrip('Z')
                    fmt = '%Y-%m-%dT%H:%M:%S.%f' if '.' in dt_str else '%Y-%m-%dT%H:%M:%S'
                    decoded_datetime = datetime.strptime(dt_str, fmt)
                except Exception:
                    pass

            frame = {
                'sonde_id': sonde_id,
                'sonde_type': sonde_type,
                'frame_number': int(frame_num),
                'frequency': self.frequency,
                'lat': float(lat),
                'lon': float(lon),
                'alt': float(alt),
                'decoded_datetime': decoded_datetime,
            }
            
            # Validate critical coordinate bounds
            if not (-90 <= frame['lat'] <= 90):
                self.logger.warning(f"Invalid latitude {frame['lat']} for {sonde_id}, skipping frame")
                return None
            if not (-180 <= frame['lon'] <= 180):
                self.logger.warning(f"Invalid longitude {frame['lon']} for {sonde_id}, skipping frame")
                return None
            if frame['alt'] < -1000 or frame['alt'] > 50000:
                self.logger.warning(f"Invalid altitude {frame['alt']}m for {sonde_id}, skipping frame")
                return None

            # Optional fields – only include when present in this JSON frame
            # Parse DFM subtype format: "0xC:DFM17" → subtype="DFM17", dfmcode="0xC"
            subtype_handled = False
            if self.sonde_type == 'DFM' and json_data.get('subtype'):
                raw_subtype = str(json_data.get('subtype'))
                if ':' in raw_subtype:
                    # Split "0xC:DFM17" format
                    parts = raw_subtype.split(':', 1)
                    frame['dfmcode'] = parts[0]  # "0xC"
                    frame['subtype'] = parts[1]  # "DFM17"
                    subtype_handled = True
                else:
                    frame['subtype'] = raw_subtype
                    subtype_handled = True
            elif self.sonde_type in ('M10', 'M20') and json_data.get('subtype'):
                # Unlike DFM's "0xC:DFM17", m10mod/m20mod report only the bare
                # hex code (e.g. "0x20") with no friendly name attached —
                # translate known codes so the web UI shows "M20" the same
                # way it shows "DFM17", instead of a bare hex code.
                raw_subtype = str(json_data.get('subtype'))
                translated = self.M_SERIES_TYPE_CODES.get(raw_subtype)
                if translated:
                    frame['subtype'] = translated
                subtype_handled = True

            for src, dst, cast in [
                ('vel_h', 'velocity_horizontal', float),
                ('vel_v', 'velocity_vertical', float),
                ('heading', 'heading', float),
                ('sats', 'sats', int),
                ('batt', 'battery', float),
                ('bt', 'burst_timer', int),
                ('subtype', 'subtype', str),
                ('rs41_mainboard', 'rs41_mainboard', str),
                ('rs41_mainboard_fw', 'rs41_mainboard_fw', int),
                ('tx_frequency', 'tx_frequency', float),
                ('ref_datetime', 'ref_datetime', str),
                ('ref_position', 'ref_position', str),
            ]:
                if dst == 'subtype' and subtype_handled:
                    continue
                value = json_data.get(src)
                # Defensive catch-all: never surface a raw hex placeholder
                # code as if it were a human-readable subtype, regardless of
                # sonde type.
                if dst == 'subtype' and isinstance(value, str) and value.startswith('0x'):
                    continue
                if value is not None:
                    try:
                        frame[dst] = cast(value)
                    except (TypeError, ValueError):
                        pass

            for field in ('temp', 'tempc', 'temperature', 'T'):
                if json_data.get(field) is not None:
                    try:
                        frame['temp'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass
            for field in ('humidity', 'humidityrh', 'rh', 'RH'):
                if json_data.get(field) is not None:
                    try:
                        frame['humidity'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass
            for field in ('pressure', 'pressurehpa', 'pres', 'P'):
                if json_data.get(field) is not None:
                    try:
                        frame['pressure'] = float(json_data.get(field))
                        break
                    except (TypeError, ValueError):
                        pass

            # -------------------------------------------------------------
            # PTU: JSON is authoritative and accurate. rs41mod (--ptu2 --json,
            # no --sat) prints each PTU field in JSON ONLY when it's valid
            # (temp > -273, humidity >= 0, pressure > 0), so a field being
            # absent means "not available this frame" — NOT an error. Common
            # cases: non-SGP sondes have no pressure sensor; humidity can lag
            # until RH cal completes. The old logic required ALL THREE to be
            # truthy (and 0.0 counted as missing), so it discarded perfectly
            # good JSON temp/humidity and fell back to the less accurate text
            # path on essentially every frame. Now: keep every JSON field the
            # decoder gave us, and fill ONLY genuinely-missing fields from the
            # recent text-PTU cache. is-not-None everywhere so 0°C / 0% are kept.
            # -------------------------------------------------------------
            json_fields = [k for k in ('temp', 'humidity', 'pressure') if frame.get(k) is not None]
            missing = [k for k in ('temp', 'humidity', 'pressure') if frame.get(k) is None]
            filled_from_text = []

            if missing:
                # Serial-aware fallback: nearest fresh same-serial cached text PTU
                current_frame = frame['frame_number']
                current_serial = frame['sonde_id']
                best_match = None
                best_distance = 999999
                now = time.time()
                freshness_window = 5.0  # seconds

                for (cached_serial, cached_frame), timestamp in self.ptu_cache_timestamps.items():
                    if cached_serial != current_serial:
                        continue
                    if now - timestamp > freshness_window:
                        continue
                    frame_distance = abs(cached_frame - current_frame)
                    if frame_distance <= 3 and frame_distance < best_distance:
                        best_match = (cached_serial, cached_frame)
                        best_distance = frame_distance

                if best_match is not None:
                    cached = self.ptu_cache[best_match]
                    for k in missing:
                        v = cached.get(k)
                        if v is not None:
                            frame[k] = v
                            filled_from_text.append(k)

            if json_fields:
                ptu_source = 'json' if not filled_from_text else 'json+text'
            elif filled_from_text:
                ptu_source = 'text_fallback'
            else:
                ptu_source = 'none'

            # One-time visibility on where PTU is actually coming from. INFO when
            # JSON is supplying PTU (the good case); WARN only if the decoder
            # emitted NO PTU in JSON at all (points to a flag/build problem).
            if not self._logged_ptu_source and ptu_source != 'none':
                self._logged_ptu_source = True
                if json_fields:
                    extra = f" (+text for {filled_from_text})" if filled_from_text else ""
                    self.logger.info(
                        f"PTU source for {self.sonde_type}: JSON {json_fields}{extra} — "
                        f"accurate JSON telemetry in use."
                    )
                else:
                    self.logger.warning(
                        f"PTU from TEXT fallback only ({filled_from_text}) — JSON carried no "
                        f"temp/humidity/pressure. Verify decoder flags: '--ptu2 --json' present "
                        f"and '--sat' absent (--sat disables JSON PTU)."
                    )

            # Add PTU source tag to frame for quality tracking
            frame['ptu_source'] = ptu_source

            if self.debug_json_ptu:
                self.logger.info(
                    f"[PTU] {frame['sonde_id']} frame {frame['frame_number']} source={ptu_source} "
                    f"T={frame.get('temp')} RH={frame.get('humidity')} P={frame.get('pressure')}"
                )

            if self.debug_json_ptu:
                ptu_keys = {k: json_data.get(k) for k in ('temp', 'tempc', 'temperature', 'T', 'humidity', 'humidityrh', 'rh', 'RH', 'pressure', 'pressurehpa', 'pres', 'P') if k in json_data}
                self.logger.info(f"JSON frame keys={sorted(json_data.keys())}")
                self.logger.info(f"JSON PTU candidate fields for {sonde_id}: {ptu_keys}")
                final_ptu = {k: frame.get(k) for k in ('temp', 'humidity', 'pressure') if frame.get(k) is not None}
                self.logger.info(f"Final frame PTU for {sonde_id} frame {frame['frame_number']} (source={ptu_source}): {final_ptu}")

            return frame
        except Exception as e:
            self.logger.error(f"Error parsing JSON frame: {e}")
            return None

    def _parse_frame(self, line: str) -> Optional[dict]:  # noqa: legacy helper
        """Legacy text-frame parser retained as fallback/debug helper."""
        try:
            frame = {}
            if '(' in line and ')' in line:
                start = line.find('(') + 1
                end = line.find(')', start)
                sonde_id = line[start:end]
                if ',' not in sonde_id:
                    frame['sonde_id'] = sonde_id
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'lat:' and i + 1 < len(parts):
                    frame['lat'] = float(parts[i + 1])
                elif part == 'lon:' and i + 1 < len(parts):
                    frame['lon'] = float(parts[i + 1])
                elif part == 'alt:' and i + 1 < len(parts):
                    frame['alt'] = float(parts[i + 1])
                elif part == 'vH:' and i + 1 < len(parts):
                    frame['velocity_horizontal'] = float(parts[i + 1])
                elif part == 'vV:' and i + 1 < len(parts):
                    frame['velocity_vertical'] = float(parts[i + 1])
                elif part == 'D:' and i + 1 < len(parts):
                    frame['heading'] = float(parts[i + 1])
            return frame or None
        except Exception as e:
            self.logger.debug(f"Could not parse legacy text frame: {e}")
            return None
