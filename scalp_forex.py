"""v4.353 — SCALP FOREX (sous-mode du mode Forex, PAPER).

Sur demande : un scalping qui cohabite avec le mode Forex actuel SANS le modifier.
  * niveaux "institutionnels" cartographies sur bougies 1H : plus haut / plus bas de la veille et de la
    semaine, du jour, ouvertures de seance (Asie 00h, Londres 07h, New York 13h UTC), nombres ronds,
    zones 1H a plusieurs contacts ;
  * structure locale 15 min : biais haussier / baissier / range ;
  * declencheur 5 min (rejet d un niveau, ou cassure puis retest) confirme par la bougie 1 min ;
  * entree et sortie rapides : stop juste au-dela du niveau, objectif = niveau suivant, breakeven,
    sortie au temps, sortie si le niveau est casse ou si le flux se retourne ;
  * ECONOMIE EXPLICITE : un setup n est pris que si, FRAIS ET SPREAD DEDUITS, il garde un gain/risque
    positif (FOREX_SCALP_MIN_NET_RR) — sinon il est journalise "filtre : frais trop lourds" ;
  * remplissages PRUDENTS en paper (demi-spread + glissement a l entree ET a la sortie).

Fonctions pures (aucun acces reseau) : testables seules. Le branchement au moteur est dans bot_engine.py.
"""
import math
import statistics

import mtf_analysis as mtf

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
SESSION_OPENS_UTC = (("Asie", 0), ("Londres", 7), ("New York", 13))
DEFAULT_ROUND_STEPS = {"xyz:EUR": 0.005, "xyz:JPY": 0.5, "xyz:KRW": 5.0, "xyz:DXY": 0.25, "PAXG": 10.0}


# ───────────────────────────── outils ─────────────────────────────
def atr(candles, period=14):
    if len(candles) < 3:
        return 0.0
    c = candles[-(period + 1):]
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"])) for i in range(1, len(c))]
    return sum(trs) / len(trs) if trs else 0.0


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def round_step_for(ticker, price, overrides=None):
    steps = dict(DEFAULT_ROUND_STEPS)
    if overrides:
        steps.update(overrides)
    if ticker in steps:
        return steps[ticker]
    if price <= 0:
        return 0.0
    mag = 10 ** math.floor(math.log10(price))
    return mag / 20 if price >= 1 else mag / 10


def fill_price(side, mid, spread_pct, slip_pct, action):
    """Prix de remplissage PRUDENT en paper. action : "entry" ou "exit".
    entree long / sortie short : on ACHETE (ask + glissement) ; entree short / sortie long : on VEND (bid - glissement)."""
    adverse = (spread_pct / 2 + slip_pct) / 100
    buys = (side == "long") == (action == "entry")
    return mid * (1 + adverse) if buys else mid * (1 - adverse)


# ───────────────────────────── niveaux ─────────────────────────────
def build_levels(h1, now_ms, price, round_step=0.0, merge_pct=0.03):
    """Niveaux utiles autour du prix, issus des bougies 1H cloturees. Retourne une liste triee
    [{"p", "kinds", "w"}] ou w = nombre de sources qui se superposent (confluence)."""
    raw = []
    if not h1:
        return raw
    day0 = now_ms // DAY_MS * DAY_MS
    prev = [c for c in h1 if day0 - DAY_MS <= c["t"] < day0]
    if len(prev) >= 12:
        raw += [(max(c["h"] for c in prev), "PDH"), (min(c["l"] for c in prev), "PDL")]
    week = [c for c in h1 if day0 - 5 * DAY_MS <= c["t"] < day0]
    if len(week) >= 48:
        raw += [(max(c["h"] for c in week), "PWH"), (min(c["l"] for c in week), "PWL")]
    today = [c for c in h1 if c["t"] >= day0]
    if len(today) >= 3:
        raw += [(max(c["h"] for c in today), "DH"), (min(c["l"] for c in today), "DL")]
    for name, hr in SESSION_OPENS_UTC:
        start = day0 + hr * HOUR_MS
        for c in h1:
            if c["t"] == start:
                raw.append((c["o"], f"open {name}"))
                break
    try:
        for z in mtf.find_zones(h1, lookback=120):
            if z.get("touches", 0) >= 2:
                raw.append(((z["low"] + z["high"]) / 2, f"zone 1H x{z['touches']}"))
    except Exception:
        pass
    if round_step and price > 0:
        base = round(price / round_step)
        for k in range(-4, 5):
            raw.append(((base + k) * round_step, "rond"))
    lo, hi = price * 0.985, price * 1.015
    raw = sorted((p, k) for p, k in raw if lo <= p <= hi)
    out = []
    for p, k in raw:
        if out and abs(p - out[-1]["p"]) / price * 100 <= merge_pct:
            lvl = out[-1]
            lvl["p"] = (lvl["p"] * lvl["w"] + p) / (lvl["w"] + 1)
            lvl["w"] += 1
            if k not in lvl["kinds"]:
                lvl["kinds"].append(k)
        else:
            out.append({"p": p, "kinds": [k], "w": 1})
    return out


