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


def evaluate(c5, c15, c1h, btc_chg, K):
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
                              "reasons": [f"rebond sur support {zl:.6g}-{zh:.6g} ({nm} 5 min)"] + why, "level": zh})
        # ── REBOND sur resistance (SHORT) ──
        if zh + tol >= price and price >= zl - 1.2 * a15 and max(c["h"] for c in c5[-3:]) >= zl - tol:
            nm, _ = mtf.candle_signal(c5, "short")
            if nm and last["c"] < last["o"] and last["c"] < zh and (rsi5 is None or rsi5 > 45):
                sc, why, rm = score_of("short", "rebond", z, 6)
                cands.append({"side": "short", "kind": "rebond", "zone": (zl, zh), "score": sc, "room": rm,
                              "reasons": [f"rejet de la resistance {zl:.6g}-{zh:.6g} ({nm} 5 min)"] + why, "level": zl})
        # ── CASSURE haussiere confirmee ──
        strong_up = rng > 0 and body >= 0.5 * a5 and last["c"] > last["o"] and (last["c"] - last["l"]) / rng >= 0.7
        if (strong_up and last["c"] > zh + 0.1 * a15 and min(prev["c"], prev2["c"]) <= zh and vr >= K["vol_mult"]
                and (last["c"] - zh) / zh * 100 <= K["max_chase_pct"] and (rsi5 is None or rsi5 < 80)):
            sc, why, rm = score_of("long", "cassure", z, 8)
            cands.append({"side": "long", "kind": "cassure", "zone": (zl, zh), "score": sc, "room": rm,
                          "reasons": [f"cassure confirmee de {zh:.6g} (5 min, corps fort)"] + why, "level": zh})
        # ── CASSURE baissiere confirmee ──
        strong_dn = rng > 0 and body >= 0.5 * a5 and last["c"] < last["o"] and (last["h"] - last["c"]) / rng >= 0.7
        if (strong_dn and last["c"] < zl - 0.1 * a15 and max(prev["c"], prev2["c"]) >= zl and vr >= K["vol_mult"]
                and (zl - last["c"]) / zl * 100 <= K["max_chase_pct"] and (rsi5 is None or rsi5 > 20)):
            sc, why, rm = score_of("short", "cassure", z, 8)
            cands.append({"side": "short", "kind": "cassure", "zone": (zl, zh), "score": sc, "room": rm,
                          "reasons": [f"cassure confirmee de {zl:.6g} (5 min, corps fort)"] + why, "level": zl})
    if not cands:
        return None, "aucun signal"
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
    return best, "ok"


# ───────────────────────────── moteur ─────────────────────────────
class ScalpEngine:
    def __init__(self, bot, manual):
        self.bot, self.manual = bot, manual
        self.events = deque(maxlen=80)       # journal en memoire (signaux pris / refuses)
        self._uni, self._uni_ts = [], 0
        self._seen = {}                      # ticker -> derniere tranche 5 min evaluee
        self._cool = {}                      # ticker -> fin de pause
        self._busy = False
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
                "min_score_short": float(c.get("SCALP_SHORT_MIN_SCORE", 62))}

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
                if d.get("strategy_source") == STRATEGY and d.get("status") == "closed" and d.get("mode") == mode:
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
            fees = sum(float(d.get("fees_est") or 0) for d in lst)
            return {"n": n, "wins": w, "win_rate": (w / n * 100) if n else None, "pnl": pnl, "fees": fees}
        streak = 0
        for d in rows:
            if float(d.get("pnl") or 0) <= 0:
                streak += 1
            else:
                break
        by_kind = {}
        for k in ("rebond", "cassure"):
            by_kind[k] = agg([d for d in rows if (d.get("scalp") or {}).get("kind") == k])
        by_hour = {}
        for d in rows:
            h = datetime.fromtimestamp(float(d.get("opened_at") or d.get("closed_at") or 0), timezone.utc).hour
            by_hour.setdefault(h, []).append(d)
        out = {"mode": mode, "total": agg(rows), "today": agg(today), "streak_losses": streak, "by_kind": by_kind,
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
        if mx <= 0:
            return
        for it in list(self.manual.items.values()):
            if it.get("strategy_source") == STRATEGY and it.get("status") == "open":
                if now - float(it.get("opened_at") or now) > mx:
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
                if time.time() < self._cool.get((t, mode), 0) or self._busy_same_mode(t, mode):
                    continue
                try:
                    c5 = self.bot._mtf_candles(t, "5m", 60)
                    c15 = self.bot._mtf_candles(t, "15m", 60)
                    c1h = self.bot._mtf_candles(t, "1h", 230)
                except Exception as e:
                    self.last_error = f"bougies {t} : {type(e).__name__}: {e}"
                    continue
                sig, motif = evaluate(c5, c15, c1h, btc, K)
                if not sig:
                    if "refuse" in motif:
                        self._ev("refus", t, motif)
                    continue
                self._take(t, sig)
        except Exception as e:
            self.last_error = f"scan : {type(e).__name__}: {e}"
            print(f"[SCALP] {self.last_error}")
        finally:
            self._busy = False

    def _busy_same_mode(self, ticker, mode):
        """v4.422 : une double ouverture n est bloquee que dans le MEME mode (une position paper n empeche pas un live, et inversement)."""
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
        params = {"strategy": STRATEGY, "ticker": t, "direction": sig["side"], "market": "perp", "mode": mode,
                  "execution": "now", "leverage": int(c.get("SCALP_LEVERAGE", 3)),
                  "notional_usd": float(c.get("SCALP_NOTIONAL_USD", 60.0)),
                  "sl_pct": float(c.get("SCALP_SL_PCT", 1.0)), "tp_pct": float(c.get("SCALP_TP_PCT", 0.6)),
                  "confirm_live": mode == "live"}
        try:
            res = self.manual.submit(params)
        except Exception as e:
            self._ev("erreur", t, f"{sig['kind']} {sig['side']} non ouvert : {e}")
            self._cool[(t, mode)] = time.time() + 300
            return
        self._cool[(t, mode)] = time.time() + float(c.get("SCALP_COOLDOWN_MIN", 30)) * 60
        try:
            with self.manual.lock:
                it = self.manual.items.get(res["id"])
                if it is not None:
                    it["scalp"] = {"kind": sig["kind"], "score": sig["score"], "level": sig["level"], "room": round(sig["room"], 2),
                                   "reasons": sig["reasons"], "volume_ratio": round(sig["volume_ratio"], 2),
                                   "atr15_pct": round(sig["atr15_pct"], 3)}
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
                                                 "SCALP_MIN_SCORE", "SCALP_SHORT_MIN_SCORE", "SCALP_DAILY_LOSS_USD", "SCALP_MAX_PER_DAY", "SCALP_MAX_HOLD_MIN", "SCALP_TOP_N")},
                "stats_paper": self.stats("paper"), "stats_live": self.stats("live"),
                "events": list(self.events)[:40], "error": self.last_error}
