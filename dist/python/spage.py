"""Read-only parser for Samsung Notes `.page` files (the S Pen SDK page format).

Format knowledge comes from the reverse-engineering notes of the GPL-3.0 `sdocx`
project (https://github.com/Kreijstal/sdocx); this is an independent Python
implementation, checked against real Samsung Notes for Windows 4.3 data.

Everything is little-endian. Strings are UTF-16 unless noted.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

STROKE = 1
IMAGE = 3
TEXT_BOX = 2
WIDE_CANVAS = 1848
HIGHLIGHTER_SIZE = 40  # thicker pens overhang the page on purpose
TRAILER = 32  # every object and layer ends in a 32-byte integrity hash
OBJECT_TYPES = {
    0: "none", 1: "stroke", 2: "textbox", 3: "image", 4: "container", 7: "shape",
    8: "line", 9: "dummy_stroke", 10: "voice", 11: "formula", 12: "old_table",
    13: "web", 14: "painting", 15: "stroke_dev", 16: "video", 17: "link",
    18: "stroke_brush", 19: "unknown", 20: "plot", 21: "math", 22: "table",
    23: "code", 24: "file", 100: "stroke_group",
}


class FormatError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data, self.pos = data, pos
        self.end = len(data) if end is None else end

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.end:
            raise FormatError(f"read of {n} bytes past end at {self.pos:#x}")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def u8(self): return self.unpack("B")[0]
    def u16(self): return self.unpack("H")[0]
    def i16(self): return self.unpack("h")[0]
    def u32(self): return self.unpack("I")[0]
    def i32(self): return self.unpack("i")[0]
    def f32(self): return self.unpack("f")[0]
    def f64(self): return self.unpack("d")[0]

    def mask(self) -> int:
        """Length-prefixed bit mask; bit n is byte n/8, LSB first."""
        return int.from_bytes(self.take(self.u8()), "little")

    @property
    def left(self) -> int:
        return self.end - self.pos


@dataclass
class Frame:
    kind: int
    properties: int
    fields: int
    fixed: bytes
    flexible: bytes


def read_frame(r: Reader) -> Frame:
    start = r.pos
    size = r.u32()
    if size < 12 or start + size > r.end:
        raise FormatError(f"bad frame size {size} at {start:#x}")
    kind = r.i16()
    flex_off = r.u32()
    properties = r.mask()
    fields = r.mask()
    fixed_end = size if (flex_off == 0 and fields == 0) else flex_off
    if start + fixed_end < r.pos or fixed_end > size:
        raise FormatError(f"bad flexible offset {flex_off} at {start:#x}")
    fixed = r.data[r.pos:start + fixed_end]
    flexible = r.data[start + fixed_end:start + size]
    r.pos = start + size
    return Frame(kind, properties, fields, fixed, flexible)


@dataclass
class Stroke:
    xs: list[float]
    ys: list[float]
    pressures: list[float]
    bbox: tuple[float, float, float, float]
    rotation: float | None
    pen_size: float | None
    color_argb: int | None
    pen_name_id: int | None
    tool_type: int
    original_width: int | None = None  # canvas width the stroke was drawn on, if recorded


@dataclass
class ImageObject:
    bbox: tuple[float, float, float, float]
    rotation: float | None
    media_id: int
    crop: tuple[int, int, int, int] | None  # left, top, right, bottom


@dataclass
class PdfBackground:
    media_id: int
    page_index: int
    rect: tuple[float, float, float, float]  # note units: left, top, right, bottom


@dataclass
class Page:
    uuid: str
    width: int
    height: int
    format_version: int | None
    strokes: list[Stroke] = field(default_factory=list)
    images: list[ImageObject] = field(default_factory=list)
    text_boxes: list = field(default_factory=list)
    drawables: list = field(default_factory=list)  # strokes and images, in z-order
    other_objects: dict[str, int] = field(default_factory=dict)
    bg_color: int | None = None
    bg_image: int | None = None
    bg_mode: int | None = None
    template_type: int | None = None
    pdf_backgrounds: list[PdfBackground] = field(default_factory=list)
    trailing_bytes: int = 0
    rescaled_ink: float | None = None  # set when stored ink was shrunk to fit the page


def _delta(raw: int, divisor: float) -> float:
    value = (raw & 0x7FFF) / divisor
    return -value if raw & 0x8000 else value


def _float_channel(r: Reader, n: int, compressed: bool) -> list[float]:
    out = [r.f32()]
    if compressed:
        for raw in r.unpack(f"{n - 1}H"):
            out.append(out[-1] + _delta(raw, 4096.0))
    else:
        out.extend(r.unpack(f"{n - 1}f"))
    return out


def _decode_channels(frame: Frame):
    r = Reader(frame.fixed)
    n = r.u16()
    compressed = bool(frame.properties & 1)
    stylus = bool(frame.properties & 4)
    s = 1 if stylus else 0
    if n == 0:
        need = 0
    elif compressed:
        need = 16 + (n - 1) * 4 + (2 + s * 2) * (4 + (n - 1) * 2)
    else:
        need = n * (24 + s * 8)
    if r.left != need + 2:
        raise FormatError(f"stroke of {n} points has {r.left} channel bytes, expected {need + 2}")
    xs: list[float] = []
    ys: list[float] = []
    pressures: list[float] = []
    if n:
        if compressed:
            x, y = r.f64(), r.f64()
            xs.append(x); ys.append(y)
            raw = r.unpack(f"{(n - 1) * 2}H")
            for i in range(n - 1):
                x += _delta(raw[i * 2], 32.0)
                y += _delta(raw[i * 2 + 1], 32.0)
                xs.append(x); ys.append(y)
        else:
            for _ in range(n):
                x, y = r.f64(), r.f64()
                xs.append(x); ys.append(y)
        pressures = _float_channel(r, n, compressed)
        r.take(4 + (n - 1) * (2 if compressed else 4))  # timestamps
        if stylus:
            r.take(2 * (4 + (n - 1) * (2 if compressed else 4)))  # tilt, orientation
    tool_type = r.u16()
    return xs, ys, pressures, tool_type


def _decode_style(frame: Frame):
    """Returns (pen_size, color_argb, pen_name_id, original_width). Stops at bits it can't size."""
    r = Reader(frame.flexible)
    f = frame.fields
    legacy_name = pen_size = color = name = original_width = None
    if f & 1: legacy_name = r.i32()
    if f & 2: r.i32()  # advanced pen setting id
    if f & 4: color = r.u32()
    if f & 8: pen_size = r.f32()
    if f & 16: r.u8()
    if f & 32 or f & 64:  # legacy rects need base-object data; bit 6 has no known size
        return pen_size, color, legacy_name, None
    if f & 128: name = r.i32()
    if f & 256: r.f32()  # fixed width
    if f & 512: r.i32()  # size level
    if f & 1024: r.i32()  # particle density
    if f & 2048: r.i32()  # rendering level
    if f & 4096: original_width = r.i32()
    return pen_size, color, name if name not in (None, -1) else legacy_name, original_width


