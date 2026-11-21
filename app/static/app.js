'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let timelineHours = 24;
let settings = null;
let lastStatus = null;
let updating = false;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Erreur ${r.status}`);
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });

function toast(msg, bad = false) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

function confirmBox(html) {
  return new Promise((resolve) => {
    const d = $('confirm');
    $('confirmText').innerHTML = html;
    const done = (v) => { d.close(); resolve(v); };
    $('confirmYes').onclick = () => done(true);
    $('confirmNo').onclick = () => done(false);
    d.showModal();
  });
}

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString('fr-FR', {
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString('fr-FR', {
  weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const fmtSize = (b) => b > 1e9 ? `${(b / 1e9).toFixed(2)} Go` : `${Math.round(b / 1e6)} Mo`;
function ago(ts) {
  if (!ts) return 'jamais';
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `il y a ${s} s`;
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  return `il y a ${Math.round(s / 86400)} j`;
}
function duration(sec) {
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  return d ? `${d} j ${h} h` : `${h} h ${Math.floor((sec % 3600) / 60)} min`;
}

// ================================================================== statut
function tile(name, ok, value, sub) {
  const cls = ok === null || ok === undefined ? '' : ok === 'warn' ? 'warn' : ok ? 'ok' : 'bad';
  return `<div class="tile"><div class="name"><span class="dot ${cls}"></span>${esc(name)}</div>
    <div class="value">${esc(value)}</div><div class="sub">${esc(sub || '')}</div></div>`;
}

function renderBanner(s) {
  const ha = s.ha || {};
  const wd = s.watchdog;
  const banner = $('banner');
  let cls = '', dot = '', title, sub = '';
  if (!s.ha) {
    title = 'Premier contrôle en cours…';
  } else if (ha.ok) {
    cls = ha.slow ? 'warn' : 'ok'; dot = cls;
    title = ha.slow ? 'Home Assistant répond lentement' : 'Home Assistant fonctionne';
    sub = `${ha.location || ''} · version ${ha.version || '?'} · ${ha.latency_ms} ms`;
    const extra = Object.values(s.alerts || {});
    if (extra.length) { cls = 'warn'; dot = 'warn'; sub += ` · ⚠️ ${extra.join(' · ')}`; }
  } else {
    const hostUp = s.host && s.host.ok;
    cls = hostUp ? 'warn' : 'bad'; dot = cls;
    title = hostUp ? 'Home Assistant ne répond pas (mini-PC joignable)' : 'Mini-PC injoignable';
    const since = wd.incident_since ? ` · depuis ${fmtTime(wd.incident_since)}` : '';
    sub = `${ha.error || ''} · ${wd.consecutive_failures} échec(s) consécutif(s)${since}`;
  }
  if (wd.maintenance_until) {
    cls = 'maint';
    sub = `🔧 Maintenance jusqu'à ${fmtTime(wd.maintenance_until)} — aucune action ni alerte · ` + sub;
  }
  if (wd.busy) sub += ` · ⏳ ${wd.busy}`;
  banner.className = 'banner ' + cls;
  $('bannerDot').className = 'dot lg ' + dot;
  $('bannerTitle').textContent = title;
  $('bannerSub').textContent = sub;
}

