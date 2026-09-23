[README.md](https://github.com/user-attachments/files/32576973/README.md)
# HyperBot Web — déploiement GitHub + Railway

Version web (sans interface Tkinter) du bot de trading, avec l'interface
`index.html` fournie branchée sur une vraie API FastAPI et une base SQLite.

Version courante : **4.276** (visible dans `/health` et dans les logs de démarrage).

## 1. Structure du projet

```
hyperbot_web/
├── api.py           # API FastAPI (tous les endpoints consommés par index.html)
├── bot_engine.py     # Moteur de trading (extrait du bot Tkinter, sans dépendance graphique)
├── db.py            # Persistance SQLite (utilisateurs, trades, config)
├── auth.py          # Hash de mot de passe + tokens de session (JWT)
├── index.html        # Interface web (React, servie directement par l API)
├── requirements.txt
├── Procfile          # Commande de démarrage Railway
└── .gitignore
```

## 2. Tester en local avant de déployer

```bash
python3 -m venv venv
source venv/bin/activate          # Windows : venv\Scripts\activate
pip install -r requirements.txt

export HYPERBOT_SECRET_KEY="change-moi-en-une-longue-chaine-aleatoire"
export HYPERBOT_DATA_DIR="./data"

uvicorn api:app --reload --port 8000
```

Ouvre `http://localhost:8000` — tu dois voir l'écran de connexion. Crée ton
compte (premier et unique compte autorisé, voir section 5), connecte-toi,
puis clique sur DÉMARRER pour vérifier que le bot tourne (regarde les logs
dans le terminal et dans l'onglet Logs de l'interface).

## 3. Déploiement GitHub

```bash
cd hyperbot_web
git init
git add .
git commit -m "HyperBot Web v1"
git branch -M main
git remote add origin https://github.com/TON_COMPTE/hyperbot-web.git
git push -u origin main
```

Le `.gitignore` exclut déjà la base SQLite, les fichiers de capital/session
et les logs — ils ne doivent jamais être versionnés (données personnelles +
état runtime, pas du code).

## 4. Déploiement Railway

1. Sur [railway.app](https://railway.app), **New Project → Deploy from GitHub repo** → sélectionne `hyperbot-web`.
2. Railway détecte le `Procfile` automatiquement (sinon Settings → Start Command : `uvicorn api:app --host 0.0.0.0 --port $PORT`).
3. **Variables d'environnement** (Settings → Variables) :

| Variable | Rôle |
|---|---|
| `HYPERBOT_SECRET_KEY` | **Obligatoire (32 caractères minimum).** Le serveur refuse de démarrer sans elle. Générer : `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `HYPERBOT_DATA_DIR` | `/data` (voir volume ci-dessous) |
| `HYPERBOT_PRIVATE_KEY` | Clé privée Hyperliquid (mode live) |
| `HYPERBOT_WALLET_ADDRESS` | Adresse du wallet Hyperliquid |
| `HYPERBOT_FINNHUB_API_KEY` | Clé Finnhub (optionnel, blackout CPI) |

4. **Volume persistant (important)** — Settings → Volumes → *Add Volume*, monte-le sur `/data`. Sans ça, la base SQLite, le capital et les logs sont **remis à zéro à chaque redéploiement**. Avec le volume monté sur `/data` et `HYPERBOT_DATA_DIR=/data`, tout survit aux redéploiements.
5. Déploie. Railway te donne une URL (`https://xxxx.up.railway.app`) — c'est ton dashboard.

## 4bis. Clé API Hyperliquid — désormais obligatoire (paper ET live)

Depuis cette version, `HYPERBOT_PRIVATE_KEY` et `HYPERBOT_WALLET_ADDRESS` sont
**obligatoires**, y compris en mode paper : le bot n'utilise plus jamais de
prix simulés en interne. En mode paper, les prix et le flux WebSocket
viennent bien de Hyperliquid — seule la **passation d'ordres réels** reste
désactivée (aucun ordre n'est envoyé à l'exchange tant que `trading_mode`
n'est pas `live`). Sans ces deux variables, `/api/bot/start` répond
immédiatement avec une erreur 400 explicite plutôt que de démarrer un bot
qui échouerait silencieusement.

## 4ter. Le bot tourne côté serveur, indépendamment du navigateur

Le bot tourne **dans le process Railway**, pas dans ton navigateur : une
fois démarré, il continue de tourner même si tu fermes l'onglet ou éteins
ton PC. La page web n'est qu'une télécommande à distance.

**Redémarrage automatique après un redéploiement** — l'état souhaité
(DÉMARRÉ / ARRÊTÉ) est mémorisé en base à chaque clic sur DÉMARRER/ARRÊTER.
Au démarrage du process (redéploiement Railway, crash-restart, etc.), le
bot relit cet état :
- s'il était **DÉMARRÉ**, il redémarre automatiquement tout seul (aucune
  action requise sur la page web) ;
- s'il était **ARRÊTÉ** (tu as explicitement cliqué ARRÊTER), il reste
  arrêté même après un redéploiement, jusqu'à ce que tu recliques sur
  DÉMARRER.

Cliquer sur ARRÊTER depuis la page web arrête bien le vrai bot qui tourne
sur Railway (il n'y a qu'une seule instance, pas de simulation côté
navigateur) — et cet arrêt persiste across redéploiements, comme décrit
ci-dessus.

## 4quater. Vérifier que la persistance fonctionne vraiment

Va sur `https://TON_URL.up.railway.app/health` — tu verras :

```json
{
  "data_dir": "/data",
  "data_dir_configured": true,
  "boot_count": 3,
  ...
}
```

- `data_dir_configured: false` → tu n'as pas défini `HYPERBOT_DATA_DIR` sur Railway → rien ne persiste.
- `boot_count` doit **augmenter** (1, 2, 3...) à chaque redéploiement. S'il repart toujours à 1, le Volume n'est pas monté correctement même si la variable est définie (vérifie le point de montage dans Settings > Volumes).

## 4quinquies. Garder les actifs actifs/inactifs sans Volume

Comme pour les clés Hyperliquid/Finnhub, tu peux fixer les actifs actifs via
une variable d'environnement Railway plutôt que via l'interface web (ce qui
nécessiterait un Volume pour survivre à un redéploiement) :

| Variable | Exemple | Rôle |
|---|---|---|
| `HYPERBOT_ACTIVE_COINS` | `BTC,ETH,xyz:EUR` | Liste des actifs actifs, séparés par des virgules, parmi ceux de `SYMBOLS` (cryptos + forex `xyz:EUR`, `xyz:JPY`, `xyz:KRW`, `xyz:DXY`). La casse est normalisée (`XYZ:eur` → `xyz:EUR`). Absente = liste par défaut. |

Un changement fait depuis l'interface web reste prioritaire tant que le
process ne redémarre pas, mais **sans Volume, il est perdu au prochain
redéploiement** — la variable d'environnement, elle, survit toujours.

## 4sexies. Temps de fonctionnement réel (hors arrêts)

Le "temps de trading" affiché dans le bilan ne compte plus le temps écoulé
depuis un reset, mais le **temps réellement passé avec le bot démarré**
(`running_seconds`) — les périodes où il était arrêté (manuellement ou
suite à un redéploiement en attente de redémarrage) ne sont pas comptées.
Ce compteur est stocké en base (`total_running_seconds` / `running_since`)
et **nécessite donc lui aussi un Volume pour survivre aux redéploiements** —
sans Volume, il repart de zéro à chaque redéploiement comme le reste des
données dynamiques (trades, etc.), contrairement aux clés API qui peuvent
être fixées par variable d'environnement.

## 4septies. Indexation des trades et reprise après redémarrage

Chaque trade reçoit à l'ouverture un **identifiant unique** (`trade_uid`) qui
encode son **mode source** (Forex, Accumulation, Spot-Accum, Funding) et son
**heure d'ouverture**. Il est :

- écrit en base **avant** l'envoi de l'ordre (statut `pending`), puis complété
  dès l'ouverture confirmée (statut `open`) ;
- envoyé à Hyperliquid comme identifiant client de l'ordre d'entrée (`cloid`).

Au redémarrage, la reprise se fait dans cet ordre :

1. **Positions sauvegardées** (`hyperbot_positions.json`) restaurées à
   l'identique (mode, heure, niveaux, état du trailing). Une position live
   absente d'Hyperliquid est clôturée comme « fermée pendant la coupure ».
2. **Positions réelles non couvertes** : rattachées à leur trade via la base,
   puis via le `cloid` dans l'historique d'ordres Hyperliquid (fonctionne même
   si la base locale a été perdue).
3. En dernier recours seulement, une position inconnue est suivie en mode
   forex avec une **alerte explicite** dans les logs.

Si Hyperliquid est illisible au démarrage, rien n'est supprimé ni déduit.
Le fichier de positions est écrit de façon atomique et n'est plus vidé à la
lecture. Les DEX natif **et** `xyz` (forex) sont interrogés partout.

## 4octies. Fermetures réelles vérifiées

En live, toute fermeture (SL, TTP, retournement, objectif, conflit, fermeture
manuelle) envoie **d'abord** l'ordre à Hyperliquid et vérifie ensuite que la
position a réellement disparu. Le suivi interne n'est fermé qu'à cette
condition ; sinon la position reste suivie et la fermeture est retentée au
cycle suivant. Le mode paper/live est celui **de l'ouverture** de la position,
même si la stratégie a été rebasculée depuis.

## 4nonies. Suivi et export des trades Spot-Accum / Accumulation / Funding

Onglet **Historique** → bouton **📥 Télécharger le suivi (CSV)** (fichier
lisible directement par Excel : séparateur `;`, virgule décimale).

Pour chaque trade : mode, actif, sens, paper/live, heures d'ouverture et de
fermeture (heure de Paris), durée, motif de sortie, levier, marge et
notionnel, prix d'entrée et de sortie (avec leur source), mouvement de prix,
pic, PnL brut, frais, PnL net, puis le **comportement du prix après la
sortie** : prix à +30 et +60 min, meilleur mouvement dans l'heure et, pour les
trades perdants, si le prix est revenu au niveau d'entrée dans l'heure.

- Le suivi après sortie est calculé automatiquement ~1 h après chaque
  fermeture à partir des bougies Hyperliquid, dans un fil séparé (aucun
  impact sur le trading). Les trades déjà en base (jusqu'à ~16 jours) sont
  complétés rétroactivement, par lots, après le déploiement.
- En live, les frais et le PnL réels proviennent des remplissages
  Hyperliquid ; à défaut, les frais sont estimés (0,09 % du notionnel par
  aller-retour) et signalés comme tels.
- Depuis la 4.265, le PnL d'une sortie live est calculé sur le **prix
  réellement exécuté** par Hyperliquid (colonne « source prix sortie » =
  `hyperliquid`), et non plus sur le prix vu par le bot au moment de décider.

## 4decies. Flux de transactions (pression acheteurs / vendeurs)

Spot-Accum et Accumulation utilisent le flux WebSocket `trades` d'Hyperliquid
pour mesurer la pression directionnelle (−1 = 100 % vendeurs agressifs, +1 =
100 % acheteurs agressifs), calculée sur une **fenêtre de temps fixe**
(`TRADE_FLOW_WINDOW_SEC`, 180 s par défaut). Le flux est réabonné à chaque
reconnexion WebSocket ; s'il ne reçoit plus rien (`TRADE_FLOW_WS_DEAD_SEC`),
le bot bascule automatiquement sur des requêtes REST.

Réglages (onglet Réglages avancés) :

| Réglage | Défaut | Rôle |
|---|---|---|
| `ENTRY_FLOW_CONTRADICTION_THRESHOLD` | 0.3 | Veto : refuse l'entrée si le flux est nettement contraire |
| `ENTRY_FLOW_CONFIRM_MIN_PRESSURE` | 0 (désactivé) | Exige un flux favorable à l'entrée (ex. 0.1 : achat net ≥ +0,1 pour Spot-Accum, vente nette ≤ −0,1 pour Accumulation) |
| `TRADE_FLOW_WINDOW_SEC` | 180 | Fenêtre d'analyse du flux |

## 4undecies. Trailing take profit du mode Funding

Le TTP Funding s'arme dès **+1 %** de mouvement de prix favorable
(`FUNDING_TTP_ARM_PCT`). Une fois armé, quand le prix se replie, le **flux de
transactions** décide de la patience :

| Flux au moment du repli | Repli toléré depuis le pic | Motif affiché |
|---|---|---|
| Favorable au trade (vendeurs dominants pour un short) | 0,9 % (patience) | `(Funding, après patience)` |
| Neutre ou indisponible | 0,5 % | `(Funding)` |
| Contraire au trade | 0,25 % (sortie anticipée) | `(Funding, flux contraire)` |

Quelle que soit la décision, un trade armé ne redescend jamais sous **+0,3 %**
de gain (`FUNDING_TTP_MIN_LOCK_PCT`, motif `(Funding, plancher)`). Tous ces
seuils sont modifiables dans les réglages avancés.

## 4duodecies. Réglages par mode et simulation du SL (v4.269)

**Simulation « SL plus large ».** Pour chaque trade sorti par stop loss, le
suivi rejoue la minute par minute l'heure qui suit la sortie et indique, pour
un SL hypothétique à 0,75 %, 1 % et 1,5 %, si le prix serait d'abord **revenu
au niveau d'entrée** ou aurait **touché ce SL** (bougie ambiguë = SL touché,
par prudence). Colonnes « si SL à … : issue » de l'export CSV. L'export
indique aussi les **raisons d'entrée** de chaque trade, et les heures sont
dans le fuseau de votre navigateur.

**Réglages par mode** (onglet Réglages avancés, vide = réglage général) :

| Réglage | Défaut |
|---|---|
| Spot-Accum / Accumulation — SL plafond (% de prix) | 0,5 (hérité) |
| Spot-Accum / Accumulation — confirmation flux à l'entrée | réglage général (0) |
| Accumulation — délai après perte sur un actif | **3600 s** |
| Accumulation — entrées max par fenêtre de 10 min | **3** |
| Spot-Accum / Funding — délai après perte, Spot-Accum — entrées max | 0 (désactivé) |

**Correctif important :** jusqu'à la 4.268, le profil (SWING/SCALP) était
appliqué **après** les réglages enregistrés, et écrasait à chaque redémarrage
37 réglages avancés. Depuis la 4.269, vos réglages sont conservés.

## 4terdecies. Patience du SL Spot-Accum pilotée par le flux (v4.270)

Quand un trade Spot-Accum atteint son SL plafond (0,5 % par défaut), le bot
lit le flux de transactions :

- **acheteurs dominants** (pression ≥ +0,2) **et tendance de fond intacte**
  (prix au-dessus de l'EMA200, v4.271) → il **attend** au lieu de couper,
  dans la limite d'une perte de **1 %** et de **15 minutes** ;
- flux neutre ou vendeur → sortie immédiate, comme avant.

Pendant l'attente, la sortie se fait dès que le flux devient défavorable, que
la perte atteint 1 % ou que les 15 minutes sont écoulées. Motifs distincts :
`STOP LOSS (flux defavorable)`, `(tendance cassee)`, `(plafond patience)`,
`(patience expiree)` ;
un trade sauvé qui finit autrement porte la mention `· apres patience SL`.
Réglages avancés : seuil de pression, perte maximale, durée maximale
(0 = patience désactivée).

## 4quaterdecies. Mode Forex recalibré (v4.272)

Le mode Forex (lignes « 💱 FOREX LONG / SHORT » du diagnostic) ne trade que
les actifs de `FOREX_MODE_SYMBOLS` (devises `xyz:` et PAXG). Deux seuils
calibrés pour les cryptos l'empêchaient d'entrer :

| Réglage | Avant | Après |
|---|---|---|
| `FOREX_ANTI_RANGE_MIN_PCT` (mouvement minimal sur 30 bougies 5 min) | 2 % | 0,25 % |
| `FOREX_LONG_TERM_MOMENTUM_MIN_CHANGE_PCT` (repli de la confirmation de tendance) | 2 % | 0,4 % |

Le diagnostic affiche désormais le blocage « marché en range » pour les
devises, et « non concerné » pour les cryptos.

## 4quindecies. Onglet « ✋ Trading Manuel » (v4.273)

1. **Opportunités** : le bot publie en continu les entrées que ses modes
   détectent (y compris celles qu'il ne prend pas, faute de place). Une
   opportunité reste choisissable tant que le bot la reconfirme (moins de
   `MANUAL_OPPORTUNITY_TTL_SEC` = 120 s), et **uniquement dans le sens prévu**.
2. **Proposition** : prix, taille (15 $ de notionnel par défaut), levier, SL
   (logique du mode source), TP (80 % de la fourchette support/résistance) et
   trailing (réglages du mode source). **Tout est modifiable.**
3. **Exécution** : maintenant, ou programmée (prix ≥, prix ≤, ou heure), avec
   expiration (24 h par défaut) et annulation automatique si le bot ne
   confirme plus l'opportunité au déclenchement (option).
4. **Paper / Live** : le live exige une confirmation explicite. **Perps ou
   Spot** : le spot n'autorise que l'achat, sans levier ; les USDC doivent
   être disponibles côté Spot.
5. **Suivi** : chaque position manuelle est gérée par le bot selon SES
   paramètres (SL, TP, trailing), modifiables à tout moment, avec fermeture
   manuelle. Historique dédié, strategie « Manuel » dans l'export CSV.

**Sécurités** : Hyperliquid ne tient qu'une position perp par actif — un ordre
manuel live est refusé si le bot (ou vous) détient déjà une position live sur
l'actif, et le bot n'ouvre pas en live sur un actif tenu manuellement. En perp
live, le SL est aussi posé en ordre natif sur Hyperliquid (mis à jour si vous
le modifiez). En spot, la protection repose sur le bot seul. Les ordres
programmés et positions manuelles survivent aux redémarrages.

## 4sedecies. Régime de marché et plages horaires (v4.276)

**Régime de marché** (recalculé toutes les 5 min) : il est **haussier
confirmé** quand BTC en 1 h est au-dessus de son EMA200 (d'au moins 0,2 %)
avec l'EMA50 au-dessus de l'EMA200, **et** qu'au moins 60 % des actifs suivis
sont au-dessus de leur propre EMA200 ; **baissier confirmé** dans le cas
inverse ; **neutre** sinon.

- Marché baissier confirmé → **Spot-Accum** (achats) n'ouvre plus rien.
- Marché haussier confirmé → **Accumulation** (shorts) n'ouvre plus rien.
- Neutre → les deux modes fonctionnent normalement.

Le régime s'affiche en tête du diagnostic d'entrée et dans l'onglet
Historique ; chaque changement de régime est journalisé.

**Plages horaires par mode** (réglages avancés, heures UTC, 0-24 = toujours) :
Spot-Accum, Accumulation et Funding. Le tableau « Résultats par tranche
horaire » de l'onglet Historique (tranches de 4 h, en UTC et à votre heure
locale) aide à les choisir.

## 5. Premier lancement

Va sur l'URL Railway, clique "Créer un compte" et crée **ton unique compte**.
**L'inscription se ferme automatiquement dès qu'un compte existe** — personne
d'autre ne pourra créer de compte même si l'URL fuite. Si tu dois un jour
recréer un compte, connecte-toi à la base et vide la table `users`.

## 6. Ce qui a changé par rapport au bot Tkinter

- **Aucune fenêtre graphique** : tout se pilote depuis `index.html`, servi
  directement par l'API sur `/`.
- **Persistance réelle** : utilisateurs, trades, réglages custom vivent dans
  `hyperbot.db` (SQLite) sur le volume Railway.
- **Un seul `BotEngine`** créé au démarrage du serveur (pas recréé à chaque
  clic DÉMARRER comme dans la version Tkinter) — le PnL cumulé de session
  (`total_pnl`) survit donc à tous les démarrages/arrêts tant que le
  **process** ne redémarre pas (redeploy Railway = nouveau process).

## 7. Correspondances et simplifications assumées

L'interface `index.html` a été conçue pour un bot plus riche que le nôtre.
Voici comment chaque champ est réellement branché :

| Champ interface | Branché sur | Note |
|---|---|---|
| `position_pct` | `POSITION_SIZE_PCT` | ✅ direct |
| `max_loss_usd` | `SL_PCT_OF_E` | ⚠️ converti en % de la taille de position (E) au moment de l'enregistrement |
| `quick_profit_usd` | `TTP_ARM1_PRICE_PCT` | ⚠️ converti en % de mouvement de prix au moment de l'enregistrement |
| `max_open_trades` | `MAX_OPEN_TRADES` (nouveau garde-fou ajouté) | ✅ |
| `filter_hours` | `CRYPTO_OFFPEAK_ENABLED` (heures creuses 2h-6h UTC) | ✅ correspond à une vraie fonctionnalité |
| `filter_weekend` | `FOREX_SYMBOLS` (fermeture Forex sur PAXG) | ✅ correspond à une vraie fonctionnalité |
| `filter_macro` | `CPI_BLACKOUT_ENABLED` (blackout CPI Finnhub) | ✅ correspond à une vraie fonctionnalité |
| `active_coins` | `ACTIVE_COINS` | ✅ tous les actifs de `SYMBOLS` (cryptos et forex HIP-3) |
| Signal `take_profit1` / `take_profit2` | Prix équivalents calculés depuis les seuils $ (Quick Profit / Trailing) | ⚠️ nôtre bot ne raisonne pas en % fixe — conversion informative au moment de l'entrée, pas un vrai ordre TP1/TP2 |
| `leverage` par signal | `CONFIG["LEVERAGE"]` | ⚠️ toujours le même (pas de levier variable par trade) |
| `risk_reward` | `QUICK_PROFIT_ARM_USD / MAX_LOSS_USD` | ⚠️ ratio informatif, pas un vrai calcul de risk/reward par trade |
| Toggle "IA continue" | Stocké, **sans effet** | ❌ pas d'équivalent — ce bot n'a pas de moteur de génération de signaux IA continu |
| Login email/mot de passe | Table `users` réelle (PBKDF2 + JWT) | ✅ mais inscription limitée à un seul compte (voir section 5) |

## 8. Sécurité

- `HYPERBOT_SECRET_KEY` est obligatoire : sans elle (ou si elle fait moins de
  32 caractères) le serveur ne démarre pas. Un token n'est accepté que si le
  compte existe toujours en base.
- Préfère renseigner `HYPERBOT_PRIVATE_KEY` en variable d'environnement
  Railway plutôt que via le formulaire de l'interface (`/api/config/hyperliquid`)
  — ce formulaire écrit la clé dans le fichier SQLite du volume, ce qui est
  acceptable si le volume est privé, mais une variable d'environnement reste
  la manière la plus sûre de gérer un secret.
- Le changement de wallet/clé API ne prend effet qu'au **prochain démarrage**
  du bot (arrêt puis démarrage) — reconnecter l'exchange à chaud n'est pas
  géré.
