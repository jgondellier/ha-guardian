"""Notifications ntfy et/ou Telegram, avec boutons d'action signés pour ntfy."""
import hashlib
import hmac
import logging
import time

import httpx

from . import config

log = logging.getLogger("guardian.notify")

HOOK_VALIDITY = 12 * 3600
# Actions déclenchables depuis une notification (clé -> libellé du bouton)
HOOK_ACTIONS = {
    "ssh_core_restart": "Redémarrer HA",
    "plug_cycle": "Couper la prise",
    "maintenance_60": "Pause 1 h",
    "wol": "Réveiller le PC",
}


def enabled() -> bool:
    return bool(config.NTFY_TOPIC or (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID))


def _hook_sig(key: str, exp: int) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"hook:{key}:{exp}".encode(),
                    hashlib.sha256).hexdigest()


def hook_url(key: str) -> str:
    exp = int(time.time()) + HOOK_VALIDITY
    return f"{config.PUBLIC_URL}/api/hook/{key}?exp={exp}&sig={_hook_sig(key, exp)}"


def verify_hook(key: str, exp: int, sig: str) -> bool:
    return (key in HOOK_ACTIONS and exp > time.time()
            and hmac.compare_digest(sig, _hook_sig(key, exp)))


async def send(title: str, message: str, priority: int = 3,
               actions: list[str] | None = None) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        if config.NTFY_TOPIC:
            headers = {}
            if config.NTFY_TOKEN:
                headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
            payload = {"topic": config.NTFY_TOPIC, "title": title, "message": message,
                       "priority": priority, "tags": ["house"]}
            if actions and config.PUBLIC_URL:
                payload["actions"] = [
                    {"action": "http", "label": HOOK_ACTIONS[k], "url": hook_url(k),
                     "method": "POST", "clear": True}
                    for k in actions[:3] if k in HOOK_ACTIONS
                ]
            if config.PUBLIC_URL:
                payload["click"] = config.PUBLIC_URL
            try:
                await client.post(config.NTFY_SERVER, json=payload, headers=headers)
            except Exception as exc:  # noqa: BLE001
                log.warning("ntfy : %s", exc)
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": config.TELEGRAM_CHAT_ID, "text": f"{title}\n{message}"},
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram : %s", exc)
