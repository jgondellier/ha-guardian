"""Contrôles et actions : Home Assistant, mini-PC (SSH, WoL), prise, SLZB-06."""
import asyncio
import json
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import httpx

from . import config, db

log = logging.getLogger("guardian.devices")

SSH_DIR = config.DATA_DIR / "ssh"
SSH_KEY_PATH = SSH_DIR / "id_ed25519"
KNOWN_HOSTS = SSH_DIR / "known_hosts"


# ================================================================ SSH
def ensure_ssh_key() -> str:
    """Crée la clé SSH du NAS au premier démarrage et renvoie la clé publique."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    if not SSH_KEY_PATH.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment="ha-guardian@synology")
        key.write_private_key(str(SSH_KEY_PATH))
        key.write_public_key(str(SSH_KEY_PATH) + ".pub")
        SSH_KEY_PATH.chmod(0o600)
    return (SSH_DIR / "id_ed25519.pub").read_text().strip()


def _host_id() -> str:
    return config.HA_HOST if config.SSH_PORT == 22 else f"[{config.HA_HOST}]:{config.SSH_PORT}"


def known_fingerprint() -> str | None:
    if not KNOWN_HOSTS.exists():
        return None
    for line in KNOWN_HOSTS.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == _host_id():
            try:
                return asyncssh.import_public_key(" ".join(parts[1:3])).get_fingerprint()
            except Exception:  # noqa: BLE001
                return "?"
    return None


def reset_fingerprint() -> None:
    KNOWN_HOSTS.unlink(missing_ok=True)


async def _ensure_known_host() -> None:
    """Principe « confiance à la première connexion » : l'empreinte du mini-PC est
    mémorisée au premier contact, puis toute différence bloque la connexion."""
    if known_fingerprint():
        return
    key = await asyncio.wait_for(
        asyncssh.get_server_host_key(config.HA_HOST, config.SSH_PORT), 15)
    if key is None:
        raise RuntimeError("Impossible de lire l'empreinte SSH du mini-PC")
    KNOWN_HOSTS.write_text(f"{_host_id()} {key.export_public_key().decode().strip()}\n")
    db.add_event("info", f"Empreinte SSH du mini-PC mémorisée : {key.get_fingerprint()}")


async def _connect():
    if not config.SSH_ENABLED:
        raise RuntimeError("SSH désactivé dans la configuration")
    ensure_ssh_key()
    await _ensure_known_host()
    try:
        return await asyncssh.connect(
            config.HA_HOST, port=config.SSH_PORT, username=config.SSH_USER,
            client_keys=[str(SSH_KEY_PATH)], known_hosts=str(KNOWN_HOSTS),
            connect_timeout=10,
        )
    except asyncssh.HostKeyNotVerifiable as exc:
        raise RuntimeError("Empreinte SSH différente de celle mémorisée. Si HA a été "
                           "réinstallé, réinitialise l'empreinte dans Réglages.") from exc
    except asyncssh.PermissionDenied as exc:
        raise RuntimeError("Clé SSH refusée : colle la clé du NAS dans l'add-on SSH") from exc


async def ssh_exec(command: str, timeout: int = 240) -> tuple[int | None, str, str]:
    conn = await _connect()
    async with conn:
        res = await asyncio.wait_for(conn.run(command, check=False), timeout)
    return res.exit_status, (res.stdout or ""), (res.stderr or "")


async def ssh_run(command: str, timeout: int = 240) -> str:
    try:
        status, out, err = await ssh_exec(command, timeout)
    except (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError):
        return f"« {command} » envoyé (connexion fermée par le redémarrage)"
    text = (out + err).strip()
    if status not in (0, None):
        raise RuntimeError(f"code {status} : {text[:300]}")
    return f"« {command} » OK" + (f" — {text[:200]}" if text else "")


async def sftp_list(path: str) -> list[dict]:
    conn = await _connect()
    async with conn, conn.start_sftp_client() as sftp:
        out = []
        for entry in await sftp.readdir(path):
            if entry.filename in (".", ".."):
                continue
            out.append({"name": entry.filename, "size": entry.attrs.size or 0,
                        "mtime": entry.attrs.mtime or 0})
        return out


async def sftp_get(remote: str, local: Path) -> None:
    conn = await _connect()
    async with conn, conn.start_sftp_client() as sftp:
        await sftp.get(remote, str(local))


# ---------------------------------------------------------------- santé du mini-PC
_SYS_CMD = (
    "sh -c 'ha host info --raw-json 2>/dev/null; echo @@; cat /proc/meminfo; echo @@; "
    "cat /proc/loadavg; echo @@; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null; "
    "echo @@; nproc; echo @@; cat /proc/uptime; echo @@; "
    "stat -c %s /config/home-assistant_v2.db 2>/dev/null || "
    "stat -c %s /homeassistant/home-assistant_v2.db 2>/dev/null'"
)


def parse_system(output: str) -> dict:
    parts = (output.split("@@") + [""] * 7)[:7]
    res: dict = {}
    try:
        data = json.loads(parts[0].strip()).get("data", {})
        total = data.get("disk_total")
        if total:
            free = data.get("disk_free")
            used = data.get("disk_used") or (total - (free or 0))
            res["disk_total_gb"] = round(total, 1)
            res["disk_free_gb"] = round(free if free is not None else total - used, 1)
            res["disk_percent"] = round(used / total * 100, 1)
    except (ValueError, AttributeError):
        pass
    mem = {}
    for line in parts[1].splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            mem[k.strip()] = int(v.split()[0])
    if mem.get("MemTotal") and "MemAvailable" in mem:
        res["mem_percent"] = round((1 - mem["MemAvailable"] / mem["MemTotal"]) * 100, 1)
        res["mem_total_gb"] = round(mem["MemTotal"] / 1048576, 1)
    try:
        res["load1"] = float(parts[2].split()[0])
    except (IndexError, ValueError):
        pass
    temps = [int(t) / 1000 for t in parts[3].split() if t.strip().lstrip("-").isdigit()]
    temps = [t for t in temps if 0 < t < 150]
    if temps:
        res["temp_c"] = round(max(temps), 1)
    try:
        res["cpus"] = int(parts[4].strip())
    except ValueError:
        pass
    try:
        res["uptime_s"] = int(float(parts[5].split()[0]))
    except (IndexError, ValueError):
        pass
    db_size = parts[6].strip().split()
    if db_size and db_size[0].isdigit():
        res["db_mb"] = round(int(db_size[0]) / 1048576)
    return res


async def system_stats() -> dict:
    _, out, _ = await ssh_exec(_SYS_CMD, timeout=30)
    stats = parse_system(out)
    stats["ts"] = time.time()
    return stats


# ================================================================ contrôles réseau
async def ping(host: str) -> bool:
    if not host:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", host,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), 5) == 0
    except Exception:  # noqa: BLE001
        return False


async def tcp_open(host: str, port: int, timeout: float = 3) -> bool:
    if not host:
        return False
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def wake_on_lan() -> str:
    mac = config.HA_MAC.replace(":", "").replace("-", "").replace(".", "")
    if len(mac) != 12:
        raise RuntimeError("HA_MAC invalide (format attendu AA:BB:CC:DD:EE:FF)")
    packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            s.sendto(packet, (config.WOL_BROADCAST, port))
    return f"Paquet Wake-on-LAN envoyé à {config.HA_MAC}"


# ================================================================ Home Assistant
def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {config.HA_TOKEN}", "Content-Type": "application/json"}


def _ha_client(timeout: float = 10) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, verify=ha_tls_context(), headers=_ha_headers())


def ha_tls_context() -> ssl.SSLContext:
    """Contexte TLS pour HA (API REST et WebSocket), selon HA_VERIFY_TLS."""
    ctx = ssl.create_default_context()
    if not config.HA_VERIFY_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def check_ha() -> dict:
    result = {"ok": False, "latency_ms": None, "version": None, "error": None}
    start = time.perf_counter()
    try:
        async with _ha_client() as client:
            r = await client.get(f"{config.HA_URL}/api/config")
        result["latency_ms"] = round((time.perf_counter() - start) * 1000)
        if r.status_code == 200:
            data = r.json()
            result.update(ok=True, version=data.get("version"),
                          location=data.get("location_name"))
        elif r.status_code == 401:
            result["error"] = "Jeton refusé (401) : vérifier HA_TOKEN"
        else:
            result["error"] = f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        result["error"] = "Délai dépassé"
    except Exception as exc:  # noqa: BLE001
        result["error"] = type(exc).__name__
    return result


async def ha_error_log() -> str:
    async with _ha_client(20) as client:
        r = await client.get(f"{config.HA_URL}/api/error_log")
    if r.status_code == 404:
        raise RuntimeError("Journal indisponible via l'API (HA trop récent ou trop ancien)")
    r.raise_for_status()
    return r.text[-2_000_000:]


async def ha_states() -> list[dict]:
    async with _ha_client(20) as client:
        r = await client.get(f"{config.HA_URL}/api/states")
        r.raise_for_status()
        return r.json()


async def ha_state(entity_id: str) -> dict | None:
    async with _ha_client() as client:
        r = await client.get(f"{config.HA_URL}/api/states/{entity_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


async def check_sentinels(entities: list[str], max_minutes: int) -> list[dict]:
    """Vérifie que chaque capteur témoin a donné signe de vie récemment."""
    async def one(eid: str) -> dict:
        try:
            st = await ha_state(eid)
        except Exception as exc:  # noqa: BLE001
            return {"entity_id": eid, "ok": False, "error": type(exc).__name__}
        if st is None:
            return {"entity_id": eid, "ok": False, "error": "Entité introuvable"}
        seen = _parse_ts(st.get("last_reported")) or _parse_ts(st.get("last_updated"))
        age = None if seen is None else int((time.time() - seen) / 60)
        state = st.get("state")
        ok = state not in ("unavailable", "unknown") and age is not None and age <= max_minutes
        return {"entity_id": eid, "ok": ok, "state": state, "age_min": age,
                "name": st.get("attributes", {}).get("friendly_name", eid)}

    return await asyncio.gather(*(one(e) for e in entities))


async def ha_updates() -> list[dict]:
    out = []
    for st in await ha_states():
        if st["entity_id"].startswith("update.") and st.get("state") == "on":
            a = st.get("attributes", {})
            out.append({
                "entity_id": st["entity_id"],
                "title": a.get("title") or a.get("friendly_name") or st["entity_id"],
                "installed": a.get("installed_version"),
                "latest": a.get("latest_version"),
                "in_progress": bool(a.get("in_progress")),
                "release_url": a.get("release_url"),
            })
    out.sort(key=lambda u: (not u["entity_id"].startswith("update.home_assistant"), u["title"]))
    return out


async def ha_service(domain: str, service: str, data: dict | None = None,
                     timeout: float = 20) -> str:
    async with _ha_client(timeout) as client:
        try:
            r = await client.post(f"{config.HA_URL}/api/services/{domain}/{service}",
                                  json=data or {})
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            return f"{domain}.{service} envoyé (connexion coupée, redémarrage probable)"
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
    return f"{domain}.{service} exécuté"


async def check_slzb() -> dict | None:
    if not config.SLZB_URL:
        return None
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(config.SLZB_URL)
        return {"ok": r.status_code < 500,
                "latency_ms": round((time.perf_counter() - start) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


# ================================================================ prise
def _plug_auth():
    if not config.PLUG_USER:
        return None
    if config.PLUG_TYPE == "shelly_gen2":
        return httpx.DigestAuth(config.PLUG_USER, config.PLUG_PASSWORD)
    return httpx.BasicAuth(config.PLUG_USER, config.PLUG_PASSWORD)


async def _plug_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=6, auth=_plug_auth()) as client:
        r = await client.get(f"{config.PLUG_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def plug_status() -> dict | None:
    if not config.PLUG_ENABLED:
        return None
    ch = config.PLUG_CHANNEL
    try:
        if config.PLUG_TYPE == "shelly_gen1":
            d = await _plug_get("/status")
            power = None
            if d.get("meters"):
                power = d["meters"][min(ch, len(d["meters"]) - 1)].get("power")
            return {"ok": True, "on": d["relays"][ch]["ison"], "power": power}
        if config.PLUG_TYPE == "shelly_gen2":
            d = await _plug_get("/rpc/Switch.GetStatus", {"id": ch})
            return {"ok": True, "on": d.get("output"), "power": d.get("apower")}
        if config.PLUG_TYPE == "tasmota":
            d = await _plug_get("/cm", {"cmnd": "Status 0"})
            on = d.get("StatusSTS", {}).get("POWER") == "ON"
            power = d.get("StatusSNS", {}).get("ENERGY", {}).get("Power")
            return {"ok": True, "on": on, "power": power}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    return None


async def plug_set(on: bool) -> None:
    if not config.PLUG_ENABLED:
        raise RuntimeError("Aucune prise configurée")
    ch = config.PLUG_CHANNEL
    if config.PLUG_TYPE == "shelly_gen1":
        await _plug_get(f"/relay/{ch}", {"turn": "on" if on else "off"})
    elif config.PLUG_TYPE == "shelly_gen2":
        await _plug_get("/rpc/Switch.Set", {"id": ch, "on": "true" if on else "false"})
    elif config.PLUG_TYPE == "tasmota":
        await _plug_get("/cm", {"cmnd": f"Power{ch + 1} {'On' if on else 'Off'}"})


async def plug_cycle() -> str:
    await plug_set(False)
    await asyncio.sleep(config.PLUG_OFF_SECONDS)
    await plug_set(True)
    return f"Prise coupée {config.PLUG_OFF_SECONDS} s puis rallumée"
