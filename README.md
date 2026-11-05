# HyperBot Web — déploiement GitHub + Railway

Version web (sans interface Tkinter) du bot de trading, avec l'interface
`index.html` fournie branchée sur une vraie API FastAPI et une base SQLite.

Version courante : **4.299** (visible dans `/health` et dans les logs de démarrage).

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

### Ordre libre (v4.343)

Carte « ✏️ Ordre libre » en haut de l'onglet : choisissez **n'importe quel actif suivi par le bot**
(crypto ou Forex `xyz:`), le sens, puis « Préparer l'ordre ». Le ticket s'ouvre avec des valeurs
par défaut (`MANUAL_DEFAULT_NOTIONAL_USD`, `MANUAL_DEFAULT_SL_PCT`), sans opportunité du bot, et
**tout est modifiable** : paper ou live, perp ou spot, notionnel, levier, entrée (maintenant, prix,
heure), SL, TP, trailing, et deux options ajoutées :

- **SL dynamique (verrou progressif)** : dès que le gain maximal atteint le seuil choisi (+X %), le
  SL monte pour garantir la part choisie (Y %) de ce gain maximal, et ne redescend jamais (miroir en
  short). Sortie « STOP DYNAMIQUE (manuel) ». En perp live, le SL natif Hyperliquid est mis à jour
  (écart minimal `MANUAL_LOCK_PUSH_MIN_STEP_PCT` = 0,05 %, dans un thread). Sans effet sur un SL
  fixe : le verrou ne fait que relever le SL existant. Modifiable en cours de route.
- **Critères de tendance H4 (choix manuels)** : *condition d'entrée* (haussière, baissière ou
  neutre — l'ordre est refusé si la tendance diffère, est indisponible ou si les bougies sont
  périmées ; pour un ordre programmé, il est annulé au déclenchement) et *règle de sortie* (« si la
  tendance devient opposée » ou « si elle n'est plus celle du sens du trade », vérifiée toutes les
  5 s au plus, uniquement sur des bougies H4 à jour). Même calcul que le top-down.

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
sont au-dessus de leur propre EMA200 **en 1 h** (même unité de temps que
BTC — depuis la 4.280) ; **baissier confirmé** dans le cas
inverse ; **neutre** sinon, y compris tant que la largeur de marché n'est pas
mesurable (au moins 8 actifs).

À ne pas confondre avec la tendance propre à chaque mode : Spot-Accum et
Accumulation exigent en plus la tendance **court terme** de l'actif (EMA200 sur
des points de 2 min, soit ~6 h 40). Un marché peut donc être haussier de fond
(régime) tout en étant en repli à court terme sur de nombreux actifs.

- Marché baissier confirmé → **Spot-Accum** (achats) n'ouvre plus rien.
- Marché haussier confirmé → **Accumulation** (shorts) n'ouvre plus rien.
- Neutre → les deux modes fonctionnent normalement.

Le régime s'affiche en tête du diagnostic d'entrée et dans l'onglet
Historique ; chaque changement de régime est journalisé.

**Plages horaires par mode** (réglages avancés, heures UTC, 0-24 = toujours) :
Spot-Accum, Accumulation et Funding. Le tableau « Résultats par tranche
horaire » de l'onglet Historique (tranches de 4 h, en UTC et à votre heure
locale) aide à les choisir.

## 4septdecies. Entrées par cassure/rebond encadrées (v4.277)

Spot-Accum et Accumulation ont des voies d'entrée « de contournement »
(cassure fraîche, cassure sur volume, fausse cassure, tendance persistante,
étoile filante pour Accumulation). Elles dispensaient de TOUS les filtres
principaux, y compris le sens de la tendance EMA200 et le veto du flux.
Depuis la 4.277, elles ne dispensent plus que de la stabilité de tendance et
de l'ADX : elles doivent respecter le sens de la tendance et passer le veto
du flux. Réglages avancés `*_BYPASS_REQUIRE_TREND` / `*_BYPASS_FLOW_VETO`
(1 = oui, 0 = ancien comportement).

## 4octodecies. Spot-Accum : achat sur repli (v4.278)

En hausse régulière, le prix s'éloigne du support d'origine de la tendance.
Spot-Accum suit désormais aussi un **support ascendant** : le dernier creux de
repli « plus haut que le précédent » sur les bougies 1 h (creux validé par 2
bougies de chaque côté, sur les 3 derniers jours). Un repli sur ce niveau est
une entrée valide, avec les mêmes protections (bougie haussière, veto du flux,
tendance EMA200, régime de marché). Si le creux est cassé en clôture, le
support ascendant disparaît.

La **voie d'entrée** (proche du support, repli sur support ascendant, cassure
fraîche, fausse cassure, tendance persistante, volume, étoile filante…) figure
maintenant dans les raisons d'entrée — colonne « raisons d'entrée » de
l'export CSV — pour Spot-Accum et Accumulation.

## 4novodecies. Cassure fraîche et cohérence des voies d'entrée (v4.279)

**Cassure fraîche contre la tendance** : elle peut de nouveau capter le début
d'un retournement avant que l'EMA200 ne suive, mais seulement si le flux le
confirme franchement (pression ≥ +0,2 pour Spot-Accum, ≤ −0,2 pour
Accumulation) et si le régime de marché n'est pas opposé. Les autres voies de
contournement restent soumises au sens de l'EMA200.

