# 🛡️ HA Guardian

Surveillance et intervention sur **Home Assistant** depuis un **NAS Synology** (Docker).

- **Tableau de bord** : HA (version, latence, état « lent »), mini-PC (disque, mémoire, charge, température, durée de fonctionnement), SSH, SLZB-06, capteurs témoins Zigbee, prise connectée.
- **Chien de garde** à paliers : réveil Wake-on-LAN → redémarrage de HA Core (SSH) → redémarrage du mini-PC (SSH) → coupure de la prise. Il inclut un double contrôle, une période de grâce, un délai entre actions et un nombre maximum de coupures par jour.
- **Mode maintenance** (pause 1 h / 4 h), activé automatiquement pendant les mises à jour.
- **Sauvegardes** HA quotidiennes copiées sur le NAS, avec rotation, téléchargement et alerte si elles sont trop anciennes.
- **Mises à jour HA** visibles dans l'interface et installées après une sauvegarde.
- **Alertes** ntfy / Telegram, **boutons d'action** dans les notifications ntfy, **rapport hebdomadaire**, **signal de vie** (healthchecks.io).
- **Mise à jour en un clic** de HA Guardian depuis GitHub.

---

## 1. Préparer Home Assistant et le mini-PC

1. **Jeton** : *Profil* > *Sécurité* > **Jetons d'accès longue durée** > *Créer*. Le copier dans `HA_TOKEN`.
2. **Add-on SSH** : installer **Advanced SSH & Web Terminal**, puis régler le port **22** dans l'onglet *Réseau*. La clé du NAS sera ajoutée à l'étape 4.
3. **BIOS du mini-PC** :
   - **Restore on AC Power Loss = Power On** : le PC redémarre seul après une coupure de la prise.
   - **Wake on LAN = Enabled**, si tu veux utiliser le réveil à distance (et renseigner `HA_MAC`).

## 2. Préparer le NAS (DSM 7.1.1)

1. *Panneau de configuration* > *Terminal & SNMP* > **Activer le service SSH**.
2. Avec **File Station**, créer le dossier `docker/ha-guardian`. Y déposer `docker-compose.yml` et `.env.example` depuis ce dépôt.
3. Créer aussi le dossier des sauvegardes, par exemple `backup/home-assistant`.

## 3. Configurer et démarrer

En SSH sur le NAS :

```sh
cd /volume1/docker/ha-guardian
cp .env.example .env
openssl rand -hex 24          # copier le résultat dans UPDATE_TOKEN
vi .env                       # APP_PASSWORD, HA_URL, HA_TOKEN, BACKUP_PATH, UPDATE_TOKEN…
sudo docker-compose pull
sudo docker-compose up -d
```

Ouvrir ensuite **http://IP_DU_NAS:8099**.

> Le conteneur utilise le réseau de l'hôte, ce qui est nécessaire au Wake-on-LAN. Le port se règle avec `PORT` dans `.env`.

## 4. Autoriser le NAS en SSH

Dans l'interface, va dans *Réglages* > **SSH** > *Copier la clé*. Colle-la ensuite dans la configuration (YAML) de l'add-on :

```yaml
ssh:
  username: hassio          # = SSH_USER dans .env
  authorized_keys:
    - ssh-ed25519 AAAA… ha-guardian@synology
```

Redémarre l'add-on. À la première connexion, l'empreinte du mini-PC est mémorisée, puis vérifiée à chaque connexion. Après une réinstallation de HA, utilise *Réinitialiser l'empreinte*.

> Si `ha host reboot` ou `ha backups new` renvoie une erreur de permission, désactive le **mode protection** de l'add-on.

## 5. Tester, puis activer

1. Vérifier que toutes les tuiles sont vertes et que la santé du mini-PC s'affiche (sous 5 minutes environ).
2. Ajouter 1 ou 2 **capteurs témoins** : des capteurs Zigbee qui remontent souvent (température, prise avec mesure).
3. Lancer *Sauvegarder maintenant* et vérifier que le fichier arrive dans `BACKUP_PATH`.
4. Tester les actions manuelles, *Tester les alertes*, puis activer la **sauvegarde automatique** et le **chien de garde**.

