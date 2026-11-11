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
  - analyses_wallets : historique des analyses de wallets (note, verdict, rapport)
  - tokens_crees     : tokens créés par les devs suivis ou analysés (market cap max, rug)
  - createurs        : bilan de chaque créateur (tokens lancés, réussites, rugs, verdict)
  - canaux_suivis    : canaux Telegram surveillés par la veille (modules/veille.py)
  - mentions_veille  : adresses de tokens repérées dans ces canaux
  - trades_simules   : les trades simulés (portefeuilles « Bot seul » et « Mes choix », modules/simulation.py)
  - suivi_alertes    : prix de chaque alerte au signal, à 5 min, 1 h et 24 h
  - decisions        : chaque clic Accepter / Ignorer, avec l'heure
  - appels_ia        : chaque appel à l'IA (pour la limite par jour, modules/ia.py)
  - propositions_ia  : changements proposés par la revue IA (en attente / acceptés / refusés)
  - versions_regles  : historique des versions des règles (pour /rules et /rollback)
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

-- Tokens pump.fun créés par les devs suivis ou analysés (pas TOUS les tokens : il y en a des dizaines de milliers par jour)
CREATE TABLE IF NOT EXISTS tokens_crees (
    mint        TEXT PRIMARY KEY,
    createur    TEXT NOT NULL,
    nom         TEXT,
    symbole     TEXT,
    cree_le     INTEGER,            -- timestamp Unix de la création
    mc_actuel   REAL,               -- market cap actuelle en $ (dernière vérification)
    mc_max      REAL,               -- plus haute market cap connue en $
    rug         INTEGER,            -- 1 = rug, 0 = non, vide = inconnu
    maj_le      INTEGER             -- dernière vérification (timestamp Unix)
);

-- Bilan de chaque créateur (recalculé par /devscore, /adddev et chaque jour pour les devs suivis)
CREATE TABLE IF NOT EXISTS createurs (
    adresse        TEXT PRIMARY KEY,
    nb_lances      INTEGER NOT NULL,   -- tokens créés
    nb_analyses    INTEGER NOT NULL,   -- tokens dont on a pu vérifier le résultat
    nb_reussites   INTEGER NOT NULL,   -- tokens ayant dépassé le seuil de market cap (100k $)
    nb_rugs        INTEGER NOT NULL,
    verdict        TEXT NOT NULL,      -- SUIVRE / À SURVEILLER / ÉVITER
    date_maj       TEXT NOT NULL,
    rapport        TEXT
);

-- Veille Telegram : canaux surveillés avec le compte utilisateur (lecture seule)
CREATE TABLE IF NOT EXISTS canaux_suivis (
    id                  INTEGER PRIMARY KEY,   -- identifiant Telegram du canal (ex : -1001234567890)
    titre               TEXT,
    nom_utilisateur     TEXT,                  -- @nom pour un canal public, vide sinon
    lien                TEXT,                  -- lien donné à /addchannel
    membre              INTEGER NOT NULL,      -- 1 = le compte en est membre (lecture en direct)
    dernier_message_id  INTEGER NOT NULL DEFAULT 0,
    date_ajout          TEXT NOT NULL
);

-- Adresses de tokens repérées dans les canaux suivis (pour le croisement entre canaux)
CREATE TABLE IF NOT EXISTS mentions_veille (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mint           TEXT NOT NULL,
    canal_id       INTEGER NOT NULL,
    canal_titre    TEXT,
    message_id     INTEGER NOT NULL,
    lien_message   TEXT,
    horodatage     INTEGER NOT NULL,       -- heure du message (timestamp Unix)
    UNIQUE (mint, canal_id, message_id)
);