function renderTiles(s) {
  const ha = s.ha || {};
  let html = tile('Home Assistant', s.ha ? (ha.ok ? (ha.slow ? 'warn' : true) : false) : null,
    ha.ok ? (ha.slow ? 'Lent' : 'En ligne') : (s.ha ? 'Hors ligne' : '…'),
    ha.ok ? `v${ha.version} · ${ha.latency_ms} ms` : ha.error);
  const sys = s.system || {};
  html += tile('Mini-PC', s.host ? s.host.ok : null,
    s.host ? (s.host.ok ? 'Joignable' : 'Injoignable') : '…',
    sys.uptime_s ? `allumé depuis ${duration(sys.uptime_s)}` : s.ha_host);
  if (s.ssh !== null) {
    html += tile('SSH', s.ssh ? s.ssh.ok : null, s.ssh ? (s.ssh.ok ? 'Port ouvert' : 'Fermé') : '…', 'add-on SSH');
  }
  if (s.slzb) {
    html += tile('SLZB-06', s.slzb.ok, s.slzb.ok ? 'En ligne' : 'Hors ligne',
      s.slzb.ok ? `${s.slzb.latency_ms} ms` : s.slzb.error);
  }
  if (s.sentinels && s.sentinels.length) {
    const bad = s.sentinels.filter((x) => !x.ok);
    html += tile('Capteurs témoins', bad.length === 0, `${s.sentinels.length - bad.length} / ${s.sentinels.length} OK`,
      bad.length ? `muets : ${bad.map((b) => b.name || b.entity_id).join(', ')}` : 'Zigbee actif');
  }
  if (s.plug) {
    const p = s.plug;
    html += tile('Prise du mini-PC', p.ok ? p.on : false, p.ok ? (p.on ? 'Allumée' : 'Éteinte') : 'Injoignable',
      p.ok ? (p.power != null ? `${Number(p.power).toFixed(1)} W` : s.plug_type) : p.error);
  }
  if (sys.disk_percent != null) {
    const lim = settings ? settings.settings.disk_alert_percent : 85;
    html += tile('Disque', sys.disk_percent < lim ? true : 'warn', `${sys.disk_percent} %`,
      `${sys.disk_free_gb} Go libres sur ${sys.disk_total_gb}`);
  }
  if (sys.mem_percent != null) {
    html += tile('Mémoire', sys.mem_percent < 90 ? true : 'warn', `${sys.mem_percent} %`,
      `${sys.mem_total_gb} Go · charge ${sys.load1 ?? '?'}${sys.cpus ? ` / ${sys.cpus} cœurs` : ''}`);
  }
  if (sys.temp_c != null) {
    const lim = settings ? settings.settings.temp_alert_celsius : 80;
    html += tile('Température', sys.temp_c < lim ? true : 'warn', `${sys.temp_c} °C`, `alerte à ${lim} °C`);
  }
  if (sys.error) html += tile('Santé du mini-PC', false, 'Indisponible', sys.error);
  $('tiles').innerHTML = html;
}

function renderWatchdog(s) {
  const wd = s.watchdog;
  $('wdToggle').checked = wd.enabled;
  $('wdLabel').textContent = wd.enabled ? 'Interventions automatiques activées'
    : 'Interventions automatiques désactivées (alertes seulement)';
  const m = $('maint');
  m.classList.toggle('on', !!wd.maintenance_until);
  $('maintText').textContent = wd.maintenance_until
    ? `🔧 En maintenance jusqu'à ${fmtTime(wd.maintenance_until)}` : 'Maintenance :';
  $('btnMaintEnd').hidden = !wd.maintenance_until;
  if (!settings) return;
  const st = settings.settings;
  const steps = [
    ['wol', 'Réveiller le mini-PC (WoL), s\'il est injoignable', st.fail_core_restart, s.wol],
    ['ssh_core_restart', 'Redémarrer HA Core (SSH)', st.fail_core_restart, s.ssh !== null],
    ['ssh_host_reboot', 'Redémarrer le mini-PC (SSH)', st.fail_host_reboot, s.ssh !== null],
    ['plug_cycle', 'Couper / rallumer la prise', st.fail_power_cycle, !!s.plug_type],
  ].filter((x) => x[0] !== 'wol' || x[3]);
  const mins = (n) => Math.round(n * s.check_interval / 60);
  $('ladder').innerHTML = steps.map(([k, label, n, avail]) => {
    const done = wd.steps_done.includes(k);
    return `<li class="${done ? 'done' : ''}"><span>${done ? '✔︎ ' : ''}${esc(label)}</span>
      <span class="muted">${avail ? `après ${n} échecs (~${mins(n)} min)` : 'non configuré'}</span></li>`;
  }).join('');
  const parts = [];
  if (wd.grace_remaining > 0) parts.push(`Période de grâce : <b>${Math.ceil(wd.grace_remaining / 60)} min</b>`);
  if (wd.last_action) parts.push(`Dernière action : <b>${esc(wd.last_action)}</b> ${ago(wd.last_action_ts)}`);
  parts.push(`Échecs consécutifs : <b>${wd.consecutive_failures}</b>`);
  $('wdInfo').innerHTML = parts.join(' · ');
}

