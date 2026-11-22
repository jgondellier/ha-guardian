"""Configuration lue depuis les variables d'environnement (fichier .env)."""
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    return _env(key, "true" if default else "false").lower() in ("1", "true", "yes", "oui")


# --- Version (injectée à la construction de l'image) ---
APP_VERSION = _env("APP_VERSION", "dev")
GIT_SHA = _env("GIT_SHA", "dev")

DATA_DIR = Path(_env("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = Path(_env("BACKUP_DIR", "/backups"))

# --- Accès à l'interface ---
APP_PASSWORD = _env("APP_PASSWORD")
# URL de l'interface vue depuis le téléphone (VPN) : active les boutons dans les alertes ntfy
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")

_secret_file = DATA_DIR / "secret.key"
if not _secret_file.exists():
    _secret_file.write_text(secrets.token_hex(32))
SECRET_KEY = _secret_file.read_text().strip()

# --- Home Assistant ---
HA_URL = _env("HA_URL", "http://192.168.1.10:8123").rstrip("/")
HA_TOKEN = _env("HA_TOKEN")
HA_HOST = _env("HA_HOST") or (urlparse(HA_URL).hostname or "")
HA_MAC = _env("HA_MAC")                          # pour le Wake-on-LAN
WOL_BROADCAST = _env("WOL_BROADCAST", "255.255.255.255")

# --- SSH (add-on « Advanced SSH & Web Terminal ») ---
SSH_ENABLED = _bool("SSH_ENABLED", True)
SSH_PORT = _int("SSH_PORT", 22)
SSH_USER = _env("SSH_USER", "hassio")

# --- Coordinateur Zigbee SLZB-06 (optionnel) ---
SLZB_URL = _env("SLZB_URL").rstrip("/")

# --- Prise connectée pilotant l'alimentation du mini-PC (optionnel) ---
# none | shelly_gen1 | shelly_gen2 | tasmota
PLUG_TYPE = _env("PLUG_TYPE", "none").lower()
PLUG_URL = _env("PLUG_URL").rstrip("/")
PLUG_USER = _env("PLUG_USER")
PLUG_PASSWORD = _env("PLUG_PASSWORD")
PLUG_CHANNEL = _int("PLUG_CHANNEL", 0)
PLUG_OFF_SECONDS = _int("PLUG_OFF_SECONDS", 15)

# --- Routeur Asus principal (optionnel) : état des nœuds AiMesh ---
ASUS_URL = _env("ASUS_URL").rstrip("/")
ASUS_USER = _env("ASUS_USER")
ASUS_PASSWORD = _env("ASUS_PASSWORD")
ASUS_ENABLED = bool(ASUS_URL and ASUS_USER and ASUS_PASSWORD)

# --- Notifications (optionnelles) ---
NTFY_SERVER = _env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = _env("NTFY_TOPIC")
NTFY_TOKEN = _env("NTFY_TOKEN")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
# Signal de vie (ex. healthchecks.io) : alerte si le NAS / HA Guardian s'arrête
HEARTBEAT_URL = _env("HEARTBEAT_URL")

# --- Surveillance ---
CHECK_INTERVAL = max(15, _int("CHECK_INTERVAL", 60))
SYSTEM_INTERVAL = max(60, _int("SYSTEM_INTERVAL", 300))
HISTORY_DAYS = _int("HISTORY_DAYS", 14)

# --- Mise à jour de HA Guardian ---
GITHUB_REPO = _env("GITHUB_REPO", "jgondellier/ha-guardian")
GITHUB_TOKEN = _env("GITHUB_TOKEN")              # seulement si le dépôt est privé
UPDATE_URL = _env("UPDATE_URL", "http://127.0.0.1:8090").rstrip("/")
UPDATE_TOKEN = _env("UPDATE_TOKEN")

PLUG_ENABLED = PLUG_TYPE in ("shelly_gen1", "shelly_gen2", "tasmota") and bool(PLUG_URL)
WOL_ENABLED = bool(HA_MAC)
