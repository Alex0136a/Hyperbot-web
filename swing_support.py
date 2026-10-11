"""v4.362 — SWING SUPPORT (paper) : logique pure, sans acces reseau ni base.

Hypothese testee : en tendance Daily haussiere, un LONG pris pres d un support (retest tenu sur les dernieres bougies 1 h,
ou balayage de liquidite sous le support suivi d une reprise) a de bonnes chances d atteindre un objectif proche (2 %) ;
le stop large (5 %) n est qu un filet : la protection est une SORTIE ANTICIPEE quand le support casse (cloture 1 h sous la
zone) ET que le flux de transactions est hostile. Ce module ne decide rien tout seul : le moteur l appelle, et le journal
mesure ensuite si les sorties anticipees etaient justifiees (le prix aurait-il atteint l objectif / le stop ?).
"""
import mtf_analysis as mtf

HOUR_MS = 3_600_000


def weekly_trend(daily, fast=8, slow=21):
    """Tendance hebdomadaire deduite des bougies Daily regroupees par 7 (en partant de la plus recente).
    Retourne 'haussiere' / 'baissiere' / 'neutre', ou None si l historique est trop court."""
    if not daily or len(daily) < slow * 7:
        return None
    closes = [daily[i]["c"] for i in range(len(daily) - 1, -1, -7)][::-1]
    label, _, _ = mtf.trend([{"c": c} for c in closes], fast=fast, slow=slow)
    return label if label in ("haussiere", "baissiere", "neutre") else None


def detect_setup(c1h, zone, tol, window=5):
    """Cherche UN des deux declencheurs sur les `window` dernieres bougies 1 h CLOTUREES (la derniere vient de se cloturer).
    Retourne (setup | None, motif). setup = {"kind", "name", "held", "pierce_pct"}.
      - 'balayage' : une des bougies de la fenetre perce sous la zone (meche) mais cloture dedans/au-dessus ; aucune cloture
        sous la zone dans la fenetre ; la derniere cloture est au-dessus du bas de zone.
      - 'retest'   : aucune cloture sous (bas de zone - tolerance) ; la zone a ete touchee ; la derniere bougie est
        haussiere ou forme un creux plus haut que les precedentes."""
    if not zone:
        return None, "pas de zone de support"
    if not c1h or len(c1h) < window + 1:
        return None, f"historique 1h insuffisant ({len(c1h or [])} bougies)"
    w = c1h[-window:]
    lo, hi = zone["low"], zone["high"]
    last = w[-1]
    held = sum(1 for c in w if c["l"] >= lo - tol)
    if any(c["c"] < lo for c in w):
        if any(c["c"] < lo - tol for c in w):
            return None, "cloture 1h sous le support dans les dernieres bougies (cassure, pas un retest)"
    # balayage de liquidite
    if not any(c["c"] < lo for c in w) and last["c"] >= lo:
        for c in w[::-1]:
            if c["l"] < lo and c["c"] >= lo:
                pierce = (lo - c["l"]) / lo * 100
                return {"kind": "balayage", "held": held, "pierce_pct": round(pierce, 3), "inval_low": min(x["l"] for x in w),
                        "name": f"balayage de liquidite sous le support (meche {pierce:.2f} % sous la zone, reprise)"}, None
    # retest tenu
    touched = any(c["l"] <= hi + tol for c in w)
    if not touched:
        return None, "le prix n a pas touche la zone sur les dernieres bougies 1h"
    if last["c"] <= lo:
        return None, "derniere cloture 1h sous le bas de la zone"
    higher_low = last["l"] > min(c["l"] for c in w[:-1])
    if not (last["c"] >= last["o"] or higher_low):
        return None, "derniere bougie 1h baissiere sans creux plus haut (pas de confirmation)"
    return {"kind": "retest", "held": held, "pierce_pct": None, "inval_low": min(x["l"] for x in w),
            "name": f"retest du support tenu ({held}/{window} bougies 1 h au-dessus)"}, None


def invalidation_level(entry, setup, zone_low, mode=1, min_pct=0.5, max_pct=3.0):
    """Niveau dont la cloture 1 h en dessous = setup invalide (sortie anticipee si le flux confirme).
    mode 1 : plus bas du retest (le plus bas des bougies 1 h du signal), borne entre min_pct et max_pct sous l entree
             (ni hypersensible, ni plus bas que le stop) ; mode 0 : bas de la zone Daily (ancien comportement)."""
    if mode == 0 or not setup or setup.get("inval_low") is None:
        return zone_low
    hi = entry * (1 - min_pct / 100)
    lo = entry * (1 - max_pct / 100)
    return max(min(setup["inval_low"], hi), lo)


