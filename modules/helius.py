"""
modules/helius.py — Petites fonctions pour parler à l'API Helius.

  - gestion des webhooks (lister, créer, modifier, supprimer)
  - récupération du nom / symbole d'un token (API "DAS" : getAsset)
  - historique d'un wallet (API "Enhanced Transactions", même format que les webhooks)
  - premières transactions d'une adresse et transactions sur une période
    (méthode getTransactionsForAddress, bien moins chère en crédits)

Documentation : https://www.helius.dev/docs/api-reference/webhooks

⚠️ La clé API est passée dans l'URL (?api-key=...). On ne logge donc JAMAIS
les URLs ni les exceptions httpx complètes (elles contiennent l'URL).
"""

import asyncio
import logging
import re

import httpx

import config

log = logging.getLogger(__name__)

URL_WEBHOOKS = "https://mainnet.helius-rpc.com/v0/webhooks"
URL_RPC = "https://mainnet.helius-rpc.com/"
URL_HISTORIQUE = "https://mainnet.helius-rpc.com/v0/addresses/{adresse}/transactions"
DELAI = 20  # secondes maximum par requête

# Pas plus de 5 requêtes Helius en même temps (le plan gratuit limite le débit)
_limiteur = asyncio.Semaphore(5)


class ErreurHelius(Exception):
    """Erreur renvoyée par l'API Helius (message sans la clé API)."""


async def _requete(
    methode: str, url: str, json: dict | None = None, params: dict | None = None
) -> httpx.Response:
    """
    Envoie une requête à Helius avec la clé API, et renvoie la réponse.
    En cas de "trop de requêtes" (429), on attend un peu et on réessaie (3 essais).
    """
    parametres = {"api-key": config.HELIUS_API_KEY, **(params or {})}
    for essai in range(3):
        try:
            async with _limiteur, httpx.AsyncClient(timeout=DELAI) as client:
                reponse = await client.request(methode, url, params=parametres, json=json)
        except httpx.HTTPError as erreur:
            # On n'affiche que le type d'erreur : le message complet contiendrait la clé API
            raise ErreurHelius(f"Helius injoignable ({type(erreur).__name__})") from None
        if reponse.status_code != 429:
            return reponse
        await asyncio.sleep(1 + essai * 2)
    return reponse


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


# ---------------------------------------------------------------------------
# Historique d'un wallet
# ---------------------------------------------------------------------------

# Quand Helius ne trouve rien dans sa fenêtre de recherche, il répond une erreur
# du type "... query the API again with the `before-signature` parameter set to XXXX"
_SIGNATURE_DANS_ERREUR = re.compile(r"set to\W*([1-9A-HJ-NP-Za-km-z]{60,100})")

# Les transactions récentes sont au format "version 1" : sans ce réglage, Helius ne les renvoie pas
VERSION_MAX = 1


async def historique_wallet(adresse: str, depuis: int, max_transactions: int) -> tuple[list[dict], bool, str]:
    """
    Récupère les transactions d'un wallet depuis le timestamp `depuis` (de la plus récente
    à la plus ancienne), page par page, jusqu'à `depuis` ou `max_transactions`.
    Renvoie (transactions au format enhanced, tronque, source) ; tronque=True si on s'est
    arrêté à max_transactions avant d'atteindre `depuis`.

    Source principale : getTransactionsForAddress, filtré côté Helius sur les transactions
    RÉUSSIES où le wallet a envoyé ou reçu un token. Indispensable pour les gros traders : leur
    adresse apparaît dans des milliers de transactions d'autres personnes (souvent échouées),
    qui noieraient leurs vrais trades. Coût : 10 crédits par 100 transactions.
    Si cette méthode n'est pas disponible, on se rabat sur l'API Enhanced Transactions.
    """
    try:
        return await _historique_filtre(adresse, depuis, max_transactions)
    except ErreurHelius as erreur:
        log.warning("getTransactionsForAddress indisponible (%s) : repli sur l'API Enhanced", erreur)
        return await _historique_enhanced(adresse, depuis, max_transactions)


