"""
modules/analyse.py — Analyse de l'historique d'un wallet (30 derniers jours).

Étapes :
  1. On récupère les transactions du wallet via Helius (même format que les webhooks).
  2. On repère chaque achat (SOL -> token) et chaque vente (token -> SOL).
  3. On reconstitue les TRADES : un trade commence au premier achat d'un token et se
     termine quand le wallet a tout revendu.
  4. On calcule les statistiques : taux de réussite, gain / perte moyens, résultat total,
     durée de détention.
  5. On cherche les comportements à risque :
       - achats dans le tout premier bloc du token (sniper ou initié) ;
       - reventes juste après une vague d'achats d'autres wallets (revend sur ses copieurs) ;
       - wallet créé récemment.
  6. On donne une note de 0 à 100 et un verdict : SUIVRE / À SURVEILLER / ÉVITER.

Tous les seuils sont réglables ci-dessous.
"""

import asyncio
import html
import logging
import statistics
import time
from dataclasses import dataclass, field

import config
from modules import helius, transactions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

SEUIL_SCALPEUR_SECONDES = 120     # détention médiane < 2 min -> SCALPEUR
AGE_WALLET_RECENT_JOURS = 14      # wallet créé il y a moins de 14 jours -> risque
MIN_TRADES_FIABLE = 5             # en dessous, pas assez de données pour juger

MAX_TOKENS_VERIFIES_SNIPER = 40   # nombre de tokens vérifiés pour le "premier bloc"
MAX_TRADES_VERIFIES_COPIEURS = 20 # nombre de trades vérifiés pour les "copieurs"
DUREE_MAX_COPIEURS_SECONDES = 30 * 60  # on ne vérifie que les trades rapides (< 30 min)

NOTE_MIN_SUIVRE = 65              # note >= 65 -> SUIVRE
NOTE_MIN_SURVEILLER = 40          # note >= 40 -> À SURVEILLER, sinon ÉVITER

SUIVRE, SURVEILLER, EVITER = "SUIVRE", "À SURVEILLER", "ÉVITER"
EMOJI_VERDICT = {SUIVRE: "🟢", SURVEILLER: "🟡", EVITER: "🔴"}


# ---------------------------------------------------------------------------
# Reconstitution des trades
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    mint: str
    debut: int            # heure du premier achat (timestamp Unix)
    fin: int              # heure de la vente qui a tout soldé
    sol_investi: float
    sol_recupere: float
    slot_achat: int       # bloc du premier achat

    @property
    def resultat(self) -> float:
        """Gain (positif) ou perte (négatif) en SOL."""
        return self.sol_recupere - self.sol_investi

    @property
    def resultat_pct(self) -> float:
        return self.resultat / self.sol_investi * 100 if self.sol_investi else 0.0

    @property
    def duree(self) -> int:
        """Durée de détention en secondes."""
        return self.fin - self.debut


@dataclass
class _Position:
    """Un trade en cours de reconstitution."""
    mint: str
    debut: int
    slot_achat: int
    tokens: float = 0.0
    max_tokens: float = 0.0
    sol_investi: float = 0.0
    sol_recupere: float = 0.0


def reconstituer_trades(mouvements: list[transactions.Mouvement]) -> tuple[list[Trade], list[_Position]]:
    """
    Transforme la liste des achats/ventes en trades terminés.
    Renvoie (trades terminés, positions encore ouvertes).
    Les ventes de tokens achetés AVANT la période analysée sont ignorées (prix d'achat inconnu).
    """
    trades: list[Trade] = []
    positions: dict[str, _Position] = {}

    for m in sorted(mouvements, key=lambda m: (m.horodatage, m.slot)):
        position = positions.get(m.mint)

        if m.sens == "achat":
            if position is None:
                position = positions[m.mint] = _Position(m.mint, m.horodatage, m.slot)
            position.tokens += m.tokens
            position.max_tokens = max(position.max_tokens, position.tokens)
            position.sol_investi += m.sol
            continue

        # Vente
        if position is None or position.tokens <= 0:
            continue  # acheté avant la période analysée : on ne connaît pas le prix d'achat
        # S'il vend plus que ce qu'on l'a vu acheter (tokens reçus d'ailleurs), on ne compte que sa part
        part = min(1.0, position.tokens / m.tokens)
        position.sol_recupere += m.sol * part
        position.tokens = max(0.0, position.tokens - m.tokens)

        # Il reste moins de 1 % de la position : on considère le trade comme terminé
        if position.tokens <= position.max_tokens * 0.01:
            trades.append(Trade(m.mint, position.debut, m.horodatage,
                                position.sol_investi, position.sol_recupere, position.slot_achat))
            del positions[m.mint]

    return trades, list(positions.values())


