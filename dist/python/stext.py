"""Parses typed text from Samsung Notes: a note's flowing body (note.note) and text boxes
placed on a page. Both use the same rich-text record."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from spage import FormatError, Reader, read_frame
from swidths import BOLD, REGULAR

SPAN_COLOR, SPAN_SIZE, SPAN_FONT, SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE = 1, 3, 4, 5, 6, 7
SPAN_BACKGROUND, SPAN_STRIKE = 17, 20
PARA_INDENT, PARA_ALIGN, PARA_SPACING, PARA_BULLET = 2, 3, 4, 5
DEFAULT_DELTA = -5  # what the app does for a note with no font size adjustment stored (measured from its thumbnails)


@dataclass
class Span:
    kind: int
    start: int
    end: int
    payload: bytes


@dataclass
class Paragraph:
    kind: int
    start: int  # paragraph ordinal, inclusive
    end: int  # exclusive
    payload: bytes


@dataclass
class ObjectSpan:
    object_type: int
    index: int
    data: bytes


@dataclass
class RichText:
    text: str = ""
    spans: list[Span] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    margins: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    gravity: int = 0
    sections: list[tuple[int, int]] = field(default_factory=list)  # (start, length) per page; start -1 if empty
    objects: list[ObjectSpan] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    rotation: float | None = None
    visible: bool = True
    font_scale: float = 1.0  # set when a text box is enlarged with --ink-scale
    size_delta: int = DEFAULT_DELTA  # the note's font size adjustment
    _styled: dict = field(default_factory=dict, repr=False)  # per-scale style cache


def _read_common(data: bytes) -> RichText:
    r = Reader(data)
    out = RichText()
    raw_text = r.take(r.u32() * 2)
    out.text = raw_text.decode("utf-16-le", "replace")
    offsets = _offset_map(out.text)
    # Same length as before, so stored offsets stay valid: every line ending becomes \\n.
    out.text = out.text.replace("\r\n", " \n").replace("\r", "\n")
    for _ in range(r.u32()):
        rec = Reader(r.take(r.u16()))
        kind, start, end, _interval = rec.unpack("4I")
        out.spans.append(Span(kind, offsets(start), offsets(end), rec.take(rec.left)))
    for _ in range(r.u32()):
        rec = Reader(r.take(r.u16()))
        kind, start, end = rec.unpack("3I")
        out.paragraphs.append(Paragraph(kind, start, end, rec.take(rec.left)))
    out.margins = r.unpack("4f")
    out.gravity = r.u8()
    if r.left >= 2:
        for _ in range(r.u16()):
            start, length = r.unpack("2i")
            if start >= 0:
                end = offsets(start + length)
                start = offsets(start)
                length = end - start
            out.sections.append((start, length))
    return out


def _offset_map(text: str):
    """Stored offsets count UTF-16 units; Python counts characters. They differ after emoji."""
    if all(ord(c) < 0x10000 for c in text):
        return lambda unit: unit
    table = []
    for index, ch in enumerate(text):
        table.extend([index] * (2 if ord(ch) >= 0x10000 else 1))
    table.append(len(text))
    return lambda unit: table[unit] if 0 <= unit < len(table) else (len(text) if unit > 0 else 0)


def parse_rich_text(payload: bytes) -> RichText:
    """Object base frame, shape frame (6), shape-text frame (7)."""
    r = Reader(payload)
    base = read_frame(r)
    if base.kind != 0:
        raise FormatError(f"text object base frame has kind {base.kind}")
    b = Reader(base.fixed)
    b.u32()
    b.take(b.u16())
    b.take(8)
    bbox = b.unpack("4d")
    shape = read_frame(r)
    if shape.kind != 6:
        raise FormatError(f"text shape frame has kind {shape.kind}")
    frame = read_frame(r)
    if frame.kind != 7:
        raise FormatError(f"shape-text frame has kind {frame.kind}")
    out = RichText()
    if frame.fields & 1:
        flexible = Reader(frame.flexible)
        out = _read_common(flexible.take(flexible.u32()))
    out.bbox = bbox
    out.rotation = Reader(base.flexible).f32() if base.fields & 1 else None
    if not math.isfinite(sum(bbox) + sum(out.margins) + (out.rotation or 0.0)):
        raise FormatError("text object has non-finite geometry")
    out.visible = bool(base.properties & 8)
    return out


@dataclass
class NoteHeader:
    format_version: int
    width: int
    height: int
    padding_x: int
    padding_y: int
    flags: int
    title: RichText
    body: RichText
    default_page: tuple[int, int] | None


def parse_note(data: bytes) -> NoteHeader:
    r = Reader(data)
    fixed_end = r.u32()
    r.end = fixed_end
    flags = r.mask()
    fields = r.mask()
    version = r.u32()
    r.take(r.u16() * 2)  # note id
    r.u32()  # revision
    r.take(16)  # created, modified
    width, height, pad_x, pad_y = r.unpack("4I")
    r.u32()  # minimum version
    title = parse_rich_text(r.take(r.u32()))
    body = parse_rich_text(r.take(r.u32()))
    default_page = None
    if r.left >= 8:
        w, h = r.unpack("2I")
        if w and h:
            default_page = (w, h)
    body.size_delta = _size_delta(data, fixed_end, fields)
    return NoteHeader(version, width, height, pad_x, pad_y, flags, title, body, default_page)


def _size_delta(data: bytes, start: int, fields: int) -> int:
    """The note's body font size adjustment (field 11), in the flexible data after the
    fixed part. Fields before it are read only to skip them."""
    if not fields >> 11 & 1:
        return DEFAULT_DELTA
    r = Reader(data, start, max(start, len(data) - 32))
    try:
        for bit in range(11):
            if not fields >> bit & 1:
                continue
            if bit == 0 or bit == 6:
                r.take(r.u16() * 2)  # application name; template uri
            elif bit == 1:
                r.take(8)
                r.take(r.u16() * 2)  # application version
            elif bit == 3:
                r.take(16)  # location
            elif bit == 7:
                r.take(4)  # last edited page index
            elif bit == 9:
                r.take(12)  # last edited page
            elif bit == 10:
                r.take(r.u32())  # string table (pen names)
            else:
                return DEFAULT_DELTA
        delta = r.i32()
    except (FormatError, struct.error):
        return DEFAULT_DELTA
    return DEFAULT_DELTA if delta == MISSING_DELTA else delta


def parse_u32(payload: bytes, default: int = 0) -> int:
    return struct.unpack_from("<I", payload)[0] if len(payload) >= 4 else default


def parse_f32(payload: bytes, default: float = 0.0) -> float:
    return struct.unpack_from("<f", payload)[0] if len(payload) >= 4 else default


# ------------------------------------------------------------------ layout

DEFAULT_SIZE = 17.0
DEFAULT_RGB = 0x000000
LINE_HEIGHT = 1.35
BASELINE = 0.80  # of the font size, below the top of the glyph box
DP_PER_WIDTH = 360  # sizes and margins are in units of canvas width / 360
MISSING_DELTA = -(2 ** 31)  # stored when a note has no font size adjustment
# Common symbols missing from WinAnsi, the encoding of PDF's built-in fonts.
PDF_SUBSTITUTES = {"\u2212": "-", "\u2264": "<=", "\u2265": ">=", "\u2192": "->", "\u2190": "<-", "\u2260": "!=", "\u2248": "~"}
BULLET_DIGIT, BULLET_NONE = 4, 0


@dataclass
class TextRun:
    text: str
    x: float
    width: float
    size: float
    rgb: int
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    background: int | None = None


@dataclass
class TextLine:
    baseline: float
    runs: list[TextRun]


def char_width(ch: str, size: float, bold: bool) -> float:
    return (BOLD if bold else REGULAR).get(ch, 556) * size / 1000


def _flag(payload: bytes) -> bool:
    return bool(payload[:1]) and payload[0] != 0


def _styles(rich: RichText, scale: float) -> list[tuple]:
    """One (size, rgb, bold, italic, underline, strike, background) per text character.
    Later spans win, as they do in the app. Cached: a long note is laid out page by page."""
    cached = rich._styled.get(scale)
    if cached is None:
        cached = rich._styled[scale] = _build_styles(rich, scale)
    return cached


def _resolved(size: float, delta: int, scale: float) -> float:
    return max(size + delta, 1.0) * scale


def _build_styles(rich: RichText, scale: float) -> list[tuple]:
    n = len(rich.text)
    size = [DEFAULT_SIZE] * n
    rgb = [DEFAULT_RGB] * n
    flags: dict[int, list] = {k: [False] * n for k in (SPAN_BOLD, SPAN_ITALIC, SPAN_UNDERLINE, SPAN_STRIKE)}
    back: list[int | None] = [None] * n
    for span in rich.spans:
        start, end = max(span.start, 0), min(span.end, n)
        if start >= end:
            continue
        if span.kind == SPAN_SIZE:
            value = parse_f32(span.payload, DEFAULT_SIZE)
            if value > 0 and math.isfinite(value):
                size[start:end] = [value] * (end - start)
        elif span.kind == SPAN_COLOR:
            rgb[start:end] = [parse_u32(span.payload) & 0xFFFFFF] * (end - start)
        elif span.kind in flags:
            flags[span.kind][start:end] = [_flag(span.payload)] * (end - start)
        elif span.kind == SPAN_BACKGROUND:
            value = parse_u32(span.payload)
            back[start:end] = [value & 0xFFFFFF if value >> 24 else None] * (end - start)
    return [(_resolved(size[i], rich.size_delta, scale), rgb[i], flags[SPAN_BOLD][i], flags[SPAN_ITALIC][i],
             flags[SPAN_UNDERLINE][i], flags[SPAN_STRIKE][i], back[i]) for i in range(n)]


def _bullets(rich: RichText) -> dict[int, str]:
    marks = {}
    for para in rich.paragraphs:
        if para.kind == PARA_BULLET and len(para.payload) >= 8:
            kind, number = struct.unpack_from("<II", para.payload)
            if kind == BULLET_NONE:
                continue
            marker = f"{number}. " if kind == BULLET_DIGIT else "\u2022 "
            for ordinal in range(para.start, min(para.end, para.start + 10000)):
                marks[ordinal] = marker
    return marks


def layout_text(rich: RichText, start: int, end: int, x0: float, y0: float, width: float, scale: float) -> list[TextLine]:
    """Word-wraps text[start:end] into lines, from y0 downward, in page units."""
    styles = _styles(rich, scale)
    bullets = _bullets(rich)
    text = rich.text
    lines: list[TextLine] = []
    y = y0
    ordinal = text.count("\n", 0, start)
    pos = start
    default = (_resolved(DEFAULT_SIZE, rich.size_delta, scale), DEFAULT_RGB, False, False, False, False, None)
    while pos < end:
        stop = text.find("\n", pos, end)
        stop = end if stop < 0 else stop
        marker = bullets.get(ordinal, "")
        first_style = styles[pos] if pos < len(styles) else default
        indent = sum(char_width(c, first_style[0], first_style[2]) for c in marker)
        for a, b in _wrap(text, pos, stop, styles, width - indent):
            size = max((styles[i][0] for i in range(a, b)), default=styles[min(a, len(styles) - 1)][0] if styles else default[0])
            runs = []
            x = x0
            if a == pos and marker:
                runs.append(TextRun(marker, x, indent, first_style[0], first_style[1], first_style[2]))
            x += indent
            i = a
            while i < b:
                j = i + 1
                while j < b and styles[j] == styles[i]:
                    j += 1
                sz, rgb, bold, italic, under, strike, back = styles[i]
                chunk = text[i:j].replace("\t", "    ").replace("\r", "")
                w = sum(char_width(c, sz, bold) for c in chunk)
                runs.append(TextRun(chunk, x, w, sz, rgb, bold, italic, under, strike, back))
                x += w
                i = j
            height = size * LINE_HEIGHT
            lines.append(TextLine(y + (height - size) / 2 + size * BASELINE, runs))
            y += height
        pos = stop + 1
        ordinal += 1
    return lines


def _wrap(text: str, start: int, stop: int, styles, width: float) -> list[tuple[int, int]]:
    """Greedy word wrap; a word wider than the line is split. Blank lines stay as lines."""
    if start >= stop:
        return [(start, stop)]
    out = []
    a = i = start
    used = 0.0
    last_space = -1
    while i < stop:
        sz, _, bold, *_ = styles[i]
        w = char_width(text[i], sz, bold) if text[i] != "\t" else char_width(" ", sz, bold) * 4
        if used + w > width and i > a and text[i] != " ":
            if last_space >= a:
                out.append((a, last_space + 1))
                a = i = last_space + 1
            else:
                out.append((a, i))
                a = i
            used = 0.0
            last_space = -1
            continue
        if text[i] == " ":
            last_space = i
        used += w
        i += 1
    out.append((a, stop))
    return out


def body_lines(rich: RichText, page_index: int, page_width: int) -> list[TextLine]:
    """Body text saved on this page, per the note's per-page text sections."""
    if not 0 <= page_index < len(rich.sections):
        return []
    start, length = rich.sections[page_index]
    if start < 0 or length <= 0:
        return []
    scale = page_width / DP_PER_WIDTH
    left, top, right, _ = rich.margins
    return layout_text(rich, start, min(start + length, len(rich.text)), left * scale, top * scale,
                       page_width - (left + right) * scale, scale)


