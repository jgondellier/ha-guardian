"""Protection de l'accès : blocage d'IP après trop d'échecs, jetons de session, en-têtes.

Les échecs récents sont gardés en mémoire ; les blocages sont enregistrés dans la base
(table kv) pour survivre à un redémarrage ou à une mise à jour du conteneur.
"""
import hashlib
import hmac
import ipaddress
import threading
import time

from . import config, db

_KV_BLOCKS = "ip_blocks"
_KV_EPOCH = "session_epoch"

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}

# En-têtes ajoutés à toutes les réponses
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000"

# En-tête exigé sur les requêtes qui modifient quelque chose (protection CSRF) :
# un autre site ne peut pas l'ajouter sans autorisation CORS, que l'appli ne donne pas.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "HA-Guardian"


# ================================================================ adresses IP
def ip_key(ip: str) -> str:
    """Clé de comptage : l'adresse IPv4, ou le préfixe /64 en IPv6 (un abonné en
    dispose d'un entier : changer d'adresse ne doit pas contourner le blocage)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip or "?"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _limits() -> tuple[int, float, float]:
    s = db.get_settings()
    attempts = max(1, int(s["login_max_attempts"]))
    window = max(1, int(s["login_window_minutes"])) * 60
    block = max(1, int(s["login_block_minutes"])) * 60
    return attempts, window, block


def _blocks() -> dict:
    now = time.time()
    blocks = db.kv_get(_KV_BLOCKS, {}) or {}
    active = {k: v for k, v in blocks.items() if v.get("until", 0) > now}
    if len(active) != len(blocks):
        db.kv_set(_KV_BLOCKS, active)
    return active


def blocked_until(ip: str) -> float | None:
    b = _blocks().get(ip_key(ip))
    return b["until"] if b else None


def remaining_attempts(ip: str) -> int:
    attempts, window, _ = _limits()
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(ip_key(ip), []) if t > now - window]
    return max(0, attempts - len(recent))


def register_failure(ip: str) -> tuple[int, float | None]:
    """Compte un échec. Renvoie (essais restants, fin du blocage si l'IP vient d'être bloquée)."""
    attempts, window, block = _limits()
    key = ip_key(ip)
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(key, []) if t > now - window]
        recent.append(now)
        if len(recent) < attempts:
            _failures[key] = recent
            return attempts - len(recent), None
        _failures.pop(key, None)
        # Évite que la mémoire grossisse indéfiniment sous une attaque répartie
        if len(_failures) > 10000:
            _failures.clear()
    until = now + block
    blocks = _blocks()
    blocks[key] = {"ip": ip, "since": now, "until": until, "attempts": len(recent)}
    db.kv_set(_KV_BLOCKS, blocks)
    return 0, until


def register_success(ip: str) -> None:
    with _lock:
        _failures.pop(ip_key(ip), None)


def list_blocks() -> list[dict]:
    return sorted(({"key": k, **v} for k, v in _blocks().items()),
                  key=lambda b: b["since"], reverse=True)


def unblock(key: str) -> bool:
    blocks = _blocks()
    if key not in blocks:
        return False
    blocks.pop(key)
    db.kv_set(_KV_BLOCKS, blocks)
    with _lock:
        _failures.pop(key, None)
    return True


# ================================================================ mot de passe
_KDF_ITERATIONS = 100_000
_kdf_cache: dict[str, bytes] = {}


def _derive(password: str) -> bytes:
    """Empreinte lente (PBKDF2) du mot de passe, salée avec la clé secrète de l'instance."""
    salt = hmac.new(config.SECRET_KEY.encode(), b"password-salt", hashlib.sha256).digest()
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", "replace"), salt,
                               _KDF_ITERATIONS)


def _expected() -> bytes:
    """Empreinte du mot de passe configuré, calculée une seule fois."""
    pw = config.APP_PASSWORD
    if pw not in _kdf_cache:
        _kdf_cache.clear()
        _kdf_cache[pw] = _derive(pw)
    return _kdf_cache[pw]


def check_password(given: str) -> bool:
    """Comparaison en temps constant des empreintes (aussi pour les caractères non ASCII)."""
    return hmac.compare_digest(_derive(given), _expected())


def weak_password() -> bool:
    pw = config.APP_PASSWORD
    return len(pw) < 12 or len(set(pw)) < 6


# ================================================================ sessions
def _epoch() -> int:
    return int(db.kv_get(_KV_EPOCH, 0) or 0)


def _sign(expires: str, epoch: int) -> str:
    # Le mot de passe entre dans la signature : le changer déconnecte toutes les sessions
    msg = f"session:{expires}:{epoch}:{_expected().hex()}"
    return hmac.new(config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_token(days: int) -> str:
    expires = str(int(time.time()) + max(1, days) * 86400)
    return f"{expires}.{_sign(expires, _epoch())}"


def valid_token(token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit() or int(expires) <= time.time():
        return False
    return hmac.compare_digest(sig, _sign(expires, _epoch()))


def revoke_sessions() -> None:
    """Invalide toutes les sessions ouvertes."""
    db.kv_set(_KV_EPOCH, _epoch() + 1)
