// Network-path hyperlinks on graph node labels — post-render DOM patch.
//
// Read-only counterpart of admin/js/nodeLinks.js: when a node's network_path
// starts with http:// or https://, a transparent overlay rect is placed over
// the label text in the node header; clicking it opens the path in a new tab.
// Paths without an http(s) scheme are left completely untouched. The vendor
// library is never modified — same pattern as decorateNodes() in graph.js.
import { state } from './state.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const HEADER_H = 30; // matches the library's exported HEADER_H
const LABEL_X = 8;   // vendor #renderNode places .ng-label at x=8

let containerEl = null;
let linkObserver = null;

const isHttpUrl = (p) => /^https?:\/\//i.test((p || '').trim());
export const isPathLinkEvent = (e) => !!e.target?.closest?.('[data-role="path-link"]');

// Add/remove the overlay rect for one rendered node group. Idempotent.
function decorateGroup(g) {
  const id = g.getAttribute('data-node');
  const path = state.node(id)?.network_path?.trim() || '';
  let overlay = g.querySelector('[data-role="path-link"]');
  if (!isHttpUrl(path)) {
    if (overlay) overlay.remove();
    return;
  }
  if (!overlay) {
    overlay = document.createElementNS(SVG_NS, 'rect');
    overlay.setAttribute('class', 'ng-path-link');
    overlay.setAttribute('data-role', 'path-link');
    g.querySelector('.ng-header')?.appendChild(overlay);
    if (!overlay) return;
  }
  overlay.setAttribute('width', String(labelBoxWidth(g)));
  overlay.setAttribute('height', String(HEADER_H));
  overlay.setAttribute('y', '0');
  overlay.setAttribute('fill', 'transparent');
  overlay.setAttribute('pointer-events', 'all');
  const title = overlay.querySelector('title');
  if (title) title.textContent = path;
  else {
    const t = document.createElementNS(SVG_NS, 'title');
    t.textContent = path;
    overlay.appendChild(t);
  }
}

// Overlay width: the rendered label's bbox when available, otherwise the same
// canvas measurement the library uses for node sizing (labelWidth), clamped to
// the node so the overlay never covers the header buttons.
function labelBoxWidth(g) {
  const nodeW = Number(g.querySelector('.ng-node-bg')?.getAttribute('width')) || 180;
  try {
    const w = g.querySelector('.ng-label')?.getBBox?.().width;
    if (w && isFinite(w)) return Math.min(Math.max(w, 10), nodeW - LABEL_X - 36);
  } catch { /* no layout (hidden/detached) — fall through */ }
  const label = state.node(g.getAttribute('data-node'))?.label || '';
  return Math.min(Math.max(label.length * 8, 10), nodeW - LABEL_X - 36);
}

function decorateAll() {
  if (!containerEl) return;
  for (const g of containerEl.querySelectorAll('g.ng-node[data-node]')) decorateGroup(g);
}

// Wire decoration + click handling onto the graph container. Called once from
// initGraph(); safe even before the first loadGraph(). The dashboard's own
// read-only blockers in graph.js check isPathLinkEvent() and let these events
// through to the handlers below.
export function attachPathLinks(container) {
  containerEl = container;

  // Re-decorate after every re-render (the library rebuilds node DOM on each
  // render, which would drop our overlay). rAF-coalesced, and the observer is
  // disconnected while decorating so our own mutations don't re-trigger it.
  let queued = false;
  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      linkObserver.disconnect();
      decorateAll();
      linkObserver.observe(container, { childList: true, subtree: true });
    });
  };
  linkObserver = new MutationObserver(schedule);
  linkObserver.observe(container, { childList: true, subtree: true });

  // Capture-phase on the container. Stopping pointerdown keeps the library
  // from starting a select/drag gesture; stopping click dodges its svg-level
  // data-role click delegation.
  container.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !isPathLinkEvent(e)) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);
  container.addEventListener('click', (e) => {
    const hit = e.target.closest?.('[data-role="path-link"]');
    if (!hit) return;
    e.preventDefault();
    e.stopPropagation();
    const path = state.node(hit.closest('[data-node]')?.getAttribute('data-node'))?.network_path?.trim();
    if (isHttpUrl(path)) window.open(path, '_blank', 'noopener');
  }, true);

  decorateAll();
}
