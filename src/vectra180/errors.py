"""Exception hierarchy for Vectra-180.

Every failure the CLI reports to a user is one of these, so ``cli.main`` can
print a clean message instead of a traceback.
"""

from __future__ import annotations

__all__ = [
    "CaptureError",
    "ConfigError",
    "DeviceNotFoundError",
    "RecorderError",
    "ServiceError",
    "VectraError",
]


class VectraError(Exception):
    """Base class for all recoverable Vectra-180 failures."""


class ConfigError(VectraError):
    """The configuration file or environment is invalid."""


class CaptureError(VectraError):
    """The camera could not be opened or the stream failed unrecoverably."""


class DeviceNotFoundError(CaptureError):
    """No capture device matched the requested index or path."""


class RecorderError(VectraError):
    """The encoder could not be started or a segment could not be written."""


class ServiceError(VectraError):
    """The HTTP service could not start."""
