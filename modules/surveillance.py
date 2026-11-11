"""
modules/surveillance.py — Boucle de surveillance qui tourne en arrière-plan.

Pour l'instant, elle ne fait que vérifier l'état (pause ou non) et lister
les wallets/devs suivis. C'est ici que tu brancheras plus tard les appels
à l'API Helius (config.HELIUS_API_KEY) pour détecter les achats, les
nouveaux tokens, etc., puis appeler alertes.envoyer_alerte().
"""

import asyncio
import logging

from telegram.ext import Application

import config
import db

log = logging.getLogger(__name__)


async def un_tour(application: Application) -> None:
    """Un passage de surveillance. À compléter avec la vraie logique."""
    wallets = db.lister_wallets()
    devs = db.lister_devs()
    log.debug("Surveillance : %d wallets, %d devs suivis", len(wallets), len(devs))

    # Exemple de ce que tu pourras faire plus tard :
    #
    #   for wallet in wallets:
    #       achats = await recuperer_achats_helius(wallet["adresse"])   # à écrire
    #       for achat in achats:
    #           await alertes.envoyer_alerte(
    #               application,
    #               type_alerte="wallet",
    #               message=f"🟢 {wallet['nom']} a acheté {achat['symbole']}",
    #               token_adresse=achat["mint"],
    #           )


async def boucle(application: Application) -> None:
    """Tourne en continu tant que le bot est allumé."""
    log.info("Boucle de surveillance démarrée (toutes les %ss)", config.INTERVALLE_SURVEILLANCE)
    while True:
        try:
            if not db.est_en_pause():
                await un_tour(application)
        except asyncio.CancelledError:
            raise  # arrêt normal du bot : on laisse remonter
        except Exception:
            # Une erreur ne doit jamais arrêter la boucle : on la note et on continue
            log.exception("Erreur pendant la surveillance")
        await asyncio.sleep(config.INTERVALLE_SURVEILLANCE)
