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

import html
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
        "/wallets — liste des wallets suivis (avec leur note)\n"
        "/score <adresse> — analyser un wallet sans le suivre\n"
        "/debugtx <adresse> — voir comment ses 5 dernières transactions sont comprises\n"
        "/check <adresse> — note de sécurité d'un token\n"
        "/minscore <0-100> — ne recevoir que les alertes au-dessus de ce score\n\n"
        "/adddev <adresse> <surnom> — suivre un créateur de tokens\n"
        "/removedev <adresse> — ne plus le suivre\n"
        "/devs — liste des devs suivis et état du flux pump.fun\n"
        "/devscore <adresse> — bilan des lancements d'un créateur\n"
        "/scanall on <score> | off — alertes pour les créateurs inconnus\n\n"
        "/addchannel <lien> — suivre un canal Telegram (veille)\n"
        "/removechannel <lien> — ne plus le suivre\n"
        "/channels — canaux suivis\n"
        "/veille — état de la veille Telegram\n\n"
        "/stats — résultats des trades simulés (Bot seul contre Mes choix)\n"
        "/journal — les 10 derniers trades simulés\n"
        "/positions — trades simulés encore ouverts\n"
        "Sous chaque alerte : ✅ Accepter = achat simulé dans « Mes choix »\n\n"
        "/revue — revue IA de la semaine (propositions à accepter ou refuser)\n"
        "/rules — historique des versions des règles\n"
        "/rollback [n] — revenir à une version des règles"
        "/webhook — état du webhook Helius (/webhook sync pour le resynchroniser)"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """État du bot. Chaque partie est calculée à part : si l'une échoue, les autres s'affichent quand même."""
    from modules import supervision  # import ici pour éviter une boucle d'imports
    lignes = [f"<b>En ligne depuis :</b> {_duree_lisible(time.time() - HEURE_DEMARRAGE)}"]
    try:
        stats = db.statistiques()
        lignes += [
            f"<b>Alertes :</b> {'⏸️ en pause' if db.est_en_pause() else '✅ actives'}",
            "",
            f"<b>Wallets suivis :</b> {stats['wallets']} · <b>Devs suivis :</b> {stats['devs']}",
            f"<b>Achats détectés :</b> {stats['achats']} · <b>Alertes envoyées :</b> {stats['alertes']}",
            f"<b>Trades simulés :</b> {stats['trades_ouverts']} ouverts, {stats['trades_fermes']} fermés "
            f"(PnL {stats['pnl_total']:+.3f} SOL)",
        ]
    except Exception as erreur:
        lignes.append(f"❌ Base de données illisible ({type(erreur).__name__})")
    lignes += ["", "<b>💾 Stockage</b>"]
    try:
        lignes.append(db.texte_stockage())
        if not config.BASE_SUR_VOLUME:
            lignes.append("⚠️ <b>Aucun volume : la mémoire sera perdue à chaque mise à jour</b>"
                          if not config.VOLUME_RAILWAY else "⚠️ <b>La base n'est pas sur le volume : mémoire perdue à chaque mise à jour</b>")
    except Exception as erreur:
        lignes.append(f"❌ Stockage illisible ({type(erreur).__name__})")
    lignes += ["", f"<b>Clé Helius :</b> {config.masquer(config.HELIUS_API_KEY) if config.HELIUS_API_KEY else '⚠️ manquante'}",
               f"<b>Webhook :</b> {config.PUBLIC_URL + '/helius' if config.PUBLIC_URL else '⚠️ PUBLIC_URL non définie'}"
               " (détails : /webhook)"]

    modules = supervision.texte_status()
    if modules:
        lignes += ["", "<b>Modules</b>", html.escape(modules, quote=False)]
    if config.PROBLEMES:
        lignes += ["", "<b>⚠️ Variables à corriger sur Railway</b>"]
        lignes += [f"• {html.escape(nom)} : {html.escape(probleme, quote=False)}" for nom, probleme in config.PROBLEMES.items()]
    await update.message.reply_text("\n".join(lignes), parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))


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

# Commandes de chaque module : utilisées pour répondre proprement si le module ne se charge pas
COMMANDES_MODULES = {
    "wallets": ("wallets et alertes d'achat",
                ["addwallet", "removewallet", "wallets", "webhook", "score", "debugtx", "check", "minscore"]),
    "nouveaux_tokens": ("nouveaux tokens", ["adddev", "removedev", "devs", "devscore", "scanall"]),
    "veille": ("veille Telegram", ["addchannel", "removechannel", "channels", "veille"]),
    "simulation": ("simulation", ["stats", "journal", "positions"]),
    "ia": ("IA", ["revue", "rules", "rollback"]),
}


def _commande_indisponible(nom_module: str, raison: str):
    async def repondre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"⏸️ Commande indisponible : le module « {nom_module} » est désactivé "
                                        f"({raison}). Détails : /status")
    return repondre


def construire_application() -> Application:
    """Crée l'application Telegram et branche les commandes."""
    application = Application.builder().token(config.TELEGRAM_TOKEN).build()

    # Chaque commande n'est acceptée que si elle vient de TON chat (filtre SEULEMENT_MOI)
    application.add_handler(CommandHandler("start", cmd_start, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("status", cmd_status, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("pause", cmd_pause, filters=SEULEMENT_MOI))
    application.add_handler(CommandHandler("resume", cmd_resume, filters=SEULEMENT_MOI))

    # Commandes des modules. Chaque module est branché à part : si l'un ne peut pas se charger
    # (bibliothèque manquante, erreur), ses commandes répondent qu'il est désactivé, et les autres marchent.
    import importlib

    from modules import supervision
    for module, (nom_affiche, commandes) in COMMANDES_MODULES.items():
        try:
            importlib.import_module(f"modules.{module}").enregistrer_commandes(application)
        except Exception as erreur:
            log.exception("Commandes du module %s indisponibles", module)
            raison = f"chargement impossible ({type(erreur).__name__} : {erreur})"[:160]
            supervision.marquer(nom_affiche, "désactivé", raison)
            application.add_handler(CommandHandler(commandes, _commande_indisponible(nom_affiche, raison),
                                                   filters=SEULEMENT_MOI))

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
