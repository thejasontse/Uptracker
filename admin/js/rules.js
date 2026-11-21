// Rules page — the third dock page (Dataset → Metrics → Rules). Edits one
// metric's rules and email alert. A rule compares the parsed value and assigns
// one of the fixed statuses: good / warning / critical. When no rule matches,
// the metric is "unknown". Rules have no PATCH endpoint, so saving replaces the
// metric's rule set wholesale (delete + recreate) and upserts/deletes the alert.
import { api } from './api.js';
import { state } from './state.js';
import { breadcrumb, esc, toast, RULE_STATUSES } from './util.js';
import {
  scanParams, paramValueMap, missingParams,
  paintParamFields, attachParamAutocomplete,
} from './params.js';
import { showTestResult } from './dockTerminal.js';

const OPS = [
  ['gt', '>'], ['lt', '<'], ['gte', '≥'], ['lte', '≤'], ['eq', '='], ['ne', '≠'],
  ['contains', 'contains'], ['regex', 'regex'], ['missing', 'is missing'], ['present', 'is present'],
  ['is', 'is'], ['is_not', 'is not'], ['between', 'between'],
];

// Words treated as boolean true/false by the is / is_not operators (mirrors
// app/rules.py). Anything else fails to normalise and never matches.
const TRUE_WORDS = new Set(['1', 'true', 'yes', 'on', 'up', 'ok']);
const FALSE_WORDS = new Set(['0', 'false', 'no', 'off', 'down']);

let draft = null;        // working copy: { rules: [...], alert }
let metric = null;       // the metric row from the snapshot
let dataset = null;      // the dataset row (script/method for test run)
let nodeRef = null;      // the owning node (for [Param] auto-fill)
let ctx = null;          // { go, onSaved } from the panel router
let paramValues = {};    // user-entered [Param] values, keyed by display name

// Decode a stored default trigger_statuses JSON string (from Settings) into an
// array, falling back to ["warning","critical"] when unset or malformed.
function _parseStatusDefault(raw) {
  if (!raw) return ['warning', 'critical'];
  try {
    const p = JSON.parse(raw);
    if (Array.isArray(p) && p.length) return p.map(String);
  } catch { /* fall through */ }
  return ['warning', 'critical'];
}
let lastTest = null;     // parsed value entry from the most recent test run

// True when *text*'s leading numeric prefix equals cnum — mirrors _text_num_equal
// in app/rules.py so eq/ne match percentage-style values ("85 %", rule "85%")
// that never compare equal as raw strings.
function textNumEqual(text, cnum) {
  const m = /^\s*(-?\d+(?:\.\d+)?)/.exec(String(text ?? ''));
  return m ? parseFloat(m[1]) === cnum : false;
}

// Client-side mirror of app/rules.py rule_matches(). *value* is a parsed test
// result {num, txt, raw, error}; returns true when the rule condition holds.
function ruleMatches(op, ruleValue, value) {
  if (op === 'missing') return !!(value && value.error);
  if (op === 'present') return !!(value && !value.error);
  if (!value || value.error) return false;
  const text = value.txt != null ? value.txt : (value.raw || '');
  const num = typeof value.num === 'number' ? value.num : null;
  const cnum = parseFloat(ruleValue);
  const hasCnum = ruleValue !== '' && !Number.isNaN(cnum);
  switch (op) {
    case 'gt': return num != null && hasCnum && num > cnum;
    case 'lt': return num != null && hasCnum && num < cnum;
    case 'gte': return num != null && hasCnum && num >= cnum;
    case 'lte': return num != null && hasCnum && num <= cnum;
    case 'eq':
      if (hasCnum) return (num != null && num === cnum) || textNumEqual(text, cnum);
      return text === ruleValue;
    case 'ne':
      if (hasCnum) return !((num != null && num === cnum) || textNumEqual(text, cnum));
      return text !== ruleValue;
    case 'contains': return text.includes(ruleValue);
    case 'regex': try { return new RegExp(ruleValue).test(text); } catch { return false; }
    case 'is':
    case 'is_not': {
      const want = asBool(ruleValue);
      let got = asBool(text);
      if (got == null && num != null) got = num !== 0;
      if (want == null || got == null) return false;
      return op === 'is' ? got === want : got !== want;
    }
    case 'between': {
      const parts = String(ruleValue).split(',');
      if (parts.length !== 2) return false;
      const lo = parseFloat(parts[0]); const hi = parseFloat(parts[1]);
      if (num == null || Number.isNaN(lo) || Number.isNaN(hi)) return false;
      return num >= lo && num <= hi;
    }
    default: return false;
  }
}