-- Prix de chaque alerte au moment du signal puis à 5 min, 1 h et 24 h (même les alertes ignorées)
CREATE TABLE IF NOT EXISTS suivi_alertes (
    alerte_id       INTEGER PRIMARY KEY,   -- id dans alertes_envoyees
    mint            TEXT NOT NULL,
    type_alerte     TEXT NOT NULL,         -- achat_wallet, signal_fort, nouveau_token_dev, nouveau_token_scan, veille
    wallet_adresse  TEXT,
    cree_le         INTEGER NOT NULL,      -- heure de l'alerte (timestamp Unix)
    prix_signal     REAL,                  -- prix en $ au moment de l'alerte
    prix_5min       REAL,
    prix_1h         REAL,
    prix_24h        REAL,
    note_token      INTEGER,
    decision        TEXT                   -- 'acceptee', 'ignoree' ou vide
);

-- Historique des décisions (boutons Accepter / Ignorer)
CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alerte_id  INTEGER NOT NULL,
    decision   TEXT NOT NULL,              -- 'accepter' ou 'ignorer'
    date       TEXT NOT NULL               -- heure du clic (UTC)
);

-- IA : appels (limite par jour), propositions de la revue, versions des règles
CREATE TABLE IF NOT EXISTS appels_ia (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    jour    TEXT NOT NULL,              -- AAAA-MM-JJ (fuseau FUSEAU_HORAIRE)
    quand   TEXT NOT NULL,
    usage   TEXT NOT NULL,              -- haussier, baissier, juge, revue
    ok      INTEGER NOT NULL,
    erreur  TEXT
);

CREATE TABLE IF NOT EXISTS propositions_ia (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sorte            TEXT NOT NULL,     -- 'regle' ou 'wallet'
    chemin           TEXT,              -- règle : ex simulation.stop_loss_pct
    adresse          TEXT,              -- wallet à retirer
    ancienne_valeur  TEXT,              -- JSON
    nouvelle_valeur  TEXT,              -- JSON
    raison           TEXT,
    statut           TEXT NOT NULL DEFAULT 'en_attente',  -- en_attente, acceptee, refusee, obsolete
    cree_le          TEXT NOT NULL,
    decide_le        TEXT,
    telegram_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS versions_regles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    source       TEXT NOT NULL,         -- départ, revue IA, retour arrière...
    description  TEXT,
    surcharges   TEXT NOT NULL,         -- JSON {chemin: valeur} appliqué par-dessus rules.json
    regles       TEXT NOT NULL          -- JSON des règles effectives à ce moment
);

CREATE TABLE IF NOT EXISTS parametres (
    cle     TEXT PRIMARY KEY,
    valeur  TEXT NOT NULL
);

-- Historique des analyses de wallets (commande /score, ajout d'un wallet, analyse du dimanche)
CREATE TABLE IF NOT EXISTS analyses_wallets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    adresse             TEXT NOT NULL,
    date_analyse        TEXT NOT NULL,
    note                INTEGER,            -- de 0 à 100 (vide si DONNÉES INSUFFISANTES)
    verdict             TEXT NOT NULL,      -- SUIVRE / À SURVEILLER / ÉVITER / DONNÉES INSUFFISANTES
    scalpeur            INTEGER NOT NULL,   -- 1 si détention médiane < 2 min
    nb_trades           INTEGER NOT NULL,
    taux_reussite       REAL,               -- entre 0 et 1
    resultat_total_sol  REAL,
    duree_mediane_s     REAL,               -- durée de détention médiane, en secondes
    rapport             TEXT NOT NULL       -- le message envoyé sur Telegram
);

