"""v4.517 — ORDRES SUR ZONE (paper par defaut, live si ZONEORD_LIVE = 1) : le bot programme lui-meme un rebond sur le support / la resistance Daily que le prix vient de quitter,
dans le sens de la tendance, puis annule l ordre si l evenement ne se produit pas ou le contredit. Les ordres sont des ordres programmes ordinaires
(type « rebond confirme » du Trading Manuel) : declenchement, SL, objectif et suiveur sont geres par le moteur manuel. Live : seulement apres bascule volontaire (ZONEORD_LIVE = 1) ; les ordres paper et live ne se melangent pas."""
import time
import threading

import mtf_analysis as mtf

DEFAULTS = {"ZONEORD_ON": 1, "ZONEORD_LIVE": 0, "ZONEORD_RECENT_ON": 1, "ZONEORD_REC_HOURS": 10, "ZONEORD_REC_MIN_VISITS": 2, "ZONEORD_REC_MAX_DIST_FRAC": 0.6, "ZONEORD_REC_MIN_RANGE_ATR": 3.0, "ZONEORD_REC_SL_ATR": 1.0, "ZONEORD_REC_BREAK_ATR": 1.0, "ZONEORD_REC_EXPIRY_H": 4, "ZONEORD_SYMBOLS": ["BTC", "ETH", "HYPE", "TAO", "SUI"], "ZONEORD_ZONE_MAX_WIDTH_ATR": 1.0, "ZONEORD_ZONE_H4": 1, "ZONEORD_MIN_TOUCHES": 2, "ZONEORD_MIN_DIST_ATR": 0.1, "ZONEORD_SL_MAX_ATR": 1.0, "ZONEORD_MAX_DIST_ATR": 1.5,
            "ZONEORD_CONFIRM_PCT": 0.3, "ZONEORD_SL_BUFFER_ATR": 0.3, "ZONEORD_SL_MAX_PCT": 6.0, "ZONEORD_MIN_RR": 1.5, "ZONEORD_MAX_RR": 4.0, "ZONEORD_TP_BUFFER_PCT": 0.3,
            "ZONEORD_NOTIONAL_USD": 60.0, "ZONEORD_LEVERAGE": 3, "ZONEORD_MAX_ORDERS": 5, "ZONEORD_EXPIRY_H": 48, "ZONEORD_BREAK_ATR": 0.5, "ZONEORD_TRAIL_PCT": 1.0}


