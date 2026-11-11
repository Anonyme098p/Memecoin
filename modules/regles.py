"""
modules/regles.py — Changements de rules.json et historique des versions (/rules, /rollback).

Sur Railway, le dossier du code est remis à zéro à chaque déploiement : modifier rules.json
sur place serait perdu. Les changements acceptés (propositions de l'IA, retours en arrière) sont
donc enregistrés dans la base, sur le Volume, sous forme de « surcharges » :
    {"simulation.stop_loss_pct": -30, "criteres.top10_pct.poids": 20, ...}
Les règles effectives = rules.json du projet + ces surcharges par-dessus.
Chaque changement crée une version (table versions_regles) : on peut revenir à n'importe laquelle.

Rien n'est jamais changé automatiquement : uniquement après ton clic sur Accepter, ou /rollback.
"""

import copy
import json
import logging

import config
import db

log = logging.getLogger(__name__)


def _base() -> dict:
    """Le rules.json du projet, tel quel."""
    with open(config.RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def surcharges() -> dict:
    try:
        return json.loads(db.lire_parametre("regles_surcharges", "{}") or "{}")
    except ValueError:
        log.error("Surcharges de règles illisibles : ignorées")
        return {}


def lire_chemin(regles: dict, chemin: str) -> tuple[bool, object]:
    """Valeur d'un chemin du type "simulation.stop_loss_pct". Renvoie (trouvé, valeur)."""
    noeud: object = regles
    for morceau in chemin.split("."):
        if not isinstance(noeud, dict) or morceau not in noeud:
            return False, None
        noeud = noeud[morceau]
    return True, noeud


def _poser(regles: dict, chemin: str, valeur) -> None:
    morceaux = chemin.split(".")
    noeud = regles
    for morceau in morceaux[:-1]:
        noeud = noeud[morceau]
    noeud[morceaux[-1]] = valeur


def appliquer(base: dict, surch: dict | None = None) -> dict:
    """Règles effectives : la base + les surcharges (un chemin qui n'existe plus est ignoré)."""
    regles = copy.deepcopy(base)
    for chemin, valeur in (surcharges() if surch is None else surch).items():
        if lire_chemin(regles, chemin)[0]:
            _poser(regles, chemin, valeur)
        else:
            log.warning("Surcharge ignorée : %s n'existe plus dans rules.json", chemin)
    return regles


def effectives() -> dict:
    return appliquer(_base())


def aplatir(regles: dict, prefixe: str = "") -> dict:
    """{chemin: valeur} de toutes les valeurs modifiables (les explications "_..." sont exclues)."""
    resultat = {}
    for cle, valeur in regles.items():
        if str(cle).startswith("_"):
            continue
        chemin = f"{prefixe}{cle}"
        if isinstance(valeur, dict):
            resultat.update(aplatir(valeur, chemin + "."))
        else:
            resultat[chemin] = valeur
    return resultat


def differences(avant: dict, apres: dict) -> list[tuple[str, object, object]]:
    a, b = aplatir(avant), aplatir(apres)
    return [(c, a.get(c), b.get(c)) for c in sorted(set(a) | set(b)) if a.get(c) != b.get(c)]


def meme_type(ancienne, nouvelle) -> bool:
    """La nouvelle valeur a-t-elle le même genre que l'ancienne ? (nombre, oui/non, liste, texte)"""
    if isinstance(ancienne, bool) or isinstance(nouvelle, bool):
        return isinstance(ancienne, bool) and isinstance(nouvelle, bool)
    if isinstance(ancienne, (int, float)):
        return isinstance(nouvelle, (int, float))
    return type(ancienne) is type(nouvelle)


def _initialiser() -> None:
    """Crée la version de départ la première fois."""
    if not db.versions_regles(1):
        db.ajouter_version_regles("départ", "rules.json du projet", json.dumps(surcharges()),
                                  json.dumps(effectives(), ensure_ascii=False))


def changer(changements: dict, source: str, description: str) -> int:
    """Applique des changements {chemin: valeur} et crée une nouvelle version. Renvoie son numéro."""
    _initialiser()
    base = _base()
    for chemin, valeur in changements.items():
        trouve, actuelle = lire_chemin(base, chemin)
        if not trouve or chemin.split(".")[0].startswith("_"):
            raise ValueError(f"règle inconnue : {chemin}")
        if not meme_type(actuelle, valeur):
            raise ValueError(f"{chemin} : type de valeur incorrect")
    nouvelles = {**surcharges(), **changements}
    db.ecrire_parametre("regles_surcharges", json.dumps(nouvelles))
    version = db.ajouter_version_regles(source, description, json.dumps(nouvelles),
                                        json.dumps(appliquer(base, nouvelles), ensure_ascii=False))
    log.info("Règles : version %d (%s)", version, description)
    return version


def revenir(version_id: int) -> int:
    """Remet les surcharges d'une version passée (crée une nouvelle version « retour à la version N »)."""
    _initialiser()
    ancienne = db.lire_version_regles(version_id)
    if ancienne is None:
        raise ValueError(f"version {version_id} introuvable")
    surch = json.loads(ancienne["surcharges"])
    db.ecrire_parametre("regles_surcharges", json.dumps(surch))
    return db.ajouter_version_regles("retour arrière", f"retour à la version {version_id}", json.dumps(surch),
                                     json.dumps(appliquer(_base(), surch), ensure_ascii=False))


def versions(limite: int = 10) -> list:
    _initialiser()
    return db.versions_regles(limite)
