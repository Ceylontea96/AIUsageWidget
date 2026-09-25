"""PNG encoding, icon resampling and widget shapes without Tk or UI state.

All functions operate on bytes, RGBA rows or explicit colour parameters.
Tk PhotoImage creation and asset paths stay with the UI. The per-pixel bar
renderer remains the reference for bar_raster's cached frame renderer.
"""
from __future__ import annotations

from functools import lru_cache
import math
import struct
import zlib

from bar_raster import cover_round_rect as _cover_round_rect, progress_rgba


def read_png_rgba(data):
    """Decode an 8-bit RGBA, non-interlaced PNG, the only kind the icon masters are."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    pos, idat, header = 8, [], None
    while pos < len(data):
        size, tag = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if tag == b'IHDR':
            header = struct.unpack('>IIBBBBB', body)
        elif tag == b'IDAT':
            idat.append(body)
        pos += 12 + size
    if header is None or header[2:4] != (8, 6) or header[6] != 0:
        raise ValueError('unsupported PNG')
    width, height = header[:2]
    raw, stride, rows, prev, at = zlib.decompress(b''.join(idat)), width * 4, [], bytearray(width * 4), 0
    for _ in range(height):
        kind, line = raw[at], bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            left = line[i - 4] if i >= 4 else 0
            up, corner = prev[i], (prev[i - 4] if i >= 4 else 0)
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                guess = left + up - corner
                pa, pb, pc = abs(guess - left), abs(guess - up), abs(guess - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        rows.append(line)
        prev = line
    return width, height, rows


def shrink_rgba(width, height, rows, target_w, target_h):
    """Area-average to a smaller size. Alpha is premultiplied so edges do not darken."""
    def weights(source, target):
        scale = source / target
        spans = []
        for t in range(target):
            start, end = t * scale, (t + 1) * scale
            spans.append([(s, min(end, s + 1) - max(start, s)) for s in range(int(start), min(source, math.ceil(end)))])
        return spans, scale
    cols, sx = weights(width, target_w)
    lines, sy = weights(height, target_h)
    wide = []
    for row in rows:
        out = []
        for span in cols:
            r = g = b = a = 0.0
            for s, w in span:
                alpha = row[s * 4 + 3] * w
                r += row[s * 4] * alpha
                g += row[s * 4 + 1] * alpha
                b += row[s * 4 + 2] * alpha
                a += alpha
            out.append((r, g, b, a))
        wide.append(out)
    result = []
    for span in lines:
        row = bytearray()
        for x in range(target_w):
            r = g = b = a = 0.0
            for s, w in span:
                pr, pg, pb, pa = wide[s][x]
                r += pr * w
                g += pg * w
                b += pb * w
                a += pa * w
            if a <= 0:
                row += b'\x00\x00\x00\x00'
                continue
            row += bytes((min(255, int(r / a + .5)), min(255, int(g / a + .5)), min(255, int(b / a + .5)),
                          min(255, int(a / (sx * sy) + .5))))
        result.append(row)
    return target_w, target_h, result


@lru_cache(maxsize=16)
def chevron_png(size, down, color, stroke):
    """A two-stroke chevron with soft edges, like the header's line icons.

    Tk canvas lines have no anti-aliasing on Windows, so it is rasterised here
    with 4x4 samples per pixel.
    """
    s = float(size)
    if down:
        points = ((.22*s, .38*s), (.5*s, .66*s), (.78*s, .38*s))
    else:
        points = ((.38*s, .22*s), (.66*s, .5*s), (.38*s, .78*s))
    half = stroke / 2.0

    def near(x, y):
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
            if math.hypot(x - ax - t * dx, y - ay - t * dy) <= half:
                return True
        return False

    r, g, b = hex_rgb(color)
    rows = []
    for py in range(size):
        row = bytearray()
        for px_ in range(size):
            hits = sum(near(px_ + (i + .5) / 4, py + (j + .5) / 4) for i in range(4) for j in range(4))
            row += bytes((r, g, b, int(round(255 * hits / 16))))
        rows.append(row)
    return png_rgba(size, size, rows, level=6)


def hex_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def lighten(color, amount=0.18):
    r, g, b = hex_rgb(color)
    mix = lambda c: min(255, int(round(c + (255 - c) * amount)))
    return '#%02X%02X%02X' % (mix(r), mix(g), mix(b))


def blend(a, b, t):
    t = max(0.0, min(1.0, float(t)))
    ar, ag, ab = hex_rgb(a)
    br, bg, bb = hex_rgb(b)
    mix = lambda x, y: int(round(x + (y - x) * t))
    return '#%02X%02X%02X' % (mix(ar, br), mix(ag, bg), mix(ab, bb))


def _box_downsample(rows, samples, dst_w, dst_h):
    n = samples * samples
    out = []
    for y in range(dst_h):
        row = bytearray()
        for x in range(dst_w):
            rs = gs = bs = 0
            for dy in range(samples):
                src = rows[y * samples + dy]
                for dx in range(samples):
                    i = (x * samples + dx) * 4
                    rs += src[i]; gs += src[i+1]; bs += src[i+2]
            row.extend((rs // n, gs // n, bs // n, 255))
        out.append(row)
    return dst_w, dst_h, out


def progress_bar_rgba(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None, shape_height=None, glow=0.65):
    """Track + clipped fill as opaque RGBA rows. Fill cannot paint outside the track.

    Every pixel is computed on its own. Frames are drawn by
    bar_raster.progress_rgba, which must return the same bytes.
    """
    samples = max(1, int(samples))
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    shape_height = float(height) if shape_height is None else max(0.0, min(height, float(shape_height)))
    fill_width = max(0.0, min(float(width), float(fill_width)))
    if samples > 1:
        src_w, src_h, rows = progress_bar_rgba(
            width * samples, height * samples, radius * samples, fill_width * samples,
            track, fill, background, samples=1, shimmer=shimmer, shape_height=shape_height * samples, glow=glow)
        return _box_downsample(rows, samples, width, height)
    tr, tg, tb = hex_rgb(track)
    fr, fg, fb = hex_rgb(fill)
    br, bg_, bb = hex_rgb(background)
    colors = [(fr, fg, fb)] * width
    if shimmer is not None and fill_width > 0 and 0.15 < shimmer < 0.75:
        t = (shimmer - 0.15) / 0.60
        t = t * t * (3.0 - 2.0 * t)
        pulse = math.sin(math.pi * t) * 0.12
        fr += (255 - fr) * pulse
        fg += (255 - fg) * pulse
        fb += (255 - fb) * pulse
        colors = [(fr, fg, fb)] * width
        band = max(height * 1.5, fill_width * 0.20)
        center = -band + (fill_width + 2.0 * band) * t
        for x in range(min(width, int(math.ceil(fill_width)))):
            weight = max(0.0, 1.0 - abs(x + 0.5 - center) / band)
            highlight = weight * weight * (3.0 - 2.0 * weight) * glow
            colors[x] = (fr + (255 - fr) * highlight, fg + (255 - fg) * highlight, fb + (255 - fb) * highlight)
    rows = []
    for y in range(height):
        py = y + 0.5 - (height - shape_height) / 2
        row = bytearray()
        for x in range(width):
            px = x + 0.5
            fr, fg, fb = colors[x]
            track_a = _cover_round_rect(px, py, width, shape_height, radius)
            fill_a = _cover_round_rect(px, py, fill_width, shape_height, radius) if fill_width > 0 else 0.0
            fill_a = min(fill_a, track_a)
            r = fr * fill_a + tr * (track_a - fill_a) + br * (1.0 - track_a)
            g = fg * fill_a + tg * (track_a - fill_a) + bg_ * (1.0 - track_a)
            b = fb * fill_a + tb * (track_a - fill_a) + bb * (1.0 - track_a)
            row.extend((int(r + 0.5), int(g + 0.5), int(b + 0.5), 255))
        rows.append(row)
    return width, height, rows


def png_rgba(width, height, rows, level=9):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    raw = b''.join(b'\x00' + bytes(row) for row in rows)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw, level)) + chunk(b'IEND', b'')


@lru_cache(maxsize=128)
def pill_png(width, height, fill, background):
    """A solid anti-aliased stadium, the update button's shape."""
    w, h, rows = progress_rgba(width, height, height / 2, width, fill, fill, background)
    return png_rgba(w, h, rows, level=1)


