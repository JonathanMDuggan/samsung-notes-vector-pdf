"""Writes one note as a multi-page PDF: background PDF or paper, then images and ink in
drawing order. Pen strokes and PDF backgrounds stay vector; photos stay as images."""
from __future__ import annotations

import io
import math
import os
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from PIL import Image  # noqa: E402
from pypdf import PdfReader, PdfWriter, Transformation  # noqa: E402
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject,  # noqa: E402
                           NameObject, NumberObject, StreamObject)

from sgeom import Shape, stroke_shape  # noqa: E402
from spage import ImageObject, Page, Stroke, content_bottom, parse_page  # noqa: E402
from stext import RichText, TextLine, body_lines, box_lines, encode_pdf, lines_bottom  # noqa: E402

BLANK_WIDTH_PT = 596.0  # A4 width, used for pages with no background PDF to take a size from
K = 0.5522847498  # cubic Bezier control distance for a quarter circle
NEAR_WHITE = 0.97
MAX_PAGE_PT = 14400  # the largest page PDF viewers are required to handle
PAPER_GRAY = 0.75  # matches the grey of the app's grid-paper PDFs
MediaLoader = Callable[[int], "tuple[str, bytes] | None"]


def _n(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _half_circle(ax: float, ay: float, bx: float, by: float) -> str:
    """Two cubics from a to b, counter-clockwise on screen (see sgeom's "A" command)."""
    cx, cy = (ax + bx) / 2, (ay + by) / 2
    vx, vy = ax - cx, ay - cy
    mx, my = cx + vy, cy - vx
    return (f"{_n(ax + K * vy)} {_n(ay - K * vx)} {_n(mx + K * vx)} {_n(my + K * vy)} {_n(mx)} {_n(my)} c\n"
            f"{_n(mx - K * vx)} {_n(my - K * vy)} {_n(bx + K * vy)} {_n(by - K * vx)} {_n(bx)} {_n(by)} c\n")


def path_ops(commands: list[tuple]) -> str:
    out, x, y = [], 0.0, 0.0
    for c in commands:
        op = c[0]
        if op == "M":
            x, y = c[1], c[2]
            out.append(f"{_n(x)} {_n(y)} m\n")
        elif op == "L":
            x, y = c[1], c[2]
            out.append(f"{_n(x)} {_n(y)} l\n")
        elif op == "Q":  # PDF has no quadratic, so raise the degree
            cx, cy, ex, ey = c[1:]
            out.append(f"{_n(x + (cx - x) * 2 / 3)} {_n(y + (cy - y) * 2 / 3)} "
                       f"{_n(ex + (cx - ex) * 2 / 3)} {_n(ey + (cy - ey) * 2 / 3)} {_n(ex)} {_n(ey)} c\n")
            x, y = ex, ey
        elif op == "A":
            out.append(_half_circle(x, y, c[3], c[4]))
            x, y = c[3], c[4]
        else:
            out.append("h\n")
    return "".join(out)


def _visible_box(page) -> tuple[float, float, float, float]:
    """Left, bottom, width, height of the page's crop box, or media box if that is empty."""
    for box in (page.cropbox, page.mediabox):
        if float(box.width) > 0 and float(box.height) > 0:
            return float(box.left), float(box.bottom), float(box.width), float(box.height)
    raise ValueError("page has no area")


def _rgb(value: int) -> tuple[float, float, float]:
    return (value >> 16 & 255) / 255, (value >> 8 & 255) / 255, (value & 255) / 255


def _fresh(prefix: str, taken: set) -> str:
    """A resource name unused on this page, including by its background PDF."""
    i = 0
    while f"{prefix}{i}" in taken:
        i += 1
    taken.add(f"{prefix}{i}")
    return f"{prefix}{i}"


class NotePdf:
    def __init__(self, media: MediaLoader, pens: dict[int, str], centerline: bool, tol: float, backgrounds: bool,
                 ink_scale: float | None = None, body: RichText | None = None):
        self.writer = PdfWriter()
        self.body = body
        self.page_index = -1
        self.ink_scale = ink_scale
        self.media, self.pens, self.centerline, self.tol = media, pens, centerline, tol
        self.backgrounds = backgrounds
        self.readers: dict[int, PdfReader | None] = {}
        self.images: dict[tuple, object] = {}
        self.warnings: dict[str, int] = {}

    def warn(self, text: str) -> None:
        self.warnings[text] = self.warnings.get(text, 0) + 1

    def _reader(self, media_id: int) -> PdfReader | None:
        if media_id not in self.readers:
            reader = None
            found = self.media(media_id)
            if found is None:
                self.warn(f"background file {media_id} is missing")
            else:
                try:
                    reader = PdfReader(io.BytesIO(found[1]))
                    if reader.is_encrypted and not reader.decrypt(""):
                        self.warn(f"background {found[0]} is password-protected")
                        reader = None
                    elif "/OCProperties" in reader.trailer["/Root"]:
                        self.warn(f"background {found[0]} has optional layers; hidden ones may show")
                except Exception as e:  # malformed third-party PDFs raise many types
                    self.warn(f"background {found[0]} could not be read: {type(e).__name__}")
                    reader = None
            self.readers[media_id] = reader
        return self.readers[media_id]

    def _background_page(self, page: Page):
        """The PDF page behind this note page and its target rectangle, if any."""
        if not self.backgrounds or not page.pdf_backgrounds:
            return None, None
        record = page.pdf_backgrounds[0]
        if len(page.pdf_backgrounds) > 1:
            self.warn("page has several background PDFs; only the first was drawn")
        reader = self._reader(record.media_id)
        if reader is None:
            return None, None
        try:  # pypdf reads lazily, so a damaged PDF can fail at any of these steps
            if not 0 <= record.page_index < len(reader.pages):
                self.warn(f"background page {record.page_index} does not exist")
                return None, None
            src = reader.pages[record.page_index]
            # Links would drag in clones of the pages they target, and cannot work here anyway.
            if "/Annots" in src:
                del src["/Annots"]
            try:
                src.transfer_rotation_to_content()
            except Exception as e:
                self.warn(f"background page could not be rotated: {type(e).__name__}")
            box = _visible_box(src)
        except Exception as e:
            self.warn(f"background page could not be read: {type(e).__name__}")
            return None, None
        return src, (record.rect, box)

    def _image_xobject(self, media_id: int, crop) -> object | None:
        key = (media_id, crop)
        if key in self.images:
            return self.images[key]
        ref = None
        found = self.media(media_id)
        if found is None:
            self.warn(f"image file {media_id} is missing")
        else:
            try:
                ref = self._embed_image(found[1], crop)
            except Exception as e:  # Pillow raises many types on damaged or odd files
                self.warn(f"image {found[0]} could not be decoded: {type(e).__name__}")
        self.images[key] = ref
        return ref

    def _embed_image(self, data: bytes, crop) -> object:
        img = Image.open(io.BytesIO(data))
        if crop:
            box = (max(crop[0], 0), max(crop[1], 0), min(crop[2], img.width), min(crop[3], img.height))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("crop has no area")
            img = img.crop(box)
        if img.mode.startswith("I;16") or img.mode == "I":  # convert() would clip these to white
            img = img.point(lambda v: v * (1 / 257), "L")
        stream = StreamObject()
        d = {"/Type": NameObject("/XObject"), "/Subtype": NameObject("/Image"),
             "/Width": NumberObject(img.width), "/Height": NumberObject(img.height),
             "/BitsPerComponent": NumberObject(8)}
        if not crop and img.format == "JPEG" and img.mode in ("RGB", "L"):  # embed untouched
            stream._data = data
            d["/ColorSpace"] = NameObject("/DeviceRGB" if img.mode == "RGB" else "/DeviceGray")
            d["/Filter"] = NameObject("/DCTDecode")
        else:
            rgba = img.convert("RGBA")
            flat = DecodedStreamObject()
            flat.set_data(rgba.convert("RGB").tobytes())
            flat = flat.flate_encode()
            stream._data = flat._data
            d["/ColorSpace"] = NameObject("/DeviceRGB")
            d["/Filter"] = NameObject("/FlateDecode")
            alpha = rgba.getchannel("A")
            if alpha.getextrema() != (255, 255):
                mask = DecodedStreamObject()
                mask.set_data(alpha.tobytes())
                mask = mask.flate_encode()
                mask.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                             NameObject("/Width"): NumberObject(img.width), NameObject("/Height"): NumberObject(img.height),
                             NameObject("/BitsPerComponent"): NumberObject(8), NameObject("/ColorSpace"): NameObject("/DeviceGray")})
                d["/SMask"] = self.writer._add_object(mask)
        for key, value in d.items():
            stream[NameObject(key)] = value
        return self.writer._add_object(stream)

    def add_page(self, raw: bytes) -> Page:
        page = parse_page(raw, self.ink_scale)
        src, placement = self._background_page(page)
        self.page_index += 1

        note_w = max(page.width, 1)
        typed = body_lines(self.body, self.page_index, note_w) if self.body else []
        for box in page.text_boxes:
            if self.body:
                box.size_delta = self.body.size_delta
        boxes = {id(b): box_lines(b, note_w) for b in page.text_boxes}
        bottom = math.ceil(max(content_bottom(page), lines_bottom(typed)))
        if src is not None:
            rect, (box_x, box_y, box_w, box_h) = placement
            left, top, right, low = rect if rect[2] > rect[0] and rect[3] > rect[1] else (0, 0, note_w, page.height)
            scale = box_w / (right - left) if right > left else BLANK_WIDTH_PT / note_w
        else:
            scale = BLANK_WIDTH_PT / note_w
        if bottom * scale > MAX_PAGE_PT:
            self.warn("a page exceeded the PDF size limit, so its end was cut off")
            bottom = int(MAX_PAGE_PT / scale)
        width_pt, height_pt = note_w * scale, bottom * scale

        out = self.writer.add_blank_page(width=width_pt, height=height_pt)
        background_ops = b""
        if src is not None:
            sx = (right - left) * scale / box_w
            sy = (low - top) * scale / box_h
            place = (Transformation().translate(-box_x, -box_y).scale(sx, sy)
                     .translate(left * scale, height_pt - low * scale))
            try:
                out.merge_transformed_page(src, place, over=True, expand=False)
                merged = out.get_contents()
                background_ops = merged.get_data() if merged is not None else b""
            except Exception as e:  # keep the ink even if the background cannot be used
                self.warn(f"background page could not be drawn: {type(e).__name__}")

        resources = out["/Resources"].get_object()
        taken = set()
        for category in ("/XObject", "/ExtGState", "/Font"):
            if category in resources:
                taken.update(resources[category].get_object().keys())
        ops: list[str] = []
        # The app shows its stored off-white (#fcfcfc) as plain paper. #010101 behind a PDF
        # is a dark-mode marker shown as white; with no PDF it is real black (screen-off memos).
        if (self.backgrounds and page.bg_color is not None and page.bg_color >> 24
                and min(_rgb(page.bg_color)) < NEAR_WHITE
                and (page.bg_color & 0xFFFFFF != 0x010101 or src is None)):
            r, g, b = _rgb(page.bg_color)
            prefix = f"q {r:.4f} {g:.4f} {b:.4f} rg 0 0 {_n(width_pt)} {_n(height_pt)} re f Q\n"
        else:
            prefix = ""

        xobjects = DictionaryObject()
        states = DictionaryObject()
        alpha_names: dict[float, str] = {}
        # Full precision: a rounded scale drifts ink away from the background down the page.
        ops.append(f"q {scale:.8f} 0 0 {-scale:.8f} 0 {height_pt:.6f} cm 1 j\n")

        if self.backgrounds and page.bg_image is not None and src is None:
            ref = self._image_xobject(page.bg_image, None)
            if ref is not None:
                name = _fresh("/SnBg", taken)
                xobjects[NameObject(name)] = ref
                ops.append(f"q {note_w} 0 0 {-page.height} 0 {page.height} cm {name} Do Q\n")

        if self.backgrounds and src is None and page.bg_image is None and page.template_type is not None:
            ops.append(self._template_ops(page, bottom))

        fonts = DictionaryObject()
        font_names: dict[tuple, str] = {}
        if typed:
            ops.append(self._text_ops(typed, fonts, font_names, taken))

        color: int | None = None
        current_alpha = 1.0
        for item in page.drawables:
            if isinstance(item, RichText):
                if current_alpha != 1.0:
                    ops.append(f"{self._alpha(1.0, states, alpha_names, taken)} gs\n")
                    current_alpha = 1.0
                ops.append(self._text_ops(boxes[id(item)], fonts, font_names, taken, item))
                color = None  # text ops changed the fill colour
                continue
            if isinstance(item, ImageObject):
                current_alpha = self._draw_image(item, xobjects, ops, current_alpha, states, alpha_names, taken)
                continue
            shape = stroke_shape(item, self.pens.get(item.pen_name_id, ""), self.centerline, self.tol)
            if shape is None:
                continue
            if shape.alpha != current_alpha:
                ops.append(f"{self._alpha(shape.alpha, states, alpha_names, taken)} gs\n")
                current_alpha = shape.alpha
            if shape.rgb != color:
                r, g, b = _rgb(shape.rgb)
                ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg {r:.4f} {g:.4f} {b:.4f} RG\n")
                color = shape.rgb
            if shape.filled:
                ops.append(path_ops(shape.commands) + "f\n")
            else:
                ops.append(f"{_n(shape.width)} w {1 if shape.round_cap else 0} J\n" + path_ops(shape.commands) + "S\n")
        ops.append("Q\n")

        stream = DecodedStreamObject()
        stream.set_data(prefix.encode() + b"q\n" + background_ops + b"\nQ\n" + "".join(ops).encode("latin-1"))
        out[NameObject("/Contents")] = self.writer._add_object(stream.flate_encode())
        if xobjects:
            existing = resources.get("/XObject")
            target = existing.get_object() if existing is not None else DictionaryObject()
            target.update(xobjects)
            resources[NameObject("/XObject")] = target
        if fonts:
            existing = resources.get("/Font")
            target = existing.get_object() if existing is not None else DictionaryObject()
            target.update(fonts)
            resources[NameObject("/Font")] = target
        if states:
            existing = resources.get("/ExtGState")
            target = existing.get_object() if existing is not None else DictionaryObject()
            target.update(states)
            resources[NameObject("/ExtGState")] = target
        return page

    def _text_ops(self, lines: list[TextLine], fonts, names: dict[tuple, str], taken: set,
                  box: RichText | None = None) -> str:
        """Real text in Helvetica, one of PDF's built-in fonts: vector and selectable."""
        ops = ["q\n"]
        if box is not None and box.rotation:
            a = math.radians(box.rotation)
            c, s = math.cos(a), math.sin(a)
            cx, cy = (box.bbox[0] + box.bbox[2]) / 2, (box.bbox[1] + box.bbox[3]) / 2
            ops.append(f"{c:.6f} {s:.6f} {-s:.6f} {c:.6f} {_n(cx - c * cx + s * cy)} {_n(cy - s * cx - c * cy)} cm\n")
        for line in lines:
            for run in line.runs:
                if not run.text:
                    continue
                if run.background is not None:
                    r, g, b = _rgb(run.background)
                    ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg {_n(run.x)} {_n(line.baseline - run.size * 0.95)} "
                               f"{_n(run.width)} {_n(run.size * 1.3)} re f\n")
                r, g, b = _rgb(run.rgb)
                key = (run.bold, run.italic)
                if key not in names:
                    names[key] = _fresh("/SnF", taken)
                    base = "Helvetica" + {(False, False): "", (True, False): "-Bold", (False, True): "-Oblique",
                                          (True, True): "-BoldOblique"}[key]
                    fonts[NameObject(names[key])] = DictionaryObject({
                        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject(f"/{base}"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
                encoded, missing = encode_pdf(run.text)
                if missing:
                    self.warn("some typed characters are not in the PDF font and show as ?")
                ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg BT {names[key]} {_n(run.size)} Tf "
                           f"1 0 0 -1 {_n(run.x)} {_n(line.baseline)} Tm {encoded.decode('latin-1')} Tj ET\n")
                for flag, offset in ((run.underline, 0.12), (run.strike, -0.3)):
                    if flag:
                        ops.append(f"{_n(run.x)} {_n(line.baseline + run.size * offset)} {_n(run.width)} {_n(run.size * 0.06)} re f\n")
        ops.append("Q\n")
        return "".join(ops)

    def _template_ops(self, page: Page, height: int) -> str:
        """Built-in paper with no PDF behind it: 1-3 lines, 4-6 grid, 7-9 dots; each in
        three spacings. Lines and dots follow the sdocx project's layout formula."""
        kind = page.template_type
        if not 1 <= kind <= 9 or page.width <= 0 or height / page.width > 100:
            self.warn(f"built-in paper type {kind} was not drawn")
            return ""
        density = page.width / 360
        pitch = (12, 17, 28)[(kind - 1) % 3] * 1.35 * density
        top = 10 * density
        rows = [top + i * pitch for i in range(int((height - top) / pitch) + 1)]
        line = max(0.5 * density, 1)
        ops = [f"q {PAPER_GRAY} G 1 j\n"]
        if kind <= 6:
            ops.append(f"{_n(line)} w 0 J\n")
            ops.extend(f"0 {_n(y)} m {page.width} {_n(y)} l S\n" for y in rows)
            if kind >= 4:
                columns = int(page.width / pitch) + 1
                ops.extend(f"{_n(i * pitch)} {_n(top)} m {_n(i * pitch)} {_n(rows[-1])} l S\n" for i in range(columns))
        else:
            ops.append(f"{_n(max(2 * density, 1))} w 1 J\n")
            across = pitch + 1.5 * density
            xs = [across * (i + 0.5) for i in range(int(page.width / across))]
            ops.extend(f"{_n(x)} {_n(y)} m {_n(x)} {_n(y)} l S\n" for y in rows for x in xs)
        ops.append("Q\n")
        return "".join(ops)

    def _alpha(self, alpha: float, states: DictionaryObject, names: dict[float, str], taken: set) -> str:
        if alpha not in names:
            name = _fresh("/SnGs", taken)
            names[alpha] = name
            states[NameObject(name)] = DictionaryObject({NameObject("/ca"): FloatObject(alpha), NameObject("/CA"): FloatObject(alpha)})
        return names[alpha]

    def _draw_image(self, item: ImageObject, xobjects, ops, current_alpha, states, names, taken) -> float:
        ref = self._image_xobject(item.media_id, item.crop)
        if ref is None:
            return current_alpha
        if current_alpha != 1.0:
            ops.append(f"{self._alpha(1.0, states, names, taken)} gs\n")
        name = _fresh("/SnIm", taken)
        xobjects[NameObject(name)] = ref
        x0, y0, x1, y1 = item.bbox
        w, h = x1 - x0, y1 - y0
        ops.append("q\n")
        if item.rotation:
            a = math.radians(item.rotation)
            c, s = math.cos(a), math.sin(a)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            ops.append(f"{c:.6f} {s:.6f} {-s:.6f} {c:.6f} {_n(cx - c * cx + s * cy)} {_n(cy - s * cx - c * cy)} cm\n")
        ops.append(f"{_n(w)} 0 0 {_n(-h)} {_n(x0)} {_n(y1)} cm {name} Do Q\n")
        return 1.0

    def save(self, path: Path) -> None:
        try:  # hashing decodes every stream, and some (JBIG2 scans) need tools we don't have
            self.writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        except Exception as e:
            self.warn(f"file not size-optimised: {type(e).__name__}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        try:
            with open(temp, "wb") as f:
                self.writer.write(f)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


__all__ = ["NotePdf", "Shape", "Stroke", "path_ops"]
