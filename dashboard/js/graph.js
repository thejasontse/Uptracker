// nodegraph integration — READ-ONLY variant.
//
// Differences from the admin bundle:
//   * No layout persistence: the admin version observes the rendered DOM and
//     PUTs the serialized model back whenever it changes. Here there is no
//     MutationObserver→flush→putGraph persistence, so the server's layout is
//     never written. (A separate observer only re-decorates the nodes — see
//     decorateNodes().)
//   * Edit gestures are swallowed before the library sees them: left-button
//     pointerdown (select/drag/resize/connect), contextmenu (edit menu),
//     dblclick (label edit / add node), and Delete/Backspace/undo shortcuts.
//     Middle-button pan and wheel zoom still work — they are navigation, not
//     editing.
//   * The library's per-node editor glyphs (+ add param, × delete node,
//     − delete param) are stripped after every render and replaced with a
//     single refresh glyph (⟳) that queues collection of the node's datasets.
//     The vendor library is never modified — this is a post-render DOM patch.
import { createGraph } from '../vendor/nodegraph.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const HEADER_H = 30; // matches the library's exported HEADER_H

let graph = null;
let containerEl = null;
let refreshCb = null; // (nodeId) => void | Promise — set via onRefreshNode()
let decorateObserver = null;
const statusColors = new Map(); // nodeId -> color currently applied

// Selection + view-menu state.
let suppressDrag = false; // true while a node/group press is in flight (drag suppressed)
const selectionSubscribers = []; // callbacks set via onSelectionChanged()
let selectionRaf = null; // rAF debounce handle for the selection observer
let viewMenuEl = null; // the open view-menu element (or null)
let viewMenuDismiss = null; // cleanup fn for the view-menu's document listeners
let lastEffectiveSelection = new Set(); // last effective selection (change detection)
let lastSelectionKeyStr = ''; // stringified last reported selection (flush comparison)

// Replace the library's editor glyphs on every node with a refresh button.
// Idempotent: safe to call after any re-render (status repaints rebuild the
// node DOM, which would otherwise bring the +/×/− glyphs back).
function decorateNodes() {
  if (!containerEl) return;
  for (const g of containerEl.querySelectorAll('g.ng-node')) {
    const header = g.querySelector('.ng-header');
    if (!header) continue;
    // Strip the editor-only glyphs (add param, delete node, delete param).
    g.querySelectorAll('text[data-role="add-param"], text[data-role="delete-node"], text[data-role="delete-param"]')
      .forEach((t) => t.remove());
    if (header.querySelector('text[data-role="refresh-node"]')) continue;
    const w = Number(g.querySelector('.ng-node-bg')?.getAttribute('width')) || 160;
    const t = document.createElementNS(SVG_NS, 'text');
    t.setAttribute('class', 'ng-btn ng-btn-refresh');
    t.setAttribute('data-role', 'refresh-node');
    t.setAttribute('x', String(w - 14));
    t.setAttribute('y', String(HEADER_H / 2 + 5));
    t.setAttribute('text-anchor', 'middle');
    t.textContent = '\u27F3'; // ⟳ clockwise gapped circle arrow
    header.appendChild(t);
  }
}

