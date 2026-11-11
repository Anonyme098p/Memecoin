"""
modules/veille.py — Veille des canaux Telegram avec TON compte Telegram (Telethon), en lecture seule.

  1. Connexion du compte depuis le téléphone, via la page web protégée PUBLIC_URL/tglogin :
     numéro, code reçu dans Telegram, mot de passe 2FA si besoin. La session est ensuite
     enregistrée CHIFFRÉE dans la base de données (clé dérivée de TGLOGIN_PASSWORD).
  2. LECTURE SEULE : le client Telethon refuse, avant tout envoi, toute requête d'écriture
     (envoyer / transférer / modifier un message, rejoindre ou quitter un canal, marquer comme lu...).
     Seules les requêtes de connexion (envoi du code, déconnexion) sont autorisées.
  3. /addchannel <lien> : suit un canal. Canal dont le compte est membre -> lu en direct ;
     canal public non rejoint -> relu toutes les minutes ; canal privé non rejoint -> refusé
     (rejoins-le toi-même depuis ton téléphone : le bot ne rejoint jamais rien).
  4. Dans chaque message : détection des adresses de tokens Solana (texte et liens).
  5. Si un même token est cité dans 2 canaux ou plus en moins de 30 minutes : alerte avec
     l'analyse du token (modules/analyse.py) et la liste des canaux.

Commandes : /addchannel, /removechannel, /channels, /veille
"""

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession
from telethon.tl import functions, types

import config
import db
import telegram_bot
from modules import analyse, helius, transactions, wallets
from modules.supervision import ModuleDesactive, marquer

NOM_MODULE = "veille Telegram"

log = logging.getLogger(__name__)

# État affiché par /veille
etat = {
    "client": None, "connecte": False, "compte": None, "depuis": None, "erreur": None,
    "messages_lus": 0, "adresses_vues": 0, "alertes": 0,
}
_application: Application | None = None
_canaux: dict[int, dict] = {}            # canaux suivis, en mémoire : id -> ligne de la table
_derniere_alerte: dict[str, tuple[int, float]] = {}   # mint -> (nombre de canaux alerté, heure)


def configuree() -> str | None:
    """Renvoie None si la veille peut fonctionner, sinon ce qui manque."""
    if not config.TG_API_ID or not config.TG_API_HASH:
        return "les variables TG_API_ID et TG_API_HASH ne sont pas définies"
    if len(config.TGLOGIN_PASSWORD) < 12:
        return "la variable TGLOGIN_PASSWORD est absente ou trop courte (12 caractères minimum)"
    return None


# ---------------------------------------------------------------------------
# Lecture seule
# ---------------------------------------------------------------------------

# Toute requête Telegram dont le nom commence ainsi est une écriture : elle est bloquée.
_ECRITURES = re.compile(
    r"^(Send|Forward|Edit|Delete|Join|Leave|ImportChatInvite|Report|Block|Unblock|Save|Read|Update|"
    r"SetTyping|Upload|Install|Toggle|Create|Invite|Kick|EditBanned|Pin|Unpin|Clear|Start|Vote|Accept|"
    r"Request|Hide|Mark|Set|Add|Remove|Reorder|Transfer|Migrate|Convert)"
)


# Familles de requêtes qui peuvent modifier le compte. Les autres (auth : connexion ; help, updates
# et requêtes de base : fonctionnement interne de Telethon) ne font que lire ou établir la connexion.
_FAMILLES_SURVEILLEES = {"messages", "channels", "account", "contacts", "photos", "upload", "stories", "phone",
                         "payments", "bots", "folders", "chatlists", "stickers", "premium", "smsjobs", "fragment"}


def verifier_lecture_seule(requete) -> None:
    """Lève PermissionError si la requête modifierait quoi que ce soit sur le compte."""
    nom, famille = type(requete).__name__, type(requete).__module__.rsplit(".", 1)[-1]
    if famille in _FAMILLES_SURVEILLEES and _ECRITURES.match(nom):
        raise PermissionError(f"Veille en lecture seule : requête « {nom} » refusée")