def progress_bar_png(width, height, radius, fill_width, track, fill, background, samples=1, shimmer=None, shape_height=None, glow=0.65):
    w, h, rows = progress_bar_rgba(width, height, radius, fill_width, track, fill, background,
                                   samples=samples, shimmer=shimmer, shape_height=shape_height, glow=glow)
    return png_rgba(w, h, rows)


def padded_stadium_rgba(width, height, radius, fill, background, pad=1, samples=4):
    """Stadium with 1px AA padding so 3px caps can round without looking square-cut."""
    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    pad = max(0, int(pad))
    samples = max(1, int(samples))
    img_w, img_h = width + pad * 2, height + pad * 2
    sw, sh = img_w * samples, img_h * samples
    fr, fg, fb = hex_rgb(fill)
    br, bg_, bb = hex_rgb(background)
    ox, oy = pad * samples, pad * samples
    rw, rh, rr = width * samples, height * samples, radius * samples
    src = []
    for y in range(sh):
        py = y + 0.5
        row = bytearray()
        for x in range(sw):
            px = x + 0.5
            a = _cover_round_rect(px - ox, py - oy, rw, rh, rr)
            row.extend((
                int(fr * a + br * (1.0 - a) + 0.5),
                int(fg * a + bg_ * (1.0 - a) + 0.5),
                int(fb * a + bb * (1.0 - a) + 0.5),
                255,
            ))
        src.append(row)
    return _box_downsample(src, samples, img_w, img_h)


