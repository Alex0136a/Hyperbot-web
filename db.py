"""
db.py — Persistance SQLite pour HyperBot Web.

Toutes les données (utilisateurs, trades fermés, réglages personnalisés)
vivent dans un seul fichier SQLite. Pour survivre aux redéploiements sur
Railway, ce fichier doit se trouver sur un Volume monté (voir README.md) —
sinon il est remis à zéro à chaque nouveau déploiement, comme le reste du
système de fichiers du conteneur.

Aucune dépendance externe : uniquement la bibliothèque standard (sqlite3).
"""
import sqlite3
import os
import json
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("HYPERBOT_DB_PATH", "hyperbot.db")

_lock = threading.Lock()  # sqlite3 + threads : on sérialise les écritures


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL,
                leverage INTEGER,
                position_size_pct REAL,
                risk_reward REAL,
                timeframe TEXT,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                take_profit1 REAL,
                take_profit2 REAL,
                exit_price REAL,
                pnl REAL,
                reason TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                rsi REAL,
                entry_reasons TEXT,
                confidence_breakdown TEXT
            )
        """)
        # Migration : ajoute la colonne rsi si la table trades existait deja
        # (CREATE TABLE IF NOT EXISTS n ajoute pas les colonnes manquantes a
        # une table deja creee par une version anterieure du code).
        existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "rsi" not in existing_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN rsi REAL")
        if "entry_reasons" not in existing_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN entry_reasons TEXT")
        if "confidence_breakdown" not in existing_cols:
            # v4.2 — detail brut (JSON) de quel indicateur etait confirme a
            # l entree de CE trade, necessaire pour calibrer statistiquement
            # les poids de CONFIDENCE_WEIGHTS a partir des resultats reels
            # (voir api.py, module de calibration de la confiance).
            conn.execute("ALTER TABLE trades ADD COLUMN confidence_breakdown TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config_overrides (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── Utilisateurs ─────────────────────────────────────────────────────────
def create_user(email, password_hash):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, password_hash, now_iso())
        )
        conn.commit()


def get_user_by_email(email):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def user_count():
    with _lock, _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ── Trades ───────────────────────────────────────────────────────────────
def insert_open_trade(coin, action, confidence, leverage, position_size_pct,
                       risk_reward, timeframe, entry_price, stop_loss,
                       take_profit1, take_profit2, rsi=None, entry_reasons=None,
                       confidence_breakdown=None):
    with _lock, _connect() as conn:
        cur = conn.execute("""
            INSERT INTO trades (coin, action, confidence, leverage, position_size_pct,
                                 risk_reward, timeframe, entry_price, stop_loss,
                                 take_profit1, take_profit2, rsi, entry_reasons,
                                 confidence_breakdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (coin, action, confidence, leverage, position_size_pct, risk_reward,
              timeframe, entry_price, stop_loss, take_profit1, take_profit2, rsi,
              entry_reasons, confidence_breakdown, now_iso()))
        conn.commit()
        return cur.lastrowid


def close_trade(trade_id, exit_price, pnl, reason):
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl, reason, now_iso(), trade_id)
        )
        conn.commit()


