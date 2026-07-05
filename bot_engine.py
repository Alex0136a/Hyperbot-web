"""
╔═══════════════════════════════════════════════════════╗
║       HyperBot — Pro Edition                          ║
║  RSI + EMA + MACD + BB + Volume + Trailing SL         ║
║  Plage horaire | Multi-Crypto | Dashboard temps réel  ║
║  Perp (BTC, SOL, PAXG) — Long + Short sur tous       ║
╚═══════════════════════════════════════════════════════╝

INSTALLATION :
    pip install hyperliquid-python-sdk eth-account

LANCEMENT :
    python hyperbot_dashboard.py

NOTES :
    - BTC, SOL : marchés perpétuels (long + short)
    - PAXG     : perpétuel or sur Hyperliquid (index 187, levier max x10)
                 Tracker du prix de l or (1 PAXG = 1 once d or)
                 Long ET Short disponibles — remplace XAUT spot
"""

import time
import threading
from datetime import datetime
from collections import deque
import queue

# ─────────────────────────────────────────────
#  VERSION
# ─────────────────────────────────────────────
# Incrementer a chaque modification importante
# Visible dans le header du dashboard pour identifier
# exactement quelle version tourne sans ambiguite
BOT_VERSION = "3.1"
BOT_BUILD   = "2026-07-04-d"  # incremente a chaque correctif — visible dans les logs
                               # pour confirmer sans ambiguite quelle version tourne
# Historique :
# 3.1 (build 2026-07-04-d) — FIX CRITIQUE : NameError sur 'rsi_mode' utilise
#        avant d etre defini dans le message du filtre ATR — plantait
#        silencieusement le traitement d un actif des que le marche etait
#        juge trop calme (cause probable du blocage "collecte bloquee" —
#        le symbole disparaissait du log sans trace car _process_with_timeout
#        n avait pas de except pour capturer/loguer l exception). Egalement
#        fix : les exceptions dans _process sont desormais capturees et
#        loguees explicitement (message + traceback) au lieu d etre avalees.
# 3.1 (build 2026-07-04-c) — Timeout sur le fetch prix (get_prices), sur le
#        fetch CPI Finnhub, ET filet generique par symbole (_process_with_timeout,
#        12s) — protege contre tout gel du cycle, quelle qu en soit la cause
# 3.1 (build 2026-07-04-b) — WebSocket temps reel (allMids) pour Max Loss/SL/
#        Trailing TP, alarme visible ON/OFF, timeout sur get_prices
# 3.1 (build initial) —
# 3.1 — Fix bug cle API (check placeholder incorrect qui ne se declenchait jamais)
#        Fix bug session fantome (sauvegarde capital/session meme sans demarrage)
#        Levier (LEVERAGE) desormais reellement applique sur Hyperliquid
#        Cle privee/wallet chargeables depuis variables d environnement (securite)
#        Nouveau moteur de risque en dollars : Max Loss -0.75$ gere par le bot,
#        SL 1.5% conserve uniquement comme filet de securite sur Hyperliquid
#        Trailing Take Profit 2 etages : Quick Profit arme a +1$ (sortie si retour a 1$),
#        puis trailing illimite a partir de +1.5$ tant que le profit progresse
#        Score de confiance (0-100%) sur chaque signal — entree seulement si >= 65%
#        Confiance minimale dynamique par actif : +5% apres chaque perte, -5% apres chaque gain
# 1.0 — Version initiale (RSI + EMA + Trailing SL/TP)
# 1.1 — Ajout PAXG EMA 20/50, paliers corriges
# 1.2 — 6 slots ON/OFF, boutons SL/TP/FERMER manuels
# 1.3 — Filtre ATR par symbole, pivot EMA, 2 cycles PAXG
# 1.4 — Support/Resistance scalp (50 cycles)
# 1.5 — Persistance session, reprise auto, uptime
# 1.6 — ATR affiche sur cartes, PnL Session
# 1.7 — Filtre Momentum instantane (4 cycles, tous actifs)
# 1.8 — BTC SL/TP resseres 1.0%/2.0%, Trailing delta 0.8%
# 1.9 — Paliers BTC specifiques (0.35/0.7/1.0/1.5%)
# 2.0 — Fix repertoire travail (chdir au demarrage)
# 2.1 — RSI mode tendance BTC (>50=LONG, <50=SHORT)
# 2.2 — RSI mode tendance HYPE (meme logique que BTC)
# 2.3 — RSI mode tendance ETH + SOL + BNB (PAXG garde retournement)
# 2.4 — Logs enrichis : RSI+mode+ATR dans chaque ouverture et blocage
# 2.5 — Sauvegarde logs dans fichier (hyperbot_log_swing/scalp.txt) avec rotation 5MB
# 2.6 — Dashboard : max 5 messages de position visibles (6eme efface le plus ancien)
# 2.7 — Fichier log : epuration automatique 7 jours (declenchee si >2MB)
# 2.8 — Momentum BTC swing releve a 0.20% (etait 0.15%) — plus selectif SHORT
# 2.9 — EMA intermediaire BTC+ETH (EMA50 swing/EMA60 scalp) filtre tendance 25-50 min
# 2.9b— Log : retention reduite a 24h (etait 7 jours), epuration si >500KB
# 3.0 — TP ramene a 1.5% sur tous les actifs, EMA50/60 etendue a SOL/BNB/HYPE
#        TSL delta 0.6% (etait 1.2%), TTP step 0.8% (etait 1.5%)
#        SOL : MACD+BB obligatoires supprimes (bloquaient tous les trades)

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
CONFIG = {
    "PRIVATE_KEY":        "",
    "WALLET_ADDRESS":     "",


    # Cryptos à trader — 6 slots disponibles
    # Modifiables depuis le dashboard via les boutons ACTIF 1 a 6
    # v3.2 — liste etendue a 30 actifs (Hyperliquid propose 300+ perpetuels,
    # ces 30 sont les plus liquides/suivis). Les 6 premiers (BTC, PAXG, ETH,
    # SOL, BNB, HYPE) beneficient d un reglage fin par symbole (voir
    # SYMBOL_RSI_MODE, ATR_MIN_PCT_BY_SYMBOL, etc. plus bas) — les 24 autres
    # utilisent les reglages globaux par defaut (pas de tuning specifique).
    # ACTIVE_COINS (pilotable depuis l interface web) permet de n en activer
    # qu une partie a la fois ; MAX_OPEN_TRADES limite le nombre de positions
    # simultanees quel que soit le nombre d actifs actifs.
    "SYMBOLS":            ["BTC", "PAXG", "ETH", "SOL", "BNB", "HYPE",
                           "ARB", "AVAX", "LINK", "OP", "INJ", "TIA", "TAO",
                           "WIF", "JUP", "PENDLE", "EIGEN", "RENDER", "SUI",
                           "APT", "SEI", "DOGE", "XRP", "NEAR", "FTM", "AAVE",
                           "UNI", "CRV", "SUSHI", "GMX"],

    # v3.2 — Actifs actifs par defaut au tout premier demarrage (modifiable
    # ensuite depuis l interface web ou via HYPERBOT_ACTIVE_COINS) : parmi
    # les 30 marches disponibles, seuls ceux-la sont scannes pour de
    # nouvelles entrees tant que la selection n est pas changee.
    "ACTIVE_COINS":       ["SUI", "TAO", "NEAR", "WIF", "SOL", "HYPE"],
    "MAX_OPEN_TRADES":    6,

    # Tous les symboles sont des perpétuels — SPOT_SYMBOLS vide
    # PAXG remplace XAUT spot : index 187 sur Hyperliquid, levier max x10
    # Ticker direct "PAXG" dans l API (pas de @XXX)
    "SPOT_SYMBOLS":       [],
    "SPOT_TICKER_MAP":    {},
    "SPOT_SL_ASSET_MAP":  {},

    # Stop Loss / Take Profit specifiques par symbole — v3.2 : generalises,
    # plus aucune exception par symbole (voir PROFILE_SWING/PROFILE_SCALP,
    # qui ecrasent de toute facon ces valeurs a chaque demarrage/reset via
    # apply_profile). Utilise STOP_LOSS_PCT/TAKE_PROFIT_PCT globaux pour tous.
    "SYMBOL_SL_PCT":      {},
    "SYMBOL_TP_PCT":      {},

    # RSI specifique par symbole — v3.2 : generalise, plus d exception.
    "SYMBOL_RSI_OVERSOLD":   {},
    "SYMBOL_RSI_OVERBOUGHT": {},

    # Symboles pour lesquels MACD + BB sont OBLIGATOIRES pour entrer (pas juste optionnels)
    "SYMBOL_REQUIRE_MACD_BB": [],

    # Symboles pour lesquels l EMA200 est OBLIGATOIRE — v3.2 : generalise,
    # plus aucun symbole n a cette contrainte particuliere.
    "SYMBOL_REQUIRE_EMA200": [],

    "CAPITAL_USD":        1000,
    "POSITION_SIZE_PCT":  5,               # 5% du capital par trade — coherent avec Max Loss -0.75$
    "LEVERAGE":           1,

    # RSI — seuils elargis pour signaux plus forts et moins de faux positifs
    "RSI_PERIOD":         14,
    "RSI_OVERSOLD":       32,              # etait 38 : entre uniquement sur vraie survente
    "RSI_OVERBOUGHT":     68,              # etait 62 : entre uniquement sur vrai surachat

    # EMA — periodes plus longues pour reduire les faux croisements (whipsaws)
    "EMA_SHORT":          12,             # etait 8
    "EMA_LONG":           26,             # etait 21

    # MACD — inchange, deja bien calibre
    "MACD_FAST":          12,
    "MACD_SLOW":          26,
    "MACD_SIGNAL":        9,

    # Bollinger Bands — std reduit pour que les bandes soient utiles
    "BB_PERIOD":          20,             # etait 14 : periode standard
    "BB_STD":             2.0,            # etait 2.5 : bandes plus proches = filtre actif

    # Volume : ratio minimum vs moyenne (1.0 = desactive)
    "VOLUME_MIN_RATIO":   1.2,            # etait 1.5 : moins restrictif

    # Gestion du risque — SL elargi pour laisser le trade respirer
    "STOP_LOSS_PCT":      1.5,            # etait 0.8 : evite les SL sur simple bruit
    "TAKE_PROFIT_PCT":    3.0,            # etait 2.0 : ratio RR 1:2 maintenu

    # Trailing Stop Loss — delta elargi pour ne pas couper les trades gagnants
    "TRAILING_STOP":      True,
    "TRAILING_DELTA_PCT": 1.2,   # delta global, applique desormais a tous uniformement

    # v3.2 : plus de delta specifique par symbole — generalise a tous.
    "SYMBOL_TRAILING_DELTA_PCT": {},

    # Seuil minimum de deplacement du Trailing SL avant synchronisation sur Hyperliquid.
    # Evite les appels API inutiles sur de micro-mouvements de prix.
    # Exemple : 0.1 = le SL doit avoir bouge d au moins 0.1% pour etre envoye a Hyperliquid.
    "TRAILING_SL_MIN_MOVE_PCT": 0.1,

    # Buffer de securite sur les ordres SL/TP poses sur Hyperliquid.
    # Meme avec le mark price, un micro-ecart residuel peut exister.
    # Ce buffer decale legerement les niveaux pour eviter les sorties inattendues.
    # Exemple : 0.05% sur un SL a $60000 long = SL pose a $59970 au lieu de $60000
    "MARK_PRICE_BUFFER_PCT": 0.05,
    # Apres la reouverture du marche Forex (lundi matin, fin de pause nocturne),
    # le bot observe PAXG pendant cette duree avant de prendre des positions.
    # Permet au spread de se normaliser et aux indicateurs de se recaler.
    # S applique aussi bien aux longs qu aux shorts sur PAXG.
    "FOREX_WARMUP_MINUTES": 15,
    "FOREX_SYMBOLS":        ["PAXG"],  # symboles soumis a la chauffe Forex

    # ── Trailing Take Profit ──────────────────────────────────────────────────
    # Quand le prix atteint le TP initial, au lieu de fermer la position,
    # le bot deplace le TP plus haut (step) et attend un retournement de tendance
    # confirme par au moins 2 signaux sur 3 avant de sortir.
    "TRAILING_TP":             True,
    "TRAILING_TP_STEP_PCT":    1.5,
    # Seuils de retournement pour la sortie Trailing TP :
    "TRAILING_TP_RSI_EXIT":    55,
    "TRAILING_TP_MIN_SIGNALS": 2,

    # SL protecteur des gains — quand le Trailing TP se deplace vers un nouveau sommet,
    # le SL remonte a ce pourcentage du TP precedent (calcule sur le gain, pas le prix brut).
    # 97% = on preserve 97% du gain acquis au moment ou le dernier TP etait atteint.
    # Garantit qu on ne peut jamais reperdre ce qui a ete gagne une fois le TP initial touche.
    "TRAILING_TP_PROTECT_PCT": 0.97,

    # Plage horaire Paris (0 et 24 = 24h/24) — appliquee en mode paper ET live
    "TRADE_HOUR_START":   0,
    "TRADE_HOUR_END":     24,

    "MODE":               "paper",
    "CYCLE_INTERVAL":     15,
    # Duree (s) sans tick WebSocket recu au-dela de laquelle le cycle reprend
    # la main sur la surveillance des positions ouvertes (filet de secours si
    # le WebSocket se deconnecte silencieusement). Voir _on_ws_allmids et
    # _maybe_manage_position_via_cycle.
    "WS_STALE_AFTER_SEC": 20,
    # Delai max (s) tolere pour un appel reseau de recuperation des prix
    # (get_prices). Au-dela, l appel est ABANDONNE (thread daemon laisse
    # tourner en arriere-plan) plutot que de geler tout le cycle indefiniment
    # en cas de coupure reseau. Voir _get_prices_with_timeout.
    "PRICE_FETCH_TIMEOUT_SEC": 10,
    # Delai max (s) tolere pour le traitement complet d un symbole (_process).
    # Filet de secours generique : si N IMPORTE QUELLE partie du traitement se
    # bloque un jour (appel reseau cache, I/O disque, etc.), ce symbole est
    # simplement ignore pour ce cycle au lieu de geler tout le bot.
    "PROCESS_TIMEOUT_SEC": 12,

    # Profil actif au demarrage : "swing" ou "scalp"
    "PROFILE":            "swing",

    # ── Moteur de risque en DOLLARS (v3.1) ──────────────────────────────────
    # Le SL % ci-dessus (STOP_LOSS_PCT / SYMBOL_SL_PCT) n est plus utilise
    # pour la gestion normale des sorties : il sert desormais UNIQUEMENT a
    # poser un ordre de securite fixe sur Hyperliquid (filet de secours si
    # le bot est deconnecte / en retard). La gestion normale se fait en $ :
    "EXCHANGE_SAFETY_SL_PCT": 2.0,   # SL pose sur Hyperliquid — filet de securite uniquement (cas bot non surveille)
    "MAX_LOSS_USD":           0.75,  # Perte max geree par le bot avant fermeture immediate

    # Trailing Take Profit a 2 etages, en dollars de PnL latent :
    # Etage 1 (Quick Profit)   : arme des que le profit atteint QUICK_PROFIT_ARM_USD.
    #                            Si le profit retombe a QUICK_PROFIT_LOCK_USD ou moins,
    #                            fermeture immediate pour capturer ce montant.
    # Etage 2 (Trailing illimite) : active des que le profit atteint TRAILING_TP_ARM_USD.
    #                            Le pic de profit est traque en continu ; la position
    #                            reste ouverte tant qu un nouveau pic est atteint et se
    #                            ferme des que le profit cesse de progresser (1ere baisse
    #                            depuis le pic), pour capturer le maximum atteint.
    "QUICK_PROFIT_ARM_USD":   1.0,
    "QUICK_PROFIT_LOCK_USD":  1.0,
    "TRAILING_TP_ARM_USD":    1.5,

    # ── Score de confiance (0-100%) — filtre final avant toute entree ───────
    # Poids relatifs des confirmations optionnelles disponibles pour un signal.
    # Le score est ramene sur 100% du poids REELLEMENT disponible pour ce
    # cycle/symbole (ex: si EMA200 n est pas calculable, son poids est retire
    # du total plutot que compte comme un echec).
    # Valeurs par defaut raisonnables — a ajuster selon les resultats observes.
    "CONFIDENCE_WEIGHTS": {
        "macd":      20,   # MACD aligne avec la direction du signal
        "bollinger": 15,   # Prix du bon cote de la bande de Bollinger
        "volume":    15,   # Volume superieur a la moyenne recente
        "ema200":    15,   # Alignement avec la tendance longue (EMA200)
        "ema_mid":   15,   # Alignement avec la tendance intermediaire (25-50 min)
        "momentum":  10,   # Momentum instantane franchement dans le sens du signal
        "consec":    10,   # Cycles consecutifs au-dela du minimum requis (conviction)
    },
    "CONFIDENCE_MIN_PCT":  65.0,  # Seuil minimum pour prendre un trade
    "CONFIDENCE_STEP_PCT": 5.0,   # Ajustement du seuil par actif a chaque perte/gain
    "CONFIDENCE_MAX_PCT":  90.0,  # Plafond du seuil dynamique (evite de bloquer un actif a vie)

    # v3.2 — Auto-activation d un actif INACTIF (pas dans ACTIVE_COINS) si
    # une opportunite exceptionnelle est detectee dessus (confiance >= ce
    # seuil). Permet de profiter d une belle opportunite sur l un des 30
    # marches suivis sans devoir l activer manuellement a l avance.
    "AUTO_ACTIVATE_CONFIDENCE_PCT": 80.0,

    # v3.2 — Prudence live : session de trading limitee a 23h45 sur chaque
    # periode de 24h. Passe ce delai, plus aucune NOUVELLE entree n est
    # ouverte tant que TOUTES les positions de la session ne sont pas
    # fermees (normalement ou manuellement) — une fois toutes fermees, une
    # nouvelle session de 24h redemarre immediatement. Les positions deja
    # ouvertes continuent d etre gerees normalement (SL/Quick Profit/
    # Trailing) pendant cette periode de blocage.
    "SESSION_MAX_HOURS": 23.75,

    # v3.2 — Zones RSI extremes : evite d entrer a contre-sens dans une zone
    # de retournement violent probable (survente/surachat extreme). Ne
    # bloque QUE les nouvelles entrees dans le sens "continuation" quand le
    # RSI est deja tres extreme.
    "RSI_EXTREME_LOW": 15,   # ne pas SHORT si RSI < ce seuil (survente extreme)
    "RSI_EXTREME_HIGH": 85,  # ne pas LONG si RSI > ce seuil (surachat extreme)

    # ── Heures creuses crypto — nouvelles entrees suspendues (PAXG exclu, ─────
    #    deja gere par FOREX_SYMBOLS/is_forex_open) ───────────────────────────
    # Fenetre par defaut : 02h-06h UTC, periode de liquidite generalement la
    # plus faible sur les marches crypto. A ajuster si besoin.
    "CRYPTO_OFFPEAK_HOUR_START_UTC": 2,
    "CRYPTO_OFFPEAK_HOUR_END_UTC":   6,

    # ── Blackout CPI (annonces US) via calendrier economique Finnhub ─────────
    # Cle chargeable depuis la variable d environnement HYPERBOT_FINNHUB_API_KEY.
    # Bloque uniquement les NOUVELLES entrees crypto autour de l heure de
    # publication du CPI (le PAXG est deja couvert par la fermeture Forex).
    "FINNHUB_API_KEY":         "",
    "CPI_BLACKOUT_BEFORE_MIN": 15,   # minutes avant l annonce
    "CPI_BLACKOUT_AFTER_MIN":  30,   # minutes apres l annonce
    "CPI_CACHE_REFRESH_HOURS": 12,   # frequence de rafraichissement du calendrier
}

