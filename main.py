"""
main.py — Point d'entrée du programme.

Lance dans le MÊME processus (une seule boucle asyncio, rien ne bloque) :
  - le serveur web FastAPI : routes /, /health, webhook Helius /helius, page /tglogin ;
  - le bot Telegram (mode "polling" : il va chercher lui-même les nouveaux messages) ;
  - les modules en tâches de fond : surveillance, analyse du dimanche, nouveaux tokens pump.fun,
    veille Telegram...

Règle d'or : LE BOT NE S'ARRÊTE JAMAIS EN ENTIER À CAUSE D'UN MODULE.
  - une variable manquante ou mal écrite -> valeur par défaut ou module désactivé (config.PROBLEMES) ;
  - un module qui ne se charge pas ou qui plante -> lui seul est désactivé ou relancé
    (modules/supervision.py), les autres continuent ;
  - l'état de chaque module est visible dans /status (Telegram) et /health (web).

Ordre de démarrage important pour Railway :
  1. le serveur web ouvre TOUT DE SUITE son port (0.0.0.0 et la variable PORT) ;
  2. le bot et les modules démarrent ensuite, en arrière-plan.

Démarrage : python main.py
"""

import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

import config  # ne plante jamais : les problèmes sont notés dans config.PROBLEMES
from modules import supervision
from modules.supervision import ModuleDesactive, marquer

# Configuration des logs (visibles dans l'onglet "Deploy Logs" de Railway)
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
# On réduit le bruit des bibliothèques (sinon chaque requête Telegram est affichée)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("main")

# Port d'écoute : Railway fournit la variable PORT ; en local, 8000
try:
    PORT = int(os.environ.get("PORT", 8000))
except ValueError:
    PORT = 8000


def _importer(nom: str):
    """Importe un module ; renvoie None (et le note) s'il ne se charge pas, au lieu de tout arrêter."""
    try:
        return importlib.import_module(nom)
    except Exception as erreur:
        log.exception("Le module %s ne se charge pas", nom)
        _erreurs_import[nom] = f"{type(erreur).__name__} : {erreur}"[:160]
        return None


_erreurs_import: dict[str, str] = {}
db = _importer("db")
telegram_bot = _importer("telegram_bot")
wallets = _importer("modules.wallets")
webhooks = _importer("modules.webhooks")
surveillance = _importer("modules.surveillance")
nouveaux_tokens = _importer("modules.nouveaux_tokens")
veille = _importer("modules.veille")
simulation = _importer("modules.simulation")
ia = _importer("modules.ia")


def _raison_import(nom: str) -> str:
    return f"chargement impossible ({_erreurs_import.get(nom, 'module absent')})"


# ---------------------------------------------------------------------------
# Lancement des modules
# ---------------------------------------------------------------------------

def _lancer(app: FastAPI, nom: str, fabrique) -> None:
    """Lance un module en tâche de fond, sous surveillance (relancé s'il plante)."""
    app.state.taches[nom] = asyncio.create_task(supervision.superviser(nom, fabrique))


def _desactiver_tous(raison: str) -> None:
    for nom in ("surveillance", "analyse du dimanche", "flux pump.fun", "scanall", "bilan des devs",
                "veille Telegram", "webhook Helius", "simulation", "IA"):
        marquer(nom, "désactivé", raison)


async def _verifier_regles() -> None:
    """rules.json lisible ? Sinon, seule la note des tokens est touchée (les alertes partent sans note)."""
    try:
        from modules import analyse
        analyse.charger_regles()
        marquer("note des tokens", "actif")
    except Exception as erreur:
        marquer("note des tokens", "désactivé", f"rules.json illisible ({erreur})"[:160])


