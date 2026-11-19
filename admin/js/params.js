// Shared [Square Bracket] parameter helpers (mirrors app/templates.py). Used by
// the dataset, metrics, and rules dock pages so a [Name] token behaves the same
// wherever it appears.
import { esc } from './util.js';

// Name must start with a letter; word-ish chars only (spaces/hyphens allowed),
// so `[ -f x ]`, `[[ ]]`, `[1]`, `[id,name]` are never treated as parameters.
export const PARAM_RE_SRC = String.raw`\[(?=[A-Za-z])([A-Za-z0-9 _.\-]+)\]`;

// Known auto-fillable names (lower-cased). Mirrors app/templates.py. The raw
// DB field names are intentionally NOT parameters — only label/id and the
// friendly UI labels (network-path reference / network resource) are.
export const KNOWN_PARAM_KEYS = new Set(['label', 'id', 'network-path reference', 'network resource']);

// Intentional parameters are Title Case ([Host]), multi-word ([Network-Path
// Reference]), or a known node field. Lower-case single words like [a-z] or
// [0-9] stay literal — keeps regex character classes out of the param fields.
export function isParamName(name) {
  return KNOWN_PARAM_KEYS.has(name.toLowerCase()) || /^[A-Z]/.test(name) || /\s/.test(name);
}

// Autocomplete candidates shown after typing `[`.
export function paramHints() {
  return [
    { name: 'label', hint: 'node label' },
    { name: 'id', hint: 'node id' },
    { name: 'Network-Path Reference', hint: "node's network-path reference" },
    { name: 'Network Resource', hint: "node's network resource" },
  ];
}

export function scanParams(texts) {
  const re = new RegExp(PARAM_RE_SRC, 'g');
  const seen = [];
  for (const t of texts) {
    if (typeof t !== 'string') continue;
    let m;
    while ((m = re.exec(t))) {
      const name = m[1].trim();
      if (name && isParamName(name) && !seen.includes(name)) seen.push(name);
    }
  }
  return seen;
}

// The friendly labels used on the node form, so [Network-Path Reference]
// fills from the node's network path (mirrors app/templates.py).
export function knownParams(node) {
  return {
    label: node.label,
    id: node.id,
    'network-path reference': node.network_path,
    'network resource': node.network_resource,
  };
}

// Merge node-field defaults with the user-entered values (user wins).
export function paramValueMap(node, paramValues) {
  const values = knownParams(node);
  for (const [k, v] of Object.entries(paramValues)) values[k.toLowerCase()] = v;
  return values;
}

// The user-entered values to PERSIST, keyed by display name, limited to the
// tokens in *names*. Node-derived params ([label], [id], [Network-Path
// Reference], [Network Resource]) are skipped: they always resolve live from the
// node at execution time, so saving a snapshot would shadow the current node
// value once the node is renamed or its network path changes (e.g. a stale
// "label" making `ping [label]` target an old name). Only genuine custom tokens
// ([Host], [Threshold], …) are stored. Blank values are dropped.
export function customParamValues(names, paramValues) {
  const out = {};
  for (const name of names || []) {
    if (KNOWN_PARAM_KEYS.has(String(name).toLowerCase())) continue;
    const v = paramValues[name];
    if (v !== undefined && String(v).trim()) out[name] = v;
  }
  return out;
}

export function substituteParams(text, values) {
  if (typeof text !== 'string' || !text) return text;
  return text.replace(new RegExp(PARAM_RE_SRC, 'g'), (whole, name) => {
    if (!isParamName(name.trim())) return whole;
    const key = name.trim().toLowerCase();
    return key in values ? String(values[key]) : whole;
  });
}

// Client-side mirror of the backend {{a.b}} renderer (node context only),
// applied when loading a template so the form shows resolved text.
export function renderMustaches(text, node) {
  if (typeof text !== 'string') return text;
  return text.replace(/\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}/g, (whole, path) => {
    let cur = { node: { label: node.label, network_path: node.network_path, network_resource: node.network_resource, id: node.id } };
    for (const p of path.split('.')) {
      if (cur && typeof cur === 'object' && p in cur) cur = cur[p];
      else return '';
    }
    return cur == null ? '' : String(cur);
  });
}

