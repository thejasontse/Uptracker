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
