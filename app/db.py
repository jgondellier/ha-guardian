"""Stockage SQLite : historique des contrôles, journal, réglages, petites valeurs."""
import json
import sqlite3
import threading
import time

from . import config

_DB_PATH = config.DATA_DIR / "guardian.db"
_lock = threading.Lock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

# Le type de la valeur par défaut fixe le type accepté (bool, int, str)
DEFAULT_SETTINGS = {
    # Chien de garde
    "watchdog_enabled": False,
    "notify_after": 2,
    "remind_minutes": 30,           # rappel pendant une panne (0 = jamais)
    "fail_core_restart": 3,
    "fail_host_reboot": 6,
    "fail_power_cycle": 10,
    "cooldown_minutes": 5,
    "boot_grace_minutes": 8,
    "max_power_cycles_per_day": 3,
    # Détection
    "retry_seconds": 10,            # 2e contrôle avant de compter un échec
    "slow_ms": 3000,                # au-delà : HA « lent »
    "sentinel_entities": "",        # capteurs témoins, un par ligne
    "sentinel_max_minutes": 60,
    "disk_alert_percent": 85,
    "temp_alert_celsius": 80,
    # Sauvegardes
    "backup_enabled": False,
    "backup_hour": 3,
    "backup_keep": 14,
    "backup_delete_remote": False,
    "backup_max_age_hours": 48,
    # Santé de Home Assistant
    "app_autorestart": False,       # redémarrer une app (ex add-on) arrêtée de façon inattendue
    "app_max_restarts_per_day": 3,
    "log_errors_per_hour": 30,      # alerte au-delà de ce nombre d'erreurs dans le journal HA
    "db_alert_mb": 3000,            # alerte si la base de HA dépasse cette taille
    # Batteries
    "battery_alert_percent": 15,
    # Réseau : un équipement par ligne « Nom ; hôte [; port ou URL] [; critique] »
    "network_devices": "",
    "network_fail_after": 3,
    # Sécurité de l'accès à l'interface
    "login_max_attempts": 5,        # échecs tolérés avant de bloquer l'adresse IP
    "login_window_minutes": 15,     # période sur laquelle les échecs sont comptés
    "login_block_minutes": 60,      # durée du blocage
    "login_notify": True,           # notifier chaque connexion réussie et chaque blocage
    "session_days": 7,              # durée d'une session avant de devoir se reconnecter
    # Rapport hebdomadaire
    "report_enabled": True,
    "report_day": 0,                # 0 = lundi
    "report_hour": 9,
}


def init() -> None:
    with _lock, _conn:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checks (
                ts REAL NOT NULL, ha_ok INTEGER, host_ok INTEGER,
                ssh_ok INTEGER, slzb_ok INTEGER, latency_ms REAL
            );
            CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )


# ---------------------------------------------------------------- contrôles
def add_check(ha_ok, host_ok, ssh_ok, slzb_ok, latency_ms) -> None:
    def b(v):
        return None if v is None else int(bool(v))

    with _lock, _conn:
        _conn.execute(
            "INSERT INTO checks VALUES (?,?,?,?,?,?)",
            (time.time(), b(ha_ok), b(host_ok), b(ssh_ok), b(slzb_ok), latency_ms),
        )


def timeline(hours: int = 24, buckets: int = 96) -> list[dict]:
    """Disponibilité de HA découpée en tranches (None = pas de données)."""
    now = time.time()
    start = now - hours * 3600
    width = hours * 3600 / buckets
    with _lock:
        rows = _conn.execute(
            """SELECT CAST((ts - ?) / ? AS INTEGER) AS b,
                      AVG(ha_ok) AS ratio, AVG(latency_ms) AS lat
               FROM checks WHERE ts >= ? GROUP BY b""",
            (start, width, start),
        ).fetchall()
    by_bucket = {r["b"]: r for r in rows}
    out = []
    for i in range(buckets):
        r = by_bucket.get(i)
        out.append({
            "start": start + i * width,
            "ratio": None if r is None or r["ratio"] is None else round(r["ratio"], 3),
            "latency": None if r is None or r["lat"] is None else round(r["lat"]),
        })
    return out


def uptime_ratio(hours: int) -> float | None:
    with _lock:
        row = _conn.execute(
            "SELECT AVG(ha_ok) FROM checks WHERE ts >= ?", (time.time() - hours * 3600,)
        ).fetchone()
    return None if row[0] is None else round(row[0] * 100, 2)


# ---------------------------------------------------------------- événements
def add_event(level: str, message: str) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO events (ts, level, message) VALUES (?,?,?)",
            (time.time(), level, message),
        )


def events(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT ts, level, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_events_since(message_prefix: str, since_ts: float, level: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM events WHERE ts >= ? AND message LIKE ?"
    args: list = [since_ts, message_prefix + "%"]
    if level:
        sql += " AND level = ?"
        args.append(level)
    with _lock:
        return _conn.execute(sql, args).fetchone()[0]


def prune() -> None:
    limit = time.time() - config.HISTORY_DAYS * 86400
    with _lock, _conn:
        _conn.execute("DELETE FROM checks WHERE ts < ?", (limit,))
        _conn.execute("DELETE FROM events WHERE ts < ?", (limit,))


# ---------------------------------------------------------------- réglages
def get_settings() -> dict:
    with _lock:
        rows = _conn.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r["key"] in s:
            s[r["key"]] = json.loads(r["value"])
    return s


def _coerce(default, val):
    if isinstance(default, bool):
        return val if isinstance(val, bool) else str(val).lower() in ("1", "true", "on", "yes")
    if isinstance(default, int):
        return max(0, int(float(val)))
    return str(val)[:4000]


def save_settings(new: dict) -> dict:
    current = get_settings()
    for key, default in DEFAULT_SETTINGS.items():
        if key in new:
            current[key] = _coerce(default, new[key])
    with _lock, _conn:
        for k, v in current.items():
            _conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v)),
            )
    return current


# ---------------------------------------------------------------- petites valeurs
def kv_get(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def kv_set(key: str, value) -> None:
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
