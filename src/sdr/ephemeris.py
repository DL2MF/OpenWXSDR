"""
# =============================================================================
#  OpenWX -- Open Weather Radiosonde Telemetry System
# =============================================================================
#
#  File   : ephemeris.py
#  Author : M.F. Guenther, DL2MF - DL2MF@darc.de
#  License: GNU General Public License v2.0 (GPL-2.0)
#
# -----------------------------------------------------------------------------
#  Description
# -----------------------------------------------------------------------------
#
#  RS92 GPS broadcast-ephemeris (RINEX) downloader.
#
#  Vaisala RS92 sondes transmit raw GPS pseudoranges, so rs92mod needs the
#  current GPS-day broadcast ephemeris (a RINEX navigation file) to compute a
#  position. radiosonde_auto_rx classically fetches this from NASA CDDIS, but
#  CDDIS discontinued anonymous FTP (Oct 2020) and now requires an Earthdata
#  login. This module downloads the file from a configurable HTTPS source —
#  default BKG (Germany, no login, fast in Europe) — into data/rs92 and hands
#  the local path to the RS92 decode chain (rs92mod -e).
#
#  Opt-in via config rs92.ephemeris_download. When disabled nothing is fetched
#  and RS92 decodes exactly as before (position may be unavailable).
#
#  URL template placeholders:
#    {yyyy}  4-digit year        {yy}  2-digit year
#    {doy}   zero-padded day-of-year (001-366)
#  Default = BKG daily broadcast ephemeris. BKG only serves RINEX3 mixed-nav
#  (BRDC00WRD_R_<yyyy><doy>0000_01D_MN.rnx.gz) — no RINEX2 brdc in this tree.
#  Current rs92mod builds read RINEX3, and the _MN (mixed) file contains the GPS
#  records rs92mod needs. Only .gz (gzip) and plain files are supported; CDDIS
#  ships .Z (Unix compress) which stdlib can't inflate.
# =============================================================================
"""

import gzip
import logging
import os
import shutil
import threading
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = ('https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{yyyy}/{doy}/'
               'BRDC00WRD_R_{yyyy}{doy}0000_01D_MN.rnx.gz')


class RS92EphemerisManager:
    """Downloads and caches the current GPS-day RS92 ephemeris file."""

    def __init__(self, config=None):
        rs92 = ((config or {}).get('rs92', {}) or {})
        self.enabled = bool(rs92.get('ephemeris_download', False))
        self.url_template = str(rs92.get('ephemeris_url') or DEFAULT_URL)
        self.out_dir = str(rs92.get('ephemeris_dir') or 'data/rs92')
        self.logger = logging.getLogger('RS92Ephemeris')
        self._lock = threading.Lock()

    def _parts(self):
        now = datetime.now(timezone.utc)
        return now.strftime('%Y'), now.strftime('%y'), now.strftime('%j')

    def _today_url(self):
        yyyy, yy, doy = self._parts()
        return self.url_template.format(yyyy=yyyy, yy=yy, doy=doy)

    @staticmethod
    def _local_for_url(url, out_dir):
        base = os.path.basename(url)
        if base.endswith('.gz'):
            base = base[:-3]
        return os.path.join(out_dir, base)

    def today_file(self):
        """Local path to today's ephemeris IF already downloaded, else None.
        Never touches the network — safe to call from the decode-start path."""
        if not self.enabled:
            return None
        local = self._local_for_url(self._today_url(), self.out_dir)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            return local
        return None

    def ensure_current(self):
        """Download today's ephemeris if missing; return the local path or None.
        May block on network I/O — call from a background thread / at startup."""
        if not self.enabled:
            return None
        with self._lock:
            url = self._today_url()
            local = self._local_for_url(url, self.out_dir)
            if os.path.exists(local) and os.path.getsize(local) > 0:
                return local
            tmp = local + '.download'
            try:
                os.makedirs(self.out_dir, exist_ok=True)
                self.logger.info(f"Downloading RS92 ephemeris: {url}")
                req = urllib.request.Request(url, headers={'User-Agent': 'OpenWXSDR'})
                with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, 'wb') as fo:
                    shutil.copyfileobj(resp, fo)
                if url.endswith('.gz'):
                    with gzip.open(tmp, 'rb') as fi, open(local, 'wb') as fo:
                        shutil.copyfileobj(fi, fo)
                    os.remove(tmp)
                else:
                    os.replace(tmp, local)
                # Drop stale (previous-day) files so the decoder can't pick a
                # wrong GPS-day ephemeris.
                self._cleanup_old(keep=os.path.basename(local))
                self.logger.info(
                    f"RS92 ephemeris ready: {local} ({os.path.getsize(local)} bytes)")
                return local
            except Exception as exc:  # noqa: BLE001 - never crash the caller
                self.logger.warning(f"RS92 ephemeris download failed ({url}): {exc}")
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return None

    def _cleanup_old(self, keep):
        try:
            for f in os.listdir(self.out_dir):
                if f != keep and not f.endswith('.download'):
                    try:
                        os.remove(os.path.join(self.out_dir, f))
                    except OSError:
                        pass
        except OSError:
            pass

    def start_background_refresh(self):
        """Fetch the current file in the background (non-blocking), retrying on
        failure so a boot-time network hiccup or a transient server error
        doesn't leave the day without ephemeris until a manual restart."""
        if not self.enabled:
            return
        threading.Thread(target=self._refresh_loop, daemon=True,
                         name='RS92EphemerisDownload').start()

    def _refresh_loop(self):
        delay = 15
        while True:
            got = self.ensure_current()
            if got:
                # Today's file is present. Re-check hourly to pick up the next
                # GPS-day at UTC midnight; reset the failure backoff.
                delay = 15
                time.sleep(3600)
            else:
                # Failed (e.g. network not ready at boot) — back off and retry.
                time.sleep(delay)
                delay = min(delay * 2, 600)   # up to 10 min between attempts


# Module-level singleton wired by the app at startup, so the RS92 decode path
# can query the current ephemeris without plumbing config through every call.
_manager = None


def configure(config):
    """Create/replace the singleton manager from the app config; returns it."""
    global _manager
    _manager = RS92EphemerisManager(config)
    return _manager


def rs92_today_file():
    """Today's ephemeris local path if already downloaded (no network), else
    None. Called by the RS92 decoder to decide whether to pass rs92mod -e."""
    return _manager.today_file() if _manager is not None else None


def status():
    """Small status dict for the System Health panel: whether the download is
    enabled and whether today's file is present."""
    if _manager is None or not _manager.enabled:
        return {'enabled': False, 'available': False, 'state': 'disabled'}
    f = _manager.today_file()
    return {
        'enabled': True,
        'available': bool(f),
        'state': 'ready' if f else 'pending',
        'file': os.path.basename(f) if f else None,
    }
