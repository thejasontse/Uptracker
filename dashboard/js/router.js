// Tiny hash router for the dashboard — two views, no dependencies.
//
//   #/graph  canvas + dock (table collapsed)   [default]
//   #/table  nodes table maximized (canvas hidden)
//
// Hash-based so it survives the port-forwarding proxy and the <base>-tag URL
// anchoring in index.html, needs no server support, and gives back/forward
// for free. The route is applied by toggling the same classes app.js's
// collapse/maximize helpers already manage; localStorage keeps remembering
// the last view across reloads.
export const ROUTES = ['graph', 'table'];
const KEY = 'sm_view_route';

let current = 'graph';
const listeners = []; // (route) => void

export function currentRoute() { return current; }

function parse(hash) {
  const m = /^#\/(graph|table)(?:[/?].*)?$/.exec(hash || '');
  return m ? m[1] : null;
}

// Apply a route: normalize the hash, mark <body> for CSS, notify subscribers.
function apply(route, writeHash) {
  current = ROUTES.includes(route) ? route : 'graph';
  if (writeHash && parse(location.hash) !== current) {
    location.replace(`${location.pathname}#/` + current);
  }
  localStorage.setItem(KEY, current);
  document.body.dataset.view = current;
  for (const cb of listeners) cb(current);
}

// Start routing. Returns the initial route. `initial` overrides the saved/
// hash route on first boot (app.js passes nothing: hash wins, then storage).
export function start(initial) {
  const fromHash = parse(location.hash);
  const fromStore = localStorage.getItem(KEY);
  apply(fromHash || (ROUTES.includes(initial) ? initial : null) || fromStore || 'graph', true);
  window.addEventListener('hashchange', () => apply(parse(location.hash) || current, false));
  return current;
}

// Programmatic navigation (view switch buttons). Same route = no-op.
export function navigate(route) {
  if (!ROUTES.includes(route)) return;
  if (parse(location.hash) === route) { apply(route, true); return; }
  location.hash = `#/${route}`; // hashchange fires apply()
}

// Subscribe to route changes; callback receives the new route.
export function onRoute(cb) {
  if (!listeners.includes(cb)) listeners.push(cb);
}
