"""
scalp_crypto.py — v4.421 — MODE SCALP CRYPTO (paper par defaut, passage en live par bouton).

Objectif : beaucoup de petits gains (TP fixe 0,6 % du prix, x3, stop 1 %) sur les 15 actifs les plus echanges
d Hyperliquid, avec des entrees RAPIDES (bougies 5 min) qui partent d un support / d une resistance H1 :
  - REBOND  : le prix touche une zone H1 et une bougie 5 min de retournement se confirme (avalement, marteau, etoile) ;
  - CASSURE : une bougie 5 min cloture au-dela d une zone H1 avec volume et corps forts, sans courir apres le mouvement
              (entree au plus a 0,4 % du niveau casse) -> on n attend pas des heures apres le debut du mouvement.
Chaque signal recoit une note 0-100 (tendance H1, tendance M15, volume, contacts de la zone, BTC, place restante avant la
zone suivante). NOTE NON CALIBREE sur des resultats : le paper sert justement a mesurer le vrai taux de reussite.

Les trades sont ouverts par le moteur de trading manuel (strategie "scalp_crypto") : memes stops natifs, memes historiques,
meme suivi des prix de sortie. Aucune logique n est partagee avec les autres modes automatiques.
"""
import threading
import time
from collections import deque
from datetime import datetime, timezone

import db
import bot_engine as be
import mtf_analysis as mtf

STRATEGY = "scalp_crypto"


# ───────────────────────────── fonctions pures (testables) ─────────────────────────────
def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    g = l = 0.0
    for a, b in zip(closes[-(period + 1):-1], closes[-period:]):
        d = b - a
        if d > 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    rs = (g / period) / (l / period)
    return 100 - 100 / (1 + rs)


