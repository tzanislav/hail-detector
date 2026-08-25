from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RadarFrame:
    """A single accepted CAPPI frame described by the public manifest."""

    filename: str
    url: str
    observed_at: datetime
    display_timestamp: str

    @property
    def source_timestamp(self) -> str:
        return self.observed_at.isoformat()

