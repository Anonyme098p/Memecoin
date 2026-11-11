"""
config.py — Lecture de la configuration depuis les variables d'environnement.

Aucune clé secrète n'est écrite dans le code : tout vient de l'environnement
(onglet "Variables" sur Railway, ou fichier .env en local).
"""

import hashlib
import hmac
import os
import sys

from dotenv import load_dotenv

# En local, on charge le fichier .env s'il existe.
# Sur Railway, il n'y a pas de .env : les variables sont déjà dans l'environnement.
# load_dotenv n'écrase jamais une variable déjà définie.
load_dotenv()


def _obligatoire(nom: str) -> str:
    """Lit une variable obligatoire. Arrête le programme avec un message clair si elle manque."""
    valeur = os.getenv(nom, "").strip()
    if not valeur:
        print(
            f"ERREUR : la variable d'environnement {nom} est manquante.\n"
            f"Ajoute-la dans l'onglet 'Variables' de Railway (ou dans ton fichier .env en local).",
            file=sys.stderr,
        )
        sys.exit(1)
    return valeur


# --- Telegram ---
# Le token donné par @BotFather
TELEGRAM_TOKEN: str = _obligatoire("TELEGRAM_TOKEN")

# Ton identifiant de chat personnel : le bot ne répondra qu'à lui
_chat_id_brut = _obligatoire("TELEGRAM_CHAT_ID")
try:
    TELEGRAM_CHAT_ID: int = int(_chat_id_brut)
except ValueError:
    print(
        f"ERREUR : TELEGRAM_CHAT_ID doit être un nombre (reçu : {_chat_id_brut!r}).",
        file=sys.stderr,
    )
    sys.exit(1)

# --- Solana / Helius ---
HELIUS_API_KEY: str = _obligatoire("HELIUS_API_KEY")

# --- Base de données ---
# Sur Railway, on pointe vers le volume persistant, par exemple /data/bot.db
# En local, par défaut, le fichier est créé dans ./data/bot.db
DB_PATH: str = os.getenv("DB_PATH", "./data/bot.db").strip()

# --- Serveur web ---
# Railway fournit automatiquement la variable PORT. En local, on utilise 8000.
PORT: int = int(os.getenv("PORT", "8000"))

# Adresse publique du bot (le domaine Railway), ex : https://mon-bot.up.railway.app
# Helius enverra les transactions sur PUBLIC_URL + "/helius".
# Si PUBLIC_URL n'est pas définie, on utilise RAILWAY_PUBLIC_DOMAIN, que Railway
# remplit tout seul dès que tu as cliqué sur "Generate Domain".
def _url_publique() -> str:
    url = os.getenv("PUBLIC_URL", "").strip() or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


PUBLIC_URL: str = _url_publique()

# Secret que Helius met dans l'en-tête "Authorization" de chaque requête :
# il prouve que la requête vient bien de Helius.
# Si tu ne le définis pas, il est calculé à partir de ta clé Helius
# (il reste donc secret et identique d'un redémarrage à l'autre).
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "").strip() or hmac.new(
    HELIUS_API_KEY.encode(), b"webhook-helius", hashlib.sha256
).hexdigest()

# --- Signal fort ---
# Si au moins SIGNAL_FORT_MIN_WALLETS wallets suivis achètent le même token
# en moins de SIGNAL_FORT_MINUTES minutes, l'alerte est marquée SIGNAL FORT.
SIGNAL_FORT_MIN_WALLETS: int = int(os.getenv("SIGNAL_FORT_MIN_WALLETS", "2"))
SIGNAL_FORT_MINUTES: int = int(os.getenv("SIGNAL_FORT_MINUTES", "10"))

# --- Divers ---
# Intervalle (en secondes) entre deux tours de la boucle de surveillance
INTERVALLE_SURVEILLANCE: int = int(os.getenv("INTERVALLE_SURVEILLANCE", "60"))

# Niveau de détail des logs : DEBUG, INFO, WARNING, ERROR
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


def masquer(secret: str) -> str:
    """Affiche seulement le début et la fin d'un secret (pour /status ou les logs)."""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}…{secret[-4:]}"