def bias_15m(m15, price, slope_atr=0.15):
    """Structure locale 15 min : ("up" | "down" | "range", details)."""
    if len(m15) < 30:
        return "range", {"why": "historique 15 min insuffisant"}
    closes = [c["c"] for c in m15]
    e_now, e_prev = ema(closes, 20), ema(closes[:-4], 20)
    a = atr(m15) or 0
    if e_now is None or e_prev is None or a <= 0:
        return "range", {"why": "donnees insuffisantes"}
    slope = (e_now - e_prev) / a
    if price > e_now and slope >= slope_atr:
        return "up", {"slope_atr": round(slope, 2), "ema20": e_now}
    if price < e_now and slope <= -slope_atr:
        return "down", {"slope_atr": round(slope, 2), "ema20": e_now}
    return "range", {"slope_atr": round(slope, 2), "ema20": e_now}


# ───────────────────────────── declencheurs ─────────────────────────────
def is_rejection(c, side, atr_ref, min_range_atr=0.5):
    """Bougie de rejet : longue meche vers le niveau et cloture du bon cote de la bougie."""
    rng = c["h"] - c["l"]
    if rng <= 0 or atr_ref <= 0 or rng < min_range_atr * atr_ref:
        return False
    if side == "long":
        return (min(c["o"], c["c"]) - c["l"]) / rng >= 0.45 and (c["c"] - c["l"]) / rng >= 0.55
    return (c["h"] - max(c["o"], c["c"])) / rng >= 0.45 and (c["h"] - c["c"]) / rng >= 0.55


