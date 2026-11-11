"""
modules/wallets.py — Suivi des wallets Solana et alertes d'achat.

Fonctionnement :
  1. Tu ajoutes un wallet avec /addwallet <adresse> <surnom>.
  2. Le bot met à jour le webhook Helius : Helius surveille ces adresses et nous
     envoie chaque transaction (déjà décodée, format "enhanced") sur PUBLIC_URL/helius.
  3. Pour chaque transaction reçue, on cherche un achat : le wallet a dépensé des SOL
     et a reçu un token (qui n'est ni du SOL ni un stablecoin).
  4. On t'envoie une alerte Telegram avec deux boutons : « Acheter » et « Ignorer ».
  5. Si au moins 2 wallets suivis achètent le même token en moins de 10 minutes,
     l'alerte devient un SIGNAL FORT.

Analyse d'historique (le calcul est dans modules/analyse_wallet.py) :
  - à chaque /addwallet, et avec /score <adresse> pour un wallet qu'on ne suit pas ;
  - chaque dimanche pour tous les wallets suivis, avec une alerte si l'un d'eux passe en ÉVITER.

Note de chaque token alerté (modules/analyse.py), filtre /minscore et analyse à la demande /check.

Commandes Telegram : /addwallet, /removewallet, /wallets, /webhook, /score, /debugtx, /check, /minscore
"""

import asyncio
import html
import logging
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
import telegram_bot
from modules import analyse, analyse_wallet, helius, tokens, transactions

log = logging.getLogger(__name__)

# Une adresse Solana : 32 à 44 caractères en "base58" (pas de 0, O, I ni l)
FORMAT_ADRESSE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Empêche deux mises à jour du webhook en même temps
_verrou_webhook = asyncio.Lock()

# Si l'analyse d'un wallet dure plus que ça (secondes), on prévient « Analyse en cours… »
DELAI_MESSAGE_ATTENTE = 2


def adresse_valide(adresse: str) -> bool:
    return bool(FORMAT_ADRESSE.match(adresse))


# ---------------------------------------------------------------------------
# Liens
# ---------------------------------------------------------------------------