export function initGraph(container) {
  graph = createGraph(container);
  containerEl = container;

  // Mirror the graph's dark theme onto <html> so the whole page follows
  // (the library toggles `dark` on the container from its context menu).
  const syncTheme = () =>
    document.documentElement.classList.toggle('dark', container.classList.contains('dark'));
  new MutationObserver(syncTheme).observe(container, { attributes: true, attributeFilter: ['class'] });
  syncTheme();

  // Re-decorate after every re-render. The library rebuilds the node DOM on
  // each render (e.g. when paintStatus changes a node's colour), which would
  // restore the +/×/− glyphs and drop ours. Disconnect while decorating so our
  // own mutations don't re-trigger the callback.
  decorateObserver = new MutationObserver(() => {
    decorateObserver.disconnect();
    decorateNodes();
    decorateObserver.observe(container, { childList: true, subtree: true });
  });
  decorateObserver.observe(container, { childList: true, subtree: true });

  // Fire onSelectionChanged when the effective selection changes. Two triggers:
  //   1. A capture-phase pointerdown on the container (same trick as the admin
  //      view). It runs BEFORE the library's own pointerdown handler, which
  //      calls render() and detaches the pressed element — by the time any
  //      later listener (or a MutationObserver) sees the new ng-selected
  //      classes, the browser may already have painted the stale table. We
  //      annotate the click with the selection it is about to produce (a node
  //      adds that node id; a group adds all of its member ids, nested groups
  //      included — plain and Shift+click both covered) and fire the callback
  //      in a setTimeout(0), i.e. right after the library's handler finishes,
  //      still within the same task boundary as the click.
  //   2. A rAF-coalesced poll of the rendered selection as a catch-all for
  //      everything else: marquee drags, Escape / background clears, undo/redo
  //      and any programmatic change. One comparison per frame instead of one
  //      MutationObserver callback per node per frame (the previous observer
  //      fired hundreds of times per drag on wide graphs).
  const emitSelectionChange = () => {
    lastEffectiveSelection = getEffectiveSelectedNodeIds();
    for (const cb of selectionSubscribers) cb();
  };

  // The selection the next flush should report, precomputed from the click
  // annotation. null = nothing predicted; the flush compares against the
  // live DOM instead.
  let pendingKey = null;

  const selectionKey = () => Array.from(getEffectiveSelectedNodeIds()).sort().join(',');

  const flushSelectionChange = () => {
    if (pendingKey !== null) {
      const key = pendingKey;
      pendingKey = null;
      if (key !== lastSelectionKeyStr) {
        lastSelectionKeyStr = key;
        emitSelectionChange();
      }
      return;
    }
    const key = selectionKey();
    if (key === lastSelectionKeyStr) return;
    lastSelectionKeyStr = key;
    emitSelectionChange();
  };

  // Capture-phase: runs before the library's pointerdown listener on the SVG.
  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const nodeEl = e.target.closest && e.target.closest('[data-node]');
    if (nodeEl && !e.shiftKey) {
      pendingKey = nodeEl.getAttribute('data-node');
      setTimeout(flushSelectionChange, 0);
      return;
    }
    const groupEl = e.target.closest && e.target.closest('[data-group]');
    if (groupEl && !e.shiftKey) {
      const gid = groupEl.getAttribute('data-group');
      // Match getEffectiveSelectedNodeIds(): a group selection surfaces its
      // direct geometric members.
      pendingKey = Array.from(graph.groupMembers(gid)).sort().join(',');
      setTimeout(flushSelectionChange, 0);
      return;
    }
    // marquee / background / shift-click: can't predict cheaply — let the
    // rAF poll notice the change on the next frame.
  }, true);

  // rAF catch-all poll: notices marquee / Escape / programmatic changes.
  const pollSelection = () => {
    selectionRaf = null;
    flushSelectionChange();
    scheduleSelectionPoll();
  };
  const scheduleSelectionPoll = () => {
    if (!selectionRaf) selectionRaf = requestAnimationFrame(pollSelection);
  };
  scheduleSelectionPoll();

  // Block editing. Capture-phase handlers on the container run before the
  // library's own listeners on the SVG it renders inside.
  const block = (e) => { e.preventDefault(); e.stopPropagation(); };
  const isRefresh = (e) => e.target.closest && e.target.closest('[data-role="refresh-node"]');
  container.addEventListener('contextmenu', openViewMenu, true);
  container.addEventListener('dblclick', block, true);
  // The library also mutates via `click` on its node header/param buttons
  // (× delete, + add param) — a click is dispatched independently of
  // pointerdown, so stopPropagation on pointerdown alone doesn't stop it.
  // Block all clicks EXCEPT our refresh glyph: for that, stop propagation so
  // the library's svg-level click delegation never sees it, then run the
  // registered callback (with a brief spin for feedback).
  container.addEventListener('click', (e) => {
    if (e.target.closest('.ng-menu')) return; // view-menu items handle their own clicks
    const btn = isRefresh(e);
    if (!btn) { block(e); return; }
    e.preventDefault();
    e.stopPropagation();
    const nodeId = btn.closest('[data-node]')?.dataset.node;
    if (!nodeId || !refreshCb) return;
    btn.classList.add('ng-btn-spin');
    // Keep the spin visible for a beat even though the queue call is fast.
    const minSpin = new Promise((r) => setTimeout(r, 700));
    Promise.all([Promise.resolve(refreshCb(nodeId)), minSpin])
      .finally(() => btn.classList.remove('ng-btn-spin'));
  }, true);
  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return; // middle (1) = pan, right (2) handled by contextmenu
    suppressDrag = false; // reset at the start of every gesture
    if (isRefresh(e)) { e.stopPropagation(); return; } // don't drag/select, but keep default so click fires
    // Editing elements: block so the library never starts a connect / resize /
    // reorder / segment / label drag. Param rows are NOT blocked: blocking them
    // made presses on the (large) row area fail to select the node at all.
    // Instead the press falls into the node-body branch below — suppressDrag
    // kills the library's pointermove before its 3px threshold can promote a
    // maybe-paramrow gesture into a real reorder, and the library's pointerup
    // then selects the node (Shift toggles).
    if (e.target.closest('[data-port], [data-resize], [data-resize-group], [data-edgelabel], [data-seg], [data-gport]')) {
      block(e);
      return;
    }
    // Node or group body: let the library select it, but flag the gesture so
    // the drag is suppressed below (a read-only dashboard never moves things).
    if (e.target.closest('[data-node], [data-group]')) {
      suppressDrag = true;
      return;
    }
    // Edge / empty canvas: let through (edge-select / marquee).
  }, true);
  // Suppress the library's window-level pointermove while a node/group press is
  // in flight. Capture phase on document fires before the target and before the
  // window bubble listener, so stopImmediatePropagation() keeps the node still.
  document.addEventListener('pointermove', (e) => {
    if (suppressDrag) e.stopImmediatePropagation();
  }, true);
  const endSuppress = () => { suppressDrag = false; };
  document.addEventListener('pointerup', endSuppress, true);
  document.addEventListener('pointercancel', endSuppress, true);
  // The library binds its Delete/Backspace/undo shortcuts on window; block them
  // outright — the dashboard has no text inputs that need them.
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Delete' || e.key === 'Backspace') block(e);
    if ((e.ctrlKey || e.metaKey) && ['z', 'y', 'Z', 'Y', 'a', 'A'].includes(e.key)) block(e);
    // Copy / paste are editing gestures too — the dashboard is read-only.
    if ((e.ctrlKey || e.metaKey) && ['c', 'v', 'C', 'V', 'x', 'X'].includes(e.key)) block(e);
  }, true);

  return graph;
}

