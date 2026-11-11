"""
modules/transactions.py — Interprétation d'une transaction : achat, vente ou ignorée ?

Principe : on ne regarde PAS le type annoncé (SWAP, BUY...), qui varie selon les DEX.
On calcule simplement, pour le wallet, la VARIATION DE SOLDE de la transaction :
  - pour chaque token (comptes de tokens appartenant au wallet) ;
  - en SOL (SOL natif + SOL "emballé" WSOL, qui est la même chose pour un DEX).
Puis :
  - token reçu  + SOL dépensé = ACHAT
  - token envoyé + SOL reçu   = VENTE
  - tout le reste est ignoré (avec la raison, visible via /debugtx).
Ça marche pareil pour pump.fun, PumpSwap, Raydium, Jupiter, Meteora, Orca...

Deux formats de transaction sont acceptés :
  - "enhanced" (webhooks et API Helius) : on lit accountData (variations de solde
    calculées par Helius) et, à défaut, tokenTransfers / nativeTransfers ;
  - "brut" Solana (getTransactionsForAddress) : converti en "enhanced" par depuis_brute().
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field

# Adresse du SOL "emballé" (wrapped SOL), utilisé par les DEX à la place du SOL natif
MINT_WSOL = "So11111111111111111111111111111111111111112"
MINT_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_USDT = "Es9vMFrzaCERmJfrF4H2FYD4KConky1jhcEdGnyR4sGq"

# Tokens qui ne sont PAS des memecoins : les échanger n'est ni un achat ni une vente
MINTS_DE_BASE = {MINT_WSOL, MINT_USDC, MINT_USDT}

LAMPORTS_PAR_SOL = 1_000_000_000  # 1 SOL = 1 milliard de lamports

# En dessous de ce montant, on considère qu'aucun SOL n'a été échangé
# (évite de prendre un simple frais de création de compte pour un achat)
SOL_MINIMUM = 0.0001


@dataclass
class Mouvement:
    sens: str            # "achat" ou "vente"
    wallet: str          # adresse du wallet
    mint: str            # adresse (contrat) du token
    sol: float           # SOL dépensés (achat) ou reçus (vente), frais réseau exclus
    tokens: float        # quantité de tokens reçus (achat) ou envoyés (vente)
    signature: str       # signature de la transaction
    horodatage: int      # heure de la transaction (timestamp Unix, en secondes)
    slot: int            # numéro du bloc Solana

    @property
    def prix_sol(self) -> float:
        """Prix d'1 token, en SOL."""
        return self.sol / self.tokens if self.tokens else 0.0


@dataclass
class Interpretation:
    """Ce que le bot a compris d'une transaction (affiché par /debugtx)."""
    signature: str
    horodatage: int
    sens: str | None                 # "achat", "vente" ou None (ignorée)
    raison: str                      # explication en français
    sol: float = 0.0                 # variation de SOL du wallet (frais réseau exclus)
    tokens: dict[str, float] = field(default_factory=dict)  # variation par token (hors SOL/WSOL)
    mouvement: Mouvement | None = None


# ---------------------------------------------------------------------------
# Calcul des variations de solde
# ---------------------------------------------------------------------------

def _variations_tokens(transaction: dict, wallet: str) -> dict[str, float]:
    """Variation de chaque token pour le wallet, en unités lisibles (décimales appliquées)."""
    variations: dict[str, float] = defaultdict(float)

    # 1. accountData.tokenBalanceChanges : variations de solde exactes des comptes du wallet
    trouve = False
    for compte in transaction.get("accountData") or []:
        for changement in compte.get("tokenBalanceChanges") or []:
            if changement.get("userAccount") != wallet:
                continue
            brut = changement.get("rawTokenAmount") or {}
            decimales = int(brut.get("decimals") or 0)
            variations[changement.get("mint")] += int(brut.get("tokenAmount") or 0) / 10 ** decimales
            trouve = True

    # 2. Sinon, tokenTransfers (déjà en unités lisibles)
    if not trouve:
        for t in transaction.get("tokenTransfers") or []:
            montant = float(t.get("tokenAmount") or 0)
            if t.get("toUserAccount") == wallet:
                variations[t.get("mint")] += montant
            if t.get("fromUserAccount") == wallet:
                variations[t.get("mint")] -= montant

    return {mint: v for mint, v in variations.items() if mint and abs(v) > 1e-12}


