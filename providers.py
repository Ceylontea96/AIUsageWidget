"""Read local ChatGPT/Cursor login state and fetch remaining quota."""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from quota_policy import (
    FIVE_HOURS,
    FIVE_HOUR_TOLERANCE,
    ONE_WEEK,
    ONE_WEEK_TOLERANCE,
    select_hero_items,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 8.0
SQLITE_RETRY_S = 2.0

_OPENER = urllib.request.build_opener()
_CURSOR_MEM: dict[str, Any] = {}
_CHATGPT_MEM: dict[str, Any] = {}
_PLAN_MEM: dict[str, Any] = {"name": "", "until": 0.0}


@dataclass
class QuotaBar:
    label: str
    remaining_percent: float | None
    used_percent: float | None
    detail: str
    reset_text: str = ""
    usage_scope: str = ""


@dataclass
class InfoRow:
    label: str
    value: str
    emphasis: str = ""


@dataclass
class QuotaItem:
    quota_id: str
    source: str
    category: str
    display_name: str
    raw_identifier: str = ""
    window_seconds: float | None = None
    window_label: str = ""
    used_percent: float | None = None
    remaining_percent: float | None = None
    reset_at: float | None = None
    model_name: str = ""
    scope: str = "global"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LimitGroup:
    group_id: str
    source: str
    display_name: str
    category: str = "additional"
    raw_identifier: str = ""
    limits: list[QuotaItem] = field(default_factory=list)
    scope: str = "scoped"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BillingItem:
    billing_id: str
    source: str
    kind: str
    raw_value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class JsonPayload(dict):
    def __init__(self, mapping=None, retry_after: str = "") -> None:
        super().__init__(mapping or {})
        self.retry_after = str(retry_after or "")


class FetchError(RuntimeError):
    def __init__(self, message: str, retry_after: str = "") -> None:
        super().__init__(message)
        self.retry_after = str(retry_after or "")


@dataclass
class ProviderSnapshot:
    key: str
    title: str
    plan: str
    ok: bool
    hero_percent: float | None
    hero_caption: str
    bars: list[QuotaBar] = field(default_factory=list)
    info_rows: list[InfoRow] = field(default_factory=list)
    footer: str = ""
    error: str = ""
    dashboard_url: str = ""
    fetched_at: float = 0.0
    stale: bool = False
    blocked: bool = False
    main_limits: list[QuotaItem] = field(default_factory=list)
    additional_groups: list[LimitGroup] = field(default_factory=list)
    billing: list[BillingItem] = field(default_factory=list)
    retry_after: str = ""
    internal: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.main_limits and self.bars:
            self.main_limits = [
                quota_item_from_bar(self.key, bar, index) for index, bar in enumerate(self.bars)
            ]
        elif self.main_limits and not self.bars:
            # Legacy adapter boundary: only global main quota is exposed as
            # bars. Scoped and additional quota never leak into this view.
            self.bars = [
                bar_from_quota_item(item)
                for item in self.main_limits
                if item.category == "main" and item.scope == "global"
            ]


def jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return {}


def jwt_exp(token: str) -> float | None:
    exp = jwt_payload(token).get("exp")
    try:
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, Any]:
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            chunks = []
            size = 0
            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise RuntimeError("사용량 응답 시간이 초과되었습니다.")
                chunk = resp.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 1024 * 1024:
                    raise RuntimeError("사용량 응답 크기가 너무 큽니다.")
            raw = b''.join(chunks)
            if not raw:
                return int(getattr(resp, "status", 200) or 200), JsonPayload({})
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise RuntimeError("사용량 응답 형식이 올바르지 않습니다.")
            return int(getattr(resp, "status", 200) or 200), JsonPayload(parsed)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        retry_after = ""
        headers = getattr(exc, "headers", None)
        if headers is not None:
            retry_after = str(headers.get("Retry-After") or "")
        exc.close()
        return status, JsonPayload({}, retry_after)
    except Exception as exc:
        raise RuntimeError("서버 연결을 확인한 뒤 다시 시도하세요.") from None


def dollars(cents: float | int | None) -> str:
    if cents is None:
        return "-"
    return f"${cents / 100:.2f}"


def fmt_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(total, 3600)
    minutes, _secs = divmod(rem, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}일 {hours}시간 후"
    if hours:
        return f"{hours}시간 {minutes}분 후"
    if minutes:
        return f"{minutes}분 후"
    return "곧"


def fmt_local(ts: float | int | None, fmt: str = "%m/%d %H:%M") -> str:
    if ts is None:
        return ""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value > 10_000_000_000:
        value /= 1000.0
    moment = datetime.fromtimestamp(value)
    if fmt == "reset":
        return f"{moment.month}월 {moment.day}일 {moment.hour:02d}:{moment.minute:02d}"
    return moment.strftime(fmt)


def translate_cursor_message(message: str) -> str:
    text = (message or "").strip()
    mapping = {
        "You've hit your usage limit": "포함 한도 도달",
        "You've used 100% of your included total usage": "포함 사용량 소진",
        "You've used 100% of your included API usage": "포함 API 소진",
    }
    if text in mapping:
        return mapping[text]
    return (
        text.replace("You've used", "사용")
        .replace("of your included total usage", "(포함)")
        .replace("of your included API usage", "(API)")
    )


def remaining_from_used(used_percent: float | None) -> float | None:
    if used_percent is None:
        return None
    return max(0.0, min(100.0, 100.0 - float(used_percent)))


