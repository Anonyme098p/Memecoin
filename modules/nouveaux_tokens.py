"""
modules/nouveaux_tokens.py — Créations de tokens pump.fun en temps réel et suivi des devs.

  1. Écoute en continu le flux PumpPortal (websocket gratuit) des créations de tokens pump.fun,
     avec reconnexion automatique si la connexion coupe ou devient muette.
  2. Si le créateur est un dev suivi (/adddev) : alerte PRIORITAIRE immédiate, avec la note du
     token (modules/analyse.py) et le bilan du dev.
  3. Créateurs inconnus : aucune alerte, sauf si /scanall est activé. Dans ce cas, chaque token
     est revérifié 2 minutes après sa création (DexScreener) ; s'il décolle, il est analysé et
     alerté si sa note atteint le score minimum de /scanall.
  4. Au maximum 20 alertes de nouveaux tokens par heure (les devs suivis passent toujours).
  5. /devscore <adresse> : bilan des lancements d'un créateur (tokens créés, réussites, rugs)
     et verdict SUIVRE / À SURVEILLER / ÉVITER (seuils dans rules.json, section "createurs").

Sources du bilan d'un créateur :
  - Helius (scan côté serveur) : la liste des tokens qu'il a créés sur pump.fun ;
  - DexScreener : market cap actuelle ; GeckoTerminal : plus haut historique (prix journaliers) ;
  - RugCheck : token signalé "rugged".
"""

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
import websockets
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db
import telegram_bot
from modules import analyse, helius, wallets
from modules.supervision import marquer

log = logging.getLogger(__name__)

URL_DEXSCREENER_LOT = "https://api.dexscreener.com/tokens/v1/solana/{mints}"   # jusqu'à 30 tokens par appel
URL_GECKO_TOKEN = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}?include=top_pools"
URL_GECKO_OHLCV = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/day?limit=1000&currency=usd"
URL_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

SUIVRE, SURVEILLER, EVITER = "SUIVRE", "À SURVEILLER", "ÉVITER"
EMOJI_VERDICT = {SUIVRE: "🟢", SURVEILLER: "🟡", EVITER: "🔴"}
SUPPLY_PUMP_FUN = 1_000_000_000  # tous les tokens pump.fun ont 1 milliard de tokens
INTERVALLE_SCAN_S = 15          # /scanall : fréquence de revérification des tokens en attente
SILENCE_MAX_S = 120              # plusieurs tokens sont créés chaque minute : 2 min sans rien = connexion morte

# État du flux, affiché par /devs et /status
statut = {"connecte": False, "depuis": None, "creations_vues": 0, "derniere": None, "reconnexions": 0, "erreur": None}


# ---------------------------------------------------------------------------
# Petits outils : limiteurs de débit (APIs gratuites)
# ---------------------------------------------------------------------------

class Limiteur:
    """Laisse passer au plus une requête toutes les `intervalle` secondes."""

    def __init__(self, intervalle: float):
        self.intervalle, self.prochain, self.verrou = intervalle, 0.0, asyncio.Lock()

    async def attendre(self) -> None:
        async with self.verrou:
            attente = self.prochain - time.monotonic()
            if attente > 0:
                await asyncio.sleep(attente)
            self.prochain = time.monotonic() + self.intervalle


_gecko = Limiteur(2.1)      # GeckoTerminal gratuit : 30 requêtes / minute
_rugcheck = Limiteur(0.5)
_dexscreener = Limiteur(0.25)


async def _get_json(url: str, limiteur: Limiteur, delai: float = 10) -> object:
    await limiteur.attendre()
    async with httpx.AsyncClient(timeout=delai, headers={"Accept": "application/json"}) as client:
        reponse = await client.get(url)
    if reponse.status_code == 404:
        return None
    if reponse.status_code >= 400:
        raise RuntimeError(f"HTTP {reponse.status_code}")
    return reponse.json()


