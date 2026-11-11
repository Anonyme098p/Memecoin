"""
main.py — Point d'entrée du programme.

Lance dans le MÊME processus (une seule boucle asyncio, rien ne bloque) :
  - le serveur web FastAPI : routes /, /health, webhook Helius /helius, page /tglogin ;
  - le bot Telegram (mode "polling" : il va chercher lui-même les nouveaux messages) ;
  - les tâches de fond : surveillance, analyse du dimanche, nouveaux tokens pump.fun, veille Telegram.

Ordre de démarrage important pour Railway :
  1. le serveur web ouvre TOUT DE SUITE son port (0.0.0.0 et la variable PORT) ;
  2. le bot et les tâches de fond démarrent ensuite, en arrière-plan.
Ainsi l'adresse publique répond même si Telegram ou Helius sont lents à répondre au démarrage.

Démarrage : python main.py
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from telegram.error import InvalidToken

import config
import db
import telegram_bot
from modules import nouveaux_tokens, surveillance, veille, wallets, webhooks

# Configuration des logs (visibles dans l'onglet "Deploy Logs" de Railway)
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
# On réduit le bruit des bibliothèques (sinon chaque requête Telegram est affichée)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("main")

# Port d'écoute : Railway fournit la variable PORT ; en local, 8000
PORT = int(os.environ.get("PORT", 8000))


async def demarrer_bot(app: FastAPI) -> None:
    """
    Démarre le bot Telegram puis les tâches de fond, en arrière-plan.
    En cas de souci réseau, on réessaie (5 s, 10 s, 20 s... jusqu'à 5 min) : le serveur web,
    lui, continue de répondre pendant ce temps.
    """
    etat = app.state.etat_bot
    attente = 5
    while True:
        application = telegram_bot.construire_application()
        try:
            await application.initialize()  # vérifie le token auprès de Telegram
            await application.start()
            # drop_pending_updates=True : on ignore les commandes envoyées pendant que le bot était éteint
            await application.updater.start_polling(drop_pending_updates=True)
            break
        except InvalidToken:
            etat.update(statut="erreur", erreur="TELEGRAM_TOKEN refusé par Telegram : vérifie la variable sur Railway")
            log.error(etat["erreur"])
            return  # inutile de réessayer : il faut corriger la variable
        except Exception as erreur:
            etat.update(statut="nouvel essai", erreur=f"{type(erreur).__name__} : {erreur}"[:200])
            log.warning("Démarrage du bot Telegram impossible (%s) : nouvel essai dans %d s", type(erreur).__name__, attente)
            await asyncio.sleep(attente)
            attente = min(attente * 2, 300)

    app.state.telegram = application  # utilisé par les routes web (webhook Helius, /tglogin)
    etat.update(statut="ok", erreur=None, depuis=time.time())
    log.info("Bot Telegram démarré")

    # Tâches de fond (chacune tourne en boucle de son côté)
    app.state.taches.update({
        "surveillance": asyncio.create_task(surveillance.boucle(application)),
        "analyse_hebdomadaire": asyncio.create_task(wallets.boucle_hebdomadaire(application)),
        "flux_pump_fun": asyncio.create_task(nouveaux_tokens.ecouter(application)),
        "scanall": asyncio.create_task(nouveaux_tokens.boucle_scan(application)),
        "bilan_devs": asyncio.create_task(nouveaux_tokens.boucle_maj_devs(application)),
        "veille_telegram": asyncio.create_task(veille.demarrer(application)),
    })

    # On remet le webhook Helius à jour (le domaine ou la liste des wallets a pu changer).
    # S'il existe déjà un webhook pour PUBLIC_URL/helius, il est modifié : jamais de doublon.
    try:
        etat_webhook = await asyncio.wait_for(wallets.synchroniser_webhook(), 60)
    except Exception as erreur:
        etat_webhook = f"⚠️ Webhook Helius non vérifié au démarrage ({type(erreur).__name__})"
    log.info(etat_webhook)
    try:
        await telegram_bot.envoyer_message(application, "🚀 Bot démarré.\n" + etat_webhook)
    except Exception:
        log.exception("Impossible d'envoyer le message de démarrage (vérifie TELEGRAM_CHAT_ID)")


async def arreter_bot(app: FastAPI) -> None:
    """Arrêt propre (redéploiement, Ctrl+C...)."""
    log.info("Arrêt en cours...")
    await veille.arreter()
    for tache in list(app.state.taches.values()):
        tache.cancel()
    for tache in list(app.state.taches.values()):
        try:
            await tache
        except (asyncio.CancelledError, Exception):
            pass
    application = app.state.telegram
    if application is not None:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    log.info("Bot arrêté proprement")


@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    """
    Avant le "yield" : ce qui se passe au démarrage du serveur web. On y fait le strict minimum
    (la base de données) et on lance le bot EN ARRIÈRE-PLAN : uvicorn n'ouvre le port qu'une fois
    cette partie terminée, il ne faut donc rien y attendre de lent.
    Après le "yield" : l'arrêt.
    """
    db.init_db()
    log.info("Base de données prête : %s", config.DB_PATH)
    app.state.telegram = None
    app.state.taches = {}
    app.state.etat_bot = {"statut": "démarrage", "erreur": None, "depuis": None, "lance_le": time.time()}
    app.state.taches["demarrage_bot"] = asyncio.create_task(demarrer_bot(app))
    log.info("Serveur web en écoute sur 0.0.0.0:%d (le bot Telegram démarre en arrière-plan)", PORT)

    yield  # <- le programme tourne ici jusqu'à l'arrêt

    await arreter_bot(app)


# L'application web FastAPI
app = FastAPI(title="Blot-nsm", lifespan=cycle_de_vie)
app.include_router(webhooks.router)  # /, /health, /helius
app.include_router(veille.router)    # page /tglogin


if __name__ == "__main__":
    # host="0.0.0.0" est obligatoire sur Railway pour être joignable depuis l'extérieur.
    # proxy_headers : Railway passe par un proxy ; on lui fait confiance pour l'IP et le https.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=config.LOG_LEVEL.lower(),
                proxy_headers=True, forwarded_allow_ips="*")
