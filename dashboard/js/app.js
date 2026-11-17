// Dashboard entry: read-only graph canvas + bottom nodes table + polling.
// A read-only dock (js/dock.js) shows dataset last-run info for the selected
// node; no editing. Both the dock and the table collapse/expand, and the table
// can maximize to fill the screen — states persist in localStorage.
import { api } from './api.js';
import { state } from './state.js';
import { initGraph, loadGraph, paintStatus, onRefreshNode, getEffectiveSelectedNodeIds, onSelectionChanged } from './graph.js';
import { startPolling, refreshNow } from './poll.js';
import { initDock } from './dock.js';
import { toast } from './util.js';

const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Last saved-view query result ({ columns, rows }) or null. When set, the bottom
// table shows exactly what the view's SQL returned (its columns + rows), like admin.
let queryResults = null;
// SQL of the active saved view (null = "All nodes"). Re-run on every snapshot so a
// view stays live across polls rather than freezing its first result.
let activeViewSql = null;

// --- bottom table: one row per node, one column per metric name ---
// Statuses a rule can assign; anything else (unknown) is not rule-colored.
const RULE_STATUSES = new Set(['good', 'warning', 'critical']);

// Worst-of rank for merging duplicate metric names on one node (higher = worse;
// unknown/absent ranks as 0 so any real status wins).
function statusRank(name) {
  const lvl = state.snapshot.status_levels.find((l) => l.name === name);
  return lvl ? lvl.rank : 0;
}

// Map lowercased metric name -> best (worst-status) value across the node's datasets.
function metricValuesByNode(node) {
  const out = new Map();
  for (const ds of node.datasets || []) {
    for (const m of ds.metrics || []) {
      if (!m.value) continue;
      const key = String(m.name).toLowerCase();
      const prev = out.get(key);
      if (!prev || statusRank(m.value.status) > statusRank(prev.value.status)) out.set(key, m);
    }
  }
  return out;
}

// --- column sorting: click a header to cycle ascending → descending → unsorted ---
let sortCol = null; // 'label', 'status', or lowercased metric name
let sortDir = 0;    // 1 = asc, -1 = desc, 0 = default order (by label)

function initTableSort() {
  const table = document.getElementById('nodesTable');
  if (!table || table.dataset.sortInit) return;
  table.dataset.sortInit = '1';
  // Delegation on the persistent <thead> element (only its innerHTML is rebuilt).
  table.querySelector('thead').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-col]');
    if (!th) return;
    const col = th.dataset.col;
    if (col !== sortCol) { sortCol = col; sortDir = 1; }
    else sortDir = sortDir === 1 ? -1 : 0;
    renderTable();
  });
}

