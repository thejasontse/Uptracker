// Dataset editor — the first of three dock pages (Dataset → Metrics → Rules).
// This page edits only the dataset itself: how to retrieve raw data (method +
// script), schedule, and credential. Metrics live on their own page (metrics.js),
// reached via "Add Metrics"/"Metrics". "Test run" is a dry run of the script.
// Enforces the credential guard: saving a script/method/credential change while
// a credential is attached requires re-entering the secret (HTTP 428 retry).
import { api } from './api.js';
import { state } from './state.js';
import { breadcrumb, esc, toast } from './util.js';
import {
  scanParams, paramValueMap, substituteParams, renderMustaches,
  paintParamFields, attachParamAutocomplete,
} from './params.js';

const METHODS = ['bash', 'cmd', 'powershell'];

let draft = null;
let editorNode = null;   // node being edited (for [Param] auto-fill)
let tplActive = null;    // dataset template payload currently applied to the draft
let paramValues = {};    // user-entered [Param] values, keyed by display name

// All [Param] names referenced anywhere in the draft: the dataset name/script
// plus every metric name/regex/unit and rule value. Mirrors the backend scan in
// templates._dataset_param_texts so a token in a metric/rule field gets an input.
function draftParamNames() {
  const texts = [draft.name, draft.script];
  for (const m of draft.metrics || []) {
    texts.push(m.name, m.regex, m.unit);
    for (const r of m.rules || []) texts.push(r.value);
  }
  return scanParams(texts);
}

// Deep copy of the draft with every [Param] token replaced by its value. Used
// only for local dry-runs (Test run) — persisted data keeps the raw tokens.
function resolveDraft(d, values) {
  const out = JSON.parse(JSON.stringify(d));
  out.name = substituteParams(out.name, values);
  out.script = substituteParams(out.script, values);
  for (const m of out.metrics) {
    m.name = substituteParams(m.name, values);
    m.regex = substituteParams(m.regex, values);
    m.unit = substituteParams(m.unit, values);
    for (const r of m.rules) r.value = substituteParams(r.value, values);
  }
  return out;
}

export function renderDatasetEditor(body, node, ds, ctx) {
  draft = ds
    ? JSON.parse(JSON.stringify({ ...ds, metrics: ds.metrics.map((m) => ({ ...m, rules: [...m.rules] })) }))
    : { id: null, name: 'New dataset', method: 'bash', script: '', timeout_s: 30, interval_s: 600, enabled: true, credential_id: null, credential: null, metrics: [] };
  draft.secret = null;
  editorNode = node;
  tplActive = null;
  // Repopulate saved [Param] values (stored raw on the dataset) so re-opening
  // an existing dataset shows the last-entered values, not blanks.
  paramValues = ds && ds.param_values ? { ...ds.param_values } : {};

  body.innerHTML = `
  <div class="section">
    <h2 class="page-title">${esc(node.label)}</h2>
    ${breadcrumb([
      { label: 'Datasets', nav: `data-nav="node" data-id="${node.id}"` },
      { label: ds ? ds.name : 'New dataset' },
    ])}
    <div class="row"><div style="flex:2"><label class="f">Name</label><input id="dsName" value="${esc(draft.name)}"></div>
      <div style="flex:1"><label class="f">Method</label><select id="dsMethod">${METHODS.map((m) => `<option ${m === draft.method ? 'selected' : ''}>${m}</option>`).join('')}</select></div></div>
    <label class="f">Script / command</label>
    <textarea id="dsScript" placeholder="e.g. curl -s http://{{node.network_path}}/health">${esc(draft.script)}</textarea>
    <p class="muted">Protocol probes are ordinary commands — <code>ping -c1 host</code>, <code>nc -z host 443</code>, <code>curl -s url</code>. Credentials are available to the script as <code>$SM_USER</code> / <code>$SM_SECRET</code>.</p>
    <div id="tplParams" class="card" hidden>
      <div class="head"><span class="t">Parameters</span><span class="muted">[Name] tokens are saved as-is and filled in at collection time</span></div>
      <div id="tplParamFields"></div>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f">Timeout (s)</label><input id="dsTimeout" type="number" value="${draft.timeout_s}"></div>
      <div style="flex:1"><label class="f">Interval (s)</label><input id="dsInterval" type="number" value="${draft.interval_s}"></div>
      <div style="flex:1"><label class="f">Enabled</label><input id="dsEnabled" type="checkbox" ${draft.enabled ? 'checked' : ''}></div>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f">Credential</label><select id="dsCred"></select></div>
      <div style="flex:1"><label class="f">Secret</label><input id="dsSecret" type="password" placeholder="re-enter to save" autocomplete="new-password"></div>
    </div>
    <div class="row"><button id="btnTest" class="small">Test run</button>
      <span id="testOut" class="muted"></span></div>
    <div id="testResult"></div>
    <div class="row" style="margin-top:12px">
      <button id="btnTpl" class="small">Save as template</button>
      <span style="flex:1"></span>
      <button data-nav="node" data-id="${esc(node.id)}" class="small">Cancel</button>
      <button id="btnSave" class="small">Save</button>
      <button id="btnMetrics" class="primary small">${ds ? 'Metrics' : 'Add Metrics'} →</button>
    </div>
  </div>`;

  loadCredentials(body.querySelector('#dsCred'));

  body.querySelector('#dsMethod').onchange = () => refreshParams();
  body.querySelector('#btnTest').onclick = () => testRun();
  body.querySelector('#btnTpl').onclick = () => saveAsTemplate(node);
  body.querySelector('#btnSave').onclick = () => commit(node, ds, ctx, 'node');
  body.querySelector('#btnMetrics').onclick = () => commit(node, ds, ctx, 'metrics');

  // Live re-scan: typing [Param] tokens into name/script shows their inputs.
  const scriptEl = body.querySelector('#dsScript');
  scriptEl.oninput = () => refreshParams();
  body.querySelector('#dsName').oninput = () => refreshParams();
  attachParamAutocomplete(scriptEl, () => refreshParams());
  refreshParams();

  // A template is applied from the Templates view (Apply → here), which passes
  // ctx.tplId. There is no in-form picker — templates are managed on that page.
  if (!ds && ctx.tplId) {
    api.listTemplates('dataset').then(({ templates }) => {
      const t = templates.find((x) => x.id === ctx.tplId);
      if (t) applyTemplateToDraft(t);
    }).catch((e) => toast(e.message, 'error'));
  }
}

