"""Canonical quota policy: hero selection, scope filters and risk targets.

Every consumer (card UI, compact chip, alerts, polling) reads quota meaning
from this module. Nothing here looks at a provider's raw payload or at a
display string: identity and meaning come from ``QuotaItem`` fields only, so a
translation or a renamed limit can never move the hero or change a severity.

Two channels are deliberately separate and must stay separate:

* the representative channel (``select_hero``/``representative_*``) drives the
  one big number the user reads, and
* the risk channel (``service_remaining``/``limiting_quota``) drives badges and
  alerts, which may point at a different, more limiting quota.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

# Snapshots and quota are duck-typed here on purpose: this module must stay
# importable from providers.py, which owns those dataclasses.

FIVE_HOURS = 5 * 60 * 60
ONE_WEEK = 7 * 24 * 60 * 60
# Providers round their window lengths, so compare with a tolerance instead of
# demanding an exact match. Both stay far below the gap to the next window.
FIVE_HOUR_TOLERANCE = 60.0
ONE_WEEK_TOLERANCE = 900.0

MAIN_CATEGORY = "main"
GLOBAL_SCOPE = "global"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


# Shared remaining-percent bands. The ring, badge, bars and compact chip all
# use these cutovers. 50% is still ok; 20% is still warn; 5% is still danger.
# Alerts stay separate and fire only at 10%.
WARN_BELOW = 50.0
DANGER_BELOW = 20.0
CRITICAL_BELOW = 5.0


def remaining_band(value: Any) -> str | None:
    """ok / warn / danger / critical for a remaining percent, or None if unmeasured."""
    number = _number(value)
    if number is None:
        return None
    if number < CRITICAL_BELOW:
        return "critical"
    if number < DANGER_BELOW:
        return "danger"
    if number < WARN_BELOW:
        return "warn"
    return "ok"


def window_matches(item: Any, seconds: float, tolerance: float) -> bool:
    """True when a quota's measured window is the given duration."""
    actual = _number(getattr(item, "window_seconds", None))
    return actual is not None and abs(actual - seconds) <= tolerance


def _window_rule(seconds: float, tolerance: float) -> Callable[[Any], bool]:
    def rule(item: Any) -> bool:
        return window_matches(item, seconds, tolerance)

    return rule


def _raw_rule(*identifiers: str) -> Callable[[Any], bool]:
    wanted = {text.casefold() for text in identifiers}

    def rule(item: Any) -> bool:
        return str(getattr(item, "raw_identifier", "") or "").casefold() in wanted

    return rule


# Provider policy, expressed over canonical fields. The first rule that matches
# a usable candidate wins; otherwise the first candidate in canonical order is
# used, so every provider has a deterministic hero.
HERO_RULES: dict[str, tuple[Callable[[Any], bool], ...]] = {
    "chatgpt": (
        _window_rule(FIVE_HOURS, FIVE_HOUR_TOLERANCE),
        _window_rule(ONE_WEEK, ONE_WEEK_TOLERANCE),
    ),
    "claude": (_raw_rule("five_hour"), _raw_rule("seven_day")),
    "cursor": (_raw_rule("autoPercentUsed"),),
}


def _items(snap: Any) -> list[Any]:
    limits = getattr(snap, "main_limits", None)
    return list(limits) if isinstance(limits, (list, tuple)) else []


def global_main_limits(snap: Any) -> list[Any]:
    """Main quota the provider applies account-wide.

    Scoped quota (a single feature or model) never appears here, so it can
    never drive the hero, the compact chip, provider exhaustion or polling.
    """
    return [
        item
        for item in _items(snap)
        if str(getattr(item, "category", "")) == MAIN_CATEGORY
        and str(getattr(item, "scope", "")) == GLOBAL_SCOPE
    ]


def scoped_limits(snap: Any) -> list[Any]:
    """Every scoped quota, from additional groups and from main_limits alike."""
    found = [item for item in _items(snap) if str(getattr(item, "scope", "")) != GLOBAL_SCOPE]
    for group in getattr(snap, "additional_groups", None) or []:
        for item in getattr(group, "limits", None) or []:
            found.append(item)
    return found


def measured(items: Iterable[Any]) -> list[Any]:
    return [item for item in items if _number(getattr(item, "remaining_percent", None)) is not None]


def select_hero_items(key: str, items: Iterable[Any]) -> Any | None:
    """The single hero selector. Every consumer calls this, directly or not."""
    candidates = measured(items)
    if not candidates:
        return None
    for rule in HERO_RULES.get(str(key or ""), ()):
        for item in candidates:
            if rule(item):
                return item
    return candidates[0]


def select_hero(snap: Any) -> Any | None:
    if snap is None:
        return None
    return select_hero_items(getattr(snap, "key", ""), global_main_limits(snap))


def representative_percent(snap: Any) -> float | None:
    """The number shown in the ring and on the compact chip."""
    hero = select_hero(snap)
    if hero is not None:
        return _number(hero.remaining_percent)
    return _number(getattr(snap, "hero_percent", None)) if snap is not None else None


def representative_blocked(snap: Any) -> bool:
    """Provider-wide unusable state.

    ``blocked`` is a semantic field owned by provider normalization, not a
    guess made here and not a provider allowlist. A scoped quota running out
    is never promoted into it.
    """
    return bool(snap is not None and getattr(snap, "blocked", False))


def main_remaining_percents(snap: Any) -> list[float]:
    """Every measured global main quota. Never truncated: risk reads them all."""
    return [
        value
        for value in (
            _number(getattr(item, "remaining_percent", None))
            for item in global_main_limits(snap)
        )
        if value is not None
    ]


def limiting_quota(snap: Any) -> Any | None:
    """The alert target: the most limiting global main quota.

    This is intentionally not the hero. The hero answers "how much is left for
    what I am doing"; this answers "what is about to stop me".
    """
    candidates = measured(global_main_limits(snap))
    if not candidates:
        return None
    return min(candidates, key=lambda item: float(item.remaining_percent))


def exhausted_main_limits(snap: Any) -> list[Any]:
    return [
        item
        for item in measured(global_main_limits(snap))
        if float(item.remaining_percent) <= 0
    ]


def provider_exhausted(snap: Any) -> bool:
    """True only when the provider itself is out, never for a scoped quota."""
    if snap is None or not getattr(snap, "ok", False):
        return False
    return representative_blocked(snap) or bool(exhausted_main_limits(snap))


def service_remaining(snap: Any) -> float | None:
    """The risk channel's percentage: the most limiting global main quota.

    This reads canonical quota and nothing else. A provider's own overall
    summary figure stays internal reference data in ``snapshot.internal`` and
    must never reach a badge, a strip, an alert or a poll interval.
    """
    if snap is None:
        return None
    values = main_remaining_percents(snap)
    return min(values) if values else representative_percent(snap)


def group_stale(group: Any, snap: Any = None) -> bool:
    """Additional freshness is its own signal and never promotes to the hero."""
    metadata = getattr(group, "metadata", None)
    if isinstance(metadata, dict) and "stale" in metadata:
        return bool(metadata.get("stale"))
    return bool(snap is not None and getattr(snap, "stale", False))
