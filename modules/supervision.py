"""
modules/supervision.py — Chaque module tourne isolé : s'il échoue, seul lui s'arrête.

  - superviser(nom, fabrique) lance une tâche de fond et la relance si elle plante
    (après 30 s, puis 1 min, 2 min... jusqu'à 10 min), sans jamais arrêter le reste du bot ;
  - un module qui ne PEUT pas fonctionner (variable manquante, bibliothèque absente) lève
    ModuleDesactive : il est marqué « désactivé » avec la raison, et n'est pas relancé ;
  - etats contient l'état de chaque module, affiché par /status (Telegram) et /health (web).
"""

import asyncio
import logging
import time

log = logging.getLogger(__name__)

# nom du module -> {"statut", "raison", "depuis", "relances"}
#   statut : "actif" | "en attente" | "désactivé" | "erreur" | "arrêté"
etats: dict[str, dict] = {}

ICONES = {"actif": "✅", "en attente": "⏳", "désactivé": "⏸️", "erreur": "❌", "arrêté": "⏹️"}


class ModuleDesactive(Exception):
    """Le module ne peut pas fonctionner (ex : variable manquante). Il ne sera pas relancé."""


def marquer(nom: str, statut: str, raison: str | None = None) -> None:
    precedent = etats.get(nom, {})
    etats[nom] = {
        "statut": statut,
        "raison": raison,
        "depuis": precedent.get("depuis") if precedent.get("statut") == statut else time.time(),
        "relances": precedent.get("relances", 0),
    }
    if statut in ("désactivé", "erreur"):
        log.warning("Module %s %s : %s", nom, statut, raison)


async def superviser(nom: str, fabrique) -> None:
    """
    Fait tourner `fabrique()` (une fonction qui renvoie une coroutine) en la relançant si elle plante.
    Ne lève jamais d'exception (sauf l'annulation normale à l'arrêt du bot).
    """
    attente = 30
    while True:
        if etats.get(nom, {}).get("statut") not in ("en attente",):
            marquer(nom, "actif")
        try:
            await fabrique()
            if etats.get(nom, {}).get("statut") == "actif":
                marquer(nom, "arrêté", "tâche terminée")
            return
        except asyncio.CancelledError:
            raise
        except ModuleDesactive as raison:
            marquer(nom, "désactivé", str(raison))
            return
        except Exception as erreur:
            log.exception("Le module %s a planté", nom)
            etats.setdefault(nom, {})["relances"] = etats.get(nom, {}).get("relances", 0) + 1
            marquer(nom, "erreur", f"{type(erreur).__name__} : {erreur}"[:160] + f" — relance dans {attente} s")
            await asyncio.sleep(attente)
            attente = min(attente * 2, 600)


def texte_status() -> str:
    """Lignes pour /status (texte brut, à échapper si on l'envoie en HTML)."""
    lignes = []
    for nom, e in sorted(etats.items()):
        lignes.append(f"{ICONES.get(e['statut'], '•')} {nom} : {e['statut']}" + (f" — {e['raison']}" if e.get("raison") else ""))
    return "\n".join(lignes)


def pour_health() -> dict:
    return {nom: {"statut": e["statut"], "raison": e.get("raison"), "relances": e.get("relances", 0)}
            for nom, e in sorted(etats.items())}
