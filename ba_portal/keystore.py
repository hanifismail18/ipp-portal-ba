"""Admin-managed LLM API keys, encrypted at rest (Fernet, key derived from KEY_ENC_SECRET in .env).

The full key is only ever: received in PUT, encrypted, decrypted in memory for an LLM call. It is never returned by the
API, never logged. Replacing a key overwrites the row (old ciphertext gone); DELETE removes it.
"""
import base64, hashlib, os, secrets

from cryptography.fernet import Fernet, InvalidToken

import db as dbm

SCHEMA = """CREATE TABLE IF NOT EXISTS api_key (
  provider TEXT PRIMARY KEY,
  enc TEXT NOT NULL,
  last4 TEXT NOT NULL,
  updated_at TEXT NOT NULL
)"""


def ensure_secret(env_path):
    """KEY_ENC_SECRET from the environment, or generate one and append it to .env (0600)."""
    s = os.environ.get("KEY_ENC_SECRET", "").strip()
    if not s:
        s = secrets.token_urlsafe(32)
        with open(env_path, "a") as f:
            f.write(f"\n# encrypts admin-managed API keys in the DB; losing it = re-enter keys\nKEY_ENC_SECRET={s}\n")
        os.environ["KEY_ENC_SECRET"] = s
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    return s


class KeyStore:
    def __init__(self, db, secret):
        self.db = db
        self.f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        with db.conn() as c:
            c.execute(SCHEMA)
        self._cache = {}

    def get(self, provider):
        """Decrypted key or None (cached in memory; cache cleared on set/delete)."""
        if provider not in self._cache:
            row = self.db.q("SELECT enc FROM api_key WHERE provider=?", (provider,), one=True)
            val = None
            if row:
                try:
                    val = self.f.decrypt(row["enc"].encode()).decode()
                except InvalidToken:  # KEY_ENC_SECRET changed: treat as not set
                    val = None
            self._cache[provider] = val
        return self._cache[provider]

    def set(self, provider, key):
        self.db.x("INSERT INTO api_key(provider, enc, last4, updated_at) VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE "
                  "SET enc=excluded.enc, last4=excluded.last4, updated_at=excluded.updated_at",
                  (provider, self.f.encrypt(key.encode()).decode(), key[-4:], dbm.now()))
        self._cache.pop(provider, None)

    def delete(self, provider):
        self.db.x("DELETE FROM api_key WHERE provider=?", (provider,))
        self._cache.pop(provider, None)

    def meta(self, provider):
        return self.db.q("SELECT last4, updated_at FROM api_key WHERE provider=?", (provider,), one=True)
