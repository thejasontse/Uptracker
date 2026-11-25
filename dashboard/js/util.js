// Small shared helpers for the dashboard bundle.

// Lightweight toast: non-blocking feedback for actions like node refresh.
// `kind` is 'error' or 'success' (default). Auto-dismisses after 3 s. Anchored at
// the bottom of the page; when the dock panel is open, it floats over the dock,
// centered on its bottom edge.
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
  // Reset any inline positioning from a previous toast, then re-anchor over the
  // dock when it's open (inline styles override the stylesheet defaults). Width
  // stays compact (CSS-capped) so it never blocks content.
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

// How a dataset's schedule reads on cards: on-demand datasets (interval 0 /
// mode "none") never run from the timer. Mirrors admin/js/editor.js.
export function scheduleLabel(ds) {
  if (!ds.interval_s || ds.schedule_mode === 'none') return 'on demand';
  const n = ds.interval_s;
  const unit = ds.interval_unit || 'seconds';
  const mult = { seconds: 1, minutes: 60, hours: 3600, days: 86400 }[unit] || 1;
  if (unit === 'seconds' || n % mult !== 0) return `every ${n}s`;
  const count = n / mult;
  // `unit` is stored plural ("minutes", "hours", "days"); singularize for 1.
  return `every ${count} ${count === 1 && unit.endsWith('s') ? unit.slice(0, -1) : unit}`;
}