def zone_broken_1h(c1h, zone_low, opened_ts, buf):
    """Derniere bougie 1 h cloturee APRES l ouverture, avec cloture sous (bas de zone - tampon) ?"""
    if not c1h:
        return False
    c = c1h[-1]
    return (c["t"] / 1000 + 3600) > opened_ts and c["c"] < zone_low - buf


def post_exit_metrics(side, ref_px, candles_1h, start_ms):
    """Evolution (en %, dans le sens du trade) par rapport a ref_px, 1 h / 4 h / 24 h apres start_ms (clotures 1 h)."""
    sgn = 1 if side == "long" else -1
    out = {"fin1h": None, "fin4h": None, "fin24h": None}
    if not ref_px:
        return out
    cs = sorted((c for c in candles_1h if c["t"] >= start_ms), key=lambda c: c["t"])
    if not cs:
        return out
    by_t = {c["t"]: c for c in cs}
    t0 = cs[0]["t"]            # premiere bougie 1 h entierement posterieure a start_ms (decalage < 1 h)
    for key, h in (("fin1h", 1), ("fin4h", 4), ("fin24h", 24)):
        c = by_t.get(t0 + (h - 1) * HOUR_MS)
        if c is not None:
            out[key] = round(sgn * (c["c"] / ref_px - 1) * 100, 4)
    return out


def scaled_levels(lev, base_sl, base_tp, margin_risk_pct=7.5, min_sl=1.5, tp1=2.0, tp2=1.5, tp3=1.0):
    """v4.366 — stop et objectif (en % du PRIX) selon le levier. Stop = margin_risk_pct / levier, plafonne a base_sl et plancher a min_sl
    (la perte au stop reste ~constante en % de la marge, et la liquidation reste loin). Objectif : tp1 a x1, tp2 a x2, tp3 a x3 et plus.
    Retourne (sl_pct, tp_pct)."""
    lev = max(int(lev or 1), 1)
    sl = min(max(margin_risk_pct / lev, min_sl), base_sl)
    tp = tp1 if lev <= 1 else (tp2 if lev == 2 else tp3)
    return round(sl, 4), round(tp, 4)


def quality_score(daily_trend, weekly, btc, h4_up, touches, flow, funding_ann):
    """v4.378 — note de QUALITE d un setup (0-100), sert UNIQUEMENT a filtrer les alertes (aucun trade n en depend).
    Barème : Daily 20 (repli en tendance : 10) · Weekly 15 (neutre 7) · BTC 15 (neutre 7) · H4 haussier 10 · contacts de la zone 15/10/5
    · flux acheteur 15 (>= +0,2) ou 9 (>= 0) (inconnu : 7) · funding <= 25 %/an 10 (<= 60 : 4) (inconnu : 5). Non calibree sur des resultats."""
    parts, tot = [], 0
    d = 20 if daily_trend == "haussiere" else 10
    tot += d; parts.append("Daily " + ("✓" if d == 20 else "repli"))
    for lbl, tr in (("Weekly", weekly), ("BTC", btc)):
        pts = 15 if tr == "haussiere" else (7 if tr == "neutre" else 0)
        tot += pts; parts.append(f"{lbl} " + ("✓" if pts == 15 else ("~" if pts else "✗")))
    tot += 10 if h4_up else 0; parts.append("H4 " + ("✓" if h4_up else "✗"))
    t = int(touches or 1)
    tot += 15 if t >= 3 else (10 if t == 2 else 5); parts.append(f"{t} contact{'s' if t > 1 else ''}")
    if flow is None:
        tot += 7; parts.append("flux ?")
    else:
        tot += 15 if flow >= 0.2 else (9 if flow >= 0 else 0); parts.append(f"flux {flow:+.2f}")
    if funding_ann is None:
        tot += 5; parts.append("funding ?")
    else:
        tot += 10 if funding_ann <= 25 else (4 if funding_ann <= 60 else 0); parts.append(f"funding {funding_ann:.0f} %/an")
    return int(tot), " · ".join(parts)
