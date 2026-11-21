// Fixed terminal at the bottom of the docked panel. Shows the timestamped raw
// stdout/stderr and parsed metric values from Test runs (Dataset / Metrics /
// Rules editors). Cleared on route change; hidden outside editor pages. The
// manual "Run" button on a dataset card does NOT write here — only dry-runs do,
// so the terminal always reflects what the operator just tested.
let headerEl = null;
let rawEl = null;

function ensureElements() {
  if (headerEl && rawEl) return;
  headerEl = document.getElementById('dockTerminalHeader');
  rawEl = document.getElementById('dockTerminalRaw');
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
}

// Render a /api/test/run response into the terminal: timestamped header line,
// raw stdout/stderr, and an aligned metric/value block printed inline below the
// raw output (inside the same <pre>, not in a separate table). ``source`` labels
// which page ran the test (e.g. "dataset editor", "metrics", "rules").
export function showTestResult(res, source) {
  ensureElements();
  const status = res.ok ? 'ok' : (res.error || 'failed');
  if (headerEl) headerEl.textContent = `${timestamp()} — ${source} — ${status}`;
  if (!rawEl) return;
  let text = res.raw || '(no output)';
  const parsed = res.parsed || [];
  if (parsed.length) text += '\n\n' + renderParsed(parsed);
  rawEl.textContent = text;
}

// Build an aligned two-column "metric / value" plain-text block for the terminal.
function renderParsed(parsed) {
  const rows = parsed.map((p) => [String(p.name ?? ''), formatParsed(p)]);
  const nameWidth = Math.max('metric'.length, ...rows.map(([n]) => n.length));
  const pad = (s, w) => s + ' '.repeat(Math.max(0, w - s.length));
  const lines = ['── metrics ──', `${pad('metric', nameWidth)}  value`];
  for (const [name, value] of rows) lines.push(`${pad(name, nameWidth)}  ${value}`);
  return lines.join('\n');
}

function formatParsed(p) {
  if (p.error) return `(error: ${p.error})`;
  if (p.txt != null && p.txt !== '') return String(p.txt);
  if (p.num != null) return String(p.num);
  return '';
}