-- Index pour accélérer les recherches
CREATE INDEX IF NOT EXISTS idx_alertes_token ON alertes_envoyees (token_adresse);
CREATE INDEX IF NOT EXISTS idx_achats_token ON achats_wallets (token_adresse, horodatage);
CREATE INDEX IF NOT EXISTS idx_analyses_adresse ON analyses_wallets (adresse, date_analyse);
CREATE INDEX IF NOT EXISTS idx_tokens_createur ON tokens_crees (createur, cree_le);
CREATE INDEX IF NOT EXISTS idx_mentions_mint ON mentions_veille (mint, horodatage);
CREATE INDEX IF NOT EXISTS idx_suivi_cree ON suivi_alertes (cree_le);
"""

# Colonnes ajoutées après la première version : si ta base existe déjà sur Railway,
# init_db() les ajoute automatiquement (sans rien effacer).
COLONNES_AJOUTEES = {
    "trades_simules": {
        "portefeuille": "TEXT",          # 'bot' (Bot seul) ou 'moi' (Mes choix)
        "alerte_id": "INTEGER",
        "type_alerte": "TEXT",
        "wallet_adresse": "TEXT",
        "prix_entree_marche": "REAL",    # prix du marché à l'achat (prix_entree = avec frais)
        "prix_actuel": "REAL",           # dernier prix vérifié
        "prix_max": "REAL",              # plus haut prix vu pendant la position
        "ouvert_le": "INTEGER",          # timestamp Unix
        "ferme_le": "INTEGER",
        "raison_sortie": "TEXT",         # prise de profit, stop loss, durée max...
        "pnl_pct": "REAL",
    },
    "wallets_suivis": {
        "note": "INTEGER",          # dernière note d'analyse (0 à 100)
        "verdict": "TEXT",          # dernier verdict : SUIVRE / À SURVEILLER / ÉVITER
        "scalpeur": "INTEGER",      # 1 si SCALPEUR
        "date_analyse": "TEXT",     # date de la dernière analyse
    },
    "alertes_envoyees": {
        "wallet_adresse": "TEXT",
        "signature": "TEXT",
        "montant_sol": "REAL",
        "prix_sol": "REAL",
        "prix_usd": "REAL",
        "telegram_message_id": "INTEGER",
        "statut": "TEXT NOT NULL DEFAULT 'envoyee'",
        "ia_verdict": "TEXT",       # ACHETER / ATTENDRE / IGNORER (modules/ia.py)
        "ia_explication": "TEXT",
        "note_token": "INTEGER",    # note du token (0 à 100) au moment de l'alerte
        "danger": "INTEGER",        # 1 si le token était marqué DANGER
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


def resume_stockage() -> dict:
    """Où est la base, est-elle sur le volume, et combien de wallets / devs / canaux elle contient."""
    import config
    resume = {
        "chemin": os.path.abspath(DB_PATH),
        "volume": config.VOLUME_RAILWAY or None,
        "sur_volume": config.BASE_SUR_VOLUME,
        "avertissements": list(config.AVERTISSEMENTS_STOCKAGE),
    }
    try:
        with connexion() as conn:
            for cle, table in (("wallets", "wallets_suivis"), ("devs", "devs_suivis"), ("canaux", "canaux_suivis")):
                resume[cle] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        resume["taille_ko"] = round(os.path.getsize(DB_PATH) / 1024)
    except Exception as erreur:
        resume["erreur"] = type(erreur).__name__
    return resume


def texte_stockage(html_ok: bool = True) -> str:
    """Résumé du stockage en quelques lignes (pour le démarrage et /status)."""
    import html as module_html
    r = resume_stockage()
    echapper = module_html.escape if html_ok else (lambda t: t)
    gras = (lambda t: f"<b>{t}</b>") if html_ok else (lambda t: t)
    code = (lambda t: f"<code>{echapper(t)}</code>") if html_ok else (lambda t: t)
    lignes = [f"{gras('Base :')} {code(r['chemin'])}"]
    if r["volume"]:
        lignes.append(f"{gras('Volume Railway :')} {'✅' if r['sur_volume'] else '⚠️'} détecté sur {code(r['volume'])}"
                      + ("" if r["sur_volume"] else " — mais la base n'est PAS dessus"))
    else:
        lignes.append(f"{gras('Volume Railway :')} ⚠️ aucun")
    if "erreur" in r:
        lignes.append(f"❌ Base illisible ({r['erreur']})")
    else:
        lignes.append(f"{gras('Enregistrés :')} {r['wallets']} wallet(s), {r['devs']} dev(s), {r['canaux']} canal(aux)"
                      f" · {r.get('taille_ko', 0)} Ko")
    return "\n".join(lignes)


def init_db() -> None:
    """Crée le dossier et les tables si besoin. À appeler une fois au démarrage."""
    dossier = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(dossier, exist_ok=True)
    with connexion() as conn:
        conn.executescript(SCHEMA)
        # Mise à jour d'une ancienne base : la note peut maintenant être vide
        _note_facultative(conn)
        # Mise à jour d'une ancienne base : on ajoute les colonnes manquantes
        for table, colonnes in COLONNES_AJOUTEES.items():
            existantes = {ligne["name"] for ligne in conn.execute(f"PRAGMA table_info({table})")}
            for nom, definition in colonnes.items():
                if nom not in existantes:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {definition}")


def _note_facultative(conn: sqlite3.Connection) -> None:
    """
    Dans la première version, analyses_wallets.note était obligatoire (NOT NULL).
    SQLite ne sait pas retirer cette contrainte : on recrée la table en gardant les données.
    """
    colonnes = {c["name"]: c for c in conn.execute("PRAGMA table_info(analyses_wallets)")}
    if not colonnes.get("note") or not colonnes["note"]["notnull"]:
        return
    conn.executescript("""
        ALTER TABLE analyses_wallets RENAME TO analyses_wallets_ancienne;
        CREATE TABLE analyses_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, adresse TEXT NOT NULL, date_analyse TEXT NOT NULL,
            note INTEGER, verdict TEXT NOT NULL, scalpeur INTEGER NOT NULL, nb_trades INTEGER NOT NULL,
            taux_reussite REAL, resultat_total_sol REAL, duree_mediane_s REAL, rapport TEXT NOT NULL
        );
        INSERT INTO analyses_wallets SELECT * FROM analyses_wallets_ancienne;
        DROP TABLE analyses_wallets_ancienne;
        CREATE INDEX IF NOT EXISTS idx_analyses_adresse ON analyses_wallets (adresse, date_analyse);
    """)


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


def lire_wallet(adresse: str) -> sqlite3.Row | None:
    """Renvoie le wallet suivi (ou None s'il n'est pas suivi)."""
    with connexion() as conn:
        return conn.execute("SELECT * FROM wallets_suivis WHERE adresse = ?", (adresse,)).fetchone()


def enregistrer_analyse(
    adresse: str, note: int | None, verdict: str, scalpeur: bool, nb_trades: int,
    taux_reussite: float | None, resultat_total_sol: float, duree_mediane_s: float | None, rapport: str,
) -> None:
    """Garde l'analyse dans l'historique et met à jour la note du wallet s'il est suivi."""
    date = maintenant()
    with connexion() as conn:
        conn.execute(
            "INSERT INTO analyses_wallets (adresse, date_analyse, note, verdict, scalpeur, nb_trades, "
            "taux_reussite, resultat_total_sol, duree_mediane_s, rapport) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (adresse, date, note, verdict, int(scalpeur), nb_trades, taux_reussite,
             resultat_total_sol, duree_mediane_s, rapport),
        )
        conn.execute(
            "UPDATE wallets_suivis SET note = ?, verdict = ?, scalpeur = ?, date_analyse = ? WHERE adresse = ?",
            (note, verdict, int(scalpeur), date, adresse),
        )


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

