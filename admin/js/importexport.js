// Import / Export — whole-instance archive (zip of monitor.db + graph.json).
// Export downloads the current database + graph. Import replaces the running
// instance from such an archive (the current DB is backed up server-side first).
// The credential keyfile is NOT in the archive, so credentials imported from
// another instance are flagged and must be re-keyed in the Credentials tab.
import { api } from './api.js';
import { esc } from './util.js';

// ---------------- export ----------------
export function renderExportView(body, ctx) {
  body.innerHTML = `
  <div class="section">
    <h2>Export</h2>
    <p class="muted">Download the entire instance — the node graph and the SQLite
      database (nodes, datasets, metrics, rules, settings, and encrypted
      credentials) — as a single <code>.zip</code> archive.</p>
    <div class="row"><a href="${api.exportUrl}" download><button class="primary small">Download archive (.zip)</button></a></div>
    <p class="muted">The archive does <b>not</b> include this server's credential
      encryption key. Credentials imported elsewhere will need their secrets
      re-entered.</p>
  </div>
  <div class="section">
    <h2>Dataset templates</h2>
    <p class="muted">Download all saved dataset templates as a portable
      <code>.json</code> file — share them with another instance without moving
      nodes, credentials, or settings.</p>
    <div class="row"><a href="${api.templatesExportUrl}" download><button class="small">Download templates (.json)</button></a></div>
  </div>`;
}

// ---------------- import ----------------
export function renderImportView(body, ctx) {
  body.innerHTML = `
  <div class="section">
    <h2>Import</h2>
    <p class="err">Importing <b>replaces all current data</b> — the graph, nodes,
      datasets, credentials, and settings — with the contents of an exported
      archive. The current database is backed up on the server first.</p>
    <label class="f">Archive (.zip)</label>
    <input type="file" id="impFile" accept=".zip,application/zip">
    <div class="row"><button id="impGo" class="primary small" disabled>Import &amp; replace</button>
      <span id="impStatus" class="muted"></span></div>
    <div id="impResult"></div>
  </div>`;

  const fileIn = body.querySelector('#impFile');
  const go = body.querySelector('#impGo');
  const status = body.querySelector('#impStatus');
  const result = body.querySelector('#impResult');
  go.disabled = !fileIn.files.length;
  fileIn.onchange = () => { go.disabled = !fileIn.files.length; result.innerHTML = ''; };

  go.onclick = async () => {
    const file = fileIn.files[0];
    if (!file) return;
    if (!confirm('Replace ALL current data with this archive? This cannot be undone from the UI.')) return;
    go.disabled = true;
    status.textContent = 'Reading…';
    try {
      const b64 = await readAsBase64(file);
      status.textContent = 'Importing…';
      const res = await api.importArchive(b64);
      status.textContent = '';
      result.innerHTML = `
        <p class="ok">Imported ${res.nodes} node(s), ${res.datasets} dataset(s).</p>
        ${res.credentials_needing_rekey.length
          ? `<p class="err">${res.credentials_needing_rekey.length} credential(s) need their secret
              re-entered (open the Credentials tab):
              ${res.credentials_needing_rekey.map((c) => c.name).join(', ')}.</p>`
          : ''}
        <p class="muted">Reloading…</p>`;
      // Reload so the canvas, table, and all state reflect the new database.
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      status.textContent = '';
      result.innerHTML = `<p class="err">Import failed: ${e.message}</p>`;
      go.disabled = false;
    }
  };

  renderTemplateImport(body, ctx);
}

// ---------------- dataset templates import ----------------
// Additive (unlike the archive import): every template from the JSON file is
// added with a _YYYYMMDDHHMMSS suffix on its name — nothing is overwritten.
function renderTemplateImport(body, ctx) {
  const sec = document.createElement('div');
  sec.className = 'section';
  sec.innerHTML = `
    <h2>Dataset templates</h2>
    <p class="muted">Add dataset templates from an exported <code>.json</code> file.
      Templates are never overwritten — each imported template gets the current
      date/time appended to its name.</p>
    <label class="f">Templates file (.json)</label>
    <input type="file" id="tplImpFile" accept=".json,application/json">
    <div class="row"><button id="tplImpGo" class="primary small" disabled>Import templates</button>
      <span id="tplImpStatus" class="muted"></span></div>
    <div id="tplImpResult"></div>`;
  body.appendChild(sec);

  const fileIn = sec.querySelector('#tplImpFile');
  const go = sec.querySelector('#tplImpGo');
  const status = sec.querySelector('#tplImpStatus');
  const result = sec.querySelector('#tplImpResult');
  fileIn.onchange = () => { go.disabled = !fileIn.files.length; result.innerHTML = ''; };

  go.onclick = async () => {
    const file = fileIn.files[0];
    if (!file) return;
    go.disabled = true;
    status.textContent = 'Reading…';
    try {
      const doc = JSON.parse(await file.text());
      if (!doc || doc.kind !== 'uptracker-dataset-templates' || !Array.isArray(doc.templates)) {
        throw new Error('not an uptracker dataset templates file');
      }
      status.textContent = 'Importing…';
      const res = await api.importTemplates(doc);
      status.textContent = '';
      result.innerHTML = res.created.length
        ? `<p class="ok">Added ${res.created.length} template(s): ${res.created.map((n) => esc(n)).join(', ')}.</p>
           ${res.dropped_rules ? `<p class="muted">${res.dropped_rules} rule(s) dropped (unknown status level).</p>` : ''}`
        : '<p class="muted">No templates found in the file.</p>';
      fileIn.value = '';
      if (ctx && ctx.onChange) await ctx.onChange();
    } catch (e) {
      status.textContent = '';
      result.innerHTML = `<p class="err">Import failed: ${esc(e.message)}</p>`;
      go.disabled = !fileIn.files.length;
    }
  };
}

// Read a File into a base64 string (the transport for POST /api/import).
function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const bytes = new Uint8Array(reader.result);
      let bin = '';
      const chunk = 0x8000;
      for (let i = 0; i < bytes.length; i += chunk) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
      }
      resolve(btoa(bin));
    };
    reader.onerror = () => reject(reader.error || new Error('could not read file'));
    reader.readAsArrayBuffer(file);
  });
}