# ---------------------------------------------------------------------------
# Comportements à risque
# ---------------------------------------------------------------------------

@dataclass
class Risque:
    verifie: bool   # False si Helius n'a pas pu répondre (on ne pénalise pas alors)
    detecte: bool
    detail: str


async def _verifier_sniper(trades: list[Trade], positions: list[_Position]) -> Risque:
    """Le wallet achète-t-il souvent dans le tout premier bloc du token (même bloc que sa création) ?"""
    achats: dict[str, int] = {}
    for t in sorted(trades + positions, key=lambda x: x.debut, reverse=True):  # les plus récents d'abord
        if t.mint not in achats and t.slot_achat:
            achats[t.mint] = t.slot_achat
    achats = dict(list(achats.items())[:MAX_TOKENS_VERIFIES_SNIPER])
    if not achats:
        return Risque(True, False, "aucun achat à vérifier")

    creations = await asyncio.gather(
        *(helius.premiere_transaction(mint) for mint in achats), return_exceptions=True
    )
    verifies = snipes = 0
    for (mint, slot_achat), creation in zip(achats.items(), creations):
        if isinstance(creation, Exception) or not creation:
            continue
        verifies += 1
        if slot_achat <= int(creation.get("slot") or 0):
            snipes += 1

    if not verifies:
        return Risque(False, False, "non vérifié (Helius indisponible)")
    detecte = snipes >= 2 and snipes / verifies >= 0.2
    return Risque(True, detecte, f"{snipes} token(s) sur {verifies} achetés dans le tout premier bloc")


def _acheteurs(transactions_brutes: list[dict], mint: str, exclure: str) -> list[tuple[int, str]]:
    """
    Dans des transactions Solana brutes, trouve les acheteurs du token :
    un wallet qui SIGNE la transaction et dont le solde de ce token AUGMENTE.
    (Les pools de liquidité ne signent jamais : on ne les compte donc pas.)
    Renvoie une liste de (heure, wallet).
    """
    resultat = []
    for tx in transactions_brutes:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        message = (tx.get("transaction") or {}).get("message") or {}
        cles = [c if isinstance(c, str) else c.get("pubkey") for c in message.get("accountKeys") or []]
        nb_signataires = (message.get("header") or {}).get("numRequiredSignatures") \
            or len((tx.get("transaction") or {}).get("signatures") or [])
        signataires = set(cles[:nb_signataires])

        def soldes(cle: str) -> dict[str, int]:
            total: dict[str, int] = {}
            for b in meta.get(cle) or []:
                if b.get("mint") == mint and b.get("owner"):
                    montant = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                    total[b["owner"]] = total.get(b["owner"], 0) + montant
            return total

        avant, apres = soldes("preTokenBalances"), soldes("postTokenBalances")
        for proprietaire, montant in apres.items():
            if montant > avant.get(proprietaire, 0) and proprietaire in signataires and proprietaire != exclure:
                resultat.append((int(tx.get("blockTime") or 0), proprietaire))
    return resultat


