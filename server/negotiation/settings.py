"""One global context setting; no unmeasured aggregation-window default."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping
from .prices import InvalidFact


@dataclass(frozen=True)
class Settings:
    context_messages: int = 20

    def __post_init__(self):
        if type(self.context_messages) is not int or not 10 <= self.context_messages <= 30:
            raise InvalidFact("Context message count must be an integer between 10 and 30")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        raw = env.get("XUNJIA_BOND_CONTEXT_MESSAGES", "20")
        if not isinstance(raw, str) or not raw.isdecimal():
            raise InvalidFact("XUNJIA_BOND_CONTEXT_MESSAGES must be 10..30")
        return cls(int(raw))


@dataclass(frozen=True)
class Aggregation:
    quiet_ms: int
    max_wait_ms: int
    reference_ms: float
    measurement_id: str

    def __post_init__(self):
        if type(self.quiet_ms) is not int or type(self.max_wait_ms) is not int:
            raise InvalidFact("Aggregation windows must use integral milliseconds")
        if (isinstance(self.reference_ms, bool) or
                not isinstance(self.reference_ms, (int, float)) or
                not math.isfinite(self.reference_ms) or self.reference_ms <= 0):
            raise InvalidFact("Reference must be a positive measured successful Chatflow duration")
        if not 0 <= self.quiet_ms <= self.max_wait_ms < self.reference_ms / 2:
            raise InvalidFact("Cumulative aggregation wait must be less than half the reference duration")
        if not isinstance(self.measurement_id, str) or not self.measurement_id:
            raise InvalidFact("A measured latency report reference is required")

    def ready(self, *, now: float, first_pending: float, last_received: float) -> bool:
        return (now >= last_received + self.quiet_ms / 1000 or
                now >= first_pending + self.max_wait_ms / 1000)
