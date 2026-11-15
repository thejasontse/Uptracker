// Rules page — the third dock page (Dataset → Metrics → Rules). Edits one
// metric's rules and email alert. A rule compares the parsed value and assigns
// one of the fixed statuses: good / warning / critical. When no rule matches,
// the metric is "unknown". Rules have no PATCH endpoint, so saving replaces the
// metric's rule set wholesale (delete + recreate) and upserts/deletes the alert.
import { api } from './api.js';
import { state } from './state.js';
import { breadcrumb, esc, toast, RULE_STATUSES } from './util.js';

const OPS = [
  ['gt', '>'], ['lt', '<'], ['gte', '≥'], ['lte', '≤'], ['eq', '='], ['ne', '≠'],
  ['contains', 'contains'], ['regex', 'regex'], ['missing', 'is missing'], ['present', 'is present'],
];

let draft = null;        // working copy: { rules: [...], alert }
let metric = null;       // the metric row from the snapshot
let dataset = null;      // the dataset row (for the back nav)
let ctx = null;          // { go, onSaved } from the panel router

// The three assignable statuses (unknown is the fallback, never chosen here).
function assignableLevels() {
  return state.snapshot.status_levels.filter((l) => RULE_STATUSES.has(l.name));
}

export function renderRulesView(body, node, ds, metricRow, navCtx) {
  dataset = ds;
  metric = metricRow;
  ctx = navCtx;
  draft = {
    rules: JSON.parse(JSON.stringify(metric.rules)),
    alert: metric.alert ? JSON.parse(JSON.stringify(metric.alert)) : null,
  };

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
    <div class="row"><button id="btnAddRule" class="small">+ Rule</button></div>
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
  body.querySelector('#btnAddRule').onclick = () => {
    draft.rules.push({ id: null, op: 'gt', value: '', status_level_id: assignableLevels()[0]?.id ?? 1, priority: 0 });
    renderRules();
  };
  body.querySelector('#btnSave').onclick = () => save();
}

function ruleRow(r, i) {
  const levels = assignableLevels();
  // One line per rule: fixed-column grid (see .rule-grid in app.css) so the
  // selects fill their column instead of wrapping like the old flex row did.
  return `
  <div class="rule-grid">
    <select data-ri="${i}" data-rulefield="op">${OPS.map(([v, l]) => `<option value="${v}" ${r.op === v ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <input data-ri="${i}" data-rulefield="value" value="${esc(r.value)}" placeholder="value">
    <span class="muted">→</span>
    <select data-ri="${i}" data-rulefield="status_level_id">${levels.map((l) => `<option value="${l.id}" ${r.status_level_id === l.id ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}</select>
    <button class="small danger" data-ri="${i}" data-delrule>×</button>
  </div>`;
}

function renderRules() {
  const list = document.getElementById('ruleList');
  if (!list) return;
  list.innerHTML = draft.rules.map(ruleRow).join('') || '<p class="muted">No rules. This metric stays unknown.</p>';
  list.querySelectorAll('[data-rulefield]').forEach((el) => {
    el.onchange = () => { draft.rules[Number(el.dataset.ri)][el.dataset.rulefield] = el.type === 'number' ? Number(el.value) : el.value; };
  });
  list.querySelectorAll('[data-delrule]').forEach((el) => {
    el.onclick = () => { draft.rules.splice(Number(el.dataset.ri), 1); renderRules(); };
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
      };
      renderAlert();
    };
    return;
  }
  const a = draft.alert;
  box.innerHTML = `
    <div class="card" style="margin-top:12px">
      <div class="head"><span class="t">Email alert</span>
        <label class="toggle"><input data-alertfield="enabled" type="checkbox" ${a.enabled ? 'checked' : ''}> enabled</label>
        <label class="toggle"><input data-alertfield="only_on_change" type="checkbox" ${a.only_on_change ? 'checked' : ''}> on change</label>
        <button class="small danger" id="btnDelAlert">Remove</button></div>
      <div class="row"><div style="flex:3"><label class="f">Recipients (comma separated)</label>
        <input data-alertfield="recipients" value="${esc(a.recipients)}"></div>
        <div style="flex:1"><label class="f">Cooldown (s)</label><input data-alertfield="cooldown_s" type="number" value="${a.cooldown_s}"></div></div>
      <label class="f">Subject</label><input data-alertfield="subject" value="${esc(a.subject)}">
      <label class="f">Body</label><textarea data-alertfield="body" style="min-height:48px">${esc(a.body)}</textarea>
    </div>`;
  box.querySelectorAll('[data-alertfield]').forEach((el) => {
    el.onchange = () => { draft.alert[el.dataset.alertfield] = el.type === 'number' ? Number(el.value) : (el.type === 'checkbox' ? el.checked : el.value); };
  });
  box.querySelector('#btnDelAlert').onclick = () => { draft.alert = null; renderAlert(); };
}

async function save() {
  try {
    // Rules have no PATCH — replace the whole set for this metric.
    for (const r of metric.rules) await api.deleteRule(r.id);
    for (const r of draft.rules) {
      await api.createRule(metric.id, { op: r.op, value: r.value, status_level_id: r.status_level_id, priority: r.priority });
    }
    if (draft.alert) await api.upsertAlert(metric.id, draft.alert);
    else if (metric.alert) await api.deleteAlert(metric.id);
    await ctx.onSaved();
    toast('Saved.');
    ctx.go({ view: 'metrics', dsId: dataset.id, nodeId: state.selectedId });
  } catch (e) { toast(e.message, 'error'); }
}