function asBool(s) {
  const low = String(s == null ? '' : s).trim().toLowerCase();
  if (TRUE_WORDS.has(low)) return true;
  if (FALSE_WORDS.has(low)) return false;
  return null;
}

// All [Param] names referenced across the test: dataset script, metric regex,
// and every rule value. Mirrors the backend scan so each token gets an input.
function testParamNames() {
  const texts = [dataset && dataset.script, metric && metric.regex];
  for (const r of draft.rules) texts.push(r.value);
  return scanParams(texts.filter((t) => typeof t === 'string'));
}

function refreshParams() {
  paintParamFields(
    document.getElementById('rpParamsWrap') || document.getElementById('rpParams'),
    document.getElementById('rpParamFields'),
    testParamNames(), nodeRef, paramValues,
  );
}

// The three assignable statuses (unknown is the fallback, never chosen here).
// Sorted by rank so "warning" — not whichever row happens to come first in the
// snapshot (which is ordered by rank too, but unknown sits at rank 0) — is the
// first option and the preselect for new rules. With the seeded ids that puts
// critical(3), warning(2), good(1): a stray submit lands on warning instead of
// silently flipping every rule to "good".
function assignableLevels() {
  return state.snapshot.status_levels
    .filter((l) => RULE_STATUSES.has(l.name))
    .sort((a, b) => b.rank - a.rank);
}

export function renderRulesView(body, node, ds, metricRow, navCtx) {
  dataset = ds;
  metric = metricRow;
  nodeRef = node;
  ctx = navCtx;
  draft = {
    rules: JSON.parse(JSON.stringify(metric.rules)),
    alert: metric.alert ? JSON.parse(JSON.stringify(metric.alert)) : null,
  };
  paramValues = ds && ds.param_values ? { ...ds.param_values } : {};
  lastTest = null;

  body.innerHTML = `
  <div class="section">
    <h2 class="page-title">${esc(node.label)}</h2>
    ${breadcrumb([
      { label: 'Datasets', nav: `data-nav="node" data-id="${node.id}"` },
      { label: ds.name, nav: `data-nav="dataset" data-ds="${ds.id}"` },
      { label: 'Metrics', nav: `data-nav="metrics" data-ds="${ds.id}"` },
      { label: 'Rules' },
    ])}
    <p class="muted">A rule sets the metric's status when its value matches. Highest priority wins;
      ties go to the worse status. No matching rule → <b>unknown</b>.</p>
    <div id="rpParamsWrap" hidden>
      <div id="rpParams" class="card">
        <div class="head"><span class="t">Parameters</span></div>
        <div id="rpParamFields"></div>
      </div>
      <hr class="params-hr">
    </div>
    <div class="row"><button id="btnAddRule" class="small">+ Rule</button>
      <button id="btnTestRules" class="small">Test rules</button></div>
    <div class="rule-grid rule-head"><span>When</span><span>Value</span><span></span><span>Status</span><span></span></div>
    <div id="ruleList"></div>
    <div id="alertBox"></div>
    <div class="row" style="margin-top:12px">
      <span style="flex:1"></span>
      <button id="btnSave" class="primary small">Save</button>
    </div>
  </div>`;

  renderRules();
  renderAlert();
  refreshParams();
  body.querySelector('#btnAddRule').onclick = () => {
    draft.rules.push({ id: null, op: 'gt', value: '', status_level_id: assignableLevels()[0]?.id ?? 1, priority: 0 });
    renderRules();
    refreshParams();
  };
  body.querySelector('#btnTestRules').onclick = () => testRules();
  body.querySelector('#btnSave').onclick = () => save();
}

