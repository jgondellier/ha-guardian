# Historique des versions

## 2.3.0
- Santé de Home Assistant : état des apps (ex add-ons), alerte si l'une s'arrête et redémarrage automatique optionnel (limité, respecte la maintenance).
- Intégrations en échec et réparations proposées par HA, nombre d'erreurs dans le journal (alerte en cas de pic), taille de la base de données.
- Batteries faibles et appareils à batterie injoignables, dans l'interface et le rapport hebdomadaire.
- Réseau : surveillance des équipements (ping, port ou URL) et des routeurs Asus AiMesh (nœuds, charge, appareils connectés).
- Si un équipement critique (routeur principal) tombe, aucune intervention n'est faite sur Home Assistant : la panne vient du réseau.
- Certificats TLS vérifiés pour les équipements surveillés par URL (un certificat auto-signé compte comme « joignable ») et pour le routeur Asus (`ASUS_VERIFY_TLS`). Nouveau réglage `HA_VERIFY_TLS` (désactivé par défaut, comportement inchangé) pour vérifier le certificat de HA en https.

## 2.2.0
- Rappels pendant une panne : tant que Home Assistant reste injoignable, une nouvelle notification part toutes les 30 min (réglable, 0 pour désactiver), avec la durée de la panne, les actions déjà tentées et les boutons d'action.
- Si l'alerte initiale a été retenue par le mode maintenance, un rappel part une fois la maintenance terminée si la panne continue.

## 2.1.2
- Correctif : les commandes `ha` lancées par SSH (sauvegarde avant mise à jour, sauvegardes, redémarrage de HA Core ou du mini-PC, disque dans la santé du mini-PC) passent par un shell de connexion. Elles étaient refusées par le Supervisor (« missing or invalid API token ») car le jeton de l'add-on SSH n'était pas chargé.

## 2.1.1
- Python 3.15 (image Alpine) et dépendances à jour (FastAPI, Uvicorn, httpx, AsyncSSH).
- Correctifs de sécurité Alpine appliqués à chaque construction de l'image ; pip et setuptools retirés de l'image finale (plus aucune faille connue de gravité haute ou critique).
- Actions GitHub mises à jour ; tests exécutés avec la même version de Python que l'image.

## 2.1.0
- Mises à jour par versions numérotées : le bandeau affiche les nouveautés de chaque version, et l'on peut revenir à une version précise (retour arrière).
- Tests automatiques (escalade, maintenance, alertes, sauvegardes, API) lancés à chaque modification.
- Chaque image est testée au démarrage puis analysée (Trivy) avant publication ; une faille critique bloque la publication.
- Attestation de provenance signée pour chaque image publiée.
- Conteneurs durcis : système de fichiers en lecture seule, pas d'élévation de privilèges.
- Dependabot et analyse CodeQL activés sur le dépôt.

## 2.0.0
- Mode maintenance (pause 1 h / 4 h), activé aussi automatiquement pendant les mises à jour HA.
- Double contrôle avant de compter un échec ; état « lent » au-delà d'un seuil de latence.
- Capteurs témoins : alerte si des capteurs (Zigbee) ne donnent plus signe de vie.
- Santé du mini-PC : disque, mémoire, charge, température, durée de fonctionnement + alertes.
- Sauvegardes HA quotidiennes copiées sur le NAS, rotation, téléchargement, alerte si trop anciennes.
- Mises à jour HA visibles dans l'interface, installation avec sauvegarde préalable.
- Wake-on-LAN (manuel et premier palier si le mini-PC est injoignable).
- Signal de vie (healthchecks.io) pour être prévenu si le NAS ou HA Guardian s'arrête.
- Boutons d'action dans les notifications ntfy (liens signés, valables 12 h).
- Rapport hebdomadaire.
- Empreinte SSH du mini-PC mémorisée et vérifiée ; cookie sécurisé derrière HTTPS.
- Image publiée sur GitHub (ghcr.io) et bouton « Mettre à jour » via Watchtower.

## 1.0.0
- Tableau de bord, actions manuelles, chien de garde à 3 paliers, alertes ntfy/Telegram.
