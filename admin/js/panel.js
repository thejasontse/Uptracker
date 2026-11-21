// Right dock: the primary interface. A small view router over inline views —
// home (folder tabs: settings / credentials / import / export), node detail,
// and the dataset editor. The admin side never auto-polls; snapshots arrive only
// on demand (Refresh button) or after a mutation, so in-progress edits are safe.
import { api } from './api.js';
import { state } from './state.js';
import { esc, breadcrumb, NETWORK_PATH_PLACEHOLDER, toast, formatTimestamp } from './util.js';
import { renderDatasetEditor } from './editor.js';
import { renderMetricsView } from './metrics.js';
import { renderRulesView } from './rules.js';
import { renderTemplatesView } from './templates.js';
import { renderCredentialsView } from './credentials.js';
import { renderSettingsView } from './settings.js';
import { renderImportView, renderExportView } from './importexport.js';
import { setVisible as setTerminalVisible, clearTerminal } from './dockTerminal.js';

let route = { view: 'home' };
let homeTab = 'settings';

// Editor pages that own the bottom terminal. Other views (home tabs, node
// detail, templates) hide it entirely.
const EDITOR_VIEWS = new Set(['dataset', 'metrics', 'rules']);

const HOME_TABS = [
  { id: 'settings', label: 'Settings' },
  { id: 'credentials', label: 'Credentials' },
  { id: 'import', label: 'Import' },
  { id: 'export', label: 'Export' },
];

export function navigate(r) { route = r; render(); }

export function initPanel() {
  state.subscribe((s, why) => {
    if (why === 'select') { route = { view: state.selectedId ? 'node' : 'home', nodeId: state.selectedId }; render(); return; }
    // Snapshot update: only refresh the read-only node view. The home tab views
    // and the dataset/metrics/rules editors are live forms — re-rendering would
    // clobber in-progress edits.
    if (route.view === 'node') render();
  });
  const body = document.getElementById('dockBody');
  body.addEventListener('click', onDockClick);
  body.addEventListener('change', onDockChange);
  render();
}

function render() {
  const body = document.getElementById('dockBody');
  // Terminal visibility follows the route: only editor pages show it, and any
  // page change wipes its contents so a stale test result never lingers.
  setTerminalVisible(EDITOR_VIEWS.has(route.view));
  clearTerminal();
  switch (route.view) {
    case 'dataset': {
      const found = route.dsId ? findDataset(route.dsId) : null;
      const node = state.node(route.nodeId);
      if (!node) { route = { view: 'home' }; return render(); }
      renderDatasetEditor(body, node, found ? found.ds : null, { go: navigate, onSaved: refresh, tplId: route.tplId });
      break;
    }
    case 'metrics': {
      const found = findDataset(route.dsId);
      const node = state.node(route.nodeId);
      if (!found || !node) { route = { view: 'home' }; return render(); }
      renderMetricsView(body, node, found.ds, { go: navigate, onSaved: refresh });
      break;
    }
    case 'rules': {
      const found = findDataset(route.dsId);
      const node = state.node(route.nodeId);
      const metric = found ? found.ds.metrics.find((m) => m.id === route.metricId) : null;
      if (!found || !node || !metric) { route = { view: 'home' }; return render(); }
      renderRulesView(body, node, found.ds, metric, { go: navigate, onSaved: refresh });
      break;
    }
    case 'templates': {
      const node = state.node(route.nodeId);
      if (!node) { route = { view: 'home' }; return render(); }
      renderTemplatesView(body, node, { go: navigate, onApplied: refresh });
      break;
    }
    case 'node': {
      const n = state.node(route.nodeId);
      if (n) body.innerHTML = nodeHtml(n);
      else homeHtml();
      break;
    }
    default:
      homeHtml();
  }
}

// ---------------- home: folder tabs ----------------
// Shown when nothing is selected. Four folder tabs render their view into
// #tabBody; the tab bar stays visible while a tab is open.
function homeHtml() {
  const body = document.getElementById('dockBody');
  body.innerHTML = `
  <div class="tabs">${HOME_TABS.map((t) =>
    `<button class="tab${t.id === homeTab ? ' active' : ''}" data-tab="${t.id}">${t.label}</button>`).join('')}
    <button id="dockRefresh" class="dock-refresh" title="Fetch the latest status now">⟳ Refresh</button></div>
  <div id="tabBody"></div>`;
  const host = body.querySelector('#tabBody');
  const ctx = { go: navigate, onChange: refresh };
  if (homeTab === 'credentials') renderCredentialsView(host, ctx);
  else if (homeTab === 'import') renderImportView(host, ctx);
  else if (homeTab === 'export') renderExportView(host, ctx);
  else renderSettingsView(host, ctx);
}

// ---------------- node detail ----------------
// Tags used by other nodes, for the Tags input's datalist suggestions (split on
// comma, trimmed, deduped case-insensitively, first-seen casing wins).
function tagSuggestions(excludeNodeId) {
  const seen = new Set();
  const out = [];
  for (const other of state.snapshot.nodes) {
    if (other.id === excludeNodeId) continue;
    for (const part of String(other.tags || '').split(',')) {
      const t = part.trim();
      if (!t || seen.has(t.toLowerCase())) continue;
      seen.add(t.toLowerCase());
      out.push(t);
    }
  }
  return out.sort((a, b) => a.localeCompare(b));
}

