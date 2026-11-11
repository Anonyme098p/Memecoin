"""
config.py — Lecture de la configuration depuis les variables d'environnement.

Aucune clé secrète n'est écrite dans le code : tout vient de l'environnement
(onglet "Variables" sur Railway, ou fichier .env en local).
"""

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# En local, on charge le fichier .env s'il existe.
# Sur Railway, il n'y a pas de .env : les variables sont déjà dans l'environnement.
# load_dotenv n'écrase jamais une variable déjà définie.
load_dotenv()

# Problèmes de configuration trouvés au démarrage : {nom de la variable: explication}.
# Le bot ne s'arrête JAMAIS pour une variable manquante ou mal écrite : il prend la valeur
# par défaut (ou désactive le module concerné) et affiche ces problèmes dans /status et /health.
PROBLEMES: dict[str, str] = {}


def _brut(nom: str, defaut: str = "") -> str:
    """Valeur texte d'une variable, sans espaces ni guillemets collés par erreur (ex : "123")."""
    valeur = os.getenv(nom)
    if valeur is None:
        return defaut
    return valeur.strip().strip('"').strip("'").strip()


def _texte(nom: str, defaut: str = "") -> str:
    return _brut(nom, defaut) or defaut


def _obligatoire(nom: str) -> str:
    """Variable indispensable : si elle manque, on le note (le module concerné sera désactivé)."""
    valeur = _brut(nom)
    if not valeur:
        PROBLEMES[nom] = "manquante"
    return valeur


def _entier(nom: str, defaut: int) -> int:
    valeur = _brut(nom)
    if not valeur:
        return defaut
    try:
        return int(valeur)
    except ValueError:
        PROBLEMES[nom] = f"doit être un nombre entier : valeur par défaut {defaut} utilisée"
        return defaut


def _decimal(nom: str, defaut: float) -> float:
    valeur = _brut(nom).replace(",", ".")
    if not valeur:
        return defaut
    try:
        return float(valeur)
    except ValueError:
        PROBLEMES[nom] = f"doit être un nombre : valeur par défaut {defaut:g} utilisée"
        return defaut


# --- Telegram ---
# Le token donné par @BotFather
TELEGRAM_TOKEN: str = _obligatoire("TELEGRAM_TOKEN")

# Ton identifiant de chat personnel : le bot ne répondra qu'à lui
TELEGRAM_CHAT_ID: int = 0
if _obligatoire("TELEGRAM_CHAT_ID"):
    try:
        TELEGRAM_CHAT_ID = int(_brut("TELEGRAM_CHAT_ID"))
    except ValueError:
        PROBLEMES["TELEGRAM_CHAT_ID"] = "doit être un nombre (ton identifiant donné par @userinfobot)"

# Le bot Telegram ne peut fonctionner qu'avec ces deux variables correctes
TELEGRAM_OK: bool = bool(TELEGRAM_TOKEN) and bool(TELEGRAM_CHAT_ID)

# --- Solana / Helius ---
# Sans clé Helius : suivi des wallets, analyses d'historique et webhook désactivés.
HELIUS_API_KEY: str = _obligatoire("HELIUS_API_KEY")

# --- Base de données ---
# Sur Railway, on pointe vers le volume persistant, par exemple /data/bot.db
# En local, par défaut, le fichier est créé dans ./data/bot.db
DB_PATH: str = _texte("DB_PATH", "./data/bot.db")

# --- Serveur web ---
# Railway fournit automatiquement la variable PORT. En local, on utilise 8000.
PORT: int = _entier("PORT", 8000)

# Adresse publique du bot (le domaine Railway), ex : https://mon-bot.up.railway.app
# Helius enverra les transactions sur PUBLIC_URL + "/helius".
# Si PUBLIC_URL n'est pas définie, on utilise RAILWAY_PUBLIC_DOMAIN, que Railway
# remplit tout seul dès que tu as cliqué sur "Generate Domain".
def _url_publique() -> str:
    url = _brut("PUBLIC_URL") or _brut("RAILWAY_PUBLIC_DOMAIN")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


PUBLIC_URL: str = _url_publique()

# Secret que Helius recopie dans l'en-tête "Authorization" de chaque requête
# envoyée sur /helius : il prouve que la requête vient bien de Helius.
# Sans lui, le webhook n'est pas créé et toutes les requêtes sur /helius sont refusées.
# Pour en générer un : python -c "import secrets; print(secrets.token_urlsafe(32))"
HELIUS_WEBHOOK_SECRET: str = _brut("HELIUS_WEBHOOK_SECRET")

# Types de transactions que Helius doit nous envoyer (séparés par des virgules).
# Par défaut "ANY" (toutes) : les achats pump.fun ne sont PAS classés "SWAP" par Helius
# (PumpSwap = "BUY", courbe pump.fun = souvent "UNKNOWN"). Le bot trie lui-même les achats.
# Pour économiser des crédits Helius, tu peux mettre par exemple : SWAP,BUY
HELIUS_TRANSACTION_TYPES: list[str] = [
    t.strip().upper() for t in _texte("HELIUS_TRANSACTION_TYPES", "ANY").split(",") if t.strip()
] or ["ANY"]

# --- Signal fort ---
# Si au moins SIGNAL_FORT_MIN_WALLETS wallets suivis achètent le même token
# en moins de SIGNAL_FORT_MINUTES minutes, l'alerte est marquée SIGNAL FORT.
SIGNAL_FORT_MIN_WALLETS: int = _entier("SIGNAL_FORT_MIN_WALLETS", 2)
SIGNAL_FORT_MINUTES: int = _entier("SIGNAL_FORT_MINUTES", 10)

