// Read-only docked panel: shows each dataset on the selected node with its
// latest metric readings, last-run time and any error — mirroring the admin
// panel's card layout. Deliberately minimal apart from that: no dataset/metric
// editing controls. The only action is a per-dataset Run button (admin panel.js).
import { api } from './api.js';
import { state } from './state.js';
import { getEffectiveSelectedNodeIds, onSelectionChanged } from './graph.js';
import { refreshNow } from './poll.js';
import { formatTimestamp } from './util.js';

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function datasetCard(d) {
  // Latest reading per metric: value text (txt, or num+unit) coloured by the
  // metric's current status. Metrics with no collected value yet are skipped.
  const readings = (d.metrics || [])
    .filter((m) => m.value)
    .map((m) => {
      const v = m.value;
      const num = v.num != null ? `${v.num}${v.unit ? esc(m.unit) : ''}` : '';
      const shown = v.txt != null && v.txt !== '' ? v.txt : num;
      if (shown === '' || shown == null) return '';
      return `<span class="reading" style="color:${esc(v.status_color || '#888')}" title="${esc(m.name)}: ${esc(shown)}">${esc(m.name)} <b>${esc(shown)}</b></span>`;
    })
    .filter(Boolean)
    .join('');
  const ts = formatTimestamp(d.last_run_at);
  return `
  <div class="card">
    <div class="head">
      <span class="t">${esc(d.name)}</span>
      <button class="small" data-act="run" data-ds="${d.id}" title="Run this dataset now">Run</button>
    </div>
    ${readings ? `<div class="readings">${readings}</div>` : ''}
    ${d.last_error ? `<div class="err">last run: ${esc(d.last_error)}</div>` : ''}
    ${ts ? `<div class="kv"><span class="k">last run</span><span>${esc(ts)}</span></div>` : ''}
  </div>`;
}

function nodeHtml(n) {
  return `
  <div class="section">
    <h2>${esc(n.label)}</h2>
    <h3>Datasets</h3>
    ${(n.datasets || []).map(datasetCard).join('') || '<p class="muted">No datasets on this node.</p>'}
  </div>`;
}

function renderDock() {
  const body = document.getElementById('dockBody');
  if (!body) return;
  const sel = getEffectiveSelectedNodeIds();
  if (sel.size === 1) {
    // Selection ids come from the DOM as strings; node ids may be numeric.
    const id = [...sel][0];
    const node = state.snapshot.nodes.find((n) => String(n.id) === String(id));
    if (node) { body.innerHTML = nodeHtml(node); return; }
  }
  body.innerHTML = sel.size > 1
    ? '<p class="muted pad">Select a single node to see its datasets.</p>'
    : '<p class="muted pad">Select a node to see its datasets.</p>';
}

async function onDockClick(e) {
  const act = e.target.closest('[data-act="run"]');
  if (!act) return;
  act.disabled = true;
  const prev = act.textContent;
  act.textContent = '\u2026';
  try {
    await api.runDataset(Number(act.dataset.ds));
  } catch (err) {
    console.error('dataset run failed', err);
    act.textContent = '!';
    setTimeout(() => { act.textContent = prev; act.disabled = false; }, 1200);
    return;
  }
  try { await refreshNow(); } catch { /* the poll will catch up */ }
  // The snapshot repaint may have replaced this button already; guard.
  if (document.contains(act)) { act.textContent = prev; act.disabled = false; }
}

export function initDock() {
  const body = document.getElementById('dockBody');
  if (!body) return;
  body.addEventListener('click', onDockClick);
  onSelectionChanged(renderDock);
  state.subscribe(renderDock);
  renderDock();
}