def lien_dexscreener(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"


def lien_gmgn(mint: str) -> str:
    return f"https://gmgn.ai/sol/token/{mint}"


def lien_jupiter(mint: str) -> str:
    """Page d'achat Jupiter : échange de SOL contre ce token."""
    return f"https://jup.ag/swap/SOL-{mint}"


def lien_transaction(signature: str) -> str:
    return f"https://solscan.io/tx/{signature}"


# ---------------------------------------------------------------------------
# Synchronisation du webhook Helius
# ---------------------------------------------------------------------------

def url_reception() -> str:
    """Adresse à laquelle Helius envoie les transactions."""
    return f"{config.PUBLIC_URL}/helius"


def _probleme_de_config() -> str | None:
    """Renvoie un message si une variable indispensable au webhook manque, sinon None."""
    if not config.PUBLIC_URL:
        return (
            "⚠️ Webhook Helius NON mis à jour : la variable PUBLIC_URL est vide.\n"
            "Ajoute-la sur Railway (ex : https://mon-bot.up.railway.app)."
        )
    if not config.HELIUS_WEBHOOK_SECRET:
        return (
            "⚠️ Webhook Helius NON mis à jour : la variable HELIUS_WEBHOOK_SECRET est vide.\n"
            "Ajoute-la sur Railway (une longue phrase secrète, voir le README)."
        )
    return None


def _nos_webhooks(webhooks: list[dict]) -> list[dict]:
    """
    Parmi les webhooks du compte Helius, trouve ceux qui sont à nous :
      1. ceux qui pointent déjà vers PUBLIC_URL/helius ;
      2. sinon, celui dont on a mémorisé l'id (cas d'un changement de domaine Railway).
    """
    par_url = [w for w in webhooks if w.get("webhookURL") == url_reception()]
    if par_url:
        return par_url
    id_memorise = db.lire_parametre("helius_webhook_id")
    return [w for w in webhooks if id_memorise and w.get("webhookID") == id_memorise]


def _est_a_jour(webhook: dict, adresses: list[str]) -> bool:
    """True si le webhook Helius a déjà exactement la bonne configuration."""
    return (
        webhook.get("webhookURL") == url_reception()
        and webhook.get("webhookType") == "enhanced"
        and sorted(webhook.get("transactionTypes") or []) == sorted(config.HELIUS_TRANSACTION_TYPES)
        and set(webhook.get("accountAddresses") or []) == set(adresses)
        and webhook.get("authHeader") == config.HELIUS_WEBHOOK_SECRET
    )


async def synchroniser_webhook() -> str:
    """
    Met le webhook Helius en accord avec la liste des wallets suivis.
    S'il existe déjà un webhook pour notre URL, il est modifié (jamais de second webhook).
    Renvoie un petit texte qui explique ce qui a été fait (affiché sur Telegram).
    """
    probleme = _probleme_de_config()
    if probleme:
        return probleme

    async with _verrou_webhook:
        adresses = [w["adresse"] for w in db.lister_wallets()]
        try:
            trouves = _nos_webhooks(await helius.lister_webhooks())
            webhook = trouves[0] if trouves else None

            # S'il y a plusieurs webhooks vers notre URL, on garde le premier et on supprime
            # les autres : sinon chaque achat serait reçu (et alerté) plusieurs fois.
            for doublon in trouves[1:]:
                await helius.supprimer_webhook(doublon["webhookID"])
                log.warning("Webhook Helius en double supprimé : %s", doublon["webhookID"])
            note = f"\n🧹 {len(trouves) - 1} webhook(s) en double supprimé(s)." if len(trouves) > 1 else ""

            # Plus aucun wallet : on supprime le webhook (Helius refuse une liste vide)
            if not adresses:
                db.ecrire_parametre("helius_webhook_id", "")
                if webhook:
                    await helius.supprimer_webhook(webhook["webhookID"])
                    return "🔕 Aucun wallet suivi : webhook Helius supprimé." + note
                return "🔕 Aucun wallet suivi : pas de webhook Helius nécessaire." + note

            # Helius peut désactiver un webhook tout seul (trop d'erreurs) : on le réactive
            if webhook and webhook.get("active") is False:
                await helius.activer_webhook(webhook["webhookID"])
                log.warning("Webhook Helius réactivé : %s", webhook["webhookID"])
                note += "\n▶️ Le webhook était désactivé chez Helius : il a été réactivé."

            # Rien n'a changé : inutile de le modifier (chaque modification coûte 100 crédits)
            if webhook and _est_a_jour(webhook, adresses):
                db.ecrire_parametre("helius_webhook_id", webhook["webhookID"])
                return f"🔔 Webhook Helius déjà à jour ({len(adresses)} wallet(s) surveillé(s))." + note

            resultat = None
            if webhook:
                resultat = await helius.modifier_webhook(webhook["webhookID"], url_reception(), adresses)
                action = "mis à jour"
            if resultat is None:  # aucun webhook (ou supprimé à la main entre-temps) : on le crée
                resultat = await helius.creer_webhook(url_reception(), adresses)
                action = "créé"
            db.ecrire_parametre("helius_webhook_id", resultat.get("webhookID", ""))
        except helius.ErreurHelius as erreur:
            log.error("Mise à jour du webhook Helius impossible : %s", erreur)
            return f"❌ Erreur Helius, webhook non mis à jour : {html.escape(str(erreur))}"

    log.info("Webhook Helius %s : %d wallet(s) -> %s", action, len(adresses), url_reception())
    return f"🔔 Webhook Helius {action} ({len(adresses)} wallet(s) surveillé(s))." + note


async def etat_webhook() -> str:
    """Texte (HTML) décrivant l'état du webhook Helius, pour la commande /webhook."""
    oui_non = lambda ok: "✅" if ok else "❌"  # noqa: E731
    adresses = {w["adresse"] for w in db.lister_wallets()}
    lignes = [
        "🔌 <b>Webhook Helius</b>",
        "",
        f"<b>URL attendue :</b> <code>{html.escape(url_reception())}</code>"
        if config.PUBLIC_URL else "<b>URL attendue :</b> ❌ PUBLIC_URL non définie",
        f"<b>Secret HELIUS_WEBHOOK_SECRET :</b> {oui_non(config.HELIUS_WEBHOOK_SECRET)}"
        + ("" if config.HELIUS_WEBHOOK_SECRET else " manquant"),
        f"<b>Types demandés :</b> {html.escape(', '.join(config.HELIUS_TRANSACTION_TYPES))}",
        f"<b>Wallets suivis :</b> {len(adresses)}",
        f"<b>Dernière réception :</b> {db.lire_parametre('dernier_webhook_recu') or 'jamais'}",
        "",
    ]

    try:
        webhooks = await helius.lister_webhooks()
    except helius.ErreurHelius as erreur:
        lignes.append(f"❌ Impossible de lire les webhooks Helius : {html.escape(str(erreur))}")
        return "\n".join(lignes)

    trouves = _nos_webhooks(webhooks) if config.PUBLIC_URL else []
    if not trouves:
        lignes.append("❌ <b>Aucun webhook Helius</b> ne pointe vers ce bot.")
        if adresses and not _probleme_de_config():
            lignes.append("👉 Tape /webhook sync pour le créer.")
    else:
        w = trouves[0]
        sur_helius = set(w.get("accountAddresses") or [])
        manquants, en_trop = len(adresses - sur_helius), len(sur_helius - adresses)
        actif = w.get("active", True) is not False
        lignes += [
            f"<b>ID :</b> <code>{html.escape(str(w.get('webhookID')))}</code>",
            f"<b>URL chez Helius :</b> {oui_non(w.get('webhookURL') == url_reception())} "
            f"<code>{html.escape(str(w.get('webhookURL')))}</code>",
            f"<b>Type :</b> {oui_non(w.get('webhookType') == 'enhanced')} {html.escape(str(w.get('webhookType')))}",
            f"<b>Transactions :</b> {html.escape(', '.join(w.get('transactionTypes') or []) or 'aucune')}",
            f"<b>Actif :</b> {oui_non(actif)}" + ("" if actif else " (désactivé par Helius)"),
            f"<b>Adresses chez Helius :</b> {len(sur_helius)}"
            + (f" — ⚠️ {manquants} manquante(s)" if manquants else "")
            + (f" — ⚠️ {en_trop} en trop" if en_trop else ""),
            f"<b>Secret identique :</b> {oui_non(w.get('authHeader') == config.HELIUS_WEBHOOK_SECRET)}",
        ]
        if len(trouves) > 1:
            lignes.append(f"⚠️ {len(trouves)} webhooks pointent vers ce bot (doublons).")
        lignes.append("")
        if _est_a_jour(w, sorted(adresses)) and actif and len(trouves) == 1:
            lignes.append("✅ <b>Tout est synchronisé.</b>")
        elif _probleme_de_config():
            lignes.append("⚠️ <b>Pas synchronisé.</b> Corrige d'abord la variable manquante ci-dessus.")
        else:
            lignes.append("⚠️ <b>Pas synchronisé.</b> Tape /webhook sync pour corriger.")

    lignes.append(f"\n<i>Webhooks sur ton compte Helius : {len(webhooks)}</i>")
    return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Détection des achats dans une transaction Helius
# ---------------------------------------------------------------------------

@dataclass
class Achat:
    wallet: str          # adresse du wallet suivi
    mint: str            # adresse (contrat) du token acheté
    montant_sol: float   # SOL dépensés
    tokens_recus: float  # quantité de tokens reçus
    signature: str       # signature de la transaction
    horodatage: int      # heure de la transaction (timestamp Unix)

    @property
    def prix_sol(self) -> float:
        """Prix payé pour 1 token, en SOL."""
        return self.montant_sol / self.tokens_recus


def detecter_achats(transaction: dict, wallets_suivis: set[str]) -> list[Achat]:
    """Renvoie la liste des achats (SOL -> token) faits par nos wallets dans cette transaction."""
    achats = []
    for wallet in wallets_suivis:
        for m in transactions.detecter_mouvements(transaction, wallet):
            if m.sens == "achat":
                achats.append(Achat(wallet, m.mint, m.sol, m.tokens, m.signature, m.horodatage))
    return achats


# ---------------------------------------------------------------------------
# Construction et envoi des alertes
# ---------------------------------------------------------------------------

def formater_prix(prix: float | None, devise: str) -> str:
    """Affiche un prix lisible, même tout petit (ex : 0.00001234 SOL)."""
    if not prix:
        return "?"
    if prix >= 1:
        texte = f"{prix:,.2f}"
    else:
        decimales = min(-math.floor(math.log10(prix)) + 3, 15)  # 4 chiffres significatifs
        texte = f"{prix:.{decimales}f}"
    return f"${texte}" if devise == "$" else f"{texte} {devise}"


def construire_message(
    achat: Achat,
    surnom: str,
    infos: tokens.InfosToken,
    acheteurs: list,
    signal_fort: bool,
    analyse_token: "analyse.AnalyseToken | None" = None,
) -> str:
    donnees = analyse_token.donnees if analyse_token else None
    symbole = html.escape(infos.symbole or (donnees.symbole if donnees else None) or "???")
    nom = html.escape(infos.nom or (donnees.nom if donnees else None) or "Nom inconnu")
    lignes = []

    if signal_fort:
        lignes += [
            "🚨🚨 <b>SIGNAL FORT</b> 🚨🚨",
            f"<b>{len(acheteurs)} wallets suivis</b> ont acheté ce token "
            f"en moins de {config.SIGNAL_FORT_MINUTES} min !",
            "",
        ]

    lignes += [
        f"🟢 <b>{html.escape(surnom)}</b> vient d'acheter",
        f"🪙 <b>{symbole}</b> — {nom}",
        f"💰 Montant : <b>{achat.montant_sol:.3f} SOL</b>",
        f"💵 Prix : {formater_prix(achat.prix_sol, 'SOL')} (≈ {formater_prix(infos.prix_usd, '$')})",
        f"📄 Contrat : <code>{achat.mint}</code>",
    ]

    if signal_fort:
        lignes += ["", "👥 <b>Acheteurs :</b>"]
        for a in acheteurs:
            nom_wallet = html.escape(a["nom"] or a["wallet_adresse"][:6])
            lignes.append(f"  • {nom_wallet} — {a['total_sol']:.3f} SOL")

    # Note du token, 5 chiffres clés et drapeaux rouges (modules/analyse.py)
    lignes.append("")
    lignes.append(analyse.bloc_alerte(analyse_token) if analyse_token
                  else "🧪 <i>Note du token : inconnue (analyse impossible)</i>")

    lignes += [
        "",
        f'🔗 <a href="{lien_dexscreener(achat.mint)}">DexScreener</a> | '
        f'<a href="{lien_gmgn(achat.mint)}">GMGN</a> | '
        f'<a href="{lien_transaction(achat.signature)}">Transaction</a>',
    ]
    return "\n".join(lignes)


def boutons(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Acheter", url=lien_jupiter(mint)),
        InlineKeyboardButton("🙈 Ignorer", callback_data="ignorer"),
    ]])


