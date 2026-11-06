"""Turns a decoded pen stroke into drawing commands shared by the SVG and PDF writers.

Commands are tuples in note coordinates (y pointing down):
    ("M", x, y)  ("L", x, y)  ("Q", cx, cy, x, y)  ("Z",)
    ("A", r, large_arc, x, y)   half-circle to (x, y), counter-clockwise on screen
"""
from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from spage import Stroke

# An approximation of Samsung's pen engine: width grows with pressure, then levels off.
# Fitted by minimising pixel error against the app's own thumbnails across six notes.
WIDTH_AT_ZERO = 0.30
WIDTH_AT_KNEE = 0.55
PRESSURE_KNEE = 0.60
SPLIT_COS = 0.5  # split the outline where the pen turns by more than 60 degrees
DEFAULT_COLOR = 0xFF000000  # strokes with no stored colour are drawn in default black
MARKER_OPACITY = 0.45


def width_factor(pressure: float) -> float:
    """Stroke width as a fraction of pen size."""
    t = min(1.0, max(0.0, pressure) / PRESSURE_KNEE)
    return WIDTH_AT_ZERO + (WIDTH_AT_KNEE - WIDTH_AT_ZERO) * t


@dataclass
class Shape:
    commands: list[tuple]
    rgb: int
    alpha: float
    filled: bool
    width: float = 0.0  # stroked shapes only
    round_cap: bool = True


def simplify(pts: list[tuple[float, float, float]], tol: float) -> list[tuple[float, float, float]]:
    """Ramer-Douglas-Peucker over x, y and half-width, so width changes survive."""
    if tol <= 0 or len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay, aw = pts[a]
        bx, by, bw = pts[b]
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        worst, at = tol, -1
        for i in range(a + 1, b):
            x, y, w = pts[i]
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg)) if seg else 0.0
            err = max(hypot(x - ax - t * dx, y - ay - t * dy), abs(w - (aw + t * (bw - aw))))
            if err > worst:
                worst, at = err, i
        if at >= 0:
            keep[at] = True
            stack.append((a, at))
            stack.append((at, b))
    return [p for p, k in zip(pts, keep) if k]


def _smooth(xy: list[tuple[float, float]], move: bool) -> list[tuple]:
    """Quadratic curve through segment midpoints, using each point as a control."""
    out: list[tuple] = [("M" if move else "L", xy[0][0], xy[0][1])]
    if len(xy) > 2:
        for i in range(1, len(xy) - 1):
            cx, cy = xy[i]
            nx, ny = xy[i + 1]
            ex, ey = (nx, ny) if i == len(xy) - 2 else ((cx + nx) / 2, (cy + ny) / 2)
            out.append(("Q", cx, cy, ex, ey))
    elif len(xy) == 2:
        out.append(("L", xy[1][0], xy[1][1]))
    return out


def _ink_commands(pts: list[tuple[float, float, float]]) -> list[tuple]:
    """Closed outline: left edge, round cap, right edge reversed, round cap."""
    clean = [pts[0]]
    for p in pts[1:]:
        if hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 1e-3:
            clean.append(p)
    if len(clean) == 1:
        x, y, r = clean[0]
        r = max(r, 0.1)
        return [("M", x - r, y), ("A", r, 1, x + r, y), ("A", r, 1, x - r, y), ("Z",)]
    out: list[tuple] = []
    for part in _split_at_folds(clean):
        out.extend(_ribbon(part))
    return out


def _split_at_folds(pts: list[tuple[float, float, float]]) -> list[list[tuple[float, float, float]]]:
    """An outline folds over itself at a hairpin, and the fold's reversed winding can
    punch a hole in the fill. Break the stroke there, sharing the corner point: every
    ribbon winds the same way, so overlaps fill solidly under the nonzero rule."""
    parts, start = [], 0
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        la, lb = hypot(ax, ay), hypot(bx, by)
        cos_turn = (ax * bx + ay * by) / (la * lb)
        cross = abs(ax * by - ay * bx) / (la * lb)
        # Sharp corner, or a bend tighter than the pen is wide (inner edge reverses).
        if cos_turn < SPLIT_COS or cross * pts[i][2] > min(la, lb) * 0.5:
            parts.append(pts[start:i + 1])
            start = i
    parts.append(pts[start:])
    return parts


def _ribbon(pts: list[tuple[float, float, float]]) -> list[tuple]:
    left, right = [], []
    last = len(pts) - 1
    for i, (x, y, w) in enumerate(pts):
        ax, ay, _ = pts[max(i - 1, 0)]
        bx, by, _ = pts[min(i + 1, last)]
        dx, dy = bx - ax, by - ay
        d = hypot(dx, dy) or 1.0
        nx, ny = -dy / d * w, dx / d * w
        left.append((x + nx, y + ny))
        right.append((x - nx, y - ny))
    r_end, r_start = max(pts[-1][2], 0.01), max(pts[0][2], 0.01)
    return (_smooth(left, True)
            + [("A", r_end, 0, right[-1][0], right[-1][1])]
            + _smooth(right[::-1], False)
            + [("A", r_start, 0, left[0][0], left[0][1]), ("Z",)])


def _centerline_commands(pts: list[tuple[float, float, float]]) -> list[tuple]:
    xy = [(p[0], p[1]) for p in pts]
    if len(xy) == 1:
        xy.append((xy[0][0] + 0.01, xy[0][1]))
    return _smooth(xy, True)


def stroke_shape(s: Stroke, pen_name: str, centerline: bool, tol: float) -> Shape | None:
    if not s.xs:
        return None
    size = s.pen_size if s.pen_size else 1.0
    is_marker = any(k in pen_name.lower() for k in ("marker", "highlighter", "tapepen"))
    argb = DEFAULT_COLOR if s.color_argb is None else s.color_argb
    alpha = (argb >> 24 & 255) / 255 or 1.0
    if is_marker:
        alpha = min(alpha, MARKER_OPACITY)
    rgb = argb & 0xFFFFFF
    if is_marker or centerline:
        pts = simplify([(x, y, 0.0) for x, y in zip(s.xs, s.ys)], tol)
        width = size if is_marker else size * sum(map(width_factor, s.pressures)) / len(s.pressures)
        return Shape(_centerline_commands(pts), rgb, alpha, False, width, not is_marker)
    pts = simplify([(x, y, size / 2 * width_factor(p)) for x, y, p in zip(s.xs, s.ys, s.pressures)], tol)
    return Shape(_ink_commands(pts), rgb, alpha, True)