def ajouter_dev(adresse: str, nom: str | None = None, note: str | None = None) -> bool:
    """
    Ajoute un dev (créateur de tokens) à surveiller. S'il existe déjà, son surnom est mis à jour.
    Renvoie True si c'est un nouveau dev.
    """
    with connexion() as conn:
        if conn.execute("SELECT 1 FROM devs_suivis WHERE adresse = ?", (adresse,)).fetchone():
            conn.execute("UPDATE devs_suivis SET nom = ?, actif = 1 WHERE adresse = ?", (nom, adresse))
            return False
        conn.execute(
            "INSERT INTO devs_suivis (adresse, nom, note, date_ajout) VALUES (?, ?, ?, ?)",
            (adresse, nom, note, maintenant()),
        )
        return True


def supprimer_dev(adresse: str) -> bool:
    with connexion() as conn:
        return conn.execute("DELETE FROM devs_suivis WHERE adresse = ?", (adresse,)).rowcount > 0


def lire_dev(adresse: str) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM devs_suivis WHERE adresse = ? AND actif = 1", (adresse,)).fetchone()


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
    note_token: int | None = None,
    danger: bool | None = None,
) -> int:
    """Enregistre une alerte envoyée (avec l'heure et le prix au moment du signal). Renvoie son id."""
    with connexion() as conn:
        curseur = conn.execute(
            "INSERT INTO alertes_envoyees (type, token_adresse, message, date_envoi, wallet_adresse, "
            "signature, montant_sol, prix_sol, prix_usd, telegram_message_id, note_token, danger) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (type_alerte, token_adresse, message, maintenant(), wallet_adresse, signature, montant_sol,
             prix_sol, prix_usd, telegram_message_id, note_token, None if danger is None else int(danger)),
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
# Tokens créés et bilan des créateurs
# ---------------------------------------------------------------------------

