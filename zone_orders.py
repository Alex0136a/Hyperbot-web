"""v4.517 — ORDRES SUR ZONE (paper par defaut, live si ZONEORD_LIVE = 1) : le bot programme lui-meme un rebond sur le support / la resistance Daily que le prix vient de quitter,
dans le sens de la tendance, puis annule l ordre si l evenement ne se produit pas ou le contredit. Les ordres sont des ordres programmes ordinaires
(type « rebond confirme » du Trading Manuel) : declenchement, SL, objectif et suiveur sont geres par le moteur manuel. Live : seulement apres bascule volontaire (ZONEORD_LIVE = 1) ; les ordres paper et live ne se melangent pas."""
import time
import threading

import mtf_analysis as mtf

DEFAULTS = {"ZONEORD_ON": 1, "ZONEORD_LIVE": 0, "ZONEORD_SYMBOLS": ["BTC", "ETH", "HYPE", "TAO", "SUI"], "ZONEORD_ZONE_MAX_WIDTH_ATR": 2.0, "ZONEORD_MIN_DIST_ATR": 0.5, "ZONEORD_MAX_DIST_ATR": 1.5,
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
    def view(self, ticker, cache_only=False):
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
        zones = mtf.find_zones(d1, lookback=self.bot.cfg.get("MTF_ZONE_LOOKBACK", 120), max_width_atr=(float(self.c("ZONEORD_ZONE_MAX_WIDTH_ATR")) or self.bot.cfg.get("MTF_ZONE_MAX_WIDTH_ATR", 0) or None))
        sup, res = mtf.nearest_zones(zones, float(price))
        tr, _, _ = mtf.trend(d1 + [{**d1[-1], "c": float(price)}])
        return {"price": float(price), "atr": a, "sup": sup, "res": res, "trend": tr, "zones": zones}

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
        zt = f"{'support' if side == 'long' else 'résistance'} {z['low']:.6g}–{z['high']:.6g}"
        if dist < 0:
            return None, f"prix dans {zt}" if z["low"] <= p <= z["high"] else f"prix au-delà de {zt}"
        if dist < c["ZONEORD_MIN_DIST_ATR"]:
            return None, f"prix trop près de {zt} ({dist:.2f} ATR Daily, minimum {c['ZONEORD_MIN_DIST_ATR']:g})"
        if dist > c["ZONEORD_MAX_DIST_ATR"]:
            return None, f"{zt} trop loin ({dist:.1f} ATR Daily, maximum {c['ZONEORD_MAX_DIST_ATR']:g})"
        far = z["low"] - c["ZONEORD_SL_BUFFER_ATR"] * a if side == "long" else z["high"] + c["ZONEORD_SL_BUFFER_ATR"] * a
        sl = abs(edge - far) / edge * 100
        if sl > c["ZONEORD_SL_MAX_PCT"]:
            return None, f"stop trop large ({sl:.1f} %) pour {zt}, largeur {(z['high'] - z['low']) / a:.1f} ATR"
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
        lo, hi = zo.get("zone", (None, None))
        if lo is None or v is None:
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
            v = self.view(item["ticker"], cache_only=True)       # cache seul : appele depuis le fil des prix, aucune requete reseau ici
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
            try:
                r = self.contradiction(it, self.view(it["ticker"]))
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
                pl, why = self.plan(side, v, c)
                if pl is None:
                    msgs.append(f"{side} : {why}")
                    continue
                if self.manual.ticker_busy(t):
                    msgs.append(f"{side} : position déjà ouverte")
                    continue
                msgs.append(f"{side} : " + self._submit(t, side, v, pl, c))
            self.diag[t] = (time.time(), " · ".join(msgs))

    def _submit(self, t, side, v, pl, c):
        up = side == "long"
        trail = max(float(c["ZONEORD_TRAIL_PCT"]), 0.25 * pl["sl_pct"])
        params = {"strategy": "libre", "ticker": t, "direction": side, "market": "perp", "mode": ("live" if int(self.c("ZONEORD_LIVE")) else "paper"), "confirm_live": bool(int(self.c("ZONEORD_LIVE"))), "execution": "bounce_up" if up else "bounce_down",
                  "trigger_price": pl["edge"], "confirm_pct": c["ZONEORD_CONFIRM_PCT"], "leverage": int(c["ZONEORD_LEVERAGE"]), "notional_usd": c["ZONEORD_NOTIONAL_USD"],
                  "sl_pct": pl["sl_pct"], "tp_pct": pl["tp_pct"], "tp_run": True, "tp_run_lock_pct": 0.0, "tp_run_trail_pct": round(trail, 3),
                  "expiry_hours": c["ZONEORD_EXPIRY_H"], "require_valid": False, "no_ttp": True}
        try:
            with self.manual.lock:
                res = self.manual.submit(params)
                it = self.manual.items.get(res.get("id")) if isinstance(res, dict) else None
                if it is not None:
                    it["zone_order"] = {"zone": pl["zone"], "trend": v["trend"], "created": time.time(), "dist_atr": pl["dist_atr"], "h4_create": self.h4_info(t, v["price"], cache_only=False)}
                    it["reasons"] = [f"🎯 Ordre sur zone (programmé par le bot) : {'support' if up else 'résistance'} Daily {pl['zone'][0]:.6g}–{pl['zone'][1]:.6g} quitté "
                                     f"(prix à {pl['dist_atr']} ATR Daily), tendance Daily {v['trend']}. Se déclenche sur "
                                     f"{'un rebond' if up else 'un recul'} de {c['ZONEORD_CONFIRM_PCT']:g} % après retour au niveau ; annulé si la zone casse, si la tendance se retourne ou après {c['ZONEORD_EXPIRY_H']:g} h."]
                    self.manual._save(it)
            return f"ordre programmé à {pl['edge']:.6g} (stop {pl['sl_pct']} %, objectif {pl['tp_pct']} %)"
        except Exception as e:
            return f"refusé par le moteur manuel : {e}"