class ClientLectureSeule(TelegramClient):
    """Client Telethon qui refuse toute écriture (vérification avant chaque envoi à Telegram)."""

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        for r in request if isinstance(request, (list, tuple)) else [request]:
            verifier_lecture_seule(r)
        return await super().__call__(request, ordered, flood_sleep_threshold)


def nouveau_client(session: str = "") -> ClientLectureSeule:
    return ClientLectureSeule(StringSession(session), config.TG_API_ID, config.TG_API_HASH,
                              device_model="Bot veille memecoins", connection_retries=None, retry_delay=5)


# ---------------------------------------------------------------------------
# Session chiffrée dans la base
# ---------------------------------------------------------------------------

def _chiffreur() -> Fernet:
    cle = hashlib.pbkdf2_hmac("sha256", config.TGLOGIN_PASSWORD.encode(),
                              b"veille-memecoin:" + config.TG_API_HASH.encode(), 200_000)
    return Fernet(base64.urlsafe_b64encode(cle))


def sauver_session(session: str) -> None:
    db.ecrire_parametre("tg_session", _chiffreur().encrypt(session.encode()).decode())


def lire_session() -> str | None:
    brut = db.lire_parametre("tg_session")
    if not brut:
        return None
    try:
        return _chiffreur().decrypt(brut.encode()).decode()
    except InvalidToken:
        etat["erreur"] = "session illisible (TGLOGIN_PASSWORD ou TG_API_HASH a changé ?) : reconnecte-toi sur /tglogin"
        return None


# ---------------------------------------------------------------------------
# Détection des adresses de tokens
# ---------------------------------------------------------------------------

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_CANDIDAT = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_IGNOREES = transactions.MINTS_DE_BASE | {"11111111111111111111111111111111", helius.PROGRAMME_PUMP_FUN}
_cache_mints: dict[str, bool] = {}


def _octets_base58(texte: str) -> int:
    """Nombre d'octets d'une chaîne base58 (une adresse Solana en fait exactement 32)."""
    nombre = 0
    for c in texte:
        nombre = nombre * 58 + _ALPHABET.index(c)
    zeros = len(texte) - len(texte.lstrip("1"))
    return zeros + (nombre.bit_length() + 7) // 8


def adresses_dans(texte: str) -> list[str]:
    """Adresses Solana possibles dans un texte (sans doublon, dans l'ordre)."""
    vues: list[str] = []
    for candidat in _CANDIDAT.findall(texte or ""):
        if candidat not in vues and candidat not in _IGNOREES and _octets_base58(candidat) == 32:
            vues.append(candidat)
    return vues


async def est_un_token(adresse: str) -> bool:
    """Garde seulement les tokens (pas les wallets ni les pools). Résultat mémorisé."""
    if adresse in _cache_mints:
        return _cache_mints[adresse]
    try:
        resultat = await helius.est_un_mint(adresse)
    except helius.ErreurHelius:
        return adresse.endswith("pump")  # Helius indisponible : on garde au moins les tokens pump.fun
    if len(_cache_mints) > 20_000:
        _cache_mints.clear()
    _cache_mints[adresse] = resultat
    return resultat


def texte_complet(message) -> str:
    """Texte du message + adresses cachées derrière des liens (ex : « acheter ici »)."""
    morceaux = [message.message or ""]
    for entite in message.entities or []:
        if isinstance(entite, types.MessageEntityTextUrl) and entite.url:
            morceaux.append(entite.url)
    return "\n".join(morceaux)


def lien_message(canal: dict, message_id: int) -> str:
    if canal.get("nom_utilisateur"):
        return f"https://t.me/{canal['nom_utilisateur']}/{message_id}"
    return f"https://t.me/c/{str(canal['id']).removeprefix('-100')}/{message_id}"


# ---------------------------------------------------------------------------
# Traitement des messages et alertes
# ---------------------------------------------------------------------------