function ruleRow(r, i) {
  const levels = assignableLevels();
  const noValue = r.op === 'missing' || r.op === 'present';
  // One line per rule: fixed-column grid (see .rule-grid in app.css) so the
  // selects fill their column instead of wrapping like the old flex row did.
  // missing/present need no operand, so the value cell renders as an empty
  // placeholder span — the field is removed but the grid column (and thus the
  // alignment of every other rule row) is preserved. "between" takes two
  // numbers, hinted via its placeholder.
  const ph = r.op === 'between' ? 'min, max' : 'value';
  return `
  <div class="rule-grid">
    <select data-ri="${i}" data-rulefield="op">${OPS.map(([v, l]) => `<option value="${v}" ${r.op === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
    ${noValue ? '<span></span>' : `<span><input data-ri="${i}" data-rulefield="value" value="${esc(r.value)}" placeholder="${ph}"></span>`}
    <span class="muted">→</span>
    <select data-ri="${i}" data-rulefield="status_level_id">${levels.map((l) => `<option value="${esc(String(l.id))}" ${Number(r.status_level_id) === l.id ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}</select>
    <button class="small danger" data-ri="${i}" data-delrule>×</button>
  </div>`;
}

function renderRules() {
  const list = document.getElementById('ruleList');
  if (!list) return;
  list.innerHTML = draft.rules.map(ruleRow).join('') || '<p class="muted">No rules. This metric stays unknown.</p>';
  list.querySelectorAll('[data-rulefield]').forEach((el) => {
    el.onchange = () => {
      const r = draft.rules[Number(el.dataset.ri)];
      const field = el.dataset.rulefield;
      // The status select's DOM value is a string; store it as a number so the
      // row re-renders with the right option selected and save() PATCHes an int.
      r[field] = (field === 'status_level_id' || el.type === 'number') ? Number(el.value) : el.value;
      // Changing the operator can toggle the value field (missing/present) or
      // reveal the between hint, so repaint the row list.
      if (el.dataset.rulefield === 'op') { renderRules(); refreshParams(); }
    };
  });
  // A rule's threshold can be a [Param] token, resolved at collection time.
  list.querySelectorAll('[data-rulefield="value"]').forEach((el) => attachParamAutocomplete(el, () => refreshParams()));
  list.querySelectorAll('[data-delrule]').forEach((el) => {
    el.onclick = () => { draft.rules.splice(Number(el.dataset.ri), 1); renderRules(); refreshParams(); };
  });
}

function renderAlert() {
  const box = document.getElementById('alertBox');
  if (!box) return;
  if (!draft.alert) {
    box.innerHTML = `<div class="row" style="margin-top:12px"><button id="btnAddAlert" class="small">+ Email alert</button></div>`;
    box.querySelector('#btnAddAlert').onclick = async () => {
      // Pre-fill from the global defaults (Settings → Email alert defaults) so
      // subject/body arrive as generated name/status templates, still editable.
      let d = {};
      try { d = await api.settings(); } catch { /* fall back to built-ins */ }
      draft.alert = {
        enabled: true,
        recipients: d.alert_default_recipients || '',
        subject: d.alert_default_subject || '{{status}}: {{node.label}} / {{metric.name}}',
        body: d.alert_default_body || '{{node.label}} — metric {{metric.name}} is {{status}} (value={{value}}).',
        cooldown_s: Number(d.alert_default_cooldown_s || 3600),
        only_on_change: true,
        trigger_count: Number(d.alert_default_trigger_count || 1),
        trigger_statuses: _parseStatusDefault(d.alert_default_trigger_statuses),
      };
      renderAlert();
    };
    return;
  }
  const a = draft.alert;
  // All status levels (including unknown) are selectable as streak-qualifying
  // statuses; the stored set decides which are pre-selected.
  const allStatuses = (state.snapshot?.status_levels || []).map((l) => l.name);
  const selStatuses = Array.isArray(a.trigger_statuses) && a.trigger_statuses.length ? a.trigger_statuses : ['warning', 'critical'];
  box.innerHTML = `
    <div class="card" style="margin-top:12px">
      <div class="head"><span class="t">Email alert</span>
        <label class="toggle"><input data-alertfield="enabled" type="checkbox" ${a.enabled ? 'checked' : ''}> enabled</label>
        <label class="toggle"><input data-alertfield="only_on_change" type="checkbox" ${a.only_on_change ? 'checked' : ''}> on change</label>
        <button class="small danger" id="btnDelAlert">Remove</button></div>
      <div class="row"><div style="flex:1"><label class="f">Fire after (runs)</label><input data-alertfield="trigger_count" type="number" min="1" value="${a.trigger_count ?? 1}"></div>
        <div style="flex:2"><label class="f">Qualifying statuses (Ctrl/Cmd-click for multiple)</label>
          <select data-alertfield="trigger_statuses" multiple size="4">${allStatuses.map((s) => `<option value="${esc(s)}" ${selStatuses.includes(s) ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></div></div>
      <div class="row"><div style="flex:3"><label class="f">Recipients (comma separated)</label>
        <input data-alertfield="recipients" value="${esc(a.recipients)}"></div>
        <div style="flex:1"><label class="f">Cooldown (s)</label><input data-alertfield="cooldown_s" type="number" value="${a.cooldown_s}"></div></div>
      <label class="f">Subject</label><input data-alertfield="subject" value="${esc(a.subject)}">
      <label class="f">Body</label><textarea data-alertfield="body" style="min-height:48px">${esc(a.body)}</textarea>
    </div>`;
  box.querySelectorAll('[data-alertfield]').forEach((el) => {
    el.onchange = () => {
      if (el.multiple) draft.alert[el.dataset.alertfield] = [...el.selectedOptions].map((o) => o.value);
      else draft.alert[el.dataset.alertfield] = el.type === 'number' ? Number(el.value) : (el.type === 'checkbox' ? el.checked : el.value);
    };
  });
  box.querySelector('#btnDelAlert').onclick = () => { draft.alert = null; renderAlert(); };
}

async function save() {
  try {
    // Rules have no PATCH — replace the whole set for this metric.
    for (const r of metric.rules) await api.deleteRule(r.id);
    for (const r of draft.rules) {
      const saved = await api.createRule(metric.id, { op: r.op, value: r.value, status_level_id: r.status_level_id, priority: r.priority });
      r.id = saved.id;   // keep ids so a second save doesn't orphan/duplicate
    }
    if (draft.alert) await api.upsertAlert(metric.id, draft.alert);
    else if (metric.alert) await api.deleteAlert(metric.id);
    // Reflect the persisted set back into the snapshot's metric so a subsequent
    // test/save starts from saved state. Stay on the rules view (no redirect).
    metric.rules = JSON.parse(JSON.stringify(draft.rules));
    metric.alert = draft.alert ? JSON.parse(JSON.stringify(draft.alert)) : null;
    await ctx.onSaved();
    toast('Saved.');
  } catch (e) { toast(e.message, 'error'); }
}

// Run the dataset script once and evaluate every draft rule against the parsed
// value, showing each rule's result in the bottom terminal. Reuses /api/test/run
// with the raw regex + [Param] values so it matches what collection would do.
async function testRules() {
  // Block the dry-run when any [Param] token is blank — otherwise the server
  // substitutes an empty string and the command runs with a missing value.
  const missing = missingParams(testParamNames(), nodeRef, paramValues);
  if (missing.length) {
    toast('Fill parameter' + (missing.length > 1 ? 's' : '') + ': ' + missing.join(', '), 'error');
    refreshParams();
    return;
  }
  try {
    const pvals = paramValueMap(nodeRef, paramValues);
    const res = await api.testRun({
      method: dataset.method || 'local',
      credential_id: dataset.credential ? dataset.credential.id : null,
      script: dataset.script,
      metrics: [{ name: metric.name, regex: metric.regex || '', group_index: metric.group_index || 1, cast: metric.cast || 'auto' }],
      node_id: nodeRef ? nodeRef.id : state.selectedId,
      param_values: pvals,
    });
    lastTest = res;
    const entry = (res.parsed || [])[0] || {};
    if (entry.error) { showTestResult(res, 'rules editor'); return; }
    // Evaluate each rule and report which fire, plus the resulting status.
    const fired = [];
    for (const r of draft.rules) {
      if (ruleMatches(r.op, r.value, entry)) {
        const lvl = state.snapshot.status_levels.find((l) => l.id === Number(r.status_level_id));
        fired.push(lvl ? lvl.name : '?');
      }
    }
    // Worst status among the firing rules (ties → worse), mirroring evaluate_metric.
    let result = 'unknown';
    const order = { unknown: 0, good: 1, warning: 2, critical: 3 };
    for (const f of fired) if ((order[f] ?? 0) > (order[result] ?? 0)) result = f;
    const summary = `${fired.length ? 'matched ' + fired.join(', ') : 'no rule matched'} · status: ${result}`;
    showTestResult({ ...res, error: res.ok ? null : res.error, raw: (res.raw || '') + '\n— rules —\n' + summary }, 'rules editor');
  } catch (e) {
    showTestResult({ ok: false, error: e.message, raw: '', parsed: [] }, 'rules editor');
  }
}
