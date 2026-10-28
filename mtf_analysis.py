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


def find_zones(candles, pivot=2, lookback=120, merge_atr=0.6, min_touches=1, max_width_atr=None):
    """Zones cles : sommets et creux de marche (pivots) regroupes quand ils
    sont proches (a moins de merge_atr x ATR). Chaque zone : bornes basse et
    haute, nombre de contacts. Le role support / resistance depend ensuite de
    la position du prix (un ancien plafond casse devient un plancher).
    v4.357 — max_width_atr (optionnel) : largeur MAXIMALE d une zone, en ATR. Le regroupement se fait de proche en
    proche : sur une longue consolidation, des dizaines de pivots s enchainent et forment UNE zone de 40 a 50 % du
    prix ("zone unique", "prix DANS le support" quasi permanent, aucune zone opposee). Avec la limite, une zone
    n absorbe plus un pivot qui la ferait depasser cette largeur : il ouvre une nouvelle zone. None = comportement
    d origine, inchange (intraday)."""
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
    max_w = (max_width_atr * a) if max_width_atr else None
    for lv in levels[1:]:
        z = zones[-1]
        if lv - z["high"] <= merge_atr * a and (max_w is None or lv - z["low"] <= max_w):
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


def _pivots(candles, kind, left=2, right=1):
    """v4.352 — indices des pivots : creux (kind "low") ou sommets (kind "high") locaux, strictement plus
    bas / plus haut que `left` bougies avant et au plus egal a `right` bougies apres (confirme 1 bougie plus tard)."""
    out = []
    for i in range(left, len(candles) - right):
        if kind == "low":
            v = candles[i]["l"]
            ok = all(v < candles[i - k]["l"] for k in range(1, left + 1)) and all(v <= candles[i + k]["l"] for k in range(1, right + 1))
        else:
            v = candles[i]["h"]
            ok = all(v > candles[i - k]["h"] for k in range(1, left + 1)) and all(v >= candles[i + k]["h"] for k in range(1, right + 1))
        if ok:
            out.append(i)
    return out


def double_pattern(candles, direction, tol_atr=0.35, min_gap=8, max_gap=45, lookback=70, max_age=3, min_depth_atr=1.5, min_body_atr=0.5):
    """v4.352 — SUR DEMANDE EXPLICITE (Swing) : figure de retournement a DEUX POINTS, sur la DERNIERE bougie cloturee.
      "long"  : double creux — deux creux au meme niveau (ecart <= tol_atr x ATR), separes de min_gap a max_gap bougies,
                avec une ligne de cou au moins min_depth_atr x ATR AU-DESSUS des creux (figure reelle, pas du bruit),
                le second recent (<= max_age bougies) ; confirme quand la derniere bougie est haussiere, cloture
                au-dessus du plus haut de la bougie du second creux, et reste SOUS la ligne de cou (le plus haut entre les
                deux creux : il reste de la place jusqu a la cible).
      "short" : double sommet, symetrique.
    Retourne (nom, extreme de la figure) ou (None, None). Aucune information du futur."""
    if len(candles) < 20:
        return None, None
    c = candles[-lookback:]
    a = atr(c) or 0
    if a <= 0:
        return None, None
    n, last = len(c), c[-1]
    kind = "low" if direction == "long" else "high"
    key = "l" if direction == "long" else "h"
    piv = _pivots(c, kind)
    if len(piv) < 2:
        return None, None
    i2 = piv[-1]
    if n - 1 - i2 > max_age or i2 >= n - 1:      # le second point doit etre recent ET deja confirme
        return None, None
    for i1 in reversed(piv[:-1]):
        gap = i2 - i1
        if gap < min_gap:
            continue
        if gap > max_gap:
            break
        v1, v2 = c[i1][key], c[i2][key]
        if abs(v1 - v2) > tol_atr * a:
            continue
        seg = c[i1:i2 + 1]
        if direction == "long":
            neck = max(x["h"] for x in seg)
            if neck - max(v1, v2) < min_depth_atr * a:
                continue
            rng = last["h"] - last["l"]
            strong = (last["c"] - last["o"]) >= min_body_atr * a and rng > 0 and (last["c"] - last["l"]) / rng >= 0.6
            confirmed = strong and last["c"] > c[i2]["h"] and last["c"] < neck
            extreme, label = min(v1, v2), "double creux"
        else:
            neck = min(x["l"] for x in seg)
            if min(v1, v2) - neck < min_depth_atr * a:
                continue
            rng = last["h"] - last["l"]
            strong = (last["o"] - last["c"]) >= min_body_atr * a and rng > 0 and (last["h"] - last["c"]) / rng >= 0.6
            confirmed = strong and last["c"] < c[i2]["l"] and last["c"] > neck
            extreme, label = max(v1, v2), "double sommet"
        if confirmed:
            return f"{label} ({v1:.6g} / {v2:.6g}, ecart {abs(v1 - v2) / a:.2f} ATR, {gap} bougies)", extreme
        return None, None                            # la paire la plus proche n est pas confirmee
    return None, None


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


