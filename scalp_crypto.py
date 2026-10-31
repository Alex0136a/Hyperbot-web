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
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import db
import bot_engine as be
import mtf_analysis as mtf
try:
    import macro_events      # v4.507 : pause autour des annonces macro (si le fichier n est pas depose, la pause est simplement inactive)
except Exception:      # pragma: no cover
    macro_events = None

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
        soft = float(K.get("short_btc_soft_max", 0) or 0)      # v4.497 : shorts "prudents" tant que BTC monte moderement (rebond sur resistance testee, taille reduite, flux vendeur exige dans _take)
        keep = []
        for c_ in cands:
            if c_["side"] != "short":
                keep.append(c_)
            elif soft > K.get("short_btc_max", 0.1) and btc_chg <= soft and c_["kind"] == "rebond" and c_["touches"] >= 2:
                c_["prudent_btc"] = round(btc_chg, 3)
                keep.append(c_)
        cands = keep
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


def _pair_corr(a, b, n=24):
    """v4.504 : correlation des rendements 5 min de deux actifs sur les n dernieres bougies (None si indisponible)."""
    try:
        ca = [float(x["c"]) for x in a][-(n + 1):]
        cb = [float(x["c"]) for x in b][-(n + 1):]
        m = min(len(ca), len(cb))
        if m < 8:
            return None
        ca, cb = ca[-m:], cb[-m:]
        ra = [ca[i] / ca[i - 1] - 1 for i in range(1, m)]
        rb = [cb[i] / cb[i - 1] - 1 for i in range(1, m)]
        ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
        da, db_ = [x - ma for x in ra], [x - mb for x in rb]
        sa, sb = sum(x * x for x in da) ** 0.5, sum(x * x for x in db_) ** 0.5
        if sa <= 0 or sb <= 0:
            return None
        return sum(x * y for x, y in zip(da, db_)) / (sa * sb)
    except Exception:
        return None


def cont_eval(c5, c15, c1h, btc_chg, K, flow=None, side=None, follower=False):
    """v4.504 : MODE CONTINUATION. Dans une tendance franche (15 min dans le sens, pente 1 h reguliere dans le sens, H1 pas contraire, mouvement
    net sur 2 h), le bot entre dans le sens de la tendance :
      - « repli » : le prix a fait un repli de PB_MIN a PB_MAX x ATR 5 min depuis le dernier extreme (12 bougies) puis une bougie de reprise
        (cloture au-dela de l extreme de la bougie precedente) ;
      - « elan » : la derniere bougie forte, avec volume, fait un nouvel extreme (aucun repli) — pour ne pas laisser passer un mouvement ;
      - « suiveur » (follower=True, side impose) : actif correle d un trade de continuation : tendance alignee et prix du bon cote de la
        moyenne 9 (5 min), sans exiger son propre repli.
    Garde-fous : prix pas etire (ATR 15 min au-dela de la moyenne 20, RSI), BTC pas contre, flux pas contre, place avant la zone H1 suivante.
    Retourne (signal ou None, motif) ; un motif contenant « refuse » est journalise."""
    if len(c5) < 40 or len(c15) < 30 or len(c1h) < 60:
        return None, "historique insuffisant"
    price = float(c5[-1]["c"])
    a5, a15 = mtf.atr(c5, 14), mtf.atr(c15, 14)
    if not (a5 and a15 and price > 0):
        return None, "ATR indisponible"
    atr15_pct = a15 / price * 100
    if atr15_pct < K["min_atr_pct"]:
        return None, "marche trop calme"
    short = bool(K.get("cont_short_on", 1))
    t1h = mtf.trend(c1h)[0]      # info seulement en mode court terme (n entre plus dans la decision)
    early = False
    if short:
        # v4.510 : le Scalp dure 10 a 90 min -> la tendance se juge sur SON horizon : les bougies 5 min des 3 dernieres heures
        # (pente + regularite de la regression des clotures), confirmee par la derniere heure. Ni EMA 15 min / 1 h, ni pente 12 h.
        nL = max(int(K.get("cont_trend_bars", 36)), 18)
        closes5 = [c["c"] for c in c5]
        if len(closes5) < nL + 1:
            return None, "historique 5 min insuffisant"
        bL, r2 = mtf._linreg(closes5[-nL:])
        bS, r2s = mtf._linreg(closes5[-12:])
        if bL is None or bS is None:
            return None, "pente 5 min indisponible"
        if side is None:
            side = "long" if bL > 0 else "short"
        sg = 1 if side == "long" else -1
        pa = bL / a5
        if sg * bL <= 0:
            return None, f"pente {nL * 5 // 60} h (5 min) contre le sens ({pa:+.2f} ATR 5 min/bougie)"
        r2_need = K.get("cont_min_r2_5m", 0.3) * (0.6 if follower else 1.0)
        if r2 < r2_need:
            return None, f"tendance {nL * 5 // 60} h irrégulière (R² {r2:.2f} < {r2_need:.2f})"
        if sg * bS <= 0:
            return None, f"dernière heure en sens contraire (pente 1 h 5 min {bS / a5:+.2f} ATR/bougie) — pas de continuation"
        m15 = f"5 min {nL * 5 // 60} h"
        ext_ref = mtf.ema(closes5, nL) or price
    else:
        closes15 = [c["c"] for c in c15]
        e20 = mtf.ema(closes15, 20)
        e50 = mtf.ema(closes15, 50) if len(closes15) >= 50 else None
        if not (e20 and e50):
            return None, "historique insuffisant"
        m15 = "haussiere" if (closes15[-1] > e20 > e50) else ("baissiere" if (closes15[-1] < e20 < e50) else "neutre")
        early = False
        if m15 == "neutre" and K.get("cont_early_on", 1):
            # v4.506 : DEBUT de mouvement. L EMA50 15 min (~12 h) n est croisee qu apres une longue baisse/hausse : on accepte aussi
            # « prix du bon cote de l EMA20 15 min ET EMA20 orientee dans le sens depuis 1 h » (la pente 1 h reguliere et le mouvement 2 h restent exiges).
            e20p = mtf.ema(closes15[:-4], 20) if len(closes15) >= 30 else None
            if e20p:
                if closes15[-1] > e20 and e20 > e20p:
                    m15, early = "haussiere", True
                elif closes15[-1] < e20 and e20 < e20p:
                    m15, early = "baissiere", True
        if side is None:
            side = "long" if m15 == "haussiere" else ("short" if m15 == "baissiere" else None)
            if side is None:
                return None, "15 min neutre (pas de tendance)"
        elif m15 != ("haussiere" if side == "long" else "baissiere"):
            return None, "15 min pas dans le sens"
        sg = 1 if side == "long" else -1
        t1h = mtf.trend(c1h)[0]
        if (side == "long" and t1h == "baissiere") or (side == "short" and t1h == "haussiere"):
            return None, f"H1 contraire ({t1h})"
        nb_s = max(int(K["cont_slope_bars"]), 6)
        sl_ = mtf.slope_features(c1h, n=nb_s, lookback=max(48, nb_s))
        pa, r2 = sl_.get("pente_atr"), sl_.get("pente_r2")
        r2_need = K["cont_min_r2"] * (0.6 if follower else 1.0)
        if pa is None or r2 is None:
            return None, "pente 1 h indisponible"
        if sg * pa <= 0:
            return None, f"pente 1 h contre le sens ({pa:+.2f} ATR)"
        if r2 < r2_need:
            return None, f"pente 1 h irrégulière (R² {r2:.2f} < {r2_need:.2f})"
        ext_ref = e20
    if K.get("cont_h1_block", 1) and ((side == "long" and t1h == "baissiere") or (side == "short" and t1h == "haussiere")):
        # v4.534 : constat 30/10 : 14 longs de continuation pris H1 baissiere = 2 gagnants, 7 stops, -2,34 $ (0/7 la semaine d avant) ; H1 neutre = 2/2
        return None, f"continuation {side} refusée : H1 {t1h} (contraire)"
    mv = (price / float(c5[-25]["c"]) - 1) * 100 * sg
    mv_need = K["cont_min_move_pct"] * (0.5 if follower else 1.0)
    if mv < mv_need:
        return None, f"mouvement 2 h trop faible ({mv:+.2f} % < {mv_need:.2f} %)"
    ext = (price - ext_ref) / a15 * sg      # etirement par rapport a la moyenne de reference (3 h en 5 min / EMA20 en 15 min), en ATR 15 min
    if ext > K["cont_max_ext_atr"]:
        return None, f"trop étiré ({ext:.1f} ATR 15 min > {K['cont_max_ext_atr']:g})"
    rsi5 = _rsi([c["c"] for c in c5], 14)
    if rsi5 is not None and ((side == "long" and rsi5 >= K["cont_rsi_max"]) or (side == "short" and rsi5 <= 100 - K["cont_rsi_max"])):
        return None, f"RSI 5 min trop {'haut' if side == 'long' else 'bas'} ({rsi5:.0f})"
    last, prev = c5[-1], c5[-2]
    rng, body = last["h"] - last["l"], abs(last["c"] - last["o"])
    vols = [c["v"] for c in c5[-21:-1]]
    vavg = sum(vols) / len(vols) if vols else 0
    vr = (last["v"] / vavg) if vavg > 0 else 1.0
    extra, sub = {}, None
    if follower:
        e9 = mtf.ema([c["c"] for c in c5], 9)
        if e9 is None or sg * (price - e9) < 0:
            return None, "prix du mauvais côté de la moyenne 9 (5 min)"
        sub, level, hi_, lo_ = "suiveur", e9, max(price, e9), min(price, e9)
        why_t = [f"actif corrélé : tendance 15 min et pente 1 h dans le sens, prix du bon côté de la moyenne 9 (5 min)"]
    else:
        nbw = max(int(K["cont_bars"]), 6)
        w = c5[-nbw:]
        if side == "long":
            xv = max(x["h"] for x in w)
            ix = max(i for i, x in enumerate(w) if x["h"] == xv)
        else:
            xv = min(x["l"] for x in w)
            ix = max(i for i, x in enumerate(w) if x["l"] == xv)
        after = w[ix + 1:]
        if after:
            if side == "long":
                pb = min(x["l"] for x in after)
                ip = max(i for i, x in enumerate(w) if i > ix and x["l"] == pb)
                depth = (xv - pb) / a5
            else:
                pb = max(x["h"] for x in after)
                ip = max(i for i, x in enumerate(w) if i > ix and x["h"] == pb)
                depth = (pb - xv) / a5
            if not (K["cont_pb_min_atr"] <= depth <= K["cont_pb_max_atr"]):
                return None, f"repli hors fourchette ({depth:.1f} ATR 5 min, il faut {K['cont_pb_min_atr']:g} à {K['cont_pb_max_atr']:g})"
            if len(w) - ip > 7:
                return None, "repli trop ancien (pas de reprise)"
            resume = (last["c"] > last["o"] and last["c"] > prev["h"]) if side == "long" else (last["c"] < last["o"] and last["c"] < prev["l"])
            if not resume:
                return None, "repli en cours, pas de bougie de reprise"
            sub, level = "repli", pb
            hi_, lo_ = (xv, pb) if side == "long" else (pb, xv)
            extra = {"pb_depth_atr": round(depth, 2), "swing": xv}
            why_t = [f"repli de {depth:.1f} ATR 5 min depuis {xv:.6g}, puis bougie de reprise"]
        else:
            if not K["cont_momentum_on"]:
                return None, "pas de repli (élan désactivé)"
            strong = rng > 0 and body >= 0.5 * a5 and ((last["c"] > last["o"] and (last["c"] - last["l"]) / rng >= 0.7) if side == "long"
                                                       else (last["c"] < last["o"] and (last["h"] - last["c"]) / rng >= 0.7))
            if not strong:
                return None, "pas de repli, dernière bougie sans élan"
            if vr < K["cont_vol_mult"]:
                return None, f"élan sans volume (x{vr:.1f} < {K['cont_vol_mult']:g})"
            prior = max(x["h"] for x in w[:-1]) if side == "long" else min(x["l"] for x in w[:-1])
            sub, level = "elan", prior
            hi_, lo_ = max(price, prior), min(price, prior)
            why_t = [f"élan : bougie forte au nouvel extrême des {nbw} dernières bougies 5 min (volume x{vr:.1f})"]
    if btc_chg is not None:
        if side == "long" and btc_chg <= -0.5:
            return None, f"continuation long refuse : BTC en baisse ({btc_chg:+.2f} % / 1 h)"
        if side == "short" and K.get("short_btc_block", 1) and btc_chg > K.get("short_btc_max", 0.1):
            return None, f"continuation short refuse : BTC en hausse ({btc_chg:+.2f} % / 1 h)"
    fmin = float(K.get("cont_flow_min", 0) or 0)
    if fmin > 0 and flow is not None and flow.get("n", 0) >= K.get("flow_all_trades", 30):
        br = flow["buy_ratio"]
        if (side == "long" and br < fmin) or (side == "short" and br > 1 - fmin):
            return None, f"continuation {side} refuse : flux pas dans le sens ({br * 100:.0f} % d'achats agressifs)"
    zones = mtf.find_zones(c1h, pivot=2, lookback=120, merge_atr=0.6, max_width_atr=2.0) or []
    if side == "long":
        ups = [z["low"] for z in zones if z["low"] > price * 1.0005]
        room = ((min(ups) - price) / price * 100) if ups else 99.0
    else:
        dns = [z["high"] for z in zones if z["high"] < price * 0.9995]
        room = ((price - max(dns)) / price * 100) if dns else 99.0
    if room < K["cont_min_room_pct"]:
        return None, f"continuation {side} refuse : zone H1 à {room:.2f} % seulement (il faut ≥ {K['cont_min_room_pct']:g} %)"
    aligned_h1 = (t1h in ("haussiere", "baissiere")) and not short      # v4.510 : en mode court terme, la structure H1 ne compte plus dans la note
    sc_ = 50 + 8 + (12 if aligned_h1 else 0) + (10 if vr >= 1.2 else 0) + (5 if vr >= 2.5 else 0) + int(round(min(1.0, r2) * 8)) + (6 if sub == "repli" else 0)
    if btc_chg is not None and ((side == "long" and btc_chg >= 0.2) or (side == "short" and btc_chg <= -0.2)):
        sc_ += 8
    sc_ = max(0, min(100, sc_))
    reasons = [(f"continuation {side} ({sub}) : tendance des bougies 5 min sur {nL * 5 // 60} h dans le sens (régularité {r2:.2f}), dernière heure confirmée, mouvement {mv:+.2f} % sur 2 h" if short
                else f"continuation {side} ({sub}) : tendance 15 min + pente 1 h dans le sens (régularité {r2:.2f}), mouvement {mv:+.2f} % sur 2 h")] + why_t
    if aligned_h1:
        reasons.append("tendance H1 dans le sens")
    reasons.append(f"place avant la zone suivante : {'>3' if room > 3 else f'{room:.1f}'} %")
    sig = {"side": side, "kind": "continuation", "sub": sub, "zone": (lo_, hi_), "score": sc_, "room": room, "touches": 0,
           "reasons": reasons, "level": level, "price": price, "atr15_pct": atr15_pct, "volume_ratio": vr,
           "sig_high": last["h"], "sig_low": last["l"], "atr5": a5,
           "ctx": {"flow_buy": (round(flow["buy_ratio"], 3) if flow else None), "flow_n": (flow.get("n") if flow else None),
                   "t1h": t1h, "m15": m15, "btc1h": (round(btc_chg, 3) if btc_chg is not None else None)},
           "cont": {"sub": sub, "early": early, "court": short, "mv_pct": round(mv, 3), "r2": round(r2, 3), "pente_atr": round(pa, 3), "ext_atr": round(ext, 2), **extra}}
    return sig, "ok"


