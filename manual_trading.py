"""
manual_trading.py — v4.273 — TRADING MANUEL assiste par le bot.

Principe
--------
Le bot publie ses OPPORTUNITES (les candidats d entree qu il detecte a
chaque cycle, mode par mode). L utilisateur peut en choisir une TANT QU ELLE
EST VALABLE et uniquement DANS LE SENS PREVU PAR LE BOT. Le bot propose tous
les parametres (prix, taille, levier, SL, TP, TTP), tous modifiables.
L ordre peut etre execute tout de suite ou PROGRAMME (declenchement sur prix
ou a une heure donnee, avec expiration), en paper ou en live (confirmation
obligatoire), sur les perps ou en SPOT (achat uniquement, sans levier).

Une position manuelle est ensuite suivie EN TEMPS REEL par le bot selon SES
propres parametres (SL / TP / TTP choisis), independamment de la logique
des modes automatiques. Elle est enregistree dans l historique (strategie
"manual"), l export CSV, et survit aux redemarrages.

Regles de securite (Hyperliquid ne tient qu UNE position perp par actif) :
  - un ordre manuel LIVE perp est refuse si le bot (ou un autre ordre manuel)
    detient deja une position live sur cet actif, ou si une position existe
    deja sur Hyperliquid ;
  - reciproquement, le bot n ouvre pas de position live sur un actif tenu
    manuellement en live (voir BotEngine._finalize_open).
En perp live, le SL choisi est aussi pose en ORDRE NATIF sur Hyperliquid
(protection meme si le bot est arrete). En SPOT, la protection est geree par
le bot uniquement.
"""
import threading
import time
from datetime import datetime

import db
import bot_engine as be

STRATEGY_LABELS = {
    "libre": "Ordre libre",
    "forex": "Forex",
    "accumulation": "Accumulation",
    "spot_accumulation": "Spot-Accum",
    "funding_contrarian": "Funding",
}
OPEN_STATUSES = ("scheduled", "opening", "open", "closing")
TREND_FR = {"haussiere": "haussière", "baissiere": "baissière", "neutre": "neutre"}  # v4.343
FEE_ROUND_TRIP = be.FEE_RATE_TAKER_ESTIMATE * 2


class ManualError(Exception):
    """Erreur de validation ou d execution presentee telle quelle a l utilisateur."""


