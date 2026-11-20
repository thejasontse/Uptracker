// HTML-escape a value for safe interpolation into innerHTML templates.
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Statuses a rule may assign. Anything else (notably "unknown") is the fallback
// status and is never selectable in the rules editor. Shared by app.js and rules.js.
export const RULE_STATUSES = new Set(['good', 'warning', 'critical']);

// Lightweight toast: replaces alert() popups. `kind` is 'error' or 'success'
// (default). Auto-dismisses after 3 s. Anchored at the bottom of the page; when
// the dock panel is open, it floats over the dock, centered on its bottom edge.
export function toast(msg, kind = 'success') {
  let el = document.getElementById('sm-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'sm-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.hidden = false;
  // Reset any inline positioning from a previous toast, then re-anchor. The dock
  // is a fixed-width right panel; when it's open we center over it instead of the
  // whole viewport. Width stays compact (CSS-capped) so it never blocks content.
  el.style.left = el.style.transform = '';
  // Float just above the nodes-table footer (the bottom of the canvas/dock area)
  // rather than the viewport bottom, which the table occupies.
  const wrap = document.getElementById('tableWrap');
  if (wrap && !wrap.classList.contains('maximized')) {
    el.style.bottom = `${window.innerHeight - wrap.getBoundingClientRect().top + 12}px`;
  } else {
    el.style.bottom = '';
  }
  const dock = document.getElementById('dock');
  if (dock && !dock.classList.contains('collapsed')) {
    const r = dock.getBoundingClientRect();
    if (r.width > 0) {
      el.classList.add('toast-dock');
      el.style.left = `${r.left + r.width / 2}px`;
      el.style.transform = 'translateX(-50%)';
    }
  }
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3000);
}

// Hint shown under the Network Path input.
export const NETWORK_PATH_PLACEHOLDER = 'DNS name / IP / URL';

// Breadcrumb trail for the dock's drill-down views (Node › Dataset › Metric › …).
// items: [{ label, nav }] — `nav` is a string of data-nav/data-* attributes that
// onDockClick routes; omit it for a non-clickable crumb. The LAST item is the
// active page: rendered bold and non-interactive.
export function breadcrumb(items) {
  const parts = items.map((it, i) => {
    const sep = i > 0 ? '<span class="crumb-sep">›</span>' : '';
    const last = i === items.length - 1;
    if (last || !it.nav) return `${sep}<span class="crumb active">${esc(it.label)}</span>`;
    return `${sep}<button class="crumb" ${it.nav}>${esc(it.label)}</button>`;
  });
  return `<div class="crumbs">${parts.join('')}</div>`;
}

// Human-friendly timestamp for cards/lists: "Feb 3, 2026, 4:05 PM" in the
// viewer's local timezone. Returns '' for empty/invalid input so callers can
// fall back to a placeholder. `last_run_at` is stored as an ISO UTC string.
export function formatTimestamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: 'numeric', minute: '2-digit',
    }).format(d);
  } catch {
    return d.toLocaleString();
  }
}