def _regles_createurs() -> dict:
    """Section "createurs" de rules.json, avec des valeurs par défaut si elle manque."""
    try:
        regles = analyse.charger_regles().get("createurs") or {}
    except RuntimeError:
        regles = {}
    verdict = regles.get("verdict") or {}
    return {
        "reussite_mc_usd": float(regles.get("reussite_mc_usd", 100_000)),
        "rug_mc_min": float((regles.get("rug") or {}).get("mc_min_usd", 20_000)),
        "rug_chute_pct": float((regles.get("rug") or {}).get("chute_pct", 90)),
        "min_tokens": int(verdict.get("min_tokens_pour_juger", 3)),
        "eviter_taux_rug": float(verdict.get("eviter_si_taux_rug_au_moins", 0.3)),
        "eviter_nb_rugs": int(verdict.get("eviter_si_nb_rugs_au_moins", 3)),
        "eviter_lances": int(verdict.get("eviter_si_lances_au_moins", 20)),
        "eviter_lances_taux": float(verdict.get("et_taux_reussite_sous", 0.05)),
        "suivre_taux": float(verdict.get("suivre_si_taux_reussite_au_moins", 0.2)),
        "suivre_taux_rug": float(verdict.get("et_taux_rug_sous", 0.1)),
    }


# ---------------------------------------------------------------------------
# Résultat d'un token : market cap actuelle, plus haute, rug ?
# ---------------------------------------------------------------------------

async def _dexscreener_lot(mints: list[str]) -> dict[str, dict]:
    """Paire la plus liquide de chaque token (30 tokens par appel)."""
    resultat: dict[str, dict] = {}
    for i in range(0, len(mints), 30):
        paires = await _get_json(URL_DEXSCREENER_LOT.format(mints=",".join(mints[i:i + 30])), _dexscreener) or []
        for p in paires if isinstance(paires, list) else []:
            mint = (p.get("baseToken") or {}).get("address")
            liquidite = float((p.get("liquidity") or {}).get("usd") or 0)
            if mint and liquidite >= float(((resultat.get(mint) or {}).get("liquidity") or {}).get("usd") or -1):
                resultat[mint] = p
    return resultat