class ManualTrading:
    def __init__(self, bot):
        self.bot = bot
        self.lock = threading.RLock()
        self.opportunities = {}   # (strategy, ticker, direction) -> dict
        self.items = {}           # id -> dict (ordres programmes + positions ouvertes)
        self._busy = set()        # ids en cours d execution (ouverture/fermeture)
        self._fire_busy = set()   # v4.343 : ordres programmes en cours de declenchement (controle de tendance)
        self._push_busy = set()   # v4.343 : mises a jour du SL natif en cours (verrou progressif)
        self._trend_last = {}     # v4.343 : id -> derniere verification de la regle de sortie sur tendance
        self._last_persist = {}
        self._load()

    # ─────────────────────────────── outils ──────────────────────────────
    @property
    def cfg(self):
        return self.bot.cfg

    def _log(self, msg, level="info"):
        self.bot.emit("log", {"msg": f"✋ {msg}", "level": level})

    def _wallet(self):
        return self.cfg.get("WALLET_ADDRESS")

    def _load(self):
        for row in db.manual_list(OPEN_STATUSES):
            item = row["data"]
            item["id"] = row["id"]
            item["status"] = row["status"]
            if item["status"] in ("opening", "closing"):
                # coupure pendant une operation : on repart d un etat stable,
                # la reconciliation avec Hyperliquid tranchera
                item["status"] = "open" if item.get("entry_price") else "scheduled"
            self.items[item["id"]] = item

    def _save(self, item):
        data = {k: v for k, v in item.items() if k not in ("id", "status")}
        db.manual_update(item["id"], item["status"], data)
        self._last_persist[item["id"]] = time.time()

    # ─────────────────────────── opportunites ────────────────────────────
    def record_candidates(self, strategy, candidates):
        """Appele par le bot a chaque cycle avec ses candidats d entree
        (avant tout filtrage par plafond de positions)."""
        now = time.time()
        with self.lock:
            for c in candidates:
                key = (strategy, c["ticker"], c["signal"])
                opp = self.opportunities.get(key)
                if opp is None or now - opp["last_seen"] > self._ttl():
                    opp = {"strategy": strategy, "ticker": c["ticker"], "direction": c["signal"],
                           "first_seen": now}
                    self.opportunities[key] = opp
                opp.update({
                    "last_seen": now,
                    "price": c.get("price"),
                    "confidence": c.get("confidence"),
                    "support": c.get("support"),
                    "resistance": c.get("resistance"),
                    "reasons": [str(r) for r in (c.get("reasons") or [])][:8],
                })

    def _ttl(self):
        return self.cfg.get("MANUAL_OPPORTUNITY_TTL_SEC", 120)

    def get_opportunity(self, strategy, ticker, direction):
        opp = self.opportunities.get((strategy, ticker, direction))
        if opp and time.time() - opp["last_seen"] <= self._ttl():
            return opp
        return None

    def list_opportunities(self):
        now = time.time()
        with self.lock:
            for key in [k for k, o in self.opportunities.items() if now - o["last_seen"] > self._ttl() * 5]:
                del self.opportunities[key]
            out = []
            for o in self.opportunities.values():
                valid = now - o["last_seen"] <= self._ttl()
                if not valid:
                    continue
                out.append({**o, "strategy_label": STRATEGY_LABELS.get(o["strategy"], o["strategy"]),
                            "age_sec": round(now - o["first_seen"]),
                            "current_price": self.price(o["ticker"], "perp"),
                            "spot_available": self.spot_pair(o["ticker"]) is not None and o["direction"] == "long"})
            out.sort(key=lambda o: -(o.get("confidence") or 0))
            return out

    # ─────────────────────────── marches / prix ──────────────────────────
    def spot_pair(self, ticker):
        """Paire spot Hyperliquid correspondant a un ticker perp (ex: BTC ->
        UBTC/USDC, HYPE -> HYPE/USDC). None si aucun marche spot."""
        if not ticker or ":" in ticker:
            return None
        info = self.bot.info
        if info is None:
            return None
        names = getattr(info, "name_to_coin", {}) or {}
        for base in (ticker, "U" + ticker):
            pair = f"{base}/USDC"
            if pair in names:
                coin = names[pair]
                try:
                    szd = info.asset_to_sz_decimals[info.coin_to_asset[coin]]
                except (KeyError, AttributeError):
                    szd = 4
                return {"pair": pair, "coin": coin, "base": base, "sz_decimals": szd}
        return None

    def price(self, ticker, market="perp"):
        mids = getattr(self.bot, "all_mids", {}) or {}
        key = ticker
        if market == "spot":
            sp = self.spot_pair(ticker)
            if not sp:
                return None
            key = sp["coin"]
        raw = mids.get(key)
        try:
            p = float(raw)
            return p if p > 0 else None
        except (TypeError, ValueError):
            return None

    # ───────────────────────────── proposition ───────────────────────────
    # ─────────────────────── ORDRE LIBRE (v4.343) ────────────────────────
    def _propose_free(self, ticker, direction, market):
        """Proposition par defaut d un ORDRE LIBRE : n importe quel actif suivi (crypto ou
        Forex), sans opportunite du bot. Tout reste modifiable sur le ticket."""
        if direction not in ("long", "short"):
            raise ManualError("Sens invalide.")
        if market == "spot" and (direction != "long" or not self.spot_pair(ticker)):
            raise ManualError("Pas de marche spot disponible pour cet actif (le spot n autorise que l achat).")
        price = self.price(ticker, market)
        if not price:
            raise ManualError(f"Prix indisponible pour {ticker} : actif inconnu ou non suivi par le bot.")
        cfg = self.cfg
        lev = 1 if market == "spot" else int(cfg.get("MANUAL_DEFAULT_LEVERAGE", 1))
        sl_pct = float(cfg.get("MANUAL_DEFAULT_SL_PCT", 1.0))
        return {
            "strategy": "libre", "free": True, "ticker": ticker, "direction": direction, "market": market,
            "price": price, "leverage": lev, "notional_usd": round(float(cfg.get("MANUAL_DEFAULT_NOTIONAL_USD", 15.0)), 2),
            "margin_usd": round(float(cfg.get("MANUAL_DEFAULT_NOTIONAL_USD", 15.0)) / max(lev, 1), 2),
            "sl_pct": round(sl_pct, 3), "sl_basis": "SL par defaut (ordre libre)",
            "tp_pct": round(2 * sl_pct, 3), "tp_basis": "2 x le SL (a activer si souhaite)",
            "ttp_arm_pct": 1.0, "ttp_trail_pct": 0.5,
            "lock_arm_pct": round(sl_pct, 3), "lock_fraction": float(cfg.get("MANUAL_DEFAULT_LOCK_FRACTION", 0.5)),
            "trend": self.trend_info(ticker),
            "confidence": None, "reasons": [],
            "spot_available": self.spot_pair(ticker) is not None and direction == "long",
            "live_available": self.bot.exchange is not None and bool(self._wallet()),
            "min_notional_usd": 10.0,
        }

    def trend_info(self, ticker, use_cache=False):
        """Tendance de fond H4 d un actif (meme calcul que le top-down : EMA50/EMA200 sur bougies
        H4 cloturees + prix live), avec l etat de fraicheur des donnees."""
        price = self.price(ticker, "perp")
        if not price:
            return {"ok": False, "why": "prix indisponible"}
        try:
            mv = self.bot.mtf_view(ticker, price, cache_only=use_cache)
        except Exception as e:
            return {"ok": False, "why": f"{type(e).__name__}: {e}"}
        if not mv or not mv.get("ok"):
            return {"ok": False, "why": (mv or {}).get("why") or "historique H4 indisponible pour cet actif"}
        return {"ok": True, "trend": mv["trend"], "data_fresh": bool(mv.get("data_fresh", True)),
                "last_closed_t": mv.get("last_closed_t"), "major_tf": mv.get("major_tf")}

    def _check_trend_entry(self, ticker, required):
        """Condition d entree sur la tendance H4 : leve ManualError si elle n est pas remplie
        (ou si la tendance est indisponible / perimee : on n ouvre jamais a l aveugle)."""
        info = self.trend_info(ticker)
        if not info["ok"]:
            raise ManualError(f"Tendance H4 indisponible pour {ticker} ({info['why']}) — ordre non ouvert (condition d entree demandee).")
        if not info["data_fresh"]:
            raise ManualError(f"Donnees H4 de {ticker} perimees — ordre non ouvert (condition d entree demandee).")
        if info["trend"] != required:
            raise ManualError(f"Tendance H4 de {ticker} : {TREND_FR.get(info['trend'], info['trend'])} "
                              f"(requis : {TREND_FR.get(required, required)}) — ordre non ouvert.")
        return info["trend"]

    @staticmethod
    def _validate_lock_trend(p):
        """Validation des options v4.343 : verrou progressif et criteres de tendance."""
        la, lf = p.get("lock_arm_pct"), p.get("lock_fraction")
        if (la in (None, "", 0)) != (lf in (None, "", 0)):
            raise ManualError("Verrou progressif : renseignez a la fois le seuil de depart et la part garantie (ou aucun des deux).")
        if la not in (None, "", 0):
            la, lf = float(la), float(lf)
            if not 0.05 <= la <= 500:
                raise ManualError("Verrou progressif : seuil de depart entre 0,05 % et 500 %.")
            if not 0.05 <= lf <= 0.95:
                raise ManualError("Verrou progressif : part garantie entre 5 % et 95 % du gain maximal.")
            p["lock_arm_pct"], p["lock_fraction"] = la, lf
        else:
            p["lock_arm_pct"] = p["lock_fraction"] = None
        te = p.get("trend_entry") or None
        if te not in (None, "haussiere", "baissiere", "neutre"):
            raise ManualError("Condition d entree sur la tendance invalide.")
        tx = p.get("trend_exit") or None
        if tx not in (None, "opposee", "perdue"):
            raise ManualError("Regle de sortie sur la tendance invalide.")
        p["trend_entry"], p["trend_exit"] = te, tx

    def propose(self, strategy, ticker, direction, market="perp"):
        if strategy == "libre":
            return self._propose_free(ticker, direction, market)
        opp = self.get_opportunity(strategy, ticker, direction)
        if opp is None:
            raise ManualError("Cette opportunite n est pas (ou plus) valable dans ce sens.")
        if market == "spot" and (direction != "long" or not self.spot_pair(ticker)):
            raise ManualError("Pas de marche spot disponible pour cet actif (le spot n autorise que l achat).")
        cfg = self.cfg
        price = self.price(ticker, market) or opp.get("price")
        if not price:
            raise ManualError("Prix indisponible pour le moment.")
        lev = 1 if market == "spot" else int(cfg.get("MANUAL_DEFAULT_LEVERAGE", 1))
        notional = float(cfg.get("MANUAL_DEFAULT_NOTIONAL_USD", 15.0))

        # SL : meme logique que le mode source
        if strategy == "spot_accumulation":
            sl_pct, sl_basis = (cfg.get("SPOT_ACCUM_SL_CAP_PCT") or cfg.get("STRUCTURAL_SL_HARD_CAP_PCT", 0.5)), "plafond SL Spot-Accum"
        elif strategy == "accumulation":
            sl_pct, sl_basis = (cfg.get("ACCUMULATION_SL_CAP_PCT") or cfg.get("STRUCTURAL_SL_HARD_CAP_PCT", 0.5)), "plafond SL Accumulation"
        else:
            sl_of_e = (cfg.get("FUNDING_SL_PCT_OF_E") if strategy == "funding_contrarian" else None) or cfg.get("SL_PCT_OF_E", 1.0)
            sl_pct = max(sl_of_e / max(lev, 1), cfg.get("SL_MIN_PRICE_MOVE_PCT", 0.3))
            sl_basis = f"SL {STRATEGY_LABELS.get(strategy, strategy)} ({sl_of_e}% de la marge / levier)"

        # TP : 80 % de la fourchette support/resistance si exploitable
        sup, res = opp.get("support"), opp.get("resistance")
        tp_pct, tp_basis = None, ""
        if sup and res and res > sup:
            target = sup + 0.8 * (res - sup) if direction == "long" else res - 0.8 * (res - sup)
            dist = (target - price) / price * 100 if direction == "long" else (price - target) / price * 100
            if dist >= max(sl_pct, 0.3):
                tp_pct, tp_basis = dist, "80 % de la fourchette support/resistance"
        if tp_pct is None:
            tp_pct, tp_basis = max(2 * sl_pct, cfg.get("TAKE_PROFIT_PCT", 1.5)), "2 x le SL (fourchette S/R inexploitable)"

        # TTP : reglages du mode source
        if strategy == "funding_contrarian":
            arm, trail = cfg.get("FUNDING_TTP_ARM_PCT", 1.0), cfg.get("FUNDING_TTP_TOLERANCE_PCT", 0.5)
        elif strategy == "accumulation":
            arm = cfg.get("ACCUMULATION_TTP_ARM1_PRICE_PCT") or cfg.get("TTP_ARM1_PRICE_PCT", 1.0)
            trail = cfg.get("ACCUMULATION_TTP_TRAIL_GAP_PRICE_PCT") or cfg.get("TTP_DYNAMIC_TRAIL_GAP_PCT", 0.5)
        else:
            arm, trail = cfg.get("TTP_ARM1_PRICE_PCT", 1.0), cfg.get("TTP_DYNAMIC_TRAIL_GAP_PCT", 0.5)

        return {
            "strategy": strategy, "ticker": ticker, "direction": direction, "market": market,
            "price": price, "leverage": lev, "notional_usd": round(notional, 2),
            "margin_usd": round(notional / max(lev, 1), 2),
            "sl_pct": round(sl_pct, 3), "sl_basis": sl_basis,
            "tp_pct": round(tp_pct, 3), "tp_basis": tp_basis,
            "ttp_arm_pct": arm, "ttp_trail_pct": trail,
            "confidence": opp.get("confidence"), "reasons": opp.get("reasons", []),
            "spot_available": self.spot_pair(ticker) is not None and direction == "long",
            "live_available": self.bot.exchange is not None and bool(self._wallet()),
            "min_notional_usd": 10.0,
        }

    # ───────────────────────────── validations ───────────────────────────
    def has_live_perp(self, ticker, exclude_id=None):
        return any(i["status"] in ("opening", "open", "closing") and i["mode"] == "live"
                   and i["market"] == "perp" and i["ticker"] == ticker
                   and i.get("id") != exclude_id for i in self.items.values())

    def live_perp_coins(self):
        return {i["ticker"] for i in self.items.values()
                if i["status"] in ("opening", "open", "closing") and i["mode"] == "live" and i["market"] == "perp"}

    def _bot_live_position_on(self, ticker):
        for pool in (self.bot.states, self.bot.accum_states):
            for slot, st in pool.items():
                if st.position and be.ticker_from_slot_key(slot) == ticker and self.bot._position_mode(st.position) == "live":
                    return True
        return False

    def _check_live_perp_allowed(self, ticker, exclude_id=None):
        """exclude_id (v4.344) : l ordre en cours d ouverture est deja dans self.items au statut "opening" ;
        sans l exclure, il se comptait LUI-MEME comme "position manuelle live deja ouverte" et aucun ordre
        live perp ne pouvait jamais s ouvrir (bug present depuis v4.273 : le controle est refait dans
        _open_impl, apres le passage au statut "opening")."""
        if self._bot_live_position_on(ticker):
            raise ManualError(f"Le bot detient deja une position LIVE sur {ticker} : Hyperliquid ne tient qu une position par actif (elles se fondraient et se fermeraient ensemble).")
        if self.has_live_perp(ticker, exclude_id=exclude_id):
            raise ManualError(f"Une position manuelle LIVE est deja ouverte sur {ticker}.")
        szi = be.get_exchange_position_szi(self.bot.info, self._wallet(), ticker)
        if szi is None:
            raise ManualError("Impossible de verifier les positions Hyperliquid pour le moment — reessayez.")
        if szi != 0:
            raise ManualError(f"Une position existe deja sur {ticker} dans votre compte Hyperliquid.")

    def _validate(self, p):
        for key in ("strategy", "ticker", "direction", "market", "mode", "execution"):
            if not p.get(key):
                raise ManualError(f"Champ manquant : {key}")
        if p["direction"] not in ("long", "short") or p["market"] not in ("perp", "spot") or p["mode"] not in ("paper", "live"):
            raise ManualError("Parametres invalides.")
        if p["strategy"] == "libre":
            # v4.343 — ORDRE LIBRE : aucun besoin d une opportunite du bot ; l actif doit simplement etre suivi
            opp = {}
            p["require_valid"] = False
            if not self.price(p["ticker"], p["market"]):
                raise ManualError(f"Prix indisponible pour {p['ticker']} : actif inconnu ou non suivi par le bot.")
        else:
            opp = self.get_opportunity(p["strategy"], p["ticker"], p["direction"])
            if opp is None:
                raise ManualError("Cette opportunite n est plus valable (ou le sens ne correspond pas a celui prevu par le bot).")
        self._validate_lock_trend(p)
        if p["market"] == "spot":
            if p["direction"] != "long":
                raise ManualError("Le spot n autorise que l achat (LONG).")
            if not self.spot_pair(p["ticker"]):
                raise ManualError(f"Pas de marche spot pour {p['ticker']}.")
            p["leverage"] = 1
        lev = int(p.get("leverage") or 1)
        if not 1 <= lev <= int(self.cfg.get("MANUAL_MAX_LEVERAGE", 20)):
            raise ManualError(f"Levier hors limites (1 a {self.cfg.get('MANUAL_MAX_LEVERAGE', 20)}).")
        p["leverage"] = lev
        notional = float(p.get("notional_usd") or 0)
        if notional <= 0:
            raise ManualError("Taille invalide.")
        if p["mode"] == "live" and notional < 10:
            raise ManualError("Hyperliquid exige au moins 10 $ de notionnel par ordre.")
        sl = float(p.get("sl_pct") or 0)
        if not 0.05 <= sl <= 50:
            raise ManualError("Le stop loss doit etre compris entre 0,05 % et 50 % du prix d entree.")
        tp = p.get("tp_pct")
        if tp not in (None, "", 0) and not 0.05 <= float(tp) <= 500:
            raise ManualError("Take profit invalide (0,05 % a 500 %, ou vide).")
        arm, trail = float(p.get("ttp_arm_pct") or 0), float(p.get("ttp_trail_pct") or 0)
        if arm and not (0.05 <= arm <= 500 and 0.02 <= trail <= 100):
            raise ManualError("Trailing TP invalide (armement 0,05-500 %, repli 0,02-100 %).")
        if p["execution"] not in ("now", "price_above", "price_below", "time"):
            raise ManualError("Type d execution invalide.")
        if p["execution"] in ("price_above", "price_below") and not float(p.get("trigger_price") or 0) > 0:
            raise ManualError("Prix de declenchement manquant.")
        if p["execution"] == "time" and not float(p.get("trigger_time") or 0) > time.time():
            raise ManualError("L heure de declenchement doit etre dans le futur.")
        if p["mode"] == "live":
            if not p.get("confirm_live"):
                raise ManualError("Confirmation LIVE requise.")
            if self.bot.exchange is None or not self._wallet():
                raise ManualError("Connexion Hyperliquid indisponible : le live est impossible pour le moment.")
            if p["market"] == "perp":
                self._check_live_perp_allowed(p["ticker"])
        return opp

    # ───────────────────────────── soumission ────────────────────────────
    def submit(self, params):
        with self.lock:
            p = dict(params)
            opp = self._validate(p)
            if p["execution"] == "now" and p.get("trend_entry"):
                self._check_trend_entry(p["ticker"], p["trend_entry"])   # v4.343 : refuse avant toute creation
            now = time.time()
            item = {
                "strategy_source": p["strategy"], "ticker": p["ticker"], "direction": p["direction"],
                "market": p["market"], "mode": p["mode"], "leverage": p["leverage"],
                "notional_usd": float(p["notional_usd"]),
                "sl_pct": float(p["sl_pct"]),
                "tp_pct": float(p["tp_pct"]) if p.get("tp_pct") not in (None, "", 0) else None,
                "ttp_arm_pct": float(p.get("ttp_arm_pct") or 0) or None,
                "ttp_trail_pct": float(p.get("ttp_trail_pct") or 0) or None,
                # v4.343 — verrou progressif du SL + criteres de tendance (choix manuels)
                "lock_arm_pct": p.get("lock_arm_pct"), "lock_fraction": p.get("lock_fraction"), "lock_active": False,
                "trend_entry": p.get("trend_entry"), "trend_exit": p.get("trend_exit"),
                "execution": p["execution"],
                "trigger_price": float(p["trigger_price"]) if p.get("trigger_price") else None,
                "trigger_time": float(p["trigger_time"]) if p.get("trigger_time") else None,
                "expires_at": now + float(p.get("expiry_hours") or self.cfg.get("MANUAL_ORDER_EXPIRY_HOURS", 24)) * 3600,
                "require_valid": bool(p.get("require_valid", True)),
                "confidence": opp.get("confidence"), "reasons": opp.get("reasons", []),
                "created_at": now, "trade_uid": be.make_trade_uid("manual"),
            }
            status = "opening" if p["execution"] == "now" else "scheduled"
            item["id"] = db.manual_insert(status, item)
            item["status"] = status
            self.items[item["id"]] = item
        if p["execution"] == "now":
            self._open(item)
        else:
            self._log(f"Ordre programme #{item['id']} : {item['direction'].upper()} {item['ticker']} ({item['market']}, {item['mode']}) — {self._trigger_text(item)}", "info")
        return self.public(item)

    def _trigger_text(self, item):
        if item["execution"] == "price_above":
            return f"si le prix monte a {item['trigger_price']:.6g}"
        if item["execution"] == "price_below":
            return f"si le prix descend a {item['trigger_price']:.6g}"
        if item["execution"] == "time":
            return "a " + datetime.fromtimestamp(item["trigger_time"]).strftime("%d/%m %H:%M")
        return "immediat"

    # ─────────────────────────────── ouverture ───────────────────────────
    def _open(self, item):
        if item["id"] in self._busy:
            return
        self._busy.add(item["id"])
        try:
            self._open_impl(item)
        except Exception as e:
            item["status"], item["error"] = "failed", f"{type(e).__name__}: {e}"
            self._save(item)
            self._log(f"#{item['id']} {item['ticker']} : echec d ouverture — {item['error']}", "error")
        finally:
            self._busy.discard(item["id"])

    def _open_impl(self, item):
        ticker, market, is_long = item["ticker"], item["market"], item["direction"] == "long"
        price = self.price(ticker, market)
        if not price:
            raise ManualError("prix indisponible")
        item["status"] = "opening"
        self._save(item)
        if item["mode"] == "live":
            if market == "perp":
                self._check_live_perp_allowed(ticker, exclude_id=item["id"])
                entry, qty = self._open_live_perp(item, price, is_long)
            else:
                entry, qty = self._open_live_spot(item, price)
        else:
            entry = price
            qty = item["notional_usd"] / price
        item.update({
            "status": "open", "entry_price": entry, "qty": qty, "opened_at": time.time(),
            "notional_usd": qty * entry,
            "sl_price": entry * (1 - item["sl_pct"] / 100) if is_long else entry * (1 + item["sl_pct"] / 100),
            "tp_price": (entry * (1 + item["tp_pct"] / 100) if is_long else entry * (1 - item["tp_pct"] / 100)) if item["tp_pct"] else None,
            "peak_pct": 0.0, "armed": False, "last_price": price, "lock_active": False,
        })
        try:
            ti = self.trend_info(ticker, use_cache=True)
            item["trend_at_entry"] = ti.get("trend") if ti.get("ok") else None
        except Exception:
            item["trend_at_entry"] = None
        self._save(item)
        try:
            db.upsert_open_trade({
                "trade_uid": item["trade_uid"], "coin": ticker, "action": "LONG" if is_long else "SHORT",
                "entry": entry, "strategy": "manual", "trade_mode": item["mode"],
                "slot_key": f"MANUAL__{item['id']}", "leverage": item["leverage"],
                "size_usd": item["notional_usd"] / max(item["leverage"], 1), "confidence": item.get("confidence"),
                "stop_loss": item["sl_price"], "take_profit1": item["tp_price"],
                "entry_reasons": f"Manuel ({market.upper()}) sur opportunite {STRATEGY_LABELS.get(item['strategy_source'], item['strategy_source'])} | " + " | ".join(item.get("reasons", [])[:4]),
            })
        except Exception as e:
            print(f"[MANUEL] Ecriture historique #{item['id']} impossible : {e}")
        self._log(f"#{item['id']} OUVERT : {item['direction'].upper()} {ticker} {market.upper()} ({item['mode']}) @ {entry:.6g} | "
                  f"{item['notional_usd']:.2f} $ x{item['leverage']} | SL {item['sl_price']:.6g}"
                  + (f" | TP {item['tp_price']:.6g}" if item["tp_price"] else "")
                  + (f" | TTP +{item['ttp_arm_pct']}%/-{item['ttp_trail_pct']}%" if item["ttp_arm_pct"] else "")
                  + (f" | verrou {round(item['lock_fraction'] * 100)}% des +{item['lock_arm_pct']}%" if item.get("lock_arm_pct") else "")
                  + (f" | tendance H4 a l entree : {TREND_FR.get(item.get('trend_at_entry'), '?')}" if item.get("trend_entry") or item.get("trend_exit") else ""), "signal")

    def _open_live_perp(self, item, price, is_long):
        ex, ticker, lev = self.bot.exchange, item["ticker"], item["leverage"]
        try:
            ex.update_leverage(lev, ticker, is_cross=not ticker.startswith("xyz:"))
        except Exception as e:
            raise ManualError(f"levier x{lev} refuse par Hyperliquid ({e})")
        sl_price = price * (1 - item["sl_pct"] / 100) if is_long else price * (1 + item["sl_pct"] / 100)
        margin = item["notional_usd"] / max(lev, 1)
        ok, err, fill = be.place_order(ex, ticker, is_long, margin, price, self.cfg, sl_price=sl_price,
                                       tp_price=None, leverage=lev, trade_uid=item["trade_uid"])
        if not ok:
            raise ManualError(err or "ordre refuse")
        ep = None
        for _ in range(4):
            ep = (be.fetch_exchange_positions(self.bot.info, self._wallet(), [ticker]) or {}).get(ticker)
            if ep and ep["szi"] != 0:
                break
            time.sleep(0.7)
        if not ep or ep["szi"] == 0:
            raise ManualError("ordre accepte mais aucune position constatee sur Hyperliquid")
        return (fill or ep["entry"] or price), abs(ep["szi"])

    def _spot_available(self, token):
        state = self.bot.info.spot_user_state(self._wallet()) or {}
        for b in state.get("balances", []):
            if b.get("coin") == token:
                return float(b.get("total", 0) or 0) - float(b.get("hold", 0) or 0)
        return 0.0

    def _open_live_spot(self, item, price):
        sp = self.spot_pair(item["ticker"])
        usdc = self._spot_available("USDC")
        if usdc < item["notional_usd"]:
            raise ManualError(f"USDC insuffisant cote SPOT ({usdc:.2f} $ disponibles, {item['notional_usd']:.2f} $ requis) — transferez des USDC du compte Perp vers le compte Spot.")
        before = self._spot_available(sp["base"])
        sz = be.format_size_hl(item["notional_usd"] / price, sp["sz_decimals"])
        result = self.bot.exchange.market_open(sp["pair"], True, sz)
        status = be._order_first_status(result) if result else None
        if not (result and result.get("status") == "ok" and status and "filled" in status):
            raise ManualError(f"achat spot refuse : {str(result)[:200]}")
        fill = float(status["filled"]["avgPx"])
        time.sleep(0.7)
        qty = self._spot_available(sp["base"]) - before  # quantite REELLEMENT recue (frais preleves en jetons)
        if qty <= 0:
            qty = float(status["filled"]["totalSz"])
        return fill, qty

    # ─────────────────────────────── fermeture ───────────────────────────
    def close(self, item_id, reason="MANUEL"):
        item = self.items.get(item_id)
        if not item or item["status"] != "open":
            raise ManualError("Position introuvable ou deja fermee.")
        self._close(item, reason, self.price(item["ticker"], item["market"]) or item.get("last_price"))
        if item["status"] != "closed":
            raise ManualError(item.get("error") or "Fermeture non confirmee — position conservee.")
        return self.public(item)

    def _close(self, item, reason, price):
        if item["id"] in self._busy:
            return
        self._busy.add(item["id"])
        try:
            self._close_impl(item, reason, price)
        except Exception as e:
            item["status"], item["error"] = "open", f"{type(e).__name__}: {e}"
            self._log(f"#{item['id']} {item['ticker']} : erreur de fermeture ({item['error']}) — position conservee", "error")
        finally:
            self._busy.discard(item["id"])

    def _close_impl(self, item, reason, price):
        if True:
            if not price:
                raise ManualError("prix indisponible")
            item["status"] = "closing"
            exit_price, source = price, "simulation"
            if item["mode"] == "live":
                if item["market"] == "perp":
                    pos = {"type": item["direction"], "entry": item["entry_price"],
                           "size": item["notional_usd"] / max(item["leverage"], 1), "leverage": item["leverage"]}
                    if not be.close_order(self.bot.exchange, item["ticker"], pos, self.cfg):
                        item["status"], item["error"] = "open", "fermeture reelle non confirmee par Hyperliquid"
                        self._log(f"#{item['id']} {item['ticker']} : fermeture NON confirmee — position conservee, nouvel essai au prochain signal", "error")
                        return
                    fill = be.pop_last_close_fill(item["ticker"])
                    exit_price, source = (fill, "hyperliquid") if fill else (price, "bot")
                    self._cancel_native_orders(item["ticker"])
                else:
                    sp = self.spot_pair(item["ticker"])
                    sz = be.format_size_hl(self._spot_available(sp["base"]), sp["sz_decimals"])
                    result = self.bot.exchange.market_open(sp["pair"], False, sz) if sz > 0 else None
                    status = be._order_first_status(result) if result else None
                    if not (status and "filled" in status):
                        item["status"], item["error"] = "open", f"vente spot non executee : {str(result)[:150]}"
                        self._log(f"#{item['id']} {item['ticker']} spot : vente NON executee — position conservee", "error")
                        return
                    exit_price, source = float(status["filled"]["avgPx"]), "hyperliquid"
            sign = 1 if item["direction"] == "long" else -1
            move_pct = sign * (exit_price - item["entry_price"]) / item["entry_price"] * 100
            pnl = item["qty"] * (exit_price - item["entry_price"]) * sign
            fees = item["notional_usd"] * FEE_ROUND_TRIP
            item.update({"status": "closed", "closed_at": time.time(), "exit_price": exit_price,
                         "pnl": pnl, "fees_est": fees, "reason": reason, "exit_source": source, "error": None})
            self._save(item)
            self.items.pop(item["id"], None)
            try:
                tid = db.get_open_trade_id_by_uid(item["trade_uid"])
                if tid:
                    db.close_trade(tid, exit_price, pnl, reason, peak_pnl_pct=item.get("peak_pct"),
                                   fees_paid=fees if item["mode"] == "live" else None, exit_price_source=source)
            except Exception as e:
                print(f"[MANUEL] Cloture historique #{item['id']} impossible : {e}")
            self._log(f"#{item['id']} FERME ({reason}) : {item['ticker']} @ {exit_price:.6g} | {move_pct:+.2f}% | PnL {pnl:+.2f} $", "win" if pnl > 0 else "loss")

    def _cancel_native_orders(self, ticker):
        try:
            sl_oids, tp_oids = be._get_open_orders_by_type(self.bot.info, self._wallet(), ticker)
            oids = sl_oids + tp_oids
            if oids:
                self.bot.exchange.bulk_cancel([{"coin": ticker, "oid": o} for o in oids])
        except Exception as e:
            print(f"[MANUEL] Annulation ordres natifs {ticker} : {e}")

    # ─────────────────────────────── modifications ───────────────────────
    def modify(self, item_id, changes):
        with self.lock:
            item = self.items.get(item_id)
            if not item:
                raise ManualError("Element introuvable.")
            if item["status"] == "scheduled":
                for k in ("sl_pct", "tp_pct", "ttp_arm_pct", "ttp_trail_pct", "trigger_price", "notional_usd", "leverage",
                          "lock_arm_pct", "lock_fraction", "trend_entry", "trend_exit"):
                    if k in changes:
                        item[k] = changes[k]
                probe = {k: item.get(k) for k in ("lock_arm_pct", "lock_fraction", "trend_entry", "trend_exit")}
                self._validate_lock_trend(probe)      # v4.343 : memes regles qu a la creation
                item.update(probe)
                self._save(item)
                return self.public(item)
            if item["status"] != "open":
                raise ManualError("Position non modifiable dans son etat actuel.")
            is_long = item["direction"] == "long"
            price = self.price(item["ticker"], item["market"]) or item["last_price"]
            if "sl_price" in changes and changes["sl_price"]:
                new_sl = float(changes["sl_price"])
                if (is_long and new_sl >= price) or (not is_long and new_sl <= price):
                    raise ManualError("Le SL doit rester du bon cote du prix actuel.")
                item["sl_price"] = new_sl
                if item["mode"] == "live" and item["market"] == "perp":
                    be.update_sl_on_hyperliquid(self.bot.exchange, self.bot.info, self._wallet(), item["ticker"],
                                                {"type": item["direction"], "entry": item["entry_price"], "size": item["notional_usd"]},
                                                new_sl, self.cfg)
            if "tp_price" in changes:
                tp = changes["tp_price"]
                if tp:
                    tp = float(tp)
                    if (is_long and tp <= price) or (not is_long and tp >= price):
                        raise ManualError("Le TP doit rester du bon cote du prix actuel.")
                item["tp_price"] = tp or None
            for k in ("ttp_arm_pct", "ttp_trail_pct"):
                if k in changes:
                    item[k] = float(changes[k]) if changes[k] else None
            if "lock_arm_pct" in changes or "lock_fraction" in changes:   # v4.343
                probe = {"lock_arm_pct": changes.get("lock_arm_pct", item.get("lock_arm_pct")),
                         "lock_fraction": changes.get("lock_fraction", item.get("lock_fraction"))}
                self._validate_lock_trend(probe)
                item["lock_arm_pct"], item["lock_fraction"] = probe["lock_arm_pct"], probe["lock_fraction"]
            if "trend_exit" in changes:
                if changes["trend_exit"] not in (None, "", "opposee", "perdue"):
                    raise ManualError("Regle de sortie sur la tendance invalide.")
                item["trend_exit"] = changes["trend_exit"] or None
            self._save(item)
            self._log(f"#{item['id']} {item['ticker']} modifie : SL {item['sl_price']:.6g}"
                      + (f" | TP {item['tp_price']:.6g}" if item.get("tp_price") else " | sans TP")
                      + (f" | TTP +{item['ttp_arm_pct']}%/-{item['ttp_trail_pct']}%" if item.get("ttp_arm_pct") else " | sans TTP"), "info")
            return self.public(item)

    def cancel(self, item_id):
        with self.lock:
            item = self.items.get(item_id)
            if not item or item["status"] != "scheduled":
                raise ManualError("Ordre programme introuvable (deja execute, annule ou expire ?).")
            self._finish_order(item, "cancelled", "annule par l utilisateur")
            return {"ok": True}

    def _finish_order(self, item, status, why):
        item["status"], item["reason"] = status, why
        item["closed_at"] = time.time()
        self._save(item)
        self.items.pop(item["id"], None)
        self._log(f"Ordre programme #{item['id']} {item['ticker']} : {why}", "warn" if status != "cancelled" else "dim")

    # ─────────────────────────────── temps reel ──────────────────────────
    def on_tick(self):
        """Appele a chaque tick de prix WebSocket (et par le cycle en secours)."""
        if not self.items:
            return
        for item in list(self.items.values()):
            try:
                if item["status"] == "open":
                    self._manage(item)
                elif item["status"] == "scheduled":
                    self._check_trigger(item)
            except Exception as e:
                print(f"[MANUEL] Erreur suivi #{item.get('id')} : {type(e).__name__}: {e}")

    def on_cycle(self):
        if not self.bot._is_ws_healthy():
            self.on_tick()
        now = time.time()
        for item in list(self.items.values()):
            if item["status"] == "scheduled" and now > item.get("expires_at", now + 1):
                self._finish_order(item, "expired", "expire sans declenchement")
            elif item["status"] == "open" and now - self._last_persist.get(item["id"], 0) > 30:
                self._save(item)  # persiste regulierement le pic du trailing

    def _check_trigger(self, item):
        if item["id"] in self._fire_busy:
            return
        price = self.price(item["ticker"], item["market"])
        if price is None:
            return
        ex = item["execution"]
        fire = ((ex == "price_above" and price >= item["trigger_price"])
                or (ex == "price_below" and price <= item["trigger_price"])
                or (ex == "time" and time.time() >= item["trigger_time"]))
        if not fire:
            return
        if (item.get("require_valid", True) and item.get("strategy_source") != "libre"
                and not self.get_opportunity(item["strategy_source"], item["ticker"], item["direction"])):
            self._finish_order(item, "cancelled", "declenche mais l opportunite n est plus valable — ordre annule")
            return
        self._log(f"Ordre programme #{item['id']} declenche ({self._trigger_text(item)}) — ouverture", "signal")
        self._fire_busy.add(item["id"])
        threading.Thread(target=self._fire, args=(item,), daemon=True).start()

    def _fire(self, item):
        """v4.343 — declenchement : controle de tendance H4 (si demande) puis ouverture. Execute dans un
        thread : la lecture des bougies peut prendre une seconde, ce qui ne doit pas bloquer les prix."""
        try:
            if item.get("trend_entry"):
                try:
                    self._check_trend_entry(item["ticker"], item["trend_entry"])
                except ManualError as e:
                    self._finish_order(item, "cancelled", f"declenche mais condition de tendance non remplie : {e}")
                    return
            self._open(item)
        finally:
            self._fire_busy.discard(item["id"])

    def _manage(self, item):
        if item["id"] in self._busy:
            return
        price = self.price(item["ticker"], item["market"])
        if price is None:
            return
        item["last_price"] = price
        sign = 1 if item["direction"] == "long" else -1
        move = sign * (price - item["entry_price"]) / item["entry_price"] * 100
        if move > item.get("peak_pct", 0):
            item["peak_pct"] = move
        reason = None
        # v4.343 — VERROU PROGRESSIF : des que le gain maximal atteint le seuil, le SL monte pour garantir
        # une part (lock_fraction) de ce gain maximal. Il ne fait que MONTER (long) / DESCENDRE (short).
        la, lf = item.get("lock_arm_pct"), item.get("lock_fraction")
        if la and lf and item["peak_pct"] >= la:
            lock_pct = lf * item["peak_pct"]
            new_sl = item["entry_price"] * (1 + sign * lock_pct / 100)
            if (sign > 0 and new_sl > item["sl_price"]) or (sign < 0 and new_sl < item["sl_price"]):
                item["sl_price"] = new_sl
                if not item.get("lock_active"):
                    item["lock_active"] = True
                    self._log(f"#{item['id']} {item['ticker']} : verrou du SL actif a +{item['peak_pct']:.2f}% — SL {new_sl:.6g} (garantit {lock_pct:.2f}%)", "signal")
                    self._save(item)
                self._maybe_push_sl(item, new_sl)
        if (sign > 0 and price <= item["sl_price"]) or (sign < 0 and price >= item["sl_price"]):
            reason = "STOP DYNAMIQUE (manuel)" if item.get("lock_active") else "STOP LOSS (manuel)"
        elif item.get("tp_price") and ((sign > 0 and price >= item["tp_price"]) or (sign < 0 and price <= item["tp_price"])):
            reason = "TAKE PROFIT (manuel)"
        elif item.get("ttp_arm_pct"):
            if not item.get("armed") and item["peak_pct"] >= item["ttp_arm_pct"]:
                item["armed"] = True
                self._log(f"#{item['id']} {item['ticker']} : trailing arme a +{item['peak_pct']:.2f}%", "signal")
                self._save(item)
            if item.get("armed") and move <= item["peak_pct"] - (item.get("ttp_trail_pct") or 0.5):
                reason = "TRAILING TAKE PROFIT (manuel)"
        if not reason and item.get("trend_exit"):
            reason = self._trend_exit_reason(item, price, sign)
        if reason:
            threading.Thread(target=self._close, args=(item, reason, price), daemon=True).start()

    def _trend_exit_reason(self, item, price, sign):
        """v4.343 — regle de sortie choisie sur la tendance H4 : "opposee" = la tendance devient l inverse
        de celle du trade ; "perdue" = elle n est plus celle du sens du trade. Lecture du cache (jamais
        de requete reseau ici, le bot rafraichit les bougies H4 des actifs tenus), verifiee au plus toutes
        les 5 s, et uniquement sur des donnees a jour."""
        now = time.time()
        if now - self._trend_last.get(item["id"], 0) < 5:
            return None
        self._trend_last[item["id"]] = now
        try:
            mv = self.bot.mtf_view(item["ticker"], price, cache_only=True)
        except Exception:
            return None
        if not mv or not mv.get("ok") or not mv.get("data_fresh", True):
            return None
        want = "haussiere" if sign > 0 else "baissiere"
        opposite = "baissiere" if sign > 0 else "haussiere"
        if item["trend_exit"] == "opposee" and mv["trend"] == opposite:
            return "TENDANCE RETOURNEE (manuel)"
        if item["trend_exit"] == "perdue" and mv["trend"] != want:
            return "TENDANCE PERDUE (manuel)"
        return None

    def _maybe_push_sl(self, item, new_sl):
        """Live perp : renvoie le SL a Hyperliquid (ordre natif) quand il a bouge d au moins
        MANUAL_LOCK_PUSH_MIN_STEP_PCT depuis le dernier envoi — dans un thread, sans bloquer les prix."""
        if item["mode"] != "live" or item["market"] != "perp" or self.bot.exchange is None:
            return
        pushed = item.get("lock_pushed")
        step = self.cfg.get("MANUAL_LOCK_PUSH_MIN_STEP_PCT", 0.05)
        if pushed is not None and abs(new_sl - pushed) / item["entry_price"] * 100 < step:
            return
        iid = item["id"]
        if iid in self._push_busy:
            return
        self._push_busy.add(iid)

        def run():
            try:
                be.update_sl_on_hyperliquid(self.bot.exchange, self.bot.info, self._wallet(), item["ticker"],
                                            {"type": item["direction"], "entry": item["entry_price"], "size": item["notional_usd"]},
                                            new_sl, self.cfg)
                item["lock_pushed"] = new_sl
            except Exception as e:
                print(f"[MANUEL] Mise a jour du SL natif #{iid} impossible : {e}")
            finally:
                self._push_busy.discard(iid)
        threading.Thread(target=run, daemon=True).start()

    # ─────────────────────────── reprise au demarrage ────────────────────
    def reconcile(self, exch_positions):
        """Positions manuelles LIVE perp absentes d Hyperliquid au demarrage :
        fermees pendant la coupure (SL natif le plus souvent)."""
        for item in list(self.items.values()):
            if item["status"] != "open" or item["mode"] != "live" or item["market"] != "perp":
                continue
            ep = exch_positions.get(item["ticker"])
            if ep and (ep["szi"] > 0) == (item["direction"] == "long"):
                continue
            exit_px = item.get("sl_price") or item["entry_price"]
            try:
                for f in self.bot.info.user_fills(self._wallet()) or []:
                    if f.get("coin") == item["ticker"] and str(f.get("dir", "")).startswith("Close"):
                        exit_px = float(f["px"])
                        break
            except Exception:
                pass
            item["mode_close_note"] = "fermee pendant la coupure"
            item_mode = item["mode"]
            item["mode"] = "paper"  # pas d ordre reel a envoyer : deja fermee
            self._close(item, "FERMEE PENDANT COUPURE (manuel)", exit_px)
            item["mode"] = item_mode

    def adopt_orphan(self, coin, ep, uid, opened_ts=None):
        """Position LIVE identifiee comme manuelle (cloid) mais inconnue de la
        base locale (base perdue) : reprise avec un SL de secours."""
        direction = "long" if ep["szi"] > 0 else "short"
        entry = ep["entry"]
        sl_pct = self.cfg.get("RECOVERY_RESCUE_SL_PCT", 15.0)
        item = {"strategy_source": "manual", "ticker": coin, "direction": direction, "market": "perp",
                "mode": "live", "leverage": ep.get("leverage") or 1, "notional_usd": abs(ep["szi"]) * entry,
                "sl_pct": sl_pct, "tp_pct": None, "ttp_arm_pct": None, "ttp_trail_pct": None,
                "execution": "now", "created_at": opened_ts or time.time(), "opened_at": opened_ts or time.time(),
                "trade_uid": uid, "entry_price": entry, "qty": abs(ep["szi"]),
                "sl_price": entry * (1 - sl_pct / 100) if direction == "long" else entry * (1 + sl_pct / 100),
                "tp_price": None, "peak_pct": 0.0, "armed": False, "reasons": ["reprise apres perte de la base locale"]}
        item["id"] = db.manual_insert("open", item)
        item["status"] = "open"
        self.items[item["id"]] = item
        self._log(f"#{item['id']} Position manuelle LIVE {direction.upper()} {coin} retrouvee sur Hyperliquid — suivie avec un SL de secours a {sl_pct}%. Verifiez-la.", "error")

    # ─────────────────────────────── affichage ───────────────────────────
    def public(self, item):
        out = {k: v for k, v in item.items()}
        out["strategy_label"] = STRATEGY_LABELS.get(item.get("strategy_source"), item.get("strategy_source"))
        if item.get("status") == "open":
            price = self.price(item["ticker"], item["market"]) or item.get("last_price")
            sign = 1 if item["direction"] == "long" else -1
            if price:
                out["current_price"] = price
                out["move_pct"] = sign * (price - item["entry_price"]) / item["entry_price"] * 100
                out["pnl"] = item["qty"] * (price - item["entry_price"]) * sign
        elif item.get("status") == "scheduled":
            out["current_price"] = self.price(item["ticker"], item["market"])
            out["opportunity_valid"] = (True if item.get("strategy_source") == "libre"
                                        else self.get_opportunity(item["strategy_source"], item["ticker"], item["direction"]) is not None)
        return out

    def snapshot(self):
        with self.lock:
            items = [self.public(i) for i in self.items.values()]
        history = []
        for row in db.manual_history(50):
            d = row["data"]
            d.update({"id": row["id"], "status": row["status"]})
            d["strategy_label"] = STRATEGY_LABELS.get(d.get("strategy_source"), d.get("strategy_source"))
            history.append(d)
        return {
            "opportunities": self.list_opportunities(),
            "scheduled": [i for i in items if i["status"] == "scheduled"],
            "positions": [i for i in items if i["status"] in ("open", "opening", "closing")],
            "history": history,
            "live_available": self.bot.exchange is not None and bool(self._wallet()),
            "opportunity_ttl_sec": self._ttl(),
        }
