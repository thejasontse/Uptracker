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
  scanParams, paramValueMap, renderMustaches, customParamValues,
  paintParamFields, attachParamAutocomplete,
} from './params.js';
import { showTestResult } from './dockTerminal.js';

const METHODS = ['bash', 'cmd', 'powershell'];

// Schedule units offered by the "Every <n> <unit>" row; seconds stored in the DB.
const INTERVAL_UNITS = [
  { id: 'seconds', label: 'seconds', mult: 1 },
  { id: 'minutes', label: 'minutes', mult: 60 },
  { id: 'hours', label: 'hours', mult: 3600 },
  { id: 'days', label: 'days', mult: 86400 },
];

// Decompose a stored interval_s into { value, unit } for the form. Prefers the
// unit recorded by the server (draft.interval_unit) when it divides evenly;
// otherwise the largest unit that does (fallback: seconds).
function splitInterval(intervalS, preferredUnit) {
  const n = Math.max(0, intervalS || 0);
  if (preferredUnit) {
    const u = INTERVAL_UNITS.find((x) => x.id === preferredUnit);
    if (u && n > 0 && n % u.mult === 0) return { value: n / u.mult, unit: u.id };
  }
  for (const u of [...INTERVAL_UNITS].reverse()) {
    if (n > 0 && n % u.mult === 0) return { value: n / u.mult, unit: u.id };
  }
  return { value: n || 1, unit: 'seconds' };
}

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

// How the schedule reads in the dataset editor's summary line: on-demand
// datasets (interval 0 / mode "none") never run from the timer.
export function scheduleLabel(ds) {
  if (!ds.interval_s || ds.schedule_mode === 'none') return 'on demand';
  const n = ds.interval_s;
  const unit = ds.interval_unit || 'seconds';
  const mult = { seconds: 1, minutes: 60, hours: 3600, days: 86400 }[unit] || 1;
  if (unit === 'seconds' || n % mult !== 0) return `every ${n}s`;
  const count = n / mult;
  // `unit` is stored plural ("minutes", "hours", "days"); singularize for 1.
  return `every ${count} ${count === 1 && unit.endsWith('s') ? unit.slice(0, -1) : unit}`;
}

// Timeout summary: 0 means the script runs without a time limit.
export function timeoutLabel(ds) {
  return ds.timeout_s > 0 ? `${ds.timeout_s}s` : 'none';
}

