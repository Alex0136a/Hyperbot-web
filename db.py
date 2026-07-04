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
                closed_at TEXT
            )
        """)
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
                       take_profit1, take_profit2):
    with _lock, _connect() as conn:
        cur = conn.execute("""
            INSERT INTO trades (coin, action, confidence, leverage, position_size_pct,
                                 risk_reward, timeframe, entry_price, stop_loss,
                                 take_profit1, take_profit2, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (coin, action, confidence, leverage, position_size_pct, risk_reward,
              timeframe, entry_price, stop_loss, take_profit1, take_profit2, now_iso()))
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


def clear_all_trades():
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


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
