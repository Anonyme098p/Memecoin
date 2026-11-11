"""
modules/webhooks.py — Routes du serveur web (FastAPI).

  GET  /         -> « Blot-nsm en ligne » (pour vérifier que l'adresse publique répond)
  GET  /health   -> état de chaque module (utilisé aussi par Railway pour savoir si l'appli est en vie)
  POST /helius   -> reçoit les transactions envoyées par le webhook Helius

Sécurité : Helius recopie notre secret (variable HELIUS_WEBHOOK_SECRET) dans
l'en-tête "Authorization" de chaque requête. Toute requête sans ce secret est refusée.
"""

import asyncio
import hmac
import logging
import time

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

import config
import db
from modules import wallets

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=PlainTextResponse)
async def accueil() -> str:
    return "Blot-nsm en ligne"


def _etat_tache(tache: asyncio.Task) -> str:
    if not tache.done():
        return "en cours"
    if tache.cancelled():
        return "arrêtée"
    erreur = tache.exception()
    return f"plantée ({type(erreur).__name__})" if erreur else "terminée"


@router.get("/health")
async def sante(request: Request) -> dict:
    """
    État de chaque module. Répond toujours 200 tant que le serveur web tourne (c'est ce que
    vérifie Railway) ; le champ "statut" passe à "degrade" si un module a un problème.
    Cette page est publique : elle ne montre que des états et des compteurs, aucun secret.
    """
    from modules import nouveaux_tokens, veille  # import ici pour éviter une boucle d'imports

    etat_bot = getattr(request.app.state, "etat_bot", {"statut": "inconnu"})
    taches = {nom: _etat_tache(t) for nom, t in getattr(request.app.state, "taches", {}).items()}
    try:
        with db.connexion() as conn:
            conn.execute("SELECT 1")
        base = {"statut": "ok"}
    except Exception as erreur:
        base = {"statut": "erreur", "erreur": type(erreur).__name__}
    probleme_webhook = wallets._probleme_de_config()
    actif, score = nouveaux_tokens.scanall_reglage() if base["statut"] == "ok" else (False, 0)

    modules = {
        "serveur_web": {"statut": "ok"},
        "base_de_donnees": base,
        "bot_telegram": {
            "statut": etat_bot.get("statut"),
            "erreur": etat_bot.get("erreur"),
            "en_ligne_depuis_s": int(time.time() - etat_bot["depuis"]) if etat_bot.get("depuis") else None,
        },
        "alertes": {"pause": db.est_en_pause(), "score_minimum": wallets.score_minimum()},
        "webhook_helius": {
            "statut": "ok" if not probleme_webhook else "non configuré",
            "wallets_suivis": len(db.lister_wallets()),
            "dernier_recu": db.lire_parametre("dernier_webhook_recu"),
        },
        "flux_pump_fun": {
            "statut": "ok" if nouveaux_tokens.statut["connecte"] else "déconnecté",
            "creations_vues": nouveaux_tokens.statut["creations_vues"],
            "reconnexions": nouveaux_tokens.statut["reconnexions"],
            "devs_suivis": len(db.lister_devs()),
            "scanall": {"actif": actif, "score_minimum": score},
        },
        "veille_telegram": {
            "statut": "ok" if veille.etat["connecte"] else ("désactivée" if veille.configuree() else "déconnectée"),
            "canaux_suivis": len(db.lister_canaux()),
            "messages_lus": veille.etat["messages_lus"],
            "alertes": veille.etat["alertes"],
        },
        "taches_de_fond": taches,
    }
    en_panne = (
        base["statut"] != "ok"
        or etat_bot.get("statut") != "ok"
        or any(e.startswith("plantée") for e in taches.values())
    )
    return {"statut": "degrade" if en_panne else "ok", "modules": modules}


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