# ─────────────────────────────────────────────
#  SECURITE — CLE PRIVEE / WALLET DEPUIS L ENVIRONNEMENT
# ─────────────────────────────────────────────
# Priorite aux variables d environnement HYPERBOT_PRIVATE_KEY /
# HYPERBOT_WALLET_ADDRESS pour eviter de stocker un secret en clair dans ce
# fichier (risque si le fichier est partage, versionne ou sauvegarde dans le
# cloud). Si absentes, on retombe sur les valeurs codees en dur ci-dessus
# (deconseille en usage reel).
# Exemple avant lancement (Windows PowerShell) :
#   $env:HYPERBOT_PRIVATE_KEY   = "0x..."
#   $env:HYPERBOT_WALLET_ADDRESS = "0x..."
# Exemple avant lancement (Linux / macOS) :
#   export HYPERBOT_PRIVATE_KEY=0x...
#   export HYPERBOT_WALLET_ADDRESS=0x...
import os as _os_env
CONFIG["PRIVATE_KEY"]    = _os_env.environ.get("HYPERBOT_PRIVATE_KEY", CONFIG["PRIVATE_KEY"])
CONFIG["WALLET_ADDRESS"] = _os_env.environ.get("HYPERBOT_WALLET_ADDRESS", CONFIG["WALLET_ADDRESS"])
CONFIG["FINNHUB_API_KEY"] = _os_env.environ.get("HYPERBOT_FINNHUB_API_KEY", CONFIG["FINNHUB_API_KEY"])

# ─────────────────────────────────────────────
#  PROFILS SWING / SCALP
# ─────────────────────────────────────────────
PROFILE_SWING = {
    "PROFILE":                  "swing",
    # v3.2 — REGLES GENERALISEES A TOUS LES CRYPTOS : plus aucun traitement
    # special par symbole (RSI, EMA, ATR, pivot, EMA200, cycles consecutifs).
    # Tous les actifs (BTC, ETH, SOL, BNB, HYPE, PAXG et les 24 autres)
    # utilisent exactement les memes seuils globaux ci-dessous.
    # SEULE EXCEPTION : PAXG (l or) suit en plus les heures de fermeture du
    # Forex (voir FOREX_SYMBOLS dans CONFIG, gere independamment de ce profil)
    # — c est la seule difference de traitement qui subsiste pour l or.
    "RSI_OVERSOLD":             32,
    "RSI_OVERBOUGHT":           68,
    "SYMBOL_RSI_OVERSOLD":      {},
    "SYMBOL_RSI_OVERBOUGHT":    {},
    # Mode RSI unique pour tous : "trend" (entre dans le sens du momentum,
    # RSI>50=LONG, RSI<50=SHORT) — auparavant reserve a BTC/ETH/SOL/BNB/HYPE,
    # desormais le comportement par defaut pour tout le monde (voir le
    # fallback "trend" dans _process, plus "reversal").
    "SYMBOL_RSI_MODE":          {},
    "EMA_SHORT":                12,
    "EMA_LONG":                 26,
    "SYMBOL_EMA_SHORT":         {},
    "SYMBOL_EMA_LONG":          {},
    # EMA intermediaire — filtre de tendance 25-50 min, applique desormais a
    # TOUS les actifs de la meme facon via EMA_MID_PERIOD (plus de dict par
    # symbole).
    "EMA_MID_PERIOD":           50,
    "SYMBOL_EMA_MID":           {},
    "STOP_LOSS_PCT":            1.5,
    "TAKE_PROFIT_PCT":          1.5,
    "SYMBOL_SL_PCT":            {},
    "SYMBOL_TP_PCT":            {},
    "TRAILING_STOP":            True,
    "TRAILING_DELTA_PCT":       0.6,
    "SYMBOL_TRAILING_DELTA_PCT":{},
    "TRAILING_TP":              True,
    "TRAILING_TP_STEP_PCT":     0.8,
    "TRAILING_TP_RSI_EXIT":     55,
    "TRAILING_TP_MIN_SIGNALS":  2,
    "TRAILING_TP_PROTECT_PCT":  0.97,
    "TRAILING_SL_MIN_MOVE_PCT": 0.1,
    "SYMBOL_REQUIRE_MACD_BB":   [],
    "SYMBOL_REQUIRE_EMA200":    [],
    "PIVOT_CONFIRM_SYMBOLS":    [],
    "CONSEC_CONFIRM_SYMBOLS":   {},
    "VOLUME_MIN_RATIO":         1.2,
    "FOREX_WARMUP_MINUTES":     15,
    # Filtre ATR en swing — bloque les entrees sur marche trop calme.
    # Seuil global unique pour tous les actifs.
    "ATR_FILTER":               True,
    "ATR_PERIOD":               14,
    "ATR_MIN_PCT":              0.05,
    "ATR_MIN_PCT_BY_SYMBOL":    {},

    # ── SL par paliers de gains (swing uniquement) ───────────────────────────
    "SL_LOCK_ENABLED":          True,
    "SL_LOCK_STEPS": [
        (0.5, 0.0),   # +0.5% → breakeven
        (1.0, 0.9),   # +1.0% → +0.9%
        (1.5, 1.3),   # +1.5% → +1.3%
        (2.0, 1.8),   # +2.0% → +1.8%
    ],
    "SL_LOCK_STEPS_BY_SYMBOL": {},

    # Momentum Instantane — "ce qui se passe MAINTENANT" prevaut sur les EMA
    # Si le prix a bouge de +/-0.20% sur les 4 derniers cycles (2 min) dans
    # le sens OPPOSE au signal EMA/RSI, l entree est bloquee. Seuil unique
    # pour tous les actifs.
    "MOMENTUM_PERIOD":          4,
    "MOMENTUM_THRESHOLD_PCT":   0.20,
}

PROFILE_SCALP = {
    "PROFILE":                  "scalp",
    # RSI compromis swing/scalp — BTC entre plus facilement
    "RSI_OVERSOLD":             40,
    "RSI_OVERBOUGHT":           60,
    "SYMBOL_RSI_OVERSOLD":      {},
    "SYMBOL_RSI_OVERBOUGHT":    {},
    "SYMBOL_RSI_MODE":          {},
    # EMA plus courtes pour etre plus reactif
    "EMA_SHORT":                8,
    "EMA_LONG":                 21,
    "SYMBOL_EMA_SHORT":         {},
    "SYMBOL_EMA_LONG":          {},
    # EMA intermediaire scalp — fenetre plus courte (30 min), uniforme pour tous
    "EMA_MID_PERIOD":           30,
    "SYMBOL_EMA_MID":           {},
    # SL et TP serres
    "STOP_LOSS_PCT":            0.4,
    "TAKE_PROFIT_PCT":          0.8,
    "SYMBOL_SL_PCT":            {},
    "SYMBOL_TP_PCT":            {},
    # Trailing serre
    "TRAILING_STOP":            True,
    "TRAILING_DELTA_PCT":       0.3,
    "SYMBOL_TRAILING_DELTA_PCT":{},
    "TRAILING_TP":              True,
    "TRAILING_TP_STEP_PCT":     0.4,
    "TRAILING_TP_RSI_EXIT":     52,
    "TRAILING_TP_MIN_SIGNALS":  2,
    "TRAILING_TP_PROTECT_PCT":  0.97,
    "TRAILING_SL_MIN_MOVE_PCT": 0.05,
    "SYMBOL_REQUIRE_EMA200":    [],
    "SYMBOL_REQUIRE_MACD_BB":   [],
    "PIVOT_CONFIRM_SYMBOLS":    [],
    "CONSEC_CONFIRM_SYMBOLS":   {},
    "VOLUME_MIN_RATIO":         1.0,
    "FOREX_WARMUP_MINUTES":     5,
    # Filtre ATR — seuil global unique pour tous les actifs
    "ATR_FILTER":               True,
    "ATR_PERIOD":               14,
    "ATR_MIN_PCT":              0.06,
    "ATR_MIN_PCT_BY_SYMBOL":    {},
    "ATR_EXCLUDE_SYMBOLS":      [],     # plus d exclusion

    # Support/Resistance — confirmation de breakout (SCALP uniquement)
    # LONG  : prix doit CASSER au-dessus de la resistance des 50 derniers cycles (25 min)
    # SHORT : prix doit CASSER en-dessous du support des 50 derniers cycles
    # Filtre les faux signaux RSI/EMA en exigeant un vrai mouvement directionnel
    "SR_PERIOD":                50,

    # Momentum Instantane — "ce qui se passe MAINTENANT" prevaut sur les EMA
    # Si le prix a bouge de +/-0.10% sur les 4 derniers cycles (2 min) dans
    # le sens OPPOSE au signal EMA/RSI, l entree est bloquee.
    # Seuil plus bas qu en swing car cycles plus courts et mouvements rapides.
    "MOMENTUM_PERIOD":          4,
    "MOMENTUM_THRESHOLD_PCT":   0.10,
}

def apply_profile(cfg, profile_name):
    """Applique un profil SWING ou SCALP sur le cfg actif.
    Preserve les parametres fixes (cles API, symboles, capital, mode).
    """
    profile = PROFILE_SWING if profile_name == "swing" else PROFILE_SCALP
    for k, v in profile.items():
        cfg[k] = v
    cfg["PROFILE"] = profile_name