def enregistrer_token_cree(mint: str, createur: str, nom: str | None, symbole: str | None, cree_le: int) -> None:
    with connexion() as conn:
        conn.execute(
            "INSERT INTO tokens_crees (mint, createur, nom, symbole, cree_le) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mint) DO UPDATE SET nom = COALESCE(excluded.nom, nom), symbole = COALESCE(excluded.symbole, symbole)",
            (mint, createur, nom, symbole, cree_le),
        )


def tokens_du_createur(createur: str) -> list[sqlite3.Row]:
    with connexion() as conn:
        return conn.execute("SELECT * FROM tokens_crees WHERE createur = ? ORDER BY cree_le DESC", (createur,)).fetchall()


def lire_token_cree(mint: str) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM tokens_crees WHERE mint = ?", (mint,)).fetchone()


def maj_resultat_token(mint: str, mc_actuel: float | None, mc_max: float | None, rug: bool | None, maj_le: int) -> None:
    """Met à jour la market cap et le statut rug d'un token (la market cap max ne fait que monter)."""
    with connexion() as conn:
        conn.execute(
            "UPDATE tokens_crees SET mc_actuel = COALESCE(?, mc_actuel), "
            "mc_max = MAX(COALESCE(mc_max, 0), COALESCE(?, 0), COALESCE(?, 0)), "
            "rug = COALESCE(?, rug), maj_le = ? WHERE mint = ?",
            (mc_actuel, mc_max, mc_actuel, None if rug is None else int(rug), maj_le, mint),
        )


def enregistrer_bilan_createur(adresse: str, nb_lances: int, nb_analyses: int, nb_reussites: int,
                               nb_rugs: int, verdict: str, rapport: str) -> None:
    with connexion() as conn:
        conn.execute(
            "INSERT INTO createurs (adresse, nb_lances, nb_analyses, nb_reussites, nb_rugs, verdict, date_maj, rapport) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(adresse) DO UPDATE SET nb_lances = excluded.nb_lances, "
            "nb_analyses = excluded.nb_analyses, nb_reussites = excluded.nb_reussites, nb_rugs = excluded.nb_rugs, "
            "verdict = excluded.verdict, date_maj = excluded.date_maj, rapport = excluded.rapport",
            (adresse, nb_lances, nb_analyses, nb_reussites, nb_rugs, verdict, maintenant(), rapport),
        )


def lire_bilan_createur(adresse: str) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM createurs WHERE adresse = ?", (adresse,)).fetchone()


def compter_alertes(prefixe_type: str, depuis_iso: str) -> int:
    """Nombre d'alertes d'un type (ex : "nouveau_token") envoyées depuis une date ISO."""
    with connexion() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM alertes_envoyees WHERE type LIKE ? AND date_envoi >= ?",
            (prefixe_type + "%", depuis_iso),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Veille Telegram
# ---------------------------------------------------------------------------

def ajouter_canal(id_canal: int, titre: str, nom_utilisateur: str | None, lien: str,
                  membre: bool, dernier_message_id: int) -> bool:
    """Ajoute (ou met à jour) un canal suivi. Renvoie True s'il est nouveau."""
    with connexion() as conn:
        existe = conn.execute("SELECT 1 FROM canaux_suivis WHERE id = ?", (id_canal,)).fetchone()
        conn.execute(
            "INSERT INTO canaux_suivis (id, titre, nom_utilisateur, lien, membre, dernier_message_id, date_ajout) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET titre = excluded.titre, "
            "nom_utilisateur = excluded.nom_utilisateur, lien = excluded.lien, membre = excluded.membre",
            (id_canal, titre, nom_utilisateur, lien, int(membre), dernier_message_id, maintenant()),
        )
        return existe is None


