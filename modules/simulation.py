"""
modules/simulation.py — Trades simulés (sans argent réel) pour mesurer la qualité des alertes.

Pour chaque alerte (wallet, signal fort, dev, /scanall, canal Telegram) :
  1. on relève le prix du token au signal, puis à 5 min, 1 h et 24 h (même si l'alerte est ignorée) ;
  2. portefeuille « Bot seul » : achat simulé de 0,05 SOL si la note du token atteint /minscore ;
  3. portefeuille « Mes choix » : achat simulé seulement quand tu appuies sur ✅ Accepter
     (au prix du moment du clic) ; chaque clic Accepter / Ignorer est enregistré avec l'heure.

Toutes les minutes, les positions ouvertes sont revérifiées (DexScreener) et fermées selon les
règles de rules.json (section "simulation") : prise de profit +100 %, stop loss -40 %, 24 h maximum.
Frais + glissement : 2 % à l'achat ET à la vente.

Les résultats sont calculés à partir du prix en dollars du token (les variations du SOL lui-même
sont ignorées). Tout est enregistré dans la base (Volume Railway) : rien n'est perdu au redémarrage.

Commandes : /stats, /journal, /positions (+ bouton ✅ Accepter sous chaque alerte)
"""

import asyncio
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
import telegram_bot

log = logging.getLogger(__name__)

URL_DEXSCREENER_LOT = "https://api.dexscreener.com/tokens/v1/solana/{mints}"   # 30 tokens par appel
URL_SOL_EUR = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=eur"

NOMS_TYPES = {"achat_wallet": "wallet", "signal_fort": "signal fort", "nouveau_token_dev": "dev",
              "nouveau_token_scan": "scanall", "veille": "canal"}
PORTEFEUILLES = {"bot": "🤖 Bot seul", "moi": "👤 Mes choix"}

# Délai et tolérance de chaque relevé de prix (secondes) : un relevé raté (bot éteint...) reste vide
RELEVES = (("prix_5min", 300, 900), ("prix_1h", 3600, 1800), ("prix_24h", 86400, 7200))


def regles() -> dict:
    """Section "simulation" de rules.json, avec des valeurs par défaut si elle manque."""
    try:
        from modules import analyse
        r = analyse.charger_regles().get("simulation") or {}
    except Exception:
        r = {}
    return {
        "mise": float(r.get("mise_sol", 0.05)),
        "frais": float(r.get("frais_glissement_pct", 2)),
        "tp": float(r.get("prise_de_profit_pct", 100)),
        "sl": float(r.get("stop_loss_pct", -40)),
        "duree_h": float(r.get("duree_max_heures", 24)),
        "intervalle": max(10, int(r.get("intervalle_verification_s", 60))),
    }


# ---------------------------------------------------------------------------
# Prix
# ---------------------------------------------------------------------------

async def prix_actuels(mints: list[str]) -> dict[str, tuple[float, str | None]]:
    """{mint: (prix en $, symbole)} d'après la paire la plus liquide sur DexScreener."""
    resultat: dict[str, tuple[float, str | None, float]] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for i in range(0, len(mints), 30):
            reponse = await client.get(URL_DEXSCREENER_LOT.format(mints=",".join(mints[i:i + 30])))
            paires = reponse.json() if reponse.status_code == 200 else []
            for p in paires if isinstance(paires, list) else []:
                token = p.get("baseToken") or {}
                prix, liquidite = float(p.get("priceUsd") or 0), float((p.get("liquidity") or {}).get("usd") or 0)
                mint = token.get("address")
                if mint and prix > 0 and liquidite >= resultat.get(mint, (0, None, -1))[2]:
                    resultat[mint] = (prix, token.get("symbol"), liquidite)
    return {m: (p, s) for m, (p, s, _) in resultat.items()}


_cache_eur: list = [0.0, None]


