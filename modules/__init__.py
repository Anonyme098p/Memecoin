"""
Dossier modules/ : les fonctionnalités du bot, chacune dans son fichier.

  - alertes.py      : envoi d'une alerte (respecte la pause, évite les doublons, l'enregistre en base)
  - surveillance.py : boucle de fond qui tournera en permanence (logique Helius à ajouter)
  - webhooks.py     : routes du serveur web (FastAPI) pour recevoir des webhooks plus tard

Pour ajouter une fonctionnalité, crée un nouveau fichier ici (ex : modules/pumpfun.py).
"""
