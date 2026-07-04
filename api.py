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

# ── Dossier de donnees persistantes (a monter en Volume sur Railway) ─────
# DOIT etre fait avant tout le reste : bot_engine.py et db.py ecrivent des
# fichiers avec des chemins relatifs, resolus par rapport au repertoire
# courant du process.
_DATA_DIR = os.environ.get("HYPERBOT_DATA_DIR", ".")
os.makedirs(_DATA_DIR, exist_ok=True)
os.chdir(_DATA_DIR)

import json
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import db
import auth
import bot_engine as be

# ─────────────────────────────────────────────────────────────────────────
#  INITIALISATION
# ─────────────────────────────────────────────────────────────────────────
db.init_db()

# Nos 6 symboles reellement supportes (voir bot_engine.CONFIG["SYMBOLS"]).
# L interface propose 30 cryptos (ALL_COINS) — on ne peut en activer que
# parmi ce sous-ensemble reellement tradable par ce bot.
SUPPORTED_TICKERS = [be.ticker_from_slot_key(s) for s in be.CONFIG["SYMBOLS"]]

cfg = dict(be.CONFIG)
for k, v in db.get_all_config_overrides().items():
    cfg[k] = v
be.apply_profile(cfg, cfg.get("PROFILE", "swing"))

if db.get_meta("initial_balance") is None:
    db.set_meta("initial_balance", str(cfg["CAPITAL_USD"]))
if db.get_meta("reset_at") is None:
    db.set_meta("reset_at", db.now_iso())

event_queue = queue.Queue()
bot = be.BotEngine(cfg, event_queue)

log_buffer = deque(maxlen=500)
_state_lock = threading.Lock()


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
                log_buffer.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "level": data.get("level", "info"),
                    "msg": data.get("msg", ""),
                })
            elif etype == "trade_opened":
                db.insert_open_trade(
                    coin=data["coin"], action=data["action"], confidence=data["confidence"],
                    leverage=data["leverage"], position_size_pct=data["position_size_pct"],
                    risk_reward=data["risk_reward"], timeframe=data["timeframe"],
                    entry_price=data["entry"], stop_loss=data["stop_loss"],
                    take_profit1=data["take_profit1"], take_profit2=data["take_profit2"],
                )
            elif etype == "trade":
                ticker = be.ticker_from_slot_key(data.get("symbol", ""))
                action = "LONG" if data.get("type") == "long" else "SHORT"
                trade_id = db.get_open_trade_id_by_coin_action(ticker, action)
                if trade_id:
                    db.close_trade(trade_id, data.get("exit"), data.get("pnl"), data.get("reason"))
        except Exception as e:
            print(f"[event_consumer] Erreur traitement evenement {etype}: {e}")


threading.Thread(target=_consume_events, daemon=True).start()

