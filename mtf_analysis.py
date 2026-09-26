"""
mtf_analysis.py — v4.299 — ANALYSE MULTI-UNITES DE TEMPS (approche \"top-down\").

1. Unite de temps MAJEURE (H4 ou Daily) : tendance de fond et ZONES CLES
   (supports / resistances issus des sommets et creux de marche).
2. Unite de temps INFERIEURE (H1 ou M15) : quand le prix est DANS une zone,
   recherche d une BOUGIE DE RETOURNEMENT (avalement, marteau / etoile
   filante, etoile du matin / du soir) pour entrer avec precision.

Fonctions pures (aucun acces reseau) : les bougies sont des dicts
{\"t\", \"o\", \"h\", \"l\", \"c\", \"v\"} tries du plus ancien au plus recent, et
ne contiennent QUE des bougies cloturees.
"""


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles, period=14):
    """Moyenne des amplitudes vraies (True Range) sur les dernieres bougies."""
    if len(candles) < 2:
        return None
    trs = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else None


def trend(candles, fast=50, slow=200):
    """Tendance de fond : \"haussiere\" si cloture > EMA rapide > EMA lente,
    \"baissiere\" dans le cas inverse, sinon \"neutre\"."""
    closes = [c["c"] for c in candles]
    ef, es = ema(closes, fast), ema(closes, slow)
    if ef is None or es is None:
        return "inconnue", ef, es
    last = closes[-1]
    if last > ef > es:
        return "haussiere", ef, es
    if last < ef < es:
        return "baissiere", ef, es
    return "neutre", ef, es


def find_zones(candles, pivot=2, lookback=120, merge_atr=0.6, min_touches=1):
    """Zones cles : sommets et creux de marche (pivots) regroupes quand ils
    sont proches (a moins de merge_atr x ATR). Chaque zone : bornes basse et
    haute, nombre de contacts. Le role support / resistance depend ensuite de
    la position du prix (un ancien plafond casse devient un plancher)."""
    window = candles[-lookback:]
    a = atr(window) or 0
    levels = []
    for i in range(pivot, len(window) - pivot):
        hi, lo = window[i]["h"], window[i]["l"]
        neigh = [window[j] for j in range(i - pivot, i + pivot + 1) if j != i]
        if all(hi > n["h"] for n in neigh):
            levels.append(hi)
        if all(lo < n["l"] for n in neigh):
            levels.append(lo)
    if not levels or a <= 0:
        return []
    levels.sort()
    zones = [{"low": levels[0], "high": levels[0], "touches": 1}]
    for lv in levels[1:]:
        z = zones[-1]
        if lv - z["high"] <= merge_atr * a:
            z["high"] = lv
            z["touches"] += 1
        else:
            zones.append({"low": lv, "high": lv, "touches": 1})
    # une zone a toujours une epaisseur minimale (un quart d ATR)
    for z in zones:
        if z["high"] - z["low"] < 0.25 * a:
            mid = (z["high"] + z["low"]) / 2
            z["low"], z["high"] = mid - 0.125 * a, mid + 0.125 * a
    return [z for z in zones if z["touches"] >= min_touches]


def nearest_zones(zones, price):
    """(zone support la plus proche SOUS le prix, zone resistance la plus
    proche AU-DESSUS du prix)."""
    below = [z for z in zones if z["low"] <= price]
    above = [z for z in zones if z["high"] >= price]
    support = max(below, key=lambda z: z["high"]) if below else None
    resistance = min(above, key=lambda z: z["low"]) if above else None
    return support, resistance


def in_zone(price, zone, tolerance):
    return zone is not None and zone["low"] - tolerance <= price <= zone["high"] + tolerance


def _body(c):
    return abs(c["c"] - c["o"])


def candle_signal(candles, direction):
    """Signal de retournement sur la DERNIERE bougie cloturee.
    direction \"long\" : avalement haussier, marteau, etoile du matin.
    direction \"short\" : avalement baissier, etoile filante, etoile du soir.
    Retourne (nom, bougie la plus extreme du motif) ou (None, None)."""
    if len(candles) < 3:
        return None, None
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    rng = c3["h"] - c3["l"]
    if rng <= 0:
        return None, None
    body3 = _body(c3)
    upper3 = c3["h"] - max(c3["o"], c3["c"])
    lower3 = min(c3["o"], c3["c"]) - c3["l"]
    if direction == "long":
        if c2["c"] < c2["o"] and c3["c"] > c3["o"] and c3["c"] >= c2["o"] and c3["o"] <= c2["c"] and body3 > _body(c2):
            return "avalement haussier", min(c2["l"], c3["l"])
        if lower3 >= 2 * max(body3, rng * 0.05) and upper3 <= max(body3, rng * 0.1) and (c3["c"] - c3["l"]) / rng >= 0.6:
            return "marteau", c3["l"]
        if (c1["c"] < c1["o"] and _body(c2) <= 0.3 * _body(c1) and c3["c"] > c3["o"]
                and c3["c"] >= (c1["o"] + c1["c"]) / 2):
            return "etoile du matin", min(c1["l"], c2["l"], c3["l"])
    else:
        if c2["c"] > c2["o"] and c3["c"] < c3["o"] and c3["c"] <= c2["o"] and c3["o"] >= c2["c"] and body3 > _body(c2):
            return "avalement baissier", max(c2["h"], c3["h"])
        if upper3 >= 2 * max(body3, rng * 0.05) and lower3 <= max(body3, rng * 0.1) and (c3["h"] - c3["c"]) / rng >= 0.6:
            return "etoile filante", c3["h"]
        if (c1["c"] > c1["o"] and _body(c2) <= 0.3 * _body(c1) and c3["c"] < c3["o"]
                and c3["c"] <= (c1["o"] + c1["c"]) / 2):
            return "etoile du soir", max(c1["h"], c2["h"], c3["h"])
    return None, None


def plan_trade(direction, price, zone, target_zone, pattern_extreme, major_atr, sl_buffer_atr=0.15):
    """SL au-dela de la zone ET de l extreme du motif (+ marge), objectif sur
    la zone opposee. Retourne dict (sl, tp, risk_pct, reward_pct, rr)."""
    buf = major_atr * sl_buffer_atr
    if direction == "long":
        sl = min(zone["low"], pattern_extreme) - buf
        tp = target_zone["low"] if target_zone and target_zone["low"] > price else None
        risk = price - sl
        reward = (tp - price) if tp else None
    else:
        sl = max(zone["high"], pattern_extreme) + buf
        tp = target_zone["high"] if target_zone and target_zone["high"] < price else None
        risk = sl - price
        reward = (price - tp) if tp else None
    if risk <= 0:
        return None
    return {"sl": sl, "tp": tp, "risk_pct": risk / price * 100,
            "reward_pct": (reward / price * 100) if reward else None,
            "rr": (reward / risk) if reward else None}