function renderHaUpdates(s) {
  const u = s.ha_updates;
  const box = $('haUpdates');
  if (u.installing) {
    box.innerHTML = `<div class="row progress">⏳ Installation en cours : ${esc(u.installing)}</div>`;
    return;
  }
  if (u.error) { box.innerHTML = `<div class="empty">Erreur : ${esc(u.error)}</div>`; return; }
  if (!u.checked) { box.innerHTML = '<div class="empty">Pas encore vérifié</div>'; return; }
  if (!u.items.length) {
    box.innerHTML = `<div class="empty">✔︎ Tout est à jour (vérifié ${ago(u.checked)})</div>`;
    return;
  }
  box.innerHTML = u.items.map((x) => `<div class="row"><div class="grow"><b>${esc(x.title)}</b>
      <div class="muted">${esc(x.installed || '?')} → ${esc(x.latest || '?')}
      ${x.release_url ? ` · <a href="${esc(x.release_url)}" target="_blank" rel="noopener">notes</a>` : ''}</div></div>
      <button class="small" data-update="${esc(x.entity_id)}" ${x.in_progress ? 'disabled' : ''}>
        ${x.in_progress ? 'En cours…' : 'Installer'}</button></div>`).join('');
}

function renderBackupState(s) {
  const b = s.backup;
  const parts = [];
  if (b.running) parts.push(`<span class="progress">⏳ ${esc(b.step || 'En cours')}…</span>`);
  else if (b.last_result) {
    parts.push(b.last_result.ok
      ? `✔︎ Dernière sauvegarde ${ago(b.last_result.ts)}`
      : `<span style="color:var(--bad)">✖ Échec ${ago(b.last_result.ts)} : ${esc(b.last_result.message)}</span>`);
  }
  parts.push(b.enabled ? 'Automatique : activée' : 'Automatique : désactivée (voir Réglages)');
  $('backupInfo').innerHTML = parts.join(' · ');
  $('btnBackup').disabled = b.running || (s.ssh === null);
}

