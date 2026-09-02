"""Shared blocked-hospital list used by both the Flask app (app.py) and the
Playwright bot (sevaro_bot.py).

The list is stored as a JSON array of hospital-name strings in a single file so
the value survives container recreation when that file is bind-mounted from the
host (see README). Both processes run in the same container with cwd /app, so
the default relative path resolves to the same file for both.

Reads are lock-free but always see a complete file because writes go through a
temp file + atomic os.replace. Reads fail open (return an empty list) so a
missing or corrupt file never blocks legitimate cases or crashes the bot.
"""

import json
import os
import re
import tempfile
from threading import Lock

BLOCKED_HOSPITALS_FILE = os.environ.get(
    "BLOCKED_HOSPITALS_FILE", "blocked_hospitals.json"
)

# Serializes read-modify-write from the Flask app. The bot only ever reads.
_WRITE_LOCK = Lock()


def _normalize(name):
    """Lowercase and collapse whitespace so trivial formatting differences
    (extra spaces, casing) don't defeat a match."""
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def load_blocked_hospitals():
    """Return the stored list of blocked hospital names. Fail open (empty list)
    on any error so a missing/corrupt file never blocks real cases."""
    try:
        with open(BLOCKED_HOSPITALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # Preserve display form, drop blanks/dupes while keeping order.
            seen = set()
            result = []
            for item in data:
                name = str(item).strip()
                key = _normalize(name)
                if name and key not in seen:
                    seen.add(key)
                    result.append(name)
            return result
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return []


def _save(names):
    """Write the list. Caller must hold _WRITE_LOCK.

    Prefer a temp-file + atomic os.replace so a concurrent reader (the bot)
    never sees a half-written file. When the target is a single-file bind mount,
    the temp file lands on the container overlay fs while the target is on the
    host mount, so os.replace fails with EXDEV (cross-device); fall back to an
    in-place write in that case. The file is tiny and only written on UI
    add/remove, and readers fail open, so the fallback is safe."""
    path = BLOCKED_HOSPITALS_FILE
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(names, f, indent=2)
        os.replace(tmp, path)  # atomic on POSIX (same filesystem)
    except OSError:
        # Cross-device rename (single-file bind mount) or similar: write in place.
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as f:
            json.dump(names, f, indent=2)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def ensure_blocklist_file():
    """Create the blocklist file as an empty JSON list if it doesn't exist yet.

    Safe to call once on startup. It never raises: if the path is unwritable or
    misconfigured, reads already fail open (empty list) and the first UI add
    would re-attempt creation, so a failure here must not crash the bot."""
    path = BLOCKED_HOSPITALS_FILE
    if os.path.exists(path):
        return
    try:
        with _WRITE_LOCK:
            # Re-check under the lock in case another thread just created it.
            if os.path.exists(path):
                return
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f)
    except OSError:
        pass


def add_blocked_hospital(name):
    """Add a hospital name (trimmed, case-insensitive dedupe). Returns the new list."""
    name = (name or "").strip()
    with _WRITE_LOCK:
        current = load_blocked_hospitals()
        if name and _normalize(name) not in {_normalize(n) for n in current}:
            current.append(name)
            _save(current)
        return current


def remove_blocked_hospital(name):
    """Remove a hospital name (case-insensitive). Returns the new list."""
    target = _normalize(name)
    with _WRITE_LOCK:
        current = load_blocked_hospitals()
        updated = [n for n in current if _normalize(n) != target]
        if len(updated) != len(current):
            _save(updated)
        return updated


def is_hospital_blocked(hospital):
    """Return (True, matched_name) if the case hospital matches any blocked
    entry, else (False, None). A blocked entry matches when it appears as a
    substring of the case hospital (both normalized), so a partial name like
    "Mercy" blocks every "Mercy ..." facility."""
    if not hospital:
        return False, None
    haystack = _normalize(hospital)
    for entry in load_blocked_hospitals():
        needle = _normalize(entry)
        if needle and needle in haystack:
            return True, entry
    return False, None