async def _historique_filtre(adresse: str, depuis: int, max_transactions: int) -> tuple[list[dict], bool, str]:
    from modules.transactions import depuis_brute  # import ici pour éviter une boucle d'imports

    filtres: dict = {"status": "succeeded", "tokenAccounts": "balanceChanged", "tokenTransfer": {}}
    if depuis:
        filtres["blockTime"] = {"gte": depuis}
    options = {
        "transactionDetails": "full", "encoding": "json", "maxSupportedTransactionVersion": VERSION_MAX,
        "sortOrder": "desc", "limit": min(100, max_transactions), "filters": filtres,
    }
    resultat: list[dict] = []
    while len(resultat) < max_transactions:
        corps = {"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress", "params": [adresse, options]}
        donnees = await _appel_rpc(corps)
        page = donnees.get("data") or []
        resultat.extend(depuis_brute(t) for t in page)
        jeton = donnees.get("paginationToken")
        if not page or not jeton:
            return resultat[:max_transactions], False, _SOURCE_FILTREE
        options = {**options, "paginationToken": jeton}
    return resultat[:max_transactions], True, _SOURCE_FILTREE


_SOURCE_FILTREE = "getTransactionsForAddress (transactions réussies avec mouvement de token)"


async def _historique_enhanced(adresse: str, depuis: int, max_transactions: int) -> tuple[list[dict], bool, str]:
    """Repli : API Enhanced Transactions, page par page avec le paramètre before-signature."""
    source = "API Enhanced Transactions (toutes les transactions, échecs exclus)"
    resultat: list[dict] = []
    vues: set[str] = set()
    avant: str | None = None
    lues = 0
    while lues < max_transactions:
        params = {"limit": 100}
        if avant:
            params["before-signature"] = avant
        reponse = await _requete("GET", URL_HISTORIQUE.format(adresse=adresse), params=params)
        if reponse.status_code >= 400:
            suite = _SIGNATURE_DANS_ERREUR.search(reponse.text)
            if suite and suite.group(1) != avant:
                avant = suite.group(1)  # Helius nous dit où reprendre la recherche
                continue
            if reponse.status_code == 404:
                return resultat, False, source
            _verifier(reponse)

        page = reponse.json() or []
        nouvelles = [t for t in page if t.get("signature") not in vues]
        if not nouvelles:
            return resultat, False, source  # fin de l'historique (ou page déjà vue)
        for t in nouvelles:
            vues.add(t.get("signature"))
            lues += 1
            if int(t.get("timestamp") or 0) < depuis:
                return resultat, False, source  # on a atteint la limite des 30 jours
            if not t.get("transactionError"):
                resultat.append(t)
        avant = nouvelles[-1].get("signature")
    return resultat, True, source


async def _appel_rpc(corps: dict) -> dict:
    """Appel JSON-RPC à Helius ; renvoie "result" ou lève ErreurHelius."""
    reponse = await _requete("POST", URL_RPC, corps)
    _verifier(reponse)
    donnees = reponse.json()
    if "error" in donnees:
        raise ErreurHelius(f"{corps.get('method')} : {str(donnees['error'])[:200]}")
    return donnees.get("result") or {}


async def _transactions_pour_adresse(adresse: str, options: dict) -> list[dict]:
    """Appelle getTransactionsForAddress et renvoie la liste "data"."""
    corps = {"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress", "params": [adresse, options]}
    return (await _appel_rpc(corps)).get("data") or []


async def premiere_transaction(adresse: str) -> dict | None:
    """
    La toute première transaction d'une adresse ({"slot", "blockTime", "signature"}).
    - pour un token : c'est sa création (on en déduit le "premier bloc") ;
    - pour un wallet : c'est son premier usage (on en déduit son âge).
    """
    data = await _transactions_pour_adresse(
        adresse, {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 1}
    )
    return data[0] if data else None


async def transactions_periode(adresse: str, debut: int, fin: int, limite: int = 300) -> list[dict]:
    """Transactions complètes (format brut Solana) d'une adresse entre deux timestamps."""
    return await _transactions_pour_adresse(adresse, {
        "transactionDetails": "full",
        "encoding": "json",
        "maxSupportedTransactionVersion": VERSION_MAX,
        "sortOrder": "asc",
        "limit": limite,
        "filters": {"blockTime": {"gte": debut, "lte": fin}, "status": "succeeded"},
    })