def _linreg(ys):
    """(pente par barre, R2) de la regression lineaire de ys sur 0..n-1."""
    n = len(ys)
    if n < 3:
        return None, None
    mx, my = (n - 1) / 2.0, sum(ys) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0:
        return None, None
    b = sxy / sxx
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    return b, r2


def slope_features(candles, n=36, lookback=48, piv=2):
    """v4.489 — MESURE SEULE (aucune decision n en depend) : pente de la tendance et lignes de support / resistance inclinees.
    - pente_atr   : pente de la regression des clotures sur les n dernieres bougies, en multiples d ATR par bougie ;
    - pente_pct   : meme pente en % du prix par bougie (1 h : % par heure) ;
    - pente_r2    : qualite de la tendance (1 = droite parfaite, 0 = bruit) ;
    - lig_sup_dist_pct / lig_sup_pente_pct : ecart du prix a la ligne de support (par les deux derniers creux) et sa pente (% prix / bougie) ;
    - lig_res_dist_pct / lig_res_pente_pct : idem pour la resistance (deux derniers sommets) ;
    - canal_pct   : ecart resistance - support au prix actuel, en % du prix (si les deux lignes existent).
    Valeurs None quand les donnees manquent."""
    out = {k: None for k in ("pente_atr", "pente_pct", "pente_r2", "lig_sup_dist_pct", "lig_sup_pente_pct", "lig_res_dist_pct", "lig_res_pente_pct", "canal_pct")}
    try:
        if not candles or len(candles) < max(n, 12):
            return out
        last = float(candles[-1]["c"])
        if last <= 0:
            return out
        closes = [float(c["c"]) for c in candles[-n:]]
        b, r2 = _linreg(closes)
        a = atr(candles[-(n + 1):])
        if b is not None:
            out["pente_pct"] = round(b / last * 100, 4)
            out["pente_r2"] = round(r2, 3)
            if a:
                out["pente_atr"] = round(b / a, 4)
        w = candles[-lookback:]
        L = len(w)
        lows, highs = [], []
        for i in range(piv, L - piv):
            neigh = [w[j] for j in range(i - piv, i + piv + 1) if j != i]
            if all(float(w[i]["l"]) < float(x["l"]) for x in neigh):
                lows.append((i, float(w[i]["l"])))
            if all(float(w[i]["h"]) > float(x["h"]) for x in neigh):
                highs.append((i, float(w[i]["h"])))
        def line(pts):
            if len(pts) < 2:
                return None
            (i1, y1), (i2, y2) = pts[-2], pts[-1]
            if i2 == i1:
                return None
            m = (y2 - y1) / (i2 - i1)
            return m, y2 + m * ((L - 1) - i2)       # pente par bougie, valeur projetee sur la derniere bougie
        sl_, rl_ = line(lows), line(highs)
        if sl_:
            out["lig_sup_dist_pct"] = round((last - sl_[1]) / last * 100, 3)
            out["lig_sup_pente_pct"] = round(sl_[0] / last * 100, 4)
        if rl_:
            out["lig_res_dist_pct"] = round((rl_[1] - last) / last * 100, 3)
            out["lig_res_pente_pct"] = round(rl_[0] / last * 100, 4)
        if sl_ and rl_:
            out["canal_pct"] = round((rl_[1] - sl_[1]) / last * 100, 3)
    except Exception:
        pass
    return out
