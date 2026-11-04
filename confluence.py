"""v4.568 — Principes de trading (contexte majeur + confluence de 3 critères + ratio risque/rendement net de frais), backtest et test fantôme.

Tout est PUR (aucun accès réseau, sauf `fetch_candles` qui reçoit l objet d accès Hyperliquid) : mêmes fonctions pour le trading, le backtest
et le test fantôme. Les principes ne dépendent d aucun filtre historique du bot.

Principes :
 1. Contexte : H1 dans le sens du trade (EMA20 > EMA50 et clôture > EMA50 pour un achat), H4 (construit à partir du H1) pas contre.
 2. Confluence de 3 critères (tous requis par défaut) : (a) zone clé = support / résistance H1 testé au moins N fois, ou chiffre rond ;
    (b) indicateur = RSI 5 min en zone extrême OU réaction sur l EMA 20 / 50 en 15 min ; (c) price action = bougie de retournement 5 min
    (marteau, avalement, étoile) sur la zone, ou cassure de base 1 min fournie par l appelant.
 3. Ratio risque / rendement NET de frais >= minimum (par défaut 1,0), objectif = RR x stop (1,5), et de la place avant la zone opposée.
"""
import math
import time
import bisect

import mtf_analysis as mtf

BAR_MS = {"5m": 300000, "15m": 900000, "1h": 3600000}


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def thresholds(cfg):
    g = lambda k, d: float(cfg.get(k, d) if cfg.get(k, d) not in (None, "") else d)
    return {"min_conf": int(g("SCALP_CONF_MIN", 3)), "min_touches": int(g("SCALP_CONF_MIN_TOUCHES", 2)), "rsi": g("SCALP_CONF_RSI", 35),
            "rr": g("SCALP_CONF_RR", 1.5), "min_rr": g("SCALP_CONF_MIN_RR", 1.0), "sl_min": g("SCALP_CONF_SL_MIN_PCT", 0.4),
            "sl_max": g("SCALP_CONF_SL_MAX_PCT", 1.0), "fee": g("SCALP_CONF_FEE_PCT", 0.10), "h1_strict": int(g("SCALP_CONF_H1_STRICT", 1)),
            "pad": g("SCALP_CONF_PAD_ATR", 0.15), "tol": g("SCALP_CONF_TOL_ATR", 0.6), "hold_bars": int(g("SCALP_CONF_HOLD_BARS", 24)),
            "zone_max_atr": g("SCALP_CONF_ZONE_MAX_ATR", 1.5), "ctx_mode": int(g("SCALP_CTX_MODE", 1)), "sl_atr": g("SCALP_CONF_SL_ATR", 1.5)}


def sl_atr_floor(sl_pct, atr5, price, mult, cap_pct):
    """v4.569 : plancher du stop = mult x ATR 5 min (stop au-dela du bruit). Retourne le nouveau stop en % (jamais plus petit que sl_pct, plafonne a cap_pct)."""
    if not (atr5 and price and mult > 0):
        return sl_pct
    return max(sl_pct, min(mult * atr5 / price * 100.0, max(cap_pct, sl_pct)))


def agg_h4(c1h):
    """Bougies 4 h COMPLETES (4 bougies H1) construites à partir du H1."""
    out, cur, n = [], None, 0
    for c in c1h:
        b = int(c["t"] // 14400000)
        if cur is None or cur["_b"] != b:
            if cur is not None and n == 4:
                out.append(cur)
            cur, n = {"_b": b, "t": b * 14400000, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v", 0) or 0}, 1
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], c["h"]), min(cur["l"], c["l"]), c["c"]
            cur["v"] = (cur.get("v", 0) or 0) + (c.get("v", 0) or 0)
            n += 1
    if cur is not None and n == 4:
        out.append(cur)
    return out


