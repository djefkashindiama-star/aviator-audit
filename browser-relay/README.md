# Relais local Aviator

Cette extension Chrome lit uniquement l'identifiant et le multiplicateur final
de chaque manche dans l'historique Aviator. Elle prend en charge l'interface
actuelle `aviator.studio` et l'ancien historique DOM SPRIBE. Elle n'accède pas au
mot de passe, au solde, aux mises, au chat ou aux cookies PremierBet.

Les manches sont mises en file dans le stockage local de l'extension, puis
transmises au point d'ingestion Render. La file est réessayée automatiquement
après une coupure réseau et le serveur déduplique les identifiants de manche.

## Installation

Sur macOS, double-cliquer sur `launch_aviator_relay.command` à la racine du
projet ouvre un profil Chrome dédié avec l'extension et la bonne page. Il suffit
alors de se connecter à PremierBet dans cette fenêtre et de laisser Aviator ouvert.

Installation manuelle alternative :

1. Copier `config.example.js` vers `config.js` et y placer le secret configuré
   dans `AVIATOR_INGEST_TOKEN` sur Render.
2. Ouvrir `chrome://extensions`, activer **Mode développeur**, puis choisir
   **Charger l'extension non empaquetée** et ce dossier `browser-relay`.
3. Recharger la page Aviator. Le badge affiche `ON` lorsque l'historique est
   détecté. La campagne de 20 jours démarre à la première manche suivante.

Le secret local `config.js` est ignoré par Git et ne doit jamais être publié.
