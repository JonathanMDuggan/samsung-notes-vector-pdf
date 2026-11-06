# Noteform

Convert a Samsung Notes `.sdocx` file into a vector PDF in your browser.
Pen strokes remain vector shapes. Typed text is selectable; embedded photos stay images.
Conversion runs in a Web Worker using Pyodide. Note files are never uploaded.

## Run locally

Serve the `dist` directory with a local web server, for example:

```sh
python -m http.server 8766 --directory dist
```

Open `http://localhost:8766`. Choose a `.sdocx`, convert, and download the PDF.
Opening `index.html` directly does not work because browsers restrict local workers.

## GitHub Pages

The included workflow publishes `dist` on pushes to `main`.
In repository **Settings → Pages**, choose **GitHub Actions** as the source.
No server, account sign-in, API key, or database is required for visitors.

## Runtime and dependencies

- Pyodide 314.0.7 and Pillow load from the official Pyodide distribution on jsDelivr.
- pypdf 6.21.4 is bundled as a pure-Python ZIP; its license is included.
- The conversion engine is shared with the Samsung Notes desktop exporter.
- Initial conversion needs internet access to download the runtime. Note bytes stay local.
- Fonts load from Google Fonts, with system-font fallbacks.

## Limits

- Accepts `.sdocx` files, up to 150 MB compressed and 512 MB uncompressed.
- A raster PDF or screenshot cannot restore the original vector pen strokes.
- Typed-text fonts, symbols and line wrapping may differ from Samsung Notes.
- Unusual note features may be omitted; export notices appear beside the download.
- Large notes may exceed browser memory; use the desktop exporter for those.
- This independent project is not affiliated with Samsung.

No personal note files or sample PDFs are included in this repository.