def supprimer_canal(id_canal: int) -> bool:
    with connexion() as conn:
        return conn.execute("DELETE FROM canaux_suivis WHERE id = ?", (id_canal,)).rowcount > 0


def lister_canaux() -> list[sqlite3.Row]:
    with connexion() as conn:
        return conn.execute("SELECT * FROM canaux_suivis ORDER BY date_ajout").fetchall()


def maj_dernier_message(id_canal: int, message_id: int) -> None:
    with connexion() as conn:
        conn.execute("UPDATE canaux_suivis SET dernier_message_id = MAX(dernier_message_id, ?) WHERE id = ?",
                     (message_id, id_canal))


def enregistrer_mention(mint: str, canal_id: int, canal_titre: str, message_id: int,
                        lien_message: str | None, horodatage: int) -> bool:
    with connexion() as conn:
        return conn.execute(
            "INSERT OR IGNORE INTO mentions_veille (mint, canal_id, canal_titre, message_id, lien_message, horodatage) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mint, canal_id, canal_titre, message_id, lien_message, horodatage),
        ).rowcount > 0


def canaux_ayant_cite(mint: str, depuis: int, jusqua: int) -> list[sqlite3.Row]:
    """Pour chaque canal ayant cité ce token dans la fenêtre : sa PREMIÈRE mention."""
    with connexion() as conn:
        return conn.execute(
            "SELECT canal_id, canal_titre, lien_message, MIN(horodatage) AS horodatage FROM mentions_veille "
            "WHERE mint = ? AND horodatage BETWEEN ? AND ? GROUP BY canal_id ORDER BY horodatage",
            (mint, depuis, jusqua),
        ).fetchall()


def compter_mentions_canal(canal_id: int) -> int:
    with connexion() as conn:
        return conn.execute("SELECT COUNT(DISTINCT mint) FROM mentions_veille WHERE canal_id = ?", (canal_id,)).fetchone()[0]


# ---------------------------------------------------------------------------
# Simulation : suivi des prix, décisions, positions (modules/simulation.py)
# ---------------------------------------------------------------------------

def creer_suivi_alerte(alerte_id: int, mint: str, type_alerte: str, wallet_adresse: str | None,
                       cree_le: int, prix_signal: float | None, note_token: int | None) -> None:
    with connexion() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO suivi_alertes (alerte_id, mint, type_alerte, wallet_adresse, cree_le, prix_signal, note_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (alerte_id, mint, type_alerte, wallet_adresse, cree_le, prix_signal, note_token),
        )


def releves_a_faire(maintenant_ts: int) -> list[sqlite3.Row]:
    """Alertes dont un relevé (signal, 5 min, 1 h ou 24 h) est dû et pas encore fait (48 h max)."""
    with connexion() as conn:
        return conn.execute(
            "SELECT * FROM suivi_alertes WHERE cree_le >= ? AND ("
            " prix_signal IS NULL OR (prix_5min IS NULL AND cree_le <= ?)"
            " OR (prix_1h IS NULL AND cree_le <= ?) OR (prix_24h IS NULL AND cree_le <= ?))",
            (maintenant_ts - 48 * 3600, maintenant_ts - 300, maintenant_ts - 3600, maintenant_ts - 86400),
        ).fetchall()


def maj_releve(alerte_id: int, colonne: str, prix: float) -> None:
    if colonne not in ("prix_signal", "prix_5min", "prix_1h", "prix_24h"):
        raise ValueError(colonne)
    with connexion() as conn:
        conn.execute(f"UPDATE suivi_alertes SET {colonne} = ? WHERE alerte_id = ? AND {colonne} IS NULL", (prix, alerte_id))


