// Thin JSON API client. All calls return parsed JSON or throw Error(detail).
// URLs are RELATIVE (no leading slash) so the app works both when served
// directly (http://host:8090/) and through code-server's port proxy
// (http://host:8080/proxy/8090/). The <base> fixer in index.html anchors them.
async function req(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const api = {
  snapshot: () => req('GET', 'api/snapshot'),
  putGraph: (model) => req('PUT', 'api/graph', { model }),
  patchNode: (id, p) => req('PATCH', `api/nodes/${id}`, p),

  createDataset: (nodeId, b) => req('POST', `api/nodes/${nodeId}/datasets`, b),
  patchDataset: (id, b) => req('PATCH', `api/datasets/${id}`, b),
  deleteDataset: (id) => req('DELETE', `api/datasets/${id}`),
  runDataset: (id) => req('POST', `api/datasets/${id}/run`),
  testRun: (b) => req('POST', 'api/test/run', b),

  createMetric: (dsId, b) => req('POST', `api/datasets/${dsId}/metrics`, b),
  patchMetric: (id, b) => req('PATCH', `api/metrics/${id}`, b),
  deleteMetric: (id) => req('DELETE', `api/metrics/${id}`),
  createRule: (metricId, b) => req('POST', `api/metrics/${metricId}/rules`, b),
  deleteRule: (id) => req('DELETE', `api/rules/${id}`),
  upsertAlert: (metricId, b) => req('PUT', `api/metrics/${metricId}/alert`, b),
  deleteAlert: (metricId) => req('DELETE', `api/metrics/${metricId}/alert`),

  listCredentials: () => req('GET', 'api/credentials'),
  createCredential: (b) => req('POST', 'api/credentials', b),
  updateCredential: (id, b) => req('PUT', `api/credentials/${id}`, b),
  deleteCredential: (id) => req('DELETE', `api/credentials/${id}`),

  listTemplates: (templateType) => req('GET', 'api/templates' + (templateType ? `?template_type=${templateType}` : '')),
  saveTemplate: (b) => req('POST', 'api/templates', b),
  deleteTemplate: (id) => req('DELETE', `api/templates/${id}`),
  applyTemplate: (id, nodeId, params) => req('POST', `api/templates/${id}/apply`, { node_id: nodeId, params: params || {} }),

  levels: () => req('GET', 'api/status-levels'),
  settings: () => req('GET', 'api/settings'),
  putSettings: (values) => req('PUT', 'api/settings', { values }),

  query: (sql) => req('POST', 'api/query', { sql }),
  queryColumns: () => req('GET', 'api/query/columns'),
  listSavedQueries: () => req('GET', 'api/saved-queries'),
  saveQuery: (name, sql) => req('POST', 'api/saved-queries', { name, sql }),
  deleteSavedQuery: (id) => req('DELETE', `api/saved-queries/${id}`),

  importArchive: (zipB64) => req('POST', 'api/import', { zip_b64: zipB64 }),
  exportUrl: 'api/export',
};