// --- theme ---
export function setDark(on) { graph.setDark(on); }
export function isDark() { return containerEl ? containerEl.classList.contains('dark') : false; }

// --- selection (read from the rendered ng-selected classes) ---
export function getSelectedNodeIds() {
  const s = new Set();
  if (containerEl) for (const g of containerEl.querySelectorAll('g.ng-node.ng-selected')) s.add(g.getAttribute('data-node'));
  return s;
}
export function getSelectedGroupIds() {
  const s = new Set();
  if (containerEl) for (const g of containerEl.querySelectorAll('g.ng-group.ng-selected')) s.add(g.getAttribute('data-group'));
  return s;
}
// Selected nodes plus every node inside a selected group (group membership is
// geometric, so a group selection surfaces its members).
export function getEffectiveSelectedNodeIds() {
  const ids = new Set(getSelectedNodeIds());
  const groups = getSelectedGroupIds();
  if (groups.size && graph) for (const gid of groups) for (const id of graph.groupMembers(gid)) ids.add(id);
  return ids;
}
// Register a callback fired whenever the effective selection changes. Multiple
// modules subscribe (app.js renders the table, dock.js renders the panel) —
// this is a list, not a single slot; last-writer-wins here once silently
// dropped the table's subscriber and the grid only updated on the 15s poll.
export function onSelectionChanged(cb) {
  if (!selectionSubscribers.includes(cb)) selectionSubscribers.push(cb);
}

