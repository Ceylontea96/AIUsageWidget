"""What a snapshot reads as: service states, check-age phrases and quota row text.

Plain functions over snapshots, without Tk, shared by the cards, the compact
row and the window.
"""
from __future__ import annotations

import math
import re
import time

from providers import dollars, fmt_local, to_float
from quota_policy import (
    FIVE_HOURS,
    FIVE_HOUR_TOLERANCE,
    global_main_limits,
    remaining_band,
    representative_blocked,
    representative_percent,
    select_hero,
    service_remaining,
    window_matches,
)
from widget_theme import *  # noqa: F401,F403  colours and titles


def _state_for(snap, remaining, blocked):
    if snap.stale:
        return 'stale'
    if not snap.ok or blocked:
        return 'danger'
    band = remaining_band(remaining)
    # The chip has no separate critical fill; under 5% stays on the danger colour.
    if band in ('critical', 'danger'):
        return 'danger'
    if band == 'warn':
        return 'warn'
    return 'ok'


def service_state(snap):
    """Risk channel: the most limiting quota, which may not be the hero.

    Badges, the alert strip and the footer read this, so a quota the hero does
    not speak for can still raise a warning.
    """
    return _state_for(snap, service_remaining(snap), representative_blocked(snap))


def representative_state(snap):
    """Representative channel: the same quota the big number comes from.

    The compact chip uses the same 50 / 20 / 5 bands as the ring, so a
    percentage cannot be calm in one place and cautious in the other.
    """
    return _state_for(snap, representative_percent(snap), representative_blocked(snap))


def chip_style(key, snap):
    if snap is None:
        return CHIP_STALE, CHIP_FG
    state = representative_state(snap)
    if state == 'stale':
        return CHIP_STALE, CHIP_FG
    if state == 'danger':
        return CHIP_DANGER, CHIP_FG
    if state == 'warn':
        return CHIP_WARN, CHIP_FG
    return CHIP_OK[key], CHIP_FG


def compact_warning(snap):
    """Flag a low secondary global quota without changing the hero's meaning."""
    if snap is None or not snap.ok or snap.stale:
        return None
    hero = select_hero(snap)
    candidates = [item for item in global_main_limits(snap)
                  if (hero is None or item.quota_id != hero.quota_id)
                  and remaining_band(item.remaining_percent) in ('warn', 'danger', 'critical')]
    return min(candidates, key=lambda item: item.remaining_percent) if candidates else None


def compact_ring_color(snap):
    """The compact chip's ring: the most severe of its own quota and the others.

    The ring was only for a low secondary quota, so an exhausted or blocked
    service whose other quota was merely low got a yellow ring, and at 0% its
    red fill has no width to show. Its own danger now rings red first.
    """
    if snap is None or not snap.ok or snap.stale:
        return None
    if representative_state(snap) == 'danger':
        return DANGER
    warning = compact_warning(snap)
    if warning is None:
        return None
    return WARN if remaining_band(warning.remaining_percent) == 'warn' else DANGER


def _checked_phrase(stamp, now, *, confirmed):
    """Relative time for one service. Empty when this snapshot was never stamped."""
    try:
        stamp = float(stamp or 0)
    except (TypeError, ValueError):
        return ''
    if not math.isfinite(stamp) or stamp <= 0:
        return ''
    age = max(0, int(now - stamp))
    if age < 5:
        head = '방금'
    elif age < 60:
        head = f'{age}초 전'
    elif age < 3600:
        head = f'{age // 60}분 전'
    elif age < 86400:
        head = f'{age // 3600}시간 전'
    else:
        head = f'{age // 86400}일 전'
    return f'{head} 확인' if confirmed else f'{head} 시도'


def failure_cause(snap):
    """Short reason for a failed check. Login failures share one phrase."""
    if snap is None:
        return ''
    internal = getattr(snap, 'internal', None) or {}
    text = str(getattr(snap, 'error', '') or '').strip()
    if internal.get('requires_login'):
        return '재로그인 필요'
    if not text:
        return ''
    if any(phrase in text for phrase in ('가져오는 중', '기다리는 중', '조회 중')):
        return '확인 중'
    folded = text.casefold()
    if any(token in folded for token in ('로그인', 'login', 'sign in', 'signin', '인증', '토큰', 'token')):
        return '재로그인 필요'
    if '초과' in text or 'timeout' in folded:
        return '응답 시간 초과'
    if '연결' in text or 'offline' in folded or 'connection' in folded:
        return '연결 확인 필요'
    if '형식' in text:
        return '응답 형식 확인 필요'
    sentence = text.split('\n', 1)[0].strip()
    for sep in ('。', '. '):
        sentence = sentence.split(sep, 1)[0]
    sentence = sentence.strip(' .')
    if len(sentence) > 24:
        return '조회 실패'
    return sentence


def service_status_text(snap, now=None):
    """Per-service check age, and the cached-value cause when a later check failed."""
    if snap is None:
        return ''
    now = time.time() if now is None else now
    age = _checked_phrase(getattr(snap, 'fetched_at', 0), now, confirmed=bool(snap.ok))
    if not snap.ok:
        return age
    if snap.stale:
        cause = failure_cause(snap)
        detail = f'이전 값 · {cause}' if cause else '이전 값'
        return f'{age} · {detail}' if age else detail
    return age


def service_needs_actions(snap):
    """Retry and login guidance belong on a real failure, not a check still in flight."""
    if snap is None or failure_cause(snap) == '확인 중':
        return False
    if not snap.ok:
        return True
    return bool(snap.stale and getattr(snap, 'error', ''))