@lru_cache(maxsize=12)
def ring_geometry(size):
    radius, center = size * 35 / 84, size / 2
    points = []
    for y in range(size):
        for x in range(size):
            dx,dy=x+.5-center,y+.5-center
            distance=abs(math.hypot(dx,dy)-radius)
            if distance < size * .075:
                points.append((y,x*4,distance,(math.atan2(dy,dx)+math.pi/2)%math.tau,dx,dy))
    return points


@lru_cache(maxsize=48)
def ring_png(size, percent, color, thickness, background, track):
    """A drawn ring costs several milliseconds; the same ring is reused."""
    radius, half = size*35/84, thickness/2
    fraction = max(0,min(100,percent))/100
    end = fraction*math.tau
    ex,ey=math.sin(end)*radius,-math.cos(end)*radius
    bg,track,fg=hex_rgb(background),hex_rgb(track),hex_rgb(color)
    track_palette=[bytes([round(b+(t-b)*i/255) for b,t in zip(bg,track)]+[255]) for i in range(256)]
    fill_palette=[bytes([round(b+(f-b)*i/255) for b,f in zip(bg,fg)]+[255]) for i in range(256)]
    rows=[bytearray(bytes(bg)+b'\xff')*size for _ in range(size)]
    for y,x,distance,angle,dx,dy in ring_geometry(size):
        cov=max(0,min(255,int((half+.5-distance)*255)))
        if not cov:
            continue
        if fraction>=1 or 0<fraction and angle<=end:
            pixel=fill_palette[cov]
        else:
            cap=min(math.hypot(dx,dy+radius),math.hypot(dx-ex,dy-ey)) if fraction>0 else size
            arc=max(0,min(255,int((half+.5-cap)*255)))
            if arc:
                base=track_palette[cov]
                pixel=bytes([round(base[i]+(fg[i]-base[i])*arc/255) for i in range(3)]+[255])
            else:
                pixel=track_palette[cov]
        rows[y][x:x+4]=pixel
    return png_rgba(size,size,rows)
