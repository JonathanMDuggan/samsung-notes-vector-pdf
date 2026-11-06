"""Browser entry point. Files stay inside the browser's in-memory filesystem."""
import gc
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import zipfile

from snotes2svg import load_sdocx
from spdf import NotePdf


def convert(backgrounds, notify):
    source = Path('/converter/input.sdocx')
    destination = Path('/converter/output.pdf')
    if not zipfile.is_zipfile(source):
        raise ValueError('This is not a valid Samsung Notes file. Export it as .sdocx from Samsung Notes and try again.')
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > 20000 or sum(item.file_size for item in members) > 512 * 1024 * 1024:
            raise ValueError('This note is too large to process in a browser. Use the desktop app instead.')
        if any(item.flag_bits & 1 for item in members):
            raise ValueError('This file is password-protected. Export an unlocked Samsung Notes file first.')
    log = io.StringIO()
    with redirect_stderr(log), redirect_stdout(log):
        note = load_sdocx(source)
        if not note.pages:
            raise ValueError('No Samsung Notes pages found. Choose an original .sdocx file.')
        pdf = NotePdf(note.media, note.pen_names, False, 0.1, backgrounds, body=note.body)
        total = len(note.pages)
        for index, (_, raw) in enumerate(note.pages):
            notify(index, total)
            page = pdf.add_page(raw)
            for kind, count in page.other_objects.items():
                pdf.warn(f'{kind}: {count} objects left out on page {index + 1}')
        pdf.save(destination)
    warnings = log.getvalue().strip()
    if pdf.warnings:
        warnings += '\n' + '\n'.join(f'{text} ({count} times)' for text, count in pdf.warnings.items())
    del pdf, note
    gc.collect()
    return {'pages': total, 'warnings': warnings.strip()}