app = FastAPI(title="HyperBot API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


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
    return {
        "trading_mode": cfg.get("MODE", "paper"),
        "profile": cfg.get("PROFILE", "swing"),
        "position_pct": cfg.get("POSITION_SIZE_PCT"),
        "max_loss_usd": cfg.get("MAX_LOSS_USD"),
        "quick_profit_usd": cfg.get("QUICK_PROFIT_ARM_USD"),
        "max_open_trades": cfg.get("MAX_OPEN_TRADES", len(SUPPORTED_TICKERS)),
        "active_coins": cfg.get("ACTIVE_COINS") or SUPPORTED_TICKERS,
        "supported_coins": SUPPORTED_TICKERS,
        "wallet": cfg.get("WALLET_ADDRESS", ""),
        "api_key": _mask(cfg.get("PRIVATE_KEY", "")),
        "finnhub_key": _mask(cfg.get("FINNHUB_API_KEY", "")),
        "filter_hours": cfg.get("CRYPTO_OFFPEAK_ENABLED", True),
        "filter_weekend": bool(cfg.get("FOREX_SYMBOLS")),
        "filter_macro": cfg.get("CPI_BLACKOUT_ENABLED", True),
        "ai_continuous": db.get_config_override("ai_continuous", False),
        "running": bot.running,
        "ws_healthy": bot._is_ws_healthy() if bot.info is not None else False,
        "hyperliquid_configured": bool(cfg.get("PRIVATE_KEY") and cfg.get("WALLET_ADDRESS")),
    }


def _apply_and_persist(key: str, value):
    cfg[key] = value
    db.set_config_override(key, value)


def _open_positions() -> List[Dict[str, Any]]:
    out = []
    for slot_key, state in bot.states.items():
        pos = state.position
        if not pos:
            continue
        ticker = be.ticker_from_slot_key(slot_key)
        price = state.current_price or pos["entry"]
        if pos["type"] == "long":
            pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
        else:
            pnl_pct = (pos["entry"] - price) / pos["entry"] * 100
        pnl = pos["size"] * pnl_pct / 100
        out.append({
            "id": slot_key,
            "coin": ticker,
            "action": "LONG" if pos["type"] == "long" else "SHORT",
            "entry_price": pos["entry"],
            "current_price": price,
            "size": pos["size"],
            "stop_loss": pos["sl"],
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
        })
    return out


def _trade_row_to_signal(row: Dict[str, Any]) -> Dict[str, Any]:
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
        "stop_loss": row["stop_loss"],
        "take_profit1": row["take_profit1"],
        "take_profit2": row["take_profit2"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "exit_price": row["exit_price"],
        "pnl": row["pnl"],
        "reason": row["reason"],
    }


# ─────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────
class ConfigBody(BaseModel):
    trading_mode: Optional[str] = None
    position_pct: Optional[float] = None
    max_loss_usd: Optional[float] = None
    quick_profit_usd: Optional[float] = None
    max_open_trades: Optional[int] = None
    wallet: Optional[str] = None
    api_key: Optional[str] = None
    active_coins: Optional[List[str]] = None


@app.get("/api/config")
def get_config(email: str = Depends(require_user)):
    return _public_config()


@app.put("/api/config")
def put_config(body: ConfigBody, email: str = Depends(require_user)):
    if body.trading_mode is not None:
        if bot.running:
            raise HTTPException(400, "Arretez le bot avant de changer de mode (paper/live)")
        if body.trading_mode not in ("paper", "live"):
            raise HTTPException(400, "trading_mode doit etre 'paper' ou 'live'")
        _apply_and_persist("MODE", body.trading_mode)

    if body.position_pct is not None:
        _apply_and_persist("POSITION_SIZE_PCT", body.position_pct)

    if body.max_loss_usd is not None:
        _apply_and_persist("MAX_LOSS_USD", body.max_loss_usd)

    if body.quick_profit_usd is not None:
        _apply_and_persist("QUICK_PROFIT_ARM_USD", body.quick_profit_usd)
        _apply_and_persist("QUICK_PROFIT_LOCK_USD", body.quick_profit_usd)

    if body.max_open_trades is not None:
        _apply_and_persist("MAX_OPEN_TRADES", body.max_open_trades)

    if body.wallet is not None:
        _apply_and_persist("WALLET_ADDRESS", body.wallet)

    if body.api_key is not None and not body.api_key.startswith("****"):
        _apply_and_persist("PRIVATE_KEY", body.api_key)

    if body.active_coins is not None:
        valid = [c for c in body.active_coins if c in SUPPORTED_TICKERS]
        ignored = [c for c in body.active_coins if c not in SUPPORTED_TICKERS]
        _apply_and_persist("ACTIVE_COINS", valid)
        if ignored:
            log_buffer.append({
                "time": datetime.now(timezone.utc).isoformat(), "level": "warn",
                "msg": f"Actifs ignores (non supportes par ce bot) : {', '.join(ignored)}"
            })

    return _public_config()


class HyperliquidBody(BaseModel):
    wallet: Optional[str] = None
    api_key: Optional[str] = None


@app.put("/api/config/hyperliquid")
def put_hyperliquid(body: HyperliquidBody, email: str = Depends(require_user)):
    if body.wallet is not None:
        _apply_and_persist("WALLET_ADDRESS", body.wallet)
    if body.api_key is not None and not body.api_key.startswith("****"):
        _apply_and_persist("PRIVATE_KEY", body.api_key)
    return {"ok": True, "note": "Prend effet au prochain demarrage du bot (arret puis demarrage)."}


class FinnhubBody(BaseModel):
    finnhub_key: str


@app.put("/api/config/finnhub")
def put_finnhub(body: FinnhubBody, email: str = Depends(require_user)):
    _apply_and_persist("FINNHUB_API_KEY", body.finnhub_key)
    return {"ok": True}


class FiltersBody(BaseModel):
    filter_hours: Optional[bool] = None
    filter_weekend: Optional[bool] = None
    filter_macro: Optional[bool] = None


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
    if bot.running:
        raise HTTPException(400, "Le bot tourne deja")
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
    return {"ok": True}


@app.post("/api/bot/stop")
def bot_stop(email: str = Depends(require_user)):
    bot.stop()
    return {"ok": True}


@app.get("/api/bot/logs")
def bot_logs(persistent: bool = Query(False), limit: int = Query(200), email: str = Depends(require_user)):
    if persistent:
        # Lit la fin du fichier de log sur disque (persiste entre redemarrages
        # si HYPERBOT_DATA_DIR pointe vers un Volume Railway).
        try:
            with open(be.LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-limit:]
            return {"logs": [l.rstrip("\n") for l in lines]}
        except FileNotFoundError:
            return {"logs": []}
    return {"logs": list(log_buffer)[-limit:]}


# ─────────────────────────────────────────────────────────────────────────
#  DONNEES DE MARCHE / POSITIONS / SIGNAUX
# ─────────────────────────────────────────────────────────────────────────
@app.get("/api/prices")
def get_prices(email: str = Depends(require_user)):
    return {be.ticker_from_slot_key(k): s.current_price for k, s in bot.states.items() if s.current_price}


@app.get("/api/positions")
def get_positions(email: str = Depends(require_user)):
    return _open_positions()


@app.get("/api/signals")
def get_signals(limit: int = Query(50), email: str = Depends(require_user)):
    return [_trade_row_to_signal(r) for r in db.get_trades(limit=limit)]


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
    total_pnl = sum(s.pnl for s in bot.states.values())
    return {
        "balance": round(bot.capital + total_pnl, 2),
        "open_trades": _open_positions(),
    }


@app.post("/api/paper/reset")
def paper_reset(email: str = Depends(require_user)):
    if bot.running:
        raise HTTPException(400, "Arretez le bot avant de reinitialiser")
    db.clear_all_trades()
    for state in bot.states.values():
        state.position = None
        state.pnl = 0.0
        state.trades = 0
        state.wins = 0
        state.closed_trades.clear()
    bot.capital = cfg["CAPITAL_USD"]
    bot.sessions = 0
    bot.total_pnl_all = 0.0
    be.save_capital(bot.capital, 0, 0.0)
    db.set_meta("reset_at", db.now_iso())
    db.set_meta("initial_balance", str(bot.capital))
    return {"ok": True}


class PaperCloseBody(BaseModel):
    trade_id: str  # = slot_key (ex: "BTC_0")
    reason: str = "MANUEL"


@app.post("/api/paper/close")
def paper_close(body: PaperCloseBody, email: str = Depends(require_user)):
    state = bot.states.get(body.trade_id)
    if not state or not state.position:
        raise HTTPException(404, "Aucune position ouverte pour cet identifiant")
    price = state.current_price or state.position["entry"]
    ticker = be.ticker_from_slot_key(body.trade_id)
    pos_snapshot = dict(state.position)  # avant fermeture — necessaire pour close_order (actifs spot)
    with _state_lock:
        pnl, win, trade = state.close_position(price, body.reason)
        trade["symbol"] = body.trade_id
        if cfg.get("MODE") == "live" and bot.exchange:
            be.close_order(bot.exchange, body.trade_id, pos_snapshot, cfg)
        action = "LONG" if trade["type"] == "long" else "SHORT"
        trade_id = db.get_open_trade_id_by_coin_action(ticker, action)
        if trade_id:
            db.close_trade(trade_id, trade["exit"], trade["pnl"], trade["reason"])
    log_buffer.append({
        "time": datetime.now(timezone.utc).isoformat(), "level": "warn",
        "msg": f"[{ticker}] Fermeture manuelle @ ${price:.2f} | PnL: {pnl:+.2f}$"
    })
    return {"ok": True, "pnl": pnl}


# ─────────────────────────────────────────────────────────────────────────
#  BILAN / STATISTIQUES / RAPPORT
# ─────────────────────────────────────────────────────────────────────────
def _day_key(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).astimezone(timezone.utc).strftime("%d/%m")


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    wins = [r for r in rows if (r["pnl"] or 0) > 0]
    losses = [r for r in rows if (r["pnl"] or 0) <= 0]
    gains = round(sum(r["pnl"] for r in wins), 2)
    pertes = round(sum(r["pnl"] for r in losses), 2)
    net = round(gains + pertes, 2)
    win_rate = round(len(wins) / total * 100, 1) if total else 0
    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "gains": gains, "pertes": pertes, "net": net, "win_rate": win_rate,
    }


