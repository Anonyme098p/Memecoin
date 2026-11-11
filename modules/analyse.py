"""
modules/analyse.py — Note de 0 à 100 d'un token Solana.

Sources (appelées en même temps) :
  - DexScreener (gratuit)  : market cap, liquidité, volumes 5 min / 1 h, achats / ventes, âge de la paire ;
  - RugCheck (gratuit)     : plus gros holders, créateur, liquidité bloquée, risques connus ;
  - Helius (RPC)           : mint / freeze authority (source de référence), et solution de secours
                             pour les holders et le créateur si RugCheck ne répond pas.
Bonus : détection de "bundle" (plusieurs gros holders financés par le même wallet).

Les règles de la note (poids, seuils, conditions DANGER, drapeaux rouges) sont dans rules.json.
Si une donnée manque (API en panne, token trop récent), le critère est noté « inconnu » :
il rapporte une partie fixe de ses points (part_si_inconnu) et c'est écrit dans le rapport.
"""

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

import config
from modules import helius

log = logging.getLogger(__name__)

URL_DEXSCREENER = "https://api.dexscreener.com/tokens/v1/solana/{mint}"
URL_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

DELAI_SOURCE = 6    # secondes maximum par source (DexScreener, RugCheck, Helius)
DELAI_BUNDLE = 5    # secondes maximum pour la détection de bundle


# ---------------------------------------------------------------------------
# Règles (rules.json)
# ---------------------------------------------------------------------------

_regles_valides: dict | None = None  # dernière version correcte du fichier


def charger_regles() -> dict:
    """
    Relit rules.json (on peut le modifier sans redémarrer le bot).
    Si le fichier est cassé (faute de frappe JSON), on garde la dernière version correcte.
    """
    global _regles_valides
    try:
        with open(config.RULES_PATH, encoding="utf-8") as f:
            regles = json.load(f)
        for cle in ("criteres", "danger", "drapeaux_rouges", "bundle", "niveaux"):
            if cle not in regles:
                raise ValueError(f"section « {cle} » manquante")
        _regles_valides = regles
    except (OSError, ValueError) as erreur:
        log.error("rules.json illisible (%s) : on garde la version précédente", erreur)
        if _regles_valides is None:
            raise RuntimeError(f"rules.json illisible : {erreur}") from None
    return _regles_valides


# ---------------------------------------------------------------------------
# Données collectées
# ---------------------------------------------------------------------------

@dataclass
class Donnees:
    mint: str
    nom: str | None = None
    symbole: str | None = None
    prix_usd: float | None = None
    dex: str | None = None
    market_cap: float | None = None
    liquidite_usd: float | None = None
    volume_5m: float | None = None
    volume_1h: float | None = None
    achats_5m: int | None = None
    ventes_5m: int | None = None
    achats_1h: int | None = None
    ventes_1h: int | None = None
    age_minutes: float | None = None
    mint_revoquee: bool | None = None
    freeze_revoquee: bool | None = None
    liquidite_bloquee_pct: float | None = None
    top10_pct: float | None = None
    createur: str | None = None
    createur_pct: float | None = None
    bundle: bool | None = None
    bundle_detail: str = ""
    risques_rugcheck: list[str] = field(default_factory=list)
    holders: list[tuple[str, float]] = field(default_factory=list)  # (wallet, % du supply)
    indisponible: dict[str, str] = field(default_factory=dict)       # source -> raison


async def _limite(coro, delai: float):
    """Lance une coroutine avec un délai maximum ; renvoie l'exception au lieu de la lever."""
    try:
        return await asyncio.wait_for(coro, delai)
    except asyncio.TimeoutError:
        return TimeoutError("délai dépassé")
    except Exception as erreur:  # API en panne, réponse bizarre...
        return erreur


def _raison(erreur: Exception) -> str:
    if isinstance(erreur, TimeoutError):
        return "délai dépassé"
    if isinstance(erreur, helius.ErreurHelius):
        return str(erreur)[:80]
    return f"{type(erreur).__name__}"


