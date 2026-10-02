"""v4.347 — REJEU HISTORIQUE des entrees (LECTURE SEULE).

Sur demande : savoir, avec des milliers d exemples plutot que quelques dizaines de trades, si une
entree "long" ou "short" a un avantage reel selon le contexte (tendance H4, zone, mouvement recent,
volatilite, heure) — AVANT de modifier la moindre regle. Rien ici ne passe d ordre ni ne modifie le
bot : le module telecharge des bougies publiques, rejoue ce qu un signal aurait donne et agrege.

Principe
  * chaque bougie M15 cloturee est un point d entree possible (entree a l OUVERTURE de la bougie
    suivante, comme dans la realite) ;
  * "signal" = les motifs de retournement du bot (mtf_analysis.candle_signal) SANS aucun filtre ;
  * "base" = une entree prise sur une bougie sur deux, sans signal : c est le temoin. Le signal n a
    de valeur que s il fait MIEUX que la base dans le meme contexte ;
  * issue = premier niveau touche, +T % (favorable) ou -T % (defavorable), sur un horizon donne ; si
    aucun n est touche, le rendement a la fin de l horizon. Gain net = moyenne - frais ;
  * si les deux niveaux sont touches dans la meme bougie, l ordre est inconnu : compte comme perte ;
  * l intervalle de confiance regroupe les entrees PAR JOUR (elles ne sont pas independantes).
"""
import math
import random
import statistics
import threading
import time

import mtf_analysis as mtf

SEC15_MS = 900_000
SEC4H_MS = 14_400_000
WARM_BARS = 700           # historique M15 requis avant une entree (volatilite de reference sur ~7 jours)
MAXH = 24                 # horizon maximal (6 h) en bougies M15
METRICS = (("0.3_2h", 0.3, 8), ("0.5_2h", 0.5, 8), ("0.5_6h", 0.5, 24), ("1.0_6h", 1.0, 24))
PRIMARY = "0.5_2h"
FAMILIES = ("all", "trend", "zone", "move", "vol", "hour")
TREND_LABELS = {"up_strict": "haussiere stricte", "up_pullback": "haussiere avec repli",
                "other": "autre / rupture", "down_bounce": "baissiere avec rebond",
                "down_strict": "baissiere stricte"}
KF, KS = 2 / 51, 2 / 201


def fetch_candles(info, coin, interval, days_back, now_ms=None):
    """Bougies cloturees (t, o, h, l, c) d un actif. candleSnapshot : au plus 5000 bougies par requete."""
    end_ms = int(now_ms or time.time() * 1000)
    start = end_ms - int(days_back * 86400 * 1000)
    raw = info.post("/info", {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval, "startTime": start, "endTime": end_ms}})
    if not isinstance(raw, list):
        return []
    out = [{"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]),
            "c": float(c["c"]), "v": float(c.get("v", 0))} for c in raw if int(c.get("T", 0)) <= end_ms]
    out.sort(key=lambda c: c["t"])
    return out


def fetch_with_retry(info, coin, interval, days_back, now_ms, retries=2, wait=20.0):
    """Nouvelle tentative apres une pause si l API refuse (limite de requetes) ou renvoie une erreur."""
    last = None
    for attempt in range(retries + 1):
        try:
            data = fetch_candles(info, coin, interval, days_back, now_ms)
            if data:
                return data
            last = "reponse vide"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(wait)
    raise RuntimeError(f"bougies {interval} indisponibles pour {coin} ({last})")


def _ema_list(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, n):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def build_h4_context(h4):
    """Pour chaque bougie H4 cloturee j : EMA50/EMA200, ATR et zones calculees UNIQUEMENT avec les
    bougies jusqu a j (aucune information du futur)."""
    closes = [c["c"] for c in h4]
    ef, es = _ema_list(closes, 50), _ema_list(closes, 200)
    ctx = [None] * len(h4)
    for j in range(len(h4)):
        if es[j] is None:
            continue
        a = mtf.atr(h4[max(0, j - 14):j + 1]) or 0
        if a <= 0:
            continue
        zones = mtf.find_zones(h4[max(0, j + 1 - 120):j + 1], lookback=120)
        ctx[j] = {"ef": ef[j], "es": es[j], "atr": a, "zones": zones}
    return ctx


