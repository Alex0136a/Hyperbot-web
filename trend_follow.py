"""v4.365 — TENDANCE CONFIRMEE (module separe, paper) : logique pure, sans acces reseau ni base.

Principe : on entre quand la tendance est CONFIRMEE (Daily ET H4 dans le meme sens, momentum 1 h et flux dans le meme sens), et on
reste tant qu elle tient. Pas d objectif fixe : un stop suiveur (multiple de l ATR H4) protege, et la position se ferme quand la
tendance cesse (Daily perdue sur une bougie cloturee, ou H4 retournee). Le moteur appelle ces fonctions ; le journal mesure ce que
valent les entrees, notamment selon leur RETARD (distance au prix moyen Daily, en ATR).
"""
import mtf_analysis as mtf

HOUR_MS = 3_600_000


def want_trend(side):
    return "haussiere" if side == "long" else "baissiere"


def confirmed_sides(d1_trend, h4_trend, allow_short=True):
    """Sens confirmes : Daily ET H4 dans le meme sens."""
    out = []
    if d1_trend == "haussiere" and h4_trend == "haussiere":
        out.append("long")
    if allow_short and d1_trend == "baissiere" and h4_trend == "baissiere":
        out.append("short")
    return out


def momentum_ok(c1h, side, period=20):
    """Derniere cloture 1 h du bon cote de sa moyenne mobile exponentielle (momentum dans le sens de la tendance)."""
    if not c1h or len(c1h) < period + 2:
        return False, "historique 1h insuffisant"
    closes = [c["c"] for c in c1h]
    e = mtf.ema(closes, period)
    if e is None:
        return False, "moyenne 1h indisponible"
    last = closes[-1]
    if side == "long":
        return (last > e), (None if last > e else f"cloture 1 h {last:.6g} sous sa moyenne {e:.6g}")
    return (last < e), (None if last < e else f"cloture 1 h {last:.6g} au-dessus de sa moyenne {e:.6g}")


def breakout_ok(c1h, side, n=12):
    """Mode 'cassure' : la derniere cloture 1 h depasse le plus haut (long) / plus bas (short) des n bougies precedentes."""
    if not c1h or len(c1h) < n + 2:
        return False, "historique 1h insuffisant"
    prev = c1h[-(n + 1):-1]
    last = c1h[-1]["c"]
    if side == "long":
        lvl = max(c["h"] for c in prev)
        return (last > lvl), (None if last > lvl else f"pas de cassure : cloture {last:.6g} sous le plus haut des {n} dernieres heures ({lvl:.6g})")
    lvl = min(c["l"] for c in prev)
    return (last < lvl), (None if last < lvl else f"pas de cassure : cloture {last:.6g} au-dessus du plus bas des {n} dernieres heures ({lvl:.6g})")


def extension_atr(side, price, ema_ref, atr_daily):
    """RETARD de l entree : distance du prix a la moyenne Daily, en ATR Daily, dans le sens du trade (grand = mouvement deja avance)."""
    if not atr_daily or ema_ref is None or not price:
        return None
    sgn = 1 if side == "long" else -1
    return round(sgn * (price - ema_ref) / atr_daily, 3)


def stop_distance(entry, atr_h4, mult, min_pct, max_pct):
    """Distance du stop (en prix) : mult x ATR H4, bornee entre min_pct et max_pct de l entree."""
    d = (atr_h4 or 0) * mult
    lo, hi = entry * min_pct / 100, entry * max_pct / 100
    return min(max(d, lo), hi)


def trail_stop(side, best, dist, current_sl):
    """Stop suiveur : ne fait que se rapprocher du prix (monte pour un long, descend pour un short)."""
    new = best - dist if side == "long" else best + dist
    if current_sl is None:
        return new
    return max(current_sl, new) if side == "long" else min(current_sl, new)


def trend_end(side, d1_label, h4_label, h4_reverse_exit=1, h1_label=None, h1_exit=0):
    """La tendance a-t-elle cesse ? Retourne (finie, motif).
    - Daily qui n est plus dans le sens du trade (neutre inclus) : toujours.
    - H4 : mode 1 = franchement retournee ; mode 2 = n est plus dans le sens (neutre inclus) ; 0 = ignoree.
    - H1 (v4.476) : mode 1 = franchement retournee ; mode 2 = n est plus dans le sens (neutre inclus) ; 0 = ignoree."""
    want = want_trend(side)
    opp = "baissiere" if side == "long" else "haussiere"
    known = ("haussiere", "baissiere", "neutre")
    if d1_label is not None and d1_label != want:
        return True, f"TENDANCE FINIE (Daily : {d1_label})"
    m4 = int(h4_reverse_exit or 0)
    if m4 == 1 and h4_label == opp:
        return True, f"TENDANCE FINIE (H4 : {h4_label})"
    if m4 >= 2 and h4_label in known and h4_label != want:
        return True, f"TENDANCE FINIE (H4 : {h4_label})"
    m1 = int(h1_exit or 0)
    if m1 == 1 and h1_label == opp:
        return True, f"TENDANCE FINIE (H1 : {h1_label})"
    if m1 >= 2 and h1_label in known and h1_label != want:
        return True, f"TENDANCE FINIE (H1 : {h1_label})"
    return False, None


def gain_lock(peak_pct, gain_pct, arm_pct, giveback_pct):
    """Gain acquis (v4.476) : une fois le gain maximal >= arm_pct, on sort si le gain retombe de giveback_pct % de ce maximum.
    arm_pct <= 0 : desactive."""
    if not arm_pct or arm_pct <= 0 or peak_pct < arm_pct:
        return False
    return gain_pct <= peak_pct * (1 - max(min(giveback_pct, 100.0), 0.0) / 100.0)
