"""v4.376 — Alertes sur telephone (Web Push), sans service tiers.

Fonctionne avec la PWA installee sur l'ecran d'accueil (iPhone iOS 16.4+, Android, ordinateur).
Chiffrement RFC 8291 (aes128gcm) et signature VAPID RFC 8292, uniquement avec la bibliotheque `cryptography`.
Si `cryptography` est absente, les alertes sont simplement indisponibles : le reste du bot n'est pas touche.

Fichier d'etat : <HYPERBOT_DATA_DIR>/hyperbot_push.json (cle VAPID, appareils abonnes, preferences).
"""
import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    CRYPTO_OK, CRYPTO_ERR = True, ""
except Exception as _e:           # pragma: no cover
    CRYPTO_OK, CRYPTO_ERR = False, f"{type(_e).__name__}: {_e}"

DEFAULT_PREFS = {
    "trade_open": 1,          # trade ouvert
    "trade_close": 1,         # trade ferme (avec resultat)
    "daily_loss": 1,          # perte du jour au-dela du seuil
    "daily_loss_usd": 2.0,
    "offline": 1,             # connexion Hyperliquid perdue depuis N minutes
    "offline_min": 3,
    "opps": 0,                # opportunites du bot (Trading Manuel)
    "opps_min_conf": 70,      # confiance minimale (Accumulation, Spot-Accum, Funding, Forex)
    "opps_min_score": 60,     # note de qualite minimale Swing Support (0-100)
    "opps_types": ["forex", "accumulation", "spot_accumulation", "funding_contrarian"],   # types d'opportunites annonces
    "opps_cooldown_min": 30,  # pas de nouvelle alerte pour la meme opportunite avant ce delai
}
_PREF_BOUNDS = {"opps_min_score": (0, 100), "daily_loss_usd": (0.1, 100000.0), "offline_min": (1, 120), "opps_min_conf": (0, 100), "opps_cooldown_min": (1, 1440)}

STRATEGY_LABELS = {
    "forex_scalp": "Scalp Forex", "forex_swing": "Swing Forex", "swing_support": "Swing Support", "trend_follow": "Tendance",
    "spot_accumulation": "Spot-Accum", "accumulation": "Accumulation", "funding_contrarian": "Funding", "forex": "Forex",
    "manual": "Manuel", "normal": "Normal",
}


OPP_TYPES = {"forex": "Forex", "accumulation": "Accumulation", "spot_accumulation": "Spot-Accum",
             "funding_contrarian": "Funding", "swing_support": "Swing Support"}


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encrypt_payload(plaintext, ua_public_b64, auth_b64, as_private=None, salt=None):
    """RFC 8291 : corps `aes128gcm` pret a poster. `as_private`/`salt` ne servent qu'aux tests (vecteur de l'annexe A)."""
    ua_pub = _b64u_dec(ua_public_b64)
    auth = _b64u_dec(auth_b64)
    as_priv = as_private or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    secret = as_priv.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=b"WebPush: info\x00" + ua_pub + as_pub).derive(secret)
    salt = salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    ct = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + ct