class ZoneOrders:
    def __init__(self, bot, manual):
        self.bot, self.manual = bot, manual
        self.diag = {}          # ticker -> (ts, texte) : pourquoi un ordre est (ou n'est pas) programme
        self._last = 0
        self._busy = False
        self.why = ""

    def c(self, k):
        v = self.bot.cfg.get(k)
        return DEFAULTS[k] if v is None else v

    # ── lecture du marche (pure vis-a-vis du trading) ──
    def view(self, ticker, cache_only=False, recent=None):
        if recent is None:
            recent = bool(int(self.c("ZONEORD_RECENT_ON")))
        if recent:
            return self.view_recent(ticker, cache_only)
        return self.view_zones(ticker, cache_only)

    # ── v4.526 : lecture de la FENETRE RECENTE (sommet / base des dernieres heures, bougies 15 min) ──
    @staticmethod
    def window_levels(cs, tol, min_visits=2):
        """Pur : sommet et base d'une fenetre de bougies. Une « visite » = une bougie (ou un groupe de bougies a moins de 2 bougies d'ecart)
        dont la meche atteint le niveau a tol pres. Retourne (sommet, base) : dict {low, high, visits, react_pct, last_bars} ou None si moins de min_visits."""
        n = len(cs)
        top, base = max(c["h"] for c in cs), min(c["l"] for c in cs)
        R = top - base
        out = []
        for kind in ("top", "base"):
            lvl = top if kind == "top" else base
            idx = [i for i, c in enumerate(cs) if ((c["h"] >= lvl - tol) if kind == "top" else (c["l"] <= lvl + tol))]
            visits, last = [], None
            for i in idx:
                if last is not None and i - last <= 2:
                    visits[-1].append(i)
                else:
                    visits.append([i])
                last = i
            react = []
            for vs in visits:
                e = vs[-1]
                nxt = cs[e + 1:e + 5]
                if nxt and R > 0:
                    react.append(((lvl - min(c["l"] for c in nxt)) if kind == "top" else (max(c["h"] for c in nxt) - lvl)) / R * 100)
            if len(visits) >= min_visits and R > 0:
                out.append({"low": (lvl - tol) if kind == "top" else lvl, "high": lvl if kind == "top" else (lvl + tol), "level": lvl, "visits": len(visits),
                            "react_pct": round(sum(react) / len(react)) if react else 0, "last_bars_ago": n - 1 - visits[-1][-1]})
            else:
                out.append(None)
        return out[0], out[1]

    def view_recent(self, ticker, cache_only=False):
        hours = float(self.c("ZONEORD_REC_HOURS"))
        bars = max(int(hours * 4), 12)
        cs = self.bot._mtf_candles(ticker, "15m", max(bars + 30, 96), cache_only=cache_only)
        if not cs or len(cs) < bars + 14:
            self.why = f"historique 15 min insuffisant ({len(cs or [])} bougies)"
            return None
        if time.time() * 1000 - int(cs[-1]["t"]) > 3 * 900 * 1000:
            self.why = "bougies 15 min trop anciennes (non rafraîchies)"
            return None
        price = self.manual.price(ticker, "perp")
        if not price:
            return None
        a = mtf.atr(cs[-(bars + 14):]) or 0
        if a <= 0:
            return None
        win = cs[-bars:]
        tol = max(0.15 * a, 0.0005 * float(price))
        top, base = self.window_levels(win, tol, int(self.c("ZONEORD_REC_MIN_VISITS")))
        hi, lo = max(c["h"] for c in win), min(c["l"] for c in win)
        d1 = self.bot._mtf_candles(ticker, "1d", 230, cache_only=cache_only)
        tr = mtf.trend(d1 + [{**d1[-1], "c": float(price)}])[0] if d1 and len(d1) >= 60 else "inconnue"
        mom = (float(price) - win[-4]["c"]) / (hi - lo) * 100 if hi > lo else 0.0
        return {"recent": True, "price": float(price), "atr15": a, "top": top, "base": base, "win_hi": hi, "win_lo": lo, "R": hi - lo, "trend": tr,
                "mom_pct": round(mom), "hours": hours}

    def plan_recent(self, side, v, c):
        """Pur : ordre sur le sommet (short) ou la base (long) de la fenetre recente, ou (None, raison)."""
        p, a, R = v["price"], v["atr15"], v["R"]
        if side == "long" and v["trend"] == "baissiere":
            return None, "tendance Daily baissière : pas d'achat sur base"
        if side == "short" and v["trend"] == "haussiere":
            return None, "tendance Daily haussière : pas de vente sur sommet"
        if R < c["ZONEORD_REC_MIN_RANGE_ATR"] * a:
            return None, f"fenêtre {v['hours']:g} h trop étroite ({R / p * 100:.2f} % de hauteur, {R / a:.1f} ATR 15 min ; minimum {c['ZONEORD_REC_MIN_RANGE_ATR']:g})"
        z = v["top"] if side == "short" else v["base"]
        nom = "sommet" if side == "short" else "base"
        if z is None:
            return None, f"{nom} de la fenêtre {v['hours']:g} h : moins de {int(c['ZONEORD_REC_MIN_VISITS'])} visites (pic isolé)"
        edge = z["low"] if side == "short" else z["high"]
        zt = f"{nom} {z['level']:.6g} ({z['visits']} visites, rejet moyen {z['react_pct']} % de la fenêtre, dernière il y a {z['last_bars_ago'] * 15} min)"
        dist = (edge - p) if side == "short" else (p - edge)
        if dist <= 0:
            return None, f"prix déjà sur le {nom} ({zt})"
        if dist > c["ZONEORD_REC_MAX_DIST_FRAC"] * R:
            return None, f"{zt} trop loin ({dist / R * 100:.0f} % de la fenêtre, maximum {c['ZONEORD_REC_MAX_DIST_FRAC'] * 100:.0f} %)"
        far = z["high"] + c["ZONEORD_REC_SL_ATR"] * a if side == "short" else z["low"] - c["ZONEORD_REC_SL_ATR"] * a
        sl = abs(far - edge) / edge * 100
        if sl > c["ZONEORD_SL_MAX_PCT"]:
            return None, f"stop trop large ({sl:.2f} %) pour le {nom}"
        opp = v["base"] if side == "short" else v["top"]
        tx = (opp["high"] if side == "short" else opp["low"]) if opp is not None else (edge - 0.6 * R if side == "short" else edge + 0.6 * R)
        tp = abs(edge - tx) / edge * 100
        if tp < c["ZONEORD_MIN_RR"] * sl:
            return None, f"gain visé trop faible ({tp:.2f} % pour un stop de {sl:.2f} %, {zt})"
        tp = min(tp, c["ZONEORD_MAX_RR"] * sl)
        return {"edge": edge, "sl_pct": round(max(sl, 0.2), 3), "tp_pct": round(tp, 3), "zone": [z["low"], z["high"]], "dist_atr": round(dist / a, 2), "recent": True, "txt": zt}, None

    def contradiction_recent(self, item, v):
        zo = item.get("zone_order") or {}
        lo, hi = zo.get("zone", (None, None))
        if lo is None or v is None or not v.get("recent"):
            return None
        a, p, R = v["atr15"], v["price"], v["R"]
        short = item["direction"] == "short"
        brk = float(self.c("ZONEORD_REC_BREAK_ATR")) * a
        if short and p > hi + brk:
            return "sommet dépassé"
        if (not short) and p < lo - brk:
            return "base cassée"
        if (short and v["trend"] == "haussiere") or ((not short) and v["trend"] == "baissiere"):
            return "la tendance Daily est devenue contraire"
        z = v["top"] if short else v["base"]
        if z is None or abs(z["level"] - ((hi) if short else (lo))) > max(0.5 * a, 0.0007 * p):
            return f"le {'sommet' if short else 'bas'} n'est plus un niveau de la fenêtre {v['hours']:g} h (recalcul)"
        dist = (z["low"] - p) if short else (p - z["high"])
        if dist > 1.5 * float(self.c("ZONEORD_REC_MAX_DIST_FRAC")) * R:
            return "le prix s'est trop éloigné du niveau : le retour n'est plus attendu"
        return None

    def view_zones(self, ticker, cache_only=False):
        # Chargement PROPRE (hors cache seul) : le module ne depend plus d un autre pour avoir les bougies Daily ; la fonction ne rappelle
        # le reseau qu une fois par bougie Daily et par actif (temporisation interne).
        d1 = self.bot._mtf_candles(ticker, "1d", 230, cache_only=cache_only)
        if not d1 or len(d1) < 60:
            self.why = f"historique Daily insuffisant ({len(d1 or [])} bougies)"
            return None
        if time.time() * 1000 - int(d1[-1]["t"]) > 2.2 * 86400 * 1000:
            self.why = "bougies Daily trop anciennes (non rafraîchies)"
            return None
        price = self.manual.price(ticker, "perp")
        if not price:
            return None
        a = mtf.atr(d1) or 0
        if a <= 0:
            return None
        tr, _, _ = mtf.trend(d1 + [{**d1[-1], "c": float(price)}])
        zc, tfn = d1, "Daily"
        if int(self.c("ZONEORD_ZONE_H4")):
            # v4.525 : zones sur le H4 (pivot confirme en 8 h au lieu de 2 jours, 20 jours d'historique) ; volatilite (ATR), distances et stops restent mesures en ATR Daily
            h4 = self.bot._mtf_candles(ticker, "4h", 260, cache_only=cache_only)
            if not h4 or len(h4) < 60:
                self.why = f"historique H4 insuffisant ({len(h4 or [])} bougies)"
                return None
            if time.time() * 1000 - int(h4[-1]["t"]) > 2.2 * 4 * 3600 * 1000:
                self.why = "bougies H4 trop anciennes (non rafraîchies)"
                return None
            zc, tfn = h4, "H4"
        az = mtf.atr(zc) or 0
        wcap = float(self.c("ZONEORD_ZONE_MAX_WIDTH_ATR"))
        zones = mtf.find_zones(zc, lookback=self.bot.cfg.get("MTF_ZONE_LOOKBACK", 120), min_touches=int(self.c("ZONEORD_MIN_TOUCHES")),
                               max_width_atr=((wcap * a / az) if (wcap and az) else (self.bot.cfg.get("MTF_ZONE_MAX_WIDTH_ATR", 0) or None)))
        sup, res = mtf.nearest_zones(zones, float(price))
        for q in (sup, res):
            if q is not None:      # dernier contact : derniere bougie (hors les 2 dernieres) dont la meche touche la zone
                tol = 0.1 * az
                for cd in reversed(zc[:-2]):
                    if cd["l"] <= q["high"] + tol and cd["h"] >= q["low"] - tol:
                        q["age_h"] = round((time.time() * 1000 - int(cd["t"])) / 3600000.0)
                        break
        return {"price": float(price), "atr": a, "sup": sup, "res": res, "trend": tr, "zones": zones, "zone_tf": tfn}

    def h4_info(self, ticker, price, cache_only=True):
        """Observation seule (v4.520) : tendance H4 et position du prix par rapport a sa moyenne 20 H4. Jamais bloquant, None si indisponible."""
        try:
            h4 = self.bot._mtf_candles(ticker, "4h", 260, cache_only=cache_only)
            if not h4 or len(h4) < 60 or not price:
                return None
            tr, _, _ = mtf.trend(h4 + [{**h4[-1], "c": float(price)}])
            e20 = mtf.ema([x["c"] for x in h4], 20)
            return {"trend": tr, "vs_ema20_pct": round((float(price) / e20 - 1) * 100, 2) if e20 else None}
        except Exception:
            return None

    @staticmethod
    def plan(side, v, c):
        """Pur : ordre a programmer, ou (None, raison). side : long (rebond sur le support quitte) / short (sur la resistance quittee)."""
        p, a = v["price"], v["atr"]
        z, opp = (v["sup"], v["res"]) if side == "long" else (v["res"], v["sup"])
        if side == "long" and v["trend"] == "baissiere":
            return None, "tendance Daily baissière : pas d'achat sur support"
        if side == "short" and v["trend"] == "haussiere":
            return None, "tendance Daily haussière : pas de vente sur résistance"
        if z is None:
            return None, "aucun support" if side == "long" else "aucune résistance"
        edge = z["high"] if side == "long" else z["low"]            # bord de la zone du cote du prix
        dist = (p - edge) / a if side == "long" else (edge - p) / a
        _ag = z.get("age_h")
        zt = (f"{'support' if side == 'long' else 'résistance'} {v.get('zone_tf', '')} {z['low']:.6g}–{z['high']:.6g} ({z.get('touches', '?')} contacts"
              + (f", dernier il y a {_ag / 24:.1f} j" if _ag is not None and _ag >= 36 else (f", dernier il y a {_ag} h" if _ag is not None else "")) + ")")
        if dist < 0:
            return None, f"prix dans {zt}" if z["low"] <= p <= z["high"] else f"prix au-delà de {zt}"
        if dist < c["ZONEORD_MIN_DIST_ATR"]:
            return None, f"prix trop près de {zt} ({dist:.2f} ATR Daily, minimum {c['ZONEORD_MIN_DIST_ATR']:g})"
        if dist > c["ZONEORD_MAX_DIST_ATR"]:
            return None, f"{zt} trop loin ({dist:.1f} ATR Daily, maximum {c['ZONEORD_MAX_DIST_ATR']:g})"
        far = z["low"] - c["ZONEORD_SL_BUFFER_ATR"] * a if side == "long" else z["high"] + c["ZONEORD_SL_BUFFER_ATR"] * a
        sl_dist = abs(edge - far)
        if c.get("ZONEORD_SL_MAX_ATR", 0) > 0:
            sl_dist = min(sl_dist, c["ZONEORD_SL_MAX_ATR"] * a)      # v4.523 : le risque dépend de la volatilité, pas de la largeur de la zone
        sl = sl_dist / edge * 100
        if sl > c["ZONEORD_SL_MAX_PCT"]:
            return None, f"stop trop large ({sl:.1f} %, maximum {c['ZONEORD_SL_MAX_PCT']:g} %) pour {zt}, ATR Daily {a / edge * 100:.1f} % du prix"
        buf = c["ZONEORD_TP_BUFFER_PCT"] / 100.0
        if opp is not None:
            tpx = opp["low"] * (1 - buf) if side == "long" else opp["high"] * (1 + buf)
            tp = abs(tpx - edge) / edge * 100
        else:
            tp = 2 * sl
        if (side == "long" and opp is not None and opp["low"] <= edge) or (side == "short" and opp is not None and opp["high"] >= edge):
            tp = 2 * sl
        if tp < c["ZONEORD_MIN_RR"] * sl:
            return None, f"gain visé trop faible ({tp:.2f} % pour un stop de {sl:.2f} %, {zt})"
        tp = min(tp, c["ZONEORD_MAX_RR"] * sl)          # objectif plafonne : la zone opposee peut etre tres loin ; ensuite le suiveur laisse courir
        return {"edge": edge, "sl_pct": round(max(sl, 0.2), 3), "tp_pct": round(tp, 3), "zone": [z["low"], z["high"]], "dist_atr": round(dist, 2)}, None

    def contradiction(self, item, v):
        """Pur : raison d'annuler un ordre programme (zone cassee, tendance retournee, zone disparue), ou None."""
        zo = item.get("zone_order") or {}
        if zo.get("recent"):
            return self.contradiction_recent(item, v)
        lo, hi = zo.get("zone", (None, None))
        if lo is None or v is None or v.get("recent"):
            return None
        a, p = v["atr"], v["price"]
        brk = float(self.c("ZONEORD_BREAK_ATR")) * a
        long_ = item["direction"] == "long"
        if long_ and p < lo - brk:
            return "support cassé"
        if not long_ and p > hi + brk:
            return "résistance cassée"
        edge = hi if long_ else lo
        dist = (p - edge) / a if long_ else (edge - p) / a
        if dist > 1.5 * float(self.c("ZONEORD_MAX_DIST_ATR")):
            return f"le prix s'est trop éloigné de la zone ({dist:.1f} ATR Daily) : le retour n'est plus attendu"
        if (long_ and v["trend"] == "baissiere") or (not long_ and v["trend"] == "haussiere"):
            return "la tendance Daily est devenue contraire"
        z = v["sup"] if long_ else v["res"]
        if z is None or abs((z["high"] if long_ else z["low"]) - (hi if long_ else lo)) > 0.75 * a:
            if not any(abs((q["high"] if long_ else q["low"]) - (hi if long_ else lo)) <= 0.75 * a for q in v["zones"]):
                return "la zone n'existe plus (zones recalculées)"
        return None

    def still_valid(self, item):
        """Appele par le moteur manuel au declenchement : (True, '') ou (False, raison)."""
        try:
            v = self.view(item["ticker"], cache_only=True, recent=bool((item.get("zone_order") or {}).get("recent")))       # cache seul : appele depuis le fil des prix, aucune requete reseau ici
            r = self.contradiction(item, v)
            if r is None and v is not None:
                (item.get("zone_order") or {})["h4_fire"] = self.h4_info(item["ticker"], v["price"], cache_only=True)      # v4.520 : observation
            return (r is None), (r or "")
        except Exception:
            return True, ""

    # ── cycle ──
    def on_cycle(self):
        if not int(self.c("ZONEORD_ON")) or self._busy or time.time() - self._last < 60:
            return
        self._last, self._busy = time.time(), True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._cancel_pass()
            self._create_pass()
        except Exception as e:
            self.diag["_erreur"] = (time.time(), f"{type(e).__name__}: {e}")
        finally:
            self._busy = False

    def _mine(self):
        return [i for i in list(self.manual.items.values()) if i.get("zone_order") and i.get("status") == "scheduled"]

    def summary(self):
        """Bilan des ordres sur zone (paper / live separes) : programmes, declenches, annules, expires, net des trades clos."""
        import db
        out = {"paper": {"scheduled": 0, "open": 0, "closed": 0, "cancelled": 0, "expired": 0, "wins": 0, "net_usd": 0.0},
               "live": {"scheduled": 0, "open": 0, "closed": 0, "cancelled": 0, "expired": 0, "wins": 0, "net_usd": 0.0}}
        for i in list(self.manual.items.values()):
            if i.get("zone_order") and i.get("status") in ("scheduled", "open"):
                out["live" if i.get("mode") == "live" else "paper"][i["status"]] += 1
        for r in db.manual_history(400):
            d = r["data"]
            if not d.get("zone_order"):
                continue
            o = out["live" if d.get("mode") == "live" else "paper"]
            st = r.get("status")
            if st == "closed" and d.get("entry_price"):
                o["closed"] += 1
                pnl = float(d.get("pnl") or 0)
                o["net_usd"] = round(o["net_usd"] + pnl, 2)
                o["wins"] += 1 if pnl > 0 else 0
            elif st in ("cancelled", "expired"):
                o[st] += 1
        out["h4"] = self._h4_breakdown()
        return out

    def _h4_breakdown(self):
        """Observation (v4.520) : trades clos (paper + live) par tendance H4 au declenchement, et selon que le prix etait deja du bon cote de la moyenne 20 H4."""
        import db
        grp = {}
        for r in db.manual_history(400):
            d = r["data"]
            zo = d.get("zone_order")
            if not zo or r.get("status") != "closed" or not d.get("entry_price"):
                continue
            h = zo.get("h4_fire") or zo.get("h4_create")
            if not h:
                continue
            sgn = 1 if d.get("direction") == "long" else -1
            vs = h.get("vs_ema20_pct")
            keys = ["H4 " + str(h.get("trend"))]
            if vs is not None:
                keys.append("H4 : prix du bon côté de la moyenne 20" if sgn * vs > 0 else "H4 : prix encore du mauvais côté de la moyenne 20")
            pnl = float(d.get("pnl") or 0)
            for k in keys:
                g = grp.setdefault(k, {"label": k, "n": 0, "wins": 0, "net_usd": 0.0})
                g["n"] += 1
                g["wins"] += 1 if pnl > 0 else 0
                g["net_usd"] = round(g["net_usd"] + pnl, 2)
        return sorted(grp.values(), key=lambda g: g["label"])


    def _cancel_pass(self):
        for it in self._mine():
            if int(self.c("ZONEORD_RECENT_ON")) and not (it.get("zone_order") or {}).get("recent"):
                with self.manual.lock:      # v4.526 : ordre de l'ancien mode (zones longues) remplace par la lecture de la fenetre recente
                    if it.get("status") == "scheduled":
                        self.manual._finish_order(it, "cancelled", "ordre sur zone annulé : remplacé par la lecture de la fenêtre récente (v4.526)")
                continue
            try:
                r = self.contradiction(it, self.view(it["ticker"], recent=bool((it.get("zone_order") or {}).get("recent"))))
            except Exception:
                r = None
            if r:
                with self.manual.lock:
                    if it.get("status") == "scheduled":
                        self.manual._finish_order(it, "cancelled", f"ordre sur zone annulé : {r}")

    def _create_pass(self):
        c = {k: float(self.c(k)) for k in DEFAULTS if k not in ("ZONEORD_ON", "ZONEORD_LIVE", "ZONEORD_SYMBOLS")}
        for t in list(self.c("ZONEORD_SYMBOLS")):
            if len(self._mine()) >= int(c["ZONEORD_MAX_ORDERS"]):
                self.diag[t] = (time.time(), "nombre maximal d'ordres sur zone atteint")
                continue
            self.why = ""
            v = self.view(t)
            if v is None:
                self.diag[t] = (time.time(), self.why or "prix indisponible")
                continue
            msgs = []
            for side in ("long", "short"):
                if any(i["ticker"] == t and i["direction"] == side for i in self._mine()):
                    msgs.append(f"{side} : ordre déjà programmé")
                    continue
                pl, why = (self.plan_recent(side, v, c) if v.get("recent") else self.plan(side, v, c))
                if pl is None:
                    msgs.append(f"{side} : {why}")
                    continue
                if self.manual.ticker_busy(t):
                    msgs.append(f"{side} : position déjà ouverte")
                    continue
                msgs.append(f"{side} : " + self._submit(t, side, v, pl, c))
            if v.get("recent"):
                msgs.insert(0, f"fenêtre {v['hours']:g} h : {v['win_lo']:.6g}–{v['win_hi']:.6g}, prix {v['price']:.6g}, élan 1 h {v['mom_pct']:+d} % de la fenêtre, tendance Daily {v['trend']}")
            self.diag[t] = (time.time(), " · ".join(msgs))

    def _submit(self, t, side, v, pl, c):
        up = side == "long"
        trail = max(float(c["ZONEORD_TRAIL_PCT"]), 0.25 * pl["sl_pct"]) if not pl.get("recent") else max(0.5 * pl["sl_pct"], 0.15)
        params = {"strategy": "libre", "ticker": t, "direction": side, "market": "perp", "mode": ("live" if int(self.c("ZONEORD_LIVE")) else "paper"), "confirm_live": bool(int(self.c("ZONEORD_LIVE"))), "execution": "bounce_up" if up else "bounce_down",
                  "trigger_price": pl["edge"], "confirm_pct": c["ZONEORD_CONFIRM_PCT"], "leverage": int(c["ZONEORD_LEVERAGE"]), "notional_usd": c["ZONEORD_NOTIONAL_USD"],
                  "sl_pct": pl["sl_pct"], "tp_pct": pl["tp_pct"], "tp_run": True, "tp_run_lock_pct": 0.0, "tp_run_trail_pct": round(trail, 3),
                  "expiry_hours": (c["ZONEORD_REC_EXPIRY_H"] if pl.get("recent") else c["ZONEORD_EXPIRY_H"]), "require_valid": False, "no_ttp": True}
        try:
            with self.manual.lock:
                res = self.manual.submit(params)
                it = self.manual.items.get(res.get("id")) if isinstance(res, dict) else None
                if it is not None:
                    it["zone_order"] = {"zone": pl["zone"], "trend": v["trend"], "created": time.time(), "dist_atr": pl["dist_atr"], "recent": bool(pl.get("recent")), "h4_create": self.h4_info(t, v["price"], cache_only=False)}
                    if pl.get("recent"):
                        it["reasons"] = [f"🎯 Ordre sur {'base' if up else 'sommet'} récent (programmé par le bot) : {pl['txt']} ; fenêtre {v['hours']:g} h en bougies 15 min, tendance Daily {v['trend']}. "
                                         f"Se déclenche sur {'un rebond' if up else 'un recul'} de {c['ZONEORD_CONFIRM_PCT']:g} % après retour au niveau ; annulé si le niveau est dépassé / cassé, si la tendance se retourne, "
                                         f"si le niveau disparaît de la fenêtre ou après {c['ZONEORD_REC_EXPIRY_H']:g} h."]
                    else:
                        it["reasons"] = [f"🎯 Ordre sur zone (programmé par le bot) : {'support' if up else 'résistance'} {v.get('zone_tf', 'Daily')} {pl['zone'][0]:.6g}–{pl['zone'][1]:.6g} quitté "
                                         f"(prix à {pl['dist_atr']} ATR Daily), tendance Daily {v['trend']}. Se déclenche sur "
                                         f"{'un rebond' if up else 'un recul'} de {c['ZONEORD_CONFIRM_PCT']:g} % après retour au niveau ; annulé si la zone casse, si la tendance se retourne ou après {c['ZONEORD_EXPIRY_H']:g} h."]
                    self.manual._save(it)
            return f"ordre programmé à {pl['edge']:.6g} (stop {pl['sl_pct']} %, objectif {pl['tp_pct']} %)"
        except Exception as e:
            return f"refusé par le moteur manuel : {e}"
