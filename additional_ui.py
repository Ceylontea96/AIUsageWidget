"""Provider-independent Additional Limits layout and widget."""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk

from providers import LimitGroup, fmt_local


WARN_AT = 30
DANGER_AT = 15
ROW_H = 28
HEADING_H = 22
WINDOW_ROW_H = 36
TOGGLE_H = 28


@dataclass(frozen=True)
class AdditionalRow:
    kind: str
    text: str
    percent: float | None = None
    reset_text: str = ""
    warn: bool = False
    group_id: str = ""
    quota_id: str = ""


def additional_count(groups) -> int:
    return len(groups or [])


def additional_toggle_text(count: int, expanded: bool = False) -> str:
    if count <= 0:
        return ""
    arrow = "▲" if expanded else "▾"
    noun = "추가 한도 1개" if count == 1 else f"추가 한도 {count}개"
    return f"{noun} {arrow}"


def _reset_stamp(item) -> str:
    reset_at = getattr(item, "reset_at", None)
    if reset_at:
        try:
            return fmt_local(reset_at, "reset")
        except Exception:
            return ""
    return ""


def layout_additional(groups) -> list[AdditionalRow]:
    rows: list[AdditionalRow] = []
    for group in groups or []:
        limits = list(getattr(group, "limits", None) or [])
        heading = str(getattr(group, "display_name", "") or "").strip()
        group_id = str(getattr(group, "group_id", "") or "")
        show_heading = True
        if len(limits) == 1:
            window_label = str(getattr(limits[0], "window_label", "") or getattr(limits[0], "display_name", "") or "").strip()
            if not heading or heading == window_label:
                show_heading = False
        if show_heading and heading:
            rows.append(AdditionalRow("heading", heading, group_id=group_id))
        if not limits:
            rows.append(AdditionalRow("window", heading or "추가 한도", group_id=group_id, warn=False))
            continue
        for item in limits:
            label = str(getattr(item, "window_label", "") or getattr(item, "display_name", "") or "기간 미상")
            remaining = getattr(item, "remaining_percent", None)
            warn = remaining is not None and remaining <= WARN_AT
            rows.append(
                AdditionalRow(
                    "window",
                    label,
                    percent=remaining,
                    reset_text=_reset_stamp(item),
                    warn=warn,
                    group_id=group_id,
                    quota_id=str(getattr(item, "quota_id", "") or ""),
                )
            )
    return rows


def additional_content_height(rows: list[AdditionalRow], metrics_p) -> int:
    total = 0
    for row in rows:
        if row.kind == "heading":
            total += metrics_p(HEADING_H)
            continue
        total += metrics_p(WINDOW_ROW_H)
        if row.reset_text:
            total += metrics_p(14)
    return total