def _plan(side, entry, level, c5, levels, spread_pct, atr5, cfg):
    """Stop au-dela du niveau / de la meche, objectif au niveau suivant ; refuse si les frais mangent le gain."""
    spread_abs = spread_pct / 100 * entry
    sl_buf = max(cfg.get("FOREX_SCALP_SL_ATR", 0.5) * atr5, 2 * spread_abs)
    if side == "long":
        sl = min(c5["l"], level["p"]) - sl_buf
        risk = entry - sl
    else:
        sl = max(c5["h"], level["p"]) + sl_buf
        risk = sl - entry
    if risk <= 0:
        return None, "stop invalide"
    risk_pct = risk / entry * 100
    if risk_pct < cfg.get("FOREX_SCALP_MIN_RISK_PCT", 0.06):
        return None, f"stop trop serre ({risk_pct:.3f} % < {cfg.get('FOREX_SCALP_MIN_RISK_PCT', 0.06)} %) face au spread"
    if risk_pct > cfg.get("FOREX_SCALP_MAX_RISK_PCT", 0.30):
        return None, f"stop trop large ({risk_pct:.3f} % > {cfg.get('FOREX_SCALP_MAX_RISK_PCT', 0.30)} %)"
    cost = cfg.get("FOREX_SCALP_FEE_PCT", 0.089) + spread_pct
    min_tp = max(cfg.get("FOREX_SCALP_MIN_TARGET_PCT", 0.15), cfg.get("FOREX_SCALP_RR", 1.2) * risk_pct * 0.7)
    tp, tp_kind = None, None
    for lv in (sorted(levels, key=lambda x: x["p"]) if side == "long" else sorted(levels, key=lambda x: -x["p"])):
        dist = (lv["p"] - entry) / entry * 100 if side == "long" else (entry - lv["p"]) / entry * 100
        if dist >= min_tp:
            tp, tp_kind = lv["p"], "niveau " + "+".join(lv["kinds"])
            break
    if tp is None:
        dist_syn = cfg.get("FOREX_SCALP_RR", 1.2) * risk_pct
        if dist_syn >= cfg.get("FOREX_SCALP_MIN_TARGET_PCT", 0.15):
            tp = entry * (1 + dist_syn / 100) if side == "long" else entry * (1 - dist_syn / 100)
            tp_kind = f"synthetique {cfg.get('FOREX_SCALP_RR', 1.2)} R"
        else:
            return None, f"objectif trop proche ({dist_syn:.3f} % < {cfg.get('FOREX_SCALP_MIN_TARGET_PCT', 0.15)} %)"
    tp_pct = abs(tp - entry) / entry * 100
    net_rr = (tp_pct - cost) / (risk_pct + cost)
    if net_rr < cfg.get("FOREX_SCALP_MIN_NET_RR", 0.3):
        return None, (f"frais trop lourds : objectif {tp_pct:.3f} % - frais+spread {cost:.3f} % = {tp_pct - cost:+.3f} % "
                      f"pour un risque de {risk_pct:.3f} % (gain/risque net {net_rr:.2f} < {cfg.get('FOREX_SCALP_MIN_NET_RR', 0.3)})")
    return {"sl": sl, "tp": tp, "tp_kind": tp_kind, "risk_pct": risk_pct, "tp_pct": tp_pct,
            "rr": tp_pct / risk_pct, "net_rr": net_rr, "cost_pct": cost}, None