def parse_hours(txt):
    """'7-11,13-21' -> [(7, 11), (13, 21)] (heures UTC, debut inclus, fin exclue). Vide = toujours actif."""
    out = []
    for part in str(txt or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            a, b = part.split("-")
            a, b = int(a), int(b)
            if 0 <= a <= 24 and 0 <= b <= 24 and a != b:
                out.append((a, b))
        except ValueError:
            continue
    return out


def hour_allowed(hours, utc_hour):
    if not hours:
        return True
    for a, b in hours:
        if (a < b and a <= utc_hour < b) or (a > b and (utc_hour >= a or utc_hour < b)):
            return True
    return False


def evaluate(c5, c15, c1h, btc_chg, K, flow=None):
    """Retourne le meilleur signal {side, kind, zone, score, reasons, level} ou (None, motif).
    c5 / c15 / c1h : bougies CLOTUREES (dicts t,o,h,l,c,v), les plus anciennes d abord.
    K : seuils (min_atr_pct, min_room_pct, vol_mult, max_chase_pct, min_score)."""
    if len(c5) < 40 or len(c15) < 30 or len(c1h) < 60:
        return None, "historique insuffisant"
    price = c5[-1]["c"]
    a1, a15, a5 = mtf.atr(c1h, 14), mtf.atr(c15, 14), mtf.atr(c5, 14)
    if not (a1 and a15 and a5 and price > 0):
        return None, "ATR indisponible"
    atr15_pct = a15 / price * 100
    if atr15_pct < K["min_atr_pct"]:
        return None, f"marche trop calme (ATR 15 min {atr15_pct:.2f} % < {K['min_atr_pct']} %)"
    zones = mtf.find_zones(c1h, pivot=2, lookback=120, merge_atr=0.6, max_width_atr=2.0)
    if not zones:
        return None, "aucune zone H1"
    tol = 0.3 * a15
    last, prev, prev2 = c5[-1], c5[-2], c5[-3]
    vols = [c["v"] for c in c5[-21:-1]]
    vavg = sum(vols) / len(vols) if vols else 0
    vr = (last["v"] / vavg) if vavg > 0 else 1.0
    rsi5 = _rsi([c["c"] for c in c5], 14)
    t1h = mtf.trend(c1h)[0]
    closes15 = [c["c"] for c in c15]
    e20, e50 = mtf.ema(closes15, 20), mtf.ema(closes15, 50) if len(closes15) >= 50 else None
    m15 = "neutre"
    if e20 and e50:
        m15 = "haussiere" if (closes15[-1] > e20 > e50) else ("baissiere" if (closes15[-1] < e20 < e50) else "neutre")
    rng = last["h"] - last["l"]
    body = abs(last["c"] - last["o"])
    cands = []

    def room(side, ref_zone):
        if side == "long":
            ups = [z["low"] for z in zones if z is not ref_zone and z["low"] > price * 1.0005]
            return ((min(ups) - price) / price * 100) if ups else 99.0
        dns = [z["high"] for z in zones if z is not ref_zone and z["high"] < price * 0.9995]
        return ((price - max(dns)) / price * 100) if dns else 99.0

    def score_of(side, kind, z, extra):
        s, why = 45, []
        aligned_h1 = (t1h == "haussiere") == (side == "long") and t1h in ("haussiere", "baissiere")
        opposed_h1 = t1h in ("haussiere", "baissiere") and not aligned_h1
        if aligned_h1:
            s += 12; why.append("tendance H1 dans le sens")
        elif opposed_h1:
            s -= 25 if kind == "cassure" else 15; why.append("tendance H1 contraire")
        if m15 in ("haussiere", "baissiere"):
            if (m15 == "haussiere") == (side == "long"):
                s += 8; why.append("tendance 15 min dans le sens")
            else:
                s -= 8; why.append("tendance 15 min contraire")
        need = K["vol_mult"] if kind == "cassure" else 1.2
        if vr >= need:
            s += 10; why.append(f"volume x{vr:.1f}")
            if vr >= 2.5:
                s += 5
        t = int(z.get("touches") or 1)
        if t >= 3:
            s += 10; why.append(f"zone testee {t} fois")
        elif t == 2:
            s += 6; why.append("zone testee 2 fois")
        if btc_chg is not None:
            if (side == "long" and btc_chg >= 0.2) or (side == "short" and btc_chg <= -0.2):
                s += 8; why.append(f"BTC dans le sens ({btc_chg:+.2f} % / 1 h)")
            elif (side == "long" and btc_chg <= -0.5) or (side == "short" and btc_chg >= 0.5):
                s -= 15; why.append(f"BTC contraire ({btc_chg:+.2f} % / 1 h)")
        rm = room(side, z)
        if rm >= 1.5:
            s += 5
        elif rm < 0.9:
            s -= 8; why.append("peu de place avant la zone suivante")     # v4.423 : motivation, plus un blocage
        if atr15_pct >= 0.15:
            s += 4
        else:
            s -= 4; why.append(f"marche calme (ATR 15 min {atr15_pct:.2f} %)")
        why.append(f"place avant la zone suivante : {'>3' if rm > 3 else f'{rm:.1f}'} %")
        if side == "short" and t1h == "haussiere":
            s -= 8; why.append("SHORT contre une tendance H1 haussiere")      # v4.428
        s += extra
        return max(0, min(100, s)), why, rm

    for z in zones:
        zh, zl = z["high"], z["low"]
        # ── REBOND sur support (zone sous ou autour du prix) ──
        if zl - tol <= price and price <= zh + 1.2 * a15 and min(c["l"] for c in c5[-3:]) <= zh + tol:
            nm, _ = mtf.candle_signal(c5, "long")
            if nm and last["c"] > last["o"] and last["c"] > zl and (rsi5 is None or rsi5 < 55):
                sc, why, rm = score_of("long", "rebond", z, 6)
                cands.append({"side": "long", "kind": "rebond", "zone": (zl, zh), "score": sc, "room": rm,
                              "touches": int(z.get("touches") or 1), "reasons": [f"rebond sur support {zl:.6g}-{zh:.6g} ({nm} 5 min)"] + why, "level": zh})
        # ── REBOND sur resistance (SHORT) ──
        if zh + tol >= price and price >= zl - 1.2 * a15 and max(c["h"] for c in c5[-3:]) >= zl - tol:
            nm, _ = mtf.candle_signal(c5, "short")
            if nm and last["c"] < last["o"] and last["c"] < zh and (rsi5 is None or rsi5 > 45):
                sc, why, rm = score_of("short", "rebond", z, 6)
                cands.append({"side": "short", "kind": "rebond", "zone": (zl, zh), "score": sc, "room": rm,
                              "touches": int(z.get("touches") or 1), "reasons": [f"rejet de la resistance {zl:.6g}-{zh:.6g} ({nm} 5 min)"] + why, "level": zl})
        # ── CASSURE haussiere confirmee ──
        strong_up = rng > 0 and body >= 0.5 * a5 and last["c"] > last["o"] and (last["c"] - last["l"]) / rng >= 0.7
        if (strong_up and last["c"] > zh + 0.1 * a15 and min(prev["c"], prev2["c"]) <= zh and vr >= K["vol_mult"]
                and (last["c"] - zh) / zh * 100 <= K["max_chase_pct"] and (rsi5 is None or rsi5 < 80)):
            sc, why, rm = score_of("long", "cassure", z, 8)
            cands.append({"side": "long", "kind": "cassure", "zone": (zl, zh), "score": sc, "room": rm,
                          "touches": int(z.get("touches") or 1), "reasons": [f"cassure confirmee de {zh:.6g} (5 min, corps fort)"] + why, "level": zh})
        # ── CASSURE baissiere confirmee ──
        strong_dn = rng > 0 and body >= 0.5 * a5 and last["c"] < last["o"] and (last["h"] - last["c"]) / rng >= 0.7
        if (strong_dn and last["c"] < zl - 0.1 * a15 and max(prev["c"], prev2["c"]) >= zl and vr >= K["vol_mult"]
                and (zl - last["c"]) / zl * 100 <= K["max_chase_pct"] and (rsi5 is None or rsi5 > 20)):
            sc, why, rm = score_of("short", "cassure", z, 8)
            cands.append({"side": "short", "kind": "cassure", "zone": (zl, zh), "score": sc, "room": rm,
                          "touches": int(z.get("touches") or 1), "reasons": [f"cassure confirmee de {zl:.6g} (5 min, corps fort)"] + why, "level": zl})
    if not cands:
        return None, "aucun signal"
    # v4.438 : SHORT interdit tant que BTC monte (variation 1 h > seuil). Appliquer avant le filtre de tendance.
    if K.get("short_btc_block", 1) and btc_chg is not None and btc_chg > K.get("short_btc_max", 0.1):
        n0 = len(cands)
        cands = [c for c in cands if c["side"] != "short"]
        if not cands:
            return None, f"short refuse : BTC en hausse ({btc_chg:+.2f} % / 1 h > {K.get('short_btc_max', 0.1):+.2f} %)" if n0 else "aucun signal"
    if K.get("trend_mode", 1):
        # v4.429 : sens du trade = sens de la tendance H1 (si H1 neutre : tendance 15 min)
        # v4.433 : une CONTRE-TENDANCE est autorisee (SCALP_COUNTER_ENABLED) si TOUTES les securites sont reunies :
        # rebond (pas de cassure) sur zone testee >= 2 fois, flux agressif dans le sens du trade, BTC pas contre, note >= seuil contre-tendance.
        def with_trend(c):
            want = "haussiere" if c["side"] == "long" else "baissiere"
            if t1h in ("haussiere", "baissiere"):
                return t1h == want
            return m15 in ("haussiere", "baissiere") and m15 == want
        aligned = []
        refus = None
        for c in cands:
            if with_trend(c):
                c["counter"] = False
                aligned.append(c)
                continue
            if not K.get("counter_enabled", 1):
                refus = refus or f"{c['kind']} {c['side']} refuse : contre-tendance (H1 {t1h}, 15 min {m15}) ; contre-tendance sécurisée désactivée"
                continue
            why_no = None
            if c["kind"] != "rebond":
                why_no = "cassure à contre-tendance non admise"
            elif c["touches"] < 2:
                why_no = f"zone testée {c['touches']} fois (< 2)"
            elif flow is None or flow.get("n", 0) < K.get("counter_min_trades", 30):
                why_no = f"flux insuffisant ({0 if not flow else flow.get('n', 0)} transactions)"
            elif (c["side"] == "long" and flow["buy_ratio"] < K.get("counter_flow", 0.58)) or (c["side"] == "short" and flow["buy_ratio"] > 1 - K.get("counter_flow", 0.58)):
                why_no = f"flux pas dans le sens ({flow['buy_ratio'] * 100:.0f} % d'achats agressifs)"
            elif btc_chg is not None and ((c["side"] == "long" and btc_chg <= -0.2) or (c["side"] == "short" and btc_chg >= 0.2)):
                why_no = f"BTC contre ({btc_chg:+.2f} % / 1 h)"
            elif c["score"] < K.get("counter_min_score", 65):
                why_no = f"note {c['score']:.0f} < {K.get('counter_min_score', 65):.0f} (exigence contre-tendance)"
            if why_no:
                refus = refus or f"{c['kind']} {c['side']} refuse : contre-tendance (H1 {t1h}, 15 min {m15}) — {why_no}"
                continue
            c["counter"] = True
            c["reasons"] = [f"CONTRE-TENDANCE sécurisée : flux {flow['buy_ratio'] * 100:.0f} % acheteurs agressifs ({flow['n']} transactions), zone testée {c['touches']} fois"] + c["reasons"]
            aligned.append(c)
        if not aligned:
            return None, refus or "contre-tendance refusée"
        cands = aligned
    # v4.450 : FLUX POUR TOUS LES TRADES (option) : le flux agressif des 10 dernieres minutes doit etre dans le sens du trade.
    # Flux indisponible / trop peu de transactions : on ne bloque pas (donnee manquante != flux contraire).
    if K.get("flow_all_on", 0) and flow is not None and flow.get("n", 0) >= K.get("flow_all_trades", 30):
        need = K.get("flow_all_min", 0.52)
        kept, refus_f = [], None
        for c in cands:
            bad = (c["side"] == "long" and flow["buy_ratio"] < need) or (c["side"] == "short" and flow["buy_ratio"] > 1 - need)
            if bad and not c.get("counter"):
                refus_f = refus_f or f"{c['kind']} {c['side']} refuse : flux pas dans le sens ({flow['buy_ratio'] * 100:.0f} % d'achats agressifs, {flow['n']} transactions)"
            else:
                kept.append(c)
        if not kept:
            return None, refus_f
        cands = kept
    good = [c for c in cands if c["room"] >= K["min_room_pct"]]
    if not good:
        best = max(cands, key=lambda c: c["score"])
        return None, f"{best['kind']} {best['side']} refuse : place insuffisante avant la zone suivante ({best['room']:.1f} % < {K['min_room_pct']} %)"
    # v4.428 : exigence propre aux SHORT (seuil de note distinct) ; les LONG gardent le seuil general
    ok = [c for c in good if c["score"] >= (K.get("min_score_short", 0) if c["side"] == "short" else K["min_score"])]
    if not ok:
        best = max(good, key=lambda c: c["score"])
        need = K.get("min_score_short", 0) if best["side"] == "short" else K["min_score"]
        return None, f"{best['kind']} {best['side']} refuse : note {best['score']} < {need}" + (" (exigence SHORT)" if best["side"] == "short" else "")
    best = max(ok, key=lambda c: c["score"])
    best["price"] = price
    best["atr15_pct"] = atr15_pct
    best["volume_ratio"] = vr
    best["sig_high"], best["sig_low"], best["atr5"] = last["h"], last["l"], a5
    # v4.450 : contexte a l entree (observation, pour calibrer les filtres) : flux, tendances, BTC
    best["ctx"] = {"flow_buy": (round(flow["buy_ratio"], 3) if flow else None), "flow_n": (flow.get("n") if flow else None),
                   "t1h": t1h, "m15": m15, "btc1h": (round(btc_chg, 3) if btc_chg is not None else None)}
    return best, "ok"


def p_fmt(x):
    return f"{x:.6g}"


def compute_regime(closes_by_t, win_bars, high, flat_pct=0.05, lag_bars=6):
    """v4.442 — REGIME DU PANIER (pur). closes_by_t : ticker -> clotures 5 min (anciennes d abord).
    corr = correlation moyenne des rendements 5 min entre actifs (fenetre win_bars) ; ch = variation sur lag_bars bougies (30 min) en % ;
    up / down = part des actifs qui montent / baissent de plus de flat_pct ; basket = variation moyenne. label : « correle » si corr >= high."""
    rets, ch = {}, {}
    for t, cl in closes_by_t.items():
        if len(cl) < max(win_bars, lag_bars) + 2 or min(cl[-(win_bars + 1):]) <= 0:
            continue
        w = cl[-(win_bars + 1):]
        rets[t] = [w[i] / w[i - 1] - 1 for i in range(1, len(w))]
        ch[t] = (cl[-1] / cl[-1 - lag_bars] - 1) * 100
    n = len(ch)
    if n < 3:
        return {"n": n, "label": "inconnu", "corr": None, "up": None, "down": None, "basket30": None, "ch30": {}}
    names = list(rets)
    mats = {}
    for t in names:
        r = rets[t]
        m = sum(r) / len(r)
        d = [x - m for x in r]
        mats[t] = (d, sum(x * x for x in d) ** 0.5)
    cs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            da, sa = mats[names[i]]
            db_, sb = mats[names[j]]
            if sa > 0 and sb > 0:
                cs.append(sum(x * y for x, y in zip(da, db_)) / (sa * sb))
    corr = sum(cs) / len(cs) if cs else None
    up = sum(1 for v in ch.values() if v > flat_pct) / n
    down = sum(1 for v in ch.values() if v < -flat_pct) / n
    basket = sum(ch.values()) / n
    return {"n": n, "corr": None if corr is None else round(corr, 3), "up": round(up, 3), "down": round(down, 3), "basket30": round(basket, 3),
            "label": "correle" if (corr is not None and corr >= high) else "disperse", "ch30": {t: round(v, 3) for t, v in ch.items()}}


# ───────────────────────────── moteur ─────────────────────────────
class ScalpEngine:
    def __init__(self, bot, manual):
        self.bot, self.manual = bot, manual
        self.events = deque(maxlen=80)       # journal en memoire (signaux pris / refuses)
        self._uni, self._uni_ts = [], 0
        self._seen = {}                      # ticker -> derniere tranche 5 min evaluee
        self._cool = {}                      # ticker -> fin de pause
        self._zone_cache = (0, {})           # v4.432 : zones ou un stop vient de tomber
        self.regime = {"ts": 0, "n": 0, "label": "inconnu"}     # v4.442 : regime du panier (correlation / largeur)
        self._opens = deque(maxlen=200)      # (ts, side, mode, ticker) des ouvertures recentes (grappe)
        self._sigs = deque(maxlen=300)       # (ts, ticker, side) signaux vus (confirmation par les pairs)
        self.peer_wait = {}                  # ticker -> signal en attente d un pair
        self._busy = False
        self.pending = {}                    # v4.429 : signaux de rebond en attente de confirmation (ticker -> dict)
        self._last_tick = 0
        self._stats_cache = (0, None, None)
        self.paused_until = 0
        self.last_error = None

    @property
    def cfg(self):
        return self.bot.cfg

    def _ev(self, kind, ticker, text):
        self.events.appendleft({"ts": time.time(), "kind": kind, "ticker": ticker, "text": text})

    def thresholds(self):
        c = self.cfg
        return {"min_atr_pct": float(c.get("SCALP_MIN_ATR15_PCT", 0.0)), "min_room_pct": float(c.get("SCALP_MIN_ROOM_PCT", 0.0)),
                "vol_mult": float(c.get("SCALP_BREAK_VOL_MULT", 1.5)), "max_chase_pct": float(c.get("SCALP_MAX_CHASE_PCT", 0.4)),
                "min_score": float(c.get("SCALP_MIN_SCORE", 0)),
                "min_score_short": float(c.get("SCALP_SHORT_MIN_SCORE", 62)),
                "trend_mode": int(c.get("SCALP_TREND_MODE", 1)),
                "short_btc_block": int(c.get("SCALP_SHORT_BTC_BLOCK", 1)), "short_btc_max": float(c.get("SCALP_SHORT_BTC_MAX_PCT", 0.1)),
                "counter_enabled": int(c.get("SCALP_COUNTER_ENABLED", 1)), "counter_min_score": float(c.get("SCALP_COUNTER_MIN_SCORE", 65)),
                "counter_flow": float(c.get("SCALP_COUNTER_FLOW_MIN", 0.58)), "counter_min_trades": int(c.get("SCALP_COUNTER_MIN_TRADES", 30)),
                "flow_all_on": int(c.get("SCALP_FLOW_ALL_ON", 0)), "flow_all_min": float(c.get("SCALP_FLOW_ALL_MIN", 0.52)),
                "flow_all_trades": int(c.get("SCALP_FLOW_ALL_MIN_TRADES", 30))}

    # ── univers : top N par volume 24 h parmi les actifs suivis ──
    def universe(self):
        now = time.time()
        if self._uni and now - self._uni_ts < 1800:
            return self._uni
        n = max(int(self.cfg.get("SCALP_TOP_N", 15)), 1)
        # v4.424 : cfg["SYMBOLS"] contient des cles de slot ("BTC_0", "PAXG_1"...) : on en extrait le vrai ticker
        tracked, seen = [], set()
        for raw in self.cfg.get("SYMBOLS", []):
            t = be.ticker_from_slot_key(str(raw))
            if t in seen or t.startswith("xyz:") or t == "PAXG":
                continue
            seen.add(t); tracked.append(t)
        try:
            meta, ctxs = self.bot.info.meta_and_asset_ctxs()
            vol = {}
            for a, cx in zip(meta.get("universe", []), ctxs):
                try:
                    vol[a.get("name")] = float(cx.get("dayNtlVlm") or 0)
                except (TypeError, ValueError):
                    pass
            ranked = sorted([t for t in tracked if t in vol], key=lambda t: -vol[t])
            if ranked:
                self._uni, self._uni_ts = ranked[:n], now
                return self._uni
        except Exception as e:
            self.last_error = f"univers : {type(e).__name__}: {e}"
        self._uni, self._uni_ts = tracked[:n], now - 1500      # repli : reessai dans 5 min
        return self._uni

    # ── statistiques (a partir de l historique des trades manuels) ──
    def stats(self, mode=None):
        mode = mode or self.cfg.get("SCALP_MODE", "paper")
        ts, m, cached = self._stats_cache
        if cached is not None and m == mode and time.time() - ts < 15:
            return cached
        rows = []
        try:
            for r in db.manual_history(800):
                d = r["data"]
                # le statut est une colonne de la base, absent du JSON des donnees (bug v4.421-v4.429 : filtre sur d["status"] => toujours 0 trade)
                if d.get("strategy_source") == STRATEGY and r.get("status") == "closed" and d.get("mode") == mode:
                    rows.append(d)
        except Exception as e:
            self.last_error = f"stats : {type(e).__name__}: {e}"
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = [d for d in rows if float(d.get("closed_at") or 0) >= midnight]
        rows.sort(key=lambda d: float(d.get("closed_at") or 0), reverse=True)

        def agg(lst):
            n = len(lst)
            w = sum(1 for d in lst if float(d.get("pnl") or 0) > 0)
            pnl = sum(float(d.get("pnl") or 0) for d in lst)
            fees = sum(((float(d["real_costs"]["fees"]) - float(d["real_costs"]["funding"])) if d.get("real_costs")
                        else (float(d.get("fees_est") or 0) or float(d.get("notional_usd") or 0) * 0.0009)) for d in lst)
            return {"n": n, "wins": w, "win_rate": (w / n * 100) if n else None, "pnl": pnl, "fees": fees, "pnl_net": pnl - fees}
        streak = 0
        for d in rows:
            if float(d.get("pnl") or 0) <= 0:
                streak += 1
            else:
                break
        by_kind = {}
        for k in ("rebond", "cassure"):
            by_kind[k] = agg([d for d in rows if (d.get("scalp") or {}).get("kind") == k])
        by_trend = {"avec": agg([d for d in rows if not (d.get("scalp") or {}).get("counter")]),
                    "contre": agg([d for d in rows if (d.get("scalp") or {}).get("counter")])}
        def _rg(d):
            return ((d.get("scalp") or {}).get("regime") or {})
        by_regime = {"correle": agg([d for d in rows if _rg(d).get("label") == "correle"]),
                     "disperse": agg([d for d in rows if _rg(d).get("label") == "disperse"]),
                     "inconnu": agg([d for d in rows if _rg(d).get("label") not in ("correle", "disperse")])}
        by_cluster = {k: agg([d for d in rows if _rg(d).get("cluster") is not None and (min(int(_rg(d)["cluster"]), 3) == k)]) for k in (0, 1, 2, 3)}
        by_hour = {}
        for d in rows:
            h = datetime.fromtimestamp(float(d.get("opened_at") or d.get("closed_at") or 0), timezone.utc).hour
            by_hour.setdefault(h, []).append(d)
        out = {"mode": mode, "total": agg(rows), "today": agg(today), "streak_losses": streak, "by_kind": by_kind, "by_trend": by_trend, "by_regime": by_regime, "by_cluster": {str(k): v for k, v in by_cluster.items()},
               "by_hour": {h: agg(v) for h, v in sorted(by_hour.items())}, "last_closed": rows[0].get("closed_at") if rows else None}
        self._stats_cache = (time.time(), mode, out)
        return out

    # ── cycle ──
    def on_cycle(self):
        """Appele par le moteur manuel a chaque cycle ; le travail lourd est fait dans un thread, au plus toutes les 15 s."""
        now = time.time()
        try:
            self._manage_open(now)
        except Exception as e:
            self.last_error = f"suivi : {type(e).__name__}: {e}"
        if not int(self.cfg.get("SCALP_ENABLED", 0)) or self._busy or now - self._last_tick < 15:
            return
        self._last_tick = now
        self._busy = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _manage_open(self, now):
        mx = float(self.cfg.get("SCALP_MAX_HOLD_MIN", 90)) * 60
        if mx <= 0 and not int(self.cfg.get("SCALP_CORR_BASKET_EXIT_ON", 0)):
            return
        for it in list(self.manual.items.values()):
            if it.get("strategy_source") == STRATEGY and it.get("status") == "open":
                if int(self.cfg.get("SCALP_CORR_BASKET_EXIT_ON", 0)) and not it.get("tp_run_armed"):
                    try:
                        reg = self.regime
                        px, en = float(it.get("last_price") or 0), float(it.get("entry_price") or 0)
                        if reg.get("label") == "correle" and now - reg.get("ts", 0) < 300 and px > 0 and en > 0 and now - float(it.get("opened_at") or now) > 120:
                            sg = 1 if it["direction"] == "long" else -1
                            against = reg["down"] if sg > 0 else reg["up"]
                            if sg * (px - en) / en < 0 and against >= float(self.cfg.get("SCALP_BASKET_EXIT_PCT", 70)) / 100:
                                self.manual.close(it["id"], "SCALP SORTIE PANIER")
                                self._ev("sortie", it["ticker"], f"#{it['id']} fermé : le panier se retourne ({against * 100:.0f} % des actifs contre, trade en perte)")
                                continue
                    except Exception as e:
                        self.last_error = f"sortie panier : {e}"
                if it.get("tp_run_armed"):      # v4.434 : objectif atteint, le trade court avec son stop suiveur : la duree max ne s applique plus
                    continue
                if mx > 0 and now - float(it.get("opened_at") or now) > mx:
                    try:
                        self.manual.close(it["id"], "SCALP DUREE MAX")
                        self._ev("sortie", it["ticker"], f"#{it['id']} ferme : duree maximale {mx / 60:.0f} min atteinte")
                    except Exception as e:
                        self.last_error = f"fermeture duree : {e}"

    def _open_count(self):
        return sum(1 for it in self.manual.items.values()
                   if it.get("strategy_source") == STRATEGY and it.get("status") in ("scheduled", "opening", "open", "closing"))

    def _btc_change(self):
        try:
            c = self.bot._mtf_candles("BTC", "15m", 30)
            if len(c) >= 5:
                return (c[-1]["c"] / c[-5]["c"] - 1) * 100
        except Exception:
            pass
        return None

    def window_state(self):
        """(ouvert ?, motif) : tranche horaire UTC + arrets de securite."""
        c = self.cfg
        now = time.time()
        hrs = parse_hours(c.get("SCALP_HOURS_UTC", ""))
        h = datetime.now(timezone.utc).hour
        if not hour_allowed(hrs, h):
            return False, f"hors tranche horaire ({c.get('SCALP_HOURS_UTC')} UTC)"
        if now < self.paused_until:
            return False, f"pause apres pertes jusqu a {datetime.fromtimestamp(self.paused_until, timezone.utc):%H:%M} UTC"
        st = self.stats()
        lim = float(c.get("SCALP_DAILY_LOSS_USD", 3.0))
        if lim > 0 and st["today"]["pnl"] <= -lim:
            return False, f"perte journaliere maximale atteinte ({st['today']['pnl']:+.2f} $)"
        mx = int(c.get("SCALP_MAX_PER_DAY", 40))
        if mx > 0 and st["today"]["n"] >= mx:
            return False, f"{st['today']['n']} trades aujourd hui (maximum {mx})"
        ls = int(c.get("SCALP_PAUSE_AFTER_LOSSES", 3))
        if ls > 0 and st["streak_losses"] >= ls and st["last_closed"] and now - float(st["last_closed"]) < float(c.get("SCALP_PAUSE_MIN", 60)) * 60:
            self.paused_until = float(st["last_closed"]) + float(c.get("SCALP_PAUSE_MIN", 60)) * 60
            self._ev("pause", "-", f"{st['streak_losses']} pertes de suite : pause de {c.get('SCALP_PAUSE_MIN', 60)} min")
            return False, "pause apres pertes consecutives"
        return True, "ouvert"

    def _scan(self):
        try:
            ok, why = self.window_state()
            if not ok:
                return
            K = self.thresholds()
            self._regime()          # v4.442 : regime du panier (correlation / largeur), recalcule au plus toutes les 60 s
            self._check_pending(K)
            btc = self._btc_change()
            maxo = int(self.cfg.get("SCALP_MAX_OPEN", 0))       # v4.422 : 0 = pas de plafond (en live, c est le capital qui limite)
            mode = "live" if self.cfg.get("SCALP_MODE") == "live" else "paper"
            for t in self.universe():
                if maxo > 0 and self._open_count() >= maxo:
                    break
                bucket = int(time.time() // 300)
                if self._seen.get(t) == bucket:
                    continue
                self._seen[t] = bucket
                if time.time() < self._cool.get((t, mode), 0) or self._busy_same_mode(t, mode) or t in self.pending:
                    continue
                try:
                    c5 = self.bot._mtf_candles(t, "5m", 60)
                    c15 = self.bot._mtf_candles(t, "15m", 60)
                    c1h = self.bot._mtf_candles(t, "1h", 230)
                except Exception as e:
                    self.last_error = f"bougies {t} : {type(e).__name__}: {e}"
                    continue
                sig, motif = evaluate(c5, c15, c1h, btc, K, self._flow(t))
                if not sig:
                    if "refuse" in motif:
                        self._ev("refus", t, motif)
                    continue
                if self._zone_blocked(t, mode, sig.get("level")):
                    self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : un stop vient de tomber sur cette zone ({sig['level']:.6g}), pause de zone")
                    continue
                if not self._gate(t, sig, mode):
                    continue
                if not self._peers(t, sig, mode):
                    continue
                self._dispatch(t, sig)
                self._release_peers(sig["side"], mode)
        except Exception as e:
            self.last_error = f"scan : {type(e).__name__}: {e}"
            print(f"[SCALP] {self.last_error}")
        finally:
            self._busy = False

    # ── v4.442 : correlation, largeur du marche, grappe ──
    def _regime(self):
        c, now = self.cfg, time.time()
        if now - self.regime.get("ts", 0) < 60 and self.regime.get("n"):
            return self.regime
        closes = {}
        for t in self.universe():
            try:
                cs = self.bot._mtf_candles(t, "5m", 60)
                if cs:
                    closes[t] = [float(x["c"]) for x in cs]
            except Exception:
                continue
        win = max(int(float(c.get("SCALP_CORR_WINDOW_MIN", 120)) // 5), 6)
        reg = compute_regime(closes, min(win, 55), float(c.get("SCALP_CORR_HIGH", 0.6)))
        reg["ts"] = now
        self.regime = reg
        return reg

    def _cluster_count(self, side, mode):
        w = float(self.cfg.get("SCALP_CLUSTER_WINDOW_MIN", 30)) * 60
        now = time.time()
        return sum(1 for ts, sd, md, _t in self._opens if sd == side and md == mode and now - ts <= w)

    def _gate(self, t, sig, mode):
        """Regles du regime correle (chacune a son interrupteur, desactivees par defaut). True = on peut continuer."""
        c, side = self.cfg, sig["side"]
        reg = self._regime()
        if not reg.get("n") or reg.get("label") == "inconnu":
            return True
        corr_on = reg["label"] == "correle"
        why = None
        if corr_on and int(c.get("SCALP_CORR_DIR_ON", 0)):
            need = float(c.get("SCALP_BREADTH_MIN", 60)) / 100
            share = reg["up"] if side == "long" else reg["down"]
            if share < need:
                why = f"marché corrélé (corr {reg['corr']:.2f}) : seulement {share * 100:.0f} % des actifs vont dans le sens {side.upper()} (< {need * 100:.0f} %)"
        if why is None and corr_on and int(c.get("SCALP_CORR_RELSTR_ON", 0)):
            rs = (reg["ch30"].get(t, 0.0)) - reg["basket30"]
            if (side == "long" and rs < 0) or (side == "short" and rs > 0):
                why = f"marché corrélé : {t} est {'plus faible' if side == 'long' else 'plus fort'} que le panier ({rs:+.2f} % / 30 min), on ne prend que le meilleur du groupe"
        cm = int(c.get("SCALP_CLUSTER_MAX", 0))
        if why is None and corr_on and cm > 0 and self._cluster_count(side, mode) >= cm:
            why = f"marché corrélé : grappe {side.upper()} pleine ({self._cluster_count(side, mode)}/{cm} ouvertures en {c.get('SCALP_CLUSTER_WINDOW_MIN', 30)} min)"
        if why:
            self._ev("refus", t, f"{sig['kind']} {side} refusé : {why}")
            return False
        return True

    def _peers(self, t, sig, mode):
        """Confirmation par les pairs : un signal n est pris que si au moins SCALP_PEER_MIN AUTRES actifs ont donne un signal dans le meme sens
        (ou ont ete ouverts) dans la fenetre. Sinon il attend. Retourne True si on peut continuer maintenant."""
        pm = int(self.cfg.get("SCALP_PEER_MIN", 0))
        now = time.time()
        self._sigs.append((now, t, sig["side"]))
        if pm <= 0:
            return True
        if int(self.cfg.get("SCALP_PEER_CORR_ONLY", 1)) and self._regime().get("label") != "correle":
            return True      # v4.446 : la confirmation par les pairs ne s applique qu en marche correle (sauf reglage contraire)
        w = float(self.cfg.get("SCALP_PEER_WINDOW_MIN", 30)) * 60
        peers = {tt for ts, tt, sd in self._sigs if sd == sig["side"] and tt != t and now - ts <= w}
        peers |= {tt for ts, sd, md, tt in self._opens if sd == sig["side"] and md == mode and tt != t and now - ts <= w}
        if len(peers) >= pm:
            sig["peers"] = len(peers)
            self.peer_wait.pop(t, None)
            return True
        self.peer_wait[t] = {"sig": sig, "ts": now, "side": sig["side"]}
        self._ev("attente", t, f"{sig['side'].upper()} {sig['kind']} note {sig['score']:.0f} en attente d'un autre actif dans le même sens ({len(peers)}/{pm}, jusqu'à {w / 60:.0f} min)")
        return False

    def _release_peers(self, side, mode):
        pm = int(self.cfg.get("SCALP_PEER_MIN", 0))
        if pm <= 0 or not self.peer_wait:
            return
        now = time.time()
        w = float(self.cfg.get("SCALP_PEER_WINDOW_MIN", 30)) * 60
        for t2, wt in list(self.peer_wait.items()):
            if now - wt["ts"] > w:
                self.peer_wait.pop(t2, None)
                continue
            if wt["side"] != side or t2 in self.pending or self._busy_same_mode(t2, mode):
                continue
            peers = {tt for ts, tt, sd in self._sigs if sd == side and tt != t2 and now - ts <= w}
            peers |= {tt for ts, sd, md, tt in self._opens if sd == side and md == mode and tt != t2 and now - ts <= w}
            if len(peers) >= pm and self._gate(t2, wt["sig"], mode):
                wt["sig"]["peers"] = len(peers)
                self.peer_wait.pop(t2, None)
                self._dispatch(t2, wt["sig"])

    def _dispatch(self, t, sig):
        nb = int(self.cfg.get("SCALP_CONFIRM_BARS", 3))
        if nb > 0 and sig["kind"] == "rebond":
            self._arm(t, sig, nb)
        elif int(self.cfg.get("SCALP_RETEST_ON", 0)) and sig["kind"] == "cassure":
            self._arm_retest(t, sig)
        else:
            self._take(t, sig)

    def _flow(self, t, minutes=10):
        """v4.433 : part des achats agressifs sur les N dernieres minutes (flux WebSocket du bot). S abonne a la volee aux actifs hors liste du bot.
        Retourne {buy_ratio, n} ou None si aucun flux."""
        bot = self.bot
        try:
            sub = getattr(self, "_flow_sub", None)
            if sub is None or sub[0] != getattr(bot, "_ws_trades_subscribed_at", None):
                sub = self._flow_sub = (getattr(bot, "_ws_trades_subscribed_at", None), set())
            buffers = getattr(bot, "_ws_trades_buffer", None)
            if buffers is None:
                return None
            if t not in sub[1] and t not in buffers and getattr(bot, "info", None) is not None:
                sub[1].add(t)
                try:
                    bot.info.subscribe({"type": "trades", "coin": t}, bot._on_ws_trades)
                except Exception as e:
                    self.last_error = f"flux {t} : {type(e).__name__}: {e}"
                return None
            buf = list(buffers.get(t) or [])
            lim = (time.time() - minutes * 60) * 1000
            b = a = 0.0
            n = 0
            for x in buf:
                if x.get("t", 0) < lim:
                    continue
                sz = float(x.get("sz") or 0)
                if x.get("side") == "B":
                    b += sz
                elif x.get("side") == "A":
                    a += sz
                else:
                    continue
                n += 1
            if n == 0 or b + a <= 0:
                return None
            return {"buy_ratio": b / (b + a), "n": n}
        except Exception as e:
            self.last_error = f"flux : {type(e).__name__}: {e}"
            return None

    def _zone_blocked(self, t, mode, level):
        """v4.432 : apres un stop sur une zone (support/resistance), on n y re-entre pas pendant SCALP_ZONE_COOLDOWN_MIN (0 = off).
        Motivation, pas interdiction du comportement passe : seulement la MEME zone (+-0,3 %), pas le ticker entier."""
        mins = float(self.cfg.get("SCALP_ZONE_COOLDOWN_MIN", 60))
        if mins <= 0 or not level:
            return False
        ts, zones = self._zone_cache
        now = time.time()
        if now - ts > 60:
            zones = {}
            try:
                for r in db.manual_history(300):
                    d = r["data"]
                    if d.get("strategy_source") != STRATEGY or r.get("status") != "closed":
                        continue
                    if float(d.get("pnl") or 0) >= 0 or "DUREE" in str(d.get("reason") or "").upper():
                        continue
                    lv = (d.get("scalp") or {}).get("level")
                    ca = float(d.get("closed_at") or 0)
                    if lv and now - ca < 86400:
                        zones.setdefault((d.get("ticker"), d.get("mode")), []).append((float(lv), ca))
            except Exception as e:
                self.last_error = f"zones : {type(e).__name__}: {e}"
            self._zone_cache = (now, zones)
        for lv, ca in zones.get((t, mode), []):
            if now - ca < mins * 60 and abs(level - lv) / lv <= 0.003:
                return True
        return False

    def _arm(self, t, sig, nb):
        """v4.429 : un rebond n ouvre PAS tout de suite. On attend que le prix confirme le sens choisi : un SHORT entre quand le prix passe sous le
        plus bas de la bougie de rejet, un LONG quand il passe au-dessus du plus haut de la bougie de rebond. Tant que le prix va a contre-sens, c est du
        bruit s il reste sous la limite (extreme de la bougie signal + marge de bruit) ; au-dela, ou sans confirmation en N bougies, le signal est abandonne."""
        noise = float(self.cfg.get("SCALP_NOISE_ATR", 0.5)) * sig["atr5"]
        if sig["side"] == "short":
            trig, inval = sig["sig_low"], sig["sig_high"] + noise
        else:
            trig, inval = sig["sig_high"], sig["sig_low"] - noise
        self.pending[t] = {"sig": sig, "trigger": trig, "invalid": inval, "armed": time.time(), "expires": time.time() + nb * 300}
        self._ev("attente", t, f"{sig['side'].upper()} {sig['kind']} note {sig['score']:.0f} : entrée si le prix {'passe sous' if sig['side'] == 'short' else 'repasse au-dessus de'} {trig:.6g} "
                               f"(abandon au-delà de {inval:.6g}, ou sans confirmation en {nb} bougies)")

    def _arm_retest(self, t, sig):
        """v4.450 : CASSURE AVEC RETEST. On n entre pas sur la cassure : on attend que le prix revienne pres du niveau casse (zone de retest), puis
        qu'il reparte dans le sens de la cassure (rebond d'au moins SCALP_RETEST_BOUNCE_ATR x ATR5 depuis le point extreme du retest).
        Abandon : le prix repasse de l'autre cote du niveau (fausse cassure, marge de bruit), ou pas de retest dans SCALP_RETEST_BARS bougies."""
        c = self.cfg
        a5 = sig["atr5"]
        nb = int(c.get("SCALP_RETEST_BARS", 6))
        lvl = float(sig["level"])
        noise = float(c.get("SCALP_NOISE_ATR", 0.5)) * a5
        tol = float(c.get("SCALP_RETEST_TOL_ATR", 0.3)) * a5
        bounce = float(c.get("SCALP_RETEST_BOUNCE_ATR", 0.25)) * a5
        long_ = sig["side"] == "long"
        self.pending[t] = {"sig": sig, "retest": True, "level": lvl, "tol": tol, "bounce": bounce, "touched": False, "extreme": None,
                           "trigger": lvl, "invalid": (lvl - noise) if long_ else (lvl + noise),
                           "armed": time.time(), "expires": time.time() + nb * 300}
        self._ev("attente", t, f"{sig['side'].upper()} cassure note {sig['score']:.0f} : attente d'un RETEST de {lvl:.6g} puis d'un rebond de {bounce:.6g} "
                               f"(abandon si le prix repasse {'sous' if long_ else 'au-dessus de'} {p_fmt(self.pending[t]['invalid'])}, ou sans retest en {nb} bougies)")

    def _check_retest(self, t, p, px, now):
        """Retourne True si l'entree doit avoir lieu maintenant."""
        sig = p["sig"]
        long_ = sig["side"] == "long"
        if now > p["expires"]:
            del self.pending[t]
            self._ev("abandon", t, f"{sig['side'].upper()} cassure : pas de retest dans le délai" if not p["touched"] else f"{sig['side'].upper()} cassure : retest sans rebond dans le délai")
            return False
        if (long_ and px < p["invalid"]) or ((not long_) and px > p["invalid"]):
            del self.pending[t]
            self._ev("abandon", t, f"{sig['side'].upper()} cassure : le prix repasse de l'autre côté du niveau ({px:.6g}) — fausse cassure")
            return False
        near = (px <= p["level"] + p["tol"]) if long_ else (px >= p["level"] - p["tol"])
        if near:
            if not p["touched"]:
                p["touched"] = True
                self._ev("attente", t, f"{sig['side'].upper()} cassure : retest du niveau {p['level']:.6g} en cours ({px:.6g})")
            p["extreme"] = px if p["extreme"] is None else (min(p["extreme"], px) if long_ else max(p["extreme"], px))
        if p["touched"] and p["extreme"] is not None:
            if long_ and px >= p["extreme"] + p["bounce"] and px > p["level"]:
                return True
            if (not long_) and px <= p["extreme"] - p["bounce"] and px < p["level"]:
                return True
        return False

    def _check_pending(self, K):
        now = time.time()
        mode = "live" if self.cfg.get("SCALP_MODE") == "live" else "paper"
        for t, p in list(self.pending.items()):
            sig = p["sig"]
            px = self.manual.price(t, "perp")
            if not px:
                continue
            short = sig["side"] == "short"
            if p.get("retest"):      # v4.450 : cassure avec retest
                if self._check_retest(t, p, px, now):
                    del self.pending[t]
                    if self._busy_same_mode(t, mode):
                        continue
                    sig = dict(sig); sig["price"] = px
                    sig["reasons"] = list(sig["reasons"]) + [f"retest du niveau {p['level']:.6g} confirmé, rebond au prix {px:.6g}"]
                    self._take(t, sig)
                continue
            if now > p["expires"]:
                del self.pending[t]; self._ev("abandon", t, f"{sig['side'].upper()} : pas de confirmation du mouvement dans le délai"); continue
            if (short and px > p["invalid"]) or ((not short) and px < p["invalid"]):
                del self.pending[t]; self._ev("abandon", t, f"{sig['side'].upper()} : le prix va à contre-sens au-delà du bruit ({px:.6g}) — signal annulé"); continue
            if (short and px <= p["trigger"]) or ((not short) and px >= p["trigger"]):
                del self.pending[t]
                if self._busy_same_mode(t, mode):
                    continue
                sig = dict(sig); sig["price"] = px
                sig["reasons"] = list(sig["reasons"]) + [f"mouvement confirmé au prix {px:.6g}"]
                self._take(t, sig)

    def _busy_same_mode(self, ticker, mode):
        """v4.431 : seul le LIVE interdit d ouvrir deux fois le meme actif (Hyperliquid ne tient qu une position par actif). En PAPER, rien ne
        bloque : le meme actif peut etre ouvert plusieurs fois, et une position paper n empeche jamais un live (ni l inverse)."""
        if mode != "live":
            return False
        for it in list(self.manual.items.values()):
            if it.get("ticker") == ticker and it.get("mode") == mode and it.get("status") in ("scheduled", "opening", "open", "closing"):
                return True
        if mode == "live":
            try:
                return bool(self.manual._bot_live_position_on(ticker))
            except Exception:
                return False
        return False

    def _free_margin(self):
        """Marge libre du compte Hyperliquid (cache 10 s) ; None si illisible (Hyperliquid refusera alors lui-meme)."""
        ts, val = getattr(self, "_fm", (0, None))
        if time.time() - ts < 10:
            return val
        val = None
        try:
            w = self.manual._wallet()
            st = self.bot.info.user_state(w)
            ms = st["marginSummary"]
            val = float(ms["accountValue"]) - float(ms["totalMarginUsed"])
            wd = st.get("withdrawable")
            if wd is not None:
                val = min(val, float(wd)) if float(wd) > 0 else val
        except Exception as e:
            self.last_error = f"marge libre : {type(e).__name__}: {e}"
        self._fm = (time.time(), val)
        return val

    def _take(self, t, sig):
        c = self.cfg
        mode = "live" if c.get("SCALP_MODE") == "live" else "paper"
        if mode == "live":
            need = float(c.get("SCALP_NOTIONAL_USD", 60.0)) / max(int(c.get("SCALP_LEVERAGE", 3)), 1)
            free = self._free_margin()
            if free is not None and free < need * 1.05 + 0.5:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} non ouvert : marge libre insuffisante ({free:.2f} $ < {need:.2f} $ requis)")
                return
        reg = dict(self.regime) if self.regime.get("n") else {}
        notional = float(c.get("SCALP_NOTIONAL_USD", 60.0))
        sp = float(c.get("SCALP_CORR_SIZE_PCT", 100))
        if reg.get("label") == "correle" and 0 < sp < 100:      # v4.442 : marche correle = un seul pari ; on reduit la taille de chaque ligne
            notional = max(notional * sp / 100.0, 10.5 if mode == "live" else 1.0)
        cluster_before = self._cluster_count(sig["side"], mode)
        params = {"strategy": STRATEGY, "ticker": t, "direction": sig["side"], "market": "perp", "mode": mode,
                  "execution": "now", "leverage": int(c.get("SCALP_LEVERAGE", 3)),
                  "notional_usd": round(notional, 2),
                  "sl_pct": float(c.get("SCALP_SL_PCT", 0.65)), "tp_pct": float(c.get("SCALP_TP_PCT", 0.6)),
                  "confirm_live": mode == "live"}
        if int(c.get("SCALP_RUN_ENABLED", 1)):     # v4.434 : laisser courir apres l objectif ; sortie si le prix recule de X % depuis son plus haut
            params.update({"tp_run": True, "tp_run_lock_pct": 0.0, "tp_run_trail_pct": float(c.get("SCALP_RUN_TRAIL_PCT", 0.2))})
        if int(c.get("SCALP_BE_ENABLED", 0)):      # v4.445 : stop au seuil de rentabilite apres un gain de X %
            params.update({"be_arm_pct": float(c.get("SCALP_BE_ARM_PCT", 0.4)), "be_lock_pct": float(c.get("SCALP_BE_LOCK_PCT", 0.1))})
        try:
            res = self.manual.submit(params)
        except Exception as e:
            self._ev("erreur", t, f"{sig['kind']} {sig['side']} non ouvert : {e}")
            self._cool[(t, mode)] = time.time() + 300
            return
        self._cool[(t, mode)] = time.time() + float(c.get("SCALP_COOLDOWN_MIN", 30)) * 60
        self._opens.append((time.time(), sig["side"], mode, t))
        try:
            with self.manual.lock:
                it = self.manual.items.get(res["id"])
                if it is not None:
                    it["scalp"] = {"kind": sig["kind"], "score": sig["score"], "level": sig["level"], "room": round(sig["room"], 2),
                                   "reasons": sig["reasons"], "volume_ratio": round(sig["volume_ratio"], 2),
                                   "atr15_pct": round(sig["atr15_pct"], 3), "counter": bool(sig.get("counter")),
                                   "regime": {"label": reg.get("label", "inconnu"), "corr": reg.get("corr"), "up": reg.get("up"), "down": reg.get("down"),
                                              "basket30": reg.get("basket30"), "rel30": (round(reg["ch30"].get(t, 0) - reg["basket30"], 3) if reg.get("ch30") and reg.get("basket30") is not None else None),
                                              "cluster": cluster_before, "peers": sig.get("peers")},
                                   "ctx": sig.get("ctx"), "retest": bool(sig.get("retest"))}
                    it["reasons"] = sig["reasons"]
                    self.manual._save(it)
        except Exception as e:
            self.last_error = f"annotation : {e}"
        self._ev("entree", t, f"{sig['side'].upper()} {sig['kind']} note {sig['score']:.0f} ({mode}) — " + " · ".join(sig["reasons"][:3]))
        self.manual._log(f"SCALP {t} {sig['side'].upper()} ({sig['kind']}, note {sig['score']:.0f}, {mode}) — " + " ; ".join(sig["reasons"]), "signal")

    # ── etat pour l interface ──
    def snapshot(self):
        c = self.cfg
        ok, why = self.window_state() if int(c.get("SCALP_ENABLED", 0)) else (False, "desactive")
        return {"enabled": bool(int(c.get("SCALP_ENABLED", 0))), "mode": c.get("SCALP_MODE", "paper"), "window_open": ok, "window_reason": why,
                "universe": list(self._uni), "open": self._open_count(), "max_open": int(c.get("SCALP_MAX_OPEN", 0)),
                "params": {k: c.get(k) for k in ("SCALP_LEVERAGE", "SCALP_NOTIONAL_USD", "SCALP_SL_PCT", "SCALP_TP_PCT", "SCALP_HOURS_UTC",
                                                 "SCALP_MIN_SCORE", "SCALP_SHORT_MIN_SCORE", "SCALP_TREND_MODE", "SCALP_SHORT_BTC_BLOCK", "SCALP_SHORT_BTC_MAX_PCT", "SCALP_RUN_ENABLED", "SCALP_RUN_TRAIL_PCT", "SCALP_BE_ENABLED", "SCALP_BE_ARM_PCT", "SCALP_BE_LOCK_PCT", "SCALP_FLOW_ALL_ON", "SCALP_FLOW_ALL_MIN", "SCALP_FLOW_ALL_MIN_TRADES", "SCALP_RETEST_ON", "SCALP_RETEST_BARS", "SCALP_RETEST_TOL_ATR", "SCALP_RETEST_BOUNCE_ATR", "SCALP_COUNTER_ENABLED", "SCALP_COUNTER_MIN_SCORE", "SCALP_COUNTER_FLOW_MIN", "SCALP_COUNTER_MIN_TRADES", "SCALP_CONFIRM_BARS", "SCALP_NOISE_ATR", "SCALP_CORR_HIGH", "SCALP_DAILY_LOSS_USD", "SCALP_MAX_PER_DAY", "SCALP_MAX_HOLD_MIN", "SCALP_TOP_N")},
                "regime": {k: v for k, v in self.regime.items()}, "peer_wait": [{"ticker": t, "side": w["side"], "age_min": round((time.time() - w["ts"]) / 60, 1)} for t, w in self.peer_wait.items()],
                "stats_paper": self.stats("paper"), "stats_live": self.stats("live"),
                "pending": [{"ticker": t, "side": p["sig"]["side"], "trigger": p["trigger"], "expires": p["expires"]} for t, p in self.pending.items()],
                "events": list(self.events)[:40], "error": self.last_error}