# ─────────────────────────────────────────────
#  INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    recent = prices[-(period + 1):]
    gains = losses = 0
    for i in range(1, len(recent)):
        d = recent[i] - recent[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    return 100 - (100 / (1 + gains / losses))

def calc_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return None, None
    macd_series = []
    for i in range(slow - 1, len(prices)):
        ef = calc_ema(prices[:i+1], fast)
        es = calc_ema(prices[:i+1], slow)
        if ef and es:
            macd_series.append(ef - es)
    if len(macd_series) < signal:
        return None, None
    macd_line = macd_series[-1]
    signal_line = calc_ema(macd_series, signal)
    return macd_line, signal_line

def calc_bollinger(prices, period=20, std_mult=2.0):
    if len(prices) < period:
        return None, None, None
    recent = prices[-period:]
    mid = sum(recent) / period
    variance = sum((p - mid) ** 2 for p in recent) / period
    std = variance ** 0.5
    return mid + std_mult * std, mid, mid - std_mult * std


def calc_atr(prices, period=14):
    """Average True Range — mesure la volatilite reelle du marche.
    Un ATR% faible = marche en range, risque de faux signaux.
    Un ATR% eleve = marche directionnel, bonne opportunite de scalping.
    Utilise les variations de close (pas de high/low disponibles).
    Retourne (atr_abs, atr_pct) ou (None, None) si insuffisant.
    """
    if len(prices) < period + 1:
        return None, None
    tr_list = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    atr = sum(tr_list[-period:]) / period
    atr_pct = (atr / prices[-1]) * 100 if prices[-1] > 0 else 0
    return atr, atr_pct


def calc_support_resistance(prices, period=50):
    """Calcule le support et la resistance recents.
    Resistance = plus haut local sur la periode (hors prix courant)
    Support    = plus bas local sur la periode (hors prix courant)
    Utilise pour confirmer les breakouts en scalping :
    - LONG valide si le prix CASSE au-dessus de la resistance recente
    - SHORT valide si le prix CASSE en-dessous du support recent
    Retourne (support, resistance) ou (None, None) si insuffisant.
    """
    if len(prices) < period + 1:
        return None, None
    # Exclure le prix courant (dernier element) pour eviter l auto-validation
    window = prices[-(period+1):-1]
    support    = min(window)
    resistance = max(window)
    return support, resistance

# ─────────────────────────────────────────────
#  CONNEXION HYPERLIQUID
# ─────────────────────────────────────────────
def connect_hyperliquid(private_key, wallet_address):
    """Retourne (info, exchange, error_detail). error_detail est None en cas
    de succes, sinon un message texte precis (type + message de l exception)
    — evite d avaler silencieusement la vraie cause d un echec de connexion
    (mauvais format de cle, dependance manquante, probleme reseau, etc.)."""
    try:
        from hyperliquid.info import Info
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        import eth_account
        account = eth_account.Account.from_key(private_key)
        # v3.1 : skip_ws=False active la connexion WebSocket du SDK, necessaire
        # pour s abonner au flux temps reel (allMids) utilise par la
        # surveillance Max Loss / Trailing TP en direct (voir _on_ws_allmids).
        info = Info(constants.MAINNET_API_URL, skip_ws=False)
        exchange = Exchange(account, constants.MAINNET_API_URL, vault_address=wallet_address)
        return info, exchange, None
    except Exception as e:
        import traceback
        detail = f"{type(e).__name__}: {e}"
        print(f"[connect_hyperliquid] {detail}")
        print(traceback.format_exc())
        return None, None, detail

def sync_capital_from_hyperliquid(info, wallet_address):
    """Lit le solde réel USDC depuis Hyperliquid et le retourne.
    Retourne None en cas d'echec pour ne pas ecraser le capital local.
    """
    try:
        state = info.user_state(wallet_address)
        real_balance = float(state["marginSummary"]["accountValue"])
        print(f"[CAPITAL] Solde reel Hyperliquid : ${real_balance:.2f}")
        return real_balance
    except Exception as e:
        print(f"[CAPITAL] Impossible de lire le solde Hyperliquid : {e}")
        return None

def recover_open_positions(info, wallet_address, symbols, cfg):
    """Recupere les positions ouvertes sur Hyperliquid apres un crash.
    Retourne un dict {symbol: position_dict} compatible avec SymbolState.
    """
    recovered = {}
    try:
        state = info.user_state(wallet_address)
        positions = state.get("assetPositions", [])
        for item in positions:
            pos = item.get("position", {})
            coin = pos.get("coin", "")
            szi  = float(pos.get("szi", 0))      # positif = long, negatif = short
            entry = float(pos.get("entryPx", 0) or 0)
            if coin not in symbols or szi == 0 or entry == 0:
                continue
            direction = "long" if szi > 0 else "short"
            size_usd  = abs(szi) * entry
            sl_pct = cfg.get("SYMBOL_SL_PCT", {}).get(coin, cfg["STOP_LOSS_PCT"])
            tp_pct = cfg.get("SYMBOL_TP_PCT", {}).get(coin, cfg["TAKE_PROFIT_PCT"])
            sl_p = entry * (1 - sl_pct/100) if direction == "long" else entry * (1 + sl_pct/100)
            tp_p = entry * (1 + tp_pct/100) if direction == "long" else entry * (1 - tp_pct/100)
            recovered[coin] = {
                "type":  direction,
                "entry": entry,
                "sl":    sl_p,
                "tp":    tp_p,
                "size":  size_usd,
                "peak":  entry,
            }
            print(f"[RECOVER] {coin} {direction.upper()} @ ${entry:.2f} | SL ${sl_p:.2f} | TP ${tp_p:.2f}")
    except Exception as e:
        print(f"[RECOVER] Erreur recuperation positions : {e}")
    return recovered

def reconcile_closed_positions(info, wallet_address, saved_positions, cfg):
    """Au redemarrage en mode live, compare les positions sauvegardees localement
    avec ce qu Hyperliquid retourne. Si une position n existe plus sur la bourse,
    c est qu elle a ete fermee pendant la deconnexion (SL ou TP touche).
    Retourne une liste de trades reconstitues pour mise a jour du capital et historique.
    """
    ghost_trades = []
    if not saved_positions:
        return ghost_trades
    try:
        state      = info.user_state(wallet_address)
        open_coins = set()
        for item in state.get("assetPositions", []):
            pos  = item.get("position", {})
            coin = pos.get("coin", "")
            szi  = float(pos.get("szi", 0))
            if coin and szi != 0:
                open_coins.add(coin)

        # Recuperer l historique recent des fills pour connaitre le prix de cloture reel
        try:
            fills = info.user_fills(wallet_address)
        except Exception:
            fills = []

        fill_map = {}  # coin -> dernier fill de cloture
        for f in fills:
            coin = f.get("coin", "")
            if f.get("dir", "") in ("Close Long", "Close Short") or f.get("reduceOnly", False):
                if coin not in fill_map:
                    fill_map[coin] = f  # on prend le plus recent

        for coin, pos in saved_positions.items():
            if coin in open_coins:
                continue  # position encore ouverte, rien a faire

            # La position a disparu pendant la deconnexion
            entry  = pos["entry"]
            size   = pos["size"]
            ptype  = pos["type"]

            fill    = fill_map.get(coin)
            exit_px = float(fill["px"]) if fill else pos["sl"]
            reason  = "SL/TP HYPERLIQUID" if fill else "SL ESTIME"

            if ptype == "long":
                pnl_pct = (exit_px - entry) / entry * 100
            else:
                pnl_pct = (entry - exit_px) / entry * 100
            pnl_usd = size * pnl_pct / 100
            win     = pnl_usd > 0

            ghost_trades.append({
                "symbol":  coin,
                "type":    ptype,
                "entry":   entry,
                "exit":    exit_px,
                "pnl":     round(pnl_usd, 4),
                "pnl_pct": round(pnl_pct, 2),
                "reason":  reason,
                "win":     win,
                "ts":      time.time(),
            })
            print(f"[RECONCILE] {coin} ferme pendant deconnexion | {reason} @ ${exit_px:.2f} | PnL: ${pnl_usd:.2f}")

    except Exception as e:
        print(f"[RECONCILE] Erreur : {e}")
    return ghost_trades

def emergency_close_all(exchange, info, wallet_address, cfg):
    """Fermeture d'urgence de toutes les positions ouvertes sur Hyperliquid.
    Utilisé si la reprise est impossible.
    """
    try:
        state = info.user_state(wallet_address)
        positions = state.get("assetPositions", [])
        for item in positions:
            pos = item.get("position", {})
            coin = pos.get("coin", "")
            szi  = float(pos.get("szi", 0))
            if szi == 0:
                continue
            try:
                if is_spot(coin, cfg):
                    api_ticker = cfg.get("SPOT_TICKER_MAP", {}).get(coin, coin)
                    sz = abs(round(szi, 6))
                    exchange.market_open(api_ticker, szi < 0, sz)  # vendre si long, racheter si short
                else:
                    exchange.market_close(coin)
                print(f"[URGENCE] {coin} ferme avec succes")
            except Exception as e:
                print(f"[URGENCE] Erreur fermeture {coin} : {e}")
    except Exception as e:
        print(f"[URGENCE] Erreur recuperation positions : {e}")

def ticker_from_slot_key(slot_key):
    """Extrait le vrai ticker API depuis une cle slot.
    "BTC_0"  → "BTC"
    "SOL_2"  → "SOL"
    "BTC_1"  → "BTC"  (deuxieme slot BTC)
    Si pas de suffixe _N, retourne tel quel (compatibilite).
    """
    parts = slot_key.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return slot_key


def get_prices(info, slot_keys, cfg):
    """Recupere les prix mark price pour une liste de slot_keys."""
    try:
        meta, asset_ctxs = info.meta_and_asset_ctxs()
        universe = meta.get("universe", [])

        mark_prices = {}
        for i, ctx in enumerate(asset_ctxs):
            if i < len(universe) and ctx and ctx.get("markPx"):
                name = universe[i].get("name", "")
                try:
                    mark_prices[name] = float(ctx["markPx"])
                except (ValueError, TypeError):
                    pass

        result = {}
        for k in slot_keys:
            t = ticker_from_slot_key(k)
            if t in mark_prices and mark_prices[t] > 0:
                result[k] = mark_prices[t]

        missing = [k for k in slot_keys if k not in result]
        if missing:
            mids = info.all_mids()
            for k in missing:
                t = ticker_from_slot_key(k)
                if t in mids:
                    try:
                        v = float(mids[t])
                        if v > 0:
                            result[k] = v
                    except (ValueError, TypeError):
                        pass

        return result

    except Exception as e:
        try:
            mids = info.all_mids()
            result = {}
            for k in slot_keys:
                t = ticker_from_slot_key(k)
                if t in mids:
                    try:
                        v = float(mids[t])
                        if v > 0:
                            result[k] = v
                    except (ValueError, TypeError):
                        pass
            return result
        except Exception:
            return {}
def is_spot(symbol, cfg):
    # symbol peut etre une slot_key "BTC_0" — extraire le vrai ticker
    return ticker_from_slot_key(symbol) in cfg.get("SPOT_SYMBOLS", [])

def place_order(exchange, symbol, is_buy, size_usd, price, cfg, sl_price=None, tp_price=None):
    """Passe un ordre market d entree avec SL et TP sur Hyperliquid.
    symbol peut etre une slot_key "BTC_0" — le vrai ticker est extrait automatiquement.
    """
    ticker = ticker_from_slot_key(symbol)
    try:
        if is_spot(symbol, cfg):
            sz = max(round(size_usd / price, 6), 0.0001)
            api_ticker = cfg.get("SPOT_TICKER_MAP", {}).get(ticker, ticker)
            result = exchange.market_open(api_ticker, is_buy, sz)
            entry_ok = result and result.get("status") == "ok"

            if entry_ok:
                position_mock = {"type": "long" if is_buy else "short", "entry": price, "size": size_usd}
                protective_orders = []

                if sl_price is not None:
                    sl_order = _build_sl_order(ticker, position_mock, sl_price, cfg)
                    if sl_order:
                        protective_orders.append(sl_order)
                    else:
                        print(f"[ORDER] SL spot {ticker} : asset ID inconnu — protection interne uniquement")

                if tp_price is not None:
                    tp_order = _build_tp_order(ticker, position_mock, tp_price, cfg)
                    if tp_order:
                        protective_orders.append(tp_order)
                    else:
                        print(f"[ORDER] TP spot {ticker} : asset ID inconnu — gere en interne uniquement")

                if protective_orders:
                    prot_result = exchange.bulk_orders(protective_orders, grouping="na")
                    prot_ok = prot_result and prot_result.get("status") == "ok"
                    if not prot_ok:
                        print(f"[ORDER] SL/TP spot {ticker} non poses — protection interne uniquement")

            return entry_ok

        # ── PERP : entree + SL + TP en groupe atomique normalTpsl ──
        sz = max(round(size_usd / price, 4), 0.001)
        close_side = not is_buy
        pos_mock = {"type": "long" if is_buy else "short", "entry": price, "size": size_usd}

        entry_order = {
            "coin":        ticker,
            "is_buy":      is_buy,
            "sz":          sz,
            "px":          price * 1.01 if is_buy else price * 0.99,
            "order_type":  {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
        }
        orders = [entry_order]

        if sl_price is not None:
            sl_order = _build_sl_order(ticker, pos_mock, sl_price, cfg)
            if sl_order:
                orders.append(sl_order)

        if tp_price is not None:
            tp_order = _build_tp_order(ticker, pos_mock, tp_price, cfg)
            if tp_order:
                orders.append(tp_order)

        grouping = "normalTpsl" if len(orders) > 1 else "na"
        result = exchange.bulk_orders(orders, grouping=grouping)

        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            return bool(statuses and "error" not in statuses[0])
        return False

    except Exception as e:
        print(f"[ORDER] Erreur place_order {ticker} : {e}")
        return False

def close_order(exchange, symbol, position, cfg):
    """Ferme une position — perp ou spot selon le symbole."""
    ticker = ticker_from_slot_key(symbol)
    try:
        if is_spot(symbol, cfg):
            sz = max(round(position["size"] / position["entry"], 6), 0.0001)
            api_ticker = cfg.get("SPOT_TICKER_MAP", {}).get(ticker, ticker)
            is_buy = position["type"] == "short"
            result = exchange.market_open(api_ticker, is_buy, sz)
        else:
            result = exchange.market_close(ticker)
        return result and result.get("status") == "ok"
    except Exception:
        return False

def _spot_sl_asset(symbol, cfg):
    """Retourne l asset ID numerique pour les ordres trigger spot (10000 + index).
    Utilise SPOT_SL_ASSET_MAP si disponible, sinon tente de parser le ticker @NNN.
    Retourne None si non resolvable (SL natif impossible).
    """
    asset_map = cfg.get("SPOT_SL_ASSET_MAP", {})
    if symbol in asset_map:
        return asset_map[symbol]
    # Fallback : parser "@182" → 10182
    ticker = cfg.get("SPOT_TICKER_MAP", {}).get(symbol, "")
    if ticker.startswith("@"):
        try:
            return 10000 + int(ticker[1:])
        except ValueError:
            pass
    return None


def _build_sl_order(symbol, position, sl_price, cfg):
    """Construit un ordre SL trigger avec buffer de securite mark price.
    Long  : trigger decale legerement sous sl_price (buffer vers le bas)
    Short : trigger decale legerement au dessus de sl_price (buffer vers le haut)
    Garantit que le SL ne se declenche pas sur un micro-ecart mark/mid.
    """
    is_long    = position["type"] == "long"
    close_side = not is_long
    sz         = max(round(position["size"] / position["entry"], 4), 0.001)

    buffer     = cfg.get("MARK_PRICE_BUFFER_PCT", 0.05) / 100
    trigger_px = round(sl_price * (1 - buffer) if is_long else sl_price * (1 + buffer), 2)
    limit_px   = round(trigger_px * 0.99 if is_long else trigger_px * 1.01, 2)

    if is_spot(symbol, cfg):
        asset_id = _spot_sl_asset(symbol, cfg)
        if asset_id is None:
            return None
        coin_field = str(asset_id)
    else:
        coin_field = symbol

    return {
        "coin":        coin_field,
        "is_buy":      close_side,
        "sz":          sz,
        "px":          limit_px,
        "order_type":  {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}},
        "reduce_only": True,
    }


def _build_tp_order(symbol, position, tp_price, cfg):
    """Construit un ordre TP trigger avec buffer de securite mark price.
    Long  : trigger decale legerement au dessus de tp_price (buffer vers le haut)
    Short : trigger decale legerement sous tp_price (buffer vers le bas)
    Garantit que le TP ne se declenche pas trop tot sur un micro-ecart mark/mid.
    """
    is_long    = position["type"] == "long"
    close_side = not is_long
    sz         = max(round(position["size"] / position["entry"], 4), 0.001)

    buffer     = cfg.get("MARK_PRICE_BUFFER_PCT", 0.05) / 100
    trigger_px = round(tp_price * (1 + buffer) if is_long else tp_price * (1 - buffer), 2)
    limit_px   = round(trigger_px * 0.99 if is_long else trigger_px * 1.01, 2)

    if is_spot(symbol, cfg):
        asset_id = _spot_sl_asset(symbol, cfg)
        if asset_id is None:
            return None
        coin_field = str(asset_id)
    else:
        coin_field = symbol

    return {
        "coin":        coin_field,
        "is_buy":      close_side,
        "sz":          sz,
        "px":          limit_px,
        "order_type":  {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "tp"}},
        "reduce_only": True,
    }


def _get_open_orders_by_type(info, wallet_address, symbol):
    """Retourne les ordres SL et TP actifs sur Hyperliquid pour un symbole.
    Classe les ordres en deux listes : sl_oids et tp_oids.
    """
    sl_oids, tp_oids = [], []
    try:
        open_orders = info.open_orders(wallet_address)
        for o in open_orders:
            if o.get("coin") != symbol or not o.get("reduceOnly", False):
                continue
            otype = o.get("orderType", "").lower()
            oid   = o["oid"]
            tpsl  = o.get("tpsl", "").lower()
            if tpsl == "sl" or "stop" in otype:
                sl_oids.append(oid)
            elif tpsl == "tp" or "take profit" in otype or "tp" in otype:
                tp_oids.append(oid)
    except Exception as e:
        print(f"[ORDERS] Erreur lecture ordres {symbol} : {e}")
    return sl_oids, tp_oids


def update_sl_on_hyperliquid(exchange, info, wallet_address, symbol, position, new_sl, cfg):
    """Met a jour le Stop Loss sur Hyperliquid pour le trailing SL.
    Supporte les PERP et le SPOT (si l asset ID est connu dans SPOT_SL_ASSET_MAP).
    Strategie ATOMIQUE : pose le nouveau SL en premier, annule l ancien ensuite.
    """
    # Pour le spot, verifier que l asset ID est resolvable
    if is_spot(symbol, cfg) and _spot_sl_asset(symbol, cfg) is None:
        print(f"[TSL] {symbol} spot : asset ID inconnu — SL gere en interne uniquement")
        return

    try:
        # ── ETAPE 1 : construire et poser le NOUVEAU SL ──────────────────────
        new_sl_order = _build_sl_order(symbol, position, new_sl, cfg)
        if new_sl_order is None:
            print(f"[TSL] {symbol} : impossible de construire l ordre SL")
            return

        result = exchange.bulk_orders([new_sl_order], grouping="na")
        new_sl_ok = result and result.get("status") == "ok"

        if not new_sl_ok:
            print(f"[TSL] ECHEC pose nouveau SL {symbol} @ {new_sl:.2f} — ancien SL conserve")
            return

        # ── ETAPE 2 : annuler les ANCIENS SL seulement si le nouveau est confirme ─
        sl_oids, _ = _get_open_orders_by_type(info, wallet_address, symbol)
        if sl_oids:
            cancels = [{"coin": symbol, "oid": oid} for oid in sl_oids]
            exchange.bulk_cancel(cancels)

    except Exception as e:
        print(f"[TSL] Erreur mise a jour SL Hyperliquid {symbol} : {e}")


def cancel_tp_on_hyperliquid(exchange, info, wallet_address, symbol, cfg):
    """Annule le TP fixe sur Hyperliquid quand le Trailing TP s active.
    Supporte les PERP et le SPOT.
    """
    try:
        _, tp_oids = _get_open_orders_by_type(info, wallet_address, symbol)
        if tp_oids:
            cancels = [{"coin": symbol, "oid": oid} for oid in tp_oids]
            exchange.bulk_cancel(cancels)
            print(f"[TTP] TP fixe annule sur Hyperliquid pour {symbol} ({len(tp_oids)} ordre(s))")
        else:
            print(f"[TTP] Aucun TP fixe actif sur Hyperliquid pour {symbol}")
    except Exception as e:
        print(f"[TTP] Erreur annulation TP {symbol} : {e}")


def ensure_sl_on_hyperliquid(exchange, info, wallet_address, symbol, position, cfg):
    """Verifie qu un SL actif existe sur Hyperliquid pour une position donnee.
    Si aucun SL n est detecte, en repose un immediatement.
    Supporte les PERP et le SPOT (si l asset ID est resolvable).
    """
    if is_spot(symbol, cfg) and _spot_sl_asset(symbol, cfg) is None:
        print(f"[GUARD] {symbol} spot : asset ID inconnu — SL natif impossible, protection interne uniquement")
        return

    try:
        sl_oids, _ = _get_open_orders_by_type(info, wallet_address, symbol)

        if sl_oids:
            print(f"[GUARD] {symbol} : SL actif detecte ({len(sl_oids)} ordre(s)) — OK")
            return

        # Aucun SL detecte — reposer un SL de secours immediatement
        rescue_sl = _build_sl_order(symbol, position, position["sl"], cfg)
        if rescue_sl is None:
            print(f"[GUARD] {symbol} : impossible de construire le SL de secours")
            return

        result = exchange.bulk_orders([rescue_sl], grouping="na")
        ok = result and result.get("status") == "ok"
        status = "POSE" if ok else "ECHEC"
        print(f"[GUARD] {symbol} : SL manquant — SL de secours {status} @ ${position['sl']:.2f}")

    except Exception as e:
        print(f"[GUARD] Erreur verification SL {symbol} : {e}")

# ─────────────────────────────────────────────
#  PLAGE HORAIRE
# ─────────────────────────────────────────────
def is_trading_hours(cfg):
    start = cfg["TRADE_HOUR_START"]
    end   = cfg["TRADE_HOUR_END"]
    if start == 0 and end == 24:
        return True
    from datetime import datetime as _dt, timezone, timedelta
    now_utc = _dt.now(timezone.utc)
    month = now_utc.month
    paris_offset = 2 if 3 < month < 10 else 1
    if month == 3 and now_utc.day >= 25:
        paris_offset = 2
    elif month == 10 and now_utc.day >= 25:
        paris_offset = 1
    hour = (now_utc + timedelta(hours=paris_offset)).hour
    return start <= hour < end

def is_forex_open():
    """Vérifie si le marché Forex/Or est ouvert.
    Ouvert : Lundi 00h01 — Vendredi 22h00 (heure Paris)
    Fermé  : Vendredi 22h00 — Lundi 00h01 + chaque nuit 22h-00h01
    Calcul basé sur UTC pour éviter les problèmes de changement d'heure.
    """
    from datetime import datetime as _dt, timezone, timedelta
    now_utc = _dt.now(timezone.utc)
    month = now_utc.month
    paris_offset = 2 if 3 < month < 10 else 1
    if month == 3 and now_utc.day >= 25:
        paris_offset = 2
    elif month == 10 and now_utc.day >= 25:
        paris_offset = 1
    now = now_utc + timedelta(hours=paris_offset)

    weekday = now.weekday()  # 0=Lundi, 4=Vendredi, 5=Samedi, 6=Dimanche
    hour = now.hour
    minute = now.minute

    if weekday == 5:   # Samedi
        return False
    if weekday == 6:   # Dimanche
        return False
    if weekday == 4 and hour >= 22:
        return False
    if hour == 22 or hour == 23:
        return False
    if hour == 0 and minute == 0:
        return False
    return True

def is_crypto_offpeak(cfg):
    """Heures creuses du marche crypto — periode de liquidite generalement
    la plus faible (fin de session US / debut de session asiatique), calculee
    en UTC (le marche crypto est mondial et 24/7, contrairement au Forex/PAXG
    deja gere separement via FOREX_SYMBOLS).
    Fenetre par defaut : 02h00-06h00 UTC (consensus general de marche) —
    ajustable via CRYPTO_OFFPEAK_HOUR_START_UTC / _END_UTC dans CONFIG.
    Ne bloque que les NOUVELLES entrees ; les positions ouvertes continuent
    d etre gerees normalement.
    """
    from datetime import datetime as _dt, timezone
    start = cfg.get("CRYPTO_OFFPEAK_HOUR_START_UTC", 2)
    end   = cfg.get("CRYPTO_OFFPEAK_HOUR_END_UTC", 6)
    hour  = _dt.now(timezone.utc).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # plage qui traverse minuit UTC

def fetch_cpi_events_from_finnhub(api_key):
    """Recupere les prochaines annonces CPI US depuis l API Economic Calendar
    de Finnhub (https://finnhub.io/docs/api/economic-calendar).
    Retourne une liste de datetime UTC (triee), ou [] en cas d echec/cle absente.
    Necessite une cle Finnhub valide (voir HYPERBOT_FINNHUB_API_KEY).
    Note : l acces a cet endpoint peut necessiter un abonnement Finnhub payant
    selon les conditions actuelles de l API — en cas d echec, le bot continue
    de trader normalement (pas de blackout CPI applique) et logue l erreur.
    """
    if not api_key:
        return []
    import urllib.request, json
    from datetime import datetime as _dt, timezone, timedelta
    try:
        today = _dt.now(timezone.utc).date()
        frm = (today - timedelta(days=1)).isoformat()
        to  = (today + timedelta(days=35)).isoformat()
        url = f"https://finnhub.io/api/v1/calendar/economic?from={frm}&to={to}&token={api_key}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        events = data.get("economicCalendar") or data.get("data") or []
        cpi_dates = []
        for ev in events:
            name    = (ev.get("event") or "").upper()
            country = (ev.get("country") or "").upper()
            if "CPI" not in name and "CONSUMER PRICE" not in name:
                continue
            if country not in ("US", "USA", ""):
                continue
            raw = ev.get("time") or ev.get("date") or ""
            dt_val = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt_val = _dt.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if dt_val:
                cpi_dates.append(dt_val)
        return sorted(cpi_dates)
    except Exception as e:
        print(f"[CPI] Erreur recuperation calendrier Finnhub : {e}")
        return []