async def _verifier_copieurs(wallet: str, trades: list[Trade]) -> Risque:
    """
    Le wallet revend-il juste après que d'autres wallets ont acheté derrière lui ?
    Pour chaque trade rapide et gagnant, on compte les acheteurs du token :
      - dans la minute AVANT son achat (le rythme "normal") ;
      - dans les 2 minutes APRÈS son achat (là où arrivent ses copieurs) ;
      - dans la minute avant sa vente.
    Trade suspect si : au moins 3 acheteurs juste après son achat, à un rythme plus de 2 fois
    supérieur à la normale, et d'autres acheteurs encore dans la minute où il revend.
    """
    candidats = [t for t in trades if t.resultat > 0 and t.duree <= DUREE_MAX_COPIEURS_SECONDES]
    candidats = sorted(candidats, key=lambda t: t.debut)[-MAX_TRADES_VERIFIES_COPIEURS:]
    if not candidats:
        return Risque(True, False, "aucun trade rapide et gagnant à vérifier")

    reponses = await asyncio.gather(
        *(helius.transactions_periode(t.mint, t.debut - 60, t.fin) for t in candidats),
        return_exceptions=True,
    )
    verifies = suspects = 0
    for trade, reponse in zip(candidats, reponses):
        if isinstance(reponse, Exception):
            continue
        verifies += 1
        acheteurs = _acheteurs(reponse, trade.mint, wallet)
        fin_ruee = min(trade.debut + 120, trade.fin)
        avant = {w for t, w in acheteurs if t < trade.debut}
        ruee = {w for t, w in acheteurs if trade.debut <= t <= fin_ruee}
        juste_avant_vente = {w for t, w in acheteurs if trade.fin - 60 <= t <= trade.fin}
        rythme_normal = max(len(avant), 0.5)                         # acheteurs par minute
        rythme_ruee = len(ruee) / max((fin_ruee - trade.debut) / 60, 1)
        if len(ruee) >= 3 and rythme_ruee > 2 * rythme_normal and juste_avant_vente:
            suspects += 1

    if not verifies:
        return Risque(False, False, "non vérifié (Helius indisponible)")
    detecte = suspects >= 2 and suspects / verifies >= 0.3
    return Risque(True, detecte, f"{suspects} trade(s) sur {verifies} revendus juste après une vague d'achats")


async def _verifier_age(wallet: str) -> tuple[Risque, float | None]:
    """Âge du wallet, d'après sa toute première transaction. Renvoie (risque, âge en jours)."""
    try:
        premiere = await helius.premiere_transaction(wallet)
    except helius.ErreurHelius:
        return Risque(False, False, "âge non vérifié (Helius indisponible)"), None
    if not premiere or not premiere.get("blockTime"):
        return Risque(False, False, "âge inconnu"), None
    age = (time.time() - int(premiere["blockTime"])) / 86400
    texte = "moins d'1 jour" if age < 1 else f"{age:.0f} jour(s)"
    return Risque(True, age < AGE_WALLET_RECENT_JOURS, f"créé il y a {texte}"), age


# ---------------------------------------------------------------------------
# Résultat de l'analyse et note
# ---------------------------------------------------------------------------

@dataclass
class Analyse:
    adresse: str
    nb_transactions: int
    jours_couverts: float
    historique_tronque: bool
    trades: list[Trade]
    positions_ouvertes: int
    sniper: Risque
    copieurs: Risque
    wallet_recent: Risque
    age_jours: float | None
    detail_note: dict[str, int] = field(default_factory=dict)
    note: int = 0
    verdict: str = SURVEILLER

    # --- Statistiques calculées à partir des trades ---
    @property
    def gagnants(self) -> list[Trade]:
        return [t for t in self.trades if t.resultat > 0]

    @property
    def perdants(self) -> list[Trade]:
        return [t for t in self.trades if t.resultat <= 0]

    @property
    def taux_reussite(self) -> float | None:
        return len(self.gagnants) / len(self.trades) if self.trades else None

    @property
    def resultat_total(self) -> float:
        return sum(t.resultat for t in self.trades)

    @property
    def duree_mediane(self) -> float | None:
        return statistics.median(t.duree for t in self.trades) if self.trades else None

    @property
    def duree_moyenne(self) -> float | None:
        return statistics.mean(t.duree for t in self.trades) if self.trades else None

    @property
    def scalpeur(self) -> bool:
        return self.duree_mediane is not None and self.duree_mediane < SEUIL_SCALPEUR_SECONDES

    @property
    def donnees_insuffisantes(self) -> bool:
        return len(self.trades) < MIN_TRADES_FIABLE