function nodeHtml(n) {
  return `
  <div class="section">
    <h2 class="page-title">${esc(n.label)}</h2>
    ${breadcrumb([{ label: 'Datasets' }])}
    <label class="f">Network Path</label>
    <div class="row"><input id="pathIn" value="${esc(n.network_path)}" placeholder="${esc(NETWORK_PATH_PLACEHOLDER)}"></div>
    <label class="f">Tags</label>
    <div class="row"><input id="tagsIn" list="tagsList" value="${esc(n.tags || '')}" placeholder="comma, separated, tags"></div>
    <datalist id="tagsList">${tagSuggestions(n.id).map((t) => `<option value="${esc(t)}"></option>`).join('')}</datalist>
    <div class="row"><button id="nodeSave" class="primary small">Save</button><button data-act="delNode" class="danger small" style="margin-left:auto">Delete</button></div>
  </div>
  <div class="section">
    <h3>Datasets</h3>
    <div class="row">
      <button data-act="addDs" class="primary small">+ Dataset</button>
      <button data-act="templates" class="small">Add dataset from templates</button>
    </div>
    ${n.datasets.map(datasetCard).join('') || '<p class="muted">No datasets. Add one to start monitoring.</p>'}
  </div>`;
}

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
      <button class="small" data-act="run" data-ds="${d.id}">Run</button>
      <button class="small" data-act="edit" data-ds="${d.id}">Edit</button>
      <button class="small" data-act="metrics" data-ds="${d.id}">Metrics</button>
      <button class="small danger" data-act="delDs" data-ds="${d.id}">×</button>
    </div>
    ${readings ? `<div class="readings">${readings}</div>` : ''}
    ${d.last_error ? `<div class="err">last run: ${esc(d.last_error)}</div>` : ''}
    ${ts ? `<div class="kv"><span class="k">last run</span><span>${esc(ts)}</span></div>` : ''}
    ${d.credential ? `<div class="kv"><span class="k">credential</span><span>🔒 ${esc(d.credential.name)}</span></div>` : ''}
  </div>`;
}

function findDataset(id) {
  for (const n of state.snapshot.nodes) {
    const d = n.datasets.find((x) => x.id === id);
    if (d) return { node: n, ds: d };
  }
  return null;
}

// ---------------- events ----------------
async function onDockClick(e) {
  const rf = e.target.closest('#dockRefresh');
  if (rf) {
    rf.disabled = true;
    try { await refresh(); } catch (err) { toast(err.message, 'error'); }
    finally { rf.disabled = false; }
    return;
  }
  const tab = e.target.closest('[data-tab]');
  if (tab) { homeTab = tab.dataset.tab; render(); return; }
  const nav = e.target.closest('[data-nav]');
  if (nav) {
    const target = nav.dataset.nav;
    if (target === 'node') { state.select(nav.dataset.id); navigate({ view: 'node', nodeId: nav.dataset.id }); return; }
    if (target === 'dataset') { navigate({ view: 'dataset', dsId: Number(nav.dataset.ds), nodeId: state.selectedId }); return; }
    if (target === 'metrics') { navigate({ view: 'metrics', dsId: Number(nav.dataset.ds), nodeId: state.selectedId }); return; }
    navigate({ view: target, nodeId: state.selectedId });
    return;
  }
  const node = state.node(state.selectedId);
  if (e.target.id === 'nodeSave' && node) {
    await api.patchNode(node.id, {
      network_path: document.getElementById('pathIn').value,
      tags: document.getElementById('tagsIn').value,
    });
    await refresh();
    return;
  }
  const act = e.target.closest('[data-act]');
  if (!act) return;
  switch (act.dataset.act) {
    case 'addDs': navigate({ view: 'dataset', dsId: null, nodeId: state.selectedId }); break;
    case 'edit': navigate({ view: 'dataset', dsId: Number(act.dataset.ds), nodeId: state.selectedId }); break;
    case 'metrics': navigate({ view: 'metrics', dsId: Number(act.dataset.ds), nodeId: state.selectedId }); break;
    case 'templates': navigate({ view: 'templates', nodeId: state.selectedId }); break;
    case 'run': {
      act.disabled = true; act.textContent = '…';
      try { await api.runDataset(Number(act.dataset.ds)); } catch (err) { toast(err.message, 'error'); }
      await refresh(); break;
    }
    case 'delDs':
      if (confirm('Delete this dataset and its metrics?')) { await api.deleteDataset(Number(act.dataset.ds)); await refresh(); }
      break;
    case 'delNode':
      if (node && confirm(`Delete node "${node.label}" and all its datasets?`)) {
        window.__graph.removeNode(node.id);
        state.select(null);
        await refresh();
        navigate({ view: 'home' });
      }
      break;
  }
}

async function onDockChange(e) {
  // No dock-level change handlers remain: the node form's fields are saved
  // together via its explicit Save button (nodeSave), and dataset/metrics/rules
  // editors manage their own inputs locally.
}

export async function refresh() {
  state.setSnapshot(await api.snapshot());
  // Only the read-only node view re-renders on refresh; the home tab views and
  // the dataset/metrics/rules editors are live forms that manage their own
  // repaint, so re-rendering would clobber edits.
  if (route.view === 'node') render();
}
