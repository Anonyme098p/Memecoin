"""
modules/tokens.py — Infos sur un token : nom, symbole et prix en dollars.

On interroge en même temps :
  - Helius (getAsset) : connaît presque tous les tokens, même tout juste créés ;
  - DexScreener      : donne le prix en dollars dès que le token a une paire de trading.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from modules import helius

log = logging.getLogger(__name__)

URL_DEXSCREENER = "https://api.dexscreener.com/tokens/v1/solana/{mint}"


@dataclass
class InfosToken:
    nom: str | None
    symbole: str | None
    prix_usd: float | None


async def _infos_dexscreener(mint: str) -> dict:
    """Renvoie le nom, le symbole et le prix de la paire la plus liquide (ou {} si inconnu)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            reponse = await client.get(URL_DEXSCREENER.format(mint=mint))
        paires = reponse.json() if reponse.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        log.warning("DexScreener injoignable pour %s", mint)
        return {}
    if not isinstance(paires, list) or not paires:
        return {}

    # On garde la paire avec le plus de liquidité, c'est la plus fiable pour le prix
    paire = max(paires, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    token = paire.get("baseToken") or {}
    if token.get("address") != mint:
        token = paire.get("quoteToken") or {}  # notre token est l'autre côté de la paire
        prix = None  # priceUsd concerne le baseToken : inutilisable ici
    else:
        prix = paire.get("priceUsd")
    return {
        "nom": token.get("name"),
        "symbole": token.get("symbol"),
        "prix_usd": float(prix) if prix else None,
    }


async def infos_token(mint: str) -> InfosToken:
    """Combine Helius et DexScreener (les deux appels partent en même temps)."""
    infos_helius, infos_dex = await asyncio.gather(helius.infos_token(mint), _infos_dexscreener(mint))
    return InfosToken(
        nom=infos_helius.get("nom") or infos_dex.get("nom"),
        symbole=infos_helius.get("symbole") or infos_dex.get("symbole"),
        # DexScreener d'abord : son prix est en temps réel
        prix_usd=infos_dex.get("prix_usd") or infos_helius.get("prix_usd"),
    )
