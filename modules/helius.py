"""
modules/helius.py — Petites fonctions pour parler à l'API Helius.

  - gestion des webhooks (lister, créer, modifier, supprimer)
  - récupération du nom / symbole d'un token (API "DAS" : getAsset)

Documentation : https://www.helius.dev/docs/api-reference/webhooks

⚠️ La clé API est passée dans l'URL (?api-key=...). On ne logge donc JAMAIS
les URLs ni les exceptions httpx complètes (elles contiennent l'URL).
"""

import logging

import httpx

import config

log = logging.getLogger(__name__)

URL_WEBHOOKS = "https://mainnet.helius-rpc.com/v0/webhooks"
URL_RPC = "https://mainnet.helius-rpc.com/"
DELAI = 10  # secondes maximum par requête


class ErreurHelius(Exception):
    """Erreur renvoyée par l'API Helius (message sans la clé API)."""


async def _requete(methode: str, url: str, json: dict | None = None) -> httpx.Response:
    """Envoie une requête à Helius avec la clé API, et renvoie la réponse."""
    try:
        async with httpx.AsyncClient(timeout=DELAI) as client:
            return await client.request(methode, url, params={"api-key": config.HELIUS_API_KEY}, json=json)
    except httpx.HTTPError as erreur:
        # On n'affiche que le type d'erreur : le message complet contiendrait la clé API
        raise ErreurHelius(f"Helius injoignable ({type(erreur).__name__})") from None


def _verifier(reponse: httpx.Response) -> None:
    if reponse.status_code >= 400:
        raise ErreurHelius(f"Helius a répondu {reponse.status_code} : {reponse.text[:200]}")


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def _corps_webhook(url_reception: str, adresses: list[str]) -> dict:
    """Configuration d'un webhook "enhanced" (transactions déjà décodées par Helius)."""
    return {
        "webhookURL": url_reception,
        "webhookType": "enhanced",
        "accountAddresses": adresses,
        # Obligatoire (au moins un type). "ANY" par défaut : voir config.HELIUS_TRANSACTION_TYPES
        "transactionTypes": config.HELIUS_TRANSACTION_TYPES,
        # Helius recopiera cette valeur dans l'en-tête "Authorization" de chaque requête
        "authHeader": config.HELIUS_WEBHOOK_SECRET,
    }


async def lister_webhooks() -> list[dict]:
    reponse = await _requete("GET", URL_WEBHOOKS)
    _verifier(reponse)
    return reponse.json() or []


async def creer_webhook(url_reception: str, adresses: list[str]) -> dict:
    reponse = await _requete("POST", URL_WEBHOOKS, _corps_webhook(url_reception, adresses))
    _verifier(reponse)
    return reponse.json()


async def modifier_webhook(webhook_id: str, url_reception: str, adresses: list[str]) -> dict | None:
    """Met à jour un webhook. Renvoie None s'il n'existe plus chez Helius (404)."""
    reponse = await _requete("PUT", f"{URL_WEBHOOKS}/{webhook_id}", _corps_webhook(url_reception, adresses))
    if reponse.status_code == 404:
        return None
    _verifier(reponse)
    return reponse.json()


async def activer_webhook(webhook_id: str) -> None:
    """Réactive un webhook que Helius a désactivé (ex : trop d'erreurs de notre côté)."""
    reponse = await _requete("PATCH", f"{URL_WEBHOOKS}/{webhook_id}", {"active": True})
    _verifier(reponse)


async def supprimer_webhook(webhook_id: str) -> None:
    reponse = await _requete("DELETE", f"{URL_WEBHOOKS}/{webhook_id}")
    if reponse.status_code != 404:  # déjà supprimé : pas grave
        _verifier(reponse)


# ---------------------------------------------------------------------------
# Infos d'un token (nom, symbole)
# ---------------------------------------------------------------------------

async def infos_token(mint: str) -> dict:
    """
    Renvoie {"nom": ..., "symbole": ..., "prix_usd": ...} grâce à la méthode getAsset.
    Les valeurs peuvent être None si Helius ne les connaît pas.
    """
    corps = {"jsonrpc": "2.0", "id": "1", "method": "getAsset", "params": {"id": mint}}
    try:
        reponse = await _requete("POST", URL_RPC, corps)
        _verifier(reponse)
        resultat = reponse.json().get("result") or {}
    except (ErreurHelius, ValueError) as erreur:
        log.warning("getAsset impossible pour %s : %s", mint, erreur)
        return {"nom": None, "symbole": None, "prix_usd": None}

    metadata = (resultat.get("content") or {}).get("metadata") or {}
    token_info = resultat.get("token_info") or {}
    prix = (token_info.get("price_info") or {}).get("price_per_token")
    return {
        "nom": metadata.get("name") or None,
        "symbole": metadata.get("symbol") or token_info.get("symbol") or None,
        "prix_usd": float(prix) if prix else None,
    }
