"""
modules/webhooks.py — Route du webhook Helius (les routes / et /health sont dans main.py).

  POST /helius   -> reçoit les transactions envoyées par le webhook Helius

Sécurité : Helius recopie notre secret (variable HELIUS_WEBHOOK_SECRET) dans
l'en-tête "Authorization" de chaque requête. Toute requête sans ce secret est refusée.
"""

import asyncio
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

import config
import db
from modules import wallets

log = logging.getLogger(__name__)

router = APIRouter()


def _vient_de_helius(authorization: str | None) -> bool:
    """Compare l'en-tête reçu avec notre secret (compare_digest évite les attaques par chronométrage)."""
    if not config.HELIUS_WEBHOOK_SECRET:
        return False  # pas de secret configuré : on refuse tout, par sécurité
    return hmac.compare_digest((authorization or "").encode(), config.HELIUS_WEBHOOK_SECRET.encode())


@router.post("/helius")
async def webhook_helius(
    request: Request,
    taches: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict:
    if not _vient_de_helius(authorization):
        log.warning("Requête refusée sur /helius (mauvais en-tête Authorization)")
        raise HTTPException(status_code=401, detail="Non autorisé")

    try:
        donnees = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON invalide")

    # Helius envoie une liste de transactions
    transactions = donnees if isinstance(donnees, list) else [donnees]
    log.info("Webhook Helius reçu : %d transaction(s)", len(transactions))
    db.ecrire_parametre("dernier_webhook_recu", db.maintenant())  # affiché par /webhook

    # Helius veut une réponse en moins d'1 seconde : on répond tout de suite
    # et on traite les transactions juste après, en arrière-plan.
    taches.add_task(_traiter_quand_le_bot_est_pret, request.app, transactions)
    return {"recu": len(transactions)}


async def _traiter_quand_le_bot_est_pret(app, transactions: list) -> None:
    """Si un achat arrive pendant que le bot démarre encore, on l'attend (60 s maximum)."""
    for _ in range(120):
        if getattr(app.state, "telegram", None) is not None:
            await wallets.traiter_webhook(app.state.telegram, transactions)
            return
        await asyncio.sleep(0.5)
    log.error("Bot Telegram toujours pas démarré : %d transaction(s) Helius non traitée(s)", len(transactions))