async def demarrer_bot(app: FastAPI) -> None:
    """
    Démarre le bot Telegram puis les modules, en arrière-plan. Si Telegram est injoignable, on
    réessaie (5 s, 10 s, 20 s... jusqu'à 5 min) : le serveur web, lui, continue de répondre.
    """
    if telegram_bot is None:
        marquer("bot Telegram", "désactivé", _raison_import("telegram_bot"))
        _desactiver_tous("bot Telegram indisponible")
        return
    if not config.TELEGRAM_OK:
        manque = ", ".join(n for n in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID") if n in config.PROBLEMES)
        marquer("bot Telegram", "désactivé", f"variable(s) à corriger : {manque}")
        _desactiver_tous("bot Telegram indisponible")
        return

    from telegram.error import InvalidToken

    attente = 5
    marquer("bot Telegram", "en attente", "connexion à Telegram")
    while True:
        try:
            application = telegram_bot.construire_application()
            await application.initialize()  # vérifie le token auprès de Telegram
            await application.start()
            # drop_pending_updates=True : on ignore les commandes envoyées pendant que le bot était éteint
            await application.updater.start_polling(drop_pending_updates=True)
            break
        except InvalidToken:
            marquer("bot Telegram", "désactivé", "TELEGRAM_TOKEN refusé par Telegram : vérifie la variable sur Railway")
            _desactiver_tous("bot Telegram indisponible")
            return  # inutile de réessayer : il faut corriger la variable
        except Exception as erreur:
            marquer("bot Telegram", "en attente", f"Telegram injoignable ({type(erreur).__name__}), nouvel essai dans {attente} s")
            await asyncio.sleep(attente)
            attente = min(attente * 2, 300)

    app.state.telegram = application  # utilisé par les routes web (webhook Helius, /tglogin)
    marquer("bot Telegram", "actif")
    log.info("Bot Telegram démarré")
    await _verifier_regles()

    # --- Modules en tâches de fond : chacun tourne et plante tout seul ---
    if surveillance:
        _lancer(app, "surveillance", lambda: surveillance.boucle(application))
    else:
        marquer("surveillance", "désactivé", _raison_import("modules.surveillance"))

    if wallets:
        async def analyse_du_dimanche():
            if not config.HELIUS_API_KEY:
                raise ModuleDesactive("HELIUS_API_KEY manquante")
            await wallets.boucle_hebdomadaire(application)
        _lancer(app, "analyse du dimanche", analyse_du_dimanche)
    else:
        marquer("analyse du dimanche", "désactivé", _raison_import("modules.wallets"))

    if nouveaux_tokens:
        _lancer(app, "flux pump.fun", lambda: nouveaux_tokens.ecouter(application))
        _lancer(app, "scanall", lambda: nouveaux_tokens.boucle_scan(application))
        _lancer(app, "bilan des devs", lambda: nouveaux_tokens.boucle_maj_devs(application))
    else:
        for nom in ("flux pump.fun", "scanall", "bilan des devs"):
            marquer(nom, "désactivé", _raison_import("modules.nouveaux_tokens"))

    if veille:
        _lancer(app, "veille Telegram", lambda: veille.demarrer(application))
    else:
        marquer("veille Telegram", "désactivé", _raison_import("modules.veille"))

    if simulation:  # trades simulés : vérification des positions toutes les minutes
        _lancer(app, "simulation", lambda: simulation.boucle(application))
    else:
        marquer("simulation", "désactivé", _raison_import("modules.simulation"))

    if ia:  # avis IA sur les alertes + revue du dimanche (désactivé sans XAI_API_KEY / ANTHROPIC_API_KEY)
        _lancer(app, "IA", lambda: ia.boucle_revue(application))
    else:
        marquer("IA", "désactivé", _raison_import("modules.ia"))

    # --- Webhook Helius (alertes d'achat des wallets suivis) ---
    etat_webhook = await _synchroniser_webhook()

    # --- Message de démarrage, avec ce qui ne tourne pas ---
    await asyncio.sleep(2)  # on laisse aux modules le temps d'indiquer leur état
    lignes = ["🚀 Bot démarré.", etat_webhook]
    hors_service = {n: e for n, e in supervision.etats.items() if e["statut"] in ("désactivé", "erreur")}
    if hors_service:
        lignes += ["", "⚠️ Modules désactivés (le reste fonctionne) :"]
        lignes += [f"• {n} : {e['raison']}" for n, e in hors_service.items()]
    if config.PROBLEMES:
        lignes += ["", "⚠️ Variables à corriger : " + ", ".join(config.PROBLEMES)]
    lignes.append("Détails : /status")
    try:
        await telegram_bot.envoyer_message(application, "\n".join(lignes))
    except Exception:
        log.exception("Impossible d'envoyer le message de démarrage (vérifie TELEGRAM_CHAT_ID)")


async def _synchroniser_webhook() -> str:
    nom = "webhook Helius"
    if wallets is None:
        marquer(nom, "désactivé", _raison_import("modules.wallets"))
        return "⚠️ Alertes d'achat désactivées (module wallets indisponible)"
    if not config.HELIUS_API_KEY:
        marquer(nom, "désactivé", "HELIUS_API_KEY manquante")
        return "⚠️ Alertes d'achat désactivées : HELIUS_API_KEY manquante"
    probleme = wallets._probleme_de_config()
    if probleme:
        marquer(nom, "désactivé", probleme.split("\n")[0].replace("⚠️ ", ""))
        return probleme
    try:
        etat = await asyncio.wait_for(wallets.synchroniser_webhook(), 60)
    except Exception as erreur:
        marquer(nom, "erreur", f"vérification impossible au démarrage ({type(erreur).__name__})")
        return f"⚠️ Webhook Helius non vérifié au démarrage ({type(erreur).__name__})"
    marquer(nom, "erreur" if etat.startswith("❌") else "actif", None if not etat.startswith("❌") else etat[:160])
    log.info(etat)
    return etat


async def arreter_bot(app: FastAPI) -> None:
    """Arrêt propre (redéploiement, Ctrl+C...). Chaque étape est protégée."""
    log.info("Arrêt en cours...")
    if veille:
        try:
            await veille.arreter()
        except Exception:
            log.exception("Arrêt de la veille")
    for tache in list(app.state.taches.values()):
        tache.cancel()
    for tache in list(app.state.taches.values()):
        try:
            await tache
        except (asyncio.CancelledError, Exception):
            pass
    application = app.state.telegram
    if application is not None:
        for etape in (application.updater.stop, application.stop, application.shutdown):
            try:
                await etape()
            except Exception:
                log.exception("Arrêt du bot Telegram")
    log.info("Bot arrêté proprement")


@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    """
    Avant le "yield" : le démarrage du serveur web. On y fait le strict minimum (la base de données)
    et on lance le bot EN ARRIÈRE-PLAN : uvicorn n'ouvre le port qu'une fois cette partie terminée.
    Après le "yield" : l'arrêt.
    """
    app.state.telegram = None
    app.state.taches = {}
    for nom, raison in _erreurs_import.items():
        log.error("Module non chargé : %s (%s)", nom, raison)
    if db is None:
        marquer("base de données", "erreur", _raison_import("db"))
    else:
        try:
            db.init_db()
            marquer("base de données", "actif")
            log.info("Base de données prête : %s", config.DB_PATH)
        except Exception as erreur:
            # Le serveur web reste en ligne pour que /health explique le problème
            marquer("base de données", "erreur", f"{type(erreur).__name__} : {erreur} (vérifie DB_PATH et le volume)"[:160])
    for nom, probleme in config.PROBLEMES.items():
        log.warning("Variable %s : %s", nom, probleme)
    app.state.taches["démarrage du bot"] = asyncio.create_task(demarrer_bot(app))
    log.info("Serveur web en écoute sur 0.0.0.0:%d (le bot Telegram démarre en arrière-plan)", PORT)

    yield  # <- le programme tourne ici jusqu'à l'arrêt

    await arreter_bot(app)


# ---------------------------------------------------------------------------
# Serveur web
# ---------------------------------------------------------------------------

app = FastAPI(title="Blot-nsm", lifespan=cycle_de_vie)
if webhooks:
    app.include_router(webhooks.router)  # /helius
if veille:
    app.include_router(veille.router)    # page /tglogin


@app.get("/", response_class=PlainTextResponse)
async def accueil() -> str:
    return "Blot-nsm en ligne"


@app.get("/health")
async def sante() -> dict:
    """
    État de chaque module. Répond TOUJOURS 200 tant que le serveur web tourne (c'est ce que vérifie
    Railway) ; "statut" vaut "degrade" si un module est en erreur, si la base ou le bot Telegram ne
    tournent pas, ou si une variable est à corriger (un module facultatif non configuré n'est pas une panne). Page publique : uniquement des états et des noms de variables, aucune valeur secrète.
    """
    try:
        modules = supervision.pour_health()
        taches = {nom: ("en cours" if not t.done() else "arrêtée" if t.cancelled() else "terminée")
                  for nom, t in app.state.taches.items()}
        # Un module facultatif désactivé exprès (ex : veille sans ses variables) n'est pas une panne
        essentiels_ko = any(modules.get(n, {}).get("statut") != "actif" for n in ("base de données", "bot Telegram"))
        degrade = (bool(config.PROBLEMES) or essentiels_ko
                   or any(m["statut"] == "erreur" for m in modules.values()))
        reponse = {"statut": "degrade" if degrade else "ok", "modules": modules,
                   "variables_a_corriger": config.PROBLEMES, "taches_de_fond": taches}
        if veille and db:
            reponse["veille_telegram"] = {"messages_lus": veille.etat["messages_lus"], "alertes": veille.etat["alertes"]}
        if nouveaux_tokens:
            reponse["flux_pump_fun"] = {"creations_vues": nouveaux_tokens.statut["creations_vues"],
                                        "reconnexions": nouveaux_tokens.statut["reconnexions"]}
        return reponse
    except Exception as erreur:  # /health ne doit jamais planter
        return {"statut": "degrade", "erreur": type(erreur).__name__}


if __name__ == "__main__":
    # host="0.0.0.0" est obligatoire sur Railway pour être joignable depuis l'extérieur.
    # proxy_headers : Railway passe par un proxy ; on lui fait confiance pour l'IP et le https.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=config.LOG_LEVEL.lower(),
                proxy_headers=True, forwarded_allow_ips="*")
