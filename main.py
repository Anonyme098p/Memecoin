"""
main.py — Point d'entrée du programme.

Lance en même temps :
  - le serveur web FastAPI (pour recevoir des webhooks, et le "health check" de Railway) ;
  - le bot Telegram (en mode "polling" : il va chercher lui-même les nouveaux messages) ;
  - la boucle de surveillance en arrière-plan.

Tout tourne dans le même processus. Démarrage : python main.py
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import config
import db
import telegram_bot
from modules import surveillance, wallets, webhooks

# Configuration des logs (visibles dans l'onglet "Deploy Logs" de Railway)
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
# On réduit le bruit des bibliothèques (sinon chaque requête Telegram est affichée)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("main")


@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    """
    Code exécuté au démarrage (avant le "yield") et à l'arrêt (après le "yield")
    du serveur web. C'est ici qu'on allume et qu'on éteint le bot Telegram.
    """
    # --- Démarrage ---
    db.init_db()
    log.info("Base de données prête : %s", config.DB_PATH)

    application = telegram_bot.construire_application()
    await application.initialize()
    await application.start()
    # drop_pending_updates=True : on ignore les commandes envoyées pendant que le bot était éteint
    await application.updater.start_polling(drop_pending_updates=True)
    log.info("Bot Telegram démarré")

    # On rend le bot accessible aux routes web (ex : request.app.state.telegram)
    app.state.telegram = application

    tache_surveillance = asyncio.create_task(surveillance.boucle(application))

    # On remet le webhook Helius à jour (le domaine ou la liste des wallets a pu changer).
    # S'il existe déjà un webhook pour PUBLIC_URL/helius, il est modifié : jamais de doublon.
    etat_webhook = await wallets.synchroniser_webhook()
    log.info(etat_webhook)
    message_demarrage = "🚀 Bot démarré.\n" + etat_webhook

    try:
        await telegram_bot.envoyer_message(application, message_demarrage)
    except Exception:
        log.exception("Impossible d'envoyer le message de démarrage (vérifie TELEGRAM_CHAT_ID)")

    yield  # <- le programme tourne ici jusqu'à l'arrêt

    # --- Arrêt (redéploiement, Ctrl+C...) ---
    log.info("Arrêt en cours...")
    tache_surveillance.cancel()
    try:
        await tache_surveillance
    except asyncio.CancelledError:
        pass
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Bot arrêté proprement")


# L'application web FastAPI
app = FastAPI(title="Bot memecoins Solana", lifespan=cycle_de_vie)
app.include_router(webhooks.router)


if __name__ == "__main__":
    # host="0.0.0.0" est obligatoire sur Railway pour être joignable depuis l'extérieur
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level=config.LOG_LEVEL.lower())