class AdditionalBlock(tk.Frame):
    """Generic additional-limits renderer. Does not interpret provider keys."""

    def __init__(self, parent, metrics, on_toggle=None, **kwargs):
        super().__init__(parent, bg=kwargs.get("bg", parent.cget("bg")), highlightthickness=0)
        self.metrics = metrics
        self.on_toggle = on_toggle
        self.groups: list[LimitGroup] = []
        self.expanded = False
        self.max_body = 0
        self.height = 0
        self._rows: list[AdditionalRow] = []
        bg = kwargs.get("bg", parent.cget("bg"))
        self.header = tk.Canvas(self, height=1, bg=bg, bd=0, highlightthickness=0, cursor="hand2")
        self.header.pack(fill="x")
        self.header.bind("<Button-1>", self._clicked)
        self.body = tk.Canvas(self, height=1, bg=bg, bd=0, highlightthickness=0)
        self._yscroll = 0
        self.body.bind("<MouseWheel>", self._wheel)

    def _clicked(self, _event=None):
        if self.on_toggle:
            self.on_toggle()

    def _wheel(self, event):
        if not self.expanded or self.body.winfo_height() <= 1:
            return
        delta = -1 if event.delta > 0 else 1
        self.body.yview_scroll(delta, "units")

    def set_metrics(self, metrics):
        self.metrics = metrics

    def render(self, groups, *, expanded: bool, max_body: int, colors: dict):
        self.groups = list(groups or [])
        self.expanded = bool(expanded) and additional_count(self.groups) > 0
        self.max_body = max(0, int(max_body or 0))
        self._rows = layout_additional(self.groups)
        count = additional_count(self.groups)
        bg = colors.get("bg")
        muted = colors.get("muted")
        text = colors.get("text")
        warn = colors.get("warn")
        danger = colors.get("danger")
        track = colors.get("track")
        dim = colors.get("dim")
        m = self.metrics
        if count <= 0:
            self.header.configure(width=m.card_w, height=1)
            self.header.delete("all")
            self.body.pack_forget()
            self.body.configure(height=1)
            self.height = 0
            self.configure(width=m.card_w, height=1)
            return
        toggle_h = m.p(TOGGLE_H)
        self.header.configure(width=m.card_w, height=toggle_h, bg=bg)
        self.header.delete("all")
        self.header.create_text(
            m.p(16), toggle_h // 2,
            text=additional_toggle_text(count, self.expanded),
            fill=muted, anchor="w", font=m.font(colors.get("font_meta")),
            tags="toggle",
        )
        if not self.expanded:
            self.body.pack_forget()
            self.body.configure(height=1)
            self.height = toggle_h
            self.configure(width=m.card_w, height=self.height)
            return
        content_h = additional_content_height(self._rows, m.p)
        budget = max(0, self.max_body)
        if budget <= 0:
            view_h = min(content_h, m.p(WINDOW_ROW_H))
        else:
            view_h = min(content_h, budget)
        view_h = max(1, view_h)
        self.body.pack(fill="x")
        self.body.configure(width=m.card_w, height=view_h, bg=bg, scrollregion=(0, 0, m.card_w, content_h))
        self.body.delete("all")
        y = 0
        for row in self._rows:
            if row.kind == "heading":
                self.body.create_text(
                    m.p(16), y + m.p(4), text=row.text, fill=text, anchor="nw",
                    font=m.font(colors.get("font_row")), tags="heading",
                )
                y += m.p(HEADING_H)
                continue
            color = muted
            if row.percent is not None and row.percent <= DANGER_AT:
                color = danger
            elif row.warn:
                color = warn
            self.body.create_text(
                m.p(16), y, text=row.text, fill=color, anchor="nw",
                font=m.font(colors.get("font_row")), tags="window",
            )
            value = "—" if row.percent is None else f"{row.percent:.0f}%"
            self.body.create_text(
                m.card_w - m.p(16), y, text=value, fill=text, anchor="ne",
                font=m.font(colors.get("font_value")), tags="percent",
            )
            bar_y = y + m.p(18)
            bar_w = m.card_w - m.p(32)
            bar_h = max(4, m.p(8))
            self.body.create_rectangle(m.p(16), bar_y, m.p(16) + bar_w, bar_y + bar_h, fill=track, outline="", tags="track")
            fill_w = 0 if row.percent is None else int(bar_w * max(0, min(100, row.percent)) / 100)
            if fill_w:
                self.body.create_rectangle(m.p(16), bar_y, m.p(16) + fill_w, bar_y + bar_h, fill=color, outline="", tags="fill")
            y += m.p(WINDOW_ROW_H)
            if row.reset_text:
                self.body.create_text(
                    m.p(16), y - m.p(2), text=row.reset_text, fill=dim, anchor="nw",
                    font=m.font(colors.get("font_meta") or colors.get("font_row")), tags="reset",
                )
                y += m.p(14)
        self.height = toggle_h + view_h
        self.configure(width=m.card_w, height=self.height)


# Palette keys are visual tokens passed in by the host. This module does not
# look up provider names or raw API fields.
