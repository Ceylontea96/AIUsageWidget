"""The widget's own dialogs: buttons, checkboxes and windows drawn in its style.

Tk's stock checkbutton, button and message box are Windows controls: a
light square tick box, flat grey buttons, and text that stays one size
however large the widget is drawn. These are drawn like the widget instead,
anti-aliased, and sized from its Metrics.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

import widget_raster as raster
from widget_theme import *  # noqa: F401,F403  colours, fonts and sizes

# Fills of the two kinds of button: the call to action, and the rest.
PRIMARY = (CODEX, BG)
SECONDARY = (CHIP_TRACK, TEXT)
CHECK_OFF_BORDER = '#4A5060'


class ThemedButton(tk.Canvas):
    """A pill button. invoke(), Return and Space run its command, as Tk's do."""

    def __init__(self, parent, text, command, metrics, kind=SECONDARY, bg=BG):
        self.metrics, self.kind, self.command, self.text = metrics, kind, command, text
        self._hover = False
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0, cursor='hand2', takefocus=True)
        self.bind('<Button-1>', lambda e: self.invoke())
        self.bind('<Return>', lambda e: self.invoke())
        self.bind('<space>', lambda e: self.invoke())
        self.bind('<Enter>', lambda e: self._set_hover(True))
        self.bind('<Leave>', lambda e: self._set_hover(False))
        self._draw()

    def invoke(self):
        if self.command:
            return self.command()

    def set_label(self, text, command=None):
        """New text, and command if given; the button keeps its place."""
        if command is not None:
            self.command = command
        if text != self.text:
            self.text = text
            self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _draw(self):
        m = self.metrics
        font = m.font(FONT_BADGE)
        width = tkfont.Font(root=self, font=font).measure(self.text) + 2 * m.p(12)
        height = m.p(26)
        fill, fg = self.kind
        if self._hover:
            fill = raster.lighten(fill, 0.12)
        self.configure(width=width, height=height)
        self.delete('all')
        self._image = tk.PhotoImage(data=raster.pill_png(width, height, fill, self.cget('bg')), format='png')
        self.create_image(0, 0, image=self._image, anchor='nw')
        self.create_text(width / 2, height / 2, text=self.text, fill=fg, font=font)


class ThemedCheck(tk.Canvas):
    """A checkbox and its label, bound to a BooleanVar like Tk's checkbutton."""

    def __init__(self, parent, text, variable, metrics, bg=BG, font=None):
        self.metrics, self.text, self.variable = metrics, text, variable
        self._font = font or FONT_SERVICE
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0, cursor='hand2', takefocus=True)
        self.bind('<Button-1>', lambda e: self.invoke())
        self.bind('<space>', lambda e: self.invoke())
        self._trace = variable.trace_add('write', lambda *a: self._draw())
        self.bind('<Destroy>', self._untrace)
        self._draw()

    def invoke(self):
        self.variable.set(not self.variable.get())

    def _untrace(self, event=None):
        if event is not None and event.widget is not self:
            return
        try:
            self.variable.trace_remove('write', self._trace)
        except (tk.TclError, ValueError):
            pass

    def _draw(self):
        m = self.metrics
        box, gap = m.p(18), m.p(10)
        font = m.font(self._font)
        # measure() comes up a pixel or two short of the drawn text; the slack keeps the last letter whole.
        width = box + gap + tkfont.Font(root=self, font=font).measure(self.text) + m.p(4)
        height = max(box, m.p(24))
        self.configure(width=width, height=height)
        self.delete('all')
        on = bool(self.variable.get())
        self._image = tk.PhotoImage(data=raster.checkbox_png(box, on, CODEX, CHECK_OFF_BORDER, BG, self.cget('bg')),
                                    format='png')
        self.create_image(0, (height - box) // 2, image=self._image, anchor='nw')
        self.create_text(box + gap, height / 2, text=self.text, anchor='w', fill=TEXT, font=font)


def styled_window(root, title):
    """A dark, fixed-size, topmost Toplevel for one of the widget's dialogs."""
    window = tk.Toplevel(root)
    window.withdraw()
    window.title(title)
    window.configure(bg=BG)
    window.resizable(False, False)
    window.transient(root)
    try:
        window.attributes('-topmost', True)
    except tk.TclError:
        pass
    return window


def dialog_label(parent, text, metrics, font=FONT_SUB, fg=TEXT, bg=BG, wrap=0, **pack):
    """A text label sized from metrics; wrap is a width in design pixels."""
    widget = tk.Label(parent, text=text, bg=bg, fg=fg, font=metrics.font(font), justify='left', anchor='w',
                      bd=0, padx=0, pady=0, wraplength=metrics.p(wrap) if wrap else 0)
    if pack:
        widget.pack(**pack)
    return widget


def dialog_rule(parent, metrics, **pack):
    """A hairline across the dialog, like the ones between the widget's cards."""
    line = tk.Frame(parent, bg=HAIR, height=max(1, metrics.p(1)))
    line.pack(fill='x', **pack)
    return line


def swatch(parent, fill, metrics, ring=None, bg=BG):
    """A small chip in one of the compact row's colours, ringed like a chip with a low quota."""
    width, height = metrics.p(30), metrics.p(14)
    canvas = tk.Canvas(parent, width=width, height=height, bg=bg, bd=0, highlightthickness=0)
    if ring:
        from bar_raster import ringed_progress_rgba
        w, h, rows = ringed_progress_rgba(width, height, height, metrics.p(2, 1), ring, width, CHIP_TRACK, fill, bg)
        data = raster.png_rgba(w, h, rows, level=1)
    else:
        data = raster.pill_png(width, height, fill, bg)
    canvas._image = tk.PhotoImage(data=data, format='png')
    canvas.create_image(0, 0, image=canvas._image, anchor='nw')
    return canvas
