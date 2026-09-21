"""Ultron voice assistant package."""
from importlib.metadata import version, PackageNotFoundError

APP_VERSION = "2026.09.21-modular"

try:
    __version__ = version("ultron")
except PackageNotFoundError:
    __version__ = APP_VERSION