**Contradictions corrigées** :
- le filtre anti-range bloquait les voies « cassure fraîche » (qui sort d'une
  consolidation) et « volume en consolidation » (qui l'exige) — pour
  Accumulation, la voie volume ne pouvait même jamais aboutir (même calcul,
  mêmes réglages) ; ces deux voies en sont désormais exemptées ;
- la voie volume subissait deux critères de proximité différents (support de
  la consolidation puis support dynamique) : un seul est conservé.

## 4vicies. Qualité du marché et anti-range relatif (v4.281)

**Anti-range relatif à chaque actif.** Le seuil absolu (2 % de mouvement pour
toutes les cryptos, 0,25 % pour le Forex) est remplacé par un seuil adapté à
l'actif : amplitude horaire **habituelle** de l'actif (médiane sur 7 jours de
bougies 1 h) × 0,6 × racine de la durée de la fenêtre en heures. Exemples :
sur 1 h, ~0,36 % pour un actif calme comme BTC, ~1,2 % pour un actif nerveux
comme WIF. Les seuils absolus ne servent plus que de repli tant que
l'habitude de l'actif n'est pas connue. Réglages : `ANTI_RANGE_RELATIVE_ENABLED`,
`ANTI_RANGE_REL_MULT`.

**Qualité du marché** (tous les modes), mesurée par rapport aux habitudes de
l'actif et affichée au diagnostic :
- **volatilité** : amplitude de la dernière heure / amplitude horaire habituelle ;
- **activité** : volume $ de la dernière heure / volume horaire habituel ;
- **flux** : pression acheteurs/vendeurs ;
- **spread** (v4.282) : écart entre meilleure offre d'achat et de vente du
  carnet d'ordres, en % du prix — coût d'un aller-retour au marché en plus
  des frais (lu à la demande, mis en cache 30 s par actif).

Ces quatre mesures sont enregistrées à **chaque entrée** (colonnes de l'export
CSV). Les filtres correspondants (« marché endormi », « marché déserté »,
« flux sans conviction » par mode, « spread trop large ») sont prêts mais **désactivés (0)** en
attendant d'être calibrés sur les résultats réels.

## 4unvicies. Indicateurs vérifiables sur le graphique Hyperliquid (v4.283)

Hyperliquid ne fournit aucun indicateur calculé (EMA, support/résistance…) :
le bot les calcule, désormais **entièrement à partir des vraies bougies
Hyperliquid** :

| Indicateur | Calcul | À vérifier sur le graphique |
|---|---|---|
| Tendance de chaque mode | **EMA 80 sur bougies 5 min** (~6 h 40, même horizon que l'ancienne EMA200 sur points internes de 2 min) | ajouter une EMA de longueur 80 en unité 5 min |
| Support / résistance (entrées) | plus bas / plus haut des **40 dernières bougies 5 min** (~3 h 20) | |
| Support / résistance Accumulation | **288 bougies 5 min** (~24 h) | |
| Régime de marché | EMA200 / EMA50 sur bougies 1 h | EMA 200 en unité 1 h |

**Depuis la 4.284, les bougies 5 min et 1 h arrivent en temps réel par
WebSocket** (canal `candle` d'Hyperliquid, 2 flux par actif) : chaque bougie
est prise en compte à l'instant où elle se clôture — les indicateurs sont
calculés sur la dernière bougie clôturée (« l'avant-dernière » du graphique),
jamais sur la bougie en cours. Le REST ne sert plus qu'au chargement initial de
l'historique, à une resynchronisation de sécurité toutes les 6 h, après chaque
reconnexion (bougies manquées) et en repli si le flux d'un actif reste muet.
Le diagnostic affiche les
valeurs exactes (« 📐 Niveaux ») pour comparaison directe avec le graphique.

## 4duovicies. Situations de marché et choix des niveaux (v4.286)

Chaque actif est classé en croisant sa tendance de **fond** (prix / EMA200
1 h, ~8 jours) et sa tendance **court terme** (prix / EMA80 5 min, ~6 h 40) :

| Situation | Spot-Accum (achat) | Accumulation (short) | Niveaux utilisés |
|---|---|---|---|
| **Hausse saine** (fond ↑, court ↑) | toutes les voies | ❌ | 1 h puis 5 min |
| **Repli dans une hausse** (fond ↑, court ↓) | **fin de repli** : achat sur le support de structure 1 h + bougie haussière + flux ≥ +0,2 | ❌ contre-tendance désactivée (v4.287) | Spot : 1 h |
| **Baisse saine** (fond ↓, court ↓) | ❌ | toutes les voies | 1 h puis 5 min |
| **Rebond dans une baisse** (fond ↓, court ↑) | ❌ contre-tendance désactivée (v4.287) | **fin de rebond** : short sous la résistance de structure 1 h + bougie baissière + flux ≤ −0,2 | Accu : 1 h |
| Fond neutre | règles court terme habituelles | règles court terme habituelles | 1 h |

Règle de choix des niveaux : **dans le sens du fond**, le bot utilise la
structure 1 h (support ascendant / résistance descendante, ou plus bas / plus
haut de la tendance 1 h) ; **à contre-fond**, il utilise les niveaux 5 min
(mouvement court) et exige de la marge avant le niveau 1 h opposé, qui sert
de plafond / plancher. Le diagnostic affiche la situation de chaque actif et
les deux jeux de niveaux. Ces règles remplacent le blocage par le régime
global (`SITUATION_RULES_ENABLED` = 0 pour revenir à l'ancien comportement).

**Depuis la 4.287, les trades à contre-tendance sont désactivés** (achat dans
un rebond baissier, short dans un repli haussier, cassure fraîche contre l'EMA
de tendance) : chaque mode ne trade que dans le sens de la tendance de fond.
Réactivables via `SITUATION_ALLOW_COUNTERTREND` et
`*_FRESH_BREAKOUT_COUNTER_TREND` (réglages avancés) lorsque des données
montreront qu'ils sont gagnants.

## 4trevicies. Voie « continuation » (v4.288)

La distance à un support (Spot-Accum) ou à une résistance (Accumulation) n'est
plus à elle seule une raison de refuser une entrée en **tendance saine**. La
voie « continuation » autorise l'entrée loin d'un niveau si **toutes** ces
conditions sont réunies :

1. situation **hausse saine** (Spot-Accum) ou **baisse saine** (Accumulation) ;
2. flux franc **et confirmé** : ≥ +0,3 (ou ≤ −0,3) sur la lecture actuelle ET la précédente ;
3. marché actif : activité ≥ 1× l'habitude de l'actif ;
4. pas d'excès : écart à l'EMA80 5 min ≤ 1,5 ATR 5 min ;
5. place pour gagner : prochain niveau opposé (5 min et 1 h) à ≥ 2× le SL ;
6. bougie dans le sens du trade.

**Phase de test : ces entrées sont simulées en paper même si le mode est en
live** (`CONTINUATION_PAPER_ONLY` = 1), et marquées « continuation » dans
l'export. Le diagnostic indique la condition manquante quand la voie est
refusée.

## 4quattuorvicies. Nettoyage : moteur d'entrée simple (v4.289)

Spot-Accum et Accumulation n'utilisent plus la longue chaîne de conditions
éliminatoires, mais trois étapes :

1. **Garde-fous** : mode actif, actif sélectionné, plage horaire, qualité du
   marché (si activée), situation compatible (jamais à contre-tendance de
   fond), étoile filante en cours (achats).
2. **Un signal parmi trois** : proche d'un niveau (1 h ou 5 min — 1 h
   seulement en fin de repli / de rebond), cassure fraîche, continuation.
3. **Une confirmation** : le flux ne contredit pas l'entrée (et confirme le
   retournement en fin de repli / de rebond).

Supprimés : stabilité 12 cycles, ADX, anti-range, couleur de bougie,
fourchette S/R minimale, seuil de confiance appris, voies fausse cassure,
volume, tendance persistante et étoile filante (doublons). Restent à
l'ouverture : plafond de positions, délai après perte, limite de rafales,
conflit entre modes. Ancienne chaîne : `ENTRY_ENGINE_SIMPLE` = 0.

**Funding** a maintenant sa ligne au diagnostic (« 💰 FUNDING »), qui indique
pourquoi il n'entre pas (taux pas assez extrême, flux contraire, range…).

## 4quinvicies. Analyse statistique des stop loss (v4.290)

Après chaque SL (Spot-Accum, Accumulation, Funding, Manuel), le bot suit le
prix **minute par minute pendant 2 h** et enregistre :
- si un SL à 0,75 / 1 / 1,5 / 2 % aurait tenu (prix revenu à l'entrée d'abord)
  ou aurait été touché ;
- le **délai** avant que le prix revienne au niveau d'entrée ;
- le **pire recul** (depuis l'entrée) subi avant ce retour — la largeur de SL
  qui aurait permis de patienter ;
- le prix 2 h après, par rapport à l'entrée.

Onglet **Historique → 🛑 Analyse des stop loss** : synthèse par mode (part des
SL suivis d'un retour à l'entrée en 30 min / 1 h / 2 h, délai médian, reculs
médian / 75 % / 90 %, et pour chaque SL simulé le gain estimé en points de %),
**téléchargeable en CSV**. Le détail trade par trade est dans l'export « Suivi »
(4 colonnes « après SL » supplémentaires). Les SL des 16 derniers jours sont
complétés rétroactivement.

## 4sexvicies. Synchronisation bot ↔ Hyperliquid (v4.292)

Toutes les 2 min (fil séparé), le bot compare ses positions **live** (tous
modes, trading manuel compris) avec celles réellement tenues sur Hyperliquid
(DEX natif et xyz), actif par actif :

| Écart | Correction automatique (après 2 contrôles consécutifs) |
|---|---|
| Position **fantôme** (bot : ouverte / Hyperliquid : aucune) | clôturée côté bot au prix réel de sortie |
| Position **orpheline** (Hyperliquid : ouverte / bot : aucune) | reprise par le bot (identification par cloid) |
| **Taille** ou **prix d'entrée** différents | alignés sur Hyperliquid |
| **SL natif** absent | reposé immédiatement |
| **Sens opposé** | jamais corrigé : alerte, vérification manuelle |

Carte « 🔄 Synchronisation bot ↔ Hyperliquid » en haut de l'onglet de
trading (bouton « Vérifier maintenant »), avec la position des deux côtés, le
prix de liquidation, le PnL latent et la valeur du compte. Chaque écart est
aussi journalisé. Réglages : `LIVE_SYNC_INTERVAL_SEC`, `LIVE_SYNC_AUTO_FIX`.

## 4septemvicies. Moteur « top-down » multi-unités de temps (v4.299)

Spot-Accum (achats) et Accumulation (ventes) utilisent désormais l'approche
**top-down** sur les vraies bougies Hyperliquid (`mtf_analysis.py`) :

1. **Unité majeure (H4 par défaut, ou Daily)** : tendance de fond (clôture /
   EMA50 / EMA200) et **zones clés** (sommets et creux de marché regroupés ;
   un ancien plafond cassé devient un plancher). Spot-Accum n'achète qu'en
   tendance haussière, Accumulation ne vend qu'en tendance baissière.
2. **Attente** que le prix entre dans une zone de support (achat) ou de
   résistance (vente), avec une marge de 0,25 ATR.
3. **Unité inférieure (M15 par défaut, ou H1)** : signal de bougie de
   retournement sur la dernière bougie clôturée — avalement, marteau / étoile
   filante, étoile du matin / du soir — dont la mèche a touché la zone.
4. **Plan de trade** : SL au-delà de la zone et du motif (+ 0,15 ATR), posé
   aussi en ordre natif ; objectif = zone opposée ; rapport gain/risque ≥ 1,5.
   **Taille calculée sur le risque** : perte au SL = 0,5 % du capital
   (notionnel plafonné à 30 $), levier 1.
5. **Gestion** : SL remonté au prix d'entrée à +1R, puis SL suiveur à 1R du
   meilleur prix au-delà de +2R ; sortie à l'objectif.

Le diagnostic affiche pour chaque actif la ligne « 📊 TOP-DOWN » (tendance,
zones, position du prix). Tous les paramètres sont réglables (réglages
avancés « Top-down »). Moteur précédent : `ENTRY_ENGINE_MTF` = 0.

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

## v4.362 — Swing Support (crypto, paper)

Sous-mode PAPER, éteint par défaut (`SUPSW_ENABLED`). Actifs : BTC, ETH, HYPE, TAO, SUI (`SUPSW_SYMBOLS`).
- **Entrée** : tendance Daily haussière + prix dans un support Daily + (retest du support tenu sur les `SUPSW_WINDOW` dernières bougies 1 h, ou balayage de liquidité puis reprise), dans les `SUPSW_SIGNAL_MAX_AGE_SEC` qui suivent la clôture de la bougie 1 h.
- **Sortie** : objectif `SUPSW_TP_PCT` (2 %), stop de sécurité `SUPSW_SL_PCT` (5 %), durée max, et **sortie anticipée** si une bougie 1 h clôture sous (bas de zone − tampon) ET flux < `SUPSW_EXIT_FLOW`. `SUPSW_EXIT_REQUIRE_H4_END=1` exige en plus la tendance H4 finie. Aucun breakeven.
- **Contexte enregistré sans filtrer** : tendance de BTC, tendance Weekly, funding annualisé, flux.
- **Journal** (`supsw_events`) : chaque signal (pris ou refusé) mesuré à 24 h / 72 h ; chaque sortie anticipée suivie à 1 h / 4 h / 24 h, avec « l'objectif ou le stop aurait-il été touché ensuite ? » (`too_early`).
- **API** : `/api/supsw/status`, `/api/supsw/stats?days=`, `/api/supsw/journal/export.csv`. **UI** : carte « Swing Support » dans TRADES P/L › Swing ; ligne « 🧱 SWING SUPPORT » dans le diagnostic.
- Fichiers : `swing_support.py` (nouveau), `bot_engine.py`, `db.py`, `api.py`, `index.html`.
- Le trade occupe le slot de l'actif tant qu'il est ouvert (les autres modes ne peuvent pas trader cet actif).

### v4.363 — Swing Support : niveau de sortie anticipée
Constat (HYPE, TAO) : sur une zone Daily large, le bas de zone est sous le stop à 5 % → la sortie anticipée était inatteignable. Désormais (`SUPSW_EXIT_LEVEL=1`, défaut) le niveau est le **plus bas du retest** (plus bas des bougies 1 h du signal), borné entre `SUPSW_EXIT_MIN_PCT` (0,5 %) et `SUPSW_EXIT_MAX_PCT` (3 %) sous l'entrée. `SUPSW_EXIT_LEVEL=0` rétablit le bas de zone. Le niveau est stocké sur la position et dans le journal (colonne `inval`) ; les positions déjà ouvertes gardent le bas de zone. Migration automatique de la table `supsw_events`.

## v4.365 — Tendance confirmée (module à part, paper)

Nouveau fichier : `trend_follow.py` (à déposer à côté de `bot_engine.py`). Fichiers modifiés : `bot_engine.py`, `db.py`, `api.py`, `index.html`.

- **Entrée** dès que le Daily ET le H4 vont dans le même sens (LONG ou SHORT), avec momentum 1 h (clôture du bon côté de sa moyenne 20) et flux de transactions dans le sens. Option « cassure » (`TRENDF_ENTRY_MODE=1`).
- **On reste tant que la tendance tient** : aucun objectif ; stop suiveur = 3 × ATR H4 (borné 0,8–6 %) ; sortie si le Daily n'est plus dans le sens (bougie clôturée), si le H4 se retourne (`TRENDF_EXIT_H4_REVERSE`), au stop, ou après 30 jours.
- **Module à part** (stratégie `trend_follow`, code 09, sous-onglet « 📈 Tendance »), paper uniquement, désactivé par défaut (`TRENDF_ENABLED`). Un actif = une seule position, tous modes confondus.
- **Traçabilité** : table `trendf_events` ; chaque entrée (prise ou refusée) est mesurée à 24 h / 72 h, avec son **retard** (distance à la moyenne Daily en ATR) ; chaque sortie est suivie 1 h / 4 h / 24 h. Statistiques `/api/trend/stats`, export `/api/trend/journal/export.csv`.
- Actifs par défaut : BTC, ETH, HYPE, TAO, SUI, PAXG, xyz:EUR, xyz:JPY (`TRENDF_SYMBOLS`).

### v4.366 — Swing Support : stop et objectif selon le levier (optionnel)

Réglage `SUPSW_LEVER_SCALE` (0 par défaut = comportement inchangé). À 1 : stop (en % du prix) = `SUPSW_MARGIN_RISK_PCT` (7,5 % de la marge) / levier, plafonné à `SUPSW_SL_PCT` et plancher `SUPSW_MIN_SL_PCT` (1,5 %) ; objectif : `SUPSW_TP_LEV1` / `LEV2` / `LEV3` = 2 / 1,5 / 1 % pour un levier ×1 / ×2 / ×3 et plus. Le levier se règle avec `SUPSW_LEVERAGE` (1 à 10). La taille reste dimensionnée par le risque (≈ 1 % du capital au stop) ; le niveau de sortie anticipée est toujours maintenu au-dessus du stop. Les positions de la carte affichent « prix X % · marge Y % ».

### v4.367 — Trades fermés Swing Support / Tendance : sens et pic

Chaque ligne de « Trades fermés » indique maintenant LONG / SHORT et le pic atteint : « pic prix +X % · marge +Y % · +Z $ » (ou « jamais en gain »).

### v4.368 — Badge de stratégie sur les trades ouverts

La liste « Trades ouverts » affiche maintenant la stratégie de chaque position : 🧱 SWING SUPPORT, 📈 TENDANCE, 🌊 SWING FX, ⚡ SCALP FX, 🧭 TOP-DOWN (moteur principal), en plus de ACCUMULATION et FUNDING.

### v4.369 — Libellés des réglages Swing Support

Les réglages avancés Swing Support portent maintenant un groupe explicite après « Swing Support · » : ACTIVATION, STOP FIXE, OBJECTIF FIXE, TAILLE, LIMITE, SIGNAL, DURÉE, SORTIE ANTICIPÉE, LEVIER, STOP ET OBJECTIF SELON LE LEVIER, SELON LE LEVIER. Aucun changement de comportement.

### v4.370 — Swing Support : alertes de dégradation (mesure seule)

Aucune sortie ne change. Chaque minute, pour chaque position Swing Support ouverte, le bot compte 7 signes de dégradation : flux vendeur, support cassé en clôture 1 h, H4 baissier, BTC baissier, sous le prix d'entrée depuis 2 h, aucun gain ≥ 0,3 % après 3 h, prix à mi-chemin du stop. Le maximum atteint et le compte à la sortie sont enregistrés dans `supsw_events` (colonnes `alerts_max`, `alerts_exit`, `alerts_json` avec l'historique), migration automatique. La carte Swing Support compare gagnants et perdants (`/api/supsw/stats`, bloc `alerts`) ; l'export CSV contient les nouvelles colonnes. Les positions déjà ouvertes commencent à être mesurées dès le déploiement. Un seuil de sortie ne sera proposé que si les perdants montent nettement plus haut que les gagnants (environ 20 trades fermés).

## v4.371 — Scalp Forex : deux sorties anticipées

Fichiers modifiés : `scalp_forex.py`, `bot_engine.py` (valeurs par défaut), `api.py` (réglages et validation), `README.md`. Actifs par défaut (1) ; chaque règle s'éteint par un réglage à 0.

- **SANS PROGRES** (`FOREX_SCALP_STALE_*`) : après 10 min (`STALE_MIN`), si le pic de gain est resté sous 0,05 % du prix (`STALE_PEAK_PCT`), le trade sort au lieu d'attendre les 20 min du temps max. Motif affiché : « SCALP SANS PROGRES ».
- **ECHEC PRECOCE** (`FOREX_SCALP_EARLYFAIL_*`) : un trade qui n'a jamais dépassé 0,03 % de gain (`EARLYFAIL_PEAK_PCT`) et a perdu la moitié de son stop (`EARLYFAIL_RISK_FRAC`) sort sans attendre le stop complet (au plus tôt 1 min après l'entrée, jamais après le breakeven). Motif : « SCALP ECHEC PRECOCE ».
- Constat à l'origine (30 trades, 04-09/10) : 6 stops simples jamais en gain (−0,247 $), 12 « temps max » à −0,159 $. Les sorties « flux contraire » (8/8 gagnantes) et le breakeven ne sont pas touchés. Les positions déjà ouvertes appliquent les nouvelles règles dès le redémarrage.

## v4.372 — Smartphone : application installable, mise en page mobile, chargement plus rapide

Fichiers : `api.py`, `index.html` (généré), `index_src.html` (source), `README.md`.

- **Installation sur l'écran d'accueil** : iPhone (Safari → Partager → « Sur l'écran d'accueil ») et Android (Chrome → menu → « Installer l'application »). L'application s'ouvre en plein écran avec son icône. Les icônes, le manifeste et le service worker sont servis par `api.py` (`/manifest.webmanifest`, `/sw.js`, `/icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png`) : aucun fichier image à déposer. Le service worker ne met rien en cache : c'est toujours la dernière version en ligne.
- **Mise en page petit écran** (≤ 640 px) : grilles de cartes à 2 colonnes (1 colonne pour les paires), champs à 16 px (plus de zoom automatique sur iPhone), boutons plus hauts, marges de sécurité (encoche et barre du bas), plus de défilement horizontal de la page, en-tête et horloge qui passent à la ligne.
- **Chargement plus rapide** : `index.html` est maintenant **précompilé** (le navigateur n'a plus à charger ni exécuter Babel, environ 3 Mo, à chaque ouverture). La source reste dans `index_src.html` : c'est elle qui sert aux futures modifications, et `index.html` en est regénéré. **Les deux fichiers sont à déposer dans le dépôt**, mais seul `index.html` est servi.
- Les notifications ne sont pas incluses (iOS ne les permet qu'à l'application installée et via un service d'envoi côté serveur) ; à reprendre plus tard.

## v4.373 — FIX CRITIQUE : positions perdues au redéploiement

Fichiers : `bot_engine.py`, `db.py`, `README.md`.

- **Cause** (confirmée par les logs Railway du 10/10 à 13:40 UTC) : la sauvegarde automatique des positions (toutes les 5 s) démarrait **avant** la reprise des positions. Quand le démarrage dépassait 5 s, elle écrasait `hyperbot_positions.json` avec 0 position ; la reprise relisait alors un fichier vide (« Chargement OK : 0 position »). Les trades Paper (Swing Support, Spot-Accum…) disparaissaient de la mémoire du bot ; leurs lignes restaient « ouvertes » en base.
- **Correctif** : aucune écriture du fichier tant que la reprise n'est pas terminée ; copie de sécurité `hyperbot_positions.json.bak` avant chaque reprise ; verrou et fichier temporaire unique par fil (corrige aussi l'erreur « No such file … .tmp »).
- **Rattrapage** : au démarrage, les trades **Paper Swing Support et Spot-Accum** encore « ouverts » en base depuis moins de 4 jours et absents du fichier sont **reconstruits** avec leurs niveaux d'origine (entrée, stop, objectif, taille, levier). Le suivi du pic et l'armement repartent de zéro ; la sortie anticipée de Swing Support garde son niveau d'invalidation. Un emplacement déjà occupé n'est jamais touché ; les vieilles lignes « ouvertes » (plus de 4 jours) sont ignorées. Message dans les logs : « RECONSTRUITE depuis la base ».

## v4.374 — Funding : plafond d'entrée, actifs exclus, sortie « sans progrès »

Fichiers : `bot_engine.py`, `api.py`, `README.md`. Les trois règles sont actives par défaut ; chacune a un réglage (groupes « Funding · PLAFOND D'ENTREE », « ACTIFS EXCLUS », « SANS PROGRES »).

Constat (346 trades Funding fermés, 24/09 → 10/10) : depuis le 28/09 +0,54 $ net (68 % de réussite), mais gains moyens +0,04 $ pour des pertes moyennes −0,077 $ et frais = 72 % du gain brut.

- **Plafond d'entrée** `FUNDING_MAX_ANNUAL_PCT` (40 %, 0 = sans plafond) : pas d'entrée quand |funding annualisé| dépasse ce niveau. Tranche 40–60 % : −0,55 $ sur 34 trades (depuis le 28/09), −1,09 $ sur 86 trades au total. L'entrée refusée est suivie en lecture seule (« signaux bloqués »).
- **Actifs exclus** `FUNDING_EXCLUDE_WEAK_COINS` (1) : plus d'entrée Funding sur GMX et RENDER (−0,40 $ / 18 trades et −0,21 $ / 6 depuis le 28/09). Liste `FUNDING_WEAK_COINS` dans le code.
- **Sans progrès** `FUNDING_STALE_ENABLED` (1), `FUNDING_STALE_MIN` (20 min), `FUNDING_STALE_PEAK_PCT` (0,15 %) : un trade dont le pic est resté sous 0,15 % après 20 min sort (motif « FUNDING SANS PROGRES ») au lieu de continuer vers le stop. Sur l'historique : 22 trades dans ce cas, 21 finissaient au stop (−1,41 $).

## v4.375 — Actifs de Swing Support réservés

Fichiers : `bot_engine.py`, `api.py`, `README.md`. Réglage `SUPSW_RESERVE_ASSETS` (1 par défaut, 0 = ancien comportement), dans la carte Swing Support.

- **Constat** : BTC, ETH, HYPE, TAO et SUI (liste `SUPSW_SYMBOLS`) sont souvent occupés par Swing Support ; Tendance, Accumulation et Spot-Accum y étaient bloqués (« actif déjà ouvert dans un autre mode »).
- **Changement** : ces trois modes **n'évaluent plus** les actifs de `SUPSW_SYMBOLS` (diagnostic : « actif réservé à Swing Support »). Ils gardent tous les autres actifs. Funding, Scalp et Forex ne changent pas.
- Sans effet si Swing Support est éteint. Les positions déjà ouvertes continuent d'être gérées normalement.

## v4.376 — Alertes sur téléphone (notifications push)

Fichiers : **nouveau `push_alerts.py`**, `api.py`, `manual_trading.py`, `index.html`, `index_src.html`, `README.md`.

- **À vérifier avant le déploiement** : le fichier `requirements.txt` doit contenir la ligne `cryptography` (seule dépendance utilisée, aucun service externe). Sans elle, le bot démarre normalement et le panneau affiche « Alertes indisponibles ».
- **Activation** : onglet **Trading Manuel**, panneau « 🔔 ALERTES TÉLÉPHONE » → *Activer les alertes sur cet appareil* (un toucher est obligatoire sur iPhone). Sur iPhone : d'abord installer l'app (Safari > Partager > Sur l'écran d'accueil), l'ouvrir depuis l'icône, puis activer. iOS 16.4 ou plus.
- **Événements** (chacun activable) : trade ouvert · trade fermé avec son résultat · perte du jour au-delà d'un seuil (2 $ par défaut, tous modes, journée locale America/Marigot, une seule alerte par jour) · connexion Hyperliquid perdue depuis plus de 3 min (puis « rétablie ») · **opportunités du bot** (désactivé par défaut ; confiance minimale 70 %, une même opportunité n'est annoncée qu'une fois par 30 min).
- Les alertes partent du serveur Railway : elles arrivent app fermée et téléphone verrouillé. Maximum 30 alertes par 10 minutes.
- Les clés et les appareils abonnés sont dans `/data/hyperbot_push.json` (survivent aux redéploiements). Si iOS invalide un abonnement, le panneau permet de se réabonner.
- Service worker : ajout de l'affichage des notifications et de l'ouverture de l'app au toucher.

## v4.377 — Alertes d'opportunités : choix du type

Fichiers : `push_alerts.py`, `bot_engine.py`, `index.html`, `index_src.html`, `README.md`.

- Dans le panneau « 🔔 ALERTES TÉLÉPHONE » (Trading Manuel), la case **Opportunités du bot** affiche maintenant des boutons pour choisir les types annoncés : **Swing Support**, Accumulation, Spot-Accum, Funding, Forex. Par défaut : les quatre types déjà existants (Swing Support à cocher).
- **Swing Support** : alerte à chaque setup valide (retest tenu ou balayage de liquidité), qu'il soit ouvert par le bot ou non (dans ce cas le motif est indiqué : nombre maximal atteint, pause après perte, arrêt…). Les signaux 1 h trop anciens et les spreads trop larges n'envoient rien. Pas de seuil de confiance pour ce type (le seuil ne s'applique qu'aux autres).
- Tendance, Swing Forex et Scalp Forex n'ont pas d'alerte d'opportunité pour l'instant.

## v4.378 — Alertes Swing Support : tous les actifs et note de qualité réglable

Fichiers : `bot_engine.py`, `push_alerts.py`, `swing_support.py`, `index.html`, `index_src.html`, `README.md`.

- **Tous les actifs crypto** : avec l'alerte « Swing Support » cochée (Trading Manuel > Alertes téléphone > Opportunités), le bot cherche aussi des setups Swing Support (tendance Daily haussière, prix dans un support Daily, retest tenu ou balayage de liquidité) sur **les autres actifs crypto** de la liste `SYMBOLS`, 6 actifs par passage (toutes les 30 s). Pour ces actifs : **alerte seulement**, jamais d'ouverture, rien dans le journal ni dans la carte Swing Support. Les 5 actifs du mode restent traités comme avant (pris par le bot ou non, avec le motif).
- **Note de qualité 0-100** (réglage « Note de qualité minimale », 60 par défaut ; 40 = souple, 60 = normal, 80 = strict) : Daily 20 (repli en tendance : 10) · Weekly 15 · BTC 15 · H4 haussier 10 · contacts de la zone 15 · flux acheteur 15 · funding 10. Un élément inconnu (flux, funding) vaut une note partielle. La note et le détail figurent dans l'alerte. **Elle ne sert qu'à filtrer les alertes** (aucun trade n'en dépend) et n'est pas calibrée sur des résultats : elle sera à ajuster à l'usage.
- La confiance minimale existante (70 % par défaut) continue de s'appliquer à Accumulation, Spot-Accum, Funding et Forex.

## v4.379 — Historique des alertes

Fichiers : `push_alerts.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- Sur iPhone, **toucher une notification la retire du centre de notifications** (comportement d'iOS, non modifiable par l'application). Les 100 dernières alertes sont donc conservées côté serveur (`/data/hyperbot_push.json`) et consultables dans Trading Manuel > Alertes téléphone > « Voir l'historique des alertes » (titre, détail, heure locale).
- Route `GET /api/push/history`. L'historique se remplit même si aucun appareil n'est abonné.

## v4.380 — Historique des alertes : sélection et suppression

Fichiers : `push_alerts.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- Dans l'historique des alertes (Trading Manuel > Alertes téléphone), chaque alerte a une case : **Tout sélectionner**, **Effacer la sélection (n)** et **Tout effacer** (avec confirmation).
- Route `POST /api/push/history/delete` (`ids` ou `everything`). Les anciennes alertes reçoivent un identifiant au chargement.

## v4.381 — Préparer un trade depuis une alerte d'opportunité

Fichiers : `manual_trading.py`, `push_alerts.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- Dans l'historique des alertes, chaque alerte d'opportunité a un bouton **🧾 Préparer le trade** : le ticket de Trading Manuel s'ouvre (et l'écran défile jusqu'à lui). Rien n'est envoyé tant que tu ne valides pas ; paper ou live au choix, comme avant.
- **Opportunité encore valable** (Accumulation, Spot-Accum, Funding, Forex) : ticket de son mode, comme depuis la liste d'opportunités. **Opportunité expirée** : ticket libre (taille, stop et objectif par défaut), avec un message.
- **Swing Support** (nouveau type de ticket, n'importe quel actif crypto, LONG uniquement) : stop, objectif, levier et taille du mode Swing Support (selon le levier si activé), sans opportunité du bot à confirmer, pas de trailing par défaut. La sortie anticipée du mode ne s'applique pas aux trades manuels.
- **Toucher la notification** d'une opportunité ouvre l'application sur l'onglet Trading Manuel (application fermée ou déjà ouverte).

## v4.382 — Pas d'opportunité sur un actif déjà occupé

Fichiers : `manual_trading.py`, `bot_engine.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- Une opportunité sur un actif où une **position est déjà ouverte** (n'importe quel mode du bot, paper ou live, ou un trade manuel) n'est plus : proposée dans la liste de Trading Manuel, annoncée par alerte (Accumulation, Spot-Accum, Funding, Forex et Swing Support), ni préparable en ticket. Si une position s'ouvre pendant qu'une opportunité est affichée, elle disparaît de la liste.
- Dans l'historique des alertes, le bouton « Préparer le trade » est remplacé par « Position déjà ouverte sur … » quand l'actif est occupé.

## v4.383 — Trades manuels ouverts dans « Tous »

Fichiers : `api.py`, `index.html`, `index_src.html`, `README.md`.

- Les trades ouverts en **Manuel** apparaissent dans Trades P/L > **Tous**, avec les autres trades ouverts, taggés « ✋ MANUEL » (entrée, prix actuel, PnL, SL/TP, bouton Fermer). Ils sont comptés dans la tuile TRADES OUVERTS.
- Ils restent affichés dans l'onglet de leur mode (shorts → Accumulation, longs → Spot-Accum), dans le bloc « TRADES MANUELS OUVERTS » existant depuis la v4.358, sans doublon.
- Ils ne sont **pas inclus** dans le solde ni dans le PnL ouvert du bot.
- API : `paper_portfolio` renvoie en plus `manual_open`.

## v4.384 — Performance : Swing Support et Tendance

Fichiers : `index.html`, `index_src.html`, `README.md` (aucun changement serveur).

- Trades P/L > **Performance** affiche désormais aussi les cartes **🧱 Swing Support** et **📈 Tendance** (ouverts/total, win rate, net, frais), en plus de Forex, Scalp, Accumulation, Funding et Spot-Accum. Même calcul que les autres modes (net après frais si l'option est active, depuis le point de réinitialisation des statistiques).

## v4.385 — Alerte Swing Support : sens indiqué

Fichiers : `bot_engine.py`, `README.md`.

- Le titre de l'alerte Swing Support indique maintenant le sens : « 🧱 Swing Support · 📈 LONG (achat) ETH · note 75/100 ». Le mode est LONG uniquement (achat sur support).
- Le statut en fin de message est plus clair : « le bot n'a pas ouvert ce trade (nombre maximal de swings support simultanes atteint) ».

## v4.386 — Alertes horodatées et fraîcheur des opportunités

Fichiers : `push_alerts.py`, `bot_engine.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- La notification d'une opportunité (y compris Swing Support) commence par l'heure locale : « 🕐 18:06 · … ».
- Dans l'historique des alertes : âge en couleur (vert ≤ 15 min, orange ≤ 1 h, rouge au-delà avec « probablement dépassée »), et « prix à l'alerte X → maintenant Y · +n % dans ton sens / contre toi » (sens LONG/SHORT pris en compte).
- Le prix à l'alerte est conservé avec chaque alerte ; les anciennes alertes n'affichent que l'âge. `GET /api/push/history` renvoie `now_price`.

## v4.387 — Journal Swing Support : contexte de marché (BTC 24 h, H4)

Fichiers : `bot_engine.py`, `db.py`, `api.py`, `README.md`.

- Trois colonnes sont ajoutées au journal des signaux Swing Support et à son export CSV : `btc_chg24` (variation de BTC sur 24 h, en %), `btc_h4` (tendance H4 de BTC) et `h4_trend` (tendance H4 de l'actif). Elles sont enregistrées seulement : aucune décision du bot n'en dépend.
- Les colonnes sont créées automatiquement au démarrage dans la base existante ; les anciens signaux restent vides pour ces colonnes.
- Objectif : tester dans quelques semaines un filtre « ne pas entrer quand BTC chute » sur des données réelles.

## v4.388 — Forex : suivi du creux de PnL pendant le trade

Fichiers : `bot_engine.py`, `README.md`.

- Les trades du mode Forex enregistrent désormais, comme Funding, le chemin de leur PnL (lecture seule, aucun effet sur une décision) : creux atteint pendant le trade, creux après l'armement du trailing, pic au moment de ce creux et délai jusqu'à l'armement. Ces valeurs apparaissent dans l'export « Suivi … Forex » (colonnes « PnL minimal pendant le trade % », « PnL minimal APRES avoir touche +0,5 % », « pic atteint au moment de ce minimum % », « delai jusqu a +0,5 % (s) »).
- **Attention au libellé** : pour le Forex, le seuil de « contact » est l'armement du trailing (`FOREX_TTP_ARM_PCT`, 0,15 %), pas +0,5 % comme l'indique l'en-tête de colonne (conservé pour Funding).
- « suivi complet du trade » = oui seulement si le trade a été ouvert après le déploiement ; un trade déjà ouvert garde un creux partiel.
- Objectif : tester dans quelques semaines une sortie « sans progrès » (stop de plafond de −0,5 % touché par des trades qui n'ont jamais bougé dans le bon sens).

## v4.389 — Swing Support : laisser courir après l'objectif + réentrée après un gain

Fichiers : `bot_engine.py`, `db.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- **Laisser courir** (`SUPSW_TRAIL_ON`, 1 par défaut) : à l'objectif (+1 %), le trade n'est plus fermé. Le stop remonte aussitôt à +0,5 % au-dessus de l'entrée (`SUPSW_TRAIL_LOCK_PCT`, gain verrouillé), puis suit le plus haut atteint à 0,5 % en dessous (`SUPSW_TRAIL_PCT`). Sortie : « SWING SUPPORT TRAILING OBJECTIF ». La durée max (5 j) reste active ; la sortie anticipée « CASSE (flux) » est suspendue une fois le gain verrouillé. `SUPSW_TRAIL_ON = 0` rétablit l'objectif fixe. Redémarrage : le niveau du stop suiveur est sauvegardé (au plus une fois par minute) ; une position reconstruite au démarrage repart du stop d'origine et se ré-arme si le prix est au-dessus de l'objectif.
- **Réentrée après un gain** (`SUPSW_REENTRY_ON`, 1 par défaut) : pendant 12 h (`SUPSW_REENTRY_HOURS`) après une sortie gagnante sur un actif, un nouveau signal n'est accepté que si le prix est au moins à (prix de sortie − 0,2 %) (`SUPSW_REENTRY_TOL_PCT`), donc « ça continue de monter ». Si le prix a baissé, le bot attend la fin de la baisse : point bas fait depuis au moins 2 bougies 1 h et dernière bougie 1 h verte. Le refus apparaît dans le journal (« baisse apres un gain … ») et la carte du mode.
- **Journal / export** : deux colonnes ajoutées (`peak_pct` = plus haut atteint pendant le trade en %, `trail_armed`), pour comparer chaque trade au gain qu'aurait donné l'objectif fixe. Les colonnes sont créées automatiquement au démarrage.
- Les 6 réglages sont modifiables dans Réglages ; le texte du mode (Trading > Swing Support) indique le trailing et la réentrée.
- Limite : l'export historique ne contient pas le chemin du prix, l'effet du trailing se mesure donc sur les trades à venir.

## v4.390 — Trades manuels ouverts : pic atteint et modification du TP

Fichiers : `api.py`, `index.html`, `index_src.html`, `README.md`.

- La carte d'un trade manuel ouvert (onglet « Tous » et volet du mode) affiche maintenant le **pic** : « pic +2,50 % (prix · gain en $) », c'est-à-dire le meilleur gain atteint depuis l'ouverture, en % du prix, en prix et en dollars.
- Bouton **« Modifier le TP »** sur la même carte : saisie du nouveau prix puis Enregistrer ; champ vide = retirer le TP. Le serveur refuse un TP du mauvais côté du prix actuel (message affiché). Le TP est géré par le bot (il n'est pas posé sur Hyperliquid), donc la modification est immédiate en paper comme en live.
- SL, trailing, verrou et règle de tendance restent modifiables dans l'onglet Trading Manuel (inchangé).
- `GET /api/paper/portfolio` : `manual_open` renvoie aussi `peak_pct`, `qty`, `armed`. Aucun changement de moteur.

## v4.391 — Swing Support : entrée seulement loin de la résistance, objectif = la résistance

Fichiers : `bot_engine.py`, `db.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- **Distance à la résistance** : pour chaque signal, le bot calcule la prochaine résistance Daily AU-DESSUS de la zone de support et du prix (mêmes zones que le reste du bot) et enregistre `res_low` (niveau) et `res_dist_pct` (distance en % du prix) dans le journal et dans l'export CSV, pour TOUS les signaux, pris ou refusés. Les colonnes sont créées au démarrage. Sans résistance au-dessus, l'entrée n'est pas limitée.
- **Filtre d'entrée** (`SUPSW_RES_FILTER_ON`, 1 par défaut) : entrée refusée si la résistance est à moins de `SUPSW_RES_MIN_PCT` (3 %) au-dessus du prix : « resistance Daily trop proche … retournement probable ». Les signaux refusés sont quand même mesurés à 24 h / 72 h : après quelques jours on pourra voir, avec les colonnes MFE/MAE de ces lignes, s'ils auraient perdu (et donc si un SHORT à la résistance aurait gagné).
- **Viser la résistance** (`SUPSW_RIDE_ON`, 1 par défaut) : l'objectif devient (résistance − 0,3 %), plafonné à 6 % (`SUPSW_RIDE_MAX_PCT`), seulement s'il est plus loin que l'objectif fixe de 1 %. À +1 % le gain est verrouillé (stop à +0,5 %, comme en v4.389), puis le stop suit le plus haut à 1 % (`SUPSW_RIDE_TRAIL_PCT`, large pour ne pas être sorti par le bruit) jusqu'à la résistance. Une fois la résistance atteinte, le stop se resserre à 0,5 % sous le plus haut (`SUPSW_TRAIL_PCT`). Sortie : « SWING SUPPORT TRAILING OBJECTIF ».
- Après un redémarrage, une position reconstruite repart avec l'objectif = résistance comme seuil de verrou (le verrou à +1 % n'est pas rejoué).
- **SHORT à la résistance** : NON implémenté. Il est mesuré d'abord via les signaux refusés (voir ci-dessus).
- Limite : ces règles ne peuvent pas être testées sur l'historique (les exports n'ont pas de bougies) ; elles sont mesurées sur les prochains signaux. Si peu d'entrées passent le filtre, baisser `SUPSW_RES_MIN_PCT`.

## v4.392 — Swing Support : porte BTC et contrôle des dernières heures

Fichiers : `bot_engine.py`, `db.py`, `api.py`, `README.md`.

- **Porte BTC** (`SUPSW_BTC_GATE_ON`, 1 par défaut) : entrée refusée si la variation de BTC sur 24 h est inférieure à `SUPSW_BTC24_MIN` (0 %) ou si la tendance H4 de BTC n'est pas conforme à `SUPSW_BTC_H4_MODE` (0 = ignorée, 1 = refus si baissière, **2 = haussière exigée**, valeur par défaut). Si la donnée manque, l'entrée n'est pas bloquée.
- **Dernières heures** (`SUPSW_RECENT_ON`, 1 par défaut) : entrée refusée pendant une chute en cours : l'actif a perdu plus de 1,5 % (`SUPSW_RECENT_ASSET_MIN_PCT`) ou BTC plus de 0,5 % (`SUPSW_RECENT_BTC_MIN_PCT`) sur les 4 dernières heures (`SUPSW_RECENT_HOURS`), mesuré sur les bougies 1 h clôturées.
- **Journal / export** : deux colonnes ajoutées, `chg_recent` (actif) et `btc_chg_recent` (BTC) sur la fenêtre, enregistrées pour tous les signaux (pris ou refusés) afin de calibrer les seuils. Les signaux refusés restent mesurés à 24 h / 72 h.
- Ordre des contrôles : pause après perte → BTC 24 h → H4 de BTC → chute actif → chute BTC → résistance proche (v4.391) → réentrée après gain (v4.389).
- Rejeu sur l'export du 14/10 (15 trades pris avec contexte BTC) : BTC 24 h ≥ 0 seul garde 7 trades (−7,5 % au lieu de −18,2 %) ; avec H4 haussière exigée, 4 trades (−6,2 %). Les seuils « chute en cours » ne peuvent pas être rejoués (colonne nouvelle). Ces deux sous-ensembles restent négatifs : l'échantillon est petit et issu d'une même phase de baisse.
- Critère d'arrêt proposé : environ 20 trades avec ces règles ; si le total net de frais n'est pas positif, Swing Support est abandonné. Si trop peu d'entrées passent, assouplir `SUPSW_BTC_H4_MODE` (2 → 1) en premier.

## v4.393 — Top-down (Spot-Accum, Accumulation, Swing) : verrou de gain en % du prix

Fichiers : `bot_engine.py`, `api.py`, `README.md`.

- **Constat** (cas UNI) : le verrou existant (v4.340) ne s'arme qu'à +1R. Avec un stop large (R de 3 à 4 % du prix), il n'armait qu'à +3 % de gain : les pics à +1,4 % et +1,9 % n'avaient aucune protection et ont fini en perte (« STRUCTURE CASSEE »), et le pic de +4,15 % n'a été rendu que pour moitié.
- **Nouveau verrou en %** (`MTF_PCT_LOCK_ON`, 1 par défaut) : dès que le meilleur gain atteint `MTF_PCT_LOCK_ARM_PCT` (1 % du prix), le SL monte pour garantir 60 % du pic (`MTF_PCT_LOCK_FRACTION`), 75 % au-delà d'un pic de 3 % (`MTF_PCT_LOCK_BIG_PCT` / `MTF_PCT_LOCK_BIG_FRACTION`), et jamais moins que +0,15 % de l'entrée (`MTF_PCT_LOCK_FLOOR_PCT`, frais couverts). Il ne fait que monter (long) / descendre (short), se combine avec le verrou en R et le suiveur existants (le plus favorable l'emporte), et marque la position « SL remonté » : la sortie s'appelle « STOP SUIVEUR (top-down) ». En live, le SL natif est mis à jour par le mécanisme existant.
- **Estimation sur l'export du 12/10** (82 trades top-down Spot-Accum / Accumulation, net actuel −12,6 $) : armement à 1 % et 60 % du pic ≈ +4 $ ; armement à 0,8 % ≈ +4,6 $ ; à 0,6 % ≈ +6,9 $. C'est une **borne haute** : l'export donne le pic et la sortie, pas le chemin ; un stop plus serré peut aussi couper des trades qui seraient repartis, et les frais ne sont pas retranchés.
- Tous les moteurs top-down sont concernés (pas seulement le profil Swing). Pour désactiver : `MTF_PCT_LOCK_ON = 0`.

## v4.394 — Affichage du PnL des trades manuels : « $ (%) » comme sur Hyperliquid

Fichiers : `index.html`, `index_src.html`, `README.md`.

- Dans les trades manuels ouverts (onglet Trading Manuel, onglet « Tous » et volet du mode), le PnL s'affiche désormais **d'abord en $, puis en % entre parenthèses**, comme sur Hyperliquid : « +0,351 $ (+2,06 %) ». Avant, l'onglet Trading Manuel affichait le % avant le $ et le volet du mode aussi.
- Le % est maintenant le **% de marge (ROE = mouvement de prix × levier)**, comme Hyperliquid et comme les trades du bot ; à levier ×1 il est identique au mouvement de prix. Le pic affiché suit la même règle : « 🏔️ pic +0,41 $ (+1,44 %) · prix … ».
- Les trades du bot et les historiques étaient déjà au format « $ (%) » : inchangés.

## v4.395 — Trades manuels : « laisser courir au TP » (trailing après l'objectif)

Fichiers : `manual_trading.py`, `api.py`, `index.html`, `index_src.html`, `README.md`.

- **Constat** : le trailing de la v4.389 ne concerne que le Swing Support du bot (paper). Un ticket manuel préparé depuis une alerte Swing Support n'avait qu'un TP fixe (sans TTP), qui fermait tout le trade à l'objectif.
- **Nouvelle option « LAISSER COURIR AU TP »** sur tout ticket manuel qui a un TP (case dans le ticket, sous le TP). Elle est **cochée par défaut pour les tickets Swing Support** (verrou +0,5 % et suiveur 0,5 %, repris de `SUPSW_TRAIL_LOCK_PCT` / `SUPSW_TRAIL_PCT`) ; décochée pour les autres, à cocher à la main.
- **Fonctionnement** : quand le prix touche le TP, le trade n'est plus fermé. Le SL monte aussitôt à +verrou % au-dessus de l'entrée (en SHORT : en dessous), puis suit le plus haut atteint à N % de distance. La sortie s'appelle « TRAILING TAKE PROFIT (manuel) ». LONG et SHORT, paper et live. En **live**, le SL natif Hyperliquid est mis à jour à chaque palier (au moins `MANUAL_LOCK_PUSH_MIN_STEP_PCT`, 0,05 %), donc le stop reste protégé même si le bot s'arrête ; le TP, lui, n'est pas un ordre natif (inchangé).
- **Trades déjà ouverts** : bouton « Modifier » (onglet Trading Manuel) : case « laisser courir au TP » + verrou et suiveur. Le verrou doit rester inférieur à la distance du TP. Retirer le TP désactive l'effet.
- Le trade ouvert affiche « laisser courir » puis « armé » une fois l'objectif atteint. Aucun changement pour les ordres sans cette option.

## v4.396 — Trades manuels : alerte si « laisser courir au TP » n'est pas pris en compte

Fichiers : `index.html`, `index_src.html`, `README.md`.

- Après « Enregistrer » dans la modification d'un trade manuel ouvert, si la case « laisser courir au TP » était cochée et que le serveur ne la confirme pas, un message l'explique. Cause typique : seuls les fichiers de l'interface (`index.html`) ont été déposés, pas `api.py` et `manual_trading.py` de la v4.395 : l'ancien serveur ignore silencieusement les champs qu'il ne connaît pas, et la case revient décochée à la réouverture.
- Aucun changement côté serveur.

## v4.397 — Version affichée par /health

Fichiers : `bot_engine.py`, `README.md`.

- `BOT_VERSION` (4.299 jusque-là, jamais mis à jour) et `BOT_BUILD` reflètent désormais la version déployée : `GET /health` renvoie `"version": "4.397"` et `"build": "2026-10-14-a"`. Ouvrir `https://<ton-app>/health` permet de confirmer que Railway fait bien tourner le dernier code (utile quand une nouveauté de l'interface ne semble pas prise en compte côté serveur).
- Vérifié de bout en bout (requête HTTP simulée, route `PUT /api/manual/positions/{id}`) : « laisser courir au TP » est bien enregistré et renvoyé par le serveur de la v4.395 ; si ce n'est pas le cas chez toi, c'est que le serveur déployé n'est pas à jour.

## v4.398 — « Laisser courir au TP » : message clair quand il n'y a pas de TP
- Cette option n'a de sens qu'avec un TP. Sans TP, le serveur refuse avec un message explicite (avant : option ignorée en silence) et la case est grisée dans le formulaire (« saisis d'abord un TP »).
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.399 — Forex : sortie « SANS PROGRES »
- Constat (export 15/10, 248 trades Forex) : 48 trades restés > 2 h sans jamais atteindre +0,15 % de pic finissent à −0,21 % en moyenne (−3,55 $ bruts). Les frais estimés (6,8 $) dépassent le PnL brut (−0,64 $).
- Nouveau : si, après `FOREX_STALE_MIN` (90 min), le pic du trade reste sous `FOREX_STALE_PEAK_PCT` (0,15 %, = seuil d'armement du suiveur), le trade est fermé. Motif affiché : « FOREX SANS PROGRES ». Interrupteur `FOREX_STALE_ENABLED` (1/0) dans les réglages (libellés « Forex · SANS PROGRES »).
- Ne touche ni l'entrée, ni le suiveur, ni le stop dynamique, ni les autres modes. Version affichée par /health : 4.399.
- À mesurer : comparer, après ~1–2 semaines, le net des sorties « FOREX SANS PROGRES » à celui des anciens trades > 2 h sans armement. Le gain réel n'est pas chiffrable à l'avance (l'export n'a pas le prix pendant le trade).
- Fichiers : bot_engine.py, api.py, README.md

## v4.400 — Ordres programmés modifiables
- Bouton **Modifier** sur chaque ligne de « ⏰ Ordres programmés » (Trading Manuel) : prix ou heure de déclenchement, taille, levier, SL %, TP %, « laisser courir au TP » (verrou + suiveur), TTP, et prolongation de l'expiration (« expire dans … h »).
- Mêmes validations qu'à la création (SL 0,05–50 %, TP 0,05–500 %, levier max, 10 $ mini en live, heure dans le futur, « laisser courir » exige un TP). En cas d'erreur, rien n'est modifié.
- Le sens, l'actif, le marché, le mode (paper/live) et le type de déclenchement ne se modifient pas : annuler puis recréer l'ordre.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.401 — Ordres programmés : changer le sens (LONG/SHORT) et le mode (PAPER/LIVE)
- Dans « Modifier » d'un ordre programmé : listes **SENS** et **MODE**.
- Passage à **LIVE** : confirmation obligatoire (fenêtre d'avertissement), connexion Hyperliquid requise, 10 $ de notionnel minimum, et aucune autre position live sur l'actif. Retour à PAPER : sans confirmation.
- Inverser le sens d'un ordre issu d'une opportunité du bot en fait un ordre libre (il ne suit plus l'opportunité). Le spot reste LONG uniquement.
- Une position déjà **ouverte** ne peut pas changer de sens ni de mode : la fermer puis ouvrir l'autre.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.402 — Ordres programmés : critères d'entrée modifiables
- Dans « Modifier » d'un ordre programmé : type de déclenchement (prix ≥, prix ≤, heure précise) avec son prix ou son heure, **condition de tendance H4 à l'entrée** (aucune / haussière / baissière / neutre), case « annuler si l'opportunité du bot n'est plus valable au déclenchement » (ordres issus d'une opportunité), ainsi que verrou progressif et règle de sortie sur la tendance.
- Mêmes validations qu'à la création ; en cas d'erreur rien n'est modifié.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.403 — « Laisser courir au TP » : champs pré-remplis
- Cocher « laisser courir au TP » remplit automatiquement les champs manquants avec les valeurs par défaut : TP = 1 % (ticket et ordre programmé) ou ±1 % autour du prix pour une position ouverte, gain verrouillé = +0,5 %, suiveur = 0,5 %. Une valeur déjà saisie n'est jamais écrasée.
- La case n'est plus grisée quand le TP est vide : elle crée le TP par défaut.
- Fichiers : bot_engine.py (version), index_src.html, index.html, README.md

## v4.404 — TP1 / TP2 (clôture partielle) pour les trades manuels
- **TP1** : à ce niveau, une part de la position (5–95 %, 50 % par défaut) est clôturée au marché ; le **TP existant devient le TP2** (le reste). Option (cochée par défaut) : le SL remonte au seuil de rentabilité (entrée + frais) après le TP1. « Laisser courir au TP » s'applique au TP2.
- Disponible : ticket (case « TP1 — clôture partielle », pré-remplie à la moitié du TP), ordres programmés (Modifier), positions ouvertes (Modifier ; non modifiable une fois encaissé). Affichage « TP1 … / TP2 … », « TP1 ✔ encaissé +x $ ».
- Le PnL affiché et le PnL final incluent la part encaissée au TP1 ; frais comptés au prorata. Motif de sortie « STOP APRES TP1 (manuel) » si le stop touche après le TP1.
- Spot LIVE : refusé (clôture partielle non gérée) ; paper et perp live acceptés.
- Live perp : la clôture partielle utilise un ordre au marché sur la quantité exacte et le SL natif est renvoyé avec la nouvelle taille. **À valider d'abord avec une petite position live** (non testé sur Hyperliquid depuis cet environnement).
- En cas d'échec de la clôture partielle : position inchangée, nouvel essai après 60 s.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.405 — TP1/TP2 : saisie en prix
- Ticket : le TP1 a maintenant un champ « prix » à côté du « % » (comme le SL et le TP), les deux se convertissent l'un en l'autre.
- Ordre programmé (Modifier) : SL, TP/TP2 et TP1 acceptent le prix ou le %. Référence du calcul : le prix de déclenchement (ordres sur prix) ou le prix actuel (ordres sur heure).
- Les niveaux restent stockés en % de l'entrée : si l'ordre s'exécute au marché à un prix légèrement différent du déclenchement, les prix réels de SL/TP/TP1 sont décalés d'autant.
- Fichiers : bot_engine.py (version), index_src.html, index.html, README.md

## v4.406 — Résumé instantané d'un actif (fenêtre)
- **Clic sur un actif** (tuiles « Prix en temps réel », position du tableau de bord, tickers de Trading Manuel : opportunités, positions, ordres programmés, historique, bouton « 📊 Résumé de l'actif » de l'ordre libre) → **fenêtre** avec bouton **✕ Fermer** (clic à l'extérieur ou Échap aussi).
- Contenu, actualisé toutes les 15 s : prix, variations 1 h / 4 h / 24 h / 7 j, plages 24 h et 7 j, et pour **H1, H4 et H24 (D1)** : tendance (EMA50/200 + prix live), RSI, ATR, **support et résistance** les plus proches (distance en %, « dans la zone » si le prix y est). Contexte : tendance BTC H4, funding annualisé.
- **Conseil LONG / SHORT / ATTENDRE** : score à règles explicites (tendance H4 ×2, D1 ×1, H1 ×1, RSI H4 en surachat/survente, prix sur support/résistance H4/H1, BTC, funding extrême). ≥ +3 → LONG, ≤ −3 → SHORT, sinon ATTENDRE (avec le côté vers lequel ça penche). Les raisons sont listées, avec repères de SL/objectif/gain-risque tirés des zones H4. **Indicatif, pas une garantie.**
- Boutons **▲ Préparer un LONG / ▼ Préparer un SHORT** : ouvrent Trading Manuel avec un ticket libre pré-rempli (valeurs par défaut, rien n'est envoyé tant que vous ne validez pas) et **ferment la fenêtre**.
- Nouvelle route lecture seule : `GET /api/asset/summary?ticker=…`.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.407 — Résumé d'actif : « ATTENDRE » expliqué + situations programmables
- Le conseil **ATTENDRE** dit maintenant **pourquoi** (signaux partagés, score insuffisant, ou bonne direction mais entrée immédiate trop chère).
- **Nouveau critère** : un LONG/SHORT dont le gain/risque d'une entrée immédiate (stop et objectif sur les zones H4) est < 1,2 devient ATTENDRE. Seuil de score inchangé (±3).
- Bloc **« Situations à attendre »** (zones H4) : LONG sur rebond du support, SHORT sur rejet de la résistance, LONG sur cassure de la résistance, SHORT sur cassure du support. Chacune : prix de déclenchement (et distance), stop au-delà de la zone, objectif sur la zone suivante, gain/risque, mention « contre-tendance » si elle va contre la tendance H4.
- Bouton **⏱ Programmer** : ouvre le ticket libre avec déclenchement (prix ≤ / ≥), SL et TP déjà remplis, validité 72 h. Rien n'est envoyé avant votre validation.
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.408 — Résumé d'actif : arguments « pour / contre » par scénario
- Chaque situation à attendre (rebond de support, rejet de résistance, cassure haussière, cassure baissière) affiche un **bilan « n pour · n contre »** et la liste des arguments.
- Critères : tendance H4 et D1 (dans le sens ou à contre-courant), RSI H4 (survente/surachat pour un rebond, élan sans excès pour une cassure), nombre de contacts de la zone (confirmée 2–4, usée ≥ 5), même zone retrouvée en H1/D1, bougie de rejet H4 (mèche) pour les rebonds, volume H4 récent et compression de volatilité pour les cassures, tendance BTC H4, funding extrême.
- Règles usuelles d'analyse technique, **non validées statistiquement** ; elles n'entrent pas dans le score du conseil. À confronter aux résultats réels des trades programmés. Le retest d'une zone cassée n'est pas détecté.
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.409 — Pré-programmation unique, choisie sur l'historique de l'actif
- La fenêtre « résumé d'actif » ne met plus les quatre situations au même niveau : elle **retient la plus favorable** (carte ⭐) avec un bouton **⏱ Pré-programmer** (ticket rempli : déclenchement, SL, TP, 72 h). Les autres sont repliées avec la raison de leur non-sélection.
- **Méthode** : chaque situation (rebond de support, rejet de résistance, cassure haussière, cassure baissière) est rejouée sur jusqu'à ~1000 bougies H4 (~6 mois) de l'actif : zones recalculées à chaque bougie avec les seules données connues alors, déclenchement dans les 72 h, puis stop / objectif / sortie à 96 h (stop prioritaire si les deux sont touchés dans la même bougie), frais 0,09 % déduits. Un même épisode n'est compté qu'une fois. Résultat : nombre de cas, % d'objectifs atteints, espérance nette en R.
- **Sélection** : espérance nette > 0, ≥ 8 cas, gain/risque actuel ≥ 1 ; classement par espérance pondérée par la taille de l'échantillon (+ léger bonus au contexte pour/contre). Sinon : « Aucune situation ne ressort — ne rien programmer est un choix valable ».
- Calcul mis en cache par bougie H4 ; mêmes règles de niveaux pour le direct et l'historique (`_levels`).
- Limites : échantillon souvent petit, le passé ne garantit rien, l'historique est propre à l'actif (pas de filtre de tendance).
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.410 — Pré-programmation : historique plus court (90 j) et confirmation récente
- Profondeur de l'historique rejoué ramenée de ~6 mois à **90 jours par défaut**, réglable (réglage `ASSET_BT_DAYS`, 30 à 180 jours). Les 150 bougies H4 précédentes ne servent qu'à calculer les zones.
- **Confirmation récente** : une situation n'est retenue que si son espérance nette n'est pas négative sur la **moitié la plus récente** de la fenêtre (dès 4 cas) ; sinon rejet « le régime a peut-être changé ». Le résultat de la moitié récente est affiché.
- Le nombre de jours affiché est celui de la fenêtre jugée.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version + réglage), index_src.html, index.html, README.md

## v4.411 — Sortie manuelle LIVE : vrai prix Hyperliquid retrouvé
- Constat : quand le **stop natif** Hyperliquid se déclenchait avant que le bot ne ferme, la position était déjà absente → le bot reprenait son propre prix du moment et affichait « sortie estimée par le bot », alors que la sortie avait bien eu lieu sur Hyperliquid.
- Correctif : à la fermeture, si aucun remplissage n'est renvoyé, le bot lit les **remplissages du compte** (dernier groupe de clôture sur l'actif) : prix moyen réel, frais réels. La sortie est alors « confirmée par Hyperliquid ».
- **Rattrapage** : une fois par heure, les trades manuels live fermés (14 derniers jours) marqués « estimée » sont recontrôlés ; si le remplissage existe, prix de sortie, PnL et source sont corrigés (fiche manuelle et table des trades). Un seul essai par trade.
- Si le remplissage reste introuvable, le libellé devient « estimée par le bot (remplissage Hyperliquid introuvable) ».
- Fichiers : manual_trading.py, db.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.412 — Levier refusé par Hyperliquid : le suivi prend le levier réel
- Constat : le SDK ne lève pas d erreur quand Hyperliquid refuse un levier, il renvoie un statut « err ». Le refus passait inaperçu : l'ordre partait avec le levier déjà en place sur Hyperliquid, mais le trade restait suivi avec le levier demandé (marge, % de marge et PnL faux).
- Correctif (trades manuels live perp, ticket et ordres programmés) :
  1. refus détecté et journalisé avec la raison donnée par Hyperliquid ;
  2. si le levier demandé dépasse le maximum de l'actif, nouvel essai au **levier maximal** de l'actif ;
  3. après l'ouverture, le **levier réel de la position** lu sur Hyperliquid devient le levier du trade (le notionnel ne change pas ; la marge utilisée en dépend).
- Affichage : « ⚠ demandé x5 » à côté du levier réel dans les positions ouvertes (survol = explication).
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.413 — Alignement du levier sur les positions déjà ouvertes
- Cas constaté : #35 TAO suivi en x10 alors que la position Hyperliquid est en x1 (levier refusé à l'ouverture, avant le correctif v4.412).
- Toutes les 5 minutes, le levier **réel** de chaque position manuelle live (perp) est lu sur Hyperliquid ; s'il diffère du suivi, le suivi est aligné (levier, marge, % de marge, PnL en %), le trade enregistré en base est mis à jour, et un avertissement « ⚠ demandé xN » s'affiche. Le notionnel ne change pas. Couvre aussi un levier modifié à la main sur Hyperliquid.
- Fichiers : manual_trading.py, db.py, bot_engine.py (version), README.md

## v4.414 — Levier réel : recoupement par la marge utilisée + bouton de vérification
- La v4.413 ne corrigeait pas le cas #35 TAO : Hyperliquid annonçait peut-être x10 dans le champ « leverage » alors que la marge réellement utilisée correspond à x1. Le levier effectif est maintenant **recoupé** : valeur de position ÷ marge utilisée ; si l'écart dépasse 30 % avec le champ annoncé, le levier effectif fait foi (journal : « levier annoncé xN mais marge utilisée => xM effectif »).
- Nouveau bouton **🔍 Vérifier le levier réel** (en-tête « Positions manuelles ouvertes ») : affiche pour chaque position ce que renvoie Hyperliquid (objet levier brut, marge utilisée, valeur de position, levier effectif) et la synthèse du compte, puis aligne le suivi immédiatement. Route : `GET /api/manual/leverage-check`.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version + marge utilisée), index_src.html, index.html, db.py, README.md

## v4.415 — Trades manuels : réglages Swing Support par défaut
- Tout nouveau ticket manuel **crypto** s'ouvre par défaut avec les réglages du mode Swing Support : levier (`SUPSW_LEVERAGE`), stop et objectif (`SUPSW_SL_PCT` / `SUPSW_TP_PCT`, ou échelle selon le levier si activée), taille (`SUPSW_RISK_PCT`, plafond `SUPSW_MAX_NOTIONAL_USD`), « laisser courir au TP » (verrou et suiveur du mode), sans trailing classique. Tout reste modifiable avant validation.
- **LONG** : le trade est étiqueté « Swing Support ». **SHORT** : le mode n'achète que ; le SHORT reçoit les mêmes réglages mais garde l'étiquette « Libre ».
- Valable pour le formulaire « Ordre libre », la fenêtre « résumé d'actif » (Préparer LONG/SHORT, Pré-programmer) et le repli des alertes expirées. Les tickets issus d'une opportunité d'un autre mode (Spot-Accum, Accumulation, Funding) gardent les réglages de ce mode ; les marchés Forex (`xyz:`) gardent les réglages libres.
- Nouveau réglage `MANUAL_DEFAULT_SWING` (1 = oui, 0 = ancien comportement).
- Fichiers : manual_trading.py, api.py, bot_engine.py (version + réglage), index_src.html, index.html, README.md

## v4.416 — Levier : remise sur Hyperliquid, raison visible, diagnostic complet
- Quand le levier réel d'une position manuelle live diffère du levier voulu, le bot **tente d'abord de remettre le levier voulu sur Hyperliquid** (une fois par trade, ou à chaque clic sur « Vérifier le levier réel »). S'il est refusé, la **raison donnée par Hyperliquid** est affichée sous la position (« ⚠ Hyperliquid refuse x10 : … ») ; repli au levier maximal de l'actif ; le suivi s'aligne ensuite sur le levier réellement appliqué.
- Le diagnostic s'affiche désormais dans la page (texte sélectionnable, boutons Copier / Fermer) et contient : position brute Hyperliquid, réglage de levier de l'actif (`activeAssetData`), levier maximal, levier effectif (valeur ÷ marge), levier suivi avant/après et note.
- Fichiers : manual_trading.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.417 — Levier : maximum Hyperliquid vérifié avant l'ordre
- Constat (#34 PENDLE, #35 TAO) : le levier demandé (x10) dépassait le maximum Hyperliquid de l'actif (x5) → « Invalid leverage value ». Le suivi (v4.416) s'alignait ensuite sur le levier réel, mais l'écart était évitable.
- Le **levier maximal de l'actif** (méta Hyperliquid, cache 10 min) est maintenant affiché sur le ticket (« maximum Hyperliquid pour PENDLE : x5 », en rouge si dépassé) et **vérifié à la création** d'un ordre et à la modification d'un ordre programmé : message clair « Levier x10 impossible : Hyperliquid limite PENDLE à x5 » au lieu d'un ordre qui part avec un autre levier. Marchés `xyz:` : pas de contrôle (méta d'un autre DEX).
- Les mécanismes de rattrapage de la v4.416 (repli au maximum, alignement du suivi, raison affichée) restent en place pour les cas résiduels.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.418 — Libellé du levier simplifié
- Les deux avertissements (« ⚠ demandé x10 » et « ⚠ Hyperliquid refuse x10 … ») sont remplacés par une seule mention : **« Levier disponible : xN »**. Le détail (raison du refus) reste visible au survol.
- Fichiers : bot_engine.py (version), index_src.html, index.html, README.md

## v4.419 — Programmation : ouvrir au rebond (ou au recul) confirmé, pas pendant la chute
- Deux nouveaux types de déclenchement, pour le ticket et la modification d'un ordre programmé :
  - **« Descend à…, puis REBONDIT »** (`bounce_up`) : quand le prix atteint le niveau, l'ordre est **armé** ; le bot suit le **point bas** et n'ouvre que lorsque le prix a **remonté** de X % depuis ce point bas (défaut 0,3 %, réglable 0,05–10 %). Aucune ouverture pendant que le prix continue de descendre.
  - **« Monte à…, puis RECULE »** (`bounce_down`) : symétrique (point haut, recul de X %), pour un SHORT sous une résistance.
- L'état armé et le point extrême sont mémorisés (survivent à un redémarrage) et affichés dans le tableau des ordres programmés (« ARMÉ (bas …) »).
- Fenêtre « résumé d'actif » : les situations **rebond de support** et **rejet de résistance** utilisent désormais ce déclenchement confirmé (0,3 %) ; les cassures restent sur prix. L'historique rejoué (méthode de la v4.409) simule la confirmation : entrée à la clôture de la bougie H4 de confirmation (prudent, évite un biais d'anticipation), pas d'entrée si le niveau du stop cède avant la confirmation. Sur des prix aléatoires, ces situations donnent une espérance proche des seuls frais, comme attendu.
- Limite : le stop et l'objectif d'un ordre programmé sont des % de l'entrée réelle ; l'entrée étant plus haute que le niveau (de la confirmation), leurs prix s'en trouvent légèrement décalés.
- Fichiers : manual_trading.py, api.py, bot_engine.py (version), index_src.html, index.html, README.md

## v4.420 — TP1 : minimum de 10 $ d'Hyperliquid
- Cause de l'erreur « Order must have minimum value of $10 » : la clôture partielle TP1 (ex. 50 % d'une position de ~15 $ ≈ 7 $) passait sous le minimum de 10 $ par ordre d'Hyperliquid, même en reduce-only. Le bot retentait toutes les 60 s sans succès.
- Création/modification : un TP1 est refusé en LIVE perp si la part clôturée ou le reste est < 10 $, avec la taille minimale requise.
- Exécution : si la situation se présente quand même (prix ayant évolué), le TP1 est ignoré proprement (une seule trace dans le journal), plus de boucle d'essais ; la position continue vers le TP2 et le SL.
- Fichiers : manual_trading.py, bot_engine.py (version), README.md

## v4.421 — Mode SCALP CRYPTO (paper par défaut, passage en live par bouton)
Fichiers : `scalp_crypto.py` (nouveau), `manual_trading.py`, `api.py`, `bot_engine.py`, `index_src.html`, `index.html` (généré), `README.md`.

- **Principe** : beaucoup de petits gains — **x3, taille 60 $, TP fixe 0,6 % du prix, stop 1 %** (≈ +0,31 $ net par trade gagnant, ≈ −0,65 $ par stop). Seuil de rentabilité ≈ 66 % de trades gagnants.
- **Actifs** : les **15 plus échangés** (volume 24 h Hyperliquid) parmi les actifs suivis, réévalués toutes les 30 min.
- **Entrées rapides** (bougies 5 min, zones support/résistance H1) :
  - **Rebond** : le prix touche une zone et une bougie de retournement se confirme (avalement, marteau/étoile).
  - **Cassure confirmée** : clôture 5 min au-delà de la zone, corps fort, volume ≥ 1,5× la moyenne, entrée au plus à 0,4 % du niveau (on ne court pas après le mouvement).
  - **Note 0-100** (tendance H1 et 15 min, volume, contacts de la zone, BTC, place avant la zone suivante) ; seuil 62. La note n'est **pas calibrée** : c'est le paper qui mesure le vrai taux de réussite.
  - **Signaux propices** : volatilité 15 min minimale (0,6 % atteignable), au moins 0,9 % de place avant la zone opposée.
- **Tranches horaires UTC** réglables (défaut 7-11 et 13-21) ; statistiques par heure pour affiner.
- **Garde-fous** : 4 positions max, 40 trades/jour, perte journalière max 3 $, pause 60 min après 3 pertes de suite, attente 30 min par actif, durée max 90 min par trade.
- **Interface** : carte « ⚡ SCALP CRYPTO » en haut de l'onglet Trading manuel : ACTIF/ARRÊTÉ, PAPER/LIVE (confirmation obligatoire), résultats du jour/total, rebond vs cassure, résultats par heure, journal des signaux, tous les réglages. **Désactivé par défaut** : à activer depuis la carte.
- Les trades passent par le moteur manuel (stratégie « Scalp ») : SL natif en live, historique, prix de sortie réels.
- Limite : testé sur données simulées uniquement (pas d'accès au marché ici) ; aucune performance réelle n'est garantie.

## v4.422 — Scalp : plus de plafond de positions, doubles ouvertures bloquées par mode, capital en live
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version, `SCALP_MAX_OPEN` = 0), `api.py`, `index_src.html`, `index.html`, `README.md`.

- **Plus de limite de positions simultanées** (`SCALP_MAX_OPEN` = 0 : illimité). Si ce réglage avait été enregistré à 4 depuis la carte, le remettre à 0.
- **Doublons bloqués seulement dans le même mode** : une position paper sur un actif n'empêche plus d'ouvrir le même actif en live, et inversement. L'attente par actif (30 min) est aussi séparée par mode.
- **En live, le capital limite** : avant chaque ouverture, le bot lit la marge libre d'Hyperliquid et n'ouvre pas si elle est inférieure à la marge requise (60 $ / levier). Le refus apparaît dans le journal des signaux. Les autres garde-fous (perte journalière, série de pertes, trades/jour, tranches horaires) restent actifs.

## v4.423 — Scalp : les données passées motivent (note) au lieu de bloquer
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `index_src.html`, `index.html`, `README.md`.

- **Ne bloquent plus** : volatilité 15 min minimale, place minimale avant la zone suivante, note minimale (réglages `SCALP_MIN_ATR15_PCT`, `SCALP_MIN_ROOM_PCT`, `SCALP_MIN_SCORE` = **0 = désactivés** par défaut ; restent disponibles comme filtres facultatifs).
- **Ils comptent dans la note** (affichée dans le journal et enregistrée sur chaque trade) : marché calme −4 / actif +4, peu de place avant la zone suivante −8 / beaucoup de place +5.
- **Ce qui décide l'entrée** : le type de signal (rebond sur support, rejet de résistance, cassure haussière ou baissière), la zone support/résistance H1 et l'emplacement de l'entrée (bougie 5 min de confirmation ; cassure : au plus 0,4 % du niveau cassé, volume de la bougie de cassure).
- Conservés : l'historique minimal de bougies (technique), la marge libre en live (capital), les garde-fous de perte et de tranches horaires.
- Si les anciens seuils (62 / 0,15 / 0,9) ont été enregistrés depuis la carte Scalp, les remettre à 0.

## v4.424 — FIX : le Scalp analysait des noms d'actifs invalides (« BTC_0 »…) et ne pouvait donc jamais ouvrir de trade
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version), `README.md`.

- Cause : la liste des actifs suivis contient des **clés de slot** (`BTC_0`, `ETH_2`…). Le Scalp les utilisait telles quelles : le classement par volume Hyperliquid ne trouvait aucun actif et les bougies de `BTC_0` ne pouvaient pas être chargées, donc aucun signal n'était évalué.
- Correction : extraction du vrai ticker (`BTC`), suppression des doublons, exclusion de PAXG (or, pas une crypto) et des actifs `xyz:`, puis classement par volume 24 h : les 15 plus échangés.

## v4.425 — Historique manuel : onglets LIVE et PAPER séparés
Fichiers : `manual_trading.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.

- L'historique du Trading manuel (qui contient aussi les trades du Scalp) est divisé en deux onglets **LIVE** et **PAPER**, avec le nombre de trades de chacun.
- Chaque onglet affiche son propre résumé : trades clôturés, % gagnants, PnL.
- Le serveur renvoie les **100 derniers par mode** (avant : 50 au total), pour que le volume du paper Scalp ne repousse pas les trades live.

## v4.426 — Historique manuel : suppression des trades terminés après sélection
Fichiers : `db.py`, `api.py`, `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.

- Dans l'historique du Trading manuel (onglets LIVE / PAPER) : case à cocher par ligne, « Tout sélectionner » et bouton **🗑 Supprimer la sélection (n)** avec confirmation.
- Ne peuvent être supprimés que les éléments **terminés** (clôturés, annulés, expirés, échoués) : une position ouverte ou un ordre programmé n'est jamais touché (contrôle côté serveur).
- La suppression est définitive et retire les lignes de l'historique manuel ; les statistiques du Scalp (calculées depuis cet historique) sont recalculées. L'historique général des trades (tableau de bord / export) n'est pas modifié.

## v4.427 — Historique : détail Scalp de chaque trade
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Sous le motif de chaque trade Scalp de l'historique : type de signal (rebond / cassure), note, **pic de gain atteint** par rapport à l'objectif, et la raison principale du signal. Sert à voir si les pertes viennent d'entrées qui n'ont jamais progressé (signal à revoir) ou de trades qui ont approché l'objectif avant de retomber (objectif ou stop à revoir).

## v4.428 — Scalp : exigence renforcée pour les SHORT
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage **`SCALP_SHORT_MIN_SCORE`** (défaut **62**, 0 = aucune exigence) : un SHORT n'est ouvert que si la note du signal atteint ce seuil ; les LONG gardent le seuil général (`SCALP_MIN_SCORE`, 0 = aucun). Une note de base à ~51 ne passe donc plus pour un short : il lui faut des appuis réels (tendance H1 baissière +12, tendance 15 min +8, volume +10, BTC en baisse +8, zone testée plusieurs fois +6/+10…).
- Un SHORT pris **contre une tendance H1 haussière** perd en plus 8 points.
- Les refus apparaissent dans le journal des signaux (« refuse : note X < 62 (exigence SHORT) »).
- Décision prise sur 3 shorts perdus pour 1 long gagné : échantillon minuscule, à réévaluer avec les résultats rebond/cassure et le pic de gain.

## v4.429 — Scalp : pas de contre-tendance, entrée seulement quand le mouvement se confirme
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.

- **Sans contre-tendance** (`SCALP_TREND_MODE` = 1, défaut) : le sens du trade doit être celui de la tendance H1 (si H1 est neutre : celui de la tendance 15 min). Un long contre une tendance baissière, ou un short contre une tendance haussière, est refusé (« refuse : contre-tendance », visible dans le journal).
- **Confirmation avant d'entrer (rebonds)** (`SCALP_CONFIRM_BARS` = 3 bougies de 5 min, 0 = entrer tout de suite) : le signal est mis en attente. Un SHORT entre quand le prix passe **sous le plus bas** de la bougie de rejet ; un LONG quand il repasse **au-dessus du plus haut** de la bougie de rebond.
- **Mouvement contraire = bruit, mais borné** (`SCALP_NOISE_ATR` = 0,5 × ATR 5 min) : tant que le prix reste sous l'extrême de la bougie signal + cette marge, on attend ; au-delà, ou sans confirmation dans le délai, le signal est abandonné (événements « attente » / « abandon » dans le journal).
- Les cassures confirmées (déjà basées sur une bougie de mouvement) entrent comme avant, mais passent aussi le filtre de tendance.
- Cause du changement : 4 shorts de rebond ouverts ensemble ont tous perdu avec un pic de gain de 0,00 à 0,16 % (le prix est parti à contre-sens dès l'entrée).

## v4.430 — FIX : les statistiques et garde-fous du Scalp ne voyaient aucun trade
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version), `README.md`.
- Cause : les statistiques du Scalp lisaient le statut « closed » dans le contenu JSON des trades, alors que le statut est une colonne de la base (absent du JSON). Résultat : « aucun trade » partout dans la carte.
- Conséquence cachée corrigée en même temps : les **garde-fous qui reposent sur ces statistiques ne fonctionnaient pas** (perte journalière max, pause après X pertes de suite, nombre max de trades par jour). Ils s'appliquent désormais.
- Vérifié sur une base de test avec des trades sans clé « status », comme en réel.

## v4.431 — Scalp : plusieurs positions paper sur un même actif
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version), `README.md`.
- **Paper** : plus aucun blocage « actif déjà ouvert » — le même actif peut être ouvert plusieurs fois.
- **Live** : un actif déjà tenu en live (par le Scalp, un trade manuel ou le bot) n'est pas rouvert (Hyperliquid ne tient qu'une position par actif).
- Un trade paper n'empêche jamais un live, et inversement.
- Reste actif : l'attente par actif (`SCALP_COOLDOWN_MIN`, 30 min), qui évite d'empiler le même signal à chaque bougie ; la mettre à 0 pour la supprimer.

## v4.432 — Scalp : PnL net de frais, pause sur une zone après un stop, stop réduit
Fichiers : `scalp_crypto.py`, `manual_trading.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- **PnL net de frais** : l'historique affiche sous chaque PnL la valeur « net » (frais estimés ≈ 0,09 % du notionnel par aller-retour), le résumé donne brut + net, et la carte Scalp affiche brut / net.
- **Pause sur une zone après un stop** (`SCALP_ZONE_COOLDOWN_MIN`, défaut 60, 0 = off) : après une perte (hors sortie « durée max ») sur un support/résistance, le Scalp ne re-entre pas sur la même zone (±0,3 %) pendant ce délai. Les autres zones du même actif restent ouvertes.
- **Stop par défaut 0,65 %** (avant 1,0 %), objectif 0,6 % : risque ≈ gain. Seuil de rentabilité ≈ 58 % de trades gagnants. Compromis : le stop tombe plus souvent (bruit). Les valeurs déjà enregistrées dans la carte ne changent pas : ajuster « Stop » si besoin.

## v4.433 — Scalp : contre-tendance sécurisée (flux + zone solide + BTC)
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Un trade **contre la tendance** (H1, sinon 15 min) est de nouveau possible, **Paper et Live**, seulement si TOUT est réuni : **rebond** (jamais une cassure) sur une zone **testée au moins 2 fois** ; **flux agressif dans le sens du trade** (long : ≥ 58 % d'achats agressifs sur 10 min ; short : ≥ 58 % de ventes), avec au moins 30 transactions ; **BTC pas contre** (±0,2 % / 1 h) ; **note ≥ 65**. Le rebond garde sa confirmation par le prix. Flux absent ou insuffisant = refus.
- Le flux utilise le WebSocket des transactions du bot ; le Scalp s'abonne tout seul aux actifs du top 15 qui ne sont pas dans la liste du bot (abonnement refait après reconnexion du bot).
- **Réglages** (carte Scalp) : `SCALP_COUNTER_ENABLED` (1 = autorisée, **0 = bloquée**, retour au comportement v4.432), `SCALP_COUNTER_MIN_SCORE` (65), `SCALP_COUNTER_FLOW_MIN` (0,58), `SCALP_COUNTER_MIN_TRADES` (30).
- La carte sépare les résultats **AVEC TENDANCE** / **CONTRE-TENDANCE** ; l'historique marque « CONTRE-TENDANCE ».
- Test sur marchés simulés : sans flux ou flux contraire = 0 contre-tendance ; flux favorable = quelques longs contre-tendance ; réglage à 0 = aucun. Seuils non calibrés sur le vrai marché : juger sur ≥ 30 trades.

## v4.434 — Scalp : laisser courir après l'objectif
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage **`SCALP_RUN_ENABLED`** (défaut 1) : à l'objectif (TP 0,6 %), le trade n'est plus fermé ; son stop remonte à 0,2 % sous le prix, puis suit chaque nouveau plus haut (plus bas pour un short). Sortie quand le prix recule de plus de **`SCALP_RUN_TRAIL_PCT`** (défaut **0,2 %**) depuis son meilleur niveau. Motif de sortie : « TRAILING TAKE PROFIT ». En Live, le stop natif Hyperliquid est mis à jour à chaque palier (mécanisme « laisser courir au TP » existant).
- **La durée max (`SCALP_MAX_HOLD_MIN`) ne s'applique plus** dès que l'objectif est atteint et le laisser courir armé ; elle reste valable avant l'objectif.
- Mettre `SCALP_RUN_ENABLED` à 0 rétablit la fermeture nette à l'objectif. Ne concerne que les nouveaux trades.
- Gain minimal garanti après l'objectif ≈ 0,4 % du prix (avant frais) ; pas de plafond de gain.

## v4.435 — Heure d'ouverture et durée écoulée sur les trades manuels/Scalp ouverts
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Chaque position ouverte (liste « Positions » du trading manuel et carte du trade dans l'onglet « Tous ») affiche « 🕒 ouvert à HH:MM · depuis X h YY min » (la date s'ajoute si l'ouverture date d'un autre jour). Heure locale de l'appareil. Le temps écoulé se rafraîchit à chaque actualisation de l'écran.

## v4.436 — PC : flèches ◀ ▶ et glisser à la souris sur les zones défilables en largeur
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Sur ordinateur (appareil avec souris), au survol d'un tableau ou d'une barre d'onglets plus large que l'écran (historique, trades, onglets…), deux flèches rondes ◀ ▶ apparaissent sur les bords ; un clic fait défiler d'environ 80 % de la largeur. La flèche disparaît quand on est au bout.
- On peut aussi **glisser à la souris** (clic maintenu sur une zone vide du tableau) ; les boutons, champs et liens gardent leur comportement normal.
- Téléphone : rien ne change (défilement tactile habituel). Les flèches sont ajoutées hors de l'interface React, sans toucher aux composants. Testé sur une page d'essai (flèche, clic, glisser) ; pas sur l'application complète.

## v4.437 — Retrait du « glisser à la souris » (sélection du texte rétablie)
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Le glisser à la souris de la v4.436 empêchait de sélectionner le texte à copier : supprimé. Les flèches ◀ ▶ au survol restent (clic = défilement), la sélection du texte au clic-glisser fonctionne comme avant.

## v4.438 — Scalp : pas de short tant que BTC monte
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage **`SCALP_SHORT_BTC_BLOCK`** (défaut 1) : un SHORT (rebond ou cassure, tendance ou contre-tendance) est refusé si BTC a monté de plus de **`SCALP_SHORT_BTC_MAX_PCT`** (défaut **+0,1 %**) sur 1 h (4 bougies de 15 min). Refus visible dans le journal : « short refusé : BTC en hausse ». Les longs ne sont pas touchés.
- Mettre `SCALP_SHORT_BTC_BLOCK` à 0 le désactive ; abaisser le seuil à 0 ou en négatif le rend plus strict.
- Cause : sur 75 trades Paper, les shorts ont 24 % de gagnants (−7,32 $ net) et 9 shorts corrélés ouverts pendant une hausse de BTC ont tous perdu.
- Testé sur marchés simulés : BTC +0,5 % = 0 short ; BTC 0 ou négatif = shorts possibles.

## v4.439 — Swing Support : stops « fantômes » à 2 % et 3 % (observation seule)
Fichiers : `manual_trading.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- Pour chaque trade **Swing Support** (Live et Paper), le moteur note le plus bas atteint et si le prix a touché −2 % / −3 % (par rapport à l'entrée). À la clôture il enregistre ce qu'auraient donné un stop à 2 % et à 3 % : si le seuil a été touché, perte de 2 % (ou 3 %) ; sinon le résultat réel.
- **Aucun effet sur les positions** : le stop réel (5 %) reste inchangé.
- L'historique affiche sous chaque trade « 🧪 stop fantôme : réel … · avec stop 2 % … · avec stop 3 % … » et une ligne de total (en $ avant frais et en %). Seuls les trades ouverts puis clôturés après cette version sont concernés.
- Limites : approximation sans slippage ; le prix est celui observé à chaque cycle du moteur (pas à la seconde), donc un franchissement très bref peut être manqué.

## v4.440 — Taille d'entrée en USDC sur les trades ouverts
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Les positions ouvertes du Trading Manuel (liste « Positions », donc aussi les trades Scalp/Swing Support) et leur carte dans l'onglet « Tous » affichent « Taille XX.XX USDC (marge YY.YY) » : valeur notionnelle de la position et marge engagée (taille / levier).

## v4.441 — Net LIVE avec frais ET funding réels Hyperliquid
Fichiers : `manual_trading.py`, `scalp_crypto.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- Pour chaque trade manuel/Scalp/Swing Support **Live perp fermé** (30 derniers jours), le bot lit dans Hyperliquid **tous les remplissages** du trade (ouverture, partiels, clôture : somme des frais réels) et le **funding payé/reçu pendant la détention**. Net réel = PnL − frais + funding. Traitement en tâche de fond : 6 trades au plus toutes les 2 min, 3 essais par trade, au moins 2,5 min après la clôture.
- L'historique affiche « net +0.39 $ ✓ (frais 0.041 · funding −0.013) » quand les coûts réels sont connus, « (est.) » sinon (estimation 0,09 %). Le résumé et les statistiques Scalp utilisent les coûts réels quand ils existent. Le Paper garde l'estimation.
- Hypothèses : le funding vient de `user_funding_history` (SDK Hyperliquid), filtré par actif ; la position étant unique par actif en Live, tous les remplissages de l'actif pendant la durée du trade lui sont attribués. Testé avec des réponses simulées, pas encore sur le compte réel : vérifier sur un trade connu (ex. PAXG : frais 0,02 $).

## v4.442 — Scalp : gestion de la corrélation (régime du panier) + onglet « 🔗 Corrélation »
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
**Étape 1 — observation (toujours active, sans effet sur les trades)**
- Toutes les 60 s, le Scalp mesure sur les 5 min des actifs suivis : **corrélation moyenne** des rendements (fenêtre `SCALP_CORR_WINDOW_MIN`, 120 min), **largeur** (part d'actifs qui montent / baissent de plus de 0,05 % sur 30 min) et variation moyenne du panier. Régime **corrélé** si corrélation ≥ `SCALP_CORR_HIGH` (0,6), sinon **dispersé**.
- Chaque trade Scalp enregistre à l'entrée : régime, corrélation, largeur dans son sens, force relative de l'actif (vs panier), **taille de la grappe** (ouvertures déjà faites dans le même sens sur 30 min) et nombre de pairs. L'historique affiche « corrélé 0.72 · panier 65 % dans le sens · grappe 3 ».
**Étape 2 — règles (désactivées par défaut, un interrupteur chacune ; ne s'appliquent qu'en régime corrélé sauf mention)**
1. `SCALP_CORR_DIR_ON` — sens du panier : long seulement si ≥ `SCALP_BREADTH_MIN` % (60) des actifs montent ; short si autant baissent.
2. `SCALP_CORR_RELSTR_ON` — meilleur du groupe : long seulement si l'actif est plus fort que le panier, short si plus faible.
3. `SCALP_CLUSTER_MAX` (0 = illimité) / `SCALP_CLUSTER_WINDOW_MIN` (30) — plafond d'ouvertures dans le même sens.
4. `SCALP_CORR_SIZE_PCT` (100) — taille de chaque trade en régime corrélé (% de la taille normale ; plancher 10,5 $ en live).
5. `SCALP_PEER_MIN` (0) / `SCALP_PEER_WINDOW_MIN` (30) — confirmation par les pairs (tous régimes) : un signal attend qu'au moins N autres actifs donnent un signal ou soient ouverts dans le même sens ; sinon il expire.
6. `SCALP_CORR_BASKET_EXIT_ON` (0) / `SCALP_BASKET_EXIT_PCT` (70) — sortie panier : en régime corrélé, un trade en perte (ouvert depuis plus de 2 min, laisser courir non armé) est fermé si ≥ 70 % des actifs vont contre lui (motif « SCALP SORTIE PANIER »).
**Interface** : la carte Scalp a trois onglets (⚙️ Réglages · 🔗 Corrélation · 📊 Résultats & journal). L'onglet Corrélation affiche le régime en direct, la variation 30 min de chaque actif, les résultats par régime et par taille de grappe, les signaux en attente de pair et tous les réglages ci-dessus.
**Limites** : seuils de départ non calibrés (le but de l'étape 1 est de les calibrer) ; testé sur séries simulées (corrélation 0,91 vs 0,06) et avec un moteur factice pour les règles, pas sur le marché réel.

## v4.443 — Historique Live : trades anciens qui disparaissaient
Fichiers : `db.py`, `manual_trading.py`, `bot_engine.py` (version), `README.md`.
- Cause : l'historique lisait les 800 derniers trades clôturés **tous modes confondus** puis gardait 100 par mode. Le Scalp Paper en produit beaucoup : les plus anciens trades Live sortaient des 800 et disparaissaient de l'affichage (ils restaient en base).
- Correctif : le filtre Live / Paper se fait maintenant en SQL ; l'historique montre toujours les 100 derniers Live et les 100 derniers Paper. Même correction pour le calcul des frais réels (trades Live anciens).
- Rien n'est supprimé : les trades « perdus » réapparaissent (dans la limite des 100 derniers Live).

## v4.444 — Réconciliation de l'historique Live avec Hyperliquid
Fichiers : `db.py`, `manual_trading.py`, `api.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- Le bot lit les remplissages Hyperliquid des 10 derniers jours, reconstitue chaque trade perp (ouverture → retour à zéro) et **ajoute à l'historique Live ceux que le bot ne connaît pas** (source « Importé Hyperliquid »). Un trade est « connu » si un trade du bot sur le même actif chevauche sa période (± 3 min).
- Prix d'entrée/sortie moyens réels, PnL brut, frais et funding réels (net ✓). Levier non disponible (non affiché dans l'historique).
- Automatique toutes les 15 min, ou à la demande : bouton **« 🔄 Comparer à Hyperliquid »** (onglet LIVE de l'historique, 14 jours).
- Les positions encore ouvertes ne sont jamais importées. Un trade importé que tu supprimes à la main n'est pas réimporté.
- Testé sur ton export Hyperliquid : retrouve SUI 14/10 12:01 (+0,45 $ brut) et BTC 14/10 13:16 (−0,70 $ brut), absents du bot, et ne duplique aucun trade déjà présent.

## v4.445 — Scalp : stop au seuil de rentabilité (option)
Fichiers : `manual_trading.py`, `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Réglages Scalp (**désactivé par défaut**) : `SCALP_BE_ENABLED` (0/1), `SCALP_BE_ARM_PCT` (0,4 : gain maximal à atteindre) et `SCALP_BE_LOCK_PCT` (0,1 : le stop passe à entrée ± 0,1 %, de quoi couvrir les frais).
- Quand le gain atteint le seuil, le stop remonte (aussi le stop natif Hyperliquid en Live). Il ne remonte pas si le prix est déjà retombé sous ce niveau. Sortie notée « STOP SEUIL RENTABILITE ».
- Compatible avec « laisser courir » (objectif 0,6 %) : le stop de rentabilité protège la zone 0,4 → 0,6 %.
- Contrepartie : certains trades qui auraient fini gagnants après un petit recul sortent à zéro ; à comparer sur une vingtaine de trades Paper.

## v4.446 — Scalp : confirmation par les pairs seulement en marché corrélé
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage `SCALP_PEER_CORR_ONLY` (1 par défaut) : la règle 5 (pairs) ne s'applique plus qu'en marché « corrélé », comme les règles 1, 2, 3, 4 et 6. Mettre 0 pour la rendre valable en tout régime.

## v4.447 — Historique : filtre par date et résultats journaliers
Fichiers : `index_src.html`, `index.html`, `manual_trading.py`, `bot_engine.py` (version), `README.md`.
- Au-dessus de l'historique (onglets LIVE et PAPER) : sélecteurs « Du … au … » et boutons Aujourd'hui / Hier / Tout. La liste et le résumé (gagnants, PnL brut, net) ne portent que sur la période choisie.
- Tableau **par jour** : nombre de trades, gagnants, PnL brut et net de frais. Cliquer sur un jour filtre la liste sur ce jour.
- Jours en heure locale de l'appareil, d'après la date de fermeture (les statuts annulés / expirés ne sont pas comptés).
- L'historique renvoyé passe de 100 à **300 derniers trades par mode** pour couvrir plus de jours.

## v4.448 — Historique : sous-onglets « 📜 Trades » et « 📅 Par jour »
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- L'historique (LIVE et PAPER) a deux sous-onglets. **Trades** : filtre par date, résumé, liste. **Par jour** : tableau des résultats journaliers ; un clic sur un jour ouvre ses trades.

## v4.449 — Historique : export CSV de la sélection
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Bouton **⬇ CSV** (sous-onglet Trades) : télécharge les lignes affichées (onglet LIVE ou PAPER, période « Du … au … » choisie) dans `historique_<mode>_<du>_<au>.csv`.
- Séparateur `;`, décimales à la virgule, UTF-8 avec BOM (ouverture directe dans Excel). Colonnes : id, actif, sens, marché, mode, source, statut, ouverture, fermeture (heure locale), entrée, sortie, taille, levier, PnL brut, frais, funding, net, coûts réels/estimés, pic de gain, motif, type et note Scalp, régime.

## v4.450 — Scalp : flux pour tous les trades, cassure avec retest, contexte d'entrée enregistré
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- **Contexte d'entrée (toujours actif, observation)** : chaque trade Scalp enregistre la part d'achats agressifs et le nombre de transactions du flux, la tendance H1 et 15 min, et la variation de BTC sur 1 h (champ `ctx` du trade). Sert à calibrer les filtres.
- **Flux pour tous (option, désactivée)** : `SCALP_FLOW_ALL_ON` (0/1), `SCALP_FLOW_ALL_MIN` (0,52 : part mini d'achats agressifs pour un long ; pour un short, part de ventes), `SCALP_FLOW_ALL_MIN_TRADES` (30). Flux indisponible ou trop peu de transactions : pas de blocage. La contre-tendance garde ses propres exigences (58 %).
- **Cassure avec retest (option, désactivée)** : `SCALP_RETEST_ON` (0/1). Une cassure n'ouvre plus le trade tout de suite : le bot attend que le prix revienne près du niveau cassé (`SCALP_RETEST_TOL_ATR`, 0,3 × ATR 5 min), puis qu'il reparte d'au moins `SCALP_RETEST_BOUNCE_ATR` (0,25 × ATR 5 min) depuis l'extrême du retest. Abandon si le prix repasse de l'autre côté du niveau (marge de bruit `SCALP_NOISE_ATR`) ou sans entrée en `SCALP_RETEST_BARS` bougies 5 min (6). Les rebonds gardent leur confirmation actuelle.
- Testé sur séries simulées : flux baissier bloque les 44 longs détectés, flux haussier ou trop peu de transactions n'en bloque aucun ; machine du retest validée (retest + rebond → entrée, pas de retest → rien, retour sous le niveau → abandon).

## v4.451 — Historique : filtre par actif
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Liste déroulante « Tous les actifs » à droite des sous-onglets de l'historique (LIVE et PAPER). Elle ne propose que les actifs présents dans l'historique de l'onglet.
- Le filtre s'applique à la liste, au résumé, au tableau « Par jour » et à l'export CSV, et se combine avec la période « Du … au … ».

## v4.452 — Swing Support : choix des actifs évalués
Fichiers : `api.py`, `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Dans la carte Swing Support : bouton « ⚙️ Choisir les actifs surveillés » → grille d'actifs à cocher (tous les actifs crypto suivis par le bot, hors Forex), boutons Tous / Par défaut (BTC, ETH, HYPE, TAO, SUI) / Enregistrer. Enregistré dans `SUPSW_SYMBOLS` (persistant), pris en compte au cycle suivant.
- Les actifs cochés sont évalués comme les cinq d'origine (support Daily, porte BTC, chute récente, résistance, réentrée…) et peuvent déclencher l'entrée automatique de Swing Support. Le plafond `SUPSW_MAX_TRADES` (3 positions simultanées) reste inchangé.
- Tant que « Actifs réservés » (`SUPSW_RESERVE_ASSETS`) est à 1, les actifs cochés sont ignorés par Tendance / Accumulation / Spot-Accum.
- API : `GET/PUT /api/supsw/symbols`.

## v4.453 — Export CSV : contexte d'entrée Scalp
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Le CSV de l'historique ajoute 5 colonnes pour les trades Scalp ouverts depuis la v4.450 : `flux_achats` (part d'achats agressifs), `flux_n` (transactions), `tend_h1`, `tend_15m`, `btc_1h_pct`.

## v4.454 — Opportunités : Scalp remplace Funding, ouverture live manuelle, vue « Par mode »
Fichiers : `manual_trading.py`, `scalp_crypto.py`, `push_alerts.py`, `api.py`, `bot_engine.py`, `index_src.html`, `index.html`, `README.md`.
- **Types annoncés (alertes téléphone)** : « Funding » est retiré, « Scalp » ajouté. Les préférences déjà enregistrées avec Funding sont migrées (Funding → Scalp). La « confiance ≥ » s'applique à la note du Scalp.
- **Ouvertures Scalp proposées en manuel** : à chaque ouverture (paper ou live) du Scalp, l'opportunité « Scalp » apparaît dans la liste pendant **5 min** (`MANUAL_SCALP_OPP_TTL_SEC`, 300) puis disparaît. Un trade paper du Scalp ne la masque pas ; seule une position LIVE sur l'actif la retire.
- **Ticket** : bouton « Ouvrir en live » → ticket **LIVE par défaut** avec les réglages du Scalp (taille, levier, stop, objectif, laisser courir, stop de rentabilité si activé), tout modifiable. Le trade ouvert est un trade Scalp (stats et gestion du Scalp).
- **Vue « 🗂 Par mode »** dans la carte des opportunités : une section par mode (Scalp, Swing Support, Accumulation, Spot-Accum, Forex, Funding) avec le nombre d'opportunités, les lignes et, pour le Scalp, le temps restant.

## v4.455 — Choix des réglages par défaut d'un mode (ticket, ordre programmé, position ouverte)
Fichiers : `manual_trading.py`, `api.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- Une barre « Réglages par défaut du mode » (⚡ Scalp · 🧱 Swing Support · ✋ Libre · 🎯 Accumulation · 🌱 Spot-Accum · 💰 Funding · 💱 Forex) apparaît :
  1. sur le **ticket** d'une opportunité : le mode de l'opportunité est présélectionné (une opportunité Scalp vient avec les réglages Scalp) ; un clic remplit taille, levier, stop, objectif, trailing, laisser courir, verrou et stop de rentabilité du mode choisi ;
  2. dans « Modifier » d'un **ordre programmé** (mêmes champs, en pourcentages) ;
  3. dans « Modifier » d'une **position ouverte** : stop et objectif sont recalculés depuis le prix d'entrée ; la taille et le levier ne changent pas.
- Rien n'est appliqué avant le bouton d'envoi / « Enregistrer » ; tous les champs restent modifiables après le choix.
- Sur un ticket, si le mode choisi diffère de celui de l'opportunité, le trade prend l'étiquette de ce mode (Scalp, Swing Support ou Libre ; Accumulation, Spot-Accum, Funding et Forex passent en « Libre » avec leurs réglages).
- API : `GET /api/manual/preset`. Le stop de rentabilité (`be_arm_pct`, `be_lock_pct`) devient modifiable sur ordres programmés et positions ouvertes.

## v4.456 — Conseil par actif : H4/D1 = direction, H1 = décision
Fichiers : `manual_trading.py`, `bot_engine.py` (version), `README.md` (+ fichiers de v4.455 non encore déployés).
- **Direction (biais)** : donnée uniquement par H4 (poids 2) et D1 (poids 1). LONG si le cumul ≥ +2, SHORT si ≤ −2, sinon pas de direction (H4 neutre ou contredit par le D1).
- **Décision** : le H1 doit aller dans le sens du biais. H1 neutre, contraire ou indisponible → **ATTENDRE** avec la raison « H4/D1 donnent la direction LONG, mais le H1 est neutre : on attend que le H1 confirme ».
- Si le H1 confirme, les autres critères (RSI, zones, BTC, funding, score ≥ ±3, gain/risque ≥ 1,2) décident toujours de l'entrée.
- « Penche LONG/SHORT » reprend désormais le biais H4/D1 ; ce n'est plus un score cumulé qui ressemble à une décision.

## v4.457 — Historique : pic affiché du plus bas au plus haut
Fichiers : `manual_trading.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md` (+ fichiers de v4.455/v4.456 non encore déployés : `api.py`).
- Le plus bas atteint par le prix (`trough_pct`) est désormais enregistré pour **tous** les trades manuels (avant : Swing Support seulement).
- Chaque ligne de l'historique clôturé affiche « 🏔️ Pic −0.20 % à 1.02 % » (mouvement du prix, sans levier).
- Le CSV de l'historique gagne une colonne `creux_pct` placée juste avant `pic_gain_pct`.
- Les trades fermés avant cette version n'ont pas de plus bas enregistré : seul le pic est affiché, avec une mention. Le plus bas est mesuré à chaque cycle de gestion, donc un creux très bref entre deux cycles peut manquer.

## v4.458 — Ajouter à une position ouverte / la réduire (paper et live)
Fichiers : `manual_trading.py`, `api.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- Deux boutons sur chaque position ouverte : **＋ Taille** et **－ Taille** (perpétuels en live ; paper et live).
- **Ajouter** (montant en $, notionnel) : paper = au prix courant ; live = ordre réel au marché sur Hyperliquid (marge ajoutée = montant / levier). La taille et le **prix d'entrée moyen** sont relus sur Hyperliquid. Le **SL et l'objectif gardent leurs prix** ; le SL natif est renvoyé à la nouvelle taille. Le pic et le creux sont recalculés par rapport à la nouvelle entrée.
- **Réduire** (montant en $ ou en %) : paper = au prix courant ; live = clôture partielle au marché (comme le TP1). Le gain ou la perte de la partie retirée est encaissé (`realized_pnl`) et compté dans le PnL final. Refus si la partie retirée ou le reste fait moins de 10 $ (minimum Hyperliquid) ou si cela revient à tout fermer (utiliser « Fermer »).
- Live : confirmation obligatoire dans l'interface et côté serveur. Chaque ajout ou réduction est consigné dans la position (`resizes`) et le journal.
- Frais : chaque ajout ou réduction paie les frais d'un ordre au marché (0,045 %). Les frais réels sont réconciliés comme pour les autres trades.
- API : `POST /api/manual/positions/{id}/resize` (`action`, `notional_usd` ou `pct`, `confirm_live`).

## v4.459 — Grappe Scalp : se débloque sur un recul de l'actif
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version + défaut), `api.py` (plage), `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage **« Grappe : débloquer après un recul de X % depuis le plus bas/haut de la fenêtre »** (`SCALP_CLUSTER_RETRACE_PCT`, 0 = désactivé, comportement inchangé).
- Contexte : la règle 3 (Grappe, `SCALP_CLUSTER_MAX`) refuse une nouvelle ouverture dans le même sens quand le marché est corrélé et que la grappe est pleine. Avant, le refus durait jusqu'à la fin de la fenêtre de temps. Avec X > 0, un actif peut entrer malgré la grappe pleine **s'il a reculé d'au moins X %** : SHORT = prix remonté de X % depuis son plus bas des bougies 5 min de la fenêtre ; LONG = prix redescendu de X % depuis son plus haut.
- Le refus indique le recul requis et le recul actuel ; une entrée débloquée enregistre `cluster_retrace` dans le signal.
- Conditions : `SCALP_CLUSTER_MAX` > 0 et régime « corrélé ». Hors régime corrélé, aucune règle de grappe ne s'applique.
- Valeurs de départ suggérées : grappe 2 ouvertures sur 30 min, recul 0,3 %.

## v4.460 — Scalp : capital composé, objectif du jour, mode prudent (tout désactivé par défaut)
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version + défauts), `api.py`, `index_src.html`, `index.html`, `README.md`.
- **Capital composé** (`SCALP_COMPOUND_ON`) : la taille de chaque trade = **capital courant × `SCALP_COMPOUND_PCT` %** (notionnel). Capital courant = **capital de départ** (`SCALP_CAPITAL_USD`) + gains/pertes **nets de frais** des trades Scalp clôturés du mode (paper ou live) depuis le départ. Les pertes réduisent aussi la taille. Plancher : 10,5 $ en live, 1 $ en paper. Les trades ouverts ne comptent pas. Le bouton « ↺ Repartir du capital de départ » (onglet Réglages) refixe le départ à maintenant.
- **Objectif du jour** (`SCALP_GOAL_PCT`, 0 = aucun) : % du capital de début de journée (jour **UTC**, comme la perte journalière max). Atteint → **mode prudent** jusqu'à minuit UTC (ou arrêt total des entrées si `SCALP_GOAL_STOP_ALL` = 1). Les positions ouvertes continuent d'être gérées.
- **Mode prudent** : une entrée n'est acceptée que si (1) l'actif a au moins `SCALP_SAFE_MIN_TRADES` trades parmi ses `SCALP_SAFE_WINDOW` derniers, un taux de réussite ≥ `SCALP_SAFE_MIN_WINRATE` % et un net positif ; (2) c'est un rebond (`SCALP_SAFE_REBOND_ONLY`) ; (3) la note est < `SCALP_SAFE_MAX_SCORE` (90 ; 0 = pas de limite — les notes ≥ 90 ont perdu davantage sur les CSV). Chaque refus est écrit dans le journal avec sa raison.
- L'interface affiche le capital de départ → courant, l'avancement vers l'objectif et la liste des actifs « sûrs ».
- Contrôle de taille live : la vérification de marge libre utilise la taille composée.
- (v4.460, ajout) Avec le capital composé, la **marge de tous les trades Scalp ouverts du mode ne dépasse jamais le capital courant** : un nouveau trade est refusé (« capital engagé ») si la marge déjà utilisée + sa marge dépasse le capital. À 100 % du capital et x3, un trade engage 1/3 du capital : 3 trades au maximum en même temps. Pour plus de trades simultanés, baisser « Taille d'un trade (% du capital) ».
- (v4.460, ajout) **Live : capital de départ lu sur Hyperliquid** (`SCALP_CAPITAL_FROM_HL`, 0 par défaut). Le bot lit le solde du compte **une fois par jour UTC** (et après un redémarrage), avec la même règle que l'onglet Bilan : le plus élevé entre la valeur du compte perps et le solde USDC spot (compte Unifié). Capital courant = ce solde + gains/pertes nets des trades Scalp live clôturés depuis la lecture. `SCALP_CAPITAL_CAP_USD` plafonne le capital utilisé (0 = aucun). Si la lecture échoue, le réglage « Capital de départ » sert de repli. Le paper garde le capital de départ saisi.
- (v4.460, correction) Le contrôle de marge libre avant un trade live lisait uniquement `accountValue` du compte perps (0 en compte Unifié) : il lit maintenant le solde USDC spot dans ce cas, comme le Bilan.

## v4.461 — Mesures de qualité de tendance à l'entrée (Scalp) — aucune décision modifiée
Fichiers : `scalp_crypto.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md`.
- À chaque ouverture Scalp, le trade enregistre (`scalp.tq`) : ratio d'efficacité (mouvement net / chemin parcouru, 1 = tendance propre, 0 = va-et-vient) de **BTC sur 1 h et 4 h**, de **l'actif sur 1 h**, du **panier sur 2 h et 4 h** ; l'**écart du prix à sa moyenne 1 h** (%) ; le **mouvement du panier sur 2 h** (%).
- Le CSV de l'historique gagne 7 colonnes : `er_btc_1h`, `er_btc_4h`, `er_actif_1h`, `ecart_moy_1h_pct`, `er_panier_2h`, `er_panier_4h`, `panier_2h_pct`. Seuls les trades ouverts après cette version les contiennent.
- But : vérifier, après quelques jours variés, si une de ces mesures sépare les trades/journées perdants des gagnants avant d'ajouter un filtre.

## v4.462 — Thermomètre de l'objectif du jour (carte Scalp)
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md`.
- Sous la ligne d'état de la carte Scalp (toujours visible, sans ouvrir les réglages) : un thermomètre horizontal de l'objectif du jour (UTC) du mode affiché (paper ou live). Barre orange jusqu'à l'objectif, repère 🎯 à la valeur de l'objectif, barre verte rayée au-delà (surplus), barre rouge vers la gauche si la journée est négative.
- Texte : avancement en % et en $, « reste X % (≈ Y $) » ou « ✔ atteint · surplus +X % (≈ +Y $) », et rappel du mode prudent ou de l'arrêt des entrées une fois l'objectif atteint.
- Si l'objectif est à 0 (désactivé), une ligne discrète affiche seulement le résultat du jour.
- Aucune décision du bot n'est modifiée.

## v4.463 — Thermomètre : message de diagnostic si le capital n'est pas reçu
Fichiers : `index_src.html`, `index.html`, `bot_engine.py` (version), `README.md` (+ `scalp_crypto.py` et `api.py` de v4.460–v4.461 en cas de dépôt incomplet).
- Si le serveur n'envoie pas le champ `capital` du Scalp (fichier `scalp_crypto.py` plus ancien que v4.460), la carte affiche maintenant une ligne jaune « 🌡️ Thermomètre indisponible … » au lieu de rien.

## v4.464 — Page jamais en cache + version de la page affichée
Fichiers : `api.py`, `bot_engine.py` (version), `index_src.html`, `index.html`, `README.md` (+ `scalp_crypto.py`).
- La route `/` envoie `Cache-Control: no-store` : le navigateur ou l'application installée ne garde plus de copie de la page.
- La carte Scalp affiche « [page v4.464] » : c'est la version de `index.html` réellement chargée par le navigateur (différente de `/health`, qui donne la version de `bot_engine.py`). Si `/health` affiche 4.464 mais que la carte affiche une autre version, `index.html` n'a pas été remplacé.
- Le thermomètre de l'objectif du jour a été vérifié par un rendu réel dans un navigateur de test (objectif en cours, atteint avec surplus, journée négative, serveur sans capital).

## v4.465 — Une seule base horaire pour les statistiques du Scalp
Fichiers : `scalp_crypto.py`, `bot_engine.py`, `api.py`, `index_src.html`, `index.html`, `README.md`.
- Nouveau réglage **« Début de la journée du Scalp : décalage avec UTC en heures »** (`SCALP_DAY_OFFSET_H`, **−4** par défaut = heure de Marigot ; 0 = UTC). La « journée » du Scalp commence maintenant à minuit à ce décalage (04:00 UTC pour −4) au lieu de 00:00 UTC.
- Cette base unique est utilisée pour : la ligne « AUJOURD'HUI » de la carte, le thermomètre et l'objectif du jour, la limite de perte journalière, le nombre de trades max par jour, et le point de départ du solde Hyperliquid lu chaque jour.
- Les libellés affichent la base (« jour UTC−4 »). Les résultats « par tranche horaire » et les tranches horaires de trading restent en UTC.

## v4.466 — Mode prudent : support/résistance obligatoire
- Objectif du jour atteint : un rebond n'est accepté que si le support/résistance a été **testé au moins X fois** (`SCALP_SAFE_MIN_TOUCHES`, 2 par défaut ; 0 = aucune exigence).
- Option `SCALP_SAFE_MIN_ROOM_PCT` (0 = off) : place minimale (%) avant la zone opposée suivante.
- Ne s'applique qu'après l'objectif atteint (aucun effet si l'objectif est à 0).

## v4.467 — Objectif du jour : net de frais explicite
- Le calcul était déjà net (gain brut − frais réels − funding, trades clôturés du jour local). Le thermomètre l'indique maintenant : « Objectif du jour NET de frais » avec le détail « net = brut − frais ».

## v4.468 — Thermomètre : sélecteur Aujourd'hui / Hier / Tout / date
- Le thermomètre du Scalp a un sélecteur : **Aujourd'hui**, **Hier**, **Tout**, ou une **date** choisie. Même calcul net de frais, même base de jour (`SCALP_DAY_OFFSET_H`).
- Pour un jour passé : net du jour ÷ capital au début de ce jour (capital de départ + net cumulé avant ce jour).
- « Tout » : cumul depuis le départ (réglage « Capital »), objectif = objectif du jour × nombre de jours de trading.
- Le serveur envoie `capital.days` (120 derniers jours) et `capital.all`.

## v4.469 — Thermomètre : choix du mode affiché (Paper / Live)
- Deux boutons PAPER / LIVE dans le cadre du thermomètre permettent de voir les résultats de l'autre mode sans changer le mode du moteur. Le serveur envoie `capital_modes` (paper et live).

## v4.470 — Diagnostic Scalp (diagnostic d'entrée + bouton dans Manuel)
- Le diagnostic « Pourquoi cet actif ne trade pas ? » a une nouvelle ligne **⚡ SCALP CRYPTO** par actif : dernier verdict du Scalp (signal valide, ou motif du refus : zone, note, tendance, grappe, mode prudent, capital…), fenêtre fermée / pause / position ouverte / signal en attente, état de l'objectif et du mode prudent, régime du marché. L'ancienne ligne « ⚡ SCALP » (Forex) devient « ⚡ SCALP FOREX ».
- Nouvelle carte **🔍 DIAGNOSTIC SCALP** dans l'onglet Manuel (sous la carte Scalp) : même diagnostic, limité aux actifs du Scalp, avec le verdict Scalp + top-down, situation, niveaux, qualité du marché et flux. Choix d'un actif ou de tous, bouton Copier.
- Le Scalp mémorise le dernier verdict de chaque actif (`verdicts`), mesure seule, aucun effet sur les décisions.

## v4.471 — Bouton « Masquer » sur les diagnostics
- Un bouton **🙈 Masquer** à côté de « Copier » (carte Diagnostic Scalp) et à côté de « Diagnostiquer maintenant » (diagnostic « Pourquoi cet actif ne trade pas ? ») cache les résultats ; un nouveau clic sur « Diagnostiquer » les réaffiche.

## v4.472 — Emplacements : N trades simultanés de taille identique, capital repris chaque jour
- Nouveau réglage **Emplacements** (`SCALP_SLOTS`, 0 = désactivé). Avec N > 0 : au plus N trades Scalp ouverts en même temps (par mode), chacun de **notionnel = capital du début de journée × levier ÷ N** (donc marge = capital ÷ N). La taille est fixe pendant toute la journée ; le capital du lendemain intègre les gains/pertes nets (intérêts composés quotidiens).
- Remplace pour le dimensionnement « Taille ($) » et « Capital composé » tant qu'il est > 0. Entrée refusée si les N emplacements sont occupés (« n/N emplacements occupés »).
- Exemple : capital 200 $, N = 4, ×3 → chaque trade 150 $ de notionnel (50 $ de marge), 4 trades au maximum.
- Le capital du début de journée suit la base de jour (`SCALP_DAY_OFFSET_H`) ; en live avec « capital = solde Hyperliquid », c'est le solde lu au début de la journée.

## v4.473 — Emplacements : réserve de capital
- Nouveau réglage **Part du capital engagée** (`SCALP_SLOTS_USE_PCT`, 100 par défaut = aucun changement). Taille d'un trade = capital du début de journée × part engagée × levier ÷ N. Exemple : 200 $, N = 4, ×3, 80 % → 120 $ de notionnel par trade (40 $ de marge), 40 $ de réserve.

## v4.474 — Historique : onglets LIVE SCALP et PAPER SCALP
- Dans l'historique, deux nouveaux onglets **LIVE SCALP** et **PAPER SCALP** n'affichent que les trades du Scalp crypto du mode correspondant (avec compteur). Les onglets LIVE et PAPER restent inchangés (tous les trades du mode). Filtre par actif, par date, « Par jour », export CSV et suppression fonctionnent sur l'onglet affiché ; le CSV est nommé `historique_live_scalp_…`.

## v4.475 — Scalp : résultats par heure en net, boutons Copier / Télécharger
- « Résultats par heure » affiche désormais le **net de frais** (comme le thermomètre et « AUJOURD'HUI »), avec l'heure locale entre parenthèses. La copie/le fichier donne aussi le brut et les frais.
- Boutons **Copier** et **Télécharger** (.txt) pour la table par heure et pour le journal des signaux.

## v4.476 — Tendance : entrée H1, sortie sur H1/H4 ou gain acquis, actifs crypto
Constat : BTC, ETH, HYPE, TAO, SUI sont réservés à Swing Support, donc Tendance n'a eu aucun signal crypto (seulement EUR, JPY, PAXG). Tous les nouveaux réglages sont **désactivés par défaut** (comportement inchangé).
- `TRENDF_IGNORE_RESERVE` (0/1) : Tendance peut aussi trader ces 5 actifs ; un actif déjà ouvert par un autre mode reste exclu (pas de double position).
- `TRENDF_REQUIRE_H1` (0/1) : entrée seulement si la tendance H1 (EMA 50/200) est aussi dans le sens, en plus de Daily et H4.
- `TRENDF_EXIT_H4_REVERSE` : 0 ignorée, 1 H4 franchement retournée (comme avant), **2** dès que H4 n'est plus dans le sens (neutre inclus).
- `TRENDF_EXIT_H1` : 0 ignorée, 1 H1 franchement retournée, 2 H1 n'est plus dans le sens. Jugée sur bougies clôturées après l'entrée.
- `TRENDF_LOCK_ARM_PCT` / `TRENDF_LOCK_GIVEBACK_PCT` : gain acquis. Une fois le gain maximal ≥ N % du prix, sortie si le gain retombe de X % de son pic (motif « TENDANCE GAIN PROTEGE »).
- Le stop suiveur ATR reste actif ; le stop maximal (`TRENDF_MAX_STOP_PCT`, jusqu'à 20) et le levier (`TRENDF_LEVERAGE`, jusqu'à 10) sont déjà réglables.

## v4.477 — Mode défensif du Scalp
- Quand la journée est à **−1,5 % du capital ou moins** (net de frais, base de jour locale), le Scalp applique les **mêmes filtres stricts que l'objectif atteint** : actifs « sûrs », rebonds seulement, support/résistance testé ≥ X fois, notes < seuil. Réglage `SCALP_DEFENSE_DAY_PCT` (1,5 par défaut ; 0 = désactivé).
- Option `SCALP_DEFENSE_LOSSES` (0 = off) + `SCALP_DEFENSE_MIN` (60) : même mode après N pertes de suite, pendant N minutes après la dernière clôture.
- Les trades déjà ouverts ne sont pas touchés ; la journée suivante repart normalement. Refus journalisés « mode défensif (…) ». Une pastille 🛡 s'affiche sous le thermomètre ; le diagnostic Scalp l'indique.

## v4.478 — Corrélation de chaque actif avec BTC
- Nouvelle carte **📉 CORRÉLATION AVEC BTC** dans l'onglet Manuel (sous « Diagnostic Scalp ») : pour chaque actif crypto du Scalp, corrélation des rendements 1 h avec BTC sur **7 jours** et **30 jours**, corrélation **quand BTC baisse**, et **bêta 30 j**. Trié du moins corrélé au plus corrélé ; couleurs vert < 0,4 / jaune 0,4–0,7 / rouge > 0,7 ; boutons Copier, Télécharger, Masquer, Recalculer.
- Endpoint `GET /api/scalp/corr-btc` (cache 30 min). Bougies lues directement chez Hyperliquid (pas dans le cache partagé du bot). Mesure seule, aucun effet sur les décisions.

## v4.479 — Bêta : exposition équivalente BTC, plafond, taille ajustée
- **Betas automatiques** : le bêta 30 j de chaque actif du Scalp par rapport à BTC est rafraîchi en arrière-plan (au plus toutes les 30 min, sans bloquer le scan).
- **Option 1 — exposition équivalente BTC** (toujours affichée, mesure seule) : sous le thermomètre, pour les positions ouvertes, somme de (notionnel × bêta) par sens, avec le notionnel réel et le net. Bêta inconnu = 1.
- **Option 2 — plafond d'exposition** `SCALP_BETA_CAP_PCT` (0 = désactivé) : refuse une entrée si l'exposition équivalente BTC dans ce sens dépasserait N % du capital courant. Refus journalisé « exposition BTC SHORT/LONG … ».
- **Option 3 — taille ajustée au bêta** `SCALP_BETA_SIZE_ON` (0 par défaut), `SCALP_BETA_REF` (1,0), `SCALP_BETA_MIN_FACTOR` (0,4) : si bêta > référence, taille × référence ÷ bêta (jamais augmentée, plancher 40 %).
- Les options 2 et 3 sont désactivées par défaut : comportement inchangé tant qu'elles ne sont pas activées.

## v4.480 — Tendance : cryptos de l'univers du Scalp
- Correction : les cryptos du Scalp (SOL, XRP, LINK, etc.) sont bien suivies par le bot ; seule la liste `TRENDF_SYMBOLS` limitait Tendance. Nouveau réglage `TRENDF_SCALP_UNIVERSE` (0 par défaut) : 1 = Tendance évalue aussi les cryptos de l'univers du Scalp, en plus de la liste habituelle. Les 5 actifs réservés à Swing Support restent exclus sauf `TRENDF_IGNORE_RESERVE` = 1. Les conditions d'entrée (Daily + H4 [+ H1], flux) et de sortie sont inchangées.

## v4.481 — Historique : SL du ticket et SL à la sortie
- Chaque trade fermé de l'historique affiche « 🛑 SL du ticket X % · SL à la sortie ±Y % du prix d'entrée » avec la mention « modifié depuis l'ouverture » si le SL a bougé. Le CSV a deux colonnes de plus : `sl_ticket_pct`, `sl_final_pct`. Permet de vérifier d'un coup d'œil si un stop a été respecté.

## v4.482 — Mesure : bruit du marché vs stop (aucun effet sur les décisions)
- Le Scalp enregistre l'ATR 5 min (en % du prix) de chaque trade, en plus de l'ATR 15 min déjà gardé.
- Le CSV de l'historique a quatre colonnes de plus : `atr15_pct`, `atr5_pct`, `sl_sur_atr15`, `sl_sur_atr5` (distance du stop en multiples d'ATR). Permet de vérifier si les stops touchés sont ceux qui sont trop serrés par rapport au bruit. Les anciens trades ont l'ATR 15 min (pas l'ATR 5 min).

## v4.483 — Mode défensif « vigilant »
- Le mode défensif (journée ≤ -X % ou N pertes de suite) ne bloque plus presque tout : il **vérifie** chaque entrée (`SCALP_DEFENSE_SOFT` = 1, par défaut) :
  1. amplitude disponible avant la zone suivante ≥ `SCALP_DEFENSE_ROOM_X` × le stop (1,5 par défaut) ;
  2. stop plus grand que `SCALP_DEFENSE_SL_ATR` × ATR 5 min (1,0) — sinon le stop est dans le bruit ;
  3. pas de contre-tendance ; cassure seulement avec la tendance H1 et un flux dans le sens ; rebond seulement sur zone testée ≥ 2 fois ;
  4. un actif n'est écarté que si son historique récent est clairement négatif (plus de « pas assez de trades »).
- Les trades acceptés sont pris à `SCALP_DEFENSE_SIZE_PCT` % de la taille normale (60 par défaut).
- `SCALP_DEFENSE_SOFT` = 0 rétablit les anciens filtres stricts. L'objectif atteint reste strict.

## v4.484 — Mode vigilant : cassures
- Correction : le mode vigilant exigeait une tendance H1 franche pour les cassures, alors que le moteur accepte déjà la tendance 15 min quand H1 est neutre. Toutes les cassures étaient refusées en marché « fond neutre ». L'exigence est retirée ; restent : pas de contre-tendance, flux dans le sens (> 55 %), amplitude, stop supérieur au bruit.

## v4.485 — Mode vigilant : contre-tendance sécurisée admise
- `SCALP_DEFENSE_COUNTER` = 1 (défaut) : une contre-tendance déjà validée par le moteur (rebond, zone testée ≥ 2 fois, flux, BTC, note) est admise en mode vigilant, à `SCALP_DEFENSE_COUNTER_SIZE_PCT` % de la taille normale (40). 0 = refusée comme avant.

## v4.486 — Trades manuels comptés dans l'objectif du jour
- `SCALP_GOAL_INCLUDE_MANUAL` = 1 (défaut) : les trades manuels clôturés (tickets libres), en paper comme en live, entrent dans le capital, l'objectif du jour et le mode défensif, avec leur propre mode (paper avec paper, live avec live). 0 = Scalp seul.
- Le thermomètre affiche « dont N manuels +X $ » (aujourd'hui, hier, date ou tout). Les statistiques du Scalp (réussite, historique par actif du mode prudent) restent calculées sur les seuls trades Scalp.

## v4.487 — Changer le type d'un ordre programmé
- Dans « ORDRES PROGRAMMÉS » → Modifier : nouveau sélecteur **TYPE** (Ordre libre / Swing Support / Scalp). Le type enregistré peut être remplacé par un autre ; un ordre issu d'une opportunité du bot (Accumulation, etc.) peut devenir libre / Swing Support / Scalp (le contrôle de validité de l'opportunité est alors retiré). L'inverse (vers une opportunité du bot) n'est pas possible : il faudrait qu'elle soit confirmée.
- Le type détermine l'historique (onglets LIVE / PAPER ou SCALP) et les statistiques du Scalp après l'ouverture. La colonne « Opportunité » affiche désormais « Scalp » pour un ordre Scalp.

## v4.488 — Swing Support : interrupteurs à 0 et plafond de tests de zone
- **Correction** : plusieurs interrupteurs Swing Support refusaient la valeur 0 à l'enregistrement (`SUPSW_RES_FILTER_ON`, `SUPSW_BTC_GATE_ON`, `SUPSW_RECENT_ON`, `SUPSW_TRAIL_ON`, `SUPSW_REENTRY_ON`, `SUPSW_RIDE_ON`, `SUPSW_BTC_H4_MODE` à 0 ou 1, `SUPSW_BTC24_MIN` négatif, `SUPSW_TRAIL_LOCK_PCT` à 0). Ils acceptent maintenant 0 (et 0/1/2 pour le mode H4).
- **Nouveau** `SUPSW_MAX_TOUCHES` (0 = aucune limite, par défaut) : pas d'entrée si la zone de support a été testée plus de N fois. Motif de refus : « zone testee N fois (maximum M) ».
- Essai paper proposé : `SUPSW_RES_FILTER_ON` = 0 (retire le refus « résistance Daily trop proche ») et `SUPSW_MAX_TOUCHES` = 7. À juger sur au moins 30 trades.

## v4.489 — Mesure des pentes et des lignes inclinées (aucun effet sur les décisions)
- Nouvelle fonction `mtf_analysis.slope_features` : sur les bougies 1 h, pente de la tendance (régression des 36 dernières clôtures) en multiples d'ATR par bougie (`pente_atr`) et en % du prix par heure (`pente_pct`), qualité de la droite (`pente_r2`, 1 = tendance régulière, 0 = bruit), ligne de **support inclinée** (deux derniers creux) et de **résistance inclinée** (deux derniers sommets) avec l'écart du prix à chaque ligne (`lig_sup_dist_pct`, `lig_res_dist_pct`), leurs pentes (`lig_sup_pente_pct`, `lig_res_pente_pct`) et la largeur du canal (`canal_pct`).
- Enregistré pour **Swing Support** et **Tendance** (8 colonnes ajoutées à leurs journaux, migration automatique de la base, colonnes dans les exports CSV) et pour **Scalp** (dans les mesures de qualité de tendance de chaque trade ; 8 colonnes à la fin du CSV de l'historique).
- Aucune règle n'en dépend. Après environ 100 trades par mode, comparaison des pentes (et du R²) entre trades gagnants et perdants pour décider d'une règle éventuelle.

## v4.490 — Scalp : « ascenseur », pente 1 h, stop élargi, stop sur clôture 5 min (paper ET live)
Toutes les règles ci-dessous s'appliquent de la même façon en paper et en live (chacune a son interrupteur, tous à 1 par défaut).
- **Ascenseur** : efficacité 1 min sur 30 bougies (`SCALP_ASC_ER_MAX` = 0,25) ; en dessous, le prix « fait l'ascenseur » (monte et descend sans avancer). Enregistré aussi : amplitude des 3 dernières bougies 5 min en ATR.
- **Pente** (`SCALP_SLOPE_FILTER_ON`) : trade refusé si la pente de la tendance 1 h va contre son sens, quand la tendance est régulière (régularité ≥ `SCALP_SLOPE_MIN_R2` = 0,4).
- **Entrée en ascenseur** (`SCALP_ASC_ENTRY_ON`) : pas de cassure (on attend un repli) ; un rebond doit être près de la ligne inclinée de support (long) ou de résistance (short), à moins de `SCALP_ASC_NEAR_LINE_PCT` = 0,5 %.
- **Stop élargi** (`SCALP_ASC_SL_ON`) : en ascenseur, stop = max(stop normal, `SCALP_ASC_SL_ATR` × ATR 5 min) plafonné à `SCALP_ASC_SL_MAX_PCT`; la taille est réduite pour garder la même perte en $.
- **Stop sur clôture 5 min** (`SCALP_ASC_CLOSE_STOP_ON`) : le stop « mou » ne ferme que si la dernière bougie 5 min clôturée (après l'entrée) a clôturé au-delà ET que le prix y est encore, tant que la pente 1 h reste dans le sens du trade ; si la pente se retourne, il agit au prix (« STOP LOSS »). Un **stop de secours** à `SCALP_ASC_HARD_MULT` × le stop (2 par défaut) est posé comme SL du ticket, donc **natif sur Hyperliquid en live** : en cas de mèche violente, la perte maximale est ce stop de secours. Motif de sortie : « STOP SUR CLOTURE 5 min ». Dès que le seuil de rentabilité, le verrou ou le TP1 prennent le relais, le stop normal s'applique.
- Le CSV de l'historique gagne `asc_er_1m`, `asc_range_atr`, `ascenseur`, `sl_mou_pct`, `sl_secours_pct`, `regles_asc`.
- Swing Support et Tendance : pentes enregistrées (v4.489), règles pas encore appliquées.

## v4.491 — Filtre de pente : fenêtre courte (12 h)
- Le filtre de pente du Scalp comparait le sens du trade à la pente des 36 dernières bougies 1 h. Après un repli récent, cette pente restait positive alors que la situation était « BAISSE SAINE » et refusait les shorts (8 refus en une heure le 28/10). Le filtre utilise maintenant `SCALP_SLOPE_BARS` = 12 bougies 1 h (12 h). La pente sur 36 h reste enregistrée dans les colonnes `pente_*_1h`.
- Nouvelles colonnes du CSV : `pente_filtre_atr`, `pente_filtre_r2`, `pente_filtre_bougies` (pente réellement utilisée par le filtre). Motif de refus : « pente N h contre le trade ».
- `SCALP_SLOPE_BARS` = 36 rétablit l'ancien comportement ; `SCALP_SLOPE_FILTER_ON` = 0 désactive le filtre.

## v4.492 — Ascenseur : seuil recalibré, ligne inclinée en mesure seule
- Journal du 28/10 (12h35–14h10) : 5 cassures refusées avec une efficacité 1 min de 0,01 à 0,16 et 2 rebonds shorts refusés car le prix était à +1,40 % et +4,42 % de la ligne inclinée de résistance. Un prix aléatoire donne environ 0,15 sur 30 bougies : le seuil de 0,25 classait presque tout en « ascenseur ».
- `SCALP_ASC_ER_MAX` : 0,25 → **0,08** (va-et-vient nettement plus marqués que le hasard).
- `SCALP_ASC_NEAR_LINE_PCT` : 0,5 → **0** (0 = mesure seule : l'écart à la ligne inclinée est enregistré dans `regles_asc` mais ne refuse plus). Une valeur > 0 rétablit le refus.
- Paper et live identiques. Aucun autre changement.

## v4.493 — Scalp : audit, interrupteur cassures, actifs en mauvaise passe, mesures de la zone
- Audit (401 trades paper, 19→28/10) : brut +9,41 $, frais 26,70 $, net −17,29 $. Sorties « durée maximale » : 91 trades, −21,8 $ (62 avaient atteint +0,2 % au plus haut). Cassures : 84 trades, −10,6 $ net. Note ≥ 80 : 172 trades, −14,3 $.
- `SCALP_BREAKOUT_ON` (1 par défaut = inchangé) : 0 = seuls les rebonds sont pris (paper et live).
- `SCALP_ASSET_BLOCK_ON` (0 par défaut) : met de côté un actif si, sur ses `SCALP_SAFE_WINDOW` derniers trades (20), au moins `SCALP_ASSET_BLOCK_MIN_TRADES` (15) sont fermés, la réussite est sous `SCALP_ASSET_BLOCK_WINRATE` (42 %) et le net est négatif. L'actif revient tout seul quand sa série s'améliore.
- Sortie anticipée sur la durée : pas de nouveau code, le stop au seuil de rentabilité existe déjà (`SCALP_BE_ENABLED` = 1, `SCALP_BE_ARM_PCT` = 0,25, `SCALP_BE_LOCK_PCT` = 0,1).
- Mesures seules (aucun effet sur les décisions), ajoutées au CSV de l'historique : `room_pct`, `zone_touches`, `zone_largeur_pct`, `dist_niveau_pct`, `volume_ratio`.
- Recherche d'un filtre sur la base existante (apprentissage 19→22/10, test 23→28/10) : aucun seuil sur les mesures enregistrées (flux, BTC, tendances H1/15 min, efficacités, note) ne tient sur les deux périodes. Les lignes obliques et les zones n'étaient pas enregistrées avant la v4.489.

## v4.494 — Scalp : trois règles de l'audit activées par défaut (paper et live)
- `SCALP_BE_ENABLED` = 1, `SCALP_BE_ARM_PCT` = 0,25 (`SCALP_BE_LOCK_PCT` = 0,1 inchangé) : stop au seuil de rentabilité dès +0,25 % de gain maximal.
- `SCALP_ASSET_BLOCK_ON` = 1 : actif mis de côté quand sa série récente est mauvaise (réussite < 42 %, net négatif, 15 trades minimum sur 20).
- `SCALP_BREAKOUT_ON` = 0 : seuls les rebonds sont pris.
- Les réglages déjà enregistrés par l'utilisateur gardent la priorité sur ces défauts.

## v4.495 — Actifs en mauvaise passe : essai après 24 h
- Correction de la v4.493 : un actif écarté ne trade plus, donc sa série récente ne change plus et il restait écarté indéfiniment. `SCALP_ASSET_BLOCK_HOURS` (24 par défaut, 0 = jamais d'essai) autorise un trade d'essai quand le dernier trade de l'actif date de plus de N heures ; si l'essai est perdant, l'actif est de nouveau écarté.

## v4.496 — Stop sur clôture 5 min : seulement en ascenseur
- Le stop sur clôture 5 min (v4.490) s'appliquait à tous les trades Scalp : stop de secours à 2 × le stop normal (1 % au lieu de 0,5 %). Cas TAO #553 (28/10 17:21) : trade jamais positif (pic de gain 0,02 %), baisse de −1,04 % en 9 minutes, sortie au stop de secours (−1,00 %, −0,87 $ net) au lieu de −0,5 %.
- Nouveau réglage `SCALP_ASC_CLOSE_STOP_ASC_ONLY` = 1 (défaut) : le stop sur clôture ne s'applique qu'aux trades pris en ascenseur ; les autres gardent le stop classique `SCALP_SL_PCT`. 0 rétablit le comportement v4.490–v4.495.
- Paper et live identiques. Les trades déjà ouverts ne changent pas.

## v4.497 — Shorts prudents quand BTC monte modérément (paper et live)
- Constat (journal du 28/10, 12h35–14h10) : 10 shorts refusés par « BTC en hausse > +0,10 % / 1 h », alors que ce sont les moments où se présentent les rebonds sur résistance.
- `SCALP_SHORT_BTC_SOFT_MAX_PCT` = 0,4 (0 = désactivé) : entre +0,10 % et +0,40 %, un short est admis seulement s'il s'agit d'un rebond sur une résistance testée au moins 2 fois. Le blocage normal reste total au-dessus de +0,40 %.
- Prudence : taille réduite à `SCALP_SHORT_BTC_SOFT_SIZE_PCT` = 50 % ; flux vendeur d'au moins `SCALP_SHORT_BTC_SOFT_FLOW` = 55 % de ventes agressives ; les autres filtres (tendance, objectif, ascenseur, actif en mauvaise passe) s'appliquent comme d'habitude.
- Le CSV de l'historique gagne `short_prudent_btc` (variation de BTC au moment de l'entrée) pour comparer ces shorts aux autres.

## v4.498 — Tendance : mode réel prêt (désactivé par défaut)
- Jusqu'ici Tendance était paper par construction. Nouvel interrupteur `TRENDF_LIVE_ON` (0 par défaut) : à 1, Tendance passe de vrais ordres sur Hyperliquid.
- Entrée réelle : même chemin que les autres stratégies — levier appliqué (marge isolée pour les actifs `xyz:`), ordre au marché avec stop natif, prix de remplissage réel, vérification que la position existe sur Hyperliquid (sinon annulation et aucun suivi), refus si une position manuelle live existe déjà sur l'actif.
- Taille réelle : `min(capital réel × TRENDF_RISK_PCT ÷ distance du stop, TRENDF_MAX_NOTIONAL_USD, TRENDF_LIVE_MAX_NOTIONAL_USD)`, au minimum 10,5 $. `TRENDF_LIVE_MAX_NOTIONAL_USD` = 15 $ par défaut.
- Suivi : le stop suiveur est répercuté sur le stop natif Hyperliquid (pas minimal `MTF_SL_PUSH_MIN_STEP_PCT`) ; les sorties (stop suiveur, retournement H4/Daily, durée maximale, gain protégé) ferment la position réelle, et la fermeture n'est enregistrée que si l'ordre réel a réussi.
- Les règles d'entrée et de sortie sont identiques en paper et en réel. Les statistiques Tendance regroupent les trades des deux modes.
- Les positions ouvertes en paper restent paper même si l'interrupteur est activé ensuite.

## v4.499 — Funding : stop au seuil de rentabilité et actifs en mauvaise passe ; correction Tendance réel
- **Correction (v4.498)** : `_position_mode` forçait « paper » pour toute position Tendance, même ouverte en réel ; la fermeture d'une position Tendance réelle aurait donc été enregistrée sans ordre de fermeture. Tendance est retirée de cette liste : le mode mémorisé à l'ouverture fait foi. Test refait avec cette fonction réelle (entrée réelle, stop natif suivi, ordre de fermeture envoyé).
- **Funding · stop au seuil de rentabilité** (`FUNDING_BE_ON` = 0 par défaut) : tant que le suiveur n'est pas armé, si le pic a atteint `FUNDING_BE_ARM_PCT` (0,2) et que le gain retombe à `FUNDING_BE_LOCK_PCT` (0,05), sortie « FUNDING STOP RENTABILITE ». Même unité que le pic affiché. Paper et réel.
- **Funding · actif en mauvaise passe** (`FUNDING_ASSET_BLOCK_ON` = 0 par défaut) : plus d'ouverture sur un actif dont, sur ses `FUNDING_ASSET_BLOCK_WINDOW` (20) derniers trades Funding du même mode, au moins `FUNDING_ASSET_BLOCK_MIN_TRADES` (15) sont fermés, la réussite est sous `FUNDING_ASSET_BLOCK_WINRATE` (45 %) et le net est négatif. Un essai est permis après `FUNDING_ASSET_BLOCK_HOURS` (24) heures sans trade sur l'actif. Motif visible dans le bloqueur de l'actif.
- Donnée d'appui (200 trades Funding, 18→28/10) : 18 stops sur 35 avaient atteint +0,2 % avant de se retourner (−1,19 au total) ; OP (16 trades, 19 % de réussite, −0,17) et SEI (21 trades, −0,17) perdent.

## v4.500 — Funding : stop au seuil de rentabilité et actifs en mauvaise passe activés par défaut
- `FUNDING_BE_ON` = 1 et `FUNDING_ASSET_BLOCK_ON` = 1 (paper et réel). Réglages et seuils inchangés (v4.499).
- Les valeurs déjà enregistrées par l'utilisateur gardent la priorité sur ces défauts.

## v4.501 — Volets de recherche dans les réglages
- **Réglages du Scalp** : champ « 🔍 Rechercher un réglage » au-dessus de la grille. Recherche par libellé ou par nom technique (ex. « rentabilité », « SCALP_BE »), sans tenir compte des accents ni des majuscules ; plusieurs mots = tous doivent correspondre. Les résultats couvrent aussi les réglages de corrélation, triés (début du nom d'abord). Chaque champ affiche son nom technique et, s'il est modifié, sa valeur actuelle. Le bouton « Enregistrer les champs modifiés » n'envoie que les champs changés (les autres réglages gardent leur valeur enregistrée).
- **Réglages avancés** : même champ de recherche au-dessus des groupes ; seuls les groupes et réglages correspondants sont affichés.
- Sans recherche, les deux écrans fonctionnent comme avant.

## v4.502 — Réglages du Scalp : enregistrement en un clic des seuls champs modifiés
- Les champs modifiés sont surlignés en jaune. Une barre « 💾 Enregistrer (N modifié(s)) » apparaît en haut des onglets Réglages et Corrélation, avec la liste des champs touchés et un bouton « Annuler les modifications ».
- Un seul clic enregistre uniquement les champs modifiés (ni les autres réglages, ni leurs valeurs enregistrées ne sont renvoyés ni revalidés). Plus de fenêtre à valider : une confirmation verte « ✅ Enregistré : … » s'affiche dans la barre.
- Les boutons du bas des deux onglets et celui de la recherche (v4.501) utilisent le même enregistrement.
- Comparaison numérique : « 0.10 » et « 0,1 » ne comptent pas comme une modification.

## v4.503 — Scalp : mode oscillation (l'« ascenseur » exploité), paper et live
- Principe : quand le prix oscille sans avancer, on joue les bords de la boîte au lieu de l'éviter. Même code en paper et en live (le trade passe par le même ticket que le Scalp).
- **Détection** : efficacité 1 min < `SCALP_OSC_ER_MAX` (0,15 ; indépendant de `SCALP_ASC_ER_MAX`). **Boîte** : plus haut / plus bas des `SCALP_OSC_BOX_BARS` (12) dernières bougies 5 min, amplitude entre `SCALP_OSC_MIN_AMP_PCT` (0,5 %) et `SCALP_OSC_MAX_AMP_PCT` (3 %).
- **Entrée** : un REBOND (signal normal, donc pente 1 h et autres filtres inchangés) dans les `SCALP_OSC_EDGE_PCT` (35 %) du bas de la boîte (long) ou du haut (short). Hors du bord, le trade se fait normalement (rien n'est interdit).
- **Objectif** : `SCALP_OSC_TP_SHARE_PCT` (50 %) de l'amplitude, entre `SCALP_OSC_MIN_TP_PCT` (0,25 %, couvre les frais) et `SCALP_OSC_MAX_TP_PCT` (1,2 %). **Laisser courir** : à l'objectif le trade continue avec un stop suiveur serré `SCALP_OSC_RUN_TRAIL_PCT` (0,15 %).
- **Stop** : juste hors de la boîte (distance au bord + ½ ATR 5 min), entre 0,3 % (ou 1 ATR 5 min) et `SCALP_OSC_SL_MAX_PCT` (0,8 %), stop réel (pas de stop sur clôture ni élargi). Taille : jamais plus grande que la taille normale ; réduite si le stop est plus large que `SCALP_SL_PCT`.
- **Durée** maximale `SCALP_OSC_MAX_HOLD_MIN` (30 min) ; elle ne s'applique plus une fois l'objectif atteint (laisser courir).
- `SCALP_OSC_ON` = 1 par défaut. Le stop au seuil de rentabilité reste réglé par `SCALP_BE_*`.
- CSV de l'historique : `osc`, `osc_er`, `osc_amp_pct`, `osc_pos_boite` (0 = bas, 1 = haut), `osc_tp_pct`, `osc_sl_pct`.
- Valeurs de départ non calibrées (aucune bougie historique disponible) : à ajuster après 40 à 60 trades en mode oscillation.

## v4.504 — Scalp : mode continuation (tendance franche), ouverture sur les actifs corrélés, paper et live
- Constat : en hausse (ou baisse) nette, le Scalp n'avait aucun type d'entrée (ni rebond sur support, ni cassure de zone). Nouveau type de trade `continuation`, actif par défaut (`SCALP_CONT_ON` = 1), même code en paper et en live.
- **Tendance exigée** : 15 min dans le sens (clôture > moyenne 20 > moyenne 50), pente 1 h sur `SCALP_SLOPE_BARS` bougies dans le sens avec régularité ≥ `SCALP_CONT_MIN_R2` (0,4), H1 pas contraire, mouvement net ≥ `SCALP_CONT_MIN_MOVE_PCT` (0,5 %) sur les 2 dernières heures.
- **Entrée « repli »** : repli de `SCALP_CONT_PB_MIN_ATR` (0,3) à `SCALP_CONT_PB_MAX_ATR` (2,5) × ATR 5 min depuis l'extrême des `SCALP_CONT_BARS` (12) dernières bougies, puis bougie de reprise (clôture au-delà de l'extrême de la bougie précédente). **Entrée « élan »** (`SCALP_CONT_MOMENTUM_ON`) : bougie forte avec volume ≥ `SCALP_CONT_VOL_MULT` (1,2) × la moyenne au nouvel extrême, sans repli (pour ne pas laisser passer un mouvement).
- **Garde-fous** : prix pas étiré (≤ `SCALP_CONT_MAX_EXT_ATR` ATR 15 min de la moyenne 20 ; RSI 5 min ≤ `SCALP_CONT_RSI_MAX`), BTC pas contre (long : > −0,5 % ; short : règle habituelle `SCALP_SHORT_BTC_MAX_PCT`), flux ≥ `SCALP_CONT_FLOW_MIN` (0,5) dans le sens quand il est connu, place avant la zone H1 suivante ≥ `SCALP_CONT_MIN_ROOM_PCT` (0,3 %). Restent actifs : actif en mauvaise passe, objectif / perte du jour, mode défensif, marge disponible.
- **Taille** : marge = `SCALP_CONT_MARGIN_PCT` (20 % = 1/5) du capital courant, notionnel = marge × levier (100 $ de capital, levier 3 → 60 $ de notionnel). Jamais réduite par la corrélation. **Stop** `SCALP_CONT_SL_PCT` (0,2 %), pas d'élargissement ascenseur ni de stop sur clôture. **Objectif** `SCALP_CONT_TP_PCT` (0,6 %), puis laisser courir ; stop au seuil de rentabilité (`SCALP_BE_*`) inchangé. Durée max `SCALP_CONT_MAX_HOLD_MIN` (60 min, sans effet une fois l'objectif atteint).
- **Actifs corrélés** (`SCALP_CONT_FANOUT_ON`) : quand le trade d'origine est ouvert et que le panier confirme (≥ `SCALP_CONT_BREADTH_MIN` = 60 % des actifs dans le sens), le même sens est pris sur les actifs dont la corrélation 5 min avec l'origine est ≥ `SCALP_CONT_PEER_CORR` (0,5), qui vont dans le sens depuis 30 min, ont la tendance 15 min et la pente 1 h alignées et le prix du bon côté de la moyenne 9 (5 min), les plus forts d'abord, au plus `SCALP_CONT_MAX_OPEN` (5) trades de continuation ouverts (5 × 1/5 = tout le capital en marge). Au plus une grappe par sens toutes les 10 min.
- Statistiques : ligne CONTINUATION dans la carte Scalp ; CSV : `continuation`, `cont_type` (repli / elan / suiveur), `cont_mouvement_2h_pct`, `cont_repli_atr`, `cont_suit` (actif d'origine), `cont_correlation`, `cont_panier_pct`.
- À savoir : stop 0,2 % proche du bruit 5 min de certains actifs ; frais aller-retour ≈ 0,09 % du notionnel (près de la moitié du stop). Les valeurs de départ ne sont pas calibrées (aucune bougie historique) : juger après 30 trades, ligne CONTINUATION et colonne `cont_type` du CSV.

## v4.505 — Continuation : raison de non-entrée visible (diagnostic, aucun changement de décision)
- Chaque actif analysé garde la raison précise pour laquelle la continuation n'est pas prise : « trop étiré (3,1 ATR 15 min > 2,5) », « RSI 5 min trop haut (81) », « pente 1 h irrégulière (R² 0,31 < 0,40) », « pente 1 h contre le sens », « mouvement 2 h trop faible », « 15 min neutre », « H1 contraire », « repli hors fourchette », « repli en cours, pas de bougie de reprise », « pas de repli, dernière bougie sans élan », « élan sans volume »…
- Où la voir : bouton « Diagnostiquer maintenant » (ligne « continuation (N min) : … » de chaque actif) et, dans la carte Scalp, une ligne de synthèse « Continuation, pourquoi pas de trade » (comptage par raison sur les actifs analysés depuis 15 min, détail par actif au survol).
- Aucun seuil ni aucune décision modifiés.


## v4.506 — bougies BTC/ETH, continuation dès le début du mouvement, motif du mode défensif, bandeau « aucun signal »
Constat (diagnostic de 14 h 20) : marché en baisse depuis plus d'1 h, mais 15 actifs sur 19 « 15 min neutre » et BTC / ETH / TAO « historique insuffisant ».
- **Correctif (bougies)** : le cache de bougies est partagé par (actif, unité de temps) ; un premier appel à 30 bougies (calcul de la variation BTC) privait les suivants des 60 bougies demandées (EMA50 15 min). Le cache se recharge maintenant si l'historique demandé est plus long que celui en mémoire ; la variation BTC demande 60 bougies. BTC, ETH, TAO redeviennent évaluables (rebond/cassure comme continuation).
- **Continuation précoce** (`SCALP_CONT_EARLY_ON`, 1) : l'EMA50 15 min (~12 h) n'est croisée qu'après une longue baisse/hausse. Est aussi accepté « prix du bon côté de l'EMA20 15 min ET EMA20 orientée dans le sens depuis 1 h » ; la pente 1 h régulière (R²), le mouvement 2 h, l'étirement, le RSI, BTC, le flux et la place avant la zone H1 restent exigés. Le trade porte `cont.early` = vrai. 0 = ancien critère.
- **Diagnostic** : un signal de continuation valide mais refusé plus loin (mode défensif « stop dans le bruit », capital, pause…) affiche « signal refusé — motif exact » au lieu de rester muet.
- **Bandeau « aucun signal »** (toutes les pages, bouton « J'ai pris connaissance ») : visible si le Scalp est actif (fenêtre ouverte), si le panier bouge d'au moins `SCALP_NOSIG_MOVE_PCT` (0,5 %) sur 30 min et si aucun signal valide n'a été vu depuis `SCALP_NOSIG_MIN` (30 min). Il indique la durée, le mouvement, le mode défensif éventuel et les motifs principaux. L'acquittement est mémorisé par épisode (réaffiché après 60 min si toujours sans signal, ou à l'épisode suivant). `SCALP_NOSIG_ON` = 0 le supprime. Nouveau point d'API léger `/api/scalp/nosig`.

## v4.507 — pause du Scalp autour des annonces macro (USD, impact fort)
- **Nouveau fichier `macro_events.py`** (à déposer). Calendrier en deux sources : (1) flux JSON public de calendrier économique téléchargé en arrière-plan (toutes les 6 h, réessai 15 min en cas d'échec) ; (2) filet de sécurité sans réseau : emploi US (1er vendredi du mois, 8 h 30 New York, heure d'été/hiver gérée) et décisions FOMC 2026 (14 h New York, liste à compléter chaque année dans `FOMC_ALL_2026`). Si le fichier n'est pas déposé, la pause est simplement inactive.
- **Pause** (`SCALP_MACRO_ON`, 1) : de `SCALP_MACRO_BEFORE_MIN` (10) minutes avant à `SCALP_MACRO_AFTER_MIN` (20) minutes après l'annonce, **aucune nouvelle entrée** (paper et live, rebond, cassure, continuation et grappe). Les positions ouvertes sont gérées normalement. Refus journalisé « pause macro : … ».
- Pendant la pause, le bandeau « aucun signal » est coupé. La carte Scalp affiche la pause en cours, les 4 prochaines annonces (UTC, dans N min) et l'état du flux ; le diagnostic par actif affiche « ⏸ pause macro ».
- Le CPI, le PCE et les autres annonces n'arrivent que par le flux (leurs dates ne sont pas codées en dur) : si la carte affiche « flux indisponible », seuls l'emploi et la Fed sont protégés.


## v4.508 — « Dernière erreur technique » s'efface quand l'appel suivant réussit
La dernière erreur technique du Scalp (réglages) restait affichée indéfiniment, même après le retour à la normale (ex. 429 de limite de débit Hyperliquid sur l'univers). Elle est maintenant effacée dès que la même source réussit : mise à jour de l'univers (« univers : … ») et chargement des bougies d'un actif (« bougies X : … »). Les autres erreurs restent affichées jusqu'à leur propre succès ou remplacement. Aucun autre changement.

## v4.509 — suivi après sortie du stop de rentabilité + résultats par situation de marché (observation seule)
Aucune règle de trading modifiée.
- **Après la sortie** : pour chaque trade Scalp coupé par le « STOP SEUIL RENTABILITE », 65 min après la sortie le bot relit les bougies 5 min des 60 minutes suivantes et calcule ce qu'aurait donné « tenir » : objectif (`tp_pct`) ou stop initial (`sl_pct`) touché en premier, sinon cours à +60 min ; dans une même bougie 5 min, le stop passe d'abord (prudent). Verdict par trade : **coûté** (tenir > sortie de plus de 0,05 %), **protégé** (tenir < sortie de plus de 0,05 %) ou **neutre**, avec l'écart en % du prix. Les anciens trades sortis par ce stop sont évalués aussi (3 par cycle de 2 min). Résultat stocké dans le trade (`post_exit`), affiché dans l'onglet corrélation du Scalp (« Stop de rentabilité : que se passe-t-il après la sortie ? ») et exporté dans le CSV (`post_sortie_*`, `cont_precoce`).
- **Tableaux par situation** (onglet corrélation, net de frais, « échantillon faible » sous 10 trades) : par sens, par séance (UTC), par mouvement du panier sur 30 min relatif au sens du trade, par efficacité de l'actif sur 1 h, par type de sortie.
- Ne pas modifier l'armement / le verrouillage du stop de rentabilité avant ~30 trades évalués.


## v4.510 — continuation : la tendance se juge sur l'horizon du Scalp (bougies 5 min des 3 dernières heures)
Constat (diagnostic de 16 h 50, chute brutale) : la continuation restait bloquée par des outils trop longs pour des trades de 10 à 90 min — pente 1 h sur 12 heures (positive à cause des heures précédant la chute), structure H1 sur la dernière bougie 1 h clôturée (en retard d'une heure sur le prix actuel), EMA 15 min.
- **`SCALP_CONT_SHORT_ON` (1)** : la tendance est lue sur les `SCALP_CONT_TREND_BARS` (36 = 3 h) dernières bougies 5 min : pente de la régression des clôtures dans le sens du trade, régularité (R² ≥ `SCALP_CONT_MIN_R2_5M`, 0,3) et **confirmation par la dernière heure** (pente des 12 dernières bougies 5 min dans le même sens ; sinon « dernière heure en sens contraire » = rebond/repli, pas de continuation). Le mouvement net sur 2 h (≥ 0,5 %), l'étirement (mesuré par rapport à la moyenne 3 h en 5 min), le RSI, BTC, le flux et la place avant la zone H1 suivante restent exigés. Les EMA 15 min, la structure H1 et la pente 1 h ne décident plus (la structure H1 reste dans le CSV, `t1h`, et les zones H1 servent toujours à mesurer la place disponible). Valeur 0 = ancien critère (15 min + pente 1 h).
- Trades marqués `cont.court` (CSV : `cont_court`). Motifs de diagnostic : « pente 3 h (5 min) contre le sens », « tendance 3 h irrégulière », « dernière heure en sens contraire », « trop étiré ».


## v4.511 — continuation : stop adapté à la volatilité (taille inchangée)
Constat (trades #585 BTC et #586 AAVE, 17 h 30) : stop de 0,2 % = 0,34 × et 0,15 × l'ATR 5 min (0,58 % et 1,30 %) ; AAVE coupé en 1 seconde, BTC en 2,5 minutes.
- `SCALP_CONT_SL_ATR_ON` (1) : stop = max(stop de base 0,2 %, `SCALP_CONT_SL_ATR` (1,5) × ATR 5 min), plafonné à `SCALP_CONT_SL_MAX_PCT` (0,8 %). **La taille reste 1/5 du capital en marge** (choix de l'utilisateur) : la perte maximale par trade monte avec le stop (à ×3 : 0,12 % du capital à 0,2 %, 0,48 % à 0,8 % ; jusqu'à 5 trades corrélés d'une grappe).
- Quand le stop est élargi : objectif = max(`SCALP_CONT_TP_PCT`, `SCALP_CONT_TP_RR` (2) × stop) ; armement du stop de rentabilité = max(armement global, 0,5 × stop) ; suiveur = max(suiveur global, 0,5 × stop).
- Rien n'est refusé pour volatilité excessive ; au-delà du plafond, le stop reste à 0,8 % (donc dans le bruit si l'ATR 5 min dépasse ~0,55 %). `sl_ticket_pct` (CSV) donne le stop effectif ; `cont.sl_base`, `cont.sl_eff`, `cont.atr5_pct` sont enregistrés dans le trade.

## v4.512 — Top-down : fourchette et scénario argumenté (information seulement)
Demande : situation de consolidation après une forte baisse (BTC −3,85 % puis stabilisation) → lecture « swing » avec fourchette entre le niveau le plus bas et le plus haut, et prédiction argumentée.
- Nouvelle fonction `mtf_analysis.range_scenario` : fourchette = plus bas / plus haut des `MTF_RANGE_BARS` (20) dernières bougies Daily ; « consolidation » si sa hauteur ≤ `MTF_RANGE_MAX_ATR` (7) ATR Daily et ≥ 2 contacts de chaque côté ; nombre d'allers-retours = traversées du milieu ; position du prix en % de la fourchette.
- Arguments chiffrés (+1 / 0 / −1) : tendance Daily, tendance H4, position dans la fourchette (haut = rejet probable, bas = rebond probable), momentum H4 sur 24 h → « penche haussier / baissier / neutre » (|score| ≥ 2).
- Deux scénarios écrits avec leurs niveaux : A) rejet ou rebond sur la borne, retour vers le milieu puis l'autre borne, invalidé par une clôture Daily au-delà de la borne ± 0,25 ATR ; B) cassure, objectif = hauteur de la fourchette projetée.
- Affiché dans le diagnostic de l'onglet Indicateurs (ligne « 🔮 SCÉNARIO ») et enregistré dans les raisons de chaque trade Top-down ouvert (« scenario : … »).
- **Aucun filtre, aucune entrée ne dépend de ce calcul.** Désactivable : `MTF_SCENARIO_ON` = 0. Lecture en cache seul (aucune requête en plus).
- Les entrées restent celles du moteur existant : zone Daily/H4 + signal de bougie (+ figure double sommet/creux avec `MTF_DOUBLE_PATTERN_ENABLED`) ; la tolérance de tendance se règle par `ACCUMULATION_TREND_MODE` / `SPOT_ACCUM_TREND_MODE`.

## v4.513 — Scénario : cassure de fourchette et bornes sur les zones Daily (information seulement)
Constat (diagnostic du 29/10, 21 h) : BTC sorti par le bas de sa fourchette de 20 jours affichait « prix à −11 % · pas de consolidation nette » au lieu de décrire la cassure ; une fourchette fixée par un seul pic (1 contact de chaque côté).
- **Sortie de fourchette** : prix au-delà de 0,25 ATR Daily d'une borne → le scénario décrit la **cassure** (« sortie par le bas / par le haut »), son objectif (hauteur de la fourchette projetée) et ce qui l'annule (clôture Daily de retour dans la fourchette = risque de piège). L'argument « prix sous / au-dessus de la fourchette » compte −1 / +1 dans le score.
- **Bornes** : quand un support et une résistance Daily distincts encadrent le prix, la fourchette va du haut du support au bas de la résistance (source indiquée entre crochets : « zones Daily » ou « extrêmes des N derniers jours »). Dans la zone de support ou de résistance, le prix est encore *dans* la fourchette ; la sortie se compte au-delà de la zone entière.
- Toujours aucun filtre ni entrée dépendant de ce calcul ; `MTF_SCENARIO_ON` = 0 pour le couper.

## v4.514 — Continuation : le meneur n'entre que si le panier suit ; les refus sont suivis pour apprendre
Constat (continuation depuis #587, 29-30/10) : 7 groupes confirmés (meneur + suiveurs, panier ≥ 60 % dans le sens) = 24 trades, **+1,74 $** ; 8 meneurs seuls (panier < 60 %) = **−2,26 $** (7 pertes). Les 7 longs de la nuit étaient tous des meneurs seuls, stoppés sans jamais monter.
- **`SCALP_CONT_LEADER_BREADTH_ON` (1)** : le meneur n'entre que si l'étendue du panier atteint `SCALP_CONT_BREADTH_MIN` (60 %), le même seuil que pour la grappe. 0 = comportement d'avant. Étendue non mesurée : on n'empêche pas.
- **Rien d'autre ne change** : suiveurs, grappe (plafond 5, 10 min), stop adapté à l'ATR (v4.511), objectif, stop de rentabilité et trailing sont inchangés.
- **`SCALP_CONT_SHADOW_ON` (1)** : chaque meneur refusé est suivi (un par actif et par 15 min) ; 65 min plus tard, on rejoue ses bougies 5 min avec le stop et l'objectif qu'aurait reçus un vrai trade (stop d'abord sur la même bougie, frais 0,09 %). Le Scalp → « corr » affiche : refus suivis, objectifs / stops atteints, résultat hypothétique net. Observation seule, rien n'est ouvert.
- Lecture : si le total hypothétique devient nettement positif sur 30 refus ou plus, la règle coûte de l'argent et sera revue ; sinon, elle protège.
- Nouvelle table dans « corr » : continuation par étendue du panier au déclenchement (≥ 60 % / < 60 %). Le CSV contient déjà `cont_panier_pct` (étendue pour les meneurs aussi).

## v4.515 — Version en cours et statut du serveur (badge dans l'en-tête)
- Nouveau badge **« 🖥 v4.515 · 3 h 12 min »** dans l'en-tête, à côté de « WEBSOCKET OK » : version du bot et durée de fonctionnement. Vert = tout va bien ; orange = page et bot à des versions différentes (le badge affiche alors « ⚠ page vX »), volume persistant non configuré, dernier cycle > 30 s ou erreur technique du Scalp ; rouge = moteur arrêté, dernier cycle > 90 s ou WebSocket instable.
- Un clic ouvre le panneau : version du bot (et build) et de la page, démarrage et durée, **numéro de redémarrage** (il doit augmenter à chaque déploiement), volume persistant (✓ ou ⚠), moteur et trading, **dernier cycle**, **WebSocket (âge du dernier prix)**, taille de la base, disque libre, mémoire, Scalp (état, mode, trades ouverts, dernier scan, **dernière erreur technique**, état du calendrier macro), heure du serveur UTC.
- Nouvelle route `GET /api/server/status` (connexion requise), interrogée toutes les 15 s ; le bot enregistre l'heure de son dernier cycle (`_last_cycle_ts`). Aucun effet sur le trading.

## v4.516 — Laboratoire de stops (observation seule)
- **But** : savoir, avec des données, si le stop de rentabilité (BE) empêche des gains et quelle règle de stop serait la meilleure. Aucune sortie réelle n'est modifiée.
- **Principe** : une fois la durée maximale d'un trade Scalp écoulée (+ 6 min), ses bougies 5 min sont rejouées depuis l'entrée avec 7 règles : actuelle, sans stop de rentabilité, rentabilité tardive (80 % de l'objectif), stop réduit (−½ stop), deux temps, verrou progressif (50 % du gain maximal), suiveur sur ATR (gain maximal − 1,5 × ATR 5 min). Stop testé avant le gain dans une même bougie (prudent), frais 0,09 %, laisser courir comme en réel.
- **Lecture** : onglet Scalp › « corr », bloc « 🧪 LABORATOIRE DE STOPS » : net cumulé par règle, tous trades et par classe de pic (< 0,4 / 0,4–0,8 / ≥ 0,8 %), comparé au réel. « Règle actuelle » doit rester proche du réel (calibrage). Décider à partir de 30 trades ou plus par classe.
- **Limite** : bougies 5 min (l'ordre des ticks dans une bougie est inconnu) ; trades de moins de 5 min peu informatifs.
- Réglage : `SCALP_LAB_ON` (1 par défaut ; 0 = arrêt). Fichiers : scalp_crypto.py, bot_engine.py, api.py, index_src.html, index.html.

## v4.517 — Ordres sur zone (programmés par le bot)
- **Principe** : toutes les minutes, pour chaque actif de `ZONEORD_SYMBOLS` (BTC, ETH, HYPE, TAO, SUI), le bot lit les zones Daily et la tendance Daily. Si le prix a quitté un support (à plus de 0,5 ATR Daily, moins de 4) il programme un **rebond** (ordre « rebond confirmé » du Trading Manuel) sur le haut de cette zone ; symétriquement un **recul** sur le bas de la résistance quittée.
- **Sens et tendance** : pas d'achat si la tendance Daily est baissière, pas de vente si elle est haussière.
- **Stop / objectif** : stop au-delà de la zone (0,3 ATR Daily, maximum 6 %) ; objectif avant la zone opposée (tampon 0,3 %), plafonné à 4 × le stop, minimum 1,5 × le stop (sinon pas d'ordre) ; puis suiveur (« laisser courir »).
- **Annulation automatique** : zone cassée (0,5 ATR Daily au-delà), tendance Daily devenue contraire, zone disparue après recalcul, ou 48 h sans déclenchement. Contrôle refait juste avant l'ouverture.
- **Paper puis live** : `ZONEORD_LIVE` = 0 (paper) par défaut ; à passer à 1 après avoir jugé le bilan (carte « 🎯 ORDRES SUR ZONE » en haut des ordres programmés : en attente, ouverts, clos, net, annulés, expirés, et la raison de chaque décision par actif). Les ordres paper et live sont comptés séparément.
- Réglages `ZONEORD_*` (Réglages avancés). Les ordres sont des « ordres libres » (étiquette conservée) portant la marque `zone_order`. Fichiers : zone_orders.py (nouveau), manual_trading.py, bot_engine.py, api.py, index_src.html, index.html.

## v4.518 — Ordres sur zone : portée réaliste
- Correctif : un ordre pouvait être programmé sur un niveau à 11 % du prix (ETH, #636) avec 48 h de validité. Distance maximale ramenée de 4 à **1,5 ATR Daily** (≈ ce que le prix parcourt en 2 jours).
- Un ordre déjà programmé est **annulé** si le prix s'éloigne à plus de 1,5 fois cette distance (« le retour n'est plus attendu »). Les ordres trop lointains déjà en place sont donc annulés au cycle suivant (1 min).

## v4.519 — Ordres sur zone : données Daily chargées par le module
- Correctif : les ordres sur zone lisaient les bougies Daily dans le cache seul, rempli par d'autres modules (Top-Down, Swing Support) : un actif non suivi par eux restait sans données. Le module charge maintenant lui-même les bougies Daily de ses actifs (une requête par bougie Daily et par actif).
- Garde-fou : bougies Daily plus vieilles de 2,2 jours ou moins de 60 bougies = aucun ordre, et la raison s'affiche dans la carte « 🎯 ORDRES SUR ZONE ».

## v4.520 — Ordres sur zone : tendance H4 enregistrée (observation seule)
- Chaque ordre sur zone enregistre la tendance H4 et la position du prix par rapport à la moyenne 20 H4, à la création (`h4_create`) et au déclenchement (`h4_fire`). Aucun effet sur les décisions.
- La carte « 🎯 ORDRES SUR ZONE » regroupe les trades clos par tendance H4 au déclenchement et selon que le prix était déjà du bon côté de la moyenne 20 H4 (nombre, gagnants, net). Décider à partir d'une vingtaine d'ordres clos.
- Correctif : le contrôle juste avant l'ouverture (v4.517) ne lit plus que le cache (jamais de requête réseau dans le fil des prix).

## v4.521 — Laboratoire de stops : variante « verrou à +0,2 % »
- Constat (trades #632, #633, #637, #638) : le stop de rentabilité est posé à +0,10 % alors que les frais font 0,09 % ; les sorties se font à +0,05 / +0,10 %, soit un net de −0,02 à 0,00 $ malgré des pics de 0,27 à 0,63 %.
- Le laboratoire rejoue maintenant aussi la règle « stop de rentabilité à +0,2 % » (observation seule). Les trades déjà rejoués sont recalculés avec cette variante (2 par passage).

## v4.522 — Ordres sur zone : largeur de zone plafonnée, messages chiffrés
- Constat (premier bilan) : presque aucun ordre possible : zones Daily démesurées sur consolidation (stop 5,4 % sur BTC, 9,9 % sur TAO ; prix « dans la zone » en permanence). Plafond de largeur propre aux ordres sur zone : `ZONEORD_ZONE_MAX_WIDTH_ATR` = 2 ATR Daily (n'affecte pas le Top-Down).
- Les raisons affichées donnent maintenant les bornes de la zone, la distance en ATR et les limites.

## v4.523 — Ordres sur zone : stop selon la volatilité
- Le stop n'est plus posé au-delà de toute la zone (trop large sur zones épaisses, ex. 5,4 % sur BTC) mais au plus à `ZONEORD_SL_MAX_ATR` (1 ATR Daily) au-delà du bord ; le risque dépend de la volatilité, pas de la largeur de la zone (0 = ancien comportement).
- `ZONEORD_MIN_DIST_ATR` : 0,5 → 0,1. Un prix qui s'approche de la zone par le bon côté est le meilleur moment pour poser l'ordre ; seul un prix déjà dans la zone est refusé. (Si la valeur 0,5 a été enregistrée dans les réglages, la mettre à 0,1.)

## v4.524 — Scalp : échelle de verrous TP1 / TP2 / TP3 (désactivée par défaut)
- Réglage `SCALP_LADDER_ON` = 0 par défaut (à activer en paper). Niveaux en % du prix : `SCALP_LADDER_TP1` 0,56, `SCALP_LADDER_TP2` 0,6, `SCALP_LADDER_TP3` 1,0.
- Tant que le prix progresse le trade court. Quand le gain maximal dépasse un palier de `SCALP_LADDER_PROG_PCT` (0,03 %), le stop monte AU palier : le prix qui revient sur 0,56 % ferme le trade (« STOP PALIER »), idem à 0,6 % puis 1 %. Le stop de rentabilité (+0,1 % dès 0,4 %) reste actif avant TP1.
- À TP3 (objectif du trade = 1 %) le suiveur habituel (`SCALP_RUN_TRAIL_PCT`, ou l'échelle du stop en continuation) prend le relais.
- Pourquoi la progression de 0,03 % : sans elle, le stop serait posé au palier à l'instant même où le prix le touche et fermerait aussitôt.
- Modes : tous ceux dont l'objectif atteint TP1 (continuation, rebond). L'oscillation (objectif < 0,56 %) garde sa sortie. S'applique aux NOUVEAUX trades.
- Les paliers 0,56 et 0,6 sont distants de 0,04 % : le deuxième ajoute peu (moins que les frais, 0,09 %).

## v4.525 — Ordres sur zone : zones H4 et au moins 2 contacts
- Constat : le bot visait la zone formée par un seul pic isolé (haut du 29/10 à 23 h) : un pivot suffisait à créer une zone, et les pivots Daily ne sont confirmés qu'après 2 jours.
- Zones calculées sur le **H4** (`ZONEORD_ZONE_H4` = 1 ; 0 = Daily) : 120 bougies (20 jours), pivot confirmé en 8 h. ATR, distances, stops et tendance restent mesurés/lus en Daily : les réglages `ZONEORD_*` (en ATR Daily) gardent le même sens.
- **Au moins 2 contacts** par zone (`ZONEORD_MIN_TOUCHES` = 2) : un pic isolé ne forme plus une zone.
- Largeur maximale d'une zone ramenée de 2 à 1 ATR Daily (≈ 2 ATR H4) pour les zones H4.
- La carte indique pour chaque zone : unité de temps, nombre de contacts et ancienneté du dernier contact. Les ordres déjà programmés dont la zone n'existe plus dans ce nouveau calcul sont annulés au cycle suivant.

## v4.526 — Ordres sur zone : lecture de la fenêtre récente (10 h, bougies 15 min)
- Remplace les zones longues (20 jours) par la lecture des **10 dernières heures** (`ZONEORD_REC_HOURS`) en bougies 15 min : **sommet** et **base** de la fenêtre. Un niveau n'existe que s'il a été visité au moins 2 fois (`ZONEORD_REC_MIN_VISITS` ; une visite = des bougies dont la mèche l'atteint, groupées à moins de 2 bougies d'écart) : un pic isolé ne compte pas.
- Short sur le sommet / long sur la base, si le prix est du bon côté du niveau, à au plus 60 % de la hauteur de la fenêtre, et si la fenêtre est assez ample (≥ 3 ATR 15 min). Stop = 1 ATR 15 min au-delà du niveau ; objectif = niveau opposé (ou 60 % de la fenêtre), plafonné à 4 × le stop, minimum `ZONEORD_MIN_RR` ; suiveur après l'objectif. Tendance Daily conservée comme filtre de sens.
- La carte indique pour chaque actif : la fenêtre (bas–haut), le prix, l'élan de la dernière heure, et pour chaque niveau : visites, rejet moyen (recul dans les 4 bougies qui suivent chaque visite, en % de la fenêtre) et ancienneté de la dernière visite.
- Annulation : niveau dépassé / cassé de 1 ATR 15 min, tendance Daily contraire, niveau disparu de la fenêtre au recalcul, prix trop éloigné, ou **4 h** sans déclenchement (`ZONEORD_REC_EXPIRY_H`).
- `ZONEORD_RECENT_ON` = 0 revient aux zones H4 / Daily de la v4.525. Les ordres de l'ancien mode encore programmés sont annulés au cycle suivant.

## v4.527 — Fenêtre récente : sommets et bases intermédiaires
- Constat (bilan v4.526) : marché en baisse sur la dernière heure, donc le haut absolu des 10 h est en début de fenêtre et la base se forme maintenant : une seule visite chacun, aucun ordre.
- Les niveaux sont maintenant les **sommets / bases intermédiaires** de la fenêtre : pivots 15 min regroupés (à moins de `ZONEORD_REC_MERGE_ATR` x ATR 15 min, largeur maximale `ZONEORD_REC_ZONE_WIDTH_ATR`), au moins 2 pivots par niveau. Résistance = niveau le plus proche au-dessus du prix (short), support = le plus proche en dessous (long).
- La carte indique visites (pivots), rejet après le dernier contact (% de la fenêtre) et ancienneté.
- Objectif : niveau opposé le plus proche s'il offre au moins `ZONEORD_MIN_RR` x le stop, sinon extrême de la fenêtre (le plus loin que le prix a déjà été).

## v4.528 — Ordres sur zone : un seul sens à la fois par actif
- Un long sur la base et un short sur le sommet peuvent être programmés ensemble (le prix touchera l'un des deux en premier). Dès qu'une position (ou une ouverture en cours) existe sur l'actif, l'ordre sur zone opposé est annulé, et un ordre ne se déclenche pas s'il y a déjà une position sur l'actif. Contrôle au cycle (1 min) et juste avant le déclenchement.

## v4.529 — Origine visible des ordres et trades
- Pastille d'origine dans les ordres programmés, les positions ouvertes et l'historique (colonne « Source » et colonne « source » du CSV) : « 🎯 Auto zone » (ordre programmé par le bot sur la fenêtre récente), « ✋ Manuel » (ordre libre créé à la main), « Scalp », ou le nom de la stratégie (Swing Support, Accumulation...).
- Filtre « Origine » dans l'historique pour isoler chaque famille (statistiques et export CSV du filtre affiché). Champ `origin` ajouté aux ordres et aux lignes d'historique.

## v4.530 — Ordres sur zone : verrou progressif et durée maximale (y compris sur les positions déjà ouvertes)
- Verrou progressif (`ZONEORD_LOCK_ON`) : dès que le gain maximal atteint 33 % de la distance à l'objectif (`ZONEORD_LOCK_ARM_FRAC`), le stop garantit 50 % du gain maximal (`ZONEORD_LOCK_FRACTION`) ; il ne fait que monter (long) / descendre (short). Mécanisme du Trading Manuel existant (v4.343), sortie « STOP DYNAMIQUE ».
- Durée maximale (`ZONEORD_HOLD_H` = 6 h, 0 = illimitée) : la position est fermée au marché (« DUREE MAXIMALE ») ; sans effet une fois le suiveur de l'objectif enclenché.
- Les positions sur zone DÉJÀ OUVERTES reçoivent ces règles au cycle suivant (1 min) ; la durée se compte depuis leur ouverture réelle.

## v4.531 — Un trade qui progresse n'est plus coupé par la durée maximale
- À l'échéance de la durée maximale (ordres sur zone : `ZONEORD_HOLD_H` ; Scalp : durée du mode, `SCALP_HOLD_RUN_ON` = 1), un trade **en gain** n'est plus fermé : le suiveur de l'objectif s'enclenche (stop = plus haut − suiveur, ne fait que monter / descendre) et le trade court tant qu'il progresse ; il sort dès qu'il recule du suiveur.
- Un trade en perte ou à zéro à l'échéance est fermé comme avant. `SCALP_HOLD_RUN_ON` = 0 rétablit la fermeture à l'échéance pour le Scalp.

## v4.532 — Scalp : « tendance soutenue » (mouvement tenu ~30 min sans repli) + détection de sortie de zone
- **Constat** (467 trades Scalp du 19 au 30/10) : le rebond dans une tendance saine (panier corrélé, 15 min dans le sens) gagnait (+5,95 $ sur 103 trades) ; la continuation (repli ou élan avec volume) ne gagnait pas (−1,45 $ sur 41). Une montée lisse sans repli (SUI/TAO du 30/10 soir) n'était prise par aucun mode.
- **Détection** (`sus_measure`, pur) sur les bougies 5 min des SUS_MIN dernières minutes (30) : mouvement net ≥ max(0,35 %, 2 ATR 5 min), efficacité ≥ 0,45, repli maximal ≤ 1,5 ATR 5 min, prix du bon côté de la moyenne 9, dernière bougie pas franchement contraire. Les va-et-vient sont tolérés.
- **Entrée** : repli d'abord, puis (si refusé) tendance soutenue, même sens que le panier, mêmes garde-fous que la continuation (BTC, flux, place avant la zone H1, le meneur n'entre que si le panier suit) + étirement ≤ 4 ATR 15 min, RSI ≤ 93. Les actifs corrélés peuvent suivre.
- **Stop** : max(SUS_SL_MIN 0,25 %, 1,3 × ATR 5 min, **1,3 × le va-et-vient observé sur 1 h**) borné à 0,8 % ; objectif ≥ 2 × le stop puis suiveur ; stop de rentabilité armé à la moitié du stop. Taille inchangée (1/5 du capital en marge).
- **Sortie de zone** (`sus_end`) : recul ≥ max(1,2 ATR 5 min, 1,3 × bruit habituel) depuis l'extrême des 30 min, ou 2 clôtures du mauvais côté de la moyenne 9. L'actif passe en phase « fin » (visible), les autres modes continuent de tourner. Trade ouvert EN GAIN : suiveur serré (0,15 %) ; en perte ou nul : durée restante 15 min, stop inchangé.
- Réglages : SCALP_SUS_ON (1) et SCALP_SUS_* (visibles dans les réglages). Même mode (paper/live) que le Scalp. Bloc « ⟿ Tendance soutenue » dans la carte Scalp ; événements « zone » dans le journal.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.533 — Ordres sur zone : le sens autorisé suit la tendance H1 (le Daily reste affiché)
- Constat : un ordre sur zone vit 4 h et est tenu 6 h ; un veto de la tendance Daily (ex. HYPE : short refusé parce que le Daily est haussier alors que le fond 1 h est baissier) est trop lent pour cet horizon. Le Scalp lui-même n'utilise pas le Daily pour entrer.
- Fenêtre récente : refus d'un long en H1 baissière / d'un short en H1 haussière, et annulation d'un ordre quand l'H1 devient contraire. Le Daily reste affiché dans la carte (« tendance H1 baissiere (Daily haussiere) ») et sur l'ordre.
- Réglage ZONEORD_REC_TREND_H1 (1 = H1, 0 = Daily comme avant). Les zones H4 / Daily (ZONEORD_RECENT_ON = 0) et le mode Tendance ne sont pas touchés.
- Fichiers : zone_orders.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.534 — Scalp : pas de continuation contre l'H1 ; ordres sur zone : tolérance au glissement de la fenêtre
- **Constat 30/10 (44 trades Scalp, −3,92 $)** : longs de continuation pris avec l'H1 baissière : 14 trades, 2 gagnants, 7 stops loss, −2,34 $ (la semaine d'avant : 0 sur 7) ; avec l'H1 neutre : 2 sur 2, +0,48 $. Stops loss de la journée : 15, aucun gagnant, pic moyen +0,14 % (jamais en gain).
- **Continuation** (repli, élan, suiveur ET tendance soutenue) : refusée quand l'H1 est contraire au sens (long en H1 baissière, short en H1 haussière). H1 neutre ou dans le sens : inchangé. Motif visible : « continuation long refusée : H1 baissiere (contraire) ». Réglage SCALP_CONT_H1_BLOCK (1 par défaut ; 0 = comme avant). Le mode Tendance et les autres modes (rebond, oscillation) ne sont pas touchés.
- **Ordres sur zone** : 16 des 19 annulations du 30/10 étaient des « recalculs » (la fenêtre de 10 h glisse, le niveau n'est plus le plus proche) avant d'avoir pu se déclencher. L'ordre reste valable tant qu'une zone de la fenêtre retrouve son niveau (même si ce n'est plus la plus proche), et sinon bénéficie d'une grâce de ZONEORD_REC_GRACE_H heures (2 par défaut ; 0 = annuler tout de suite) depuis sa création. Niveau dépassé, tendance H1 contraire, prix trop éloigné : annulation immédiate comme avant.
- Fichiers : scalp_crypto.py, zone_orders.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.535 — Ordres sur zone : qualité du niveau notée sur chaque ordre et exportée (observation seule)
- Constat 31/10 : 7 ordres sur zone clos cette nuit, 6 stops loss (−2,02 $), pic moyen +0,14 % ; depuis le début 10 clos, 2 gagnants, −0,73 $. Le CSV ne dit rien de la qualité du niveau au moment de l'ordre : impossible de savoir quels setups gagnent.
- Chaque ordre sur zone (fenêtre récente) enregistre à sa création : visites du niveau, rejet moyen (% de la fenêtre), ancienneté du dernier contact (min), hauteur de la fenêtre (ATR 15 min et %), position du prix dans la fenêtre, élan 1 h, distance au niveau (ATR 15 min, % de la fenêtre), RR, stop en ATR 15 min, ATR 15 min %, tendance H1 et Daily.
- Export CSV de l'historique : 14 colonnes « zo_* » en fin de ligne (vides pour les autres trades). Les anciens ordres n'ont pas ces valeurs.
- AUCUNE règle de décision ne change. Après ~20 ordres clos avec ces colonnes, on décide d'un filtre sur les chiffres.
- Fichiers : zone_orders.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.536 — Diagnostic Scalp : la raison de la tendance soutenue sur chaque ligne (information seule)
- Le diagnostic par actif n'affichait que le motif de la continuation ; celui de la tendance soutenue n'apparaissait qu'au survol de la carte. Chaque ligne affiche maintenant : « tendance soutenue : EN COURS (hausse depuis N min, mouvement, efficacité, repli) », « terminée il y a N min (motif) » ou « tendance soutenue (N min) : <raison du refus> » (mouvement trop faible, efficacité, repli, étirement, RSI, BTC, flux, zone H1, H1 contraire…).
- Aucune règle de décision ne change. Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.537 — Ordres sur zone : flux d'achats agressifs enregistré (observation seule) + pastille « 🎯 AUTO ZONE »
- **Constat** : les ordres sur zone respectent la tendance H1 et la fenêtre, mais ne regardent pas le flux des transactions (le Scalp, lui, refuse une continuation à contre-flux). Impossible de savoir aujourd'hui si un ordre parti contre le flux perd plus.
- **Flux** : à la création de l'ordre puis au déclenchement, le bot note la part d'achats agressifs sur les 10 dernières minutes et le nombre de transactions (`flow_create`, `flow_fire` dans l'ordre). À la création, le bot s'abonne à la volée au flux de l'actif s'il n'y est pas déjà (la première lecture peut donc être vide). Aucun filtre : l'ordre n'est jamais bloqué ni annulé à cause du flux.
- **CSV de l'historique** : 5 colonnes en fin de ligne : `origine` (zone / manuel / scalp…), `zo_flux_creation_pct`, `zo_flux_creation_n`, `zo_flux_declench_pct`, `zo_flux_declench_n`. Le pourcentage est orienté dans le sens du trade : 50 = neutre, > 50 = flux dans le sens, < 50 = contre. Vide pour les anciens ordres et pour les autres trades.
- **Pastille « 🎯 AUTO ZONE »** (violet) sur les ordres programmés par le bot : ordres programmés, positions ouvertes (onglet Trading manuel et cartes de trades ouverts), historique. Elle remplace « ✋ MANUEL » qui s'affichait à tort sur ces positions. Le filtre d'origine de l'historique (v4.529) est inchangé.
- AUCUNE règle de décision ne change. Après ~20 ordres clos avec ces colonnes, on décide d'un filtre de flux (et de hauteur de fenêtre / RR) sur les chiffres.
- Fichiers : zone_orders.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.538 — Suivi après sortie des STOP LOSS (observation) + oscillation : entrée entre le bord et le milieu de la boîte, stop qui couvre le dépassement
- **Demande** : « souvent le signal revient alors que le stop a déjà opéré » ; en oscillation, prendre un stop qui couvre l'oscillation avec un objectif fixe, et ne pas entrer pile sur le support / la résistance. Jusqu'ici le suivi après sortie (v4.509) ne couvrait que les stops de rentabilité : aucune mesure sur les vrais stops loss.
- **1. Observation (aucune règle de décision)** : 3 h après chaque STOP LOSS Scalp ou ordre sur zone (y compris les stops déjà passés, dans la limite des 400 derniers trades et de l'historique de bougies 5 min disponible), le bot note sur 60 min et 3 h : minutes avant le retour au prix d'entrée, minutes avant l'objectif, le stop qui aurait survécu jusqu'à l'objectif (pire excursion avant l'objectif), et le résultat si le stop avait été ×1,5 ou ×2 (même taille, même objectif, même bougie : stop d'abord). Stocké sous `post_stop` dans le trade (n'altère pas le suivi des stops de rentabilité).
- **Affichage** : bloc « STOP LOSS DU SCALP : QUE FAIT LE PRIX APRÈS LA SORTIE ? » dans la carte Scalp et bloc équivalent dans la carte des ordres sur zone : retours à l'entrée / à l'objectif, stop médian survivant, $ bruts réel vs stop ×1,5 / ×2. CSV : 10 colonnes `stop_h1_*` et `stop_h3_*` en fin de ligne. Sous 20 stops mesurés, ne pas conclure.
- **2. Mode oscillation** (même mode paper / live que le Scalp) : (a) pas d'entrée à moins de `SCALP_OSC_EDGE_MIN_PCT` (15) % du bord de la boîte : la zone d'entrée devient 15 à 35 % du bord (≈ « vers 30 % ») au lieu de 0 à 35 % ; (b) le stop dépasse le bord d'au moins `SCALP_OSC_SL_OVERSHOOT_AMP_PCT` (25) % de la hauteur de la boîte (avant : une demi-ATR 5 min), toujours plafonné par `SCALP_OSC_SL_MAX_PCT` (0,8 %). Objectif et durée inchangés. Les deux réglages à 0 rétablissent l'ancien comportement. Exemple de test : boîte de 1 %, entrée à 30 % → stop 0,55 % (avant 0,37 %), objectif 0,50 %. Un stop plus large à objectif égal exige un taux de réussite plus élevé : à juger sur les premiers trades « osc ».
- La reconnaissance de l'élargissement de l'oscillation (passage en suiveur) n'est PAS faite : on attend les mesures.
- Fichiers : scalp_crypto.py, zone_orders.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.539 — Suivi après sortie étendu aux trades Swing Support (stops loss et fermetures à la main en perte), fenêtres jusqu'à 48 h (observation seule)
- **Constat** (36 trades Swing Support live) : 17 sorties au suiveur +5,54 $, 14 fermetures à la main −4,24 $ (2 gagnants), 5 stops à 2,5 % −6,23 $. Les gagnants au plus bas connu (12) sont descendus jusqu'à −1,67 % avant de gagner : un stop de 1 % les aurait coupés, 2 % non. Mais le fichier ne dit pas si le prix est revenu APRÈS les stops et les fermetures à la main : aucune mesure sur le Swing.
- **Mesure** : pour chaque trade `swing_support` fermé par STOP LOSS, ou par « MANUEL » avec un résultat négatif (jamais une fermeture à la main en gain), 48 h après la sortie le bot note sur 60 min, 3 h, 24 h et 48 h : minutes avant le retour au prix d'entrée / à l'objectif, le stop qui aurait survécu jusqu'à l'objectif, et le résultat « et si » : stop ×1,5 et ×2 pour un stop loss ; stop d'origine gardé (×1) et ×1,5 pour une fermeture à la main. Même bougie : stop d'abord. Les trades déjà passés sont mesurés aussi (400 derniers, dans la limite des bougies 5 min disponibles ; un échec est réessayé 3 fois puis abandonné).
- **Ce que ça écrit** : uniquement une note `post_stop` ajoutée au trade fermé (comme le suivi des stops de rentabilité). Aucune position, aucun ordre, aucun stop n'est touché ; le trade #635 n'est pas concerné (sortie au suiveur).
- **Affichage** : dans l'historique manuel, un bloc par catégorie (« Swing Support — stops loss », « Swing Support — fermés à la main en perte »). Les blocs Scalp / ordres sur zone gardent leurs fenêtres 60 min et 3 h. CSV : 24 colonnes `stop_h1_*`, `stop_h3_*`, `stop_h24_*`, `stop_h48_*` (6 par fenêtre : retour entrée, retour objectif, stop survivant, résultats ×1 / ×1,5 / ×2) ; ATTENTION : la v4.538 avait 10 colonnes `stop_*`, la disposition change.
- AUCUNE règle de décision ne change. Au-dessous de 20 trades mesurés, ne pas conclure.
- Fichiers : scalp_crypto.py, manual_trading.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.540 — Observation : tendance Daily / H4 de chaque trade Scalp, et positions de sens contraire sur actifs corrélés (aucune règle ne change)
- **Demandes** : (1) « vérifier si les chiffres ne vont pas en faveur d'un blocage de tous les Scalp posés contre la tendance Daily » ; (2) « pourquoi des trades de sens contraire sur des actifs corrélés ? ». Le CSV du Scalp ne contenait ni Daily ni H4 : sur 50 trades, 10 seulement avaient un Daily reconstituable (9 neutres, 1 contre) → test impossible.
- **Tendance à l'ouverture** : chaque nouveau trade Scalp note la tendance Daily et H4 de son actif (même calcul que l'H1 : bougies clôturées + prix actuel). Aucun effet sur les décisions.
- **Positions contraires** : à l'ouverture d'un trade Scalp, et au déclenchement d'un ordre sur zone, le bot liste les positions ouvertes de sens contraire sur d'autres actifs (même mode paper / live) avec la corrélation de leurs rendements 5 min sur 2 h (seuil « corrélé » : 0,6). Jamais bloquant.
- **CSV** (fin de ligne, après les `stop_*`) : `tend_daily`, `tend_h4`, `daily_vs_sens` (avec / contre / neutre selon le sens du trade), `contra_n_corr60` (nombre de positions contraires avec corrélation ≥ 0,6), `contra_detail` (« actif sens corrélation »). Vide pour les trades déjà passés.
- **Suite** : après ~100 trades Scalp avec ces colonnes, comparer par type de trade « contre le Daily » / « avec » / « neutre », et « avec / sans position contraire corrélée ». Aucun blocage avant.
- Fichiers : scalp_crypto.py, zone_orders.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.541 — (REMPLACÉE par la v4.542) Pause « marché calme » sur seuil d'ATR 15 min
- **Demande** : « Ajoute une pause sur le manque de mouvement qui entraîne une grosse perte en durée max. Quand il n'y a pas de volatilité, il vaut mieux attendre. »
- **Constat (539 trades Scalp paper, 19/10 → 01/11)** : les sorties « durée max » coûtent −23,7 $ (110 trades, 24 % gagnants). Sur les 149 trades dont l'ATR 15 min est connu : sous 0,34 %, 19 trades sur 40 finissent en durée max (≈ 50 %) ; entre 0,54 et 0,75 %, 1 sur 37 ; au-dessus de 0,75 %, aucun.
- **Changement (décision, paper et live)** : nouveau réglage `SCALP_CALM_ATR15_PCT` (défaut **0,30 %**, 0 = pause désactivée). Tant que l'ATR 15 min de l'actif est sous ce seuil, aucune entrée Scalp, quel que soit le mode (rebond, oscillation, continuation, cassure). L'actif est réévalué à chaque analyse (5 min) et reprend tout seul quand la volatilité revient. Refus visible dans le journal : « pause marché calme : ATR 15 min x % < 0,3 % ». Sans données de bougies, on ne bloque pas.
- **Ordres sur zone (Auto zone)** : inchangés.
- **Attention** : BTC et ETH ont naturellement un ATR 15 min bas (≈ 0,15–0,25 %) ; ils seront donc souvent en pause. Les durées max y étaient déjà à 44 % et 42 %.
- **Effet attendu, à ne pas surestimer** : sous 0,30 %, 28 trades, 13 en durée max, net −2,4 $ (n faible). La pause ne règle qu'une partie des −23,7 $ de durée max ; le reste vient de trades à volatilité normale qui n'avancent pas.
- **À vérifier** : nombre de refus « pause marché calme », puis net des trades suivants vs avant. Retour arrière : `SCALP_CALM_ATR15_PCT` = 0 dans les réglages Scalp.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.542 — Pause « marché calme » lue sur les bougies 1 min (remplace le seuil d'ATR 15 min de la v4.541)
- **Demande** : annuler le seuil d'ATR 15 min (en retard : il bloque le début d'un mouvement) et le remplacer par une lecture plus fine des bougies 1 min, avec leur comportement sur 5 min et l'amplitude des mouvements.
- **Mesure** (cache de bougies 1 min, 35 dernières) : amplitude des 5 dernières minutes ; bougie 1 min « habituelle » = médiane des 30 précédentes ; accélération = amplitude moyenne des 2 dernières bougies / habituelle ; mouvement net sur 5 min dans le sens du trade et son efficacité (net / chemin parcouru).
- **Règle** (décision, paper et live, tous les modes) : le bot entre si AU MOINS UNE condition est vraie, sinon pause réévaluée à chaque analyse (5 min) :
  1. amplitude 5 min ≥ `SCALP_CALM_AMP5_PCT` (0,20 %) ;
  2. accélération ≥ `SCALP_CALM_ACC` (×1,8 l'habituel, avec un plancher de 0,04 %) = début de mouvement ;
  3. déplacement net dans le sens du trade ≥ `SCALP_CALM_DIR_PCT` (0,10 %) avec efficacité ≥ `SCALP_CALM_EFF` (0,6).
  Moins de 35 bougies 1 min : pas de blocage. `SCALP_CALM_ON` = 0 désactive la pause.
- **Seuils PROVISOIRES** : je n'ai pas de bougies 1 min historiques pour les caler (les anciens trades n'ont pas ces mesures). Ils sont une traduction de l'ancien seuil (ATR 15 min 0,30 % ≈ amplitude 5 min 0,20 %) complétée par les deux exceptions « début de mouvement ».
- **Journal** : « pause marché calme : sur 5 min l'actif a bougé de x % seulement … ».
- **CSV** : `calm_amp5_pct`, `calm_acc`, `calm_dir_pct`, `calm_eff` pour chaque nouveau trade Scalp (valeurs à l'entrée) → après ~100 trades, on recalera les seuils sur les résultats réels.
- **Ordres Auto zone** : inchangés.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.543 — (DÉSACTIVÉE par défaut en v4.544) Marché dispersé : rebonds et cassures suspendus
- **Demande** : « une règle immédiate qui évite la majeure partie des départs à contre-sens, en couvrant les dents de scie et en respectant les tendances ».
- **Recherche d'une règle d'entrée** (539 trades Scalp paper, 19/10 → 01/11) : aucun filtre ne sépare nettement les départs à contre-sens (≈ 31 % de stops loss quel que soit le flux, le H1 ou le 15 min). Seul le **régime** ressort : dispersé = −9,9 $ sur 113 trades (33 % de gagnants), dont rebond dispersé −9,2 $ sur 60 trades (32 % de gagnants) contre rebond corrélé +3,8 $ sur 204 (53 %). La continuation n'y change pas (−0,5 $ en dispersé, −1,0 $ en corrélé). H1 contre : 14 trades, 0 gagnant au trailing TP, −2,3 $ (trop peu pour une règle).
- **Changement (décision, paper et live)** : si le régime du panier est « dispersé », le bot refuse les rebonds et les cassures. Oscillation et continuation sont inchangées. Régime inconnu : pas de blocage. Réglage `SCALP_DISP_BLOCK_ON` (1 = actif, 0 = désactivé). Journal : « marché dispersé (corrélation x) : les rebonds y perdent… ».
- **Effet attendu** : ≈ 71 trades évités sur 539 (rebonds et cassures dispersés), ≈ +9,4 $ sur la période. Sur un échantillon d'une seule période : à surveiller, pas à tenir pour acquis.
- Le **verrou de rentabilité** n'est pas modifié (sorties au seuil : protégé 21 / coûté 17, équilibre).
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.544 — Verrou de rentabilité réglable par mode ; blocage « dispersé » désactivé
- **Demandes** : (1) ajuster le verrou de rentabilité (il coupe autant de gains qu'il évite de pertes) ; (2) « je ne souhaite pas empêcher les rebonds et les cassures ».
- **Constat verrou** (39 sorties au seuil de rentabilité suivies, 19/10 → 01/11) : protégé 21 (≈ 6,0 $ de perte évitée) contre coûté 17 (≈ 5,9 $ de gain manqué) ; continuation : 15 protégés (3,6 $) / 12 coûtés (4,0 $). Exemple #827 AVAX : verrou armé à +0,25 %, repli, sortie à ~0 $ au bout de 3 min alors que l'objectif était à 0,6 %.
- **Changement (décision, paper et live)** : l'armement du verrou (gain qui remonte le stop à +0,10 %) devient réglable par mode : `SCALP_BE_ARM_CONT_PCT` (continuation, **0,40 %** au lieu de 0,25 %, avec le minimum existant de la moitié du stop), `SCALP_BE_ARM_REB_PCT` (rebond) et `SCALP_BE_ARM_CASS_PCT` (cassure). **0 = valeur générale** `SCALP_BE_ARM_PCT` : rebond et cassure sont donc inchangés. Le niveau de verrouillage (+0,10 %) ne change pas. L'oscillation garde la valeur générale.
- **Marché dispersé (v4.543)** : le blocage des rebonds et cassures est **désactivé par défaut** (`SCALP_DISP_BLOCK_ON` = 0). Le code reste disponible mais rebonds et cassures ne sont plus empêchés.
- **À vérifier** : sorties « STOP SEUIL RENTABILITE » de la continuation (nombre, `post_sortie_verdict` protégé / coûté) et trades de continuation qui atteignent le trailing TP, avant / après la v4.544.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.545 — Rebond conditionné à l'environnement en marché dispersé (au lieu d'être bloqué)
- **Demande** : éviter les pertes des rebonds en marché dispersé SANS bloquer les rebonds (« chaque dent de scie est un rebond »), en les conditionnant à l'environnement ; les cassures, il faut les confirmer plutôt que les bloquer.
- **Constat** (rebonds en marché dispersé, 60 trades, −9,2 $, 19/10 → 01/11) :
  - H1 déjà orienté dans le sens du trade : 24 trades, 12 % de gagnants, −8,3 $ (dont 16 shorts : 6 %, −7,4 $) ;
  - H1 neutre (la dent de scie) : 36 trades, 44 % de gagnants, 42 % de trailing TP, −0,9 $ ;
  - en marché corrélé, le rebond avec H1 orienté gagne (55 %, +1,6 $) : la différence est propre au marché dispersé.
- **Changement (décision, paper et live)** : en marché dispersé, un rebond dont le H1 est déjà orienté dans le sens du trade est refusé (« en dispersé on ne prend le rebond que sur H1 neutre »). Rebonds sur H1 neutre, rebonds en marché corrélé, oscillation détectée, continuation et cassures : inchangés. Réglage `SCALP_DISP_REB_H1_ON` (1 = actif, 0 = désactivé). `SCALP_DISP_BLOCK_ON` (blocage complet) reste à 0.
- **Effet attendu** : ≈ 24 trades évités sur 539, ≈ +8 $ sur la période (une seule période : à surveiller).
- **Cassures (non modifiées, constat)** : 88 trades, −11,6 $ ; cassure avec tendance 15 min déjà dans le sens (47 trades) : 28 % gagnants, −10,6 $ ; 15 min neutre (36 trades) : 44 %, −2,6 $. Les cassures qui échouent sont surtout celles prises après le mouvement. Piste : confirmer par un retest (réglage existant `SCALP_RETEST_ON`, désactivé) — non testable faute de données.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.546 — Tous les actifs suivis dans l'univers du Scalp
- **Demande** : « il faut tous les cryptos dans l'univers ». Constat : TAO était « hors de l'univers du Scalp » (le Scalp ne gardait que les 15 actifs les plus échangés parmi les actifs suivis ; liste recalculée toutes les 30 min), donc aucun signal n'était évalué dessus.
- **Changement (décision, paper et live)** : `SCALP_TOP_N` = **0** signifie « tous les actifs suivis » (classés par volume ; PAXG et les actifs xyz restent exclus comme avant). Valeur par défaut : 0. Toute valeur > 0 garde l'ancien comportement (les N plus échangés). Plage du réglage : 0 à 100.
- **À faire si la valeur est déjà enregistrée dans tes réglages** : mets « Nb d'actifs analysés » à 0 dans les réglages Scalp ; la liste se met à jour au plus tard 30 min après.
- **Points d'attention** : environ le double d'actifs analysés (≈ 30 au lieu de 15) = plus d'appels de bougies ; les actifs moins liquides ont des spreads plus larges (relevé du 01/11 : EIGEN 0,106 %, SUSHI 0,082 %, JUP 0,054 %, contre 0,001 % pour BTC / ETH / SOL), ce qui pèse sur des objectifs de 0,3 à 0,6 %. À surveiller dans les résultats par actif.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.547 — Mouvement 1 min enclenché : réponse à l'ambiguïté du H1 (rebonds et cassures)
- **Demande** : « le H1 n'est pas assez pertinent pour le Scalp ; évaluer le mouvement sur les bougies 1 min pour savoir si le mouvement est enclenché » ; « applique le point 2 aussi, il faut donner une réponse à l'ambiguïté du H1 ».
- **Constat** (539 trades Scalp paper) : tendance H1 dans le sens du trade 45 % de gagnants (247 trades, −0,038 $ / trade), H1 neutre 45 % (278, −0,044 $), H1 contre 14 % (14 trades, −0,167 $) : le H1 ne sépare pas les gagnants des perdants. Auparavant, H1 neutre ET 15 min neutre = « ni dans le sens ni contre » → le rebond passait par les sécurités de contre-tendance, la cassure était refusée.
- **Mesure (nouveau)** : sur les 5 dernières bougies 1 min (35 bougies en cache) : déplacement net (% du prix), efficacité (net / chemin parcouru), accélération des 2 dernières bougies vs l'habituelle, sens de la dernière bougie. Seuils = ceux de la pause calme (`SCALP_CALM_DIR_PCT` 0,10 %, `SCALP_CALM_EFF` 0,6, `SCALP_CALM_ACC` 1,8). États pour un sens de trade : **enclenché** (net ≥ 0,10 %, efficacité ≥ 0,6, dernière bougie dans le sens), **naissant** (net ≥ 0,05 % avec efficacité ou accélération, dernière bougie dans le sens), **contraire** (net ≤ −0,05 %), **absent**.
- **Affichage** : le verdict de chaque actif se termine par « mouvement 1 min : enclenché haussier (+0,18 % / 5 min, efficacité 0,8) » (observation, pour tous les actifs analysés).
- **Règle (décision, paper et live ; `SCALP_MOVE1M_ON` = 1, 0 = comportement d'avant)** :
  - **Rebond** : il faut un mouvement 1 min enclenché ou naissant dans son sens, quelle que soit la tendance. H1 / 15 min dans le sens ou neutres : accepté si le mouvement est là, refusé sinon (le H1 ne suffit plus). H1 contraire : mouvement exigé EN PLUS des sécurités de contre-tendance habituelles (flux, zone testée 2 fois, BTC, note).
  - **Cassure** : inchangée si la tendance est dans le sens (acceptée) ou contraire (toujours refusée) ; si H1 ET 15 min sont neutres, elle est prise seulement si le mouvement est « enclenché » (avant : refusée).
  - Moins de 35 bougies 1 min : comportement d'avant. Continuation, oscillation, règle « dispersé + H1 orienté » (v4.545), pause calme : inchangées.
- **Seuils PROVISOIRES** (pas d'historique 1 min pour les caler) : à recaler sur les trades à partir de la v4.547. Colonnes CSV : `mvt1m_etat`, `mvt1m_net_pct`, `mvt1m_eff` (+ `calm_*` depuis la v4.542).
- **Attention** : ce changement réduit le nombre de rebonds pris tant que le mouvement n'est pas enclenché ; nombre de refus « mouvement 1 min … » à surveiller.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.548 — Vétos BTC lus sur la même échelle que la décision (1 min)
- **Question** : les vétos qui obligent les cryptos corrélées à respecter la tendance BTC existent-ils encore, et sur quelle échelle ? **Constat** : oui, ils existaient tous sur la variation de BTC sur 1 h (4 bougies 15 min) : short refusé si BTC > +0,10 % / 1 h, continuation / tendance soutenue refusées si BTC ≤ −0,5 % / 1 h (long) ou > +0,10 % / 1 h (short), contre-tendance refusée si BTC ≥ 0,2 % contre / 1 h. Une décision prise sur des bougies 1 min / 5 min pouvait donc être bloquée par une tendance BTC d'une heure (ex. SOL : « rebond short refusé : BTC en hausse +0,12 % / 1 h »).
- **Changement (décision, paper et live ; `SCALP_BTC_SCALE1M_ON` = 1, 0 = ancien calcul sur 1 h)** : les vétos BTC se lisent sur les 5 dernières bougies 1 min de BTC, avec les mêmes seuils que le mouvement 1 min des actifs (`SCALP_CALM_DIR_PCT` 0,10 %, `SCALP_CALM_EFF` 0,6).
  - **Veto dur** : BTC « enclenché » contre le trade (net ≥ 0,10 % dans le sens opposé, efficacité ≥ 0,6) → short / long refusé (rebond, continuation, tendance soutenue) ; contre-tendance refusée.
  - **Veto doux** : BTC « naissant » contre (≥ 0,05 %) → shorts « prudents » comme avant (rebond sur zone testée 2 fois, taille réduite, flux vendeur ≥ 55 % exigé dans _take) ; les autres types de short passent ; contre-tendance refusée.
  - BTC plat ou dans le sens sur 1 min : plus aucun veto, même si BTC est haussier sur 1 h.
  - Pas de bougies 1 min BTC : ancien calcul 1 h.
- **Non modifié** : la **note** (±8 / −15 points selon BTC sur 1 h), qui peut encore faire passer un trade sous le seuil de note ; le filtre « short_btc_soft_max » reste pour l'ancienne échelle.
- **À surveiller** : nombre de refus « BTC … sur 1 min » dans le journal et trades de corrélation prise contre BTC.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.549 — Même base de temps entre la décision et ses vétos / sa note : le H1 n'est plus utilisé au Scalp
- **Principe validé** : un trade décidé sur 1 min / 5 min se valide avec des vétos et une note à la même échelle (1 min, 5 min, 15 min), pas avec le H1 (facteur 60). Au Scalp, H1 dans le sens = 45 % de gagnants, H1 neutre = 45 % (539 trades).
- **Changements (décision, paper et live ; actifs si `SCALP_MOVE1M_ON` = 1, sinon ancien comportement H1)** :
  1. **Continuation / tendance soutenue** : le veto « H1 contraire » est remplacé par un veto « mouvement 1 min contraire » (le prix va franchement contre le trade sur 5 min : net ≤ −0,05 % dans le sens du trade). S'applique aux suiveurs.
  2. **Note** : les points du H1 (+12 dans le sens, −15 / −25 contre, −8 pour un short contre H1 haussier) sont remplacés par ceux du mouvement 1 min (enclenché +12, naissant +6, contraire −15, cassure −25) ; les points BTC (+8 / −15) se lisent sur BTC 1 min (même échelle que les vétos de la v4.548).
  3. **Rebond / cassure** : la tendance qui décide « dans le sens / ambigu / contraire » est maintenant celle de 15 min (le H1 n'est plus consulté dans `move_gate`) ; 15 min contraire → sécurités de contre-tendance habituelles en plus du mouvement 1 min.
- **Non modifié (à savoir)** : (a) la règle v4.545 « marché dispersé + H1 déjà orienté → pas de rebond » (basée sur les données, 24 trades, 12 % de gagnants) ; (b) la zone H1 (support / résistance) et la pente 1 h de certains sous-modes de continuation ; (c) le H1 reste enregistré (CSV `tend_h1`, `tend_15m`) comme information. Pas de coupe-circuit BTC 1 h (décision : il bloquerait une base rapide par une base lente).
- **À surveiller** : nombre de refus « mouvement 1 min contraire » et de continuations prises quand le H1 est contraire (colonne `tend_h1`).
- Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.550 — Veto H1 rétabli pour la continuation (correction de la v4.549) + mouvement 1 min enregistré pour la continuation
- **Constat (nuit du 01/11 23:50 au 02/11 08:35, 44 trades Scalp paper, net −0,54 $)** : 7 continuations LONG prises contre un H1 baissier (veto H1 remplacé par le veto 1 min en v4.549) : 0 gagnant, −1,14 $ (2 stop loss, 5 durée max), pendant que le panier baissait de 0,8 à 1,4 % sur 2 h. Avant la v4.549, le veto H1 existait suite au constat du 30/10 : 14 longs de continuation contre un H1 baissier = 2 gagnants, −2,34 $.
- **Erreur de la v4.549** : la continuation se décide sur 2 à 3 heures de tendance (mouvement 2 h, tendance 3 h sur bougies 5 min) ; le H1 est donc sa base de temps naturelle. Le principe « même base de temps » s'applique au rebond (décidé sur le 5 min / 1 min), pas à la continuation.
- **Changement (décision, paper et live)** : continuation et tendance soutenue refusées quand le H1 est contraire (`SCALP_CONT_H1_BLOCK` = 1, comme avant la v4.549), EN PLUS du veto « mouvement 1 min contraire » de la v4.549. Rebonds, cassures, note (points du mouvement 1 min), vétos BTC 1 min : inchangés.
- **Observation** : le mouvement 1 min (`mvt1m_etat`, `mvt1m_net_pct`, `mvt1m_eff`) est maintenant enregistré aussi pour les continuations et les tendances soutenues (absent cette nuit).
- Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.551 — Ne pas courir après le mouvement du panier (tous les modes)
- **Demande** : validée par l'utilisateur après l'analyse de la nuit 01-02/11 (58 trades, −2,52 $).
- **Constat** (267 trades Scalp paper dont la variation du panier sur 2 h est connue) : quand le panier d'actifs avait déjà bougé d'au moins 0,3 % dans le sens du trade, 125 trades, −7,86 $ (−0,063 $ / trade) ; les 142 autres : +0,20 $. Échantillons distincts concordants : 209 trades du 19/10 au 01/11 (104 trades, −6,75 $) et nuit du 01-02/11 (21 trades, −1,11 $). Sorties des trades « panier déjà parti » : seuil de rentabilité 33 % (13 % pour les autres), stop loss 34 % (22 %), trailing TP 22 % (36 %) ; 41 des 59 sorties au seuil de rentabilité viennent de ce groupe.
- **Changement (décision, paper et live)** : tous les modes (rebond, cassure, continuation, tendance soutenue, oscillation), refus d'entrée si `panier_2h` dans le sens du trade ≥ `SCALP_BASKET_CHASE_PCT` (0,30 %). Interrupteur `SCALP_BASKET_CHASE_ON` (1 = actif, 0 = désactivé). Panier inconnu : pas de blocage. Journal : « le panier a déjà bougé de x % en 2 h dans le sens du trade… ».
- **Effet attendu** : environ la moitié des trades en moins (signaux déjà en fin de mouvement du groupe) ; les durées max ne sont pas concernées (elles se concentrent hors de ce groupe : 27 % contre 11 %).
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.552 — Suivi après sortie des SCALP DUREE MAX (observation seule)
- **Demande** : savoir si Scalp sort trop tôt (stop / durée) ou a raison de sortir. Le suivi existait pour les STOP LOSS (v4.538) ; il manquait la durée max (121 trades, −25,3 $, pic moyen 0,26 %).
- **Changement (observation seule, aucune règle de décision)** : les sorties « SCALP DUREE MAX » de Scalp reçoivent le même suivi que les stops : prix 1 h et 3 h après la sortie (retour à l'entrée, retour à l'objectif, résultat avec stop ×1,5 / ×2). Note `post_stop.kind = "durée"` ; ces trades sont comptés à part (`duree_post`) pour ne pas mélanger avec la carte des stops loss.
- **CSV** : mêmes colonnes `stop_h1_*`, `stop_h3_*` (…) ; la colonne `motif` distingue STOP LOSS et SCALP DUREE MAX. Les colonnes se remplissent 3 h après la sortie.
- Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md (api.py inchangé).

## v4.553 — Règle « panier déjà bougé » : exemptée quand la tendance est claire
- **Demande** : l'utilisateur veut que les restrictions donnent la priorité à la tendance (baisse de 16 h du 01-02/11).
- **Constat** (276 trades Scalp, panier connu) : panier déjà bougé ≥ 0,3 % dans le sens du trade, H1 pas dans le sens : 42 trades, −5,35 $ ; H1 dans le sens et panier directionnel (ER 4 h ≥ 0,10, médiane de l'échantillon) : 54 trades, 46 % gagnants, +0,59 $ ; meilleur profil : H1 dans le sens et panier pas encore bougé : 84 trades, +4,82 $. Seuil 0,10 tiré de la médiane : à valider.
- **Changement (décision, paper et live)** : la règle v4.551 ne refuse plus si H1 est dans le sens du trade ET efficacité du panier sur 4 h ≥ `SCALP_CHASE_TREND_ER` (0,10). Ces trades portent `chase_exempt` dans le signal ; identifiables dans le CSV par `panier_2h_pct` ≥ 0,3 dans le sens du trade. `SCALP_CHASE_TREND_EXEMPT_ON` = 0 revient à la v4.551.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.554 — Verrou du pic (tous les modes) + suiveur Forex plus large
- **Demande** : (1) bloquer 80 % du gain maximal dès que le trailing atteint 1 %, dans tous les modes ; (3) suiveur Forex plus large sur les trades qui marchent.
- **Constat** (605 trades Scalp) : 57 trades à pic ≥ 1 % ; 24 sortent sous 80 % de leur pic (sortie moyenne 1,05 % pour un pic moyen 1,33 %) ; verrou 80 % à partir de 1 % : +1,3 $ environ. Un verrou de 80 % dès 0,5 % est écarté (le recul de 20 % = bruit). Forex (200 trades) : trailing + stop dynamique = gains de 0,12 à 0,22 % ; suiveur à 40 % du pic trop serré. Effet du suiveur large NON mesuré (réglage demandé).
- **Changement (décision, paper seulement)** : `PEAK_LOCK_ON` 1, `PEAK_LOCK_ARM_PCT` 1,0, `PEAK_LOCK_FRACTION` 0,8, `PEAK_LOCK_LIVE` 0. Dès que le pic atteint 1 %, le stop garantit 80 % du pic (ne fait que monter / descendre). Sortie « VERROU DU PIC ». Modes couverts : Scalp, Auto zone, Swing Support, trades manuels paper (manual_trading) ; Forex, Accumulation, Spot-Accum (bot_engine). NON couverts : Swing FX, Trend following, Funding (blocs de sortie à part). Live inchangé tant que `PEAK_LOCK_LIVE` = 0.
- **Forex** : `FOREX_DSL_WIDE_FROM_PCT` 0,30 et `FOREX_DSL_TRAIL_FRACTION_WIDE` 0,60 : à partir d'un pic de 0,30 %, la distance du suiveur passe de 40 % à 60 % du pic (le verrou du pic reprend la main à 1 %).
- Fichiers : bot_engine.py, manual_trading.py, api.py, index.html, index_src.html, README.md.

## v4.555 — Live = image du paper (verrou du pic)
- **Demande** : construire le live en image du paper pour que le passage se fasse sans réglages supplémentaires à retenir.
- **Changement** : `PEAK_LOCK_LIVE` supprimé. Le verrou du pic (v4.554) s'applique de la même façon en paper et en live, avec les mêmes réglages `PEAK_LOCK_ON` / `PEAK_LOCK_ARM_PCT` / `PEAK_LOCK_FRACTION`. Exclus (décisions de l'utilisateur) : trades manuels libres (`free`) et trades importés (`hl_import`), qui gardent leurs propres stops. Le passage paper → live reste l'interrupteur de chaque mode (Scalp, Auto zone `ZONEORD_LIVE`, Trend following `TRENDF_LIVE_ON`, Funding) ; les règles de décision sont les mêmes dans les deux.
- Fichiers : bot_engine.py, manual_trading.py, api.py, index.html, index_src.html, README.md.

## v4.556 — Trou de données xyz:DXY / xyz:KRW (diagnostic + essai sur fenêtre courte)
- **Constat** (carte du 03/11) : Swing FX « historique 1d insuffisant (0 bougies) » pour xyz:DXY et xyz:KRW ; niveaux 5 min « pas encore chargés » (EMA200 de repli) ; EUR et JPY normaux. Aucun mapping de nom particulier dans le code. La cause (marché récent ? réponse vide de l'API ?) n'est pas établie : le serveur d'analyse ne peut pas joindre Hyperliquid.
- **Changement (diagnostic, aucune règle de décision)** : quand Hyperliquid renvoie une réponse vide pour > 40 bougies, nouvel essai sur 40 bougies seulement (marché récent : la fenêtre longue démarre avant la mise en service) ; une ligne de log `[MTF] … réponse Hyperliquid vide …` ; le diagnostic précise la cause (« réponse Hyperliquid vide » ou « première bougie le jj/mm/aaaa (marché récent ?) »). Swing FX exige toujours 60 bougies Daily : si le marché a moins de 60 jours, le diagnostic le dira.
- Fichiers : bot_engine.py, index.html, index_src.html, README.md.

## v4.557 — Verrou du pic propre au Scalp (80 % dès 0,5 %)
- **Demande** : sur Scalp, armer le verrou plus tôt en gardant 80 % du gain garanti (12 sorties « seuil de rentabilité » cette nuit : pic moyen 0,52 %, sortie moyenne +0,07 %).
- **Constat** : suppression du seuil de rentabilité écartée (53 sorties suivies : −2,02 $ sans lui). Verrou 80 % dès 0,5 % : +17,3 $ au maximum sur 637 trades, plafond très optimiste (recul de 20 % sur 0,5 % = 0,1 %, du bruit pour le Scalp) ; effet réel inconnu.
- **Changement (réglages, aucune règle modifiée par défaut)** : `SCALP_PEAK_LOCK_ARM_PCT` et `SCALP_PEAK_LOCK_FRACTION` (0 = mêmes réglages que le verrou général : 1 % / 80 %). Ils ne s'appliquent qu'aux trades Scalp ; Auto zone, Swing Support, Forex gardent le réglage général. À régler : 0,5 et 0,8.
- Fichiers : bot_engine.py, manual_trading.py, api.py, index.html, index_src.html, README.md.

## v4.558 — Swing FX : déclencheur « rejet confirmé sur bougies 1 min » dans la zone Daily
- **Demande** : exploiter les rebonds sur zone avec les contraintes existantes (Daily/H4 alignés), les bougies 1 min donnant le moment d'entrée. Cas PAXG du 03/11 : Daily et H4 baissiers, prix dans la résistance Daily 3960–4021 (7 contacts), aucun signal H4 récent ; même avec un signal, le stop au-delà de toute la zone (4021 + marge) donnait un gain/risque ≈ 1,2 < 2,0 → refus.
- **Changement (décision, paper)** : si aucune figure H4 n'est présente, second déclencheur : rejet confirmé sur les 15 dernières bougies 1 clôturées (`mtf_analysis.rejection_1m`) : la bougie qui va le plus loin dans la zone a une longue mèche côté zone (≥ 50 % de sa hauteur, hauteur ≥ 0,8 × la hauteur habituelle) ET la dernière bougie clôture sous son bas (short) / au-dessus de son haut (long). Âge ≤ 300 s. Le stop se place au-delà du REJET (extrême + 0,15 ATR Daily), pas au-delà de toute la zone ; objectif inchangé (zone opposée). Tous les filtres restent : tendance Daily, prix dans la zone, flux, stop min/max (0,25–2 %), gain/risque min 2,0 brut et 1,2 net de frais, spread. Réglages : `FXSWING_1M_ON` (1), `FXSWING_1M_MAX_AGE_SEC` (300), `FXSWING_1M_BARS` (15). `FXSWING_1M_ON` = 0 revient à la v4.557.
- Exemple de test (PAXG, résistance 3960–4021, rejet à 3972, prix 3962) : stop 3979,5 (0,44 %), gain/risque 3,9. Cassures : non traitées dans cette version (étape suivante).
- Fichiers : mtf_analysis.py, bot_engine.py, api.py, index.html, index_src.html, README.md. Aucune validation sur historique possible (pas de bougies 1 min) : à juger en paper.

## v4.559 — Scalp : veto H4 (continuation et cassure)
- **Demande** : les entrées Scalp contredisent la tendance et perdent par groupes. Constat (111 trades paper avec H4 connu, 01/11 → 03/11) : contre H4 = 25 trades, 20 % de gagnants, −3,74 $ ; H4 dans le sens = 71 trades, 39 %, −1,13 $ ; H4 neutre = 15 trades, −0,94 $. Contre le Daily : 4 trades seulement (Daily presque toujours neutre) → pas de conclusion. Groupes d'entrées (< 90 s) de 2 et 3 : −3,13 $ et −1,43 $ (non traités ici).
- **Changement (décision, paper et live)** : `SCALP_H4_VETO_ON` (1). Une continuation ou une cassure n'est pas ouverte contre un H4 clair de l'actif (long en H4 baissier, short en H4 haussier), sauf si le Daily est dans le sens du trade. Rebond exempté (contre-tendance par nature). H4 neutre ou inconnu : pas de blocage. Ne ferme aucun trade ouvert. Refus affiché dans le journal : « H4 … à contre-sens du trade ». `SCALP_H4_VETO_ON` = 0 revient à la v4.558.
- Non mesuré par mode : les 25 trades contre H4 ne sont pas séparés rebond / cassure / continuation. À juger sur ~60 trades.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.560 — Scalp : sortie « sans progrès »
- **Demande** : s'occuper des pertes que le veto H4 ne voit pas. Constat (650 trades paper, veto H4 retiré) : brut +8 $, frais 39 $ ; trailing/TP +81 $ ; stop loss 200 trades −85 $ dont 93 jamais partis du bon côté (pic < 0,1 %, −38,6 $) et 56 restés > 10 min dans le mauvais sens (−25 $) ; durée max 130 trades −26 $ dont 52 à pic < 0,2 % (−15,8 $).
- **Changement (décision, paper et live)** : `SCALP_NOPROG_ON` (1), `SCALP_NOPROG_MIN` (15), `SCALP_NOPROG_PEAK_PCT` (0,10). Un trade Scalp ouvert depuis ≥ 15 min dont le gain maximal est resté < 0,10 % (en prix) est fermé. Sortie « SCALP SANS PROGRES » (avant le stop et la durée max). Ne s'applique pas aux trades dont le suiveur est enclenché. `SCALP_NOPROG_ON` = 0 revient à la v4.559.
- **Risque non mesurable sur l'historique** : les trades sous 0,10 % à 15 min qui seraient devenus gagnants sont coupés aussi. Mesure : chaque sortie sans progrès est suivie après coup (que fait le prix ensuite ?), bloc « SORTIE SANS PROGRÈS DU SCALP » dans les statistiques Scalp (`noprog_post`, ~1 h de latence).
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.561 — Virgule décimale, pic en % de la marge, Auto zone à 65 %
- **Demande** : (1) `SCALP_PEAK_LOCK_FRACTION` n'acceptait que « 0.65 » (point), pas « 0,65 » ; (2) confusion entre le pic (en % du prix) et le pnl (en % de la marge) sur les cartes ; (3) l'Auto zone garantissait 50 % du pic seulement (HYPE 0,26 $ pour un pic de 0,53 $ ; ETH 0,15 $).
- **Changements** : (1) la virgule est acceptée dans les champs de la carte Scalp (écran et serveur) ; (2) cartes de trades ouverts : « pic +2,64 % (prix +0,88 %) » (pic ramené à la marge, prix entre parenthèses) ; ligne Scalp de l'historique idem ; « SL dynamique … dès +0,675 % de prix » ; (3) `ZONEORD_LOCK_FRACTION` par défaut 0,65 (une valeur déjà enregistrée dans les réglages reste inchangée). Aucune règle de décision du Scalp touchée.
- À poser côté réglages : `SCALP_PEAK_LOCK_FRACTION` 0.65 (décision de l'utilisateur, 18:44).
- Fichiers : api.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.562 — Ordres programmés : pas de numéro de trade avant l'ouverture + onglet « Ordres non exécutés »
- **Demande** : les ordres programmés annulés ou expirés ne sont pas des trades qui ont tourné : pas de numéro de trade avant l'ouverture effective ; les ranger dans un onglet à part, archivage conservé.
- **Changements** : (1) un ordre programmé reçoit un repère provisoire négatif (affiché « P3 », « ordre P3 ») qui ne consomme pas la suite des numéros de trades ; le vrai numéro (#1082…) n'est donné qu'au déclenchement (`db.manual_promote`, `ManualTrading._promote`) ; (2) onglet « ORDRES NON EXÉCUTÉS » dans l'historique : ordres annulés, expirés ou en échec, dont les anciens (par statut), retirés des onglets LIVE / PAPER / SCALP ; (3) historique trié par date. Les compteurs annulés / expirés de l'Auto zone sont inchangés. Aucune règle de décision touchée.
- **Limites** : les ordres déjà programmés avant la mise à jour gardent leur numéro actuel ; les numéros déjà consommés par d'anciens ordres annulés ne sont pas récupérés ; un ordre déclenché puis en échec à l'ouverture a déjà son numéro de trade.
- Fichiers : db.py, manual_trading.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.563 — Sortie « sans progrès » guidée par les signaux
- **Demande** : lier la sortie sans progrès au mouvement des signaux (tendance, flux, bougies 1 min, inversion ou pas), sans qu'une inversion ponctuelle sur 1 min contredise une tendance H4 qui porte le trade.
- **Changement (décision, paper et live)** : à `SCALP_NOPROG_MIN` (15 min), pour un trade au pic < `SCALP_NOPROG_PEAK_PCT` (0,10 %), `SCALP_NOPROG_SMART_ON` (1) lit trois signaux dans le sens / neutre / contre, toutes les minutes, sur le cache (aucun appel réseau) : bougies 1 min (`move_state`), flux 10 min (`SCALP_NOPROG_FLOW_PCT` 55 %), tendance H1 ; le H4 fixe l'exigence. Fermeture si : (a) inversion durable = ≥ 2 signaux contre (3 si le H4 est dans le sens du trade) pendant `SCALP_NOPROG_REV_MIN` (3) contrôles d'affilée ; (b) aucune reprise (ni bougies 1 min ni flux dans le sens) et trade non porté par le H4 : fermeture immédiate ; (c) `SCALP_NOPROG_MAX_MIN` (30) min : fermeture dans tous les cas. Sinon on attend, stop loss en place. Une seule lecture 1 min ne ferme jamais. La procédure s'arrête dès que le pic atteint 0,10 % (gestion habituelle). `SCALP_NOPROG_SMART_ON` = 0 revient à la v4.560 (fermeture à 15 min).
- La sortie écrit les lectures dans le journal (« bougies 1 min …, flux …, H1 …, H4 … ») et dans le trade (`noprog`). Effet non mesurable sur l'historique (pas de bougies 1 min passées) : à juger en paper avec le bloc de suivi après sortie.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.564 — Limite de requêtes Hyperliquid (429) : univers Scalp
- **Constat** : « Dernière erreur technique : univers : ClientError 429 » (03/11 19:42 locale). Le classement des actifs par volume est relu toutes les 30 min, mais chaque enregistrement d'un réglage Scalp le forçait aussitôt ; en cas d'échec, le bot remplaçait le bon classement par la liste sans classement. Sans effet sur les trades (liste de repli, réessai 5 min).
- **Changements** : (1) un enregistrement de réglage ne force la relecture que si `SCALP_TOP_N` a changé ; (2) en cas d'erreur, le dernier classement qui a fonctionné est conservé (réessai dans 5 min). Aucune règle de trading touchée.
- Si le 429 revient sans enregistrement de réglage : autre source (bougies) à chercher.
- Fichiers : scalp_crypto.py, api.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.565 — Tendance soutenue : même lecture à l'entrée et à la sortie
- **Constat** (CSV du 03/11, 5 sorties « TRAILING TAKE PROFIT » en moins de 2 min : ETH, SOL, LINK, JUP à 23:05, TIA à 20:30) : stop final = pic − 0,15 % (`SCALP_SUS_END_TRAIL_PCT`) → la tendance soutenue a été déclarée finie par `sus_end` dans la minute suivant l'entrée ; le suiveur serré sort le trade à ≈ 0 brut (−0,06 $ de frais). À l'entrée, `sus_measure` accepte un repli jusqu'à 1,5 ATR 5 min sur 30 min mais ne regarde pas le recul ACTUEL depuis l'extrême récent, que `sus_end` mesure à la sortie.
- **Changement (décision, paper et live)** : à l'entrée d'une continuation soutenue, contrôle avec la mesure de `sus_end` (nouvelle fonction `sus_back`, recul en ATR 5 min depuis l'extrême des 6 dernières bougies, seuil de fin max(1,2 ; bruit × 1,3)) : refus si la tendance est déjà finie, ou si le recul atteint `SCALP_SUS_ENTRY_END_FRAC` (0,5) du seuil de fin. `SCALP_SUS_ENTRY_END_ON` = 0 revient à la v4.564. Ne touche pas les autres continuations ni les rebonds.
- Test : même série acceptée sans le contrôle (recul 0,9 ATR), refusée avec (72 % du seuil de fin). Effet sur historique non mesurable (pas de bougies 5 min passées) : à juger en paper (sorties de moins de 3 min, refus « en train de finir » dans le journal).
- Non fait (observé) : filtre d'amplitude 5 min > 0,5 % (38 trades, −4,67 $ sur 169 mesurés) ; suiveur de fin de tendance armé seulement si pic ≥ 0,25 %.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.566 — Scalp : « cassure de base 1 min » (remplace l'ancienne cassure 5 min)
- **Demande** : reconstruire la cassure (coupée car elle perdait) avec des critères qui qualifient une vraie cassure, lue sur les bougies 1 min, contexte H1/H4 inchangé.
- **Changement (décision, paper et live)** : les deux anciens blocs « CASSURE confirmée » 5 min sont remplacés par `base_break` (bougies 1 min fermées). Cinq critères, tous requis : (1) zone H1 testée ≥ `SCALP_BASE_MIN_TOUCHES` (2) fois ; (2) **base** : les `SCALP_BASE_BARS` (6) bougies avant la cassure restent collées au niveau (clôtures à moins de `SCALP_BASE_PRESS_ATR` 0,6 ATR 5 min en dessous) et dans une amplitude ≤ `SCALP_BASE_RANGE_ATR` (1,0 ATR) ; (3) **clôtures** des 2 dernières bougies au-delà du niveau d'au moins `SCALP_BASE_MARGIN_ATR` (0,15 ATR) — une mèche seule ne compte pas ; (4) **volume** des 2 bougies ≥ `SCALP_BASE_VOL_MULT` (1,5) × médiane de la base ; (5) **pas de poursuite** : dernière clôture à ≤ `SCALP_BASE_MAX_EXT_ATR` (0,6 ATR) du niveau. Stop = bord opposé de la base ∓ `SCALP_BASE_SL_PAD_ATR` (0,1 ATR), borné `SCALP_BASE_SL_MIN_PCT` 0,25 % – `SCALP_BASE_SL_MAX_PCT` 1,0 % ; le veto H4 (v4.559), le macro et les autres portes s'appliquent comme avant ; l'ascenseur ne refuse plus une cassure de base.
- **Arrêt automatique** : si les `SCALP_BASE_KILL_N` (20) dernières cassures de base fermées totalisent ≤ −`SCALP_BASE_KILL_USD` (3 $) net de frais, la cassure de base est refusée pendant `SCALP_BASE_KILL_HOURS` (12 h) après la dernière sortie (motif écrit au journal).
- `SCALP_BREAKOUT_ON` passe à 1 par défaut (0 = cassure coupée). Une valeur déjà enregistrée dans les réglages reste prioritaire : **vérifier qu'elle est à 1**.
- CSV : colonnes base_touches, base_barres, base_amplitude_atr, base_marge_atr, base_volume, base_ext_atr, base_stop_pct.
- Tests : base valide long/short acceptée ; refus mèche seule, pas de base, volume faible, trop loin, zone testée 1 fois, base trop large ; arrêt automatique. **Non testable sur historique** (pas de bougies 1 min passées) : à juger en paper sur 60–80 trades.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.567 — Scalp : taille unique de 30 $ de marge, compound à partir de 500 $
- **Demande** : même taille pour tous les trades, minimum 30 $ hors levier, en paper comme en live ; compound seulement si le capital est ≥ 500 $.
- **Audit avant changement** : les tailles variaient de 38 à 67 $ de notionnel (continuation = 20 % du capital, rebond composé, réductions corrélé / short prudent BTC / bêta / vigilance / ascenseur) ; le compound n'existait que sur la continuation (à chaque trade) et sur les emplacements (une fois par jour).
- **Changement (décision, paper et live)** : `SCALP_FIXED_ON` (1) : chaque trade Scalp ouvre avec `SCALP_FIXED_MARGIN_USD` (30 $) de marge, soit 90 $ de notionnel à levier 3. Compound seulement si le capital courant ≥ `SCALP_COMPOUND_FROM_USD` (500 $) : marge = max(30 $, capital × `SCALP_FIXED_COMPOUND_PCT` 6 %) (6 % de 500 $ = 30 $, pas de saut). Toutes les réductions de taille sont désactivées ; l'élargissement du stop (ascenseur, oscillation) reste mais la taille ne baisse plus, la perte maximale en $ augmente donc avec le stop. Refus si la marge déjà engagée + 30 $ dépasse le capital courant (capital 64 $ : 2 trades à la fois). `SCALP_FIXED_ON` = 0 revient aux anciennes tailles. L'Auto zone garde sa taille propre.
- **Attention** : perte par stop environ 2,5 × plus forte qu'avec la marge de continuation d'avant (≈ 12,7 $). Le live utilise la même règle ; marge libre Hyperliquid vérifiée comme avant.
- Test : marges 30 $ jusqu'à 499,9 $, 36 $ à 600 $, 60 $ à 1 000 $, retour aux anciennes tailles à 0.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.568 — Principes de trading : contexte, confluence, risque/rendement net + backtest + test fantôme
- **Demande** : juger et construire le Scalp sur des principes indépendants de l'historique du bot (contexte majeur, confluence de 3 critères, ratio risque/rendement ≥ 1 net de frais, pause macro 30 min, rigueur statistique), et les valider AVANT de les activer.
- **Nouveau module `confluence.py`** (fonctions pures, mêmes règles pour le trading, le backtest et le test fantôme) :
  1. **Contexte** : H1 dans le sens (EMA20 > EMA50 et clôture > EMA50 pour un achat ; `SCALP_CONF_H1_STRICT` 0 = « pas contre »), H4 (bougies de 4 h reconstruites du H1) pas contre.
  2. **Confluence** (`SCALP_CONF_MIN` = 3 sur 3) : (a) zone H1 testée ≥ `SCALP_CONF_MIN_TOUCHES` (2) fois ou chiffre rond ; (b) RSI 5 min extrême (≤ `SCALP_CONF_RSI` 35 / ≥ 65) ou réaction sur l'EMA 20 / 50 en 15 min ; (c) bougie de retournement 5 min (marteau, avalement, étoiles) sur la zone, ou cassure de base 1 min.
  3. **Risque/rendement** : stop au-delà de la zone (+ `SCALP_CONF_PAD_ATR`), borné `SCALP_CONF_SL_MIN_PCT` 0,4 – `SCALP_CONF_SL_MAX_PCT` 1,0 % ; objectif = `SCALP_CONF_RR` (1,5) × stop ; refus si (objectif − frais) / (stop + frais) < `SCALP_CONF_MIN_RR` (1,0), frais `SCALP_CONF_FEE_PCT` 0,10 % ; refus s'il n'y a pas de place avant la zone opposée.
- **Porte dans le trading** : `SCALP_CONF_ON` = **0 par défaut** (aucun changement de décision tant que tu ne l'actives pas). À 1 : tout signal Scalp (rebond, continuation, cassure) doit respecter les principes ; stop et objectif du trade sont ceux des principes ; la raison du refus s'écrit au journal.
- **Backtest** (écran Scalp, bloc « Principes de trading ») : télécharge 5 min / 15 min / 1 h de Hyperliquid (1 à 16 jours, jusqu'à 80 actifs, 0,3 s entre requêtes), rejoue les principes bougie par bougie sans regarder le futur (entrée à l'ouverture suivante, stop d'abord si stop et objectif sont touchés dans la même bougie, frais déduits, une position par actif, sortie à 24 bougies). Compare **principes complets / 2 critères sur 3 / contexte seul / témoin sans filtre**, avec taux de réussite + intervalle de confiance 95 %, gain/perte moyens, ratio, espérance, net en $ (30 $ × 3) et espérance par moitié de période. Résultat gardé dans `conf_backtest.json` (à côté de la base).
- **Test fantôme** (`SCALP_CONF_SHADOW_ON` = 1) : à chaque bougie 5 min clôturée, le bot évalue les principes sur tous les actifs analysés ; un signal valide ouvre un trade VIRTUEL (aucun ordre, aucune marge) suivi jusqu'au stop / objectif / durée ; résultats cumulés dans le même bloc (`conf_shadow.json`).
- **Pause macro** : défaut `SCALP_MACRO_BEFORE_MIN` / `AFTER_MIN` passés à 30 / 30 (une valeur déjà enregistrée dans tes réglages reste prioritaire : à vérifier).
- **Rien n'est supprimé** : les mécanismes antérieurs gardent leur interrupteur. Nettoyage des filtres redondants seulement après un résultat probant.
- **Critère de validation proposé (à fixer avant de lire les résultats)** : ≥ 100 trades, espérance nette positive, borne basse de la réussite au-dessus du seuil d'équilibre, espérance positive dans les DEUX moitiés de période, et nettement meilleure que le témoin.
- **Limites** : l'historique 5 min couvre ~16 jours, donc un seul régime de marché ; la partie 1 min (cassure de base) ne se juge que par le test fantôme ; le backtest suppose l'exécution à l'ouverture suivante sans glissement.
- Tests : `test568` (contexte, 3 critères, ratio net, place avant zone opposée, stop d'abord, statistiques, absence de gain sur données aléatoires = témoin ≈ −frais) et `test568b` (porte, test fantôme jusqu'à la clôture, rapport).
- Fichiers : confluence.py (nouveau), scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.569 — Zones étroites, stop au-delà du bruit
- **Constat** : (1) le relevé des actifs montre des « zones Daily » de 5 à 50 % du prix (JUP ~49 %, SEI ~30 %, RENDER ~29 %, SOL ~19 %) et le trade flash PENDLE #1175 (10:57 → 11:00, −0,67 $ net) est entré sur une résistance de 3,3 % de large avec un stop de 0,50 % ≈ 1 ATR 5 min (volatilité de PENDLE ≈ 0,5 %) ; le stop a aussi été dépassé de 0,156 % à l'exécution (−0,656 % de prix au lieu de −0,50 %). (2) Mon module de confluence v4.568 n'avait pas de largeur maximale de zone : le critère « zone clé » était trop facile à remplir sur une zone large.
- **Changements** :
  1. **Principes** : largeur maximale d'une zone H1 `SCALP_CONF_ZONE_MAX_ATR` (1,5 ATR 1 h, via `max_width_atr` de `find_zones`) ; une zone plus large est découpée en zones étroites. Stop des principes au moins `SCALP_CONF_SL_ATR` (1,5) × ATR 5 min ; refus « bruit trop grand » si le stop nécessaire dépasse `SCALP_CONF_SL_MAX_PCT` (1,0 %).
  2. **Chemin actuel, rebonds seulement (décision, paper et live)** : `SCALP_SL_ATR_FLOOR_ON` (1) relève le stop à `SCALP_SL_ATR_FLOOR` (1,5) × ATR 5 min (plafond `SCALP_SL_FLOOR_MAX_PCT` 1,5 %), jamais plus serré qu'avant ; l'objectif suit : max(`SCALP_TP_PCT`, `SCALP_SL_FLOOR_RR` 1,5 × stop). Taille inchangée (30 $ de marge) : la perte maximale augmente avec le stop. PENDLE (ATR 5 min 0,5 %) : stop 0,50 → 0,75 %, objectif 0,6 → 1,13 %. Continuation (déjà en ATR), oscillation et cassure de base (stop = bord de la base) ne sont pas touchées. `SCALP_SL_ATR_FLOOR_ON` = 0 revient à la v4.568.
- **Flux absent** (AAVE, EIGEN, GMX, OP, PENDLE, POL, RENDER, SEI, WIF : « pas encore de données ») : le code traite l'absence de flux comme « pas de blocage » (donnée manquante ≠ flux contraire) ; les entrées sur ces actifs reposent donc sur les autres signaux. Non modifié.
- **Backtest** : relancer après dépôt (les zones étroites changent le critère « zone clé »). `SCALP_CONF_ON` reste à 0.
- Tests : `test569` (plancher de stop, largeur des zones ≤ 1,5 ATR, refus « bruit trop grand »).
- Fichiers : confluence.py, scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.570 — Contexte unique H1 / H4 (fin de la cacophonie des tendances)
- **Constat** : cinq lectures de « tendance » coexistaient (fond = prix / EMA200 1 h ≈ 8 jours, étiqueté « 1h » ; Daily du Top-down ; veto H4 v4.559 ; blocs H1 de la continuation = `mtf.trend` 1 h ; EMA20/50 H1 du module de confluence v4.568), avec des définitions, des seuils et des replis silencieux différents ; elles pouvaient se contredire sur un même actif.
- **Changement (décision, paper et live)** : `SCALP_CTX_ON` (1) : **une seule fonction** `confluence.market_context` (H1, H4 reconstruit du H1, M5 pour information) avec la même définition partout (haussier = EMA20 > EMA50 et clôture > EMA50 ; baissier = inverse ; sinon neutre). Une porte unique dans `_take`, pour TOUS les modes (rebond, continuation, cassure), remplace le veto H4 (v4.559) et les trois blocs « H1 contraire » de la continuation / soutenue. `SCALP_CTX_MODE` 1 : aucun des deux horizons contre ET au moins un dans le sens ; 2 : H1 et H4 tous deux dans le sens. **Donnée manquante (série trop courte) ou périmée (dernière bougie H1 clôturée il y a plus de 3 h) = refus**, plus de valeur devinée. Les mêmes fonctions servent à la confluence (v4.568) et au backtest. `SCALP_CTX_ON` = 0 revient à la v4.569.
- **Écran** : « fond 8 j (EMA200 1 h) » remplace « fond … (1h) » ; nouvelle ligne « 🎯 Contexte scalp (H4 / H1 / M5) → achats / ventes autorisés ou aucun sens » par actif ; ligne de santé « actifs lisibles x/y · flux disponible x/y » (survol = actifs concernés) dans le Scalp.
- **Non modifié** : la pente 1 h, le R² et le mouvement 1 min de la continuation restent des conditions du SIGNAL de continuation (pas du contexte) ; le flux absent reste « pas de blocage » (affiché mais pas corrigé : un ré-abonnement automatique risque de dupliquer les transactions du tampon, à étudier à part) ; `SCALP_CONF_H1_STRICT` n'est plus utilisé (remplacé par `SCALP_CTX_MODE`).
- Tests : `test570` (définition, modes 1 et 2, un seul horizon contre, donnée courte, donnée périmée, série vide), `test570b` (porte dans le Scalp, ligne de contexte, santé) ; `test550` couvre désormais le chemin historique avec `ctx_on` = 0.
- Fichiers : confluence.py, scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.571 — Flux de transactions : réabonnement après reconnexion + âge du dernier flux
- **Constat** (relevé du 04/11 13:50) : 14 actifs sans flux (APT, BNB, EIGEN, GMX, JUP, OP, PENDLE, POL, RENDER, SEI, SUSHI, TAO, TIA, WIF), alors que les actifs suivis par le bot principal en avaient. Cause trouvée dans `ScalpCrypto._flow` : un actif hors de la liste du bot (abonné « à la volée ») n'était abonné que si son tampon de transactions n'existait pas encore ; après une reconnexion du WebSocket, `_subscribe_trade_flow` ne réabonne que les actifs de `SYMBOLS`, l'ancien tampon existait toujours et l'actif n'était JAMAIS réabonné. Non vérifiable sur les journaux du bot (hypothèse cohérente avec la liste des actifs concernés).
- **Changement (aucune règle de décision)** : l'abonnement se décide sur la génération du WebSocket (horodatage de souscription + objet `info`) : à chaque (re)connexion, les actifs du Scalp hors `SYMBOLS` sont réabonnés une fois ; les actifs de `SYMBOLS` ne sont jamais abonnés par le Scalp (pas de double abonnement). L'écran affiche, au survol de la ligne « Contexte H4/H1 », pour chaque actif sans flux : « aucune transaction reçue » ou « dernière il y a N min », et le nombre d'abonnements lancés par le Scalp.
- **Réserve** : si le SDK conserve l'ancien abonnement après reconnexion, une transaction peut être comptée deux fois dans le tampon ; le ratio acheteurs / vendeurs n'en est pas changé, seul le nombre de transactions est doublé.
- **Capital paper** : `SCALP_CAPITAL_USD` 300 + le bouton « Repartir du capital de départ à partir de maintenant » (sinon le capital courant = 300 $ + tout le net déjà clôturé, soit 259,45 $ aujourd'hui).
- Tests : `test571` (actif suivi jamais abonné par le Scalp, actif à la volée abonné une fois, réabonné après reconnexion avec ancien tampon, réabonné si l'objet `info` change, résumé de santé).
- Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.572 — TTP complémentaire à +0,6 % du prix (Scalp)
- **Constat** (AAVE #1177) : pic à +0,65 % du prix, retombé à +0,22 % ; entre le stop de rentabilité (+0,10 %) et l'objectif (+1,09 %), rien ne suivait le prix. Le verrou du pic (armement 1,0 %) et le suiveur après objectif n'étaient pas atteints.
- **Changement** : chaque nouveau trade Scalp (paper et live, mêmes règles) reçoit un trailing take profit complémentaire : armé quand le pic atteint `SCALP_TTP_ARM_PCT` (0,6 % du prix), sortie « TRAILING TAKE PROFIT » si le prix recule de `SCALP_TTP_TRAIL_PCT` (0,2 %) depuis le pic. Soit une sortie garantie vers +0,40 % au plus bas dès que le pic atteint +0,6 %. `SCALP_TTP_ON` = 0 désactive ; armement 0 = désactivé.
- Il s'ajoute à l'existant (stop de rentabilité, verrou du pic, objectif + laisser courir, durée max). Il utilise le TTP déjà présent dans le gestionnaire d'ordres (aucun nouveau mécanisme de sortie).
- Ne s'applique qu'aux trades ouverts après le déploiement (les positions déjà ouvertes gardent leurs paramètres).
- Réserve : en coupant à −0,2 % du pic, le TTP peut sortir avant un mouvement plus long ; le suiveur de 0,2 % après objectif reste inchangé. À juger sur ~60 trades : gain moyen des trades à pic ≥ 0,6 %, et part de ceux qui auraient continué.
- Tests : test572 (réglages, paramètres transmis, bornes) + régression.
- Fichiers : scalp_crypto.py, bot_engine.py, api.py, index.html, index_src.html, README.md.

## v4.573 — Backtest : nouvel essai, seconde passe, erreurs lisibles
- **Constat** : au backtest du 04/11, 11 actifs sur 29 ont été ignorés avec « ClientError » sans autre information (probablement limite de requêtes d'Hyperliquid, non confirmé : le message n'était pas conservé). Résultat calculé sur 18 actifs seulement.
- **Changement (aucune règle de trading)** : chaque requête de bougies est retentée jusqu'à 4 fois (attente 2, 4, 8 s) ; pause entre requêtes portée de 0,3 à 0,6 s ; les actifs en échec sont repris dans une seconde passe après 30 s de repos ; les actifs encore en erreur affichent le code et le message réels (ex. « ClientError 429 : … ») ou « données insuffisantes ». Le résumé indique combien d'actifs ont été repris. Durée par défaut du backtest : 16 jours (maximum).
- Durée attendue : environ 1 à 3 minutes pour 29 actifs. Les règles testées sont celles de la v4.570 (aucun seuil modifié).
- Tests : test573 (2 échecs puis succès, échec total avec code, seconde passe, actif toujours en erreur) + régression.
- Fichiers : confluence.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.574 — Dernière modification avant le gel de mesure (observation seule)
- **Aucune règle d'entrée, de sortie, de taille ou de filtre n'est modifiée.**
- **CSV** : deux colonnes en fin de ligne, `flux_dispo` (oui / non : le flux de transactions de l'actif était-il disponible à l'ouverture) et `flux_age_s` (âge en secondes de la dernière transaction reçue). Permet de séparer, dans l'analyse, les trades pris avec et sans flux. Trades antérieurs : colonnes vides.
- **Suivi après sortie** : il existait déjà pour les STOP LOSS, « durée max » et « sans progrès » du Scalp. Ajout des **DUREE MAXIMALE des ordres sur zone** (même suivi à 1 h et 3 h, colonnes `stop_h1_*` / `stop_h3_*` du CSV, type « durée »). Sert à savoir si prolonger la durée aurait aidé.
- **Protocole de gel (15 jours)** : à partir du déploiement de cette version, aucune modification des règles du Scalp ni des ordres sur zone (réglages `SCALP_*` / `ZONEORD_*` compris). Exceptions : correction de bug ou risque de sécurité, notée ici avec sa date, et la période de mesure est alors coupée à cette date. L'affichage seul n'est pas une modification.
- **Critères fixés avant mesure** : au moins 100 trades par mode (sinon on prolonge, on ne conclut pas) ; espérance nette positive dans les deux moitiés ; borne basse de l'intervalle de réussite au-dessus du seuil de rentabilité ; ventilation par motif de sortie et par type de signal. Le backtest n'est pas un critère (sorties simplifiées, règles différentes).
- **À régler avant le début du gel** : flux ≈ 30/30 après la v4.571 ; `SCALP_CAPITAL_USD` = 300 puis « Repartir du capital de départ » ; fixer `SCALP_SUS_ON` et la pause macro 30/30 ; `SCALP_CONF_ON` = 0.
- Tests : test574 (durée maximale des ordres sur zone suivie, stop dynamique non suivi, colonnes du CSV) + régression.
- Fichiers : scalp_crypto.py, bot_engine.py, index.html, index_src.html, README.md.

## GEL DE MESURE — départ le 04/11/2026 à 20:41 (UTC−4), fin le 19/11/2026 à 20:41
- Version figée : **v4.574** (aucune modification des règles du Scalp ni des ordres sur zone pendant la période).
- Réglages confirmés par l'utilisateur au départ : `SCALP_CAPITAL_USD` 300 avec remise à zéro du capital ; `SCALP_SUS_ON` = 0 (pas de continuation « soutenue ») ; `SCALP_CONF_ON` = 0 ; pause macro 30/30 (valeurs confirmées à jour) ; flux Scalp 29/29 et contexte 29/29 lisibles à 20:34.
- Photo de départ (avant gel, mélange de versions, à ne pas compter) : 46 trades clôturés le 04/11, 13 gagnants au net, −8,84 $ net (Scalp −4,93 $, ordres sur zone −3,91 $) ; total paper 532 trades, −11,87 $ brut, −42,57 $ net.
- Critères (rappel) : au moins 100 trades par mode (sinon prolonger) ; espérance nette positive dans les deux moitiés ; borne basse de l'intervalle de réussite au-dessus du seuil de rentabilité ; ventilation par motif de sortie et par type.
- À reprendre APRÈS la mesure, jamais pendant : Scalp à 120 min, stop resserré sur vrai changement de tendance, invalidation de zone, stop de rentabilité sur ordres sur zone, enregistrement des signaux inverses, colonne « espérance avant frais » du backtest.
- Exceptions : correction de bug ou de sécurité uniquement, notée ici avec sa date ; la mesure est alors coupée à cette date.

## v4.575 — Correction d'AFFICHAGE du diagnostic de flux (aucune règle modifiée, compatible avec le gel)
- **Problème** : la ligne « Flux transactions : pas encore de données » du diagnostic par actif lisait le suivi du bot principal (`trade_flow_history`, échelle −1 à +1), pas le flux utilisé par le Scalp. 16 actifs sur 29 affichaient « pas encore de données » alors que le flux du Scalp était disponible sur 29/29 (carte Scalp, 20:34). L'affichage induisait en erreur.
- **Correction** : la ligne est renommée « Flux transactions (bot principal, −1 à +1) : … pas de suivi pour cet actif » et une seconde ligne « 🌊 Flux lu par le Scalp » donne ce que le Scalp lit réellement : part d'achats sur 10 min et nombre de transactions, ou « aucune transaction reçue » / « dernière il y a N min ». Lecture seule : aucun abonnement, aucune décision modifiée.
- Gel : seul l'affichage change. Le redéploiement redémarre le bot (comme tout déploiement), sans effet sur les règles, les réglages ni le capital. Pas de coupure de la période de mesure.
- Tests : test575 (part d'achats, tampon périmé, actif sans transaction, aucun abonnement) + régression.
- Fichiers : scalp_crypto.py, api.py, bot_engine.py, index.html, index_src.html, README.md.

## v4.576 — Affichage : mesures après sortie dans un onglet (aucune règle modifiée, compatible avec le gel)
- Historique manuel : les tableaux « que fait le prix après la sortie ? » (Swing Support, etc.) ne s'empilent plus en haut de l'historique. Nouvel onglet **📊 Mesures après sortie**, à côté de « 📜 Trades » et « 📅 Par jour ».
- Carte « Ordres sur zone » : la mesure des stops est repliée par défaut (clic pour déplier).
- Aucune donnée ni décision modifiée ; le suivi après sortie continue de se remplir à l'identique.
- Fichiers : bot_engine.py, index.html, index_src.html, README.md.