# --- Analyse d'historique des wallets ---
# Nombre de jours d'historique analysés
ANALYSE_JOURS: int = _entier("ANALYSE_JOURS", 30)
# Nombre maximum de transactions lues par analyse (10 crédits Helius par tranche de 100)
ANALYSE_MAX_TRANSACTIONS: int = _entier("ANALYSE_MAX_TRANSACTIONS", 5000)
# Analyse automatique de tous les wallets suivis chaque dimanche, à partir de cette heure
ANALYSE_HEBDO_HEURE: int = _entier("ANALYSE_HEBDO_HEURE", 10)
# Fuseau horaire utilisé pour "dimanche à 10 h"
FUSEAU_HORAIRE: str = _texte("FUSEAU_HORAIRE", "Europe/Paris")
try:
    ZoneInfo(FUSEAU_HORAIRE)
except (ZoneInfoNotFoundError, ValueError):
    PROBLEMES["FUSEAU_HORAIRE"] = f"fuseau « {FUSEAU_HORAIRE} » inconnu (ex : Europe/Paris) : Europe/Paris utilisé"
    FUSEAU_HORAIRE = "Europe/Paris"

# --- Note des tokens ---
# Fichier des règles de la note (poids, seuils, DANGER...). Par défaut : rules.json à côté du code.
RULES_PATH: str = _brut("RULES_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
# Durée (secondes) pendant laquelle l'analyse d'un token est réutilisée sans tout recalculer
ANALYSE_TOKEN_CACHE_S: int = _entier("ANALYSE_TOKEN_CACHE_S", 120)

# --- Nouveaux tokens pump.fun (modules/nouveaux_tokens.py) ---
# Flux temps réel des créations de tokens (gratuit, sans clé)
PUMPPORTAL_WS_URL: str = _texte("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data")
# Nombre maximum d'alertes de nouveaux tokens par heure (les devs suivis passent toujours)
NOUVEAUX_TOKENS_MAX_PAR_HEURE: int = _entier("NOUVEAUX_TOKENS_MAX_PAR_HEURE", 20)
# /scanall : délai avant de revérifier un token, et seuils pour qu'il soit analysé
SCAN_DELAI_S: int = _entier("SCAN_DELAI_S", 120)
SCAN_MC_MIN_USD: float = _decimal("SCAN_MC_MIN_USD", 10000)
SCAN_TX_5MIN_MIN: int = _entier("SCAN_TX_5MIN_MIN", 20)
# Nombre maximum de tokens d'un créateur dont on vérifie le résultat (/devscore)
DEV_MAX_TOKENS_ANALYSES: int = _entier("DEV_MAX_TOKENS_ANALYSES", 30)

# --- Veille Telegram avec un compte utilisateur (modules/veille.py) ---
# À obtenir sur https://my.telegram.org (voir le README). Facultatif : sans elles, la veille est désactivée.
TG_API_ID: int = _entier("TG_API_ID", 0)
TG_API_HASH: str = _brut("TG_API_HASH")
# Mot de passe de la page /tglogin (12 caractères minimum). Il sert aussi à chiffrer la session Telegram.
TGLOGIN_PASSWORD: str = _brut("TGLOGIN_PASSWORD")
# Alerte si un même token est cité dans au moins VEILLE_MIN_CANAUX canaux en VEILLE_FENETRE_MIN minutes
VEILLE_MIN_CANAUX: int = _entier("VEILLE_MIN_CANAUX", 2)
VEILLE_FENETRE_MIN: int = _entier("VEILLE_FENETRE_MIN", 30)
# Fréquence de lecture des canaux publics dont le compte n'est pas membre (secondes)
VEILLE_RELECTURE_S: int = _entier("VEILLE_RELECTURE_S", 60)

# --- IA (modules/ia.py) : Grok (xAI) ou Claude (Anthropic), selon la clé présente ---
XAI_API_KEY: str = _brut("XAI_API_KEY")
ANTHROPIC_API_KEY: str = _brut("ANTHROPIC_API_KEY")
# Pour choisir quand les deux clés sont présentes : "grok" ou "claude" (par défaut : grok)
IA_FOURNISSEUR: str = _texte("IA_FOURNISSEUR", "").lower()
# Modèle utilisé (par défaut : grok-4.7 pour Grok, claude-opus-5-5 pour Claude)
IA_MODELE: str = _brut("IA_MODELE")
# Nombre maximum d'appels à l'IA par jour (chaque alerte en coûte 3 : haussier, baissier, juge)
IA_MAX_PAR_JOUR: int = _entier("IA_MAX_PAR_JOUR", 200)

# --- Divers ---
# Intervalle (en secondes) entre deux tours de la boucle de surveillance
INTERVALLE_SURVEILLANCE: int = _entier("INTERVALLE_SURVEILLANCE", 60)

# Niveau de détail des logs : DEBUG, INFO, WARNING, ERROR
LOG_LEVEL: str = _texte("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    PROBLEMES["LOG_LEVEL"] = "doit valoir DEBUG, INFO, WARNING ou ERROR : INFO utilisé"
    LOG_LEVEL = "INFO"


def masquer(secret: str) -> str:
    """Affiche seulement le début et la fin d'un secret (pour /status ou les logs)."""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-4:]}"