def to_float(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return to_float(value)
    return value


# Identity is built from fields a provider owns, never from a display string,
# a reset timestamp or the current clock, so the same quota keeps one id while
# its label, its reset or its remaining percentage change.
RAW_ID_FIELDS = ("limit_id", "id", "window_id", "metered_feature", "slug", "key")


def raw_identifier(raw: Any, structural_key: str) -> str:
    """Prefer an identifier the provider owns; fall back to structural position."""
    raw = raw if isinstance(raw, dict) else {}
    for field_name in RAW_ID_FIELDS:
        value = raw.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return str(structural_key)


def unique_identifier(taken: dict[str, int], candidate: str, structural_key: str) -> str:
    """Keep two quotas apart when a provider reuses one raw id.

    The disambiguator is the structural key, which is deterministic across
    polls, rather than a counter that depends on arrival order.
    """
    taken[candidate] = taken.get(candidate, 0) + 1
    if taken[candidate] == 1:
        return candidate
    combined = f"{candidate}@{structural_key}"
    taken[combined] = taken.get(combined, 0) + 1
    return combined if taken[combined] == 1 else f"{combined}#{taken[combined]}"


def quota_item_from_bar(source: str, bar: QuotaBar, index: int = 0) -> QuotaItem:
    """Migration adapter for 3.4.x caches, which stored only legacy bars.

    This is the one place where legacy display state is turned back into
    canonical state. Identity comes from the bar's position, which is stable
    across restarts, never from its label. Live fetches never take this path.
    """
    reset_at, seconds = _legacy_scope_parts(bar.usage_scope)
    seconds = seconds if seconds is not None and seconds > 0 else None
    raw = f"legacy[{int(index)}]"
    return QuotaItem(
        quota_id=f"{source}:main:{raw}",
        source=source,
        category="main",
        display_name=bar.label,
        raw_identifier=raw,
        window_seconds=seconds,
        window_label=bar.label,
        used_percent=bar.used_percent,
        remaining_percent=bar.remaining_percent,
        reset_at=reset_at,
        scope="global",
        metadata={"legacy_bar": True},
    )


def _legacy_scope_parts(scope: str) -> tuple[float | None, float | None]:
    try:
        parsed = json.loads(str(scope or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(parsed, list) or len(parsed) < 2:
        return None, None
    return to_float(parsed[0]), to_float(parsed[1])


def exhausted_names(items: list[QuotaItem]) -> list[str]:
    """Names of the account-wide quota that have run out.

    Only global main quota is considered, so a scoped feature limit can never
    declare the whole provider unusable.
    """
    return [
        item.display_name
        for item in items
        if item.category == "main"
        and item.scope == "global"
        and item.remaining_percent is not None
        and item.remaining_percent <= 0
    ]


def legacy_detail(remaining: float | None) -> str:
    return "잔여 --" if remaining is None else f"잔여 {remaining:.0f}%"


def bar_from_quota_item(item: QuotaItem) -> QuotaBar:
    return QuotaBar(
        label=item.display_name or item.window_label,
        remaining_percent=item.remaining_percent,
        used_percent=item.used_percent,
        detail=legacy_detail(item.remaining_percent),
        reset_text=fmt_local(item.reset_at, "reset") if item.reset_at else "",
        usage_scope=json.dumps([item.reset_at, item.window_seconds]),
    )


def _quota_item_to_dict(item: QuotaItem) -> dict[str, Any]:
    return {
        "quota_id": item.quota_id,
        "source": item.source,
        "category": item.category,
        "display_name": item.display_name,
        "raw_identifier": item.raw_identifier,
        "window_seconds": json_safe_value(item.window_seconds),
        "window_label": item.window_label,
        "used_percent": json_safe_value(item.used_percent),
        "remaining_percent": json_safe_value(item.remaining_percent),
        "reset_at": json_safe_value(item.reset_at),
        "model_name": item.model_name,
        "scope": item.scope,
        "metadata": json_safe_value(item.metadata) if isinstance(item.metadata, dict) else {},
    }


def _quota_item_from_dict(raw: Any) -> QuotaItem | None:
    if not isinstance(raw, dict):
        return None
    quota_id = str(raw.get("quota_id") or "")
    if not quota_id:
        return None
    metadata = raw.get("metadata")
    return QuotaItem(
        quota_id=quota_id,
        source=str(raw.get("source") or ""),
        category=str(raw.get("category") or "unknown"),
        display_name=str(raw.get("display_name") or ""),
        raw_identifier=str(raw.get("raw_identifier") or ""),
        window_seconds=to_float(raw.get("window_seconds")),
        window_label=str(raw.get("window_label") or ""),
        used_percent=to_float(raw.get("used_percent")),
        remaining_percent=to_float(raw.get("remaining_percent")),
        reset_at=to_float(raw.get("reset_at")),
        model_name=str(raw.get("model_name") or ""),
        scope=str(raw.get("scope") or "global"),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _limit_group_to_dict(group: LimitGroup) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "source": group.source,
        "display_name": group.display_name,
        "category": group.category,
        "raw_identifier": group.raw_identifier,
        "limits": [_quota_item_to_dict(item) for item in group.limits],
        "scope": group.scope,
        "metadata": json_safe_value(group.metadata) if isinstance(group.metadata, dict) else {},
    }


def _limit_group_from_dict(raw: Any) -> LimitGroup | None:
    if not isinstance(raw, dict):
        return None
    group_id = str(raw.get("group_id") or "")
    if not group_id:
        return None
    limits = []
    for item in raw.get("limits") if isinstance(raw.get("limits"), list) else []:
        parsed = _quota_item_from_dict(item)
        if parsed is not None:
            limits.append(parsed)
    metadata = raw.get("metadata")
    return LimitGroup(
        group_id=group_id,
        source=str(raw.get("source") or ""),
        display_name=str(raw.get("display_name") or ""),
        category=str(raw.get("category") or "additional"),
        raw_identifier=str(raw.get("raw_identifier") or ""),
        limits=limits,
        scope=str(raw.get("scope") or "scoped"),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def _billing_item_to_dict(item: BillingItem) -> dict[str, Any]:
    return {
        "billing_id": item.billing_id,
        "source": item.source,
        "kind": item.kind,
        "raw_value": json_safe_value(item.raw_value),
        "metadata": json_safe_value(item.metadata) if isinstance(item.metadata, dict) else {},
    }


def _billing_item_from_dict(raw: Any) -> BillingItem | None:
    if not isinstance(raw, dict):
        return None
    billing_id = str(raw.get("billing_id") or "")
    if not billing_id:
        return None
    metadata = raw.get("metadata")
    return BillingItem(
        billing_id=billing_id,
        source=str(raw.get("source") or ""),
        kind=str(raw.get("kind") or "unknown"),
        raw_value=json_safe_value(raw.get("raw_value")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


def snapshot_to_dict(snap: ProviderSnapshot) -> dict[str, Any]:
    return {
        "key": snap.key,
        "title": snap.title,
        "plan": snap.plan,
        "ok": snap.ok,
        "hero_percent": snap.hero_percent,
        "hero_caption": snap.hero_caption,
        "bars": [
            {
                "label": bar.label,
                "remaining_percent": json_safe_value(bar.remaining_percent),
                "used_percent": json_safe_value(bar.used_percent),
                "detail": bar.detail,
                "reset_text": bar.reset_text,
                "usage_scope": bar.usage_scope,
            }
            for bar in snap.bars
        ],
        "info_rows": [
            {
                "label": row.label,
                "value": row.value,
                "emphasis": row.emphasis,
            }
            for row in snap.info_rows
        ],
        "footer": snap.footer,
        "error": snap.error,
        "dashboard_url": snap.dashboard_url,
        "fetched_at": snap.fetched_at,
        "stale": snap.stale,
        "blocked": snap.blocked,
        "main_limits": [_quota_item_to_dict(item) for item in snap.main_limits],
        "additional_groups": [_limit_group_to_dict(group) for group in snap.additional_groups],
        "billing": [_billing_item_to_dict(item) for item in snap.billing],
        "retry_after": snap.retry_after,
        "internal": json_safe_value(snap.internal) if isinstance(snap.internal, dict) else {},
    }


def snapshot_from_dict(data: dict[str, Any]) -> ProviderSnapshot:
    bars = []
    raw_bars = data.get("bars")
    # No arbitrary cap: dropping a cached quota here would hide it from risk
    # detection as well as from the card.
    for raw in raw_bars if isinstance(raw_bars, list) else []:
        if not isinstance(raw, dict):
            continue
        bars.append(
            QuotaBar(
                label=str(raw.get("label") or ""),
                remaining_percent=to_float(raw.get("remaining_percent")),
                used_percent=to_float(raw.get("used_percent")),
                detail=str(raw.get("detail") or ""),
                reset_text=str(raw.get("reset_text") or ""),
                usage_scope=str(raw.get("usage_scope") or ""),
            )
        )
    info_rows = []
    raw_rows = data.get("info_rows")
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        info_rows.append(
            InfoRow(
                label=str(raw.get("label") or ""),
                value=str(raw.get("value") or ""),
                emphasis=str(raw.get("emphasis") or ""),
            )
        )
    main_limits = []
    for raw in data.get("main_limits") if isinstance(data.get("main_limits"), list) else []:
        item = _quota_item_from_dict(raw)
        if item is not None:
            main_limits.append(item)
    additional_groups = []
    for raw in data.get("additional_groups") if isinstance(data.get("additional_groups"), list) else []:
        group = _limit_group_from_dict(raw)
        if group is not None:
            additional_groups.append(group)
    billing = []
    for raw in data.get("billing") if isinstance(data.get("billing"), list) else []:
        item = _billing_item_from_dict(raw)
        if item is not None:
            billing.append(item)
    return ProviderSnapshot(
        key=str(data.get("key") or ""),
        title=str(data.get("title") or ""),
        plan=str(data.get("plan") or "-"),
        ok=bool(data.get("ok")),
        hero_percent=to_float(data.get("hero_percent")),
        hero_caption=str(data.get("hero_caption") or ""),
        bars=bars,
        info_rows=info_rows,
        footer=str(data.get("footer") or ""),
        error=str(data.get("error") or ""),
        dashboard_url=str(data.get("dashboard_url") or ""),
        fetched_at=to_float(data.get("fetched_at")) or 0.0,
        stale=True,
        blocked=bool(data.get("blocked", False)),
        main_limits=main_limits,
        additional_groups=additional_groups,
        billing=billing,
        retry_after=str(data.get("retry_after") or ""),
        internal=dict(data.get("internal")) if isinstance(data.get("internal"), dict) else {},
    )


class CursorAuth:
    def __init__(self) -> None:
        self.access_token = ""
        self.refresh_token = ""
        self.plan = ""

    def load(self) -> None:
        now = time.time()
        cached = _CURSOR_MEM
        token = str(cached.get("access_token") or "")
        exp = to_float(cached.get("exp")) or 0.0
        if token and (exp <= 0 or exp - 90 > now) and now - float(cached.get("loaded_at") or 0) < 600:
            self.access_token = token
            self.refresh_token = str(cached.get("refresh_token") or "")
            self.plan = str(cached.get("plan") or "Cursor")
            return
        db = Path(os.environ.get("APPDATA", "")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        if not db.is_file():
            raise RuntimeError("Cursor 로그인 정보를 찾지 못했습니다. Cursor에 먼저 로그인하세요.")
        uri = db.resolve().as_uri() + "?mode=ro"
        last_error = None
        rowmap = {}
        for _ in range(3):
            try:
                con = sqlite3.connect(uri, uri=True, timeout=SQLITE_RETRY_S)
                try:
                    rows = con.execute(
                        "SELECT key, value FROM ItemTable WHERE key IN (?,?,?)",
                        (
                            "cursorAuth/accessToken",
                            "unused/read-only-widget",
                            "cursorAuth/stripeMembershipType",
                        ),
                    ).fetchall()
                    rowmap = {str(k): ("" if v is None else str(v)) for k, v in rows}
                finally:
                    con.close()
                last_error = None
                break
            except sqlite3.Error as exc:
                last_error = exc
                time.sleep(0.15)
        if last_error and not rowmap:
            raise RuntimeError("Cursor 설정 DB를 읽지 못했습니다. Cursor를 연 뒤 다시 시도하세요.")
        self.access_token = rowmap.get("cursorAuth/accessToken") or ""
        self.refresh_token = rowmap.get("cursorAuth/refreshToken") or ""
        self.plan = rowmap.get("cursorAuth/stripeMembershipType") or "Cursor"
        if not self.access_token:
            raise RuntimeError("Cursor 액세스 토큰이 없습니다. Cursor에 다시 로그인하세요.")
        _CURSOR_MEM.update(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "plan": self.plan,
                "exp": jwt_exp(self.access_token) or 0.0,
                "loaded_at": now,
            }
        )

    def ensure_fresh(self) -> None:
        exp = jwt_exp(self.access_token)
        if exp is not None and exp <= time.time():
            _CURSOR_MEM.clear()
            raise RuntimeError("Cursor 로그인을 갱신한 뒤 새로고침하세요.")


class ChatGptAuth:
    def __init__(self) -> None:
        self.path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "auth.json"
        self.data: dict[str, Any] = {}

    def load(self) -> None:
        if not self.path.is_file():
            raise RuntimeError("ChatGPT 로그인 파일을 찾지 못했습니다. `codex login` 후 다시 실행하세요.")
        mtime = self.path.stat().st_mtime
        cached = _CHATGPT_MEM
        if cached.get("mtime") == mtime and isinstance(cached.get("data"), dict):
            self.data = cached["data"]
            return
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        _CHATGPT_MEM["mtime"] = mtime
        _CHATGPT_MEM["data"] = self.data

    def tokens(self) -> dict[str, Any]:
        raw = self.data.get("tokens")
        return raw if isinstance(raw, dict) else self.data

    def access_token(self) -> str:
        return str(self.tokens().get("access_token") or "")

    def account_id(self) -> str:
        return str(self.tokens().get("account_id") or self.data.get("account_id") or "")

    def ensure_fresh(self) -> None:
        token = self.access_token()
        if not token:
            raise RuntimeError("GPT 사용량 조회를 위해 Codex CLI에 로그인한 뒤 새로고침하세요.")
        exp = jwt_exp(token)
        if exp is not None and exp <= time.time():
            _CHATGPT_MEM.clear()
            raise RuntimeError("Codex CLI 로그인을 갱신한 뒤 새로고침하세요.")


def _cursor_plan_name(headers: dict[str, str], fallback: str) -> str:
    now = time.time()
    if _PLAN_MEM["name"] and float(_PLAN_MEM["until"]) > now:
        return str(_PLAN_MEM["name"])
    try:
        status, body = http_json(
            "POST",
            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo",
            headers,
            {},
        )
        if status < 400:
            info = body.get("planInfo") or {}
            name = str(info.get("planName") or fallback)
            _PLAN_MEM["name"] = name
            _PLAN_MEM["until"] = now + 1800
            return name
    except Exception:
        pass
    return fallback


def fetch_cursor() -> ProviderSnapshot:
    now = time.time()
    auth = CursorAuth()
    auth.load()
    auth.ensure_fresh()
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": USER_AGENT,
    }
    usage_status, usage = http_json(
        "POST",
        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        headers,
        {},
    )
    if usage_status == 401:
        _CURSOR_MEM.clear()
        auth.load()
        auth.ensure_fresh()
        headers["Authorization"] = f"Bearer {auth.access_token}"
        usage_status, usage = http_json(
            "POST",
            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
            headers,
            {},
        )
    if usage_status >= 400:
        raise FetchError(
            f"Cursor 사용량 API 오류 ({usage_status})",
            getattr(usage, "retry_after", ""),
        )

    plan_name = _cursor_plan_name(headers, (auth.plan or "Cursor").title())
    plan_usage = usage.get("planUsage") if isinstance(usage.get("planUsage"), dict) else {}
    auto_used = to_float(plan_usage.get("autoPercentUsed"))
    api_used = to_float(plan_usage.get("apiPercentUsed"))
    # Reference only. Cursor's own overall figure is kept for diagnostics and
    # never becomes quota: no hero, badge, alert, poll interval or history
    # reads it.
    total_used = to_float(plan_usage.get("totalPercentUsed"))
    reset_at = to_float(usage.get("billingCycleEnd"))
    reset_text = fmt_local(reset_at, "reset")
    pools = (
        ("autoPercentUsed", "Cursor Models", auto_used),
        ("apiPercentUsed", "Other Models", api_used),
    )
    main_limits = [
        QuotaItem(
            quota_id=f"cursor:main:{raw_id}",
            source="cursor",
            category="main",
            display_name=label,
            raw_identifier=raw_id,
            used_percent=used,
            remaining_percent=remaining_from_used(used),
            reset_at=reset_at,
            window_label=label,
            scope="global",
        )
        for raw_id, label, used in pools
        if used is not None
    ]
    hero = select_hero_items("cursor", main_limits)
    if hero is None:
        raise RuntimeError("Cursor 모델 한도 정보를 확인할 수 없습니다.")
    bonus = to_float(plan_usage.get("bonusSpend"))
    limit = to_float(plan_usage.get("limit"))
    included = to_float(plan_usage.get("includedSpend"))
    remaining_cents = to_float(plan_usage.get("remaining"))
    billing = []
    for kind, value in (
        ("includedSpend", included),
        ("limit", limit),
        ("remaining", remaining_cents),
        ("bonusSpend", bonus),
        ("remainingBonus", to_float(plan_usage.get("remainingBonus"))),
        ("totalSpend", to_float(plan_usage.get("totalSpend"))),
    ):
        if value is not None:
            billing.append(BillingItem(f"cursor:billing:{kind}", "cursor", kind, value))
    spend = usage.get("spendLimitUsage")
    if isinstance(spend, dict):
        for kind, raw in spend.items():
            billing.append(
                BillingItem(
                    f"cursor:billing:spendLimitUsage.{kind}",
                    "cursor",
                    f"spendLimitUsage.{kind}",
                    json_safe_value(raw),
                )
            )
    info_rows: list[InfoRow] = []
    if limit is not None:
        info_rows.append(InfoRow("기본 포함량", f"{dollars(included or 0.0)} / {dollars(limit)}"))
    footer_parts = [f"{reset_text} 초기화" if reset_text else ""]
    if bonus:
        footer_parts.append(f"보너스 {dollars(bonus)}")
    caption = (
        f"{hero.display_name} 소진"
        if hero.remaining_percent is not None and hero.remaining_percent <= 0
        else f"{hero.display_name} 기준 잔여"
    )
    return ProviderSnapshot(
        key="cursor",
        title="Cursor",
        plan=plan_name,
        ok=True,
        hero_percent=hero.remaining_percent,
        hero_caption=caption,
        main_limits=main_limits,
        info_rows=info_rows,
        footer=" · ".join(part for part in footer_parts if part),
        dashboard_url="https://cursor.com/dashboard/usage",
        fetched_at=time.time(),
        billing=billing,
        internal={"totalPercentUsed": total_used},
    )


def _window_duration(window: dict[str, Any] | None) -> float | None:
    if not isinstance(window, dict):
        return None
    seconds = to_float(window.get("limit_window_seconds"))
    return seconds if seconds is not None and seconds > 0 else None


def _duration_label(seconds: float | None, unknown_index: int = 1) -> str:
    if seconds is None:
        return "기간 미상" if unknown_index <= 1 else f"기간 미상 {unknown_index}"
    if abs(seconds - FIVE_HOURS) <= FIVE_HOUR_TOLERANCE:
        return "5시간"
    if abs(seconds - ONE_WEEK) <= ONE_WEEK_TOLERANCE:
        return "주간"
    days = seconds / 86400
    hours = seconds / 3600
    minutes = seconds / 60
    if seconds >= 86400 and abs(days - round(days)) <= 1 / 24:
        return f"{max(1, round(days))}일"
    if seconds >= 3600 and abs(hours - round(hours)) <= 1 / 60:
        return f"{max(1, round(hours))}시간"
    return f"{max(1, round(minutes))}분"


def _window_reset_at(window: dict[str, Any] | None) -> float | None:
    window = window or {}
    reset_at = to_float(window.get("reset_at"))
    seconds = to_float(window.get("reset_after_seconds"))
    if reset_at is None and seconds is not None:
        reset_at = time.time() + max(0, seconds)
    return reset_at


def _quota_item_from_window(
    *,
    quota_id: str,
    source: str,
    category: str,
    display_name: str,
    raw_identifier: str,
    window: dict[str, Any] | None,
    scope: str,
    metadata: dict[str, Any] | None = None,
) -> QuotaItem:
    window = window or {}
    used = to_float(window.get("used_percent"))
    duration = _window_duration(window)
    return QuotaItem(
        quota_id=quota_id,
        source=source,
        category=category,
        display_name=display_name,
        raw_identifier=raw_identifier,
        window_seconds=duration,
        window_label=display_name,
        used_percent=used,
        remaining_percent=remaining_from_used(used),
        reset_at=_window_reset_at(window),
        scope=scope,
        metadata=metadata or {},
    )


def _looks_like_window(raw: Any) -> bool:
    if not isinstance(raw, dict) or not raw:
        return False
    return any(
        key in raw
        for key in ("used_percent", "limit_window_seconds", "reset_at", "reset_after_seconds")
    )


def _iter_rate_windows(rate: Any) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[int] = set()

    def add(key: str, window: Any) -> None:
        if not _looks_like_window(window):
            return
        marker = id(window)
        if marker in seen:
            return
        seen.add(marker)
        found.append((key, window))

    if isinstance(rate, list):
        for index, window in enumerate(rate):
            add(f"windows[{index}]", window)
        return found
    if not isinstance(rate, dict):
        return found
    for key in ("primary_window", "secondary_window"):
        add(key, rate.get(key))
    extra = rate.get("windows")
    if isinstance(extra, list):
        for index, window in enumerate(extra):
            add(f"windows[{index}]", window)
    for key, value in rate.items():
        if key in ("primary_window", "secondary_window", "windows"):
            continue
        add(str(key), value)
    return found


def additional_groups_from_payload(source: str, body: dict[str, Any] | None) -> list[LimitGroup]:
    # Labels stay labels. Do not infer a selectable model from limit_name.
    source = str(source or "unknown")
    body = body if isinstance(body, dict) else {}
    raw_groups = body.get("additional_rate_limits")
    if raw_groups is None:
        return []
    if not isinstance(raw_groups, list):
        return [
            LimitGroup(
                group_id=f"{source}:additional:payload",
                source=source,
                display_name="추가 한도",
                category="unknown",
                raw_identifier="additional_rate_limits",
                limits=[
                    QuotaItem(
                        f"{source}:additional:payload:item",
                        source,
                        "unknown",
                        "기간 미상",
                        raw_identifier="additional_rate_limits",
                        scope="scoped",
                        metadata={"raw": json_safe_value(raw_groups)},
                    )
                ],
                scope="scoped",
                metadata={"raw": json_safe_value(raw_groups)},
            )
        ]
    groups: list[LimitGroup] = []
    seen: dict[str, int] = {}  # raw ids already taken, so reuse cannot merge groups
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, dict):
            groups.append(
                LimitGroup(
                    group_id=f"{source}:additional:unknown-{index}",
                    source=source,
                    display_name="추가 한도",
                    category="unknown",
                    raw_identifier=f"unknown-{index}",
                    limits=[
                        QuotaItem(
                            f"{source}:additional:unknown-{index}:item",
                            source,
                            "unknown",
                            "기간 미상",
                            raw_identifier=f"unknown-{index}",
                            scope="scoped",
                            metadata={"raw": json_safe_value(raw), "raw_type": type(raw).__name__},
                        )
                    ],
                    scope="scoped",
                    metadata={"raw": json_safe_value(raw), "raw_type": type(raw).__name__},
                )
            )
            continue
        display = str(
            raw.get("display_name")
            or raw.get("limit_name")
            or raw.get("name")
            or ""
        ).strip()
        # display_name is never part of identity, so renaming "Spark" to
        # "Spark Model" keeps the group's id and its saved state.
        raw_id = unique_identifier(
            seen, raw_identifier(raw, f"additional-{index}"), f"additional-{index}"
        )
        group_id = f"{source}:additional:{raw_id}"
        if not display:
            display = str(raw.get("limit_name") or raw.get("name") or raw_id)
        rate = raw.get("rate_limit") if isinstance(raw.get("rate_limit"), dict) else raw
        unknown = 0
        limits: list[QuotaItem] = []
        seen_items: dict[str, int] = {}
        windows = _iter_rate_windows(rate)
        if not windows and _looks_like_window(rate):
            windows = [("window", rate)]
        for window_key, window in windows:
            duration = _window_duration(window)
            if duration is None:
                unknown += 1
            label = _duration_label(duration, unknown)
            # The structural window key is deterministic across polls, so an
            # unknown-duration window keeps one id instead of a new one each
            # time, and two same-length windows never collapse into one.
            item_raw = unique_identifier(seen_items, raw_identifier(window, window_key), window_key)
            leftover = {
                key: json_safe_value(value)
                for key, value in window.items()
                if key not in ("used_percent", "limit_window_seconds", "reset_at", "reset_after_seconds")
            }
            limits.append(
                _quota_item_from_window(
                    quota_id=f"{group_id}:{item_raw}",
                    source=source,
                    category="additional",
                    display_name=label,
                    raw_identifier=item_raw,
                    window=window,
                    scope="scoped",
                    metadata={
                        "window_key": window_key,
                        "raw": leftover,
                    },
                )
            )
        if not limits:
            limits.append(
                QuotaItem(
                    f"{group_id}:unknown",
                    source,
                    str(raw.get("category") or "unknown"),
                    display,
                    raw_identifier=raw_id,
                    scope="scoped",
                    metadata={"raw": json_safe_value(raw)},
                )
            )
        groups.append(
            LimitGroup(
                group_id=group_id,
                source=source,
                display_name=display,
                category=str(raw.get("category") or "additional"),
                raw_identifier=raw_id,
                limits=limits,
                scope="scoped",
                metadata={"raw": json_safe_value(raw)},
            )
        )
    return groups


def _chatgpt_additional_groups(body: dict[str, Any]) -> list[LimitGroup]:
    return additional_groups_from_payload("chatgpt", body)


# Display order for main quota, decided by measured duration alone. Position in
# the payload never decides what a window means.
NAMED_MAIN_WINDOWS = ((FIVE_HOURS, FIVE_HOUR_TOLERANCE), (ONE_WEEK, ONE_WEEK_TOLERANCE))


def _window_rank(seconds: float | None) -> tuple[float, float]:
    if seconds is None:
        # Unknown duration sorts last and keeps its payload order. It is never
        # guessed into a five-hour or weekly window.
        return (2.0, 0.0)
    for rank, (target, tolerance) in enumerate(NAMED_MAIN_WINDOWS):
        if abs(seconds - target) <= tolerance:
            return (0.0, float(rank))
    return (1.0, float(seconds))


def _chatgpt_main_limits(rate: Any) -> list[QuotaItem]:
    """Collect every main window the payload carries, however many there are.

    The number of windows is not fixed at two: a third or tenth window is
    preserved as its own QuotaItem rather than dropped or written over the
    second one. Only the card's display policy decides how many are drawn.
    """
    windows = _iter_rate_windows(rate)
    if not windows and _looks_like_window(rate):
        windows = [("rate_limit", rate)]
    ordered = sorted(
        enumerate(windows),
        key=lambda pair: (_window_rank(_window_duration(pair[1][1])), pair[0]),
    )
    unknown = 0
    taken: dict[str, int] = {}
    items: list[QuotaItem] = []
    for _, (structural_key, window) in ordered:
        duration = _window_duration(window)
        if duration is None:
            unknown += 1
        label = _duration_label(duration, unknown)
        raw_id = unique_identifier(taken, raw_identifier(window, structural_key), structural_key)
        leftover = {
            key: json_safe_value(value)
            for key, value in window.items()
            if key not in ("used_percent", "limit_window_seconds", "reset_at", "reset_after_seconds")
        }
        items.append(
            _quota_item_from_window(
                quota_id=f"chatgpt:main:{raw_id}",
                source="chatgpt",
                category="main",
                display_name=label,
                raw_identifier=raw_id,
                window=window,
                scope="global",
                metadata={"window_key": structural_key, "raw": leftover},
            )
        )
    seen: dict[str, int] = {}
    for item in items:
        seen[item.display_name] = seen.get(item.display_name, 0) + 1
        if seen[item.display_name] > 1:
            # Two windows of the same length still need distinct captions; the
            # suffix is display only and never reaches quota_id.
            item.display_name = f"{item.display_name} {seen[item.display_name]}"
            item.window_label = item.display_name
    return items


def fetch_chatgpt() -> ProviderSnapshot:
    now = time.time()
    auth = ChatGptAuth()
    auth.load()
    auth.ensure_fresh()
    headers = {
        "Authorization": f"Bearer {auth.access_token()}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    account_id = auth.account_id()
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    status, body = http_json("GET", "https://chatgpt.com/backend-api/wham/usage", headers)
    if status == 401:
        _CHATGPT_MEM.clear()
        auth.load()
        auth.ensure_fresh()
        headers["Authorization"] = f"Bearer {auth.access_token()}"
        status, body = http_json("GET", "https://chatgpt.com/backend-api/wham/usage", headers)
    if status >= 400:
        raise FetchError(
            f"ChatGPT 사용량 API 오류 ({status})",
            getattr(body, "retry_after", ""),
        )
    rate = body.get("rate_limit") or {}
    main_limits = _chatgpt_main_limits(rate)
    additional_groups = _chatgpt_additional_groups(body if isinstance(body, dict) else {})
    billing: list[BillingItem] = []
    extras = []
    credits = body.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance") or 0
        billing.append(BillingItem("chatgpt:billing:credits", "chatgpt", "credits", json_safe_value(balance)))
        extras.append(f"크레딧 {balance}")
    reset_credits = body.get("rate_limit_reset_credits") or {}
    available = to_int(reset_credits.get("available_count"))
    if available:
        billing.append(
            BillingItem("chatgpt:billing:reset_credits", "chatgpt", "reset_credits", available)
        )
        extras.append(f"리셋권 {available}")
    plan = str(body.get("plan_type") or "ChatGPT").title()
    # rate_limit may arrive as a list of windows; read its flags defensively.
    flags = rate if isinstance(rate, dict) else {}
    hero = select_hero_items("chatgpt", main_limits)
    if hero is None:
        raise RuntimeError("사용량 응답에 한도 정보가 없습니다.")
    exhausted = exhausted_names(main_limits)
    blocked = (bool(flags.get("limit_reached")) or bool(exhausted)
               or flags.get("allowed") is False)
    caption = (f"{' · '.join(exhausted)} 소진" if exhausted else
               "사용 제한 · 상세 확인" if blocked else f"{hero.display_name} 기준 잔여")
    info_rows = [
        InfoRow(item.display_name, legacy_detail(item.remaining_percent)) for item in main_limits
    ]
    if extras:
        info_rows.append(InfoRow("추가", " · ".join(extras)))
    return ProviderSnapshot(
        key="chatgpt",
        title="GPT",
        plan="ChatGPT " + plan,
        ok=True,
        hero_percent=hero.remaining_percent,
        blocked=blocked,
        hero_caption=caption,
        main_limits=main_limits,
        additional_groups=additional_groups,
        info_rows=info_rows,
        footer="",
        billing=billing,
        dashboard_url="https://chatgpt.com/codex/settings/usage",
        fetched_at=time.time(),
    )


CLAUDE_DASHBOARD_URL = "https://claude.ai/settings/usage"
# The CLI needs a few seconds for a control request. Stay under the 15s worker
# deadline so a slow answer reports itself instead of being killed as a timeout.
CLAUDE_CLI_TIMEOUT = 12.0


def _claude_quota_item(
    raw_id: str, window: dict[str, Any], now: float, source: str = "claude_statusline"
) -> QuotaItem | None:
    from claude_bridge import KNOWN_WINDOWS, window_expired

    spec = KNOWN_WINDOWS.get(raw_id)
    if spec is None or not isinstance(window, dict):
        return None
    if window_expired(window, now):
        return None
    used = to_float(window.get("used_percent"))
    remaining = to_float(window.get("remaining_percent"))
    if remaining is None:
        remaining = remaining_from_used(used)
    reset = to_float(window.get("resets_at"))
    if used is None and remaining is None:
        return None
    return QuotaItem(
        quota_id=f"claude:{raw_id}",
        source=source,
        # category says where a quota belongs (main vs additional); the meter
        # kind the CLI reports is metadata, not a placement.
        category="main",
        display_name=str(spec["display_name"]),
        raw_identifier=raw_id,
        window_seconds=float(spec["window_seconds"]),
        window_label=str(spec["window_label"]),
        used_percent=used,
        remaining_percent=remaining,
        reset_at=reset,
        scope="global",
        metadata={"kind": str(spec["category"])},
    )


def _claude_items(windows: dict[str, Any], now: float, source: str) -> list[QuotaItem]:
    from claude_bridge import WINDOW_KEYS

    items = []
    for key in WINDOW_KEYS:
        raw = windows.get(key)
        if isinstance(raw, dict):
            item = _claude_quota_item(key, raw, now, source=source)
            if item is not None:
                items.append(item)
    return items


def _claude_snapshot(
    items: list[QuotaItem],
    *,
    observed: float,
    stale: bool,
    footer: str,
    source: str,
    internal: dict[str, Any] | None = None,
) -> ProviderSnapshot:
    """Shared card shape for both Claude sources, so hero and captions match."""
    meta = {"quota_observed_at": observed, "source": source}
    meta.update(internal or {})
    hero = select_hero_items("claude", items)
    if hero is None:
        return ProviderSnapshot(
            key="claude",
            title="Claude",
            plan="Claude",
            ok=False,
            hero_percent=None,
            hero_caption="사용량 없음",
            error="사용량 창을 기다리는 중",
            dashboard_url=CLAUDE_DASHBOARD_URL,
            fetched_at=observed,
            stale=stale,
            footer="Claude Code가 다음 응답 후 한도를 다시 제공합니다.",
            internal=meta,
        )
    remaining = hero.remaining_percent
    exhausted = exhausted_names(items)
    caption = (
        f"{' · '.join(exhausted)} 소진"
        if exhausted
        else f"{hero.display_name} 기준 잔여"
    )
    return ProviderSnapshot(
        key="claude",
        title="Claude",
        plan="Claude",
        ok=True,
        hero_percent=remaining,
        hero_caption=caption,
        main_limits=items,
        blocked=bool(exhausted),
        stale=stale,
        footer=footer,
        dashboard_url=CLAUDE_DASHBOARD_URL,
        fetched_at=observed,
        internal=meta,
    )


def parse_iso_epoch(value: Any) -> float | None:
    """Parse the CLI's ISO 8601 reset stamps. statusLine sends unix seconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


# The CLI names its global windows by meter kind. weekly_scoped rows are model
# or surface scoped, so they are deliberately absent here.
CLAUDE_CLI_KINDS = {
    "session": "five_hour",
    "five_hour": "five_hour",
    "weekly_all": "seven_day",
    "seven_day": "seven_day",
}


def _claude_cli_used_percent(rows: list[dict[str, Any]]) -> float | None:
    """Require an explicit percent, scale, or cross-validation; never guess."""
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except OverflowError:
            return None
        return value if math.isfinite(value) else None

    percents = []
    utilizations = []
    for row in rows:
        if "percent" in row:
            value = number(row["percent"])
            if value is None or not 0 <= value <= 100:
                return None
            percents.append(value)
        if "utilization" in row:
            value = number(row["utilization"])
            if value is None or value < 0:
                return None
            scale = row.get("utilization_scale")
            if scale in ("percent", "fraction"):
                value *= 100 if scale == "fraction" else 1
                if value > 100:
                    return None
                percents.append(value)
            elif scale is not None:
                return None
            else:
                utilizations.append(value)
    if not percents:
        return None
    used = percents[0]
    if any(not math.isclose(used, value, abs_tol=1e-6) for value in percents[1:]):
        return None
    for value in utilizations:
        if not (math.isclose(used, value, abs_tol=1e-6)
                or (0 <= value <= 1 and math.isclose(used, value * 100, abs_tol=1e-6))):
            return None
    return used


def claude_windows_from_rate_limits(rate_limits: Any, limits: Any = None) -> dict[str, dict[str, Any]]:
    """Use semantic kinds and validated used percentages, never display labels."""
    from claude_bridge import WINDOW_KEYS

    rows: dict[str, list[dict[str, Any]]] = {key: [] for key in WINDOW_KEYS}
    if limits is None and isinstance(rate_limits, dict):
        # The CLI carries its kind-classified rows inside rate_limits.
        limits = rate_limits.get("limits")
    def window_key(kind: Any) -> str | None:
        return CLAUDE_CLI_KINDS.get(kind) if isinstance(kind, str) else None

    if isinstance(limits, list):
        for raw in limits:
            if not isinstance(raw, dict):
                continue
            key = window_key(raw.get("kind"))
            if key is not None:
                rows[key].append(raw)
    if isinstance(rate_limits, dict):
        for raw_key, raw in rate_limits.items():
            if not isinstance(raw, dict):
                continue
            key = window_key(raw.get("kind", raw_key))
            if key is not None:
                rows[key].append(raw)
    windows: dict[str, dict[str, Any]] = {}
    for key, candidates in rows.items():
        used = _claude_cli_used_percent(candidates)
        if used is None:
            continue
        window: dict[str, Any] = {"used_percent": used, "remaining_percent": remaining_from_used(used)}
        for raw in candidates:
            reset = parse_iso_epoch(raw.get("resets_at"))
            if reset is not None:
                window["resets_at"] = reset
                break
        windows[key] = window
    return windows


def claude_usage_from_control_output(raw_text: str, now: float | None = None) -> ProviderSnapshot:
    """Read one `get_usage` control_response. Pure, so the CLI call stays testable."""
    current = time.time() if now is None else float(now)
    body: dict[str, Any] | None = None
    for line in str(raw_text).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "control_response":
            continue
        response = record.get("response")
        if (not isinstance(response, dict) or response.get("subtype") != "success"
                or response.get("request_id", "usage") != "usage"):
            continue
        inner = response.get("response")
        if isinstance(inner, dict):
            body = inner
    if body is None:
        return error_snapshot(
            "claude",
            "Claude",
            "Claude Code가 사용량을 돌려주지 않았습니다. 자동 재시도합니다.",
            CLAUDE_DASHBOARD_URL,
        )
    if body.get("rate_limits_available") is False:
        return error_snapshot(
            "claude",
            "Claude",
            "이 계정에서는 플랜 한도를 제공하지 않습니다.",
            CLAUDE_DASHBOARD_URL,
        )
    windows = claude_windows_from_rate_limits(body.get("rate_limits"), body.get("limits"))
    if not windows or body.get("rate_limits_available", True) is not True:
        snap = error_snapshot("claude", "Claude", "Claude Code 사용량 형식을 확인할 수 없습니다. 지원되는 응답을 기다립니다.", CLAUDE_DASHBOARD_URL)
        snap.internal["feature_available"] = False
        return snap
    items = _claude_items(windows, current, "claude_cli")
    return _claude_snapshot(
        items,
        observed=current,
        stale=False,
        footer="",
        source="claude_cli",
        internal={"feature_available": bool(items)},
    )


def _claude_cli_executable() -> Path | None:
    from claude_integration import resolve_claude_executable

    return resolve_claude_executable()


def fetch_claude_cli(now: float | None = None) -> ProviderSnapshot:
    """Ask the installed Claude Code for plan usage. No prompt, so no token spend."""
    import subprocess
    from claude_integration import claude_ready

    current = time.time() if now is None else float(now)
    deadline = time.monotonic() + CLAUDE_CLI_TIMEOUT
    executable = _claude_cli_executable()
    if executable is None:
        return error_snapshot(
            "claude",
            "Claude",
            "Claude Code를 찾지 못했습니다. 설치 후 다시 시도하세요.",
            CLAUDE_DASHBOARD_URL,
        )
    ready, detail = claude_ready(executable=executable)
    if not ready:
        return error_snapshot("claude", "Claude", detail, CLAUDE_DASHBOARD_URL)
    request = json.dumps(
        {
            "type": "control_request",
            "request_id": "usage",
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        },
        ensure_ascii=True,
    )
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-p",
                "--output-format", "stream-json",
                "--input-format", "stream-json",
                "--verbose",
            ],
            input=(request + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.1, deadline - time.monotonic()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return error_snapshot(
            "claude",
            "Claude",
            "Claude Code 조회가 시간을 초과했습니다. 자동 재시도합니다.",
            CLAUDE_DASHBOARD_URL,
        )
    except OSError:
        return error_snapshot(
            "claude",
            "Claude",
            "Claude Code를 실행하지 못했습니다. 로그인 상태를 확인하세요.",
            CLAUDE_DASHBOARD_URL,
        )
    text = (completed.stdout or b"").decode("utf-8", "replace")
    snap = claude_usage_from_control_output(text, current)
    if not snap.ok:
        # get_usage reports rate_limits_available=false for both signed-out
        # and unsupported accounts. Ask the CLI rather than reading credentials.
        try:
            status = subprocess.run(
                [str(executable), "auth", "status"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            auth = json.loads(status.stdout)
            if isinstance(auth, dict) and auth.get("loggedIn") is False:
                snap.error = "Claude Code 로그인이 필요합니다. 우클릭 → Claude 로그인에서 연결하세요."
                snap.internal["requires_login"] = True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return snap


def fetch_claude(now: float | None = None) -> ProviderSnapshot:
    from claude_bridge import (
        WINDOW_KEYS,
        list_session_caches,
        quota_stale,
        select_session_cache,
        session_inactive,
    )
    from claude_integration import is_installed

    current = time.time() if now is None else float(now)
    if not is_installed():
        return error_snapshot(
            "claude",
            "Claude",
            "Claude 연동을 켜면 대화형 Claude Code 사용량을 표시합니다.",
            CLAUDE_DASHBOARD_URL,
        )
    # This two-second UI path reads only validated cache. Version and schema
    # are established by the statusLine payload, or by the separate CLI worker.
    selected = select_session_cache(list_session_caches(current), current)
    if selected is None:
        # statusLine only runs in terminal Claude Code, so the widget asks the
        # CLI itself. That answer arrives through the worker, not from here.
        return error_snapshot(
            "claude",
            "Claude",
            "Claude Code에서 사용량을 가져오는 중입니다.",
            CLAUDE_DASHBOARD_URL,
        )
    windows = {key: selected.get(key) for key in WINDOW_KEYS}
    items = _claude_items(windows, current, "claude_statusline")
    stale = quota_stale(selected, current)
    observed = to_float(selected.get("quota_observed_at")) or current
    footer = ""
    if stale:
        footer = (
            "이전 데이터 · Claude Code 세션이 없습니다"
            if session_inactive(selected, current)
            else "이전 데이터"
        )
    return _claude_snapshot(
        items,
        observed=observed,
        stale=stale,
        footer=footer,
        source="claude_statusline",
        internal={"bridge_seen_at": selected.get("bridge_seen_at")},
    )


def error_snapshot(key: str, title: str, message: str, dashboard_url: str, retry_after: str = "") -> ProviderSnapshot:
    return ProviderSnapshot(
        key=key,
        title=title,
        plan="-",
        ok=False,
        hero_percent=None,
        hero_caption="조회 실패",
        error=message,
        dashboard_url=dashboard_url,
        fetched_at=time.time(),
        retry_after=str(retry_after or ""),
    )

