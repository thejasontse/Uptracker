// Read-only docked panel: shows when each dataset on the selected node last ran.
// Deliberately minimal — no dataset/metric editing, no metric value rows (a
// dataset can drive many metrics, so a single "result" next to it is meaningless).
// The only action is a per-dataset Run button, mirroring admin's panel.js.
import { api } from './api.js';
import { state } from './state.js';
import { getEffectiveSelectedNodeIds, onSelectionChanged } from './graph.js';
import { refreshNow } from './poll.js';

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function datasetCard(d) {
  return `
  <div class="card">
    <div class="head">
      <span class="t">${esc(d.name)}</span>
      <span class="muted">${esc(d.method)}\u00B7${Math.round((d.interval_s || 0) / 60)}m</span>
      <button class="small" data-act="run" data-ds="${d.id}" title="Run this dataset now">Run</button>
    </div>
    ${d.last_run_at ? `<div class="kv"><span class="k">last run</span><span>${esc(d.last_run_at)}</span></div>` : '<div class="kv"><span class="k">last run</span><span class="muted">never</span></div>'}
    ${d.last_error ? `<div class="err">last error: ${esc(d.last_error)}</div>` : ''}
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