// Render one input per [Param] token found in *names* into *fieldsHost*,
// toggling *boxHost* visible. Node-field params are prefilled; the rest start
// empty. *paramValues* is mutated live as the user types.
export function paintParamFields(boxHost, fieldsHost, names, node, paramValues) {
  if (!boxHost || !fieldsHost) return;
  if (!names.length) { boxHost.hidden = true; fieldsHost.innerHTML = ''; return; }
  boxHost.hidden = false;
  const known = knownParams(node);
  fieldsHost.innerHTML = names.map((name) => {
    const key = name.toLowerCase();
    const isKnown = key in known;
    const val = paramValues[name] !== undefined ? paramValues[name] : (isKnown ? String(known[key] ?? '') : '');
    return `<div class="row"><div style="flex:1"><label class="f">${esc(name)}${isKnown ? ' <span class="muted">(from node)</span>' : ''}</label>
      <input data-param="${esc(name)}" value="${esc(val)}" placeholder="${isKnown ? '' : 'value for [' + esc(name) + ']'}"></div></div>`;
  }).join('');
  fieldsHost.querySelectorAll('[data-param]').forEach((el) => {
    el.oninput = () => { paramValues[el.dataset.param] = el.value; el.style.borderColor = ''; };
  });
}

// Show a hint popup of known [Param] names while typing an open `[token` in a
// field. Arrow keys navigate, Enter/Tab insert, Escape dismisses. *onPick* is
// called after a value is inserted so the caller can re-scan.
export function attachParamAutocomplete(field, onPick) {
  let popup = null;
  let items = [];
  let active = 0;

  const close = () => { if (popup) { popup.remove(); popup = null; } items = []; active = 0; };

  // The open `[query` immediately before the caret, or null if not inside one.
  function currentQuery() {
    const pos = field.selectionStart ?? field.value.length;
    const before = field.value.slice(0, pos);
    const open = before.lastIndexOf('[');
    if (open === -1) return null;
    const after = before.slice(open + 1);
    if (/[\]\n]/.test(after) || !/^[A-Za-z0-9 _.\-]*$/.test(after)) return null;
    return { start: open, query: after };
  }

  function paint() {
    if (!popup) {
      const rect = field.getBoundingClientRect();
      popup = document.createElement('div');
      popup.className = 'param-ac';
      popup.style.position = 'fixed';
      popup.style.left = (rect.left || 0) + 'px';
      popup.style.top = ((rect.bottom || 0) + 2) + 'px';
      popup.style.width = (rect.width || 200) + 'px';
      document.body.appendChild(popup);
    }
    popup.innerHTML = items.map((it, i) =>
      `<div class="param-ac-item${i === active ? ' active' : ''}" data-ac="${i}"><b>[${esc(it.name)}]</b><span class="muted"> ${esc(it.hint)}</span></div>`).join('');
    popup.querySelectorAll('[data-ac]').forEach((el) => {
      el.onmousedown = (e) => { e.preventDefault(); choose(Number(el.dataset.ac)); };
    });
  }

  function update() {
    const q = currentQuery();
    if (!q) { close(); return; }
    items = paramHints().filter((it) => it.name.toLowerCase().includes(q.query.toLowerCase()));
    if (!items.length) { close(); return; }
    active = Math.min(active, items.length - 1);
    paint();
  }

  function choose(i) {
    const q = currentQuery();
    if (!q || !items[i]) { close(); return; }
    const pos = field.selectionStart ?? field.value.length;
    const name = items[i].name;
    field.value = field.value.slice(0, q.start) + '[' + name + ']' + field.value.slice(pos);
    const caret = q.start + name.length + 2;
    field.selectionStart = field.selectionEnd = caret;
    field.focus();
    close();
    if (onPick) onPick();
  }

  field.addEventListener('input', () => { active = 0; update(); });
  field.addEventListener('click', update);
  field.addEventListener('blur', () => setTimeout(close, 120));
  field.addEventListener('keydown', (e) => {
    if (!popup) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); active = (active + 1) % items.length; paint(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = (active - 1 + items.length) % items.length; paint(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); choose(active); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
}

// The [Param] names in *names* that have no non-blank value in the merged map
// (node-derived + user-entered). Returns [] when every token is filled. Used by
// the Metrics and Rules dry-runs so an empty value can't silently resolve to a
// blank/literal token — mirrors editor.js validateParams().
export function missingParams(names, node, paramValues) {
  const values = paramValueMap(node, paramValues);
  return names.filter((n) => !String(values[n.toLowerCase()] ?? '').trim());
}
