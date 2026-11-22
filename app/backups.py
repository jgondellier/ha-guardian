"""Sauvegardes Home Assistant rapatriées sur le NAS."""
import asyncio
import json
import re
import time
from datetime import datetime

from . import config, db, devices

_lock = asyncio.Lock()
state: dict = {"running": False, "step": None, "last_result": None}

_NAME_RE = re.compile(r"^[\w.\-]+\.tar$")


def list_local() -> list[dict]:
    if not config.BACKUP_DIR.exists():
        return []
    files = [
        {"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        for p in config.BACKUP_DIR.glob("*.tar") if p.is_file()
    ]
    return sorted(files, key=lambda f: f["mtime"], reverse=True)


def local_path(name: str):
    if not _NAME_RE.match(name):
        return None
    p = config.BACKUP_DIR / name
    return p if p.is_file() else None


def latest_age_hours() -> float | None:
    files = list_local()
    return None if not files else (time.time() - files[0]["mtime"]) / 3600


def pick_remote_file(files: list[dict], slug: str | None, start: float) -> dict | None:
    """Choisit l'archive créée : celle qui porte le slug, sinon la plus récente
    apparue depuis le lancement de la sauvegarde."""
    tars = [f for f in files if f["name"].endswith(".tar")]
    if slug:
        match = [f for f in tars if slug in f["name"]]
        if match:
            return match[0]
    recent = [f for f in tars if f["mtime"] >= start - 120]
    return max(recent, key=lambda f: f["mtime"]) if recent else None


def _prune(keep: int) -> int:
    removed = 0
    for f in list_local()[max(1, keep):]:
        (config.BACKUP_DIR / f["name"]).unlink(missing_ok=True)
        removed += 1
    return removed


async def run(origin: str = "manuel") -> str:
    if _lock.locked():
        raise RuntimeError("Une sauvegarde est déjà en cours")
    async with _lock:
        state.update(running=True, step="Création de la sauvegarde sur le mini-PC")
        db.add_event("action", f"[{origin}] Sauvegarde : lancement")
        start = time.time()
        try:
            config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            status, out, err = await devices.ssh_exec(
                devices.ha_cli(f'backups new --name "HA Guardian {stamp}" --raw-json'),
                timeout=3600)
            if status not in (0, None):
                raise RuntimeError(f"ha backups new : {(out + err).strip()[:300]}")
            slug = None
            try:
                slug = json.loads(out.strip().splitlines()[-1]).get("data", {}).get("slug")
            except (ValueError, IndexError, AttributeError):
                pass

            state["step"] = "Recherche du fichier"
            files = await devices.sftp_list("/backup")
            chosen = pick_remote_file(files, slug, start)
            if not chosen:
                raise RuntimeError("Fichier de sauvegarde introuvable dans /backup")

            state["step"] = f"Copie vers le NAS ({chosen['size'] / 1e6:.0f} Mo)"
            local_name = f"{datetime.now():%Y-%m-%d_%H%M}_{slug or 'ha'}.tar"
            tmp = config.BACKUP_DIR / (local_name + ".part")
            await devices.sftp_get(f"/backup/{chosen['name']}", tmp)
            tmp.rename(config.BACKUP_DIR / local_name)

            settings = db.get_settings()
            if settings["backup_delete_remote"] and slug:
                state["step"] = "Suppression de la copie sur le mini-PC"
                await devices.ssh_run(devices.ha_cli(f"backups remove {slug}"), timeout=120)
            removed = _prune(settings["backup_keep"])

            duration = int(time.time() - start)
            msg = (f"{local_name} ({chosen['size'] / 1e6:.0f} Mo) en {duration} s"
                   + (f", {removed} ancienne(s) supprimée(s)" if removed else ""))
            db.add_event("ok", f"[{origin}] Sauvegarde copiée sur le NAS : {msg}")
            db.kv_set("last_backup_date", datetime.now().strftime("%Y-%m-%d"))
            state["last_result"] = {"ok": True, "ts": time.time(), "message": msg}
            return msg
        except Exception as exc:  # noqa: BLE001
            db.add_event("error", f"[{origin}] Sauvegarde échouée : {exc}")
            state["last_result"] = {"ok": False, "ts": time.time(), "message": str(exc)}
            for p in config.BACKUP_DIR.glob("*.part"):
                p.unlink(missing_ok=True)
            raise
        finally:
            state.update(running=False, step=None)
