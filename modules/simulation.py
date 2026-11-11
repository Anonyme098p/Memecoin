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

Vente manuelle : sous chaque position de /positions, boutons « Vendre 50 % », « Vendre 100 % » et
« Actualiser » (ou /vendre <token ou numéro> <pourcentage>). La vente se fait au prix DexScreener du
moment moins les frais, et apparaît dans /journal avec la mention « vente manuelle ». Après une vente
manuelle, le bot continue de suivre le prix pour calculer ce que les RÈGLES auraient donné : /stats
compare ainsi tes interventions aux ventes automatiques.

Commandes : /stats, /journal, /positions, /vendre (+ bouton ✅ Accepter sous chaque alerte)
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

def raison_regles(variation_pct: float, prix_frais: bool, age_s: float, r: dict) -> str | None:
    """
    Les règles de sortie automatique (rules.json) : renvoie la raison de vendre, ou None pour garder.
    `prix_frais` : True si le prix vient d'être relevé (sinon, seule la durée max peut déclencher la vente).
    """
    if prix_frais and variation_pct >= r["tp"]:
        return f"prise de profit (+{r['tp']:.0f} %)"
    if prix_frais and variation_pct <= r["sl"]:
        return f"stop loss ({r['sl']:.0f} %)"
    if age_s >= r["duree_h"] * 3600:
        return f"durée max ({r['duree_h']:.0f} h)" + ("" if prix_frais else " — dernier prix connu")
    return None


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
    suivis_regles = db.suivis_regles_en_cours()  # ventes manuelles : que feraient les règles ?
    mints = sorted({p["token_adresse"] for p in ouvertes} | {s["mint"] for s in releves}
                   | {v["token_adresse"] for v in suivis_regles})
    bilan = {"verifies": len(mints), "ouverts": 0, "fermes": 0, "annules": 0, "releves": 0, "regles_finies": 0}
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
        raison = raison_regles(variation, bool(actuel), maintenant - (p["ouvert_le"] or maintenant), r)
        if raison:
            db.fermer_position(p["id"], dernier, r["frais"], raison, maintenant)
            bilan["fermes"] += 1

    # Ventes manuelles : on applique les règles au prix du moment, comme si tu n'avais rien fait
    for v in suivis_regles:
        actuel = prix.get(v["token_adresse"])
        dernier = actuel or v["regles_prix"]
        variation = (dernier / v["prix_entree_marche"] - 1) * 100 if dernier and v["prix_entree_marche"] else 0
        raison = raison_regles(variation, bool(actuel), maintenant - (v["ouvert_le"] or maintenant), r)
        if raison:
            pnl = (v["montant_sol"] * (dernier or 0) * (1 - r["frais"] / 100) / v["prix_entree"] - v["montant_sol"]
                   if v["prix_entree"] else 0)
            db.maj_suivi_regles(v["id"], actuel, fini=True, pnl_sol=pnl, pnl_pct=pnl / v["montant_sol"] * 100,
                                raison=raison)
            bilan["regles_finies"] += 1
        elif actuel:
            db.maj_suivi_regles(v["id"], actuel)

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

    lignes += ["", *_lignes_manuel_contre_auto(fermes, eur)]

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


