"""Configuration commune : environnement isolé, notifications et actions simulées."""
import asyncio
import os
import tempfile

# La configuration est lue à l'import : on prépare l'environnement avant tout import de app.*
_TMP = tempfile.mkdtemp(prefix="ha-guardian-tests-")
os.environ.update({
    "DATA_DIR": os.path.join(_TMP, "data"),
    "BACKUP_DIR": os.path.join(_TMP, "backups"),
    "APP_PASSWORD": "mot-de-passe-de-test",
    "HA_URL": "http://127.0.0.1:9",
    "PLUG_TYPE": "shelly_gen1",
    "PLUG_URL": "http://127.0.0.1:9",
    "HA_MAC": "AA:BB:CC:DD:EE:FF",
    "PUBLIC_URL": "http://guardian.test",
    "APP_VERSION": "2.0.0",
    "GIT_SHA": "0123456789abcdef",
})

import pytest  # noqa: E402

from app import db, network, notify, security, watchdog  # noqa: E402

db.init()
_REAL_ACTIONS = dict(watchdog.ACTIONS)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(monkeypatch):
    """Base vierge, notifications et actions enregistrées au lieu d'être exécutées."""
    with db._lock, db._conn:
        for table in ("checks", "events", "settings", "kv"):
            db._conn.execute(f"DELETE FROM {table}")
    watchdog.state.update(
        consecutive_failures=0, incident_since=None, incident_steps_done=[], last_alert_ts=None,
        last_action_ts=0.0, last_action=None, grace_until=0.0, busy=None, alerts={},
    )
    network.state["devices"] = []
    security._failures.clear()
    calls: list[tuple] = []

    async def fake_send(title, message, priority=3, actions=None):
        calls.append(("notify", title, tuple(actions or ())))

    monkeypatch.setattr(notify, "send", fake_send)
    fake_actions = {}
    for key, (label, _func) in _REAL_ACTIONS.items():
        async def f(key=key):
            calls.append(("action", key))
            return "ok"
        fake_actions[key] = (label, f)
    monkeypatch.setattr(watchdog, "ACTIONS", fake_actions)
    db.save_settings({"watchdog_enabled": True, "cooldown_minutes": 0, "boot_grace_minutes": 0})
    return calls
