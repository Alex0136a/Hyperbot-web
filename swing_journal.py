"""v4.359 — JOURNAL DU SWING (lecture seule pour le trading).

Chaque signal de retournement H4 detecte par le Swing (en zone Daily) est journalise avec sa decision :
  pris / candidat / ou la porte qui l a arrete (signal trop ancien, flux, stop trop loin, pas d objectif reel,
  gain/risque insuffisant...). Plusieurs heures puis jours plus tard, on mesure ce qu il serait devenu sur les
  bougies 1H : objectif ou stop touche d abord, gain et perte maximaux, rendement final, et le resultat en R
  (multiple du risque), brut puis net de frais. Aucun effet sur les entrees : sert a AMELIORER le Swing.
"""
HOUR_MS = 3_600_000


def evaluate_outcome(side, entry, sl, tp, candles_1h, start_ms, horizon_h=72):
    """Issue d un setup sur les bougies 1H cloturees apres start_ms (cloture de la bougie H4 du signal).
    Meme bougie : stop ET objectif touches -> stop d abord (prudence). Retourne None s il n y a pas de bougie."""
    sgn = 1 if side == "long" else -1
    cs = [c for c in candles_1h if start_ms <= c["t"] < start_ms + horizon_h * HOUR_MS]
    if not cs or not entry:
        return None
    out = {"outcome": "none", "hit_h": None}
    mfe = {24: 0.0, 72: 0.0}
    mae = {24: 0.0, 72: 0.0}
    hit = False
    for c in cs:
        hour = (c["t"] - start_ms) / HOUR_MS + 1
        fav = (c["h"] / entry - 1) * 100 if sgn > 0 else (1 - c["l"] / entry) * 100
        adv = (1 - c["l"] / entry) * 100 if sgn > 0 else (c["h"] / entry - 1) * 100
        for w in (24, 72):
            if hour <= w:
                mfe[w] = max(mfe[w], fav)
                mae[w] = max(mae[w], adv)
        if not hit and sl and tp:
            sl_hit = (c["l"] <= sl) if sgn > 0 else (c["h"] >= sl)
            tp_hit = (c["h"] >= tp) if sgn > 0 else (c["l"] <= tp)
            if sl_hit:
                out["outcome"], out["hit_h"], hit = "sl", hour, True
            elif tp_hit:
                out["outcome"], out["hit_h"], hit = "tp", hour, True

    def fin(w):
        sub = [c for c in cs if (c["t"] - start_ms) / HOUR_MS + 1 <= w]
        return None if not sub else round(sgn * (sub[-1]["c"] / entry - 1) * 100, 4)
    out.update({"mfe24": round(mfe[24], 4), "mae24": round(mae[24], 4), "mfe72": round(mfe[72], 4), "mae72": round(mae[72], 4),
                "fin24": fin(24), "fin72": fin(72), "covered_h": round((cs[-1]["t"] - start_ms) / HOUR_MS + 1, 1)})
    return out


def r_multiple(outcome, risk_pct, reward_pct, fin_pct):
    """Resultat en multiple du risque : objectif -> +gain/risque, stop -> -1, sinon rendement final / risque."""
    if not risk_pct:
        return None
    if outcome == "tp":
        return (reward_pct or 0.0) / risk_pct
    if outcome == "sl":
        return -1.0
    return None if fin_pct is None else fin_pct / risk_pct