def box_lines(rich: RichText, page_width: int) -> list[TextLine]:
    """Lines of a text box, in page units."""
    scale = page_width / DP_PER_WIDTH
    x0, y0, x1, y1 = rich.bbox
    left, top, right, bottom = rich.margins
    lines = layout_text(rich, 0, len(rich.text), x0 + left * scale, y0 + top * scale,
                        max(x1 - x0 - (left + right) * scale, 1.0), scale * rich.font_scale)
    if rich.gravity in (1, 2) and lines:  # centre or bottom within the box
        size = max((r.size for r in lines[-1].runs), default=0.0)
        used = lines[-1].baseline + size * (LINE_HEIGHT - BASELINE) / 2 - y0
        free = y1 - y0 - bottom * scale - used
        shift = free / 2 if rich.gravity == 1 else free
        if shift > 0:
            for line in lines:
                line.baseline += shift
    return lines


def lines_bottom(lines: list[TextLine]) -> float:
    # Blank lines don't count: notes use runs of them to space text, and they aren't content.
    return max((line.baseline + max(r.size for r in line.runs) * 0.4
                for line in lines if any(r.text.strip() for r in line.runs)), default=0.0)


def encode_pdf(text: str) -> tuple[bytes, int]:
    """PDF literal string in WinAnsi, and how many characters had to become '?'."""
    missing = 0
    out = bytearray(b"(")
    for original, plain in PDF_SUBSTITUTES.items():
        text = text.replace(original, plain)
    for ch in text:
        try:
            b = ch.encode("cp1252")
        except UnicodeEncodeError:
            b = b"?"
            missing += 1
        for byte in b:
            if byte in b"()\\":
                out += b"\\" + bytes([byte])
            elif byte < 32:
                out += f"\\{byte:03o}".encode()
            else:
                out.append(byte)
    out += b")"
    return bytes(out), missing