// --- selection-aware PDF export ---
export function exportPdf() {
  const ids = getEffectiveSelectedNodeIds();
  try {
    const bytes = graph.exportPDF(ids.size ? [...ids] : null);
    savePdf(bytes);
  } catch (err) {
    console.error('PDF export failed', err);
  }
}
// The library's saveFile is private; replicate its pattern (File System Access
// API with a download-link fallback). A cancelled picker stays silent.
async function savePdf(bytes) {
  const name = 'graph.pdf';
  const mime = 'application/pdf';
  try {
    if (window.showSaveFilePicker) {
      const handle = await window.showSaveFilePicker({
        suggestedName: name,
        types: [{ description: 'PDF', accept: { [mime]: ['.pdf'] } }],
      });
      const writable = await handle.createWritable();
      await writable.write(bytes);
      await writable.close();
      return;
    }
    const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    if (err && err.name === 'AbortError') return; // user cancelled
    console.error('PDF export failed', err);
  }
}

// --- filtered right-click view menu (theme / fit / reset / export PDF) ---
function closeViewMenu() {
  if (viewMenuDismiss) { viewMenuDismiss(); viewMenuDismiss = null; }
  if (viewMenuEl) { viewMenuEl.remove(); viewMenuEl = null; }
}
function openViewMenu(e) {
  e.preventDefault();
  e.stopPropagation();
  closeViewMenu();
  const cr = containerEl.getBoundingClientRect();
  const el = document.createElement('div');
  el.className = 'ng-menu';
  const item = (label, action) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('click', () => { closeViewMenu(); action(); });
    el.appendChild(b);
  };
  item(isDark() ? 'Light theme' : 'Dark theme', () => {
    const dark = !isDark();
    setDark(dark);
    localStorage.setItem('sm_theme', dark ? 'dark' : 'light');
  });
  item('Fit view', () => fit());
  item('Reset view', () => graph.resetView());
  item('Export PDF', () => exportPdf());
  containerEl.appendChild(el);
  el.style.left = `${Math.max(0, Math.min(e.clientX - cr.left, cr.width - el.offsetWidth - 4))}px`;
  el.style.top = `${Math.max(0, Math.min(e.clientY - cr.top, cr.height - el.offsetHeight - 4))}px`;
  viewMenuEl = el;
  const onDown = (ev) => { if (!el.contains(ev.target)) closeViewMenu(); };
  // Escape closes the menu only (stopPropagation keeps the library's window
  // handler from also clearing the selection).
  const onKey = (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); closeViewMenu(); } };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  viewMenuDismiss = () => {
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
  };
}

// Register the handler invoked when a node's refresh glyph is clicked.
export function onRefreshNode(cb) { refreshCb = cb; }

export function loadGraph(model) {
  graph.importJSON(JSON.stringify(model));
  decorateNodes(); // decorate immediately (the observer also catches this)
}

// Paint a node's status color (derived from monitoring state, never persisted).
export function paintStatus(nodeId, color) {
  if (statusColors.get(nodeId) === color) return;
  statusColors.set(nodeId, color);
  graph.setNodeColor(nodeId, color);
}

export function fit() { graph.fitView(); }
export function getGraph() { return graph; }
