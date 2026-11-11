# 🤖 Bot d'alertes memecoins Solana

Bot Telegram personnel qui surveillera des wallets et des devs Solana et t'enverra des alertes.
Il est prévu pour tourner 24h/24 sur [Railway](https://railway.com).

> **État actuel :** la base du projet est prête (bot Telegram, base de données, serveur web,
> boucle de surveillance). La logique de détection (appels à l'API Helius) reste à écrire dans
> `modules/surveillance.py` et `modules/webhooks.py`.

---

## 📁 Structure du projet

```
.
├── main.py              # Point d'entrée : démarre le serveur web + le bot Telegram
├── config.py            # Lit toutes les variables d'environnement
├── telegram_bot.py      # Le bot Telegram et ses commandes
├── db.py                # Base de données SQLite (tables + fonctions utiles)
├── modules/
│   ├── alertes.py       # Envoi d'une alerte (pause, anti-doublon, historique)
│   ├── surveillance.py  # Boucle qui tourne en fond (logique Helius à ajouter)
│   └── webhooks.py      # Routes du serveur web (FastAPI)
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

🔒 Le bot **ne répond qu'à ton chat** (`TELEGRAM_CHAT_ID`). Les autres personnes sont ignorées.

### Tables de la base de données

| Table              | Contenu |
|--------------------|---------|
| `wallets_suivis`   | Wallets Solana surveillés (adresse, surnom) |
| `devs_suivis`      | Devs (créateurs de tokens) surveillés, avec une note |
| `alertes_envoyees` | Historique des alertes envoyées (sert aussi à éviter les doublons) |
| `trades_simules`   | Trades fictifs : mise en SOL, prix d'entrée/sortie, gain/perte |
| `parametres`       | Petits réglages (ex : pause on/off) |

### Variables d'environnement

| Variable                  | Obligatoire | Description |
|---------------------------|:-----------:|-------------|
| `TELEGRAM_TOKEN`          | ✅ | Token du bot, donné par @BotFather |
| `TELEGRAM_CHAT_ID`        | ✅ | Ton identifiant Telegram (un nombre) |
| `HELIUS_API_KEY`          | ✅ | Clé API Helius |
| `DB_PATH`                 | ✅ sur Railway | Chemin du fichier SQLite. Sur Railway : `/data/bot.db` |
| `WEBHOOK_SECRET`          | ❌ | Secret attendu dans l'en-tête `Authorization` des webhooks |
| `INTERVALLE_SURVEILLANCE` | ❌ | Secondes entre deux tours de surveillance (défaut : 60) |
| `LOG_LEVEL`               | ❌ | `DEBUG`, `INFO` (défaut), `WARNING`, `ERROR` |
| `PORT`                    | ❌ | Fourni automatiquement par Railway, ne pas le définir |

⚠️ **Ne mets jamais tes clés dans le code ni sur GitHub.** Elles vont uniquement dans
l'onglet *Variables* de Railway (ou dans un fichier `.env` local, qui est ignoré par Git).

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
   ```

   ⚠️ `DB_PATH` doit commencer par le *Mount path* du volume (`/data`).
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

### Étape 9 (optionnel, pour plus tard) — Obtenir une adresse web publique

Pour recevoir des webhooks (par exemple de Helius), il faut une URL publique :

1. Clique sur ton service → onglet **Settings** → section **Networking** → **Generate Domain**.
2. Tu obtiens une adresse du type `https://mon-bot.up.railway.app`.
3. Test : ouvre `https://mon-bot.up.railway.app/health` dans ton navigateur → `{"statut":"ok",...}`.
4. L'URL à donner à Helius sera : `https://mon-bot.up.railway.app/webhooks/helius`.
   Pour la sécuriser, ajoute une variable `WEBHOOK_SECRET` sur Railway et mets la même valeur
   dans le champ *Authorization header* du webhook côté Helius.

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

---

## ➕ Et ensuite ?

- Écrire dans `modules/surveillance.py` les appels à l'API Helius pour détecter les achats des wallets suivis.
- Traiter les webhooks Helius dans `modules/webhooks.py`.
- Ajouter des commandes Telegram (ex : `/addwallet <adresse> <nom>`) dans `telegram_bot.py`
  en utilisant les fonctions déjà prêtes de `db.py` (`ajouter_wallet`, `ajouter_dev`, `ouvrir_trade`...).
- Toujours envoyer les alertes via `modules.alertes.envoyer_alerte()` pour respecter `/pause`.
