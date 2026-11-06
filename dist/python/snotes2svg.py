#!/usr/bin/env python3
"""Export Samsung Notes handwriting as true vector SVG, from the original pen data.

Reads the notes Samsung Notes for Windows keeps on disk (never writes to them) and
turns each stroke into a vector path, with typed text as real, selectable text (set in
Helvetica, so line breaks can differ a little from the app). SVG needs only the standard
library; PDF export uses pypdf and Pillow from the vendor/ folder.

    python snotes2svg.py --list [TEXT]          list notes, optionally filtered by title
    python snotes2svg.py "TEXT"                 export notes whose title contains TEXT
    python snotes2svg.py --all                  export every note
    python snotes2svg.py PATH                   export a .sdocx, note folder or .page file

Options:
    -o DIR          output folder (default: ./svg-output, or ./pdf-output with --pdf)
    --pdf           one PDF per note, with the note's PDF background or grid paper behind
                    the ink, and its images. Pen strokes and PDF backgrounds stay vector.
    --no-background with --pdf: leave out the paper and background PDFs
    --ink-scale N   for a note whose ink comes out too small and bunched in the top-left:
                    enlarge it (1.68 fits the cases found so far). Applies to every page.
    --centerline    one constant-width stroked path per pen stroke, easiest to edit,
                    and suited to plotters/CAD. Default is "ink": filled shapes that
                    keep the pressure-varying line width, so it looks like your writing.
    --tolerance N   path simplification in page units (default 0.1; 0 keeps every point)
"""
from __future__ import annotations

import argparse
import math
from collections import Counter
import os
import re
import struct
import sys
import zipfile
from dataclasses import dataclass
from typing import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sgeom import stroke_shape  # noqa: E402
from spage import FormatError, Stroke, content_bottom, parse_page  # noqa: E402
from stext import RichText, TextLine, body_lines, box_lines, lines_bottom, parse_note  # noqa: E402

PACKAGE = "SAMSUNGELECTRONICSCoLtd.SamsungNotes_wyx1vj98g3asy"
UUID_UTF16 = re.compile(rb"(?:[0-9a-f]\x00){8}-\x00(?:(?:[0-9a-f]\x00){4}-\x00){3}(?:[0-9a-f]\x00){12}")


def local_state() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Packages" / PACKAGE / "LocalState"


# ---------------------------------------------------------------- note loading

@dataclass
class NoteFiles:
    title: str
    pages: list[tuple[str, bytes]]  # (uuid, raw .page bytes), in page order
    pen_names: dict[int, str]
    media: Callable[[int], "tuple[str, bytes] | None"] = lambda _id: None
    body: RichText | None = None


def parse_media_manifest(info: bytes) -> dict[int, str]:
    """mediaInfo.dat: [u32 version][u16 count]([u32 size][u32 id][u16 len][UTF-16 name]...)*.
    The id here is authoritative; a filename's "N@" prefix is only a fallback."""
    out: dict[int, str] = {}
    try:
        _, count = struct.unpack_from("<IH", info, 0)
        pos = 6
        for _ in range(count):
            size, ident, length = struct.unpack_from("<IIH", info, pos)
            name = info[pos + 10:pos + 10 + length * 2].decode("utf-16-le")
            out[ident] = name.replace("\\", "/").rsplit("/", 1)[-1]
            pos += 4 + size
    except (struct.error, UnicodeDecodeError):
        pass
    return out


def _media_lookup(names: list[str], manifest: dict[int, str], read: Callable[[str], bytes]):
    """Builds id -> (filename, bytes), reading a file only when it is asked for."""
    by_prefix = {}
    for name in names:
        head = name.split("@", 1)[0]
        if head.isdigit():
            by_prefix[int(head)] = name
    available = set(names)

    def lookup(media_id: int):
        name = manifest.get(media_id)
        if name not in available:
            name = by_prefix.get(media_id)
        return None if name is None else (name, read(name))
    return lookup


def _note_body(note: bytes | None) -> RichText | None:
    """The note's typed text and text settings, or None if there's no readable note record."""
    if not note:
        return None
    try:
        body = parse_note(note).body
    except (FormatError, struct.error, IndexError, UnicodeDecodeError) as e:
        print(f"  ! this note's typed text could not be read, so it is left out: {e}", file=sys.stderr)
        return None
    return body