// Load a dataset template into the working draft: {{ }} resolved against the
// node now, [Param] tokens kept for the Parameters section to fill at save.
// The template's metrics/rules are held in the draft (not shown here) and are
// created when the dataset is saved.
function applyTemplateToDraft(t) {
  const p = t.payload || {};
  const mu = (s) => renderMustaches(s, editorNode);
  tplActive = t;
  draft.name = mu(p.name || 'Dataset');
  draft.method = METHODS.includes(p.method) ? p.method : 'bash';
  draft.script = mu(p.script || '');
  draft.timeout_s = p.timeout_s ?? 30;
  draft.interval_s = p.interval_s ?? 600;
  draft.metrics = (p.metrics || []).map((m) => ({
    id: null, name: mu(m.name || 'metric'), regex: mu(m.regex || ''),
    group_index: m.group_index ?? 1, cast: m.cast || 'auto', unit: mu(m.unit || ''),
    rules: (m.rules || []).map((r) => ({
      id: null, op: r.op || 'missing', value: mu(r.value ?? ''),
      status_level_id: r.status_level_id ?? defaultLevelId(), priority: r.priority ?? 0,
    })),
  }));
  document.getElementById('dsName').value = draft.name;
  document.getElementById('dsMethod').value = draft.method;
  document.getElementById('dsScript').value = draft.script;
  document.getElementById('dsTimeout').value = draft.timeout_s;
  document.getElementById('dsInterval').value = draft.interval_s;
  refreshParams();
}

// Render one input per [Param] token found in the current draft. Node-field
// params (label/id/network-path/network-resource) are prefilled; the rest start empty.
function refreshParams() {
  collectDraft();
  const names = draftParamNames();
  paintParamFields(
    document.getElementById('tplParams'),
    document.getElementById('tplParamFields'),
    names, editorNode, paramValues,
  );
}

