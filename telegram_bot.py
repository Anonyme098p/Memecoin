"""
telegram_bot.py — Le bot Telegram (bibliothèque python-telegram-bot).

Sécurité : le bot ne répond QU'À ton chat (TELEGRAM_CHAT_ID).
Les messages venant de n'importe qui d'autre sont simplement ignorés.

Commandes :
  /start   — message d'accueil
  /aide    — explication de toutes les commandes (liste COMMANDES, aussi envoyée au menu « / »)
  /status  — état du bot et quelques statistiques
  /pause   — met les alertes en pause
  /resume  — relance les alertes
  (+ les commandes des modules, ex : /addwallet, /removewallet, /wallets dans modules/wallets.py)
"""

import html
import logging
import time

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardMarkup, LinkPreviewOptions, Message, Update
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

# ---------------------------------------------------------------------------
# Liste des commandes : une seule liste sert au menu « / » de Telegram ET à /aide
# (commande, arguments, description courte pour le menu, explication pour /aide)
# ---------------------------------------------------------------------------

COMMANDES = [
    ("🤖 Le bot", [
        ("aide", "", "Explication de toutes les commandes", "ce message (aussi : /help)"),
        ("status", "", "État du bot, des modules et du stockage",
         "en ligne depuis quand, alertes actives ou en pause, modules actifs/désactivés, base de données"),
        ("pause", "", "Mettre les alertes en pause", "plus aucune alerte jusqu'à /resume (le bot continue de tout suivre)"),
        ("resume", "", "Relancer les alertes", "remet les alertes en marche"),
    ]),
    ("👛 Wallets suivis", [
        ("addwallet", "<adresse> <surnom>", "Suivre un wallet",
         "tu reçois une alerte à chacun de ses achats (SIGNAL FORT si plusieurs wallets achètent le même token)"),
        ("removewallet", "<adresse>", "Ne plus suivre un wallet", "retire le wallet de la liste"),
        ("wallets", "", "Liste des wallets suivis", "avec leur note et leur verdict"),
        ("score", "<adresse>", "Analyser un wallet (30 jours)",
         "trades, taux de réussite, détention, risques, note 0-100 et verdict SUIVRE / À SURVEILLER / ÉVITER"),
        ("debugtx", "<adresse>", "Voir comment ses transactions sont lues",
         "détail des 5 dernières transactions (utile si /score semble faux)"),
        ("webhook", "[sync]", "État du webhook Helius", "« /webhook sync » le recrée ou le met à jour"),
    ]),
    ("🧪 Tokens", [
        ("check", "<adresse>", "Note de sécurité d'un token",
         "liquidité, holders, mint/freeze, créateur… note 0-100, chiffres clés et drapeaux rouges"),
        ("minscore", "<0-100>", "Score minimum des alertes",
         "seules les alertes au-dessus de ce score te sont envoyées (0 = toutes)"),
    ]),
    ("🚀 Nouveaux tokens pump.fun", [
        ("adddev", "<adresse> <surnom>", "Suivre un créateur de tokens", "alerte PRIORITAIRE dès qu'il lance un token"),
        ("removedev", "<adresse>", "Ne plus suivre un créateur", "le retire de la liste"),
        ("devs", "", "Créateurs suivis et flux pump.fun", "liste des devs suivis et état de la connexion temps réel"),
        ("devscore", "<adresse>", "Bilan d'un créateur",
         "tokens lancés, réussites (> 100k), rugs, et verdict SUIVRE / À SURVEILLER / ÉVITER"),
        ("scanall", "on <score> | off", "Alertes pour les créateurs inconnus",
         "analyse tous les nouveaux tokens et alerte au-dessus du score donné (20 alertes/heure max)"),
    ]),
    ("📡 Veille Telegram", [
        ("addchannel", "<lien>", "Suivre un canal Telegram",
         "le compte de veille lit le canal (lecture seule) et repère les adresses de tokens"),
        ("removechannel", "<lien>", "Ne plus suivre un canal", "le retire de la veille"),
        ("channels", "", "Canaux suivis", "liste des canaux et tokens repérés"),
        ("veille", "", "État de la veille Telegram",
         "connexion, messages lus, alertes (token cité dans 2 canaux en 30 min). Connexion du compte : page /tglogin"),
    ]),
    ("📊 Simulation (sans argent réel)", [
        ("positions", "", "Positions simulées ouvertes (vendre)",
         "prix d'achat, prix actuel, gain ou perte, avec les boutons Vendre 50 %, Vendre 100 % et Actualiser"),
        ("vendre", "<numéro ou token> <%>", "Vendre une position simulée",
         "ex : /vendre 12 50 vend la moitié de la position #12 au prix DexScreener du moment (frais 2 %)"),
        ("stats", "", "Résultats des trades simulés",
         "Bot seul contre Mes choix, par type d'alerte, par wallet, ventes auto contre ventes manuelles"),
        ("journal", "", "Les 10 derniers trades simulés", "avec la raison de chaque sortie (règle ou vente manuelle)"),
    ]),
    ("🧠 IA et règles", [
        ("revue", "", "Revue IA de la semaine",
         "propositions de changement des règles et des wallets, à accepter ou refuser (jamais automatique)"),
        ("rules", "", "Historique des versions des règles", "les changements acceptés de rules.json"),
        ("rollback", "[numéro]", "Revenir à une version des règles", "sans numéro : annule le dernier changement"),
    ]),
]


