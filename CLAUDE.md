# HA Guardian — instructions pour Claude

Application web qui surveille **Home Assistant** (installé sur un mini-PC, HA OS) depuis un
**NAS Synology DS718+ sous DSM 7.1.1**, et intervient en cas de panne : redémarrages en SSH,
Wake-on-LAN, coupure d'une prise connectée, sauvegardes, mises à jour.

Le propriétaire est francophone : **interface, messages, journal, commentaires, commits,
CHANGELOG et README sont en français.** Les noms de code (variables, fonctions) restent en anglais.

## Architecture

- `app/main.py` : FastAPI (routes, middleware de sécurité : blocage d'IP, session, CSRF par en-tête `X-Requested-With: HA-Guardian`, en-têtes ; liens signés à usage unique `/api/hook/*`).
- `app/security.py` : blocage d'IP après N échecs (réglages `login_*`, blocages dans `kv`), jetons de session (révocables, liés au mot de passe), en-têtes (CSP stricte : aucun script ni style en ligne).
- `app/watchdog.py` : boucle de contrôle, escalade par paliers, mode maintenance, alertes dédoublonnées (`alert()`), registre `ACTIONS`.
- `app/tasks.py` : tâches planifiées (santé du mini-PC, sauvegardes, rapport hebdo, signal de vie, mises à jour HA, vérification de version).
- `app/devices.py` : tout ce qui touche l'extérieur (API HA, SSH/SFTP avec asyncssh, ping, WoL, prise Shelly/Tasmota, SLZB-06).
- `app/health.py` : santé de HA (apps via SSH `ha addons`/`ha apps` — renommées en 2026.2 —, intégrations et réparations via WebSocket, journal `/api/error_log`, batteries).
- `app/network.py` : équipements réseau (ping/port/URL, « critique » bloque l'escalade) et API non officielle des routeurs Asus (`login.cgi` + `appGet.cgi`, formats variables : parser défensivement).
- `app/backups.py`, `app/updater.py`, `app/notify.py`, `app/db.py` (SQLite : checks, events, settings, kv), `app/config.py` (variables d'environnement, lues **à l'import**).
- `app/static/` : HTML/CSS/JS sans framework ni étape de build. Toujours échapper les données avec `esc()` avant `innerHTML`.

Réglages modifiables dans l'interface : `db.DEFAULT_SETTINGS` (le type de la valeur par défaut fixe le type accepté).
Configuration du déploiement : variables d'environnement (`.env.example`, à tenir à jour avec `config.py`).

## Déploiement

- **Deux machines distinctes** : HA Guardian tourne sur le NAS Synology (Docker, `docker-compose.yml`) ;
  Home Assistant tourne sur un mini-PC séparé, joint par le réseau (API, SSH, ping, WoL, prise).
- Interface sur le port **8099** du NAS. Accès depuis Internet uniquement via le proxy inversé
  HTTPS de DSM (`https://haguardian.<domaine>` → `http://localhost:8099`, `FORWARDED_ALLOW_IPS=127.0.0.1`),
  jamais en ouvrant le port 8099. Le domaine réel reste dans `.env` (`PUBLIC_URL`) : le dépôt est public.
- Mise à jour : Watchtower tire `latest` ; le bandeau « Nouvelle version » de l'interface s'appuie sur les releases.

## Commandes

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest              # tests (conftest.py prépare un environnement isolé)
node --check app/static/app.js
```

Les tests n'appellent jamais le réseau : `notify.send` et les fonctions de `watchdog.ACTIONS`
sont remplacées par la fixture `env`. Pour l'updater, `updater._client` est remplacé par un
`httpx.MockTransport`. Ajouter un test pour toute nouvelle logique de décision.

## Contraintes à respecter

- **Docker 20.10.3 de DSM 7.1** : l'image reste sur **Alpine** (musl). Les images Debian récentes (glibc ≥ 2.34) ne peuvent pas créer de threads sur ce Docker (appel `clone3` bloqué). Ne pas passer à une base Debian.
- **docker-compose v1** sur le NAS (pas de `docker compose` v2, pas de Container Manager) : format de fichier compatible v1.
- Le conteneur tourne en **réseau hôte** (Wake-on-LAN, ping), **système de fichiers en lecture seule** (`/tmp` en tmpfs) et `no-new-privileges`. N'écrire que dans `/data` et `/backups` (déclarés `VOLUME`).
- **Watchtower `containrrr/watchtower:1.7.1`** : compatible avec Docker 20.10. Ne le remplacer par `nickfedor/watchtower` que si le NAS passe à Docker 29+.
- pip et setuptools sont retirés de l'image finale : ne pas installer de paquet au démarrage.

## Sécurité (non négociable)

- Le chien de garde est **désactivé par défaut** ; toute nouvelle action automatique doit respecter maintenance, période de grâce, délai entre actions et limite quotidienne.
- Une erreur de configuration (jeton HA refusé) ne doit jamais déclencher d'action.
- Aucune action destructive sans confirmation dans l'interface.
- Jamais de secret dans le dépôt : tout passe par `.env` (ignoré par git). Les valeurs d'exemple restent factices.
- L'interface peut être exposée sur Internet **uniquement** derrière le proxy inversé HTTPS de DSM (adresse réelle via `FORWARDED_ALLOW_IPS`, lue par Uvicorn). Garder l'authentification sur toutes les routes sauf `PUBLIC_PATHS` et les liens signés ; toute requête modifiante de l'API passe par `api()` dans `app.js` (en-tête CSRF) ; pas de `<script>` en ligne ni d'attribut `style=` (CSP).

## Publier une version

1. Modifier le code et les tests.
2. Incrémenter `VERSION` (semver : correctif → 2.1.x, fonctionnalité → 2.x.0).
3. Ajouter une section `## X.Y.Z` en tête de `CHANGELOG.md` (elle devient les notes de la release et le contenu du bandeau « Nouvelle version » sur le NAS).
4. Pousser sur `main`. La CI (`.github/workflows/docker.yml`) teste, construit, démarre l'image, la scanne avec Trivy (bloquant sur CRITICAL), publie `latest` + `X.Y.Z`, signe l'attestation et crée la release `vX.Y.Z`.

Sans changement de `VERSION`, seule l'image `edge` est publiée : rien n'est proposé au NAS.
Les PR (dont Dependabot) passent par la même chaîne, sans publication.

## Commits

Messages en français, première ligne courte et descriptive (ex. « Image : … », « Sauvegardes : … »).