def _moyenne(valeurs: list[float]) -> float | None:
    return statistics.mean(valeurs) if valeurs else None


def calculer_note(a: Analyse) -> None:
    """
    Note sur 100 = Réussite (30) + Rentabilité (30) + Résultat (10) + Activité (10) + Copiabilité (20)
                   - malus pour chaque comportement à risque.
    Remplit a.note, a.detail_note et a.verdict.
    """
    gains = sum(t.resultat for t in a.gagnants)
    pertes = -sum(t.resultat for t in a.perdants)

    # Réussite : 70 % de trades gagnants ou plus = note maximale
    reussite = min((a.taux_reussite or 0) / 0.7, 1) * 30

    # Rentabilité : "facteur de profit" = total des gains / total des pertes
    if pertes > 0:
        facteur = gains / pertes
    else:
        facteur = 3.0 if gains > 0 else 0.0
    if facteur >= 3:
        rentabilite = 30
    elif facteur >= 1:
        rentabilite = 10 + (facteur - 1) / 2 * 20
    else:
        rentabilite = facteur * 10

    gagnant = a.resultat_total > 0
    resultat = 10 if gagnant else 0
    # Activité et copiabilité ne comptent que pour un wallet qui gagne de l'argent :
    # copier facilement un wallet perdant n'a aucun intérêt.
    activite = min(len(a.trades) / 20, 1) * 10 if gagnant else 0  # 20 trades ou plus = maximum

    # Copiabilité : peut-on suivre ses achats à la main ? (détention médiane)
    mediane = a.duree_mediane
    if not gagnant or mediane is None or mediane < SEUIL_SCALPEUR_SECONDES:
        copiabilite = 0
    elif mediane >= 600:
        copiabilite = 20
    else:
        copiabilite = 5 + (mediane - SEUIL_SCALPEUR_SECONDES) / (600 - SEUIL_SCALPEUR_SECONDES) * 15

    malus = 0
    if a.sniper.detecte:
        malus += 20
    if a.copieurs.detecte:
        malus += 25
    if a.wallet_recent.detecte:
        malus += 25 if (a.age_jours or 0) < 3 else 15

    a.detail_note = {
        "Réussite": round(reussite), "Rentabilité": round(rentabilite), "Résultat": resultat,
        "Activité": round(activite), "Copiabilité": round(copiabilite), "Malus": -malus,
    }
    note = max(0, min(100, sum(a.detail_note.values())))
    if a.donnees_insuffisantes:
        note = min(note, 50)  # trop peu de trades : on reste prudent
    a.note = note

    # Verdict
    if note >= NOTE_MIN_SUIVRE:
        verdict = SUIVRE
    elif note >= NOTE_MIN_SURVEILLER:
        verdict = SURVEILLER
    else:
        verdict = EVITER
    risque_detecte = a.sniper.detecte or a.copieurs.detecte or a.wallet_recent.detecte
    if a.donnees_insuffisantes and not risque_detecte:
        verdict = SURVEILLER  # pas assez de trades : ni bon ni mauvais, à observer
    if verdict == SUIVRE and (a.scalpeur or a.resultat_total <= 0):
        verdict = SURVEILLER  # un SCALPEUR (ou un wallet perdant) n'est jamais à SUIVRE
    a.verdict = verdict


async def analyser_wallet(adresse: str) -> Analyse:
    """Analyse complète d'un wallet. Peut prendre de quelques secondes à une minute."""
    maintenant = int(time.time())
    depuis = maintenant - config.ANALYSE_JOURS * 86400
    historique, tronque = await helius.historique_wallet(adresse, depuis, config.ANALYSE_MAX_PAGES)
    log.info("Analyse %s : %d transactions récupérées", adresse, len(historique))

    mouvements = [m for tx in historique for m in transactions.detecter_mouvements(tx, adresse)]
    trades, positions = reconstituer_trades(mouvements)

    plus_ancienne = min((int(t.get("timestamp") or maintenant) for t in historique), default=maintenant)
    jours = config.ANALYSE_JOURS if not tronque else (maintenant - plus_ancienne) / 86400

    # Les trois vérifications partent en même temps
    sniper, copieurs, (recent, age) = await asyncio.gather(
        _verifier_sniper(trades, positions),
        _verifier_copieurs(adresse, trades),
        _verifier_age(adresse),
    )

    analyse = Analyse(adresse, len(historique), jours, tronque, trades, len(positions),
                      sniper, copieurs, recent, age)
    calculer_note(analyse)
    return analyse


