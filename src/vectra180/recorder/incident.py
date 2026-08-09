"""G-sensor incident detection.

A collision, a hard brake or a kerb strike all show up as total acceleration
departing from 1 g. When that happens the current segment is locked so the loop
recorder cannot overwrite it.

Detection deliberately uses the *magnitude* of acceleration rather than any
single axis, because the module's mounting angle in a given vehicle is unknown
and a per-axis threshold would need recalibrating for every install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vectra180.config import IncidentConfig
from vectra180.telemetry import TelemetrySample

__all__ = ["Incident", "IncidentDetector"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Incident:
    """A detected event."""

    #: ``time.monotonic()`` when it fired.
    monotonic: float
    #: Peak deviation from 1 g, in g.
    magnitude_g: float
    #: ``gsensor`` for an automatic trigger, ``manual`` for a user request.
    source: str = "gsensor"

    def as_dict(self) -> dict[str, object]:
        return {"magnitude_g": round(self.magnitude_g, 3), "source": self.source}


class IncidentDetector:
    """Threshold detector with a cooldown.

    An impact rings the accelerometer for hundreds of milliseconds. Without the
    cooldown one event would fire on every frame of that ring-down and lock a
    whole run of segments.
    """

    def __init__(self, config: IncidentConfig | None = None) -> None:
        self._config = config or IncidentConfig()
        self._last_trigger: float | None = None
        self.peak_magnitude_g = 0.0
        self.trigger_count = 0

    def update(self, sample: TelemetrySample | None, now: float) -> Incident | None:
        """Feed one sample. Returns an :class:`Incident` when it trips.

        Args:
            sample: Latest telemetry, or ``None`` if this frame had none.
            now: ``time.monotonic()``.
        """
        if not self._config.enabled or sample is None:
            return None

        deviation = abs(sample.accel_magnitude_g - 1.0)
        self.peak_magnitude_g = max(self.peak_magnitude_g, deviation)

        if deviation < self._config.threshold_g:
            return None
        if self._last_trigger is not None and now - self._last_trigger < self._config.cooldown_seconds:
            return None

        self._last_trigger = now
        self.trigger_count += 1
        log.warning("incident detected: %.2fg deviation from rest", deviation)
        return Incident(monotonic=now, magnitude_g=deviation)

    def trigger_manual(self, now: float) -> Incident:
        """Force an incident, for the web UI's lock button.

        The cooldown is bypassed: a person pressing the button means it.
        """
        self._last_trigger = now
        self.trigger_count += 1
        return Incident(monotonic=now, magnitude_g=0.0, source="manual")

    def reset(self) -> None:
        self._last_trigger = None
        self.peak_magnitude_g = 0.0
        self.trigger_count = 0
