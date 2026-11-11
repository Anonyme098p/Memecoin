"""
db.py — Base de données SQLite.

Le fichier de base est stocké à l'emplacement indiqué par DB_PATH.
Sur Railway, ce chemin doit être sur un volume persistant (ex : /data/bot.db),
sinon les données sont perdues à chaque redéploiement.

Tables :
  - wallets_suivis   : les wallets Solana que l'on surveille
  - devs_suivis      : les créateurs de tokens (devs) que l'on surveille
  - alertes_envoyees : l'historique des alertes envoyées sur Telegram (avec heure et prix)
  - achats_wallets   : chaque achat détecté sur un wallet suivi (sert au SIGNAL FORT)
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
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    type                 TEXT NOT NULL,   -- ex : "achat_wallet", "signal_fort", "dev"
    token_adresse        TEXT,            -- adresse (mint) du token concerné
    message              TEXT NOT NULL,   -- texte envoyé sur Telegram
    date_envoi           TEXT NOT NULL,   -- heure du signal (UTC)
    wallet_adresse       TEXT,            -- wallet qui a déclenché l'alerte
    signature            TEXT,            -- signature de la transaction Solana
    montant_sol          REAL,            -- montant de l'achat en SOL
    prix_sol             REAL,            -- prix d'1 token en SOL au moment du signal
    prix_usd             REAL,            -- prix d'1 token en dollars (si connu)
    telegram_message_id  INTEGER,         -- identifiant du message Telegram (pour le bouton Ignorer)
    statut               TEXT NOT NULL DEFAULT 'envoyee'  -- 'envoyee' ou 'ignoree'
);

-- Chaque achat détecté (même quand le bot est en pause), pour repérer les SIGNAUX FORTS
CREATE TABLE IF NOT EXISTS achats_wallets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signature       TEXT NOT NULL,
    wallet_adresse  TEXT NOT NULL,
    token_adresse   TEXT NOT NULL,
    montant_sol     REAL NOT NULL,
    tokens_recus    REAL NOT NULL,
    horodatage      INTEGER NOT NULL,  -- heure de la transaction (secondes depuis 1970, "timestamp Unix")
    UNIQUE (signature, wallet_adresse, token_adresse)  -- Helius peut envoyer 2 fois la même transaction
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

-- Index pour accélérer les recherches
CREATE INDEX IF NOT EXISTS idx_alertes_token ON alertes_envoyees (token_adresse);
CREATE INDEX IF NOT EXISTS idx_achats_token ON achats_wallets (token_adresse, horodatage);
"""

# Colonnes ajoutées après la première version : si ta base existe déjà sur Railway,
# init_db() les ajoute automatiquement (sans rien effacer).
COLONNES_AJOUTEES = {
    "alertes_envoyees": {
        "wallet_adresse": "TEXT",
        "signature": "TEXT",
        "montant_sol": "REAL",
        "prix_sol": "REAL",
        "prix_usd": "REAL",
        "telegram_message_id": "INTEGER",
        "statut": "TEXT NOT NULL DEFAULT 'envoyee'",
    },
}


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
        # Mise à jour d'une ancienne base : on ajoute les colonnes manquantes
        for table, colonnes in COLONNES_AJOUTEES.items():
            existantes = {ligne["name"] for ligne in conn.execute(f"PRAGMA table_info({table})")}
            for nom, definition in colonnes.items():
                if nom not in existantes:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {definition}")


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

def ajouter_wallet(adresse: str, nom: str | None = None) -> bool:
    """
    Ajoute un wallet à surveiller.
    S'il existe déjà, son surnom est mis à jour et il est réactivé.
    Renvoie True si c'est un nouveau wallet, False s'il existait déjà.
    """
    with connexion() as conn:
        existe = conn.execute("SELECT 1 FROM wallets_suivis WHERE adresse = ?", (adresse,)).fetchone()
        if existe:
            conn.execute("UPDATE wallets_suivis SET nom = ?, actif = 1 WHERE adresse = ?", (nom, adresse))
            return False
        conn.execute(
            "INSERT INTO wallets_suivis (adresse, nom, date_ajout) VALUES (?, ?, ?)",
            (adresse, nom, maintenant()),
        )
        return True


