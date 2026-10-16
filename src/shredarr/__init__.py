"""Shredarr: delete it in your media server, and it's gone everywhere."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("shredarr")  # set in pyproject.toml
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
