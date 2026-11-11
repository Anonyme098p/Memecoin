"""
db.py — Base de données SQLite.

Le fichier de base est stocké à l'emplacement indiqué par DB_PATH.
Sur Railway, ce chemin doit être sur un volume persistant (ex : /data/bot.db),
sinon les données sont perdues à chaque redéploiement.

Tables :
  - wallets_suivis   : les wallets Solana que l'on surveille
  - devs_suivis      : les créateurs de tokens (devs) que l'on surveille
  - alertes_envoyees : l'historique des alertes envoyées sur Telegram
  - trades_simules   : les trades fictifs (paper trading) pour tester une stratégie
  - parametres       : petits réglages clé/valeur (ex : bot en pause ou non)
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import DB_PATH

# Script de création des tables. "IF NOT EXISTS" permet de le relancer sans risque.
SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets_suivis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    adresse     TEXT NOT NULL UNIQUE,   -- adresse publique du wallet Solana
    nom         TEXT,                   -- surnom pour s'y retrouver
    actif       INTEGER NOT NULL DEFAULT 1,  -- 1 = surveillé, 0 = ignoré
    date_ajout  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devs_suivis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    adresse     TEXT NOT NULL UNIQUE,   -- adresse du wallet du dev
    nom         TEXT,
    note        TEXT,                   -- remarque libre (ex : "dev sérieux", "rug x2")
    actif       INTEGER NOT NULL DEFAULT 1,
    date_ajout  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alertes_envoyees (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,       -- ex : "wallet", "dev", "nouveau_token"
    token_adresse  TEXT,                -- adresse (mint) du token concerné
    message        TEXT NOT NULL,       -- texte envoyé sur Telegram
    date_envoi     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades_simules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_adresse   TEXT NOT NULL,
    symbole         TEXT,
    montant_sol     REAL NOT NULL,      -- mise fictive en SOL
    prix_entree     REAL NOT NULL,
    prix_sortie     REAL,               -- vide tant que le trade est ouvert
    statut          TEXT NOT NULL DEFAULT 'ouvert',  -- 'ouvert' ou 'ferme'
    pnl_sol         REAL,               -- gain / perte en SOL à la fermeture
    date_ouverture  TEXT NOT NULL,
    date_fermeture  TEXT
);

CREATE TABLE IF NOT EXISTS parametres (
    cle     TEXT PRIMARY KEY,
    valeur  TEXT NOT NULL
);

-- Index pour éviter d'envoyer deux fois la même alerte rapidement
CREATE INDEX IF NOT EXISTS idx_alertes_token ON alertes_envoyees (token_adresse);
"""


def maintenant() -> str:
    """Date et heure actuelles en UTC, au format texte ISO (ex : 2026-01-31T12:00:00+00:00)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connexion():
    """
    Ouvre une connexion SQLite, valide les changements à la fin, puis la ferme.

    Utilisation :
        with connexion() as conn:
            conn.execute("SELECT ...")
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # permet d'accéder aux colonnes par leur nom
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Crée le dossier et les tables si besoin. À appeler une fois au démarrage."""
    dossier = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(dossier, exist_ok=True)
    with connexion() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Paramètres (clé / valeur)
# ---------------------------------------------------------------------------

def lire_parametre(cle: str, defaut: str | None = None) -> str | None:
    with connexion() as conn:
        ligne = conn.execute("SELECT valeur FROM parametres WHERE cle = ?", (cle,)).fetchone()
    return ligne["valeur"] if ligne else defaut


def ecrire_parametre(cle: str, valeur: str) -> None:
    with connexion() as conn:
        conn.execute(
            "INSERT INTO parametres (cle, valeur) VALUES (?, ?) "
            "ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
            (cle, valeur),
        )


def est_en_pause() -> bool:
    """True si les alertes sont en pause (réglage conservé même après un redémarrage)."""
    return lire_parametre("pause", "0") == "1"


def definir_pause(pause: bool) -> None:
    ecrire_parametre("pause", "1" if pause else "0")


# ---------------------------------------------------------------------------
# Wallets suivis
# ---------------------------------------------------------------------------

def ajouter_wallet(adresse: str, nom: str | None = None) -> None:
    """Ajoute un wallet à surveiller (ne fait rien s'il existe déjà)."""
    with connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO wallets_suivis (adresse, nom, date_ajout) VALUES (?, ?, ?)",
            (adresse, nom, maintenant()),
        )


