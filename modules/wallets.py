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

Commandes Telegram : /addwallet, /removewallet, /wallets, /webhook
"""

import asyncio
import html
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
import telegram_bot
from modules import helius, tokens

log = logging.getLogger(__name__)

# Adresse du SOL "emballé" (wrapped SOL), utilisé par les DEX à la place du SOL natif
MINT_WSOL = "So11111111111111111111111111111111111111112"

# Tokens qui ne sont PAS des memecoins : recevoir l'un d'eux n'est pas un "achat"
MINTS_DE_BASE = {
    MINT_WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KConky1jhcEdGnyR4sGq",  # USDT
}

LAMPORTS_PAR_SOL = 1_000_000_000  # 1 SOL = 1 milliard de lamports

# Une adresse Solana : 32 à 44 caractères en "base58" (pas de 0, O, I ni l)
FORMAT_ADRESSE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Empêche deux mises à jour du webhook en même temps
_verrou_webhook = asyncio.Lock()


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


def _sol_depenses(wallet: str, transaction: dict, wsol_net: float) -> float:
    """
    Combien de SOL le wallet a dépensé dans cette transaction ?
    On essaie plusieurs sources, de la plus précise à la moins précise.
    """
    # 1. Helius a reconnu un swap et donne directement le SOL entré
    swap = (transaction.get("events") or {}).get("swap") or {}
    entree = swap.get("nativeInput") or {}
    if entree.get("account") == wallet and entree.get("amount"):
        return float(entree["amount"]) / LAMPORTS_PAR_SOL

    # 2. Le wallet a envoyé du SOL "emballé" (WSOL) : cas des swaps Jupiter / Raydium
    if wsol_net < 0:
        return -wsol_net

    # 3. Le wallet a envoyé du SOL natif : cas de pump.fun par exemple
    #    (peut inclure de petits frais de plateforme : c'est une estimation)
    lamports = 0
    for transfert in transaction.get("nativeTransfers") or []:
        montant = int(transfert.get("amount") or 0)
        if transfert.get("fromUserAccount") == wallet:
            lamports += montant
        if transfert.get("toUserAccount") == wallet:
            lamports -= montant
    return max(lamports, 0) / LAMPORTS_PAR_SOL


def detecter_achats(transaction: dict, wallets_suivis: set[str]) -> list[Achat]:
    """Renvoie la liste des achats (SOL -> token) faits par nos wallets dans cette transaction."""
    achats = []
    signature = transaction.get("signature") or ""
    horodatage = int(transaction.get("timestamp") or time.time())
    transferts = transaction.get("tokenTransfers") or []

    for wallet in wallets_suivis:
        # Solde net de chaque token pour ce wallet (+ reçu, - envoyé).
        # "tokenAmount" est déjà en unités lisibles (décimales appliquées).
        nets: dict[str, float] = defaultdict(float)
        for t in transferts:
            montant = float(t.get("tokenAmount") or 0)
            if t.get("toUserAccount") == wallet:
                nets[t.get("mint")] += montant
            if t.get("fromUserAccount") == wallet:
                nets[t.get("mint")] -= montant

        recus = {mint: qte for mint, qte in nets.items() if mint and qte > 0 and mint not in MINTS_DE_BASE}
        if not recus:
            continue  # pas de memecoin reçu : ce n'est pas un achat

        sol = _sol_depenses(wallet, transaction, nets.get(MINT_WSOL, 0.0))
        if sol <= 0:
            continue  # token reçu sans payer en SOL (cadeau, achat en USDC...) : on ignore

        mint = max(recus, key=recus.get)  # s'il y en a plusieurs, on prend le principal
        achats.append(Achat(wallet, mint, sol, recus[mint], signature, horodatage))
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
) -> str:
    symbole = html.escape(infos.symbole or "???")
    nom = html.escape(infos.nom or "Nom inconnu")
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

    infos = await tokens.infos_token(achat.mint)
    texte = construire_message(achat, surnom, infos, acheteurs, signal_fort)
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
        prix_usd=infos.prix_usd,
        telegram_message_id=message.message_id,
    )


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
        lignes.append(f"• <b>{html.escape(w['nom'] or '?')}</b>\n  <code>{w['adresse']}</code>")

    # Telegram limite un message à 4096 caractères : on découpe si la liste est longue
    morceau = ""
    for ligne in lignes:
        if len(morceau) + len(ligne) > 3500:
            await update.message.reply_text(morceau, parse_mode=ParseMode.HTML)
            morceau = ""
        morceau += ligne + "\n"
    await update.message.reply_text(morceau, parse_mode=ParseMode.HTML)


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
    application.add_handler(CallbackQueryHandler(bouton_ignorer, pattern="^ignorer$"))
