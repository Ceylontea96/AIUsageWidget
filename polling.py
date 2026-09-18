"""Provider polling policy and Retry-After parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time


@dataclass(frozen=True)
class PollingPolicy:
    normal_interval: float = 30.0
    active_interval: float = 2.0
    near_limit_interval: float = 20.0
    exhausted_interval: float = 300.0
    near_limit_percent: float = 35.0
    backoff_initial: float = 30.0
    backoff_multiplier: float = 2.0
    max_backoff: float = 900.0

    def failure_delay(self, failures: int) -> float:
        exponent = min(max(0, int(failures) - 1), 5)
        return min(self.max_backoff, self.backoff_initial * self.backoff_multiplier ** exponent)


POLICIES = {
    "chatgpt": PollingPolicy(),
    "cursor": PollingPolicy(),
}


def policy_for(provider: str) -> PollingPolicy:
    return POLICIES.get(provider, PollingPolicy())


def retry_after_delay(value, now=None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdecimal():
        delay = float(text)
        return delay if math.isfinite(delay) and delay >= 0 else None
    try:
        target = parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        current = time.time() if now is None else float(now)
        delay = target.timestamp() - current
        return delay if math.isfinite(delay) and delay >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def next_poll_delay(
    policy: PollingPolicy,
    *,
    ok: bool,
    blocked: bool,
    hero_percent: float | None,
    main_remaining: list[float],
    failures: int = 0,
    active: bool = False,
    retry_after="",
    now=None,
) -> float:
    if failures:
        server_delay = retry_after_delay(retry_after, now)
        return server_delay if server_delay is not None else policy.failure_delay(failures)
    if not ok:
        return policy.normal_interval
    if blocked or hero_percent == 0:
        return policy.exhausted_interval
    if active:
        return policy.active_interval
    if any(value <= policy.near_limit_percent for value in main_remaining):
        return policy.near_limit_interval
    return policy.normal_interval