def abs_state(candles, min_bars=50):
    """Tendance d une serie de bougies : 'haussier' (EMA20 > EMA50 et cloture > EMA50), 'baissier' (inverse), 'neutre', 'inconnu' (pas assez de bougies)."""
    if not candles or len(candles) < min_bars:
        return "inconnu"
    cl = [c["c"] for c in candles]
    e20, e50 = mtf.ema(cl, 20), mtf.ema(cl, 50)
    if e20 is None or e50 is None:
        return "inconnu"
    if e20 > e50 and cl[-1] > e50:
        return "haussier"
    if e20 < e50 and cl[-1] < e50:
        return "baissier"
    return "neutre"


def market_context(c1h, c5=None, now_ms=None, max_age_h=3.0):
    """v4.570 : SOURCE UNIQUE du contexte. H1 et H4 (H4 construit du H1) par la meme definition, M5 pour information.
    Une serie trop courte ou perimee (derniere bougie H1 cloturee depuis plus de max_age_h) donne 'inconnu' / 'perime' : jamais une valeur devinee."""
    c4h = agg_h4(c1h) if c1h else []
    h1 = abs_state(c1h, 60)
    h4 = abs_state(c4h, 50)
    age_s = None
    if c1h and now_ms:
        age_s = (now_ms - (c1h[-1]["t"] + 3600000)) / 1000.0
        if age_s > max_age_h * 3600:
            h1 = h4 = "perime"
    m5 = abs_state(c5, 50) if c5 else "inconnu"
    return {"h1": h1, "h4": h4, "m5": m5, "n1h": len(c1h or []), "n4h": len(c4h), "age_1h_s": (round(age_s) if age_s is not None else None)}


def _rel(state, side):
    if state in ("inconnu", "perime"):
        return state
    if state == "neutre":
        return "neutre"
    return "avec" if (state == "haussier") == (side == "long") else "contre"


def allowed(mc, side, mode=1):
    """(ok, texte, relation H1, relation H4). mode 1 : aucun des deux contre ET au moins un dans le sens ; mode 2 : les deux dans le sens.
    Donnee manquante ou perimee = refus."""
    r1, r4 = _rel(mc["h1"], side), _rel(mc["h4"], side)
    for nm, r in (("H1", r1), ("H4", r4)):
        if r in ("inconnu", "perime"):
            return False, f"contexte : {nm} {'périmé' if r == 'perime' else 'inconnu (pas assez de bougies)'}", r1, r4
    if "contre" in (r1, r4):
        return False, f"contexte : H1 {mc['h1']}, H4 {mc['h4']} — à contre-courant", r1, r4
    if mode >= 2:
        ok = r1 == "avec" and r4 == "avec"
    else:
        ok = "avec" in (r1, r4)
    if not ok:
        return False, f"contexte : H1 {mc['h1']}, H4 {mc['h4']} — pas dans le sens du trade", r1, r4
    return True, "", r1, r4


def _state(candles, side):
    """'avec' / 'contre' / 'neutre' : EMA20 > EMA50 et clôture > EMA50 (haussier) ; inverse (baissier)."""
    cl = [c["c"] for c in candles]
    e20, e50 = mtf.ema(cl, 20), mtf.ema(cl, 50)
    if e20 is None or e50 is None:
        return "inconnu"
    up = e20 > e50 and cl[-1] > e50
    dn = e20 < e50 and cl[-1] < e50
    if (up and side == "long") or (dn and side == "short"):
        return "avec"
    if (up and side == "short") or (dn and side == "long"):
        return "contre"
    return "neutre"


def h1_cache(c1h, max_w=None):
    """Calculs qui ne changent qu a chaque nouvelle bougie H1 (zones, H4) : à réutiliser.
    v4.569 : max_w = largeur MAXIMALE d une zone en ATR 1 h (une « zone » de 10 a 50 % du prix n est pas un niveau)."""
    c4h = agg_h4(c1h)
    return {"zones": mtf.find_zones(c1h[-150:], lookback=120, min_touches=1, max_width_atr=(max_w if max_w and max_w > 0 else None)), "c4h": c4h,
            "mc": market_context(c1h)}