def sus_measure(c5, K, side=None):
    """v4.532 : TENDANCE SOUTENUE (pur). Le mouvement des SUS_MIN dernieres minutes (bougies 5 min) est-il propre — net, efficace, sans repli ?
    Criteres : mouvement net >= max(SUS_MIN_MOVE_PCT %, SUS_MIN_MOVE_ATR x ATR 5 min) ; ratio d efficacite (net / chemin) >= SUS_MIN_ER ;
    plus fort recul (meche basse sous la plus haute cloture precedente, pour un long) <= SUS_MAX_PB_ATR x ATR 5 min ; prix du bon cote de la moyenne 9 ;
    derniere bougie pas franchement contraire. Retourne {ok, why, side, mv_pct, er, pb_atr, n, a5, price}."""
    n = max(int(K.get("sus_min", 30)) // 5, 3)
    if len(c5) < max(n + 2, 40):
        return {"ok": False, "why": "historique insuffisant"}
    a5 = mtf.atr(c5, 14)
    price = float(c5[-1]["c"])
    if not a5 or price <= 0:
        return {"ok": False, "why": "ATR indisponible"}
    w = c5[-(n + 1):]
    cl = [float(x["c"]) for x in w]
    net = cl[-1] - cl[0]
    sd = side or ("long" if net > 0 else "short")
    sg = 1 if sd == "long" else -1
    mv = sg * net / cl[0] * 100 if cl[0] > 0 else 0.0
    need = max(float(K.get("sus_min_move_pct", 0.35)), float(K.get("sus_min_move_atr", 2.0)) * a5 / price * 100)
    base = {"side": sd, "mv_pct": round(mv, 3), "n": n, "a5": a5, "price": price}
    if mv < need:
        return {"ok": False, "why": f"mouvement {n * 5} min trop faible dans ce sens ({mv:+.2f} % < {need:.2f} %)", **base}
    er = _eff_ratio(cl)
    if er < float(K.get("sus_min_er", 0.45)):
        return {"ok": False, "why": f"mouvement {n * 5} min pas assez direct (efficacité {er:.2f} < {K.get('sus_min_er', 0.45):g})", "er": round(er, 3), **base}
    pb = 0.0
    for i in range(1, len(w)):
        if sd == "long":
            pb = max(pb, max(cl[:i]) - float(w[i]["l"]))
        else:
            pb = max(pb, float(w[i]["h"]) - min(cl[:i]))
    pb_atr = pb / a5
    if pb_atr > float(K.get("sus_max_pb_atr", 1.5)):
        return {"ok": False, "why": f"repli de {pb_atr:.1f} ATR 5 min dans les {n * 5} min (max {K.get('sus_max_pb_atr', 1.5):g})", "er": round(er, 3), "pb_atr": round(pb_atr, 2), **base}
    e9 = mtf.ema([float(x["c"]) for x in c5], 9)
    if e9 is None or sg * (price - e9) < 0:
        return {"ok": False, "why": "prix du mauvais côté de la moyenne 9 (5 min)", "er": round(er, 3), "pb_atr": round(pb_atr, 2), **base}
    if sg * (cl[-1] - cl[-2]) < -0.5 * a5:
        return {"ok": False, "why": "dernière bougie franchement contraire", "er": round(er, 3), "pb_atr": round(pb_atr, 2), **base}
    # v4.532 : amplitude des va-et-vient sur la derniere heure (12 bougies) : plus fort recul contre le sens, en % du prix et en ATR 5 min.
    # Une tendance soutenue n exclut pas les allers-retours : le STOP doit passer au-dessus de ce bruit (voir _take).
    w1 = c5[-13:]
    cl1 = [float(x["c"]) for x in w1]
    nz = 0.0
    for i in range(1, len(w1)):
        nz = max(nz, (max(cl1[:i]) - float(w1[i]["l"])) if sd == "long" else (float(w1[i]["h"]) - min(cl1[:i])))
    return {"ok": True, "why": "", "er": round(er, 3), "pb_atr": round(pb_atr, 2), "noise_pct": round(nz / price * 100, 3), "noise_atr": round(nz / a5, 2), "e9": e9, "lo": min(float(x["l"]) for x in w), "hi": max(float(x["h"]) for x in w), **base}


def sus_end(c5, side, K, price=None):
    """v4.532 : FIN de la tendance soutenue (pur). Retourne (fini, motif) : recul >= SUS_END_PB_ATR x ATR 5 min depuis l extreme des 6 dernieres
    bougies, ou deux dernieres clotures du mauvais cote de la moyenne 9 (5 min). price = dernier prix connu (sinon derniere cloture)."""
    if len(c5) < 20:
        return False, ""
    a5 = mtf.atr(c5, 14)
    px = float(price) if price else float(c5[-1]["c"])
    if not a5 or px <= 0:
        return False, ""
    w = c5[-7:]
    sg = 1 if side == "long" else -1
    ext = max(float(x["h"]) for x in w) if sg > 0 else min(float(x["l"]) for x in w)
    back = sg * (ext - px) / a5
    lim = float(K.get("sus_end_pb_atr", 1.2))
    try:      # le seuil de fin passe au-dessus du bruit habituel de l actif (recul maximal des 11 bougies precedentes x 1,3)
        w0 = c5[-13:-1]
        cl0 = [float(x["c"]) for x in w0]
        nz = 0.0
        for i in range(1, len(w0)):
            nz = max(nz, (max(cl0[:i]) - float(w0[i]["l"])) if sg > 0 else (float(w0[i]["h"]) - min(cl0[:i])))
        lim = max(lim, 1.3 * nz / a5)
    except Exception:
        pass
    if back >= lim:
        return True, f"recul de {back:.1f} ATR 5 min depuis l'extrême des 30 dernières minutes"
    e9 = mtf.ema([float(x["c"]) for x in c5], 9)
    if e9 and sg * (float(c5[-1]["c"]) - e9) < 0 and sg * (float(c5[-2]["c"]) - e9) < 0:
        return True, "deux clôtures 5 min du mauvais côté de la moyenne 9"
    return False, ""


def sustained_eval(c5, c15, c1h, btc_chg, K, flow=None, side=None, follower=False):
    """v4.532 : signal de TENDANCE SOUTENUE : le mouvement tient depuis ~30 min sans repli (sus_measure) -> entree dans le sens, sans attendre
    de repli ni de volume. Garde-fous : marche pas trop calme, prix pas trop etire (SUS_MAX_EXT_ATR x ATR 15 min au-dela de la moyenne 3 h),
    RSI 5 min <= SUS_RSI_MAX, BTC pas contre, flux pas contre, place avant la zone H1 suivante. Le STOP est decide dans _take (ATR 5 min, borne).
    Retourne (signal ou None, motif). Meme forme que cont_eval (kind « continuation », sub « soutenue »)."""
    if len(c5) < 40 or len(c15) < 30 or len(c1h) < 60:
        return None, "historique insuffisant"
    m = sus_measure(c5, K, side=side)
    if not m["ok"]:
        return None, m["why"]
    price, a5 = m["price"], m["a5"]
    a15 = mtf.atr(c15, 14)
    if not a15:
        return None, "ATR 15 min indisponible"
    atr15_pct = a15 / price * 100
    if atr15_pct < K["min_atr_pct"]:
        return None, "marché trop calme"
    sd = m["side"]
    sg = 1 if sd == "long" else -1
    t1h_ = mtf.trend(c1h)[0]
    if K.get("cont_h1_block", 1) and ((sd == "long" and t1h_ == "baissiere") or (sd == "short" and t1h_ == "haussiere")):
        return None, f"continuation {sd} refusée : H1 {t1h_} (contraire)"
    closes5 = [float(x["c"]) for x in c5]
    ref = mtf.ema(closes5, 36) or price
    ext = (price - ref) / a15 * sg
    if ext > float(K.get("sus_max_ext_atr", 4.0)):
        return None, f"trop étiré ({ext:.1f} ATR 15 min > {K.get('sus_max_ext_atr', 4.0):g})"
    rsi5 = _rsi(closes5, 14)
    rmax = float(K.get("sus_rsi_max", 93))
    if rsi5 is not None and ((sd == "long" and rsi5 >= rmax) or (sd == "short" and rsi5 <= 100 - rmax)):
        return None, f"RSI 5 min trop {'haut' if sd == 'long' else 'bas'} ({rsi5:.0f})"
    if btc_chg is not None:
        if sd == "long" and btc_chg <= -0.5:
            return None, f"BTC en baisse ({btc_chg:+.2f} % / 1 h)"
        if sd == "short" and K.get("short_btc_block", 1) and btc_chg > K.get("short_btc_max", 0.1):
            return None, f"BTC en hausse ({btc_chg:+.2f} % / 1 h)"
    fmin = float(K.get("cont_flow_min", 0) or 0)
    if fmin > 0 and flow is not None and flow.get("n", 0) >= K.get("flow_all_trades", 30):
        br = flow["buy_ratio"]
        if (sd == "long" and br < fmin) or (sd == "short" and br > 1 - fmin):
            return None, f"flux pas dans le sens ({br * 100:.0f} % d'achats agressifs)"
    zones = mtf.find_zones(c1h, pivot=2, lookback=120, merge_atr=0.6, max_width_atr=2.0) or []
    if sd == "long":
        ups = [z["low"] for z in zones if z["low"] > price * 1.0005]
        room = ((min(ups) - price) / price * 100) if ups else 99.0
    else:
        dns = [z["high"] for z in zones if z["high"] < price * 0.9995]
        room = ((price - max(dns)) / price * 100) if dns else 99.0
    if room < K["cont_min_room_pct"]:
        return None, f"zone H1 à {room:.2f} % seulement (il faut ≥ {K['cont_min_room_pct']:g} %)"
    last = c5[-1]
    vols = [float(x["v"]) for x in c5[-21:-1]]
    vavg = sum(vols) / len(vols) if vols else 0
    vr = (float(last["v"]) / vavg) if vavg > 0 else 1.0
    nL = max(int(K.get("cont_trend_bars", 36)), 18)
    bL, r2 = mtf._linreg(closes5[-nL:])
    pa = (bL / a5) if bL is not None else 0.0
    r2 = r2 if r2 is not None else 0.0
    mv2h = (price / float(c5[-25]["c"]) - 1) * 100 * sg
    sc_ = 50 + 8 + int(round(min(1.0, m["er"]) * 8)) + (10 if vr >= 1.2 else 0) + (5 if vr >= 2.5 else 0) + (8 if btc_chg is not None and ((sd == "long" and btc_chg >= 0.2) or (sd == "short" and btc_chg <= -0.2)) else 0)
    sc_ = max(0, min(100, sc_))
    mins = m["n"] * 5
    reasons = [f"tendance soutenue {sd} : {m['mv_pct']:+.2f} % en {mins} min, efficacité {m['er']:.2f}, repli maximal {m['pb_atr']:.1f} ATR 5 min",
               f"prix du bon côté de la moyenne 9 (5 min), BTC et flux non contraires, place avant la zone suivante : {'>3' if room > 3 else f'{room:.1f}'} %"]
    lo_, hi_ = (m["lo"], price) if sd == "long" else (price, m["hi"])
    sig = {"side": sd, "kind": "continuation", "sub": "soutenue", "zone": (lo_, hi_), "score": sc_, "room": room, "touches": 0,
           "reasons": reasons, "level": m["e9"], "price": price, "atr15_pct": atr15_pct, "volume_ratio": vr,
           "sig_high": float(last["h"]), "sig_low": float(last["l"]), "atr5": a5,
           "ctx": {"flow_buy": (round(flow["buy_ratio"], 3) if flow else None), "flow_n": (flow.get("n") if flow else None),
                   "t1h": t1h_, "m15": f"soutenue {mins} min", "btc1h": (round(btc_chg, 3) if btc_chg is not None else None)},
           "cont": {"sub": "soutenue", "early": False, "court": True, "mv_pct": round(mv2h, 3), "r2": round(r2, 3), "pente_atr": round(pa, 3),
                    "ext_atr": round(ext, 2), "sus_mv_pct": m["mv_pct"], "sus_er": m["er"], "sus_pb_atr": m["pb_atr"], "sus_noise_pct": m["noise_pct"], "sus_noise_atr": m["noise_atr"], "sus_min": mins}}
    return sig, "ok"


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
def _eff_ratio(vals):
    """Ratio d efficacite de Kaufman : mouvement net / chemin parcouru (1 = tendance propre, 0 = va-et-vient)."""
    if len(vals) < 3:
        return 0.0
    path = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))
    return abs(vals[-1] - vals[0]) / path if path > 0 else 0.0