def _decode_stroke(payload: bytes) -> Stroke:
    r = Reader(payload)
    base = read_frame(r)
    if base.kind != 0:
        raise FormatError(f"object base frame has kind {base.kind}")
    b = Reader(base.fixed)
    b.u32()  # format version
    b.take(b.u16())  # uuid (UTF-8)
    b.take(8)  # modified time
    bbox = b.unpack("4d")
    rotation = None
    if base.fields & 1:
        rotation = Reader(base.flexible).f32()
    frame = read_frame(r)
    if frame.kind != 1:
        raise FormatError(f"stroke frame has kind {frame.kind}")
    xs, ys, pressures, tool_type = _decode_channels(frame)
    if not math.isfinite(sum(xs) + sum(ys) + sum(pressures)):
        raise FormatError("stroke has non-finite coordinates")
    pen_size, color, name, original_width = _decode_style(frame)
    return Stroke(xs, ys, pressures, bbox, rotation, pen_size, color, name, tool_type, original_width)


def _decode_image(payload: bytes) -> ImageObject | None:
    """Image objects: base frame, shape-base (6), shape (7, holds the media id), tail (3,
    holds the crop). Returns None if hidden; raises FormatError if there is no image."""
    r = Reader(payload)
    base = read_frame(r)
    if base.kind != 0:
        raise FormatError(f"object base frame has kind {base.kind}")
    b = Reader(base.fixed)
    b.u32()
    b.take(b.u16())
    b.take(8)
    bbox = b.unpack("4d")
    rotation = Reader(base.flexible).f32() if base.fields & 1 else None
    if not base.properties & 8:  # the "visible" property bit
        return None
    media_id = None
    crop = None
    while r.left >= 12:
        frame = read_frame(r)
        if frame.kind == 7:
            f = Reader(frame.flexible)
            if frame.fields & 1: f.take(f.u32())
            if frame.fields & 2: f.u8()
            if frame.fields & 4: f.i32()
            if frame.fields & 8: raise FormatError("image shape uses an unknown field")
            if frame.fields & 16: f.i32()
            if frame.fields & 32:
                f.u32()
                if f.u8() != 2:
                    raise FormatError("shape fill is not an image")
                f.u8()
                media_id = f.i32()
        elif frame.kind == 3:
            if not frame.fields & 1 and frame.fields & 2:
                crop = Reader(frame.flexible).unpack("4i")
    if media_id is None or media_id < 0:
        raise FormatError("image object has no media reference")
    return ImageObject(bbox, rotation, media_id, crop)


