"""
api.py — Backend FastAPI pour HyperBot Web (déploiement GitHub + Railway).

Sert :
  - l API JSON consommée par index.html (voir contrat dans le fichier HTML)
  - le fichier index.html lui-meme sur "/"

Demarrage local :
    pip install -r requirements.txt
    uvicorn api:app --host 0.0.0.0 --port 8000

Variables d environnement (voir README.md pour la liste complete) :
    HYPERBOT_DATA_DIR        dossier de donnees persistantes (DB + logs + capital)
    HYPERBOT_SECRET_KEY      cle secrete pour signer les tokens de session
    HYPERBOT_PRIVATE_KEY / HYPERBOT_WALLET_ADDRESS / HYPERBOT_FINNHUB_API_KEY
"""
import os
import sys
import json
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

# v3.2 — FIX CRITIQUE : les imports locaux (db, auth, bot_engine) DOIVENT se
# faire AVANT tout changement de repertoire courant (os.chdir). Auparavant,
# le chdir vers HYPERBOT_DATA_DIR (ex: /data) se faisait avant ces imports :
# Python resolvait alors "import db" par rapport au NOUVEAU repertoire
# courant (/data) au lieu du dossier contenant le code (/app), provoquant
# un crash systematique ("ModuleNotFoundError: No module named 'db'") des
# qu un Volume etait monte et HYPERBOT_DATA_DIR defini.
import db
import auth
import bot_engine as be
# v4.274 — FIX : importe ICI, avec les autres modules, AVANT le os.chdir()
# vers le dossier de donnees ci-dessous. Importe apres (v4.273), Python le
# cherchait dans le Volume -> "ModuleNotFoundError: No module named
# 'manual_trading'" et le serveur ne demarrait plus.
import manual_trading

# ── Dossier de donnees persistantes (a monter en Volume sur Railway) ─────
# Fait APRES les imports ci-dessus : seuls les FICHIERS ecrits a l execution
# (base SQLite, capital, logs, session) doivent utiliser ce dossier — pas la
# resolution des modules Python, deja faite a ce stade.
_DATA_DIR = os.environ.get("HYPERBOT_DATA_DIR", ".")
_DATA_DIR_CONFIGURED = "HYPERBOT_DATA_DIR" in os.environ
os.makedirs(_DATA_DIR, exist_ok=True)
# v4.274 — garde-fou : le dossier du code reste importable apres le chdir
# (uvicorn ajoute "." RELATIF a sys.path, qui designerait sinon le Volume).
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)
os.chdir(_DATA_DIR)

# ─────────────────────────────────────────────────────────────────────────
#  INITIALISATION
# ─────────────────────────────────────────────────────────────────────────
db.init_db()
# v4.244 — SUR DEMANDE EXPLICITE : purge le journal permanent au
# demarrage (rythme naturel de redeploiement de ce bot, suffisant pour
# eviter une croissance illimitee sans necessiter de tache planifiee
# separee).
try:
    db.purge_log_history()
except Exception as e:
    print(f"[LOG-HISTORY] Echec purge au demarrage : {e}")

# Nos 6 symboles reellement supportes (voir bot_engine.CONFIG["SYMBOLS"]).
# L interface propose 30 cryptos (ALL_COINS) — on ne peut en activer que
# parmi ce sous-ensemble reellement tradable par ce bot.
SUPPORTED_TICKERS = [be.ticker_from_slot_key(s) for s in be.CONFIG["SYMBOLS"]]

cfg = dict(be.CONFIG)

# v3.2 : fallback via variable d environnement pour ACTIVE_COINS — permet de
# fixer les actifs actifs sans dependre d un Volume Railway (comme pour les
# cles Hyperliquid/Finnhub). Une eventuelle valeur enregistree en base (via
# l interface web, necessite un Volume) reste prioritaire si presente.
def _norm_ticker(t: str) -> str:
    """v4.264 — normalise un ticker SANS casser les marches HIP-3 : "btc" ->
    "BTC", mais "xyz:eur"/"XYZ:EUR" -> "xyz:EUR" (prefixe DEX en minuscules,
    comme dans toute la configuration). Un simple .upper() produisait
    "XYZ:EUR", qui ne correspondait plus jamais a rien."""
    t = (t or "").strip()
    if ":" in t:
        dex, coin = t.split(":", 1)
        return f"{dex.lower()}:{coin.upper()}"
    return t.upper()


_env_active_coins = os.environ.get("HYPERBOT_ACTIVE_COINS", "").strip()
if _env_active_coins:
    cfg["ACTIVE_COINS"] = [_norm_ticker(c) for c in _env_active_coins.split(",") if c.strip()]

# v4.269 — FIX BUG CRITIQUE : le profil (SWING/SCALP) etait applique APRES
# les reglages enregistres par l utilisateur, et ecrasait donc a CHAQUE
# redemarrage tous les reglages avances qu il contient (RSI, TTP, flux, SL…) :
# un reglage modifie dans l interface revenait silencieusement a sa valeur
# par defaut au deploiement suivant. Ordre corrige : profil d abord, puis les
# reglages de l utilisateur par-dessus.
_saved_overrides = db.get_all_config_overrides()
be.apply_profile(cfg, _saved_overrides.get("PROFILE", cfg.get("PROFILE", "swing")))
for k, v in _saved_overrides.items():
    cfg[k] = v

if db.get_meta("initial_balance") is None:
    db.set_meta("initial_balance", str(cfg["CAPITAL_USD"]))
if db.get_meta("reset_at") is None:
    db.set_meta("reset_at", db.now_iso())

# ── Diagnostic de persistance ────────────────────────────────────────────
# BOOT_COUNT est stocke en base : s il repart TOUJOURS a 1 apres chaque
# redeploiement Railway (au lieu de s incrementer 1, 2, 3...), c est la
# preuve que les donnees ne persistent pas — HYPERBOT_DATA_DIR ne pointe
# pas vers un Volume monte. Visible dans /health et dans les logs de
# demarrage.
BOOT_COUNT = int(db.get_meta("boot_count", "0")) + 1
db.set_meta("boot_count", str(BOOT_COUNT))
_startup_warnings = []
if not _DATA_DIR_CONFIGURED:
    _startup_warnings.append(
        "HYPERBOT_DATA_DIR n est pas definie — les donnees (base, capital, cle "
        "API) NE PERSISTERONT PAS entre deux redeploiements Railway. Cree un "
        "Volume (Settings > Volumes), monte-le par exemple sur /data, et "
        "definis HYPERBOT_DATA_DIR=/data."
    )
if BOOT_COUNT == 1:
    _startup_warnings.append(
        "Premier demarrage detecte pour cette base de donnees (boot_count=1). "
        "Si ce nombre repart TOUJOURS a 1 apres un redeploiement, le Volume "
        "n est pas correctement monte — voir /health."
    )

event_queue = queue.Queue()
bot = be.BotEngine(cfg, event_queue)
# v4.273 — trading manuel (doit exister AVANT le demarrage du moteur : la
# reprise des positions tient compte des positions manuelles)
bot.manual = manual_trading.ManualTrading(bot)

log_buffer = deque(maxlen=3000)
_state_lock = threading.Lock()

# v3.2 — FIX : l interface (index.html) attend un champ "message" (pas "msg")
# et des niveaux "success"/"warning"/"error" (tout le reste s affiche en
# gris) — alors que le bot (bot_engine.py) emet des niveaux "ok"/"warn"/
# "error"/"win"/"loss"/"info"/"signal"/"dim". Sans cette traduction, TOUS
# les logs s affichaient sans aucun texte visible (mauvais nom de champ) et
# sans les bonnes couleurs.
_LEVEL_MAP = {"ok": "success", "win": "success", "warn": "warning", "loss": "error", "error": "error"}


def _push_log(level_raw: str, msg: str):
    now = datetime.now(timezone.utc).isoformat()
    log_buffer.append({
        "time": now,
        "level": _LEVEL_MAP.get(level_raw, "info"),
        "message": msg,
    })
    # v4.244 — SUR DEMANDE EXPLICITE : persiste EGALEMENT les niveaux
    # significatifs (pas le bruit "info"/"dim"/"signal" repete a chaque
    # cycle) dans le journal permanent, consultable au-dela du buffer en
    # memoire (3000 lignes, quelques heures seulement).
    if level_raw in ("warn", "error", "ok", "win", "loss"):
        try:
            db.append_log_history(now, level_raw, msg)
        except Exception as e:
            print(f"[LOG-HISTORY] Echec ecriture journal permanent : {e}")


for _w in _startup_warnings:
    _push_log("warn", _w)


def _compute_protected_trade_ids():
    """IDs exacts (pas les coins) des trades en base correspondant a des
    positions REELLEMENT ouvertes en memoire du bot en ce moment — utilise
    a la fois par le nettoyage manuel et le balayage automatique au
    demarrage."""
    protected_ids = []
    # v4.264 — FIX BUG CRITIQUE : ne parcourait que bot.states — les
    # positions Accumulation (bot.accum_states) n etaient jamais protegees,
    # leur ligne etait supprimee par le nettoyage au demarrage, puis la
    # position etait reclassee "forex" au redeploiement suivant.
    for states in (bot.states, bot.accum_states):
        for slot_key, s in states.items():
            if not s.position:
                continue
            tid = db.get_open_trade_id_by_uid(s.position.get("trade_uid"))
            if not tid:
                ticker = be.ticker_from_slot_key(slot_key)
                action = "LONG" if s.position["type"] == "long" else "SHORT"
                tid = db.get_open_trade_id_by_coin_action(ticker, action, s.position.get("strategy"))
            if tid:
                protected_ids.append(tid)
    # v4.273 — positions manuelles ouvertes
    for item in list(bot.manual.items.values()):
        if item.get("status") in ("open", "opening", "closing"):
            tid = db.get_open_trade_id_by_uid(item.get("trade_uid"))
            if tid:
                protected_ids.append(tid)
    return protected_ids


def _consume_events():
    """Tourne en tache de fond : lit la queue du BotEngine et persiste les
    evenements pertinents (logs en memoire, trades en base)."""
    while True:
        try:
            ev = event_queue.get(timeout=1)
        except queue.Empty:
            continue
        etype, data = ev.get("type"), ev.get("data") or {}
        try:
            if etype == "log":
                _push_log(data.get("level", "info"), data.get("msg", ""))
            elif etype == "ws_event":
                db.insert_ws_event(data.get("kind", "unknown"), data.get("message", ""))
            elif etype == "trade_opened":
                # v4.264 — deja ecrit de facon synchrone par bot_engine (trace
                # indexee par trade_uid) ; sinon repli : insertion/completion.
                if not data.get("persisted"):
                    db.upsert_open_trade(data)
            elif etype == "trade":
                ticker = be.ticker_from_slot_key(data.get("symbol", ""))
                action = "LONG" if data.get("type") == "long" else "SHORT"
                # v4.264 — correspondance EXACTE par identifiant de trade,
                # repli sur coin/sens/strategie pour les trades anciens.
                trade_id = db.get_open_trade_id_by_uid(data.get("trade_uid"))
                if not trade_id:
                    trade_id = db.get_open_trade_id_by_coin_action(ticker, action, data.get("strategy"))
                if trade_id:
                    db.close_trade(trade_id, data.get("exit"), data.get("pnl"), data.get("reason"), peak_pnl=data.get("peak_pnl_usd"), peak_pnl_pct=data.get("peak_pnl_pct"), fees_paid=data.get("fees_paid"), exit_price_source=data.get("exit_price_source"))
                else:
                    # v3.2 — diagnostic : auparavant, si aucune ligne ouverte
                    # ne correspondait (coin/action), la fermeture etait
                    # perdue EN SILENCE (le trade restait "ouvert" en base
                    # pour toujours, jamais comptabilise dans le Bilan, meme
                    # si la position etait bien fermee en memoire).
                    # v4.220 — SUR DEMANDE EXPLICITE, FIX BUG CRITIQUE : ne
                    # se contente plus de logger — cree un enregistrement de
                    # secours directement, pour ne plus jamais perdre le
                    # PnL/statistiques de cette fermeture, meme sans pouvoir
                    # retrouver la ligne d ouverture d origine (confidence,
                    # levier, SL/TP initiaux resteront vides pour cette
                    # ligne specifiquement, mais le PnL est preserve).
                    msg = f"[event_consumer] ATTENTION : aucun trade ouvert trouve en base pour {ticker}/{action} (symbol brut={data.get('symbol')!r}, type brut={data.get('type')!r}) — creation d un enregistrement de secours."
                    print(msg)
                    _push_log("error", msg)
                    db.insert_orphaned_closed_trade(
                        ticker, action, data.get("entry"), data.get("exit"), data.get("pnl"),
                        data.get("reason"), strategy=data.get("strategy"), trade_mode=data.get("trade_mode"),
                        peak_pnl=data.get("peak_pnl_usd"), peak_pnl_pct=data.get("peak_pnl_pct"),
                        fees_paid=data.get("fees_paid"),
                    )
            elif etype == "startup_ready":
                # v3.2 — "programme balai" automatique au demarrage : a ce
                # stade, la reconciliation Hyperliquid (mode live) ou la
                # restauration des positions paper (voir bot_engine.py) est
                # DEJA terminee — bot.states reflete donc l etat REEL exact.
                # Tout trade "ouvert" en base qui ne correspond a AUCUNE
                # position reelle a cet instant precis est forcement un
                # orphelin (pas besoin d attendre 24h de delai comme pour le
                # nettoyage manuel en cours de session, ou l incertitude est
                # plus grande) — nettoyage immediat pour un tableau de bord
                # propre des le demarrage.
                try:
                    protected_ids = _compute_protected_trade_ids()
                    dup, stale = db.cleanup_signals(stale_hours=0, protected_ids=protected_ids)
                    if dup or stale:
                        msg = f"🧹 Nettoyage automatique au demarrage : {dup} doublon(s) + {stale} orphelin(s) supprime(s)."
                        print(f"[AUDIT] {msg}")
                        _push_log("ok", msg)
                except Exception as e:
                    print(f"[event_consumer] Erreur nettoyage automatique au demarrage : {e}")
            elif etype == "active_coins_auto_added":
                # v3.2 — un actif inactif vient d etre auto-active suite a une
                # opportunite de tres forte confiance (voir
                # _gate_active_or_auto_activate dans bot_engine.py). On
                # persiste la nouvelle liste pour qu elle survive aux
                # redemarrages, exactement comme un changement manuel.
                new_list = data.get("active_coins")
                if new_list:
                    db.set_config_override("ACTIVE_COINS", new_list)
                    print(f"[AUDIT] ACTIVE_COINS auto-etendu suite a une opportunite forte sur {data.get('ticker')} : {new_list}")
        except Exception as e:
            print(f"[event_consumer] Erreur traitement evenement {etype}: {e}")


threading.Thread(target=_consume_events, daemon=True).start()