function renderAppUpdate(s) {
  const a = s.app;
  $('versionBadge').textContent = `v${a.version}${a.sha && a.sha !== 'dev' ? ' · ' + a.sha.slice(0, 7) : ''}`;
  const show = a.available && a.enabled;
  $('updateBanner').hidden = !show && !a.updating && !updating;
  if (!show) return;
  $('updateSummary').textContent = `v${a.version} → v${a.latest_version} — voir les nouveautés`;
  $('updateCommits').innerHTML = a.releases.map((r) =>
    `<li><b>v${esc(r.version)}</b>${r.date ? ` <span class="muted">(${esc(new Date(r.date).toLocaleDateString('fr-FR'))})</span>` : ''}
      ${r.notes.length ? `<ul>${r.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}</li>`).join('');
  $('btnAppUpdate').disabled = a.updating || updating;
  $('btnAppUpdate').textContent = (a.updating || updating) ? 'Mise à jour…' : 'Mettre à jour';
}

function renderStatus(s) {
  lastStatus = s;
  $('lastCheck').textContent = s.last_check ? `Contrôle ${ago(s.last_check)}` : 'Premier contrôle…';
  renderBanner(s);
  renderTiles(s);
  $('up24').textContent = s.uptime['24h'] != null ? `${s.uptime['24h']} %` : '—';
  $('up7').textContent = s.uptime['7j'] != null ? `${s.uptime['7j']} %` : '—';
  renderWatchdog(s);
  renderHaUpdates(s);
  renderBackupState(s);
  renderAppUpdate(s);
}

// ================================================================== timeline / journal
async function loadTimeline() {
  const data = await api(`/api/timeline?hours=${timelineHours}`);
  $('timeline').innerHTML = data.map((b) => {
    let cls = '';
    if (b.ratio !== null) cls = b.ratio >= 0.999 ? 'ok' : b.ratio > 0 ? 'part' : 'bad';
    const label = b.ratio === null ? 'pas de données'
      : `${Math.round(b.ratio * 100)} % disponible${b.latency ? ` · ${b.latency} ms` : ''}`;
    return `<span class="${cls}" title="${esc(fmtDateTime(b.start))} — ${esc(label)}"></span>`;
  }).join('');
  $('tlStart').textContent = timelineHours === 24 ? 'il y a 24 h' : 'il y a 7 jours';
  document.querySelectorAll('[data-hours]').forEach((b) =>
    b.classList.toggle('active', Number(b.dataset.hours) === timelineHours));
}

async function loadEvents() {
  const ev = await api('/api/events?limit=150');
  $('log').innerHTML = ev.map((e) =>
    `<div><time>${esc(fmtDateTime(e.ts))}</time><span class="${esc(e.level)}">${esc(e.message)}</span></div>`
  ).join('') || '<div><span>Aucun événement</span></div>';
}

async function loadBackups() {
  const d = await api('/api/backups');
  $('backupList').innerHTML = d.files.length
    ? d.files.slice(0, 8).map((f) => `<div class="row"><div class="grow">${esc(fmtDate(f.mtime))}
        <div class="muted">${esc(f.name)} · ${fmtSize(f.size)}</div></div>
        <a href="/api/backups/${encodeURIComponent(f.name)}" download>Télécharger</a></div>`).join('')
      + (d.files.length > 8 ? `<div class="empty">… et ${d.files.length - 8} plus anciennes dans ${esc(d.dir)}</div>` : '')
    : '<div class="empty">Aucune sauvegarde sur le NAS pour l’instant</div>';
}

// ================================================================== actions
const GROUPS = [
  ['Home Assistant (via son API)', ['ha_reload_automations', 'ha_restart', 'ha_host_reboot_api']],
  ['Par SSH (si l’interface ne répond plus)', ['ssh_core_restart', 'ssh_host_reboot']],
  ['Alimentation (si le mini-PC est figé ou éteint)', ['wol', 'plug_cycle', 'plug_off', 'plug_on']],
];
const DANGER = new Set(['ha_host_reboot_api', 'ssh_host_reboot', 'plug_cycle', 'plug_off']);

async function loadActions() {
  const list = await api('/api/actions');
  const byKey = Object.fromEntries(list.map((a) => [a.key, a]));
  $('actions').innerHTML = GROUPS.map(([title, keys]) => {
    const btns = keys.filter((k) => byKey[k] && (k !== 'wol' || byKey[k].available)).map((k) => {
      const a = byKey[k];
      return `<button class="${DANGER.has(k) ? 'danger' : ''}" data-action="${k}"
        ${a.available ? '' : 'disabled title="Non configuré dans .env"'}>${esc(a.label)}</button>`;
    }).join('');
    return `<div class="group-title">${esc(title)}</div><div class="actions">${btns}</div>`;
  }).join('');
}

$('actions').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const key = btn.dataset.action;
  let warn = `Lancer « <b>${esc(btn.textContent)}</b> » ?`;
  if (key === 'plug_off') warn += '<p>⚠️ Le mini-PC sera <b>éteint brutalement</b> et restera éteint.</p>';
  else if (key === 'plug_cycle') warn += '<p>⚠️ Coupure de courant du mini-PC : à réserver au cas où il est figé.</p>';
  if (!(await confirmBox(warn))) return;
  try {
    const r = await post(`/api/actions/${key}`);
    toast(r.message);
    setTimeout(refresh, 1500);
  } catch (err) { toast(err.message, true); }
});

document.querySelectorAll('[data-maint]').forEach((b) => b.addEventListener('click', async () => {
  try {
    await post('/api/maintenance', { minutes: Number(b.dataset.maint) });
    toast(Number(b.dataset.maint) ? 'Mode maintenance activé' : 'Surveillance normale reprise');
    refresh();
  } catch (err) { toast(err.message, true); }
}));

$('haUpdates').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-update]');
  if (!btn) return;
  const canBackup = lastStatus && lastStatus.ssh !== null;
  const ok = await confirmBox(`Installer cette mise à jour ?<p>${canBackup
    ? 'Une <b>sauvegarde</b> sera faite et copiée sur le NAS avant l’installation. '
    : ''}La surveillance passe en maintenance pendant 45 min.</p>`);
  if (!ok) return;
  try {
    const r = await post('/api/ha-updates/install', { entity_id: btn.dataset.update, backup: true });
    toast(r.message); setTimeout(refresh, 1500);
  } catch (err) { toast(err.message, true); }
});

$('btnHaUpdRefresh').addEventListener('click', async () => {
  try { await post('/api/ha-updates/refresh'); toast('Liste actualisée'); refresh(); }
  catch (err) { toast(err.message, true); }
});

$('btnBackup').addEventListener('click', async () => {
  if (!(await confirmBox('Lancer une sauvegarde complète de Home Assistant et la copier sur le NAS ?'))) return;
  try { const r = await post('/api/backups'); toast(r.message); setTimeout(refresh, 1000); }
  catch (err) { toast(err.message, true); }
});

$('btnAppUpdate').addEventListener('click', async () => {
  if (!(await confirmBox('Mettre à jour HA Guardian ?<p>L’interface sera indisponible une minute environ, puis se rechargera toute seule.</p>'))) return;
  try {
    const r = await post('/api/app/update');
    toast(r.message);
    updating = true;
    renderAppUpdate(lastStatus);
    waitForRestart();
  } catch (err) { toast(err.message, true); }
});

async function waitForRestart() {
  const before = lastStatus && lastStatus.app.sha;
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    try {
      const r = await fetch('/api/status');
      if (r.ok) {
        const s = await r.json();
        if (s.app.sha !== before) { location.reload(); return; }
        if (!s.app.updating && i > 10) { updating = false; renderStatus(s); toast('Aucune nouvelle image trouvée : voir le journal', true); return; }
      }
    } catch { /* conteneur en cours de remplacement */ }
  }
  updating = false;
}

// ================================================================== réglages
async function loadSettings() {
  settings = await api('/api/settings');
  const f = $('settingsForm');
  for (const [k, v] of Object.entries(settings.settings)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  }
  $('sshKey').value = settings.ssh_public_key;
  $('sshInfo').innerHTML = settings.ssh.enabled
    ? `Connexion : <b>${esc(settings.ssh.user)}@${esc(settings.ssh.host)}:${settings.ssh.port}</b><br>
       Empreinte mémorisée du mini-PC : <b>${esc(settings.ssh_fingerprint || 'aucune (sera mémorisée à la 1re connexion)')}</b>`
    : 'SSH désactivé (SSH_ENABLED=false)';
  $('btnTestNotif').disabled = !settings.notifications;
  $('btnReport').disabled = !settings.notifications;
  const v = settings.version;
  $('infoBox').innerHTML = [
    `Version : <b>${esc(v.version)}</b> (${esc(v.sha.slice(0, 7))})`,
    `Dépôt : <a href="https://github.com/${esc(v.repo)}" target="_blank" rel="noopener">${esc(v.repo)}</a>`,
    `Notifications : <b>${settings.notifications ? 'configurées' : 'non configurées'}</b>${settings.notification_buttons ? ' (avec boutons d’action)' : ''}`,
    `Signal de vie : <b>${settings.heartbeat ? 'actif' : 'non configuré'}</b>`,
    `Wake-on-LAN : <b>${settings.wol ? 'configuré' : 'non configuré'}</b>`,
    `Dossier des sauvegardes : <code>${esc(settings.backup_dir)}</code>`,
  ].join('<br>');
  if (lastStatus) renderStatus(lastStatus);
}

$('settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {};
  for (const el of e.target.querySelectorAll('[name]')) {
    if (el.type === 'checkbox') body[el.name] = el.checked;
    else if (el.type === 'number' || el.tagName === 'SELECT') body[el.name] = Number(el.value);
    else body[el.name] = el.value;
  }
  if (!(body.fail_core_restart < body.fail_host_reboot && body.fail_host_reboot < body.fail_power_cycle)) {
    toast('Les paliers doivent être croissants (palier 1 < palier 2 < palier 3)', true);
    return;
  }
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
    toast('Réglages enregistrés');
    await loadSettings();
    post('/api/check');
  } catch (err) { toast(err.message, true); }
});

$('wdToggle').addEventListener('change', async (e) => {
  const on = e.target.checked;
  if (on && !(await confirmBox('Activer les <b>interventions automatiques</b> ?<p>Le NAS pourra redémarrer HA, le mini-PC et couper la prise selon les paliers.</p>'))) {
    e.target.checked = false; return;
  }
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ watchdog_enabled: on }) });
    toast(on ? 'Chien de garde activé' : 'Chien de garde désactivé');
    await loadSettings();
    refresh();
  } catch (err) { toast(err.message, true); e.target.checked = !on; }
});

$('btnTestNotif').addEventListener('click', async () => {
  try { await post('/api/test-notification'); toast('Notification envoyée'); }
  catch (err) { toast(err.message, true); }
});

$('btnReport').addEventListener('click', async () => {
  try { await post('/api/report'); toast('Rapport envoyé'); }
  catch (err) { toast(err.message, true); }
});

$('btnCopyKey').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('sshKey').value); toast('Clé copiée'); }
  catch { $('sshKey').select(); toast('Sélectionnée : copie-la manuellement'); }
});

$('btnResetFp').addEventListener('click', async () => {
  if (!(await confirmBox('Réinitialiser l’empreinte SSH du mini-PC ?<p>À faire uniquement après une réinstallation de Home Assistant : la nouvelle empreinte sera mémorisée à la prochaine connexion.</p>'))) return;
  try { await post('/api/ssh/reset-fingerprint'); toast('Empreinte réinitialisée'); loadSettings(); }
  catch (err) { toast(err.message, true); }
});

$('btnCheck').addEventListener('click', async () => {
  await post('/api/check');
  post('/api/app/check').catch(() => {});
  toast('Contrôle lancé');
  setTimeout(refresh, 2500);
});

$('btnLogout').addEventListener('click', async () => {
  await post('/api/logout');
  location.href = '/login';
});

document.querySelectorAll('[data-hours]').forEach((b) => b.addEventListener('click', () => {
  timelineHours = Number(b.dataset.hours);
  loadTimeline();
}));

// ================================================================== boucle
let tick = 0;
async function refresh() {
  try {
    renderStatus(await api('/api/status'));
    const jobs = [loadEvents()];
    if (tick % 6 === 0) jobs.push(loadTimeline(), loadBackups());
    tick++;
    await Promise.all(jobs);
  } catch (err) { if (err.message !== '401') console.error(err); }
}

(async () => {
  await Promise.all([loadSettings(), loadActions()]).catch(() => {});
  await refresh();
  setInterval(refresh, 5000);
})();
