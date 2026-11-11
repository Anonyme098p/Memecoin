# 🤖 Bot d'alertes memecoins Solana

Bot Telegram personnel qui surveillera des wallets et des devs Solana et t'enverra des alertes.
Il est prévu pour tourner 24h/24 sur [Railway](https://railway.com).

> **État actuel :** le suivi des wallets fonctionne. Dès qu'un wallet suivi achète un token
> (SOL → token), tu reçois une alerte Telegram, et un **SIGNAL FORT** si plusieurs wallets
> suivis achètent le même token en peu de temps. Le suivi des devs reste à écrire.

---

## 📁 Structure du projet

```
.
├── main.py              # Point d'entrée : démarre le serveur web + le bot Telegram
├── config.py            # Lit toutes les variables d'environnement
├── telegram_bot.py      # Le bot Telegram et ses commandes
├── db.py                # Base de données SQLite (tables + fonctions utiles)
├── modules/
│   ├── wallets.py       # Suivi des wallets : commandes, détection des achats, alertes
│   ├── helius.py        # Appels à l'API Helius (webhook, infos d'un token)
│   ├── tokens.py        # Nom, symbole et prix d'un token (Helius + DexScreener)
│   ├── webhooks.py      # Routes du serveur web (FastAPI), dont /helius
│   ├── alertes.py       # Envoi d'une alerte générique (pause, anti-doublon, historique)
│   └── surveillance.py  # Boucle qui tourne en fond (pour de futures fonctionnalités)
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
| `/addwallet <adresse> <surnom>` | Suit un wallet (le surnom peut contenir des espaces). Le webhook Helius est mis à jour tout seul |
| `/removewallet <adresse>` | Arrête de suivre un wallet (et met à jour le webhook Helius) |
| `/wallets` | Liste des wallets suivis |
| `/webhook` | État du webhook Helius : URL, type, transactions, actif ou non, adresses synchronisées, secret identique, dernière réception |
| `/webhook sync` | Force la resynchronisation du webhook Helius, puis affiche son état |

🔒 Le bot **ne répond qu'à ton chat** (`TELEGRAM_CHAT_ID`). Les autres personnes sont ignorées.

### Tables de la base de données

| Table              | Contenu |
|--------------------|---------|
| `wallets_suivis`   | Wallets Solana surveillés (adresse, surnom) |
| `devs_suivis`      | Devs (créateurs de tokens) surveillés, avec une note |
| `alertes_envoyees` | Historique des alertes : heure, wallet, montant, **prix en SOL et en $ au moment du signal**, statut (envoyée / ignorée) |
| `achats_wallets`   | Chaque achat détecté (même bot en pause) : sert à repérer les SIGNAUX FORTS |
| `trades_simules`   | Trades fictifs : mise en SOL, prix d'entrée/sortie, gain/perte |
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
   Si Railway demande un port, laisse celui proposé (le bot écoute sur la variable `PORT`).
2. Tu obtiens une adresse du type `https://mon-bot.up.railway.app`.
3. Dans l'onglet **Variables**, ajoute :
   ```
   PUBLIC_URL=https://mon-bot.up.railway.app
   ```
4. Test : ouvre `https://mon-bot.up.railway.app/health` dans ton navigateur → `{"statut":"ok",...}`.

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
voir la liste, et `/webhook` pour vérifier que tout est ✅. Si tu vois « ⚠️ PUBLIC_URL est
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

## 🛠️ Problèmes fréquents

| Symptôme | Solution |
|----------|----------|
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
| Wallet ajouté mais aucune alerte | Vérifie dans les logs Railway la ligne `Webhook Helius reçu`. Si elle n'apparaît jamais, regarde les logs du webhook sur le dashboard Helius. Tape `/webhook` : tout doit être ✅. Si tu vois `Requête refusée sur /helius` dans les logs, le secret chez Helius est différent : tape `/webhook sync` |

---

## ➕ Et ensuite ?

- Suivre les devs (table `devs_suivis`) sur le même principe que `modules/wallets.py`.
- Alerter aussi sur les ventes des wallets suivis.
- Ouvrir automatiquement un trade simulé à chaque SIGNAL FORT (`db.ouvrir_trade`) pour mesurer
  la qualité des signaux.
- Toujours vérifier `db.est_en_pause()` avant d'envoyer une alerte, pour respecter `/pause`.
