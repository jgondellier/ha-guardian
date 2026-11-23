'use strict';

document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = document.getElementById('err');
  const btn = e.target.querySelector('button');
  err.textContent = '';
  btn.disabled = true;
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'HA-Guardian' },
      body: JSON.stringify({ password: document.getElementById('pw').value }),
    });
    if (r.ok) { location.href = '/'; return; }
    const d = await r.json().catch(() => ({}));
    err.textContent = d.detail || 'Erreur de connexion';
  } catch {
    err.textContent = 'Serveur injoignable';
  } finally {
    btn.disabled = false;
  }
});
