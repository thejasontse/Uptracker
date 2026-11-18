// Small shared helpers for the dashboard bundle.

// Lightweight toast: non-blocking feedback for actions like node refresh.
// `kind` is 'error' or 'success' (default). Auto-dismisses after 3 s.
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