def parse_pen_table(note: bytes) -> dict[int, str]:
    """note.note holds [u32 size][u16 count]([u32 id][u16 len][UTF-16 text])*count.
    The table has no fixed offset, so try each position before the first pen name and
    accept only one whose entries land exactly on its declared size."""
    first = note.find("com.samsung".encode("utf-16-le"))
    if first < 0:
        return {}
    for start in range(first - 12, max(first - 4096, -1), -1):
        if start < 0:
            continue
        size, count = struct.unpack_from("<IH", note, start)
        end = start + 4 + size  # size excludes its own 4 bytes
        if not 0 < count < 4096 or end > len(note):
            continue
        pos, table = start + 6, {}
        try:
            for _ in range(count):
                ident, length = struct.unpack_from("<IH", note, pos)
                pos += 6
                table[ident] = note[pos:pos + length * 2].decode("utf-16-le")
                pos += length * 2
        except (struct.error, UnicodeDecodeError):
            continue
        if pos == end:
            return table
    return {}


def _ordered(files: dict[str, bytes]) -> list[tuple[str, bytes]]:
    """Pages in document order per pageIdInfo.dat; pages it omits are unused drafts."""
    pages = {n[:-5]: d for n, d in files.items() if n.endswith(".page")}
    info = files.get("pageIdInfo.dat")
    if info:
        order = [m.group().decode("utf-16-le") for m in UUID_UTF16.finditer(info)]
        listed = [(u, pages[u]) for u in dict.fromkeys(order) if u in pages]
        if listed:
            return listed
    return sorted(pages.items())


def _folder_media(folder: Path):
    media_dir = folder / "media"
    names = [p.name for p in media_dir.iterdir() if p.is_file()] if media_dir.is_dir() else []
    manifest = parse_media_manifest((media_dir / "mediaInfo.dat").read_bytes()) if "mediaInfo.dat" in names else {}
    return _media_lookup(names, manifest, lambda name: (media_dir / name).read_bytes())


def load_folder(folder: Path, title: str | None = None) -> NoteFiles:
    wanted = [p for p in folder.iterdir() if p.is_file() and (p.suffix == ".page" or p.name in ("note.note", "pageIdInfo.dat"))]
    files = {p.name: p.read_bytes() for p in wanted}
    return NoteFiles(title or folder.name, _ordered(files), parse_pen_table(files.get("note.note", b"")),
                     _folder_media(folder), _note_body(files.get("note.note")))


def load_sdocx(path: Path) -> NoteFiles:
    # Reads members into memory only; nothing from the archive is written to disk.
    with zipfile.ZipFile(path) as z:
        files = {}
        members: dict[str, str] = {}
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1]
            if base.endswith(".page") or base in ("note.note", "pageIdInfo.dat"):
                files[base] = z.read(name)
            elif name.startswith("media/") and base:
                members[base] = name
        manifest = parse_media_manifest(z.read(members["mediaInfo.dat"])) if "mediaInfo.dat" in members else {}

    def read(base: str) -> bytes:
        with zipfile.ZipFile(path) as archive:
            return archive.read(members[base])
    return NoteFiles(path.stem, _ordered(files), parse_pen_table(files.get("note.note", b"")),
                     _media_lookup(list(members), manifest, read), _note_body(files.get("note.note")))


def library() -> list[tuple[str, str, Path]]:
    """(title, uuid, folder) of every non-deleted note, newest first."""
    import sqlite3  # Only local-library discovery needs SQLite; file conversion does not.
    state = local_state()
    db = sqlite3.connect(f"{(state / 'Storage.sqlite').as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        rows = db.execute(
            "SELECT Title, RecommendedTitle, UUID, FilePath FROM NoteDB "
            "WHERE DeletedStatus = 0 ORDER BY LastModifiedAt DESC").fetchall()
    finally:
        db.close()
    out = []
    for title, recommended, uuid, path in rows:
        folder = Path(path) if path else state / "wdoc" / uuid
        if folder.is_dir():
            out.append((title or recommended or uuid, uuid, folder))
    return out


# ------------------------------------------------------------------- SVG output

def fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def svg_path_data(commands: list[tuple]) -> str:
    out = []
    for c in commands:
        op = c[0]
        if op in "ML":
            out.append(f"{op}{fmt(c[1])} {fmt(c[2])}")
        elif op == "Q":
            out.append(f"Q{fmt(c[1])} {fmt(c[2])} {fmt(c[3])} {fmt(c[4])}")
        elif op == "A":
            r = fmt(c[1])
            out.append(f"A{r} {r} 0 {c[2]} 0 {fmt(c[3])} {fmt(c[4])}")
        else:
            out.append("Z")
    return "".join(out)