# ─────────────────────────────────────────────
#  STATE PAR SYMBOLE
# ─────────────────────────────────────────────
class SymbolState:
    def __init__(self):
        self.position      = None
        self.price_history = deque(maxlen=500)
        self.vol_history   = deque(maxlen=50)
        self.trades        = 0
        self.wins          = 0
        self.pnl           = 0.0
        self.closed_trades = []
        self.current_price = 0.0
        self.current_rsi   = None
        self.current_macd  = None
        self.current_atr_pct = None   # ATR% du dernier cycle — pour affichage dashboard
        self._last_status_log_ts = None  # limite la frequence du log "latent" (voir _manage_position_impl)
        self.current_sig   = None
        self.prev_macd     = None
        self.prev_sig      = None
        self.collecting    = True
        self.peak_price    = None
        self.trailing_tp_active = False
        self.peak_pnl_usd  = None   # Pic de PnL latent en $ (etage 2 du Trailing TP)
        self.tp_stage      = 0      # 0=inactif, 1=Quick Profit arme, 2=Trailing illimite
        self.mtf_prices    = deque(maxlen=200)
        self.forex_was_open  = None
        self.forex_reopen_time = None
        self.last_price_time = None   # Horodatage du dernier prix enregistre
        self.prev_ema_s      = None   # EMA courte du cycle precedent (detection pivot)
        self.prev_ema_l      = None   # EMA longue du cycle precedent (detection pivot)
        self.consec_bull     = 0      # Nombre de cycles consecutifs haussiers (EMA bull)
        self.consec_bear     = 0      # Nombre de cycles consecutifs baissiers (EMA bear)

    def reset_indicators(self):
        """Reinitialise les donnees de prix et indicateurs techniques.
        Appele a la reactivation d une paire si les donnees sont trop anciennes.
        Preserve : trades, PnL, position ouverte, historique des trades fermes.
        """
        self.price_history  = deque(maxlen=500)
        self.vol_history    = deque(maxlen=50)
        self.mtf_prices     = deque(maxlen=200)
        self.current_rsi    = None
        self.current_macd   = None
        self.current_sig    = None
        self.prev_macd      = None
        self.prev_sig       = None
        self.collecting     = True
        self.last_price_time = None
        self.forex_was_open  = None
        self.prev_ema_s      = None
        self.prev_ema_l      = None
        self.consec_bull     = 0
        self.consec_bear     = 0

    def open_position(self, ptype, entry, sl, tp, size, confidence=None):
        self.position = {
            "type": ptype, "entry": entry, "sl": sl, "tp": tp,
            "size": size, "opened_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "confidence": confidence,
        }
        self.peak_price = entry
        self.trailing_tp_active = False
        self.peak_pnl_usd = None
        self.tp_stage = 0

    def trades_last_24h(self):
        cutoff = datetime.now().timestamp() - 86400
        recent = [t for t in self.closed_trades if t.get("ts", 0) >= cutoff]
        count = len(recent)
        wins = sum(1 for t in recent if t["win"])
        pnl = sum(t["pnl"] for t in recent)
        wr = wins / count * 100 if count > 0 else 0.0
        return {"trades": count, "wins": wins, "pnl": pnl, "win_rate": wr}

    def update_trailing_stop(self, price, delta_pct):
        if not self.position:
            return
        pos = self.position
        delta = delta_pct / 100
        if pos["type"] == "long":
            if price > self.peak_price:
                self.peak_price = price
                new_sl = self.peak_price * (1 - delta)
                if new_sl > pos["sl"]:
                    pos["sl"] = new_sl
        elif pos["type"] == "short":
            if price < self.peak_price:
                self.peak_price = price
                new_sl = self.peak_price * (1 + delta)
                if new_sl < pos["sl"]:
                    pos["sl"] = new_sl

    def close_position(self, exit_price, reason):
        p = self.position
        if p["type"] == "long":
            pnl_pct = (exit_price - p["entry"]) / p["entry"] * 100
        else:
            pnl_pct = (p["entry"] - exit_price) / p["entry"] * 100
        pnl_usd = p["size"] * pnl_pct / 100
        self.pnl    += pnl_usd
        self.trades += 1
        win = pnl_usd > 0
        if win:
            self.wins += 1
        trade = {
            "time": datetime.now().strftime("%H:%M:%S"), "symbol": "",
            "type": p["type"], "entry": p["entry"], "exit": exit_price,
            "pnl": pnl_usd, "reason": reason, "win": win,
            "ts": datetime.now().timestamp(),
        }
        self.closed_trades.append(trade)
        self.position = None
        self.peak_price = None
        self.trailing_tp_active = False
        self.peak_pnl_usd = None
        self.tp_stage = 0
        return pnl_usd, win, trade

    def win_rate(self):
        return 0.0 if self.trades == 0 else self.wins / self.trades * 100

# ─────────────────────────────────────────────
#  CAPITAL PERSISTANT — INTERETS COMPOSES
# ─────────────────────────────────────────────
# Fichiers de persistance specifiques au profil (swing/scalp)
# Evite les conflits d ecriture quand 2 instances tournent dans le meme dossier
_PROFILE_SUFFIX = CONFIG.get("PROFILE", "swing")
CAPITAL_FILE = f"hyperbot_capital_{_PROFILE_SUFFIX}.json"
STATE_FILE   = f"hyperbot_session_state_{_PROFILE_SUFFIX}.json"
LOG_FILE     = f"hyperbot_log_{_PROFILE_SUFFIX}.txt"

def write_log(msg, level="info"):
    """Ecrit un message dans le fichier de log avec horodatage.
    Conservation : 24 dernières heures. Epuration automatique si > 500KB.
    Format : 2026-06-14 23:11:26 [LEVEL] message
    """
    from datetime import datetime as _dt, timedelta as _td
    import os
    try:
        timestamp = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} [{level.upper():8s}] {msg}\n"

        # Ajouter la ligne immediatement (leger et rapide)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)

        # Epuration 24h — declenchee si > 500KB (~6h de logs)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 500 * 1024:
            cutoff = _dt.now() - _td(hours=24)
            kept = []
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                for l in f:
                    try:
                        line_date = _dt.strptime(l[:19], "%Y-%m-%d %H:%M:%S")
                        if line_date >= cutoff:
                            kept.append(l)
                    except Exception:
                        kept.append(l)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(kept)

    except Exception:
        pass  # Ne jamais bloquer le bot pour un log

def save_session_state(mode, profile, symbols, active_slots, running):
    """Sauvegarde l etat courant de la session (mode, profil, slots, statut).
    Permet un redemarrage automatique dans le meme etat apres coupure.
    Ecriture atomique (tmp + replace) pour eviter les conflits Windows.
    """
    import json, os
    data = {
        "mode":         mode,
        "profile":      profile,
        "symbols":      symbols,
        "active_slots": sorted(list(active_slots)),
        "running":      running,
    }
    tmp_file = STATE_FILE + ".tmp"
    import time
    for attempt in range(3):
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, STATE_FILE)
            return
        except PermissionError as e:
            # Fichier verrouille temporairement (OneDrive/Defender scan en cours)
            # Reessai avec micro-pause — la sauvegarde suivante (60s) reessaiera aussi
            if attempt < 2:
                time.sleep(0.15)
                continue
            print(f"[STATE] Permission refusee apres 3 essais — ignore ({e})")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
        except Exception as e:
            print(f"[STATE] Erreur sauvegarde: {e}")
            return

def load_session_state():
    """Charge l etat de la derniere session, ou None si absent/invalide."""
    import json, os
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"[STATE] Erreur lecture: {e} — etat ignore")
        return None


def load_capital(default_capital):
    """Charge le capital depuis le fichier de sauvegarde.
    Si le fichier n'existe pas, utilise le capital par defaut de la config.
    """
    import json, os
    if os.path.exists(CAPITAL_FILE):
        try:
            with open(CAPITAL_FILE, "r") as f:
                data = json.load(f)
            capital = data.get("capital", default_capital)
            sessions = data.get("sessions", 0)
            total_pnl = data.get("total_pnl", 0.0)
            print(f"[CAPITAL] Capital charge: ${capital:.2f} | Sessions: {sessions} | PnL total: ${total_pnl:.2f}")
            return capital, sessions, total_pnl
        except Exception as e:
            print(f"[CAPITAL] Erreur lecture fichier: {e} — capital par defaut utilise")
    return default_capital, 0, 0.0

def save_capital(capital, sessions, total_pnl):
    """Sauvegarde le capital apres chaque session.
    Utilise un fichier temporaire + remplacement atomique pour eviter
    les erreurs de permission Windows (fichier verrouille par un autre processus
    ou marque lecture seule).
    """
    import json, os
    from datetime import datetime as _dt
    data = {
        "capital":    round(capital, 4),
        "sessions":   sessions,
        "total_pnl":  round(total_pnl, 4),
        "last_saved": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp_file = CAPITAL_FILE + ".tmp"
    import time
    for attempt in range(3):
        try:
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, CAPITAL_FILE)
            return
        except PermissionError as e:
            if attempt < 2:
                time.sleep(0.15)
                continue
            print(f"[CAPITAL] Permission refusee apres 3 essais — ignore ({e})")
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
        except Exception as e:
            print(f"[CAPITAL] Erreur sauvegarde: {e}")
            return