def _lignes_manuel_contre_auto(fermes: list, eur: float | None) -> list[str]:
    """/stats : ventes faites par les règles contre ventes faites par toi (et ce que les règles auraient fait)."""
    auto = [t for t in fermes if not t["vente_manuelle"]]
    manu = [t for t in fermes if t["vente_manuelle"]]
    lignes = ["<b>⚙️ Ventes automatiques  contre  ✋ ventes manuelles</b>"]

    def detail(nom: str, x: dict) -> str:
        if not x["n"]:
            return f"• {nom} : aucune vente"
        ligne = f"• {nom} : {x['n']} vente(s) · {x['reussite']:.0f} % gagnantes · {_sol_eur(x['total'], eur)}"
        morceaux = []
        if x["gain"] is not None:
            morceaux.append(f"gain moyen {x['gain']:+.3f} SOL ({x['gain_pct']:+.0f} %)")
        if x["perte"] is not None:
            morceaux.append(f"perte moyenne {x['perte']:+.3f} SOL ({x['perte_pct']:+.0f} %)")
        return ligne + ("\n   " + " · ".join(morceaux) if morceaux else "")

    lignes.append(detail("⚙️ par les règles", _resume(auto)))
    if not manu:
        lignes.append("• ✋ par toi : aucune vente manuelle pour l'instant (boutons sous /positions, ou /vendre)")
        return lignes
    lignes.append(detail("✋ par toi", _resume(manu)))

    # Même trades, deux façons de sortir : c'est la comparaison la plus juste
    finis = [t for t in manu if t["regles_statut"] == "fini"]
    en_cours = len(manu) - len(finis)
    if finis:
        toi = sum(t["pnl_sol"] or 0 for t in finis)
        regles_seules = sum(t["regles_pnl_sol"] or 0 for t in finis)
        ecart = toi - regles_seules
        verdict = ("✅ tes interventions ont fait MIEUX que les règles" if ecart > 0.00005 else
                   "❌ tes interventions ont fait MOINS BIEN que les règles" if ecart < -0.00005 else
                   "➖ même résultat que les règles")
        lignes.append(f"• Sur ces mêmes {len(finis)} vente(s), si tu avais laissé faire les règles : "
                      f"{_sol_eur(regles_seules, eur)} au lieu de {_sol_eur(toi, eur)}\n"
                      f"   {verdict} : écart {_sol_eur(ecart, eur)}")
    if en_cours:
        lignes.append(f"   <i>{en_cours} vente(s) manuelle(s) encore suivie(s) : les règles n'auraient pas encore vendu</i>")
    return lignes


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
        raison = html.escape(t["raison_sortie"] or "?")
        if t["vente_manuelle"]:
            raison = "✋ " + raison
            if t["regles_statut"] == "fini" and t["regles_pnl_sol"] is not None:
                raison += f" (les règles : {t['regles_pnl_sol']:+.4f} SOL)"
        lignes.append(
            f"{icone} <b>{(t['pnl_sol'] or 0):+.4f} SOL</b> ({(t['pnl_pct'] or 0):+.0f} %) · "
            f"<b>{html.escape(t['symbole'] or t['token_adresse'][:6])}</b> · {PORTEFEUILLES.get(t['portefeuille'], '?')}\n"
            f"   {html.escape(origine)} · {raison} · "
            f"gardé {_duree((t['ferme_le'] or 0) - (t['ouvert_le'] or 0))} · {quand}"
        )
    return "\n".join(lignes)


def _valeur_nette(p, prix: float | None, frais_pct: float) -> float | None:
    """Gain ou perte en SOL si on vendait maintenant à `prix` (frais de sortie déduits)."""
    if not prix or not p["prix_entree"]:
        return None
    return p["montant_sol"] * prix * (1 - frais_pct / 100) / p["prix_entree"] - p["montant_sol"]