def get_open_trade_id_by_coin_action(coin, action):
    """Retrouve le dernier trade ouvert (non ferme) pour ce coin/action —
    utilise quand on ne connait pas l id (ouverture geree par bot_engine,
    pas par l API)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id FROM trades WHERE coin=? AND action=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
            (coin, action)
        ).fetchone()
        return row["id"] if row else None


def get_trades(limit=50, only_closed=False):
    with _lock, _connect() as conn:
        q = "SELECT * FROM trades"
        if only_closed:
            q += " WHERE closed_at IS NOT NULL"
        q += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_all_closed_trades():
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL ORDER BY closed_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_trades_older_than(days):
    with _lock, _connect() as conn:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        rows = conn.execute("SELECT id, closed_at FROM trades WHERE closed_at IS NOT NULL").fetchall()
        to_delete = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["closed_at"]).timestamp()
                if ts < cutoff:
                    to_delete.append(r["id"])
            except Exception:
                continue
        if to_delete:
            conn.executemany("DELETE FROM trades WHERE id=?", [(i,) for i in to_delete])
            conn.commit()
        return len(to_delete)


def cleanup_signals(stale_hours=24, protected_ids=None):
    """Nettoie la table trades en deux temps :
    1. DOUBLONS "ouverts" : un seul trade peut reellement etre ouvert a la
       fois par actif (voir architecture du bot) — si plusieurs lignes du
       meme coin ont closed_at IS NULL, ce sont forcement des orphelins
       (ex: positions paper perdues lors d un redeploiement avant le fix de
       persistance) ou des doublons. On ne garde que le plus recent par coin
       (sauf la ligne protegee, si elle existe pour ce coin — voir ci-dessous).
    2. ANCIENS : supprime tout trade (ouvert ou ferme) cree il y a plus de
       stale_hours heures et qui n a jamais ete cloture proprement (evite
       de perdre les vrais trades fermes recemment, utiles au Bilan) — seuls
       les trades RESTES OUVERTS trop longtemps sont vises ici, pas
       l historique des trades fermes normalement.

    protected_ids : iterable d IDs de lignes (pas de coins entiers !)
    correspondant EXACTEMENT aux positions reellement ouvertes en memoire du
    bot en ce moment (voir api.py, qui retrouve l id exact via
    get_open_trade_id_by_coin_action). Seules CES lignes precises sont
    exclues de toute suppression — les AUTRES doublons du meme coin restent
    nettoyables normalement (contrairement a une version precedente qui
    protegeait tout le coin, empechant par erreur le nettoyage des vrais
    orphelins a cote d une position legitime).
    Retourne (doublons_supprimes, anciens_supprimes).
    """
    protected = set(protected_ids or [])
    with _lock, _connect() as conn:
        # 1. Doublons "ouverts" par coin (la ligne protegee, si presente,
        #    est toujours gardee ; sinon on garde le plus recent)
        open_rows = conn.execute(
            "SELECT id, coin, created_at FROM trades WHERE closed_at IS NULL ORDER BY coin, id DESC"
        ).fetchall()
        by_coin = {}
        for r in open_rows:
            by_coin.setdefault(r["coin"], []).append(r["id"])
        dup_ids = []
        for coin, ids in by_coin.items():
            keep = next((i for i in ids if i in protected), ids[0])
            dup_ids.extend(i for i in ids if i != keep)
        if dup_ids:
            conn.executemany("DELETE FROM trades WHERE id=?", [(i,) for i in dup_ids])

        # 2. Trades restes "ouverts" trop longtemps (orphelins probables),
        #    hors ligne protegee
        cutoff = datetime.now(timezone.utc).timestamp() - stale_hours * 3600
        still_open = conn.execute(
            "SELECT id, coin, created_at FROM trades WHERE closed_at IS NULL"
        ).fetchall()
        stale_ids = []
        for r in still_open:
            if r["id"] in protected:
                continue
            try:
                ts = datetime.fromisoformat(r["created_at"]).timestamp()
                if ts < cutoff:
                    stale_ids.append(r["id"])
            except Exception:
                continue
        if stale_ids:
            conn.executemany("DELETE FROM trades WHERE id=?", [(i,) for i in stale_ids])

        conn.commit()
        print(f"[AUDIT] cleanup_signals() a {now_iso()} — doublons supprimes: {dup_ids} | orphelins supprimes: {stale_ids} | ids proteges: {sorted(protected)}")
        return len(dup_ids), len(stale_ids)


def clear_all_trades():
    import traceback
    with _lock, _connect() as conn:
        count_before = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()["c"]
        conn.execute("DELETE FROM trades")
        conn.commit()
    print(f"[AUDIT] clear_all_trades() appelee a {now_iso()} — {count_before} trade(s) supprime(s). Pile d appel :")
    print("".join(traceback.format_stack()[:-1]))


# ── Config persistante (survit aux redemarrages) ────────────────────────
def get_config_override(key, default=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM config_overrides WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default


def set_config_override(key, value):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO config_overrides (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value))
        )
        conn.commit()


def get_all_config_overrides():
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT key, value FROM config_overrides").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                pass
        return out


def clear_config_overrides():
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM config_overrides")
        conn.commit()


# ── Meta (ex: date de premier demarrage, pour "reset_at") ───────────────
def get_meta(key, default=None):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key, value):
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()
