# 🤖 Bot d'alertes memecoins Solana

Bot Telegram personnel qui surveillera des wallets et des devs Solana et t'enverra des alertes.
Il est prévu pour tourner 24h/24 sur [Railway](https://railway.com).

> **État actuel :** le suivi des wallets fonctionne. Dès qu'un wallet suivi achète un token
> (SOL → token), tu reçois une alerte Telegram, et un **SIGNAL FORT** si plusieurs wallets
> suivis achètent le même token en peu de temps. Chaque wallet est analysé (30 derniers jours)
> et reçoit une note de 0 à 100 avec un verdict SUIVRE / À SURVEILLER / ÉVITER.
> Chaque token alerté reçoit aussi une note de sécurité de 0 à 100 (DexScreener, RugCheck,
> Helius), avec ses chiffres clés et ses drapeaux rouges. Les créations de tokens pump.fun sont
> écoutées en temps réel : alerte PRIORITAIRE quand un dev suivi lance un token. Enfin, une veille
> lit (sans jamais rien envoyer) des canaux Telegram avec ton compte et alerte quand un même token
> y est cité plusieurs fois.

---

## 📁 Structure du projet

```
.
├── main.py              # Point d'entrée : démarre le serveur web + le bot Telegram
├── config.py            # Lit toutes les variables d'environnement
├── telegram_bot.py      # Le bot Telegram et ses commandes
├── db.py                # Base de données SQLite (tables + fonctions utiles)
├── modules/
│   ├── wallets.py       # Suivi des wallets : commandes, alertes, analyse du dimanche
│   ├── nouveaux_tokens.py # Créations pump.fun en temps réel, devs suivis, /scanall, /devscore
│   ├── veille.py        # Veille de canaux Telegram (compte utilisateur, lecture seule), page /tglogin
│   ├── simulation.py    # Trades simulés : « Bot seul » contre « Mes choix », /stats, /journal, /positions
│   ├── ia.py            # Avis IA sur les alertes (haussier, baissier, juge) et revue de la semaine
│   ├── regles.py        # Changements acceptés de rules.json et historique des versions (/rules, /rollback)
│   ├── supervision.py   # État de chaque module, relance automatique en cas de plantage
│   ├── analyse.py       # Note de sécurité d'un token (0 à 100), règles dans rules.json
│   ├── analyse_wallet.py # Analyse d'historique d'un wallet : trades, statistiques, risques, note
│   ├── transactions.py  # Interprétation d'une transaction : achat, vente ou ignorée (et pourquoi)
│   ├── helius.py        # Appels à l'API Helius (webhook, infos d'un token)
│   ├── tokens.py        # Nom, symbole et prix d'un token (Helius + DexScreener)
│   ├── webhooks.py      # Routes du serveur web (FastAPI), dont /helius
│   ├── alertes.py       # Envoi d'une alerte générique (pause, anti-doublon, historique)
│   └── surveillance.py  # Boucle qui tourne en fond (pour de futures fonctionnalités)
├── rules.json           # Règles de la note des tokens (poids, seuils, DANGER) : modifiable
├── requirements.txt     # Bibliothèques Python à installer
├── railway.json         # Configuration Railway
├── Procfile             # Commande de démarrage (alternative à railway.json)
├── .python-version      # Version de Python (3.11)
└── .env.example         # Modèle des variables d'environnement
```

### Commandes Telegram

| Commande  | Effet |
|-----------|-------|
| `/start`  | Message d'accueil et liste des commandes |
| `/status` | État (actif / en pause), durée en ligne, nombre de wallets/devs suivis, alertes, trades simulés |
| `/pause`  | Met les alertes en pause (le réglage est conservé même après un redémarrage) |
| `/resume` | Relance les alertes |
| `/addwallet <adresse> <surnom>` | Suit un wallet (le surnom peut contenir des espaces). Le webhook Helius est mis à jour tout seul, puis le wallet est analysé |
| `/removewallet <adresse>` | Arrête de suivre un wallet (et met à jour le webhook Helius) |
| `/wallets` | Liste des wallets suivis, avec leur note et leur verdict |
| `/score <adresse>` | Analyse un wallet **sans le suivre** et envoie le rapport |
| `/check <adresse>` | Note de sécurité complète d'un token, à la demande |
| `/minscore <0-100>` | Ne recevoir que les alertes dont la note du token atteint ce score (`/minscore 0` = tout recevoir, `/minscore` seul = voir le réglage) |
| `/adddev <adresse> <surnom>` | Suit un créateur de tokens : alerte PRIORITAIRE dès qu'il lance un token (son bilan est calculé tout de suite) |
| `/removedev <adresse>` | Arrête de suivre un créateur |
| `/devs` | Liste des devs suivis avec leur bilan, état du flux pump.fun et de /scanall |
| `/devscore <adresse>` | Bilan des lancements d'un créateur (tokens créés, réussites, rugs) et verdict SUIVRE / À SURVEILLER / ÉVITER |
| `/scanall on <score>` / `off` | Alertes aussi pour les créateurs inconnus dont le token décolle et atteint ce score |
| `/addchannel <lien>` | Suit un canal ou groupe Telegram (`t.me/nom`, `@nom`, ou lien d'invitation d'un canal déjà rejoint) |
| `/removechannel <lien>` | Arrête de suivre un canal (lien, `@nom` ou titre) |
| `/channels` | Canaux suivis, mode de lecture et nombre de tokens repérés |
| `/veille` | État de la connexion du compte et nombre de messages lus depuis le démarrage |
| `/stats` | Résultats des trades simulés : réussite, gain et perte moyens, total en SOL et en €, par type d'alerte, par wallet, et **Bot seul contre Mes choix** |
| `/journal` | Les 10 derniers trades simulés avec leur résultat |
| `/positions` | Les trades simulés encore ouverts |
| `/revue` | Lance tout de suite la revue IA de la semaine (propositions à accepter ou refuser) |
| `/rules` | Historique des versions des règles (`/rules <n>` : détail d'une version) |
| `/rollback [n]` | Revient à la version `n` des règles (sans numéro : à la version précédente) |
| `/debugtx <adresse>` | Montre comment les 5 dernières transactions du wallet ont été comprises (achat, vente ou ignorée, et pourquoi) |
| `/webhook` | État du webhook Helius : URL, type, transactions, actif ou non, adresses synchronisées, secret identique, dernière réception |
| `/webhook sync` | Force la resynchronisation du webhook Helius, puis affiche son état |

🔒 Le bot **ne répond qu'à ton chat** (`TELEGRAM_CHAT_ID`). Les autres personnes sont ignorées.

### Tables de la base de données

| Table              | Contenu |
|--------------------|---------|
| `wallets_suivis`   | Wallets Solana surveillés (adresse, surnom) |
| `devs_suivis`      | Devs (créateurs de tokens) surveillés |
| `tokens_crees`     | Tokens des devs suivis ou analysés : market cap actuelle et maximale, rug |
| `createurs`        | Bilan de chaque créateur : tokens lancés, réussites, rugs, verdict |
| `canaux_suivis`    | Canaux Telegram de la veille |
| `mentions_veille`  | Tokens repérés dans ces canaux (canal, message, heure) |
| `alertes_envoyees` | Historique des alertes : heure, wallet, montant, **prix en SOL et en $ au moment du signal**, note du token, DANGER, statut (envoyée / ignorée) |
| `achats_wallets`   | Chaque achat détecté (même bot en pause) : sert à repérer les SIGNAUX FORTS |
| `analyses_wallets` | Historique des analyses : note, verdict, SCALPEUR, statistiques et rapport complet |
| `trades_simules`   | Trades simulés des deux portefeuilles : prix d'entrée et de sortie, résultat, raison de sortie |
| `suivi_alertes`    | Prix de chaque alerte au signal, à 5 min, 1 h et 24 h, et ta décision |
| `decisions`        | Chaque clic ✅ Accepter / 🙈 Ignorer, avec l'heure |
| `appels_ia`        | Chaque appel à l'IA (pour la limite par jour) |
| `propositions_ia`  | Propositions de la revue IA et ta décision |
| `versions_regles`  | Toutes les versions des règles (pour revenir en arrière) |
| `parametres`       | Petits réglages (ex : pause on/off) |

### Variables d'environnement

| Variable                  | Obligatoire | Description |
|---------------------------|:-----------:|-------------|
| `TELEGRAM_TOKEN`          | ✅ | Token du bot, donné par @BotFather |
| `TELEGRAM_CHAT_ID`        | ✅ | Ton identifiant Telegram (un nombre) |
| `HELIUS_API_KEY`          | ✅ | Clé API Helius |
| `DB_PATH`                 | ✅ sur Railway | Chemin du fichier SQLite. Sur Railway : `/data/bot.db` |
| `PUBLIC_URL`              | ✅ pour les alertes | Ton domaine Railway, ex : `https://mon-bot.up.railway.app`. Si absent, le bot utilise `RAILWAY_PUBLIC_DOMAIN` (rempli par Railway) |
| `HELIUS_WEBHOOK_SECRET`   | ✅ pour les alertes | Secret que Helius envoie dans l'en-tête `Authorization` de chaque requête. Sans lui, le webhook n'est pas créé et `/helius` refuse tout |
| `HELIUS_TRANSACTION_TYPES`| ❌ | Types de transactions envoyés par Helius, séparés par des virgules (défaut : `ANY`). Voir plus bas |
| `SIGNAL_FORT_MIN_WALLETS` | ❌ | Nombre de wallets pour un SIGNAL FORT (défaut : 2) |
| `SIGNAL_FORT_MINUTES`     | ❌ | Fenêtre de temps du SIGNAL FORT, en minutes (défaut : 10) |
| `ANALYSE_JOURS`           | ❌ | Nombre de jours d'historique analysés (défaut : 30) |
| `ANALYSE_MAX_TRANSACTIONS`| ❌ | Transactions lues au maximum par analyse (défaut : 5000) |
| `ANALYSE_HEBDO_HEURE`     | ❌ | Heure de l'analyse du dimanche (défaut : 10) |
| `FUSEAU_HORAIRE`          | ❌ | Fuseau horaire de cette heure (défaut : `Europe/Paris`) |
| `NOUVEAUX_TOKENS_MAX_PAR_HEURE` | ❌ | Alertes de nouveaux tokens par heure au maximum (défaut : 20 ; les devs suivis passent toujours) |
| `SCAN_DELAI_S`            | ❌ | /scanall : délai avant de revérifier un nouveau token (défaut : 120 s) |
| `SCAN_MC_MIN_USD` / `SCAN_TX_5MIN_MIN` | ❌ | /scanall : market cap et transactions en 5 min minimum pour qu'un token soit analysé (défaut : 10 000 $ et 20) |
| `DEV_MAX_TOKENS_ANALYSES` | ❌ | Nombre de tokens d'un créateur dont le résultat est vérifié (défaut : 30, les plus récents) |
| `TG_API_ID` / `TG_API_HASH` | ❌ (veille) | Identifiants d'application Telegram, voir « Veille Telegram » plus bas |
| `TGLOGIN_PASSWORD`        | ❌ (veille) | Mot de passe de la page `/tglogin` (12 caractères minimum). Sert aussi à chiffrer la session : le changer oblige à se reconnecter |
| `VEILLE_MIN_CANAUX` / `VEILLE_FENETRE_MIN` | ❌ | Alerte si un token est cité dans au moins 2 canaux en 30 minutes (défauts) |
| `VEILLE_RELECTURE_S`      | ❌ | Relecture des canaux publics non rejoints (défaut : 60 s) |
| `XAI_API_KEY`             | ❌ (IA) | Clé de l'API Grok (console.x.ai) |
| `ANTHROPIC_API_KEY`       | ❌ (IA) | Clé de l'API Claude (platform.claude.com) |
| `IA_FOURNISSEUR`          | ❌ | `grok` ou `claude`, si les deux clés sont présentes (défaut : `grok`) |
| `IA_MODELE`               | ❌ | Modèle utilisé (défaut : `grok-4.7` ou `claude-opus-5-5`) |
| `IA_MAX_PAR_JOUR`         | ❌ | Appels à l'IA par jour au maximum (défaut : 200 ; chaque alerte en coûte 3) |
| `PUMPPORTAL_WS_URL`       | ❌ | Adresse du flux temps réel (défaut : PumpPortal) |
| `RULES_PATH`              | ❌ | Chemin du fichier de règles de la note des tokens (défaut : `rules.json` du projet) |
| `ANALYSE_TOKEN_CACHE_S`   | ❌ | Secondes pendant lesquelles l'analyse d'un token est réutilisée (défaut : 120) |
| `INTERVALLE_SURVEILLANCE` | ❌ | Secondes entre deux tours de surveillance (défaut : 60) |
| `LOG_LEVEL`               | ❌ | `DEBUG`, `INFO` (défaut), `WARNING`, `ERROR` |
| `PORT`                    | ❌ | Fourni automatiquement par Railway, ne pas le définir |

⚠️ **Ne mets jamais tes clés dans le code ni sur GitHub.** Elles vont uniquement dans
l'onglet *Variables* de Railway (ou dans un fichier `.env` local, qui est ignoré par Git).

---

## 👛 Comment marche le suivi des wallets

1. Tu envoies `/addwallet 7xKX...gAsU Baleine` au bot.
2. Le bot crée (ou met à jour) **un webhook Helius** qui surveille tous tes wallets et envoie
   chaque transaction, déjà décodée (format *enhanced*), à `PUBLIC_URL/helius`.
3. À chaque transaction reçue, le bot vérifie qu'elle vient bien de Helius (en-tête
   `Authorization` = ton secret), répond tout de suite, puis cherche un **achat** :
   le wallet a dépensé des SOL et reçu un token qui n'est ni du SOL, ni de l'USDC, ni de l'USDT.
4. Il t'envoie alors une alerte :

   ```
   🟢 Baleine vient d'acheter
   🪙 MEME — Meme Coin
   💰 Montant : 1.500 SOL
   💵 Prix : 0.000001500 SOL (≈ $0.0002130)
   📄 Contrat : MeMe...pump
   🔗 DexScreener | GMGN | Transaction
   [ 🛒 Acheter ]  [ 🙈 Ignorer ]
   ```

   - **Acheter** ouvre la page d'échange SOL → token sur Jupiter (rien n'est acheté automatiquement).
   - **Ignorer** retire les boutons et marque l'alerte comme ignorée dans la base.