function renderTable() {
  const thead = document.querySelector('#nodesTable thead');
  const tbody = document.getElementById('nodesTbody');
  if (!thead || !tbody) return;
  if (queryResults) { renderQueryResults(thead, tbody); return; }
  let rows = [...state.snapshot.nodes].sort((a, b) => (a.label || '').localeCompare(b.label || ''));
  // A canvas selection narrows the default grid to the selected nodes/groups.
  const sel = getEffectiveSelectedNodeIds();
  if (sel.size) rows = rows.filter((n) => sel.has(n.id));
  setViewStatus(`${rows.length} row${rows.length === 1 ? '' : 's'}`);

  // Column set: every distinct metric name among the visible rows (case-insensitive,
  // first-seen casing wins), sorted alphabetically.
  const names = new Map();
  for (const n of rows) {
    for (const ds of n.datasets || []) {
      for (const m of ds.metrics || []) {
        const key = String(m.name).toLowerCase();
        if (!names.has(key)) names.set(key, m.name);
      }
    }
  }
  const cols = [...names.entries()].sort((a, b) => a[0].localeCompare(b[0]));

  const ind = (key) => (sortCol === key && sortDir ? (sortDir === 1 ? ' \u25B2' : ' \u25BC') : '');
  thead.innerHTML = `<tr><th data-col="label">Label${ind('label')}</th>` +
    `<th data-col="status">Status${ind('status')}</th>` +
    cols.map(([key, name]) => `<th data-col="${esc(key)}">${esc(name)}${ind(key)}</th>`).join('') + '</tr>';

  // Cache each row's metric values once (used for sorting and rendering).
  const valCache = new Map(rows.map((n) => [n, metricValuesByNode(n)]));

  // Active column sort: numeric when both values are numbers, text otherwise.
  // Missing values always sort last; ties break by label.
  if (sortDir && sortCol && (sortCol === 'label' || sortCol === 'status' || names.has(sortCol))) {
    const byLabel = (a, b) => (a.label || '').localeCompare(b.label || '');
    rows.sort((a, b) => {
      if (sortCol === 'label') return sortDir * byLabel(a, b);
      if (sortCol === 'status') {
        return sortDir * (statusRank(a.status?.name) - statusRank(b.status?.name)) || byLabel(a, b);
      }
      const va = valCache.get(a).get(sortCol), vb = valCache.get(b).get(sortCol);
      if (!va && !vb) return byLabel(a, b);
      if (!va) return 1;
      if (!vb) return -1;
      const na = va.value.num, nb = vb.value.num;
      const c = (na != null && nb != null)
        ? na - nb
        : String(va.value.txt ?? va.value.num ?? '')
            .localeCompare(String(vb.value.txt ?? vb.value.num ?? ''), undefined, { numeric: true });
      return sortDir * c || byLabel(a, b);
    });
  }

  if (!rows.length) {
    const msg = sel.size ? 'No selected nodes.' : 'No nodes in the database.';
    tbody.innerHTML = `<tr class="empty"><td colspan="${cols.length + 2}" class="muted">${msg}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((n) => {
    const vals = valCache.get(n);
    const cells = cols.map(([key]) => {
      const m = vals.get(key);
      if (!m) return '<td class="muted">N/A</td>';
      const v = m.value;
      const text = `${v.txt ?? v.num ?? ''}${v.txt == null && v.num == null ? '—' : ''}${esc(m.unit || '')}`;
      // Rule-assigned statuses color the value text; unknown keeps the default.
      return RULE_STATUSES.has(v.status)
        ? `<td style="color:${esc(v.status_color)}">${text}</td>`
        : `<td>${text}</td>`;
    }).join('');
    return '<tr>' +
      `<td>${esc(n.label)}</td>` +
      `<td><span class="dot" style="background:${esc(n.status.color)}"></span> ${esc(n.status.name)}</td>` + cells +
      '</tr>';
  }).join('');
}

// Render the bottom table straight from a saved-view query result: one column per
// selected expression, one row per returned record — exactly what was queried.
function renderQueryResults(thead, tbody) {
  const { columns, rows } = queryResults;
  setViewStatus(`${rows.length} row${rows.length === 1 ? '' : 's'}`);
  const ind = (key) => (sortCol === key && sortDir ? (sortDir === 1 ? ' \u25B2' : ' \u25BC') : '');
  thead.innerHTML = '<tr>' + columns.map((c) =>
    `<th data-col="${esc(c)}">${esc(c)}${ind(c)}</th>`).join('') + '</tr>';
  let out = rows;
  if (sortDir && sortCol && columns.includes(sortCol)) {
    out = [...rows].sort((a, b) => {
      const va = a[sortCol], vb = b[sortCol];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      const na = Number(va), nb = Number(vb);
      const c = (va !== '' && vb !== '' && !isNaN(na) && !isNaN(nb))
        ? na - nb
        : String(va).localeCompare(String(vb), undefined, { numeric: true });
      return sortDir * c;
    });
  }
  if (!out.length) {
    tbody.innerHTML = `<tr class="empty"><td colspan="${columns.length}" class="muted">No rows matched.</td></tr>`;
    return;
  }
  tbody.innerHTML = out.map((r) => '<tr>' + columns.map((c) =>
    `<td>${esc(r[c] ?? '')}</td>`).join('') + '</tr>').join('');
}

// --- views dropdown (saved queries filter the bottom table's rows) ---
function setViewStatus(text) {
  const el = document.getElementById('queryStatus');
  if (el) el.textContent = text || '';
}

async function applyView(id) {
  if (!id) {
    activeViewSql = null;
    queryResults = null;
    localStorage.removeItem('sm_view');
    renderTable();
    return;
  }
  try {
    const { saved_queries } = await api.listSavedQueries();
    const q = saved_queries.find((x) => String(x.id) === String(id));
    if (!q) { activeViewSql = null; queryResults = null; renderTable(); return; }
    activeViewSql = q.sql;
  } catch (e) {
    console.error('view query failed', e);
    setViewStatus('view failed');
    return;
  }
  localStorage.setItem('sm_view', String(id));
  await refreshActiveView();
}

// Re-run the active view's SQL and repaint the table. Called on selection and on
// every snapshot so a view reflects live data. No-op when no view is active.
let viewInFlight = false;
async function refreshActiveView() {
  if (!activeViewSql) return;
  if (viewInFlight) return;   // skip if a poll fires before the last finished
  viewInFlight = true;
  try {
    const res = await api.query(activeViewSql);
    queryResults = { columns: res.columns, rows: res.rows || [] };
    renderTable();            // reports the row count in the status span
  } catch (e) {
    console.error('view query failed', e);
    setViewStatus('view failed');
  } finally {
    viewInFlight = false;
  }
}

async function initViews() {
  const sel = document.getElementById('viewSelect');
  if (!sel) return;
  try {
    const { saved_queries } = await api.listSavedQueries();
    sel.innerHTML = '<option value="">All nodes</option>' +
      saved_queries.map((q) => `<option value="${q.id}">${esc(q.name)}</option>`).join('');
    const saved = localStorage.getItem('sm_view');
    if (saved && saved_queries.some((q) => String(q.id) === saved)) {
      sel.value = saved;
      await applyView(saved);
    }
  } catch (e) {
    console.error('list saved views failed', e);
  }
  sel.addEventListener('change', () => applyView(sel.value));
}

// --- dock / table collapse + table maximize (persisted in localStorage) ---
const DOCK_KEY = 'sm_dock_collapsed';
const TABLE_COLLAPSED_KEY = 'sm_table_collapsed';
const TABLE_MAX_KEY = 'sm_table_max';

function applyDockCollapsed(on) {
  const dock = document.getElementById('dock');
  const btn = document.getElementById('dockToggle');
  if (!dock || !btn) return;
  dock.classList.toggle('collapsed', on);
  btn.innerHTML = on ? '&rsaquo;' : '&lsaquo;';
  btn.title = on ? 'Expand panel' : 'Collapse panel';
  btn.setAttribute('aria-label', btn.title);
}

function applyTableCollapsed(on) {
  const wrap = document.getElementById('tableWrap');
  const btn = document.getElementById('tableToggle');
  if (!wrap || !btn) return;
  wrap.classList.toggle('collapsed', on);
  // Chevron points down when expanding (table below), up when collapsing.
  btn.innerHTML = on ? '&#x25B4;' : '&#x25BE;';
  btn.title = on ? 'Expand table' : 'Collapse table';
  btn.setAttribute('aria-label', btn.title);
}

function applyTableMax(on) {
  const wrap = document.getElementById('tableWrap');
  const mainEl = document.querySelector('main');
  const btn = document.getElementById('tableMax');
  if (!wrap || !mainEl || !btn) return;
  wrap.classList.toggle('maximized', on);
  mainEl.classList.toggle('table-max', on);
  btn.innerHTML = on ? '&#x2716;' : '&#x26F6;';
  btn.title = on ? 'Restore table size' : 'Maximize table';
  btn.setAttribute('aria-label', btn.title);
}

function initCollapse() {
  applyDockCollapsed(localStorage.getItem(DOCK_KEY) === '1');
  applyTableMax(localStorage.getItem(TABLE_MAX_KEY) === '1');
  // Collapsed and maximized are mutually exclusive; maximize wins if both set.
  const collapsed = localStorage.getItem(TABLE_COLLAPSED_KEY) === '1'
    && localStorage.getItem(TABLE_MAX_KEY) !== '1';
  applyTableCollapsed(collapsed);

  document.getElementById('dockToggle')?.addEventListener('click', () => {
    const on = !document.getElementById('dock').classList.contains('collapsed');
    localStorage.setItem(DOCK_KEY, on ? '1' : '0');
    applyDockCollapsed(on);
  });
  document.getElementById('tableToggle')?.addEventListener('click', () => {
    const wrap = document.getElementById('tableWrap');
    const on = !wrap.classList.contains('collapsed');
    if (on && wrap.classList.contains('maximized')) {
      localStorage.setItem(TABLE_MAX_KEY, '0');
      applyTableMax(false);
    }
    localStorage.setItem(TABLE_COLLAPSED_KEY, on ? '1' : '0');
    applyTableCollapsed(on);
  });
  document.getElementById('tableMax')?.addEventListener('click', () => {
    const wrap = document.getElementById('tableWrap');
    const on = !wrap.classList.contains('maximized');
    if (on && wrap.classList.contains('collapsed')) {
      localStorage.setItem(TABLE_COLLAPSED_KEY, '0');
      applyTableCollapsed(false);
    }
    localStorage.setItem(TABLE_MAX_KEY, on ? '1' : '0');
    applyTableMax(on);
  });
}

async function boot() {
  const snap = await api.snapshot();
  state.setSnapshot(snap);

  // Theme: a per-visitor override (localStorage) wins; otherwise the global
  // default from the server. Pre-set the container class before initGraph so
  // the page never flashes the wrong theme (the graph.js observer mirrors it
  // onto <html>).
  const canvas = document.getElementById('canvas');
  const savedTheme = localStorage.getItem('sm_theme');
  const dark = savedTheme ? savedTheme === 'dark' : snap.theme === 'dark';
  canvas.classList.toggle('dark', dark);

  const graph = initGraph(canvas);
  window.__graph = graph;
  loadGraph(snap.graph);

  // Keep the bottom table in sync with the canvas selection.
  onSelectionChanged(() => renderTable());

  // Node refresh glyph → collect all of the node's enabled datasets now
  // (in-process on the server, so it works even when the scheduler is off),
  // then pull a fresh snapshot so the status colours / table update promptly.
  onRefreshNode(async (nodeId) => {
    try {
      const res = await api.runNode(nodeId);
      if (!res || res.started === 0) {
        toast('No enabled datasets to refresh', 'error');
      } else {
        toast(`Refreshing ${res.started} dataset${res.started === 1 ? '' : 's'}…`);
      }
    } catch (e) {
      console.error(`refresh node ${nodeId} failed`, e);
      toast('Refresh failed', 'error');
    }
    try { await refreshNow(); } catch { /* poll will catch up */ }
  });

  initTableSort();
  initDock();
  initCollapse();
  startPolling();
  initViews();

  // Repaint node status colors + table whenever a snapshot arrives. An active view
  // re-runs its SQL (live rows); otherwise the default grid repaints from state.
  state.subscribe(() => {
    for (const n of state.snapshot.nodes) paintStatus(n.id, n.status.color);
    if (activeViewSql) { refreshActiveView(); return; }
    renderTable();
  });
  for (const n of snap.nodes) paintStatus(n.id, n.status.color);
  renderTable();
}

boot().catch((e) => {
  const el = document.getElementById('bootError');
  if (el) el.textContent = `Failed to start: ${e.message}`;
  console.error(e);
});