def supprimer_wallet(adresse: str) -> bool:
    """Retire un wallet de la surveillance. Renvoie False s'il n'était pas suivi."""
    with connexion() as conn:
        curseur = conn.execute("DELETE FROM wallets_suivis WHERE adresse = ?", (adresse,))
        return curseur.rowcount > 0


def surnoms_wallets() -> dict[str, str]:
    """Dictionnaire {adresse: surnom} des wallets suivis (actifs)."""
    return {w["adresse"]: (w["nom"] or w["adresse"][:6]) for w in lister_wallets()}


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

def enregistrer_alerte(
    type_alerte: str,
    message: str,
    token_adresse: str | None = None,
    wallet_adresse: str | None = None,
    signature: str | None = None,
    montant_sol: float | None = None,
    prix_sol: float | None = None,
    prix_usd: float | None = None,
    telegram_message_id: int | None = None,
) -> int:
    """Enregistre une alerte envoyée (avec l'heure et le prix au moment du signal). Renvoie son id."""
    with connexion() as conn:
        curseur = conn.execute(
            "INSERT INTO alertes_envoyees (type, token_adresse, message, date_envoi, wallet_adresse, "
            "signature, montant_sol, prix_sol, prix_usd, telegram_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (type_alerte, token_adresse, message, maintenant(), wallet_adresse,
             signature, montant_sol, prix_sol, prix_usd, telegram_message_id),
        )
        return curseur.lastrowid


def marquer_alerte_ignoree(telegram_message_id: int) -> None:
    """Appelé quand tu cliques sur le bouton « Ignorer » sous une alerte."""
    with connexion() as conn:
        conn.execute(
            "UPDATE alertes_envoyees SET statut = 'ignoree' WHERE telegram_message_id = ?",
            (telegram_message_id,),
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
# Achats détectés sur les wallets suivis
# ---------------------------------------------------------------------------

def enregistrer_achat(
    signature: str, wallet_adresse: str, token_adresse: str,
    montant_sol: float, tokens_recus: float, horodatage: int,
) -> bool:
    """Enregistre un achat. Renvoie False si on l'avait déjà (doublon envoyé par Helius)."""
    with connexion() as conn:
        curseur = conn.execute(
            "INSERT OR IGNORE INTO achats_wallets "
            "(signature, wallet_adresse, token_adresse, montant_sol, tokens_recus, horodatage) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (signature, wallet_adresse, token_adresse, montant_sol, tokens_recus, horodatage),
        )
        return curseur.rowcount > 0


def acheteurs_recents(token_adresse: str, horodatage: int, fenetre_secondes: int) -> list[sqlite3.Row]:
    """
    Liste des wallets suivis (différents) ayant acheté ce token à moins de
    `fenetre_secondes` de l'horodatage donné, avec le total acheté en SOL.
    """
    with connexion() as conn:
        return conn.execute(
            "SELECT a.wallet_adresse, w.nom, SUM(a.montant_sol) AS total_sol, MIN(a.horodatage) AS premier "
            "FROM achats_wallets a LEFT JOIN wallets_suivis w ON w.adresse = a.wallet_adresse "
            "WHERE a.token_adresse = ? AND a.horodatage BETWEEN ? AND ? "
            "GROUP BY a.wallet_adresse ORDER BY premier",
            (token_adresse, horodatage - fenetre_secondes, horodatage + fenetre_secondes),
        ).fetchall()


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
            "achats": compter("SELECT COUNT(*) FROM achats_wallets"),
            "trades_ouverts": compter("SELECT COUNT(*) FROM trades_simules WHERE statut = 'ouvert'"),
            "trades_fermes": compter("SELECT COUNT(*) FROM trades_simules WHERE statut = 'ferme'"),
            "pnl_total": conn.execute(
                "SELECT COALESCE(SUM(pnl_sol), 0) FROM trades_simules WHERE statut = 'ferme'"
            ).fetchone()[0],
        }