async def traiter_message(canal: dict, message) -> None:
    etat["messages_lus"] += 1
    db.maj_dernier_message(canal["id"], message.id)
    horodatage = int(message.date.timestamp()) if message.date else int(time.time())
    fenetre = config.VEILLE_FENETRE_MIN * 60

    for adresse in adresses_dans(texte_complet(message)):
        if not await est_un_token(adresse):
            continue
        etat["adresses_vues"] += 1
        db.enregistrer_mention(adresse, canal["id"], canal["titre"], message.id,
                               lien_message(canal, message.id), horodatage)
        canaux = db.canaux_ayant_cite(adresse, horodatage - fenetre, horodatage + fenetre)
        # Alerte au 2e canal, puis à chaque nouveau canal (3e, 4e...) dans la même fenêtre
        deja, quand = _derniere_alerte.get(adresse, (0, 0.0))
        if time.time() - quand > fenetre:
            deja = 0
        if len(canaux) >= config.VEILLE_MIN_CANAUX and len(canaux) > deja:
            _derniere_alerte[adresse] = (len(canaux), time.time())
            asyncio.create_task(alerter(adresse, canaux))


async def alerter(mint: str, canaux: list) -> None:
    if _application is None or db.est_en_pause():
        return
    try:
        a = await analyse.analyser_token(mint)
    except Exception as erreur:
        log.error("Analyse du token %s impossible : %s", mint, erreur)
        a = None
    minimum = wallets.score_minimum()
    if a and minimum and a.score_pour_filtre < minimum:
        log.info("Alerte veille non envoyée : score %d < %d (%s)", a.score_pour_filtre, minimum, mint)
        return

    fuseau = ZoneInfo(config.FUSEAU_HORAIRE)
    ecart = (canaux[-1]["horodatage"] - canaux[0]["horodatage"]) / 60
    d = a.donnees if a else None
    lignes = [
        f"📡 <b>VEILLE TELEGRAM</b> — token cité dans <b>{len(canaux)} canaux</b> en {ecart:.0f} min",
        f"🪙 <b>{html.escape((d.symbole if d else None) or '???')}</b>"
        + (f" — {html.escape(d.nom)}" if d and d.nom else ""),
        f"📄 Contrat : <code>{mint}</code>",
        "",
        "📢 <b>Canaux :</b>",
    ]
    for c in canaux:
        heure = datetime.fromtimestamp(c["horodatage"], fuseau).strftime("%H:%M")
        lignes.append(f'  • <a href="{c["lien_message"]}">{html.escape(c["canal_titre"] or "?")}</a> — {heure}')
    lignes += ["", analyse.bloc_alerte(a) if a else "🧪 <i>Note du token : inconnue (analyse impossible)</i>",
               "", f'🔗 <a href="{wallets.lien_dexscreener(mint)}">DexScreener</a> | '
                   f'<a href="{wallets.lien_gmgn(mint)}">GMGN</a>']
    texte = "\n".join(lignes)
    try:
        message = await telegram_bot.envoyer_message(_application, texte, wallets.boutons(mint))
    except Exception:
        log.exception("Impossible d'envoyer l'alerte de veille")
        return
    etat["alertes"] += 1
    db.enregistrer_alerte("veille", texte, mint, telegram_message_id=message.message_id,
                          prix_usd=d.prix_usd if d else None, note_token=a.note if a else None,
                          danger=a.danger if a else None)


# ---------------------------------------------------------------------------
# Connexion du compte, écoute en direct et relecture des canaux publics
# ---------------------------------------------------------------------------

def _recharger_canaux() -> None:
    _canaux.clear()
    for c in db.lister_canaux():
        _canaux[c["id"]] = dict(c)


async def _sur_nouveau_message(event) -> None:
    canal = _canaux.get(event.chat_id)
    if canal:  # on ignore tout le reste (conversations privées, autres groupes)
        try:
            await traiter_message(canal, event.message)
        except Exception:
            log.exception("Erreur en lisant un message du canal %s", canal.get("titre"))


async def activer(client: ClientLectureSeule) -> None:
    """Branche un client connecté et autorisé : écoute en direct + relecture des canaux publics."""
    ancien = etat.get("client")
    if ancien is not None and ancien is not client:
        await ancien.disconnect()
    moi = await client.get_me()
    nom = f"@{moi.username}" if moi.username else (moi.first_name or "compte")
    etat.update(client=client, connecte=True, compte=nom, depuis=time.time(), erreur=None)
    marquer(NOM_MODULE, "actif", f"connectée ({nom})")
    _recharger_canaux()
    client.add_event_handler(_sur_nouveau_message, events.NewMessage())
    log.info("Veille Telegram active (compte %s, %d canaux)", nom, len(_canaux))