| Palier | Action | Par défaut |
|---|---|---|
| Alerte | notification (avec boutons) | 2 échecs (~2 min) |
| 1 | Wake-on-LAN si le PC est injoignable, sinon `ha core restart` | 3 échecs |
| 2 | `ha host reboot` | 6 échecs |
| 3 | coupure de la prise | 10 échecs |

---

## Options utiles

- **Boutons dans les notifications ntfy** : renseigner `PUBLIC_URL` avec une adresse joignable depuis le téléphone, par exemple via le VPN Server de DSM ou Tailscale. Les liens sont signés et valables 12 h.
- **Signal de vie** : créer un « check » gratuit sur healthchecks.io (période 5 min) et mettre son URL dans `HEARTBEAT_URL`. Tu es alors prévenu si le NAS ou HA Guardian s'arrête.
- **HTTPS** : *Panneau de configuration* > *Portail de connexion* > *Avancé* > **Proxy inversé**. Source `https://guardian.ton-domaine:443`, destination `http://localhost:8099`.

## Mise à jour de HA Guardian

Une **version** est publiée quand le numéro du fichier `VERSION` change sur `main`. GitHub Actions :
1. lance les tests ;
2. construit l'image et la démarre pour vérifier qu'elle répond ;
3. l'analyse avec Trivy (une faille critique bloque la publication) ;
4. publie `ghcr.io/jgondellier/ha-guardian:latest` et `:X.Y.Z`, avec une attestation de provenance signée ;
5. crée la release GitHub avec les notes du `CHANGELOG.md`.

Ensuite, dans l'interface (vérification toutes les 6 h ou avec le bouton *Vérifier*), un bandeau **« Nouvelle version disponible »** liste les nouveautés. Clique sur **Mettre à jour** : Watchtower télécharge l'image et recrée le conteneur, puis la page se recharge. L'historique, les réglages et la clé SSH sont conservés.

Les modifications poussées sans changer `VERSION` ne produisent qu'une image `edge`, jamais proposée à la mise à jour.

**Revenir à une version précédente** : dans `docker-compose.yml`, remplace `:latest` par la version voulue (par exemple `:2.0.0`), puis lance `sudo docker-compose up -d`. Remets `:latest` pour suivre à nouveau les mises à jour.

**Vérifier qu'une image vient bien de ce dépôt** (depuis un poste avec la CLI GitHub) :
```sh
gh attestation verify oci://ghcr.io/jgondellier/ha-guardian:latest --owner jgondellier
```

> Le paquet GHCR doit être **public** pour que le NAS le télécharge sans authentification : sur GitHub, ouvre *Packages* > *ha-guardian* > *Package settings* > *Change visibility*.

## Développement

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

## Sécurité

- Ne publie pas le port 8099 sur Internet : pour un accès à distance, passe par un VPN.
- `.env` contient le jeton HA et n'est jamais envoyé sur GitHub (voir `.gitignore`).
- Les conteneurs tournent avec un système de fichiers en lecture seule et sans élévation de privilèges.
- Watchtower a accès au Docker du NAS (c'est nécessaire pour remplacer le conteneur). Son API n'écoute que sur le NAS lui-même et exige `UPDATE_TOKEN`. La principale protection reste donc ton compte GitHub : active la double authentification.

## Dépannage

- **Journaux** : `sudo docker logs -f ha-guardian` et `sudo docker logs ha-guardian-watchtower`.
- **`can't start new thread`** (ne devrait pas arriver avec l'image Alpine) : ajouter `security_opt: [seccomp:unconfined]` au service `ha-guardian`.
- **« Watchtower injoignable »** : vérifier que le conteneur `ha-guardian-watchtower` tourne et que `UPDATE_TOKEN` est rempli dans `.env`, puis relancer `sudo docker-compose up -d`.
- **Construire localement sans GitHub** : dans `docker-compose.yml`, décommenter `build: .`, puis lancer `sudo docker-compose up -d --build`.