async function loadCredentials(sel) {
  const { credentials } = await api.listCredentials();
  sel.innerHTML = '<option value="">— none —</option>' +
    credentials.map((c) => `<option value="${c.id}" ${draft.credential && draft.credential.id === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('');
}

// Default status for a template rule: warning (a rule that fires is a problem).
function defaultLevelId() {
  const warn = state.level('warning') || state.level('good');
  return warn ? warn.id : 1;
}

function collectDraft() {
  draft.name = document.getElementById('dsName').value;
  draft.method = document.getElementById('dsMethod').value;
  draft.script = document.getElementById('dsScript').value;
  draft.timeout_s = Number(document.getElementById('dsTimeout').value) || 30;
  draft.interval_s = Number(document.getElementById('dsInterval').value) || 600;
  draft.enabled = document.getElementById('dsEnabled').checked;
  draft.credential_id = document.getElementById('dsCred').value ? Number(document.getElementById('dsCred').value) : null;
  const secret = document.getElementById('dsSecret').value;
  if (secret) draft.secret = secret;
}

// Validate every [Param] token has a value. Returns the merged values map (for
// local dry-runs) or null when any are blank — in which case the offending
// inputs are flagged and nothing is saved. The stored data keeps the raw tokens;
// only Test run resolves them locally, and the backend resolves at collection.
function validateParams() {
  collectDraft();
  const paramNames = draftParamNames();
  if (!paramNames.length) return {};
  const values = paramValueMap(editorNode, paramValues);
  const missing = paramNames.filter((n) => !String(values[n.toLowerCase()] ?? '').trim());
  if (missing.length) {
    toast('Fill parameter' + (missing.length > 1 ? 's' : '') + ': ' + missing.join(', '), 'error');
    document.querySelectorAll('#tplParamFields [data-param]').forEach((el) => {
      if (missing.includes(el.dataset.param)) el.style.borderColor = 'var(--ng-danger)';
    });
    return null;
  }
  return values;
}

// The user-entered [Param] values keyed by display name, limited to the tokens
// actually referenced by the draft. Persisted verbatim (not resolved) so the
// backend can substitute them at execution time.
function paramValuesPayload() {
  const out = {};
  for (const name of draftParamNames()) {
    if (paramValues[name] !== undefined && String(paramValues[name]).trim()) out[name] = paramValues[name];
  }
  return out;
}

async function testRun() {
  const out = document.getElementById('testOut');
  const box = document.getElementById('testResult');
  const values = validateParams();
  if (!values) return;
  const run = resolveDraft(draft, values);
  out.textContent = 'running…';
  try {
    const res = await api.testRun({
      method: run.method, script: run.script, timeout_s: run.timeout_s,
      credential_id: run.credential_id, secret: run.secret,
      metrics: run.metrics.map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast })),
    });
    out.innerHTML = res.ok ? '<span class="ok">ok</span>' : `<span class="err">${esc(res.error || 'failed')}</span>`;
    box.innerHTML = `<pre class="out">${esc(res.raw || '(no output)')}</pre>`;
  } catch (e) { out.textContent = e.message; }
}

// Save the dataset. The stored name/script keep their [Param] tokens; the
// entered values go up as `param_values` for the backend to resolve at
// collection time. `then` is 'node' (Save: stay in place + toast) or 'metrics'
// (Add Metrics / Metrics: navigate). For a new dataset, template-provided
// metrics are created too (with their tokens intact) so they show on the Metrics page.
async function commit(node, ds, ctx, then) {
  const values = validateParams();
  if (!values) return;
  collectDraft();
  const body = {
    name: draft.name, method: draft.method, script: draft.script,
    timeout_s: draft.timeout_s, interval_s: draft.interval_s, enabled: draft.enabled,
    credential_id: draft.credential_id, secret: draft.secret,
    param_values: paramValuesPayload(),
  };
  try {
    const saved = ds ? await api.patchDataset(ds.id, body) : await api.createDataset(node.id, body);
    if (!ds) await createTemplateMetrics(saved.id, draft.metrics);
    await ctx.onSaved();
    if (then === 'metrics') {
      ctx.go({ view: 'metrics', dsId: saved.id, nodeId: node.id });
    } else {
      // Save keeps the editor open so further edits are possible; confirm with a
      // toast and re-seed from the server copy (ids/tokens/values stay in sync).
      paramValues = saved.param_values ? { ...saved.param_values } : paramValues;
      toast('Dataset saved.', 'success');
    }
  } catch (e) {
    if (e.status === 428) {
      // Credential guard: prompt inline for the secret, then retry once.
      const input = document.getElementById('dsSecret');
      input.placeholder = 'REQUIRED — re-enter secret';
      input.style.borderColor = 'var(--ng-danger)';
      input.focus();
      toast(e.message, 'error');
    } else {
      toast(e.message, 'error');
    }
  }
}

// Create a new dataset's metrics (and their rules) from the draft, keeping any
// [Param] tokens intact for backend resolution. Only used on create — editing
// metrics happens on the Metrics/Rules pages.
async function createTemplateMetrics(datasetId, metrics) {
  for (const m of metrics) {
    const mid = (await api.createMetric(datasetId, {
      name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast, unit: m.unit,
    })).id;
    for (const r of m.rules) {
      await api.createRule(mid, { op: r.op, value: r.value, status_level_id: r.status_level_id, priority: r.priority });
    }
  }
}

// Snapshot the current draft as a reusable dataset template. [Param] tokens are
// preserved verbatim (only {{node.*}} mustaches were resolved when a template
// was applied), so each future apply re-fills them per node.
async function saveAsTemplate(node) {
  const values = validateParams();
  if (!values) return;
  collectDraft();
  const name = prompt('Template name:');
  if (!name) return;
  const payload = {
    network_resource: node.network_resource,
    name: draft.name, method: draft.method, script: draft.script,
    timeout_s: draft.timeout_s, interval_s: draft.interval_s,
    metrics: draft.metrics.map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast, unit: m.unit, rules: m.rules.map((r) => ({ op: r.op, value: r.value, status_level_id: r.status_level_id, priority: r.priority })) })),
  };
  try { await api.saveTemplate({ name, template_type: 'dataset', payload }); toast('Template saved.'); }
  catch (e) { toast(e.message, 'error'); }
}