def _variation_sol_natif(transaction: dict, wallet: str) -> float:
    """Variation du SOL natif du wallet, frais réseau rendus (ils ne font pas partie du prix)."""
    # 1. accountData.nativeBalanceChange : la variation exacte du solde du wallet.
    #    Indispensable pour pump.fun : à la vente, le SOL est crédité directement sur le compte,
    #    sans "transfert" visible dans nativeTransfers.
    for compte in transaction.get("accountData") or []:
        if compte.get("account") == wallet:
            lamports = int(compte.get("nativeBalanceChange") or 0)
            if transaction.get("feePayer") == wallet:
                lamports += int(transaction.get("fee") or 0)
            return lamports / LAMPORTS_PAR_SOL

    # 2. Sinon, nativeTransfers. On ignore les envois vers ses propres comptes WSOL
    #    (c'est juste du SOL "emballé", déjà compté dans la variation de WSOL).
    comptes_wsol_du_wallet = {
        t.get("fromTokenAccount") for t in transaction.get("tokenTransfers") or []
        if t.get("fromUserAccount") == wallet and t.get("mint") == MINT_WSOL
    } | {
        t.get("toTokenAccount") for t in transaction.get("tokenTransfers") or []
        if t.get("toUserAccount") == wallet and t.get("mint") == MINT_WSOL
    }
    lamports = 0
    for transfert in transaction.get("nativeTransfers") or []:
        montant = int(transfert.get("amount") or 0)
        depuis, vers = transfert.get("fromUserAccount"), transfert.get("toUserAccount")
        if depuis == wallet and vers not in comptes_wsol_du_wallet:
            lamports -= montant
        if vers == wallet and depuis not in comptes_wsol_du_wallet:
            lamports += montant

    # 3. En dernier recours, le résumé du swap fait par Helius (events.swap)
    if lamports == 0:
        swap = (transaction.get("events") or {}).get("swap") or {}
        for cle, signe in (("nativeInput", -1), ("nativeOutput", 1)):
            natif = swap.get(cle) or {}
            if natif.get("account") == wallet and natif.get("amount"):
                lamports += signe * int(float(natif["amount"]))
    return lamports / LAMPORTS_PAR_SOL


# ---------------------------------------------------------------------------
# Interprétation
# ---------------------------------------------------------------------------