async def demarrer(application: Application) -> None:
    """Au démarrage du bot : reprend la session enregistrée, puis surveille la connexion."""
    global _application
    _application = application
    manque = configuree()
    if manque:
        etat["erreur"] = f"désactivée : {manque}"
        raise ModuleDesactive(manque)  # le reste du bot continue sans la veille
    session = lire_session()
    if session:
        client = nouveau_client(session)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await activer(client)
            else:
                etat["erreur"] = "session expirée ou révoquée : reconnecte-toi sur /tglogin"
        except Exception as erreur:
            etat["erreur"] = f"connexion impossible ({type(erreur).__name__})"
    elif not etat["erreur"]:
        etat["erreur"] = "compte pas encore connecté : ouvre PUBLIC_URL/tglogin"
    if not etat["connecte"]:
        marquer(NOM_MODULE, "en attente", etat["erreur"])
    await _boucle(application)


async def _boucle(application: Application) -> None:
    """Toutes les VEILLE_RELECTURE_S secondes : reconnexion si besoin + relecture des canaux publics."""
    while True:
        await asyncio.sleep(config.VEILLE_RELECTURE_S)
        client = etat.get("client")
        if client is None:
            continue
        try:
            if not client.is_connected():
                etat["connecte"] = False
                marquer(NOM_MODULE, "en attente", "connexion perdue, reconnexion en cours")
                await client.connect()
            etat["connecte"] = client.is_connected()
            if etat["connecte"]:
                marquer(NOM_MODULE, "actif", f"connectée ({etat['compte']})")
            for canal in list(_canaux.values()):
                if canal["membre"] or not canal["nom_utilisateur"]:
                    continue  # canal rejoint : messages reçus en direct
                messages = await client.get_messages(canal["nom_utilisateur"], min_id=canal["dernier_message_id"], limit=50)
                for message in sorted(messages, key=lambda m: m.id):
                    canal["dernier_message_id"] = max(canal["dernier_message_id"], message.id)
                    await traiter_message(canal, message)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as attente:
            log.warning("Telegram demande d'attendre %d s", attente.seconds)
            await asyncio.sleep(attente.seconds)
        except Exception as erreur:
            etat.update(connecte=False, erreur=f"{type(erreur).__name__}")
            log.warning("Veille : erreur (%s), nouvel essai dans %d s", type(erreur).__name__, config.VEILLE_RELECTURE_S)


async def arreter() -> None:
    client = etat.get("client")
    if client is not None:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Canaux : /addchannel, /removechannel, /channels
# ---------------------------------------------------------------------------

_LIEN_INVITATION = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)")
_LIEN_PRIVE = re.compile(r"(?:t\.me|telegram\.me)/c/(\d+)")
_LIEN_PUBLIC = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z]\w{3,31})")


def analyser_lien(lien: str) -> tuple[str, str | int]:
    """("invitation", hash) | ("prive", id) | ("public", nom). Lève ValueError si le lien est incompréhensible."""
    lien = lien.strip()
    if m := _LIEN_INVITATION.search(lien):
        return "invitation", m.group(1)
    if m := _LIEN_PRIVE.search(lien):
        return "prive", int("-100" + m.group(1))
    if m := _LIEN_PUBLIC.search(lien):
        return "public", m.group(1)
    if re.fullmatch(r"@?[A-Za-z]\w{3,31}", lien):
        return "public", lien.lstrip("@")
    if re.fullmatch(r"-100\d+", lien):
        return "prive", int(lien)
    raise ValueError("lien non reconnu : donne un lien t.me/..., un @nom ou un lien d'invitation")