class ScalpEngine:
    def __init__(self, bot, manual):
        self.bot, self.manual = bot, manual
        self.events = deque(maxlen=80)       # journal en memoire (signaux pris / refuses)
        self._uni, self._uni_ts = [], 0
        self._seen = {}                      # ticker -> derniere tranche 5 min evaluee
        self._cool = {}                      # ticker -> fin de pause
        self.verdicts = {}                   # v4.470 : ticker -> (ts, type, texte) dernier verdict du Scalp (diagnostic)
        self._zone_cache = (0, {})           # v4.432 : zones ou un stop vient de tomber
        self.regime = {"ts": 0, "n": 0, "label": "inconnu"}     # v4.442 : regime du panier (correlation / largeur)
        self._opens = deque(maxlen=200)      # (ts, side, mode, ticker) des ouvertures recentes (grappe)
        self._cont_fan = {}                  # v4.504 : (sens, mode) -> derniere ouverture en grappe de continuation
        self.macro = macro_events.MacroCalendar() if macro_events else None      # v4.507
        self._last_sig_ts = time.time()      # v4.506 : dernier signal valide vu (alerte « aucun signal »)
        self.cont_shadow = self._shadow_load()      # v4.514 : meneurs de continuation refuses (suivi hypothetique)
        self.sus_state = {}                  # v4.532 : ticker -> {phase soutenue|fin, side, since, ...} (tendance soutenue / sortie de zone)
        self.sus_why = {}                    # v4.532 : ticker -> (ts, raison pour laquelle la tendance soutenue n est pas retenue)
        self._sus_chk = {}                   # v4.532 : id trade -> dernier controle de fin de zone
        self.cont_diag = {}                  # v4.505 : ticker -> (ts, raison pour laquelle la continuation n est pas prise)
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
        if ticker and ticker != "-":
            self.verdicts[ticker] = (time.time(), kind, text)
        self.events.appendleft({"ts": time.time(), "kind": kind, "ticker": ticker, "text": text})

    def thresholds(self):
        c = self.cfg
        return {"min_atr_pct": float(c.get("SCALP_MIN_ATR15_PCT", 0.0)), "min_room_pct": float(c.get("SCALP_MIN_ROOM_PCT", 0.0)),
                "vol_mult": float(c.get("SCALP_BREAK_VOL_MULT", 1.5)), "max_chase_pct": float(c.get("SCALP_MAX_CHASE_PCT", 0.4)),
                "min_score": float(c.get("SCALP_MIN_SCORE", 0)),
                "min_score_short": float(c.get("SCALP_SHORT_MIN_SCORE", 62)),
                "trend_mode": int(c.get("SCALP_TREND_MODE", 1)),
                "short_btc_block": int(c.get("SCALP_SHORT_BTC_BLOCK", 1)), "short_btc_max": float(c.get("SCALP_SHORT_BTC_MAX_PCT", 0.1)), "short_btc_soft_max": float(c.get("SCALP_SHORT_BTC_SOFT_MAX_PCT", 0.4)),
                "counter_enabled": int(c.get("SCALP_COUNTER_ENABLED", 1)), "counter_min_score": float(c.get("SCALP_COUNTER_MIN_SCORE", 65)),
                "counter_flow": float(c.get("SCALP_COUNTER_FLOW_MIN", 0.58)), "counter_min_trades": int(c.get("SCALP_COUNTER_MIN_TRADES", 30)),
                "flow_all_on": int(c.get("SCALP_FLOW_ALL_ON", 0)), "flow_all_min": float(c.get("SCALP_FLOW_ALL_MIN", 0.52)),
                "flow_all_trades": int(c.get("SCALP_FLOW_ALL_MIN_TRADES", 30)),
                "cont_bars": int(c.get("SCALP_CONT_BARS", 12)), "cont_pb_min_atr": float(c.get("SCALP_CONT_PB_MIN_ATR", 0.3)),
                "cont_pb_max_atr": float(c.get("SCALP_CONT_PB_MAX_ATR", 2.5)), "cont_min_move_pct": float(c.get("SCALP_CONT_MIN_MOVE_PCT", 0.5)),
                "cont_min_r2": float(c.get("SCALP_CONT_MIN_R2", 0.4)), "cont_slope_bars": int(c.get("SCALP_SLOPE_BARS", 12) or 12),
                "cont_max_ext_atr": float(c.get("SCALP_CONT_MAX_EXT_ATR", 2.5)), "cont_rsi_max": float(c.get("SCALP_CONT_RSI_MAX", 78)),
                "cont_momentum_on": int(c.get("SCALP_CONT_MOMENTUM_ON", 1)), "cont_flow_min": float(c.get("SCALP_CONT_FLOW_MIN", 0.5)),
                "cont_min_room_pct": float(c.get("SCALP_CONT_MIN_ROOM_PCT", 0.3)), "cont_vol_mult": float(c.get("SCALP_CONT_VOL_MULT", 1.2)), "cont_early_on": int(c.get("SCALP_CONT_EARLY_ON", 1)),
                "cont_short_on": int(c.get("SCALP_CONT_SHORT_ON", 1)), "cont_trend_bars": int(c.get("SCALP_CONT_TREND_BARS", 36)), "cont_min_r2_5m": float(c.get("SCALP_CONT_MIN_R2_5M", 0.3)),
                "cont_h1_block": int(c.get("SCALP_CONT_H1_BLOCK", 1)), "sus_min": int(c.get("SCALP_SUS_MIN", 30)), "sus_min_move_pct": float(c.get("SCALP_SUS_MIN_MOVE_PCT", 0.35)), "sus_min_move_atr": float(c.get("SCALP_SUS_MIN_MOVE_ATR", 2.0)),
                "sus_min_er": float(c.get("SCALP_SUS_MIN_ER", 0.45)), "sus_max_pb_atr": float(c.get("SCALP_SUS_MAX_PB_ATR", 1.5)), "sus_max_ext_atr": float(c.get("SCALP_SUS_MAX_EXT_ATR", 4.0)),
                "sus_rsi_max": float(c.get("SCALP_SUS_RSI_MAX", 93)), "sus_end_pb_atr": float(c.get("SCALP_SUS_END_PB_ATR", 1.2))}

    def _clear_err(self, prefix):
        """v4.508 : efface la derniere erreur technique si elle vient de cette source (l appel suivant a reussi)."""
        e = self.last_error
        if e and str(e).startswith(prefix):
            self.last_error = None

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
                self._clear_err("univers :")
                return self._uni
        except Exception as e:
            self.last_error = f"univers : {type(e).__name__}: {e}"
        self._uni, self._uni_ts = tracked[:n], now - 1500      # repli : reessai dans 5 min
        return self._uni

    # ── statistiques (a partir de l historique des trades manuels) ──
    def day_start_ts(self):
        """v4.465 : debut de la journee du Scalp (stats, objectif du jour, limite de perte, trades max) = minuit au decalage SCALP_DAY_OFFSET_H
        par rapport a UTC (defaut -4 = heure de Marigot). Retourne un timestamp UTC."""
        try:
            off = float(self.cfg.get("SCALP_DAY_OFFSET_H", -4))
        except (TypeError, ValueError):
            off = -4.0
        now = datetime.now(timezone.utc)
        loc = now + timedelta(hours=off)
        return loc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - off * 3600

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
        midnight = self.day_start_ts()
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
        for k in ("rebond", "cassure", "continuation"):
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
        # v4.509 : resultats par situation de marche (n < 10 = echantillon trop faible pour conclure) et bilan « apres sortie » du stop de rentabilite
        dims = []

        def add_dim(title, fn, order=None):
            g = {}
            for d in rows:
                try:
                    lab = fn(d)
                except Exception:
                    lab = None
                if lab is not None:
                    g.setdefault(lab, []).append(d)
            labs = [l for l in (order or []) if l in g] + sorted([l for l in g if l not in (order or [])], key=lambda l: -len(g[l]))
            if labs:
                dims.append({"title": title, "rows": [{"label": l, "agg": agg(g[l])} for l in labs]})

        def _sg(d):
            return 1 if d.get("direction") == "long" else -1

        def _sess(d):
            h = datetime.fromtimestamp(float(d.get("opened_at") or d.get("closed_at") or 0), timezone.utc).hour
            return ("Asie 00–07 UTC" if h < 7 else "Europe 07–13 UTC" if h < 13 else "Europe + États-Unis 13–17 UTC" if h < 17
                    else "États-Unis soir 17–22 UTC" if h < 22 else "Nuit 22–24 UTC")

        def _bsk(d):
            b = _rg(d).get("basket30")
            if b is None:
                return "Avant mesure"
            v = float(b) * _sg(d)
            return "Panier contre le trade (≤ −0,2 %)" if v <= -0.2 else ("Panier dans le sens (≥ +0,2 %)" if v >= 0.2 else "Panier calme (−0,2 à +0,2 %)")

        def _er(d):
            e = ((d.get("scalp") or {}).get("tq") or {}).get("er_actif_1h")
            if e is None:
                return "Avant mesure"
            return "Actif chaotique (efficacité 1 h < 0,2)" if e < 0.2 else ("Actif intermédiaire (0,2 à 0,4)" if e < 0.4 else "Actif directionnel (≥ 0,4)")
        add_dim("Par sens", lambda d: "Long" if d.get("direction") == "long" else "Short", ["Long", "Short"])
        add_dim("Par séance (heure d'ouverture, UTC)", _sess, ["Asie 00–07 UTC", "Europe 07–13 UTC", "Europe + États-Unis 13–17 UTC", "États-Unis soir 17–22 UTC", "Nuit 22–24 UTC"])
        add_dim("Par mouvement du panier sur 30 min (par rapport au sens du trade)", _bsk, ["Panier contre le trade (≤ −0,2 %)", "Panier calme (−0,2 à +0,2 %)", "Panier dans le sens (≥ +0,2 %)", "Avant mesure"])
        add_dim("Par efficacité de l'actif sur 1 h (directionnel ou chaotique)", _er, ["Actif chaotique (efficacité 1 h < 0,2)", "Actif intermédiaire (0,2 à 0,4)", "Actif directionnel (≥ 0,4)", "Avant mesure"])
        def _brd(d):
            k = (d.get("scalp") or {}).get("cont")
            if not k:
                return None
            b = k.get("breadth_pct")
            if b is None:
                return "Avant mesure"
            return "Panier ≥ 60 % dans le sens (grappe confirmée)" if b >= 60 else "Panier < 60 % (meneur seul)"
        add_dim("Continuation : étendue du panier au déclenchement", _brd, ["Panier ≥ 60 % dans le sens (grappe confirmée)", "Panier < 60 % (meneur seul)", "Avant mesure"])
        add_dim("Par type de sortie", lambda d: (str(d.get("reason") or "?").replace(" (manuel)", "").strip() or "?"))
        pe = [d["post_exit"] for d in rows if (d.get("post_exit") or {}).get("ok")]
        be_rows = [d for d in rows if "SEUIL RENTABILITE" in str(d.get("reason") or "")]
        be_post = {"n": len(pe), "cost": sum(1 for p in pe if p["verdict"] == "coûté"), "protect": sum(1 for p in pe if p["verdict"] == "protégé"),
                   "neutral": sum(1 for p in pe if p["verdict"] == "neutre"), "avg_delta_pct": (sum(p["delta_pct"] for p in pe) / len(pe)) if pe else None,
                   "sum_delta_pct": sum(p["delta_pct"] for p in pe) if pe else None,
                   "pending": sum(1 for d in be_rows if not (d.get("post_exit") or {}).get("ok")), "total_be": len(be_rows)}
        out = {"mode": mode, "dims": dims, "be_post": be_post, "shadow": self.shadow_summary(), "lab": self.lab_summary(rows), "total": agg(rows), "today": agg(today), "streak_losses": streak, "by_kind": by_kind, "by_trend": by_trend, "by_regime": by_regime, "by_cluster": {str(k): v for k, v in by_cluster.items()},
               "by_hour": {h: agg(v) for h, v in sorted(by_hour.items())}, "last_closed": rows[0].get("closed_at") if rows else None}
        self._stats_cache = (time.time(), mode, out)
        return out

    # ── v4.509 : suivi APRES SORTIE des trades coupes par le stop de rentabilite (observation seule) ──
    PE_WIN_MIN = 60

    def _candles_range(self, t, start_s, end_s):
        raw = self.bot.info.post("/info", {"type": "candleSnapshot", "req": {"coin": t, "interval": "5m", "startTime": int(start_s * 1000), "endTime": int(end_s * 1000)}})
        return [{"t": int(c["t"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in raw] if isinstance(raw, list) else []

    @staticmethod
    def post_exit_calc(d, cs):
        """Pur : que se serait-il passe si le trade n avait pas ete coupe ? d : trade ferme ; cs : bougies 5 min suivant la sortie (anciennes d abord).
        « tenir » = objectif (tp_pct) ou stop initial (sl_pct) touche en premier, sinon cours a la fin de la fenetre ; meme bougie : stop d abord (prudent)."""
        ent = float(d["entry_price"])
        sg = 1 if d.get("direction") == "long" else -1
        xp = float(d["exit_price"])
        exit_pct = sg * (xp - ent) / ent * 100
        tp = float(d.get("tp_pct") or 0) or None
        sl = float(d.get("sl_pct") or 0) or None
        mf, ma, first = 0.0, 0.0, None
        for c in cs:
            fav = sg * ((c["h"] if sg > 0 else c["l"]) - ent) / ent * 100
            adv = sg * ((c["l"] if sg > 0 else c["h"]) - ent) / ent * 100
            mf, ma = max(mf, fav), min(ma, adv)
            if first is None:
                hit_sl = sl is not None and adv <= -sl
                hit_tp = tp is not None and fav >= tp
                if hit_sl:
                    first = "stop"
                elif hit_tp:
                    first = "objectif"
        end_pct = sg * (cs[-1]["c"] - ent) / ent * 100
        hold = tp if first == "objectif" else (-sl if first == "stop" else end_pct)
        delta = hold - exit_pct
        verdict = "coûté" if delta > 0.05 else ("protégé" if delta < -0.05 else "neutre")
        return {"ok": True, "win_min": 60, "exit_pct": round(exit_pct, 3), "max_fav_pct": round(mf, 3), "max_adv_pct": round(ma, 3), "end_pct": round(end_pct, 3),
                "first": first or "aucun", "hold_pct": round(hold, 3), "delta_pct": round(delta, 3), "verdict": verdict, "bars": len(cs)}

    def _post_exit_eval(self, limit=3):
        now = time.time()
        win = self.PE_WIN_MIN * 60
        done = 0
        for r in db.manual_history(400):
            if done >= limit:
                break
            d = r["data"]
            if r.get("status") != "closed" or d.get("strategy_source") != STRATEGY or "SEUIL RENTABILITE" not in str(d.get("reason") or ""):
                continue
            if (d.get("post_exit") or {}).get("ok") or (d.get("post_exit") or {}).get("fail", 0) >= 3:
                continue
            ca = float(d.get("closed_at") or 0)
            if not ca or now - ca < win + 300 or not d.get("entry_price") or not d.get("exit_price"):
                continue
            try:
                cs = [c for c in self._candles_range(d["ticker"], ca, min(ca + win + 300, now)) if ca <= c["t"] / 1000.0 < ca + win]
                pe = self.post_exit_calc(d, cs) if len(cs) >= 6 else {"ok": False, "fail": (d.get("post_exit") or {}).get("fail", 0) + 1, "why": f"{len(cs)} bougies"}
            except Exception as e:
                pe = {"ok": False, "fail": (d.get("post_exit") or {}).get("fail", 0) + 1, "why": f"{type(e).__name__}"}
            d["post_exit"] = pe
            db.manual_update(r["id"], "closed", d)
            done += 1
        if done:
            self._stats_cache = (0, None, None)
            self._rows_cache = (0, None, None)

    # ── v4.514 : meneur de continuation sans confirmation du panier = REFUSE, mais SUIVI (apprentissage) ──
    @staticmethod
    def shadow_plan(c, price, atr5):
        """Pur : stop et objectif (en % du prix) que _take donnerait a ce signal de continuation (memes formules que la v4.511)."""
        sl = float(c.get("SCALP_CONT_SL_PCT", 0.2))
        tp = float(c.get("SCALP_CONT_TP_PCT", 0.6))
        if int(c.get("SCALP_CONT_SL_ATR_ON", 1)) and atr5 and price:
            a5p = atr5 / price * 100.0
            new_sl = min(max(sl, float(c.get("SCALP_CONT_SL_ATR", 1.5)) * a5p), max(float(c.get("SCALP_CONT_SL_MAX_PCT", 0.8)), sl))
            if new_sl > sl + 1e-9:
                tp = max(tp, float(c.get("SCALP_CONT_TP_RR", 2.0)) * new_sl)
                sl = new_sl
        return sl, tp

    @staticmethod
    def shadow_calc(side, entry, sl, tp, cs, fee=0.09):
        """Pur : issue hypothetique d'un signal refuse. cs : bougies 5 min APRES l'entree (anciennes d'abord). Stop ou objectif touche en premier
        (meme bougie : stop d'abord), sinon cours a la fin des bougies. Net = resultat - frais aller-retour (fee, en % du prix)."""
        sg = 1 if side == "long" else -1
        mf = ma = 0.0
        first = None
        for c in cs:
            fav = sg * ((c["h"] if sg > 0 else c["l"]) - entry) / entry * 100
            adv = sg * ((c["l"] if sg > 0 else c["h"]) - entry) / entry * 100
            mf, ma = max(mf, fav), min(ma, adv)
            if first is None:
                if adv <= -sl:
                    first = "stop"
                elif fav >= tp:
                    first = "objectif"
        end_pct = sg * (cs[-1]["c"] - entry) / entry * 100 if cs else 0.0
        res = tp if first == "objectif" else (-sl if first == "stop" else end_pct)
        return {"first": first or "aucun", "res_pct": round(res, 3), "net_pct": round(res - fee, 3), "max_fav_pct": round(mf, 3), "max_adv_pct": round(ma, 3),
                "end_pct": round(end_pct, 3), "bars": len(cs)}

    def _shadow_load(self):
        try:
            raw = db.get_meta("scalp_cont_shadow", None)
            v = json.loads(raw) if isinstance(raw, str) else (raw or [])
            return v if isinstance(v, list) else []
        except Exception:
            return []

    def _shadow_save(self):
        try:
            db.set_meta("scalp_cont_shadow", json.dumps(self.cont_shadow[-300:]))
        except Exception as e:
            self.last_error = f"suivi des refus : {type(e).__name__}: {e}"

    def _shadow_add(self, t, sig, share, need):
        if not int(self.cfg.get("SCALP_CONT_SHADOW_ON", 1)):
            return
        now = time.time()
        for s in reversed(self.cont_shadow[-40:]):
            if s.get("ticker") == t and s.get("side") == sig["side"] and now - float(s.get("ts", 0)) < 900:
                return                      # le meme signal revient toutes les 5 min : un suivi par 15 min suffit
        sl, tp = self.shadow_plan(self.cfg, sig.get("price"), sig.get("atr5"))
        self.cont_shadow.append({"ts": now, "ticker": t, "side": sig["side"], "price": float(sig["price"]), "sl_pct": round(sl, 3), "tp_pct": round(tp, 3),
                                 "breadth_pct": round(share * 100), "need_pct": round(need * 100), "sub": (sig.get("cont") or {}).get("sub")})
        self._shadow_save()

    def _shadow_eval(self, limit=3):
        now = time.time()
        done = 0
        changed = False
        for s in self.cont_shadow:
            if done >= limit:
                break
            if s.get("res") or s.get("fail", 0) >= 3 or now - float(s.get("ts", now)) < (self.PE_WIN_MIN + 5) * 60:
                continue
            done += 1
            try:
                t0 = (int(s["ts"] // 300) + 1) * 300          # premiere bougie ENTIEREMENT apres le signal (pas de regard dans le futur)
                cs = self._candles_range(s["ticker"], t0, t0 + self.PE_WIN_MIN * 60)
                cs = [c for c in cs if c["t"] >= t0 * 1000]
                if len(cs) < 6:
                    s["fail"] = s.get("fail", 0) + 1
                else:
                    s["res"] = self.shadow_calc(s["side"], s["price"], s["sl_pct"], s["tp_pct"], cs)
            except Exception as e:
                s["fail"] = s.get("fail", 0) + 1
                self.last_error = f"suivi des refus : {type(e).__name__}: {e}"
            changed = True
        if changed:
            self._shadow_save()

    def shadow_summary(self):
        ev = [s for s in self.cont_shadow if s.get("res")]
        if not self.cont_shadow:
            return None
        nets = [s["res"]["net_pct"] for s in ev]
        return {"total": len(self.cont_shadow), "n": len(ev), "pending": sum(1 for s in self.cont_shadow if not s.get("res") and s.get("fail", 0) < 3),
                "wins": sum(1 for x in nets if x > 0.02), "losses": sum(1 for x in nets if x < -0.02),
                "obj": sum(1 for s in ev if s["res"]["first"] == "objectif"), "stop": sum(1 for s in ev if s["res"]["first"] == "stop"),
                "sum_net_pct": round(sum(nets), 2) if nets else None, "avg_net_pct": round(sum(nets) / len(nets), 3) if nets else None,
                "last": [{"ticker": s["ticker"], "side": s["side"], "ts": s["ts"], "breadth_pct": s.get("breadth_pct"), "first": (s.get("res") or {}).get("first"),
                          "net_pct": (s.get("res") or {}).get("net_pct")} for s in reversed(self.cont_shadow[-8:])]}

    # ── v4.516 : LABORATOIRE DE STOPS (observation seule) : rejoue chaque trade clos avec d'autres regles de stop ──
    LAB_VARIANTS = (("actuel", "Règle actuelle (stop de rentabilité)"), ("sans_be", "Sans stop de rentabilité"), ("be_tardif", "Rentabilité tardive (à 80 % de l'objectif)"),
                    ("reduit", "Stop réduit (−½ stop) au lieu de la rentabilité"), ("deux_temps", "Deux temps (réduit, puis rentabilité à 80 % de l'objectif)"),
                    ("lock2", "Stop de rentabilité à +0,2 % (au lieu de +0,1 %)"), ("progressif", "Verrou progressif (50 % du gain maximal)"), ("atr", "Suiveur sur ATR (gain maximal − 1,5 × ATR 5 min)"))

    @staticmethod
    def lab_sim(variant, side, sl, tp, be_arm, be_lock, trail, atr5, cs, fee=0.09):
        """Pur : rejoue des bougies 5 min (anciennes d'abord, APRES l'entree) avec une regle de stop. Tout en % du prix d'entree, cote favorable positif.
        Dans une bougie : le stop (celui d'AVANT la bougie) est teste d'abord (prudent), puis le gain maximal met le stop a jour. A l'objectif : sortie,
        sauf si trail > 0 (laisser courir : stop = plus haut - trail). Fin des bougies : cours de cloture. Retourne le net (apres frais) ou None."""
        sg = 1 if side == "long" else -1
        sl = abs(float(sl or 0))
        if not sl or not cs:
            return None
        arm, lock = be_arm, be_lock if be_lock is not None else 0.1
        if variant == "sans_be":
            arm = None
        elif variant == "be_tardif":
            arm, lock = 0.8 * tp, 0.1
        elif variant == "reduit":
            lock = -0.5 * sl
        elif variant == "lock2":
            lock = 0.2
        elif variant == "atr" and not atr5:
            return None
        stop, peak, run = -sl, 0.0, False
        for c in cs:
            fav = c["fav"]; adv = c["adv"]; cl = c["cl"]
            if adv <= stop:
                return round(stop - fee, 3)
            peak = max(peak, fav)
            if tp and not run and peak >= tp:
                if not trail:
                    return round(tp - fee, 3)
                run = True
            if run:
                stop = max(stop, peak - trail)
            elif variant == "progressif":
                if arm is not None and peak >= arm:
                    stop = max(stop, 0.5 * peak)
            elif variant == "atr":
                stop = max(stop, peak - 1.5 * atr5) if peak > 0 else stop
            elif variant == "deux_temps":
                if arm is not None and peak >= arm:
                    stop = max(stop, -0.25 * sl)
                if peak >= 0.8 * tp:
                    stop = max(stop, 0.1)
            elif arm is not None and peak >= arm:
                stop = max(stop, lock)
            stop = max(stop, -sl)
        return round(cs[-1]["cl"] - fee, 3)

    @staticmethod
    def lab_bars(side, entry, cs):
        sg = 1 if side == "long" else -1
        out = []
        for c in cs:
            out.append({"fav": sg * ((c["h"] if sg > 0 else c["l"]) - entry) / entry * 100, "adv": sg * ((c["l"] if sg > 0 else c["h"]) - entry) / entry * 100,
                        "cl": sg * (c["c"] - entry) / entry * 100})
        return out

    def _lab_eval(self, limit=2):
        if not int(self.cfg.get("SCALP_LAB_ON", 1)):
            return
        now = time.time()
        done = 0
        for r in db.manual_history(400):
            if done >= limit:
                break
            d = r["data"]
            if r.get("status") != "closed" or d.get("strategy_source") != STRATEGY or d.get("mode") != self.cfg.get("SCALP_MODE", "paper"):
                continue
            lab = d.get("lab") or {}
            if (lab.get("ok") and "lock2" in (lab.get("v") or {})) or lab.get("fail", 0) >= 3 or not d.get("entry_price") or not d.get("exit_price") or not d.get("opened_at"):
                continue
            hold = float(d.get("hold_min") or d.get("osc_hold_min") or self.cfg.get("SCALP_MAX_HOLD_MIN", 90) or 90) * 60
            op = float(d["opened_at"])
            if now < op + hold + 360:
                continue
            done += 1
            ent = float(d["entry_price"])
            side = d.get("direction")
            try:
                t0 = (int(op // 300) + 1) * 300
                raw = [c for c in self._candles_range(d["ticker"], t0, op + hold + 300) if t0 * 1000 <= c["t"] < (op + hold) * 1000]
                if len(raw) < 3:
                    lab = {"ok": False, "fail": lab.get("fail", 0) + 1, "why": f"{len(raw)} bougies"}
                else:
                    cs = self.lab_bars(side, ent, raw)
                    sl = float(d.get("sl_pct") or 0)
                    tp = float(d.get("tp_pct") or 0)
                    ba = float(d.get("be_arm_pct") or 0) or None
                    bl = d.get("be_lock_pct")
                    trail = float(d.get("tp_run_trail_pct") or 0) if d.get("tp_run") else 0.0
                    a5 = (((d.get("scalp") or {}).get("cont") or {}).get("atr5_pct"))
                    a5 = float(a5) if a5 else None
                    sg = 1 if side == "long" else -1
                    real = sg * (float(d["exit_price"]) - ent) / ent * 100 - 0.09
                    v = {}
                    for k, _ in self.LAB_VARIANTS:
                        x = self.lab_sim(k, side, sl, tp, ba, float(bl) if bl is not None else None, trail, a5, cs)
                        if x is not None:
                            v[k] = x
                    pk = max([b["fav"] for b in cs] + [0.0])
                    lab = {"ok": True, "v": v, "real": round(real, 3), "peak": round(max(pk, float(d.get("peak_pct") or 0)), 3), "bars": len(cs)}
            except Exception as e:
                lab = {"ok": False, "fail": lab.get("fail", 0) + 1, "why": type(e).__name__}
            d["lab"] = lab
            db.manual_update(r["id"], "closed", d)
        if done:
            self._stats_cache = (0, None, None)
            self._rows_cache = (0, None, None)

    def lab_summary(self, rows):
        ev = [d["lab"] for d in rows if (d.get("lab") or {}).get("ok")]
        if not ev:
            return {"n": 0, "pending": sum(1 for d in rows if not (d.get("lab") or {}).get("ok") and (d.get("lab") or {}).get("fail", 0) < 3)}
        def agg(sub):
            out = []
            for k, lbl in self.LAB_VARIANTS:
                xs = [l["v"][k] for l in sub if k in l["v"]]
                if xs:
                    out.append({"k": k, "label": lbl, "n": len(xs), "sum": round(sum(xs), 2), "avg": round(sum(xs) / len(xs), 3), "wins": sum(1 for x in xs if x > 0.02)})
            return out
        cls = {"low": [l for l in ev if l["peak"] < 0.4], "mid": [l for l in ev if 0.4 <= l["peak"] < 0.8], "high": [l for l in ev if l["peak"] >= 0.8]}
        return {"n": len(ev), "pending": sum(1 for d in rows if not (d.get("lab") or {}).get("ok") and (d.get("lab") or {}).get("fail", 0) < 3),
                "real_sum": round(sum(l["real"] for l in ev), 2), "variants": agg(ev),
                "classes": {c: {"n": len(v), "real_sum": round(sum(l["real"] for l in v), 2), "variants": agg(v)} for c, v in cls.items() if v}}

    def _post_exit_run(self):
        try:
            self._post_exit_eval()
            self._shadow_eval()
            self._lab_eval()
        except Exception as e:
            self.last_error = f"suivi après sortie : {type(e).__name__}: {e}"
        finally:
            self._pe_busy = False

    # ── cycle ──
    def on_cycle(self):
        """Appele par le moteur manuel a chaque cycle ; le travail lourd est fait dans un thread, au plus toutes les 15 s."""
        now = time.time()
        try:
            self._manage_open(now)
        except Exception as e:
            self.last_error = f"suivi : {type(e).__name__}: {e}"
        if now - getattr(self, "_pe_last", 0) >= 120 and not getattr(self, "_pe_busy", False):      # v4.509
            self._pe_last, self._pe_busy = now, True
            threading.Thread(target=self._post_exit_run, daemon=True).start()
        if not int(self.cfg.get("SCALP_ENABLED", 0)) or self._busy or now - self._last_tick < 15:
            return
        self._last_tick = now
        self._busy = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _manage_open(self, now):
        mx_cfg = float(self.cfg.get("SCALP_MAX_HOLD_MIN", 90)) * 60
        if mx_cfg <= 0 and not int(self.cfg.get("SCALP_CORR_BASKET_EXIT_ON", 0)) and not int(self.cfg.get("SCALP_OSC_ON", 1)) and not int(self.cfg.get("SCALP_CONT_ON", 1)):
            return
        for it in list(self.manual.items.values()):
            if it.get("strategy_source") == STRATEGY and it.get("status") == "open":
                mx = float(it.get("hold_min") or it.get("osc_hold_min") or 0) * 60 or mx_cfg      # v4.504 : duree propre au mode (oscillation, continuation)      # v4.503 : duree propre au mode oscillation
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
                try:
                    self._sus_manage(it, now)      # v4.532 : fin de la tendance soutenue -> façon de tenir le trade
                except Exception as e:
                    self.last_error = f"fin de zone : {type(e).__name__}: {e}"
                if it.get("tp_run_armed"):      # v4.434 : objectif atteint, le trade court avec son stop suiveur : la duree max ne s applique plus
                    continue
                if mx > 0 and now - float(it.get("opened_at") or now) > mx:
                    try:
                        if int(self.cfg.get("SCALP_HOLD_RUN_ON", 1)) and self.manual.arm_trail_at_hold(it["id"]):      # v4.531 : en gain a la fin de la duree : le suiveur prend le relais
                            self._ev("sortie", it["ticker"], f"#{it['id']} duree maximale {mx / 60:.0f} min atteinte EN GAIN : pas de fermeture, le suiveur s'enclenche")
                            continue
                        self.manual.close(it["id"], "SCALP DUREE MAX")
                        self._ev("sortie", it["ticker"], f"#{it['id']} ferme : duree maximale {mx / 60:.0f} min atteinte")
                    except Exception as e:
                        self.last_error = f"fermeture duree : {e}"

    def _open_count(self):
        return sum(1 for it in self.manual.items.values()
                   if it.get("strategy_source") == STRATEGY and it.get("status") in ("scheduled", "opening", "open", "closing"))

    def _btc_change(self):
        try:
            c = self.bot._mtf_candles("BTC", "15m", 60)      # v4.506 : 60 (et non 30) : le cache est partage, BTC doit rester evaluable (EMA50 15 min)
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
            self._beta_kick()       # v4.479 : betas a jour en arriere-plan
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
                    self._clear_err(f"bougies {t} :")
                except Exception as e:
                    self.last_error = f"bougies {t} : {type(e).__name__}: {e}"
                    continue
                try:
                    if int(self.cfg.get("SCALP_SUS_ON", 1)):
                        self._sus_update(t, c5, K)      # v4.532 : etat de la tendance soutenue (entree / sortie de zone), tous les actifs analyses
                except Exception as e:
                    self.last_error = f"tendance soutenue {t} : {type(e).__name__}: {e}"
                sig, motif = evaluate(c5, c15, c1h, btc, K, self._flow(t))
                if sig:
                    self._last_sig_ts = time.time()
                self.verdicts[t] = (time.time(), "analyse", ("signal valide : " + f"{sig['kind']} {sig['side']} note {sig['score']:.0f}") if sig else (motif or "aucun signal"))
                if not sig:
                    if "refuse" in motif:
                        self._ev("refus", t, motif)
                    if int(self.cfg.get("SCALP_CONT_ON", 1)):      # v4.504 : mode continuation (paper et live)
                        try:
                            self._cont_try(t, c5, c15, c1h, btc, K, mode)
                        except Exception as e:
                            self.last_error = f"continuation {t} : {type(e).__name__}: {e}"
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
        try:      # v4.461 : qualite de tendance du panier (mesure seule, aucun effet sur les decisions)
            ser = [v[-49:] for v in closes.values() if len(v) >= 49]
            if ser:
                idx = [sum(v[i] / v[0] for v in ser) / len(ser) * 100 for i in range(49)]
                reg["basket_er_4h"] = round(_eff_ratio(idx), 3)
                reg["basket_er_2h"] = round(_eff_ratio(idx[-25:]), 3)
                reg["basket_2h"] = round(idx[-1] / idx[-25] * 100 - 100, 3)
        except Exception:
            pass
        self.regime = reg
        return reg

    # ── v4.532 : tendance soutenue : etat par actif (entree / sortie de zone) et gestion des trades ouverts ──
    def _sus_update(self, t, c5, K):
        """Met a jour la phase de l actif : « soutenue » (mouvement tenu ~30 min sans repli) -> « fin » (sortie de zone, detectee) -> oubli apres 30 min.
        Aucune decision d entree ici : la prise du signal est dans _cont_try. Les autres modes continuent a analyser l actif dans tous les cas."""
        now = time.time()
        st = self.sus_state.get(t)
        m = sus_measure(c5, K)
        if m.get("ok"):
            if not st or st.get("phase") != "soutenue" or st.get("side") != m["side"]:
                st = {"phase": "soutenue", "side": m["side"], "since": now}
                self._ev("zone", t, f"tendance soutenue {m['side']} détectée : {m['mv_pct']:+.2f} % en {m['n'] * 5} min, efficacité {m['er']:.2f}")
            st.update({"ts": now, "last_ok": now, "why": "", "mv": m["mv_pct"], "er": m["er"], "pb": m["pb_atr"]})
            self.sus_state[t] = st
            return
        if st and st.get("phase") == "soutenue":
            ended, why = sus_end(c5, st["side"], K)
            if ended or now - st.get("last_ok", now) > 900:
                dur = (now - st["since"]) / 60.0
                self.sus_state[t] = {"phase": "fin", "side": st["side"], "since": st["since"], "ended": now, "ts": now,
                                     "why": why or "mouvement plus assez direct depuis 15 min", "dur_min": round(dur)}
                self._ev("zone", t, f"sortie de la tendance soutenue {st['side']} après {dur:.0f} min : {why or 'mouvement plus assez direct'} — retour aux autres modes (repli, rebond, cassure)")
            else:
                st["ts"], st["why"] = now, m["why"]
        elif st and st.get("phase") == "fin" and now - st.get("ended", now) > 1800:
            self.sus_state.pop(t, None)

    def _sus_manage(self, it, now):
        """Trade de tendance soutenue ouvert : si la zone se termine (recul ou clôtures sous la moyenne 9), la façon de le tenir change :
        EN GAIN -> suiveur serré enclenché (le gain est protégé, le trade court tant qu il progresse) ; en perte ou nul -> durée restante réduite
        (SCALP_SUS_END_HOLD_MIN), le stop reste en place. Lecture du cache de bougies (jamais de requete reseau ici)."""
        sc = it.get("scalp") or {}
        if (sc.get("cont") or {}).get("sub") != "soutenue" or it.get("sus_end_done"):
            return
        if now - self._sus_chk.get(it["id"], 0) < 20:
            return
        self._sus_chk[it["id"]] = now
        c5 = self.bot._mtf_candles(it["ticker"], "5m", 60, cache_only=True)
        if not c5:
            return
        K = self.thresholds()
        px = float(it.get("last_price") or 0) or None
        ended, why = sus_end(c5, it["direction"], K, price=px)
        if not ended:
            return
        en = float(it.get("entry_price") or 0)
        sg = 1 if it["direction"] == "long" else -1
        move = (sg * (px - en) / en * 100) if (px and en) else 0.0
        with self.manual.lock:
            it["sus_end_done"] = True
            if move > 0 and it.get("tp_run"):
                tr = float(it.get("tp_run_trail_pct") or 0)
                it["tp_run_trail_pct"] = min(tr, float(self.cfg.get("SCALP_SUS_END_TRAIL_PCT", 0.15))) if tr > 0 else float(self.cfg.get("SCALP_SUS_END_TRAIL_PCT", 0.15))
                done = self.manual.arm_trail_at_hold(it["id"])
                self._ev("zone", it["ticker"], f"#{it['id']} sortie de la tendance soutenue ({why}) EN GAIN ({move:+.2f} %) : suiveur serré {'enclenché' if done else 'prêt'}, le gain est protégé")
            else:
                hm = (now - float(it.get("opened_at") or now)) / 60.0 + float(self.cfg.get("SCALP_SUS_END_HOLD_MIN", 15))
                it["hold_min"] = min(float(it.get("hold_min") or hm), hm)
                self._ev("zone", it["ticker"], f"#{it['id']} sortie de la tendance soutenue ({why}), trade à {move:+.2f} % : durée restante réduite à {self.cfg.get('SCALP_SUS_END_HOLD_MIN', 15):g} min, stop inchangé")
            self.manual._save(it)

    def sus_summary(self):
        now = time.time()
        rows = []
        for t, st in self.sus_state.items():
            rows.append({"ticker": t, "phase": st.get("phase"), "side": st.get("side"), "since_min": int((now - st.get("since", now)) / 60),
                         "mv": st.get("mv"), "er": st.get("er"), "pb": st.get("pb"), "why": st.get("why"), "age_s": int(now - st.get("ts", now))})
        why = [{"ticker": t, "why": w[1], "age_s": int(now - w[0])} for t, w in self.sus_why.items() if now - w[0] < 900 and t not in self.sus_state]
        return {"on": bool(int(self.cfg.get("SCALP_SUS_ON", 1))), "rows": sorted(rows, key=lambda r: (r["phase"] != "soutenue", r["ticker"])),
                "why": sorted(why, key=lambda r: r["ticker"])}

    def _cont_diag_summary(self):
        """v4.505 : raisons pour lesquelles la continuation n est pas prise, comptees sur les actifs analysés depuis 15 min."""
        now = time.time()
        cnt, rows = {}, []
        for t, (ts, m) in self.cont_diag.items():
            if now - ts > 900:
                continue
            k = m[:70] if m.startswith("signal refusé") else m.split(" (")[0]
            cnt[k] = cnt.get(k, 0) + 1
            rows.append({"ticker": t, "motif": m})
        return {"reasons": sorted(cnt.items(), key=lambda x: -x[1]), "rows": sorted(rows, key=lambda r: r["ticker"])}

    def macro_block(self):
        """v4.507 : texte de la pause si on est dans la fenetre d une annonce macro USD a fort impact (entrees bloquees, sorties inchangees), sinon None."""
        c = self.cfg
        if self.macro is None or not int(c.get("SCALP_MACRO_ON", 1)):
            return None
        try:
            self.macro.refresh_async()
            now = time.time()
            e = self.macro.active(now, float(c.get("SCALP_MACRO_BEFORE_MIN", 10)), float(c.get("SCALP_MACRO_AFTER_MIN", 20)))
            if not e:
                return None
            dt = (e["ts"] - now) / 60.0
            hh = datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%H:%M")
            return f"pause macro : {e['title']} à {hh} UTC ({'dans ' + format(dt, '.0f') + ' min' if dt > 0 else 'il y a ' + format(-dt, '.0f') + ' min'})"
        except Exception:
            return None

    def macro_state(self):
        c = self.cfg
        if self.macro is None:
            return {"on": False, "status": "module macro_events.py non déposé"}
        try:
            now = time.time()
            return {"on": bool(int(c.get("SCALP_MACRO_ON", 1))), "pause": self.macro_block(), "status": self.macro.status(),
                    "before": float(c.get("SCALP_MACRO_BEFORE_MIN", 10)), "after": float(c.get("SCALP_MACRO_AFTER_MIN", 20)),
                    "next": [{"title": e["title"], "utc": datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%d/%m %H:%M"), "in_min": int((e["ts"] - now) / 60), "src": e["src"]}
                             for e in self.macro.upcoming(now, 4)]}
        except Exception as e:
            return {"on": False, "status": f"erreur {type(e).__name__}"}

    def nosig_state(self):
        """v4.506 : alerte « aucun signal » : le Scalp est actif, le panier bouge d au moins SCALP_NOSIG_MOVE_PCT % sur 30 min et aucun signal
        valide n a ete vu depuis SCALP_NOSIG_MIN minutes. Sert le bandeau visible sur toutes les pages (acquittement cote page)."""
        c = self.cfg
        try:
            if not int(c.get("SCALP_ENABLED", 0)) or not int(c.get("SCALP_NOSIG_ON", 1)) or self.macro_block():      # v4.507 : pause macro voulue = pas d alerte
                return {"active": False}
            ok, _why = self.window_state()
            if not ok:
                return {"active": False}
            now = time.time()
            last = max(self._last_sig_ts, max((o[0] for o in list(self._opens)), default=0))
            mins = (now - last) / 60.0
            need = float(c.get("SCALP_NOSIG_MIN", 30))
            reg = self.regime or {}
            mv = reg.get("basket30")
            mneed = float(c.get("SCALP_NOSIG_MOVE_PCT", 0.5))
            moving = mv is not None and abs(mv) >= mneed
            mode = "live" if c.get("SCALP_MODE") == "live" else "paper"
            summ = self._cont_diag_summary()
            return {"active": bool(mins >= need and moving), "since_min": int(mins), "need_min": need, "move30": mv, "move_need": mneed,
                    "dir": ("baisse" if (mv or 0) < 0 else "hausse"), "episode": int(last), "mode": mode,
                    "label": reg.get("label"), "corr": reg.get("corr"), "defense": self.defense_state(mode),
                    "reasons": [[k, n] for k, n in summ["reasons"][:4]]}
        except Exception as e:
            return {"active": False, "error": f"{type(e).__name__}: {e}"}

    def _cont_open_count(self, mode):
        return sum(1 for it in self.manual.items.values()
                   if it.get("strategy_source") == STRATEGY and it.get("mode") == mode
                   and it.get("status") in ("scheduled", "opening", "open", "closing") and (it.get("scalp") or {}).get("kind") == "continuation")

    def _cont_try(self, t, c5, c15, c1h, btc, K, mode):
        """v4.504 : signal de continuation sur t ; s il est pris et que le marche le confirme, meme sens sur les actifs correles."""
        c = self.cfg
        sig, motif = cont_eval(c5, c15, c1h, btc, K, self._flow(t))
        if not sig and int(c.get("SCALP_SUS_ON", 1)):      # v4.532 : pas de repli / d elan : tendance soutenue (mouvement tenu ~30 min sans repli)
            s2, m2 = sustained_eval(c5, c15, c1h, btc, K, self._flow(t))
            if s2:
                sig, motif = s2, "ok"
            else:
                st_ = self.sus_state.get(t)
                if not st_ or st_.get("phase") != "soutenue":
                    self.sus_why[t] = (time.time(), m2 or "")
        if not sig:
            self.cont_diag[t] = (time.time(), motif or "aucun signal")      # v4.505 : raison de non-entree visible (diagnostic)
            if motif and "refuse" in motif:
                self._ev("refus", t, motif)
            return False
        self.cont_diag[t] = (time.time(), "signal de continuation")
        self._last_sig_ts = time.time()
        cap = int(c.get("SCALP_CONT_MAX_OPEN", 5))
        n_open = self._cont_open_count(mode)
        if cap > 0 and n_open >= cap:
            self._ev("refus", t, f"continuation {sig['side']} refusée : {n_open}/{cap} trades de continuation déjà ouverts")
            return True
        # v4.514 : le MENEUR n'entre que si le panier suit (meme seuil que la grappe). Constat 29-30/10 : 7 groupes confirmes +1,74 $, 8 meneurs seuls -2,26 $.
        # Signal refuse = suivi (_shadow_*) pour verifier apres coup si le refus etait juste. Etendue inconnue : on n'empeche pas (comportement d'avant).
        try:
            reg = self._regime()
            share = reg.get("up") if sig["side"] == "long" else reg.get("down")
            need = float(c.get("SCALP_CONT_BREADTH_MIN", 60)) / 100.0
            if reg.get("n") and share is not None:
                sig.setdefault("cont", {})["breadth_pct"] = round(share * 100)
                if int(c.get("SCALP_CONT_LEADER_BREADTH_ON", 1)) and share < need:
                    txt = f"marché non confirmé ({share * 100:.0f} % des actifs vont dans le sens, il en faut ≥ {need * 100:.0f} %) — le meneur n'entre que si le panier suit"
                    self._ev("refus", t, f"continuation {sig['side']} refusée : {txt}")
                    self.cont_diag[t] = (time.time(), "signal refusé — " + txt)
                    self._shadow_add(t, sig, share, need)
                    return True
        except Exception as e:
            self.last_error = f"étendue du panier : {type(e).__name__}: {e}"
        before = self._cont_open_count(mode)
        self._take(t, sig)
        if self._cont_open_count(mode) > before:
            self._cont_fanout(t, sig, c5, K, btc, mode)
        else:      # v4.506 : signal valide mais refuse plus loin (mode defensif, capital, pause...) : le motif exact est visible au diagnostic
            v = self.verdicts.get(t)
            if v and v[1] == "refus" and time.time() - v[0] < 10:
                self.cont_diag[t] = (time.time(), "signal refusé — " + str(v[2]))
        return True

    def _cont_fanout(self, t0, sig0, c5_0, K, btc, mode):
        """v4.504 : tendance confirmee par le panier (au moins SCALP_CONT_BREADTH_MIN % des actifs dans le sens) : meme sens sur les actifs
        correles a t0 (correlation 5 min >= SCALP_CONT_PEER_CORR), les plus forts d abord, dans la limite de SCALP_CONT_MAX_OPEN trades."""
        c = self.cfg
        if not int(c.get("SCALP_CONT_FANOUT_ON", 1)):
            return
        side = sig0["side"]
        now = time.time()
        if now - self._cont_fan.get((side, mode), 0) < 600:
            return
        reg = self._regime()
        share = reg.get("up") if side == "long" else reg.get("down")
        need = float(c.get("SCALP_CONT_BREADTH_MIN", 60)) / 100.0
        if not reg.get("n") or share is None:
            return
        if share < need:
            self._ev("refus", t0, f"continuation {side} : marché non confirmé ({share * 100:.0f} % des actifs vont dans le sens, il en faut ≥ {need * 100:.0f} %) — pas de trade sur les autres actifs")
            return
        self._cont_fan[(side, mode)] = now
        cap = int(c.get("SCALP_CONT_MAX_OPEN", 5))
        free = (cap - self._cont_open_count(mode)) if cap > 0 else 99
        if free <= 0:
            return
        sg = 1 if side == "long" else -1
        pc = float(c.get("SCALP_CONT_PEER_CORR", 0.5))
        open_t = {it.get("ticker") for it in self.manual.items.values()
                  if it.get("strategy_source") == STRATEGY and it.get("mode") == mode and it.get("status") in ("scheduled", "opening", "open", "closing")}
        cands = []
        for t2 in self.universe():
            if t2 == t0 or t2 in self.pending or t2 in open_t or now < self._cool.get((t2, mode), 0) or self._busy_same_mode(t2, mode):
                continue
            ch = (reg.get("ch30") or {}).get(t2)
            if ch is None or sg * ch <= 0:
                continue
            try:
                b5 = self.bot._mtf_candles(t2, "5m", 60)
                b15 = self.bot._mtf_candles(t2, "15m", 60)
                b1h = self.bot._mtf_candles(t2, "1h", 230)
            except Exception:
                continue
            if not (b5 and b15 and b1h):
                continue
            rho = _pair_corr(c5_0, b5)
            if rho is None or rho < pc:
                continue
            s2, _m = cont_eval(b5, b15, b1h, btc, K, self._flow(t2), side=side, follower=True)
            if not s2 and int(c.get("SCALP_SUS_ON", 1)):      # v4.532 : le suiveur peut aussi etre en tendance soutenue
                s2, _m = sustained_eval(b5, b15, b1h, btc, K, self._flow(t2), side=side)
            if not s2:
                continue
            s2["cont"].update({"peer_of": t0, "peer_corr": round(rho, 2), "breadth_pct": round(share * 100)})
            s2["reasons"] = list(s2["reasons"]) + [f"même sens que {t0} (corrélation 5 min {rho:.2f}), {share * 100:.0f} % du panier dans le sens"]
            cands.append((sg * ch, t2, s2))
        cands.sort(key=lambda x: -x[0])
        for _r, t2, s2 in cands:
            if free <= 0:
                break
            n0 = self._cont_open_count(mode)
            self._take(t2, s2)
            if self._cont_open_count(mode) > n0:
                free -= 1

    def _cluster_count(self, side, mode):
        w = float(self.cfg.get("SCALP_CLUSTER_WINDOW_MIN", 30)) * 60
        now = time.time()
        return sum(1 for ts, sd, md, _t in self._opens if sd == side and md == mode and now - ts <= w)

    def _retraced(self, t, side, rr):
        """v4.459 : (ok, recul %) — l actif a-t-il reculé d au moins rr % depuis l extrême de la fenêtre de grappe ?
        SHORT : le prix est remonté de rr % depuis son plus bas ; LONG : il est redescendu de rr % depuis son plus haut."""
        try:
            n = int(float(self.cfg.get("SCALP_CLUSTER_WINDOW_MIN", 30)) // 5) + 2
            cs = self.bot._mtf_candles(t, "5m", n)
            if not cs:
                return False, None
            px = float(cs[-1]["c"])
            if side == "short":
                ext = min(float(x["l"]) for x in cs)
                ret = (px - ext) / ext * 100
            else:
                ext = max(float(x["h"]) for x in cs)
                ret = (ext - px) / ext * 100
            return ret >= rr, ret
        except Exception:
            return False, None

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
            rr = float(c.get("SCALP_CLUSTER_RETRACE_PCT", 0) or 0)
            ok_rec, ret = (self._retraced(t, side, rr) if rr > 0 else (False, None))      # v4.459 : la grappe se debloque sur un recul de l actif
            if not ok_rec:
                why = (f"marché corrélé : grappe {side.upper()} pleine ({self._cluster_count(side, mode)}/{cm} ouvertures en {c.get('SCALP_CLUSTER_WINDOW_MIN', 30)} min)"
                       + (f" — on attend un recul de {rr:g} % de {t} depuis son {'plus bas' if side == 'short' else 'plus haut'} de la fenêtre (actuel {ret:+.2f} %)" if ret is not None else ""))
            else:
                sig["cluster_retrace"] = round(ret, 3)
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
            av = float(ms["accountValue"])
            if av <= 0:      # v4.460 : compte Unifie -> les fonds sont dans le solde USDC spot (le perps renvoie 0), comme dans l onglet Bilan
                bal = (self.bot.info.spot_user_state(w) or {}).get("balances", [])
                u = next((b for b in bal if b.get("coin") == "USDC"), None)
                av = float(u.get("total", 0)) if u else 0.0
            val = av - float(ms["totalMarginUsed"])
            wd = st.get("withdrawable")
            if wd is not None:
                val = min(val, float(wd)) if float(wd) > 0 else val
        except Exception as e:
            self.last_error = f"marge libre : {type(e).__name__}: {e}"
        self._fm = (time.time(), val)
        return val

    def _trend_quality(self, t, reg):
        """v4.461 : mesures de qualite de tendance a l entree, enregistrees dans le trade (aucun effet sur les decisions)."""
        out = {}
        try:
            b = [float(x["c"]) for x in (self.bot._mtf_candles("BTC", "5m", 60) or [])]
            if len(b) >= 49:
                out["er_btc_1h"] = round(_eff_ratio(b[-13:]), 3)
                out["er_btc_4h"] = round(_eff_ratio(b[-49:]), 3)
        except Exception:
            pass
        try:
            a = [float(x["c"]) for x in (self.bot._mtf_candles(t, "5m", 60) or [])]
            if len(a) >= 13:
                out["er_actif_1h"] = round(_eff_ratio(a[-13:]), 3)
                m = sum(a[-12:]) / 12
                out["ecart_moy_1h_pct"] = round((a[-1] - m) / m * 100, 3)
        except Exception:
            pass
        try:      # v4.489 : pente de la tendance et lignes inclinees (bougies 1 h, mesure seule)
            c1 = self.bot._mtf_candles(t, "1h", 60) or []
            if len(c1) >= 40:
                out.update({k: v for k, v in mtf.slope_features(c1[:-1] if len(c1) > 1 else c1).items() if v is not None})
        except Exception:
            pass
        for k in ("basket_er_2h", "basket_er_4h", "basket_2h"):
            if reg.get(k) is not None:
                out[k] = reg[k]
        return out

    # ── v4.460 : capital compose + objectif du jour + mode prudent ──
    @staticmethod
    def _row_net(d):
        fees = ((float(d["real_costs"]["fees"]) - float(d["real_costs"]["funding"])) if d.get("real_costs")
                else (float(d.get("fees_est") or 0) or float(d.get("notional_usd") or 0) * 0.0009))
        return float(d.get("pnl") or 0) - fees

    def _closed_rows(self, mode, with_manual=False):
        """Trades Scalp clotures du mode (plus recents d abord), cache 30 s. v4.486 : with_manual ajoute les trades manuels (tickets libres)."""
        ts, m, rows = getattr(self, "_rows_cache", (0, None, None))
        if rows is not None and m == (mode, with_manual) and time.time() - ts < 30:
            return rows
        rows = []
        try:
            for r in db.manual_history(800):
                d = r["data"]
                if r.get("status") == "closed" and d.get("mode") == mode and (d.get("strategy_source") == STRATEGY or (with_manual and d.get("strategy_source") == "manual")):
                    rows.append(d)
        except Exception as e:
            self.last_error = f"capital : {type(e).__name__}: {e}"
        rows.sort(key=lambda d: float(d.get("closed_at") or 0), reverse=True)
        self._rows_cache = (time.time(), (mode, with_manual), rows)
        return rows

    def hl_balance(self):
        """Solde du compte Hyperliquid comme dans l onglet Bilan : le plus eleve de la valeur du compte perps et du solde USDC spot
        (compte Unifie : le perps renvoie 0 et les fonds sont dans le spot). None si illisible."""
        try:
            w = self.manual._wallet()
            perps = 0.0
            try:
                perps = float((self.bot.info.user_state(w).get("marginSummary") or {}).get("accountValue") or 0)
            except Exception:
                pass
            spot = 0.0
            try:
                bal = (self.bot.info.spot_user_state(w) or {}).get("balances", [])
                u = next((b for b in bal if b.get("coin") == "USDC"), None)
                spot = float(u.get("total", 0)) if u else 0.0
            except Exception:
                pass
            v = max(perps, spot)
            return v if v > 0 else None
        except Exception as e:
            self.last_error = f"solde Hyperliquid : {type(e).__name__}: {e}"
            return None

    def _day_snapshot(self):
        """(valeur, ts) : solde Hyperliquid lu UNE FOIS par jour UTC, avant le premier trade du jour (relu apres un redemarrage)."""
        today = self.day_start_ts()
        snap = getattr(self, "_hl_snap", None)
        if snap is None or snap[0] != today:
            v = self.hl_balance()
            if v is not None:
                snap = self._hl_snap = (today, v, time.time())
            elif snap is None:
                return None
        return snap[1], snap[2]

    def capital_state(self, mode=None):
        """Capital courant = capital de depart + gains/pertes nets des trades clotures depuis le depart ; objectif du jour (jour UTC).
        Live avec SCALP_CAPITAL_FROM_HL : le depart est le solde Hyperliquid lu chaque jour (plafonne par SCALP_CAPITAL_CAP_USD)."""
        c = self.cfg
        mode = mode or ("live" if c.get("SCALP_MODE") == "live" else "paper")
        base = float(c.get("SCALP_CAPITAL_USD", 100.0))
        since = float(c.get("SCALP_COMPOUND_SINCE", 0) or 0)
        src = "réglage"
        from_hl = False
        if mode == "live" and int(c.get("SCALP_CAPITAL_FROM_HL", 0)):
            sn = self._day_snapshot()
            if sn:
                base, since, src, from_hl = sn[0], sn[1], "solde Hyperliquid (lu chaque jour)", True
                cap = float(c.get("SCALP_CAPITAL_CAP_USD", 0) or 0)
                if cap > 0 and base > cap:
                    base, src = cap, f"solde Hyperliquid plafonné à {cap:g} $"
            else:
                src = "réglage (solde Hyperliquid illisible)"
        wm = bool(int(float(c.get("SCALP_GOAL_INCLUDE_MANUAL", 1) or 0)))      # v4.486 : trades manuels comptes dans l objectif
        rows = self._closed_rows(mode, wm)
        midnight = self.day_start_ts()
        net_all = sum(self._row_net(d) for d in rows if float(d.get("closed_at") or 0) >= since)
        net_today = sum(self._row_net(d) for d in rows if float(d.get("closed_at") or 0) >= (midnight if from_hl else max(midnight, since)))
        td = [d for d in rows if float(d.get("closed_at") or 0) >= (midnight if from_hl else max(midnight, since))]
        gross_today = sum(float(d.get("pnl") or 0) for d in td)
        tdm = [d for d in td if d.get("strategy_source") == "manual"]
        man_net, man_n = sum(self._row_net(d) for d in tdm), len(tdm)
        now_cap = max(base + net_all, 0.0)
        day_start = max(now_cap - net_today, 0.01)
        goal = float(c.get("SCALP_GOAL_PCT", 0) or 0)
        day_pct = net_today / day_start * 100
        # v4.468 : resultats par jour (base du jour = SCALP_DAY_OFFSET_H) pour le selecteur du thermometre
        days = []
        try:
            off = float(c.get("SCALP_DAY_OFFSET_H", -4) or 0)
            b0 = float(c.get("SCALP_CAPITAL_USD", 100.0))
            s0 = float(c.get("SCALP_COMPOUND_SINCE", 0) or 0)
            rr = sorted(((float(d.get("closed_at") or 0), d) for d in rows if float(d.get("closed_at") or 0) >= s0), key=lambda x: x[0])
            by = {}
            for ts_, d in rr:
                by.setdefault(int((ts_ + off * 3600) // 86400), []).append(d)
            run = b0
            for k in sorted(by):
                lst = by[k]
                net = sum(self._row_net(d) for d in lst)
                gr = sum(float(d.get("pnl") or 0) for d in lst)
                lm = [d for d in lst if d.get("strategy_source") == "manual"]
                days.append({"d": datetime.fromtimestamp(k * 86400, timezone.utc).strftime("%Y-%m-%d"), "n": len(lst), "mn": len(lm), "mnet": round(sum(self._row_net(d) for d in lm), 2),
                             "net": round(net, 2), "gross": round(gr, 2), "fees": round(gr - net, 2), "start": round(max(run, 0.01), 2),
                             "pct": round(net / max(run, 0.01) * 100, 2)})
                run += net
            allnet = run - b0
            allsum = {"net": round(allnet, 2), "gross": round(sum(x["gross"] for x in days), 2), "fees": round(sum(x["fees"] for x in days), 2),
                      "n": sum(x["n"] for x in days), "mn": sum(x["mn"] for x in days), "mnet": round(sum(x["mnet"] for x in days), 2), "start": round(b0, 2), "pct": round(allnet / max(b0, 0.01) * 100, 2), "active_days": len(days)}
        except Exception:
            allsum = None
        return {"days": days[-120:], "all": allsum, "mode": mode, "compound": bool(int(c.get("SCALP_COMPOUND_ON", 0))), "base": round(base, 2), "now": round(now_cap, 2), "source": src,
                "since": since or None, "day_start": round(day_start, 2), "day_net": round(net_today, 2), "day_gross": round(gross_today, 2), "day_fees": round(gross_today - net_today, 2), "day_pct": round(day_pct, 2), "include_manual": wm, "day_manual_n": man_n, "day_manual_net": round(man_net, 2),
                "slots": int(float(c.get("SCALP_SLOTS", 0) or 0)),
                "slot_notional": (round(day_start * min(max(float(c.get("SCALP_SLOTS_USE_PCT", 100) or 100), 5.0), 100.0) / 100.0 * max(int(c.get("SCALP_LEVERAGE", 3)), 1) / int(float(c.get("SCALP_SLOTS", 0))), 2) if int(float(c.get("SCALP_SLOTS", 0) or 0)) > 0 else None),
                "goal_pct": goal, "goal_reached": bool(goal > 0 and day_pct >= goal)}

    def _capital_both(self):
        out = {}
        for m in ("paper", "live"):
            try:
                out[m] = self.capital_state(m)
            except Exception:
                out[m] = None
        return out

    def corr_btc(self, force=False):
        """v4.478 : correlation de chaque actif du Scalp avec BTC (rendements des bougies 1 h) : 7 j, 30 j, quand BTC baisse, beta 30 j. Cache 30 min."""
        now = time.time()
        cc = getattr(self, "_corr_cache", None)
        if cc and not force and now - cc[0] < 1800:
            return cc[1]
        end = int(now * 1000)

        def fetch(t):
            raw = self.bot.info.post("/info", {"type": "candleSnapshot", "req": {"coin": t, "interval": "1h", "startTime": end - 31 * 86400 * 1000, "endTime": end}})
            px = {int(x["t"]): float(x["c"]) for x in (raw or []) if int(x.get("T", 0)) <= end}
            return {ts: px[ts] / px[ts - 3600000] - 1 for ts in px if (ts - 3600000) in px and px[ts - 3600000] > 0}

        def pear(a, b):
            n = len(a)
            if n < 24:
                return None
            ma, mb = sum(a) / n, sum(b) / n
            va = sum((x - ma) ** 2 for x in a)
            vb = sum((y - mb) ** 2 for y in b)
            if va <= 0 or vb <= 0:
                return None
            return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5

        rows, errs = [], []
        try:
            btc = fetch("BTC")
        except Exception as e:
            return {"rows": [], "error": f"BTC illisible : {type(e).__name__}: {e}", "ts": now}
        for t in self.universe():
            if t == "BTC" or t.startswith("xyz:") or t == "PAXG":
                continue
            try:
                r = fetch(t)
                ts = sorted(set(r) & set(btc))
                a = [r[k] for k in ts]
                b = [btc[k] for k in ts]
                n7 = 168
                dn = [(x, y) for x, y in zip(a, b) if y < 0]
                vb = sum((y - sum(b) / len(b)) ** 2 for y in b) if b else 0
                beta = (sum((x - sum(a) / len(a)) * (y - sum(b) / len(b)) for x, y in zip(a, b)) / vb) if vb > 0 and len(a) > 24 else None
                c30, c7 = pear(a, b), pear(a[-n7:], b[-n7:])
                cdn = pear([x for x, _ in dn], [y for _, y in dn]) if len(dn) >= 24 else None
                rows.append({"t": t, "corr7": None if c7 is None else round(c7, 2), "corr30": None if c30 is None else round(c30, 2),
                             "corr_btc_down": None if cdn is None else round(cdn, 2), "beta30": None if beta is None else round(beta, 2), "n": len(a)})
            except Exception as e:
                errs.append(f"{t} : {type(e).__name__}")
        rows.sort(key=lambda x: (x["corr30"] is None, x["corr30"] if x["corr30"] is not None else 9))
        out = {"rows": rows, "errors": errs, "ts": now}
        self._corr_cache = (now, out)
        self._beta = {r["t"]: r["beta30"] for r in rows if r.get("beta30") is not None}
        self._beta["BTC"] = 1.0
        return out

    def _beta_kick(self):
        """v4.479 : rafraichit les betas en arriere-plan (au plus toutes les 30 min), sans bloquer le scan."""
        cc = getattr(self, "_corr_cache", None)
        if (cc and time.time() - cc[0] < 1800) or getattr(self, "_beta_busy", False):
            return
        self._beta_busy = True

        def run():
            try:
                self.corr_btc(force=True)
            except Exception as e:
                self.last_error = f"beta : {type(e).__name__}: {e}"
            finally:
                self._beta_busy = False
                if not getattr(self, "_corr_cache", None):
                    self._corr_cache = (time.time() - 900, {"rows": [], "errors": ["echec"], "ts": time.time()})     # reessai dans 15 min
        threading.Thread(target=run, daemon=True).start()

    def beta_of(self, t):
        """Beta 30 j de l actif par rapport a BTC (None si inconnu)."""
        return (getattr(self, "_beta", None) or {}).get(t)

    def btc_exposure(self, mode):
        """v4.479 : exposition equivalente BTC des positions Scalp ouvertes = somme de (notionnel x beta), par sens. Beta inconnu = 1."""
        lg = sh = nl = ns = 0.0
        cl = cs_ = 0
        unknown = []
        for it in list(self.manual.items.values()):
            if it.get("strategy_source") != STRATEGY or it.get("mode") != mode or it.get("status") not in ("opening", "open", "closing"):
                continue
            tk = it.get("ticker")
            b = self.beta_of(tk)
            if b is None:
                unknown.append(tk)
                b = 1.0
            nt = float(it.get("notional_usd") or 0)
            if it.get("direction") == "long":
                lg += nt * b; nl += nt; cl += 1
            else:
                sh += nt * b; ns += nt; cs_ += 1
        cc = getattr(self, "_corr_cache", None)
        return {"long": round(lg, 2), "short": round(sh, 2), "net": round(lg - sh, 2), "notional_long": round(nl, 2), "notional_short": round(ns, 2),
                "n_long": cl, "n_short": cs_, "unknown": sorted(set(unknown)), "age_min": (round((time.time() - cc[0]) / 60) if cc else None)}

    def beta_adjust(self, t, side, notional, mode):
        """v4.479 : (notionnel, refus) apres les deux options bêta (desactivees par defaut).
        SCALP_BETA_SIZE_ON : si beta > SCALP_BETA_REF, taille x REF / beta (jamais augmentee), plancher SCALP_BETA_MIN_FACTOR.
        SCALP_BETA_CAP_PCT : refuse si l exposition equivalente BTC dans ce sens depasserait N % du capital courant (0 = pas de plafond)."""
        c = self.cfg
        b = self.beta_of(t)
        lo = 10.5 if mode == "live" else 1.0
        if int(c.get("SCALP_BETA_SIZE_ON", 0)) and b is not None and b > 0:
            ref = float(c.get("SCALP_BETA_REF", 1.0) or 1.0)
            if b > ref:
                f = max(ref / b, float(c.get("SCALP_BETA_MIN_FACTOR", 0.4)))
                notional = max(notional * f, lo)
        cap = float(c.get("SCALP_BETA_CAP_PCT", 0) or 0)
        if cap > 0:
            cap_usd = self.capital_state(mode)["now"] * cap / 100.0
            ex = self.btc_exposure(mode)["short" if side == "short" else "long"]
            add = notional * (b if b is not None else 1.0)
            if ex + add > cap_usd + 0.01:
                return notional, (f"exposition BTC {side.upper()} : {ex:.0f} $ déjà + {add:.0f} $ (bêta {b if b is not None else '?'}) > plafond {cap_usd:.0f} $ ({cap:g} % du capital)")
        return notional, None

    def diag_line(self, t):
        """v4.470 : ligne du diagnostic d entree pour un actif : pourquoi le Scalp crypto entre ou n entre pas."""
        c = self.cfg
        try:
            if not int(c.get("SCALP_ENABLED", 0)):
                return "Scalp désactivé (rien ne s'ouvre)"
            if t not in self.universe():
                return "hors de l'univers du Scalp (top volume + actifs suivis)"
            mode = "live" if c.get("SCALP_MODE") == "live" else "paper"
            parts = []
            ok, why = self.window_state()
            if not ok:
                parts.append(f"⏸ {why}")
            cd = self._cool.get((t, mode), 0) - time.time()
            if cd > 0:
                parts.append(f"⏳ pause sur cet actif encore {cd / 60:.0f} min")
            pm_ = self.macro_block()
            if pm_:
                parts.append(f"⏸ {pm_}")
            if self._busy_same_mode(t, mode):
                parts.append("position Scalp déjà ouverte (live : une seule par actif)")
            if t in self.pending:
                parts.append("signal en attente de confirmation")
            v = self.verdicts.get(t)
            if v:
                age = time.time() - v[0]
                parts.append(f"dernier verdict ({age / 60:.0f} min) : {v[2]}")
            cd_ = self.cont_diag.get(t)
            if cd_ and int(c.get("SCALP_CONT_ON", 1)):      # v4.505
                parts.append(f"continuation ({(time.time() - cd_[0]) / 60:.0f} min) : {cd_[1]}")
            else:
                parts.append("pas encore évalué depuis le démarrage")
            try:
                cs = self.capital_state(mode)
                if cs["goal_reached"]:
                    rec = self._asset_record(mode).get(t)
                    safe = rec and rec[0] >= int(float(c.get("SCALP_SAFE_MIN_TRADES", 6))) and rec[1] >= float(c.get("SCALP_SAFE_MIN_WINRATE", 55)) and rec[2] > 0
                    parts.append("objectif atteint : " + ("actif jugé sûr" if safe else "actif PAS assez sûr (mode prudent)"))
            except Exception:
                pass
            dfn = self.defense_state(mode)
            if dfn:
                parts.append(f"🛡 mode défensif : {dfn}")
            reg = self.regime or {}
            if reg.get("n"):
                parts.append(f"marché {reg.get('label')} (corr {reg.get('corr', 0):.2f})")
            return " · ".join(parts)
        except Exception as e:
            return f"indisponible ({type(e).__name__}: {e})"

    def compound_notional(self, mode):
        """Taille d un trade (notionnel $) = capital courant x SCALP_COMPOUND_PCT %, ou None si le capital compose est desactive."""
        c = self.cfg
        slots = int(float(c.get("SCALP_SLOTS", 0) or 0))
        if slots > 0:      # v4.472 : N emplacements ; taille identique = capital du DEBUT de journee x levier / N (le capital est repris chaque jour)
            cs = self.capital_state(mode)
            use = min(max(float(c.get("SCALP_SLOTS_USE_PCT", 100) or 100), 5.0), 100.0) / 100.0      # part du capital engagee (le reste = reserve)
            return max(cs["day_start"] * use * max(int(c.get("SCALP_LEVERAGE", 3)), 1) / slots, 10.5 if mode == "live" else 1.0)
        if not int(c.get("SCALP_COMPOUND_ON", 0)):
            return None
        cs = self.capital_state(mode)
        lo = 10.5 if mode == "live" else 1.0
        return max(cs["now"] * float(c.get("SCALP_COMPOUND_PCT", 100)) / 100.0, lo)

    def committed_margin(self, mode):
        """Marge engagee par les trades Scalp ouverts du mode."""
        return sum(float(it.get("notional_usd") or 0) / max(int(it.get("leverage") or 1), 1)
                   for it in self.manual.items.values()
                   if it.get("strategy_source") == STRATEGY and it.get("mode") == mode and it.get("status") in ("opening", "open", "closing"))

    def _asset_record(self, mode):
        """{actif : (n, taux de reussite %, net $)} sur les SCALP_SAFE_WINDOW derniers trades de chaque actif, cache 60 s."""
        ts, m, rec = getattr(self, "_rec_cache", (0, None, None))
        if rec is not None and m == mode and time.time() - ts < 60:
            return rec
        win = max(int(float(self.cfg.get("SCALP_SAFE_WINDOW", 20))), 3)
        per = {}
        for d in self._closed_rows(mode):
            per.setdefault(d.get("ticker"), [])
            if len(per[d.get("ticker")]) < win:
                per[d.get("ticker")].append(d)
        rec = {t: (len(v), sum(1 for d in v if self._row_net(d) > 0) / len(v) * 100, sum(self._row_net(d) for d in v)) for t, v in per.items() if v}
        self._rec_cache = (time.time(), mode, rec)
        return rec

    def safe_assets(self, mode):
        c = self.cfg
        mn, wr = int(float(c.get("SCALP_SAFE_MIN_TRADES", 6))), float(c.get("SCALP_SAFE_MIN_WINRATE", 55))
        return sorted(t for t, (n, w, net) in self._asset_record(mode).items() if n >= mn and w >= wr and net > 0)

    def defense_state(self, mode):
        """v4.477 : mode defensif (memes filtres que l objectif atteint) quand le Scalp est en difficulte. Texte ou None.
        - SCALP_DEFENSE_DAY_PCT = X : net du jour <= -X % du capital de debut de journee (jusqu a la fin de la journee ou retour au-dessus) ;
        - SCALP_DEFENSE_LOSSES = N : N pertes de suite, pendant SCALP_DEFENSE_MIN minutes apres la derniere cloture (0 = desactive)."""
        c = self.cfg
        try:
            x = float(c.get("SCALP_DEFENSE_DAY_PCT", 0) or 0)
            if x > 0:
                cs = self.capital_state(mode)
                if cs["day_pct"] <= -x:
                    return f"journée à {cs['day_pct']:+.2f} % (seuil -{x:g} %)"
            n = int(float(c.get("SCALP_DEFENSE_LOSSES", 0) or 0))
            if n > 0:
                st = self.stats(mode)
                lc = st.get("last_closed")
                if st["streak_losses"] >= n and lc and time.time() - float(lc) < float(c.get("SCALP_DEFENSE_MIN", 60)) * 60:
                    return f"{st['streak_losses']} pertes de suite (défensif {c.get('SCALP_DEFENSE_MIN', 60)} min)"
        except Exception:
            return None
        return None

    def _defense_soft(self, t, sig, mode, why_def):
        """v4.483 : mode defensif VIGILANT. N interdit pas les entrees : verifie que le trade a de la place pour se
        developper et qu il n est pas dans le bruit, exige plus de qualite, et reduit la taille.
        Retourne (texte du refus ou None, facteur de taille)."""
        c = self.cfg
        pre = f"vigilance ({why_def}) : "
        sl = float(c.get("SCALP_CONT_SL_PCT", 0.2)) if sig.get("kind") == "continuation" else float(c.get("SCALP_SL_PCT", 0.65))
        size = max(0.1, min(1.0, float(c.get("SCALP_DEFENSE_SIZE_PCT", 60) or 100) / 100.0))
        # 1) amplitude disponible avant la zone suivante (mouvements repetes : il faut de la place pour le TP, avec marge)
        rx = float(c.get("SCALP_DEFENSE_ROOM_X", 1.5) or 0)
        room = float(sig.get("room") or 0)
        if rx > 0 and room < rx * sl:
            return pre + f"amplitude disponible {room:.2f} % < {rx:g} × le stop ({sl:g} % → {rx * sl:.2f} %)", size
        # 2) stop dans le bruit : le stop doit depasser k x ATR 5 min
        kx = float(c.get("SCALP_DEFENSE_SL_ATR", 1.0) or 0)
        a5, px = sig.get("atr5"), sig.get("price")
        if kx > 0 and a5 and px:
            a5p = a5 / px * 100.0
            if sl < kx * a5p:
                return pre + f"stop {sl:g} % dans le bruit (ATR 5 min {a5p:.2f} % × {kx:g} = {kx * a5p:.2f} %)", size
        # 3) sens / qualite d entree
        if sig.get("counter"):
            if not int(float(c.get("SCALP_DEFENSE_COUNTER", 1) or 0)):
                return pre + "contre-tendance non admise", size
            # v4.485 : contre-tendance deja securisee par le moteur (rebond, zone >= 2, flux, BTC, note) : admise, taille encore reduite
            size = min(size, max(0.1, float(c.get("SCALP_DEFENSE_COUNTER_SIZE_PCT", 40) or 40) / 100.0))
        ctx = sig.get("ctx") or {}
        fb = ctx.get("flow_buy")
        if sig.get("kind") == "cassure":
            # entree tendancielle (cassure) : l alignement avec la tendance (H1, ou 15 min si H1 neutre) est deja garanti
            # par le moteur (sig["counter"] False) ; on exige en plus un flux dans le sens.
            if fb is not None and ((sig["side"] == "long" and fb < 0.55) or (sig["side"] == "short" and fb > 0.45)):
                return pre + f"cassure sans flux dans le sens ({fb * 100:.0f} % d'achats agressifs)", size
        elif sig.get("kind") != "continuation":      # v4.504 : la continuation n a pas de zone horizontale
            mt = int(float(c.get("SCALP_SAFE_MIN_TOUCHES", 2) or 0))
            if mt > 0 and int(sig.get("touches") or 1) < mt:
                return pre + f"zone testée {int(sig.get('touches') or 1)} fois (il en faut ≥ {mt})", size
        # 4) actif : on n ecarte que ceux dont l historique recent est clairement mauvais
        rec = self._asset_record(mode).get(t)
        mn = int(float(c.get("SCALP_SAFE_MIN_TRADES", 6)))
        if rec and rec[0] >= mn and rec[2] < 0 and rec[1] < float(c.get("SCALP_SAFE_MIN_WINRATE", 55)):
            return pre + f"{t} a un historique négatif ({rec[1]:.0f} % de réussite, {rec[2]:+.2f} $ sur {rec[0]} trades)", size
        return None, size

    def _goal_refusal(self, t, sig, mode):
        """Entree trop risquee en mode prudent : objectif du jour atteint OU mode defensif. Texte du refus ou None."""
        c = self.cfg
        pre = None
        stop_all = False
        goal = float(c.get("SCALP_GOAL_PCT", 0) or 0)
        if goal > 0:
            cs = self.capital_state(mode)
            if cs["goal_reached"]:
                pre = f"objectif du jour atteint ({cs['day_pct']:+.2f} % / {cs['goal_pct']:g} %) : "
                stop_all = bool(int(c.get("SCALP_GOAL_STOP_ALL", 0)))
        if pre is None:
            why_def = self.defense_state(mode)
            if why_def:
                if int(float(c.get("SCALP_DEFENSE_SOFT", 1) or 0)):
                    return self._defense_soft(t, sig, mode, why_def)[0]      # v4.483 : defensif = vigilance, pas blocage
                pre = f"mode défensif ({why_def}) : "
        if pre is None:
            return None
        if stop_all:
            return pre + "plus d'entrée aujourd'hui"
        rec = self._asset_record(mode).get(t)
        mn, wr = int(float(c.get("SCALP_SAFE_MIN_TRADES", 6))), float(c.get("SCALP_SAFE_MIN_WINRATE", 55))
        if not rec or rec[0] < mn or rec[1] < wr or rec[2] <= 0:
            txt = "pas assez de trades" if (not rec or rec[0] < mn) else f"{rec[1]:.0f} % de réussite, {rec[2]:+.2f} $"
            return pre + f"{t} n'a pas un historique assez sûr ({txt}; il faut ≥ {mn} trades et ≥ {wr:g} % de réussite avec un net positif)"
        if int(c.get("SCALP_SAFE_REBOND_ONLY", 1)) and sig["kind"] != "rebond":
            return pre + "seuls les rebonds sont acceptés (pas les cassures)"
        mt = int(float(c.get("SCALP_SAFE_MIN_TOUCHES", 2) or 0))
        if mt > 0 and int(sig.get("touches") or 1) < mt:
            return pre + f"support/résistance testé {int(sig.get('touches') or 1)} fois (il en faut ≥ {mt})"
        mr = float(c.get("SCALP_SAFE_MIN_ROOM_PCT", 0) or 0)
        if mr > 0 and float(sig.get("room") or 0) < mr:
            return pre + f"place avant la zone suivante {float(sig.get('room') or 0):.2f} % < {mr:g} %"
        mx = float(c.get("SCALP_SAFE_MAX_SCORE", 90) or 0)
        if mx > 0 and float(sig.get("score") or 0) >= mx:
            return pre + f"note {sig['score']:.0f} ≥ {mx:g} (les notes les plus hautes ont perdu davantage)"
        return None

    def _asc_eval(self, t, sig):
        """v4.490 : « ascenseur » (le prix monte et descend dans la meme bougie sans avancer) et pente de la tendance 1 h.
        Retourne (refus ou None, info). Meme logique en paper et en live. Mesures : ER 1 min sur 30 bougies, amplitude des 3 dernieres
        bougies 5 min en ATR, pente / regularite 1 h, lignes inclinees."""
        c = self.cfg
        info = {"er_1m": None, "range_atr": None, "asc": False, "rules": [], "slope": {}}
        try:
            c1 = self.bot._mtf_candles(t, "1m", 40) or []
            if len(c1) >= 31:
                info["er_1m"] = round(_eff_ratio([float(x["c"]) for x in c1[-31:]]), 3)
        except Exception:
            pass
        try:
            c5 = self.bot._mtf_candles(t, "5m", 24) or []
            if len(c5) >= 15:
                a = mtf.atr(c5[:-3])
                if a:
                    info["range_atr"] = round(sum(float(x["h"]) - float(x["l"]) for x in c5[-3:]) / 3 / a, 2)
        except Exception:
            pass
        try:
            c1h = self.bot._mtf_candles(t, "1h", 60) or []
            if len(c1h) >= 40:
                info["slope"] = mtf.slope_features(c1h)
                info["_c1h"] = c1h
        except Exception:
            pass
        er_max = float(c.get("SCALP_ASC_ER_MAX", 0.08))
        info["asc"] = bool(info["er_1m"] is not None and info["er_1m"] < er_max)
        sg = 1 if sig["side"] == "long" else -1
        sl_ = info["slope"] or {}
        # 1) la pente 1 h ne doit pas aller contre le trade (quand la tendance est reguliere). v4.491 : fenetre COURTE (SCALP_SLOPE_BARS, 12 h)
        #    pour suivre la tendance actuelle ; la pente sur 36 h reste enregistree pour l analyse.
        nb = max(int(float(c.get("SCALP_SLOPE_BARS", 12) or 12)), 6)
        sv = sl_
        try:
            if nb != 36 and info.get("_c1h"):
                sv = mtf.slope_features(info["_c1h"], n=nb, lookback=max(48, nb))
        except Exception:
            sv = sl_
        info["pente_veto_atr"], info["pente_veto_r2"], info["pente_veto_bars"] = sv.get("pente_atr"), sv.get("pente_r2"), nb
        if int(c.get("SCALP_SLOPE_FILTER_ON", 1)) and sv.get("pente_atr") is not None and sv.get("pente_r2") is not None:
            if sv["pente_r2"] >= float(c.get("SCALP_SLOPE_MIN_R2", 0.4)) and sg * sv["pente_atr"] < 0:
                return (f"pente {nb} h contre le trade (pente {sv['pente_atr']:+.2f} ATR par bougie, régularité {sv['pente_r2']:.2f})", info)
            info["rules"].append(f"pente {nb} h ok ({sv['pente_atr']:+.2f})")
        # 2) ascenseur : pas de cassure ; un rebond doit etre pres de la ligne inclinee de support (long) / resistance (short)
        if info["asc"] and int(c.get("SCALP_ASC_ENTRY_ON", 1)):
            if sig["kind"] == "cassure":
                return (f"ascenseur (efficacité 1 min {info['er_1m']:.2f} < {er_max:g}) : cassure refusée, on attend un repli", info)
            near = float(c.get("SCALP_ASC_NEAR_LINE_PCT", 0) or 0)      # v4.492 : 0 = mesure seule (la ligne 1 h prolongee peut etre tres loin)
            d = sl_.get("lig_sup_dist_pct") if sig["side"] == "long" else sl_.get("lig_res_dist_pct")
            if near > 0 and d is not None and not (-0.15 <= d <= near):
                return (f"ascenseur : prix à {d:+.2f} % de la ligne inclinée de {'support' if sig['side'] == 'long' else 'résistance'} (maximum {near:g} %)", info)
            info["rules"].append("ascenseur" + (f" : ligne inclinée à {d:+.2f} %" if d is not None else ""))
        return None, info

    def _osc_eval(self, t, sig, asc):
        """v4.503 : MODE OSCILLATION (« ascenseur » exploite). Quand le prix oscille sans avancer (efficacite 1 min < SCALP_OSC_ER_MAX) et
        qu un REBOND se presente sur un bord de la boite des dernieres bougies 5 min (dans le sens de la pente 1 h, deja garanti par
        _asc_eval), le trade vise une part de l amplitude de la boite (SCALP_OSC_TP_SHARE_PCT), avec un stop juste hors de la boite et une
        duree courte ; au-dela de l objectif on laisse courir (stop suiveur serre). Meme logique en paper et en live. Retourne un dict ou None."""
        c = self.cfg
        if sig.get("kind") != "rebond" or not sig.get("price"):
            return None
        er = (asc or {}).get("er_1m")
        if er is None:
            try:
                c1 = self.bot._mtf_candles(t, "1m", 40) or []
                if len(c1) >= 31:
                    er = round(_eff_ratio([float(x["c"]) for x in c1[-31:]]), 3)
            except Exception:
                er = None
        if er is None or er >= float(c.get("SCALP_OSC_ER_MAX", 0.15)):
            return None
        try:
            c5 = self.bot._mtf_candles(t, "5m", 24) or []
        except Exception:
            return None
        n = max(int(float(c.get("SCALP_OSC_BOX_BARS", 12))), 6)
        if len(c5) < max(n, 15):
            return None
        seg = c5[-n:]
        hi, lo = max(float(x["h"]) for x in seg), min(float(x["l"]) for x in seg)
        price = float(sig["price"])
        if hi <= lo or price <= 0:
            return None
        amp = (hi - lo) / price * 100.0
        if not (float(c.get("SCALP_OSC_MIN_AMP_PCT", 0.5)) <= amp <= float(c.get("SCALP_OSC_MAX_AMP_PCT", 3.0))):
            return None
        pos = (price - lo) / (hi - lo)
        edge = float(c.get("SCALP_OSC_EDGE_PCT", 35)) / 100.0
        long_side = sig["side"] == "long"
        if (long_side and pos > edge) or ((not long_side) and pos < 1.0 - edge):
            return None
        a5p = (sig["atr5"] / price * 100.0) if sig.get("atr5") else amp / 4.0
        tp = min(max(float(c.get("SCALP_OSC_TP_SHARE_PCT", 50)) / 100.0 * amp, float(c.get("SCALP_OSC_MIN_TP_PCT", 0.25))), float(c.get("SCALP_OSC_MAX_TP_PCT", 1.2)))
        d_box = ((price - lo) if long_side else (hi - price)) / price * 100.0
        sl = min(max(d_box + 0.5 * a5p, max(0.3, a5p)), float(c.get("SCALP_OSC_SL_MAX_PCT", 0.8)))
        return {"er": er, "box_lo": lo, "box_hi": hi, "amp_pct": round(amp, 3), "pos": round(pos, 3), "tp_pct": round(tp, 3), "sl_pct": round(sl, 3),
                "trail_pct": float(c.get("SCALP_OSC_RUN_TRAIL_PCT", 0.15)), "hold_min": float(c.get("SCALP_OSC_MAX_HOLD_MIN", 30))}

    def _take(self, t, sig):
        c = self.cfg
        mode = "live" if c.get("SCALP_MODE") == "live" else "paper"
        why_macro = self.macro_block()      # v4.507 : pas de nouvelle entree autour d une annonce macro (paper et live, tous types, y compris la grappe)
        if why_macro:
            self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {why_macro}")
            return
        why_goal = self._goal_refusal(t, sig, mode)          # v4.460 : objectif du jour atteint -> entrees restreintes
        if why_goal:
            self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {why_goal}")
            return
        prud = sig.get("prudent_btc")
        if prud is not None:      # v4.497 : short pris alors que BTC monte -> flux vendeur exige
            fb_ = (sig.get("ctx") or {}).get("flow_buy")
            fmin = float(c.get("SCALP_SHORT_BTC_SOFT_FLOW", 55) or 0) / 100.0
            if fb_ is None or (1.0 - fb_) < fmin:
                self._ev("refus", t, f"rebond short refusé : BTC en hausse ({prud:+.2f} % / 1 h), flux vendeur insuffisant ({'inconnu' if fb_ is None else f'{(1.0 - fb_) * 100:.0f} %'} de ventes agressives, il en faut ≥ {fmin * 100:.0f} %)")
                return
        if not int(c.get("SCALP_BREAKOUT_ON", 1)) and sig.get("kind") == "cassure":      # v4.493 : cassures coupees (paper et live)
            self._ev("refus", t, "cassure " + sig["side"] + " refusée : cassures désactivées (SCALP_BREAKOUT_ON = 0)")
            return
        if int(c.get("SCALP_ASSET_BLOCK_ON", 0)):          # v4.493 : actif en mauvaise passe (sur ses derniers trades) mis de cote
            rec = self._asset_record(mode).get(t)
            mn, wr = int(float(c.get("SCALP_ASSET_BLOCK_MIN_TRADES", 15))), float(c.get("SCALP_ASSET_BLOCK_WINRATE", 42))
            if rec and rec[0] >= mn and rec[1] < wr and rec[2] < 0:
                hrs = float(c.get("SCALP_ASSET_BLOCK_HOURS", 24) or 0)      # v4.495 : un actif ecarte ne trade plus donc sa serie ne bouge plus : essai apres N heures
                last = next((float(d.get("closed_at") or 0) for d in self._closed_rows(mode) if d.get("ticker") == t), 0)
                if hrs > 0 and last and time.time() - last >= hrs * 3600:
                    rec = None
            if rec and rec[0] >= mn and rec[1] < wr and rec[2] < 0:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {t} en mauvaise passe ({rec[1]:.0f} % de réussite, {rec[2]:+.2f} $ sur ses {rec[0]} derniers trades ; seuil {wr:g} %)")
                return
        asc = None
        if int(c.get("SCALP_ASC_ON", 1)):          # v4.490 : ascenseur + pente (paper et live)
            why_asc, asc = self._asc_eval(t, sig)
            if why_asc:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {why_asc}")
                return
        slots = int(float(c.get("SCALP_SLOTS", 0) or 0))
        if slots > 0:      # v4.472 : emplacements pleins -> pas de nouvelle entree
            n_open = sum(1 for it in self.manual.items.values()
                         if it.get("strategy_source") == STRATEGY and it.get("mode") == mode and it.get("status") in ("scheduled", "opening", "open", "closing"))
            if n_open >= slots:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {n_open}/{slots} emplacements occupés")
                return
        cont = sig.get("kind") == "continuation"      # v4.504 : continuation = 1/5 du capital engage en marge, jamais reduit par la correlation
        base_notional = self.compound_notional(mode)
        if cont:
            cs_ = self.capital_state(mode)
            lev_ = max(int(c.get("SCALP_LEVERAGE", 3)), 1)
            base_notional = max(cs_["now"] * float(c.get("SCALP_CONT_MARGIN_PCT", 20)) / 100.0 * lev_, 10.5 if mode == "live" else 1.0)
            used_ = self.committed_margin(mode)
            if used_ + base_notional / lev_ > cs_["now"] + 0.5:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : capital engagé ({used_:.2f} $ de marge déjà utilisée + {base_notional / lev_:.2f} $ > capital {cs_['now']:.2f} $)")
                return
        elif base_notional is None:
            base_notional = float(c.get("SCALP_NOTIONAL_USD", 60.0))
        elif slots > 0:
            pass
        else:      # v4.460 : capital compose -> la marge de tous les trades ouverts ne depasse jamais le capital courant
            cs_ = self.capital_state(mode)
            new_margin = base_notional / max(int(c.get("SCALP_LEVERAGE", 3)), 1)
            used = self.committed_margin(mode)
            if used + new_margin > cs_["now"] + 0.5:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : capital engagé ({used:.2f} $ de marge déjà utilisée + {new_margin:.2f} $ > capital {cs_['now']:.2f} $)")
                return
        if mode == "live":
            need = base_notional / max(int(c.get("SCALP_LEVERAGE", 3)), 1)
            free = self._free_margin()
            if free is not None and free < need * 1.05 + 0.5:
                self._ev("refus", t, f"{sig['kind']} {sig['side']} non ouvert : marge libre insuffisante ({free:.2f} $ < {need:.2f} $ requis)")
                return
        reg = dict(self.regime) if self.regime.get("n") else {}
        notional = base_notional
        sp = float(c.get("SCALP_CORR_SIZE_PCT", 100))
        if reg.get("label") == "correle" and 0 < sp < 100 and not cont:      # v4.442 : marche correle = un seul pari ; on reduit la taille de chaque ligne
            notional = max(notional * sp / 100.0, 10.5 if mode == "live" else 1.0)
        if sig.get("prudent_btc") is not None:      # v4.497 : taille reduite pour un short a contre-courant de BTC
            notional = max(notional * max(0.1, min(1.0, float(c.get("SCALP_SHORT_BTC_SOFT_SIZE_PCT", 50) or 50) / 100.0)), 10.5 if mode == "live" else 1.0)
        notional, why_beta = self.beta_adjust(t, sig["side"], notional, mode)      # v4.479 : options beta (taille, plafond d exposition)
        if why_beta:
            self._ev("refus", t, f"{sig['kind']} {sig['side']} refusé : {why_beta}")
            return
        if self.defense_state(mode) and not (float(c.get("SCALP_GOAL_PCT", 0) or 0) > 0 and self.capital_state(mode)["goal_reached"]) \
                and int(float(c.get("SCALP_DEFENSE_SOFT", 1) or 0)):      # v4.483 : vigilance -> taille reduite
            f_ = self._defense_soft(t, sig, mode, "")[1]
            notional = max(notional * f_, 10.5 if mode == "live" else 1.0)
        sl_soft = float(c.get("SCALP_CONT_SL_PCT", 0.2)) if cont else float(c.get("SCALP_SL_PCT", 0.65))
        sl_hard = None
        tp_cont = be_arm_cont = trail_cont = None
        sus = cont and (sig.get("cont") or {}).get("sub") == "soutenue"
        if sus and sig.get("atr5") and sig.get("price"):
            # v4.532 : tendance soutenue : STOP = k x ATR 5 min (borne), objectif >= RR x stop (gain vise au moins RR fois la perte maximale),
            # stop de rentabilite arme a la moitie du stop, suiveur a la moitie du stop. Taille inchangee (1/5 du capital en marge).
            a5p = sig["atr5"] / sig["price"] * 100.0
            nz_p = float((sig.get("cont") or {}).get("sus_noise_pct") or 0)      # va-et-vient observes sur 1 h : le stop passe au-dessus
            new_sl = min(max(float(c.get("SCALP_SUS_SL_MIN", 0.25)), float(c.get("SCALP_SUS_SL_ATR", 1.3)) * a5p, float(c.get("SCALP_SUS_SL_NOISE_K", 1.3)) * nz_p), float(c.get("SCALP_SUS_SL_MAX", 0.8)))
            sig["cont"]["sl_noise_pct"] = round(nz_p, 3)
            sig.setdefault("cont", {})["sl_base"], sig["cont"]["atr5_pct"] = round(sl_soft, 3), round(a5p, 3)
            tp_cont = max(float(c.get("SCALP_CONT_TP_PCT", 0.6)), float(c.get("SCALP_SUS_RR", 2.0)) * new_sl)
            be_arm_cont = max(float(c.get("SCALP_BE_ARM_PCT", 0.4)), 0.5 * new_sl)
            trail_cont = max(float(c.get("SCALP_RUN_TRAIL_PCT", 0.2)), 0.5 * new_sl)
            sl_soft = new_sl
            sig["cont"]["rr"] = round(tp_cont / new_sl, 2)
        elif cont and int(c.get("SCALP_CONT_SL_ATR_ON", 1)) and sig.get("atr5") and sig.get("price"):
            # v4.511 : stop de la continuation ADAPTE A LA VOLATILITE (k x ATR 5 min, borne). TAILLE INCHANGEE (decision de l utilisateur) : la perte
            # maximale par trade augmente avec le stop. Objectif, armement du stop de rentabilite et suiveur sont mis a l echelle du stop.
            a5p = sig["atr5"] / sig["price"] * 100.0
            new_sl = min(max(sl_soft, float(c.get("SCALP_CONT_SL_ATR", 1.5)) * a5p), max(float(c.get("SCALP_CONT_SL_MAX_PCT", 0.8)), sl_soft))
            sig.setdefault("cont", {})["sl_base"], sig["cont"]["atr5_pct"] = round(sl_soft, 3), round(a5p, 3)
            if new_sl > sl_soft + 1e-9:
                tp_cont = max(float(c.get("SCALP_CONT_TP_PCT", 0.6)), float(c.get("SCALP_CONT_TP_RR", 2.0)) * new_sl)
                be_arm_cont = max(float(c.get("SCALP_BE_ARM_PCT", 0.4)), 0.5 * new_sl)
                trail_cont = max(float(c.get("SCALP_RUN_TRAIL_PCT", 0.2)), 0.5 * new_sl)
                sl_soft = new_sl
        if cont:
            sig.setdefault("cont", {})["sl_eff"] = round(sl_soft, 3)
        osc = self._osc_eval(t, sig, asc) if (int(c.get("SCALP_OSC_ON", 1)) and not cont) else None      # v4.503 : mode oscillation (paper et live)
        if osc:
            base_sl = sl_soft
            sl_soft = osc["sl_pct"]
            notional = max(notional * min(1.0, base_sl / sl_soft), 10.5 if mode == "live" else 1.0)      # meme perte maximale en $ (jamais plus gros que la taille normale)
        if (not osc) and (not cont) and asc and asc.get("asc") and int(c.get("SCALP_ASC_SL_ON", 1)) and sig.get("atr5") and sig.get("price"):
            a5p = sig["atr5"] / sig["price"] * 100.0      # stop elargi au-dessus du bruit, taille reduite pour garder la meme perte en $
            new_sl = min(max(sl_soft, float(c.get("SCALP_ASC_SL_ATR", 1.5)) * a5p), float(c.get("SCALP_ASC_SL_MAX_PCT", 1.5)))
            if new_sl > sl_soft + 1e-9:
                notional = max(notional * sl_soft / new_sl, 10.5 if mode == "live" else 1.0)
                asc["rules"].append(f"stop élargi {sl_soft:g} % → {new_sl:.2f} % (bruit ATR 5 min {a5p:.2f} %), taille réduite")
                sl_soft = new_sl
        if (not osc) and (not cont) and int(c.get("SCALP_ASC_CLOSE_STOP_ON", 1)) and (bool(asc and asc.get("asc")) or not int(c.get("SCALP_ASC_CLOSE_STOP_ASC_ONLY", 1))):      # v4.496 : stop sur cloture seulement en ascenseur
            sl_hard = min(sl_soft * float(c.get("SCALP_ASC_HARD_MULT", 2.0)), 50.0)
            if asc is not None:
                asc["rules"].append(f"stop sur clôture 5 min (secours {sl_hard:.2f} %)")
        cluster_before = self._cluster_count(sig["side"], mode)
        params = {"strategy": STRATEGY, "ticker": t, "direction": sig["side"], "market": "perp", "mode": mode,
                  "execution": "now", "leverage": int(c.get("SCALP_LEVERAGE", 3)),
                  "notional_usd": round(notional, 2),
                  "sl_pct": (sl_hard if sl_hard else sl_soft), "tp_pct": (osc["tp_pct"] if osc else (tp_cont if tp_cont else (float(c.get("SCALP_CONT_TP_PCT", 0.6)) if cont else float(c.get("SCALP_TP_PCT", 0.6))))),
                  "confirm_live": mode == "live"}
        if int(c.get("SCALP_RUN_ENABLED", 1)):     # v4.434 : laisser courir apres l objectif ; sortie si le prix recule de X % depuis son plus haut
            params.update({"tp_run": True, "tp_run_lock_pct": 0.0, "tp_run_trail_pct": (osc["trail_pct"] if osc else (trail_cont or float(c.get("SCALP_RUN_TRAIL_PCT", 0.2))))})
        if int(c.get("SCALP_BE_ENABLED", 0)):      # v4.445 : stop au seuil de rentabilite apres un gain de X %
            params.update({"be_arm_pct": (be_arm_cont or float(c.get("SCALP_BE_ARM_PCT", 0.4))), "be_lock_pct": float(c.get("SCALP_BE_LOCK_PCT", 0.1))})
        if int(c.get("SCALP_LADDER_ON", 0)):      # v4.524 : echelle de verrous TP1 / TP2 / TP3, puis suiveur
            lv = sorted({float(c.get("SCALP_LADDER_TP1", 0.56)), float(c.get("SCALP_LADDER_TP2", 0.6)), float(c.get("SCALP_LADDER_TP3", 1.0))})
            if params["tp_pct"] >= lv[0]:      # un objectif plus bas que TP1 (oscillation) garde sa propre sortie : l echelle ne l atteindrait pas
                params["lock_ladder"] = lv
                params["ladder_prog_pct"] = float(c.get("SCALP_LADDER_PROG_PCT", 0.03))
                params["tp_pct"] = lv[-1]      # TP3 : a l objectif le suiveur habituel prend le relais (laisser courir)
                params.update({"tp_run": True, "tp_run_lock_pct": 0.0,
                                "tp_run_trail_pct": (trail_cont or float(c.get("SCALP_RUN_TRAIL_PCT", 0.2)))})
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
                                   "atr15_pct": round(sig["atr15_pct"], 3), "atr5_pct": (round(sig["atr5"] / sig["price"] * 100, 3) if sig.get("atr5") and sig.get("price") else None), "counter": bool(sig.get("counter")), "prudent_btc": sig.get("prudent_btc"),
                                   "regime": {"label": reg.get("label", "inconnu"), "corr": reg.get("corr"), "up": reg.get("up"), "down": reg.get("down"),
                                              "basket30": reg.get("basket30"), "rel30": (round(reg["ch30"].get(t, 0) - reg["basket30"], 3) if reg.get("ch30") and reg.get("basket30") is not None else None),
                                              "cluster": cluster_before, "peers": sig.get("peers")},
                                   "ctx": sig.get("ctx"), "retest": bool(sig.get("retest")), "tq": self._trend_quality(t, reg),
                                   "touches": sig.get("touches"),      # v4.493 : mesures de la zone horizontale (aucun effet sur les decisions)
                                   "zone_width_pct": (round((sig["zone"][1] - sig["zone"][0]) / sig["price"] * 100, 3) if sig.get("zone") and sig.get("price") else None),
                                   "dist_level_pct": (round((sig["price"] / sig["level"] - 1) * 100, 3) if sig.get("level") and sig.get("price") else None)}
                    if asc is not None:
                        it["scalp"]["asc"] = {"er_1m": asc.get("er_1m"), "range_atr": asc.get("range_atr"), "ascenseur": asc.get("asc"),
                                              "pente_veto_atr": asc.get("pente_veto_atr"), "pente_veto_r2": asc.get("pente_veto_r2"), "pente_veto_bars": asc.get("pente_veto_bars"),
                                              "regles": " ; ".join(asc.get("rules") or []), "sl_mou_pct": round(sl_soft, 3),
                                              "sl_secours_pct": (round(sl_hard, 3) if sl_hard else None)}
                        it["scalp"]["tq"] = {**(it["scalp"].get("tq") or {}), **{k: v for k, v in (asc.get("slope") or {}).items() if v is not None}}
                    if osc:
                        it["scalp"]["osc"] = {k: osc[k] for k in ("er", "box_lo", "box_hi", "amp_pct", "pos", "tp_pct", "sl_pct", "hold_min")}
                        it["osc_hold_min"] = osc["hold_min"]
                    if cont:
                        it["scalp"]["cont"] = dict(sig.get("cont") or {})
                        it["hold_min"] = float(c.get("SCALP_CONT_MAX_HOLD_MIN", 60))
                    if sl_hard:
                        it["sl_close"], it["sl_soft_pct"], it["sl_hard_pct"] = True, round(sl_soft, 4), round(sl_hard, 4)
                    it["reasons"] = sig["reasons"]
                    self.manual._save(it)
        except Exception as e:
            self.last_error = f"annotation : {e}"
        try:      # v4.454 : cette ouverture est aussi proposee en ouverture LIVE manuelle pendant 5 min (liste des opportunites)
            zl, zh = (sig.get("zone") or (None, None))
            self.manual.record_candidates(STRATEGY, [{"ticker": t, "signal": sig["side"], "price": sig.get("price"), "confidence": sig["score"],
                                                      "support": zl, "resistance": zh,
                                                      "reasons": [f"Scalp {sig['kind']} note {sig['score']:.0f}"] + list(sig["reasons"])[:3]}])
        except Exception as e:
            self.last_error = f"opportunite scalp : {e}"
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
                "capital": dict(self.capital_state(), safe_assets=self.safe_assets(self.capital_state()["mode"])),
                "capital_modes": self._capital_both(),
                "btc_exposure": self.btc_exposure("live" if self.cfg.get("SCALP_MODE") == "live" else "paper"),
                "defense": self.defense_state("live" if self.cfg.get("SCALP_MODE") == "live" else "paper"),
                "regime": {k: v for k, v in self.regime.items()}, "peer_wait": [{"ticker": t, "side": w["side"], "age_min": round((time.time() - w["ts"]) / 60, 1)} for t, w in self.peer_wait.items()],
                "stats_paper": self.stats("paper"), "stats_live": self.stats("live"),
                "pending": [{"ticker": t, "side": p["sig"]["side"], "trigger": p["trigger"], "expires": p["expires"]} for t, p in self.pending.items()],
                "cont_diag": self._cont_diag_summary(), "sus": self.sus_summary(), "nosig": self.nosig_state(), "macro": self.macro_state(),
                "events": list(self.events)[:40], "error": self.last_error}
