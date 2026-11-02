"""Logging filter: never write public-link tokens or admin tokens to logs."""
import logging, re

_PAT = [(re.compile(r"/f/[A-Za-z0-9_\-]{8,}"), "/f/<redacted>"),
        (re.compile(r"(?i)(x-admin-token|authorization|api[_-]?key)[=: ]+\S+"), r"\1=<redacted>"),
        (re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|sk-or-[0-9A-Za-z_\-]{10,}|gsk_[0-9A-Za-z]{10,}|sk-[0-9A-Za-z_\-]{20,})"), "<key-redacted>")]


class Redact(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        new = msg
        for p, r in _PAT:
            new = p.sub(r, new)
        if new != msg:
            record.msg, record.args = new, ()
        return True