export function renderDatasetEditor(body, node, ds, ctx) {
  draft = ds
    ? JSON.parse(JSON.stringify({ ...ds, metrics: ds.metrics.map((m) => ({ ...m, rules: [...m.rules] })) }))
    : { id: null, name: 'New dataset', method: 'bash', script: '', timeout_s: 30, interval_s: 600, hidden: false, schedule_mode: 'every', interval_unit: 'seconds', credential_id: null, credential: null, metrics: [] };
  draft.secret = null;
  editorNode = node;
  tplActive = null;
  // Repopulate saved [Param] values (stored raw on the dataset) so re-opening
  // an existing dataset shows the last-entered values, not blanks.
  paramValues = ds && ds.param_values ? { ...ds.param_values } : {};

  // Seed the 4-field schedule row from the stored config: mode dropdown, then
  // value + unit (only live for "Every"), then timeout (0 = no limit).
  const schedMode = ds && ds.interval_s === 0 ? 'none' : (ds && ds.schedule_mode) || 'every';
  draft.schedule_mode = schedMode;
  const split = splitInterval(draft.interval_s, draft.interval_unit);

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
      <div class="head"><span class="t">Parameters</span></div>
      <div id="tplParamFields"></div>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f">Credential</label><select id="dsCred"></select></div>
      <div style="flex:1"><label class="f">Secret</label><input id="dsSecret" type="password" placeholder="re-enter to save" autocomplete="new-password"></div>
    </div>
    <div class="row"><button id="btnTest" class="small">Test run</button></div>
    <hr class="sec">
    <div class="row">
      <div style="flex:1"><label class="f">Schedule</label><select id="dsScheduleMode">
        <option value="none"${schedMode === 'none' ? ' selected' : ''}>Not scheduled</option>
        <option value="every"${schedMode === 'every' ? ' selected' : ''}>Every</option>
      </select></div>
      <div style="flex:1"><label class="f">Value</label><input id="dsIntervalValue" type="number" min="1" step="1" value="${schedMode === 'none' ? '' : split.value}"${schedMode === 'none' ? ' disabled' : ''}></div>
      <div style="flex:1"><label class="f">Unit</label><select id="dsIntervalUnit"${schedMode === 'none' ? ' disabled' : ''}>
        ${INTERVAL_UNITS.map((u) => `<option value="${u.id}"${schedMode !== 'none' && u.id === split.unit ? ' selected' : ''}>${u.label}</option>`).join('')}
      </select></div>
    </div>
    <div class="row">
      <div style="flex:0 0 auto;width:8em"><label class="f">Timeout (s)</label><input id="dsTimeout" type="number" min="0" step="1" value="${draft.timeout_s}"></div>
      <div style="flex:1;display:flex;align-items:center;gap:6px;padding-top:20px">
        <input id="dsHidden" type="checkbox" ${draft.hidden ? 'checked' : ''}>
        <label for="dsHidden" style="cursor:pointer">Hide on dashboard</label>
      </div>
    </div>
    <p class="muted">Timeout <code>0</code> runs the script with no time limit. <b>Not scheduled</b> datasets never run on a timer — collect them with <b>Run</b>.</p>
    <hr class="sec">
    <p class="muted" id="hiddenHint"${draft.hidden ? '' : ' hidden'}>Hidden datasets are not shown on the dashboard and are skipped by its ⟳ node refresh. They still collect on their schedule — run them from here anytime.</p>
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
  // Mode switch: "Not scheduled" blanks the value and greys out both fields
  // (interval_s 0). Switching back to "Every" restores a usable default.
  body.querySelector('#dsScheduleMode').onchange = (e) => {
    const off = e.target.value === 'none';
    const valEl = body.querySelector('#dsIntervalValue');
    const unitEl = body.querySelector('#dsIntervalUnit');
    if (off) {
      valEl.dataset.restore = valEl.value || String(split.value);
      unitEl.dataset.restore = unitEl.value || split.unit;
      valEl.value = '';
    } else {
      valEl.value = valEl.dataset.restore || '1';
      unitEl.value = unitEl.dataset.restore || 'seconds';
    }
    valEl.disabled = off;
    unitEl.disabled = off;
  };
  // Hidden hint appears the moment the box is checked.
  body.querySelector('#dsHidden').onchange = (e) => {
    document.getElementById('hiddenHint').hidden = !e.target.checked;
  };
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
      status_level_id: Number(r.status_level_id ?? defaultLevelId()), priority: r.priority ?? 0,
    })),
  }));
  document.getElementById('dsName').value = draft.name;
  document.getElementById('dsMethod').value = draft.method;
  document.getElementById('dsScript').value = draft.script;
  document.getElementById('dsTimeout').value = draft.timeout_s;
  // Re-seed the schedule row from the template's interval (mode: none when 0).
  const mode = draft.interval_s <= 0 ? 'none' : 'every';
  draft.schedule_mode = mode;
  const split = splitInterval(draft.interval_s, p.interval_unit);
  const modeEl = document.getElementById('dsScheduleMode');
  modeEl.value = mode;
  const valEl = document.getElementById('dsIntervalValue');
  const unitEl = document.getElementById('dsIntervalUnit');
  // "Not scheduled" blanks the value and greys both fields (see render).
  valEl.value = mode === 'none' ? '' : split.value;
  unitEl.value = mode === 'none' ? '' : split.unit;
  valEl.disabled = mode === 'none';
  unitEl.disabled = mode === 'none';
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
  // Timeout: blank/invalid falls back to 30; 0 is a real value (no limit).
  const tmo = Number(document.getElementById('dsTimeout').value);
  draft.timeout_s = Number.isFinite(tmo) && tmo >= 0 ? Math.floor(tmo) : 30;
  // Schedule: "Not scheduled" stores 0; "Every" multiplies value × unit.
  draft.schedule_mode = document.getElementById('dsScheduleMode').value;
  if (draft.schedule_mode === 'none') {
    draft.interval_s = 0;
  } else {
    const u = INTERVAL_UNITS.find((x) => x.id === document.getElementById('dsIntervalUnit').value) || INTERVAL_UNITS[0];
    const v = Math.max(1, Math.floor(Number(document.getElementById('dsIntervalValue').value)) || 1);
    draft.interval_unit = u.id;
    draft.interval_s = v * u.mult;
  }
  // The enabled checkbox is gone: "Not scheduled" covers pausing, and the API
  // default keeps every dataset collectable via Run.
  draft.hidden = document.getElementById('dsHidden').checked;
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
// actually referenced by the draft. Node-derived params are excluded (they
// resolve live from the node at execution time), so only genuine custom tokens
// are persisted verbatim for the backend to substitute at collection time.
function paramValuesPayload() {
  return customParamValues(draftParamNames(), paramValues);
}

