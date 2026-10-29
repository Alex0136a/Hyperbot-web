"""v4.507 — calendrier macro (USD, impact fort) pour la pause du Scalp autour des annonces (CPI, emploi, Fed...).
Source 1 : flux JSON public de calendrier economique (telecharge en arriere-plan, toutes les 6 h ; echec -> reessai 15 min).
Source 2 (filet de securite, sans reseau) : regles fixes — emploi US (1er vendredi du mois, 8 h 30 New York) et decisions FOMC 2026 (14 h New York).
Tout est en UTC (timestamps). Aucune dependance externe."""
import json
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:      # pragma: no cover
    NY = None

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FOMC_2026 = ["2026-12-09"]      # jours de decision FOMC restants en 2026 (communique a 14 h New York) ; a completer chaque annee
FOMC_ALL_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]


def _ny_to_ts(y, m, d, hh, mm):
    if NY is not None:
        return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(timezone.utc).timestamp()
    return datetime(y, m, d, hh + 5, mm, tzinfo=timezone.utc).timestamp()      # repli : heure d hiver


def fixed_events(now_ts, days_back=1, days_ahead=35):
    """Regles fixes : emploi US (1er vendredi du mois, 8 h 30 New York) et decisions FOMC 2026 (14 h New York)."""
    out = []
    t0 = datetime.fromtimestamp(now_ts, timezone.utc) - timedelta(days=days_back)
    t1 = datetime.fromtimestamp(now_ts, timezone.utc) + timedelta(days=days_ahead)
    seen = set()
    cur = datetime(t0.year, t0.month, 1)
    while cur <= t1.replace(tzinfo=None):
        d = datetime(cur.year, cur.month, 1)
        while d.weekday() != 4:      # 1er vendredi
            d += timedelta(days=1)
        ts = _ny_to_ts(d.year, d.month, d.day, 8, 30)
        if ts not in seen:
            seen.add(ts)
            out.append({"ts": ts, "title": "Emploi US (NFP)", "src": "règle"})
        cur = datetime(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)
    for ds in FOMC_ALL_2026:
        y, m, d = (int(x) for x in ds.split("-"))
        out.append({"ts": _ny_to_ts(y, m, d, 14, 0), "title": "Décision de la Fed (FOMC)", "src": "règle"})
    lo, hi = t0.timestamp(), t1.timestamp()
    return [e for e in out if lo <= e["ts"] <= hi]


def parse_feed(data):
    """Flux ForexFactory-like : [{title, country, date (ISO avec decalage), impact}] -> evenements USD a fort impact."""
    out = []
    for e in data or []:
        try:
            if str(e.get("country", "")).upper() != "USD" or str(e.get("impact", "")).lower() != "high":
                continue
            ts = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00")).timestamp()
            out.append({"ts": ts, "title": str(e.get("title", "Annonce USD")), "src": "flux"})
        except Exception:
            continue
    return out


class MacroCalendar:
    def __init__(self, log=print):
        self.log = log
        self.feed = []
        self.feed_ts = 0.0
        self.feed_ok = None
        self.feed_err = None
        self._retry_at = 0.0
        self._busy = False

    def _fetch(self):
        try:
            req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 HyperBot"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            ev = parse_feed(data)
            self.feed, self.feed_ts, self.feed_ok, self.feed_err = ev, time.time(), True, None
        except Exception as e:
            self.feed_ok, self.feed_err = False, f"{type(e).__name__}: {e}"[:160]
            self._retry_at = time.time() + 900
        finally:
            self._busy = False

    def refresh_async(self):
        now = time.time()
        if self._busy or now < self._retry_at or (self.feed_ok and now - self.feed_ts < 6 * 3600):
            return
        self._busy = True
        threading.Thread(target=self._fetch, daemon=True).start()

    def events(self, now=None):
        now = now or time.time()
        return sorted(self.feed + fixed_events(now), key=lambda e: e["ts"])

    def active(self, now, before_min, after_min):
        for e in self.events(now):
            if e["ts"] - before_min * 60 <= now <= e["ts"] + after_min * 60:
                return e
        return None

    def upcoming(self, now, n=4):
        return [e for e in self.events(now) if e["ts"] + 3600 >= now][:n]

    def status(self):
        if self.feed_ok:
            return f"flux OK ({len(self.feed)} annonces USD à fort impact cette semaine)"
        if self.feed_ok is False:
            return f"flux indisponible ({self.feed_err}) — règles fixes seulement (emploi, Fed)"
        return "flux pas encore chargé — règles fixes seulement"