def classify_trend(ctx, p):
    """Tendance de fond avec le prix courant ajoute comme dernier point (comme le bot en direct)."""
    ef = p * KF + ctx["ef"] * (1 - KF)
    es = p * KS + ctx["es"] * (1 - KS)
    if p > ef > es:
        return "up_strict"
    if p < ef < es:
        return "down_strict"
    if ef > es and p > es:
        return "up_pullback"
    if ef < es and p < es:
        return "down_bounce"
    return "other"


def classify_zone(ctx, p):
    sup, res = mtf.nearest_zones(ctx["zones"], p)
    tol = ctx["atr"] * 0.25
    ins, inr = mtf.in_zone(p, sup, tol), mtf.in_zone(p, res, tol)
    if ins and inr:
        return "zone unique (support ET resistance)"
    if ins:
        return "dans un support"
    if inr:
        return "dans une resistance"
    return "hors zone"


def evaluate_entry(m15, i, side):
    """Issue d une entree prise a l OUVERTURE de la bougie i+1. Retourne, pour chaque metrique
    (T, H) : (issue, rendement) avec issue = +1 (T atteint d abord), -1 (-T d abord ou ambigu), 0
    (aucun) ; plus le gain maximal / la perte maximale / le rendement final a 2 h."""
    e = m15[i + 1]["o"]
    sgn = 1 if side == "long" else -1
    fav, adv = [], []
    for k in range(1, MAXH + 1):
        b = m15[i + k]
        if sgn > 0:
            fav.append((b["h"] / e - 1) * 100)
            adv.append((1 - b["l"] / e) * 100)
        else:
            fav.append((1 - b["l"] / e) * 100)
            adv.append((b["h"] / e - 1) * 100)
    res = []
    amb_primary = 0
    for name, T, H in METRICS:
        out = 0
        for k in range(H):
            up, dn = fav[k] >= T, adv[k] >= T
            if up and dn:
                out = -1
                if name == PRIMARY:
                    amb_primary = 1
                break
            if up:
                out = 1
                break
            if dn:
                out = -1
                break
        if out == 1:
            pay = T
        elif out == -1:
            pay = -T
        else:
            pay = sgn * (m15[i + H]["c"] / e - 1) * 100
        res.append((out, pay))
    fin8 = sgn * (m15[i + 8]["c"] / e - 1) * 100
    return res, max(fav[:8]), max(adv[:8]), fin8, amb_primary