def texte_carte(p, eur: float | None) -> str:
    """Le détail d'une position ouverte (affiché sous /positions, avec ses boutons)."""
    r, maintenant = regles(), time.time()
    nom = html.escape(p["symbole"] or p["token_adresse"][:6])
    origine = NOMS_TYPES.get(p["type_alerte"], p["type_alerte"] or "?")
    if p["type_alerte"] in ("achat_wallet", "signal_fort"):
        origine += f" {_nom_wallet(p['wallet_adresse'])}"
    entete = f"<b>#{p['id']} · {nom}</b> · {PORTEFEUILLES.get(p['portefeuille'], '?')} · {html.escape(origine)}"
    lien = f'<a href="https://dexscreener.com/solana/{p["token_adresse"]}">DexScreener</a>'
    if p["statut"] == "en_attente":
        return f"⏳ {entete}\n   en attente du premier prix sur DexScreener · {lien}"
    net = _valeur_nette(p, p["prix_actuel"], r["frais"]) or 0
    net_pct = net / p["montant_sol"] * 100 if p["montant_sol"] else 0
    marche = (p["prix_actuel"] / p["prix_entree_marche"] - 1) * 100 if p["prix_actuel"] and p["prix_entree_marche"] else 0
    restant = r["duree_h"] * 3600 - (maintenant - (p["ouvert_le"] or maintenant))
    return "\n".join([
        f"{'🟢' if net > 0 else '🔴'} {entete}",
        f"   Mise : {p['montant_sol']:.4g} SOL",
        f"   Prix d'achat : {_prix(p['prix_entree_marche'])} · prix actuel : {_prix(p['prix_actuel'])} "
        f"({marche:+.0f} %)",
        f"   <b>Si vendu maintenant : {net_pct:+.0f} % · {_sol_eur(net, eur)}</b> "
        f"<i>(frais {r['frais']:g} % compris)</i>",
        f"   Ouvert depuis {_duree(maintenant - (p['ouvert_le'] or maintenant))} · sortie auto à "
        f"+{r['tp']:.0f} % / {r['sl']:.0f} % ou dans {_duree(max(0, restant))} · {lien}",
    ])


def boutons_position(p) -> InlineKeyboardMarkup:
    ligne_maj = [InlineKeyboardButton("🔄 Actualiser", callback_data=f"sim_maj:{p['id']}")]
    if p["statut"] != "ouvert":
        return InlineKeyboardMarkup([ligne_maj])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Vendre 50 %", callback_data=f"sim_vendre:{p['id']}:50"),
         InlineKeyboardButton("💸 Vendre 100 %", callback_data=f"sim_vendre:{p['id']}:100")],
        ligne_maj,
    ])


async def _actualiser_prix(liste: list) -> None:
    """Relève le prix DexScreener des positions avant de les afficher (sans planter si c'est injoignable)."""
    mints = sorted({p["token_adresse"] for p in liste if p["statut"] == "ouvert"})
    if not mints:
        return
    try:
        prix = await prix_actuels(mints)
    except Exception:
        log.warning("Simulation : prix DexScreener indisponibles pour /positions")
        return
    for p in liste:
        if p["token_adresse"] in prix and p["statut"] == "ouvert":
            db.maj_prix_position(p["id"], prix[p["token_adresse"]][0])


MAX_CARTES = 15  # au-delà, on liste les positions en une ligne (trop de messages sinon)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ouverts = db.positions()
    if not ouverts:
        await _repondre(update, "📂 Aucune position simulée ouverte.")
        return
    await _actualiser_prix(ouverts)
    ouverts = sorted(db.positions(), key=lambda p: (p["portefeuille"] != "moi", p["ouvert_le"] or 0))
    r, eur = regles(), await sol_en_eur()
    total = sum(_valeur_nette(p, p["prix_actuel"], r["frais"]) or 0 for p in ouverts)
    await _repondre(update, f"📂 <b>{len(ouverts)} position(s) simulée(s) ouverte(s)</b>\n"
                            f"Si tout était vendu maintenant : {_sol_eur(total, eur)}\n"
                            "<i>Vente aussi possible avec /vendre &lt;numéro ou token&gt; &lt;pourcentage&gt;</i>")
    for p in ouverts[:MAX_CARTES]:
        await update.message.reply_text(texte_carte(p, eur), parse_mode=ParseMode.HTML, reply_markup=boutons_position(p),
                                        link_preview_options=LinkPreviewOptions(is_disabled=True))
    if len(ouverts) > MAX_CARTES:
        reste = [f"#{p['id']} {html.escape(p['symbole'] or p['token_adresse'][:6])}" for p in ouverts[MAX_CARTES:]]
        await _repondre(update, f"… et {len(reste)} autre(s) : {', '.join(reste)}\n"
                                "Pour les vendre : /vendre &lt;numéro&gt; &lt;pourcentage&gt;")