def lister_wallets(actifs_seulement: bool = True) -> list[sqlite3.Row]:
    requete = "SELECT * FROM wallets_suivis"
    if actifs_seulement:
        requete += " WHERE actif = 1"
    with connexion() as conn:
        return conn.execute(requete + " ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Devs suivis
# ---------------------------------------------------------------------------

def ajouter_dev(adresse: str, nom: str | None = None, note: str | None = None) -> None:
    """Ajoute un dev à surveiller (ne fait rien s'il existe déjà)."""
    with connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO devs_suivis (adresse, nom, note, date_ajout) VALUES (?, ?, ?, ?)",
            (adresse, nom, note, maintenant()),
        )


def lister_devs(actifs_seulement: bool = True) -> list[sqlite3.Row]:
    requete = "SELECT * FROM devs_suivis"
    if actifs_seulement:
        requete += " WHERE actif = 1"
    with connexion() as conn:
        return conn.execute(requete + " ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Alertes envoyées
# ---------------------------------------------------------------------------

def enregistrer_alerte(type_alerte: str, message: str, token_adresse: str | None = None) -> None:
    with connexion() as conn:
        conn.execute(
            "INSERT INTO alertes_envoyees (type, token_adresse, message, date_envoi) VALUES (?, ?, ?, ?)",
            (type_alerte, token_adresse, message, maintenant()),
        )


def alerte_deja_envoyee(token_adresse: str, type_alerte: str) -> bool:
    """Permet d'éviter les doublons : a-t-on déjà alerté sur ce token pour ce type ?"""
    with connexion() as conn:
        ligne = conn.execute(
            "SELECT 1 FROM alertes_envoyees WHERE token_adresse = ? AND type = ? LIMIT 1",
            (token_adresse, type_alerte),
        ).fetchone()
    return ligne is not None


# ---------------------------------------------------------------------------
# Trades simulés (paper trading)
# ---------------------------------------------------------------------------

def ouvrir_trade(token_adresse: str, montant_sol: float, prix_entree: float, symbole: str | None = None) -> int:
    """Ouvre un trade fictif et renvoie son identifiant."""
    with connexion() as conn:
        curseur = conn.execute(
            "INSERT INTO trades_simules (token_adresse, symbole, montant_sol, prix_entree, date_ouverture) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_adresse, symbole, montant_sol, prix_entree, maintenant()),
        )
        return curseur.lastrowid


def fermer_trade(trade_id: int, prix_sortie: float) -> None:
    """Ferme un trade fictif et calcule le gain/perte en SOL."""
    with connexion() as conn:
        trade = conn.execute("SELECT * FROM trades_simules WHERE id = ?", (trade_id,)).fetchone()
        if trade is None or trade["statut"] != "ouvert":
            return
        # Exemple : mise 1 SOL, prix x2 -> pnl = +1 SOL
        pnl = trade["montant_sol"] * (prix_sortie / trade["prix_entree"] - 1)
        conn.execute(
            "UPDATE trades_simules SET prix_sortie = ?, statut = 'ferme', pnl_sol = ?, date_fermeture = ? "
            "WHERE id = ?",
            (prix_sortie, pnl, maintenant(), trade_id),
        )


# ---------------------------------------------------------------------------
# Statistiques (pour la commande /status)
# ---------------------------------------------------------------------------

def statistiques() -> dict:
    """Renvoie quelques compteurs utiles."""
    with connexion() as conn:
        def compter(requete: str) -> int:
            return conn.execute(requete).fetchone()[0]

        return {
            "wallets": compter("SELECT COUNT(*) FROM wallets_suivis WHERE actif = 1"),
            "devs": compter("SELECT COUNT(*) FROM devs_suivis WHERE actif = 1"),
            "alertes": compter("SELECT COUNT(*) FROM alertes_envoyees"),
            "trades_ouverts": compter("SELECT COUNT(*) FROM trades_simules WHERE statut = 'ouvert'"),
            "trades_fermes": compter("SELECT COUNT(*) FROM trades_simules WHERE statut = 'ferme'"),
            "pnl_total": conn.execute(
                "SELECT COALESCE(SUM(pnl_sol), 0) FROM trades_simules WHERE statut = 'ferme'"
            ).fetchone()[0],
        }