def evaluate(side, c5, c15, c1h, K, cache=None, base_ok=None):
    """Évalue les principes pour `side` sur la dernière bougie 5 min CLÔTURÉE. Retourne un dict (clé `ok` = tous les principes respectés)."""
    out = {"ok": False, "why": "", "n_conf": 0, "flags": {}, "ctx_ok": False}
    if len(c5) < 30 or len(c15) < 50 or len(c1h) < 60:
        out["why"] = "historique insuffisant"
        return out
    cache = cache or h1_cache(c1h, K.get("zone_max_atr"))
    price = c5[-1]["c"]
    a5 = mtf.atr(c5[-60:]) or 0
    if price <= 0 or a5 <= 0:
        out["why"] = "ATR indisponible"
        return out
    tol = K["tol"] * a5
    ctx_ok, ctx_why, h1s, h4s = allowed(cache["mc"], side, K.get("ctx_mode", 1))
    out["ctx_ok"], out["h1"], out["h4"] = ctx_ok, h1s, h4s
    # (a) zone clé
    sup, res = mtf.nearest_zones(cache["zones"], price)
    z = sup if side == "long" else res
    zs = cache["zones"]      # zone opposée = la plus proche STRICTEMENT au-delà de la zone d entrée (pas la zone dans laquelle on est)
    if side == "long":
        lim = max(price, z["high"]) if z else price
        c_ = [x for x in zs if x["low"] > lim]
        opp = min(c_, key=lambda x: x["low"]) if c_ else None
    else:
        lim = min(price, z["low"]) if z else price
        c_ = [x for x in zs if x["high"] < lim]
        opp = max(c_, key=lambda x: x["high"]) if c_ else None
    zone_ok, zname = False, ""
    if z and z["touches"] >= K["min_touches"] and z["low"] - tol <= price <= z["high"] + tol:
        zone_ok, zname = True, f"zone H1 testée {z['touches']} fois"
    else:
        step = (10 ** math.floor(math.log10(price))) / 2.0
        if step > 0 and abs(price - round(price / step) * step) <= 0.5 * a5:
            zone_ok, zname = True, "chiffre rond"
    # (b) indicateur : RSI extrême ou réaction sur EMA 20 / 50 (15 min)
    ind_ok, iname = False, ""
    rsi5 = _rsi([c["c"] for c in c5[-60:]], 14)
    if rsi5 is not None and ((side == "long" and rsi5 <= K["rsi"]) or (side == "short" and rsi5 >= 100 - K["rsi"])):
        ind_ok, iname = True, f"RSI 5 min {rsi5:.0f}"
    else:
        cl15 = [c["c"] for c in c15]
        last = c5[-1]
        for nm, ma in (("EMA20", mtf.ema(cl15, 20)), ("EMA50", mtf.ema(cl15, 50))):
            if ma is None:
                continue
            lows, highs = min(c["l"] for c in c5[-3:]), max(c["h"] for c in c5[-3:])
            if side == "long" and lows <= ma + 0.3 * a5 and last["c"] > ma and last["c"] > last["o"]:
                ind_ok, iname = True, f"réaction sur {nm} 15 min"
                break
            if side == "short" and highs >= ma - 0.3 * a5 and last["c"] < ma and last["c"] < last["o"]:
                ind_ok, iname = True, f"réaction sur {nm} 15 min"
                break
    # (c) price action
    pa_ok, pname, ext = False, "", None
    nm, ex = mtf.candle_signal(c5[-3:], side)
    if nm:
        near = (z is None) or (z["low"] - 1.5 * a5 <= ex <= z["high"] + 1.5 * a5) or zone_ok
        if near:
            pa_ok, pname, ext = True, nm, ex
    if not pa_ok and base_ok:
        pa_ok, pname = True, "cassure de base 1 min"
    flags = {"zone": zone_ok, "indicateur": ind_ok, "price_action": pa_ok}
    n_conf = sum(1 for v in flags.values() if v)
    out.update({"flags": flags, "n_conf": n_conf, "zone": zname, "indic": iname, "pa": pname, "price": price, "a5": a5})
    # stop / objectif / ratio net de frais
    pad = K["pad"] * a5
    if z and zone_ok and zname.startswith("zone"):
        far = min(z["low"], ext if (ext is not None and side == "long") else z["low"]) if side == "long" else max(z["high"], ext if (ext is not None and side == "short") else z["high"])
        sl_dist = (price - (far - pad)) if side == "long" else ((far + pad) - price)
    else:
        sl_dist = 1.5 * a5
    sl_need = max(sl_dist / price * 100.0, K["sl_min"], K["sl_atr"] * a5 / price * 100.0)      # v4.569 : stop au moins K["sl_atr"] x ATR 5 min (au-dela du bruit)
    too_noisy = sl_need > K["sl_max"] + 1e-9
    sl_pct = min(sl_need, K["sl_max"])
    tp_pct = K["rr"] * sl_pct
    rr_net = (tp_pct - K["fee"]) / (sl_pct + K["fee"])
    room_ok = True
    if opp:
        gap = ((opp["low"] - price) if side == "long" else (price - opp["high"])) / price * 100.0
        room_ok = gap <= 0 or gap >= 0.9 * tp_pct
    out.update({"sl_pct": round(sl_pct, 3), "tp_pct": round(tp_pct, 3), "rr_net": round(rr_net, 2), "room_ok": room_ok})
    why = []
    if not ctx_ok:
        why.append(ctx_why)
    if n_conf < K["min_conf"]:
        why.append(f"confluence {n_conf}/{K['min_conf']} (" + ", ".join(k for k, v in flags.items() if not v) + " manquant)")
    if too_noisy:
        why.append(f"bruit trop grand : le stop doit être de {sl_need:.2f} % (> {K['sl_max']:g} %)")
    if rr_net < K["min_rr"]:
        why.append(f"risque/rendement net {rr_net:.2f} < {K['min_rr']:g}")
    if not room_ok:
        why.append("pas de place avant la zone opposée")
    out["why"] = " ; ".join(why)
    out["ok"] = not why
    return out


