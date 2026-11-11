"""
modules/transactions.py — Lecture des transactions "enhanced" envoyées par Helius.

Une transaction enhanced contient déjà la liste des transferts de tokens
(tokenTransfers) et de SOL (nativeTransfers). On en déduit, pour un wallet donné :
  - un ACHAT : le wallet a dépensé des SOL et reçu un memecoin ;
  - une VENTE : le wallet a envoyé un memecoin et reçu des SOL.

Utilisé par les alertes en direct (modules/wallets.py) et par l'analyse
d'historique (modules/analyse.py).
"""

import time
from collections import defaultdict
from dataclasses import dataclass

# Adresse du SOL "emballé" (wrapped SOL), utilisé par les DEX à la place du SOL natif
MINT_WSOL = "So11111111111111111111111111111111111111112"

# Tokens qui ne sont PAS des memecoins : les échanger n'est ni un achat ni une vente
MINTS_DE_BASE = {
    MINT_WSOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KConky1jhcEdGnyR4sGq",  # USDT
}

LAMPORTS_PAR_SOL = 1_000_000_000  # 1 SOL = 1 milliard de lamports


@dataclass
class Mouvement:
    sens: str            # "achat" ou "vente"
    wallet: str          # adresse du wallet
    mint: str            # adresse (contrat) du token
    sol: float           # SOL dépensés (achat) ou reçus (vente)
    tokens: float        # quantité de tokens reçus (achat) ou envoyés (vente)
    signature: str       # signature de la transaction
    horodatage: int      # heure de la transaction (timestamp Unix, en secondes)
    slot: int            # numéro du bloc Solana

    @property
    def prix_sol(self) -> float:
        """Prix d'1 token, en SOL."""
        return self.sol / self.tokens if self.tokens else 0.0


def _soldes_tokens(wallet: str, transaction: dict) -> dict[str, float]:
    """Solde net de chaque token pour ce wallet (+ reçu, - envoyé), en unités lisibles."""
    nets: dict[str, float] = defaultdict(float)
    for t in transaction.get("tokenTransfers") or []:
        montant = float(t.get("tokenAmount") or 0)  # déjà avec les décimales appliquées
        if t.get("toUserAccount") == wallet:
            nets[t.get("mint")] += montant
        if t.get("fromUserAccount") == wallet:
            nets[t.get("mint")] -= montant
    return nets


def _sol_net_natif(wallet: str, transaction: dict) -> float:
    """SOL natif reçu moins SOL natif envoyé par le wallet (en SOL, peut être négatif)."""
    lamports = 0
    for transfert in transaction.get("nativeTransfers") or []:
        montant = int(transfert.get("amount") or 0)
        if transfert.get("toUserAccount") == wallet:
            lamports += montant
        if transfert.get("fromUserAccount") == wallet:
            lamports -= montant
    return lamports / LAMPORTS_PAR_SOL


def _sol_echange(wallet: str, transaction: dict, wsol_net: float, sens: str) -> float:
    """
    Combien de SOL le wallet a dépensé (sens="achat") ou reçu (sens="vente") ?
    On essaie plusieurs sources, de la plus précise à la moins précise.
    """
    signe = -1 if sens == "achat" else 1

    # 1. Helius a reconnu un swap et donne directement le SOL entré / sorti
    swap = (transaction.get("events") or {}).get("swap") or {}
    cle = "nativeInput" if sens == "achat" else "nativeOutput"
    natif = swap.get(cle) or {}
    if natif.get("account") == wallet and natif.get("amount"):
        return float(natif["amount"]) / LAMPORTS_PAR_SOL

    # 2. Du SOL "emballé" (WSOL) a bougé : cas des swaps Jupiter / Raydium
    if wsol_net * signe > 0:
        return abs(wsol_net)

    # 3. Du SOL natif a bougé : cas de pump.fun par exemple
    #    (peut inclure de petits frais de plateforme : c'est une estimation)
    return max(_sol_net_natif(wallet, transaction) * signe, 0.0)


def detecter_mouvements(transaction: dict, wallet: str) -> list[Mouvement]:
    """Renvoie l'achat ou la vente de memecoin faits par ce wallet dans la transaction (0 ou 1 élément)."""
    nets = _soldes_tokens(wallet, transaction)
    recus = {m: q for m, q in nets.items() if m and q > 0 and m not in MINTS_DE_BASE}
    envoyes = {m: -q for m, q in nets.items() if m and q < 0 and m not in MINTS_DE_BASE}

    # Échange d'un memecoin contre un autre (sans SOL) : ni achat ni vente au sens du bot
    if recus and envoyes:
        return []
    if recus:
        sens, quantites = "achat", recus
    elif envoyes:
        sens, quantites = "vente", envoyes
    else:
        return []

    sol = _sol_echange(wallet, transaction, nets.get(MINT_WSOL, 0.0), sens)
    if sol <= 0:
        return []  # token reçu/envoyé sans SOL en face (cadeau, transfert, USDC...) : on ignore

    mint = max(quantites, key=quantites.get)  # s'il y en a plusieurs, on prend le principal
    return [Mouvement(
        sens=sens,
        wallet=wallet,
        mint=mint,
        sol=sol,
        tokens=quantites[mint],
        signature=transaction.get("signature") or "",
        horodatage=int(transaction.get("timestamp") or time.time()),
        slot=int(transaction.get("slot") or 0),
    )]
