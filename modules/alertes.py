"""
modules/alertes.py — Envoi centralisé des alertes.

Toutes les alertes du bot doivent passer par la fonction envoyer_alerte() :
  1. si le bot est en pause (/pause), rien n'est envoyé ;
  2. si on a déjà alerté sur ce token pour ce type d'alerte, on ne renvoie pas ;
  3. sinon, le message part sur Telegram et il est enregistré en base.
"""

import logging

from telegram.ext import Application

import db
import telegram_bot

log = logging.getLogger(__name__)


async def envoyer_alerte(
    application: Application,
    type_alerte: str,
    message: str,
    token_adresse: str | None = None,
) -> bool:
    """Envoie une alerte. Renvoie True si elle a vraiment été envoyée."""
    if db.est_en_pause():
        log.info("Alerte ignorée (bot en pause) : %s", type_alerte)
        return False

    if token_adresse and db.alerte_deja_envoyee(token_adresse, type_alerte):
        log.info("Alerte déjà envoyée pour %s (%s), on passe", token_adresse, type_alerte)
        return False

    try:
        await telegram_bot.envoyer_message(application, message)
    except Exception:
        log.exception("Impossible d'envoyer l'alerte sur Telegram")
        return False

    db.enregistrer_alerte(type_alerte, message, token_adresse)
    return True
