// HTML-escape a value for safe interpolation into innerHTML templates.
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Statuses a rule may assign. Anything else (notably "unknown") is the fallback
// status and is never selectable in the rules editor. Shared by app.js and rules.js.
export const RULE_STATUSES = new Set(['good', 'warning', 'critical']);

// Lightweight toast: replaces alert() popups. `kind` is 'error' or 'success'
// (default). Auto-dismisses after 3 s.
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
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3000);
}

// Network Resource types (mirrors BASE_NETWORK_RESOURCES in app/main.py). Unknown
// resource types (e.g. from a dataset template) fall back to their raw value.
export const NETWORK_RESOURCE_LABELS = { server: 'Server', dns: 'DNS', shared_folder: 'Shared Network Folder', web: 'Web' };
export const networkResourceLabel = (k) => NETWORK_RESOURCE_LABELS[k] || k;

// Hint shown under the Network-Path Reference input, per resource type.
export const NETWORK_PATH_HINTS = {
  server: 'DNS.domain / IP Address',
  dns: 'DNS.domain',
  shared_folder: '//path/to/shared/network/folder',
  web: 'https://host/path',
};

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
