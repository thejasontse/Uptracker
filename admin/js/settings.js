// Settings — inline dock view: polling, scheduler, and SMTP.
// Status levels are a fixed vocabulary (good/warning/critical/unknown) chosen
// by rules, so there is nothing to edit here.
import { api } from './api.js';
import { state } from './state.js';
import { esc, toast } from './util.js';

export function renderSettingsView(body, ctx) {
  body.innerHTML = `
  <div class="section">
    <h2>Settings</h2>
    <div id="setBody" class="muted">Loading…</div>
  </div>`;
  const host = body.querySelector('#setBody');

  api.settings().then(async (s) => {
    host.classList.remove('muted');
    // Which statuses count toward "fire after" (default warning + critical).
    const allStatuses = (state.snapshot?.status_levels || []).map((l) => l.name);
    let defStatuses;
    try { defStatuses = JSON.parse(s.alert_default_trigger_statuses); } catch { defStatuses = null; }
    if (!Array.isArray(defStatuses) || !defStatuses.length) defStatuses = ['warning', 'critical'];
    const optStatuses = (allStatuses.length ? allStatuses : ['good', 'warning', 'critical', 'unknown'])
      .map((n) => `<option value="${esc(n)}" ${defStatuses.includes(n) ? 'selected' : ''}>${esc(n)}</option>`).join('');
    host.innerHTML = `
      <label class="f">Dashboard refresh interval (s)</label><input id="s_poll" type="number" value="${esc(s.poll_interval_s || 15)}">
      <div class="row"><label class="toggle"><input id="s_sched" type="checkbox" ${s.scheduler_enabled === '1' ? 'checked' : ''}> Scheduler enabled</label></div>
      <div class="row"><label class="toggle"><input id="s_theme" type="checkbox" ${s.theme === 'dark' ? 'checked' : ''}> Dark mode (default for all views)</label></div>

      <h3>Email (SMTP)</h3>
      <div class="row"><div style="flex:2"><label class="f">Host</label><input id="s_host" value="${esc(s.smtp_host || '')}"></div>
        <div style="flex:1"><label class="f">Port</label><input id="s_port" value="${esc(s.smtp_port || 587)}"></div></div>
      <div class="row"><div style="flex:1"><label class="f">User</label><input id="s_user" value="${esc(s.smtp_user || '')}"></div>
        <div style="flex:1"><label class="f">Password</label><input id="s_pass" type="password" placeholder="${s.smtp_password ? '••••••' : ''}" autocomplete="new-password"></div></div>
      <div class="row"><label class="toggle"><input id="s_tls" type="checkbox" ${s.smtp_starttls === '1' ? 'checked' : ''}> STARTTLS</label></div>
      <label class="f">From address</label><input id="s_from" value="${esc(s.mail_from || '')}">

      <h3>Email alert defaults</h3>
      <p class="muted">Pre-fill new email alerts. Subject/body are templates —
        <code>{{node.label}}</code> <code>{{metric.name}}</code> <code>{{status}}</code> <code>{{value}}</code>.</p>
      <label class="f">Default recipients (comma separated)</label>
      <input id="s_adr" value="${esc(s.alert_default_recipients || '')}">
      <label class="f">Default subject</label>
      <input id="s_asubj" value="${esc(s.alert_default_subject || '')}">
      <label class="f">Default body</label>
      <textarea id="s_abody" style="min-height:48px">${esc(s.alert_default_body || '')}</textarea>
      <div class="row">
        <div style="flex:1"><label class="f">Cooldown (s)</label><input id="s_acool" type="number" value="${esc(s.alert_default_cooldown_s || '3600')}"></div>
        <div style="flex:1"><label class="f">Fire after (runs)</label><input id="s_atrig" type="number" min="1" value="${esc(s.alert_default_trigger_count || '1')}"></div>
      </div>
      <div class="row">
        <div style="flex:1"><label class="f">Fire after counts these statuses</label>
          <select id="s_astatuses" multiple size="4">${optStatuses}</select></div>
      </div>

      <div class="row" style="margin-top:12px"><button id="save" class="primary small">Save</button></div>`;

    // Live theme toggle: flipping the checkbox re-themes the page immediately
    // (the graph.js observer persists it to the global setting).
    host.querySelector('#s_theme').addEventListener('change', (e) => {
      document.getElementById('canvas').classList.toggle('dark', e.target.checked);
    });

    host.querySelector('#save').onclick = async () => {
      const values = {
        poll_interval_s: host.querySelector('#s_poll').value,
        scheduler_enabled: host.querySelector('#s_sched').checked ? '1' : '0',
        theme: host.querySelector('#s_theme').checked ? 'dark' : 'light',
        smtp_host: host.querySelector('#s_host').value,
        smtp_port: host.querySelector('#s_port').value,
        smtp_user: host.querySelector('#s_user').value,
        smtp_starttls: host.querySelector('#s_tls').checked ? '1' : '0',
        mail_from: host.querySelector('#s_from').value,
        alert_default_recipients: host.querySelector('#s_adr').value,
        alert_default_subject: host.querySelector('#s_asubj').value,
        alert_default_body: host.querySelector('#s_abody').value,
        alert_default_cooldown_s: host.querySelector('#s_acool').value,
        alert_default_trigger_count: host.querySelector('#s_atrig').value,
        alert_default_trigger_statuses: JSON.stringify([...host.querySelector('#s_astatuses').selectedOptions].map((o) => o.value)),
      };
      const pass = host.querySelector('#s_pass').value;
      if (pass) values.smtp_password = pass;
      try { await api.putSettings(values); toast('Saved.'); if (ctx.onChange) ctx.onChange(); }
      catch (e) { toast(e.message, 'error'); }
    };
  });
}
