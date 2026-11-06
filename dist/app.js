const $ = id => document.getElementById(id);
let selected = null, worker = null, downloadURL = null, busy = false;
function status(text, error = false) { $('status-area').hidden = false; $('status').textContent = text; $('status').classList.toggle('error', error); }
function clearResult() { if (downloadURL) URL.revokeObjectURL(downloadURL); downloadURL = null; $('result').hidden = true; $('notices').hidden = true; }
function choose(file) {
  if (busy) return;
  clearResult(); selected = null; $('convert').disabled = true;
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.sdocx')) return status('Choose a Samsung Notes .sdocx file. PDFs and screenshots don’t contain the original pen data.', true);
  if (!file.size || file.size > 150 * 1024 * 1024) return status('Choose a non-empty .sdocx file smaller than 150 MB.', true);
  selected = file;
  $('file-label').textContent = file.name;
  $('file-meta').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · Click to choose another note`;
  $('convert').disabled = false; $('status-area').hidden = true;
}
$('file').addEventListener('change', event => choose(event.target.files[0]));
for (const type of ['dragenter','dragover']) $('dropzone').addEventListener(type, event => { event.preventDefault(); if (!busy) $('dropzone').classList.add('dragging'); });
for (const type of ['dragleave','drop']) $('dropzone').addEventListener(type, event => { event.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', event => choose(event.dataTransfer.files[0]));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
function finish() { busy = false; $('convert').disabled = !selected; $('file').disabled = false; $('background').disabled = false; $('cancel').hidden = true; }
$('cancel').addEventListener('click', () => { worker?.terminate(); worker = null; finish(); $('progress').style.width = '0%'; status('Conversion cancelled. You can choose a note and try again.'); });
$('convert').addEventListener('click', async () => {
  if (!selected || busy) return;
  clearResult(); busy = true; $('convert').disabled = true; $('file').disabled = true; $('background').disabled = true; $('cancel').hidden = false;
  $('progress').style.width = '4%'; status('Preparing the converter… First use downloads the conversion engine and may take a moment.');
  try {
    if (!worker) {
      worker = new Worker(new URL('./worker.js', document.baseURI), {type: 'module'});
      worker.onmessage = ({data}) => {
        if (data.type === 'progress') { status(data.message); $('progress').style.width = `${data.percent}%`; }
        if (data.type === 'error') { status(data.message, true); finish(); }
        if (data.type === 'done') {
          downloadURL = URL.createObjectURL(new Blob([data.pdf], {type: 'application/pdf'}));
          $('download').href = downloadURL; $('download').download = selected.name.replace(/\.sdocx$/i, '') + '.pdf';
          $('result-meta').textContent = `${data.pages} ${data.pages === 1 ? 'page' : 'pages'} · ${(data.pdf.byteLength / 1024 / 1024).toFixed(1)} MB · Vector handwriting`;
          $('result').hidden = false; $('progress').style.width = '100%'; status('Conversion complete. Your PDF is ready to download.');
          if (data.warnings) { $('notices').hidden = false; $('warnings').textContent = data.warnings; }
          finish();
        }
      };
      worker.onerror = () => { status('Couldn’t start the conversion engine. Check your internet connection, then try again.', true); worker?.terminate(); worker = null; finish(); };
    }
    const bytes = await selected.arrayBuffer();
    if (!busy) return;
    worker.postMessage({bytes, backgrounds: $('background').checked}, [bytes]);
  } catch (error) { status(`Couldn’t read this file: ${error.message}`, true); finish(); }
});
