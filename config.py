"""
config.py — Lecture de la configuration depuis les variables d'environnement.

Aucune clé secrète n'est écrite dans le code : tout vient de l'environnement
(onglet "Variables" sur Railway, ou fichier .env en local).
"""

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

# Secret optionnel pour protéger les webhooks (on vérifiera l'en-tête Authorization).
# Laisse vide tant que tu n'utilises pas encore les webhooks.
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "").strip()

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