# ---------------------------------------------------------------------------
# Vente manuelle : boutons et /vendre
# ---------------------------------------------------------------------------

async def vendre(trade_id: int, pourcentage: float) -> tuple[bool, str]:
    """Vend `pourcentage` % d'une position au prix DexScreener du moment (moins les frais). Renvoie (réussi, message)."""
    p = db.lire_position(trade_id)
    if p is None:
        return False, f"❓ Position #{trade_id} introuvable. Tape /positions pour la liste."
    nom = html.escape(p["symbole"] or p["token_adresse"][:6])
    if p["statut"] == "en_attente":
        return False, f"⏳ #{trade_id} {nom} : pas encore de prix sur DexScreener, impossible de vendre pour l'instant."
    if p["statut"] != "ouvert":
        return False, f"ℹ️ #{trade_id} {nom} est déjà fermée ({html.escape(p['raison_sortie'] or p['statut'])})."
    try:
        prix = (await prix_actuels([p["token_adresse"]])).get(p["token_adresse"], (None, None))[0]
    except Exception:
        prix = None
    if not prix:
        return False, f"⚠️ Prix DexScreener indisponible pour {nom} : vente NON faite, réessaie dans un instant."
    r = regles()
    db.maj_prix_position(trade_id, prix)
    resultat = db.vendre_position(trade_id, pourcentage, prix, r["frais"], int(time.time()))
    if resultat is None:  # les règles l'ont fermée pendant qu'on cherchait le prix
        return False, f"ℹ️ #{trade_id} {nom} vient d'être fermée par les règles : voir /journal."
    eur = await sol_en_eur()
    texte = (f"✋ <b>Vente manuelle de {pourcentage:g} %</b> · #{trade_id} {nom}\n"
             f"   {resultat['vendu_sol']:.4g} SOL vendus à {_prix(prix)} (frais {r['frais']:g} % déduits)\n"
             f"   {'🟢' if resultat['pnl_sol'] > 0 else '🔴'} <b>{resultat['pnl_pct']:+.0f} % · "
             f"{_sol_eur(resultat['pnl_sol'], eur)}</b>")
    texte += (f"\n   Reste en position : {resultat['reste_sol']:.4g} SOL" if resultat["reste_sol"] > 0
              else "\n   Position fermée. Le bot suit encore le prix pour comparer avec les règles (/stats).")
    log.info("Simulation : vente manuelle de %g %% de la position %d", pourcentage, trade_id)
    return True, texte


def _de_mon_chat(update: Update) -> bool:
    message = update.callback_query.message
    return message is not None and message.chat.id == config.TELEGRAM_CHAT_ID


async def bouton_vendre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons « Vendre 50 % » / « Vendre 100 % » sous une position."""
    query = update.callback_query
    if not _de_mon_chat(update):
        await query.answer()
        return
    _, trade_id, pourcentage = query.data.split(":")
    reussi, texte = await vendre(int(trade_id), float(pourcentage))
    await query.answer("Vente simulée faite" if reussi else "Vente non faite")
    p = db.lire_position(int(trade_id))
    encore_ouverte = p is not None and p["statut"] == "ouvert"
    if encore_ouverte:
        texte += "\n\n" + texte_carte(p, await sol_en_eur())
    await query.edit_message_text(texte, parse_mode=ParseMode.HTML,
                                  reply_markup=boutons_position(p) if encore_ouverte else None,
                                  link_preview_options=LinkPreviewOptions(is_disabled=True))


async def bouton_actualiser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton « Actualiser » : prix DexScreener du moment, la carte est mise à jour."""
    query = update.callback_query
    if not _de_mon_chat(update):
        await query.answer()
        return
    p = db.lire_position(int(query.data.split(":")[1]))
    if p is None or p["statut"] not in ("ouvert", "en_attente"):
        await query.answer("Position fermée")
        texte = "ℹ️ Position fermée" + (f" ({html.escape(p['raison_sortie'] or '')}) : voir /journal." if p else ".")
        await query.edit_message_text(texte, parse_mode=ParseMode.HTML)
        return
    await _actualiser_prix([p])
    p = db.lire_position(p["id"])
    await query.answer("Prix actualisé")
    nouveau = texte_carte(p, await sol_en_eur())
    if nouveau != query.message.text_html:  # Telegram refuse une modification identique
        try:
            await query.edit_message_text(nouveau, parse_mode=ParseMode.HTML, reply_markup=boutons_position(p),
                                          link_preview_options=LinkPreviewOptions(is_disabled=True))
        except Exception as erreur:  # « message is not modified » si rien n'a bougé
            log.debug("Actualiser : %s", erreur)