def entries_for_asset(m15, h4, replay_start_ms, base_stride=2, cooldown=4):
    """Genere les entrees d un actif : liste de (side, kind, contexte, jour, t_close, issues...)."""
    ctx = build_h4_context(h4)
    n = len(m15)
    if n < WARM_BARS + MAXH + 10 or not any(ctx):
        return []
    closes = [c["c"] for c in m15]
    rng = [(c["h"] - c["l"]) / c["c"] * 100 for c in m15]
    out = []
    last_sig = {"long": -999, "short": -999}
    j = -1
    for i in range(16, n - MAXH - 1):
        t_close = m15[i]["t"] + SEC15_MS
        while j + 1 < len(h4) and h4[j + 1]["t"] + SEC4H_MS <= t_close:
            j += 1
        if i < WARM_BARS or m15[i]["t"] < replay_start_ms or j < 0 or ctx[j] is None:
            continue
        if m15[i + MAXH]["t"] - m15[i - 16]["t"] != (MAXH + 16) * SEC15_MS:
            continue  # trou dans les donnees
        c = ctx[j]
        p = closes[i]
        base_rng = rng[max(0, i - 688):i - 16]
        if len(base_rng) < 200:
            continue
        med = statistics.median(base_rng)
        if med <= 0:
            continue
        vr = (sum(rng[i - 15:i + 1]) / 16) / med
        chg = (p / closes[i - 8] - 1) * 100
        hour = (t_close // 3_600_000) % 24
        cv = {
            "all": "Tout",
            "trend": TREND_LABELS[classify_trend(c, p)],
            "zone": classify_zone(c, p),
            "move": "hausse recente (>= +1 % en 2 h)" if chg >= 1.0 else ("baisse recente (<= -1 % en 2 h)" if chg <= -1.0 else "mouvement calme"),
            "vol": "marche calme (< 0,8x)" if vr < 0.8 else ("marche agite (> 1,25x)" if vr > 1.25 else "volatilite normale"),
            "hour": f"{(hour // 6) * 6:02d}h-{(hour // 6) * 6 + 6:02d}h UTC",
        }
        day = t_close // 86_400_000
        window = m15[i - 5:i + 1]
        for side in ("long", "short"):
            name, _ex = mtf.candle_signal(window, side)
            is_sig = bool(name) and (i - last_sig[side] >= cooldown)
            if is_sig:
                last_sig[side] = i
            is_base = (i % base_stride == 0)
            if not (is_sig or is_base):
                continue
            ev = evaluate_entry(m15, i, side)
            if is_sig:
                out.append((side, "signal", cv, day, t_close, ev))
            if is_base:
                out.append((side, "base", cv, day, t_close, ev))
    return out


class Aggregator:
    """Cumule les issues par groupe (cote, signal/base, famille de contexte, valeur) — memoire bornee."""

    def __init__(self, cut_ms, seed=1):
        self.g = {}
        self.cut_ms = cut_ms
        self.rnd = random.Random(seed)

    def _new(self):
        return {"n": 0, "days": set(), "m": {nm: [0, 0, 0, 0.0] for nm, _t, _h in METRICS},
                "mfe": [], "mae": [], "fin": [], "amb": 0,
                "byday": {}, "p1": [0, 0.0], "p2": [0, 0.0]}

    def add(self, side, kind, cv, day, t_close, ev):
        issues, mfe, mae, fin8, amb = ev
        part = "p1" if t_close < self.cut_ms else "p2"
        for fam in FAMILIES:
            key = (side, kind, fam, cv[fam])
            G = self.g.get(key)
            if G is None:
                G = self.g[key] = self._new()
            G["n"] += 1
            G["days"].add(day)
            G["amb"] += amb
            for (nm, _T, _H), (out, pay) in zip(METRICS, issues):
                M = G["m"][nm]
                if out == 1:
                    M[0] += 1
                elif out == -1:
                    M[1] += 1
                else:
                    M[2] += 1
                M[3] += pay
                if nm == PRIMARY:
                    bd = G["byday"].get(day)
                    if bd is None:
                        bd = G["byday"][day] = [0, 0.0]
                    bd[0] += 1
                    bd[1] += pay
                    pp = G[part]
                    pp[0] += 1
                    pp[1] += pay
            for lst, v in ((G["mfe"], mfe), (G["mae"], mae), (G["fin"], fin8)):
                if len(lst) < 4000:
                    lst.append(v)
                else:
                    r = self.rnd.randrange(G["n"])
                    if r < 4000:
                        lst[r] = v

    def summary(self, fee_pct):
        rows = []
        for (side, kind, fam, val), G in self.g.items():
            n = G["n"]
            if n < 20:
                continue
            m = {}
            for nm, _T, _H in METRICS:
                up, dn, no, sp = G["m"][nm]
                m[nm] = {"up": round(up / n * 100, 1), "down": round(dn / n * 100, 1), "none": round(no / n * 100, 1),
                         "edge": round(sp / n - fee_pct, 3)}
            mean_p = G["m"][PRIMARY][3] / n
            # erreur standard regroupee par jour (les entrees d un meme jour ne sont pas independantes)
            ss = sum((s - cnt * mean_p) ** 2 for cnt, s in G["byday"].values())
            se = math.sqrt(ss) / n if n else 0.0
            n1, s1 = G["p1"]
            n2, s2 = G["p2"]
            rows.append({
                "side": side, "kind": kind, "family": fam, "value": val, "n": n, "days": len(G["days"]),
                "m": m, "ci95": round(1.96 * se, 3), "amb_pct": round(G["amb"] / n * 100, 1),
                "mfe_med": round(statistics.median(G["mfe"]), 3), "mae_med": round(statistics.median(G["mae"]), 3),
                "fin_med": round(statistics.median(G["fin"]), 3),
                "n1": n1, "edge1": round(s1 / n1 - fee_pct, 3) if n1 else None,
                "n2": n2, "edge2": round(s2 / n2 - fee_pct, 3) if n2 else None,
            })
        return rows


def run_replay(info, assets, days=30, fee_pct=0.089, progress=None, sleeps=(9.0, 3.0), now_ms=None, cancel=None,
               retry_wait=20.0):
    """Telecharge les bougies, rejoue, agrege. progress(i, total, actif) est appele a chaque actif.
    sleeps = pauses apres la requete M15 (lourde : ~3 800 bougies) puis H4. Le debit est volontairement
    bride (~500 de poids par minute, moins de la moitie de la limite publique) pour ne JAMAIS perturber
    les requetes du bot en marche."""
    now_ms = int(now_ms or time.time() * 1000)
    days = max(7, min(int(days), 40))                         # 15 min : 5000 bougies au plus par requete
    start_ms = now_ms - days * 86400 * 1000
    agg = Aggregator(cut_ms=start_ms + (now_ms - start_ms) * 2 // 3)
    ok, skipped, counts = [], [], {"signal": 0, "base": 0}
    for idx, coin in enumerate(assets):
        if cancel is not None and cancel():
            skipped.append((coin, "annule"))
            break
        if progress:
            progress(idx, len(assets), coin)
        try:
            m15 = fetch_with_retry(info, coin, "15m", days + 9, now_ms, wait=retry_wait)
            time.sleep(sleeps[0])
            h4 = fetch_with_retry(info, coin, "4h", days + 50, now_ms, wait=retry_wait)
            time.sleep(sleeps[1])
            if len(m15) < WARM_BARS + MAXH + 50 or len(h4) < 230:
                skipped.append((coin, f"historique insuffisant (M15 {len(m15)}, H4 {len(h4)})"))
                continue
            ents = entries_for_asset(m15, h4, start_ms)
            for side, kind, cv, day, t_close, ev in ents:
                agg.add(side, kind, cv, day, t_close, ev)
                counts[kind] += 1
            ok.append(coin)
        except Exception as e:  # un actif en erreur ne doit pas interrompre le rejeu
            skipped.append((coin, f"{type(e).__name__}: {e}"))
    if progress:
        progress(len(assets), len(assets), "calcul des resultats")
    rows = agg.summary(fee_pct)
    return {"meta": {"days": days, "fee_pct": fee_pct, "start_ms": start_ms, "end_ms": now_ms,
                     "cut_ms": agg.cut_ms, "assets_ok": ok, "assets_skipped": skipped,
                     "signals": counts["signal"], "base": counts["base"],
                     "metrics": [{"name": nm, "T": T, "H_bars": H} for nm, T, H in METRICS], "primary": PRIMARY},
            "groups": rows}


class ReplayJob:
    """Execute le rejeu dans un thread ; un seul a la fois. Etat consultable par l API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.progress = {"done": 0, "total": 0, "current": ""}
        self.result = None
        self.error = None
        self.started_at = self.finished_at = None
        self._cancel = False

    def start(self, info, assets, days, fee_pct, on_done=None, sleeps=(9.0, 3.0), retry_wait=20.0):
        with self.lock:
            if self.state == "running":
                return False
            self.state, self.result, self.error = "running", None, None
            self.started_at, self.finished_at, self._cancel = time.time(), None, False
            self.progress = {"done": 0, "total": len(assets), "current": ""}

        def prog(i, total, coin):
            self.progress = {"done": i, "total": total, "current": coin}

        def work():
            try:
                res = run_replay(info, assets, days, fee_pct, progress=prog, sleeps=sleeps,
                                 cancel=lambda: self._cancel, retry_wait=retry_wait)
                self.result, self.state = res, "done"
                if on_done:
                    on_done(res)
            except Exception as e:
                self.error, self.state = f"{type(e).__name__}: {e}", "error"
            finally:
                self.finished_at = time.time()

        threading.Thread(target=work, daemon=True).start()
        return True

    def cancel(self):
        self._cancel = True

    def status(self, with_result=True):
        out = {"state": self.state, "progress": self.progress, "error": self.error,
               "started_at": self.started_at, "finished_at": self.finished_at}
        if with_result and self.state == "done":
            out["result"] = self.result
        return out