def alerte_par_message(telegram_message_id: int) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute(
            "SELECT a.*, s.decision, s.type_alerte AS type_suivi FROM alertes_envoyees a "
            "LEFT JOIN suivi_alertes s ON s.alerte_id = a.id WHERE a.telegram_message_id = ? "
            "ORDER BY a.id DESC LIMIT 1", (telegram_message_id,)).fetchone()


def enregistrer_decision(alerte_id: int, decision: str) -> None:
    """Garde le clic (avec l'heure) et la décision sur l'alerte."""
    statut = {"accepter": "acceptee", "ignorer": "ignoree"}[decision]
    with connexion() as conn:
        conn.execute("INSERT INTO decisions (alerte_id, decision, date) VALUES (?, ?, ?)", (alerte_id, decision, maintenant()))
        conn.execute("UPDATE suivi_alertes SET decision = ? WHERE alerte_id = ?", (statut, alerte_id))
        conn.execute("UPDATE alertes_envoyees SET statut = ? WHERE id = ?", (statut, alerte_id))


def ouvrir_position(portefeuille: str, alerte_id: int | None, mint: str, symbole: str | None, type_alerte: str,
                    wallet_adresse: str | None, mise_sol: float, prix_marche: float | None,
                    frais_pct: float, ouvert_le: int) -> int:
    """Ouvre une position simulée. Sans prix connu, elle reste 'en_attente' jusqu'au premier prix."""
    with connexion() as conn:
        curseur = conn.execute(
            "INSERT INTO trades_simules (token_adresse, symbole, montant_sol, prix_entree, statut, date_ouverture, "
            "portefeuille, alerte_id, type_alerte, wallet_adresse, prix_entree_marche, prix_actuel, prix_max, ouvert_le) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mint, symbole, mise_sol, prix_marche * (1 + frais_pct / 100) if prix_marche else 0,
             "ouvert" if prix_marche else "en_attente", maintenant(), portefeuille, alerte_id, type_alerte,
             wallet_adresse, prix_marche, prix_marche, prix_marche, ouvert_le),
        )
        return curseur.lastrowid


def fixer_prix_entree(trade_id: int, prix_marche: float, frais_pct: float) -> None:
    with connexion() as conn:
        conn.execute(
            "UPDATE trades_simules SET statut = 'ouvert', prix_entree_marche = ?, prix_entree = ?, prix_actuel = ?, "
            "prix_max = ? WHERE id = ? AND statut = 'en_attente'",
            (prix_marche, prix_marche * (1 + frais_pct / 100), prix_marche, prix_marche, trade_id),
        )


def positions(statuts: tuple[str, ...] = ("ouvert", "en_attente")) -> list[sqlite3.Row]:
    with connexion() as conn:
        marques = ",".join("?" * len(statuts))
        return conn.execute(f"SELECT * FROM trades_simules WHERE statut IN ({marques}) AND portefeuille IS NOT NULL "
                            "ORDER BY ouvert_le", statuts).fetchall()


def maj_prix_position(trade_id: int, prix: float) -> None:
    with connexion() as conn:
        conn.execute("UPDATE trades_simules SET prix_actuel = ?, prix_max = MAX(COALESCE(prix_max, 0), ?) WHERE id = ?",
                     (prix, prix, trade_id))


def fermer_position(trade_id: int, prix_marche: float | None, frais_pct: float, raison: str, ferme_le: int) -> None:
    """Ferme une position : résultat net des frais de sortie. Sans prix (token mort), la mise est perdue."""
    with connexion() as conn:
        t = conn.execute("SELECT * FROM trades_simules WHERE id = ?", (trade_id,)).fetchone()
        if t is None or t["statut"] not in ("ouvert", "en_attente"):
            return
        if t["statut"] == "en_attente":
            conn.execute("UPDATE trades_simules SET statut = 'annule', raison_sortie = ?, ferme_le = ?, "
                         "date_fermeture = ? WHERE id = ?", (raison, ferme_le, maintenant(), trade_id))
            return
        prix_net = (prix_marche or 0) * (1 - frais_pct / 100)
        valeur = t["montant_sol"] * prix_net / t["prix_entree"] if t["prix_entree"] else 0
        pnl = valeur - t["montant_sol"]
        conn.execute(
            "UPDATE trades_simules SET statut = 'ferme', prix_sortie = ?, pnl_sol = ?, pnl_pct = ?, raison_sortie = ?, "
            "ferme_le = ?, date_fermeture = ?, prix_actuel = COALESCE(?, prix_actuel) WHERE id = ?",
            (prix_marche, pnl, pnl / t["montant_sol"] * 100, raison, ferme_le, maintenant(), prix_marche, trade_id),
        )