def stroke_svg(s: Stroke, pen_name: str, centerline: bool, tol: float) -> str:
    shape = stroke_shape(s, pen_name, centerline, tol)
    if shape is None:
        return ""
    color = f"#{shape.rgb:06x}"
    d = svg_path_data(shape.commands)
    if shape.filled:
        opacity = f' fill-opacity="{shape.alpha:.2f}"' if shape.alpha < 1 else ""
        return f'<path d="{d}" fill="{color}"{opacity}/>'
    opacity = f' stroke-opacity="{shape.alpha:.2f}"' if shape.alpha < 1 else ""
    cap = "round" if shape.round_cap else "butt"
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{fmt(shape.width)}" '
            f'stroke-linecap="{cap}" stroke-linejoin="round"{opacity}/>')


def _xml(text: str) -> str:
    # Characters XML 1.0 cannot hold at all (stray controls) are dropped.
    clean = "".join(c for c in text if (c >= " " or c == "\t") and c not in "\ufffe\uffff" and not "\ud800" <= c <= "\udfff")
    return clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_svg(lines: list[TextLine], box: RichText | None = None) -> str:
    """Real, editable <text>; each line is positioned, so wrapping survives any editor."""
    out = []
    for line in lines:
        for run in line.runs:
            if run.text and run.background is not None:
                out.append(f'<rect x="{fmt(run.x)}" y="{fmt(line.baseline - run.size * 0.95)}" width="{fmt(run.width)}" '
                           f'height="{fmt(run.size * 1.3)}" fill="#{run.background:06x}"/>')
        spans = []
        for run in line.runs:
            if not run.text:
                continue
            attrs = f' x="{fmt(run.x)}" font-size="{fmt(run.size)}" fill="#{run.rgb:06x}"'
            if run.bold:
                attrs += ' font-weight="bold"'
            if run.italic:
                attrs += ' font-style="italic"'
            deco = " ".join(n for n, on in (("underline", run.underline), ("line-through", run.strike)) if on)
            if deco:
                attrs += f' text-decoration="{deco}"'
            spans.append(f"<tspan{attrs}>{_xml(run.text)}</tspan>")
        if spans:
            out.append(f'<text y="{fmt(line.baseline)}" font-family="Helvetica, Arial, sans-serif" '
                       f'xml:space="preserve">{"".join(spans)}</text>')
    if not out:
        return ""
    if box is not None and box.rotation:
        cx, cy = (box.bbox[0] + box.bbox[2]) / 2, (box.bbox[1] + box.bbox[3]) / 2
        return f'<g transform="rotate({fmt(box.rotation)} {fmt(cx)} {fmt(cy)})">' + "".join(out) + "</g>"
    return "\n".join(out)


def page_svg(raw: bytes, pens: dict[int, str], centerline: bool, tol: float, ink_scale: float | None = None,
             note_body: RichText | None = None, index: int = -1):
    page = parse_page(raw, ink_scale)
    width = max(page.width, 1)
    typed = body_lines(note_body, index, width) if note_body else []
    for box in page.text_boxes:
        if note_body:
            box.size_delta = note_body.size_delta
    boxes = {id(b): box_lines(b, width) for b in page.text_boxes}
    height = math.ceil(max(content_bottom(page), lines_bottom(typed)))
    body = [text_svg(typed)]
    for item in page.drawables:
        if isinstance(item, RichText):
            body.append(text_svg(boxes[id(item)], item))
        elif isinstance(item, Stroke):
            body.append(stroke_svg(item, pens.get(item.pen_name_id, ""), centerline, tol))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'width="{width}" height="{height}">\n' + "\n".join(filter(None, body)) + "\n</svg>\n")
    return svg, page


# ------------------------------------------------------------------- commands

RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{d}{i}" for d in ("COM", "LPT") for i in "123456789\u00b9\u00b2\u00b3"}


def safe_name(text: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)[:100].strip(" .") or "note"
    return f"_{name}" if name.split(".")[0].strip().upper() in RESERVED else name


def export(note: NoteFiles, out_dir: Path, centerline: bool, tol: float,
           ink_scale: float | None = None) -> tuple[int, int, int]:
    dest = out_dir
    if dest.is_dir():  # a re-export with fewer pages must not leave old pages behind
        for old in dest.glob("page-*.svg"):
            old.unlink()
    pages = strokes = 0
    skipped: dict[str, int] = {}
    errors = 0
    for i, (uuid, raw) in enumerate(note.pages, 1):
        try:
            svg, page = page_svg(raw, note.pen_names, centerline, tol, ink_scale, note.body, i - 1)
        except (ValueError, struct.error, IndexError) as e:
            print(f"  ! page {i} ({uuid}) could not be read: {e}", file=sys.stderr)
            errors += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"page-{i:03d}.svg").write_text(svg, encoding="utf-8")
        pages += 1
        strokes += len(page.strokes)
        for k, v in page.other_objects.items():
            skipped[k] = skipped.get(k, 0) + v
    extra = f"  (not exported: {skipped})" if skipped else ""
    print(f"  {note.title}: {pages} pages, {strokes} strokes -> {dest}{extra}")
    return pages, strokes, errors


