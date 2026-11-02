"""PDF storage: local folder is the working copy; if S3_BUCKET is set, every PDF is also mirrored to an
S3-compatible bucket (Backblaze B2, Cloudflare R2, MinIO...) and re-downloaded on demand when the local copy is
missing (ephemeral hosts such as Hugging Face Spaces lose the disk on restart).

Env: S3_BUCKET, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_REGION (default auto), S3_PREFIX (default 'ba/').
Without S3_BUCKET everything is a no-op and behaviour is exactly the old local-only one.
"""
import os
import threading
from pathlib import Path

BUCKET = os.environ.get("S3_BUCKET", "").strip()
PREFIX = os.environ.get("S3_PREFIX", "ba/")
_client = None
_lock = threading.Lock()


DB_URL = os.environ.get("DATABASE_URL", "").strip()
# backend: 's3' if S3_BUCKET is set, else 'db' (Postgres bytea) if DATABASE_URL is postgres, else local only.
# Override with STORAGE_BACKEND=s3|db|local.
BACKEND = os.environ.get("STORAGE_BACKEND", "").strip().lower() or (
    "s3" if BUCKET else "db" if DB_URL.startswith(("postgres://", "postgresql://")) else "local")
_db_ready = False


def enabled():
    return BACKEND in ("s3", "db")


def _pg():
    global _db_ready
    import psycopg
    c = psycopg.connect(DB_URL, connect_timeout=15)
    if not _db_ready:
        c.execute("CREATE TABLE IF NOT EXISTS pdf_blob (name TEXT PRIMARY KEY, data BYTEA NOT NULL, created_at TIMESTAMPTZ DEFAULT now())")
        c.commit()
        _db_ready = True
    return c


def _s3():
    global _client
    with _lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            _client = boto3.client(
                "s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
                aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"), aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
                region_name=os.environ.get("S3_REGION", "auto"), config=Config(retries={"max_attempts": 3}, signature_version="s3v4"))
        return _client


def _key(path):
    return PREFIX + Path(path).name


def put(path):
    """Mirror a local file to the bucket. Raises on failure (caller decides)."""
    if BACKEND == "s3":
        _s3().upload_file(str(path), BUCKET, _key(path), ExtraArgs={"ContentType": "application/pdf"})
    elif BACKEND == "db":
        with _pg() as c:
            c.execute("INSERT INTO pdf_blob(name, data) VALUES (%s,%s) ON CONFLICT (name) DO NOTHING",
                      (Path(path).name, Path(path).read_bytes()))


def ensure_local(path):
    """True if the file is available locally (downloading it from the bucket if needed)."""
    p = Path(path)
    if p.exists():
        return True
    if not enabled():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        if BACKEND == "s3":
            _s3().download_file(BUCKET, _key(p), str(tmp))
        else:
            with _pg() as c:
                r = c.execute("SELECT data FROM pdf_blob WHERE name=%s", (p.name,)).fetchone()
            if not r:
                return False
            tmp.write_bytes(bytes(r[0]))
        tmp.replace(p)
        return True
    except Exception:
        return False


def delete(path):
    if enabled():
        try:
            if BACKEND == "s3":
                _s3().delete_object(Bucket=BUCKET, Key=_key(path))
            else:
                with _pg() as c:
                    c.execute("DELETE FROM pdf_blob WHERE name=%s", (Path(path).name,))
        except Exception:
            pass
