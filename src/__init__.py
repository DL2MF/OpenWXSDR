"""
OpenWXSDR - Streamlined Radiosonde Decoder Framework
"""

__version__ = '1.0.62'
__build_date__ = '2026-08-09'
__release_date__ = 'August 9, 2026'
__software_name__ = 'OpenWXSDR'
__author__ = 'DL2MF@darc.de'

__all__ = ['OpenWXSDR']


def __getattr__(name):
	"""Lazy export to avoid import-time cycles during package init."""
	if name == 'OpenWXSDR':
		from .openwxsdr_app import OpenWXSDR
		return OpenWXSDR
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
