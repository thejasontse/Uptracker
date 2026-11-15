// Credentials — inline dock view. Secrets are write-only (never returned).
import { api } from './api.js';
import { esc, toast } from './util.js';

export function renderCredentialsView(body, ctx) {
  body.innerHTML = `
  <div class="section">
    <h2>Credentials</h2>
    <p class="muted">Secrets are encrypted at rest and never shown. Re-enter a secret whenever a
      dataset's script changes.</p>
    <div id="credList"></div>
  </div>
  <div class="section">
    <h3>Add credential</h3>
    <label class="f">Name</label><input id="cName">
    <label class="f">Username</label><input id="cUser">
    <label class="f">Secret</label><input id="cSecret" type="password" autocomplete="new-password">
    <div class="row"><button id="cAdd" class="primary small">Add</button></div>
  </div>`;
  const list = body.querySelector('#credList');

  async function repaint() {
    const { credentials } = await api.listCredentials();
    const flagged = credentials.filter((c) => c.needs_rekey);
    list.innerHTML = (flagged.length
      ? `<p class="err">${flagged.length} imported credential${flagged.length > 1 ? 's' : ''} cannot be
          decrypted with this server's key. Re-enter ${flagged.length > 1 ? 'their' : 'its'} secret to re-encrypt.</p>`
      : '') + (credentials.length
      ? credentials.map((c) => `<div class="card${c.needs_rekey ? ' needs-rekey' : ''}">
          <div class="head"><span class="t">${esc(c.name)}</span>
            <span class="muted">${esc(c.username || '')}</span>
            ${c.needs_rekey ? '<span class="pill" style="background:#d99400">needs re-key</span>' : ''}
            <button class="small" data-rekey="${c.id}" data-name="${esc(c.name)}" data-user="${esc(c.username || '')}">Re-enter secret</button>
            <button class="small danger" data-del="${c.id}">Delete</button></div></div>`).join('')
      : '<p class="muted">No credentials stored.</p>');
    list.querySelectorAll('[data-del]').forEach((b) => b.onclick = async () => {
      if (!confirm('Delete credential? Datasets using it will lose access.')) return;
      try { await api.deleteCredential(Number(b.dataset.del)); await repaint(); if (ctx.onChange) ctx.onChange(); }
      catch (e) { toast(e.message, 'error'); }
    });
    list.querySelectorAll('[data-rekey]').forEach((b) => b.onclick = async () => {
      const secret = prompt(`Enter a new secret for "${b.dataset.name}" (re-encrypts with this server's key):`);
      if (secret == null || secret === '') return;
      try {
        await api.updateCredential(Number(b.dataset.rekey), { name: b.dataset.name, username: b.dataset.user, secret });
        await repaint();
        if (ctx.onChange) ctx.onChange();
      } catch (e) { toast(e.message, 'error'); }
    });
  }

  body.querySelector('#cAdd').onclick = async () => {
    const name = body.querySelector('#cName').value.trim();
    const username = body.querySelector('#cUser').value;
    const secret = body.querySelector('#cSecret').value;
    if (!name || !secret) { toast('Name and secret are required.', 'error'); return; }
    try {
      await api.createCredential({ name, username, secret });
      body.querySelector('#cName').value = '';
      body.querySelector('#cSecret').value = '';
      await repaint();
      if (ctx.onChange) ctx.onChange();
    } catch (e) { toast(e.message, 'error'); }
  };
  repaint();
}
