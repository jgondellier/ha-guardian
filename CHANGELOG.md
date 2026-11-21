# Historique des versions

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