def step_trade(tr, bar, hold_bars):
    """Fait avancer un trade virtuel d une bougie 5 min. tr : side, entry, sl_pct, tp_pct, n. Retourne (prix de sortie, motif) ou None. Stop d abord si les deux sont touchés."""
    e = tr["entry"]
    tr["n"] = tr.get("n", 0) + 1
    if tr["side"] == "long":
        sl_p, tp_p = e * (1 - tr["sl_pct"] / 100.0), e * (1 + tr["tp_pct"] / 100.0)
        if bar["o"] <= sl_p:
            return bar["o"], "stop"
        if bar["l"] <= sl_p:
            return sl_p, "stop"
        if bar["h"] >= tp_p:
            return tp_p, "objectif"
    else:
        sl_p, tp_p = e * (1 + tr["sl_pct"] / 100.0), e * (1 - tr["tp_pct"] / 100.0)
        if bar["o"] >= sl_p:
            return bar["o"], "stop"
        if bar["h"] >= sl_p:
            return sl_p, "stop"
        if bar["l"] <= tp_p:
            return tp_p, "objectif"
    if tr["n"] >= hold_bars:
        return bar["c"], "durée"
    return None


def net_pct(side, entry, exit_p, fee):
    g = (exit_p - entry) / entry * 100.0 * (1 if side == "long" else -1)
    return g - fee


