// Column autocomplete for the admin query bar (bottom nodes table). Modeled on
// attachParamAutocomplete in params.js — same popup lifecycle and .param-ac CSS.
// Suggests the columns of the temp `nodes_wide` table (node fields + every metric
// name), the table name itself, and common SQL keywords. Metric names that aren't
// plain identifiers (spaces, punctuation) are inserted double-quoted so the query
// stays valid SQLite.
import { esc } from './util.js';

// The temp table user queries run against (see app/query.py).
export const QUERY_TABLE = 'nodes_wide';

// Common read-only SQL keywords + a few aggregates worth suggesting.
export const SQL_KEYWORDS = [
  'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'AS', 'DISTINCT',
  'ORDER BY', 'GROUP BY', 'HAVING', 'LIMIT', 'ASC', 'DESC',
  'LIKE', 'IN', 'IS NULL', 'IS NOT NULL', 'BETWEEN',
  'JOIN', 'ON', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
  'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'COALESCE',
];

// A column needs double-quoting in SQL when it isn't a bare identifier.
const needsQuote = (name) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name);

// Build the candidate list from the current columns + table name + keywords.
function candidates(getColumns) {
  const cols = (typeof getColumns === 'function' ? getColumns() : []) || [];
  return [
    ...cols.map((name) => ({ label: name, insert: needsQuote(name) ? `"${name}"` : name, kind: 'col' })),
    { label: QUERY_TABLE, insert: QUERY_TABLE, kind: 'table' },
    ...SQL_KEYWORDS.map((k) => ({ label: k, insert: k, kind: 'kw' })),
  ];
}

// Show a hint popup of matching columns/keywords while typing. Arrow keys navigate,
// Enter/Tab insert, Escape dismisses. Returns { isOpen } so the caller can suppress
// its own Enter handler (e.g. run-on-Enter) while the popup is visible.
export function attachQueryAutocomplete(field, getColumns) {
  let popup = null;
  let items = [];
  let active = 0;

  const close = () => { if (popup) { popup.remove(); popup = null; } items = []; active = 0; };

  // The identifier fragment immediately before the caret, or null when not inside one.
  // Handles two modes: a double-quoted run ("CPU Load") and a bare word (statu).
  function currentQuery() {
    const pos = field.selectionStart ?? field.value.length;
    const before = field.value.slice(0, pos);
    const lastQuote = before.lastIndexOf('"');
    // Odd number of quotes → we are inside an open quoted identifier.
    if (lastQuote !== -1 && (before.match(/"/g) || []).length % 2 === 1) {
      return { start: lastQuote + 1, query: before.slice(lastQuote + 1), quoted: true };
    }
    const m = /[A-Za-z_][A-Za-z0-9_]*$/.exec(before);
    if (!m) return null;
    return { start: m.index, query: m[0], quoted: false };
  }

  function paint() {
    if (!popup) {
      const rect = field.getBoundingClientRect();
      popup = document.createElement('div');
      popup.className = 'param-ac';
      popup.style.position = 'fixed';
      popup.style.left = (rect.left || 0) + 'px';
      // Anchor above the input: the query bar sits at the bottom of the viewport.
      popup.style.top = Math.max(4, (rect.top || 0) - 2 - Math.min(180, items.length * 26)) + 'px';
      popup.style.width = Math.max(rect.width || 240, 240) + 'px';
      document.body.appendChild(popup);
    }
    popup.innerHTML = items.map((it, i) => {
      const tag = it.kind === 'col' ? '' : `<span class="muted"> ${it.kind === 'table' ? 'table' : 'kw'}</span>`;
      return `<div class="param-ac-item${i === active ? ' active' : ''}" data-ac="${i}"><b>${esc(it.label)}</b>${tag}</div>`;
    }).join('');
    popup.querySelectorAll('[data-ac]').forEach((el) => {
      el.onmousedown = (e) => { e.preventDefault(); choose(Number(el.dataset.ac)); };
    });
  }

  function update() {
    const q = currentQuery();
    if (!q) { close(); return; }
    const needle = q.query.toLowerCase();
    const all = candidates(getColumns);
    const starts = [];
    const contains = [];
    for (const c of all) {
      const l = c.label.toLowerCase();
      if (l.startsWith(needle)) starts.push(c);
      else if (l.includes(needle)) contains.push(c);
    }
    items = [...starts, ...contains].slice(0, 40);
    if (!items.length) { close(); return; }
    active = Math.min(active, items.length - 1);
    paint();
  }

  function choose(i) {
    const q = currentQuery();
    if (!q || !items[i]) { close(); return; }
    const pos = field.selectionStart ?? field.value.length;
    const text = q.quoted ? items[i].label : items[i].insert;
    field.value = field.value.slice(0, q.start) + text + field.value.slice(pos);
    const caret = q.start + text.length;
    field.selectionStart = field.selectionEnd = caret;
    field.focus();
    close();
  }

  field.addEventListener('input', () => { active = 0; update(); });
  field.addEventListener('click', update);
  field.addEventListener('blur', () => setTimeout(close, 120));
  field.addEventListener('keydown', (e) => {
    if (!popup) return;
    // Consume navigation/selection keys so no other same-element keydown handler
    // (e.g. the bar's run-on-Enter) also reacts. stopImmediatePropagation stops
    // listeners attached after this one on the same field, regardless of order.
    if (e.key === 'ArrowDown') { e.preventDefault(); e.stopImmediatePropagation(); active = (active + 1) % items.length; paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopImmediatePropagation(); active = (active - 1 + items.length) % items.length; paint(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopImmediatePropagation(); choose(active); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); }
  });

  return { isOpen: () => !!popup };
}