def _walk_object(r: Reader, page: Page, depth: int = 0) -> None:
    if depth > 64:
        raise FormatError("objects nested too deeply")
    kind = r.u8()
    children = r.u16()
    size = r.u32()
    if size < TRAILER:
        raise FormatError(f"object size {size} at {r.pos:#x}")
    payload = r.take(size - TRAILER)
    r.take(TRAILER)
    if kind == STROKE:
        stroke = _decode_stroke(payload)
        page.strokes.append(stroke)
        page.drawables.append(stroke)
    elif kind == IMAGE:
        try:
            image = _decode_image(payload)
        except (FormatError, struct.error):
            page.other_objects["unreadable_image"] = page.other_objects.get("unreadable_image", 0) + 1
        else:
            if image is not None:
                page.images.append(image)
                page.drawables.append(image)
    elif kind == TEXT_BOX:
        from stext import parse_rich_text  # stext builds on this module
        try:
            box = parse_rich_text(payload)
        except (FormatError, struct.error, IndexError):
            page.other_objects["unreadable_text_box"] = page.other_objects.get("unreadable_text_box", 0) + 1
        else:
            if box.visible and box.text.strip():
                page.text_boxes.append(box)
                page.drawables.append(box)
    else:
        label = OBJECT_TYPES.get(kind, f"type_{kind}")
        page.other_objects[label] = page.other_objects.get(label, 0) + 1
    for _ in range(children):
        _walk_object(r, page, depth + 1)


def _read_properties(data: bytes, start: int, end: int, mask: int, page: Page) -> None:
    """Fields appear in bit order, each only if its bit is set in the page's mask."""
    f = Reader(data, start, end)
    if mask & 1: f.take(32)  # content bounding box
    if mask & 2:
        for _ in range(f.u16()):
            f.take(f.u16() * 2)  # tags
    if mask & 4: f.take(f.u16() * 2)  # template uri
    if mask & 8:
        image = f.u32()
        page.bg_image = None if image == 0xFFFFFFFF else image
    if mask & 16: page.bg_mode = f.u32()
    if mask & 32: page.bg_color = f.u32()
    if mask & 64: f.u32()  # background width
    if mask & 128: f.u32()  # background rotation
    if mask & 256:
        legacy = page.format_version is not None and page.format_version < 2034
        for _ in range(f.u16()):
            media_id, index = f.i32(), f.i32()
            rect = f.unpack("4f") if legacy else f.unpack("4i")
            page.pdf_backgrounds.append(PdfBackground(media_id, index, tuple(float(v) for v in rect)))
    if mask & 512: page.template_type = f.u32()