# ───────────────────────── Statistiques ─────────────────────────
def wilson(w, n, z=1.96):
    if n <= 0:
        return (0.0, 0.0)
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def stats(trades, usd_per_pct=0.9):
    """trades : liste de dict avec net (en % du notionnel, frais déduits), t (ms). usd_per_pct : 30 $ de marge x levier 3 = 90 $ -> 0,9 $ par %."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    w = [x["net"] for x in trades if x["net"] > 0]
    l = [x["net"] for x in trades if x["net"] <= 0]
    aw, al = (sum(w) / len(w) if w else 0.0), (sum(l) / len(l) if l else 0.0)
    lo, hi = wilson(len(w), n)
    tot = sum(x["net"] for x in trades)
    ts = sorted(x["t"] for x in trades)
    half = ts[n // 2] if n >= 2 else ts[0]
    h1 = [x["net"] for x in trades if x["t"] < half]
    h2 = [x["net"] for x in trades if x["t"] >= half]
    return {"n": n, "wins": len(w), "win_pct": round(100.0 * len(w) / n, 1), "win_ci": [round(100 * lo, 1), round(100 * hi, 1)],
            "avg_win_pct": round(aw, 3), "avg_loss_pct": round(al, 3), "ratio": round(-aw / al, 2) if al < 0 else None,
            "expectancy_pct": round(tot / n, 3), "net_pct": round(tot, 2), "net_usd": round(tot * usd_per_pct, 2),
            "half1": [len(h1), round(sum(h1) / len(h1), 3) if h1 else None], "half2": [len(h2), round(sum(h2) / len(h2), 3) if h2 else None],
            "stops": sum(1 for x in trades if x.get("why") == "stop"), "objectifs": sum(1 for x in trades if x.get("why") == "objectif")}


# ───────────────────────── Backtest ─────────────────────────
def collect_signals(tk, c5, c15, c1h, K, sample_every=12):
    """Parcourt les bougies 5 min et note, à chaque clôture, l évaluation des principes pour les deux sens (sans regarder le futur)."""
    ends15 = [c["t"] + BAR_MS["15m"] for c in c15]
    ends1h = [c["t"] + BAR_MS["1h"] for c in c1h]
    sigs, cache_j, cache = [], -1, None
    for i in range(60, len(c5) - 1):
        T = c5[i]["t"] + BAR_MS["5m"]
        j15 = bisect.bisect_right(ends15, T)
        j1h = bisect.bisect_right(ends1h, T)
        if j15 < 50 or j1h < 60:
            continue
        if j1h != cache_j:
            cache, cache_j = h1_cache(c1h[max(0, j1h - 230):j1h], K.get("zone_max_atr")), j1h
        w5, w15, w1h = c5[max(0, i - 59):i + 1], c15[max(0, j15 - 80):j15], c1h[max(0, j1h - 230):j1h]
        for side in ("long", "short"):
            r = evaluate(side, w5, w15, w1h, K, cache=cache)
            if r["n_conf"] >= 2 and r["ctx_ok"] or i % sample_every == 0:
                sigs.append((tk, i, side, r["n_conf"], r["ctx_ok"], r["rr_net"] >= K["min_rr"] and r["room_ok"], r["sl_pct"], r["tp_pct"]))
    return sigs


def simulate(sigs, c5s, K, accept):
    """accept(sig) -> bool. Une position à la fois par actif, pause de 6 bougies après une sortie. Entrée à l ouverture de la bougie suivante."""
    trades = []
    by = {}
    for s in sigs:
        by.setdefault(s[0], []).append(s)
    for tk, lst in by.items():
        c5 = c5s[tk]
        free_at = 0
        for (_t, i, side, nc, ctx, rr, sl, tp) in sorted(lst, key=lambda x: x[1]):
            if i < free_at or not accept((nc, ctx, rr)):
                continue
            tr = {"side": side, "entry": c5[i + 1]["o"], "sl_pct": sl, "tp_pct": tp}
            j = i + 1
            res = None
            while j < len(c5):
                res = step_trade(tr, c5[j], K["hold_bars"])
                if res:
                    break
                j += 1
            if not res:
                continue
            trades.append({"tk": tk, "side": side, "t": c5[i]["t"], "net": net_pct(side, tr["entry"], res[0], K["fee"]), "why": res[1]})
            free_at = j + 6
    return trades


def run_variants(c5s, sigs, K):
    """Compare : principes complets, 2 critères sur 3, contexte seul, témoin sans filtre (entrées toutes les 12 bougies, sens alterné)."""
    def alt(s):
        return True
    out = {}
    out["principes"] = stats(simulate(sigs, c5s, K, lambda s: s[1] and s[0] >= K["min_conf"] and s[2]))
    out["deux_criteres"] = stats(simulate(sigs, c5s, K, lambda s: s[1] and s[0] >= 2 and s[2]))
    # témoin : un échantillon sur 12 bougies, un sens sur deux
    base = [s for s in sigs if s[1] % 12 == 0 and ((s[1] // 12) % 2 == 0) == (s[2] == "long")]
    out["temoin"] = stats(simulate(base, c5s, K, alt))
    # contexte seul : échantillon sur 12 bougies, contexte respecté, sans exiger les critères
    ctxs = [s for s in sigs if s[1] % 12 == 0 and s[4]]
    out["contexte_seul"] = stats(simulate(ctxs, c5s, K, alt))
    return out


def _err_txt(e):
    """v4.573 : texte lisible d une erreur reseau / API (code HTTP + message), pour comprendre pourquoi un actif est ignore."""
    code = getattr(e, "status_code", None)
    msg = str(getattr(e, "error_message", "") or getattr(e, "args", [""])[-1] or "")[:60]
    return f"{type(e).__name__}" + (f" {code}" if code else "") + (f" : {msg}" if msg else "")


def fetch_candles(info, coin, interval, start_s, end_s, retries=4, sleep=time.sleep):
    """v4.573 : nouvel essai avec attente croissante (2, 4, 8 s) : une limite de requetes (429) ne doit pas faire perdre l actif."""
    raw, last = None, None
    for k in range(max(1, retries)):
        try:
            raw = info.post("/info", {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": int(start_s * 1000), "endTime": int(end_s * 1000)}})
            last = None
            break
        except Exception as e:
            last = e
            if k < retries - 1:
                sleep(2 * (2 ** k))
    if last is not None:
        raise last
    out = []
    if isinstance(raw, list):
        for c in raw:
            try:
                out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    return out


def run_backtest(info, tickers, days, K, progress=None, pause=0.6, sleep=time.sleep):
    """Télécharge 5 min / 15 min / 1 h pour chaque actif (les bougies encore ouvertes sont écartées), puis rejoue les principes.
    v4.573 : pause plus longue, nouvel essai par requete, seconde passe (apres 30 s de repos) pour les actifs en erreur, texte d erreur reel."""
    now = time.time()
    days = max(1, min(float(days), 16.0))
    c5s, sigs, skipped, errs = {}, [], [], {}
    total, done = len(tickers), 0

    def _one(tk):
        c5 = fetch_candles(info, tk, "5m", now - days * 86400, now, sleep=sleep)
        sleep(pause)
        c15 = fetch_candles(info, tk, "15m", now - (days + 4) * 86400, now, sleep=sleep)
        sleep(pause)
        c1h = fetch_candles(info, tk, "1h", now - (days + 12) * 86400, now, sleep=sleep)
        sleep(pause)
        c5 = [c for c in c5 if c["t"] + BAR_MS["5m"] <= now * 1000]
        if len(c5) < 200 or len(c15) < 60 or len(c1h) < 100:
            return "donnees insuffisantes"
        c5s[tk] = c5
        sigs.extend(collect_signals(tk, c5, c15, c1h, K))
        return None

    retry = []
    for tk in tickers:
        try:
            r = _one(tk)
            if r:
                skipped.append(f"{tk} ({r})")
        except Exception as e:      # un actif en erreur ne bloque pas les autres : on le retente apres un repos
            retry.append(tk); errs[tk] = _err_txt(e)
        done += 1
        if progress:
            progress(done, total)
    if retry:
        sleep(30)
        for tk in retry:
            try:
                r = _one(tk)
                if r:
                    skipped.append(f"{tk} ({r})")
            except Exception as e:
                skipped.append(f"{tk} ({_err_txt(e)})")
    res = run_variants(c5s, sigs, K)
    return {"days": days, "tickers": len(c5s), "retried": len(retry), "skipped": skipped, "signals": len(sigs), "variants": res, "ts": time.time(),
            "params": {k: K[k] for k in ("min_conf", "min_touches", "rsi", "rr", "min_rr", "sl_min", "sl_max", "fee", "ctx_mode", "hold_bars", "zone_max_atr", "sl_atr")}}