async function testRun() {
  if (!validateParams()) return;
  // The dry-run endpoint resolves [Param] tokens server-side (node-derived
  // params + the saved values), so send the raw script/regex as stored — the
  // same canonical path the Metrics and Rules pages use. Results go to the
  // fixed terminal at the bottom of the dock.
  try {
    const res = await api.testRun({
      method: draft.method, script: draft.script, timeout_s: draft.timeout_s,
      credential_id: draft.credential_id, secret: draft.secret,
      node_id: editorNode ? editorNode.id : state.selectedId, param_values: paramValues,
      metrics: (draft.metrics || []).map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast })),
    });
    showTestResult(res, 'dataset editor');
  } catch (e) {
    showTestResult({ ok: false, error: e.message, raw: '', parsed: [] }, 'dataset editor');
  }
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
    timeout_s: draft.timeout_s, interval_s: draft.interval_s,
    hidden: draft.hidden, schedule_mode: draft.schedule_mode, interval_unit: draft.interval_unit,
    credential_id: draft.credential_id, secret: draft.secret,
    param_values: paramValuesPayload(),
  };
  // Branch on draft.id, not the `ds` closure arg: after a create the editor
  // stays open with ds still null, so a second Save must PATCH the row we just
  // made — otherwise it INSERTs a duplicate dataset.
  const isCreate = !draft.id;
  // Guard against double-clicking Save (or Save then Add Metrics) while the
  // first request is still in flight, which also created duplicates.
  const btnSave = document.getElementById('btnSave');
  const btnMetrics = document.getElementById('btnMetrics');
  if (btnSave) btnSave.disabled = true;
  if (btnMetrics) btnMetrics.disabled = true;
  try {
    const saved = isCreate ? await api.createDataset(node.id, body) : await api.patchDataset(draft.id, body);
    draft.id = saved.id;
    if (isCreate) await createTemplateMetrics(saved.id, draft.metrics);
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
  } finally {
    if (btnSave) btnSave.disabled = false;
    if (btnMetrics) btnMetrics.disabled = false;
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
    name: draft.name, method: draft.method, script: draft.script,
    timeout_s: draft.timeout_s, interval_s: draft.interval_s,
    interval_unit: draft.interval_unit,
    metrics: draft.metrics.map((m) => ({ name: m.name, regex: m.regex, group_index: m.group_index, cast: m.cast, unit: m.unit, rules: m.rules.map((r) => ({ op: r.op, value: r.value, status_level_id: r.status_level_id, priority: r.priority })) })),
  };
  try { await api.saveTemplate({ name, template_type: 'dataset', payload }); toast('Template saved.'); }
  catch (e) { toast(e.message, 'error'); }
}