# ─────────────────────────────────────────────
class BotEngine:
    def __init__(self, cfg, event_queue):
        self.cfg = cfg
        self.q = event_queue
        self.running = False
        # Sauvegarder les noms originaux (vrais tickers sans suffixe)
        # Normaliser : si cfg["SYMBOLS"] contient deja des slot_keys, les extraire
        raw_symbols = [ticker_from_slot_key(s) for s in cfg["SYMBOLS"]]
        self._original_symbols = raw_symbols[:]  # ["BTC", "PAXG", "SOL"]

        # Construire les slot_keys proprement : "BTC_0", "PAXG_1", "SOL_2"
        slot_keys = [f"{s}_{i}" for i, s in enumerate(raw_symbols)]
        self.states = {k: SymbolState() for k in slot_keys}
        cfg["SYMBOLS"] = slot_keys
        self._all_symbols = slot_keys[:]
        # Chargement capital persistant — interets composes
        self.capital, self.sessions, self.total_pnl_all = load_capital(cfg["CAPITAL_USD"])
        self.cfg["CAPITAL_USD"] = self.capital
        self.cycle = 0
        self.info = None
        self.exchange = None
        # Fix v3.1 : evite qu un stop() sans start() prealable (ex: fermeture
        # de l app sans jamais avoir clique DEMARRER) n incremente le compteur
        # de sessions et ne resauvegarde le capital pour rien.
        self._started = False
        # Confiance minimale dynamique par actif (ticker -> seuil %)
        # Absent de ce dict = utilise CONFIDENCE_MIN_PCT (seuil de base)
        self.confidence_thresholds = {}
        # v3.2 — Session de trading limitee a 23h45/24h (prudence live) :
        # session_started_at = debut de la session en cours ; trading_blocked
        # = True des que 23h45 sont ecoulees, jusqu a ce que toutes les
        # positions de la session soient fermees.
        self.session_started_at = None
        self.trading_blocked = False
        # Cache du calendrier CPI Finnhub (evite d appeler l API a chaque cycle)
        self._cpi_events = []
        self._cpi_last_fetch = None
        # ── v3.1 : surveillance temps reel des positions via WebSocket ───────
        # self.lock protege les mutations d etat (position, pnl, ...) car
        # _manage_position peut desormais etre appele soit depuis le thread
        # du cycle (_run, toutes les CYCLE_INTERVAL sec) soit depuis le thread
        # WebSocket du SDK Hyperliquid (_on_ws_allmids, a chaque tick de prix).
        self.lock = threading.Lock()
        self._ws_subscribed = False
        self._last_ws_tick = None   # timestamp (time.time()) du dernier tick allMids recu
        self._ws_was_healthy = None  # None = pas encore evalue ; sert a detecter les transitions
        self.all_mids = {}  # v3.2 : cache brut de tous les prix Hyperliquid (affichage marche complet)

    # ── Sauvegarde des positions ouvertes pour reconciliation au redemarrage ──
    POSITIONS_FILE = "hyperbot_positions.json"
    CONFIDENCE_FILE = "hyperbot_confidence.json"
    INDICATOR_STATE_FILE = "hyperbot_indicators.json"
    TRADING_SESSION_FILE = "hyperbot_trading_session.json"
    INDICATOR_RESUME_MAX_GAP_SEC = 90  # au-dela, on repart en collecte fraiche

    def _save_open_positions(self):
        """Sauvegarde les positions ouvertes (live ET paper depuis v3.2, pour
        survivre a un redemarrage/redeploiement) — indexe par slot_key (ex:
        "BTC_0"), pas par ticker brut, pour eviter toute ambiguite si un
        meme ticker occupait plusieurs emplacements.
        v3.2 — FIX CRITIQUE : sauvegarde aussi l etat du Trailing TP (pic de
        profit atteint, etage Quick Profit/Trailing) — sans ca, un
        redemarrage faisait "oublier" au bot qu une position avait deja
        depasse son pic, lui faisant reprendre une reference basse et rater
        la fermeture qui aurait du se produire (perte de l avantage acquis)."""
        import json, os
        positions = {}
        for sym, st in self.states.items():
            if st.position:
                snapshot = dict(st.position)
                snapshot["_peak_pnl_usd"] = st.peak_pnl_usd
                snapshot["_tp_stage"] = st.tp_stage
                snapshot["_trailing_tp_active"] = st.trailing_tp_active
                positions[sym] = snapshot
        try:
            with open(self.POSITIONS_FILE, "w") as f:
                json.dump(positions, f, indent=2)
            print(f"[POSITIONS] Sauvegarde OK : {len(positions)} position(s) -> {os.path.abspath(self.POSITIONS_FILE)} (coins: {[ticker_from_slot_key(k) for k in positions]})")
        except Exception as e:
            print(f"[POSITIONS] ERREUR sauvegarde : {e}")

    def _load_saved_positions(self):
        """Charge les positions sauvegardees lors de la derniere session live."""
        import json, os
        abspath = os.path.abspath(self.POSITIONS_FILE)
        if not os.path.exists(self.POSITIONS_FILE):
            print(f"[POSITIONS] Aucun fichier trouve a {abspath} — rien a restaurer.")
            return {}
        try:
            with open(self.POSITIONS_FILE, "r") as f:
                data = json.load(f)
            print(f"[POSITIONS] Chargement OK depuis {abspath} : {len(data)} position(s) trouvee(s) (coins: {[ticker_from_slot_key(k) for k in data]})")
            # Vider le fichier apres lecture pour eviter double reconciliation
            with open(self.POSITIONS_FILE, "w") as f:
                json.dump({}, f)
            return data
        except Exception as e:
            print(f"[POSITIONS] ERREUR lecture : {e}")
            return {}

    def _save_confidence_thresholds(self):
        """Sauvegarde les seuils de confiance dynamiques par actif (survit aux
        redemarrages/redeploiements) — sans ca, un Max Loss qui avait rendu
        un actif plus exigeant serait oublie au prochain demarrage, comme si
        de rien n etait."""
        import json
        try:
            with open(self.CONFIDENCE_FILE, "w") as f:
                json.dump(self.confidence_thresholds, f, indent=2)
        except Exception as e:
            print(f"[CONFIANCE] Erreur sauvegarde : {e}")

    def _load_confidence_thresholds(self):
        """Restaure les seuils de confiance dynamiques par actif au demarrage."""
        import json, os
        if not os.path.exists(self.CONFIDENCE_FILE):
            return
        try:
            with open(self.CONFIDENCE_FILE, "r") as f:
                data = json.load(f)
            self.confidence_thresholds = {k: float(v) for k, v in data.items()}
            if self.confidence_thresholds:
                print(f"[CONFIANCE] Seuils restaures : {self.confidence_thresholds}")
        except Exception as e:
            print(f"[CONFIANCE] Erreur lecture : {e}")

    def _save_indicator_state(self):
        """Sauvegarde l etat des indicateurs (prix collectes, compteurs de
        cycles consecutifs, etc.) avec un horodatage precis. Ne sera restaure
        au demarrage QUE si l arret a dure moins de
        INDICATOR_RESUME_MAX_GAP_SEC secondes (voir _load_indicator_state) —
        au-dela, une collecte fraiche est toujours preferable (un trou de
        donnees trop long fausserait les indicateurs)."""
        import json
        try:
            snapshot = {}
            for slot_key, st in self.states.items():
                snapshot[slot_key] = {
                    "price_history": list(st.price_history),
                    "vol_history": list(st.vol_history),
                    "mtf_prices": list(st.mtf_prices),
                    "collecting": st.collecting,
                    "consec_bull": st.consec_bull,
                    "consec_bear": st.consec_bear,
                    "prev_ema_s": st.prev_ema_s,
                    "prev_ema_l": st.prev_ema_l,
                    "prev_macd": st.prev_macd,
                    "prev_sig": st.prev_sig,
                }
            payload = {"saved_at": datetime.now(timezone.utc).isoformat(), "states": snapshot}
            with open(self.INDICATOR_STATE_FILE, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            print(f"[INDICATEURS] Erreur sauvegarde : {e}")

    def _load_indicator_state_if_recent(self):
        """Restaure l etat des indicateurs UNIQUEMENT si le fichier a ete
        sauvegarde il y a moins de INDICATOR_RESUME_MAX_GAP_SEC secondes —
        evite de reprendre une collecte avec un trou de donnees trop
        important (fausserait RSI/EMA/MACD/ATR)."""
        import json, os
        if not os.path.exists(self.INDICATOR_STATE_FILE):
            print("[INDICATEURS] Aucun etat sauvegarde trouve — collecte fraiche.")
            return
        try:
            with open(self.INDICATOR_STATE_FILE, "r") as f:
                payload = json.load(f)
            saved_at = datetime.fromisoformat(payload["saved_at"])
            gap = (datetime.now(timezone.utc) - saved_at).total_seconds()
            if gap > self.INDICATOR_RESUME_MAX_GAP_SEC:
                print(f"[INDICATEURS] Etat sauvegarde il y a {gap:.0f}s (> {self.INDICATOR_RESUME_MAX_GAP_SEC}s) — collecte fraiche.")
                return
            restored = 0
            for slot_key, data in payload.get("states", {}).items():
                st = self.states.get(slot_key)
                if not st:
                    continue
                st.price_history = deque(data.get("price_history", []), maxlen=500)
                st.vol_history = deque(data.get("vol_history", []), maxlen=50)
                st.mtf_prices = deque(data.get("mtf_prices", []), maxlen=200)
                st.collecting = data.get("collecting", True)
                st.consec_bull = data.get("consec_bull", 0)
                st.consec_bear = data.get("consec_bear", 0)
                st.prev_ema_s = data.get("prev_ema_s")
                st.prev_ema_l = data.get("prev_ema_l")
                st.prev_macd = data.get("prev_macd")
                st.prev_sig = data.get("prev_sig")
                restored += 1
            print(f"[INDICATEURS] Etat restaure ({gap:.0f}s d arret) : {restored} actif(s), collecte NON relancee.")
            self.emit("log", {"msg": f"⏩ Reprise rapide : etat des indicateurs restaure ({gap:.0f}s d arret, < {self.INDICATOR_RESUME_MAX_GAP_SEC}s) — pas de nouvelle collecte necessaire.", "level": "ok"})
        except Exception as e:
            print(f"[INDICATEURS] Erreur lecture, collecte fraiche par securite : {e}")

    def _save_trading_session(self):
        import json
        try:
            payload = {
                "session_started_at": self.session_started_at.isoformat() if self.session_started_at else None,
                "trading_blocked": self.trading_blocked,
            }
            with open(self.TRADING_SESSION_FILE, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            print(f"[SESSION] Erreur sauvegarde : {e}")

    def _load_trading_session(self):
        import json, os
        if not os.path.exists(self.TRADING_SESSION_FILE):
            return
        try:
            with open(self.TRADING_SESSION_FILE, "r") as f:
                payload = json.load(f)
            if payload.get("session_started_at"):
                self.session_started_at = datetime.fromisoformat(payload["session_started_at"])
            self.trading_blocked = payload.get("trading_blocked", False)
            print(f"[SESSION] Restauree : debut={self.session_started_at}, bloquee={self.trading_blocked}")
        except Exception as e:
            print(f"[SESSION] Erreur lecture : {e}")

    def _update_trading_session(self):
        """v3.2 — Prudence live : verifie/actualise l etat de la session de
        trading 24h. Appelee une fois par cycle. N affecte JAMAIS la gestion
        des positions deja ouvertes — seulement l ouverture de nouvelles."""
        now = datetime.now(timezone.utc)
        if self.session_started_at is None:
            self.session_started_at = now
            self._save_trading_session()
            return

        max_hours = self.cfg.get("SESSION_MAX_HOURS", 23.75)
        elapsed_hours = (now - self.session_started_at).total_seconds() / 3600

        if not self.trading_blocked and elapsed_hours >= max_hours:
            self.trading_blocked = True
            self._save_trading_session()
            open_count = sum(1 for st in self.states.values() if st.position)
            self.emit("log", {
                "msg": f"⏸️ SESSION DE TRADING TERMINEE ({elapsed_hours:.1f}h/{max_hours}h) — nouvelles entrees suspendues jusqu a la fermeture des {open_count} position(s) en cours.",
                "level": "warn"
            })

        elif self.trading_blocked:
            open_count = sum(1 for st in self.states.values() if st.position)
            if open_count == 0:
                self.session_started_at = now
                self.trading_blocked = False
                self._save_trading_session()
                self.emit("log", {"msg": "▶️ Toutes les positions de la session precedente sont fermees — nouvelle session de trading (24h) demarree.", "level": "ok"})

    def force_fresh_collection(self):
        """Force une collecte entierement fraiche pour tous les actifs,
        meme si un etat recent aurait pu etre restaure — utile si on
        soupconne un probleme sur les indicateurs sans attendre un
        redeploiement. Ne touche pas aux positions ouvertes ni au PnL."""
        import os
        for st in self.states.values():
            st.reset_indicators()
        try:
            if os.path.exists(self.INDICATOR_STATE_FILE):
                os.remove(self.INDICATOR_STATE_FILE)
        except Exception as e:
            print(f"[INDICATEURS] Erreur suppression fichier lors du forcage : {e}")

    def clear_all_persisted_files(self):
        """v3.2 — FIX CRITIQUE : supprime les fichiers de sauvegarde sur
        disque (positions, seuils de confiance, etat des indicateurs).
        Sans cet appel, une reinitialisation depuis l interface (base de
        donnees + memoire vidées) ne servait a rien : au prochain
        demarrage, le bot relisait ces fichiers restes intacts et
        restaurait les anciennes positions comme si de rien n etait."""
        import os
        for f in (self.POSITIONS_FILE, self.CONFIDENCE_FILE, self.INDICATOR_STATE_FILE, self.TRADING_SESSION_FILE):
            try:
                if os.path.exists(f):
                    os.remove(f)
                    print(f"[RESET] Fichier supprime : {f}")
            except Exception as e:
                print(f"[RESET] Erreur suppression {f} : {e}")
        self.confidence_thresholds = {}
        self.session_started_at = None
        self.trading_blocked = False
        self.emit("log", {"msg": "🔄 Fichiers de sauvegarde (positions, confiance, indicateurs, session) purges suite a une reinitialisation.", "level": "warn"})

    def emit(self, etype, data=None):
        self.q.put({"type": etype, "data": data or {}})
        # Sauvegarde simultanee dans le fichier de log
        if etype == "log" and data and "msg" in data:
            write_log(data["msg"], data.get("level", "info"))

    def start(self):
        self.running = True
        self._started = True
        # Marquer le debut de session dans le fichier log
        from datetime import datetime as _dt
        write_log(f"{'='*60}", "info")
        write_log(f"DEMARRAGE SESSION — v{BOT_VERSION} (build {BOT_BUILD}) | mode={self.cfg.get('MODE')} | profil={self.cfg.get('PROFILE')}", "info")
        write_log(f"Actifs : {[s for s in self.cfg.get('SYMBOLS', [])]}", "info")
        write_log(f"{'='*60}", "info")
        threading.Thread(target=self._run, daemon=True).start()
        # v3.2 : sauvegarde dediee toutes les 5s, decouplee du cycle de trading
        # (15s) — reduit la fenetre de "fraicheur perdue" en cas de coupure
        # brutale (redeploiement, crash) sans avoir a acce le rythme du cycle
        # de trading lui-meme. Cout negligeable (petit fichier JSON local).
        threading.Thread(target=self._periodic_save_loop, daemon=True).start()

    def _periodic_save_loop(self):
        while self.running:
            time.sleep(5)
            if not self.running:
                break
            try:
                self._save_open_positions()
                self._save_confidence_thresholds()
            except Exception as e:
                print(f"[SAVE-5S] Erreur : {e}")

    def stop(self):
        self.running = False
        # Sauvegarde l etat des indicateurs AVANT toute autre chose, avec un
        # horodatage precis a l instant de l arret — c est ce moment precis
        # qui sert de reference pour la reprise rapide (< 90s) au prochain
        # demarrage.
        self._save_indicator_state()
        # Fermer proprement le WebSocket si actif, pour eviter d accumuler des
        # connexions/threads orphelins entre deux demarrages successifs.
        if self._ws_subscribed and self.info is not None:
            try:
                self.info.disconnect_websocket()
            except Exception as e:
                print(f"[WS] Deconnexion websocket : {e}")
            self._ws_subscribed = False
        if not self._started:
            # Jamais demarre (ex: fermeture de l app avant tout DEMARRER) —
            # rien a sauvegarder, evite de gonfler le compteur de sessions.
            return
        self._started = False
        # Sauvegarde du capital pour interets composes
        total_pnl = sum(s.pnl for s in self.states.values())
        new_capital = self.cfg["CAPITAL_USD"] + total_pnl
        self.sessions += 1
        self.total_pnl_all += total_pnl
        save_capital(new_capital, self.sessions, self.total_pnl_all)
        self.emit("log", {"msg": f"Capital sauvegarde: ${new_capital:.2f} (session #{self.sessions})", "level": "ok"})

    def _sim_prices(self):
        import random
        # Prix de base pour la simulation paper — etendu a tous les actifs courants
        base = {
            "BTC": 63500, "ETH": 1700, "SOL": 68, "BNB": 605,
            "PAXG": 4290, "XRP": 1.17, "DOGE": 0.09, "ADA": 0.17,
            "AVAX": 6.81, "LINK": 8.03, "DOT": 0.99, "UNI": 2.58,
            "NEAR": 2.24, "AAVE": 64.41, "LTC": 43.21, "BCH": 211.0,
            "HYPE": 64.57, "TAO": 217.0, "HBAR": 0.08,
        }
        return {
            k: base.get(ticker_from_slot_key(k), 10.0) * (1 + random.uniform(-0.005, 0.005))
            for k in self.cfg["SYMBOLS"]
        }

    def _send_snapshot(self):
        total_pnl = sum(s.pnl for s in self.states.values())
        # Stats 24h glissantes
        h24_trades = sum(s.trades_last_24h()["trades"] for s in self.states.values())
        h24_pnl    = sum(s.trades_last_24h()["pnl"]    for s in self.states.values())
        h24_wins   = sum(s.trades_last_24h()["wins"]   for s in self.states.values())
        h24_wr     = h24_wins / h24_trades * 100 if h24_trades > 0 else 0.0
        self.emit("snapshot", {
            "cycle": self.cycle,
            "capital": self.capital + total_pnl,
            "total_pnl": total_pnl,
            "in_hours": is_trading_hours(self.cfg),
            "ws_connected": self.info is not None,
            "ws_healthy": self._is_ws_healthy(),
            "h24": {"trades": h24_trades, "pnl": h24_pnl, "win_rate": h24_wr},
            "states": {
                sym: {
                    "price": st.current_price, "rsi": st.current_rsi,
                    "macd": st.current_macd, "pnl": st.pnl,
                    "trades": st.trades, "win_rate": st.win_rate(),
                    "position": st.position, "collecting": st.collecting,
                    "atr_pct": st.current_atr_pct,
                } for sym, st in self.states.items()
            }
        })

    # ── v3.1 : Score de confiance et seuil dynamique par actif ──────────────
    def _score_confidence(self, direction, macd_bull, macd_bear, bb_low_ok, bb_up_ok,
                           vol_ok, ema200, ema_mid, price, momentum_pct,
                           momentum_threshold, consec, min_consec, cfg):
        """Calcule un score de confiance 0-100% a partir des confirmations
        optionnelles disponibles pour ce signal (en plus des filtres deja
        obligatoires comme RSI/EMA/tendance qui ont deja valide avant d arriver
        ici). Seuls les indicateurs reellement calculables pour ce cycle/symbole
        entrent dans le score : celui-ci est ramene sur 100% du poids
        REELLEMENT disponible, pas du poids total theorique. Ainsi un actif
        sans EMA_MID configure n est pas penalise pour un indicateur absent.
        """
        w = cfg.get("CONFIDENCE_WEIGHTS", {})
        earned, available = 0.0, 0.0

        def add(key, confirmed):
            nonlocal earned, available
            pts = w.get(key, 0)
            available += pts
            if confirmed:
                earned += pts

        add("macd", macd_bull if direction == "long" else macd_bear)
        add("bollinger", bb_low_ok if direction == "long" else bb_up_ok)
        add("volume", vol_ok)

        if ema200 is not None:
            add("ema200", price > ema200 if direction == "long" else price < ema200)
        if ema_mid is not None:
            add("ema_mid", price > ema_mid if direction == "long" else price < ema_mid)
        if momentum_pct is not None:
            # Confirmation franche : momentum au-dela de la moitie du seuil de blocage,
            # dans le sens du signal (pas juste "non oppose")
            half = momentum_threshold / 2
            add("momentum", momentum_pct >= half if direction == "long" else momentum_pct <= -half)

        add("consec", consec > min_consec)

        if available <= 0:
            return 100.0  # aucun critere optionnel disponible -> ne bloque pas artificiellement
        return earned / available * 100

    def _get_confidence_threshold(self, ticker):
        base = self.cfg.get("CONFIDENCE_MIN_PCT", 65.0)
        return self.confidence_thresholds.get(ticker, base)

    def _gate_active_or_auto_activate(self, ticker, confidence, direction):
        """Decide si un signal valide (confiance deja >= seuil normal) peut
        reellement s executer, en fonction de la selection ACTIVE_COINS :
        - Si l actif est deja actif -> laisse passer normalement.
        - Si l actif est INACTIF mais que la confiance atteint le seuil
          d auto-activation (AUTO_ACTIVATE_CONFIDENCE_PCT, defaut 80%) ->
          l active automatiquement (persiste via evenement pour l API web),
          logue une alarme bien visible, et laisse le trade s executer.
        - Sinon (inactif, confiance insuffisante pour l auto-activation) ->
          bloque silencieusement (pas de bruit pour chaque actif inactif a
          chaque cycle).
        Retourne True si le trade peut s executer, False sinon.
        """
        active_coins = self.cfg.get("ACTIVE_COINS")
        if active_coins is None or ticker in active_coins:
            return True  # pas de restriction, ou deja actif

        auto_threshold = self.cfg.get("AUTO_ACTIVATE_CONFIDENCE_PCT", 80.0)
        if confidence < auto_threshold:
            return False  # inactif et pas assez fort pour justifier une auto-activation

        # ── Auto-activation : opportunite trop belle pour la laisser passer ──
        new_list = list(active_coins) + [ticker]
        self.cfg["ACTIVE_COINS"] = new_list
        self.emit("log", {
            "msg": f"🚨 OPPORTUNITE FORTE [{ticker}] {direction.upper()} a {confidence:.0f}% de confiance (seuil auto-activation {auto_threshold:.0f}%) — actif AUTO-ACTIVE et trade en cours d execution.",
            "level": "warn"
        })
        # Persistance cote API web (bot_engine.py ne touche jamais directement
        # a la base de donnees, pour rester independant/portable).
        self.emit("active_coins_auto_added", {"ticker": ticker, "active_coins": new_list})
        return True

    def _register_max_loss(self, ticker, entry_confidence=None):
        """Apres un Max Loss / SL securite sur un actif, on releve son seuil
        de confiance minimum requis a : confiance qu avait CE trade a son
        entree + 5% (pas juste +5% sur le dernier seuil utilise) — plus un
        trade perdant avait ete pris avec confiance elevee, plus la barre
        remonte haut pour le prochain, jusqu au plafond CONFIDENCE_MAX_PCT.
        Si la confiance d entree n est pas disponible (positions recuperees
        apres un crash, anciennes positions), on se rabat sur l ancien
        comportement (+5% sur le seuil actuel).
        """
        base = self.cfg.get("CONFIDENCE_MIN_PCT", 65.0)
        step = self.cfg.get("CONFIDENCE_STEP_PCT", 5.0)
        cap  = self.cfg.get("CONFIDENCE_MAX_PCT", 90.0)
        current = self.confidence_thresholds.get(ticker, base)
        if entry_confidence is not None:
            new_threshold = min(entry_confidence + step, cap)
        else:
            new_threshold = min(current + step, cap)
        if new_threshold != current:
            self.confidence_thresholds[ticker] = new_threshold
            self.emit("log", {"msg": f"[{ticker}] Confiance minimale requise relevee a {new_threshold:.0f}% (confiance d entree {entry_confidence:.0f}% + {step:.0f}%)" if entry_confidence is not None else f"[{ticker}] Confiance minimale requise relevee a {new_threshold:.0f}% (apres perte)", "level": "warn"})
            self._save_confidence_thresholds()

    def _register_win(self, ticker):
        """Apres une sortie positive (Quick Profit ou Trailing TP), on rend
        IMMEDIATEMENT et INTEGRALEMENT la confiance de base a cet actif
        (reset complet, pas une simple decroissance de -5%) — un gain efface
        entierement la mefiance accumulee suite a d eventuelles pertes
        precedentes."""
        base = self.cfg.get("CONFIDENCE_MIN_PCT", 65.0)
        current = self.confidence_thresholds.get(ticker, base)
        if current > base:
            self.confidence_thresholds[ticker] = base
            self.emit("log", {"msg": f"[{ticker}] Confiance minimale requise reinitialisee a {base:.0f}% (apres gain Quick Profit/Trailing TP)", "level": "ok"})
            self._save_confidence_thresholds()

    # ── v3.1 : Blackout CPI (Finnhub) ────────────────────────────────────────
    def _refresh_cpi_events_if_needed(self):
        """Rafraichit le calendrier CPI depuis Finnhub au maximum toutes les
        CPI_CACHE_REFRESH_HOURS heures (evite de spammer l API a chaque cycle).
        En cas d echec, timeout ou cle absente, le cache reste vide et aucun
        blackout n est applique (le bot continue de trader normalement).
        v3.1 : l appel reseau tourne dans un thread separe avec timeout — un
        gel de ce bloc (observe en usage reel, coincidant exactement avec la
        toute premiere execution de ce code apres la collecte) ne doit plus
        jamais bloquer le cycle principal du bot.
        """
        from datetime import datetime as _dt, timezone
        now = _dt.now(timezone.utc)
        refresh_h = self.cfg.get("CPI_CACHE_REFRESH_HOURS", 12)
        if self._cpi_last_fetch is not None and (now - self._cpi_last_fetch).total_seconds() < refresh_h * 3600:
            return
        self._cpi_last_fetch = now
        api_key = self.cfg.get("FINNHUB_API_KEY", "")
        if not api_key:
            return
        result = {}
        def _worker():
            result["events"] = fetch_cpi_events_from_finnhub(api_key)
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=self.cfg.get("PRICE_FETCH_TIMEOUT_SEC", 10))
        if t.is_alive():
            self.emit("log", {"msg": "⚠ Calendrier CPI (Finnhub) : reponse trop lente — abandon, nouvel essai au prochain rafraichissement", "level": "warn"})
            return
        events = result.get("events", [])
        self._cpi_events = events
        if events:
            next_ev = events[0].strftime("%d/%m %H:%M UTC")
            self.emit("log", {"msg": f"Calendrier CPI (Finnhub) mis a jour — prochaine annonce : {next_ev}", "level": "info"})
        else:
            self.emit("log", {"msg": "Calendrier CPI (Finnhub) : aucune donnee recuperee — blackout CPI inactif", "level": "dim"})

    def _is_cpi_blackout(self):
        """Retourne (True, datetime_event) si l instant present tombe dans la
        fenetre de blackout autour d une annonce CPI, sinon (False, None)."""
        if not self._cpi_events:
            return False, None
        from datetime import datetime as _dt, timezone, timedelta
        now    = _dt.now(timezone.utc)
        before = timedelta(minutes=self.cfg.get("CPI_BLACKOUT_BEFORE_MIN", 15))
        after  = timedelta(minutes=self.cfg.get("CPI_BLACKOUT_AFTER_MIN", 30))
        for ev in self._cpi_events:
            if ev - before <= now <= ev + after:
                return True, ev
        return False, None

    def _on_ws_allmids(self, msg):
        """Callback WebSocket Hyperliquid — flux 'allMids' (prix mid de tous
        les actifs, mis a jour en temps reel par l exchange).
        Tourne dans le thread interne du SDK Hyperliquid (PAS le thread
        principal du bot _run) : des qu un prix arrive pour un actif ayant
        une position ouverte, on verifie IMMEDIATEMENT Max Loss / SL securite /
        Trailing TP via _manage_position (thread-safe grace a self.lock),
        sans attendre le prochain cycle de CYCLE_INTERVAL secondes.
        Format du message attendu : {"channel": "allMids", "data": {"mids": {...}}}
        (a verifier selon la version du SDK hyperliquid-python-sdk installee —
        cette integration n a pas pu etre testee en conditions reelles, faute
        d acces reseau dans l environnement de developpement).
        """
        try:
            data = msg.get("data", {}) if isinstance(msg, dict) else {}
            mids = data.get("mids", {})
            if not mids:
                return
            self._last_ws_tick = time.time()
            # v3.2 : cache BRUT de tous les prix recus (pas seulement nos
            # symboles tradés) — permet a l API web d afficher un marche
            # complet (jusqu a 30 cryptos) sans avoir besoin d ouvrir une
            # position sur chacun.
            self.all_mids = mids
            for slot_key, state in list(self.states.items()):
                if not state.position:
                    continue
                ticker = ticker_from_slot_key(slot_key)
                raw = mids.get(ticker)
                if raw is None:
                    continue
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                state.current_price = price
                self._manage_position(slot_key, price, state)
        except Exception as e:
            print(f"[WS] Erreur traitement flux allMids : {e}")

    def _is_ws_healthy(self):
        """True si le WebSocket est abonne ET a recu un tick recemment
        (moins de WS_STALE_AFTER_SEC secondes)."""
        stale_after = self.cfg.get("WS_STALE_AFTER_SEC", 20)
        return (
            self._ws_subscribed
            and self._last_ws_tick is not None
            and (time.time() - self._last_ws_tick) < stale_after
        )

    def _check_ws_health_alert(self):
        """ALARME WebSocket — appelee une fois par cycle depuis _run.
        Detecte les TRANSITIONS de sante (sain -> defaillant, defaillant ->
        retabli) et emet un log bien visible a chaque changement d etat
        (pas de spam a chaque cycle). C est ce log qui sert d alarme visible
        dans le panneau LOG EN DIRECT du dashboard (niveau 'error' = rouge).
        """
        if self.info is None:
            return  # pas de connexion Hyperliquid du tout (paper simule sans cle)
        healthy = self._is_ws_healthy()
        if self._ws_was_healthy is None:
            self._ws_was_healthy = healthy
            return
        if healthy == self._ws_was_healthy:
            return
        if healthy:
            self.emit("log", {"msg": "✅ WebSocket retabli — surveillance temps reel des positions active", "level": "ok"})
        else:
            stale_after = self.cfg.get("WS_STALE_AFTER_SEC", 20)
            self.emit("log", {
                "msg": f"🔴 ALARME — WebSocket hors service (aucun tick depuis {stale_after}s) — bascule sur surveillance par cycle ({self.cfg.get('CYCLE_INTERVAL')}s)",
                "level": "error"
            })
        self._ws_was_healthy = healthy

    def _maybe_manage_position_via_cycle(self, symbol, price, state):
        """Le WebSocket (_on_ws_allmids) est desormais la source PRINCIPALE de
        surveillance des positions ouvertes (Max Loss / SL securite / Trailing
        TP / Quick Profit) : il verifie ces seuils a CHAQUE tick de prix recu,
        bien plus reactif que le cycle. Tant que le WebSocket est actif et
        recoit des ticks recemment, le cycle NE FAIT RIEN sur les positions
        ouvertes — il se contente d afficher le prix.
        Le cycle ne reprend la main que si le WebSocket n est pas abonne, ou
        n a plus donne signe de vie depuis WS_STALE_AFTER_SEC secondes
        (deconnexion silencieuse) : filet de secours pour ne jamais laisser
        une position totalement sans surveillance active du bot.
        """
        if self._is_ws_healthy():
            return  # le websocket gere deja cette position en temps reel
        self._manage_position(symbol, price, state)

    def _get_prices_with_timeout(self, timeout_sec):
        """Recupere les prix via get_prices() avec un delai maximum.
        get_prices() fait un appel reseau bloquant vers Hyperliquid sans
        timeout expose par le SDK — en cas d accroc reseau, cet appel peut
        rester bloque tres longtemps et geler tout le cycle du bot (plus
        aucune collecte, plus aucun log — symptome observe en usage reel).
        Ici, l appel tourne dans un thread separe : si le delai est depasse,
        on ABANDONNE ce thread (il restera bloque en arriere-plan jusqu a ce
        que l appel reseau finisse par echouer/reussir de son cote — sans
        consequence puisqu on ignore son resultat) et on rend la main
        immediatement au cycle, qui reessaiera au tour suivant avec un
        thread neuf. Le bot ne se fige donc plus jamais indefiniment.
        """
        if self.info is None:
            return self._sim_prices()
        cfg = self.cfg
        result = {}
        def _worker():
            try:
                result["value"] = get_prices(self.info, cfg["SYMBOLS"], cfg)
            except Exception as e:
                result["error"] = e
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            self.emit("log", {
                "msg": f"⚠ Recuperation des prix bloquee depuis plus de {timeout_sec}s (coupure reseau probable) — cycle ignore, nouvelle tentative au prochain cycle",
                "level": "error"
            })
            return {}
        if "error" in result:
            self.emit("log", {"msg": f"Erreur recuperation des prix : {result['error']}", "level": "warn"})
            return {}
        return result.get("value", {})

    def _process_with_timeout(self, sym, price):
        """Wrapper de securite autour de _process : execute le traitement
        complet d un symbole (indicateurs, filtres, CPI, entree) dans un
        thread separe avec un delai maximum (PROCESS_TIMEOUT_SEC).
        - Si ce traitement ne termine pas a temps (gel du a une cause
          quelconque — reseau, disque...), ce symbole est ignore pour ce
          cycle, avec une alarme explicite.
        - Si ce traitement leve une EXCEPTION (bug), celle-ci est desormais
          capturee et loguee en detail (message + traceback complet) au lieu
          d etre avalee silencieusement — fix v3.1 : la version precedente ne
          capturait pas les exceptions (try/finally sans except), ce qui
          masquait totalement l erreur reelle (le symbole disparaissait du
          log sans aucune trace).
        """
        import traceback
        timeout_sec = self.cfg.get("PROCESS_TIMEOUT_SEC", 12)
        done = threading.Event()
        error_holder = {}
        def _worker():
            try:
                self._process(sym, price)
            except Exception as e:
                error_holder["error"] = e
                error_holder["trace"] = traceback.format_exc()
            finally:
                done.set()
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        ticker = ticker_from_slot_key(sym)
        if not done.wait(timeout=timeout_sec):
            self.emit("log", {
                "msg": f"⚠ [{ticker}] Traitement du cycle bloque depuis {timeout_sec}s — ignore pour ce cycle, nouvelle tentative au prochain",
                "level": "error"
            })
            return
        if "error" in error_holder:
            print(error_holder["trace"])  # traceback complet dans la console/stdout
            self.emit("log", {
                "msg": f"🔴 ERREUR [{ticker}] {type(error_holder['error']).__name__}: {error_holder['error']}",
                "level": "error"
            })

    def _run(self):
        cfg = self.cfg
        self.emit("log", {"msg": "Connexion a Hyperliquid...", "level": "info"})
        # v3.2 : la cle API + le wallet Hyperliquid sont desormais OBLIGATOIRES,
        # en mode paper COMME en mode live — plus de repli silencieux vers des
        # prix simules (_sim_prices). Le paper trading doit s appuyer sur les
        # vraies donnees de marche (prix + WebSocket), seule la passation
        # d ordres reels reste desactivee en paper (voir place_order/close_order,
        # tous deux gates par cfg["MODE"]=="live").
        if not cfg["PRIVATE_KEY"] or not cfg["WALLET_ADDRESS"]:
            self.emit("log", {
                "msg": "ERREUR : cle API et/ou wallet Hyperliquid manquants — obligatoires desormais (paper ET live). Configurez HYPERBOT_PRIVATE_KEY / HYPERBOT_WALLET_ADDRESS (ou via l API /api/config/hyperliquid) puis redemarrez.",
                "level": "error"
            })
            self.running = False
            self._started = False
            self.emit("stopped", {})
            return

        self.info, self.exchange, conn_error = connect_hyperliquid(cfg["PRIVATE_KEY"], cfg["WALLET_ADDRESS"])
        if self.info is None:
            self.emit("log", {"msg": f"Connexion Hyperliquid echouee — {conn_error}", "level": "error"})
            self.running = False
            self._started = False
            self.emit("stopped", {})
            return
        self.emit("log", {"msg": "Connexion etablie.", "level": "ok"})

        # ── v3.1 : abonnement WebSocket temps reel (flux allMids) ─────────
        # Objectif : verifier Max Loss / SL securite / Trailing TP a CHAQUE
        # tick de prix recu, sans attendre le prochain cycle (CYCLE_INTERVAL).
        # v3.2 : actif desormais systematiquement en paper ET en live, puisque
        # la connexion Hyperliquid est obligatoire dans les deux modes — seul
        # le PASSAGE D ORDRE reel reste reserve au mode live (voir plus bas et
        # _on_ws_allmids -> _manage_position -> close_order).
        # Repli automatique sur la surveillance par cycle (15s) si l abonnement
        # echoue (SDK incompatible, pas de reseau, etc.) — aucune perte de
        # fonctionnalite, juste moins reactif.
        try:
            self.info.subscribe({"type": "allMids"}, self._on_ws_allmids)
            self._ws_subscribed = True
            self.emit("log", {"msg": "WebSocket temps reel actif — surveillance Max Loss/TP en direct (independante du cycle)", "level": "ok"})
        except Exception as e:
            self._ws_subscribed = False
            self.emit("log", {"msg": f"Echec abonnement WebSocket ({e}) — repli sur surveillance par cycle ({cfg['CYCLE_INTERVAL']}s)", "level": "warn"})

        # ── Application reelle du levier configure (fix v3.1 : LEVERAGE ──
        # existait dans CONFIG mais n etait jamais envoye a Hyperliquid) ──
        if cfg["MODE"] == "live":
            leverage = cfg.get("LEVERAGE", 1)
            real_tickers_lev = list({ticker_from_slot_key(s) for s in cfg["SYMBOLS"]})
            lev_errors = []
            for t in real_tickers_lev:
                try:
                    self.exchange.update_leverage(leverage, t, is_cross=True)
                except Exception as e:
                    lev_errors.append(t)
                    print(f"[LEVERAGE] Echec x{leverage} sur {t} : {e}")
            if lev_errors:
                self.emit("log", {"msg": f"Levier x{leverage} : echec sur {', '.join(lev_errors)} (verifiez manuellement sur Hyperliquid).", "level": "warn"})
            else:
                self.emit("log", {"msg": f"Levier x{leverage} applique (cross margin) sur : {', '.join(real_tickers_lev)}", "level": "ok"})

        # ── Synchronisation capital réel Hyperliquid ──
        if cfg["MODE"] == "live":
            real_balance = sync_capital_from_hyperliquid(self.info, cfg["WALLET_ADDRESS"])
            if real_balance is not None and real_balance > 0:
                self.capital = real_balance
                self.cfg["CAPITAL_USD"] = real_balance
                self.emit("log", {"msg": f"Capital synchronise depuis Hyperliquid : ${real_balance:.2f}", "level": "ok"})
            else:
                self.emit("log", {"msg": "Sync capital echouee — capital local utilise.", "level": "warn"})

            # ── Reconciliation : trades fermes par Hyperliquid pendant la deconnexion ──
            saved_positions = self._load_saved_positions()
            if saved_positions:
                ghost_trades = reconcile_closed_positions(self.info, cfg["WALLET_ADDRESS"], saved_positions, cfg)
                for gt in ghost_trades:
                    sym = gt["symbol"]
                    # Trouver la slot_key correspondant a ce ticker
                    slot_key = next((s for s in self.states if ticker_from_slot_key(s) == sym), None)
                    target = self.states.get(slot_key) if slot_key else None
                    if target:
                        target.trades += 1
                        target.pnl    += gt["pnl"]
                        if gt["win"]:
                            target.wins += 1
                        target.closed_trades.append(gt)
                        self.cfg["CAPITAL_USD"] += gt["pnl"]
                        level = "win" if gt["win"] else "loss"
                        self.emit("trade", gt)
                        self.emit("log", {"msg": f"[{sym}] {gt['reason']} pendant deconnexion @ ${gt['exit']:.2f} | PnL: ${gt['pnl']:.2f}", "level": level})

            # ── Reprise des positions encore ouvertes apres crash ──
            real_tickers = list({ticker_from_slot_key(s) for s in cfg["SYMBOLS"]})
            recovered = recover_open_positions(self.info, cfg["WALLET_ADDRESS"], real_tickers, cfg)
            if recovered:
                self.emit("log", {"msg": f"{len(recovered)} position(s) recuperee(s) apres reprise.", "level": "warn"})
                for ticker_sym, pos in recovered.items():
                    # Trouver la slot_key correspondant a ce ticker
                    slot_key = next((s for s in self.states if ticker_from_slot_key(s) == ticker_sym), None)
                    if slot_key:
                        self.states[slot_key].position = pos
                        # v3.2 — FIX : recover_open_positions reconstruit la
                        # position depuis l EXCHANGE reel (entry/sl/tp exacts),
                        # mais ne connait pas la memoire du Trailing TP (pic de
                        # profit, etage) — on la retrouve ici en croisant avec
                        # notre propre sauvegarde (saved_positions, chargee plus
                        # haut), pour ne pas "oublier" une progression deja faite.
                        saved = saved_positions.get(slot_key) if saved_positions else None
                        if saved:
                            self.states[slot_key].peak_pnl_usd = saved.get("_peak_pnl_usd")
                            self.states[slot_key].tp_stage = saved.get("_tp_stage", 0)
                            self.states[slot_key].trailing_tp_active = saved.get("_trailing_tp_active", False)
                        self.emit("log", {"msg": f"[{ticker_sym}] Position {pos['type'].upper()} @ ${pos['entry']:.2f} reintegree | SL ${pos['sl']:.2f} | TP ${pos['tp']:.2f}", "level": "warn"})
                        ensure_sl_on_hyperliquid(self.exchange, self.info, cfg["WALLET_ADDRESS"], ticker_sym, pos, cfg)
                        self.emit("log", {"msg": f"[{ticker_sym}] Verification SL Hyperliquid effectuee", "level": "ok"})
            else:
                self.emit("log", {"msg": "Aucune position ouverte a recuperer.", "level": "info"})
            self._save_open_positions()
        else:
            # ── v3.2 : le mode PAPER beneficie desormais aussi de la ──────────
            # persistance des positions (auparavant reservee au mode live).
            # Sans ca, un redeploiement Railway pendant qu une position paper
            # est ouverte la faisait disparaitre de la memoire du bot SANS
            # jamais la clore proprement en base — elle restait alors
            # eternellement "ouverte" dans le Bilan/l historique, meme si
            # plus aucune gestion active ne s en occupait.
            # Pas de reconciliation avec un exchange reel ici (ca n a pas de
            # sens en simulation) : on restaure simplement telles quelles les
            # positions sauvegardees lors du dernier arret/redemarrage.
            saved_positions = self._load_saved_positions()
            restored = 0
            for slot_key, state in self.states.items():
                pos = saved_positions.get(slot_key) if saved_positions else None
                if pos and not state.position:
                    ticker = ticker_from_slot_key(slot_key)
                    # v3.2 — FIX : extrait l etat du Trailing TP (pic de profit,
                    # etage) sauvegarde avec la position, pour ne pas "oublier"
                    # qu elle avait deja depasse un pic avant le redemarrage.
                    peak_pnl_usd = pos.pop("_peak_pnl_usd", None)
                    tp_stage = pos.pop("_tp_stage", 0)
                    trailing_tp_active = pos.pop("_trailing_tp_active", False)
                    state.position = pos
                    state.peak_pnl_usd = peak_pnl_usd
                    state.tp_stage = tp_stage
                    state.trailing_tp_active = trailing_tp_active
                    restored += 1
                    stage_info = f" | Trailing etage {tp_stage}, pic +${peak_pnl_usd:.2f}" if peak_pnl_usd is not None else ""
                    self.emit("log", {"msg": f"[{ticker}] Position {pos['type'].upper()} @ ${pos['entry']:.2f} restauree (paper, apres redemarrage){stage_info}", "level": "warn"})
            if restored:
                self.emit("log", {"msg": f"{restored} position(s) paper restauree(s) apres redemarrage.", "level": "warn"})

        self._load_confidence_thresholds()
        self._load_indicator_state_if_recent()
        self._load_trading_session()

        symbols_display = ", ".join(self._original_symbols)
        self.emit("log", {"msg": f"Demarrage | {symbols_display} | ${cfg['CAPITAL_USD']}", "level": "ok"})
        self.emit("log", {"msg": f"Plage horaire : {cfg['TRADE_HOUR_START']}h-{cfg['TRADE_HOUR_END']}h Paris", "level": "info"})

        # v3.2 — signale la fin complete de l initialisation (reconciliation
        # Hyperliquid en live deja faite, restauration paper deja faite) :
        # permet a l API web de lancer un nettoyage automatique des
        # signaux/trades orphelins juste apres, en connaissant avec
        # certitude l etat REEL des positions a cet instant precis.
        self.emit("startup_ready", {})

        while self.running:
            self.cycle += 1
            self._check_ws_health_alert()
            self._update_trading_session()
            prices = self._get_prices_with_timeout(cfg.get("PRICE_FETCH_TIMEOUT_SEC", 10))
            in_hours = is_trading_hours(cfg)

            if not in_hours:
                self.emit("log", {"msg": f"Hors plage {cfg['TRADE_HOUR_START']}h-{cfg['TRADE_HOUR_END']}h — nouvelles entrees suspendues (positions actives conservees)", "level": "dim"})
                for sym in cfg["SYMBOLS"]:
                    if sym in prices:
                        self.states[sym].current_price = prices[sym]
                # Continuer a gerer les positions ouvertes (SL / Trailing TP)
                for sym in cfg["SYMBOLS"]:
                    if sym in prices and self.states[sym].position:
                        self._process_with_timeout(sym, prices[sym])
                self._save_open_positions()
                self._save_confidence_thresholds()
                self._save_indicator_state()
                self._send_snapshot()
                time.sleep(cfg["CYCLE_INTERVAL"])
                continue

            if not prices:
                self.emit("log", {"msg": "Prix indisponibles", "level": "warn"})
                time.sleep(cfg["CYCLE_INTERVAL"])
                continue

            for sym in cfg["SYMBOLS"]:
                if sym in prices:
                    self._process_with_timeout(sym, prices[sym])

            self._save_open_positions()
            self._save_confidence_thresholds()
            self._save_indicator_state()
            self._send_snapshot()
            time.sleep(cfg["CYCLE_INTERVAL"])

        self.emit("stopped", {})

    def _manage_position(self, symbol, price, state):
        """Wrapper thread-safe : _manage_position_impl peut etre appelee soit
        depuis le thread du cycle (_run), soit depuis le thread WebSocket
        (_on_ws_allmids). Le lock evite qu une meme position soit traitee/
        fermee deux fois en parallele (ex: MAX LOSS declenche par les deux
        threads presque simultanement)."""
        with self.lock:
            self._manage_position_impl(symbol, price, state)

    def _manage_position_impl(self, symbol, price, state):
        """v3.1 — Moteur de risque en dollars.
        1) Max Loss (-0.75$ par defaut) : sortie immediate geree par le bot.
        2) SL Hyperliquid (1.5%) : filet de securite uniquement (cas ou le
           bot serait en retard/deconnecte) — ne devrait quasiment jamais
           se declencher avant le Max Loss ci-dessus en usage normal.
        3) Trailing Take Profit a 2 etages, en $ de PnL latent :
           - Etage 1 "Quick Profit" : arme des +QUICK_PROFIT_ARM_USD (defaut 1$).
             Si le profit retombe a QUICK_PROFIT_LOCK_USD (defaut 1$) ou moins,
             fermeture immediate pour capturer ce montant.
           - Etage 2 "Trailing illimite" : active des +TRAILING_TP_ARM_USD
             (defaut 1.5$). Le pic de profit est traque en continu ; la
             position reste ouverte tant qu un nouveau pic est atteint et se
             ferme des que le profit cesse de progresser (1ere baisse depuis
             le pic), pour capturer le maximum atteint.
        """
        cfg    = self.cfg
        ticker = ticker_from_slot_key(symbol)
        pos    = state.position
        if not pos:
            return
        mode = cfg["MODE"]

        # ── PnL latent en $ ───────────────────────────────────────────────
        if pos["type"] == "long":
            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
        else:
            pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
        pnl_usd = pos["size"] * pnl_pct / 100

        # ── 1. MAX LOSS — priorite absolue, gere par le bot ─────────────────
        max_loss_usd = cfg.get("MAX_LOSS_USD", 0.75)
        if pnl_usd <= -max_loss_usd:
            pnl, _, trade = state.close_position(price, "MAX LOSS")
            trade["symbol"] = symbol
            if mode == "live" and self.exchange:
                close_order(self.exchange, ticker, pos, self.cfg)
            self.emit("trade", trade)
            self.emit("log", {"msg": f"[{ticker}] MAX LOSS @ ${price:.2f} | PnL: ${pnl:.2f} (seuil -${max_loss_usd:.2f})", "level": "loss"})
            self._register_max_loss(ticker, pos.get("confidence"))
            self._save_open_positions()  # v3.2 : sauvegarde en live ET en paper
            return

        # ── 2. SL Hyperliquid — filet de securite (ne devrait presque jamais
        #      se declencher en premier, le Max Loss $ est plus serre) ───────
        sl_hit = (pos["type"] == "long" and price <= pos["sl"]) or \
                 (pos["type"] == "short" and price >= pos["sl"])
        if sl_hit:
            pnl, _, trade = state.close_position(price, "SL SECURITE HYPERLIQUID")
            trade["symbol"] = symbol
            if mode == "live" and self.exchange:
                close_order(self.exchange, ticker, pos, self.cfg)
            self.emit("trade", trade)
            self.emit("log", {"msg": f"[{ticker}] SL SECURITE @ ${price:.2f} | PnL: ${pnl:.2f}", "level": "loss"})
            self._register_max_loss(ticker, pos.get("confidence"))
            self._save_open_positions()  # v3.2 : sauvegarde en live ET en paper
            return

        # ── 3. Trailing Take Profit a 2 etages (en $) ───────────────────────
        quick_arm  = cfg.get("QUICK_PROFIT_ARM_USD", 1.0)
        quick_lock = cfg.get("QUICK_PROFIT_LOCK_USD", 1.0)
        trail_arm  = cfg.get("TRAILING_TP_ARM_USD", 1.5)

        if state.tp_stage == 0 and pnl_usd >= quick_arm:
            state.tp_stage = 1
            state.trailing_tp_active = True
            state.peak_pnl_usd = pnl_usd
            self.emit("log", {"msg": f"[{ticker}] Profit +${pnl_usd:.2f} — Quick Profit arme (sortie si retour a ${quick_lock:.2f})", "level": "signal"})

        if state.tp_stage == 1 and pnl_usd >= trail_arm:
            state.tp_stage = 2
            state.peak_pnl_usd = pnl_usd
            self.emit("log", {"msg": f"[{ticker}] Profit +${pnl_usd:.2f} — Trailing illimite active (persiste tant que le profit progresse)", "level": "signal"})

        if state.tp_stage == 2:
            if state.peak_pnl_usd is None or pnl_usd > state.peak_pnl_usd:
                state.peak_pnl_usd = pnl_usd
                self.emit("log", {"msg": f"[{ticker}] ${price:.2f} nouveau pic latent +${pnl_usd:.2f} — Trailing illimite poursuit", "level": "info"})
                return
            elif pnl_usd < state.peak_pnl_usd:
                # Le profit ne progresse plus -> on prend le pic atteint
                pnl, _, trade = state.close_position(price, f"TRAILING TP (pic +${state.peak_pnl_usd:.2f})")
                trade["symbol"] = symbol
                if mode == "live" and self.exchange:
                    close_order(self.exchange, ticker, pos, self.cfg)
                self.emit("trade", trade)
                self._register_win(ticker)
                self.emit("log", {"msg": f"[{ticker}] TRAILING TP SORTIE @ ${price:.2f} | pic +${state.peak_pnl_usd:.2f} | PnL: +${pnl:.2f}", "level": "win"})
                self._save_open_positions()  # v3.2 : sauvegarde en live ET en paper
                return
            else:
                self.emit("log", {"msg": f"[{ticker}] ${price:.2f} Trailing illimite actif | latent +${pnl_usd:.2f} | pic +${state.peak_pnl_usd:.2f}", "level": "dim"})
                return

        elif state.tp_stage == 1:
            if pnl_usd <= quick_lock:
                pnl, _, trade = state.close_position(price, "QUICK PROFIT")
                trade["symbol"] = symbol
                if mode == "live" and self.exchange:
                    close_order(self.exchange, ticker, pos, self.cfg)
                self.emit("trade", trade)
                self._register_win(ticker)
                self.emit("log", {"msg": f"[{ticker}] QUICK PROFIT @ ${price:.2f} | PnL: +${pnl:.2f}", "level": "win"})
                self._save_open_positions()  # v3.2 : sauvegarde en live ET en paper
                return
            else:
                self.emit("log", {"msg": f"[{ticker}] ${price:.2f} Quick Profit arme | latent +${pnl_usd:.2f} (sortie si <= ${quick_lock:.2f})", "level": "dim"})
                return

        # ── 4. Rien de declenche — affichage du latent ──────────────────────
        # v3.2 — FIX : ce log se declenchait a CHAQUE tick WebSocket (plusieurs
        # fois par seconde), inondant le buffer de logs et poussant hors de
        # vue les messages plus rares (collecte des autres actifs, alarmes...).
        # Limite desormais a une fois toutes les 30 secondes par actif.
        now_ts = time.time()
        if state._last_status_log_ts is None or (now_ts - state._last_status_log_ts) >= 30:
            state._last_status_log_ts = now_ts
            self.emit("log", {"msg": f"[{ticker}] ${price:.2f} {pos['type'].upper()} | latent: ${pnl_usd:+.2f} | Max Loss: -${max_loss_usd:.2f} | SL secu: ${pos['sl']:.2f}", "level": "dim"})

    def _process(self, symbol, price):
        cfg   = self.cfg
        ticker = ticker_from_slot_key(symbol)   # vrai ticker API (ex: "BTC" depuis "BTC_0")
        state = self.states[symbol]
        # v3.2 — FIX : ne pas ecraser le prix avec la valeur REST (cycle,
        # potentiellement vieille de 15s) si le WebSocket est sain — il
        # fournit deja une valeur plus fraiche en continu pour les actifs en
        # position. Sans ce garde-fou, les deux sources ecrivaient
        # concurremment sur state.current_price depuis des threads
        # differents, causant un "retour en arriere" brutal et confus du
        # PnL affiche des qu un cycle REST arrivait apres plusieurs ticks
        # WebSocket plus recents.
        if not self._is_ws_healthy():
            state.current_price = price
        state.last_price_time = datetime.now()
        state.price_history.append(price)

        if len(state.price_history) >= 2:
            vol = abs(list(state.price_history)[-1] - list(state.price_history)[-2])
            state.vol_history.append(vol)

        prices = list(state.price_history)
        rsi = calc_rsi(prices, cfg["RSI_PERIOD"])

        # EMA specifiques au symbole ou globales
        ema_short = cfg.get("SYMBOL_EMA_SHORT", {}).get(ticker, cfg["EMA_SHORT"])
        ema_long  = cfg.get("SYMBOL_EMA_LONG",  {}).get(ticker, cfg["EMA_LONG"])
        ema_s = calc_ema(prices, ema_short)
        ema_l = calc_ema(prices, ema_long)

        # EMA intermediaire — tendance 25-50 minutes
        # BTC/ETH : EMA50 swing (25 min) ou EMA60 scalp (30 min)
        # Prix > EMA_MID → tendance haussiere de fond → bloquer SHORT
        # Prix < EMA_MID → tendance baissiere de fond → bloquer LONG
        ema_mid_period = cfg.get("SYMBOL_EMA_MID", {}).get(ticker, cfg.get("EMA_MID_PERIOD", 50))
        ema_mid = calc_ema(prices, ema_mid_period) if ema_mid_period else None
        macd, sig = calc_macd(prices, cfg["MACD_FAST"], cfg["MACD_SLOW"], cfg["MACD_SIGNAL"])
        bb_up, bb_mid, bb_low = calc_bollinger(prices, cfg["BB_PERIOD"], cfg["BB_STD"])

        # EMA 200 multi-timeframe — 1 point toutes les 4 cycles (30s x 4 = 2 min par bougie)
        # Couvre 200 x 2min = 6h40 de tendance longue
        MTF_STEP = 4  # nombre de cycles entre chaque point EMA200
        if len(prices) % MTF_STEP == 0:
            state.mtf_prices.append(price)
        ema200 = calc_ema(list(state.mtf_prices), 200) if len(state.mtf_prices) >= 10 else None
        trend_up   = ema200 is None or price > ema200   # au dessus EMA200 = tendance haussiere
        trend_down = ema200 is None or price < ema200   # en dessous EMA200 = tendance baissiere

        # Sauvegarde du MACD du cycle precedent pour detection crossover (Trailing TP)
        state.prev_macd = state.current_macd
        state.prev_sig  = state.current_sig

        state.current_rsi  = rsi
        state.current_macd = macd
        state.current_sig  = sig

        needed = max(cfg["RSI_PERIOD"]+1, ema_long, cfg["MACD_SLOW"]+cfg["MACD_SIGNAL"], cfg["BB_PERIOD"], cfg.get("SR_PERIOD", 0)+1, ema_mid_period or 0)
        if any(v is None for v in [rsi, ema_s, ema_l, macd, sig, bb_up]):
            state.collecting = True
            self.emit("log", {"msg": f"[{ticker}] Collecte... ({len(prices)}/{needed})", "level": "dim"})
            return
        state.collecting = False

        # Stocker l ATR% courant pour affichage dashboard — calcule a chaque cycle
        # independamment de l etat (position ouverte ou non, filtre bloque ou non)
        _, _atr_pct_now = calc_atr(prices, cfg.get("ATR_PERIOD", 14))
        state.current_atr_pct = _atr_pct_now

        if state.position:
            self._maybe_manage_position_via_cycle(symbol, price, state)
            return

        # v3.2 — Prudence live : session de trading terminee (23h45 ecoulees) —
        # aucune NOUVELLE entree tant que toutes les positions de la session
        # precedente ne sont pas fermees. Les positions deja ouvertes
        # continuent d etre gerees normalement (deja fait juste au-dessus).
        if self.trading_blocked:
            return

        # ── v3.2 web : max_open_trades (pilote depuis l interface) — le filtre
        #    active_coins est applique plus loin (apres le calcul de confiance),
        #    pour permettre l auto-activation d un actif inactif si une
        #    opportunite tres forte est detectee.
        max_open = cfg.get("MAX_OPEN_TRADES")
        if max_open is not None:
            open_count = sum(1 for st in self.states.values() if st.position)
            if open_count >= max_open:
                self.emit("log", {"msg": f"[{ticker}] Max {max_open} positions ouvertes atteint — nouvelle entree suspendue", "level": "dim"})
                return

        # ── Plage horaire — bloque les NOUVELLES entrées en paper ET en live ──
        if not is_trading_hours(cfg):
            self.emit("log", {"msg": f"[{ticker}] Hors plage horaire — aucune nouvelle entree", "level": "dim"})
            return

        # Filtre volume
        vol_ok = True
        if len(state.vol_history) >= 10 and cfg["VOLUME_MIN_RATIO"] > 1.0:
            avg_vol = sum(list(state.vol_history)[:-1]) / (len(state.vol_history) - 1)
            cur_vol = list(state.vol_history)[-1]
            vol_ok = cur_vol >= avg_vol * cfg["VOLUME_MIN_RATIO"]

        # Filtre ATR — bloque les entrees en marche range (volatilite insuffisante)
        atr_excluded = ticker in cfg.get("ATR_EXCLUDE_SYMBOLS", [])
        if cfg.get("ATR_FILTER", False) and not atr_excluded:
            atr_period  = cfg.get("ATR_PERIOD", 14)
            # Seuil specifique au symbole ou seuil global
            atr_min_pct = cfg.get("ATR_MIN_PCT_BY_SYMBOL", {}).get(ticker,
                          cfg.get("ATR_MIN_PCT", 0.06))
            _, atr_pct  = calc_atr(prices, atr_period)
            if atr_pct is not None and atr_pct < atr_min_pct:
                self.emit("log", {
                    "msg": f"[{ticker}] ATR {atr_pct:.3f}% < {atr_min_pct}% — marche en range, entree bloquee | RSI:{rsi:.1f}",
                    "level": "dim"
                })
                return

        # Pour les symboles or (PAXG) — respecter les horaires Forex + periode de chauffe
        if ticker in cfg.get("FOREX_SYMBOLS", []):
            forex_now = is_forex_open()

            # Detection de la transition ferme → ouvert
            if state.forex_was_open is not None and not state.forex_was_open and forex_now:
                state.forex_reopen_time = datetime.now()
                warmup = cfg.get("FOREX_WARMUP_MINUTES", 15)
                self.emit("log", {
                    "msg": f"[{ticker}] Forex reouvert — chauffe {warmup} min avant entrees",
                    "level": "warn"
                })

            state.forex_was_open = forex_now

            if not forex_now:
                self.emit("log", {"msg": f"[{ticker}] Marche Forex ferme — {symbol} ignore", "level": "dim"})
                return

            # Verifier si la periode de chauffe est ecoulee
            if state.forex_reopen_time is not None:
                warmup_min = cfg.get("FOREX_WARMUP_MINUTES", 15)
                elapsed = (datetime.now() - state.forex_reopen_time).total_seconds() / 60
                remaining = warmup_min - elapsed
                if remaining > 0:
                    self.emit("log", {
                        "msg": f"[{ticker}] Chauffe Forex : encore {remaining:.0f} min avant entrees — observation en cours",
                        "level": "dim"
                    })
                    return
                else:
                    # Chauffe terminee — on ne reinitialise pas forex_reopen_time
                    # pour ne pas redeclencher la chauffe au prochain cycle
                    pass
        else:
            # ── Cryptos (tout ce qui n est pas dans FOREX_SYMBOLS) ───────────
            # Heures creuses + blackout CPI : bloquent uniquement les NOUVELLES
            # entrees. Les positions deja ouvertes continuent d etre gerees
            # normalement par _manage_position. PAXG/or n est pas concerne ici
            # (deja gere ci-dessus par la logique Forex).
            # CRYPTO_OFFPEAK_ENABLED / CPI_BLACKOUT_ENABLED : bascules pilotees
            # depuis l interface web (filter_hours / filter_macro) — v3.2 web.
            if cfg.get("CRYPTO_OFFPEAK_ENABLED", True) and is_crypto_offpeak(cfg):
                self.emit("log", {
                    "msg": f"[{ticker}] Heures creuses crypto ({cfg.get('CRYPTO_OFFPEAK_HOUR_START_UTC',2)}h-{cfg.get('CRYPTO_OFFPEAK_HOUR_END_UTC',6)}h UTC) — nouvelles entrees suspendues",
                    "level": "dim"
                })
                return

            if cfg.get("CPI_BLACKOUT_ENABLED", True):
                self._refresh_cpi_events_if_needed()
                cpi_blackout, cpi_event = self._is_cpi_blackout()
                if cpi_blackout:
                    self.emit("log", {
                        "msg": f"[{ticker}] Blackout CPI ({cpi_event.strftime('%d/%m %H:%M UTC')}) — nouvelles entrees suspendues",
                        "level": "warn"
                    })
                    return

        ema_bull = ema_s > ema_l
        ema_bear = ema_s < ema_l

        # Filtre pivot — detection du croisement EMA frais (cycle N-1 → cycle N)
        # Pour un LONG : EMA courte vient de passer AU-DESSUS de EMA longue
        # Pour un SHORT : EMA courte vient de passer EN-DESSOUS de EMA longue
        # Evite d entrer sur un croisement ancien — on veut le pivot tout frais
        require_pivot = ticker in cfg.get("PIVOT_CONFIRM_SYMBOLS", [])
        pivot_bull = True  # par defaut : pas de filtre pivot
        pivot_bear = True

        if require_pivot and state.prev_ema_s is not None and state.prev_ema_l is not None:
            prev_bull = state.prev_ema_s > state.prev_ema_l
            prev_bear = state.prev_ema_s < state.prev_ema_l
            # Croisement haussier frais : etait baissier avant, haussier maintenant
            pivot_bull = (not prev_bull) and ema_bull
            # Croisement baissier frais : etait haussier avant, baissier maintenant
            pivot_bear = (not prev_bear) and ema_bear

        # Compteur de cycles consecutifs dans le sens de la tendance EMA
        if ema_bull:
            state.consec_bull += 1
            state.consec_bear  = 0
        elif ema_bear:
            state.consec_bear += 1
            state.consec_bull  = 0
        else:
            state.consec_bull  = 0
            state.consec_bear  = 0

        # Nombre minimum de cycles consecutifs requis par symbole
        # PAXG : 2 cycles consecutifs dans le sens de la tendance avant entree
        # Evite les faux croisements EMA de courte duree sur l or
        min_consec = cfg.get("CONSEC_CONFIRM_SYMBOLS", {}).get(ticker, 1)

        # Support / Resistance — confirmation de breakout en SCALP uniquement
        # LONG  valide si le prix CASSE au-dessus de la resistance recente (50 cycles = 25min)
        # SHORT valide si le prix CASSE en-dessous du support recent
        is_scalp = cfg.get("PROFILE") == "scalp"
        sr_period = cfg.get("SR_PERIOD", 50)
        support, resistance = (None, None)
        if is_scalp:
            support, resistance = calc_support_resistance(prices, sr_period)

        # Sauvegarder les EMA pour le prochain cycle
        state.prev_ema_s = ema_s
        state.prev_ema_l = ema_l

        # ── Filtre Momentum Instantane — "ce qui se passe MAINTENANT" ──────
        # Les EMA moyennent le passe (12-26 cycles = 6-13 min) et peuvent
        # generer un signal qui contredit le mouvement TRES recent.
        # On calcule le % de variation sur les MOMENTUM_PERIOD derniers cycles
        # (defaut 4 cycles = 2 min). Si ce mouvement recent est fortement
        # oppose au signal (au-dela de MOMENTUM_THRESHOLD_PCT), on bloque
        # l entree — "maintenant" prevaut sur la moyenne.
        # Applique a TOUS les actifs, SWING et SCALP.
        momentum_period    = cfg.get("MOMENTUM_PERIOD", 4)
        momentum_threshold = cfg.get("MOMENTUM_THRESHOLD_PCT", 0.15)
        momentum_pct = None
        if len(prices) > momentum_period:
            ref_price = prices[-(momentum_period+1)]
            if ref_price > 0:
                momentum_pct = (price - ref_price) / ref_price * 100

        # Seuils RSI specifiques au symbole
        rsi_oversold   = cfg.get("SYMBOL_RSI_OVERSOLD",  {}).get(ticker, cfg["RSI_OVERSOLD"])
        rsi_overbought = cfg.get("SYMBOL_RSI_OVERBOUGHT", {}).get(ticker, cfg["RSI_OVERBOUGHT"])

        # Mode RSI — deux logiques possibles par symbole :
        # "reversal"  (defaut) : RSI < oversold (survente) pour LONG — strategie retournement
        # "trend"              : RSI > 50 pour LONG, RSI < 50 pour SHORT — strategie suivi tendance
        # BTC utilise le mode "trend" — entre dans le sens du momentum, pas contre lui
        rsi_mode = cfg.get("SYMBOL_RSI_MODE", {}).get(ticker, "trend")
        if rsi_mode == "trend":
            rsi_buy  = rsi > 50   # momentum haussier confirme
            rsi_sell = rsi < 50   # momentum baissier confirme
        else:
            rsi_buy  = rsi < rsi_oversold
            rsi_sell = rsi > rsi_overbought
        macd_bull = macd > sig
        macd_bear = macd < sig
        bb_low_ok = price <= bb_low
        bb_up_ok  = price >= bb_up

        # Pour certains symboles (SOL), MACD + BB sont OBLIGATOIRES pour entrer
        require_macd_bb  = ticker in cfg.get("SYMBOL_REQUIRE_MACD_BB", [])

        # Pour certains symboles (BTC), l EMA200 est OBLIGATOIRE pour confirmer la direction
        # Long uniquement si prix > EMA200 | Short uniquement si prix < EMA200
        require_ema200 = ticker in cfg.get("SYMBOL_REQUIRE_EMA200", [])

        signal = None
        reasons = []

        if rsi_buy and ema_bull and trend_up:
            # v3.2 — Zone RSI extreme : evite d entrer LONG (continuation
            # haussiere) quand le marche est deja en surachat extreme —
            # risque de retournement violent plus eleve dans cette zone.
            rsi_extreme_high = cfg.get("RSI_EXTREME_HIGH", 85)
            if rsi > rsi_extreme_high:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} LONG bloque — zone de surachat extreme (RSI > {rsi_extreme_high}), risque de retournement",
                    "level": "dim"
                })
                return
            # Filtre EMA intermediaire — tendance 25-50 min
            # Bloquer LONG si prix sous EMA_MID (tendance baissiere de fond)
            if ema_mid is not None and price < ema_mid:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} LONG bloque — prix sous EMA{ema_mid_period} ${ema_mid:.2f} (tendance baissiere 25-50 min)",
                    "level": "dim"
                })
                return
            # Filtre Momentum Instantane — bloque LONG si le marche vient
            # de chuter fortement MAINTENANT (contredit le signal haussier)
            if momentum_pct is not None and momentum_pct <= -momentum_threshold:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} LONG bloque — momentum instantane {momentum_pct:+.2f}% (baisse en cours MAINTENANT)",
                    "level": "dim"
                })
                return
            # Filtre cycles consecutifs — PAXG : 2 cycles haussiers requis
            if state.consec_bull < min_consec:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — tendance haussiere {state.consec_bull}/{min_consec} cycles confirmes",
                    "level": "dim"
                })
                return
            # Filtre pivot — croisement EMA frais obligatoire si configure
            if require_pivot and not pivot_bull:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — pivot EMA non confirme, attente croisement frais",
                    "level": "dim"
                })
                return
            # Filtre EMA200 obligatoire : bloquer les longs si prix < EMA200
            if require_ema200 and ema200 is not None and price <= ema200:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} LONG bloque — prix sous EMA200 (${ema200:.2f}), tendance baissiere",
                    "level": "dim"
                })
                return
            # Filtre MACD + BB obligatoire si configure pour ce symbole
            if require_macd_bb and not (macd_bull and bb_low_ok):
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — MACD/BB insuffisants (obligatoires pour {symbol})",
                    "level": "dim"
                })
                return
            # Filtre Support/Resistance — SCALP uniquement
            # LONG valide seulement si le prix CASSE au-dessus de la resistance recente
            if is_scalp and resistance is not None and price <= resistance:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — prix sous resistance ${resistance:.2f}, pas de breakout",
                    "level": "dim"
                })
                return
            # Filtre Confiance — dernier filtre, score 0-100% des confirmations
            # optionnelles disponibles. Seuil dynamique par actif (v3.1).
            confidence = self._score_confidence(
                "long", macd_bull, macd_bear, bb_low_ok, bb_up_ok, vol_ok,
                ema200, ema_mid, price, momentum_pct, momentum_threshold,
                state.consec_bull, min_consec, cfg
            )
            conf_threshold = self._get_confidence_threshold(ticker)
            if confidence < conf_threshold:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} LONG bloque — confiance {confidence:.0f}% < seuil requis {conf_threshold:.0f}%",
                    "level": "dim"
                })
                return
            if not self._gate_active_or_auto_activate(ticker, confidence, "long"):
                return
            signal = "long"
            reasons = [f"RSI {rsi:.1f}", f"EMA{ema_short}/{ema_long} hausse", f"confiance {confidence:.0f}%"]
            if macd_bull:
                reasons.append("MACD hausse")
            if bb_low_ok:
                reasons.append("BB bas OK")
            if ema200:
                reasons.append(f"EMA200 ↑ ${ema200:.0f}")
            if ema_mid:
                reasons.append(f"EMA{ema_mid_period} ↑ ${ema_mid:.0f}")
            if is_scalp and resistance is not None:
                reasons.append(f"breakout R ${resistance:.2f}")
            if momentum_pct is not None:
                reasons.append(f"momentum {momentum_pct:+.2f}%")
        elif rsi_sell and ema_bear and trend_down:
            # v3.2 — Zone RSI extreme : evite d entrer SHORT (continuation
            # baissiere) quand le marche est deja en survente extreme —
            # risque de rebond violent plus eleve dans cette zone.
            rsi_extreme_low = cfg.get("RSI_EXTREME_LOW", 15)
            if rsi < rsi_extreme_low:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} SHORT bloque — zone de survente extreme (RSI < {rsi_extreme_low}), risque de rebond",
                    "level": "dim"
                })
                return
            # Filtre EMA intermediaire — tendance 25-50 min
            # Bloquer SHORT si prix au-dessus EMA_MID (tendance haussiere de fond)
            if ema_mid is not None and price > ema_mid:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} SHORT bloque — prix au-dessus EMA{ema_mid_period} ${ema_mid:.2f} (tendance haussiere 25-50 min)",
                    "level": "dim"
                })
                return
            # Filtre Momentum Instantane — bloque SHORT si le marche vient
            # de monter fortement MAINTENANT (contredit le signal baissier)
            # C est le cas BTC observe : signal SHORT alors que le prix
            # est en hausse nette sur les derniers cycles
            if momentum_pct is not None and momentum_pct >= momentum_threshold:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} SHORT bloque — momentum instantane {momentum_pct:+.2f}% (hausse en cours MAINTENANT)",
                    "level": "dim"
                })
                return
            # Filtre cycles consecutifs — PAXG : 2 cycles baissiers requis
            if state.consec_bear < min_consec:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — tendance baissiere {state.consec_bear}/{min_consec} cycles confirmes",
                    "level": "dim"
                })
                return
            # Filtre pivot — croisement EMA frais obligatoire si configure
            if require_pivot and not pivot_bear:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — pivot EMA non confirme, attente croisement frais",
                    "level": "dim"
                })
                return
            # Filtre EMA200 obligatoire : bloquer les shorts si prix > EMA200
            if require_ema200 and ema200 is not None and price >= ema200:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} SHORT bloque — prix sur EMA200 (${ema200:.2f}), tendance haussiere",
                    "level": "dim"
                })
                return
            if require_macd_bb and not (macd_bear and bb_up_ok):
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — MACD/BB insuffisants (obligatoires pour {symbol})",
                    "level": "dim"
                })
                return
            # Filtre Support/Resistance — SCALP uniquement
            # SHORT valide seulement si le prix CASSE en-dessous du support recent
            if is_scalp and support is not None and price >= support:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} signal potentiel — prix au-dessus support ${support:.2f}, pas de breakout",
                    "level": "dim"
                })
                return
            # Filtre Confiance — dernier filtre, score 0-100% des confirmations
            # optionnelles disponibles. Seuil dynamique par actif (v3.1).
            confidence = self._score_confidence(
                "short", macd_bull, macd_bear, bb_low_ok, bb_up_ok, vol_ok,
                ema200, ema_mid, price, momentum_pct, momentum_threshold,
                state.consec_bear, min_consec, cfg
            )
            conf_threshold = self._get_confidence_threshold(ticker)
            if confidence < conf_threshold:
                self.emit("log", {
                    "msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} SHORT bloque — confiance {confidence:.0f}% < seuil requis {conf_threshold:.0f}%",
                    "level": "dim"
                })
                return
            if not self._gate_active_or_auto_activate(ticker, confidence, "short"):
                return
            signal = "short"
            reasons = [f"RSI {rsi:.1f}", f"EMA{ema_short}/{ema_long} baisse", f"confiance {confidence:.0f}%"]
            if macd_bear:
                reasons.append("MACD baisse")
            if bb_up_ok:
                reasons.append("BB haut OK")
            if ema200:
                reasons.append(f"EMA200 ↓ ${ema200:.0f}")
            if ema_mid:
                reasons.append(f"EMA{ema_mid_period} ↓ ${ema_mid:.0f}")
            if is_scalp and support is not None:
                reasons.append(f"breakout S ${support:.2f}")
            if momentum_pct is not None:
                reasons.append(f"momentum {momentum_pct:+.2f}%")
        else:
            self.emit("log", {"msg": f"[{ticker}] ${price:.2f} RSI:{rsi:.1f} ATTENDRE", "level": "dim"})
            return

        if not vol_ok and cfg["VOLUME_MIN_RATIO"] > 1.0:
            self.emit("log", {"msg": f"[{ticker}] Signal ignore - volume faible", "level": "dim"})
            return

        capital_engaged = sum(s.position["size"] for s in self.states.values() if s.position)
        total_pnl = sum(s.pnl for s in self.states.values())
        capital_available = cfg["CAPITAL_USD"] + total_pnl - capital_engaged

        if capital_available <= 0:
            self.emit("log", {"msg": f"[{ticker}] Capital insuffisant (${capital_available:.2f})", "level": "warn"})
            return

        size = min(capital_available * cfg["POSITION_SIZE_PCT"] / 100, capital_available)

        # ── v3.1 : SL Hyperliquid = filet de securite fixe uniquement ────────
        # (le SL %/symbole n est plus utilise pour la gestion normale des sorties,
        # voir MAX_LOSS_USD gere par le bot dans _manage_position)
        safety_sl_pct = cfg.get("EXCHANGE_SAFETY_SL_PCT", 1.5)
        sl_p = price * (1 - safety_sl_pct/100) if signal == "long" else price * (1 + safety_sl_pct/100)
        # tp_p conserve uniquement a titre informatif / pour le bouton manuel TP
        # du dashboard — plus jamais envoye a Hyperliquid ni utilise pour fermer
        # automatiquement (remplace par le Trailing TP en $ de _manage_position).
        tp_pct = cfg.get("SYMBOL_TP_PCT", {}).get(ticker, cfg["TAKE_PROFIT_PCT"])
        tp_p = price * (1 + tp_pct/100) if signal == "long" else price * (1 - tp_pct/100)

        label = "LONG" if signal == "long" else "SHORT"
        _, atr_at_entry = calc_atr(prices, cfg.get("ATR_PERIOD", 14))
        atr_str = f"ATR {atr_at_entry:.3f}%" if atr_at_entry else "ATR ?"
        self.emit("log", {"msg": f"[{ticker}] {label} @ ${price:.2f} | RSI:{rsi:.1f}({rsi_mode}) | {atr_str} | {' | '.join(reasons)} | ${size:.2f} | MaxLoss -${cfg.get('MAX_LOSS_USD', 0.75):.2f} | SL secu {safety_sl_pct}% [PERP]", "level": "signal"})

        if cfg["MODE"] == "live" and self.exchange:
            # tp_price=None : plus d ordre TP fixe sur Hyperliquid, la prise de
            # profit est entierement geree par le bot (Quick Profit / Trailing)
            ok = place_order(self.exchange, ticker, signal == "long", size, price, cfg, sl_price=sl_p, tp_price=None)
            if not ok:
                self.emit("log", {"msg": f"[{ticker}] Ordre non execute", "level": "warn"})
                return

        state.open_position(signal, price, sl_p, tp_p, size, confidence=confidence)
        self._save_open_positions()  # v3.2 : sauvegarde en live ET en paper

        # ── v3.2 web : evenement structure pour l API (table trades / signaux) ──
        # tp1/tp2 sont deduits des seuils $ (Quick Profit / Trailing) — notre
        # bot ne raisonne pas en % fixe comme un TP1/TP2 classique, ceci est
        # une conversion informative en prix equivalent au moment de l entree.
        max_loss_usd  = cfg.get("MAX_LOSS_USD", 0.75)
        qp_arm_usd    = cfg.get("QUICK_PROFIT_ARM_USD", 1.0)
        trail_arm_usd = cfg.get("TRAILING_TP_ARM_USD", 1.5)
        if size > 0:
            pct1 = qp_arm_usd / size * 100
            pct2 = trail_arm_usd / size * 100
            tp1_price = price * (1 + pct1/100) if signal == "long" else price * (1 - pct1/100)
            tp2_price = price * (1 + pct2/100) if signal == "long" else price * (1 - pct2/100)
        else:
            tp1_price = tp2_price = None
        self.emit("trade_opened", {
            "coin": ticker,
            "action": label,
            "confidence": round(confidence, 1),
            "leverage": cfg.get("LEVERAGE", 1),
            "position_size_pct": cfg["POSITION_SIZE_PCT"],
            "risk_reward": round(qp_arm_usd / max_loss_usd, 2) if max_loss_usd else None,
            "timeframe": cfg.get("PROFILE", "swing"),
            "entry": price,
            "stop_loss": sl_p,
            "take_profit1": tp1_price,
            "take_profit2": tp2_price,
            "rsi": round(rsi, 1) if rsi is not None else None,
        })


# ─────────────────────────────────────────────
#  DASHBOARD TKINTER
# ─────────────────────────────────────────────