5. Si **au moins 2 wallets suivis** achètent le **même token en moins de 10 minutes**,
   l'alerte commence par **🚨🚨 SIGNAL FORT 🚨🚨** et liste tous les acheteurs.

Bon à savoir :
- Pendant une pause (`/pause`), les achats sont quand même enregistrés (pour le SIGNAL FORT),
  mais aucune alerte n'est envoyée.
- Helius envoie parfois deux fois la même transaction : le bot ne t'alerte qu'une seule fois.
- Le montant en SOL des achats pump.fun peut inclure de petits frais (c'est une estimation).
- Chaque création ou modification du webhook consomme des crédits Helius : le bot ne contacte
  Helius que si la liste des wallets ou l'adresse a vraiment changé.

---

## 🎯 Trades simulés : le bot contre tes choix

Sans dépenser un centime, le bot mesure si ses alertes (et tes choix) rapportent.

**Sous chaque alerte**, trois boutons :
- **✅ Accepter** : achat simulé dans ton portefeuille « 👤 Mes choix », au prix du moment du clic ;
- **🙈 Ignorer** : rien n'est acheté pour toi ;
- **🛒 Acheter sur Jupiter** : le vrai achat, si tu veux le faire toi-même (rien d'automatique).

Chaque clic est enregistré avec l'heure. Une fois décidé, Accepter et Ignorer disparaissent.

**Deux portefeuilles séparés :**
- **🤖 Bot seul** achète **toutes** les alertes dont la note du token atteint `/minscore` (un token
  DANGER compte pour 0 ; une note inconnue n'est achetée que si `/minscore` vaut 0) ;
- **👤 Mes choix** n'achète que les alertes que tu acceptes.

**Règles de chaque trade simulé** (section `simulation` de `rules.json`, modifiable sans redémarrer) :

| Réglage | Défaut |
|---------|--------|
| Mise | 0,05 SOL |
| Frais + glissement | 2 % à l'achat **et** 2 % à la vente |
| Prise de profit | quand le prix a monté de +100 % |
| Stop loss | quand le prix a baissé de 40 % |
| Sortie forcée | après 24 h (au dernier prix connu) |
| Vérification des prix | toutes les 60 s (DexScreener) |

**Prix de chaque alerte.** Pour **toutes** les alertes, même ignorées, le bot relève le prix au
signal, puis à 5 min, 1 h et 24 h. `/stats` s'en sert pour te montrer, par type d'alerte, ce que
fait le prix après l'alerte, et combien ont fait les alertes que tu as ignorées.

**`/stats`** compare côte à côte Bot seul et Mes choix (trades terminés, en cours, réussite, gain
moyen, perte moyenne, résultat en SOL et en euros), puis détaille par type d'alerte (wallet,
signal fort, dev, scanall, canal) et par wallet suivi. **`/journal`** montre les 10 derniers trades
terminés, **`/positions`** ceux encore ouverts (avec leur résultat s'ils étaient vendus maintenant).

Bon à savoir :
- Le prix d'entrée est le prix du marché au moment de l'alerte (ou du clic), plus les frais. Un
  token pas encore listé sur DexScreener (création pump.fun) est acheté dès qu'il a un prix ; sans
  prix pendant 15 min, le trade est annulé.
- Les résultats sont calculés à partir du prix en dollars du token : les variations du SOL lui-même
  sont ignorées. Les euros viennent du cours SOL/€ de CoinGecko.
- Les prix sont vérifiés toutes les minutes : un pic très court entre deux vérifications n'est pas vu.
- Une alerte sur le même token deux fois (ex : puis en SIGNAL FORT) donne deux trades simulés.
- Tout est dans la base du Volume : les positions ouvertes reprennent après un redémarrage. Si le
  bot était éteint au moment d'un relevé de prix (5 min, 1 h, 24 h), ce relevé reste vide.

---

## 🤖 L'IA : avis sur les alertes et revue de la semaine

Facultatif. Ajoute **une** de ces variables sur Railway :
- `XAI_API_KEY` pour **Grok** (clé créée sur console.x.ai), modèle `grok-4.7` ;
- `ANTHROPIC_API_KEY` pour **Claude** (clé créée sur platform.claude.com), modèle `claude-opus-5-5`.

Si les deux sont présentes, Grok est utilisé (mets `IA_FOURNISSEUR=claude` pour choisir Claude).
`IA_MODELE` permet de changer de modèle. Sans clé, le module est simplement désactivé.

### Avis sur chaque alerte

Pour chaque alerte dont la note du token atteint `/minscore`, l'IA joue trois rôles :
1. un **analyste haussier** donne ses meilleurs arguments pour acheter ;
2. un **analyste baissier**, en même temps et sans voir le premier, donne ses arguments contre ;
3. un **juge** lit les deux et conclut **🟢 ACHETER / 🟡 ATTENDRE / 🔴 IGNORER**, en une phrase.

L'alerte part tout de suite, sans attendre l'IA ; le verdict y est ajouté dès qu'il arrive
(quelques secondes à quelques dizaines de secondes) :

```
🤖 Avis IA : 🟡 ATTENDRE — Intéressant mais liquidité trop faible pour entrer maintenant.
🐂 Volume 1 h solide, holders répartis. (6/10)
🐻 Liquidité de 5 k$ seulement. (7/10)
```

Si tu as déjà appuyé sur Accepter ou Ignorer, l'avis arrive en réponse sous l'alerte. Le verdict
est enregistré avec l'alerte. Si l'IA ne répond pas, l'alerte affiche « Avis IA indisponible ».

### Revue de la semaine

Chaque **dimanche** (une heure après l'analyse des wallets, donc vers 11 h par défaut), ou à tout
moment avec **`/revue`**, l'IA reçoit les résultats des 7 derniers jours (trades simulés Bot seul
et Mes choix, par type d'alerte, par wallet, prix après les alertes, tes décisions) et les règles
actuelles. Elle propose des **changements de `rules.json`** et des **wallets à retirer**.

**Rien n'est appliqué automatiquement.** Chaque proposition arrive dans un message à part, avec
**✅ Accepter / ❌ Refuser**. Avant de te les montrer, le bot écarte celles qui sont
inapplicables (règle qui n'existe pas, valeur du mauvais genre, wallet que tu ne suis pas). Une
proposition dont la règle a changé entre-temps n'est pas appliquée.

### Versions des règles : `/rules` et `/rollback`

Les règles acceptées sont enregistrées **dans la base, sur le Volume** (un fichier du dossier du
code serait effacé à chaque déploiement Railway), par-dessus le `rules.json` du projet. Chaque
changement crée une version numérotée :
- `/rules` liste les dernières versions et ce qui a changé ; `/rules 3` détaille la version 3 ;
- `/rollback 3` remet les règles comme dans la version 3 ; `/rollback` seul revient à la version
  précédente. Un retour en arrière est lui-même une version : on peut toujours l'annuler.

Tu peux toujours modifier `rules.json` dans le projet : tes modifications s'appliquent, sauf sur
les réglages que tu as changés via une proposition acceptée (ceux-là restent ceux de la base,
jusqu'à un `/rollback`).

### Coûts et sécurité

- **Limite :** `IA_MAX_PAR_JOUR` appels par jour (200 par défaut). Une alerte en coûte 3, une
  revue 1. Quand la limite est atteinte, tu es prévenu une fois et les alertes continuent sans avis
  jusqu'au lendemain (minuit, fuseau `FUSEAU_HORAIRE`). Le compteur est visible dans `/status`.
- **Coût indicatif :** chaque appel envoie l'alerte et son analyse (quelques milliers de tokens).
  Surveille ta consommation sur la console de ton fournisseur et baisse `IA_MAX_PAR_JOUR` au besoin.
- Les noms de tokens et les messages des canaux sont écrits par des inconnus : l'IA a pour consigne
  de les traiter comme de simples données, jamais comme des instructions, et ses réponses sont
  vérifiées avant de t'être proposées. Garde quand même un œil critique : un avis de l'IA n'est pas
  un conseil financier.
- Avec Claude, si le modèle refuse une demande, l'API la relance automatiquement sur un autre
  modèle (repli côté serveur activé par défaut). Avec Grok, les conversations ne sont pas
  conservées chez xAI (`store: false`).

---

## 📡 Veille Telegram (avec ton compte, en lecture seule)

Le bot lit des canaux Telegram **avec ton propre compte** (bibliothèque Telethon), y repère les
adresses de tokens Solana, et t'alerte quand **un même token est cité dans au moins 2 canaux en
moins de 30 minutes**, avec la note du token et la liste des canaux (liens vers les messages).

### Étape 1 — Obtenir TG_API_ID et TG_API_HASH (une seule fois, 2 minutes)

1. Sur ton téléphone, ouvre **https://my.telegram.org** dans le navigateur.
2. Entre ton numéro au format international (ex : `+33612345678`) et touche **Next**.
3. Telegram t'envoie un code **dans l'application Telegram** (conversation « Telegram »).
   Recopie-le sur le site et touche **Sign In**.
4. Touche **API development tools**.
5. Remplis le formulaire « Create new application » :
   - **App title** : par exemple `Veille memecoins` ;
   - **Short name** : par exemple `veillememe` (5 à 32 lettres ou chiffres) ;
   - **Platform** : `Other` ; les autres champs peuvent rester vides.
6. Touche **Create application**. La page affiche :
   - **App api_id** : un nombre → c'est `TG_API_ID` ;
   - **App api_hash** : 32 caractères → c'est `TG_API_HASH`.

⚠️ Ces deux valeurs, comme ta session Telegram, donnent accès à ton compte : ne les partage avec
personne et ne les mets jamais dans le code ou sur GitHub.

### Étape 2 — Ajouter les variables sur Railway

Dans l'onglet **Variables** du service :

```
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
TGLOGIN_PASSWORD=une-longue-phrase-secrete-a-toi
```

`TGLOGIN_PASSWORD` protège la page de connexion (12 caractères minimum) et sert aussi à chiffrer
ta session dans la base. Si tu le changes plus tard, il faudra te reconnecter via `/tglogin`.
`TG_SESSION` n'est pas nécessaire.

### Étape 3 — Connecter ton compte depuis ton téléphone

1. Ouvre `https://ton-bot.up.railway.app/tglogin` dans le navigateur du téléphone.
2. Entre le mot de passe `TGLOGIN_PASSWORD` (5 essais au maximum par quart d'heure).
3. Entre ton numéro de téléphone, touche **Recevoir le code**.
4. Ouvre l'application Telegram : le code est dans la conversation « Telegram ». Reviens dans le
   navigateur et entre-le.
5. Si ton compte a un mot de passe (validation en deux étapes), entre-le.
6. C'est fini : la session est enregistrée **chiffrée** dans la base, le bot t'envoie
   « ✅ Veille Telegram connectée », et elle reprend toute seule à chaque redémarrage.

La même page permet de **déconnecter** le compte (la session est alors supprimée, y compris
côté Telegram). Tu peux aussi la révoquer depuis Telegram : Paramètres → Appareils.

### Étape 4 — Choisir les canaux

- `/addchannel https://t.me/nomducanal` (ou `@nomducanal`) :
  - si ton compte **est membre** du canal, ses messages sont lus **en direct** ;
  - si c'est un canal **public** que tu n'as pas rejoint, il est relu **toutes les minutes** ;
  - un canal **privé** doit d'abord être rejoint par toi, depuis ton téléphone, avec son lien
    d'invitation ; ensuite `/addchannel <lien d'invitation>` fonctionne.
- `/channels` liste les canaux, `/removechannel` en retire un, `/veille` montre l'état de la
  connexion et le nombre de messages lus depuis le démarrage.

### Lecture seule, garantie par le code

Le compte n'envoie jamais rien. Le client Telethon du bot **refuse avant tout envoi** toute
requête d'écriture : envoyer, transférer, modifier ou supprimer un message, réagir, rejoindre ou
quitter un canal, marquer comme lu, modifier le profil… Seules les requêtes de connexion (envoi du
code, déconnexion) sont permises. C'est aussi pour ça que le bot ne rejoint jamais un canal tout
seul. Seuls les messages des canaux suivis sont lus ; tes conversations privées sont ignorées.

### Détection des tokens

Le bot cherche les adresses Solana dans le texte **et** dans les liens cachés (« acheter ici »,
liens pump.fun, DexScreener, GMGN…), puis vérifie auprès de Helius que c'est bien un **token** (les
adresses de wallets, de pools ou les signatures de transactions sont ignorées). Les liens DexScreener
qui contiennent l'adresse de la paire au lieu de celle du token ne sont pas reconnus. Les alertes de
veille respectent `/pause` et `/minscore`. Si le token est cité dans un 3e canal, une nouvelle
alerte est envoyée.

---

## 🆕 Nouveaux tokens pump.fun et suivi des devs

Le bot reste connecté en permanence au flux gratuit **PumpPortal**, qui annonce chaque token créé
sur pump.fun (plusieurs par minute). Si la connexion coupe, ou si le flux reste muet 2 minutes, il
se reconnecte tout seul (après 1 s, puis 2 s, 4 s... jusqu'à 60 s). `/devs` et `/status` montrent
l'état du flux.

**Devs suivis.** Ajoute un créateur avec `/adddev <adresse> <surnom>`. Dès qu'il crée un token,
tu reçois une alerte **⚡⚡ PRIORITAIRE** en quelques secondes :

```
⚡⚡ PRIORITAIRE — nouveau token d'un dev suivi ⚡⚡
👤 Le Dev vient de créer un token
🪙 MONSTERBOY — Monsterboy
📄 Contrat : GQp4…pump
💰 Achat initial du dev : 1.284 SOL (4.4 % du supply)
📜 Bilan du dev : 12 token(s) lancé(s) · 3 > $100K · 1 rug(s) — 🟢 SUIVRE
🟢 Note token : 70/100 — BON
📊 MC … · Liq $5K · … (note du token, voir plus bas)
🔗 pump.fun | DexScreener | GMGN | Transaction
[ 🛒 Acheter ]  [ 🙈 Ignorer ]
```

À la seconde de sa création, un token est encore inconnu de DexScreener et de RugCheck : beaucoup
de critères de sa note sont donc « inconnus », et c'est indiqué. Les alertes des devs suivis ne
passent ni par `/minscore` ni par la limite par heure, mais elles respectent `/pause`.

**Créateurs inconnus.** Aucune alerte, sauf si tu actives `/scanall on 60`. Chaque nouveau token est
alors revérifié 2 minutes après sa création (DexScreener, gratuit) ; seuls ceux qui décollent
(market cap ≥ 10 000 $ et au moins 20 transactions en 5 minutes) sont analysés, et tu reçois une
alerte 🔭 si leur note atteint le score choisi. `/scanall` seul affiche les compteurs, `/scanall off`
désactive.

**Limite.** Au maximum **20 alertes de nouveaux tokens par heure** (réglable). Quand elle est
atteinte, tu reçois un seul message pour le signaler ; les devs suivis passent quand même.

**Bilan d'un créateur (`/devscore <adresse>`).**

| Étape | Source |
|-------|--------|
| Liste des tokens qu'il a créés sur pump.fun | Helius (recherche côté serveur des instructions « create » dans son historique, quelques dizaines de crédits) + tokens déjà vus par le bot |
| Market cap actuelle | DexScreener (30 tokens par requête) |
| Plus haute market cap | GeckoTerminal (prix journaliers, gratuit mais 30 requêtes / minute) |
| Rug | RugCheck (token signalé « rugged »), ou token monté au-dessus de 20 000 $ puis effondré de plus de 90 % |

Le résultat est vérifié sur ses 30 tokens les plus récents (le nombre total de tokens lancés compte
tout son historique) ; compte environ une minute. Les résultats sont gardés en base : un deuxième
`/devscore` est quasi instantané, et le bilan des devs suivis est recalculé une fois par jour.

Verdict (seuils dans la section `createurs` de `rules.json`) :
- 🔴 **ÉVITER** : au moins 30 % de rugs (ou 3 rugs), ou au moins 20 tokens lancés avec moins de 5 % de réussites ;
- 🟢 **SUIVRE** : au moins 20 % de ses tokens ont dépassé 100 000 $ de market cap, et moins de 10 % de rugs ;
- 🟡 **À SURVEILLER** : le reste, et tout créateur avec moins de 3 tokens vérifiables.

Limite à connaître : la plupart des « rugs » pump.fun sont des tokens que le dev revend avant qu'ils
ne décollent. Ils restent sous 20 000 $ et sont comptés comme « morts sans décoller », pas comme
rugs. Baisse `mc_min_usd` dans `rules.json` pour être plus sévère.

---

## 🧪 Note de sécurité d'un token

Chaque alerte d'achat contient maintenant, juste avant les liens :

```
🟢 Note token : 78/100 — BON
📊 MC $120K · Liq $30K · Vol 1h $33K · Top10 22 % · Âge 3 h
🚩 liquidité non bloquée (0 % bloquée) · RugCheck : Creator history of rugged tokens
❔ Inconnu : Pas de bundle
```

`/check <adresse>` donne le rapport complet pour n'importe quel token.

**Les données** (toutes gratuites, appelées en même temps, 2 à 6 secondes) :

| Source | Ce qu'on y prend |
|--------|------------------|
| DexScreener | market cap, liquidité, volume 5 min et 1 h, nombre d'achats et de ventes, âge de la paire |
| RugCheck | 20 plus gros holders (pools exclues), créateur et sa part, liquidité bloquée ou brûlée, risques connus |
| Helius (RPC) | mint et freeze authority (source de référence) ; secours pour les holders et le créateur si RugCheck ne répond pas |

**Bundle (faux holders).** Le bot regarde qui a envoyé ses premiers SOL à chacun des 10 plus
gros holders. Si au moins 2 d'entre eux ont été financés par **le même wallet dans les mêmes
24 heures**, c'est un bundle : une seule personne qui se fait passer pour plusieurs holders. Les
holders signalés comme « insiders » liés entre eux par RugCheck comptent aussi. Un exchange qui
finance beaucoup de wallets peut provoquer une fausse alerte : ajoute son adresse dans
`financeurs_ignores` de `rules.json`.

**La note** est la somme des points de chaque critère, ramenée sur 100. Tout se règle dans
**`rules.json`** (poids, paliers, seuils), sans toucher au code et sans redémarrer : le fichier est
relu à chaque analyse. S'il contient une erreur, le bot garde la dernière version correcte et
l'écrit dans les logs.

| Critère (défaut) | Points | Meilleure valeur |
|------------------|-------:|------------------|
| Liquidité | 15 | ≥ 50 000 $ |
| Volume 1 h | 10 | ≥ 100 000 $ |
| Part d'achats sur 1 h | 5 | ≥ 60 % |
| Transactions sur 5 min | 5 | ≥ 100 |
| Âge de la paire | 5 | ≥ 24 h |
| Mint authority révoquée | 15 | oui |
| Freeze authority révoquée | 10 | oui |
| Liquidité bloquée ou brûlée | 10 | ≥ 95 % |
| Top 10 holders (hors pools) | 15 | ≤ 20 % |
| Part du créateur | 5 | ≤ 1 % |
| Pas de bundle | 5 | oui |

Niveaux : 🟢 **BON** à partir de 70, 🟡 **MOYEN** à partir de 45, 🔴 **FAIBLE** en dessous.

**⛔ DANGER**, quelle que soit la note : mint authority active, freeze authority active, ou plus
de **30 %** du supply chez le créateur.

**Données indisponibles.** Si une API est en panne ou si le token est trop récent pour être
listé, le critère s'affiche « inconnu », rapporte la moitié de ses points (`part_si_inconnu`) et
le rapport précise quelle source a manqué et pourquoi. Si plus de la moitié des points viennent
de critères inconnus, la note est marquée « peu fiable ».

**`/minscore`.** `/minscore 60` : tu ne reçois plus que les alertes dont la note du token atteint
60. Un token **DANGER compte pour 0** (il est donc toujours filtré dès que `/minscore` est
au-dessus de 0). Si l'analyse a complètement échoué, l'alerte est envoyée quand même, avec
« note inconnue » : mieux vaut une alerte sans note qu'une alerte perdue. Les achats filtrés
restent enregistrés (ils comptent pour le SIGNAL FORT).

**Crédits Helius.** Environ 1 à 3 crédits par token (autorités), plus environ 110 crédits pour la
détection de bundle (10 holders). Les financeurs déjà vus sont mémorisés, et une analyse est
réutilisée pendant 2 minutes.

---

## 🔬 Analyse d'un wallet (note et verdict)

L'analyse est lancée quand tu ajoutes un wallet (`/addwallet`), quand tu tapes
`/score <adresse>` (le wallet n'est alors **pas** suivi), et chaque **dimanche à 10 h**
pour tous les wallets suivis. Si elle dure plus de 2 secondes, le bot envoie d'abord
« 🔎 Analyse en cours… », puis remplace ce message par le rapport.

**1. L'historique.** Le bot lit, page par page, les transactions des 30 derniers jours
(5 000 au maximum). Il demande à Helius (`getTransactionsForAddress`) **uniquement les
transactions réussies où le wallet a envoyé ou reçu un token**. C'est indispensable pour les
gros traders : leur adresse apparaît dans des milliers de transactions d'autres personnes, le
plus souvent échouées, qui noieraient leurs vrais trades. Si cette méthode n'est pas disponible
avec ton offre Helius, le bot se rabat sur l'API Enhanced Transactions (moins précise pour ces
wallets). Si la limite de 5 000 est atteinte avant 30 jours, le rapport indique la période
réellement couverte (en heures si elle fait moins de 2 jours).

**2. Achat ou vente ?** Le type annoncé par Helius (SWAP, BUY...) n'est pas utilisé. Pour
chaque transaction, le bot calcule la **variation de solde du wallet** : par token, et en SOL
(SOL natif + SOL « emballé » WSOL, frais réseau exclus). Token reçu + SOL dépensé = **achat** ;
token envoyé + SOL reçu = **vente** ; tout le reste est ignoré (transfert, airdrop, achat en
USDC, échange token contre token...). Ça marche de la même façon sur pump.fun, PumpSwap,
Raydium, Jupiter, Meteora... `/debugtx <adresse>` montre ce que le bot a compris de chaque
transaction.

**3. Les trades.** Les achats et les ventes de chaque token sont appariés selon la méthode
**FIFO** (premier acheté = premier vendu), ventes partielles comprises. Un trade est terminé
quand tout le token acheté a été revendu (à 1 % près, pour la « poussière »). Le bot calcule le
nombre de trades, le taux de réussite, le gain moyen, la perte moyenne, le résultat total en SOL
et la durée de détention (médiane et moyenne ; pour un trade en plusieurs achats/ventes, c'est
la durée moyenne pendant laquelle chaque token a été gardé). Les tokens encore détenus sont
comptés comme « positions ouvertes ». Les ventes de tokens achetés avant les 30 jours sont
ignorées (prix d'achat inconnu).

**4. Les comportements à risque** (🚩 = détecté, ✅ = rien de suspect, ❔ = pas pu vérifier) :

| Risque | Comment c'est détecté |
|--------|-----------------------|
| Sniper / initié | Il achète dans le **tout premier bloc** du token (le bloc de sa création) : soit un robot sniper, soit quelqu'un qui savait à l'avance. Signalé si c'est au moins 20 % de ses tokens |
| Revend sur ses copieurs | Juste après son achat, une **ruée d'acheteurs** (au moins 3 en 2 minutes, plus de 2 fois le rythme normal), et il revend pendant qu'ils achètent encore. Signalé si c'est au moins 30 % de ses trades rapides et gagnants |
| Wallet récent | Sa toute première transaction date de **moins de 14 jours** |

**5. SCALPEUR.** Si la durée de détention médiane est **inférieure à 2 minutes**, le wallet est
marqué ⚡ SCALPEUR : impossible de le copier à la main, ses alertes arrivent trop tard.
Un SCALPEUR n'est jamais en SUIVRE.

**6. La note sur 100.**

| Critère | Points |
|---------|--------|
| Réussite (70 % de trades gagnants ou plus = maximum) | 30 |
| Rentabilité (total des gains ÷ total des pertes ; 3 ou plus = maximum) | 30 |
| Résultat total positif | 10 |
| Activité (20 trades ou plus = maximum), seulement si le wallet gagne | 10 |
| Copiabilité (détention médiane ≥ 10 min = maximum, < 2 min = 0), seulement si le wallet gagne | 20 |
| Malus : sniper −20, revend sur ses copieurs −25, wallet récent −15 (−25 s'il a moins de 3 jours) | |

**7. Le verdict.** 🟢 **SUIVRE** à partir de 65, 🟡 **À SURVEILLER** de 40 à 64, 🔴 **ÉVITER**
en dessous de 40. Un SCALPEUR ou un wallet perdant sur la période ne peut pas être en SUIVRE.
Avec **moins de 10 trades terminés**, il n'y a pas de note : le verdict est
⚪ **DONNÉES INSUFFISANTES**.

**Chaque dimanche**, tu reçois un résumé (note et verdict de chaque wallet, avec l'ancien
verdict s'il a changé). Si un wallet **passe en ÉVITER**, tu reçois en plus une alerte avec
son rapport complet. Tous les seuils sont réglables en haut de `modules/analyse_wallet.py`.

**Crédits Helius.** `getTransactionsForAddress` coûte 10 crédits par tranche de 100
transactions : jusqu'à 500 crédits pour 5 000 transactions, plus quelques centaines de crédits
pour les vérifications de risques. En repli sur l'API Enhanced, c'est 100 crédits par page de
100 transactions (jusqu'à 5 000 crédits).

---

## 🚀 Déploiement sur Railway, étape par étape

### Étape 1 — Créer ton bot Telegram et récupérer le token

1. Dans Telegram, ouvre une conversation avec **@BotFather**.
2. Envoie `/newbot`, choisis un nom puis un identifiant qui finit par `bot` (ex : `mon_memecoin_bot`).
3. BotFather te donne un **token** qui ressemble à `123456789:AAH...`. Garde-le précieusement :
   c'est ton `TELEGRAM_TOKEN`.

### Étape 2 — Récupérer ton chat ID

1. Dans Telegram, ouvre une conversation avec **@userinfobot** et envoie-lui n'importe quel message.
2. Il te répond avec ton **Id** (un nombre comme `123456789`). C'est ton `TELEGRAM_CHAT_ID`.
3. Ouvre ensuite la conversation avec **ton propre bot** (lien donné par BotFather) et appuie sur
   **Démarrer**. Sans ça, ton bot n'a pas le droit de t'écrire en premier.

### Étape 3 — Récupérer ta clé Helius

1. Crée un compte gratuit sur [dashboard.helius.dev](https://dashboard.helius.dev).
2. Dans la section **API Keys**, copie ta clé. C'est ton `HELIUS_API_KEY`.

### Étape 4 — Mettre le code sur GitHub

Le code doit être dans un dépôt GitHub (c'est déjà le cas si tu lis ce fichier sur GitHub).
Vérifie que le fichier `.env` n'y est **pas** (il est exclu grâce au `.gitignore`).

### Étape 5 — Créer le projet sur Railway

1. Crée un compte sur [railway.com](https://railway.com) (connexion avec GitHub, c'est le plus simple).
2. Clique sur **New Project** → **Deploy from GitHub repo**.
3. Autorise Railway à accéder à ton GitHub si on te le demande, puis choisis ce dépôt.
4. Railway lance un premier déploiement. **Il va échouer, c'est normal** : les variables ne sont
   pas encore configurées. On s'en occupe maintenant.

### Étape 6 — Ajouter un volume (pour que la base de données ne soit pas effacée)

Sans volume, le fichier SQLite est **effacé à chaque redéploiement**.

1. Dans ton projet Railway, fais un **clic droit sur ton service** (ou utilise le bouton **+ Create**
   / la palette `Ctrl+K`) et choisis **Volume** / **Attach volume**.
2. Indique comme **Mount path** : `/data`
3. Valide.

### Étape 7 — Ajouter les variables d'environnement

1. Clique sur ton service, puis sur l'onglet **Variables**.
2. Ajoute ces variables une par une avec **New Variable** (ou en bloc avec **Raw Editor**) :

   ```
   TELEGRAM_TOKEN=le_token_de_botfather
   TELEGRAM_CHAT_ID=ton_id_telegram
   HELIUS_API_KEY=ta_cle_helius
   DB_PATH=/data/bot.db
   HELIUS_WEBHOOK_SECRET=une_longue_phrase_secrete
   ```

   ⚠️ `DB_PATH` doit commencer par le *Mount path* du volume (`/data`).

   Pour `HELIUS_WEBHOOK_SECRET`, invente une longue suite de caractères (au moins 30), ou
   génère-la avec : `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Ne la partage avec personne : elle prouve que les requêtes reçues viennent bien de Helius.
3. Clique sur **Deploy** (ou **Apply changes**) : Railway redéploie automatiquement.

### Étape 8 — Vérifier que tout marche

1. Onglet **Deployments** → clique sur le dernier déploiement → **Deploy Logs**.
   Tu dois voir des lignes comme :
   ```
   Base de données prête : /data/bot.db
   Bot Telegram démarré
   Boucle de surveillance démarrée
   ```
2. Sur Telegram, tu reçois le message **🚀 Bot démarré.**
3. Envoie `/status` à ton bot : il te répond avec son état.

### Étape 9 — Obtenir une adresse web publique (indispensable pour les alertes)

Helius a besoin d'une adresse publique pour envoyer les transactions à ton bot :

1. Clique sur ton service → onglet **Settings** → section **Networking** → **Generate Domain**.
2. Tu obtiens une adresse du type `https://mon-bot.up.railway.app`.
3. Dans l'onglet **Variables**, ajoute :
   ```
   PUBLIC_URL=https://mon-bot.up.railway.app
   ```
4. Test : ouvre `https://mon-bot.up.railway.app/` dans ton navigateur → `Blot-nsm en ligne`.
   Puis `https://mon-bot.up.railway.app/health` → l'état de chaque module (voir plus bas).

#### Le « target port » (Settings → Networking)

Railway reçoit les visiteurs sur l'adresse publique et les envoie vers **un port** de ton
application : c'est le *target port*, affiché à côté du domaine (« → Port 8080 » par exemple).
Le bot, lui, écoute sur le port donné par la variable `PORT`, que Railway fournit tout seul.
**Les deux doivent être identiques**, sinon l'adresse publique affiche « Application failed to
respond » alors que le déploiement est vert.

Pour vérifier :
1. Onglet **Deployments** → dernier déploiement → **Deploy Logs**. Cherche la ligne
   `Serveur web en écoute sur 0.0.0.0:XXXX` (ou `Uvicorn running on http://0.0.0.0:XXXX`).
2. **Settings** → **Networking** → le domaine public : le port affiché doit être ce même `XXXX`.
   Sinon, clique sur le domaine (icône crayon) et mets ce numéro comme port.
3. N'ajoute **pas** toi-même de variable `PORT` dans l'onglet Variables, sauf pour forcer une
   valeur précise : dans ce cas, mets exactement le même numéro comme *target port*.

Le serveur web ouvre son port **dès le lancement**, avant même que le bot Telegram soit connecté :
l'adresse publique répond donc tout de suite, même si Telegram ou Helius sont lents.

#### La page `/health`

Elle répond toujours (code 200) tant que le serveur tourne : c'est ce que Railway vérifie après
chaque déploiement. Le champ `statut` vaut `ok`, ou `degrade` si un module a un problème, et
`modules` détaille chacun : serveur web, base de données, bot Telegram (avec l'erreur s'il ne
démarre pas), alertes (pause, `/minscore`), webhook Helius, flux pump.fun et `/scanall`, veille
Telegram, et l'état de chaque tâche de fond (`en cours`, `arrêtée`, `plantée (...)`). Elle est
publique mais ne montre que des états et des compteurs, aucun secret.

**Tu n'as rien à configurer chez Helius** : le bot crée lui-même le webhook dès que tu ajoutes
un premier wallet, avec cette configuration :

| Champ | Valeur |
|-------|--------|
| `webhookURL` | `PUBLIC_URL` + `/helius` |
| `webhookType` | `enhanced` |
| `transactionTypes` | `HELIUS_TRANSACTION_TYPES` (par défaut `["ANY"]`) |
| `accountAddresses` | tous les wallets suivis |
| `authHeader` | `HELIUS_WEBHOOK_SECRET` |

À chaque démarrage, le bot cherche s'il existe déjà un webhook pour cette URL : si oui, il le
**met à jour** (il n'en crée jamais un second, et supprime les doublons éventuels). Tu peux le
voir sur [dashboard.helius.dev/webhooks](https://dashboard.helius.dev/webhooks) ou avec `/webhook`.

**Pourquoi `ANY` et pas `SWAP` ?** Helius ne classe pas les achats pump.fun en `SWAP` : sur
PumpSwap ils sont classés `BUY`, et sur la courbe de lancement pump.fun ils sont souvent
`UNKNOWN`. Avec `SWAP` seul, ces achats ne seraient jamais reçus. Le bot reçoit donc toutes les
transactions de tes wallets et trie lui-même les achats. Chaque transaction reçue coûte 1 crédit
Helius : pour en économiser (en perdant les achats sur la courbe pump.fun), tu peux mettre
`HELIUS_TRANSACTION_TYPES=SWAP,BUY`.

### Étape 10 — Ajouter tes premiers wallets

Sur Telegram, envoie à ton bot :

```
/addwallet 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU Baleine
```

Le bot répond « ✅ Wallet ajouté » puis « 🔔 Webhook Helius à jour ». Tape `/wallets` pour
voir la liste, et `/webhook` pour vérifier que tout est ✅. Quelques secondes plus tard, tu
reçois le rapport d'analyse du wallet. Si tu vois « ⚠️ PUBLIC_URL est
vide » ou « HELIUS_WEBHOOK_SECRET est vide », ajoute la variable sur Railway (étapes 7 et 9).

### Mettre à jour le bot

Chaque `git push` sur la branche connectée à Railway relance automatiquement un déploiement.
Les données de la base sont conservées grâce au volume.

---

## 💻 Lancer le bot sur ton ordinateur (optionnel)

Il faut **Python 3.11** installé.

```bash
# 1. Créer un environnement virtuel et l'activer
python3.11 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate

# 2. Installer les bibliothèques
pip install -r requirements.txt

# 3. Créer le fichier .env à partir du modèle, puis le remplir
cp .env.example .env               # Windows : copy .env.example .env

# 4. Lancer
python main.py
```

Le serveur web est alors accessible sur <http://localhost:8000/health>.

⚠️ **N'exécute pas le bot en local pendant qu'il tourne sur Railway** avec le même token :
Telegram n'autorise qu'une seule connexion à la fois et tu verras l'erreur
`Conflict: terminated by other getUpdates request`. Pour la même raison, garde **1 seule
instance** (replica) sur Railway.

---

## 🛡️ Le bot ne plante jamais en entier

Chaque partie du bot (« module ») tourne isolée. Si l'une a un problème, **seule elle s'arrête** :
les autres continuent, et le serveur web répond toujours.

| Situation | Ce qui se passe |
|-----------|-----------------|
| Variable obligatoire manquante (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`) | Le module qui en a besoin est désactivé (sans Telegram, le bot ne peut rien envoyer, mais `/health` explique pourquoi) |
| Variable mal écrite (`ANALYSE_JOURS=trente`, `"1234567"` entre guillemets, `10,5`...) | Les espaces et guillemets sont retirés ; une valeur invalide est remplacée par sa valeur par défaut et signalée |
| Veille Telegram non configurée ou non connectée | Veille désactivée ou « en attente », le reste tourne |
| Bibliothèque absente, module qui ne se charge pas | Ce module est désactivé ; ses commandes répondent « Commande indisponible » au lieu de se taire |
| Module qui plante en cours de route | Il est relancé tout seul (après 30 s, puis 1 min, 2 min... jusqu'à 10 min) |
| Base de données inaccessible (`DB_PATH`, volume) | Marquée en erreur ; le serveur web reste en ligne pour l'expliquer |
| Telegram injoignable au démarrage | Nouveaux essais automatiques (5 s, 10 s... jusqu'à 5 min) |

Où voir l'état de chaque module :
- **`/status`** sur Telegram : section « Modules » (✅ actif, ⏳ en attente, ⏸️ désactivé, ❌ erreur,
  avec la raison) et section « Variables à corriger sur Railway » ;
- **`/health`** dans le navigateur : la même chose au format JSON (`statut` = `ok` ou `degrade`) ;
- le **message « 🚀 Bot démarré »** liste les modules désactivés et les variables à corriger.

## 🛠️ Problèmes fréquents

| Symptôme | Solution |
|----------|----------|
| Le service est **CRASHED** sur Railway | Ouvre **Deploy Logs** et regarde les dernières lignes (`Traceback`, `Error`). Depuis cette version, une variable ou un module en panne ne fait plus planter le bot : un plantage complet vient d'une erreur de code ou d'une installation ratée (onglet **Build Logs**) |
| L'adresse publique affiche « Application failed to respond » alors que le déploiement est vert | Le *target port* du domaine ne correspond pas au port d'écoute : voir « Le target port » à l'étape 9 |
| `/health` : `bot_telegram` en `erreur` « TELEGRAM_TOKEN refusé » | Le token est faux : recopie-le depuis @BotFather (le serveur web, lui, reste en ligne) |
| `/health` : `bot_telegram` en `nouvel essai` | Telegram injoignable pour l'instant : le bot réessaie tout seul (5 s, 10 s, 20 s... jusqu'à 5 min) |
| `ERREUR : la variable d'environnement ... est manquante` | Ajoute la variable dans l'onglet *Variables* de Railway |
| `The token ... was rejected by the server` | Le `TELEGRAM_TOKEN` est faux : recopie-le depuis @BotFather |
| Pas de message « 🚀 Bot démarré » | Vérifie `TELEGRAM_CHAT_ID` et que tu as bien appuyé sur *Démarrer* dans la conversation avec ton bot |
| Le bot ne répond pas à `/status` | Il ne répond qu'au chat ID configuré : vérifie que c'est bien le tien |
| Les données disparaissent après un redéploiement | Le volume n'est pas monté, ou `DB_PATH` ne commence pas par `/data` |
| `Conflict: terminated by other getUpdates request` | Le bot tourne à deux endroits en même temps (local + Railway, ou 2 replicas) |
| `⚠️ Webhook Helius NON mis à jour : la variable PUBLIC_URL est vide` | Génère un domaine Railway et ajoute `PUBLIC_URL` (étape 9) |
| `⚠️ ... HELIUS_WEBHOOK_SECRET est vide` | Ajoute la variable `HELIUS_WEBHOOK_SECRET` (étape 7) |
| `❌ Erreur Helius ... 400 ... transaction type` | Vérifie `HELIUS_TRANSACTION_TYPES` (ex : `ANY` ou `SWAP,BUY`), puis `/webhook sync` |
| `❌ Erreur Helius ... 401` | `HELIUS_API_KEY` est fausse |
| `❌ Erreur Helius ... 429` ou limite de webhooks | Trop de requêtes, ou trop de webhooks sur ton compte Helius (5 en gratuit) : supprime les anciens sur le dashboard Helius |
| `❌ Analyse impossible ... 401` / `403` | Clé Helius fausse, ou méthode non disponible avec ton offre Helius |
| `/tglogin` affiche « Veille Telegram désactivée » | Ajoute `TG_API_ID`, `TG_API_HASH` et `TGLOGIN_PASSWORD` (12 caractères minimum) |
| `/veille` : « session illisible » | `TGLOGIN_PASSWORD` ou `TG_API_HASH` a changé : reconnecte-toi sur `/tglogin` |
| `/veille` : « session expirée ou révoquée » | La session a été fermée depuis Telegram (Appareils) : reconnecte-toi sur `/tglogin` |
| `/addchannel` : « canal privé non rejoint » | Rejoins le canal depuis ton téléphone, puis recommence |
| Risques marqués ❔ « non vérifié » | Helius n'a pas répondu à `getTransactionsForAddress` : la note est calculée sans ces vérifications (aucun malus) |
| L'analyse du dimanche n'arrive pas | Le bot doit être allumé le dimanche après 10 h. Vérifie les logs : `Analyse hebdomadaire de N wallet(s)` |
| Wallet ajouté mais aucune alerte | Vérifie dans les logs Railway la ligne `Webhook Helius reçu`. Si elle n'apparaît jamais, regarde les logs du webhook sur le dashboard Helius. Tape `/webhook` : tout doit être ✅. Si tu vois `Requête refusée sur /helius` dans les logs, le secret chez Helius est différent : tape `/webhook sync` |

---

## ➕ Et ensuite ?

- Suivre les devs (table `devs_suivis`) sur le même principe que `modules/wallets.py`.
- Alerter aussi sur les ventes des wallets suivis.
- Ouvrir automatiquement un trade simulé à chaque SIGNAL FORT (`db.ouvrir_trade`) pour mesurer
  la qualité des signaux.
- Ne plus envoyer les alertes d'achat des wallets en ÉVITER (ou des SCALPEURS).
- Toujours vérifier `db.est_en_pause()` avant d'envoyer une alerte, pour respecter `/pause`.
