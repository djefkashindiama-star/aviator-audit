# Audit statistique Aviator

Projet open source sous licence MIT.

Ce projet collecte des résultats historiques afin d'étudier leur distribution et
leur indépendance. Il **ne promet pas de prédire la prochaine manche** et ne place
aucun pari.

Le dossier `browser-relay` contient une extension Chrome en lecture seule. Elle
s'exécute dans l'iframe Aviator déjà authentifiée sur l'ordinateur de l'utilisateur
et transmet uniquement chaque multiplicateur terminé vers Render. La campagne
de 20 jours démarre à la première manche reçue, pas au déploiement.
Le lanceur macOS `launch_aviator_relay.command` ouvre automatiquement un profil
Chrome dédié avec cette extension.

Sur la machine de collecte, `relay_watchdog.py` peut être installé comme agent
macOS permanent. Il maintient Chrome et la prévention de mise en veille actifs,
contrôle Render toutes les 30 secondes, rouvre la page après cinq minutes sans
nouvelle manche puis redémarre le profil dédié si le blocage persiste. La file
locale de l'extension et la date de fin de campagne sont conservées. Le relais ne
stocke aucun identifiant PremierBet : une session expirée doit donc toujours être
reconnectée par son propriétaire.

## Interface locale React

Le tableau de bord affiche automatiquement la base SQLite, les dernières manches,
la distribution des multiplicateurs, le volume horaire, les taux de dépassement,
l'autocorrélation et les résultats de validation chronologique.

Lancez l'ensemble avec :

```bash
python3 start_dashboard.py
```

La page s'ouvre sur `http://localhost:3000` et se rafraîchit toutes les cinq
secondes. Utilisez `Ctrl+C` dans le terminal pour arrêter les services locaux.

Lorsque `config.json` contient la source réelle, le même lanceur démarre une
campagne de **20 jours exactement**. Après une coupure, la campagne reprend avec
la date de fin d'origine au lieu de recommencer à zéro.

Pour chaque lecture réussie, le système conserve à la fois les manches
normalisées et la réponse JSON complète compressée, avec son empreinte SHA-256.
Les erreurs, tentatives, taux de succès et dates de dernière lecture restent
également enregistrés dans SQLite et visibles dans l'interface.

## État vérifié de la source PremierBet

Le catalogue public identifie Aviator avec l'identifiant `291195`, le fournisseur
`36`, et indique `isFunModeAvailable: false`. L'application PremierBet demande
ensuite `GET /cd/v1/casino/game/291195/launch-url`; cet endpoint répond
`401 Unauthorized` sans session PremierBet authentifiée. Le flux du fournisseur et
l'historique des manches ne sont donc jamais chargés pour un visiteur public.

Le service déployé sonde ces deux endpoints toutes les cinq minutes et expose le
diagnostic dans `/health` et dans le tableau de bord. La campagne de 20 jours ne
démarre qu'à la première source réelle autorisée; il n'injecte pas de valeurs
aléatoires ou de données d'un autre opérateur.

Depuis le service Render actuel, Cloudflare renvoie en plus `403 Forbidden` sur
le catalogue de jeu alors que le même endpoint public répond depuis une connexion
locale. Le tableau de bord distingue ce blocage réseau de l'authentification
`401` observée localement sur l'URL de lancement.

Ne contournez pas de connexion, CAPTCHA, limitation de débit ou protection du
site. Vérifiez les conditions d'utilisation et n'enregistrez jamais de cookie ou
mot de passe dans `config.json`.

## Format stocké

Chaque observation contient :

- l'identifiant de manche ;
- l'heure observée et, si disponible, l'heure de la manche ;
- le multiplicateur final ;
- la réponse JSON brute pour audit ;
- une contrainte d'unicité qui empêche les doublons.

## Démarrage

Python 3.10 ou plus récent suffit, sans dépendance externe.

```bash
cp config.example.json config.json
python3 aviator_audit.py --db aviator.sqlite3 collect --config config.json --once
python3 aviator_audit.py --db aviator.sqlite3 collect --config config.json --duration-days 20
```

Pour une collecte de vingt jours, laissez le second processus tourner dans un
terminal dédié. Le programme reprend sans dupliquer les manches après une coupure
et ralentit automatiquement en cas d'erreurs répétées.

Import alternatif d'un export CSV :

```bash
python3 aviator_audit.py --db aviator.sqlite3 import-csv historique.csv
```

Le CSV doit avoir les colonnes `round_id`, `timestamp` et `multiplier`.

## Analyse

```bash
python3 aviator_audit.py --db aviator.sqlite3 analyze --output report.json
```

Le rapport mesure notamment :

- les quantiles et fréquences de dépassement de plusieurs multiplicateurs ;
- les intervalles de confiance à 95 % ;
- l'autocorrélation des multiplicateurs logarithmiques sur 50 retards ;
- un test chronologique 80/20 d'un petit modèle fondé sur les cinq manches
  précédentes, comparé à une prédiction constante.

L'évaluation chronologique est essentielle : un motif trouvé dans les données
d'entraînement mais absent des 20 % de manches suivantes est seulement du
surapprentissage.

## Déploiement continu sur Render

Le dépôt contient un `Dockerfile`, un point d'entrée `render_start.py` et un
Blueprint `render.yaml`. Ils lancent dans le même service l'interface, l'API et
le collecteur. La base SQLite et les réponses JSON brutes sont conservées sur
le disque persistant `/var/data`.

Le Blueprint utilise un service **Starter** avec un disque persistant de 1 Go.
Cette configuration évite la mise en veille de l'offre gratuite et permet à la
campagne de reprendre avec sa date de fin d'origine après un redémarrage.

Pour le relais local, `AVIATOR_INGEST_TOKEN` doit contenir un secret aléatoire
conservé uniquement dans Render et dans `browser-relay/config.js`. L'endpoint
`POST /api/ingest` refuse les requêtes sans ce secret, valide strictement les
valeurs et déduplique les reprises réseau par identifiant de manche.

La source ne doit jamais être inscrite dans Git. Ajoutez dans les variables
d'environnement Render l'une de ces deux configurations :

- `AVIATOR_CONFIG_JSON` : le contenu JSON complet au format de
  `config.example.json` ;
- ou `AVIATOR_SOURCE_URL`, puis au besoin `AVIATOR_ITEMS_PATH`,
  `AVIATOR_ROUND_ID_PATH`, `AVIATOR_TIMESTAMP_PATH`,
  `AVIATOR_MULTIPLIER_PATH`, `AVIATOR_HEADERS_JSON` et
  `AVIATOR_POLL_SECONDS`.

Tant qu'aucune source JSON autorisée n'est configurée, le dashboard reste en
ligne mais affiche « collecte en attente ». L'endpoint `/health` expose l'état
du serveur et du collecteur, sans révéler les secrets.