def texte_aide() -> str:
    lignes = ["📖 <b>Commandes du bot</b>",
              "<i>Tape « / » pour voir la liste. Les &lt;...&gt; sont à remplacer, les [...] sont facultatifs.</i>"]
    for section, commandes in COMMANDES:
        lignes += ["", f"<b>{section}</b>"]
        for nom, arguments, courte, explication in commandes:
            appel = f"/{nom}" + (f" {html.escape(arguments)}" if arguments else "")
            lignes.append(f"{appel} — {html.escape(courte, quote=False)}\n   "
                          f"<i>{html.escape(explication, quote=False)}</i>")
    lignes += ["", "Sous chaque alerte : ✅ Accepter = achat simulé dans « Mes choix » · 🙈 Ignorer · "
                   "🛒 Acheter sur Jupiter (vrai achat, dans ton wallet)"]
    return "\n".join(lignes)


def commandes_menu() -> list[BotCommand]:
    """Liste envoyée à Telegram (set_my_commands) pour le menu qui s'ouvre quand on tape « / »."""
    return [BotCommand(nom, courte[:256]) for _, commandes in COMMANDES for nom, _, courte, _ in commandes]


async def enregistrer_menu(application: Application) -> None:
    """Enregistre le menu « / » auprès de Telegram. Ne plante jamais : sans menu, les commandes marchent quand même."""
    menu = commandes_menu()
    try:
        # Seulement dans TON chat : les autres ne voient pas de menu (et le bot ne leur répond pas)
        await application.bot.set_my_commands(menu, scope=BotCommandScopeChat(config.TELEGRAM_CHAT_ID))
        log.info("Menu des commandes enregistré auprès de Telegram (%d commandes)", len(menu))
    except Exception as erreur:
        log.warning("Menu des commandes pour ton chat impossible (%s) : menu général à la place", erreur)
        try:
            await application.bot.set_my_commands(menu)
        except Exception as erreur_generale:
            log.warning("Menu des commandes non enregistré (%s)", erreur_generale)


def decouper(texte: str, limite: int = 4000) -> list[str]:
    """Coupe un long texte en morceaux (Telegram refuse les messages de plus de 4096 caractères), entre deux sections."""
    morceaux, courant = [], ""
    for bloc in texte.split("\n\n"):
        if courant and len(courant) + len(bloc) + 2 > limite:
            morceaux.append(courant)
            courant = bloc
        else:
            courant = f"{courant}\n\n{bloc}" if courant else bloc
    return morceaux + [courant]


async def _envoyer_aide(update: Update, intro: str = "") -> None:
    for morceau in decouper(intro + texte_aide()):
        await update.message.reply_text(morceau, parse_mode=ParseMode.HTML,
                                        link_preview_options=LinkPreviewOptions(is_disabled=True))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _envoyer_aide(update, "👋 Salut ! Je suis ton bot d'alertes memecoins Solana.\n\n")


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _envoyer_aide(update)


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
    "simulation": ("simulation", ["stats", "journal", "positions", "vendre"]),
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
    application.add_handler(CommandHandler(["aide", "help"], cmd_aide, filters=SEULEMENT_MOI))
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