# ---------------------------------------------------------------------------
# Rapport Telegram
# ---------------------------------------------------------------------------

def formater_duree(secondes: float | None) -> str:
    if secondes is None:
        return "?"
    s = int(secondes)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s" if s < 600 else f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d}"
    return f"{s // 86400} j {s % 86400 // 3600} h"


def _sol(valeur: float | None) -> str:
    return "?" if valeur is None else f"{valeur:+.3f} SOL"


def _pct(valeur: float | None) -> str:
    return "?" if valeur is None else f"{valeur:+.0f} %"


def rapport(a: Analyse, surnom: str | None = None) -> str:
    """Construit le message Telegram (HTML) du rapport d'analyse."""
    nom = f"<b>{html.escape(surnom)}</b>" if surnom else "<b>wallet</b>"
    lignes = [
        f"📊 Analyse du {nom}",
        f"<code>{a.adresse}</code>",
        f"Période : {a.jours_couverts:.0f} derniers jours — {a.nb_transactions} transactions",
    ]
    if a.historique_tronque:
        lignes.append(f"⚠️ Wallet très actif : seules les {a.nb_transactions} dernières transactions ont été lues.")

    lignes += ["", f"{EMOJI_VERDICT[a.verdict]} <b>VERDICT : {a.verdict}</b> — note <b>{a.note}/100</b>"]
    if a.scalpeur:
        lignes.append(
            f"⚡ <b>SCALPEUR</b> : détention médiane de {formater_duree(a.duree_mediane)}. "
            "Impossible à copier à la main, ses alertes arriveront trop tard."
        )
    if a.donnees_insuffisantes:
        lignes.append(f"ℹ️ Seulement {len(a.trades)} trade(s) terminé(s) : pas assez pour juger avec certitude.")

    lignes += [
        "",
        "<b>Trades (achat → revente complète)</b>",
        f"• Trades terminés : {len(a.trades)}"
        + (f" (+ {a.positions_ouvertes} position(s) encore ouverte(s))" if a.positions_ouvertes else ""),
    ]
    if a.trades:
        lignes += [
            f"• Taux de réussite : {a.taux_reussite * 100:.0f} % "
            f"({len(a.gagnants)} gagnant(s), {len(a.perdants)} perdant(s))",
            f"• Gain moyen : {_sol(_moyenne([t.resultat for t in a.gagnants]))} "
            f"({_pct(_moyenne([t.resultat_pct for t in a.gagnants]))})",
            f"• Perte moyenne : {_sol(_moyenne([t.resultat for t in a.perdants]))} "
            f"({_pct(_moyenne([t.resultat_pct for t in a.perdants]))})",
            f"• Résultat total : <b>{_sol(a.resultat_total)}</b>",
            f"• Détention : médiane {formater_duree(a.duree_mediane)} · moyenne {formater_duree(a.duree_moyenne)}",
        ]

    lignes += ["", "<b>Comportements à risque</b>"]
    for titre, risque in [("Sniper / initié", a.sniper), ("Revend sur ses copieurs", a.copieurs),
                          ("Wallet récent", a.wallet_recent)]:
        icone = "🚩" if risque.detecte else ("✅" if risque.verifie else "❔")
        lignes.append(f"{icone} {titre} : {html.escape(risque.detail, quote=False)}")

    detail = " · ".join(f"{cle} {valeur}" for cle, valeur in a.detail_note.items())
    lignes += [
        "",
        f"<i>Détail de la note : {detail}</i>",
        f'🔗 <a href="https://gmgn.ai/sol/address/{a.adresse}">GMGN</a> | '
        f'<a href="https://solscan.io/account/{a.adresse}">Solscan</a>',
    ]
    return "\n".join(lignes)
