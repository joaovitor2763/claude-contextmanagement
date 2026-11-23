"""Atomic, canonical checkpoints for external summaries (the .md is only a human mirror).

A pending/failed checkpoint keeps the last successful summary and its verified cursor,
but must never be injected. Legacy Markdown alone is deliberately not promoted.
"""
import hashlib
import json
import os
import re
import tempfile

from common import SUMMARIES

VERSION = 1
_STATUSES = {"pending", "ready", "failed"}


def normalize_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _path(sid, suffix):
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise ValueError("invalid session id")
    return os.path.abspath(os.path.join(SUMMARIES, sid + suffix))


def summary_path(sid):
    return _path(sid, ".md")


def state_path(sid):
    return _path(sid, ".json")


def _valid(sid, state):
    if not isinstance(state, dict) or state.get("version") != VERSION:
        return False
    if state.get("session_id") != sid or state.get("status") not in _STATUSES:
        return False
    path = state.get("transcript_path")
    if not isinstance(path, str) or not path or path != normalize_path(path):
        return False
    summary, cursor = state.get("summary"), state.get("cursor")
    if not isinstance(summary, str):
        return False
    if cursor is None:
        return not summary and state["status"] != "ready"
    if not isinstance(cursor, dict) or not summary.strip():
        return False
    chars, digest = cursor.get("chars"), cursor.get("sha256")
    return (type(chars) is int and chars >= 0 and isinstance(digest, str)
            and re.fullmatch(r"[a-f0-9]{64}", digest) is not None)


def load_state(sid):
    """Return a validated checkpoint or None. Never fall back to the legacy .md.

    Validation here is structural; the producer verifies the cursor against the actual
    transcript snapshot before resuming. Consumers must additionally require ready and
    match the normalized transcript_path to the hook's path when one is available.
    """
    try:
        with open(state_path(sid), encoding="utf-8") as f:
            state = json.load(f)
        return state if _valid(sid, state) else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".summary-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state(sid, state):
    """Atomically replace canonical JSON, or raise without publishing a partial file."""
    if not _valid(sid, state):
        raise ValueError("invalid summary checkpoint")
    _atomic_write(state_path(sid), json.dumps(state, ensure_ascii=False) + "\n")


def save_summary(sid, text):
    """Atomically update the optional human-readable mirror, never the authority."""
    _atomic_write(summary_path(sid), text)


def make_cursor(text):
    """Character cursor over UTF-8-decoded text, with a hash of that exact prefix."""
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def cursor_matches(cursor, text):
    if not cursor or cursor["chars"] > len(text):
        return False
    prefix = text[:cursor["chars"]]
    # A previous partial JSONL record may now have completed; replay it in full.
    if prefix and not prefix.endswith("\n") and len(text) > len(prefix):
        return False
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest() == cursor["sha256"]