def _compute_daily(rows: List[Dict[str, Any]], days: int = 7) -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    buckets = {}
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        buckets[d.strftime("%d/%m")] = []
    for r in rows:
        if not r["closed_at"]:
            continue
        try:
            key = _day_key(r["closed_at"])
        except Exception:
            continue
        if key in buckets:
            buckets[key].append(r)
    out = []
    for day, day_rows in buckets.items():
        agg = _aggregate(day_rows)
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


@app.get("/api/bilan")
def get_bilan(email: str = Depends(require_user)):
    closed = db.get_all_closed_trades()
    total_pnl_open = sum(s.pnl for s in bot.states.values())  # realise cette session (redondant avec DB si tout est bien synchro)
    initial_balance = float(db.get_meta("initial_balance", cfg["CAPITAL_USD"]))
    total_capital = bot.capital + total_pnl_open
    open_positions = _open_positions()
    open_pnl = round(sum(p["pnl"] for p in open_positions), 2)
    performance_pct = round((total_capital - initial_balance) / initial_balance * 100, 2) if initial_balance else 0

    today_str = datetime.now(timezone.utc).strftime("%d/%m")
    today_rows = [r for r in closed if r["closed_at"] and _day_key(r["closed_at"]) == today_str]

    return {
        "balance": round(total_capital - sum(p["size"] for p in open_positions), 2),
        "total_capital": round(total_capital, 2),
        "initial_balance": round(initial_balance, 2),
        "performance_pct": performance_pct,
        "open_pnl": open_pnl,
        "open_count": len(open_positions),
        "reset_at": db.get_meta("reset_at"),
        "today": _aggregate(today_rows),
        "total": _aggregate(closed),
        "daily": _compute_daily(closed, days=7),
        "by_coin": _compute_by_coin(closed),
    }


