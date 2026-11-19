// Metrics page — the second dock page (Dataset → Metrics → Rules). Lists the
// dataset's metrics as editable cards. Each metric can jump to its own Rules
// page. "Test run" re-runs the dataset's script and shows each metric's parsed
// value inline. Saving diff-syncs metric fields via the per-metric API.
import { api } from './api.js';
import { state } from './state.js';
import { breadcrumb, esc, toast } from './util.js';
import {
  scanParams, paramValueMap, substituteParams,
  paintParamFields, attachParamAutocomplete,
} from './params.js';

let draft = null;        // working copy of the dataset's metrics
let original = null;     // snapshot metrics at load (for the diff)
let dataset = null;      // the dataset row (script/method for test run)
let nodeRef = null;      // the owning node (for [Param] auto-fill)
let ctx = null;          // { go, onSaved } from the panel router
let lastTest = null;     // { [metricName]: parsedEntry } from the most recent test run
let paramValues = {};    // user-entered [Param] values, keyed by display name
let rawOut = '';         // raw script output shown for regex building (last run / test)

// All [Param] names referenced across the draft: the dataset script plus every
// metric name/regex/unit. Mirrors the backend scan so a token gets an input.
function draftParamNames() {
  const texts = [dataset && dataset.script];
  for (const m of draft) texts.push(m.name, m.regex, m.unit);
  return scanParams(texts.filter((t) => typeof t === 'string'));
}

// The most recent raw output to show for regex building: a fresh test run wins,
// otherwise the dataset's last persisted collection output.
function initialRaw() {
  if (rawOut) return rawOut;
  return (dataset && dataset.last_output) || '';
}

function paintRaw() {
  const box = document.getElementById('rawBox');
  if (!box) return;
  const txt = initialRaw();
  if (!txt) { box.hidden = true; return; }
  box.hidden = false;
  const src = rawOut ? 'latest test run' : 'last collected run';
  box.querySelector('#rawSrc').textContent = src;
  box.querySelector('#rawPre').textContent = txt;
}

function refreshParams() {
  const names = draftParamNames();
  paintParamFields(
    document.getElementById('mpParams'),
    document.getElementById('mpParamFields'),
    names, nodeRef, paramValues,
  );
}

export function renderMetricsView(body, node, ds, navCtx) {
  dataset = ds;
  nodeRef = node;
  ctx = navCtx;
  original = JSON.parse(JSON.stringify(ds.metrics));
  draft = JSON.parse(JSON.stringify(ds.metrics));
  lastTest = null;
  paramValues = ds && ds.param_values ? { ...ds.param_values } : {};
  rawOut = '';

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
    <div id="mpParams" class="card" hidden>
      <div class="head"><span class="t">Parameters</span><span class="muted">[Name] tokens are saved as-is and filled in at collection time</span></div>
      <div id="mpParamFields"></div>
    </div>
    <div class="row"><button id="btnNewMetric" class="small">+ Metric</button>
      <button id="btnTest" class="small">Test run</button>
      <span id="testOut" class="muted"></span></div>
    <div id="rawBox" class="card" hidden>
      <div class="head"><span class="t">Raw output</span><span id="rawSrc" class="muted"></span></div>
      <pre id="rawPre" class="out"></pre>
    </div>
    <div id="metricList"></div>
    <div class="row" style="margin-top:12px">
      <span style="flex:1"></span>
      <button id="btnSave" class="primary small">Save</button>
    </div>
  </div>`;

  renderMetrics();
  refreshParams();
  paintRaw();
  body.querySelector('#btnNewMetric').onclick = () => {
    draft.push({ id: null, name: 'metric', regex: 'value=(\\d+)', group_index: 1, cast: 'auto', unit: '', rules: [], alert: null });
    renderMetrics();
    refreshParams();
  };
  body.querySelector('#btnTest').onclick = () => testRun();
  body.querySelector('#btnSave').onclick = async () => {
    try {
      await save();
      // Persist the [Param] values alongside the metrics so they survive a
      // reload and are used by scheduled collection. Send the full dataset
      // fields (patch replaces them) with param_values added.
      await api.patchDataset(dataset.id, {
        name: dataset.name, method: dataset.method, script: dataset.script,
        timeout_s: dataset.timeout_s, interval_s: dataset.interval_s,
        enabled: dataset.enabled, credential_id: dataset.credential ? dataset.credential.id : null,
        param_values: paramValues,
      });
      dataset.param_values = { ...paramValues };
      await ctx.onSaved(); toast('Saved.');
    }
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
  // [Param] autocomplete on the free-text fields; picking a token re-scans so
  // its value input appears in the Parameters panel.
  list.querySelectorAll('[data-field="name"], [data-field="regex"], [data-field="unit"]').forEach((el) => {
    attachParamAutocomplete(el, () => refreshParams());
  });
  list.querySelectorAll('[data-del]').forEach((el) => { el.onclick = () => { draft.splice(Number(el.dataset.mi), 1); renderMetrics(); refreshParams(); }; });
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
  // The dry-run endpoint resolves [Param] tokens server-side (node-derived
  // params + the values entered here), so send the raw script/regex as stored.
  try {
    const res = await api.testRun({
      method: dataset.method, script: dataset.script, timeout_s: dataset.timeout_s,
      credential_id: dataset.credential ? dataset.credential.id : null,
      node_id: nodeRef ? nodeRef.id : state.selectedId, param_values: paramValues,
      metrics: draft.map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast })),
    });
    out.innerHTML = res.ok ? '<span class="ok">ok</span>' : `<span class="err">${esc(res.error || 'failed')}</span>`;
    lastTest = {};
    for (const p of (res.parsed || [])) lastTest[p.name] = p;
    rawOut = res.raw || '';
    renderMetrics();
    paintRaw();
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