def trouver_positions(cle: str) -> list:
    """Positions ouvertes qui correspondent à un numéro (#12 ou 12), un symbole ($TST ou TST) ou une adresse."""
    ouvertes = db.positions()
    numero = cle.lstrip("#")
    if numero.isdigit():
        par_numero = [p for p in ouvertes if p["id"] == int(numero)]
        if par_numero or cle.startswith("#"):
            return par_numero
    symbole = cle.lstrip("$").lower()
    return [p for p in ouvertes if p["token_adresse"] == cle or (p["symbole"] or "").lower() == symbole]


async def cmd_vendre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vendre <token ou numéro> <pourcentage> — ex : /vendre 12 50, /vendre TST 100, /vendre #12 25 %"""
    usage = ("Utilisation : /vendre &lt;numéro ou token&gt; &lt;pourcentage&gt;\n"
             "Exemples : <code>/vendre 12 50</code> · <code>/vendre BONK 100</code>\n"
             "Les numéros sont affichés dans /positions (#12).")
    if len(context.args) < 2:
        await _repondre(update, usage)
        return
    try:
        pourcentage = float("".join(context.args[1:]).replace("%", "").replace(",", "."))
    except ValueError:
        pourcentage = 0
    if not 0 < pourcentage <= 100:
        await _repondre(update, "❌ Le pourcentage doit être entre 1 et 100.\n\n" + usage)
        return
    trouvees = trouver_positions(context.args[0])
    if not trouvees:
        await _repondre(update, f"❓ Aucune position ouverte pour « {html.escape(context.args[0])} ». "
                                "Tape /positions pour la liste.")
        return
    if len(trouvees) > 1:
        liste = "\n".join(f"• #{p['id']} {html.escape(p['symbole'] or '?')} · {PORTEFEUILLES.get(p['portefeuille'])}"
                          for p in trouvees)
        await _repondre(update, f"Plusieurs positions correspondent, précise le numéro :\n{liste}\n\n"
                                f"Ex : <code>/vendre {trouvees[0]['id']} {pourcentage:g}</code>")
        return
    _, texte = await vendre(trouvees[0]["id"], pourcentage)
    await _repondre(update, texte)


async def _repondre(update: Update, texte: str) -> None:
    await update.message.reply_text(texte, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(is_disabled=True))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _repondre(update, await texte_stats())


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _repondre(update, texte_journal())


def enregistrer_commandes(application: Application) -> None:
    moi = telegram_bot.SEULEMENT_MOI
    application.add_handler(CommandHandler("stats", cmd_stats, filters=moi))
    application.add_handler(CommandHandler("journal", cmd_journal, filters=moi))
    application.add_handler(CommandHandler("positions", cmd_positions, filters=moi))
    application.add_handler(CommandHandler("vendre", cmd_vendre, filters=moi))
    application.add_handler(CallbackQueryHandler(bouton_vendre, pattern=r"^sim_vendre:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(bouton_actualiser, pattern=r"^sim_maj:\d+$"))
    application.add_handler(CallbackQueryHandler(bouton_accepter, pattern="^accepter$"))