async def sol_en_eur() -> float | None:
    """Prix d'1 SOL en euros (CoinGecko, mémorisé 10 minutes). None si indisponible."""
    if time.time() - _cache_eur[0] < 600 and _cache_eur[1]:
        return _cache_eur[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            reponse = await client.get(URL_SOL_EUR)
        _cache_eur[:] = [time.time(), float(reponse.json()["solana"]["eur"])]
    except Exception:
        log.warning("Prix du SOL en euros indisponible")
    return _cache_eur[1]


def _prix(prix: float | None) -> str:
    if not prix:
        return "?"
    from modules.wallets import formater_prix
    return formater_prix(prix, "$")


# ---------------------------------------------------------------------------
# À chaque alerte
# ---------------------------------------------------------------------------

def _score_minimum() -> int:
    try:
        return int(db.lire_parametre("score_minimum", "0") or 0)
    except ValueError:
        return 0


def bot_achete(analyse_token) -> bool:
    """« Bot seul » achète si la note atteint /minscore (DANGER = 0 ; note inconnue = seulement si /minscore est 0)."""
    minimum = _score_minimum()
    if analyse_token is None:
        return minimum == 0
    return analyse_token.score_pour_filtre >= minimum


async def nouvelle_alerte(alerte_id: int, type_alerte: str, mint: str, symbole: str | None,
                          wallet_adresse: str | None, analyse_token=None) -> None:
    """Appelée juste après l'envoi d'une alerte : relevé du prix au signal + achat « Bot seul ». Ne plante jamais."""
    try:
        r = regles()
        maintenant = int(time.time())
        try:
            prix, symbole_dex = (await prix_actuels([mint])).get(mint, (None, None))
        except Exception:
            prix, symbole_dex = None, None  # pas encore listé : le prix sera pris au prochain tour
        symbole = symbole or symbole_dex
        note = analyse_token.note if analyse_token is not None else None
        db.creer_suivi_alerte(alerte_id, mint, type_alerte, wallet_adresse, maintenant, prix, note)
        if bot_achete(analyse_token):
            db.ouvrir_position("bot", alerte_id, mint, symbole, type_alerte, wallet_adresse, r["mise"], prix,
                               r["frais"], maintenant)
            log.info("Simulation : achat « Bot seul » de %s (%s)", symbole or mint, type_alerte)
    except Exception:
        log.exception("Simulation : alerte %s non enregistrée", alerte_id)


def programmer(alerte_id: int | None, type_alerte: str, mint: str, symbole: str | None,
               wallet_adresse: str | None, analyse_token=None) -> None:
    """Version « lance et oublie », pour les modules qui envoient les alertes."""
    if alerte_id:
        asyncio.create_task(nouvelle_alerte(alerte_id, type_alerte, mint, symbole, wallet_adresse, analyse_token))


# ---------------------------------------------------------------------------
# Boucle : toutes les minutes
# ---------------------------------------------------------------------------

def _releves_utiles(maintenant: int) -> list:
    """Relevés de prix dus maintenant (on ignore ceux dont la fenêtre est passée)."""
    utiles = []
    for s in db.releves_a_faire(maintenant):
        age = maintenant - s["cree_le"]
        if s["prix_signal"] is None and age <= 900:
            utiles.append(s)
            continue
        if any(s[col] is None and delai <= age <= delai + tolerance for col, delai, tolerance in RELEVES):
            utiles.append(s)
    return utiles


async def un_tour(maintenant: int | None = None) -> dict:
    """Met à jour les positions et les relevés de prix. Renvoie un petit compte rendu (pour les tests et les logs)."""
    r = regles()
    maintenant = int(maintenant or time.time())
    ouvertes, releves = db.positions(), _releves_utiles(maintenant)
    mints = sorted({p["token_adresse"] for p in ouvertes} | {s["mint"] for s in releves})
    bilan = {"verifies": len(mints), "ouverts": 0, "fermes": 0, "annules": 0, "releves": 0}
    if not mints:
        return bilan
    try:
        prix = {m: p for m, (p, _) in (await prix_actuels(mints)).items()}
    except Exception as erreur:
        log.warning("Simulation : DexScreener injoignable (%s), on réessaie au prochain tour", type(erreur).__name__)
        prix = {}

    for p in ouvertes:
        actuel = prix.get(p["token_adresse"])
        if p["statut"] == "en_attente":  # alerte arrivée avant que le token soit listé
            if actuel:
                db.fixer_prix_entree(p["id"], actuel, r["frais"])
                bilan["ouverts"] += 1
            elif maintenant - (p["ouvert_le"] or maintenant) > 900:
                db.fermer_position(p["id"], None, r["frais"], "aucun prix pendant 15 min", maintenant)
                bilan["annules"] += 1
            continue
        if actuel:
            db.maj_prix_position(p["id"], actuel)
        dernier = actuel or p["prix_actuel"]
        variation = (dernier / p["prix_entree_marche"] - 1) * 100 if dernier and p["prix_entree_marche"] else 0
        raison = None
        if actuel and variation >= r["tp"]:
            raison = f"prise de profit (+{r['tp']:.0f} %)"
        elif actuel and variation <= r["sl"]:
            raison = f"stop loss ({r['sl']:.0f} %)"
        elif maintenant - (p["ouvert_le"] or maintenant) >= r["duree_h"] * 3600:
            raison = f"durée max ({r['duree_h']:.0f} h)" + ("" if actuel else " — dernier prix connu")
        if raison:
            db.fermer_position(p["id"], dernier, r["frais"], raison, maintenant)
            bilan["fermes"] += 1

    for s in releves:
        actuel = prix.get(s["mint"])
        if not actuel:
            continue
        age = maintenant - s["cree_le"]
        if s["prix_signal"] is None and age <= 900:  # plus tard, ce ne serait plus le « prix du signal »
            db.maj_releve(s["alerte_id"], "prix_signal", actuel)
            bilan["releves"] += 1
        for colonne, delai, tolerance in RELEVES:
            if s[colonne] is None and delai <= age <= delai + tolerance:
                db.maj_releve(s["alerte_id"], colonne, actuel)
                bilan["releves"] += 1
    return bilan


async def boucle(application: Application) -> None:
    log.info("Simulation : vérification des positions toutes les %d s", regles()["intervalle"])
    while True:
        try:
            bilan = await un_tour()
            if bilan["fermes"] or bilan["ouverts"]:
                log.info("Simulation : %s", bilan)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Simulation : erreur pendant la vérification")
        await asyncio.sleep(regles()["intervalle"])


# ---------------------------------------------------------------------------
# Boutons ✅ Accepter / 🙈 Ignorer
# ---------------------------------------------------------------------------

def bouton_jupiter_seul(mint: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Acheter sur Jupiter", url=f"https://jup.ag/swap/SOL-{mint}")]])


async def bouton_accepter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clic sur ✅ Accepter : décision enregistrée + achat simulé dans « Mes choix » au prix du moment."""
    query = update.callback_query
    if query.message is None or query.message.chat.id != config.TELEGRAM_CHAT_ID:
        await query.answer()
        return
    alerte = db.alerte_par_message(query.message.message_id)
    if alerte is None:
        await query.answer("Alerte introuvable dans la base")
        return
    if alerte["decision"] or alerte["statut"] in ("acceptee", "ignoree"):
        await query.answer("Tu as déjà décidé pour cette alerte")
        return
    db.enregistrer_decision(alerte["id"], "accepter")
    r = regles()
    mint = alerte["token_adresse"]
    try:
        prix, symbole = (await prix_actuels([mint])).get(mint, (None, None))
    except Exception:
        prix, symbole = None, None
    db.ouvrir_position("moi", alerte["id"], mint, symbole, alerte["type_suivi"] or alerte["type"],
                       alerte["wallet_adresse"], r["mise"], prix, r["frais"], int(time.time()))
    await query.answer("Position simulée ouverte dans « Mes choix »")
    suite = (f"✅ Acceptée — achat simulé de {r['mise']:g} SOL à {_prix(prix)}" if prix
             else f"✅ Acceptée — achat simulé de {r['mise']:g} SOL dès que le prix sera connu")
    await query.edit_message_text(query.message.text_html + f"\n\n<i>{suite}</i>", parse_mode=ParseMode.HTML,
                                  reply_markup=bouton_jupiter_seul(mint),
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))


def enregistrer_ignorer(telegram_message_id: int) -> str | None:
    """Appelé par le bouton 🙈 Ignorer (modules/wallets.py). Renvoie un message si c'était déjà décidé."""
    alerte = db.alerte_par_message(telegram_message_id)
    if alerte is None:
        return None
    if alerte["decision"] or alerte["statut"] in ("acceptee", "ignoree"):
        return "Tu as déjà décidé pour cette alerte"
    db.enregistrer_decision(alerte["id"], "ignorer")
    return None


# ---------------------------------------------------------------------------
# /stats, /journal, /positions
# ---------------------------------------------------------------------------

def _resume(trades: list) -> dict:
    gagnants = [t for t in trades if (t["pnl_sol"] or 0) > 0]
    perdants = [t for t in trades if (t["pnl_sol"] or 0) <= 0]

    def moyenne(liste, cle):
        return sum(t[cle] or 0 for t in liste) / len(liste) if liste else None

    return {
        "n": len(trades), "reussite": len(gagnants) / len(trades) * 100 if trades else None,
        "gain": moyenne(gagnants, "pnl_sol"), "gain_pct": moyenne(gagnants, "pnl_pct"),
        "perte": moyenne(perdants, "pnl_sol"), "perte_pct": moyenne(perdants, "pnl_pct"),
        "total": sum(t["pnl_sol"] or 0 for t in trades),
    }


def _sol_eur(sol: float | None, eur: float | None) -> str:
    if sol is None:
        return "—"
    return f"{sol:+.3f} SOL" + (f" ({sol * eur:+.2f} €)" if eur else "")


def _ligne_resume(nom: str, x: dict, eur: float | None) -> str:
    if not x["n"]:
        return f"• {nom} : aucun trade terminé"
    return f"• {nom} : {x['n']} trade(s) · {x['reussite']:.0f} % gagnants · {_sol_eur(x['total'], eur)}"


def _nom_wallet(adresse: str | None) -> str:
    if not adresse:
        return "?"
    w = db.lire_wallet(adresse)
    return (w["nom"] if w and w["nom"] else None) or f"{adresse[:4]}…{adresse[-4:]}"


async def texte_stats() -> str:
    r, eur = regles(), await sol_en_eur()
    fermes = db.trades_fermes()
    ouverts = db.positions()
    par_portefeuille = {p: _resume([t for t in fermes if t["portefeuille"] == p]) for p in PORTEFEUILLES}

    lignes = [
        "📊 <b>Trades simulés</b>",
        f"<i>Mise {r['mise']:g} SOL · frais + glissement {r['frais']:g} % à l'achat et à la vente · "
        f"sortie à +{r['tp']:.0f} %, {r['sl']:.0f} % ou après {r['duree_h']:.0f} h"
        + (f" · 1 SOL ≈ {eur:.2f} €" if eur else " · cours SOL/€ indisponible") + "</i>",
        "",
        "<b>🤖 Bot seul  contre  👤 Mes choix</b>",
    ]

    def cellule(x: dict, cle: str) -> str:
        v = x[cle]
        if v is None:
            return "—"
        if cle == "reussite":
            return f"{v:.0f} %"
        if cle in ("gain", "perte"):
            pct = x[cle + "_pct"]
            return f"{v:+.3f} ({pct:+.0f} %)"
        return f"{v:+.3f} SOL"

    b, m = par_portefeuille["bot"], par_portefeuille["moi"]
    tableau = [f"{'':14}{'Bot seul':>18}{'Mes choix':>18}",
               f"{'Trades finis':14}{b['n']:>18}{m['n']:>18}",
               f"{'En cours':14}{sum(1 for o in ouverts if o['portefeuille'] == 'bot'):>18}"
               f"{sum(1 for o in ouverts if o['portefeuille'] == 'moi'):>18}"]
    for titre, cle in (("Réussite", "reussite"), ("Gain moyen", "gain"), ("Perte moyenne", "perte"), ("Résultat", "total")):
        tableau.append(f"{titre:14}{cellule(b, cle):>18}{cellule(m, cle):>18}")
    if eur:
        tableau.append(f"{'Résultat (€)':14}{b['total'] * eur:>16.2f} €{m['total'] * eur:>16.2f} €")
    lignes.append("<pre>" + html.escape("\n".join(tableau)) + "</pre>")

    # Par type d'alerte (Bot seul : il prend toutes les alertes, c'est la mesure la plus juste)
    bot = [t for t in fermes if t["portefeuille"] == "bot"]
    suivis = db.suivis_alertes()
    lignes += ["", "<b>Par type d'alerte</b> <i>(Bot seul)</i>"]
    for type_alerte, nom in NOMS_TYPES.items():
        x = _resume([t for t in bot if t["type_alerte"] == type_alerte])
        alertes = [s for s in suivis if s["type_alerte"] == type_alerte]
        if not x["n"] and not alertes:
            continue
        ligne = _ligne_resume(nom, x, eur)
        variations = []
        for colonne, etiquette in (("prix_1h", "1 h"), ("prix_24h", "24 h")):
            v = [s[colonne] / s["prix_signal"] * 100 - 100 for s in alertes if s[colonne] and s["prix_signal"]]
            if v:
                variations.append(f"{etiquette} {sum(v) / len(v):+.0f} %")
        if variations:
            ligne += f"\n   prix moyen après l'alerte : {', '.join(variations)} ({len(alertes)} alerte(s))"
        lignes.append(ligne)

    # Par wallet suivi
    par_wallet: dict[str, list] = {}
    for t in bot:
        if t["type_alerte"] in ("achat_wallet", "signal_fort") and t["wallet_adresse"]:
            par_wallet.setdefault(t["wallet_adresse"], []).append(t)
    if par_wallet:
        lignes += ["", "<b>Par wallet suivi</b> <i>(Bot seul)</i>"]
        classes = sorted(par_wallet.items(), key=lambda kv: -sum(t["pnl_sol"] or 0 for t in kv[1]))
        for adresse, trades in classes[:10]:
            lignes.append(_ligne_resume(html.escape(_nom_wallet(adresse)), _resume(trades), eur))

    # Décisions : avais-tu raison d'ignorer ?
    acceptees = [s for s in suivis if s["decision"] == "acceptee"]
    ignorees = [s for s in suivis if s["decision"] == "ignoree"]
    if acceptees or ignorees:
        v = [s["prix_24h"] / s["prix_signal"] * 100 - 100 for s in ignorees if s["prix_24h"] and s["prix_signal"]]
        lignes += ["", f"<b>Tes décisions :</b> {len(acceptees)} acceptée(s), {len(ignorees)} ignorée(s)"
                   + (f"\n   les alertes ignorées ont fait en moyenne {sum(v) / len(v):+.0f} % en 24 h" if v else "")]
    if not fermes and not ouverts:
        lignes += ["", "Aucun trade simulé pour l'instant : ils démarrent avec les prochaines alertes."]
    return "\n".join(lignes)


def _duree(secondes: float) -> str:
    from modules.analyse_wallet import formater_duree
    return formater_duree(secondes)


def texte_journal(limite: int = 10) -> str:
    trades = db.trades_fermes(limite=limite)
    if not trades:
        return "📒 Aucun trade simulé terminé pour l'instant."
    fuseau = ZoneInfo(config.FUSEAU_HORAIRE)
    lignes = [f"📒 <b>{len(trades)} derniers trades simulés</b>", ""]
    for t in trades:
        icone = "🟢" if (t["pnl_sol"] or 0) > 0 else "🔴"
        quand = datetime.fromtimestamp(t["ferme_le"], fuseau).strftime("%d/%m %H:%M") if t["ferme_le"] else "?"
        origine = NOMS_TYPES.get(t["type_alerte"], t["type_alerte"] or "?")
        if t["type_alerte"] in ("achat_wallet", "signal_fort"):
            origine += f" {_nom_wallet(t['wallet_adresse'])}"
        lignes.append(
            f"{icone} <b>{(t['pnl_sol'] or 0):+.4f} SOL</b> ({(t['pnl_pct'] or 0):+.0f} %) · "
            f"<b>{html.escape(t['symbole'] or t['token_adresse'][:6])}</b> · {PORTEFEUILLES.get(t['portefeuille'], '?')}\n"
            f"   {html.escape(origine)} · {html.escape(t['raison_sortie'] or '?')} · "
            f"gardé {_duree((t['ferme_le'] or 0) - (t['ouvert_le'] or 0))} · {quand}"
        )
    return "\n".join(lignes)


def texte_positions() -> str:
    ouverts = db.positions()
    if not ouverts:
        return "📂 Aucune position simulée ouverte."
    r, maintenant = regles(), time.time()
    lignes = [f"📂 <b>{len(ouverts)} position(s) simulée(s) ouverte(s)</b>", ""]
    for p in ouverts:
        nom = html.escape(p["symbole"] or p["token_adresse"][:6])
        restant = r["duree_h"] * 3600 - (maintenant - (p["ouvert_le"] or maintenant))
        if p["statut"] == "en_attente":
            lignes.append(f"⏳ <b>{nom}</b> · {PORTEFEUILLES.get(p['portefeuille'])} · en attente du premier prix")
            continue
        variation = (p["prix_actuel"] / p["prix_entree_marche"] - 1) * 100 if p["prix_actuel"] and p["prix_entree_marche"] else 0
        net = (p["montant_sol"] * p["prix_actuel"] * (1 - r["frais"] / 100) / p["prix_entree"] - p["montant_sol"]
               if p["prix_actuel"] and p["prix_entree"] else 0)
        lignes.append(
            f"{'🟢' if net > 0 else '🔴'} <b>{nom}</b> · {PORTEFEUILLES.get(p['portefeuille'])} · "
            f"{NOMS_TYPES.get(p['type_alerte'], '?')}\n"
            f"   entrée {_prix(p['prix_entree_marche'])} → {_prix(p['prix_actuel'])} ({variation:+.0f} %) · "
            f"si vendu maintenant {net:+.4f} SOL\n"
            f"   ouvert depuis {_duree(maintenant - (p['ouvert_le'] or maintenant))} · "
            f"sortie forcée dans {_duree(max(0, restant))}"
        )
    return "\n".join(lignes)


async def _repondre(update: Update, texte: str) -> None:
    await update.message.reply_text(texte, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _repondre(update, await texte_stats())


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _repondre(update, texte_journal())


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _repondre(update, texte_positions())


def enregistrer_commandes(application: Application) -> None:
    moi = telegram_bot.SEULEMENT_MOI
    application.add_handler(CommandHandler("stats", cmd_stats, filters=moi))
    application.add_handler(CommandHandler("journal", cmd_journal, filters=moi))
    application.add_handler(CommandHandler("positions", cmd_positions, filters=moi))
    application.add_handler(CallbackQueryHandler(bouton_accepter, pattern="^accepter$"))
