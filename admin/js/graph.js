// nodegraph integration. Owns the editor instance, syncs layout to the server,
// paints status colors, mirrors the dark theme, and reports node selection.
//
// Persistence: the library exposes no change event, and some actions (e.g.
// right-click "Add node here") mutate the model internally without going
// through the public API. So instead of wrapping mutators we observe the DOM
// the renderer rebuilds and persist whenever the serialized model actually
// differs from the last save. Status colors are stripped from the serialized
// model before diffing/saving — they are derived from monitoring state, not
// part of the layout, so painting never triggers a save.
import { createGraph } from '../vendor/nodegraph.js';
import { api } from './api.js';

let graph = null;
let dirtyTimer = null;
let lastSaved = '';
let onFlushedCb = null;
const statusColors = new Map(); // nodeId -> color currently applied

// The layout as we persist it: exportJSON minus node status colors.
function canonical() {
  const m = JSON.parse(graph.exportJSON());
  for (const n of m.nodes) delete n.color;
  return JSON.stringify(m);
}

function scheduleFlush() {
  clearTimeout(dirtyTimer);
  dirtyTimer = setTimeout(flushNow, 600);
}

// Persist the layout immediately if it changed. Exported so the panel can
// ensure a just-added node exists as a DB row before PATCHing it.
export async function flushNow() {
  clearTimeout(dirtyTimer);
  if (!graph) return;
  const json = canonical();
  if (json === lastSaved) return;
  try {
    await api.putGraph(JSON.parse(json));
    lastSaved = json;
    if (onFlushedCb) await onFlushedCb();
  } catch (e) { console.error('graph save failed', e); }
}

// Called after a successful layout save (to refresh nodes/table).
export function setOnFlushed(cb) { onFlushedCb = cb; }

export function initGraph(container, onSelect) {
  graph = createGraph(container);

  // Persist whenever the rendered model changed (see note above).
  new MutationObserver(scheduleFlush).observe(container, { childList: true, subtree: true });

  // Mirror the graph's dark theme onto <html> so the whole page follows
  // (the library toggles `dark` on the container from its context menu), and
  // persist the choice to the global `theme` setting so it survives a reload.
  // `lastTheme` is seeded with the current state so the initial sync (and the
  // one-time `ng-graph` class add) never writes a redundant value.
  let lastTheme = container.classList.contains('dark') ? 'dark' : 'light';
  const syncTheme = () => {
    const dark = container.classList.contains('dark');
    document.documentElement.classList.toggle('dark', dark);
    const theme = dark ? 'dark' : 'light';
    if (theme !== lastTheme) {
      lastTheme = theme;
      api.putSettings({ theme }).catch((e) => console.error('theme save failed', e));
    }
  };
  new MutationObserver(syncTheme).observe(container, { attributes: true, attributeFilter: ['class'] });
  syncTheme();

  // Selection: capture-phase pointerdown fires BEFORE the library's own
  // pointerdown handler (which may re-render and detach the pressed element,
  // so by `click` time e.target is the SVG root and closest('[data-node]')
  // is null). Read the node here while the DOM is intact.
  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;              // right button: context menu; middle: pan
    if (e.target.closest('.ng-menu')) return; // clicking a context-menu item keeps selection
    const nodeEl = e.target.closest('[data-node]');
    onSelect(nodeEl ? nodeEl.getAttribute('data-node') : null);
  }, true);

  return graph;
}

export function loadGraph(model) {
  graph.importJSON(JSON.stringify(model));
  lastSaved = canonical();
}

// Paint a node's status color. Stripped from the persisted model, so this
// never marks the layout dirty.
export function paintStatus(nodeId, color) {
  if (statusColors.get(nodeId) === color) return;
  statusColors.set(nodeId, color);
  graph.setNodeColor(nodeId, color);
}
