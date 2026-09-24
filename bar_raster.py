"""Progress-bar pixels without redoing the geometry every frame.

A bar is a rounded track with a clipped fill. How much of a pixel the track
and the fill cover depends only on the shape, and along the straight middle
of a row it is the same value for every column. So each row is kept as a few
runs of equal coverage plus the exact columns under the round ends. That
layout is cached per shape, and a frame only mixes colours, once per run.

`progress_rgba` returns the same bytes as computing every pixel on its own
(`usage_widget.progress_bar_rgba` with samples=1); the tests compare the two.
"""
from __future__ import annotations

import math
from functools import lru_cache


def cover_round_rect(px, py, width, height, radius):
    if width <= 0 or height <= 0:
        return 0.0
    radius = max(0.0, min(float(radius), width / 2.0, height / 2.0))
    dx = abs(px - width / 2.0) - (width / 2.0 - radius)
    dy = abs(py - height / 2.0) - (height / 2.0 - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - radius
    return max(0.0, min(1.0, 0.5 - outside))


def _hex_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def _coverage_runs(width, py, rect_width, shape_height, radius):
    """Coverage of one rounded rect along a row as [x0, x1, value] runs.

    A column's value ignores its x whenever dx < 0 and dx < dy, because
    cover_round_rect then reduces to a function of dy. Those columns form
    one contiguous middle run that takes a single computed value; only the
    columns under the round ends are computed one by one.
    """
    if rect_width <= 0 or shape_height <= 0:
        return [(0, width, 0.0)]
    r = max(0.0, min(float(radius), rect_width / 2.0, shape_height / 2.0))
    dy = abs(py - shape_height / 2.0) - (shape_height / 2.0 - r)
    limit = min(0.0, dy)
    half, straight = rect_width / 2.0, rect_width / 2.0 - r

    def plain(x):
        return abs(x + 0.5 - half) - straight < limit

    # Past rect_width + 0.5 a column is at least one pixel outside: 0.
    last = min(width, max(0, int(math.ceil(rect_width + 0.5))))
    start = 0
    while start < last and not plain(start):
        start += 1
    end = last
    while end > start and not plain(end - 1):
        end -= 1
    runs = [(x, x + 1, cover_round_rect(x + 0.5, py, rect_width, shape_height, radius)) for x in range(start)]
    if end > start:
        runs.append((start, end, cover_round_rect(start + 0.5, py, rect_width, shape_height, radius)))
    runs += [(x, x + 1, cover_round_rect(x + 0.5, py, rect_width, shape_height, radius)) for x in range(end, last)]
    if last < width:
        runs.append((last, width, 0.0))
    return runs


@lru_cache(maxsize=96)
def bar_layout(width, height, radius, fill_width, shape_height):
    """Per row: runs of (x0, x1, track_cover, fill_cover)."""
    rows = []
    for y in range(height):
        py = y + 0.5 - (height - shape_height) / 2
        track = _coverage_runs(width, py, width, shape_height, radius)
        fill = _coverage_runs(width, py, fill_width, shape_height, radius) if fill_width > 0 else [(0, width, 0.0)]
        runs, i, j, x = [], 0, 0, 0
        while x < width:
            _, t_end, ta = track[i]
            _, f_end, fv = fill[j]
            stop = min(t_end, f_end)
            fa = min(fv, ta)
            if runs and runs[-1][2] == ta and runs[-1][3] == fa:
                runs[-1] = (runs[-1][0], stop, ta, fa)
            else:
                runs.append((x, stop, ta, fa))
            x = stop
            i += t_end == stop
            j += f_end == stop
        rows.append(tuple(runs))
    return tuple(rows)


def progress_rgba(width, height, radius, fill_width, track, fill, background, shimmer=None, shape_height=None, glow=0.65):
    """Track + clipped fill as opaque RGBA rows, like progress_bar_rgba(samples=1)."""
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    shape_height = float(height) if shape_height is None else max(0.0, min(height, float(shape_height)))
    fill_width = max(0.0, min(float(width), float(fill_width)))
    tr, tg, tb = _hex_rgb(track)
    fr, fg, fb = _hex_rgb(fill)
    br, bg_, bb = _hex_rgb(background)
    band_colors = {}
    if shimmer is not None and fill_width > 0 and 0.15 < shimmer < 0.75:
        t = (shimmer - 0.15) / 0.60
        t = t * t * (3.0 - 2.0 * t)
        pulse = math.sin(math.pi * t) * 0.12
        fr += (255 - fr) * pulse
        fg += (255 - fg) * pulse
        fb += (255 - fb) * pulse
        band = max(height * 1.5, fill_width * 0.20)
        center = -band + (fill_width + 2.0 * band) * t
        lit_end = min(width, int(math.ceil(fill_width)))
        # Outside center ± band the weight is 0 and the colour is the base.
        for x in range(max(0, int(center - band) - 1), min(lit_end, int(center + band) + 2)):
            weight = max(0.0, 1.0 - abs(x + 0.5 - center) / band)
            light = weight * weight * (3.0 - 2.0 * weight) * glow
            band_colors[x] = (fr + (255 - fr) * light, fg + (255 - fg) * light, fb + (255 - fb) * light)
    base = (fr, fg, fb)
    lit = sorted(band_colors)
    lit_from, lit_to = (lit[0], lit[-1] + 1) if lit else (0, 0)
    memo = {}

    def pixel(color, ta, fa):
        cr, cg, cb = color
        r = cr * fa + tr * (ta - fa) + br * (1.0 - ta)
        g = cg * fa + tg * (ta - fa) + bg_ * (1.0 - ta)
        b = cb * fa + tb * (ta - fa) + bb * (1.0 - ta)
        return bytes((int(r + 0.5), int(g + 0.5), int(b + 0.5), 255))

    rows = []
    for runs in bar_layout(width, height, radius, fill_width, shape_height):
        row = bytearray()
        for x0, x1, ta, fa in runs:
            if fa == 0.0 or x1 <= lit_from or x0 >= lit_to:
                key = (ta, fa)
                if key not in memo:
                    memo[key] = pixel(base, ta, fa)
                row += memo[key] * (x1 - x0)
                continue
            for a, b in ((x0, max(x0, lit_from)), (max(x0, lit_from), min(x1, lit_to)), (min(x1, lit_to), x1)):
                if b <= a:
                    continue
                if a >= lit_from and b <= lit_to:
                    for x in range(a, b):
                        key = (x, ta, fa)
                        if key not in memo:
                            memo[key] = pixel(band_colors.get(x, base), ta, fa)
                        row += memo[key]
                else:
                    key = (ta, fa)
                    if key not in memo:
                        memo[key] = pixel(base, ta, fa)
                    row += memo[key] * (b - a)
        rows.append(row)
    return width, height, rows


def ringed_progress_rgba(width, height, shape_height, ring_width, ring, fill_width, track, fill, background,
                         shimmer=None, glow=0.65):
    """A progress pill inside a ring of `ring` colour, as one opaque image.

    The inner bar is drawn inset and blended into the ring colour, then only
    the columns its rounded shape covers are copied over the ring. Copying the
    whole inner rectangle would put its square corners outside the ring's
    round ends. `fill_width` is measured on the inner bar.
    """
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    shape_height = max(0.0, min(float(height), float(shape_height)))
    ring_width = max(0, int(ring_width))
    _, _, rows = progress_rgba(width, height, shape_height / 2, 0.0, ring, ring, background,
                               shape_height=shape_height, glow=glow)
    inner_w = width - 2 * ring_width
    inner_h = int(round(shape_height)) - 2 * ring_width
    if inner_w <= 0 or inner_h <= 0:
        return width, height, rows
    top = (height - inner_h) // 2
    _, _, inner = progress_rgba(inner_w, inner_h, inner_h / 2, fill_width, track, fill, ring,
                                shimmer=shimmer, shape_height=float(inner_h), glow=glow)
    for y, runs in enumerate(bar_layout(inner_w, inner_h, inner_h / 2, 0.0, float(inner_h))):
        row, source = rows[top + y], inner[y]
        for x0, x1, covered, _ in runs:
            if covered > 0.0:
                row[(ring_width + x0) * 4:(ring_width + x1) * 4] = source[x0 * 4:x1 * 4]
    return width, height, rows