async def traiter_achat(application: Application, achat: Achat, surnom: str) -> None:
    """Enregistre l'achat, vérifie le signal fort, puis envoie l'alerte."""
    # Enregistrement + comptage des acheteurs, sans "await" entre les deux :
    # ainsi, deux achats reçus en même temps ne peuvent pas se manquer l'un l'autre.
    nouveau = db.enregistrer_achat(
        achat.signature, achat.wallet, achat.mint, achat.montant_sol, achat.tokens_recus, achat.horodatage
    )
    if not nouveau:
        log.info("Achat déjà traité (doublon Helius) : %s", achat.signature)
        return
    acheteurs = db.acheteurs_recents(achat.mint, achat.horodatage, config.SIGNAL_FORT_MINUTES * 60)
    signal_fort = len(acheteurs) >= config.SIGNAL_FORT_MIN_WALLETS

    log.info("Achat détecté : %s -> %s (%.3f SOL)%s", surnom, achat.mint, achat.montant_sol,
             " [SIGNAL FORT]" if signal_fort else "")

    if db.est_en_pause():
        log.info("Bot en pause : pas d'alerte envoyée")
        return

    # Infos du token et analyse de sécurité, en même temps
    infos, resultat = await asyncio.gather(
        tokens.infos_token(achat.mint), analyse.analyser_token(achat.mint), return_exceptions=True
    )
    if isinstance(infos, Exception):
        infos = tokens.InfosToken(None, None, None)
    analyse_token = None
    if isinstance(resultat, Exception):
        log.error("Analyse du token %s impossible : %s", achat.mint, resultat)
    else:
        analyse_token = resultat

    # /minscore : on n'envoie que les alertes au-dessus du score choisi (un token DANGER compte 0).
    # Si l'analyse a échoué, on envoie quand même (mieux vaut une alerte sans note que rien).
    minimum = score_minimum()
    if analyse_token and minimum and analyse_token.score_pour_filtre < minimum:
        log.info("Alerte non envoyée : score %d < minimum %d (%s)",
                 analyse_token.score_pour_filtre, minimum, achat.mint)
        return

    texte = construire_message(achat, surnom, infos, acheteurs, signal_fort, analyse_token)
    try:
        message = await telegram_bot.envoyer_message(application, texte, boutons(achat.mint))
    except Exception:
        log.exception("Impossible d'envoyer l'alerte d'achat sur Telegram")
        return

    db.enregistrer_alerte(
        type_alerte="signal_fort" if signal_fort else "achat_wallet",
        message=texte,
        token_adresse=achat.mint,
        wallet_adresse=achat.wallet,
        signature=achat.signature,
        montant_sol=achat.montant_sol,
        prix_sol=achat.prix_sol,
        prix_usd=infos.prix_usd or (analyse_token.donnees.prix_usd if analyse_token else None),
        telegram_message_id=message.message_id,
        note_token=analyse_token.note if analyse_token else None,
        danger=analyse_token.danger if analyse_token else None,
    )


