"""
Dossier modules/ : les fonctionnalités du bot, chacune dans son fichier.

  - alertes.py      : envoi d'une alerte (respecte la pause, évite les doublons, l'enregistre en base)
  - surveillance.py : boucle de fond qui tournera en permanence (logique Helius à ajouter)
  - webhooks.py     : routes du serveur web (FastAPI), dont /helius qui reçoit les transactions
  - wallets.py      : suivi des wallets (/addwallet, /removewallet, /wallets) et alertes d'achat
  - helius.py       : appels à l'API Helius (gestion du webhook, infos d'un token)
  - tokens.py       : nom, symbole et prix d'un token (Helius + DexScreener)

Pour ajouter une fonctionnalité, crée un nouveau fichier ici (ex : modules/pumpfun.py).
"""