async def _get_json(url: str) -> object:
    async with httpx.AsyncClient(timeout=DELAI_SOURCE, headers={"Accept": "application/json"}) as client:
        reponse = await client.get(url)
    if reponse.status_code == 404:
        return None
    if reponse.status_code >= 400:
        raise RuntimeError(f"HTTP {reponse.status_code}")
    return reponse.json()


def _remplir_dexscreener(d: Donnees, paires: object) -> None:
    if not isinstance(paires, list) or not paires:
        d.indisponible["DexScreener"] = "token pas encore listé (trop récent ?)"
        return
    # La paire la plus liquide est la plus représentative
    p = max(paires, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    token = p.get("baseToken") or {}
    if token.get("address") != d.mint:
        token = p.get("quoteToken") or {}
    txns, volume = p.get("txns") or {}, p.get("volume") or {}

    def nombre(v):
        return float(v) if v not in (None, "") else None

    d.nom, d.symbole, d.dex = token.get("name"), token.get("symbol"), p.get("dexId")
    d.prix_usd = nombre(p.get("priceUsd"))
    d.market_cap = nombre(p.get("marketCap") or p.get("fdv"))
    d.liquidite_usd = nombre((p.get("liquidity") or {}).get("usd"))
    d.volume_5m, d.volume_1h = nombre(volume.get("m5")), nombre(volume.get("h1"))
    d.achats_5m, d.ventes_5m = (txns.get("m5") or {}).get("buys"), (txns.get("m5") or {}).get("sells")
    d.achats_1h, d.ventes_1h = (txns.get("h1") or {}).get("buys"), (txns.get("h1") or {}).get("sells")
    if p.get("pairCreatedAt"):
        d.age_minutes = max(0.0, (time.time() - p["pairCreatedAt"] / 1000) / 60)


def _remplir_rugcheck(d: Donnees, rapport: object, regles: dict) -> None:
    if not isinstance(rapport, dict):
        d.indisponible["RugCheck"] = "token inconnu de RugCheck"
        return
    connus = rapport.get("knownAccounts") or {}
    token = rapport.get("token") or {}

    # Autorités : RugCheck sert de secours si Helius n'a pas répondu
    if d.mint_revoquee is None and "mintAuthority" in rapport:
        d.mint_revoquee = rapport.get("mintAuthority") is None
    if d.freeze_revoquee is None and "freezeAuthority" in rapport:
        d.freeze_revoquee = rapport.get("freezeAuthority") is None

    # Liquidité : secours si DexScreener ne la donne pas (cas des tokens encore sur la courbe pump.fun)
    if d.liquidite_usd is None and rapport.get("totalMarketLiquidity"):
        d.liquidite_usd = float(rapport["totalMarketLiquidity"])

    # Liquidité bloquée ou brûlée : marché principal (le plus de liquidité)
    marches = [m for m in rapport.get("markets") or [] if m.get("lp")]
    if marches:
        principal = max(marches, key=lambda m: float(m["lp"].get("quoteUSD") or 0) + float(m["lp"].get("baseUSD") or 0))
        if principal["lp"].get("lpLockedPct") is not None:
            d.liquidite_bloquee_pct = float(principal["lp"]["lpLockedPct"])

    # Holders : on retire les pools, courbes de lancement et autres comptes connus (sauf le créateur)
    holders = []
    for h in rapport.get("topHolders") or []:
        proprietaire = h.get("owner") or h.get("address")
        type_connu = (connus.get(proprietaire) or connus.get(h.get("address")) or {}).get("type")
        if type_connu and type_connu != "CREATOR":
            continue
        holders.append((proprietaire, float(h.get("pct") or 0), bool(h.get("insider"))))
    if holders:
        d.holders = [(w, pct) for w, pct, _ in holders]
        d.top10_pct = sum(pct for _, pct, _ in holders[:10])
        insiders = sum(1 for _, _, insider in holders[: regles["bundle"]["holders_verifies"]] if insider)
        if insiders >= regles["bundle"]["min_wallets_meme_financeur"]:
            d.bundle = True
            d.bundle_detail = f"{insiders} gros holders liés entre eux (RugCheck)"

    # Créateur
    d.createur = rapport.get("creator") or d.createur
    if d.createur:
        dans_top = [pct for w, pct, _ in holders if w == d.createur]
        if dans_top:
            d.createur_pct = dans_top[0]
        elif rapport.get("creatorBalance") == 0:
            d.createur_pct = 0.0
        elif rapport.get("creatorBalance") and token.get("supply"):
            d.createur_pct = float(rapport["creatorBalance"]) / float(token["supply"]) * 100

    seuils = set(regles["drapeaux_rouges"].get("afficher_risques_rugcheck") or [])
    d.risques_rugcheck = [r.get("name") for r in rapport.get("risks") or [] if r.get("level") in seuils and r.get("name")]


async def _holders_par_helius(d: Donnees, supply: int) -> None:
    """Secours si RugCheck ne répond pas : 20 plus gros comptes, en retirant ceux tenus par un programme (pools)."""
    gros = await helius.plus_gros_comptes(d.mint)
    comptes_token = await helius.comptes([g["address"] for g in gros])
    proprietaires = [(((c or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner") for c in comptes_token]
    infos_proprietaires = await helius.comptes([p for p in proprietaires if p])
    programme = {p: (i or {}).get("owner") for p, i in zip([p for p in proprietaires if p], infos_proprietaires)}

    holders: dict[str, float] = {}
    for g, proprietaire in zip(gros, proprietaires):
        # Un vrai wallet appartient au System Program ; une pool ou une courbe appartient à un programme DEX
        if not proprietaire or programme.get(proprietaire) not in (SYSTEM_PROGRAM, None):
            continue
        holders[proprietaire] = holders.get(proprietaire, 0) + int(g.get("amount") or 0) / supply * 100
    d.holders = sorted(holders.items(), key=lambda x: -x[1])
    d.top10_pct = sum(pct for _, pct in d.holders[:10])


async def _createur_par_helius(d: Donnees, supply: int) -> None:
    """Secours : le créateur est celui qui a payé la toute première transaction du token."""
    if not d.createur:
        premiere = await helius.premiere_transaction(d.mint)
        tx = await helius.transaction(premiere["signature"]) if premiere else None
        cles = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        d.createur = (cles[0] if isinstance(cles[0], str) else cles[0].get("pubkey")) if cles else None
    if d.createur:
        d.createur_pct = await helius.solde_token(d.createur, d.mint) / supply * 100


# --- Bundle : plusieurs gros holders financés par le même wallet ---------------

_cache_financeurs: dict[str, tuple[str | None, int | None]] = {}


async def _financeur(wallet: str) -> tuple[str | None, int | None]:
    """Qui a envoyé ses premiers SOL à ce wallet, et quand ? (mémorisé : ça ne change jamais)"""
    if wallet in _cache_financeurs:
        return _cache_financeurs[wallet]
    premiere = await helius.premiere_transaction(wallet)
    resultat: tuple[str | None, int | None] = (None, None)
    if premiere:
        tx = await helius.transaction(premiere["signature"])
        cles = [c if isinstance(c, str) else c.get("pubkey") for c in
                (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
        if cles:
            meta = (tx or {}).get("meta") or {}
            if cles[0] != wallet:
                financeur = cles[0]  # celui qui a signé et payé la transaction
            else:  # sinon : le compte qui a perdu le plus de SOL
                variations = [(post - pre, cle) for cle, pre, post in
                              zip(cles, meta.get("preBalances") or [], meta.get("postBalances") or []) if cle != wallet]
                financeur = min(variations)[1] if variations else None
            resultat = (financeur, premiere.get("blockTime"))
    if len(_cache_financeurs) > 5000:
        _cache_financeurs.clear()
    _cache_financeurs[wallet] = resultat
    return resultat


async def _detecter_bundle(d: Donnees, regles: dict) -> None:
    reglage = regles["bundle"]
    wallets = [w for w, _ in d.holders[: reglage["holders_verifies"]] if w != d.createur]
    resultats = await asyncio.gather(*(_financeur(w) for w in wallets), return_exceptions=True)
    ignores = set(reglage.get("financeurs_ignores") or [])
    par_financeur: dict[str, list[int]] = {}
    verifies = 0
    for wallet, r in zip(wallets, resultats):
        if isinstance(r, Exception) or not r[0]:
            continue
        verifies += 1
        if r[0] not in ignores:
            par_financeur.setdefault(r[0], []).append(r[1] or 0)
    if not verifies:
        if d.bundle is None:
            d.indisponible["Bundle"] = "financement des holders non vérifiable"
        return
    fenetre = reglage["fenetre_heures"] * 3600
    for financeur, heures in par_financeur.items():
        if len(heures) >= reglage["min_wallets_meme_financeur"] and max(heures) - min(heures) <= fenetre:
            d.bundle = True
            d.bundle_detail = f"{len(heures)} gros holders financés par {financeur[:4]}…{financeur[-4:]}"
            return
    if d.bundle is None:
        d.bundle = False


async def collecter(mint: str, regles: dict) -> Donnees:
    """Récupère toutes les données du token. Ne lève jamais d'erreur : ce qui manque reste « inconnu »."""
    d = Donnees(mint)
    dex, rug, info_mint = await asyncio.gather(
        _limite(_get_json(URL_DEXSCREENER.format(mint=mint)), DELAI_SOURCE),
        _limite(_get_json(URL_RUGCHECK.format(mint=mint)), DELAI_SOURCE),
        _limite(helius.infos_mint(mint), DELAI_SOURCE),
    )

    if isinstance(dex, Exception):
        d.indisponible["DexScreener"] = _raison(dex)
    else:
        _remplir_dexscreener(d, dex)

    supply = None
    if isinstance(info_mint, Exception) or not info_mint:
        d.indisponible["Helius"] = _raison(info_mint) if isinstance(info_mint, Exception) else "mint introuvable"
    else:
        d.mint_revoquee = info_mint.get("mintAuthority") is None
        d.freeze_revoquee = info_mint.get("freezeAuthority") is None
        supply = int(info_mint.get("supply") or 0) or None

    if isinstance(rug, Exception):
        d.indisponible["RugCheck"] = _raison(rug)
    else:
        _remplir_rugcheck(d, rug, regles)

    # Secours Helius pour les holders et le créateur
    if supply and d.top10_pct is None:
        r = await _limite(_holders_par_helius(d, supply), DELAI_SOURCE)
        if isinstance(r, Exception):
            d.indisponible["Holders (Helius)"] = _raison(r)
    if supply and d.createur_pct is None:
        r = await _limite(_createur_par_helius(d, supply), DELAI_SOURCE)
        if isinstance(r, Exception):
            d.indisponible["Créateur (Helius)"] = _raison(r)

    # Âge : si DexScreener ne le donne pas, âge du token d'après sa première transaction
    if d.age_minutes is None:
        premiere = await _limite(helius.premiere_transaction(mint), DELAI_SOURCE)
        if isinstance(premiere, dict) and premiere.get("blockTime"):
            d.age_minutes = (time.time() - premiere["blockTime"]) / 60

    if d.holders:
        r = await _limite(_detecter_bundle(d, regles), DELAI_BUNDLE)
        if isinstance(r, Exception) and d.bundle is None:
            d.indisponible["Bundle"] = _raison(r)
    return d


# ---------------------------------------------------------------------------
# Note, DANGER et drapeaux rouges
# ---------------------------------------------------------------------------

NOMS_CRITERES = {
    "liquidite_usd": "Liquidité", "volume_1h_usd": "Volume 1 h", "part_achats_1h": "Part d'achats 1 h",
    "transactions_5min": "Transactions 5 min", "age_minutes": "Âge", "mint_revoquee": "Mint révoquée",
    "freeze_revoquee": "Freeze révoquée", "liquidite_bloquee_pct": "Liquidité bloquée",
    "top10_pct": "Top 10 holders", "createur_pct": "Part du créateur", "pas_de_bundle": "Pas de bundle",
}


def valeurs_criteres(d: Donnees) -> dict:
    """Valeur de chaque critère (None = inconnu)."""
    achats_ventes = (d.achats_1h or 0) + (d.ventes_1h or 0)
    return {
        "liquidite_usd": d.liquidite_usd,
        "volume_1h_usd": d.volume_1h,
        "part_achats_1h": d.achats_1h / achats_ventes if d.achats_1h is not None and achats_ventes else None,
        "transactions_5min": d.achats_5m + d.ventes_5m if d.achats_5m is not None and d.ventes_5m is not None else None,
        "age_minutes": d.age_minutes,
        "mint_revoquee": d.mint_revoquee,
        "freeze_revoquee": d.freeze_revoquee,
        "liquidite_bloquee_pct": d.liquidite_bloquee_pct,
        "top10_pct": d.top10_pct,
        "createur_pct": d.createur_pct,
        "pas_de_bundle": (not d.bundle) if d.bundle is not None else None,
    }


def _part_des_points(regle: dict, valeur) -> float:
    if regle["type"] == "oui":
        return 1.0 if valeur else 0.0
    for seuil, part in regle["paliers"]:
        if (regle["type"] == "min" and valeur >= seuil) or (regle["type"] == "max" and valeur <= seuil):
            return float(part)
    return 0.0


@dataclass
class AnalyseToken:
    donnees: Donnees
    note: int
    niveau: dict                      # {"nom", "emoji"}
    danger: bool
    raisons_danger: list[str]
    drapeaux: list[str]
    detail: list[tuple[str, float, float, bool]]   # (critère, points, max, inconnu)
    criteres_inconnus: list[str]
    part_si_inconnu: float = 0.5
    poids_inconnu: float = 0.0        # part (0 à 1) des points qui reposent sur des critères inconnus
    date: float = field(default_factory=time.time)

    @property
    def fiable(self) -> bool:
        """Note peu fiable si plus de la moitié des points viennent de critères inconnus."""
        return self.poids_inconnu <= 0.5

    @property
    def score_pour_filtre(self) -> int:
        """Score utilisé par /minscore : un token DANGER compte pour 0."""
        return 0 if self.danger else self.note


def noter(d: Donnees, regles: dict) -> AnalyseToken:
    valeurs = valeurs_criteres(d)
    part_inconnu = float(regles.get("part_si_inconnu", 0.5))
    total = maximum = maximum_inconnu = 0.0
    detail, inconnus = [], []
    for nom, regle in regles["criteres"].items():
        if nom not in valeurs:
            log.warning("rules.json : critère inconnu « %s » ignoré", nom)
            continue
        poids = float(regle.get("poids", 0))
        valeur = valeurs[nom]
        inconnu = valeur is None
        points = poids * (part_inconnu if inconnu else _part_des_points(regle, valeur))
        total += points
        maximum += poids
        detail.append((nom, points, poids, inconnu))
        if inconnu:
            inconnus.append(NOMS_CRITERES.get(nom, nom))
            maximum_inconnu += poids
    note = round(total / maximum * 100) if maximum else 0
    niveau = next((n for n in regles["niveaux"] if note >= n["min"]), {"nom": "?", "emoji": "⚪"})

    # DANGER, quelle que soit la note
    regle_danger, raisons = regles["danger"], []
    if regle_danger.get("mint_actif") and d.mint_revoquee is False:
        raisons.append("mint authority active (le créateur peut imprimer des tokens)")
    if regle_danger.get("freeze_actif") and d.freeze_revoquee is False:
        raisons.append("freeze authority active (tes tokens peuvent être bloqués)")
    if d.createur_pct is not None and d.createur_pct > regle_danger.get("createur_pct_max", 30):
        raisons.append(f"le créateur détient {d.createur_pct:.0f} % du supply")

    # Drapeaux rouges
    seuils, drapeaux = regles["drapeaux_rouges"], list(raisons)
    if d.liquidite_usd is not None and d.liquidite_usd < seuils.get("liquidite_usd_min", 0):
        drapeaux.append(f"liquidité faible ({formater_usd(d.liquidite_usd)})")
    if d.liquidite_bloquee_pct is not None and d.liquidite_bloquee_pct < seuils.get("liquidite_bloquee_pct_min", 0):
        drapeaux.append(f"liquidité non bloquée ({d.liquidite_bloquee_pct:.0f} % bloquée)")
    if d.top10_pct is not None and d.top10_pct > seuils.get("top10_pct_max", 100):
        drapeaux.append(f"top 10 holders : {d.top10_pct:.0f} % du supply")
    if (d.createur_pct is not None and seuils.get("createur_pct_max", 100) < d.createur_pct
            <= regle_danger.get("createur_pct_max", 30)):
        drapeaux.append(f"le créateur détient {d.createur_pct:.0f} %")
    part = valeurs["part_achats_1h"]
    if part is not None and part < seuils.get("part_achats_1h_min", 0):
        drapeaux.append(f"beaucoup plus de ventes que d'achats (1 h : {part * 100:.0f} % d'achats)")
    if d.age_minutes is not None and d.age_minutes < seuils.get("age_minutes_min", 0):
        drapeaux.append(f"paire créée il y a {d.age_minutes:.0f} min")
    if d.bundle:
        drapeaux.append(f"bundle : {d.bundle_detail}")
    drapeaux += [f"RugCheck : {r}" for r in d.risques_rugcheck]

    return AnalyseToken(d, note, niveau, bool(raisons), raisons, drapeaux, detail, inconnus, part_inconnu,
                        maximum_inconnu / maximum if maximum else 1.0)


# ---------------------------------------------------------------------------
# Point d'entrée (avec cache)
# ---------------------------------------------------------------------------

_cache: dict[str, AnalyseToken] = {}


async def analyser_token(mint: str) -> AnalyseToken:
    """
    Analyse complète d'un token (quelques secondes). Le résultat est gardé en mémoire
    ANALYSE_TOKEN_CACHE_S secondes : plusieurs alertes sur le même token ne refont pas tout.
    """
    en_cache = _cache.get(mint)
    if en_cache and time.time() - en_cache.date < config.ANALYSE_TOKEN_CACHE_S:
        return en_cache
    regles = charger_regles()
    resultat = noter(await collecter(mint, regles), regles)
    if len(_cache) > 500:
        _cache.clear()
    _cache[mint] = resultat
    return resultat


# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

def formater_usd(v: float | None) -> str:
    if v is None:
        return "inconnu"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def formater_age(minutes: float | None) -> str:
    if minutes is None:
        return "inconnu"
    if minutes < 60:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h"
    return f"{minutes / 1440:.0f} j"


def _pct(v: float | None) -> str:
    return "inconnu" if v is None else f"{v:.0f} %"


def _oui_non(v: bool | None, bien: str, mal: str) -> str:
    return "❔ inconnu" if v is None else (f"✅ {bien}" if v else f"❌ {mal}")


def _ligne_note(a: AnalyseToken) -> str:
    fiabilite = "" if a.fiable else f" <i>(peu fiable : {len(a.criteres_inconnus)} critères inconnus)</i>"
    if a.danger:
        return f"⛔ <b>DANGER</b> — note {a.note}/100{fiabilite}"
    return f"{a.niveau['emoji']} <b>Note token : {a.note}/100 — {a.niveau['nom']}</b>{fiabilite}"


def chiffres_cles(a: AnalyseToken) -> str:
    """Les 5 chiffres clés affichés dans chaque alerte."""
    d = a.donnees
    return (f"📊 MC {formater_usd(d.market_cap)} · Liq {formater_usd(d.liquidite_usd)} · "
            f"Vol 1h {formater_usd(d.volume_1h)} · Top10 {_pct(d.top10_pct)} · Âge {formater_age(d.age_minutes)}")


def bloc_alerte(a: AnalyseToken) -> str:
    """Bloc court ajouté à chaque alerte Telegram : note, 5 chiffres clés, drapeaux rouges."""
    lignes = [_ligne_note(a), chiffres_cles(a)]
    if a.drapeaux:
        lignes.append("🚩 " + html.escape(" · ".join(a.drapeaux[:4]), quote=False)
                      + (f" (+{len(a.drapeaux) - 4})" if len(a.drapeaux) > 4 else ""))
    if a.criteres_inconnus:
        lignes.append(f"<i>❔ Inconnu : {html.escape(', '.join(a.criteres_inconnus), quote=False)}</i>")
    return "\n".join(lignes)


def rapport(a: AnalyseToken) -> str:
    """Rapport complet (commande /check)."""
    d = a.donnees
    titre = html.escape(d.symbole or "???") + (f" — {html.escape(d.nom)}" if d.nom else "")
    lignes = [f"🧪 <b>Analyse du token {titre}</b>", f"<code>{d.mint}</code>", "", _ligne_note(a)]
    if a.danger:
        lignes += [f"   ⛔ {html.escape(r, quote=False)}" for r in a.raisons_danger]

    total_5m = (d.achats_5m or 0) + (d.ventes_5m or 0)
    lignes += [
        "",
        "<b>Marché</b>" + (f" ({html.escape(d.dex)})" if d.dex else ""),
        f"• Market cap : {formater_usd(d.market_cap)} · Liquidité : {formater_usd(d.liquidite_usd)}",
        f"• Volume : 5 min {formater_usd(d.volume_5m)} · 1 h {formater_usd(d.volume_1h)}",
        f"• Achats / ventes : 5 min {d.achats_5m if d.achats_5m is not None else 'inconnu'} / "
        f"{d.ventes_5m if d.ventes_5m is not None else 'inconnu'} · 1 h "
        f"{d.achats_1h if d.achats_1h is not None else 'inconnu'} / {d.ventes_1h if d.ventes_1h is not None else 'inconnu'}"
        + (f" ({total_5m} tx en 5 min)" if d.achats_5m is not None else ""),
        f"• Âge de la paire : {formater_age(d.age_minutes)}",
        "",
        "<b>Sécurité</b>",
        f"• Mint authority : {_oui_non(d.mint_revoquee, 'révoquée', 'ACTIVE')}",
        f"• Freeze authority : {_oui_non(d.freeze_revoquee, 'révoquée', 'ACTIVE')}",
        f"• Liquidité bloquée ou brûlée : {_pct(d.liquidite_bloquee_pct)}",
        f"• Top 10 holders : {_pct(d.top10_pct)} du supply (hors pools)",
        f"• Créateur : {_pct(d.createur_pct)}"
        + (f" (<code>{d.createur[:4]}…{d.createur[-4:]}</code>)" if d.createur else ""),
        "• Bundle (faux holders) : "
        + ("❔ inconnu" if d.bundle is None else (f"🚩 {html.escape(d.bundle_detail, quote=False)}" if d.bundle
                                                 else "✅ aucun financeur commun détecté")),
    ]
    if a.drapeaux:
        lignes += ["", "<b>Drapeaux rouges</b>"] + [f"🚩 {html.escape(x, quote=False)}" for x in a.drapeaux]

    detail = " · ".join(f"{NOMS_CRITERES.get(n, n)} {'?' if inc else ''}{p:.0f}/{m:.0f}" for n, p, m, inc in a.detail)
    lignes += ["", f"<i>Détail de la note : {detail}</i>"]
    if a.criteres_inconnus:
        lignes.append(f"<i>❔ Critères inconnus (comptés à {a.part_si_inconnu * 100:.0f} % de leurs points) : "
                      f"{html.escape(', '.join(a.criteres_inconnus), quote=False)}</i>")
    if d.indisponible:
        sources = " · ".join(f"{s} : {r}" for s, r in d.indisponible.items())
        lignes.append(f"<i>⚠️ Données indisponibles — {html.escape(sources, quote=False)}</i>")
    lignes.append(
        f'🔗 <a href="https://dexscreener.com/solana/{d.mint}">DexScreener</a> | '
        f'<a href="https://rugcheck.xyz/tokens/{d.mint}">RugCheck</a> | '
        f'<a href="https://gmgn.ai/sol/token/{d.mint}">GMGN</a>'
    )
    return "\n".join(lignes)