async def resoudre_canal(client: ClientLectureSeule, lien: str) -> dict:
    """Trouve le canal SANS le rejoindre. Lève ValueError avec une explication si impossible."""
    sorte, valeur = analyser_lien(lien)
    if sorte == "invitation":
        invitation = await client(functions.messages.CheckChatInviteRequest(valeur))
        if not isinstance(invitation, types.ChatInviteAlready):
            raise ValueError("ce canal privé n'est pas encore rejoint par ton compte. Rejoins-le toi-même "
                             "depuis ton téléphone avec ce lien, puis refais /addchannel (le bot ne rejoint jamais "
                             "de canal tout seul : il est en lecture seule)")
        entite = invitation.chat
    else:
        try:
            entite = await client.get_entity(valeur if sorte == "public" else types.PeerChannel(int(str(valeur)[4:])))
        except (ValueError, errors.UsernameNotOccupiedError, errors.ChannelPrivateError):
            raise ValueError("canal introuvable, ou privé et pas encore rejoint par ton compte") from None
    if isinstance(entite, types.User):
        raise ValueError("c'est un compte personnel, pas un canal ni un groupe")
    membre = not getattr(entite, "left", False)
    nom = getattr(entite, "username", None)
    if not membre and not nom:
        raise ValueError("canal privé non rejoint : rejoins-le d'abord depuis ton téléphone")
    dernier = await client.get_messages(entite, limit=1)
    return {"id": utils.get_peer_id(entite), "titre": getattr(entite, "title", None) or nom or "?",
            "nom_utilisateur": nom, "membre": membre, "dernier_message_id": dernier[0].id if dernier else 0}


def _client_ou_erreur() -> ClientLectureSeule | None:
    return etat["client"] if etat.get("client") is not None and etat["connecte"] else None


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /addchannel <lien t.me/... ou @nom>")
        return
    client = _client_ou_erreur()
    if client is None:
        await update.message.reply_text(f"❌ Veille non connectée. {texte_etat_court()}")
        return
    try:
        canal = await resoudre_canal(client, context.args[0])
    except ValueError as erreur:
        await update.message.reply_text(f"❌ {erreur}")
        return
    except errors.FloodWaitError as attente:
        await update.message.reply_text(f"⏳ Telegram demande d'attendre {attente.seconds} s, réessaie plus tard.")
        return
    nouveau = db.ajouter_canal(canal["id"], canal["titre"], canal["nom_utilisateur"], context.args[0],
                               canal["membre"], canal["dernier_message_id"])
    _recharger_canaux()
    mode = ("📶 lu en direct (ton compte en est membre)" if canal["membre"]
            else f"🔁 relu toutes les {config.VEILLE_RELECTURE_S} s (canal public non rejoint)")
    await update.message.reply_text(
        f"{'✅ Canal ajouté' if nouveau else '✏️ Canal déjà suivi, mis à jour'} : <b>{html.escape(canal['titre'])}</b>\n{mode}",
        parse_mode=ParseMode.HTML)


async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /removechannel <lien, @nom ou titre>")
        return
    recherche = {context.args[0].strip().lstrip("@").lower()}
    try:
        recherche.add(str(analyser_lien(context.args[0])[1]).lower())  # ex : t.me/nom -> nom
    except ValueError:
        pass
    for c in db.lister_canaux():
        valeurs = {str(c["id"]), (c["nom_utilisateur"] or "").lower(), (c["lien"] or "").lower(), (c["titre"] or "").lower()}
        if recherche & valeurs:
            db.supprimer_canal(c["id"])
            _recharger_canaux()
            await update.message.reply_text(f"🗑️ Canal retiré : <b>{html.escape(c['titre'] or '?')}</b>", parse_mode=ParseMode.HTML)
            return
    await update.message.reply_text("❓ Canal introuvable dans la liste. Tape /channels pour la voir.")


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    canaux = db.lister_canaux()
    if not canaux:
        await update.message.reply_text("Aucun canal suivi. Ajoute-en un avec /addchannel <lien>")
        return
    lignes = [f"📢 <b>{len(canaux)} canal(aux) suivi(s) :</b>", ""]
    for c in canaux:
        mode = "📶 direct" if c["membre"] else "🔁 relu chaque minute"
        lignes.append(f"• <b>{html.escape(c['titre'] or '?')}</b> — {mode} — "
                      f"{db.compter_mentions_canal(c['id'])} token(s) repéré(s)")
    await update.message.reply_text("\n".join(lignes), parse_mode=ParseMode.HTML)


