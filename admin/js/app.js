// App entry: wire the graph canvas, the right dock, and status repaints.
import { api } from './api.js';
import { state } from './state.js';
import { initGraph, loadGraph, paintStatus, setOnFlushed, flushNow } from './graph.js';
import { initPanel } from './panel.js';
import { attachQueryAutocomplete } from './query.js';
import { toast, esc, RULE_STATUSES } from './util.js';

// Last query-bar result ({ columns, rows }) or null. When set, the bottom table
// shows exactly what the query returned instead of the default node grid.
let queryResults = null;

// Column names available to the query bar (node fields + every metric name),
// refreshed on boot/refresh. Feeds the query input's autocomplete popup.
let queryColumns = [];

// --- bottom table: one row per node, one column per metric name ---

// Worst-of rank for merging duplicate metric names on one node (higher = worse;
// unknown/absent ranks as 0 so any real status wins).
function statusRank(name) {
  const lvl = state.level(name);
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

// Render the bottom table straight from a query result: one column per
// selected expression, one row per returned record — exactly what was queried.
function renderQueryResults(thead, tbody) {
  const { columns, rows } = queryResults;
  setHint(`${rows.length} row${rows.length === 1 ? '' : 's'}`);
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
  // A selected node narrows the table further (the dock shows that node's detail).
  if (state.selectedId) rows = rows.filter((n) => n.id === state.selectedId);
  setHint(`${rows.length} row${rows.length === 1 ? '' : 's'}`);

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
    const msg = state.selectedId
      ? 'No nodes match the filter.'
      : 'No nodes in the database.';
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

// --- query bar (filters the bottom table by a read-only SQL SELECT) ---
function setHint(text) {
  const el = document.getElementById('queryStatus');
  if (el) el.textContent = text || '';
}

async function refreshSavedQueries(selectId) {
  const sel = document.getElementById('querySaved');
  if (!sel) return;
  try {
    const { saved_queries } = await api.listSavedQueries();
    sel.innerHTML = '<option value="">Saved queries…</option>' +
      saved_queries.map((q) => `<option value="${q.id}">${esc(q.name)}</option>`).join('');
    if (selectId != null) sel.value = String(selectId);
  } catch (e) {
    console.error('list saved queries failed', e);
  }
}

async function runQuery(sql) {
  sql = (sql || '').trim();
  if (!sql) { clearQuery(); return; }
  try {
    const res = await api.query(sql);
    queryResults = { columns: res.columns, rows: res.rows || [] };
    renderTable();   // reports the row count in the status span
  } catch (e) {
    toast(`Query failed: ${e.message}`, 'error');
    setHint('Query failed — the table still shows the previous rows.');
  }
}

function clearQuery() {
  queryResults = null;
  const input = document.getElementById('queryInput');
  if (input) input.value = '';
  const sel = document.getElementById('querySaved');
  if (sel) sel.value = '';
  renderTable();
  showColumnsHint();
}

async function showColumnsHint() {
  try {
    const { columns } = await api.queryColumns();
    queryColumns = columns || [];
  } catch { /* hint only */ }
}

function initQueryBar() {
  const input = document.getElementById('queryInput');
  // Column/keyword autocomplete. Keep the handle so Enter-to-run can stand down
  // while the suggestion popup is open (otherwise Enter both inserts and runs).
  const ac = attachQueryAutocomplete(input, () => queryColumns);
  document.getElementById('queryRun').addEventListener('click', () => runQuery(document.getElementById('queryInput').value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { if (ac.isOpen()) return; runQuery(e.target.value); }
  });
  document.getElementById('queryClear').addEventListener('click', clearQuery);
  document.getElementById('querySave').addEventListener('click', async () => {
    const sql = document.getElementById('queryInput').value.trim();
    if (!sql) { toast('Nothing to save — write a query first.', 'error'); return; }
    const name = prompt('Save this query as:');
    if (!name || !name.trim()) return;
    try {
      const saved = await api.saveQuery(name.trim(), sql);
      await refreshSavedQueries(saved.id);
      toast(`Saved "${saved.name}".`);
    } catch (e) {
      toast(`Save failed: ${e.message}`, 'error');
    }
  });
  document.getElementById('querySaved').addEventListener('change', (e) => {
    const sel = e.target;
    const id = sel.value;
    const name = sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : '';
    if (!id) { clearQuery(); return; }
    setHint(`Loading "${name}"…`);
    // The SQL itself is not in the option text; fetch the list (which includes sql).
    api.listSavedQueries().then(({ saved_queries }) => {
      const q = saved_queries.find((x) => String(x.id) === id);
      if (!q) return;
      document.getElementById('queryInput').value = q.sql;
      runQuery(q.sql);
    }).catch((err) => toast(`Load failed: ${err.message}`, 'error'));
  });
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
  btn.innerHTML = on ? '&laquo;' : '&raquo;';
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
  state.snapshot._network_resources = (await api.networkResources()).network_resources;

  // Apply the global theme default on load (pre-set the container class before
  // initGraph so the page never flashes the wrong theme; the graph.js observer
  // mirrors it onto <html>).
  const canvas = document.getElementById('canvas');
  canvas.classList.toggle('dark', snap.theme === 'dark');

  const graph = initGraph(canvas, (id) => state.select(id));
  window.__graph = graph;
  loadGraph(snap.graph);

  // After a layout save the DB node rows may have changed (added/pruned) —
  // refresh state so the table and panel stay current.
  setOnFlushed(async () => {
    state.setSnapshot(await api.snapshot());
    renderTable();
  });

  initPanel();
  initQueryBar();
  initTableSort();
  initCollapse();
  refreshSavedQueries();
  showColumnsHint();

  // Repaint node status colors + table whenever a snapshot arrives (the dock's
  // Refresh button and every mutation drive panel.refresh(); the admin side never
  // auto-polls, so in-progress edits are never clobbered). Selection changes only
  // re-filter the table (the metric columns depend on which nodes are shown).
  state.subscribe((s, why) => {
    if (why === 'select') { renderTable(); return; }
    for (const n of s.nodes) paintStatus(n.id, n.status.color);
    renderTable();
  });
  for (const n of snap.nodes) paintStatus(n.id, n.status.color);
  renderTable();

  document.getElementById('dockBody').dataset.ready = '1';
}

boot().catch((e) => {
  document.getElementById('dockBody').innerHTML = `<p class="err pad">Failed to start: ${e.message}</p>`;
  console.error(e);
});

// Exposed so the panel can ensure a just-added node exists as a DB row before
// PATCHing it (e.g. enabling tracking on a freshly dropped node).
window.__flushGraph = flushNow;
