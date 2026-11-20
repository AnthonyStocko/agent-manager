"""Base de données : Postgres si DATABASE_URL est défini (Neon, Supabase…), sinon SQLite local."""
import datetime
import os
import sqlite3
import threading

URL = (os.getenv("DATABASE_URL") or "").strip()
PG = URL.startswith(("postgres://", "postgresql://"))
if PG:
    import psycopg
    from psycopg.rows import dict_row

DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(DATA_DIR, "data.sqlite3")
_lock = threading.Lock()
ID = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS memories (
        id {ID}, category TEXT NOT NULL, content TEXT NOT NULL, source TEXT DEFAULT '',
        importance INTEGER DEFAULT 3, created_at TEXT, updated_at TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS events (
        id {ID}, agent TEXT, kind TEXT, content TEXT, created_at TEXT,
        processed INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0)""",
    f"""CREATE TABLE IF NOT EXISTS messages (
        id {ID}, role TEXT, content TEXT, agent TEXT DEFAULT '', created_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)",
    # Journal des changements de la mémoire : chaque création, modification, suppression ou refus, avec
    # l'avant et l'après (JSON), pour pouvoir annuler. Contenus effacés au bout de 30 jours (memory.py).
    f"""CREATE TABLE IF NOT EXISTS memory_log (
        id {ID}, memory_id INTEGER, op TEXT, source TEXT DEFAULT '', event_ids TEXT DEFAULT '',
        before_json TEXT, after_json TEXT, reason TEXT DEFAULT '', created_at TEXT, undone_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS dev_requests (
        ref TEXT PRIMARY KEY, depot TEXT, demande TEXT, statut TEXT, titre TEXT DEFAULT '',
        resume TEXT DEFAULT '', pr_url TEXT DEFAULT '', run_url TEXT DEFAULT '',
        created_at TEXT, updated_at TEXT)""",
]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _connect():
    if PG:
        return psycopg.connect(URL, autocommit=True, row_factory=dict_row, connect_timeout=20)
    con = sqlite3.connect(SQLITE_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def run(sql, params=()):
    """Exécute une requête (paramètres « ? ») et renvoie les lignes éventuelles en dictionnaires."""
    if PG:
        sql = sql.replace("?", "%s")
    with _lock:
        con = _connect()
        try:
            cur = con.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()] if cur.description else []
        finally:
            con.close()


# Colonnes ajoutées après la création des tables : ajoutées au démarrage si elles manquent.
COLUMNS = [
    ("memories", "event_ids", "TEXT DEFAULT ''"),    # événements d'où vient le souvenir (ids, séparés par des virgules)
    ("memories", "confirmed_at", "TEXT"),            # dernière fois qu'un événement l'a confirmé ou précisé
    ("memories", "valid_until", "TEXT"),             # date (AAAA-MM-JJ) après laquelle il n'est plus utilisé
]


def _columns(table):
    if PG:
        return {r["column_name"] for r in run(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}
    return {r["name"] for r in run(f"PRAGMA table_info({table})")}


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    for statement in SCHEMA:
        run(statement)
    for table, column, decl in COLUMNS:
        if column not in _columns(table):
            run(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def kv_get(key, default=""):
    rows = run("SELECT value FROM kv WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def kv_set(key, value):
    run("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value))
