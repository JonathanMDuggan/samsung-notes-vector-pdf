let engine;
const progress = (message, percent) => self.postMessage({type:'progress', message, percent});
async function initialize() {
  progress('Loading the conversion engine…', 7);
  const {loadPyodide} = await import('https://cdn.jsdelivr.net/pyodide/v314.0.7/full/pyodide.mjs');
  const py = await loadPyodide({indexURL:'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/'});
  progress('Preparing image support…', 16);
  await py.loadPackage('pillow');
  py.FS.mkdirTree('/converter');
  const files = ['snotes2svg.py','sgeom.py','spage.py','spdf.py','stext.py','swidths.py','pypdf.zip','web_convert.py'];
  await Promise.all(files.map(async name => {
    const response = await fetch(new URL(`./python/${name}`, import.meta.url));
    if (!response.ok) throw new Error(`Couldn’t load a converter file (${response.status}). Please reload this page.`);
    py.FS.writeFile(`/converter/${name}`, new Uint8Array(await response.arrayBuffer()));
  }));
  py.runPython("import sys; sys.path[:0] = ['/converter', '/converter/pypdf.zip']");
  return py;
}
let converting = false;
self.onmessage = async ({data}) => {
  if (converting) return;
  converting = true;
  let py;
  try {
    if (!engine) engine = initialize().catch(error => { engine = null; throw error; });
    py = await engine;
    py.FS.writeFile('/converter/input.sdocx', new Uint8Array(data.bytes));
    py.globals.set('include_backgrounds', Boolean(data.backgrounds));
    py.globals.set('notify_progress', (done,total) => progress(`Converting page ${done + 1} of ${total}…`, 25 + 65 * done / total));
    const result = JSON.parse(py.runPython("from web_convert import convert; import json; json.dumps(convert(include_backgrounds, notify_progress))"));
    progress('Preparing your download…', 96);
    const bytes = py.FS.readFile('/converter/output.pdf');
    self.postMessage({type:'done',pdf:bytes.buffer,pages:result.pages,warnings:result.warnings}, [bytes.buffer]);
  } catch (error) {
    const lines = String(error.message || error).trim().split('\n');
    const detail = lines[lines.length-1].replace(/^(?:[\w.]+Error|[\w.]+Exception):\s*/, '');
    self.postMessage({type:'error',message: `Couldn’t convert this note. ${detail}`});
  } finally {
    if (py) {
      for (const path of ['/converter/input.sdocx','/converter/output.pdf','/converter/output.pdf.tmp']) {
        try { py.FS.unlink(path); } catch { /* File may not have been created. */ }
      }
      py.runPython('import gc; gc.collect()');
    }
    converting = false;
  }
};
