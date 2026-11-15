// Metrics page — the second dock page (Dataset → Metrics → Rules). Lists the
// dataset's metrics as editable cards. Each metric can jump to its own Rules
// page. "Test run" re-runs the dataset's script and shows each metric's parsed
// value inline. Saving diff-syncs metric fields via the per-metric API.
import { api } from './api.js';
import { state } from './state.js';
import { breadcrumb, esc, toast } from './util.js';

let draft = null;        // working copy of the dataset's metrics
let original = null;     // snapshot metrics at load (for the diff)
let dataset = null;      // the dataset row (script/method for test run)
let ctx = null;          // { go, onSaved } from the panel router
let lastTest = null;     // { [metricName]: parsedEntry } from the most recent test run

export function renderMetricsView(body, node, ds, navCtx) {
  dataset = ds;
  ctx = navCtx;
  original = JSON.parse(JSON.stringify(ds.metrics));
  draft = JSON.parse(JSON.stringify(ds.metrics));
  lastTest = null;

  body.innerHTML = `
  <div class="section">
    <h2 class="page-title">${esc(node.label)}</h2>
    ${breadcrumb([
      { label: 'Datasets', nav: `data-nav="node" data-id="${node.id}"` },
      { label: ds.name, nav: `data-nav="dataset" data-ds="${ds.id}"` },
      { label: 'Metrics' },
    ])}
    <p class="muted">Each metric mines the dataset's raw output with a regex capture group.
      Add rules to a metric to turn its value into a status.</p>
    <div class="row"><button id="btnNewMetric" class="small">+ Metric</button>
      <button id="btnTest" class="small">Test run</button>
      <span id="testOut" class="muted"></span></div>
    <div id="metricList"></div>
    <div class="row" style="margin-top:12px">
      <span style="flex:1"></span>
      <button id="btnSave" class="primary small">Save</button>
    </div>
  </div>`;

  renderMetrics();
  body.querySelector('#btnNewMetric').onclick = () => {
    draft.push({ id: null, name: 'metric', regex: 'value=(\\d+)', group_index: 1, cast: 'auto', unit: '', rules: [], alert: null });
    renderMetrics();
  };
  body.querySelector('#btnTest').onclick = () => testRun();
  body.querySelector('#btnSave').onclick = async () => {
    try { await save(); await ctx.onSaved(); toast('Saved.'); ctx.go({ view: 'metrics', dsId: dataset.id, nodeId: state.selectedId }); }
    catch (e) { toast(e.message, 'error'); }
  };
}

function metricCard(m, i) {
  const t = lastTest && lastTest[m.name];
  const testVal = t
    ? (t.error ? `<span class="err">${esc(t.error)}</span>` : `<b>${esc(t.txt ?? t.num ?? '—')}</b>${esc(m.unit || '')}`)
    : '';
  const testLine = t ? `<div class="kv"><span class="k">last test</span><span>${testVal}</span></div>` : '';
  const ruleCount = m.rules.length;
  return `
  <div class="card">
    <div class="head"><span class="t">${esc(m.name)}</span>
      <span class="muted">${ruleCount ? ruleCount + ' rule' + (ruleCount > 1 ? 's' : '') : 'no rules'}</span>
      <button class="small" data-mi="${i}" data-rules>Add Rules →</button>
      <button class="small danger" data-mi="${i}" data-del>×</button></div>
    <div class="row" style="align-items:flex-start">
      <div style="flex:2"><label class="f">Name</label><input data-mi="${i}" data-field="name" value="${esc(m.name)}" placeholder="name"></div>
      <div style="flex:3"><label class="f">Regex</label><input data-mi="${i}" data-field="regex" value="${esc(m.regex)}" placeholder="regex"></div>
      <div style="width:80px"><label class="f">Cast</label><select data-mi="${i}" data-field="cast">${['auto', 'int', 'float', 'str', 'bool'].map((c) => `<option ${m.cast === c ? 'selected' : ''}>${c}</option>`).join('')}</select></div>
      <div style="width:70px"><label class="f">Unit</label><input data-mi="${i}" data-field="unit" value="${esc(m.unit)}" placeholder="unit"></div>
    </div>
    ${testLine}
  </div>`;
}

function renderMetrics() {
  const list = document.getElementById('metricList');
  if (!list) return;
  list.innerHTML = draft.map(metricCard).join('') || '<p class="muted">No metrics yet. Add one.</p>';
  list.querySelectorAll('[data-field]').forEach((el) => {
    el.onchange = () => { draft[Number(el.dataset.mi)][el.dataset.field] = el.type === 'number' ? Number(el.value) : el.value; };
  });
  list.querySelectorAll('[data-del]').forEach((el) => { el.onclick = () => { draft.splice(Number(el.dataset.mi), 1); renderMetrics(); }; });
  list.querySelectorAll('[data-rules]').forEach((el) => {
    el.onclick = async () => {
      const mi = Number(el.dataset.mi);
      try {
        await save();          // persist the metric (and any edits) so it has an id
        const mid = draft[mi].id;
        await ctx.onSaved();   // refresh the snapshot so the rules route can find it
        ctx.go({ view: 'rules', dsId: dataset.id, metricId: mid, nodeId: state.selectedId });
      } catch (e) { toast(e.message, 'error'); }
    };
  });
}

async function testRun() {
  const out = document.getElementById('testOut');
  out.textContent = 'running…';
  try {
    const res = await api.testRun({
      method: dataset.method, script: dataset.script, timeout_s: dataset.timeout_s,
      credential_id: dataset.credential ? dataset.credential.id : null,
      metrics: draft.map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast })),
    });
    out.innerHTML = res.ok ? '<span class="ok">ok</span>' : `<span class="err">${esc(res.error || 'failed')}</span>`;
    lastTest = {};
    for (const p of (res.parsed || [])) lastTest[p.name] = p;
    renderMetrics();
  } catch (e) { out.textContent = e.message; }
}

// Diff-sync metric fields between the snapshot and the draft. Rules/alerts are
// owned by the Rules page and are left untouched here. On success, `original`
// is refreshed to the persisted draft so a later save diffs against real ids.
async function save() {
  for (const m of original) {
    if (!draft.some((x) => x.id === m.id)) await api.deleteMetric(m.id);
  }
  for (const m of draft) {
    const body = { name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast, unit: m.unit };
    if (!m.id) m.id = (await api.createMetric(dataset.id, body)).id;
    else await api.patchMetric(m.id, body);
  }
  original = JSON.parse(JSON.stringify(draft));
}