def texte_etat_court() -> str:
    if etat["connecte"]:
        return f"🟢 Veille connectée ({etat['compte']})"
    return f"🔴 Veille non connectée : {etat['erreur'] or 'démarrage en cours'}"


async def cmd_veille(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lignes = ["📡 <b>Veille Telegram</b>", "", html.escape(texte_etat_court(), quote=False)]
    if etat["connecte"] and etat["depuis"]:
        lignes.append(f"Connectée depuis {analyse.formater_age((time.time() - etat['depuis']) / 60)}")
    canaux = db.lister_canaux()
    lignes += [
        f"Canaux suivis : {len(canaux)} ({sum(1 for c in canaux if c['membre'])} en direct, "
        f"{sum(1 for c in canaux if not c['membre'])} relus chaque minute)",
        f"Messages lus depuis le démarrage : <b>{etat['messages_lus']}</b>",
        f"Adresses de tokens repérées : {etat['adresses_vues']} · alertes envoyées : {etat['alertes']}",
        f"Règle : un token cité dans {config.VEILLE_MIN_CANAUX} canaux ou plus en {config.VEILLE_FENETRE_MIN} min",
    ]
    if not etat["connecte"] and not configuree():
        lignes.append(f"\n👉 Connexion du compte : {html.escape(config.PUBLIC_URL or 'PUBLIC_URL')}/tglogin")
    await update.message.reply_text("\n".join(lignes), parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))


def enregistrer_commandes(application: Application) -> None:
    moi = telegram_bot.SEULEMENT_MOI
    application.add_handler(CommandHandler("addchannel", cmd_addchannel, filters=moi))
    application.add_handler(CommandHandler("removechannel", cmd_removechannel, filters=moi))
    application.add_handler(CommandHandler("channels", cmd_channels, filters=moi))
    application.add_handler(CommandHandler("veille", cmd_veille, filters=moi))


# ---------------------------------------------------------------------------
# Page web /tglogin (connexion du compte depuis le téléphone)
# ---------------------------------------------------------------------------

router = APIRouter()
DUREE_ACCES_S = 30 * 60            # un accès à la page reste valable 30 minutes
_acces: dict[str, float] = {}       # jeton du cookie -> heure d'expiration
_essais: dict[str, list[float]] = {}  # adresse IP -> heures des mauvais mots de passe
_connexion: dict = {}               # connexion Telegram en cours (client, numéro, code envoyé)


def _page(titre: str, contenu: str, statut: int = 200) -> HTMLResponse:
    corps = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(titre)}</title><style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1c1e21;margin:0;padding:16px}}