@app.get("/api/stats/daily")
def get_stats_daily(email: str = Depends(require_user)):
    closed = db.get_all_closed_trades()
    return _compute_daily(closed, days=7)


@app.post("/api/cleanup")
def cleanup(email: str = Depends(require_user)):
    deleted = db.delete_trades_older_than(30)
    return {"ok": True, "deleted": deleted}


@app.post("/api/reset-all")
def reset_all(email: str = Depends(require_user)):
    if bot.running:
        raise HTTPException(400, "Arretez le bot avant une reinitialisation complete")
    db.clear_all_trades()
    db.clear_config_overrides()
    for state in bot.states.values():
        state.position = None
        state.pnl = 0.0
        state.trades = 0
        state.wins = 0
        state.closed_trades.clear()
    cfg.clear()
    cfg.update(be.CONFIG)
    be.apply_profile(cfg, cfg.get("PROFILE", "swing"))
    # cfg["SYMBOLS"] doit rester sous forme de slot_keys ("BTC_0", ...) pour
    # rester coherent avec les cles de bot.states (jamais reconstruit ici) —
    # be.CONFIG contient les tickers "nus", il faut donc reappliquer la forme
    # slot_key existante plutot que la forme d origine.
    cfg["SYMBOLS"] = list(bot.states.keys())
    bot.cfg = cfg
    bot.capital = cfg["CAPITAL_USD"]
    bot.sessions = 0
    bot.total_pnl_all = 0.0
    be.save_capital(bot.capital, 0, 0.0)
    db.set_meta("reset_at", db.now_iso())
    db.set_meta("initial_balance", str(bot.capital))
    log_buffer.clear()
    return {"ok": True}


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
    return {"ok": True, "bot_running": bot.running, "version": be.BOT_VERSION, "build": be.BOT_BUILD}