def header_freshness(snaps, now=None):
    """Header age follows the oldest confirmation, so one fresh service cannot refresh the rest."""
    now = time.time() if now is None else now
    snaps = list(snaps or ())
    stamps = []
    for snap in snaps:
        if not (snap.ok or snap.stale):
            continue
        try:
            stamp = float(snap.fetched_at or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(stamp) and stamp > 0:
            stamps.append(stamp)
    if not stamps:
        return ('확인 실패', None) if any(not snap.ok for snap in snaps) else ('갱신 대기', None)
    oldest, newest = min(stamps), max(stamps)
    age = max(0, int(now - oldest))
    newest_age = max(0, int(now - newest))
    if len(stamps) > 1 and newest - oldest >= 30 and newest_age < 60:
        return '서비스별 확인', age
    return _checked_phrase(oldest, now, confirmed=True) or '갱신 대기', age


def compact_tooltip(snap):
    if snap is None:
        return '사용량 확인 중'
    lines = [TITLES.get(snap.key, snap.title)]
    status = service_status_text(snap)
    if status:
        lines.append(status)
    if snap.error and snap.error not in (status or ''):
        lines.append(snap.error)
    if snap.blocked and not snap.stale:
        lines.append('현재 사용 제한')
    warning = compact_warning(snap)
    if warning is not None:
        lines.append(f'주의: {warning.display_name} 잔여 {warning.remaining_percent:.0f}%')
    for item in global_main_limits(snap):
        value = '확인 중' if item.remaining_percent is None else f'잔여 {item.remaining_percent:.0f}%'
        lines.append(f'{item.display_name} · {value}')
    if no_five_hour_limit(snap):
        lines.append(NO_FIVE_HOUR_NOTE)
    return '\n'.join(lines)


def reset_stamp(text):
    if not text:
        return ''
    found = re.search(r'(\d{1,2}:\d{2})', text)
    return f'{found.group(1)} 리셋' if found else ''


def dated_reset_stamp(text):
    clean = str(text or '').replace(' 초기화', '').replace(' 재설정', '').replace(' 리셋', '').strip()
    return f'{clean} 리셋' if clean else ''


def billing_entry(snap, kind):
    for item in getattr(snap, 'billing', None) or []:
        if item.kind == kind:
            return item
    return None


def billing_value(snap, kind):
    item = billing_entry(snap, kind)
    return None if item is None else item.raw_value


def reset_credit(snap, now=None):
    item = billing_entry(snap, 'reset_credits')
    available = to_float(item.raw_value) if item is not None else None
    if not available:
        return ''
    text = f'리셋권 {available:.0f}'
    # With several credits the provider already picked the nearest expiry.
    expiry = to_float((item.metadata or {}).get('nearest_expires_at'))
    current = time.time() if now is None else now
    if expiry is not None and expiry > current:
        text += f' · {fmt_local(expiry, "reset")} 만료'
    return text


def included_amount(snap):
    limit = to_float(billing_value(snap, 'limit'))
    if limit is None:
        return ''
    included = to_float(billing_value(snap, 'includedSpend')) or 0.0
    return f'{dollars(included)} / {dollars(limit)}'


def bonus_line(snap):
    bonus = to_float(billing_value(snap, 'bonusSpend'))
    return f'보너스 {dollars(bonus)}' if bonus else ''


def main_limits(snap):
    """The card's rows: global main quota, in canonical order."""
    return global_main_limits(snap) if snap is not None and snap.ok else []


NO_FIVE_HOUR_NOTE = '5시간 한도 없음 · Codex 기준'


def no_five_hour_limit(snap):
    """GPT limits from Codex, none of them a five-hour window.

    Some plans have only a weekly limit. The card then shows the weekly one as
    its large number; without a word it looked as if the five-hour limit had
    gone missing from the widget.
    """
    limits = main_limits(snap)
    return (bool(limits) and snap.key == 'chatgpt'
            and not any(window_matches(item, FIVE_HOURS, FIVE_HOUR_TOLERANCE) for item in limits))


def hero_index(snap):
    """Position of the hero within main_limits(snap), for painting only."""
    hero = select_hero(snap) if snap is not None and snap.ok else None
    if hero is None:
        return None
    for index, item in enumerate(main_limits(snap)):
        if item is hero:
            return index
    return None


def status_percent(snap):
    """Service warnings can differ from the window chosen for the large number."""
    return service_remaining(snap)


def quota_alert_copy(key, severity, remaining, label):
    provider = {'chatgpt': 'ChatGPT', 'claude': 'Claude'}.get(key, TITLES.get(key, key))
    quota = quota_window_title(label)
    status = '소진' if remaining <= 0 else '제한' if severity == 2 else '임박'
    return f'{provider} {quota} {status}', f'{quota} · 잔여 {remaining:.0f}%'


def quota_window_title(label):
    label = str(label or '').strip()
    return f'{label} 한도' if label else '사용량 한도'


def quota_row_title(item):
    """Time-boxed quota reads as a limit; a named pool keeps its own name.

    The distinction is the measured window, not the provider or the label.
    """
    name = str(getattr(item, 'display_name', '') or getattr(item, 'window_label', '') or '')
    if getattr(item, 'window_seconds', None) is None:
        return name or '남은 사용량'
    return quota_window_title(name)


def quota_extras(snap):
    parts = []
    credits = to_float(billing_value(snap, 'credits'))
    if credits is not None:
        parts.append(f'크레딧 {credits:g}')
    reset = reset_credit(snap)
    if reset:
        parts.append(reset)
    return ' · '.join(parts)


# How many secondary quota rows the card draws. This is a display budget so a
# provider that starts reporting many windows cannot push the card off screen;
# risk detection and alerts still read every one of them.
MAX_SECONDARY_ROWS = 6
