"""
modules/webhooks.py — Routes du serveur web (FastAPI).

  GET  /         -> petit message pour vérifier que le serveur répond
  GET  /health   -> utilisé par Railway pour savoir si l'appli est en vie
  POST /helius   -> reçoit les transactions envoyées par le webhook Helius

Sécurité : Helius recopie notre secret (variable HELIUS_WEBHOOK_SECRET) dans
l'en-tête "Authorization" de chaque requête. Toute requête sans ce secret est refusée.
"""

import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

import config
import db
from modules import wallets

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def accueil() -> dict:
    return {"message": "Bot memecoins Solana en ligne"}


@router.get("/health")
async def sante() -> dict:
    return {"statut": "ok", "pause": db.est_en_pause()}


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
    taches.add_task(wallets.traiter_webhook, request.app.state.telegram, transactions)
    return {"recu": len(transactions)}