def trades_fermes(portefeuille: str | None = None, limite: int | None = None) -> list[sqlite3.Row]:
    requete = "SELECT * FROM trades_simules WHERE statut = 'ferme' AND portefeuille IS NOT NULL"
    params: list = []
    if portefeuille:
        requete += " AND portefeuille = ?"
        params.append(portefeuille)
    requete += " ORDER BY ferme_le DESC"
    if limite:
        requete += f" LIMIT {int(limite)}"
    with connexion() as conn:
        return conn.execute(requete, params).fetchall()


def suivis_alertes() -> list[sqlite3.Row]:
    with connexion() as conn:
        return conn.execute("SELECT * FROM suivi_alertes").fetchall()


# ---------------------------------------------------------------------------
# IA (modules/ia.py et modules/regles.py)
# ---------------------------------------------------------------------------

def enregistrer_appel_ia(jour: str, usage: str, ok: bool, erreur: str | None = None) -> None:
    with connexion() as conn:
        conn.execute("INSERT INTO appels_ia (jour, quand, usage, ok, erreur) VALUES (?, ?, ?, ?, ?)",
                     (jour, maintenant(), usage, int(ok), erreur))


def compter_appels_ia(jour: str) -> int:
    with connexion() as conn:
        return conn.execute("SELECT COUNT(*) FROM appels_ia WHERE jour = ?", (jour,)).fetchone()[0]


def maj_verdict_ia(alerte_id: int, verdict: str, explication: str) -> None:
    with connexion() as conn:
        conn.execute("UPDATE alertes_envoyees SET ia_verdict = ?, ia_explication = ? WHERE id = ?",
                     (verdict, explication, alerte_id))


def lire_alerte(alerte_id: int) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM alertes_envoyees WHERE id = ?", (alerte_id,)).fetchone()


def creer_proposition(sorte: str, chemin: str | None, adresse: str | None, ancienne: str | None,
                      nouvelle: str | None, raison: str) -> int:
    with connexion() as conn:
        return conn.execute(
            "INSERT INTO propositions_ia (sorte, chemin, adresse, ancienne_valeur, nouvelle_valeur, raison, cree_le) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (sorte, chemin, adresse, ancienne, nouvelle, raison, maintenant()),
        ).lastrowid


def lire_proposition(proposition_id: int) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM propositions_ia WHERE id = ?", (proposition_id,)).fetchone()


def maj_proposition(proposition_id: int, statut: str | None = None, telegram_message_id: int | None = None) -> None:
    with connexion() as conn:
        if statut:
            conn.execute("UPDATE propositions_ia SET statut = ?, decide_le = ? WHERE id = ?",
                         (statut, maintenant(), proposition_id))
        if telegram_message_id:
            conn.execute("UPDATE propositions_ia SET telegram_message_id = ? WHERE id = ?",
                         (telegram_message_id, proposition_id))


def ajouter_version_regles(source: str, description: str, surcharges: str, regles: str) -> int:
    with connexion() as conn:
        return conn.execute(
            "INSERT INTO versions_regles (date, source, description, surcharges, regles) VALUES (?, ?, ?, ?, ?)",
            (maintenant(), source, description, surcharges, regles),
        ).lastrowid


def versions_regles(limite: int = 10) -> list[sqlite3.Row]:
    with connexion() as conn:
        return conn.execute("SELECT * FROM versions_regles ORDER BY id DESC LIMIT ?", (limite,)).fetchall()


def lire_version_regles(version_id: int) -> sqlite3.Row | None:
    with connexion() as conn:
        return conn.execute("SELECT * FROM versions_regles WHERE id = ?", (version_id,)).fetchone()


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
