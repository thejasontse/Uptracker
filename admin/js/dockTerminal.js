// Fixed terminal at the bottom of the docked panel. Shows the timestamped raw
// stdout/stderr and parsed metric values from Test runs (Dataset / Metrics /
// Rules editors). Cleared on route change; hidden outside editor pages. The
// manual "Run" button on a dataset card does NOT write here — only dry-runs do,
// so the terminal always reflects what the operator just tested.
let headerEl = null;
let rawEl = null;
let parsedEl = null;

function ensureElements() {
  if (headerEl && rawEl && parsedEl) return;
  headerEl = document.getElementById('dockTerminalHeader');
  rawEl = document.getElementById('dockTerminalRaw');
  parsedEl = document.getElementById('dockTerminalParsed');
}

function timestamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function setVisible(visible) {
  const el = document.getElementById('dockTerminal');
  if (el) el.hidden = !visible;
}

export function clearTerminal() {
  ensureElements();
  if (headerEl) headerEl.textContent = '';
  if (rawEl) rawEl.textContent = '';
  if (parsedEl) parsedEl.innerHTML = '';
}

// Render a /api/test/run response: timestamped header line, raw stdout/stderr,
// and a small table of parsed metric values. ``source`` labels which page ran
// the test (e.g. "dataset editor", "metrics", "rules").
export function showTestResult(res, source) {
  ensureElements();
  const status = res.ok ? 'ok' : (res.error || 'failed');
  if (headerEl) headerEl.textContent = `${timestamp()} — ${source} — ${status}`;
  if (rawEl) rawEl.textContent = res.raw || '(no output)';
  if (!parsedEl) return;
  const parsed = res.parsed || [];
  if (!parsed.length) { parsedEl.innerHTML = ''; return; }
  parsedEl.innerHTML = `
    <table class="term-parsed">
      <thead><tr><th>metric</th><th>value</th></tr></thead>
      <tbody>${parsed.map((p) => `<tr><td>${escapeHtml(p.name)}</td><td>${formatParsed(p)}</td></tr>`).join('')}</tbody>
    </table>`;
}

function formatParsed(p) {
  if (p.error) return `<span class="err">${escapeHtml(p.error)}</span>`;
  const shown = p.txt != null && p.txt !== '' ? p.txt : (p.num != null ? String(p.num) : '');
  return escapeHtml(shown);
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
