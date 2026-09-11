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


@dataclass
class InfoRow:
    label: str
    value: str
    emphasis: str = ""


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
                return int(getattr(resp, "status", 200) or 200), {}
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise RuntimeError("사용량 응답 형식이 올바르지 않습니다.")
            return int(getattr(resp, "status", 200) or 200), parsed
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        return status, {}
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
                "remaining_percent": bar.remaining_percent,
                "used_percent": bar.used_percent,
                "detail": bar.detail,
                "reset_text": bar.reset_text,
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
    }


def snapshot_from_dict(data: dict[str, Any]) -> ProviderSnapshot:
    bars = []
    raw_bars = data.get("bars")
    for raw in (raw_bars if isinstance(raw_bars, list) else [])[:8]:
        if not isinstance(raw, dict):
            continue
        bars.append(
            QuotaBar(
                label=str(raw.get("label") or ""),
                remaining_percent=to_float(raw.get("remaining_percent")),
                used_percent=to_float(raw.get("used_percent")),
                detail=str(raw.get("detail") or ""),
                reset_text=str(raw.get("reset_text") or ""),
            )
        )
    info_rows = []
    raw_rows = data.get("info_rows")
    for raw in (raw_rows if isinstance(raw_rows, list) else [])[:8]:
        if not isinstance(raw, dict):
            continue
        info_rows.append(
            InfoRow(
                label=str(raw.get("label") or ""),
                value=str(raw.get("value") or ""),
                emphasis=str(raw.get("emphasis") or ""),
            )
        )
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
            raise RuntimeError("Codex에 로그인한 뒤 새로고침하세요.")
        exp = jwt_exp(token)
        if exp is not None and exp <= time.time():
            _CHATGPT_MEM.clear()
            raise RuntimeError("Codex 로그인을 갱신한 뒤 새로고침하세요.")


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
        raise RuntimeError(f"Cursor 사용량 API 오류 ({usage_status})")

    plan_name = _cursor_plan_name(headers, (auth.plan or "Cursor").title())
    plan_usage = usage.get("planUsage") or {}
    auto_used = to_float(plan_usage.get("autoPercentUsed"))
    api_used = to_float(plan_usage.get("apiPercentUsed"))
    total_used = to_float(plan_usage.get("totalPercentUsed"))
    reset_text = fmt_local(usage.get("billingCycleEnd"), "reset")
    bars = []
    if auto_used is not None:
        bars.append(
            QuotaBar("자사 모델", remaining_from_used(auto_used), auto_used, f"{auto_used:.0f}% 사용", reset_text)
        )
    if api_used is not None:
        bars.append(
            QuotaBar("API 사용량", remaining_from_used(api_used), api_used, f"{api_used:.0f}% 사용", reset_text)
        )
    bonus = to_float(plan_usage.get("bonusSpend")) or 0.0
    limit = to_float(plan_usage.get("limit"))
    included = to_float(plan_usage.get("includedSpend")) or 0.0
    remaining_cents = to_float(plan_usage.get("remaining"))
    if remaining_cents is None and limit is not None:
        remaining_cents = max(0.0, limit - included)
    info_rows: list[InfoRow] = []
    if auto_used is not None:
        info_rows.append(InfoRow("자사 모델", f"{auto_used:.0f}% 사용"))
    if api_used is not None:
        info_rows.append(InfoRow("API 사용량", f"{api_used:.0f}% 사용"))
    if limit is not None:
        info_rows.append(
            InfoRow(
                "기본 포함량",
                f"{dollars(included)} / {dollars(limit)}",
                "",
            )
        )
    footer_parts = [f"{reset_text} 초기화" if reset_text else ""]
    if bonus:
        footer_parts.append(f"보너스 {dollars(bonus)}")
    hero = remaining_from_used(total_used)
    if hero is None:
        raise RuntimeError("Cursor 전체 한도 정보를 확인할 수 없습니다.")
    used_label = f"{total_used:.0f}% 사용" if total_used is not None else "30일 한도"
    caption = "전체 잔여"
    return ProviderSnapshot(
        key="cursor",
        title="Cursor",
        plan=plan_name,
        ok=True,
        hero_percent=hero,
        hero_caption=caption,
        bars=bars,
        info_rows=info_rows,
        footer=" · ".join(part for part in footer_parts if part),
        dashboard_url="https://cursor.com/dashboard/usage",
        fetched_at=time.time(),
    )


def _window_bar(label: str, window: dict[str, Any] | None) -> QuotaBar:
    window = window or {}
    used = to_float(window.get("used_percent"))
    reset_at = to_float(window.get("reset_at"))
    seconds = to_float(window.get("reset_after_seconds"))
    if reset_at is None and seconds is not None:
        reset_at = time.time() + max(0, seconds)
    reset = fmt_local(reset_at, "reset")
    remaining = remaining_from_used(used)
    detail = "잔여 --" if remaining is None else f"잔여 {remaining:.0f}%"
    return QuotaBar(label, remaining, used, detail, reset)


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
        raise RuntimeError(f"ChatGPT 사용량 API 오류 ({status})")
    rate = body.get("rate_limit") or {}
    primary = rate.get("primary_window") if isinstance(rate, dict) else None
    secondary = rate.get("secondary_window") if isinstance(rate, dict) else None
    five = _window_bar("5시간", primary if isinstance(primary, dict) else None)
    week = _window_bar("주간", secondary if isinstance(secondary, dict) else None)
    extras = []
    credits = body.get("credits") or {}
    if credits.get("has_credits"):
        extras.append(f"크레딧 {credits.get('balance') or 0}")
    reset_credits = body.get("rate_limit_reset_credits") or {}
    available = to_int(reset_credits.get("available_count"))
    if available:
        extras.append(f"리셋권 {available}")
    plan = str(body.get("plan_type") or "ChatGPT").title()
    reached = bool(rate.get("limit_reached")) if isinstance(rate, dict) else False
    known = [b for b in (five, week) if b.remaining_percent is not None]
    if not known:
        raise RuntimeError("사용량 응답에 한도 정보가 없습니다.")
    tightest = min(known, key=lambda b: b.remaining_percent)
    exhausted = [b.label for b in known if b.remaining_percent <= 0]
    blocked = reached or bool(exhausted) or rate.get("allowed") is False
    caption = (" · ".join(exhausted) + " 소진") if exhausted else ("사용 제한 · 상세 확인" if blocked else f"{tightest.label} 기준 잔여")
    footer = ""
    info_rows = [
        InfoRow("5시간", five.detail),
        InfoRow("주간", week.detail),
    ]
    if extras:
        info_rows.append(InfoRow("추가", " · ".join(extras)))
    return ProviderSnapshot(
        key="chatgpt",
        title="Codex",
        plan="ChatGPT " + plan,
        ok=True,
        hero_percent=tightest.remaining_percent,
        blocked=blocked,
        hero_caption=caption,
        bars=[five, week],
        info_rows=info_rows,
        footer=footer,
        dashboard_url="https://chatgpt.com/codex/settings/usage",
        fetched_at=time.time(),
    )


def error_snapshot(key: str, title: str, message: str, dashboard_url: str) -> ProviderSnapshot:
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
    )