def find_setup(ctx):
    """Cherche UN setup. ctx : price, spread_pct, m5, m1, levels, bias, now (s), cfg.
    Retourne (candidat | None, raison_du_refus | None, setup_detecte (bool), info)."""
    cfg, price, m5, m1 = ctx["cfg"], ctx["price"], ctx["m5"], ctx.get("m1") or []
    spread_pct, levels, bias, now = ctx["spread_pct"], ctx["levels"], ctx["bias"], ctx["now"]
    if len(m5) < 20 or price <= 0:
        return None, "historique 5 min insuffisant", False, {}
    atr5 = atr(m5)
    if atr5 <= 0:
        return None, "ATR 5 min nul", False, {}
    c5 = m5[-1]
    age = now - (c5["t"] / 1000 + 300)
    max_age = cfg.get("FOREX_SCALP_MAX_SIGNAL_AGE_SEC", 150)
    spread_abs = spread_pct / 100 * price
    tol = max(cfg.get("FOREX_SCALP_TOUCH_ATR", 0.6) * atr5, 1.5 * spread_abs)
    buf = 0.3 * atr5
    counter_w = cfg.get("FOREX_SCALP_COUNTER_BIAS_MIN_W", 2)
    best, refused, detected = None, None, False
    for side in ("long", "short"):
        for lv in levels:
            lp = lv["p"]
            if side == "long":
                touched = lp <= c5["c"] + tol and abs(c5["l"] - lp) <= tol and c5["c"] > lp
            else:
                touched = lp >= c5["c"] - tol and abs(c5["h"] - lp) <= tol and c5["c"] < lp
            if not (touched and is_rejection(c5, side, atr5)):
                continue
            # cassure puis retest : une cloture 5 min au-dela du niveau dans les 8 dernieres bougies, retest maintenant
            recent = m5[-9:-1]
            if side == "long":
                broke = any(recent[i]["c"] > lp + buf and recent[i - 1]["c"] <= lp for i in range(1, len(recent)))
            else:
                broke = any(recent[i]["c"] < lp - buf and recent[i - 1]["c"] >= lp for i in range(1, len(recent)))
            aligned = (side == "long" and bias == "up") or (side == "short" and bias == "down")
            against = (side == "long" and bias == "down") or (side == "short" and bias == "up")
            setup = "cassure + retest" if (broke and aligned) else "rejet de niveau"
            detected = True
            relaxed = {**cfg, "FOREX_SCALP_MIN_NET_RR": -9, "FOREX_SCALP_MIN_RISK_PCT": 0.0, "FOREX_SCALP_MAX_RISK_PCT": 9.0, "FOREX_SCALP_MIN_TARGET_PCT": 0.0}
            hyp, _w = _plan(side, price, lv, c5, levels, spread_pct, atr5, relaxed)     # plan "pour voir" : alimente le journal des refus
            info = {"side": side, "setup": setup, "level": lv, "bias": bias, "hyp": hyp, "c5": c5, "atr5": atr5}
            if age > max_age:
                refused = ("filtre : signal 5 min trop ancien", info)
                continue
            if setup == "rejet de niveau" and against and lv["w"] < counter_w:
                refused = (f"filtre : rejet contre le biais 15 min ({bias}) sur un niveau sans confluence", info)
                continue
            if cfg.get("FOREX_SCALP_REQUIRE_1M", 1):
                if not m1:
                    refused = ("filtre : bougies 1 min indisponibles", info)
                    continue
                c1 = m1[-1]
                ok1 = (c1["c"] > c1["o"] and price >= c5["c"]) if side == "long" else (c1["c"] < c1["o"] and price <= c5["c"])
                if not ok1:
                    refused = ("filtre : pas de confirmation 1 min", info)
                    continue
            plan, why = _plan(side, price, lv, c5, levels, spread_pct, atr5, cfg)
            if plan is None:
                refused = ("filtre : " + why, info)
                continue
            cand = {**info, **plan, "atr5": atr5, "age": age, "w": lv["w"], "c5": c5}
            if best is None or cand["net_rr"] > best["net_rr"]:
                best = cand
    if best:
        return best, None, True, {"side": best["side"], "setup": best["setup"], "level": best["level"], "bias": bias}
    if refused:
        return None, refused[0], True, refused[1]
    return None, "aucun setup (pas de rejet ni de retest sur un niveau)", False, {}