app = FastAPI(title="HyperBot API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.on_event("shutdown")
def _on_shutdown():
    # Best-effort : accumule le temps de fonctionnement si le process
    # s arrete proprement (Railway envoie SIGTERM avant un redeploiement).
    # Ne couvre pas un arret brutal (crash, kill -9) — dans ce cas, la
    # derniere fraction de temps depuis "running_since" ne sera comptee
    # qu au prochain calcul via _get_running_seconds si "running_since"
    # est encore renseigne (le calcul inclut deja la session en cours).
    # v4.14 — trading_enabled (pas running, qui reste toujours actif tant
    # que le process tourne desormais) : ce compteur mesure le temps de
    # TRADING actif, pas la duree de vie du process/moteur.
    if bot.trading_enabled:
        _mark_running_stop_and_accumulate()


# ─────────────────────────────────────────────────────────────────────────
#  TEMPS DE FONCTIONNEMENT REEL (exclut les periodes d arret)
# ─────────────────────────────────────────────────────────────────────────
# total_running_seconds (persiste en base) + running_since (horodatage du
# demarrage en cours, tant que le bot tourne). La duree affichee au client
# ne compte donc que le temps ou le bot a reellement tourne, jamais les
# periodes ou il etait arrete — contrairement a un simple "temps ecoule
# depuis le dernier reset".
def _mark_running_start():
    db.set_meta("running_since", db.now_iso())


def _mark_running_stop_and_accumulate():
    since = db.get_meta("running_since")
    if not since:
        return
    try:
        started = datetime.fromisoformat(since)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        total = float(db.get_meta("total_running_seconds", "0")) + max(elapsed, 0)
        db.set_meta("total_running_seconds", str(total))
    except Exception as e:
        print(f"[uptime] Erreur calcul temps de fonctionnement: {e}")
    finally:
        db.set_meta("running_since", "")


def _get_running_seconds() -> float:
    """Temps de fonctionnement cumule, y compris la session en cours si le
    bot tourne actuellement (sans attendre un arret pour la comptabiliser)."""
    total = float(db.get_meta("total_running_seconds", "0"))
    since = db.get_meta("running_since")
    if since:
        try:
            started = datetime.fromisoformat(since)
            total += max((datetime.now(timezone.utc) - started).total_seconds(), 0)
        except Exception:
            pass
    return total


# ─────────────────────────────────────────────────────────────────────────
#  AUTO-DEMARRAGE PERSISTANT (independant de la page web)
# ─────────────────────────────────────────────────────────────────────────
# Le bot tourne cote serveur (dans ce process), pas dans le navigateur : une
# fois lance, il continue de tourner meme navigateur ferme. Pour qu il
# redemarre TOUT SEUL apres un redeploiement/redemarrage Railway (sans
# intervention manuelle sur la page web), on persiste un etat souhaite
# ("running"/"stopped") en base, mis a jour par /api/bot/start et
# /api/bot/stop. Au boot du process, on relit cet etat :
#   - "running" (valeur par defaut, y compris au tout premier deploiement)
#     -> demarrage automatique si la cle/wallet sont configures
#   - "stopped" (l utilisateur a explicitement clique ARRETER) -> reste
#     arrete tant qu il ne clique pas DEMARRER, meme apres un redeploiement.
def _auto_start_if_desired():
    # v4.14 — SUR DEMANDE EXPLICITE : le MOTEUR (collecte de prix,
    # indicateurs, WebSocket, gestion des positions ouvertes) doit tourner
    # en continu des que le process demarre, INDEPENDAMMENT du dernier etat
    # "running"/"stopped" choisi par l utilisateur — cet etat persiste ne
    # controle desormais plus que l ouverture de NOUVEAUX trades.
    if not cfg.get("PRIVATE_KEY") or not cfg.get("WALLET_ADDRESS"):
        _push_log("warn", "Moteur non demarre : cle API / wallet Hyperliquid non configures. Configurez-les puis redemarrez le service.")
        return
    bot.start_engine()
    _push_log("ok", "Moteur demarre (collecte de prix/indicateurs, WebSocket) — actif en continu.")

    desired = db.get_meta("bot_desired_state", "running")
    if desired != "running":
        _push_log("info", "Trading non active automatiquement (dernier etat : ARRETE manuellement). Le moteur tourne, mais aucun nouveau trade ne s ouvrira tant que DEMARRER n est pas reclique.")
        return
    bot.start()
    _mark_running_start()
    _push_log("ok", "Trading active automatiquement (etat persistant : en cours d execution).")


_auto_start_if_desired()


# ─────────────────────────────────────────────────────────────────────────
#  AUTHENTIFICATION
# ─────────────────────────────────────────────────────────────────────────
class AuthBody(BaseModel):
    email: str
    password: str


def require_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Authentification requise")
    token = authorization.split(" ", 1)[1]
    email = auth.decode_token(token)
    if not email:
        raise HTTPException(401, "Token invalide ou expire")
    # v4.264 — le compte doit toujours exister (un token d un compte supprime
    # ne donne plus acces au bot).
    if not db.get_user_by_email(email):
        raise HTTPException(401, "Compte inconnu")
    return email


@app.post("/api/register")
def register(body: AuthBody):
    # Un seul compte proprietaire pour ce bot — l inscription se ferme
    # d elle-meme des qu un premier compte existe (evite qu un tiers
    # s inscrive et prenne le controle du bot si l URL fuite).
    if db.user_count() > 0:
        raise HTTPException(403, "Inscription fermee — un compte existe deja sur cette instance")
    if len(body.password) < 8:
        raise HTTPException(400, "Mot de passe trop court (8 caracteres minimum)")
    if db.get_user_by_email(body.email):
        raise HTTPException(400, "Ce compte existe deja")
    db.create_user(body.email, auth.hash_password(body.password))
    return {"ok": True}


@app.post("/api/login")
def login(body: AuthBody):
    user = db.get_user_by_email(body.email)
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Email ou mot de passe incorrect")
    token = auth.create_token(body.email)
    return {"token": token, "email": body.email}


@app.post("/api/logout")
def logout(email: str = Depends(require_user)):
    # JWT sans etat : rien a invalider cote serveur, le client oublie le token.
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────
#  HELPERS DE MAPPING (bot_engine <-> contrat API)
# ─────────────────────────────────────────────────────────────────────────
def _mask(secret: Optional[str]) -> str:
    if not secret:
        return ""
    return "****" + secret[-4:] if len(secret) > 4 else "****"


def _public_config() -> Dict[str, Any]:
    last_times = [s.last_price_time for s in bot.states.values() if s.last_price_time]
    return {
        "trading_mode": cfg.get("MODE", "paper"),
        "profile": cfg.get("PROFILE", "swing"),
        "position_pct": cfg.get("POSITION_SIZE_PCT"),
        # v4.10 — moteur ASYMETRIQUE : SL en % de E (perte $ plafonnee,
        # independante du levier), TP en % de mouvement de prix (gain $
        # amplifie par le levier). max_loss_usd/quick_profit_usd = valeurs $
        # informatives a titre indicatif (capital courant, levier x1 pour le TP).
        # Utiliser sl_pct_of_e / ttp_arm1_price_pct pour les valeurs reelles.
        "max_loss_usd": round(cfg["CAPITAL_USD"] * cfg["POSITION_SIZE_PCT"] / 100 * cfg.get("SL_PCT_OF_E", 1.0) / 100, 4),
        "quick_profit_usd": round(cfg["CAPITAL_USD"] * cfg["POSITION_SIZE_PCT"] / 100 * cfg.get("TTP_ARM1_PRICE_PCT", 1.0) / 100, 4),
        "sl_pct_of_e": cfg.get("SL_PCT_OF_E", 1.0),
        "ttp_arm1_price_pct": cfg.get("TTP_ARM1_PRICE_PCT", 1.0),
        "ttp_lock1_price_pct": cfg.get("TTP_LOCK1_PRICE_PCT", 0.8),
        "ttp_arm2_price_pct": cfg.get("TTP_ARM2_PRICE_PCT", 1.3),
        "ttp_trail_gap_price_pct": cfg.get("TTP_TRAIL_GAP_PRICE_PCT", 0.3),
        "max_open_trades": cfg.get("MAX_OPEN_TRADES", 15),
        "auto_activate_confidence_pct": cfg.get("AUTO_ACTIVATE_CONFIDENCE_PCT", 80.0),
        "active_coins": cfg.get("ACTIVE_COINS") or SUPPORTED_TICKERS,
        "manual_exclude_coins": cfg.get("MANUAL_EXCLUDE_COINS", []),
        "supported_coins": SUPPORTED_TICKERS,
        "wallet": cfg.get("WALLET_ADDRESS", ""),
        "api_key": _mask(cfg.get("PRIVATE_KEY", "")),
        "finnhub_key": _mask(cfg.get("FINNHUB_API_KEY", "")),
        "filter_hours": cfg.get("CRYPTO_OFFPEAK_ENABLED", True),
        "filter_weekend": bool(cfg.get("FOREX_SYMBOLS")),
        "filter_macro": cfg.get("CPI_BLACKOUT_ENABLED", True),
        "accumulation_enabled": cfg.get("ACCUMULATION_ENABLED", False),
        "accumulation_require_trend_confirm": cfg.get("ACCUMULATION_REQUIRE_TREND_CONFIRM", False),
        "accumulation_max_trades": cfg.get("ACCUMULATION_MAX_TRADES", 3),
        "accumulation_proximity_pct": cfg.get("ACCUMULATION_PROXIMITY_PCT", 1.0),
        "sl_ttp_adaptive_enabled": cfg.get("SL_TTP_ADAPTIVE_ENABLED", False),
        "funding_mode_enabled": cfg.get("FUNDING_MODE_ENABLED", False),
        "funding_mode_live_allowed": cfg.get("FUNDING_MODE_LIVE_ALLOWED", False),
        "strategy_mode_override": cfg.get("STRATEGY_MODE_OVERRIDE", {
            "forex": None, "accumulation": None, "funding_contrarian": None, "spot_accumulation": None,
        }),
        "strategy_trading_enabled": cfg.get("STRATEGY_TRADING_ENABLED", {
            "forex": True, "accumulation": True, "funding_contrarian": True, "spot_accumulation": True,
        }),
        "require_sr_ema200_separation": cfg.get("REQUIRE_SR_EMA200_SEPARATION", True),
        "unified_require_sr_amplitude": cfg.get("UNIFIED_REQUIRE_SR_AMPLITUDE", False),
        "unified_simplified_mode": cfg.get("UNIFIED_SIMPLIFIED_MODE", True),
        "unified_full_simplified_mode": cfg.get("UNIFIED_FULL_SIMPLIFIED_MODE", True),
        "ttp_trend_hold_filter_enabled": cfg.get("TTP_TREND_HOLD_FILTER_ENABLED", True),
        "unified_require_adx_confirm": cfg.get("UNIFIED_REQUIRE_ADX_CONFIRM", False),
        "ttp_dynamic_from_arm1": cfg.get("TTP_DYNAMIC_FROM_ARM1", True),
        "spot_accum_enabled": cfg.get("SPOT_ACCUM_ENABLED", False),
        "spot_accum_sl_enabled": cfg.get("SPOT_ACCUM_SL_ENABLED", False),
        "spot_accum_require_adx_confirm": cfg.get("SPOT_ACCUM_REQUIRE_ADX_CONFIRM", False),
        "accumulation_reversal_exit_enabled": cfg.get("ACCUMULATION_REVERSAL_EXIT_ENABLED", True),
        "spot_accum_hard_sl_enabled": cfg.get("SPOT_ACCUM_HARD_SL_ENABLED", True),
        "accumulation_active_coins": cfg.get("ACCUMULATION_ACTIVE_COINS"),
        "funding_active_coins": cfg.get("FUNDING_ACTIVE_COINS"),
        "spot_accum_active_coins": cfg.get("SPOT_ACCUM_ACTIVE_COINS"),
        "ai_continuous": db.get_config_override("ai_continuous", False),
        "running": bot.trading_enabled,
        "is_running": bot.trading_enabled,
        "engine_running": bot.running,  # v4.14 — moteur (collecte/WS), toujours actif independamment du trading
        "started_at": db.get_meta("running_since") or None,
        "last_scan": max(last_times).isoformat() if last_times else None,
        "ws_connected": bot.info is not None,
        "ws_healthy": bot._is_ws_healthy() if bot.info is not None else False,
        "offpeak_hour_start": cfg.get("CRYPTO_OFFPEAK_HOUR_START_UTC", 21),
        "offpeak_hour_end": cfg.get("CRYPTO_OFFPEAK_HOUR_END_UTC", 23),
        "hyperliquid_configured": bool(cfg.get("PRIVATE_KEY") and cfg.get("WALLET_ADDRESS")),
    }


def _apply_and_persist(key: str, value):
    cfg[key] = value
    db.set_config_override(key, value)


def _analyze_confidence_calibration(min_samples: int = 15):
    """v4.2 — Calibration du score de confiance a partir des resultats REELS
    des trades clotures, plutot que de se fier a des poids choisis a la
    main dans le code. Pour chaque indicateur present dans
    confidence_breakdown (macd, bollinger, volume, ema200, ema_mid,
    momentum, consec, breakout), compare le taux de reussite des trades ou
    il etait confirme vs ceux ou il ne l etait pas :
      - Si l ecart (lift) est positif et mesure sur un echantillon
        suffisant dans les deux groupes (>= min_samples), l indicateur a
        vraiment un pouvoir predictif sur CE bot / CES marches -> son poids
        est augmente proportionnellement.
      - Si l ecart est nul/negatif, l indicateur n apporte rien de mesurable
        ici -> son poids diminue (mais ne tombe jamais a zero strict, pour
        eviter de l exclure definitivement sur un echantillon qui pourrait
        encore etre bruite).
      - Si l echantillon est insuffisant dans un des deux groupes, on ne
        touche PAS a son poids actuel (mieux vaut garder le defaut que de
        calibrer sur trop peu de donnees) et on le signale clairement.
    Le total de points redistribue entre indicateurs calibrables est
    preserve, pour que les seuils de confiance en % (65%, 75%, 85%...)
    gardent le meme ordre de grandeur apres calibration.
    """
    trades = db.get_all_closed_trades()
    rows = []
    for t in trades:
        bd = t.get("confidence_breakdown")
        pnl = t.get("pnl")
        if not bd or pnl is None:
            continue
        try:
            bd = json.loads(bd)
        except Exception:
            continue
        rows.append({"breakdown": bd, "win": pnl > 0})

    total = len(rows)
    overall_wins = sum(1 for r in rows if r["win"])
    overall_win_rate = round(overall_wins / total * 100, 1) if total else 0.0

    current_weights = cfg.get("CONFIDENCE_WEIGHTS", {})
    all_keys = set(current_weights.keys())
    for r in rows:
        all_keys |= set(r["breakdown"].keys())

    per_indicator = {}
    for key in sorted(all_keys):
        true_rows  = [r for r in rows if r["breakdown"].get(key) is True]
        false_rows = [r for r in rows if r["breakdown"].get(key) is False]
        n_true, n_false = len(true_rows), len(false_rows)
        wr_true  = round(sum(1 for r in true_rows if r["win"]) / n_true * 100, 1) if n_true else None
        wr_false = round(sum(1 for r in false_rows if r["win"]) / n_false * 100, 1) if n_false else None
        enough_data = n_true >= min_samples and n_false >= min_samples
        lift = round(wr_true - wr_false, 1) if enough_data else None
        per_indicator[key] = {
            "current_weight": current_weights.get(key, 0),
            "n_true": n_true, "n_false": n_false,
            "win_rate_true": wr_true, "win_rate_false": wr_false,
            "lift_pts": lift, "enough_data": enough_data,
        }

    calibratable = [k for k, v in per_indicator.items() if v["enough_data"]]
    new_weights = dict(current_weights)
    if calibratable:
        pool = sum(current_weights.get(k, 0) for k in calibratable)
        raw = {k: max(per_indicator[k]["lift_pts"], 0) + 0.5 for k in calibratable}
        raw_total = sum(raw.values())
        for k in calibratable:
            new_weights[k] = round(raw[k] / raw_total * pool, 1) if raw_total > 0 else current_weights.get(k, 0)
    for k, v in per_indicator.items():
        v["suggested_weight"] = new_weights.get(k, v["current_weight"])

    return {
        "total_closed_trades": len(trades),
        "trades_with_breakdown": total,
        "overall_win_rate": overall_win_rate,
        "min_samples_required": min_samples,
        "indicators": per_indicator,
        "current_weights": current_weights,
        "suggested_weights": new_weights,
        "ready_to_calibrate": len(calibratable) > 0,
    }


def _analyze_confidence_by_asset(min_trades: int = 5):
    """v4.4 — Probabilite de reussite REELLE par actif, calculee a partir de
    l historique des trades clotures (pas une estimation theorique). Pour
    chaque actif ayant deja des trades clotures : nombre de trades, taux de
    reussite, PnL net, confiance moyenne a l entree. min_trades est
    ajustable dynamiquement (parametre de requete) : plus il est bas, plus
    d actifs apparaissent tot, mais avec un echantillon moins fiable."""
    trades = db.get_all_closed_trades()
    by_coin = {}
    for t in trades:
        pnl = t.get("pnl")
        coin = t.get("coin")
        if pnl is None or not coin:
            continue
        by_coin.setdefault(coin, []).append(t)

    results = []
    for coin, rows in by_coin.items():
        n = len(rows)
        wins = [r for r in rows if (r.get("pnl") or 0) > 0]
        win_rate = round(len(wins) / n * 100, 1) if n else 0.0
        net = round(sum(r.get("pnl") or 0 for r in rows), 2)
        confs = [r.get("confidence") for r in rows if r.get("confidence") is not None]
        avg_conf = round(sum(confs) / len(confs), 1) if confs else None
        results.append({
            "coin": coin,
            "n_trades": n,
            "win_rate": win_rate,
            "net_pnl": net,
            "avg_confidence": avg_conf,
            "enough_data": n >= min_trades,
        })

    results.sort(key=lambda r: (r["enough_data"], r["win_rate"]), reverse=True)
    return {
        "min_trades_required": min_trades,
        "total_closed_trades": len(trades),
        "assets": results,
    }


def _open_positions() -> List[Dict[str, Any]]:
    out = []
    # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son PROPRE
    # emplacement (bot.accum_states), sans quoi ses positions n apparaitraient
    # JAMAIS dans l interface (affichage "Trades ouverts" de tous les
    # onglets, alimente par cette fonction). Combine les deux sources —
    # slot_key identique entre les deux dicts, sans risque de collision
    # dans la liste de sortie (chaque position devient une entree separee).
    combined_states = list(bot.states.items()) + list(bot.accum_states.items())
    for slot_key, state in combined_states:
        pos = state.position
        if not pos:
            continue
        try:
            ticker = be.ticker_from_slot_key(slot_key)
            price = state.current_price or pos["entry"]
            if pos["type"] == "long":
                raw_price_move_pct = (price - pos["entry"]) / pos["entry"] * 100
            else:
                raw_price_move_pct = (pos["entry"] - price) / pos["entry"] * 100
            # v4.189 — historique : bot_engine.py compare ses SEUILS internes
            # (SL, TTP) au mouvement de prix BRUT, jamais leverage-ajuste —
            # le % affiche doit rester BRUT pour correspondre a la decision
            # reelle du bot.
            # v4.229 — tentative d unifier sur un pnl_pct amplifie par le
            # levier, cote bot_engine.py ET ici — ANNULEE en urgence (v4.231)
            # suite a un effet retroactif dangereux sur les positions deja
            # ouvertes au redeploiement (fermetures en cascade cote bot,
            # non repercutees cote Hyperliquid). bot_engine.py est revenu au
            # % BRUT pour ses seuils internes — cet affichage doit donc
            # rester coherent avec CETTE decision reelle, pas avec le ROE%
            # d Hyperliquid (le montant $ ci-dessous, lui, reste correct et
            # leverage-ajuste — seul le % redevient brut).
            leverage_for_pnl = pos.get("leverage", 1)
            pnl_pct = raw_price_move_pct
            pnl = pos["size"] * leverage_for_pnl * pnl_pct / 100

            # opened_at est stocke par bot_engine.py au format "%d/%m/%Y %H:%M:%S"
            # (francais, sans fuseau) — converti en ISO pour que new Date(...) le
            # parse correctement cote navigateur (ambigu sinon selon le moteur JS).
            # v3.2 — FIX : sans le "+00:00" explicite, le navigateur interprete
            # cette heure comme une heure LOCALE (pas UTC), decalant l affichage
            # de plusieurs heures selon le fuseau du visiteur (durees et heures
            # d ouverture incoherentes, ex: "0min" alors que l heure affichee
            # semblait ancienne).
            opened_at_iso = None
            try:
                opened_at_iso = datetime.strptime(pos["opened_at"], "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass

            # Complements (leverage, take_profit1/2) recuperes depuis la ligne DB
            # ouverte correspondante — calcules une seule fois a l entree, voir
            # bot_engine.py (emit "trade_opened").
            action = "LONG" if pos["type"] == "long" else "SHORT"
            leverage = cfg.get("LEVERAGE", 1)
            tp1 = tp2 = None
            try:
                # v4.159 — FIX BUG CRITIQUE : transmet desormais la
                # strategie exacte de CETTE position pour eviter de
                # recuperer par erreur le levier/TP d une position d un
                # AUTRE mode sur le meme coin+action (ex: Normal ET
                # Accumulation shorts simultanes sur le meme actif).
                trade_id = db.get_open_trade_id_by_coin_action(ticker, action, pos.get("strategy"))
                if trade_id:
                    rows = db.get_trades(limit=1000)
                    match = next((r for r in rows if r["id"] == trade_id), None)
                    if match:
                        leverage = match.get("leverage") or leverage
                        tp1 = match.get("take_profit1")
                        tp2 = match.get("take_profit2")
            except Exception as e:
                print(f"[_open_positions] Erreur enrichissement DB pour {ticker}: {e}")

            # v4.15 — Pic de PnL latent atteint jusqu ici pendant la vie du
            # trade (trailing principal ou protection anticipee, selon
            # lequel est actif) — visible dans le panneau "Trades ouverts".
            # v4.188 — FIX BUG CRITIQUE : Spot-Accum a son PROPRE traqueur de
            # pic (state.spot_accum_peak_pnl_pct), separe et JAMAIS mis a
            # jour via peak_pnl_usd/tier0_peak_pnl_usd (utilises par les
            # AUTRES modes uniquement) — donnait un pic errone/perime
            # (parfois INFERIEUR au PnL actuel, incoherent), confirme par
            # une position reelle observee (pic affiche +0.20% alors que le
            # PnL courant etait deja a +0.77%).
            # v4.241 — SUR DEMANDE EXPLICITE, FIX BUG CRITIQUE : ces 4
            # strategies partagent desormais TOUTES le meme mecanisme de
            # sortie unifie, qui met a jour EXCLUSIVEMENT
            # state.spot_accum_peak_pnl_pct — les autres champs (tier0/
            # tier1/absolu, lus dans la branche "else" ci-dessous) ne sont
            # plus jamais mis a jour pour Accumulation/Forex/Funding
            # depuis l unification, risquant d y afficher un pic obsolete
            # ou absent.
            if pos.get("strategy") in ("spot_accumulation", "accumulation", "forex", "funding_contrarian"):
                peak_pnl_pct = round(state.spot_accum_peak_pnl_pct, 3) if state.spot_accum_peak_pnl_pct is not None else None
                # v4.231 — ROLLBACK URGENT de v4.229 (voir bot_engine.py) :
                # spot_accum_peak_pnl_pct est de nouveau un % BRUT — la
                # multiplication par le levier redevient necessaire ici.
                peak_pnl_usd = (
                    round(pos.get("size", 0) * pos.get("leverage", 1) * peak_pnl_pct / 100, 4)
                    if peak_pnl_pct is not None else None
                )
            else:
                # v4.18 — priorite tier1 > tier0 > pic absolu (des le 1er cycle en
                # profit, meme sous 0.5%) — voir close_position pour la meme logique.
                peak_pnl_usd = (
                    state.peak_pnl_usd if state.peak_pnl_usd is not None
                    else state.tier0_peak_pnl_usd if state.tier0_peak_pnl_usd is not None
                    else state.absolute_peak_pnl_usd
                )
                # v4.231 — ROLLBACK URGENT de v4.229 : peak_pnl_usd reste en
                # dollars reels (incluant le levier) — diviser par
                # (size*leverage) redonne le % BRUT, coherent avec le
                # pnl_pct BRUT restaure dans bot_engine.py.
                pos_size = pos.get("size", 0)
                pos_leverage = pos.get("leverage", 1)
                peak_pnl_pct = (
                    round(peak_pnl_usd / (pos_size * pos_leverage) * 100, 3)
                    if peak_pnl_usd is not None and pos_size and pos_leverage
                    else None
                )

            out.append({
                "id": slot_key,
                "coin": ticker,
                "action": action,
                "entry_price": pos["entry"],
                "current_price": price,
                "size": pos["size"],
                "size_usdc": round(pos["size"], 2),
                "leverage": leverage,
                "stop_loss": pos["sl"],
                "take_profit1": tp1,
                "take_profit2": tp2,
                "opened_at": opened_at_iso,
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 3),
                # v4.293 — rapprochement avec Hyperliquid (positions live)
                "pnl_hyperliquid": pos.get("hl_unrealized_pnl"),
                "hl_sync_age_sec": round(time.time() - pos["hl_sync_ts"]) if pos.get("hl_sync_ts") else None,
                "fees_est": round(pos["size"] * pos.get("leverage", 1) * 0.0009, 4),
                "pnl_net_est": round(pnl - pos["size"] * pos.get("leverage", 1) * 0.0009, 4),
                "peak_pnl": round(peak_pnl_usd, 4) if peak_pnl_usd is not None else None,
                "peak_pnl_pct": peak_pnl_pct,
                # v4.61 — SUR DEMANDE EXPLICITE : diagnostic precis du
                # mecanisme de trailing REELLEMENT actif sur ce trade, pour
                # eviter de devoir deviner ou chercher dans les logs bruts
                # (cas observe : pic affiche de 1.32% sans sortie declenchee
                # malgre un trailing dynamique cense s armer a 1%).
                "tp_stage": state.tp_stage,  # 0=aucun, 1=arme (tier1)
                "effective_mode": pos.get("effective_mode", "paper"),  # v4.90 — mode reel de CE trade
                "spot_accum_armed": state.spot_accum_armed,  # v4.62 — FIX : Spot-Accum a son propre armement, separe de tp_stage
                "spot_accum_arm_pct_used": cfg.get("SPOT_ACCUM_TTP_ARM_PCT"),  # v4.63 — seuil REELLEMENT lu, pour verifier sans deviner
                "spot_accum_peak_pnl_pct_internal": round(state.spot_accum_peak_pnl_pct, 3) if state.spot_accum_peak_pnl_pct is not None else None,
                "tier0_armed": state.tier0_armed,
                # v4.241 — SUR DEMANDE EXPLICITE, FIX BUG D AFFICHAGE : ces
                # 4 strategies partagent desormais TOUTES le meme mecanisme
                # de sortie unifie (state.spot_accum_armed), pas seulement
                # "spot_accumulation" — l ancienne condition affichait a
                # tort "aucun tier arme" pour Accumulation/Forex/Funding,
                # meme quand un pic avait reellement arme le trailing
                # (confirme par un cas reel : PENDLE short, pic +0.84%,
                # jamais montre comme arme).
                "peak_source": "spot_accum" if pos.get("strategy") in ("spot_accumulation", "accumulation", "forex", "funding_contrarian") else ("tier1" if state.peak_pnl_usd is not None else ("tier0" if state.tier0_peak_pnl_usd is not None else "absolu (aucun tier arme)")),
                # v4.243 — SUR DEMANDE EXPLICITE : tracabilite explicite du
                # mecanisme ayant valide cette entree (flirt, cassure
                # fraiche, volume, cassure ratee, etoile filante, pression
                # directionnelle, signal standard) — memorise a l ouverture,
                # jamais recalcule apres coup.
                "entry_mechanism": pos.get("entry_mechanism", "Non enregistré (position antérieure à ce suivi)"),
                "computed_exit_threshold_pct": (
                    round(peak_pnl_pct - cfg.get("TTP_DYNAMIC_TRAIL_GAP_PCT", 0.5), 3)
                    if state.tp_stage == 1 and peak_pnl_pct is not None and cfg.get("TTP_DYNAMIC_FROM_ARM1", True)
                    else None
                ),
                # v4.30 — seuils SL/TTP REELLEMENT appliques a CE trade (fixes
                # ou adaptatifs a l ATR, figes a l ouverture) — voir bot_engine.py
                # _finalize_open, stockes sur la position elle-meme.
                "sl_pct_used": pos.get("sl_pct_of_e"),
                "ttp_arm1_pct_used": pos.get("ttp_arm1_pct"),
                "ttp_lock1_pct_used": pos.get("ttp_lock1_pct"),
                "ttp_arm2_pct_used": pos.get("ttp_arm2_pct"),
                "ttp_gap_pct_used": pos.get("ttp_gap_pct"),
                "adaptive_sl_ttp": pos.get("adaptive_sl_ttp", False),
                "strategy": pos.get("strategy", "forex"),  # v4.34 — FIX : jamais expose ici avant
                "target_price": pos.get("target_price"),  # v4.47 — objectif Spot-Accum (modifiable)
                "trailing_arm_price": pos.get("trailing_arm_price"),  # v4.49 — seuil structurel d'armement du trailing (modifiable)
            })
        except Exception as e:
            print(f"[_open_positions] Erreur sur la position {slot_key}, ignoree pour cette reponse: {e}")
            continue
    return out


def _trade_row_to_signal(row: Dict[str, Any]) -> Dict[str, Any]:
    # v4.185 — historique : calculait le % amplifie par le levier.
    # v4.250 — SUR DEMANDE EXPLICITE, FIX BUG CRITIQUE : jamais inclus dans
    # le rollback d urgence (v4.231, voir bot_engine.py) qui est revenu au
    # % BRUT pour les positions OUVERTES — creant une incoherence : le
    # meme declenchement (plafond immediat a 0.5% brut) s affichait
    # correctement en position ouverte, mais AMPLIFIE PAR LE LEVIER une
    # fois passe en historique (confirme par un cas reel : plusieurs
    # "STOP LOSS (plafond immediat)" Spot-Accum affichant -2.5% a -3.5%
    # au lieu des ~0.5% reellement declenches, sur des trades a levier
    # x5). Retire la multiplication, coherent avec le % BRUT partout.
    exit_pnl_pct = None
    try:
        entry_p = row["entry_price"]
        exit_p = row["exit_price"]
        if entry_p and exit_p:
            raw_move_pct = ((exit_p - entry_p) / entry_p * 100) if row["action"] == "LONG" else ((entry_p - exit_p) / entry_p * 100)
            exit_pnl_pct = round(raw_move_pct, 3)
    except (TypeError, KeyError, ZeroDivisionError):
        pass
    return {
        "id": row["id"],
        "coin": row["coin"],
        "action": row["action"],
        "confidence": row["confidence"],
        "leverage": row["leverage"],
        "position_size": row["position_size_pct"],
        "risk_reward": row["risk_reward"],
        "timeframe": row["timeframe"],
        "entry": row["entry_price"],
        "price": row["entry_price"],
        "rsi": row["rsi"] if "rsi" in row.keys() else None,
        "entry_reasons": row["entry_reasons"] if "entry_reasons" in row.keys() else None,
        "stop_loss": row["stop_loss"],
        "take_profit1": row["take_profit1"],
        "take_profit2": row["take_profit2"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "exit_price": row["exit_price"],
        "pnl": row["pnl"],
        "pnl_pct": exit_pnl_pct,
        "reason": row["reason"],
        "strategy": row["strategy"] if "strategy" in row.keys() else "forex",
        "peak_pnl": row["peak_pnl"] if "peak_pnl" in row.keys() else None,
        "peak_pnl_pct": row["peak_pnl_pct"] if "peak_pnl_pct" in row.keys() else None,
        "size_usd": row["size_usd"] if "size_usd" in row.keys() else None,
        "sl_pct_used": row["sl_pct_used"] if "sl_pct_used" in row.keys() else None,
        "ttp_arm1_pct_used": row["ttp_arm1_pct_used"] if "ttp_arm1_pct_used" in row.keys() else None,
        "adaptive_sl_ttp": bool(row["adaptive_sl_ttp"]) if "adaptive_sl_ttp" in row.keys() and row["adaptive_sl_ttp"] is not None else False,
        # v4.294 — resultat REEL Hyperliquid (trades live)
        "trade_mode": row["trade_mode"] if "trade_mode" in row.keys() else None,
        "pnl_real_hl": row["pnl_real_hl"] if "pnl_real_hl" in row.keys() else None,
        "fees_real": row["fees_real"] if "fees_real" in row.keys() else None,
        "pnl_net_real": (row["pnl_real_hl"] - row["fees_real"])
                        if "pnl_real_hl" in row.keys() and row["pnl_real_hl"] is not None and row["fees_real"] is not None else None,
        "fills_status": row["fills_status"] if "fills_status" in row.keys() else None,
    }


# ─────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────
class ConfigBody(BaseModel):
    trading_mode: Optional[str] = None
    position_pct: Optional[float] = None
    # v4.0 — champs legacy en $, encore acceptes pour compat avec l interface
    # actuelle : convertis a la volee en % de mouvement de prix (voir put_config).
    max_loss_usd: Optional[float] = None
    quick_profit_usd: Optional[float] = None
    # v4.9 — nouveaux champs natifs, en % de MOUVEMENT DE PRIX (a privilegier)
    sl_pct_of_e: Optional[float] = None
    ttp_arm1_price_pct: Optional[float] = None
    ttp_lock1_price_pct: Optional[float] = None
    ttp_arm2_price_pct: Optional[float] = None
    ttp_trail_gap_price_pct: Optional[float] = None
    max_open_trades: Optional[int] = None
    auto_activate_confidence_pct: Optional[float] = None
    wallet: Optional[str] = None
    api_key: Optional[str] = None
    active_coins: Optional[List[str]] = None


# v3.2 — Reglages avances : whitelist de parametres de STRATEGIE surs a
# exposer/editer librement depuis l interface (indicateurs, filtres,
# session, confiance...). Volontairement separee des reglages "structurels"
# (SYMBOLS, cles API, wallet...) qui ont deja leurs propres formulaires
# dedies et securises — pas question de les rendre modifiables via un
# simple champ numerique generique.
ADVANCED_SETTINGS = {
    "RSI_PERIOD":              {"label": "RSI - Periode",                     "default": 14},
    # v4.266 — flux de transactions (Spot-Accum / Accumulation)
    "ENTRY_FLOW_CONFIRM_MIN_PRESSURE": {"label": "Flux - confirmation exigee a l entree (0 = desactivee, ex: 0.1)", "default": 0.0},
    "ENTRY_FLOW_CONTRADICTION_THRESHOLD": {"label": "Flux - veto si pression contraire au-dela de", "default": 0.3},
    "TRADE_FLOW_WINDOW_SEC":   {"label": "Flux - fenetre d analyse (secondes)", "default": 180},
    # v4.268 — TTP Funding
    "FUNDING_TTP_ARM_PCT":       {"label": "Funding - TTP armement (% de prix)", "default": 1.0},
    # v4.276 — regime de marche et plages horaires
    "MARKET_REGIME_FILTER_ENABLED": {"label": "Regime de marche - filtre actif (1 = oui, 0 = non)", "default": 1},
    "MARKET_REGIME_BREADTH_PCT": {"label": "Regime de marche - % minimal d actifs dans le meme sens", "default": 60},
    "SITUATION_RULES_ENABLED":         {"label": "Situations de marche fond 1h x court terme 5 min (1) ou regime global (0)", "default": 1},
    "ENTRY_ENGINE_SIMPLE":             {"label": "Moteur d entree simple Spot-Accum / Accumulation (1) ou ancienne chaine de conditions (0)", "default": 1},
    "SIMPLE_ENGINE_DYNAMIC_LEVERAGE":  {"label": "Moteur simple - levier dynamique 2-5x sur les entrees pres d un niveau (1/0)", "default": 0},
    "SITUATION_ALLOW_COUNTERTREND":    {"label": "Autoriser les trades a contre-tendance de fond (1 = oui, 0 = non)", "default": 0},
    "CONTINUATION_ENABLED":            {"label": "Voie continuation (entree sans proximite d un niveau) (1/0)", "default": 1},
    "CONTINUATION_PAPER_ONLY":         {"label": "Voie continuation en paper seulement, meme si le mode est live (1/0)", "default": 1},
    "CONTINUATION_MIN_FLOW":           {"label": "Continuation - flux minimal (2 lectures)", "default": 0.3},
    "CONTINUATION_MIN_ACTIVITY":       {"label": "Continuation - activite minimale (x habitude)", "default": 1.0},
    "CONTINUATION_MAX_EXTENSION_ATR":  {"label": "Continuation - ecart maximal a l EMA (en ATR 5 min)", "default": 1.5},
    "CONTINUATION_MIN_ROOM_X_SL":      {"label": "Continuation - marge minimale avant le niveau oppose (x le SL)", "default": 2.0},
    "SITUATION_REVERSAL_MIN_FLOW":     {"label": "Fin de repli / fin de rebond - flux minimal dans le sens du trade", "default": 0.2},
    "SITUATION_COUNTERTREND_MIN_FLOW": {"label": "Contre-tendance - flux minimal dans le sens du trade", "default": 0.3},
    "SITUATION_MIN_ROOM_PCT":          {"label": "Contre-tendance - marge minimale avant le niveau 1h oppose (%)", "default": 1.0},
    "COUNTERTREND_TTP_MULT":           {"label": "Contre-tendance - facteur du trailing (0,6 = arme et repli a 60 %)", "default": 0.6},
    "ANTI_RANGE_RELATIVE_ENABLED": {"label": "Anti-range relatif a l actif (1) ou seuils absolus (0)", "default": 1},
    "ANTI_RANGE_REL_MULT":         {"label": "Anti-range relatif - part de l amplitude habituelle exigee", "default": 0.6},
    "MARKET_QUALITY_MIN_VOL_RATIO":      {"label": "Qualite - volatilite minimale (x habitude, 0 = off)", "default": 0.0},
    "MARKET_QUALITY_MIN_ACTIVITY_RATIO": {"label": "Qualite - activite minimale (x habitude, 0 = off)", "default": 0.0},
    "SPOT_ACCUM_MIN_FLOW_CONVICTION":    {"label": "Spot-Accum - conviction minimale du flux |pression| (0 = off)", "default": 0.0},
    "ACCUMULATION_MIN_FLOW_CONVICTION":  {"label": "Accumulation - conviction minimale du flux |pression| (0 = off)", "default": 0.0},
    "FUNDING_MIN_FLOW_CONVICTION":       {"label": "Funding - conviction minimale du flux |pression| (0 = off)", "default": 0.0},
    "FOREX_MIN_FLOW_CONVICTION":         {"label": "Forex - conviction minimale du flux |pression| (0 = off)", "default": 0.0},
    "MARKET_QUALITY_MAX_SPREAD_PCT":     {"label": "Qualite - spread maximal a l entree (% du prix, 0 = off)", "default": 0.0},
    "SPOT_ACCUM_BYPASS_REQUIRE_TREND": {"label": "Spot-Accum - cassures/rebonds exigent la tendance EMA200 (1/0)", "default": 1},
    "SPOT_ACCUM_RISING_SUPPORT_ENABLED": {"label": "Spot-Accum - achat sur repli (support ascendant) (1/0)", "default": 1},
    "SPOT_ACCUM_FRESH_BREAKOUT_COUNTER_TREND":   {"label": "Spot-Accum - cassure fraiche autorisee contre l EMA de tendance (1/0)", "default": 0},
    "ACCUMULATION_FRESH_BREAKOUT_COUNTER_TREND": {"label": "Accumulation - cassure fraiche autorisee contre l EMA de tendance (1/0)", "default": 0},
    "FRESH_BREAKOUT_COUNTER_TREND_MIN_FLOW":     {"label": "Cassure fraiche contre-tendance - flux minimal exige", "default": 0.2},
    "SPOT_ACCUM_PIVOT_CANDLES": {"label": "Spot-Accum - creux : bougies 1h de chaque cote", "default": 2},
    "SPOT_ACCUM_BYPASS_FLOW_VETO":     {"label": "Spot-Accum - cassures/rebonds soumis au veto du flux (1/0)", "default": 1},
    "ACCUMULATION_BYPASS_REQUIRE_TREND": {"label": "Accumulation - cassures/rejets exigent la tendance EMA200 (1/0)", "default": 1},
    "ACCUMULATION_BYPASS_FLOW_VETO":     {"label": "Accumulation - cassures/rejets soumis au veto du flux (1/0)", "default": 1},
    "SPOT_ACCUM_TRADE_HOUR_START_UTC":   {"label": "Spot-Accum - debut plage horaire (h UTC)", "default": 0},
    "SPOT_ACCUM_TRADE_HOUR_END_UTC":     {"label": "Spot-Accum - fin plage horaire (h UTC, 24 = toujours)", "default": 24},
    "ACCUMULATION_TRADE_HOUR_START_UTC": {"label": "Accumulation - debut plage horaire (h UTC)", "default": 0},
    "ACCUMULATION_TRADE_HOUR_END_UTC":   {"label": "Accumulation - fin plage horaire (h UTC, 24 = toujours)", "default": 24},
    "FUNDING_TRADE_HOUR_START_UTC":      {"label": "Funding - debut plage horaire (h UTC)", "default": 0},
    "FUNDING_TRADE_HOUR_END_UTC":        {"label": "Funding - fin plage horaire (h UTC, 24 = toujours)", "default": 24},
    # v4.269 — reglages par mode (vide = herite du reglage general)
    "SPOT_ACCUM_SL_CAP_PCT":     {"label": "Spot-Accum - SL plafond (% de prix, vide = 0,5)", "default": None},
    "SPOT_ACCUM_SL_FLOW_THRESHOLD": {"label": "Spot-Accum - patience SL : pression acheteuse minimale", "default": 0.2},
    "SPOT_ACCUM_SL_FLOW_MAX_PCT":   {"label": "Spot-Accum - patience SL : perte maximale toleree (%)", "default": 1.0},
    "SPOT_ACCUM_SL_FLOW_MAX_WAIT_SEC": {"label": "Spot-Accum - patience SL : duree maximale (s, 0 = desactivee)", "default": 900},
    "SPOT_ACCUM_SL_PATIENCE_REQUIRE_TREND": {"label": "Spot-Accum - patience SL : exiger la tendance EMA200 (1 = oui, 0 = non)", "default": 1},
    "ACCUMULATION_SL_CAP_PCT":   {"label": "Accumulation - SL plafond (% de prix, vide = 0,5)", "default": None},
    "SPOT_ACCUM_ENTRY_FLOW_CONFIRM_MIN": {"label": "Spot-Accum - confirmation flux a l entree (vide = reglage general)", "default": None},
    "ACCUMULATION_ENTRY_FLOW_CONFIRM_MIN": {"label": "Accumulation - confirmation flux a l entree (vide = reglage general)", "default": None},
    "ACCUMULATION_LOSS_COOLDOWN_SEC": {"label": "Accumulation - delai apres perte sur un actif (s, 0 = aucun)", "default": 3600},
    "SPOT_ACCUM_LOSS_COOLDOWN_SEC":   {"label": "Spot-Accum - delai apres perte sur un actif (s, 0 = aucun)", "default": 0},
    "FUNDING_LOSS_COOLDOWN_SEC":      {"label": "Funding - delai apres perte sur un actif (s, 0 = aucun)", "default": 0},
    "ACCUMULATION_MAX_ENTRIES_PER_WINDOW": {"label": "Accumulation - entrees max par fenetre (0 = illimite)", "default": 3},
    "SPOT_ACCUM_MAX_ENTRIES_PER_WINDOW":   {"label": "Spot-Accum - entrees max par fenetre (0 = illimite)", "default": 0},
    "ENTRY_BURST_WINDOW_SEC":    {"label": "Fenetre de comptage des entrees (s)", "default": 600},
    "FUNDING_TTP_TOLERANCE_PCT": {"label": "Funding - TTP repli normal (%)", "default": 0.5},
    "FUNDING_TTP_FLOW_MAX_TOLERANCE_PCT": {"label": "Funding - TTP repli si flux favorable (patience, %)", "default": 0.9},
    "FUNDING_TTP_FLOW_FAST_TOLERANCE_PCT": {"label": "Funding - TTP repli si flux contraire (%)", "default": 0.25},
    "FUNDING_TTP_FLOW_THRESHOLD": {"label": "Funding - TTP seuil de flux favorable/contraire", "default": 0.2},
    "FUNDING_TTP_MIN_LOCK_PCT":  {"label": "Funding - TTP plancher de gain une fois arme (%)", "default": 0.3},
    "RSI_OVERSOLD":            {"label": "RSI - Seuil survente",              "default": 32},
    "RSI_OVERBOUGHT":          {"label": "RSI - Seuil surachat",              "default": 68},
    "RSI_EXTREME_LOW":         {"label": "RSI - Zone survente extreme (no SHORT sous)",  "default": 15},
    "RSI_EXTREME_HIGH":        {"label": "RSI - Zone surachat extreme (no LONG au-dessus)", "default": 85},
    "EMA_SHORT":               {"label": "EMA courte - periode",              "default": 12},
    "EMA_LONG":                {"label": "EMA longue - periode",              "default": 26},
    "EMA_MID_PERIOD":          {"label": "EMA intermediaire - periode (cycles, ~33min a 10s/cycle)", "default": 200},
    "MACD_FAST":               {"label": "MACD - rapide",                    "default": 12},
    "MACD_SLOW":               {"label": "MACD - lent",                      "default": 26},
    "MACD_SIGNAL":             {"label": "MACD - signal",                    "default": 9},
    "BB_PERIOD":               {"label": "Bollinger - periode",              "default": 20},
    "BB_STD":                  {"label": "Bollinger - ecart-type",           "default": 2.0},
    "ATR_PERIOD":              {"label": "ATR - periode (cycles)",           "default": 21},
    "ATR_MIN_PCT":             {"label": "ATR - seuil min % (filtre marche calme)", "default": 0.015},
    "ADX_PERIOD":              {"label": "ADX - periode",                    "default": 14},
    "ADX_TREND_THRESHOLD":     {"label": "ADX - seuil Trend/Reversal",       "default": 25.0},
    "ACCUMULATION_ADX_TREND_THRESHOLD": {"label": "Accumulation - seuil ADX dedie (distinct du mode normal)", "default": 20.0},
    "ACCUMULATION_MIN_ABOVE_SUPPORT_PCT": {"label": "Accumulation - minimum fenetre proximite (% de l'amplitude, dedie)", "default": 5.0},
    "ACCUMULATION_MAX_ABOVE_SUPPORT_PCT": {"label": "Accumulation - maximum fenetre proximite (% de l'amplitude, dedie)", "default": 15.0},
    "ACCUMULATION_ANTI_RANGE_MIN_PCT": {"label": "Accumulation - mouvement minimum requis pour eviter le range (%)", "default": 2.0},
    "ACCUMULATION_MIN_AMPLITUDE_TO_ATR_RATIO": {"label": "Accumulation - amplitude S/R minimale, en multiple de l'ATR", "default": 3.0},
    "ACCUMULATION_ANTI_RANGE_LOOKBACK": {"label": "Accumulation - echantillons pour le detecteur de range (200 = 6h40, aligne sur EMA200)", "default": 200},
    "SPOT_ACCUM_ANTI_RANGE_MIN_PCT": {"label": "Spot-Accum - mouvement minimum requis pour eviter le range (%)", "default": 2.0},
    "SPOT_ACCUM_ANTI_RANGE_LOOKBACK": {"label": "Spot-Accum - echantillons pour le detecteur de range (200 = 6h40, aligne sur EMA200)", "default": 200},
    "SPOT_ACCUM_TREND_STABILITY_CYCLES":   {"label": "Spot-Accum - stabilité tendance requise avant entrée (cycles ~10s)", "default": 24},
    "FOREX_TREND_STABILITY_CYCLES":       {"label": "Forex - stabilité tendance requise avant entrée (cycles ~10s)", "default": 12},
    "FOREX_ANTI_RANGE_MIN_PCT":  {"label": "Forex - mouvement minimal pour ne pas etre en range (%)", "default": 0.25},
    "FOREX_LONG_TERM_MOMENTUM_MIN_CHANGE_PCT": {"label": "Forex - momentum long terme minimal (%)", "default": 0.4},
    "ACCUMULATION_TREND_STABILITY_CYCLES": {"label": "Accumulation - stabilité tendance requise avant entrée (cycles ~10s)", "default": 24},
    "SR_PERIOD":               {"label": "Support/Resistance - periode (cycles, repli seulement)", "default": 50},
    "SR_PERIOD_CANDLES":       {"label": "Support/Resistance - periode (bougies ~2min, ex: 100=~3h20)", "default": 100},
    "SL_PCT_OF_E":                {"label": "Stop Loss (% de E)", "default": 1.0},
    "EXCHANGE_SAFETY_SL_MULT":    {"label": "SL Hyperliquid - multiple du Stop Loss bot", "default": 2.0},
    "TTP_ARM1_PRICE_PCT":          {"label": "TTP - 1er seuil d'armement (% de mouvement de prix)", "default": 1.0},
    "TTP_LOCK1_PRICE_PCT":         {"label": "TTP - seuil de sortie initial (% de mouvement de prix)", "default": 0.8},
    "TTP_ARM2_PRICE_PCT":          {"label": "TTP - 2e seuil, active le trailing continu (% de mouvement de prix)", "default": 1.3},
    "TTP_TRAIL_GAP_PRICE_PCT":     {"label": "TTP - marge de repli continue sous le pic (% de mouvement de prix)", "default": 0.3},
    "TTP_DYNAMIC_TRAIL_GAP_PCT":   {"label": "TTP - marge du trailing dynamique dès l'armement (%)", "default": 0.5},
    "ACCUMULATION_MAX_TRADES":     {"label": "Accumulation - trades simultanes max", "default": 3},
    "ACCUMULATION_PROXIMITY_PCT":  {"label": "Accumulation - proximite support/resistance (%)", "default": 1.0},
    "SL_ATR_MULTIPLIER":       {"label": "SL/TTP adaptatif - multiplicateur ATR", "default": 2.0},
    "SL_PCT_MIN":              {"label": "SL/TTP adaptatif - plancher de securite (%)", "default": 0.3},
    "SL_MIN_PRICE_MOVE_PCT":   {"label": "SL - mouvement de prix minimum garanti, compense par le levier (%)", "default": 0.3},
    "TTP_MIN_ARM_PCT_FLOOR":   {"label": "TTP - plancher strict d'armement, anti trades microscopiques (%)", "default": 0.3},
    "TTP_MAX_GIVEBACK_PCT_OF_PEAK": {"label": "TTP - plafond de redonnage maximum du pic, même tendance intacte (%)", "default": 20.0},
    "TTP_MAX_GIVEBACK_MIN_PEAK_PCT": {"label": "TTP - pic minimum requis avant que le plafond de redonnage s'applique (%)", "default": 1.0},
    "TTP_SMALL_PEAK_GIVEBACK_PCT": {"label": "TTP - plafond de redonnage pour petits pics, si repli soutenu (%)", "default": 50.0},
    "TTP_SMALL_PEAK_GIVEBACK_MIN_CYCLES": {"label": "TTP - cycles consecutifs de repli requis avant fermeture (petits pics)", "default": 5},
    "TTP_MAX_GIVEBACK_PCT_SMALL_PEAK": {"label": "TTP - plafond de redonnage pour petits pics, sous le seuil ci-dessus (%)", "default": 50.0},
    "TIER0_ARM_RATIO_OF_SL":   {"label": "TTP - ratio fixe tier0 armement / SL", "default": 0.5},
    "TIER0_GAP_RATIO_OF_SL":   {"label": "TTP - ratio fixe tier0 marge de repli / SL", "default": 0.42},
    "TIER0_REARM_HYSTERESIS_PCT": {"label": "TTP - marge d'hysteresis anti-oscillation tier1->tier0 (%)", "default": 15.0},
    "SL_PCT_MAX":              {"label": "SL/TTP adaptatif - plafond de securite (%)", "default": 3.0},
    "FUNDING_ANNUAL_THRESHOLD_PCT": {"label": "Funding Contrarian - seuil annualise (%)", "default": 25.0},
    "FUNDING_MODE_MAX_TRADES":      {"label": "Funding Contrarian - trades simultanes max", "default": 3},
    "FUNDING_REFRESH_SEC":          {"label": "Funding Contrarian - frequence de rafraichissement (s)", "default": 300},
    "SR_EMA200_PROXIMITY_PCT":      {"label": "Separation S/R vs EMA200 - proximite (%)", "default": 0.5},
    "UNIFIED_MIN_ABOVE_SUPPORT_PCT":   {"label": "Base commune - minimum au-dessus du niveau (% de l'amplitude S/R)", "default": 5.0},
    "UNIFIED_MAX_ABOVE_SUPPORT_PCT":   {"label": "Base commune - maximum au-dessus du niveau (% de l'amplitude S/R)", "default": 10.0},
    "UNIFIED_MIN_SR_AMPLITUDE_PCT":    {"label": "Base commune - amplitude minimale fourchette S/R (%)", "default": 4.0},
    "SPOT_ACCUM_MAX_TRADES":            {"label": "Spot-Accum - trades simultanes max", "default": 3},
    "SPOT_ACCUM_MIN_ABOVE_SUPPORT_PCT": {"label": "Spot-Accum - minimum au-dessus du support (% de l'amplitude S/R)", "default": 5.0},
    "SPOT_ACCUM_MIN_SR_AMPLITUDE_PCT": {"label": "Spot-Accum - amplitude minimale fourchette S/R (%)", "default": 2.0},
    "SPOT_ACCUM_MAX_ABOVE_SUPPORT_PCT": {"label": "Spot-Accum - maximum au-dessus du support (% de l'amplitude S/R)", "default": 10.0},
    "SPOT_ACCUM_TTP_ARM_PCT":           {"label": "Spot-Accum - TTP armement (% de PnL)", "default": 1.0},
    "SPOT_ACCUM_TTP_TOLERANCE_PCT":     {"label": "Spot-Accum - TTP marge de repli depuis le pic (%)", "default": 0.5},
    "SPOT_ACCUM_TARGET_SR_PCT":         {"label": "Spot-Accum - objectif (% distance support-resistance)", "default": 80.0},
    "SPOT_ACCUM_TRAILING_ARM_SR_PCT":   {"label": "Spot-Accum - armement trailing (% distance support-résistance)", "default": 70.0},
    "SPOT_ACCUM_SL_PCT_OF_PNL":         {"label": "Spot-Accum - SL simple (% du PnL)", "default": 1.5},
    "SPOT_ACCUM_HARD_SL_PCT":           {"label": "Spot-Accum - plafond dur (% du PnL, ferme sans condition)", "default": 5.0},
    "SPOT_ACCUM_REVERSAL_CONFIRM_CYCLES":    {"label": "Spot-Accum - retournement confirmé (cycles, ~10s chacun)", "default": 180},
    "SPOT_ACCUM_REVERSAL_MIN_EMA_MATURITY":  {"label": "Spot-Accum - maturité EMA200 minimale (bougies)", "default": 100},
    "ACCUMULATION_REVERSAL_CONFIRM_CYCLES":    {"label": "Accumulation - retournement confirmé (cycles, ~10s chacun)", "default": 180},
    "ACCUMULATION_REVERSAL_MIN_EMA_MATURITY":  {"label": "Accumulation - maturité EMA200 minimale (bougies)", "default": 100},
    "VOLUME_MIN_RATIO":        {"label": "Volume - ratio minimum vs moyenne","default": 1.2},
    "MOMENTUM_PERIOD":         {"label": "Momentum - periode (cycles)",      "default": 4},
    "MOMENTUM_THRESHOLD_PCT":  {"label": "Momentum - seuil %",               "default": 0.20},
    "CONFIDENCE_MIN_PCT":      {"label": "Confiance - seuil minimum %",      "default": 65.0},
    "CONFIDENCE_STEP_PCT":     {"label": "Confiance - pas d ajustement %",   "default": 5.0},
    "CONFIDENCE_MAX_PCT":      {"label": "Confiance - plafond dynamique %",  "default": 87.0},
    "CONFIDENCE_RESET_HOURS":  {"label": "Confiance - decroissance auto apres (h)", "default": 2.0},
    "AUTO_ACTIVATE_CONFIDENCE_PCT": {"label": "Auto-activation - seuil %",   "default": 80.0},
    "CRYPTO_OFFPEAK_HOUR_START_UTC": {"label": "Heures creuses - debut (UTC)", "default": 21},
    "CRYPTO_OFFPEAK_HOUR_END_UTC":   {"label": "Heures creuses - fin (UTC)",   "default": 23},
    "CPI_BLACKOUT_BEFORE_MIN": {"label": "Blackout CPI - minutes avant",     "default": 15},
    "CPI_BLACKOUT_AFTER_MIN":  {"label": "Blackout CPI - minutes apres",     "default": 30},

    # v4.42 — SUR DEMANDE EXPLICITE : jeu de seuils SL/TTP DEDIE au mode
    # Accumulation (LONG et SHORT confondus). default=None : Accumulation
    # continue de retomber sur les reglages du mode normal tant que rien
    # n est explicitement defini ici — ne rien toucher = aucun changement.
    "ACCUMULATION_SL_PCT_OF_E":             {"label": "Accumulation - SL (% de E)", "default": None},
    "ACCUMULATION_TTP_ARM1_PRICE_PCT":      {"label": "Accumulation - TTP armement (% de prix)", "default": None},
    "ACCUMULATION_TTP_LOCK1_PRICE_PCT":     {"label": "Accumulation - TTP verrou initial (% de prix)", "default": None},
    "ACCUMULATION_TTP_ARM2_PRICE_PCT":      {"label": "Accumulation - TTP palier 2 (% de prix)", "default": None},
    "ACCUMULATION_TTP_TRAIL_GAP_PRICE_PCT": {"label": "Accumulation - TTP marge de repli (% de prix)", "default": None},
    "ACCUMULATION_SL_ATR_MULTIPLIER":       {"label": "Accumulation - multiplicateur ATR (mode adaptatif)", "default": None},
    "FUNDING_SL_PCT_OF_E":             {"label": "Funding - SL (% de E)", "default": None},
    "FUNDING_TTP_ARM1_PRICE_PCT":      {"label": "Funding - TTP armement (% de prix)", "default": None},
    "FUNDING_TTP_LOCK1_PRICE_PCT":     {"label": "Funding - TTP verrou initial (% de prix)", "default": None},
    "FUNDING_TTP_ARM2_PRICE_PCT":      {"label": "Funding - TTP palier 2 (% de prix)", "default": None},
    "FUNDING_TTP_TRAIL_GAP_PRICE_PCT": {"label": "Funding - TTP marge de repli (% de prix)", "default": None},
    "ACCUMULATION_TTP_DYNAMIC_TRAIL_GAP_PCT": {"label": "Accumulation - marge du trailing dynamique dès l'armement (%)", "default": None},
    "FUNDING_TTP_DYNAMIC_TRAIL_GAP_PCT": {"label": "Funding - marge du trailing dynamique dès l'armement (%)", "default": None},
    "FUNDING_SL_ATR_MULTIPLIER":       {"label": "Funding - multiplicateur ATR (mode adaptatif)", "default": None},
}


@app.get("/api/config/advanced")
def get_advanced_config(email: str = Depends(require_user)):
    return {
        key: {"value": cfg.get(key, meta["default"]), "label": meta["label"], "default": meta["default"]}
        for key, meta in ADVANCED_SETTINGS.items()
    }


# v4.264 — FIX BUG CRITIQUE : le corps de requete type les valeurs en float,
# donc RSI_PERIOD=14 devenait 14.0 — et `prices[-(period + 1):]` leve alors
# TypeError (slice indices must be integers) : modifier une periode depuis
# l interface cassait l analyse de TOUS les actifs. Les reglages entiers
# (periodes, cycles, compteurs, heures) sont desormais convertis en int et
# bornes ; None n est accepte que pour les reglages "herite" (defaut None).
_RSI_FLOAT_THRESHOLDS = {"RSI_OVERSOLD", "RSI_OVERBOUGHT", "RSI_EXTREME_LOW", "RSI_EXTREME_HIGH"}
_ZERO_ALLOWED_INT_KEYS = {"CRYPTO_OFFPEAK_HOUR_START_UTC", "CRYPTO_OFFPEAK_HOUR_END_UTC",
                          "CPI_BLACKOUT_BEFORE_MIN", "CPI_BLACKOUT_AFTER_MIN",
                          "ACCUMULATION_LOSS_COOLDOWN_SEC", "SPOT_ACCUM_LOSS_COOLDOWN_SEC", "FUNDING_LOSS_COOLDOWN_SEC",
                          "ACCUMULATION_MAX_ENTRIES_PER_WINDOW", "SPOT_ACCUM_MAX_ENTRIES_PER_WINDOW",
                          "SPOT_ACCUM_SL_FLOW_MAX_WAIT_SEC", "SPOT_ACCUM_SL_PATIENCE_REQUIRE_TREND",
                          "MARKET_REGIME_FILTER_ENABLED", "SPOT_ACCUM_TRADE_HOUR_START_UTC", "ANTI_RANGE_RELATIVE_ENABLED", "SITUATION_RULES_ENABLED", "SITUATION_ALLOW_COUNTERTREND", "CONTINUATION_ENABLED", "CONTINUATION_PAPER_ONLY", "ENTRY_ENGINE_SIMPLE", "SIMPLE_ENGINE_DYNAMIC_LEVERAGE",
                          "SPOT_ACCUM_BYPASS_REQUIRE_TREND", "SPOT_ACCUM_BYPASS_FLOW_VETO", "SPOT_ACCUM_RISING_SUPPORT_ENABLED",
                          "SPOT_ACCUM_FRESH_BREAKOUT_COUNTER_TREND", "ACCUMULATION_FRESH_BREAKOUT_COUNTER_TREND",
                          "ACCUMULATION_BYPASS_REQUIRE_TREND", "ACCUMULATION_BYPASS_FLOW_VETO", "ACCUMULATION_TRADE_HOUR_START_UTC",
                          "FUNDING_TRADE_HOUR_START_UTC", "SPOT_ACCUM_TRADE_HOUR_END_UTC", "ACCUMULATION_TRADE_HOUR_END_UTC",
                          "FUNDING_TRADE_HOUR_END_UTC"}


def _is_int_setting(key: str) -> bool:
    default = ADVANCED_SETTINGS.get(key, {}).get("default")
    return isinstance(default, int) and not isinstance(default, bool) and key not in _RSI_FLOAT_THRESHOLDS


def _coerce_advanced_value(key: str, value):
    """Retourne (True, valeur_propre) ou (False, raison)."""
    default = ADVANCED_SETTINGS[key]["default"]
    if value is None:
        return (True, None) if default is None else (False, "valeur vide refusee pour ce reglage")
    if value != value or value in (float("inf"), float("-inf")):
        return False, "valeur invalide"
    if (key.startswith("ENTRY_FLOW_") or key.endswith("_FLOW_THRESHOLD") or key.endswith("_ENTRY_FLOW_CONFIRM_MIN") or key == "FRESH_BREAKOUT_COUNTER_TREND_MIN_FLOW") and not 0 <= value <= 1:
        return False, "doit etre entre 0 et 1 (pression de -1 a +1)"
    if key.endswith("_MIN_FLOW_CONVICTION") and not 0 <= value <= 1:
        return False, "entre 0 et 1"
    if key in ("SITUATION_RULES_ENABLED", "SITUATION_ALLOW_COUNTERTREND", "CONTINUATION_ENABLED", "CONTINUATION_PAPER_ONLY", "ENTRY_ENGINE_SIMPLE", "SIMPLE_ENGINE_DYNAMIC_LEVERAGE", "CONTINUATION_ENABLED", "CONTINUATION_PAPER_ONLY") and value not in (0, 1):
        return False, "1 (oui) ou 0 (non)"
    if key in ("SITUATION_REVERSAL_MIN_FLOW", "SITUATION_COUNTERTREND_MIN_FLOW") and not 0 <= value <= 1:
        return False, "entre 0 et 1"
    if key == "COUNTERTREND_TTP_MULT" and not 0.2 <= value <= 1:
        return False, "entre 0,2 et 1"
    if key == "MARKET_QUALITY_MAX_SPREAD_PCT" and not 0 <= value <= 5:
        return False, "entre 0 et 5 %"
    if key in ("MARKET_QUALITY_MIN_VOL_RATIO", "MARKET_QUALITY_MIN_ACTIVITY_RATIO") and not 0 <= value <= 5:
        return False, "entre 0 et 5"
    if key == "ANTI_RANGE_REL_MULT" and not 0.1 <= value <= 3:
        return False, "entre 0,1 et 3"
    if (key == "MARKET_REGIME_FILTER_ENABLED" or key == "ANTI_RANGE_RELATIVE_ENABLED" or "_BYPASS_" in key or key == "SPOT_ACCUM_RISING_SUPPORT_ENABLED" or key.endswith("_FRESH_BREAKOUT_COUNTER_TREND")) and value not in (0, 1):
        return False, "1 (oui) ou 0 (non)"
    if key.endswith("_TRADE_HOUR_START_UTC") and not 0 <= value <= 23:
        return False, "heure entre 0 et 23"
    if key.endswith("_TRADE_HOUR_END_UTC") and not 0 <= value <= 24:
        return False, "heure entre 0 et 24"
    if key == "MARKET_REGIME_BREADTH_PCT" and not 50 <= value <= 100:
        return False, "entre 50 et 100 %"
    if key == "SPOT_ACCUM_SL_PATIENCE_REQUIRE_TREND" and value not in (0, 1):
        return False, "1 (oui) ou 0 (non)"
    if key.endswith("_SL_CAP_PCT") and not 0.2 <= value <= 5:
        return False, "doit etre entre 0,2 et 5 %"
    if _is_int_setting(key):
        value = int(round(value))
        if key.endswith("_UTC") and not key.endswith("_END_UTC") and not 0 <= value <= 23:
            return False, "heure hors plage 0-23"
        if value < (0 if key in _ZERO_ALLOWED_INT_KEYS else 1):
            return False, "doit etre >= 1" if key not in _ZERO_ALLOWED_INT_KEYS else "doit etre >= 0"
    return True, value


def _normalize_loaded_advanced_settings():
    """Corrige les valeurs deja enregistrees en base (floats 14.0 ecrits par
    les versions precedentes) au chargement."""
    for key in ADVANCED_SETTINGS:
        if key in cfg and cfg[key] is not None and _is_int_setting(key):
            try:
                cfg[key] = int(round(float(cfg[key])))
            except (TypeError, ValueError):
                cfg[key] = ADVANCED_SETTINGS[key]["default"]


_normalize_loaded_advanced_settings()


class AdvancedConfigBody(BaseModel):
    # v4.42 — Optional[float] (pas juste float) : autorise l envoi explicite
    # de None pour REINITIALISER un reglage Accumulation dedie et revenir a
    # l heritage du mode normal (voir ACCUMULATION_SL_PCT_OF_E etc.).
    values: Dict[str, Optional[float]]


@app.put("/api/config/advanced")
def put_advanced_config(body: AdvancedConfigBody, email: str = Depends(require_user)):
    applied, ignored = {}, []
    for key, value in body.values.items():
        if key not in ADVANCED_SETTINGS:
            ignored.append(key)
            continue
        ok, clean = _coerce_advanced_value(key, value)
        if not ok:
            ignored.append(f"{key} ({clean})")
            continue
        _apply_and_persist(key, clean)
        applied[key] = clean
    return {"ok": True, "applied": applied, "ignored": ignored}


@app.get("/api/config")
def get_config(email: str = Depends(require_user)):
    return _public_config()


@app.put("/api/config")
def put_config(body: ConfigBody, email: str = Depends(require_user)):
    if body.trading_mode is not None:
        if bot.trading_enabled:
            raise HTTPException(400, "Arretez le bot avant de changer de mode (paper/live)")
        if body.trading_mode not in ("paper", "live"):
            raise HTTPException(400, "trading_mode doit etre 'paper' ou 'live'")
        _apply_and_persist("MODE", body.trading_mode)

    if body.position_pct is not None:
        _apply_and_persist("POSITION_SIZE_PCT", body.position_pct)

    # v4.10 — moteur ASYMETRIQUE : SL en % de E (perte $ plafonnee), TP en %
    # de mouvement de prix (gain $ amplifie par le levier). Les champs $
    # legacy (max_loss_usd, quick_profit_usd) restent acceptes pour compat
    # avec l interface actuelle : convertis a la volee via une estimation de
    # E a levier x1 (capital/POSITION_SIZE_PCT courants).
    e_estimate = cfg["CAPITAL_USD"] * cfg["POSITION_SIZE_PCT"] / 100

    if body.max_loss_usd is not None:
        if e_estimate > 0:
            _apply_and_persist("SL_PCT_OF_E", body.max_loss_usd / e_estimate * 100)
        else:
            raise HTTPException(400, "Capital/position_pct invalides pour convertir max_loss_usd en %")

    if body.quick_profit_usd is not None:
        if e_estimate > 0:
            _apply_and_persist("TTP_ARM1_PRICE_PCT", body.quick_profit_usd / e_estimate * 100)
        else:
            raise HTTPException(400, "Capital/position_pct invalides pour convertir quick_profit_usd en %")

    if body.sl_pct_of_e is not None:
        _apply_and_persist("SL_PCT_OF_E", body.sl_pct_of_e)

    if body.ttp_arm1_price_pct is not None:
        _apply_and_persist("TTP_ARM1_PRICE_PCT", body.ttp_arm1_price_pct)

    if body.ttp_lock1_price_pct is not None:
        _apply_and_persist("TTP_LOCK1_PRICE_PCT", body.ttp_lock1_price_pct)

    if body.ttp_arm2_price_pct is not None:
        _apply_and_persist("TTP_ARM2_PRICE_PCT", body.ttp_arm2_price_pct)

    if body.ttp_trail_gap_price_pct is not None:
        _apply_and_persist("TTP_TRAIL_GAP_PRICE_PCT", body.ttp_trail_gap_price_pct)

    if body.max_open_trades is not None:
        clamped = max(1, min(body.max_open_trades, len(SUPPORTED_TICKERS)))
        _apply_and_persist("MAX_OPEN_TRADES", clamped)

    if body.auto_activate_confidence_pct is not None:
        clamped_conf = max(50.0, min(body.auto_activate_confidence_pct, 100.0))
        _apply_and_persist("AUTO_ACTIVATE_CONFIDENCE_PCT", clamped_conf)

    # v3.2 — FIX : ignore une chaine vide plutot que d ecraser un wallet deja
    # enregistre — un formulaire n envoyant pas de wallet ne doit jamais
    # pouvoir effacer celui deja configure (defense en profondeur, en plus
    # du fix cote interface qui ne l envoie plus vide).
    if body.wallet:
        _apply_and_persist("WALLET_ADDRESS", be._clean_hex_secret(body.wallet))

    if body.api_key and not body.api_key.startswith("****"):
        _apply_and_persist("PRIVATE_KEY", be._clean_hex_secret(body.api_key))

    if body.active_coins is not None:
        valid = [c for c in body.active_coins if c in SUPPORTED_TICKERS]
        ignored = [c for c in body.active_coins if c not in SUPPORTED_TICKERS]
        old_active = set(cfg.get("ACTIVE_COINS") or SUPPORTED_TICKERS)
        new_active = set(valid)
        removed = old_active - new_active  # actifs que l utilisateur vient de desactiver
        added    = new_active - old_active  # actifs que l utilisateur vient de reactiver
        # v4.3 — une desactivation manuelle depuis l onglet Marches est une
        # exclusion EXPLICITE : elle doit tenir meme si la confiance de cet
        # actif remonte tres haut ensuite (voir _gate_active_or_auto_activate
        # dans bot_engine.py). Une reactivation manuelle leve cette exclusion.
        manual_exclude = set(cfg.get("MANUAL_EXCLUDE_COINS", []))
        manual_exclude |= removed
        manual_exclude -= added
        _apply_and_persist("ACTIVE_COINS", valid)
        _apply_and_persist("MANUAL_EXCLUDE_COINS", sorted(manual_exclude))
        if removed:
            _push_log("warn", f"Actifs desactives manuellement (ne seront plus jamais auto-reactives) : {', '.join(sorted(removed))}")
        if ignored:
            _push_log("warn", f"Actifs ignores (non supportes par ce bot) : {', '.join(ignored)}")

    return _public_config()


class HyperliquidBody(BaseModel):
    # v3.2 — FIX : l interface (index.html) envoie ces champs sous les noms
    # "hl_wallet" / "hl_api_key" a cet endpoint precis (un autre formulaire,
    # sur /api/config, utilise "wallet"/"api_key" — les deux sont donc geres
    # ici par securite). Le mismatch precedent faisait que la requete
    # "reussissait" (200 OK) sans rien enregistrer reellement.
    wallet: Optional[str] = None
    api_key: Optional[str] = None
    hl_wallet: Optional[str] = None
    hl_api_key: Optional[str] = None


@app.put("/api/config/hyperliquid")
def put_hyperliquid(body: HyperliquidBody, email: str = Depends(require_user)):
    wallet = body.wallet or body.hl_wallet
    api_key = body.api_key or body.hl_api_key
    if wallet:
        _apply_and_persist("WALLET_ADDRESS", be._clean_hex_secret(wallet))
    if api_key and not api_key.startswith("****"):
        _apply_and_persist("PRIVATE_KEY", be._clean_hex_secret(api_key))
    return {"ok": True, "note": "Prend effet au prochain demarrage du bot (arret puis demarrage)."}


class FinnhubBody(BaseModel):
    finnhub_key: str


@app.put("/api/config/finnhub")
def put_finnhub(body: FinnhubBody, email: str = Depends(require_user)):
    _apply_and_persist("FINNHUB_API_KEY", body.finnhub_key)
    return {"ok": True}


MODE_ACTIVE_COINS_KEY = {
    "accumulation": "ACCUMULATION_ACTIVE_COINS",
    "funding_contrarian": "FUNDING_ACTIVE_COINS",
    "spot_accumulation": "SPOT_ACCUM_ACTIVE_COINS",
}


class ModeCoinBody(BaseModel):
    mode: str
    ticker: str
    active: bool


@app.put("/api/config/mode-coins")
def put_mode_coins(body: ModeCoinBody, email: str = Depends(require_user)):
    """v4.44 — SUR DEMANDE EXPLICITE : bascule un actif ON/OFF pour un mode
    PRECIS (Accumulation/Funding/Spot-Accum), en un clic, independamment de
    la liste globale (Marches). Au premier reglage pour un mode, initialise
    sa liste dediee a partir de la liste globale ACTIVE_COINS actuelle
    (pour ne pas desactiver silencieusement tout le reste d un coup)."""
    key = MODE_ACTIVE_COINS_KEY.get(body.mode)
    if not key:
        raise HTTPException(400, f"Mode inconnu : {body.mode}")
    current = cfg.get(key)
    if current is None:
        current = list(cfg.get("ACTIVE_COINS") or [])
    else:
        current = list(current)
    ticker = _norm_ticker(body.ticker)
    if body.active and ticker not in current:
        current.append(ticker)
    elif not body.active and ticker in current:
        current.remove(ticker)
    _apply_and_persist(key, current)
    return {"ok": True, "mode": body.mode, "active_coins": current}


class StrategyModeBody(BaseModel):
    strategy: str  # "forex" | "accumulation" | "funding_contrarian" | "spot_accumulation"
    value: Optional[str] = None  # "paper" | "live" | None (suit le mode global)


@app.put("/api/config/strategy-mode")
def put_strategy_mode(body: StrategyModeBody, email: str = Depends(require_user)):
    """v4.87 — SUR DEMANDE EXPLICITE : bascule un mode precis entre paper et
    live, independamment du mode global du bot et des 3 autres modes.
    value=None retire la personnalisation (retombe sur le mode global)."""
    valid_strategies = ("forex", "accumulation", "funding_contrarian", "spot_accumulation")
    if body.strategy not in valid_strategies:
        raise HTTPException(400, f"Mode inconnu : {body.strategy}")
    if body.value is not None and body.value not in ("paper", "live"):
        raise HTTPException(400, "value doit etre 'paper', 'live', ou absent")
    current = cfg.get("STRATEGY_MODE_OVERRIDE") or {}
    current = dict(current)
    current[body.strategy] = body.value
    _apply_and_persist("STRATEGY_MODE_OVERRIDE", current)
    return {"ok": True, "strategy": body.strategy, "value": body.value}


class StrategyTradingEnabledBody(BaseModel):
    strategy: str  # "forex" | "accumulation" | "funding_contrarian" | "spot_accumulation"
    enabled: bool


@app.put("/api/config/strategy-trading-enabled")
def put_strategy_trading_enabled(body: StrategyTradingEnabledBody, email: str = Depends(require_user)):
    """v4.106 — SUR DEMANDE EXPLICITE : Marche/Arret INDEPENDANT par mode —
    enabled=False bloque UNIQUEMENT l ouverture de nouveaux trades pour ce
    mode precis ; les positions deja ouvertes de ce mode continuent d etre
    gerees normalement (SL/TTP/retournement) jusqu a leur fermeture."""
    valid_strategies = ("forex", "accumulation", "funding_contrarian", "spot_accumulation")
    if body.strategy not in valid_strategies:
        raise HTTPException(400, f"Mode inconnu : {body.strategy}")
    current = cfg.get("STRATEGY_TRADING_ENABLED") or {}
    current = dict(current)
    current[body.strategy] = body.enabled
    _apply_and_persist("STRATEGY_TRADING_ENABLED", current)
    _push_log("ok" if body.enabled else "warn", f"{'▶️' if body.enabled else '⏸️'} Mode {body.strategy} {'redemarre' if body.enabled else 'arrete'} — {'nouveaux trades autorises' if body.enabled else 'nouveaux trades bloques, positions ouvertes gerees normalement'}.")
    return {"ok": True, "strategy": body.strategy, "enabled": body.enabled}


class StrategyGoLiveBody(BaseModel):
    strategy: str  # "forex" | "accumulation" | "funding_contrarian" | "spot_accumulation"


@app.post("/api/config/strategy-go-live")
def post_strategy_go_live(body: StrategyGoLiveBody, email: str = Depends(require_user)):
    """v4.88 — SUR DEMANDE EXPLICITE : bascule un mode precis en LIVE, en
    une seule action combinee (le frontend demande deja confirmation avant
    d appeler cet endpoint, vu son caractere irreversible) :
      1) Ferme TOUTES les positions actuellement ouvertes pour ce mode
      2) Efface l historique (trades fermes) de ce mode uniquement
      3) Si des identifiants Hyperliquid sont configures, synchronise le
         capital reel du compte (remplace le capital local — attention,
         ce capital est PARTAGE entre tous les modes, meme ceux restes en
         paper, car le bot n a qu un seul pot de capital)
      4) Force enfin ce mode sur "live" via STRATEGY_MODE_OVERRIDE
    """
    valid_strategies = ("forex", "accumulation", "funding_contrarian", "spot_accumulation")
    if body.strategy not in valid_strategies:
        raise HTTPException(400, f"Mode inconnu : {body.strategy}")

    closed_count = bot._close_all_trades_for_strategy(body.strategy)
    deleted_count = db.clear_trades_by_strategy(body.strategy)

    capital_synced = False
    new_capital = None
    if bot.info is not None and cfg.get("WALLET_ADDRESS"):
        real_balance = be.sync_capital_from_hyperliquid(bot.info, cfg["WALLET_ADDRESS"])
        if real_balance is not None and real_balance > 0:
            # v4.89 — FIX : alimente le pot LIVE separe, plus jamais le
            # capital paper partage — les deux ne se melangent plus.
            bot.live_capital_base = real_balance
            capital_synced = True
            new_capital = real_balance
            _push_log("ok", f"💰 Capital LIVE synchronise depuis Hyperliquid suite au passage de {body.strategy} en live : ${real_balance:.2f} (capital paper des autres modes inchange)")

    current = cfg.get("STRATEGY_MODE_OVERRIDE") or {}
    current = dict(current)
    current[body.strategy] = "live"
    _apply_and_persist("STRATEGY_MODE_OVERRIDE", current)

    _push_log("warn", f"🔴 Mode {body.strategy} bascule en LIVE — {closed_count} position(s) fermee(s), {deleted_count} trade(s) d historique efface(s).")

    return {
        "ok": True,
        "strategy": body.strategy,
        "closed_positions": closed_count,
        "deleted_history": deleted_count,
        "capital_synced": capital_synced,
        "new_capital": new_capital,
    }


@app.post("/api/config/strategy-clear-only")
def post_strategy_clear_only(body: StrategyGoLiveBody, email: str = Depends(require_user)):
    """v4.165 — SUR DEMANDE EXPLICITE : version SANS le passage en live de
    strategy-go-live — ferme toutes les positions ouvertes pour ce mode et
    efface son historique, mais NE TOUCHE PAS a son statut paper/live
    (contrairement a strategy-go-live, qui force le passage en live comme
    etape finale). Utile pour repartir sur une base propre (ex: mode
    Normal reoriente vers un nouvel univers d actifs) sans forcer d
    engagement en argent reel."""
    valid_strategies = ("forex", "accumulation", "funding_contrarian", "spot_accumulation")
    if body.strategy not in valid_strategies:
        raise HTTPException(400, f"Mode inconnu : {body.strategy}")

    closed_count = bot._close_all_trades_for_strategy(body.strategy)
    deleted_count = db.clear_trades_by_strategy(body.strategy)

    _push_log("info", f"🧹 Nettoyage {body.strategy} — {closed_count} position(s) fermee(s), {deleted_count} trade(s) d historique efface(s). Statut paper/live inchange.")

    return {
        "ok": True,
        "strategy": body.strategy,
        "closed_positions": closed_count,
        "deleted_history": deleted_count,
    }


class ModeCoinResetBody(BaseModel):
    mode: str


@app.put("/api/config/mode-coins/reset")
def reset_mode_coins(body: ModeCoinResetBody, email: str = Depends(require_user)):
    """v4.52 — SUR DEMANDE EXPLICITE : reinitialise la liste d actifs d un
    mode PRECIS pour qu elle revienne a heriter de la liste globale
    (Marches) — corrige le cas ou un mode etait reste bloque sur une liste
    personnalisee figee (ex: 5 actifs), sans plus jamais suivre les
    changements faits dans Marches, faute d un moyen de revenir en arriere."""
    key = MODE_ACTIVE_COINS_KEY.get(body.mode)
    if not key:
        raise HTTPException(400, f"Mode inconnu : {body.mode}")
    _apply_and_persist(key, None)
    return {"ok": True, "mode": body.mode, "active_coins": None}


class FiltersBody(BaseModel):
    filter_hours: Optional[bool] = None
    filter_weekend: Optional[bool] = None
    filter_macro: Optional[bool] = None
    accumulation_enabled: Optional[bool] = None
    accumulation_require_trend_confirm: Optional[bool] = None
    sl_ttp_adaptive_enabled: Optional[bool] = None
    funding_mode_enabled: Optional[bool] = None
    funding_mode_live_allowed: Optional[bool] = None
    require_sr_ema200_separation: Optional[bool] = None
    unified_simplified_mode: Optional[bool] = None
    unified_full_simplified_mode: Optional[bool] = None
    unified_require_sr_amplitude: Optional[bool] = None
    ttp_trend_hold_filter_enabled: Optional[bool] = None
    unified_require_adx_confirm: Optional[bool] = None
    ttp_dynamic_from_arm1: Optional[bool] = None
    spot_accum_enabled: Optional[bool] = None
    spot_accum_sl_enabled: Optional[bool] = None
    spot_accum_require_adx_confirm: Optional[bool] = None
    accumulation_reversal_exit_enabled: Optional[bool] = None
    spot_accum_hard_sl_enabled: Optional[bool] = None


@app.put("/api/config/filters")
def put_filters(body: FiltersBody, email: str = Depends(require_user)):
    # filter_hours  -> heures creuses crypto (CRYPTO_OFFPEAK_ENABLED)
    # filter_weekend-> fermeture Forex sur PAXG (FOREX_SYMBOLS)
    # filter_macro  -> blackout CPI Finnhub (CPI_BLACKOUT_ENABLED)
    if body.filter_hours is not None:
        _apply_and_persist("CRYPTO_OFFPEAK_ENABLED", body.filter_hours)
    if body.filter_weekend is not None:
        _apply_and_persist("FOREX_SYMBOLS", ["PAXG"] if body.filter_weekend else [])
    if body.filter_macro is not None:
        _apply_and_persist("CPI_BLACKOUT_ENABLED", body.filter_macro)
    # v4.8 — mode Accumulation (LONG pres du support / SHORT pres de la
    # resistance, independant de la logique RSI/tendance normale)
    if body.accumulation_enabled is not None:
        _apply_and_persist("ACCUMULATION_ENABLED", body.accumulation_enabled)
    if body.accumulation_require_trend_confirm is not None:
        _apply_and_persist("ACCUMULATION_REQUIRE_TREND_CONFIRM", body.accumulation_require_trend_confirm)
    # v4.24 — SL/TTP adaptatifs a l ATR reel (unique/global : un seul
    # multiplicateur pour tous les actifs, mais le resultat differe par
    # actif car chacun a sa propre ATR au moment de l entree — voir
    # SL_ATR_MULTIPLIER/SL_PCT_MIN/SL_PCT_MAX dans Parametres avances).
    if body.sl_ttp_adaptive_enabled is not None:
        _apply_and_persist("SL_TTP_ADAPTIVE_ENABLED", body.sl_ttp_adaptive_enabled)
    # v4.33 — mode Funding Contrarian : source de signal differente
    # (desequilibre de position via le funding rate, pas RSI/MACD/EMA).
    # funding_mode_live_allowed reste False par defaut : meme active, ce
    # mode simule ses trades (paper) tant que ce deuxieme interrupteur n est
    # pas leve manuellement, quel que soit le mode global du bot — protege
    # un capital de trading deja fragilise pendant la phase de validation.
    if body.funding_mode_enabled is not None:
        _apply_and_persist("FUNDING_MODE_ENABLED", body.funding_mode_enabled)
    if body.funding_mode_live_allowed is not None:
        _apply_and_persist("FUNDING_MODE_LIVE_ALLOWED", body.funding_mode_live_allowed)
    # v4.37 — bloque un LONG/SHORT si le support/resistance est proche ET du
    # mauvais cote de l EMA200 (marche en range pur, sans separation nette
    # de sa moyenne longue) — DESACTIVE par defaut.
    if body.require_sr_ema200_separation is not None:
        _apply_and_persist("REQUIRE_SR_EMA200_SEPARATION", body.require_sr_ema200_separation)
    # v4.58 — mode SIMPLIFIE : 3 conditions communes (tendance+ADX,
    # proximite 1-5%, amplitude S/R) remplacent la complexite empilee sur le
    # mode normal, et s ajoutent a Accumulation. ACTIF par defaut.
    if body.unified_simplified_mode is not None:
        _apply_and_persist("UNIFIED_SIMPLIFIED_MODE", body.unified_simplified_mode)
    if body.unified_full_simplified_mode is not None:
        _apply_and_persist("UNIFIED_FULL_SIMPLIFIED_MODE", body.unified_full_simplified_mode)
    if body.unified_require_sr_amplitude is not None:
        _apply_and_persist("UNIFIED_REQUIRE_SR_AMPLITUDE", body.unified_require_sr_amplitude)
    if body.ttp_trend_hold_filter_enabled is not None:
        _apply_and_persist("TTP_TREND_HOLD_FILTER_ENABLED", body.ttp_trend_hold_filter_enabled)
    if body.unified_require_adx_confirm is not None:
        _apply_and_persist("UNIFIED_REQUIRE_ADX_CONFIRM", body.unified_require_adx_confirm)
    if body.ttp_dynamic_from_arm1 is not None:
        _apply_and_persist("TTP_DYNAMIC_FROM_ARM1", body.ttp_dynamic_from_arm1)
    # v4.43 — mode Spot-Accumulation (achat d actif esprit spot, aucun SL
    # par defaut, levier toujours x1). spot_accum_sl_enabled ajoute un SL
    # optionnel en % du PnL (voir SPOT_ACCUM_SL_PCT_OF_PNL dans Parametres avances).
    if body.spot_accum_enabled is not None:
        _apply_and_persist("SPOT_ACCUM_ENABLED", body.spot_accum_enabled)
    if body.spot_accum_sl_enabled is not None:
        _apply_and_persist("SPOT_ACCUM_SL_ENABLED", body.spot_accum_sl_enabled)
    if body.spot_accum_require_adx_confirm is not None:
        _apply_and_persist("SPOT_ACCUM_REQUIRE_ADX_CONFIRM", body.spot_accum_require_adx_confirm)
    if body.accumulation_reversal_exit_enabled is not None:
        _apply_and_persist("ACCUMULATION_REVERSAL_EXIT_ENABLED", body.accumulation_reversal_exit_enabled)
    if body.spot_accum_hard_sl_enabled is not None:
        _apply_and_persist("SPOT_ACCUM_HARD_SL_ENABLED", body.spot_accum_hard_sl_enabled)
    return {"ok": True}


class AiContinuousBody(BaseModel):
    enabled: bool


@app.put("/api/config/ai-continuous")
def put_ai_continuous(body: AiContinuousBody, email: str = Depends(require_user)):
    # Aucun equivalent fonctionnel dans ce bot (pas de couche IA generative
    # de signaux) — stocke pour compatibilite avec l interface, sans effet.
    db.set_config_override("ai_continuous", body.enabled)
    return {"ok": True, "note": "Reserve — sans effet sur ce bot (pas de moteur IA continu)."}


# ─────────────────────────────────────────────────────────────────────────
#  CONTROLE DU BOT
# ─────────────────────────────────────────────────────────────────────────
@app.post("/api/bot/start")
def bot_start(email: str = Depends(require_user)):
    if bot.trading_enabled:
        raise HTTPException(400, "Le trading tourne deja")
    # v3.2 : la cle API + le wallet Hyperliquid sont obligatoires (paper ET
    # live) — on le verifie ici pour repondre immediatement plutot que de
    # laisser le thread du bot echouer silencieusement en arriere-plan.
    if not cfg.get("PRIVATE_KEY") or not cfg.get("WALLET_ADDRESS"):
        raise HTTPException(
            400,
            "Cle API et wallet Hyperliquid obligatoires (paper et live). "
            "Configurez-les via /api/config/hyperliquid ou les variables "
            "d environnement HYPERBOT_PRIVATE_KEY / HYPERBOT_WALLET_ADDRESS."
        )
    bot.start()
    _mark_running_start()
    db.set_meta("bot_desired_state", "running")
    return {"ok": True}


@app.post("/api/bot/stop")
def bot_stop(email: str = Depends(require_user)):
    bot.stop()
    _mark_running_stop_and_accumulate()
    # Persiste explicitement l intention d arret : le TRADING ne redemarrera
    # pas tout seul apres un redeploiement/redemarrage Railway tant que
    # quelqu un n aura pas reclique sur DEMARRER (voir _auto_start_if_desired).
    # v4.14 — le MOTEUR (collecte/WebSocket), lui, redemarre toujours
    # automatiquement des que le process reboote, meme si le trading reste
    # arrete — seule la persistance de nouveaux trades depend de cet etat.
    db.set_meta("bot_desired_state", "stopped")
    return {"ok": True}


@app.post("/api/bot/force-recollect")
def force_recollect(email: str = Depends(require_user)):
    """Force une collecte d indicateurs entierement fraiche pour tous les
    actifs, sans attendre un redeploiement — utile si un probleme est
    suspecte sur les indicateurs restaures (reprise rapide < 90s). Ne touche
    ni aux positions ouvertes, ni au capital, ni a l historique."""
    bot.force_fresh_collection()
    return {"ok": True, "message": "Collecte fraiche forcee pour tous les actifs — les indicateurs vont se reconstruire progressivement."}


@app.post("/api/bot/reset-confidence")
def reset_confidence(email: str = Depends(require_user)):
    """Reinitialisation CIBLEE des seuils de confiance dynamiques par actif —
    ne touche ni aux positions ouvertes, ni au capital, ni a l historique.
    Utile quand de nombreux actifs sont "au frigo" (seuil eleve suite a des
    pertes), sans avoir a attendre la decroissance automatique ni a faire
    une reinitialisation complete destructrice."""
    bot.reset_confidence_penalties()
    return {"ok": True, "message": "Toutes les penalites de confiance ont ete reinitialisees — chaque actif repart au seuil de base."}


@app.get("/api/confidence/calibration")
def get_confidence_calibration(email: str = Depends(require_user)):
    """v4.2 — Analyse (sans rien modifier) le pouvoir predictif reel de
    chaque indicateur du score de confiance, a partir de l historique des
    trades clotures. Permet de voir AVANT d appliquer si les poids actuels
    sont corrects, sur-estimes ou sous-estimes par rapport aux resultats
    reellement observes sur ce compte."""
    return _analyze_confidence_calibration()


@app.post("/api/confidence/calibration/apply")
def apply_confidence_calibration(email: str = Depends(require_user)):
    """v4.2 — Recalcule ET applique les poids de CONFIDENCE_WEIGHTS a partir
    de l historique reel. Refuse si aucun indicateur n a assez de donnees
    (evite de calibrer sur du bruit). Les indicateurs sans assez de donnees
    gardent leur poids actuel inchange ; seuls ceux avec un echantillon
    suffisant (>= min_samples dans les deux groupes) sont ajustes."""
    print(f"[AUDIT] /api/confidence/calibration/apply appele par {email} a {datetime.now(timezone.utc).isoformat()}")
    analysis = _analyze_confidence_calibration()
    if not analysis["ready_to_calibrate"]:
        raise HTTPException(
            400,
            f"Pas assez de donnees pour calibrer (minimum {analysis['min_samples_required']} trades "
            f"dans chaque groupe 'confirme'/'non confirme' par indicateur). "
            f"Trades clotures avec detail disponible : {analysis['trades_with_breakdown']}."
        )
    _apply_and_persist("CONFIDENCE_WEIGHTS", analysis["suggested_weights"])
    return {"ok": True, "applied_weights": analysis["suggested_weights"], "analysis": analysis}


@app.get("/api/confidence/by-asset")
def get_confidence_by_asset(min_trades: int = 5, email: str = Depends(require_user)):
    """v4.4 — Probabilite de reussite reelle par actif, a partir de
    l historique des trades clotures. min_trades ajustable dynamiquement
    depuis l interface (bouton dans l onglet Historique)."""
    return _analyze_confidence_by_asset(min_trades=min_trades)


@app.get("/api/ws/events")
def get_ws_events(email: str = Depends(require_user)):
    """v4.6 — Journal des evenements WebSocket (connexion, deconnexion,
    echec/succes de reconnexion) des 7 derniers jours, persiste en base
    (survit aux redemarrages, contrairement au log en memoire)."""
    events = db.get_ws_events(days=7)
    return {
        "events": events,
        "currently_healthy": bot._is_ws_healthy() if bot.info is not None else False,
        "currently_connected": bot.info is not None,
    }


@app.get("/api/bot/logs")
def bot_logs(persistent: bool = Query(False), limit: int = Query(200), search: str = Query(None), email: str = Depends(require_user)):
    if persistent:
        # Lit la fin du fichier de log sur disque (persiste entre redemarrages
        # si HYPERBOT_DATA_DIR pointe vers un Volume Railway).
        # v3.2 — FIX : le fichier contient des lignes texte brutes
        # ("YYYY-MM-DD HH:MM:SS [LEVEL   ] message"), mais l interface attend
        # des objets {time, level, message} comme pour les logs en direct —
        # sans ce parsing, les logs persistants s affichaient vides.
        # v3.2 — FIX #2 : la limite par defaut (200 lignes) etait bien trop
        # basse compte tenu du volume de log genere (jusqu a 30 actifs
        # values a chaque cycle de 10s) — elle ne couvrait parfois que 2-3
        # minutes reelles, alors que le fichier lui-meme garde 24h. Un
        # parametre "search" permet desormais de filtrer par mot-cle (ex: un
        # ticker precis) AVANT d appliquer la limite, pour retrouver un
        # evenement precis n importe ou dans la fenetre de 24h — pas
        # seulement dans les dernieres minutes.
        import re
        pattern = re.compile(r"^(\S+ \S+) \[(\w+)\s*\] (.*)$")
        try:
            with open(be.LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            if search:
                all_lines = [l for l in all_lines if search.lower() in l.lower()]
            lines = all_lines[-limit:]
            parsed = []
            for line in lines:
                line = line.rstrip("\n")
                m = pattern.match(line)
                if m:
                    time_str, level_str, msg = m.groups()
                    parsed.append({
                        "time": time_str,
                        "level": _LEVEL_MAP.get(level_str.lower(), "info"),
                        "message": msg,
                    })
                else:
                    parsed.append({"time": "", "level": "info", "message": line})
            return {"logs": parsed}
        except FileNotFoundError:
            return {"logs": []}
    return {"logs": list(log_buffer)[-limit:]}


@app.get("/api/bot/logs/history")
def bot_logs_history(limit: int = Query(200), before_id: int = Query(None), level: str = Query(None), email: str = Depends(require_user)):
    """v4.244 — SUR DEMANDE EXPLICITE : journal PERSISTANT en base de
    donnees (survit aux redemarrages, retention 30 jours) — distinct du
    buffer en memoire (quelques heures) et du fichier 24h existant
    (?persistent=true sur /api/bot/logs). Pagination via before_id (id du
    plus ancien deja recu, pour charger la page precedente/plus ancienne).
    Filtre optionnel par niveau (warn/error/ok/win/loss)."""
    try:
        rows = db.query_log_history(limit=min(limit, 500), before_id=before_id, level=level)
        return {"logs": rows, "has_more": len(rows) == min(limit, 500)}
    except Exception as e:
        return {"logs": [], "has_more": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
#  DONNEES DE MARCHE / POSITIONS / SIGNAUX
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/prices")
def get_prices(email: str = Depends(require_user)):
    # v3.2 : priorite au cache WebSocket complet (bot.all_mids, alimente par
    # le flux allMids — couvre potentiellement TOUS les actifs Hyperliquid,
    # pas seulement ceux tradés par ce bot). Complete avec les prix suivis
    # individuellement (state.current_price) pour nos symboles, au cas ou
    # le WebSocket ne serait pas encore actif (repli cycle REST).
    prices = {}
    try:
        for ticker, raw in (bot.all_mids or {}).items():
            try:
                prices[ticker] = float(raw)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    for k, s in bot.states.items():
        ticker = be.ticker_from_slot_key(k)
        if s.current_price and ticker not in prices:
            prices[ticker] = s.current_price
    return {"prices": prices}


@app.get("/api/volatility")
def get_volatility(email: str = Depends(require_user)):
    """Classement en direct de la volatilite (ATR%) des actifs suivis — aide
    a identifier ou le mouvement de prix est le plus fort a l instant present
    (donc le potentiel de capture le plus eleve avec les seuils actuels).
    Inclut aussi l ADX (force de la tendance) et le mode detecte
    (trend/reversal) pour chaque actif."""
    active_coins = cfg.get("ACTIVE_COINS") or SUPPORTED_TICKERS
    adx_threshold = cfg.get("ADX_TREND_THRESHOLD", 25.0)
    manual_modes = cfg.get("SYMBOL_RSI_MODE", {})
    rows = []
    for slot_key, state in bot.states.items():
        ticker = be.ticker_from_slot_key(slot_key)
        if state.current_atr_pct is None:
            continue
        adx = state.current_adx
        if manual_modes.get(ticker):
            mode = manual_modes[ticker]
        elif adx is not None:
            mode = "trend" if adx >= adx_threshold else "reversal"
        else:
            # v3.2 — FIX : le bot bascule sur "trend" par defaut si l ADX
            # n est pas encore calculable (collecte insuffisante) — l API
            # doit refleter EXACTEMENT ce meme comportement, sinon
            # l interface affichait aucun badge, donnant l impression
            # trompeuse d un actif "non tradable" alors qu il l est deja.
            mode = "trend"
        rows.append({
            "coin": ticker,
            "atr_pct": round(state.current_atr_pct, 4),
            "adx": round(adx, 1) if adx is not None else None,
            "mode": mode,
            "price": state.current_price,
            "active": ticker in active_coins,
            "has_position": bool(state.position),
        })
    rows.sort(key=lambda r: r["atr_pct"], reverse=True)
    return {"ranking": rows, "updated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/positions")
def get_positions(email: str = Depends(require_user)):
    return {"positions": _open_positions()}


@app.get("/api/indicators/{ticker}")
def get_indicator_history(ticker: str, email: str = Depends(require_user)):
    """v4.21 — Historique des indicateurs (RSI, MACD, EMA200, ATR,
    support/resistance) d un actif, pour affichage en graphe cote
    interface. Cherche parmi TOUS les slots (BTC_0, BTC_1...) portant ce
    ticker et retourne celui qui a le plus de donnees (le plus actif)."""
    ticker = _norm_ticker(ticker)
    best_state = None
    best_len = -1
    for slot_key, state in bot.states.items():
        if be.ticker_from_slot_key(slot_key) == ticker:
            n = len(state.indicator_history)
            if n > best_len:
                best_len = n
                best_state = state
    if best_state is None:
        raise HTTPException(404, f"Actif inconnu ou non suivi : {ticker}")
    return {"ticker": ticker, "history": list(best_state.indicator_history)}


@app.get("/api/atr-summary")
def get_atr_summary(email: str = Depends(require_user)):
    """v4.38 — SUR DEMANDE EXPLICITE : resume de l ATR (vrai calcul haut/bas
    de Wilder, avec repli sur l ancien calcul cloture-a-cloture si pas
    encore assez de bougies accumulees) de TOUS les actifs suivis, a la
    demande — pour recalibrer ATR_MIN_PCT sur des donnees reelles plutot
    que de scanner des dizaines de lignes eparpillees dans les logs."""
    cfg = bot.cfg
    atr_period = cfg.get("ATR_PERIOD", 14)
    results = []
    for slot_key, state in bot.states.items():
        ticker = be.ticker_from_slot_key(slot_key)
        atr_pct_val = None
        source = None
        if len(state.candle_history) >= atr_period + 1:
            _, atr_pct_val = be.calc_true_range_atr(list(state.candle_history), atr_period)
            source = "haut/bas (vrai)"
        if atr_pct_val is None and len(state.price_history) >= atr_period + 1:
            _, atr_pct_val = be.calc_atr(list(state.price_history), atr_period)
            source = "cloture-a-cloture (repli)"
        results.append({
            "ticker": ticker,
            "atr_pct": round(atr_pct_val, 4) if atr_pct_val is not None else None,
            "source": source,
            "candles_collected": len(state.candle_history),
            "active": ticker in cfg.get("ACTIVE_COINS", []),
        })
    results.sort(key=lambda r: (r["atr_pct"] is None, -(r["atr_pct"] or 0)))
    return {"atr_period": atr_period, "results": results}


@app.get("/api/trend-summary")
def get_trend_summary(email: str = Depends(require_user)):
    """v4.59 — SUR DEMANDE EXPLICITE : classe chaque actif suivi en
    "haussier franc" / "baissier franc" / "indecis", a la demande — meme
    logique que la base commune des 3 modes (_unified_trend_confirmed) :
    EMA200 pour la direction, ADX >= seuil pour la force (une direction
    sans force n est pas consideree franche)."""
    cfg = bot.cfg
    adx_period = cfg.get("ADX_PERIOD", 14)
    adx_threshold = cfg.get("ADX_TREND_THRESHOLD", 25.0)
    results = []
    for slot_key, state in bot.states.items():
        ticker = be.ticker_from_slot_key(slot_key)
        price = state.current_price
        prices = list(state.price_history)
        ema200 = be.calc_ema(list(state.mtf_prices), 200) if len(state.mtf_prices) >= 5 else None
        adx = be.calc_adx(prices, adx_period) if len(prices) >= adx_period + 1 else None
        if price is None or ema200 is None or adx is None:
            label = "donnees insuffisantes"
        else:
            strong = adx >= adx_threshold
            if price > ema200 and strong:
                label = "haussier franc"
            elif price < ema200 and strong:
                label = "baissier franc"
            else:
                label = "indecis"
        results.append({
            "ticker": ticker,
            "price": price,
            "ema200": round(ema200, 6) if ema200 is not None else None,
            "adx": round(adx, 1) if adx is not None else None,
            "adx_threshold": adx_threshold,
            "label": label,
            "active": ticker in (cfg.get("ACTIVE_COINS") or []),
        })
    order = {"haussier franc": 0, "baissier franc": 1, "indecis": 2, "donnees insuffisantes": 3}
    results.sort(key=lambda r: (order.get(r["label"], 9), r["ticker"]))
    return {"adx_threshold": adx_threshold, "results": results, "refreshed_at": time.time()}


@app.get("/api/strategy-performance/{strategy}")
def get_strategy_performance(strategy: str, email: str = Depends(require_user)):
    """v4.43 — SUR DEMANDE EXPLICITE : performance d un MODE precis
    (normal/accumulation/funding_contrarian/spot_accumulation), calculee a
    la demande depuis l historique reel en base — pour le bouton
    "Performance" de chaque sous-onglet de l onglet Paper Trading."""
    all_closed = db.get_all_closed_trades()
    filtered = [t for t in all_closed if (t.get("strategy") or "forex") == strategy]
    wins = [t for t in filtered if (t.get("pnl") or 0) > 0]
    losses = [t for t in filtered if (t.get("pnl") or 0) <= 0]
    total_pnl = sum((t.get("pnl") or 0) for t in filtered)
    win_pnl = sum((t.get("pnl") or 0) for t in wins)
    loss_pnl = sum((t.get("pnl") or 0) for t in losses)
    # v4.186 — SUR DEMANDE EXPLICITE : total des frais REELS estimes payes
    # a Hyperliquid pour ce mode — uniquement les trades LIVE ont un frais
    # non-None (voir bot_engine.py close_position), le paper n en a jamais.
    total_fees = sum((t.get("fees_paid") or 0) for t in filtered)
    open_count = sum(1 for st in bot.states.values() if st.position and (st.position.get("strategy") or "forex") == strategy)
    # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son PROPRE
    # emplacement (bot.accum_states), jamais compte ci-dessus.
    if strategy == "accumulation":
        open_count += sum(1 for st in bot.accum_states.values() if st.position)
    return {
        "strategy": strategy,
        "total_trades": len(filtered),
        "open_trades": open_count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(filtered) * 100, 1) if filtered else 0,
        "net_pnl": round(total_pnl, 4),
        "win_pnl": round(win_pnl, 4),
        "loss_pnl": round(loss_pnl, 4),
        "total_fees_paid": round(total_fees, 4),
        "refreshed_at": time.time(),
    }


@app.get("/api/entry-diagnostics")
def get_entry_diagnostics_all(email: str = Depends(require_user)):
    """v4.40 — SUR DEMANDE EXPLICITE, suite a un ecart de 13h sans aucun
    trade jamais explique faute de logs disponibles : instantane de l etat
    de TOUTES les portes d entree (RSI, MACD, amplitude, niveaux, fraicheur,
    confirmation post-trade, separation EMA200...) pour TOUS les actifs
    suivis, a la demande — capture a chaque cycle cote bot_engine, donc
    toujours a jour au moment de l appel, quelle que soit la retention des
    logs. Vue d ensemble compacte : pour le detail complet d un actif, voir
    /api/entry-diagnostics/{ticker}."""
    results = []
    for slot_key, state in bot.states.items():
        ticker = be.ticker_from_slot_key(slot_key)
        snap = state.last_gate_snapshot or {}
        has_position = state.position is not None
        # v4.40 — resume compact : identifie le PREMIER obstacle qui bloque
        # chaque sens, pour un coup d oeil rapide sans lire les 20 champs.
        blocker_long = None
        if has_position:
            blocker_long = "position deja ouverte"
        elif not snap:
            blocker_long = "pas encore de donnees"
        # v4.134 — FIX BUG CRITIQUE : en mode simplifie (unified_mode_active),
        # la VRAIE decision n utilise plus du tout rsi_buy/ema_bull/trend_up
        # individuellement — seulement long_level_ok_final (tendance+ADX+
        # proximite combines). Ce gate obsolete bloquait AVANT MEME d
        # atteindre la logique correcte du mode simplifie (deja codee plus
        # bas, mais jamais atteinte) — expliquant "signal de base non
        # reuni" en quasi-permanence, meme quand le signal simplifie etait
        # en realite tout pres de qualifier.
        elif not snap.get("unified_mode_active") and not (snap.get("rsi_buy") and snap.get("ema_bull") and snap.get("trend_up")):
            blocker_long = "signal de base non reuni (RSI/EMA/tendance)"
        elif snap.get("long_signal_stale"):
            blocker_long = "signal pas encore renouvele (fraicheur)"
        elif not snap.get("long_level_ok_final"):
            # v4.58 — SUR DEMANDE EXPLICITE : raisons COHERENTES avec le mode
            # actif — le mode simplifie (base commune) n a plus rien a voir
            # avec MACD/amplitude ATR/separation EMA200/confirmation post-trade.
            if snap.get("unified_mode_active"):
                if not snap.get("unified_trend_confirmed_long"):
                    blocker_long = "tendance/ADX pas assez forte"
                elif not snap.get("unified_proximity_long_ok"):
                    blocker_long = "hors fenetre 1-5% du support (et pas de cassure)"
                elif not snap.get("unified_amplitude_ok"):
                    blocker_long = "fourchette S/R trop etroite"
                else:
                    blocker_long = "base commune non reunie (raison indeterminee)"
            elif not snap.get("direction_confirmed_long"):
                blocker_long = "MACD ne confirme pas"
            elif not snap.get("amplitude_coherent"):
                blocker_long = "amplitude ATR incoherente"
            elif not snap.get("sr_ema_long_ok"):
                blocker_long = "support trop proche de l EMA200"
            elif snap.get("post_win_confirm_long"):
                blocker_long = f"confirmation post-trade en attente ({snap.get('confirm_count_long',0)}/18 cycles, {snap.get('post_win_wait_long',0)}/180 max)"
            else:
                blocker_long = "niveau (support/resistance) non respecte"
        else:
            blocker_long = None  # rien ne bloque, devrait trader au prochain signal
        blocker_short = None
        if has_position:
            blocker_short = "position deja ouverte"
        elif not snap:
            blocker_short = "pas encore de donnees"
        # v4.134 — FIX BUG CRITIQUE : meme correctif que blocker_long.
        elif not snap.get("unified_mode_active") and not (snap.get("rsi_sell") and snap.get("ema_bear") and snap.get("trend_down")):
            blocker_short = "signal de base non reuni (RSI/EMA/tendance)"
        elif snap.get("short_signal_stale"):
            blocker_short = "signal pas encore renouvele (fraicheur)"
        elif not snap.get("short_level_ok_final"):
            if snap.get("unified_mode_active"):
                if not snap.get("unified_trend_confirmed_short"):
                    blocker_short = "tendance/ADX pas assez forte"
                elif not snap.get("unified_proximity_short_ok"):
                    blocker_short = "hors fenetre 1-5% de la resistance (et pas de cassure)"
                elif not snap.get("unified_amplitude_ok"):
                    blocker_short = "fourchette S/R trop etroite"
                else:
                    blocker_short = "base commune non reunie (raison indeterminee)"
            elif not snap.get("direction_confirmed_short"):
                blocker_short = "MACD ne confirme pas"
            elif not snap.get("amplitude_coherent"):
                blocker_short = "amplitude ATR incoherente"
            elif not snap.get("sr_ema_short_ok"):
                blocker_short = "resistance trop proche de l EMA200"
            elif snap.get("post_win_confirm_short"):
                blocker_short = f"confirmation post-trade en attente ({snap.get('confirm_count_short',0)}/18 cycles, {snap.get('post_win_wait_short',0)}/180 max)"
            else:
                blocker_short = "niveau (support/resistance) non respecte"
        else:
            blocker_short = None
        # v4.55 — SUR DEMANDE EXPLICITE : le diagnostic ne couvrait jusqu ici
        # que le mode normal — ajoute Spot-Accumulation, dont la logique
        # d entree est completement differente (LONG uniquement, fenetre de
        # distance au support, amplitude S/R, confirmation ADX...).
        spot_snap = state.spot_accum_gate_snapshot or {}
        if has_position and state.position.get("strategy") == "spot_accumulation":
            blocker_spot_accum = "position deja ouverte"
        elif not spot_snap:
            blocker_spot_accum = "pas encore de donnees"
        else:
            blocker_spot_accum = spot_snap.get("blocker", "pas encore de donnees")
        # v4.80 — SUR DEMANDE EXPLICITE : meme diagnostic pour Accumulation,
        # qui n en avait aucun jusqu ici.
        # v4.121 — FIX : Accumulation a desormais son PROPRE emplacement
        # (bot.accum_states), independant de bot.states — lit depuis le bon
        # endroit, verifie sa PROPRE position (pas celle du mode normal).
        accum_state = bot.accum_states.get(slot_key)
        accum_snap = (accum_state.accumulation_gate_snapshot if accum_state else None) or {}
        if accum_state and accum_state.position is not None:
            blocker_accumulation = "position deja ouverte"
            blocker_accumulation_long = blocker_accumulation
            blocker_accumulation_short = blocker_accumulation
        elif not accum_snap:
            blocker_accumulation = "pas encore de donnees"
            blocker_accumulation_long = blocker_accumulation
            blocker_accumulation_short = blocker_accumulation
        else:
            blocker_accumulation = accum_snap.get("blocker", "pas encore de donnees")
            # v4.136 — SUR DEMANDE EXPLICITE : expose desormais separement
            # LONG et SHORT pour Accumulation (comme le mode normal),
            # au lieu d un seul champ combine qui masquait si un seul des
            # deux sens etait en realite bloque.
            blocker_accumulation_long = accum_snap.get("blocker_long", blocker_accumulation)
            blocker_accumulation_short = accum_snap.get("blocker_short", blocker_accumulation)
        # v4.272 — les lignes LONG/SHORT sont celles du mode FOREX : il ne
        # trade que FOREX_MODE_SYMBOLS (devises xyz + PAXG). Pour une crypto,
        # "aucun obstacle" etait trompeur — et pour une devise, le filtre
        # anti-range (blocage le plus frequent) n apparaissait pas.
        if ticker not in cfg.get("FOREX_MODE_SYMBOLS", []):
            blocker_long = blocker_short = "non concerne (crypto : le mode Forex ne trade que les devises et PAXG)"
        elif not has_position and (snap.get("forex_ranging") or snap.get("quality_block")):
            range_txt = snap.get("quality_block") or snap.get("forex_range_text") or "marche en range"
            blocker_long = blocker_long or range_txt
            blocker_short = blocker_short or range_txt
        # v4.264 — raison EXACTE d un blocage anticipe (collecte, forex
        # ferme, chauffe, prix indisponible, horaires...) au lieu de "pas
        # encore de donnees" — cas permanent des actifs forex jusqu ici.
        blocked_reason = snap.get("blocked_reason")
        if blocked_reason and not has_position and ticker in cfg.get("FOREX_MODE_SYMBOLS", []):
            blocker_long = blocker_short = blocked_reason
        is_forex_row = ticker in cfg.get("FOREX_MODE_SYMBOLS", []) and ticker.startswith("xyz:")
        if is_forex_row:
            # Les marches forex HIP-3 sont reserves au mode Forex : les autres
            # modes ne les evaluent jamais (isolation v4.163).
            blocker_spot_accum = "non concerne (actif forex, mode Forex uniquement)"
            blocker_accumulation = blocker_accumulation_long = blocker_accumulation_short = blocker_spot_accum
        elif blocked_reason:
            if blocker_spot_accum == "pas encore de donnees":
                blocker_spot_accum = blocked_reason
            if blocker_accumulation == "pas encore de donnees":
                blocker_accumulation = blocker_accumulation_long = blocker_accumulation_short = blocked_reason
        try:  # v4.281 — qualite du marche (relative aux habitudes de l actif)
            quality = bot.market_quality(ticker, state)
        except Exception:
            quality = None
        _sr = getattr(state, "sr_display", None) or {}
        levels = {  # v4.283 — valeurs exactes, verifiables sur le graphique Hyperliquid
            "trend_ema": getattr(state, "trend_ema_value", None),
            "trend_ema_source": getattr(state, "trend_ema_source", None),
            "support": _sr.get("support"), "resistance": _sr.get("resistance"), "sr_source": _sr.get("source"),
            "price": (lambda v: float(v) if v else state.current_price)((bot.all_mids or {}).get(ticker)),
            # v4.286 — niveaux de STRUCTURE 1h
            "support_1h": bot._structural_support(state), "resistance_1h": bot._structural_resistance(state),
        }
        try:
            _px = None
            try:
                _px = float((bot.all_mids or {}).get(ticker) or 0) or None
            except (TypeError, ValueError):
                pass
            situation = bot.situation(state, _px) if ":" not in ticker else None
        except Exception:
            situation = None
        _fs = getattr(state, "funding_gate_snapshot", None) or {}
        if not cfg.get("FUNDING_MODE_ENABLED", False):
            blocker_funding = "mode desactive"
        elif not _fs:
            blocker_funding = snap.get("blocked_reason") or "pas encore evalue"
        elif _fs.get("blocker"):
            blocker_funding = _fs["blocker"]
        else:
            blocker_funding = f"signal {(_fs.get('direction') or '').upper()} ({_fs.get('annual_pct')}%/an) — candidat genere"
        results.append({
            "blocker_funding": blocker_funding,
            "situation": situation,
            "levels": levels,
            "quality": quality,
            "blocked_reason": blocked_reason,
            "ticker": ticker,
            "has_position": has_position,
            "snapshot_age_sec": round(time.time() - snap["ts"], 1) if snap.get("ts") else None,
            "blocker_long": blocker_long,
            "blocker_short": blocker_short,
            "blocker_spot_accum": blocker_spot_accum,
            "spot_accum_detail": spot_snap if spot_snap else None,
            "blocker_accumulation": blocker_accumulation,
            "blocker_accumulation_long": blocker_accumulation_long,
            "blocker_accumulation_short": blocker_accumulation_short,
            "accumulation_detail": accum_snap if accum_snap else None,
            # v4.255 — SUR DEMANDE EXPLICITE : expose directement le flux de
            # transactions calcule en interne — auparavant jamais visible
            # nulle part, empechant toute verification concrete que ce
            # mecanisme fonctionne reellement (donnees recuperees, calcul
            # correct) plutot que de rester une simple affirmation
            # theorique. None = pas encore de lecture disponible pour cet
            # actif a cet instant (historique insuffisant ou echec API).
            # v4.261 — SUR DEMANDE EXPLICITE, FIX BUG D AFFICHAGE : lisait
            # auparavant "entry_flow_pressure" (snap), calcule UNIQUEMENT
            # quand la logique d entree ATTEINT cette verification precise
            # — si un actif est bloque PLUS TOT (tendance, proximite...),
            # ce calcul n est jamais execute, affichant a tort "pas encore
            # de donnees" meme quand le mecanisme fonctionne normalement.
            # Lit desormais depuis state.trade_flow_history, alimente en
            # CONTINU par _maybe_refresh_trade_flow (tourne a chaque cycle,
            # independamment des conditions d entree) — la VRAIE source
            # utilisee par la pression soutenue et la sortie acceleree.
            # v4.266 — derniere lecture seulement si elle date de moins de 5 min
            "trade_flow_pressure": (round(list(state.trade_flow_history)[-1], 3)
                                    if getattr(state, "trade_flow_history", None)
                                    and getattr(state, "trade_flow_ts_history", None)
                                    and time.time() - list(state.trade_flow_ts_history)[-1] < 300
                                    else None),
            "trend_persistence_confirmed": snap.get("trend_persistence_confirmed") or (accum_snap.get("trend_persistence_confirmed") if accum_snap else None) or (spot_snap.get("trend_persistence_confirmed") if spot_snap else None),
        })
    results.sort(key=lambda r: r["ticker"])
    _rg = bot.market_regime()
    return {"results": results, "market_regime": {"regime": _rg.get("regime"), "detail": _rg.get("detail"),
                                                  "filter_enabled": bool(cfg.get("MARKET_REGIME_FILTER_ENABLED", 1))}}


# ─────────────────────────────────────────────────────────────────────────
#  v4.276 — REGIME DE MARCHE + STATISTIQUES PAR TRANCHE HORAIRE
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/market-regime")
def get_market_regime(email: str = Depends(require_user)):
    r = dict(bot.market_regime())
    r["filter_enabled"] = bool(cfg.get("MARKET_REGIME_FILTER_ENABLED", 1))
    r["blocks"] = {"haussier": "Accumulation (shorts) bloque", "baissier": "Spot-Accum (achats) bloque"}.get(r["regime"], "aucun blocage")
    return r


@app.get("/api/stats/hours")
def stats_by_hour(days: int = Query(14, ge=1, le=90), email: str = Depends(require_user)):
    """Resultats par mode et par tranche de 4 h UTC (trades fermes)."""
    labels = {"spot_accumulation": "Spot-Accum", "accumulation": "Accumulation", "funding_contrarian": "Funding", "forex": "Forex", "manual": "Manuel"}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = {}
    for t in db.get_all_closed_trades():
        if not t.get("closed_at") or (t.get("created_at") or "") < since or t.get("pnl") is None:
            continue
        try:
            h = datetime.fromisoformat(t["created_at"]).astimezone(timezone.utc).hour
        except (TypeError, ValueError):
            continue
        mode = labels.get(t.get("strategy") or "forex", t.get("strategy"))
        block = f"{h // 4 * 4:02d}-{h // 4 * 4 + 4:02d}"
        notional = (t.get("size_usd") or 0) * (t.get("leverage") or 1)
        fees = t.get("fees_paid") if t.get("trade_mode") == "live" and t.get("fees_paid") else notional * 0.0009
        b = out.setdefault(mode, {}).setdefault(block, {"n": 0, "wins": 0, "pnl": 0.0, "net": 0.0})
        b["n"] += 1
        b["wins"] += 1 if t["pnl"] > 0 else 0
        b["pnl"] += t["pnl"]
        b["net"] += t["pnl"] - fees
    for mode in out.values():
        for b in mode.values():
            b["win_rate"] = round(b["wins"] / b["n"] * 100, 1)
            b["pnl"], b["net"] = round(b["pnl"], 3), round(b["net"], 3)
    return {"days": days, "blocks_utc": ["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"], "modes": out}


# ─────────────────────────────────────────────────────────────────────────
#  v4.292 — CONTROLE DE SYNCHRONISATION BOT <-> HYPERLIQUID
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/live-sync")
def live_sync_report(email: str = Depends(require_user)):
    rep = getattr(bot, "live_sync_report", None)
    return rep or {"ts": None, "rows": [], "ok": None, "info": "premier controle dans les 2 minutes suivant le demarrage"}


@app.post("/api/live-sync/run")
def live_sync_run(email: str = Depends(require_user)):
    if bot.info is None or not cfg.get("WALLET_ADDRESS"):
        raise HTTPException(400, "Connexion Hyperliquid indisponible")
    return bot.run_live_sync()


# ─────────────────────────────────────────────────────────────────────────
#  v4.273 — TRADING MANUEL
# ─────────────────────────────────────────────────────────────────────────
class ManualOrderBody(BaseModel):
    strategy: str
    ticker: str
    direction: str
    market: str = "perp"
    mode: str = "paper"
    execution: str = "now"
    notional_usd: float
    leverage: int = 1
    sl_pct: float
    tp_pct: Optional[float] = None
    ttp_arm_pct: Optional[float] = None
    ttp_trail_pct: Optional[float] = None
    trigger_price: Optional[float] = None
    trigger_time: Optional[float] = None
    expiry_hours: Optional[float] = None
    require_valid: bool = True
    confirm_live: bool = False


class ManualModifyBody(BaseModel):
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    clear_tp: bool = False
    ttp_arm_pct: Optional[float] = None
    ttp_trail_pct: Optional[float] = None
    clear_ttp: bool = False


def _manual_call(fn, *args):
    try:
        return fn(*args)
    except manual_trading.ManualError as e:
        raise HTTPException(400, str(e))


@app.get("/api/manual/state")
def manual_state(email: str = Depends(require_user)):
    return bot.manual.snapshot()


@app.get("/api/manual/proposal")
def manual_proposal(strategy: str, ticker: str, direction: str, market: str = "perp",
                    email: str = Depends(require_user)):
    return _manual_call(bot.manual.propose, strategy, _norm_ticker(ticker), direction, market)


@app.post("/api/manual/orders")
def manual_submit(body: ManualOrderBody, email: str = Depends(require_user)):
    params = body.model_dump()
    params["ticker"] = _norm_ticker(params["ticker"])
    item = _manual_call(bot.manual.submit, params)
    if item.get("status") == "failed":
        raise HTTPException(400, f"Ouverture impossible : {item.get('error')}")
    return item


@app.delete("/api/manual/orders/{item_id}")
def manual_cancel(item_id: int, email: str = Depends(require_user)):
    return _manual_call(bot.manual.cancel, item_id)


@app.put("/api/manual/positions/{item_id}")
def manual_modify(item_id: int, body: ManualModifyBody, email: str = Depends(require_user)):
    changes = {}
    if body.sl_price:
        changes["sl_price"] = body.sl_price
    if body.clear_tp:
        changes["tp_price"] = None
    elif body.tp_price:
        changes["tp_price"] = body.tp_price
    if body.clear_ttp:
        changes["ttp_arm_pct"] = changes["ttp_trail_pct"] = None
    else:
        if body.ttp_arm_pct:
            changes["ttp_arm_pct"] = body.ttp_arm_pct
        if body.ttp_trail_pct:
            changes["ttp_trail_pct"] = body.ttp_trail_pct
    return _manual_call(bot.manual.modify, item_id, changes)


@app.post("/api/manual/positions/{item_id}/close")
def manual_close(item_id: int, email: str = Depends(require_user)):
    return _manual_call(bot.manual.close, item_id)


# ─────────────────────────────────────────────────────────────────────────
#  v4.265 — EXPORT CSV DU SUIVI DES TRADES SPOT-ACCUM / ACCUMULATION
# ─────────────────────────────────────────────────────────────────────────
_EXPORT_STRATEGY_LABEL = {"spot_accumulation": "Spot-Accum", "accumulation": "Accumulation", "funding_contrarian": "Funding", "manual": "Manuel"}
_ROUND_TRIP_FEE_RATE = 0.0009  # 2 x 0,045 % (taker) — estimation quand les frais reels manquent


def _resolve_tz(tz_name):
    """v4.269 — fuseau horaire de l utilisateur (transmis par le navigateur),
    UTC si inconnu ou invalide."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name), tz_name
        except Exception:
            pass
    return timezone.utc, "UTC"


def _local_time(iso_str, tz):
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).astimezone(tz).strftime("%d/%m/%Y %H:%M:%S")
    except (TypeError, ValueError):
        return ""


_SIM_LABEL = {"entree": "revenu a l entree", "sl": "SL touche", "aucun": "ni l un ni l autre (2 h)"}


def _fr(v, nd=4):
    """Nombre au format Excel francais (virgule decimale), vide si inconnu."""
    if v is None:
        return ""
    try:
        return f"{float(v):.{nd}f}".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _export_row(t, tz=timezone.utc):
    sign = 1 if t.get("action") == "LONG" else -1
    entry, exit_p = t.get("entry_price"), t.get("exit_price")
    lev = t.get("leverage") or 1
    E = t.get("size_usd")
    notional = E * lev if E else None
    try:
        opened = datetime.fromisoformat(t["created_at"])
        closed = datetime.fromisoformat(t["closed_at"]) if t.get("closed_at") else None
        duration = round((closed - opened).total_seconds() / 60, 1) if closed else None
    except (TypeError, ValueError, KeyError):
        duration = None
    move_pct = sign * (exit_p - entry) / entry * 100 if entry and exit_p else None
    fees_est = notional * _ROUND_TRIP_FEE_RATE if notional else None
    is_live = t.get("trade_mode") == "live"
    fees_used = t.get("fees_real") if t.get("fees_real") is not None else fees_est
    if is_live and t.get("pnl_real_hl") is not None and t.get("fees_real") is not None:
        pnl_net, net_src = t["pnl_real_hl"] - t["fees_real"], "reel Hyperliquid"
    elif t.get("pnl") is not None and fees_est is not None:
        pnl_net, net_src = t["pnl"] - fees_est, ("estime" if is_live else "estime (si live)")
    else:
        pnl_net, net_src = None, ""

    def after(p):
        return sign * (p - exit_p) / exit_p * 100 if p and exit_p else None
    best_60 = None
    back_to_entry = ""
    if exit_p and t.get("high_60m") is not None and t.get("low_60m") is not None:
        best_60 = (t["high_60m"] - exit_p) / exit_p * 100 if sign > 0 else (exit_p - t["low_60m"]) / exit_p * 100
        if entry and move_pct is not None and move_pct < 0:  # utile pour les pertes (SL trop serre ?)
            back_to_entry = "oui" if (t["high_60m"] >= entry if sign > 0 else t["low_60m"] <= entry) else "non"
    return [
        t.get("id"), _EXPORT_STRATEGY_LABEL.get(t.get("strategy"), t.get("strategy") or ""),
        t.get("coin", ""), t.get("action", ""), t.get("trade_mode") or "",
        _local_time(t.get("created_at"), tz), _local_time(t.get("closed_at"), tz), _fr(duration, 1),
        t.get("reason") or ("ouvert" if not t.get("closed_at") else ""),
        _fr(t.get("confidence"), 1), _fr(lev, 0), _fr(E, 2), _fr(notional, 2),
        _fr(entry, 6), _fr(exit_p, 6), t.get("exit_price_source") or "",
        _fr(move_pct, 3), _fr(t.get("peak_pnl_pct"), 3), _fr(t.get("pnl"), 4),
        _fr(fees_used, 4), "reels" if t.get("fees_real") is not None else (("estimes" if is_live else "estimes (si live)") if fees_used is not None else ""),
        _fr(t.get("pnl_real_hl"), 4), _fr(pnl_net, 4), net_src,
        _fr(t.get("price_after_30m"), 6), _fr(after(t.get("price_after_30m")), 3),
        _fr(t.get("price_after_60m"), 6), _fr(after(t.get("price_after_60m")), 3),
        _fr(best_60, 3), back_to_entry,
        _SIM_LABEL.get(t.get("sim_sl_075"), ""), _SIM_LABEL.get(t.get("sim_sl_100"), ""), _SIM_LABEL.get(t.get("sim_sl_150"), ""),
        _SIM_LABEL.get(t.get("sim_sl_200"), ""), _fr(t.get("sl_back_min"), 1), _fr(t.get("sl_mae_pct"), 3), _fr(t.get("sl_mark_120_pct"), 3),
        t.get("followup_status") or ("en attente" if t.get("closed_at") else ""),
        (t.get("entry_reasons") or "").replace(";", ","),
        _fr(t.get("vol_ratio"), 2), _fr(t.get("activity_ratio"), 2), _fr(t.get("flow_at_entry"), 2), _fr(t.get("spread_at_entry"), 4),
        t.get("trade_uid") or "",
    ]


_EXPORT_HEADER = [
    "id", "mode", "actif", "sens", "paper/live", "ouverture (Paris)", "fermeture (Paris)", "duree (min)",
    "motif de sortie", "confiance %", "levier", "marge E ($)", "notionnel ($)",
    "prix entree", "prix sortie", "source prix sortie", "mouvement de prix %", "pic %", "PnL brut ($)",
    "frais ($)", "type frais", "PnL Hyperliquid hors frais ($)", "PnL net ($)", "source PnL net",
    "prix +30 min", "evolution +30 min % (dans le sens du trade)",
    "prix +60 min", "evolution +60 min % (dans le sens du trade)",
    "meilleur mouvement dans l heure suivant la sortie %", "trade perdant : prix revenu a l entree dans l heure",
    "si SL a 0,75 % : issue", "si SL a 1 % : issue", "si SL a 1,5 % : issue",
    "si SL a 2 % : issue", "apres SL : minutes avant retour a l entree", "apres SL : pire recul depuis l entree avant retour %",
    "apres SL : prix 2 h apres vs entree %",
    "statut du suivi", "raisons d entree",
    "volatilite a l entree (x habitude)", "activite a l entree (x habitude)", "flux a l entree", "spread a l entree %",
    "identifiant trade",
]


# ─────────────────────────────────────────────────────────────────────────
#  v4.290 — ANALYSE STATISTIQUE DES STOP LOSS (elargir le SL ? patienter ?)
# ─────────────────────────────────────────────────────────────────────────
_SL_LEVELS = (("0,75 %", "sim_sl_075", 0.75), ("1 %", "sim_sl_100", 1.0), ("1,5 %", "sim_sl_150", 1.5), ("2 %", "sim_sl_200", 2.0))


def _pctl(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def _sl_analysis(days=None):
    rows = [t for t in db.get_trades_for_export(days=days)
            if t.get("closed_at") and (t.get("reason") or "").upper().startswith(("STOP LOSS", "SL "))
            and (t.get("sim2_status") or "").startswith("ok")]
    groups = {}
    for t in rows:
        groups.setdefault(_EXPORT_STRATEGY_LABEL.get(t.get("strategy"), t.get("strategy")), []).append(t)
    if len(groups) > 1:
        groups["Tous les modes"] = rows
    out = []
    for mode, ts in groups.items():
        n = len(ts)
        backs = [t["sl_back_min"] for t in ts if t.get("sl_back_min") is not None]
        maes_back = [t["sl_mae_pct"] for t in ts if t.get("sl_back_min") is not None and t.get("sl_mae_pct") is not None]
        actual = []
        for t in ts:
            sign = 1 if t.get("action") == "LONG" else -1
            if t.get("entry_price") and t.get("exit_price"):
                actual.append(sign * (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100)
        actual_total = sum(actual)
        levels = []
        for label, col, pct in _SL_LEVELS:
            back = sum(1 for t in ts if t.get(col) == "entree")
            hit = sum(1 for t in ts if t.get(col) == "sl")
            none = [t for t in ts if t.get(col) == "aucun"]
            simulated = -pct * hit + sum(max(t.get("sl_mark_120_pct") or 0.0, -pct) for t in none)
            levels.append({"sl": label, "revenu_entree": back, "sl_touche": hit, "ni_l_un_ni_l_autre": len(none),
                           "pct_revenu_entree": round(back / n * 100, 1) if n else None,
                           "resultat_simule_pts": round(simulated, 2), "resultat_reel_pts": round(actual_total, 2),
                           "gain_estime_pts": round(simulated - actual_total, 2)})
        out.append({
            "mode": mode, "nb_sl": n,
            "retour_entree_30min_pct": round(sum(1 for b in backs if b <= 30) / n * 100, 1) if n else None,
            "retour_entree_60min_pct": round(sum(1 for b in backs if b <= 60) / n * 100, 1) if n else None,
            "retour_entree_120min_pct": round(len(backs) / n * 100, 1) if n else None,
            "delai_median_retour_min": _pctl(backs, 0.5),
            "delai_p75_retour_min": _pctl(backs, 0.75),
            "recul_median_avant_retour_pct": _pctl(maes_back, 0.5),
            "recul_p75_avant_retour_pct": _pctl(maes_back, 0.75),
            "recul_p90_avant_retour_pct": _pctl(maes_back, 0.9),
            "niveaux": levels,
        })
    return out


@app.get("/api/stats/sl-analysis")
def sl_analysis(days: Optional[int] = Query(None, ge=1, le=3650), email: str = Depends(require_user)):
    return {"days": days, "modes": _sl_analysis(days)}


@app.get("/api/export/sl-analysis.csv")
def export_sl_analysis(days: Optional[int] = Query(None, ge=1, le=3650), email: str = Depends(require_user)):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["mode", "nb SL analyses", "revenu a l entree en 30 min %", "en 60 min %", "en 2 h %",
                "delai median de retour (min)", "delai 75e centile (min)",
                "pire recul median avant retour %", "75e centile %", "90e centile %",
                "SL simule", "revenu a l entree d abord", "SL simule touche", "ni l un ni l autre",
                "% revenu a l entree", "resultat reel des SL (pts de %)", "resultat simule (pts de %)", "gain estime (pts de %)"])
    for m in _sl_analysis(days):
        for lv in m["niveaux"]:
            w.writerow([m["mode"], m["nb_sl"], _fr(m["retour_entree_30min_pct"], 1), _fr(m["retour_entree_60min_pct"], 1),
                        _fr(m["retour_entree_120min_pct"], 1), _fr(m["delai_median_retour_min"], 1), _fr(m["delai_p75_retour_min"], 1),
                        _fr(m["recul_median_avant_retour_pct"], 3), _fr(m["recul_p75_avant_retour_pct"], 3), _fr(m["recul_p90_avant_retour_pct"], 3),
                        lv["sl"], lv["revenu_entree"], lv["sl_touche"], lv["ni_l_un_ni_l_autre"], _fr(lv["pct_revenu_entree"], 1),
                        _fr(lv["resultat_reel_pts"], 2), _fr(lv["resultat_simule_pts"], 2), _fr(lv["gain_estime_pts"], 2)])
    stamp = datetime.now().strftime("%Y-%m-%d_%Hh%M")
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="analyse_stop_loss_{stamp}.csv"'})


@app.get("/api/export/accumulation-trades.csv")
def export_accumulation_trades(days: Optional[int] = Query(None, ge=1, le=3650),
                               tz: Optional[str] = Query(None, max_length=64),
                               email: str = Depends(require_user)):
    """v4.265 — Suivi complet des trades Spot-Accum et Accumulation, au format
    CSV lisible directement par Excel (separateur ;, virgule decimale).
    Les colonnes +30/+60 min se remplissent automatiquement environ une
    heure apres chaque fermeture (bougies Hyperliquid) ; pour le live, frais
    et PnL reels proviennent des remplissages Hyperliquid."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    tzinfo, tz_label = _resolve_tz(tz)
    header = [h.replace("(Paris)", f"({tz_label})") for h in _EXPORT_HEADER]
    w.writerow(header)
    for t in db.get_trades_for_export(days=days):
        w.writerow(_export_row(t, tzinfo))
    stamp = datetime.now(tzinfo).strftime("%Y-%m-%d_%Hh%M")
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="suivi_trades_{stamp}.csv"'})


@app.get("/api/entry-diagnostics/{ticker}")
def get_entry_diagnostics_one(ticker: str, email: str = Depends(require_user)):
    """v4.40 — Detail COMPLET de l instantane des portes d entree pour UN
    actif precis (tous les champs bruts, pas juste le resume compact)."""
    ticker = _norm_ticker(ticker)
    best_state = None
    best_ts = -1
    for slot_key, state in bot.states.items():
        if be.ticker_from_slot_key(slot_key) == ticker:
            ts = (state.last_gate_snapshot or {}).get("ts", -1)
            if ts > best_ts:
                best_ts = ts
                best_state = state
    if best_state is None:
        raise HTTPException(404, f"Actif inconnu ou non suivi : {ticker}")
    return {
        "ticker": ticker,
        "has_position": best_state.position is not None,
        "position": best_state.position,
        "snapshot": best_state.last_gate_snapshot,
    }


@app.get("/api/signals")
def get_signals(limit: int = Query(50), strategy: str = Query(None), email: str = Depends(require_user)):
    """v4.44 — SUR DEMANDE EXPLICITE : parametre 'strategy' optionnel pour
    filtrer l historique PAR MODE. Sans ce filtre, la limite de 50 est
    partagee entre TOUS les modes confondus — insuffisant pour un historique
    par mode complet (avec 4 modes actifs, chacun pourrait n avoir que
    quelques lignes visibles, voire aucune, meme avec beaucoup de trades
    reels). Avec le filtre, on interroge un bassin bien plus large AVANT de
    filtrer, pour que 'limit' s applique au nombre de trades de CE mode
    precis, pas au nombre de trades tous modes confondus.
    v4.114 — FIX BUG : order_by_close=True trie desormais par date de
    FERMETURE (plus recent en premier) — l ancien tri par ouverture (id)
    inversait l ordre reel des trades fermes (un trade ouvert tot mais
    ferme tard apparaissait avant un trade ouvert tard mais ferme vite)."""
    if strategy:
        raw = db.get_trades(limit=max(limit * 20, 2000), order_by_close=True)
        filtered = [r for r in raw if (r.get("strategy") or "forex") == strategy]
        return {"signals": [_trade_row_to_signal(r) for r in filtered[:limit]]}
    return {"signals": [_trade_row_to_signal(r) for r in db.get_trades(limit=limit, order_by_close=True)]}


@app.get("/api/stats")
def get_stats(email: str = Depends(require_user)):
    rows = db.get_trades(limit=100000)
    if not rows:
        return {"total": 0, "longs": 0, "shorts": 0, "avg_confidence": 0, "avg_rr": "--"}
    longs = sum(1 for r in rows if r["action"] == "LONG")
    shorts = sum(1 for r in rows if r["action"] == "SHORT")
    confs = [r["confidence"] for r in rows if r["confidence"] is not None]
    rrs = [r["risk_reward"] for r in rows if r["risk_reward"] is not None]
    return {
        "total": len(rows),
        "longs": longs,
        "shorts": shorts,
        "avg_confidence": round(sum(confs) / len(confs), 1) if confs else 0,
        "avg_rr": round(sum(rrs) / len(rrs), 2) if rrs else "--",
    }


@app.get("/api/paper/portfolio")
def paper_portfolio(email: str = Depends(require_user)):
    # v3.2 — FIX : l interface attend total_pnl/total_pnl_pct (le PnL NON
    # REALISE des positions actuellement ouvertes), alors que cette route ne
    # renvoyait que le PnL REALISE cumule (s.pnl, uniquement des trades deja
    # fermes) — d ou "PnL ouvert" et "Performance" bloques a 0.00 en
    # permanence, meme avec des positions ouvertes en profit/perte.
    open_positions = _open_positions()
    unrealized_pnl = round(sum(p["pnl"] for p in open_positions), 2)
    # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son PROPRE
    # emplacement (bot.accum_states), jamais inclus ci-dessous auparavant.
    realized_pnl = sum(s.pnl for s in bot.states.values()) + sum(s.pnl for s in bot.accum_states.values())
    initial_balance = float(db.get_meta("initial_balance", cfg["CAPITAL_USD"])) or 1.0

    closed = db.get_all_closed_trades()
    wins = sum(1 for r in closed if (r["pnl"] or 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    # v3.2 — FIX : "balance" (affiche "SOLDE VIRTUEL") ne deduisait pas les
    # montants deja engages dans les positions ouvertes — il affichait donc
    # le capital total, pas ce qu il reste reellement disponible pour de
    # nouveaux trades.
    engaged = sum(p["size"] for p in open_positions)
    return {
        "balance": round(bot.capital + realized_pnl - engaged, 2),
        "open_trades": open_positions,
        "total_pnl": unrealized_pnl,
        "total_pnl_pct": round(unrealized_pnl / initial_balance * 100, 3),
        "win_rate": win_rate,
    }


@app.post("/api/paper/reset")
def paper_reset(email: str = Depends(require_user)):
    print(f"[AUDIT] /api/paper/reset appele par {email} a {datetime.now(timezone.utc).isoformat()}")
    if bot.trading_enabled:
        raise HTTPException(400, "Arretez le bot avant de reinitialiser")
    db.clear_all_trades()
    # v4.14 — le moteur (cycle de gestion des positions) tourne desormais en
    # continu, y compris pendant un reset (seul trading_enabled est verifie
    # ci-dessus, pas l arret du moteur) : on protege cette mutation directe
    # de l etat avec le meme verrou que _manage_position, pour eviter toute
    # collision avec un cycle en cours au meme instant.
    with bot.lock:
        for state in bot.states.values():
            state.position = None
            state.pnl = 0.0
            state.trades = 0
            state.wins = 0
            state.closed_trades.clear()
        # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son
        # PROPRE emplacement (bot.accum_states), jamais reinitialise
        # ci-dessus auparavant — les trades/PnL Accumulation auraient
        # survecu a une reinitialisation complete.
        for accum_state in bot.accum_states.values():
            accum_state.position = None
            accum_state.pnl = 0.0
            accum_state.trades = 0
            accum_state.wins = 0
            accum_state.closed_trades.clear()
    # v4.1 — FIX : repart du capital par defaut du CODE (be.CONFIG), pas de
    # cfg["CAPITAL_USD"] qui contient la derniere valeur PERSISTEE (chargee
    # au demarrage depuis hyperbot_capital_*.json) — sans ce fix, changer le
    # capital par defaut dans le code n avait plus aucun effet des qu un
    # fichier de capital existait deja sur le volume, et "reinitialiser"
    # ne faisait que re-sauvegarder cette meme valeur perimee.
    cfg["CAPITAL_USD"] = be.CONFIG["CAPITAL_USD"]
    bot.capital = cfg["CAPITAL_USD"]
    bot.sessions = 0
    bot.total_pnl_all = 0.0
    # v4.1 — un capital reinitialise demarre un lot neuf : l ancien E fige
    # (base sur l ancien capital) ne doit pas survivre a la reinitialisation.
    bot.batch_entry_size = None
    bot.clear_all_persisted_files()
    be.save_capital(bot.capital, 0, 0.0)
    be.save_batch_entry_size(None)
    db.set_meta("reset_at", db.now_iso())
    db.set_meta("initial_balance", str(bot.capital))
    db.set_meta("total_running_seconds", "0")
    db.set_meta("running_since", "")
    return {"ok": True}


class PaperCloseBody(BaseModel):
    trade_id: str  # = slot_key (ex: "BTC_0")
    reason: str = "MANUEL"


@app.post("/api/paper/close")
def paper_close(body: PaperCloseBody, email: str = Depends(require_user)):
    # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son PROPRE
    # emplacement (bot.accum_states) — repli si absent de bot.states.
    state = bot.states.get(body.trade_id)
    if not state or not state.position:
        state = bot.accum_states.get(body.trade_id)
    if not state or not state.position:
        raise HTTPException(404, "Aucune position ouverte pour cet identifiant")
    price = state.current_price or state.position["entry"]
    ticker = be.ticker_from_slot_key(body.trade_id)
    pos_snapshot = dict(state.position)  # avant fermeture — necessaire pour close_order (actifs spot)
    # v4.110 — FIX BUG CRITIQUE : utilisait cfg.get("MODE") (mode GLOBAL),
    # jamais le mode EFFECTIF par strategie — si le mode global est reste
    # "paper" alors qu UNE strategie precise (ex: Spot-Accum) a ete basculee
    # en live via le switch par mode, la fermeture reelle sur Hyperliquid
    # n etait JAMAIS tentee, alors que le suivi interne du bot marquait la
    # position comme fermee — position reelle abandonnee sans plus AUCUN
    # suivi (SL/TTP/retournement), un vrai risque de securite confirme.
    real_strategy = pos_snapshot.get("strategy", "forex")
    # v4.264 — mode REEL de la position (fige a son ouverture), et ordre
    # reel envoye AVANT de fermer le suivi interne : si Hyperliquid ne
    # confirme pas la fermeture, la position reste suivie par le bot au lieu
    # d etre abandonnee sans surveillance.
    effective_mode_close = bot._position_mode(pos_snapshot)
    close_order_ok = None
    if effective_mode_close == "live" and bot.exchange:
        close_order_ok = be.close_order(bot.exchange, body.trade_id, pos_snapshot, cfg)
        if not close_order_ok:
            _push_log("error", f"⚠️ [{ticker}] Fermeture manuelle : Hyperliquid n'a PAS confirme la fermeture — position conservee et toujours suivie par le bot. Reessayez ou verifiez sur Hyperliquid.")
            raise HTTPException(502, "Fermeture reelle non confirmee par Hyperliquid — position conservee et toujours suivie. Reessayez.")
    with _state_lock:
        if not state.position:
            raise HTTPException(409, "La position vient d'etre fermee par le bot entre-temps")
        exit_source = "simulation"
        if close_order_ok:
            real_exit = be.pop_last_close_fill(ticker)
            exit_source = "bot"
            if real_exit:
                price, exit_source = real_exit, "hyperliquid"
        pnl, win, trade = state.close_position(price, body.reason)
        trade["symbol"] = body.trade_id
        action = "LONG" if trade["type"] == "long" else "SHORT"
        trade_id = db.get_open_trade_id_by_uid(pos_snapshot.get("trade_uid")) or \
            db.get_open_trade_id_by_coin_action(ticker, action, real_strategy)
        if trade_id:
            db.close_trade(trade_id, trade["exit"], trade["pnl"], trade["reason"],
                           fees_paid=trade.get("fees_paid"), exit_price_source=exit_source)
        bot._save_open_positions()
    _push_log("warn", f"[{ticker}] Fermeture manuelle @ ${price:.2f} | PnL: {pnl:+.2f}$")
    return {"ok": True, "pnl": pnl, "real_close_confirmed": close_order_ok}


class SpotAccumTargetBody(BaseModel):
    trade_id: str  # = slot_key (ex: "BTC_0")
    target_price: Optional[float] = None  # None = retire l objectif (repli sur le trailing seul)


@app.put("/api/spot-accum/target")
def put_spot_accum_target(body: SpotAccumTargetBody, email: str = Depends(require_user)):
    """v4.47 — SUR DEMANDE EXPLICITE : permet de modifier l objectif (prix
    cible, 80% de la distance support-resistance par defaut) d une position
    Spot-Accumulation DEJA OUVERTE — utile si le marche a evolue depuis
    l entree et que l objectif initial ne semble plus pertinent. target_price
    a None retire l objectif fixe : la position ne sortira plus que via le
    trailing (3%/0.5%) ou le retournement confirme."""
    state = bot.states.get(body.trade_id)
    if not state or not state.position:
        raise HTTPException(404, "Aucune position ouverte pour cet identifiant")
    if state.position.get("strategy") != "spot_accumulation":
        raise HTTPException(400, "Cette position n'est pas en mode Spot-Accumulation")
    if body.target_price is not None and body.target_price <= (state.current_price or state.position["entry"]):
        raise HTTPException(400, "L'objectif doit être supérieur au prix actuel")
    state.position["target_price"] = body.target_price
    bot._save_open_positions()
    ticker = be.ticker_from_slot_key(body.trade_id)
    _push_log("info", f"[{ticker}] 🌱 Objectif Spot-Accum modifié manuellement : {'$'+str(body.target_price) if body.target_price else 'retiré (trailing seul)'}")
    return {"ok": True, "target_price": body.target_price}


class SpotAccumTrailingArmBody(BaseModel):
    trade_id: str
    trailing_arm_price: Optional[float] = None  # None = retire ce seuil, garde seulement le seuil de PnL


@app.put("/api/spot-accum/trailing-arm")
def put_spot_accum_trailing_arm(body: SpotAccumTrailingArmBody, email: str = Depends(require_user)):
    """v4.49 — SUR DEMANDE EXPLICITE : permet de modifier le seuil de PRIX
    (structurel, 70% de la distance support-resistance par defaut) qui arme
    le trailing pour une position Spot-Accumulation DEJA OUVERTE. S ADDITIONNE
    au seuil de PnL (SPOT_ACCUM_TTP_ARM_PCT) — arme des que l un des deux est
    atteint. None retire ce seuil : seul le PnL% arme alors le trailing."""
    state = bot.states.get(body.trade_id)
    if not state or not state.position:
        raise HTTPException(404, "Aucune position ouverte pour cet identifiant")
    if state.position.get("strategy") != "spot_accumulation":
        raise HTTPException(400, "Cette position n'est pas en mode Spot-Accumulation")
    if body.trailing_arm_price is not None and body.trailing_arm_price <= (state.current_price or state.position["entry"]):
        raise HTTPException(400, "Le seuil doit être supérieur au prix actuel")
    state.position["trailing_arm_price"] = body.trailing_arm_price
    bot._save_open_positions()
    ticker = be.ticker_from_slot_key(body.trade_id)
    _push_log("info", f"[{ticker}] 🌱 Seuil d'armement trailing Spot-Accum modifié : {'$'+str(body.trailing_arm_price) if body.trailing_arm_price else 'retiré (seuil PnL seul)'}")
    return {"ok": True, "trailing_arm_price": body.trailing_arm_price}


# ─────────────────────────────────────────────────────────────────────────
#  BILAN / STATISTIQUES / RAPPORT
# ─────────────────────────────────────────────────────────────────────────
def _day_key(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).astimezone(timezone.utc).strftime("%d/%m")


def _aggregate(rows: List[Dict[str, Any]], base: float = None) -> Dict[str, Any]:
    total = len(rows)
    wins = [r for r in rows if (r["pnl"] or 0) > 0]
    losses = [r for r in rows if (r["pnl"] or 0) <= 0]
    gains = round(sum(r["pnl"] for r in wins), 2)
    pertes = round(sum(r["pnl"] for r in losses), 2)
    net = round(gains + pertes, 2)
    win_rate = round(len(wins) / total * 100, 1) if total else 0
    # v4.2 — % du net par rapport au capital initial (base), quand fourni.
    # Permet d afficher la performance en % en plus du montant $, pour le
    # jour courant, le total et chaque jour de l historique (onglet Bilan).
    net_pct = round(net / base * 100, 2) if base else None
    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "gains": gains, "pertes": pertes, "net": net, "win_rate": win_rate,
        "net_pct": net_pct,
    }


def _compute_daily(rows: List[Dict[str, Any]], days: int = 7, base: float = None) -> List[Dict[str, Any]]:
    """v3.2 — Jour calendaire UTC fixe (00h00-23h59:59) : chaque trade est
    attribue au jour ou il a ete OUVERT (created_at), pas ferme (closed_at).
    Un trade ouvert juste avant minuit et ferme apres compte donc pour la
    journee de son ouverture — coherent avec le decoupage en jours fixes
    demande, sans qu un trade a cheval sur minuit ne soit "perdu" ou compte
    deux fois."""
    today = datetime.now(timezone.utc).date()
    buckets = {}
    for i in range(days):
        d = today - timedelta(days=i)  # v3.2 — du plus recent (aujourd hui) au plus vieux
        buckets[d.strftime("%d/%m")] = []
    for r in rows:
        if not r["created_at"]:
            continue
        try:
            key = _day_key(r["created_at"])
        except Exception:
            continue
        if key in buckets:
            buckets[key].append(r)
    out = []
    for day, day_rows in buckets.items():
        agg = _aggregate(day_rows, base)
        out.append({"day": day, **agg})
    return out


def _compute_by_coin(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_coin: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_coin.setdefault(r["coin"], []).append(r)
    out = []
    for coin, coin_rows in by_coin.items():
        agg = _aggregate(coin_rows)
        wins = [r for r in coin_rows if (r["pnl"] or 0) > 0]
        losses = [r for r in coin_rows if (r["pnl"] or 0) <= 0]
        avg_gain = round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0
        avg_loss = round(sum(r["pnl"] for r in losses) / len(losses), 2) if losses else 0
        total_minutes = 0
        for r in coin_rows:
            try:
                opened = datetime.fromisoformat(r["created_at"])
                closed = datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else opened
                total_minutes += int((closed - opened).total_seconds() / 60)
            except Exception:
                pass
        out.append({
            "coin": coin, **agg, "avg_gain": avg_gain, "avg_loss": avg_loss,
            "total_minutes": total_minutes,
        })
    out.sort(key=lambda c: c["net"], reverse=True)
    return out


@app.get("/api/report/daily-table")
def get_daily_table(email: str = Depends(require_user)):
    """Rapport journalier sur le jour calendaire UTC FIXE en cours
    (00h00:00 a 23h59:59) : gains, pertes, nombre de trades et performance
    par actif + une ligne total. v3.2 — attribution par date d OUVERTURE
    (created_at) : un trade ouvert aujourd hui mais ferme demain (ou plus
    tard) compte pour la journee de son ouverture, pas de sa fermeture —
    coherent avec le decoupage en jours fixes (plus de session glissante
    de 24h ni de blocage en fin de journee). Concu pour etre telecharge
    en tableau (CSV) depuis l interface."""
    closed = db.get_all_closed_trades()
    today_str = datetime.now(timezone.utc).strftime("%d/%m")
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = [r for r in closed if r["created_at"] and _day_key(r["created_at"]) == today_str]

    initial_balance = float(db.get_meta("initial_balance", cfg["CAPITAL_USD"])) or 1.0
    by_coin = _compute_by_coin(recent)
    for row in by_coin:
        row["performance_pct"] = round(row["net"] / initial_balance * 100, 3)

    total = _aggregate(recent)
    total["performance_pct"] = round(total["net"] / initial_balance * 100, 3)

    return {
        "period_start": day_start.isoformat(),
        "period_end": (day_start + timedelta(hours=23, minutes=59, seconds=59)).isoformat(),
        "by_coin": by_coin,
        "total": total,
    }


@app.get("/api/bilan")
def get_bilan(email: str = Depends(require_user)):
    closed = db.get_all_closed_trades()
    total_pnl_realized = sum(s.pnl for s in bot.states.values())  # realise cette session
    initial_balance = float(db.get_meta("initial_balance", cfg["CAPITAL_USD"]))
    open_positions = _open_positions()
    open_pnl = round(sum(p["pnl"] for p in open_positions), 2)
    # v3.2 — FIX : total_capital n incluait que le PnL REALISE (trades deja
    # fermes) — tant qu aucun trade n avait encore ete cloture dans la
    # session, "Total" et "Performance" restaient figes a 1000$/+0%, meme
    # avec des positions ouvertes clairement en profit/perte. On inclut
    # desormais aussi le PnL LATENT (mark-to-market), comme pour Paper
    # Trading — Capital total = valeur reelle actuelle du portefeuille.
    total_capital = bot.capital + total_pnl_realized + open_pnl
    performance_pct = round((total_capital - initial_balance) / initial_balance * 100, 2) if initial_balance else 0

    today_str = datetime.now(timezone.utc).strftime("%d/%m")
    # v3.2 — jour calendaire UTC fixe : attribution par date d OUVERTURE
    # (created_at), pas de fermeture — un trade ouvert aujourd hui mais
    # ferme demain compte pour aujourd hui.
    today_rows = [r for r in closed if r["created_at"] and _day_key(r["created_at"]) == today_str]

    return {
        "balance": round(bot.capital + total_pnl_realized - sum(p["size"] for p in open_positions), 2),
        "total_capital": round(total_capital, 2),
        "initial_balance": round(initial_balance, 2),
        "performance_pct": performance_pct,
        "open_pnl": open_pnl,
        "open_count": len(open_positions),
        "reset_at": db.get_meta("reset_at"),
        "running_seconds": round(_get_running_seconds()),
        "today": _aggregate(today_rows, initial_balance),
        "total": _aggregate(closed, initial_balance),
        "daily": _compute_daily(closed, days=7, base=initial_balance),
        "by_coin": _compute_by_coin(closed),
    }


@app.get("/api/hyperliquid/precision-check")
def get_precision_check(email: str = Depends(require_user)):
    """v4.101 — SUR DEMANDE EXPLICITE : verifie PROACTIVEMENT la precision
    reelle (szDecimals) de TOUS les actifs actuellement actifs, en une
    seule requete — au lieu de decouvrir les problemes de precision un par
    un via des echecs d ordres reels en production. Pour chaque actif,
    montre : szDecimals reel, un exemple de prix/taille formates avec la
    fonction du bot, et un avertissement si l actif semble absent des
    metadonnees Hyperliquid (cas ou un ordre echouerait a coup sur)."""
    if bot.info is None:
        raise HTTPException(503, "Connexion Hyperliquid non etablie — le bot doit etre demarre.")

    try:
        perp_map, spot_map = be._get_sz_decimals_map(bot.info)
    except Exception as e:
        raise HTTPException(503, f"Echec recuperation metadonnees Hyperliquid : {e}")

    active_coins = cfg.get("ACTIVE_COINS", [])
    results = []
    for ticker in active_coins:
        sz_dec = perp_map.get(ticker)
        row = {
            "ticker": ticker,
            "found_in_metadata": sz_dec is not None,
            "sz_decimals": sz_dec if sz_dec is not None else 4,
        }
        if sz_dec is None:
            row["warning"] = "Actif absent des metadonnees perp Hyperliquid — un ordre reel echouerait probablement (repli sur 4 decimales par defaut, potentiellement incorrect)."
        else:
            # Exemple concret avec un prix fictif typique, pour verifier que
            # le formatage produit un resultat coherent (pas de test reel
            # contre l API, juste une verification de calcul).
            sample_price = 1.23456789
            row["example_formatted_price"] = be.format_price_hl(sample_price, sz_dec, False)
            row["example_formatted_size"] = be.format_size_hl(1.23456789, sz_dec)
        results.append(row)

    missing = [r["ticker"] for r in results if not r["found_in_metadata"]]
    return {
        "total_active_coins": len(active_coins),
        "perp_assets_in_metadata": len(perp_map),
        "spot_assets_in_metadata": len(spot_map),
        "missing_from_metadata": missing,
        "assets": results,
    }


@app.get("/api/bilan-live")
def get_bilan_live(email: str = Depends(require_user)):
    """v4.90 — SUR DEMANDE EXPLICITE : bilan DEDIE au capital LIVE (reel,
    synchronise depuis Hyperliquid), completement separe du bilan paper
    ci-dessus — capital, PnL ouvert, performance, trades ouverts et win
    rate, tous calcules UNIQUEMENT a partir des trades reellement executes
    en live (trade_mode='live'), jamais melanges avec le paper."""
    closed_all = db.get_all_closed_trades()
    closed_live = [r for r in closed_all if r.get("trade_mode") == "live"]

    # v4.91 — SUR DEMANDE EXPLICITE : tente de recuperer le VRAI solde
    # Hyperliquid a CHAQUE consultation de cet onglet (pas seulement au
    # moment de basculer un mode en live) — pour reference, meme si aucun
    # mode n est encore live. Repli sur la derniere valeur connue
    # (live_capital_base) si la recuperation echoue (pas d identifiants
    # configures, ou API Hyperliquid indisponible).
    live_capital_base = getattr(bot, "live_capital_base", cfg.get("CAPITAL_USD", 0))
    hyperliquid_reachable = False
    hyperliquid_error = None
    if bot.info is None:
        hyperliquid_error = "Connexion Hyperliquid non etablie (bot.info absent) — verifiez que le bot a bien demarre."
    elif not cfg.get("WALLET_ADDRESS"):
        hyperliquid_error = "Adresse de wallet non configuree (WALLET_ADDRESS)."
    else:
        # v4.94 — FIX BUG CRITIQUE : documentation Hyperliquid confirmee par
        # recherche — "For API users, unified account and portfolio margin
        # shows all balances and holds in the spot clearinghouse state.
        # Individual perp dex user states are not meaningful." En mode
        # compte UNIFIE (celui de l utilisateur), marginSummary.accountValue
        # (endpoint perps) renvoie systematiquement $0, MEME avec des fonds
        # reels — le vrai solde se trouve dans le solde SPOT en USDC.
        # Interroge desormais LES DEUX endpoints et utilise le plus eleve
        # des deux (couvre a la fois les comptes Standard/Manual, ou les
        # fonds perps sont reellement separes, et les comptes Unified/
        # Portfolio Margin, ou le solde perps est toujours a 0 par design).
        try:
            perps_balance = 0.0
            perps_error = None
            try:
                raw_state = bot.info.user_state(cfg["WALLET_ADDRESS"])
                margin_summary = raw_state.get("marginSummary") if isinstance(raw_state, dict) else None
                if margin_summary and "accountValue" in margin_summary:
                    perps_balance = float(margin_summary["accountValue"])
                else:
                    perps_error = f"Reponse perps inattendue : {str(raw_state)[:200]}"
            except Exception as e:
                perps_error = f"{type(e).__name__}: {e}"

            spot_balance = 0.0
            spot_error = None
            try:
                spot_state = bot.info.spot_user_state(cfg["WALLET_ADDRESS"])
                balances = spot_state.get("balances", []) if isinstance(spot_state, dict) else []
                usdc_entry = next((b for b in balances if b.get("coin") == "USDC"), None)
                if usdc_entry:
                    spot_balance = float(usdc_entry.get("total", 0))
                else:
                    spot_error = f"Pas d'entree USDC dans le solde spot : {str(balances)[:200]}"
            except Exception as e:
                spot_error = f"{type(e).__name__}: {e}"

            fresh_balance = max(perps_balance, spot_balance)
            balance_source = "perps" if perps_balance >= spot_balance else "spot (compte probablement en mode Unifié)"

            if perps_error and spot_error:
                hyperliquid_error = f"Echec des deux endpoints — perps: {perps_error} | spot: {spot_error}"
            elif fresh_balance >= 0:
                live_capital_base = fresh_balance
                bot.live_capital_base = fresh_balance
                hyperliquid_reachable = True
                if fresh_balance == 0:
                    wallet_display = cfg["WALLET_ADDRESS"]
                    wallet_masked = f"{wallet_display[:6]}...{wallet_display[-4:]}" if len(wallet_display) > 12 else wallet_display
                    hyperliquid_error = f"Solde $0 sur les DEUX comptes (perps ET spot) pour l'adresse {wallet_masked} — verifiez que c'est bien la bonne adresse."
                else:
                    hyperliquid_error = f"Source du solde utilise : {balance_source} (perps: ${perps_balance:.2f}, spot USDC: ${spot_balance:.2f})"
        except Exception as e:
            import traceback
            hyperliquid_error = f"{type(e).__name__}: {e}"
            print(f"[BILAN-LIVE] Erreur complete :\n{traceback.format_exc()}")

    total_pnl_realized_live = sum(s.live_pnl for s in bot.states.values())

    open_positions = _open_positions()
    open_positions_live = [p for p in open_positions if p.get("effective_mode") == "live"]
    open_pnl_live = round(sum(p["pnl"] for p in open_positions_live), 2)

    total_capital_live = live_capital_base + total_pnl_realized_live + open_pnl_live
    performance_pct_live = round((total_capital_live - live_capital_base) / live_capital_base * 100, 2) if live_capital_base else 0

    stats_live = _aggregate(closed_live, live_capital_base)

    return {
        "hyperliquid_capital": round(live_capital_base, 2),
        "hyperliquid_reachable": hyperliquid_reachable,
        "hyperliquid_error": hyperliquid_error,
        "total_capital_live": round(total_capital_live, 2),
        "open_pnl": open_pnl_live,
        "open_count": len(open_positions_live),
        "performance_pct": performance_pct_live,
        "win_rate": stats_live["win_rate"],
        "total_trades": stats_live["total"],
        "wins": stats_live["wins"],
        "losses": stats_live["losses"],
        "net_realized": stats_live["net"],
        "open_positions": open_positions_live,
    }


@app.get("/api/stats/daily")
def get_stats_daily(email: str = Depends(require_user)):
    # v3.2 — FIX : l onglet Performance attend un OBJET structure precis
    # (summary/daily/wins/losses avec des noms de champs specifiques comme
    # total_wins_usdc, net_pnl, close_reason...), completement different de
    # ce que renvoyait cette route auparavant (une simple liste avec les
    # noms de _aggregate) — d ou la page blanche sans aucune donnee.
    closed = db.get_all_closed_trades()
    wins_rows   = [r for r in closed if (r["pnl"] or 0) > 0]
    losses_rows = [r for r in closed if (r["pnl"] or 0) <= 0]
    total_wins_usdc = round(sum(r["pnl"] for r in wins_rows), 2)
    total_losses_usdc = round(sum(r["pnl"] for r in losses_rows), 2)
    win_rate = round(len(wins_rows) / len(closed) * 100, 1) if closed else 0

    today = datetime.now(timezone.utc).date()
    buckets = {}
    for i in range(7):
        d = today - timedelta(days=6 - i)
        buckets[d.strftime("%d/%m")] = []
    for r in closed:
        if not r["closed_at"]:
            continue
        try:
            key = _day_key(r["closed_at"])
        except Exception:
            continue
        if key in buckets:
            buckets[key].append(r)

    daily = []
    for day, day_rows in buckets.items():
        dw = [r for r in day_rows if (r["pnl"] or 0) > 0]
        dl = [r for r in day_rows if (r["pnl"] or 0) <= 0]
        daily.append({
            "day": day,
            "wins": len(dw),
            "total_wins_usdc": round(sum(r["pnl"] for r in dw), 2),
            "losses": len(dl),
            "total_losses_usdc": round(sum(r["pnl"] for r in dl), 2),
            "net_pnl": round(sum(r["pnl"] or 0 for r in day_rows), 2),
        })

    def _fmt(r):
        return {
            "action": r["action"], "coin": r["coin"],
            "close_reason": r["reason"] or "?",
            "pnl": r["pnl"], "closed_at": r["closed_at"],
        }

    return {
        "summary": {
            "total_wins": len(wins_rows),
            "total_wins_usdc": total_wins_usdc,
            "total_losses": len(losses_rows),
            "total_losses_usdc": total_losses_usdc,
            "win_rate": win_rate,
        },
        "daily": daily,
        "wins": [_fmt(r) for r in sorted(wins_rows, key=lambda r: r["closed_at"] or "", reverse=True)],
        "losses": [_fmt(r) for r in sorted(losses_rows, key=lambda r: r["closed_at"] or "", reverse=True)],
    }


@app.post("/api/cleanup")
def cleanup(email: str = Depends(require_user)):
    print(f"[AUDIT] /api/cleanup appele par {email} a {datetime.now(timezone.utc).isoformat()}")
    # v3.2 — FIX : le bouton de l interface attend un champ "message" (via
    # alert(r.message)), jamais renvoye jusqu ici (d ou l impression que le
    # bouton "ne faisait rien"). Utilise desormais cleanup_signals, qui
    # cible specifiquement les doublons/orphelins "ouverts" (voir db.py) —
    # les trades reellement fermes (historique du Bilan) ne sont jamais
    # touches par ce nettoyage.
    # Retrouve l ID exact (pas juste le coin) de chaque position reellement
    # ouverte en memoire, pour proteger UNIQUEMENT cette ligne precise —
    # les eventuels AUTRES doublons du meme coin restent nettoyables.
    protected_ids = _compute_protected_trade_ids()
    duplicates, stale = db.cleanup_signals(stale_hours=24, protected_ids=protected_ids)
    old_closed = db.delete_trades_older_than(30)
    total = duplicates + stale + old_closed
    if total == 0:
        message = "Rien a nettoyer — aucun signal en double ou orphelin trouve."
    else:
        parts = []
        if duplicates:
            parts.append(f"{duplicates} doublon(s) ouvert(s)")
        if stale:
            parts.append(f"{stale} signal(aux) orphelin(s) (>24h sans fermeture)")
        if old_closed:
            parts.append(f"{old_closed} trade(s) ferme(s) de plus de 30 jours")
        message = "Nettoyage termine : " + ", ".join(parts) + "."
    return {"ok": True, "message": message, "duplicates": duplicates, "stale": stale, "old_closed": old_closed}


@app.post("/api/reset-all")
def reset_all(email: str = Depends(require_user)):
    print(f"[AUDIT] /api/reset-all appele par {email} a {datetime.now(timezone.utc).isoformat()}")
    if bot.trading_enabled:
        raise HTTPException(400, "Arretez le bot avant une reinitialisation complete")
    # v3.2 — FIX : preserve les identifiants Hyperliquid/Finnhub (wallet, cle
    # privee, cle Finnhub) avant de tout effacer, puis les restaure apres —
    # ce ne sont pas des "donnees de trading" a effacer par une remise a
    # zero du portefeuille/historique, ce sont des identifiants de connexion.
    # Un utilisateur ayant clique par erreur sur ce bouton (juste en dessous
    # du nettoyage des doublons) se retrouvait auparavant a devoir tout
    # ressaisir sans comprendre pourquoi.
    preserved_keys = ("PRIVATE_KEY", "WALLET_ADDRESS", "FINNHUB_API_KEY")
    preserved = {k: cfg.get(k) for k in preserved_keys if cfg.get(k)}
    db.clear_all_trades()
    # v4.65 — SUR DEMANDE EXPLICITE : "on ne touche pas aux reglages" — ce
    # bouton effacait AUSSI tous les reglages personnalises
    # (db.clear_config_overrides() + remise a zero complete de cfg), ce qui
    # contredit frontalement l intention exprimee. Retire : seules les
    # DONNEES DE TRADING (trades, positions, portefeuille) sont effacees
    # desormais, la configuration (SL/TTP par mode, ADX, listes d actifs,
    # tous les reglages avances) reste intacte.
    for state in bot.states.values():
        state.position = None
        state.pnl = 0.0
        state.trades = 0
        state.wins = 0
        state.closed_trades.clear()
    # v4.121 — SUR DEMANDE EXPLICITE : Accumulation a desormais son PROPRE
    # emplacement (bot.accum_states), jamais reinitialise ci-dessus
    # auparavant.
    for accum_state in bot.accum_states.values():
        accum_state.position = None
        accum_state.pnl = 0.0
        accum_state.trades = 0
        accum_state.wins = 0
        accum_state.closed_trades.clear()
    for k, v in preserved.items():
        cfg[k] = v
    bot.cfg = cfg
    bot.capital = cfg["CAPITAL_USD"]
    bot.sessions = 0
    bot.total_pnl_all = 0.0
    bot.batch_entry_size = None
    bot.clear_all_persisted_files()
    be.save_capital(bot.capital, 0, 0.0)
    be.save_batch_entry_size(None)
    db.set_meta("reset_at", db.now_iso())
    db.set_meta("initial_balance", str(bot.capital))
    db.set_meta("total_running_seconds", "0")
    db.set_meta("running_since", "")
    log_buffer.clear()
    return {"ok": True, "message": "Reinitialisation complete effectuee : signaux, trades, historique et portefeuille remis a zero."}


# ─────────────────────────────────────────────────────────────────────────
#  FICHIERS STATIQUES (index.html)
# ─────────────────────────────────────────────────────────────────────────
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    return {
        "ok": True,
        "bot_running": bot.running,  # v4.14 — moteur (collecte/WS), toujours actif independamment du trading
        "trading_enabled": bot.trading_enabled,
        "version": be.BOT_VERSION,
        "build": be.BOT_BUILD,
        "data_dir": _DATA_DIR,
        "data_dir_configured": _DATA_DIR_CONFIGURED,
        "boot_count": BOOT_COUNT,
        "persistence_note": (
            "boot_count doit augmenter (1, 2, 3...) a chaque redeploiement. "
            "S il repart toujours a 1, le Volume Railway n est pas monte "
            "correctement (voir HYPERBOT_DATA_DIR)."
        ),
    }
