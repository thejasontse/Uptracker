// Central app state with a tiny pub/sub. The snapshot from the server is the
// source of truth; UI modules subscribe to changes and read what they need.
let snapshot = { graph: { nodes: [], edges: [], groups: [] }, nodes: [], status_levels: [], poll_interval_s: 15 };
let selectedId = null;
const subs = new Set();

export const state = {
  get snapshot() { return snapshot; },
  get selectedId() { return selectedId; },
  setSnapshot(s) { snapshot = s; subs.forEach((f) => f(snapshot)); },
  select(id) { if (id !== selectedId) { selectedId = id; subs.forEach((f) => f(snapshot, 'select')); } },
  subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  node(id) { return snapshot.nodes.find((n) => n.id === id); },
  level(name) { return snapshot.status_levels.find((l) => l.name === name); },
};