async def _plus_haut_gecko(mint: str, pool: str | None, supply: float) -> float | None:
    """Plus haute market cap ($) d'après les prix journaliers de GeckoTerminal."""
    if not pool:
        token = await _get_json(URL_GECKO_TOKEN.format(mint=mint), _gecko)
        pools = (((token or {}).get("data") or {}).get("relationships") or {}).get("top_pools", {}).get("data") or []
        if not pools:
            return None
        pool = pools[0]["id"].split("_", 1)[-1]
    ohlcv = await _get_json(URL_GECKO_OHLCV.format(pool=pool), _gecko)
    bougies = (((ohlcv or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    if not bougies:
        return None
    return max(float(b[2]) for b in bougies) * supply  # b = [heure, ouverture, plus haut, plus bas, clôture, volume]


async def _rugged(mint: str) -> bool | None:
    rapport = await _get_json(URL_RUGCHECK.format(mint=mint), _rugcheck, delai=15)
    return bool(rapport.get("rugged")) if isinstance(rapport, dict) and "rugged" in rapport else None


async def mettre_a_jour_tokens(mints: list[str], regles: dict) -> dict[str, list[str]]:
    """
    Met à jour en base la market cap actuelle, la plus haute et le statut rug de ces tokens.
    Les tokens vérifiés récemment ne sont pas revérifiés. Renvoie les sources en panne.
    """
    maintenant = int(time.time())
    a_verifier = []
    for mint in mints:
        t = db.lire_token_cree(mint)
        recent = t and t["cree_le"] and maintenant - t["cree_le"] < 2 * 86400
        if t and t["maj_le"] and maintenant - t["maj_le"] < (1800 if recent else 6 * 3600):
            continue  # déjà vérifié il y a peu
        a_verifier.append(mint)
    pannes: dict[str, list[str]] = {}
    if not a_verifier:
        return pannes

    try:
        paires = await _dexscreener_lot(a_verifier)
    except Exception as erreur:
        paires = {}
        pannes.setdefault("DexScreener", []).append(type(erreur).__name__)

    async def un_token(mint: str) -> None:
        p = paires.get(mint) or {}
        mc = float(p.get("marketCap") or p.get("fdv") or 0) or None
        prix = float(p.get("priceUsd") or 0) or None
        supply = mc / prix if mc and prix else (SUPPLY_PUMP_FUN if mint.endswith("pump") else None)
        plus_haut = rug = None
        if supply:
            try:
                plus_haut = await _plus_haut_gecko(mint, p.get("pairAddress"), supply)
            except Exception:
                pannes.setdefault("GeckoTerminal", []).append(mint)
        try:
            rug = await _rugged(mint)
        except Exception:
            pannes.setdefault("RugCheck", []).append(mint)
        haut = max(x for x in (plus_haut, mc, 0) if x is not None)
        # Rug « maison » : il est monté haut, puis s'est effondré
        if not rug and mc is not None and haut >= regles["rug_mc_min"] and mc <= haut * (1 - regles["rug_chute_pct"] / 100):
            rug = True
        elif rug is None and (mc is not None or plus_haut is not None):
            rug = False
        db.maj_resultat_token(mint, mc, plus_haut, rug, maintenant)

    await asyncio.gather(*(un_token(m) for m in a_verifier))
    return pannes


# ---------------------------------------------------------------------------
# Bilan d'un créateur
# ---------------------------------------------------------------------------

@dataclass
class Bilan:
    adresse: str
    nb_lances: int = 0
    nb_analyses: int = 0
    nb_reussites: int = 0
    nb_rugs: int = 0
    verdict: str = SURVEILLER
    raisons: list[str] = field(default_factory=list)
    complet: bool = True                         # False si l'historique n'a pas été lu en entier
    remarques: list[str] = field(default_factory=list)
    meilleurs: list[tuple[str, str, float]] = field(default_factory=list)  # (symbole, mint, mc max)

    @property
    def nb_morts(self) -> int:
        return max(0, self.nb_analyses - self.nb_reussites - self.nb_rugs)


def _verdict(b: Bilan, r: dict) -> None:
    """Remplit b.verdict et b.raisons d'après les seuils de rules.json."""
    if b.nb_analyses < r["min_tokens"]:
        b.verdict = SURVEILLER
        b.raisons = [f"seulement {b.nb_analyses} token(s) vérifiable(s) : trop peu pour juger"]
        return
    taux_reussite, taux_rug = b.nb_reussites / b.nb_analyses, b.nb_rugs / b.nb_analyses
    if taux_rug >= r["eviter_taux_rug"] or b.nb_rugs >= r["eviter_nb_rugs"]:
        b.verdict, b.raisons = EVITER, [f"{b.nb_rugs} rug(s), soit {taux_rug * 100:.0f} % de ses tokens"]
    elif b.nb_lances >= r["eviter_lances"] and taux_reussite < r["eviter_lances_taux"]:
        b.verdict, b.raisons = EVITER, [f"lance en masse ({b.nb_lances} tokens) avec très peu de réussites"]
    elif taux_reussite >= r["suivre_taux"] and taux_rug < r["suivre_taux_rug"]:
        b.verdict, b.raisons = SUIVRE, [f"{taux_reussite * 100:.0f} % de ses tokens ont dépassé "
                                        f"{analyse.formater_usd(r['reussite_mc_usd'])}, peu de rugs"]
    else:
        b.verdict, b.raisons = SURVEILLER, [f"{taux_reussite * 100:.0f} % de réussites, {taux_rug * 100:.0f} % de rugs"]


async def _tokens_crees_par(createur: str, max_tokens: int, b: Bilan) -> list[str]:
    """
    Liste des mints créés par ce wallet (les plus récents d'abord) :
    scan Helius de son historique + tokens déjà vus par le bot.
    """
    connus = [t["mint"] for t in db.tokens_du_createur(createur)]
    try:
        creations, complet = await helius.creations_pump_fun(createur)
    except helius.ErreurHelius as erreur:
        b.remarques.append(f"historique Helius indisponible ({erreur}) : seuls les tokens vus par le bot sont comptés")
        b.complet = False
        b.nb_lances = len(connus)
        return connus[:max_tokens]

    b.complet = complet
    if not complet:
        b.remarques.append("historique très long : seule une partie a été parcourue")
    signatures_connues = len(creations)
    b.nb_lances = max(signatures_connues, len(connus))

    # Retrouver le mint de chaque création (une petite requête par token, pour les plus récents)
    a_lire = creations[:max_tokens]
    transactions = await asyncio.gather(*(helius.transaction(c["signature"]) for c in a_lire), return_exceptions=True)
    mints = list(connus)
    for creation, tx in zip(a_lire, transactions):
        mint = None if isinstance(tx, Exception) else helius.mint_de_la_creation(tx)
        if mint and mint not in mints:
            mints.append(mint)
            db.enregistrer_token_cree(mint, createur, None, None, int(creation.get("blockTime") or 0))
    # Les plus récents d'abord
    mints.sort(key=lambda m: -int((db.lire_token_cree(m) or {"cree_le": 0})["cree_le"] or 0))
    return mints[:max_tokens]


async def calculer_bilan(createur: str, max_tokens: int | None = None) -> Bilan:
    """Bilan complet d'un créateur (peut prendre une à deux minutes : APIs gratuites limitées)."""
    r = _regles_createurs()
    b = Bilan(createur)
    mints = await _tokens_crees_par(createur, max_tokens or config.DEV_MAX_TOKENS_ANALYSES, b)
    pannes = await mettre_a_jour_tokens(mints, r)
    for source, details in pannes.items():
        b.remarques.append(f"{source} indisponible pour {len(details)} token(s)")

    for mint in mints:
        t = db.lire_token_cree(mint)
        if not t or (t["mc_max"] is None and t["rug"] is None):
            continue
        b.nb_analyses += 1
        if t["rug"]:
            b.nb_rugs += 1
        elif (t["mc_max"] or 0) >= r["reussite_mc_usd"]:
            b.nb_reussites += 1
        if t["mc_max"]:
            b.meilleurs.append((t["symbole"] or mint[:6], mint, float(t["mc_max"])))
    b.meilleurs = sorted(b.meilleurs, key=lambda x: -x[2])[:3]
    if b.nb_analyses < len(mints):
        b.remarques.append(f"{len(mints) - b.nb_analyses} token(s) sans données (trop récents ou introuvables)")
    if b.nb_lances > len(mints):
        b.remarques.append(f"résultats vérifiés sur les {len(mints)} tokens les plus récents")
    _verdict(b, r)
    db.enregistrer_bilan_createur(createur, b.nb_lances, b.nb_analyses, b.nb_reussites, b.nb_rugs, b.verdict, rapport_dev(b))
    return b


def ligne_bilan(b) -> str:
    """Bilan en une ligne (pour les alertes). Accepte un Bilan ou une ligne de la table createurs."""
    if b is None:
        return "📜 Bilan du dev : pas encore calculé (tape /devscore pour le voir)"
    verdict = b["verdict"] if not isinstance(b, Bilan) else b.verdict
    lances = b["nb_lances"] if not isinstance(b, Bilan) else b.nb_lances
    reussites = b["nb_reussites"] if not isinstance(b, Bilan) else b.nb_reussites
    rugs = b["nb_rugs"] if not isinstance(b, Bilan) else b.nb_rugs
    seuil = analyse.formater_usd(_regles_createurs()["reussite_mc_usd"])
    return (f"📜 Bilan du dev : {lances} token(s) lancé(s) · {reussites} > {seuil} · {rugs} rug(s) — "
            f"{EMOJI_VERDICT.get(verdict, '')} {verdict}")


def rapport_dev(b: Bilan) -> str:
    """Rapport complet de /devscore."""
    seuil = analyse.formater_usd(_regles_createurs()["reussite_mc_usd"])
    lignes = [
        f"👤 <b>Bilan du créateur</b>\n<code>{b.adresse}</code>",
        "",
        f"{EMOJI_VERDICT[b.verdict]} <b>VERDICT : {b.verdict}</b>",
    ] + [f"   {html.escape(x, quote=False)}" for x in b.raisons] + [
        "",
        f"• Tokens lancés : <b>{b.nb_lances}</b>" + ("" if b.complet else " (au moins)"),
        f"• Résultat vérifié pour : {b.nb_analyses}",
        f"• Ont dépassé {seuil} de market cap : <b>{b.nb_reussites}</b>",
        f"• Rugs : <b>{b.nb_rugs}</b>",
        f"• Morts sans décoller : {b.nb_morts}",
    ]
    if b.meilleurs:
        lignes.append("• Meilleurs : " + ", ".join(
            f'<a href="https://dexscreener.com/solana/{m}">{html.escape(s)}</a> {analyse.formater_usd(mc)}'
            for s, m, mc in b.meilleurs))
    if b.remarques:
        lignes += ["", "<i>ℹ️ " + html.escape(" · ".join(b.remarques), quote=False) + "</i>"]
    lignes.append(f'🔗 <a href="https://pump.fun/profile/{b.adresse}">pump.fun</a> | '
                  f'<a href="https://gmgn.ai/sol/address/{b.adresse}">GMGN</a>')
    return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Limite d'alertes
# ---------------------------------------------------------------------------

def _alertes_derniere_heure() -> int:
    depuis = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    return db.compter_alertes("nouveau_token", depuis)


async def _limite_atteinte(application: Application) -> bool:
    """True si la limite d'alertes par heure est atteinte (prévient une seule fois par heure)."""
    if _alertes_derniere_heure() < config.NOUVEAUX_TOKENS_MAX_PAR_HEURE:
        return False
    heure = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    if db.lire_parametre("limite_nouveaux_tokens_prevenue") != heure:
        db.ecrire_parametre("limite_nouveaux_tokens_prevenue", heure)
        await telegram_bot.envoyer_message(
            application,
            f"⏸️ Limite de {config.NOUVEAUX_TOKENS_MAX_PAR_HEURE} alertes de nouveaux tokens par heure atteinte : "
            "les suivantes sont ignorées pendant un moment (les devs suivis passent toujours).",
        )
    return True


# ---------------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------------

def _message(en_tete: list[str], creation: dict, analyse_token, bilan_ligne: str) -> str:
    mint, symbole, nom = creation["mint"], creation.get("symbol") or "???", creation.get("name") or ""
    lignes = en_tete + [
        f"🪙 <b>{html.escape(symbole)}</b>" + (f" — {html.escape(nom)}" if nom else ""),
        f"📄 Contrat : <code>{mint}</code>",
    ]
    sol = float(creation.get("solAmount") or 0)
    if sol:
        part = float(creation.get("initialBuy") or 0) / SUPPLY_PUMP_FUN * 100
        lignes.append(f"💰 Achat initial du dev : {sol:.3f} SOL ({part:.1f} % du supply)")
    else:
        lignes.append("💰 Achat initial du dev : aucun")
    lignes += [bilan_ligne, ""]
    lignes.append(analyse.bloc_alerte(analyse_token) if analyse_token
                  else "🧪 <i>Note du token : inconnue (analyse impossible)</i>")
    lignes += ["", f'🔗 <a href="https://pump.fun/coin/{mint}">pump.fun</a> | '
                   f'<a href="{wallets.lien_dexscreener(mint)}">DexScreener</a> | '
                   f'<a href="{wallets.lien_gmgn(mint)}">GMGN</a>'
                   + (f' | <a href="{wallets.lien_transaction(creation["signature"])}">Transaction</a>'
                      if creation.get("signature") else "")]
    return "\n".join(lignes)


async def _analyser(mint: str):
    try:
        return await analyse.analyser_token(mint)
    except Exception as erreur:
        log.error("Analyse du token %s impossible : %s", mint, erreur)
        return None


async def _envoyer(application: Application, type_alerte: str, texte: str, creation: dict, a, createur: str) -> None:
    message = await telegram_bot.envoyer_message(application, texte, wallets.boutons(creation["mint"]))
    alerte_id = db.enregistrer_alerte(
        type_alerte=type_alerte, message=texte, token_adresse=creation["mint"], wallet_adresse=createur,
        signature=creation.get("signature"), prix_usd=a.donnees.prix_usd if a else None,
        telegram_message_id=message.message_id, note_token=a.note if a else None, danger=a.danger if a else None,
    )
    wallets._simuler(alerte_id, type_alerte, creation["mint"], creation.get("symbol"), createur, a)


async def alerte_dev_suivi(application: Application, creation: dict, dev) -> None:
    """Alerte PRIORITAIRE : un dev suivi vient de créer un token (ne passe pas par la limite par heure)."""
    if db.est_en_pause():
        log.info("Bot en pause : pas d'alerte pour le nouveau token de %s", dev["nom"])
        return
    a = await _analyser(creation["mint"])
    texte = _message([
        "⚡⚡ <b>PRIORITAIRE — nouveau token d'un dev suivi</b> ⚡⚡",
        f"👤 <b>{html.escape(dev['nom'] or dev['adresse'][:6])}</b> vient de créer un token",
    ], creation, a, ligne_bilan(db.lire_bilan_createur(dev["adresse"])))
    try:
        await _envoyer(application, "nouveau_token_dev", texte, creation, a, dev["adresse"])
    except Exception:
        log.exception("Impossible d'envoyer l'alerte de nouveau token")


# --- /scanall : créateurs inconnus -------------------------------------------

_en_attente: dict[str, tuple[float, dict]] = {}   # mint -> (heure de vérification, création)
_compteurs_scan = {"vus": 0, "verifies": 0, "retenus": 0, "envoyes": 0}
_verrou_scan = asyncio.Semaphore(2)


def scanall_reglage() -> tuple[bool, int]:
    return db.lire_parametre("scanall_actif", "0") == "1", int(db.lire_parametre("scanall_score", "60") or 60)


async def _traiter_candidat(application: Application, creation: dict, score_min: int) -> None:
    async with _verrou_scan:
        if await _limite_atteinte(application):
            return
        a = await _analyser(creation["mint"])
        if a is None or a.score_pour_filtre < score_min:
            return
        _compteurs_scan["retenus"] += 1
        createur = creation.get("traderPublicKey") or ""
        try:
            bilan = await asyncio.wait_for(calculer_bilan(createur, max_tokens=10), 60)
            ligne = ligne_bilan(bilan)
        except Exception as erreur:
            log.warning("Bilan du créateur %s impossible : %s", createur, erreur)
            ligne = ligne_bilan(db.lire_bilan_createur(createur))
        if db.est_en_pause() or await _limite_atteinte(application):
            return
        age = (time.time() - float(creation.get("_recu_le") or time.time())) / 60
        texte = _message([
            "🔭 <b>Nouveau token repéré (/scanall)</b>",
            f"👤 Créateur inconnu <code>{createur[:4]}…{createur[-4:]}</code> · créé il y a {age:.0f} min",
        ], creation, a, ligne)
        await _envoyer(application, "nouveau_token_scan", texte, creation, a, createur)
        _compteurs_scan["envoyes"] += 1


async def boucle_scan(application: Application) -> None:
    """Toutes les 15 s : revérifie (DexScreener) les tokens créés il y a SCAN_DELAI_S secondes."""
    while True:
        try:
            actif, score_min = scanall_reglage()
            if not actif:
                _en_attente.clear()
            maintenant = time.time()
            prets = [m for m, (heure, _) in _en_attente.items() if heure <= maintenant][:90]
            if prets:
                creations = {m: _en_attente.pop(m)[1] for m in prets}
                paires = await _dexscreener_lot(prets)
                for mint, creation in creations.items():
                    _compteurs_scan["verifies"] += 1
                    p = paires.get(mint) or {}
                    mc = float(p.get("marketCap") or p.get("fdv") or 0)
                    tx5 = sum(int((p.get("txns") or {}).get("m5", {}).get(k) or 0) for k in ("buys", "sells"))
                    # Pré-filtre gratuit : on n'analyse que les tokens qui décollent
                    if mc >= config.SCAN_MC_MIN_USD and tx5 >= config.SCAN_TX_5MIN_MIN:
                        asyncio.create_task(_traiter_candidat(application, creation, score_min))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erreur dans la boucle /scanall")
        await asyncio.sleep(INTERVALLE_SCAN_S)


# ---------------------------------------------------------------------------
# Flux temps réel PumpPortal (reconnexion automatique)
# ---------------------------------------------------------------------------

def traiter_creation(application: Application, creation: dict) -> None:
    """Appelé pour chaque token créé sur pump.fun."""
    statut["creations_vues"] += 1
    statut["derniere"] = time.time()
    createur, mint = creation.get("traderPublicKey"), creation.get("mint")
    if not createur or not mint:
        return
    dev = db.lire_dev(createur)
    if dev:
        log.info("Nouveau token %s du dev suivi %s", mint, dev["nom"])
        db.enregistrer_token_cree(mint, createur, creation.get("name"), creation.get("symbol"), int(time.time()))
        asyncio.create_task(alerte_dev_suivi(application, creation, dev))
        return
    actif, _ = scanall_reglage()
    if actif:
        _compteurs_scan["vus"] += 1
        creation["_recu_le"] = time.time()
        if len(_en_attente) > 5000:  # sécurité mémoire : on oublie les plus anciens
            for m in list(_en_attente)[:1000]:
                _en_attente.pop(m, None)
        _en_attente[mint] = (time.time() + config.SCAN_DELAI_S, creation)


async def ecouter(application: Application) -> None:
    """Reste connecté au flux PumpPortal ; se reconnecte tout seul (attente 1 s, 2 s, 4 s… jusqu'à 60 s)."""
    attente = 1
    while True:
        try:
            async with websockets.connect(config.PUMPPORTAL_WS_URL, open_timeout=15, ping_interval=20,
                                          ping_timeout=20, max_size=2 ** 20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                statut.update(connecte=True, depuis=time.time(), erreur=None)
                marquer("flux pump.fun", "actif", "connecté à PumpPortal")
                log.info("Connecté au flux PumpPortal des nouveaux tokens")
                attente = 1
                while True:
                    brut = await asyncio.wait_for(ws.recv(), timeout=SILENCE_MAX_S)
                    try:
                        message = json.loads(brut)
                    except ValueError:
                        continue
                    if message.get("txType") == "create":
                        traiter_creation(application, message)
        except asyncio.CancelledError:
            statut["connecte"] = False
            raise
        except Exception as erreur:
            statut.update(connecte=False, erreur=f"{type(erreur).__name__}")
            statut["reconnexions"] += 1
            marquer("flux pump.fun", "en attente", f"connexion coupée ({type(erreur).__name__}), reconnexion dans {attente} s")
            log.warning("Flux PumpPortal coupé (%s) : reconnexion dans %d s", type(erreur).__name__, attente)
        await asyncio.sleep(attente)
        attente = min(attente * 2, 60)


async def boucle_maj_devs(application: Application) -> None:
    """Recalcule une fois par jour le bilan de chaque dev suivi (pour que les alertes l'affichent)."""
    await asyncio.sleep(60)
    while True:
        try:
            for dev in db.lister_devs():
                bilan = db.lire_bilan_createur(dev["adresse"])
                if bilan and datetime.fromisoformat(bilan["date_maj"]) > datetime.now(timezone.utc) - timedelta(days=1):
                    continue
                await calculer_bilan(dev["adresse"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erreur pendant la mise à jour du bilan des devs")
        await asyncio.sleep(1800)


def texte_statut_flux() -> str:
    if statut["connecte"]:
        depuis = analyse.formater_age((time.time() - statut["depuis"]) / 60)
        return f"🟢 Flux pump.fun connecté depuis {depuis} — {statut['creations_vues']} créations vues"
    return f"🔴 Flux pump.fun déconnecté ({statut['erreur'] or 'démarrage'}) — reconnexion automatique en cours"


# ---------------------------------------------------------------------------
# Commandes Telegram
# ---------------------------------------------------------------------------

async def _avec_attente(application: Application, travail, texte_attente: str) -> None:
    """Lance `travail` (renvoie un texte HTML) ; « … en cours » d'abord si ça dure plus de 2 s."""
    tache = asyncio.create_task(travail)
    termine, _ = await asyncio.wait({tache}, timeout=2)
    attente = None if termine else await telegram_bot.envoyer_message(application, texte_attente)
    try:
        texte = await tache
    except Exception as erreur:
        log.exception("Erreur pendant %s", texte_attente)
        texte = f"❌ Impossible : {html.escape(str(erreur))}"
    if attente is not None:
        try:
            await attente.edit_text(texte, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        except Exception:
            pass
    await telegram_bot.envoyer_message(application, texte)


async def _rapport_dev(createur: str) -> str:
    return rapport_dev(await calculer_bilan(createur))


async def cmd_adddev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2 or not wallets.adresse_valide(context.args[0]):
        await update.message.reply_text("Utilisation : /adddev <adresse du créateur> <surnom>")
        return
    adresse, surnom = context.args[0], " ".join(context.args[1:])
    nouveau = db.ajouter_dev(adresse, surnom)
    await update.message.reply_text(
        f"{'✅ Dev ajouté' if nouveau else '✏️ Dev déjà suivi, surnom mis à jour'} : <b>{html.escape(surnom)}</b>\n"
        f"<code>{adresse}</code>\nTu recevras une alerte PRIORITAIRE dès qu'il crée un token.\n\n{texte_statut_flux()}",
        parse_mode=ParseMode.HTML,
    )
    context.application.create_task(
        _avec_attente(context.application, _rapport_dev(adresse), f"🔎 Bilan de {html.escape(surnom)} en cours…"),
        update=update)


async def cmd_removedev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Utilisation : /removedev <adresse>")
        return
    if db.supprimer_dev(context.args[0]):
        await update.message.reply_text(f"🗑️ Dev retiré : <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❓ Ce dev n'est pas suivi. Tape /devs pour voir la liste.")


async def cmd_devs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    devs = db.lister_devs()
    lignes = [texte_statut_flux(), ""]
    if not devs:
        lignes.append("Aucun dev suivi. Ajoute-en un avec /adddev &lt;adresse&gt; &lt;surnom&gt;")
    else:
        lignes.append(f"👤 <b>{len(devs)} dev(s) suivi(s) :</b>")
        for d in devs:
            lignes += [f"• <b>{html.escape(d['nom'] or '?')}</b> <code>{d['adresse']}</code>",
                       "  " + ligne_bilan(db.lire_bilan_createur(d["adresse"]))[2:]]
    actif, score = scanall_reglage()
    lignes += ["", f"🔭 /scanall : {'activé (score minimum ' + str(score) + ')' if actif else 'désactivé'}"]
    await update.message.reply_text("\n".join(lignes), parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))


async def cmd_devscore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not wallets.adresse_valide(context.args[0]):
        await update.message.reply_text("Utilisation : /devscore <adresse du créateur>")
        return
    context.application.create_task(
        _avec_attente(context.application, _rapport_dev(context.args[0]),
                      f"🔎 Bilan du créateur en cours… <code>{context.args[0]}</code>\n"
                      "<i>Lecture de ses lancements, ça peut prendre une à deux minutes.</i>"),
        update=update)


async def cmd_scanall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/scanall on <score> | /scanall off | /scanall (état)"""
    args = [a.lower() for a in context.args]
    if args and args[0] in ("off", "non", "stop"):
        db.ecrire_parametre("scanall_actif", "0")
        await update.message.reply_text("🔕 /scanall désactivé : seuls les devs suivis déclenchent des alertes.")
        return
    if args and args[0] in ("on", "oui"):
        score = args[1] if len(args) > 1 else db.lire_parametre("scanall_score", "60")
        if not str(score).isdigit() or not 0 <= int(score) <= 100:
            await update.message.reply_text("Utilisation : /scanall on <score minimum entre 0 et 100>")
            return
        db.ecrire_parametre("scanall_actif", "1")
        db.ecrire_parametre("scanall_score", str(int(score)))
        await update.message.reply_text(
            f"🔭 /scanall activé : alerte pour les tokens de créateurs inconnus dont la note atteint {int(score)}/100.\n"
            f"Chaque token est vérifié {config.SCAN_DELAI_S // 60} min après sa création ; seuls ceux qui décollent "
            f"(market cap ≥ {analyse.formater_usd(config.SCAN_MC_MIN_USD)}, ≥ {config.SCAN_TX_5MIN_MIN} transactions "
            f"en 5 min) sont analysés. Maximum {config.NOUVEAUX_TOKENS_MAX_PAR_HEURE} alertes par heure.")
        return
    actif, score = scanall_reglage()
    c = _compteurs_scan
    await update.message.reply_text(
        f"🔭 /scanall : {'activé, score minimum ' + str(score) if actif else 'désactivé'}\n"
        f"Depuis le démarrage : {c['vus']} tokens vus, {c['verifies']} vérifiés, {c['retenus']} retenus, "
        f"{c['envoyes']} alertes envoyées ({len(_en_attente)} en attente).\n"
        f"Alertes de nouveaux tokens la dernière heure : {_alertes_derniere_heure()}/{config.NOUVEAUX_TOKENS_MAX_PAR_HEURE}\n\n"
        "Utilisation : /scanall on <score> ou /scanall off")


def enregistrer_commandes(application: Application) -> None:
    moi = telegram_bot.SEULEMENT_MOI
    application.add_handler(CommandHandler("adddev", cmd_adddev, filters=moi))
    application.add_handler(CommandHandler("removedev", cmd_removedev, filters=moi))
    application.add_handler(CommandHandler("devs", cmd_devs, filters=moi))
    application.add_handler(CommandHandler("devscore", cmd_devscore, filters=moi))
    application.add_handler(CommandHandler("scanall", cmd_scanall, filters=moi))