def score_minimum() -> int:
    """Score minimum choisi avec /minscore (0 = toutes les alertes)."""
    try:
        return int(db.lire_parametre("score_minimum", "0") or 0)
    except ValueError:
        return 0


async def traiter_webhook(application: Application, transactions: list[dict]) -> None:
    """Appelé (en arrière-plan) pour chaque envoi de Helius sur /helius."""
    surnoms = db.surnoms_wallets()
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue
        try:
            for achat in detecter_achats(transaction, set(surnoms)):
                await traiter_achat(application, achat, surnoms[achat.wallet])
        except Exception:
            # Une transaction bizarre ne doit pas empêcher de traiter les suivantes
            log.exception("Erreur en traitant la transaction %s", transaction.get("signature"))


# ---------------------------------------------------------------------------
# Analyse d'historique : envoi du rapport et analyse du dimanche
# ---------------------------------------------------------------------------

def _enregistrer(a: analyse_wallet.Analyse, texte: str) -> None:
    db.enregistrer_analyse(
        a.adresse, a.note, a.verdict, a.scalpeur, len(a.trades),
        a.taux_reussite, a.resultat_total, a.duree_mediane, texte,
    )


async def analyser_et_envoyer(application: Application, adresse: str, surnom: str | None = None) -> None:
    """
    Analyse un wallet et t'envoie le rapport.
    Si ça prend plus de 2 secondes, on envoie d'abord « Analyse en cours… »,
    puis ce message est remplacé par le rapport.
    """
    tache = asyncio.create_task(analyse_wallet.analyser_wallet(adresse))
    attente = None
    termine, _ = await asyncio.wait({tache}, timeout=DELAI_MESSAGE_ATTENTE)
    if not termine:
        nom = html.escape(surnom) if surnom else f"<code>{adresse}</code>"
        attente = await telegram_bot.envoyer_message(
            application, f"🔎 Analyse en cours… {nom}\n<i>Lecture des 30 derniers jours, ça peut prendre une minute.</i>"
        )

    try:
        resultat = await tache
        texte = analyse_wallet.rapport(resultat, surnom)
        _enregistrer(resultat, texte)
    except Exception as erreur:
        log.exception("Analyse impossible pour %s", adresse)
        texte = f"❌ Analyse impossible pour <code>{adresse}</code> : {html.escape(str(erreur))}"

    if attente is not None:
        try:
            await attente.edit_text(texte, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        except Exception:
            log.warning("Impossible de modifier le message « Analyse en cours… », envoi d'un nouveau message")
    await telegram_bot.envoyer_message(application, texte)


async def analyse_hebdomadaire(application: Application) -> None:
    """Recalcule la note de chaque wallet suivi, envoie un résumé et alerte si l'un passe en ÉVITER."""
    suivis = db.lister_wallets()
    log.info("Analyse hebdomadaire de %d wallet(s)", len(suivis))
    lignes = ["📅 <b>Analyse hebdomadaire des wallets suivis</b>", ""]
    passent_en_eviter = []

    for w in suivis:  # un par un, pour ménager les crédits et le débit Helius
        nom = w["nom"] or w["adresse"][:6]
        ancien_verdict = w["verdict"]
        try:
            a = await analyse_wallet.analyser_wallet(w["adresse"])
        except Exception:
            log.exception("Analyse hebdomadaire impossible pour %s", w["adresse"])
            lignes.append(f"❌ <b>{html.escape(nom)}</b> : analyse impossible (voir les logs)")
            continue
        texte = analyse_wallet.rapport(a, nom)
        _enregistrer(a, texte)

        changement = f" <i>(avant : {ancien_verdict})</i>" if ancien_verdict and ancien_verdict != a.verdict else ""
        note = f"{a.note}/100 " if a.note is not None else ""
        lignes.append(
            f"{analyse_wallet.EMOJI_VERDICT[a.verdict]} <b>{html.escape(nom)}</b> — {note}{a.verdict}"
            + (" ⚡SCALPEUR" if a.scalpeur else "") + changement
        )
        if a.verdict == analyse_wallet.EVITER and ancien_verdict != analyse_wallet.EVITER:
            passent_en_eviter.append((nom, texte))

    lignes.append("\n<i>Détails d'un wallet : /score &lt;adresse&gt;</i>")
    await telegram_bot.envoyer_message(application, "\n".join(lignes))

    # Une alerte séparée (avec le rapport complet) pour chaque wallet qui passe en ÉVITER
    for nom, texte in passent_en_eviter:
        await telegram_bot.envoyer_message(
            application,
            f"🔴🔴 <b>{html.escape(nom)} passe en ÉVITER</b>\n"
            f"Pense à le retirer avec /removewallet si tu ne veux plus ses alertes.\n\n{texte}",
        )


async def boucle_hebdomadaire(application: Application) -> None:
    """
    Tourne en fond : chaque dimanche, à partir de ANALYSE_HEBDO_HEURE (heure de Paris par défaut),
    lance l'analyse hebdomadaire une seule fois. La date de la dernière analyse est gardée en base :
    un redémarrage du bot ne la relance donc pas.
    """
    fuseau = ZoneInfo(config.FUSEAU_HORAIRE)
    log.info("Analyse hebdomadaire programmée le dimanche à partir de %d h (%s)",
             config.ANALYSE_HEBDO_HEURE, config.FUSEAU_HORAIRE)
    while True:
        try:
            maintenant = datetime.now(fuseau)
            aujourd_hui = maintenant.date().isoformat()
            if (
                maintenant.weekday() == 6  # 6 = dimanche
                and maintenant.hour >= config.ANALYSE_HEBDO_HEURE
                and db.lire_parametre("derniere_analyse_hebdo") != aujourd_hui
            ):
                db.ecrire_parametre("derniere_analyse_hebdo", aujourd_hui)
                if db.lister_wallets():
                    await analyse_hebdomadaire(application)
        except asyncio.CancelledError:
            raise  # arrêt normal du bot
        except Exception:
            log.exception("Erreur pendant l'analyse hebdomadaire")
        await asyncio.sleep(600)  # on revérifie toutes les 10 minutes


# ---------------------------------------------------------------------------
# Commandes Telegram
# ---------------------------------------------------------------------------

async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text("Utilisation : /addwallet <adresse> <surnom>")
        return
    adresse, surnom = context.args[0], " ".join(context.args[1:])
    if not adresse_valide(adresse):
        await update.message.reply_text("❌ Cette adresse Solana n'est pas valide.")
        return

    nouveau = db.ajouter_wallet(adresse, surnom)
    debut = "✅ Wallet ajouté" if nouveau else "✏️ Wallet déjà suivi, surnom mis à jour"
    resultat = await synchroniser_webhook()
    await update.message.reply_text(
        f"{debut} : <b>{html.escape(surnom)}</b>\n<code>{adresse}</code>\n\n{resultat}",
        parse_mode=ParseMode.HTML,
    )
    # L'analyse tourne en arrière-plan : le bot reste disponible pour les autres commandes
    context.application.create_task(analyser_et_envoyer(context.application, adresse, surnom), update=update)


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/score <adresse> : analyse un wallet SANS l'ajouter à la liste des wallets suivis."""
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /score <adresse>")
        return
    adresse = context.args[0]
    if not adresse_valide(adresse):
        await update.message.reply_text("❌ Cette adresse Solana n'est pas valide.")
        return
    suivi = db.lire_wallet(adresse)
    surnom = suivi["nom"] if suivi else None
    context.application.create_task(analyser_et_envoyer(context.application, adresse, surnom), update=update)


async def cmd_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /removewallet <adresse>")
        return
    adresse = context.args[0]
    if not db.supprimer_wallet(adresse):
        await update.message.reply_text("❓ Ce wallet n'est pas dans la liste. Tape /wallets pour la voir.")
        return
    resultat = await synchroniser_webhook()
    await update.message.reply_text(
        f"🗑️ Wallet retiré : <code>{adresse}</code>\n\n{resultat}", parse_mode=ParseMode.HTML
    )


async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wallets = db.lister_wallets()
    if not wallets:
        await update.message.reply_text("Aucun wallet suivi. Ajoute-en un avec /addwallet <adresse> <surnom>")
        return

    lignes = [f"👛 <b>{len(wallets)} wallet(s) suivi(s) :</b>", ""]
    for w in wallets:
        if w["verdict"]:
            sur_100 = f"{w['note']}/100 " if w["note"] is not None else ""
            note = (f" — {analyse_wallet.EMOJI_VERDICT.get(w['verdict'], '')} {sur_100}{w['verdict']}"
                    + (" ⚡" if w["scalpeur"] else ""))
        else:
            note = " — pas encore analysé"
        lignes.append(f"• <b>{html.escape(w['nom'] or '?')}</b>{note}\n  <code>{w['adresse']}</code>")
    lignes.append("\n<i>⚡ = SCALPEUR. Détails : /score &lt;adresse&gt;</i>")

    # Telegram limite un message à 4096 caractères : on découpe si la liste est longue
    morceau = ""
    for ligne in lignes:
        if len(morceau) + len(ligne) > 3500:
            await update.message.reply_text(morceau, parse_mode=ParseMode.HTML)
            morceau = ""
        morceau += ligne + "\n"
    await update.message.reply_text(morceau, parse_mode=ParseMode.HTML)


async def cmd_debugtx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debugtx <adresse> : montre comment les 5 dernières transactions du wallet ont été interprétées."""
    if len(context.args) != 1 or not adresse_valide(context.args[0]):
        await update.message.reply_text("Utilisation : /debugtx <adresse>")
        return
    adresse = context.args[0]
    try:
        historique, _, source = await helius.historique_wallet(adresse, 0, 5)
    except helius.ErreurHelius as erreur:
        await update.message.reply_text(f"❌ Helius : {erreur}")
        return
    await update.message.reply_text(
        texte_debugtx(adresse, historique, source),
        parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def texte_debugtx(adresse: str, historique: list[dict], source: str) -> str:
    """Construit le message de /debugtx."""
    lignes = [f"🔍 <b>{len(historique)} dernière(s) transaction(s)</b>", f"<code>{adresse}</code>",
              f"<i>Source : {html.escape(source, quote=False)}</i>", ""]
    if not historique:
        lignes.append("Aucune transaction trouvée.")
    icones = {"achat": "🟢 ACHAT", "vente": "🔴 VENTE", None: "⚪ IGNORÉE"}
    for tx in historique:
        i = transactions.interpreter(tx, adresse)
        heure = datetime.fromtimestamp(i.horodatage, ZoneInfo(config.FUSEAU_HORAIRE)).strftime("%d/%m %H:%M:%S")
        lignes.append(f'<b>{icones[i.sens]}</b> — {heure} — <a href="{lien_transaction(i.signature)}">{i.signature[:8]}…</a>')
        lignes.append(f"  Raison : {html.escape(i.raison, quote=False)}")
        lignes.append(f"  SOL : {i.sol:+.6f}")
        for mint, variation in list(i.tokens.items())[:4]:
            lignes.append(f"  Token <code>{mint[:6]}…{mint[-4:]}</code> : {variation:+,.4f}")
        lignes.append("")
    return "\n".join(lignes)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/check <adresse> : analyse n'importe quel token à la demande."""
    if len(context.args) != 1 or not adresse_valide(context.args[0]):
        await update.message.reply_text("Utilisation : /check <adresse du token>")
        return
    context.application.create_task(verifier_et_envoyer(context.application, context.args[0]), update=update)


async def verifier_et_envoyer(application: Application, mint: str) -> None:
    """Analyse un token ; « Analyse en cours… » d'abord si ça prend plus de 2 secondes."""
    tache = asyncio.create_task(analyse.analyser_token(mint))
    attente = None
    termine, _ = await asyncio.wait({tache}, timeout=DELAI_MESSAGE_ATTENTE)
    if not termine:
        attente = await telegram_bot.envoyer_message(application, f"🔎 Analyse du token en cours… <code>{mint}</code>")
    try:
        texte = analyse.rapport(await tache)
    except Exception as erreur:
        log.exception("Analyse du token %s impossible", mint)
        texte = f"❌ Analyse impossible pour <code>{mint}</code> : {html.escape(str(erreur))}"
    if attente is not None:
        try:
            await attente.edit_text(texte, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        except Exception:
            log.warning("Impossible de modifier le message « Analyse en cours… »")
    await telegram_bot.envoyer_message(application, texte)


async def cmd_minscore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/minscore <nombre> : ne recevoir que les alertes dont la note du token atteint ce score."""
    if not context.args:
        actuel = score_minimum()
        await update.message.reply_text(
            f"Score minimum actuel : {actuel}/100" + (" (toutes les alertes)" if not actuel else "")
            + "\nPour le changer : /minscore <0 à 100>  (0 = toutes les alertes)"
        )
        return
    try:
        valeur = int(context.args[0])
        if not 0 <= valeur <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Utilisation : /minscore <nombre entre 0 et 100>")
        return
    db.ecrire_parametre("score_minimum", str(valeur))
    if valeur:
        texte = (f"✅ Tu ne recevras que les alertes dont la note du token est d'au moins {valeur}/100.\n"
                 "Les tokens DANGER comptent pour 0. Si l'analyse échoue, l'alerte est envoyée quand même.")
    else:
        texte = "✅ Filtre désactivé : tu reçois toutes les alertes."
    await update.message.reply_text(texte)


async def cmd_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/webhook : affiche l'état du webhook. /webhook sync : le resynchronise d'abord."""
    texte = ""
    if context.args and context.args[0].lower() == "sync":
        texte = await synchroniser_webhook() + "\n\n"
    texte += await etat_webhook()
    await update.message.reply_text(
        texte, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


async def bouton_ignorer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clic sur « Ignorer » : on marque l'alerte comme ignorée et on retire les boutons."""
    query = update.callback_query
    # Sécurité : on n'accepte que les clics venant de TON chat
    if query.message is None or query.message.chat.id != config.TELEGRAM_CHAT_ID:
        await query.answer()
        return

    db.marquer_alerte_ignoree(query.message.message_id)
    await query.answer("Alerte ignorée")
    await query.edit_message_text(
        query.message.text_html + "\n\n<i>🙈 Ignorée</i>",
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def enregistrer_commandes(application: Application) -> None:
    """Branche les commandes de ce module sur le bot (appelé par telegram_bot.py)."""
    moi = telegram_bot.SEULEMENT_MOI
    application.add_handler(CommandHandler("addwallet", cmd_addwallet, filters=moi))
    application.add_handler(CommandHandler("removewallet", cmd_removewallet, filters=moi))
    application.add_handler(CommandHandler("wallets", cmd_wallets, filters=moi))
    application.add_handler(CommandHandler("webhook", cmd_webhook, filters=moi))
    application.add_handler(CommandHandler("score", cmd_score, filters=moi))
    application.add_handler(CommandHandler("debugtx", cmd_debugtx, filters=moi))
    application.add_handler(CommandHandler("check", cmd_check, filters=moi))
    application.add_handler(CommandHandler("minscore", cmd_minscore, filters=moi))
    application.add_handler(CallbackQueryHandler(bouton_ignorer, pattern="^ignorer$"))