class PushAlerts:
    def __init__(self, data_dir=".", log=print, closed_trades_since=None):
        self.path = os.path.join(data_dir, "hyperbot_push.json")
        self.log = log
        self.ticker_from_slot = lambda x: x                # remplace par api.py (be.ticker_from_slot_key)
        self.closed_trades_since = closed_trades_since     # fonction(since_iso) -> liste de trades fermes (pour la perte du jour)
        self.lock = threading.RLock()
        self.q = queue.Queue(maxsize=200)
        self._sent_times = []                              # anti-rafale
        self._opp_seen = {}                                # (strategie, actif, sens) -> dernier envoi
        self._down_since = None
        self._down_alerted = False
        self._loss_alert_day = None
        self._started = False
        self.state = {"vapid_private_pem": None, "vapid_public": None, "subs": {}, "prefs": dict(DEFAULT_PREFS), "history": []}
        self._load()

    # ───────────────────────────── etat ─────────────────────────────
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.state.update({k: d[k] for k in ("vapid_private_pem", "vapid_public", "subs", "history") if k in d})
            self.state["prefs"] = {**DEFAULT_PREFS, **(d.get("prefs") or {})}
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"[PUSH] fichier d'alertes illisible ({type(e).__name__}: {e}) — reinitialise")

    def _save(self):
        try:
            tmp = f"{self.path}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except Exception as e:
            self.log(f"[PUSH] ecriture impossible : {type(e).__name__}: {e}")

    def _ensure_keys(self):
        if not CRYPTO_OK:
            return False
        with self.lock:
            if self.state.get("vapid_private_pem") and self.state.get("vapid_public"):
                return True
            k = ec.generate_private_key(ec.SECP256R1())
            self.state["vapid_private_pem"] = k.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            self.state["vapid_public"] = _b64u(k.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
            self._save()
            return True

    def available(self):
        return CRYPTO_OK

    def public_key(self):
        return self.state.get("vapid_public") if self._ensure_keys() else None

    def status(self):
        with self.lock:
            return {"available": CRYPTO_OK, "error": CRYPTO_ERR, "public_key": self.public_key(),
                    "devices": len(self.state["subs"]), "prefs": dict(self.state["prefs"]),
                    "endpoints": list(self.state["subs"].keys())}

    # ───────────────────────── appareils / preferences ─────────────────────────
    def add_sub(self, sub):
        ep = str((sub or {}).get("endpoint") or "")
        keys = (sub or {}).get("keys") or {}
        if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("abonnement invalide")
        try:
            if len(_b64u_dec(keys["p256dh"])) != 65 or len(_b64u_dec(keys["auth"])) != 16:
                raise ValueError
        except Exception:
            raise ValueError("cles d'abonnement invalides")
        with self.lock:
            self.state["subs"][ep] = {"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "added": int(time.time())}
            self._save()

    def remove_sub(self, endpoint):
        with self.lock:
            if self.state["subs"].pop(str(endpoint), None) is not None:
                self._save()

    def set_prefs(self, prefs):
        with self.lock:
            cur = self.state["prefs"]
            for k, v in (prefs or {}).items():
                if k not in DEFAULT_PREFS:
                    continue
                if k == "opps_types":
                    cur[k] = [t for t in OPP_TYPES if t in (v or [])]
                    continue
                if k in _PREF_BOUNDS:
                    lo, hi = _PREF_BOUNDS[k]
                    v = type(DEFAULT_PREFS[k])(float(v))
                    if not lo <= v <= hi:
                        raise ValueError(f"{k} : entre {lo} et {hi}")
                else:
                    v = 1 if v in (1, True, "1", "true") else 0
                cur[k] = v
            self._save()
            return dict(cur)

    def pref(self, k):
        return self.state["prefs"].get(k, DEFAULT_PREFS.get(k))

    # ───────────────────────────── envoi ─────────────────────────────
    def start(self):
        if self._started or not CRYPTO_OK:
            return
        self._started = True
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def notify(self, title, body, tag=None, url="/", urgent=True):
        """Met une alerte en file ; ne bloque jamais le bot."""
        self._remember(title, body, tag)            # v4.379 : historique consultable dans l'app (iOS efface la notification au toucher)
        if not CRYPTO_OK or not self.state["subs"]:
            return False
        now = time.time()
        self._sent_times = [t for t in self._sent_times if now - t < 600]
        if len(self._sent_times) >= 30:                    # au plus 30 alertes par 10 minutes
            return False
        self._sent_times.append(now)
        try:
            self.q.put_nowait({"title": title, "body": body, "tag": tag, "url": url, "urgent": urgent})
            return True
        except queue.Full:
            return False

    def _remember(self, title, body, tag):
        try:
            with self.lock:
                h = self.state.setdefault("history", [])
                h.append({"ts": int(time.time()), "title": title, "body": body, "tag": tag})
                del h[:-100]
                self._save()
        except Exception:
            pass

    def history(self, limit=50):
        with self.lock:
            return list(reversed(self.state.get("history", [])[-max(1, min(int(limit), 100)):]))

    def _worker(self):
        while True:
            m = self.q.get()
            try:
                self._deliver(m)
            except Exception as e:
                self.log(f"[PUSH] envoi ignore : {type(e).__name__}: {e}")

    def _vapid_headers(self, endpoint):
        u = urllib.parse.urlparse(endpoint)
        aud = f"{u.scheme}://{u.netloc}"
        key = serialization.load_pem_private_key(self.state["vapid_private_pem"].encode(), password=None)
        head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        sub = os.environ.get("PUSH_VAPID_SUB", "mailto:hyperbot@example.com")
        claims = _b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": sub}, separators=(",", ":")).encode())
        signing = f"{head}.{claims}".encode()
        r, s = decode_dss_signature(key.sign(signing, ec.ECDSA(hashes.SHA256())))
        sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={head}.{claims}.{sig}, k={self.state['vapid_public']}"

    def _post(self, endpoint, body, urgent):
        req = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "Content-Length": str(len(body)),
            "TTL": "3600", "Urgency": "high" if urgent else "normal",
            "Authorization": self._vapid_headers(endpoint)})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def _deliver(self, m):
        if not self._ensure_keys():
            return
        payload = json.dumps({"title": m["title"], "body": m["body"], "tag": m.get("tag"), "url": m.get("url", "/")}, ensure_ascii=False)
        for ep, sub in list(self.state["subs"].items()):
            try:
                body = encrypt_payload(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
                self._post(ep, body, m.get("urgent", True))
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):                    # abonnement expire : on l'oublie
                    self.remove_sub(ep)
                    self.log("[PUSH] un appareil expire a ete retire (reactiver les alertes dessus)")
                else:
                    self.log(f"[PUSH] refus {e.code} du service de notification")
            except Exception as e:
                self.log(f"[PUSH] echec d'envoi : {type(e).__name__}: {e}")

    def send_test(self):
        ok = self.notify("🔔 HyperBot", "Les alertes fonctionnent sur cet appareil.", tag="test")
        return ok

    # ───────────────────────────── evenements du bot ─────────────────────────────
    @staticmethod
    def _fmt(x, nd=6):
        try:
            return f"{float(x):.{nd}g}"
        except Exception:
            return "?"

    def on_event(self, etype, data):
        try:
            if etype == "trade_opened":
                self._on_open(data)
            elif etype == "trade":
                self._on_close(data)
            elif etype == "ws_event":
                self._on_ws(data)
        except Exception as e:
            self.log(f"[PUSH] evenement ignore : {type(e).__name__}: {e}")

    def _mode_tag(self, data):
        return "📝 Paper" if str(data.get("trade_mode") or "paper").lower() == "paper" else "💰 Reel"

    def _on_open(self, d):
        if not self.pref("trade_open"):
            return
        action = str(d.get("action") or "").upper()
        side = "SHORT" if "SHORT" in action else "LONG"
        strat = STRATEGY_LABELS.get(d.get("strategy"), d.get("strategy") or "?")
        bits = [f"entrée {self._fmt(d.get('entry'))}"]
        if d.get("stop_loss"):
            bits.append(f"SL {self._fmt(d.get('stop_loss'))}")
        if d.get("take_profit1"):
            bits.append(f"TP {self._fmt(d.get('take_profit1'))}")
        if d.get("size_usd"):
            bits.append(f"{self._fmt(d.get('size_usd'), 3)} $")
        self.notify(f"📈 {side} {d.get('coin', '?')} ouvert · {strat}", " · ".join(bits) + f" · {self._mode_tag(d)}",
                    tag=f"open-{d.get('trade_uid') or d.get('coin')}")

    def _on_close(self, d):
        pnl = d.get("pnl")
        try:
            pnl = float(pnl)
        except Exception:
            pnl = None
        sym = str(d.get("symbol") or "")
        coin = d.get("coin") or (self.ticker_from_slot(sym) if sym else "?")
        if self.pref("trade_close") and pnl is not None:
            side = "LONG" if d.get("type") == "long" else "SHORT"
            strat = STRATEGY_LABELS.get(d.get("strategy"), d.get("strategy") or "?")
            self.notify(f"{'✅' if pnl >= 0 else '🔻'} {coin} {side} fermé {pnl:+.2f} $ · {strat}",
                        f"{d.get('reason') or ''} · {self._mode_tag(d)}".strip(" ·"), tag=f"close-{d.get('trade_uid') or coin}")
        if pnl is not None and pnl < 0:
            self._check_daily_loss()

    def _local_day_start_iso(self):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Marigot")
        except Exception:
            tz = timezone(timedelta(hours=-4))
        now = datetime.now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(), start.date().isoformat()

    def _check_daily_loss(self):
        if not self.pref("daily_loss") or not self.closed_trades_since:
            return
        since, day = self._local_day_start_iso()
        if self._loss_alert_day == day:
            return
        tot = sum((t.get("pnl") or 0.0) for t in self.closed_trades_since(since))
        if tot <= -abs(float(self.pref("daily_loss_usd"))):
            self._loss_alert_day = day
            self.notify(f"⚠️ Perte du jour {tot:+.2f} $", f"Seuil d'alerte : -{self._fmt(self.pref('daily_loss_usd'), 3)} $ (tous modes, journée locale).",
                        tag="daily-loss")

    def _on_ws(self, d):
        kind = d.get("kind")
        if kind in ("disconnected", "reconnect_failed", "connect_failed"):
            if self._down_since is None:
                self._down_since = time.time()
        elif kind in ("connected", "restored", "reconnect_success"):
            if self._down_alerted and self.pref("offline"):
                self.notify("✅ Connexion rétablie", "Le bot est de nouveau connecté à Hyperliquid.", tag="offline")
            self._down_since, self._down_alerted = None, False

    def _watchdog(self):
        while True:
            time.sleep(30)
            try:
                ds = self._down_since
                if ds and not self._down_alerted and self.pref("offline") and time.time() - ds >= float(self.pref("offline_min")) * 60:
                    self._down_alerted = True
                    self.notify("⚠️ Connexion Hyperliquid perdue", f"Plus de connexion depuis {int((time.time() - ds) / 60)} min. Le bot tente de se reconnecter.",
                                tag="offline")
            except Exception:
                pass

    def on_opportunity(self, opp):
        """Appele par Trading Manuel quand une opportunite NOUVELLE apparait."""
        try:
            if not self.pref("opps") or opp.get("strategy") not in (self.pref("opps_types") or []):
                return
            conf = opp.get("confidence")
            if conf is None or float(conf) < float(self.pref("opps_min_conf")):
                return
            key = (opp.get("strategy"), opp.get("ticker"), opp.get("direction"))
            now = time.time()
            if now - self._opp_seen.get(key, 0) < float(self.pref("opps_cooldown_min")) * 60:
                return
            self._opp_seen[key] = now
            strat = STRATEGY_LABELS.get(opp.get("strategy"), opp.get("strategy") or "?")
            side = "LONG" if opp.get("direction") == "long" else "SHORT"
            why = " · ".join((opp.get("reasons") or [])[:2])
            self.notify(f"🎯 Opportunité {side} {opp.get('ticker')} · {strat} · {float(conf):.0f} %",
                        f"Prix {self._fmt(opp.get('price'))}" + (f" · {why}" if why else ""), tag=f"opp-{key[1]}-{key[2]}", urgent=False)
        except Exception as e:
            self.log(f"[PUSH] opportunite ignoree : {type(e).__name__}: {e}")

    def _opp_type_on(self, strategy):
        return bool(self.pref("opps")) and strategy in (self.pref("opps_types") or [])

    def watch_wanted(self, strategy):
        """True si une surveillance supplementaire (actifs hors liste) a un sens : alertes d'opportunites actives pour ce type."""
        return bool(self.state["subs"]) and self._opp_type_on(strategy)

    def setup_ready(self, strategy, ticker, direction):
        """True si une alerte pour ce setup pourrait partir maintenant (type coche, delai ecoule)."""
        if not self.state["subs"] or not self._opp_type_on(strategy):
            return False
        return time.time() - self._opp_seen.get((strategy, ticker, direction), 0) >= float(self.pref("opps_cooldown_min")) * 60

    def on_setup(self, strategy, ticker, direction, title, body, score=None):
        """Alerte d'un mode qui ouvre lui-meme ses trades (Swing Support) : setup valide detecte.
        Memes reglages que les opportunites (case, type coche, delai entre deux alertes) ; `score` compare a opps_min_score."""
        try:
            if not self.setup_ready(strategy, ticker, direction):
                return
            if score is not None and score < float(self.pref("opps_min_score")):
                return
            self._opp_seen[(strategy, ticker, direction)] = time.time()
            self.notify(title, body, tag=f"opp-{ticker}-{direction}", urgent=False)
        except Exception as e:
            self.log(f"[PUSH] setup ignore : {type(e).__name__}: {e}")