def export_pdf(note: NoteFiles, dest: Path, centerline: bool, tol: float, backgrounds: bool,
               ink_scale: float | None = None) -> tuple[int, int, int]:
    from spdf import NotePdf  # needs the libraries in vendor/, so only load it for PDFs

    pdf = NotePdf(note.media, note.pen_names, centerline, tol, backgrounds, ink_scale, note.body)
    pages = strokes = errors = 0
    skipped: dict[str, int] = {}
    for i, (uuid, raw) in enumerate(note.pages, 1):
        try:
            pdf.page_index = i - 2  # keep typed text on its own page if an earlier page failed
            page = pdf.add_page(raw)
        except (ValueError, struct.error, IndexError) as e:
            print(f"  ! page {i} ({uuid}) could not be read: {e}", file=sys.stderr)
            errors += 1
            continue
        pages += 1
        strokes += len(page.strokes)
        for k, v in page.other_objects.items():
            skipped[k] = skipped.get(k, 0) + v
    if pages:
        pdf.save(dest)
    extra = f"  (not exported: {skipped})" if skipped else ""
    print(f"  {note.title}: {pages} pages, {strokes} strokes -> {dest}{extra}")
    for text, count in pdf.warnings.items():
        print(f"  ! {text} ({count}x)", file=sys.stderr)
    return pages, strokes, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="title text, or a .sdocx / note folder / .page path")
    ap.add_argument("--all", action="store_true", help="export every note")
    ap.add_argument("--list", action="store_true", help="list notes instead of exporting")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--centerline", action="store_true")
    ap.add_argument("--pdf", action="store_true", help="write one PDF per note instead of SVGs")
    ap.add_argument("--ink-scale", type=float, help="multiply ink position and size, e.g. 1.68")
    ap.add_argument("--no-background", action="store_true", help="PDF: ink and images only, no paper or background PDF")
    ap.add_argument("--tolerance", type=float, default=0.1)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="backslashreplace")  # titles may not fit the console's code page
    sys.stderr.reconfigure(errors="backslashreplace")

    args.out = args.out or Path("pdf-output" if args.pdf else "svg-output")

    def run(note: NoteFiles, name: str) -> tuple[int, int, int]:
        if args.pdf:
            return export_pdf(note, args.out / f"{name}.pdf", args.centerline, args.tolerance, not args.no_background,
                              args.ink_scale)
        return export(note, args.out / name, args.centerline, args.tolerance, args.ink_scale)

    path = Path(args.target) if args.target else None
    is_note_file = path and path.is_file() and path.suffix.lower() in (".page", ".sdocx")
    is_note_folder = path and path.is_dir() and any(path.glob("*.page"))
    if (is_note_file or is_note_folder) and not args.list:
        if path.suffix.lower() == ".page":
            note = NoteFiles(path.stem, [(path.stem, path.read_bytes())],
                             parse_pen_table((path.parent / "note.note").read_bytes()) if (path.parent / "note.note").exists() else {},
                             _folder_media(path.parent))  # a lone page has no place in the note's text flow
        elif path.is_dir():
            note = load_folder(path)
        else:
            note = load_sdocx(path)
        pages, _, errors = run(note, safe_name(note.title))
        return 1 if errors or not pages else 0

    notes = library()
    if args.target:
        notes = [n for n in notes if args.target.lower() in n[0].lower()]
    if args.list:
        for title, uuid, folder in notes:
            print(f"{title}  [{uuid}]")
        print(f"{len(notes)} notes")
        return 0
    if not args.target and not args.all:
        ap.error("give a title, a path, --list or --all")
    if not notes:
        print(f'No note title contains "{args.target}". Try --list.', file=sys.stderr)
        return 1

    # Every note sharing a name gets its id, so names stay the same from run to run.
    uses = Counter(safe_name(title).casefold() for title, _, _ in notes)
    total_pages = total_strokes = errors = failed = 0
    for title, uuid, folder in notes:
        name = safe_name(title)
        if uses[name.casefold()] > 1:
            name = f"{name} [{uuid}]"
        try:
            p, s, e = run(load_folder(folder, title), name)
        except Exception as ex:  # one bad note must not stop the rest
            print(f"  ! {title} [{uuid}] could not be exported: {type(ex).__name__}: {ex}", file=sys.stderr)
            failed += 1
            continue
        total_pages += p; total_strokes += s; errors += e
    print(f"Done: {len(notes)} notes, {total_pages} pages, {total_strokes} strokes, "
          f"{errors} unreadable pages, {failed} notes failed.")
    return 1 if errors or failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
