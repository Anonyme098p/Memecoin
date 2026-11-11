"""
telegram_bot.py — Le bot Telegram (bibliothèque python-telegram-bot).

Sécurité : le bot ne répond QU'À ton chat (TELEGRAM_CHAT_ID).
Les messages venant de n'importe qui d'autre sont simplement ignorés.

Commandes :
  /start   — message d'accueil
  /status  — état du bot et quelques statistiques
  /pause   — met les alertes en pause
  /resume  — relance les alertes
  (+ les commandes des modules, ex : /addwallet, /removewallet, /wallets dans modules/wallets.py)
"""

import logging
import time

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

import config
import db

log = logging.getLogger(__name__)

# Heure de démarrage, pour afficher depuis combien de temps le bot tourne
HEURE_DEMARRAGE = time.time()

# Filtre : on n'accepte que les messages venant de TON chat
SEULEMENT_MOI = filters.Chat(chat_id=config.TELEGRAM_CHAT_ID)


def _duree_lisible(secondes: float) -> str:
    """Transforme un nombre de secondes en texte du type '2j 3h 15min'."""
    secondes = int(secondes)
    jours, secondes = divmod(secondes, 86400)
    heures, secondes = divmod(secondes, 3600)
    minutes = secondes // 60
    morceaux = []
    if jours:
        morceaux.append(f"{jours}j")
    if heures:
        morceaux.append(f"{heures}h")
    morceaux.append(f"{minutes}min")
    return " ".join(morceaux)


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Salut ! Je suis ton bot d'alertes memecoins Solana.\n\n"
        "Commandes disponibles :\n"
        "/status — état du bot\n"
        "/pause — mettre les alertes en pause\n"
        "/resume — relancer les alertes\n\n"
        "/addwallet <adresse> <surnom> — suivre un wallet\n"
        "/removewallet <adresse> — ne plus suivre un wallet\n"
        "/wallets — liste des wallets suivis"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = db.statistiques()
    etat = "⏸️ En pause" if db.est_en_pause() else "✅ Actif"
    texte = (
        f"<b>État :</b> {etat}\n"
        f"<b>En ligne depuis :</b> {_duree_lisible(time.time() - HEURE_DEMARRAGE)}\n\n"
        f"<b>Wallets suivis :</b> {stats['wallets']}\n"
        f"<b>Devs suivis :</b> {stats['devs']}\n"
        f"<b>Achats détectés :</b> {stats['achats']}\n"
        f"<b>Alertes envoyées :</b> {stats['alertes']}\n"
        f"<b>Trades simulés :</b> {stats['trades_ouverts']} ouverts, {stats['trades_fermes']} fermés\n"
        f"<b>PnL simulé :</b> {stats['pnl_total']:+.3f} SOL\n\n"
        f"<b>Clé Helius :</b> {config.masquer(config.HELIUS_API_KEY)}\n"
        f"<b>Webhook :</b> {config.PUBLIC_URL + '/helius' if config.PUBLIC_URL else '⚠️ PUBLIC_URL non définie'}"
    )
    await update.message.reply_text(texte, parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.definir_pause(True)
    log.info("Alertes mises en pause via Telegram")
    await update.message.reply_text("⏸️ Alertes en pause. Tape /resume pour les relancer.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.definir_pause(False)
    log.info("Alertes relancées via Telegram")
    await update.message.reply_text("▶️ Alertes relancées !")


async def gestion_erreurs(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toute erreur dans une commande est écrite dans les logs au lieu de faire planter le bot."""
    log.error("Erreur dans le bot Telegram", exc_info=context.error)


# ---------------------------------------------------------------------------
# Construction de l'application
# ---------------------------------------------------------------------------

def construire_application() -> Application:
    """Crée l'application Telegram et branche les commandes."""
    application = Application.builder().token(config.TELEGRAM_TOKEN).build()

    # Chaque commande n'est acceptée que si elle vient de TON chat (filtre SEULEMENT_MOI)
    application.add_handler(CommandHandler("start", cmd_start, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("status", cmd_status, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("pause", cmd_pause, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("resume", cmd_resume, filters=SEULEMENT_MOI))

    # Commandes des modules (import ici pour éviter une boucle d'imports)
    from modules import wallets
    wallets.enregistrer_commandes(application)

    application.add_error_handler(gestion_erreurs)
    return application


async def envoyer_message(
    application: Application,
    texte: str,
    boutons: InlineKeyboardMarkup | None = None,
) -> Message:
    """
    Envoie un message dans ton chat (utilisé pour les alertes et les notifications).
    `boutons` : boutons optionnels affichés sous le message.
    """
    return await application.bot.send_message(
        chat_id=config.TELEGRAM_CHAT_ID,
        text=texte,
        parse_mode=ParseMode.HTML,
        reply_markup=boutons,
        link_preview_options=LinkPreviewOptions(is_disabled=True),  # pas d'aperçu des liens
    )
