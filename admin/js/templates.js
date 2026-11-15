// Templates — inline dock view: list dataset templates, apply to a node, delete.
import { api } from './api.js';
import { esc, toast } from './util.js';

export function renderTemplatesView(body, node, ctx) {
  body.innerHTML = `
  <div class="section">
    <div class="row"><button data-nav="node" data-id="${esc(node.id)}" class="ghost small">←</button>
      <h2 style="flex:1">Templates</h2></div>
    <p class="muted">Apply a saved dataset template to <b>${esc(node.label)}</b>. Placeholders like
      <code>{{node.label}}</code> and <code>{{node.network_path}}</code> resolve from the target node;
      <code>[Parameter]</code> tokens become fillable fields in the dataset editor.</p>
    <div id="tplList" class="muted">Loading…</div>
  </div>`;
  const list = body.querySelector('#tplList');

  async function repaint() {
    const { templates } = await api.listTemplates('dataset');
    if (!templates.length) { list.innerHTML = '<p class="muted">No templates saved yet. Use “Save as template” in a dataset editor.</p>'; return; }
    list.innerHTML = templates.map((t) => `
      <div class="card">
        <div class="head"><span class="t">${esc(t.name)}</span>
          <span class="muted">${esc(t.payload.method || '')}</span>
          <button class="primary small" data-apply="${t.id}">Apply</button>
          <button class="small danger" data-del="${t.id}">×</button></div>
        <div class="kv"><span class="k">metrics</span><span>${(t.payload.metrics || []).map((m) => esc(m.name)).join(', ') || '—'}</span></div>
      </div>`).join('');
    list.querySelectorAll('[data-apply]').forEach((b) => {
      b.onclick = () => {
        // Unified flow: open the new-dataset editor with the template
        // preselected so [Param] tokens can be filled before saving.
        ctx.go({ view: 'dataset', dsId: null, nodeId: node.id, tplId: Number(b.dataset.apply) });
      };
    });
    list.querySelectorAll('[data-del]').forEach((b) => {
      b.onclick = async () => {
        if (!confirm('Delete template?')) return;
        try { await api.deleteTemplate(Number(b.dataset.del)); await repaint(); }
        catch (e) { toast(e.message, 'error'); }
      };
    });
  }
  repaint();
}