def content_bottom(page: Page) -> int:
    """The page height, grown only if ink or images really extend below it."""
    bottom = float(max(page.height, 1))
    for st in page.strokes:
        if st.ys:
            bottom = max(bottom, max(st.ys) + (st.pen_size or 0) / 2 + 1)
    for im in page.images + page.text_boxes:
        x0, y0, x1, y1 = im.bbox
        reach = y1
        if im.rotation:  # lowest corner of the rectangle rotated about its centre
            a = math.radians(im.rotation)
            reach = (y0 + y1) / 2 + (abs(math.sin(a)) * (x1 - x0) + abs(math.cos(a)) * (y1 - y0)) / 2
        bottom = max(bottom, reach + 1)
    return math.ceil(bottom)


def _scale_ink(page: Page, k: float) -> None:
    for st in page.strokes:
        st.xs = [x * k for x in st.xs]
        st.ys = [y * k for y in st.ys]
        st.bbox = tuple(v * k for v in st.bbox)
        if st.pen_size:
            st.pen_size *= k
    for im in page.images:
        im.bbox = tuple(v * k for v in im.bbox)
    for box in page.text_boxes:
        box.bbox = tuple(v * k for v in box.bbox)
        box.font_scale *= k


def _fit_overflowing_ink(page: Page) -> None:
    """Some pages declare a narrow canvas (1080) but store ink on the wide one (1848), so
    most of it lies far outside the page. The app shrinks such ink to fit; do the same."""
    ends = sorted(max(s.xs) for s in page.strokes if s.xs and (s.pen_size or 0) < HIGHLIGHTER_SIZE)
    if len(ends) < 40 or page.width >= WIDE_CANVAS or ends[int(len(ends) * 0.98)] <= page.width * 1.15:
        return
    page.rescaled_ink = page.width / WIDE_CANVAS
    _scale_ink(page, page.rescaled_ink)
    for box in page.text_boxes:
        box.font_scale = 1.0  # type size follows the declared page width, which already fits


def parse_page(data: bytes, ink_scale: float | None = None) -> Page:
    """ink_scale overrides automatic fitting, for notes whose ink is stored smaller than
    the page. Nothing in the file marks those, so it has to be asked for."""
    r = Reader(data)
    layer_offset = r.u32()
    property_offset = r.u32()
    r.mask()
    property_mask = r.mask()
    r.u32()  # orientation
    width, height = r.u32(), r.u32()
    r.u32(); r.u32()  # offset x/y
    uuid = r.take(r.u16() * 2).decode("utf-16-le", "replace")
    header_end = property_offset or layer_offset
    fmt_version = None
    if header_end - r.pos >= 12:
        r.take(8)
        fmt_version = r.u32()
    page = Page(uuid, width, height, fmt_version)
    if property_offset:
        _read_properties(data, property_offset, layer_offset, property_mask, page)

    r.pos = layer_offset
    layer_count = r.u16()
    r.u16()  # current layer
    if layer_count == 0:
        raise FormatError("page has no layers")
    for _ in range(layer_count):
        header_size = r.u32()
        if not 14 <= header_size <= 16384:
            raise FormatError(f"layer header size {header_size}")
        r.take(header_size - 4)
        for _ in range(r.u32()):
            _walk_object(r, page)
        r.take(TRAILER)
    page.trailing_bytes = r.left
    if ink_scale:
        _scale_ink(page, ink_scale)
    else:
        _fit_overflowing_ink(page)
    return page
