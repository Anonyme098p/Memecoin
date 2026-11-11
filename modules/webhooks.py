"""
modules/webhooks.py — Routes du serveur web (FastAPI).

  GET  /                 -> petit message pour vérifier que le serveur répond
  GET  /health           -> utilisé par Railway pour savoir si l'appli est en vie
  POST /webhooks/helius  -> recevra plus tard les webhooks Helius (pour l'instant : on log et on répond OK)

Si la variable WEBHOOK_SECRET est définie, l'en-tête "Authorization" de la
requête doit contenir exactement ce secret (Helius permet de le configurer).
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

import config
import db

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def accueil() -> dict:
    return {"message": "Bot memecoins Solana en ligne"}


@router.get("/health")
async def sante() -> dict:
    return {"statut": "ok", "pause": db.est_en_pause()}


@router.post("/webhooks/helius")
async def webhook_helius(request: Request, authorization: str | None = Header(default=None)) -> dict:
    # Vérification du secret (seulement s'il est configuré)
    if config.WEBHOOK_SECRET and authorization != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Secret invalide")

    donnees = await request.json()
    nombre = len(donnees) if isinstance(donnees, list) else 1
    log.info("Webhook Helius reçu : %d événement(s)", nombre)

    # TODO : analyser les transactions reçues et appeler modules.alertes.envoyer_alerte()
    # L'application Telegram est accessible via : request.app.state.telegram

    return {"recu": nombre}