main{{max-width:420px;margin:24px auto;background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 4px #0002}}
h1{{font-size:1.3em;margin-top:0}} input{{width:100%;box-sizing:border-box;font-size:1.1em;padding:12px;margin:8px 0;
border:1px solid #ccd;border-radius:8px}} button{{width:100%;font-size:1.1em;padding:12px;border:0;border-radius:8px;
background:#2481cc;color:#fff;margin-top:8px}} .erreur{{color:#c0392b}} .ok{{color:#1e8e3e}} small{{color:#667}}
.secondaire{{background:#888}}
</style></head><body><main><h1>{html.escape(titre)}</h1>{contenu}</main></body></html>"""
    return HTMLResponse(corps, status_code=statut, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _formulaire(action: str, champ: str, etiquette: str, type_champ: str = "text", extra: str = "",
                bouton: str = "Valider") -> str:
    return (f'<form method="post" action="{action}"><label>{etiquette}<input name="{champ}" type="{type_champ}" '
            f'required {extra}></label><button type="submit">{bouton}</button></form>')


def _ip(request: Request) -> str:
    # Dernière adresse de X-Forwarded-For : celle ajoutée par le proxy de Railway (les premières
    # peuvent être inventées par n'importe qui)
    transmis = request.headers.get("x-forwarded-for", "")
    return transmis.split(",")[-1].strip() or (request.client.host if request.client else "?")


def _acces_valide(request: Request) -> bool:
    jeton = request.cookies.get("tglogin", "")
    expire = _acces.get(jeton)
    return bool(jeton) and expire is not None and expire > time.time()


def _bloque(ip: str) -> bool:
    """Au plus 5 mauvais mots de passe par quart d'heure et par adresse IP, et 20 au total."""
    for cle in list(_essais):
        _essais[cle] = [t for t in _essais[cle] if time.time() - t < 900]
    total = sum(len(v) for v in _essais.values())
    return len(_essais.get(ip, [])) >= 5 or total >= 20


def _page_principale(message: str = "") -> HTMLResponse:
    if etat["connecte"]:
        contenu = (f'<p class="ok">✅ Veille connectée : <b>{html.escape(etat["compte"] or "")}</b></p>'
                   f'<p>Messages lus depuis le démarrage : {etat["messages_lus"]}</p>'
                   '<form method="post" action="/tglogin/deconnexion"><button class="secondaire" type="submit">'
                   'Déconnecter le compte</button></form>')
    else:
        contenu = ((f'<p class="erreur">{html.escape(etat["erreur"])}</p>' if etat["erreur"] else "")
                   + _formulaire("/tglogin/telephone", "telephone", "Ton numéro de téléphone (format international)",
                                 "tel", 'placeholder="+33612345678" autocomplete="tel"', "Recevoir le code"))
    return _page("Connexion de la veille Telegram", message + contenu)


@router.get("/tglogin")
async def tglogin(request: Request):
    manque = configuree()
    if manque:
        return _page("Veille Telegram désactivée", f"<p>Impossible d'utiliser cette page : {html.escape(manque)}.</p>", 503)
    if not _acces_valide(request):
        return _page("Accès protégé", _formulaire("/tglogin", "mot_de_passe", "Mot de passe (TGLOGIN_PASSWORD)",
                                                   "password", 'autocomplete="current-password"', "Entrer"))
    return _page_principale()


@router.post("/tglogin")
async def tglogin_mot_de_passe(request: Request, mot_de_passe: str = Form(...)):
    if configuree():
        return RedirectResponse("/tglogin", status_code=303)
    ip = _ip(request)
    if _bloque(ip):
        return _page("Accès protégé", '<p class="erreur">Trop d\'essais. Réessaie dans 15 minutes.</p>', 429)
    if not hmac.compare_digest(mot_de_passe.encode(), config.TGLOGIN_PASSWORD.encode()):
        _essais.setdefault(ip, []).append(time.time())
        log.warning("/tglogin : mauvais mot de passe depuis %s", ip)
        return _page("Accès protégé", '<p class="erreur">Mot de passe incorrect.</p>'
                     + _formulaire("/tglogin", "mot_de_passe", "Mot de passe", "password", "", "Entrer"), 401)
    jeton = secrets.token_urlsafe(32)
    _acces[jeton] = time.time() + DUREE_ACCES_S
    reponse = RedirectResponse("/tglogin", status_code=303)
    reponse.set_cookie("tglogin", jeton, max_age=DUREE_ACCES_S, httponly=True, samesite="strict",
                       secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return reponse


def _refuser_sans_acces(request: Request):
    if configuree() or not _acces_valide(request):
        return RedirectResponse("/tglogin", status_code=303)
    return None


@router.post("/tglogin/telephone")
async def tglogin_telephone(request: Request, telephone: str = Form(...)):
    if refus := _refuser_sans_acces(request):
        return refus
    telephone = re.sub(r"[\s.()-]", "", telephone)
    if not re.fullmatch(r"\+\d{8,15}", telephone):
        return _page_principale('<p class="erreur">Numéro invalide : écris-le avec l\'indicatif, ex : +33612345678.</p>')
    ancien = _connexion.get("client")
    if ancien is not None:
        await ancien.disconnect()
    client = nouveau_client()
    try:
        await client.connect()
        envoi = await client.send_code_request(telephone)
    except errors.PhoneNumberInvalidError:
        await client.disconnect()
        return _page_principale('<p class="erreur">Telegram ne reconnaît pas ce numéro.</p>')
    except errors.FloodWaitError as attente:
        await client.disconnect()
        return _page_principale(f'<p class="erreur">Telegram demande d\'attendre {attente.seconds} secondes.</p>')
    _connexion.update(client=client, telephone=telephone, hash=envoi.phone_code_hash, expire=time.time() + 600)
    return _page("Code de connexion", "<p>Telegram vient d'envoyer un code dans l'application Telegram de ton "
                 "téléphone (conversation « Telegram »).</p>"
                 + _formulaire("/tglogin/code", "code", "Code reçu", "text",
                               'inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]*"', "Se connecter"))


@router.post("/tglogin/code")
async def tglogin_code(request: Request, code: str = Form(...)):
    if refus := _refuser_sans_acces(request):
        return refus
    client = _connexion.get("client")
    if client is None or _connexion.get("expire", 0) < time.time():
        return _page_principale('<p class="erreur">Connexion expirée : recommence.</p>')
    try:
        await client.sign_in(_connexion["telephone"], code.replace(" ", ""), phone_code_hash=_connexion["hash"])
    except errors.SessionPasswordNeededError:
        return _page("Mot de passe 2FA", "<p>Ton compte est protégé par un mot de passe (validation en deux étapes).</p>"
                     + _formulaire("/tglogin/2fa", "mot_de_passe_2fa", "Mot de passe 2FA Telegram", "password",
                                   'autocomplete="current-password"', "Valider"))
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
        return _page("Code de connexion", '<p class="erreur">Code incorrect ou expiré.</p>'
                     + _formulaire("/tglogin/code", "code", "Code reçu", "text", 'inputmode="numeric"', "Se connecter"))
    return await _terminer(client)


@router.post("/tglogin/2fa")
async def tglogin_2fa(request: Request, mot_de_passe_2fa: str = Form(...)):
    if refus := _refuser_sans_acces(request):
        return refus
    client = _connexion.get("client")
    if client is None or _connexion.get("expire", 0) < time.time():
        return _page_principale('<p class="erreur">Connexion expirée : recommence.</p>')
    try:
        await client.sign_in(password=mot_de_passe_2fa)
    except errors.PasswordHashInvalidError:
        return _page("Mot de passe 2FA", '<p class="erreur">Mot de passe incorrect.</p>'
                     + _formulaire("/tglogin/2fa", "mot_de_passe_2fa", "Mot de passe 2FA Telegram", "password", "", "Valider"))
    return await _terminer(client)


async def _terminer(client: ClientLectureSeule) -> HTMLResponse:
    """Connexion réussie : session chiffrée en base, veille activée, message sur le bot."""
    sauver_session(client.session.save())
    _connexion.clear()
    await activer(client)
    if _application is not None:
        try:
            await telegram_bot.envoyer_message(_application, f"✅ Veille Telegram connectée ({html.escape(etat['compte'])}). "
                                               "Ajoute des canaux avec /addchannel <lien>.")
        except Exception:
            log.warning("Message de confirmation de la veille non envoyé")
    return _page("Veille connectée", f'<p class="ok">✅ Connecté : <b>{html.escape(etat["compte"])}</b>.</p>'
                 "<p>La session est enregistrée (chiffrée) dans la base. Tu peux fermer cette page "
                 "et ajouter des canaux depuis le bot avec /addchannel.</p>")


@router.post("/tglogin/deconnexion")
async def tglogin_deconnexion(request: Request):
    if refus := _refuser_sans_acces(request):
        return refus
    client = etat.get("client")
    if client is not None:
        try:
            await client.log_out()  # supprime aussi la session côté Telegram
        except Exception:
            await client.disconnect()
    db.ecrire_parametre("tg_session", "")
    etat.update(client=None, connecte=False, compte=None, erreur="compte déconnecté depuis /tglogin")
    marquer(NOM_MODULE, "en attente", etat["erreur"])
    return _page_principale('<p class="ok">Compte déconnecté et session supprimée.</p>')
