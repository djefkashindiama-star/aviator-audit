# Relais local Aviator

Cette extension Chrome lit uniquement le premier multiplicateur terminé dans
l'historique SPRIBE (`.payouts-block`). Elle n'accède pas au mot de passe, au
solde, aux mises, au chat ou aux cookies PremierBet.

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
