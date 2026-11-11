"""
Dossier modules/ : les fonctionnalités du bot, chacune dans son fichier.

  - alertes.py      : envoi d'une alerte (respecte la pause, évite les doublons, l'enregistre en base)
  - surveillance.py : boucle de fond qui tournera en permanence (logique Helius à ajouter)
  - webhooks.py     : routes du serveur web (FastAPI), dont /helius qui reçoit les transactions
  - wallets.py      : suivi des wallets (/addwallet, /removewallet, /wallets) et alertes d'achat
  - analyse.py      : analyse de l'historique d'un wallet (trades, risques, note, verdict)
  - transactions.py : lecture des transactions Helius (détection des achats et des ventes)
  - helius.py       : appels à l'API Helius (webhook, historique, infos d'un token)
  - tokens.py       : nom, symbole et prix d'un token (Helius + DexScreener)

Pour ajouter une fonctionnalité, crée un nouveau fichier ici (ex : modules/pumpfun.py).
"""