# ───────────────────────────── gestion ─────────────────────────────
def manage(pos, mid, now, m5, flow, cfg):
    """Decision pour une position scalp ouverte. pos : type, entry, sl, tp + pos["scalp"] (r, level, opened_ts, be_done, best).
    Retourne {"exit": raison | None, "new_sl": float | None}."""
    sc = pos["scalp"]
    long_side = pos["type"] == "long"
    entry, r = pos["entry"], sc["r"]
    sc["best"] = max(sc.get("best", entry), mid) if long_side else min(sc.get("best", entry), mid)
    gain = (mid - entry) if long_side else (entry - mid)
    best_gain = (sc["best"] - entry) if long_side else (entry - sc["best"])
    if (long_side and mid <= pos["sl"]) or ((not long_side) and mid >= pos["sl"]):
        return {"exit": "SCALP STOP (breakeven/verrou)" if sc.get("be_done") else "SCALP STOP", "new_sl": None}
    if (long_side and mid >= pos["tp"]) or ((not long_side) and mid <= pos["tp"]):
        return {"exit": "SCALP OBJECTIF", "new_sl": None}
    if now - sc["opened_ts"] >= cfg.get("FOREX_SCALP_MAX_HOLD_MIN", 20) * 60:
        return {"exit": "SCALP TEMPS MAX", "new_sl": None}
    if m5:
        c = m5[-1]
        buf = 0.3 * atr(m5)
        if c["t"] / 1000 + 300 > sc["opened_ts"]:      # uniquement une bougie 5 min cloturee APRES l entree
            if (long_side and c["c"] < sc["level"] - buf) or ((not long_side) and c["c"] > sc["level"] + buf):
                return {"exit": "SCALP NIVEAU CASSE", "new_sl": None}
    if flow is not None and r > 0 and gain >= cfg.get("FOREX_SCALP_FLOW_EXIT_MIN_GAIN_R", 0.3) * r:
        thr = cfg.get("FOREX_SCALP_FLOW_EXIT", 0.5)
        if (long_side and flow <= -thr) or ((not long_side) and flow >= thr):
            return {"exit": "SCALP FLUX CONTRAIRE", "new_sl": None}
    new_sl = None
    if r > 0 and not sc.get("be_done") and best_gain >= cfg.get("FOREX_SCALP_BE_R", 0.6) * r:
        # breakeven NET de frais, mais jamais au-dela de la moitie du gain deja acquis : sinon le stop se
        # retrouverait AU-DESSUS du prix (frais plus grands que le risque) et fermerait aussitot en perte
        pad = min(entry * sc.get("cost_pct", 0.1) / 100, 0.5 * best_gain)
        new_sl = entry + pad if long_side else entry - pad
        sc["be_done"] = True
    if r > 0 and sc.get("be_done") and best_gain >= 1.0 * r:
        lock = entry + 0.5 * best_gain if long_side else entry - 0.5 * best_gain
        cur = new_sl if new_sl is not None else pos["sl"]
        if (long_side and lock > cur) or ((not long_side) and lock < cur):
            new_sl = lock
    if new_sl is not None and ((long_side and new_sl <= pos["sl"]) or ((not long_side) and new_sl >= pos["sl"])):
        new_sl = None
    return {"exit": None, "new_sl": new_sl}


# ───────────────────────────── journal : issue ─────────────────────────────
def evaluate_outcome(side, entry, sl, tp, candles_1m, start_ms, horizon_min=60):
    """Ce qu'aurait donne un setup, sur les bougies 1 min qui suivent. Meme bougie SL et TP : SL d abord (prudence)."""
    sgn = 1 if side == "long" else -1
    cs = [c for c in candles_1m if start_ms <= c["t"] < start_ms + horizon_min * 60_000]
    if not cs:
        return None
    out = {"outcome": "none", "hit_min": None}
    mfe = {5: 0.0, 15: 0.0, 60: 0.0}
    mae = {5: 0.0, 15: 0.0, 60: 0.0}
    hit = False
    for c in cs:
        minute = (c["t"] - start_ms) / 60_000 + 1
        fav = (c["h"] / entry - 1) * 100 if sgn > 0 else (1 - c["l"] / entry) * 100
        adv = (1 - c["l"] / entry) * 100 if sgn > 0 else (c["h"] / entry - 1) * 100
        for w in (5, 15, 60):
            if minute <= w:
                mfe[w] = max(mfe[w], fav)
                mae[w] = max(mae[w], adv)
        if not hit:
            sl_hit = (c["l"] <= sl) if sgn > 0 else (c["h"] >= sl)
            tp_hit = (c["h"] >= tp) if sgn > 0 else (c["l"] <= tp)
            if sl_hit:
                out["outcome"], out["hit_min"], hit = "sl", minute, True
            elif tp_hit:
                out["outcome"], out["hit_min"], hit = "tp", minute, True

    def fin(w):
        sub = [c for c in cs if (c["t"] - start_ms) / 60_000 + 1 <= w]
        if not sub:
            return None
        last = sub[-1]["c"]
        return sgn * (last / entry - 1) * 100
    out.update({"mfe5": round(mfe[5], 4), "mae5": round(mae[5], 4), "mfe15": round(mfe[15], 4), "mae15": round(mae[15], 4),
                "mfe60": round(mfe[60], 4), "mae60": round(mae[60], 4),
                "fin15": None if fin(15) is None else round(fin(15), 4), "fin60": None if fin(60) is None else round(fin(60), 4)})
    return out