def interpreter(transaction: dict, wallet: str) -> Interpretation:
    """Achat, vente ou ignorée ? Avec la raison."""
    signature = transaction.get("signature") or ""
    horodatage = int(transaction.get("timestamp") or time.time())
    resultat = Interpretation(signature, horodatage, None, "")

    if transaction.get("transactionError"):
        resultat.raison = "transaction échouée"
        return resultat

    tokens = _variations_tokens(transaction, wallet)
    # SOL total = SOL natif + WSOL (le SOL "emballé" est du SOL pour un DEX)
    sol = _variation_sol_natif(transaction, wallet) + tokens.pop(MINT_WSOL, 0.0)
    resultat.sol, resultat.tokens = sol, tokens

    memecoins = {m: v for m, v in tokens.items() if m not in MINTS_DE_BASE}
    recus = {m: v for m, v in memecoins.items() if v > 0}
    envoyes = {m: -v for m, v in memecoins.items() if v < 0}

    if not memecoins:
        resultat.raison = ("aucun memecoin n'a bougé (seulement SOL / USDC / USDT)"
                           if tokens or abs(sol) >= SOL_MINIMUM else "aucun solde du wallet n'a changé")
        return resultat
    if recus and envoyes:
        resultat.raison = "échange d'un token contre un autre (pas de SOL en face)"
        return resultat

    if recus:
        if sol > -SOL_MINIMUM:
            resultat.raison = "token reçu sans dépenser de SOL (transfert, airdrop ou achat en USDC)"
            return resultat
        sens, quantites, montant_sol = "achat", recus, -sol
    else:
        if sol < SOL_MINIMUM:
            resultat.raison = "token envoyé sans recevoir de SOL (transfert ou vente en USDC)"
            return resultat
        sens, quantites, montant_sol = "vente", envoyes, sol

    mint = max(quantites, key=quantites.get)  # s'il y en a plusieurs, on prend le principal
    resultat.sens = sens
    resultat.raison = (f"token reçu et {montant_sol:.4f} SOL dépensés" if sens == "achat"
                       else f"token envoyé et {montant_sol:.4f} SOL reçus")
    resultat.mouvement = Mouvement(
        sens=sens, wallet=wallet, mint=mint, sol=montant_sol, tokens=quantites[mint],
        signature=signature, horodatage=horodatage, slot=int(transaction.get("slot") or 0),
    )
    return resultat


def detecter_mouvements(transaction: dict, wallet: str) -> list[Mouvement]:
    """Renvoie l'achat ou la vente de memecoin faits par ce wallet dans la transaction (0 ou 1 élément)."""
    mouvement = interpreter(transaction, wallet).mouvement
    return [mouvement] if mouvement else []


# ---------------------------------------------------------------------------
# Conversion d'une transaction Solana "brute" (getTransactionsForAddress)
# ---------------------------------------------------------------------------

def depuis_brute(brute: dict) -> dict:
    """
    Convertit une transaction brute (format getTransaction) en dictionnaire au format
    "enhanced" : on recalcule accountData à partir des soldes avant / après.
    """
    meta = brute.get("meta") or {}
    message = (brute.get("transaction") or {}).get("message") or {}
    adresses = meta.get("loadedAddresses") or {}
    cles = [c if isinstance(c, str) else c.get("pubkey") for c in message.get("accountKeys") or []]
    cles += (adresses.get("writable") or []) + (adresses.get("readonly") or [])

    avant, apres = meta.get("preBalances") or [], meta.get("postBalances") or []
    comptes = {
        cle: {"account": cle, "nativeBalanceChange": apres[i] - avant[i], "tokenBalanceChanges": []}
        for i, cle in enumerate(cles) if i < len(avant) and i < len(apres)
    }

    # Soldes de tokens avant / après, par compte de token
    soldes: dict[int, dict] = {}
    for cle_meta, signe in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(cle_meta) or []:
            info = soldes.setdefault(b["accountIndex"], {
                "mint": b.get("mint"), "owner": b.get("owner"), "delta": 0,
                "decimals": int((b.get("uiTokenAmount") or {}).get("decimals") or 0),
            })
            info["delta"] += signe * int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            info["owner"] = info["owner"] or b.get("owner")
    for index, info in soldes.items():
        if info["delta"] and index < len(cles) and cles[index] in comptes:
            comptes[cles[index]]["tokenBalanceChanges"].append({
                "userAccount": info["owner"], "tokenAccount": cles[index], "mint": info["mint"],
                "rawTokenAmount": {"tokenAmount": str(info["delta"]), "decimals": info["decimals"]},
            })

    signatures = (brute.get("transaction") or {}).get("signatures") or [""]
    return {
        "signature": signatures[0],
        "timestamp": brute.get("blockTime"),
        "slot": brute.get("slot"),
        "fee": meta.get("fee") or 0,
        "feePayer": cles[0] if cles else None,
        "transactionError": meta.get("err"),
        "accountData": list(comptes.values()),
        "tokenTransfers": [],
        "nativeTransfers": [],
    }
