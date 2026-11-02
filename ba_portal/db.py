"""SQLite storage (plain sqlite3; schema kept portable to Postgres: TEXT/INTEGER only, no SQLite-only types)."""
import json, os, re, sqlite3, threading, datetime as dt
from decimal import Decimal
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS ba (
  id TEXT PRIMARY KEY,
  sha256 TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  pages INTEGER,
  status TEXT NOT NULL,            -- processing|done|error|ditunda (all LLM layers exhausted, retried at retry_at)
  error TEXT,
  engine TEXT,
  result_json TEXT,                -- API-shaped result (workers, criteria, alerts, verdict, quality)
  extraction_json TEXT,            -- raw LLM extraction (for audit / re-evaluation without LLM)
  uploader_ip TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ba_id TEXT UNIQUE NOT NULL REFERENCES ba(id),
  submitter TEXT,
  submitter_ip TEXT,
  submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ba_id TEXT,
  ts TEXT NOT NULL,
  engine TEXT,
  model TEXT,
  attempt INTEGER,
  status TEXT,                     -- ok|error|http_429|...
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  images INTEGER,
  latency_ms INTEGER,
  cost_usd REAL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS quota (
  day TEXT NOT NULL,
  client TEXT NOT NULL,
  uploads INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, client)
);
CREATE TABLE IF NOT EXISTS public_link (
  token TEXT PRIMARY KEY,           -- 22+ url-safe random chars, unguessable
  ba_id TEXT UNIQUE NOT NULL REFERENCES ba(id),
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON llm_usage(ts);
CREATE INDEX IF NOT EXISTS idx_ba_created ON ba(created_at);
CREATE INDEX IF NOT EXISTS idx_ba_status ON ba(status);
"""


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def today():
    return dt.date.today().isoformat()


def _pg_sql(sql):
    """Translate the (SQLite-flavoured) app SQL to Postgres."""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY").replace(" REAL", " DOUBLE PRECISION")
    sql = re.sub(r"INSERT OR IGNORE INTO", "INSERT INTO", sql)
    sql = re.sub(r"SUM\((status(?: NOT)? IN \([^)]*\)|status\s*=\s*'[^']*')\)", r"SUM(CASE WHEN \1 THEN 1 ELSE 0 END)", sql)
    sql = sql.replace("ROUND(COALESCE(SUM(cost_usd),0),4)", "ROUND(COALESCE(SUM(cost_usd),0)::numeric,4)")
    return sql.replace("?", "%s")


class _PgConn:
    """Minimal sqlite3-connection look-alike over psycopg (execute/executescript/commit/close)."""
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        ignore = bool(re.match(r"\s*INSERT OR IGNORE", sql, re.I))
        sql = _pg_sql(sql)
        if ignore:
            sql += " ON CONFLICT DO NOTHING"
        cur = self.raw.cursor()
        cur.execute(sql, tuple(args))
        return cur

    def executescript(self, script):
        for st in script.split(";"):
            if st.strip():
                self.execute(st)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def _plain(v):
    return float(v) if isinstance(v, Decimal) and v % 1 else int(v) if isinstance(v, Decimal) else v


class DB:
    """path = SQLite file, or a postgres:// URL (e.g. Neon) -> Postgres via psycopg."""
    def __init__(self, path):
        self.path = path
        self.pg = str(path).startswith(("postgres://", "postgresql://"))
        self.lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            if self.pg:
                c.execute("ALTER TABLE ba ADD COLUMN IF NOT EXISTS retry_at TEXT")
                c.execute("ALTER TABLE ba ADD COLUMN IF NOT EXISTS retries INTEGER DEFAULT 0")
                return
            cols = {r[1] for r in c.execute("PRAGMA table_info(ba)")}
            if "retry_at" not in cols:  # v3: 'ditunda' backoff
                c.execute("ALTER TABLE ba ADD COLUMN retry_at TEXT")
                c.execute("ALTER TABLE ba ADD COLUMN retries INTEGER DEFAULT 0")

    @contextmanager
    def conn(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            c = _PgConn(psycopg.connect(self.path, row_factory=dict_row, connect_timeout=15))
        else:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA secure_delete=ON")  # replaced/deleted (encrypted) API keys are zeroed on disk
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def q(self, sql, args=(), one=False):
        with self.conn() as c:
            rows = [{k: _plain(v) for k, v in dict(r).items()} for r in c.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    def x(self, sql, args=()):
        with self.lock, self.conn() as c:
            cur = c.execute(sql, args)
            return None if self.pg else cur.lastrowid

    # --- helpers
    def bump_quota(self, client, limit):
        """Atomically count one new upload; return False if over the daily limit."""
        with self.lock, self.conn() as c:
            day = today()
            c.execute("INSERT OR IGNORE INTO quota(day, client, uploads) VALUES (?,?,0)", (day, client))
            r = c.execute("SELECT uploads FROM quota WHERE day=? AND client=?", (day, client)).fetchone()
            n = r["uploads"]
            if n >= limit:
                return False
            c.execute("UPDATE quota SET uploads=uploads+1 WHERE day=? AND client=?", (day, client))
            return True

    def llm_calls_today(self):
        r = self.q("SELECT COUNT(*) n FROM llm_usage WHERE ts >= ? AND status IN ('ok','bad_json')", (today(),), one=True)
        return r["n"] if r else 0

    def layer_calls_today(self, layer):
        """Every attempt counts (free tiers count failed requests too)."""
        r = self.q("SELECT COUNT(*) n FROM llm_usage WHERE ts >= ? AND engine=?", (today(), layer), one=True)
        return r["n"] if r else 0

    def log_usage(self, **kw):
        kw.setdefault("ts", now())
        cols = ",".join(kw)
        self.x(f"INSERT INTO llm_usage({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))

    def set_result(self, ba_id, status, result=None, extraction=None, error=None, engine=None):
        self.x("UPDATE ba SET status=?, result_json=?, extraction_json=?, error=?, engine=COALESCE(?, engine), updated_at=? WHERE id=?",
               (status, json.dumps(result, ensure_ascii=False) if result else None,
                json.dumps(extraction, ensure_ascii=False) if extraction else None, error, engine, now(), ba_id))
