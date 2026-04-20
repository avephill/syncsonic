"""
Rockbox .scrobbler.log parser and Navidrome scrobble submitter.

Format (AUDIOSCROBBLER 1.1):
    #AUDIOSCROBBLER/1.1
    #TZ/UTC  (or UNKNOWN)
    #CLIENT/Rockbox ...
    <artist>\t<album>\t<title>\t<tracknum>\t<duration>\t<rating>\t<timestamp>\t<musicbrainz_id>

Rating field: L = listened (full play), S = skipped.
Only 'L' entries should be scrobbled.

We persist submitted entries in the manifest DB to avoid double-scrobbling.
The log is NOT deleted; we simply record which entries have been submitted.
"""

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# ============================================================================
# DB helpers (separate table within the manifest DB)
# ============================================================================

@contextmanager
def _connect(db_path: Path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_scrobble_table(db_path: Path) -> None:
    with _connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS submitted_scrobbles (
                entry_hash  TEXT PRIMARY KEY,
                artist      TEXT,
                title       TEXT,
                timestamp   INTEGER,
                submitted_at TEXT
            )
        """)


def _entry_hash(artist: str, title: str, timestamp: int) -> str:
    raw = f"{artist}|{title}|{timestamp}"
    return hashlib.sha1(raw.encode()).hexdigest()


def is_submitted(db_path: Path, artist: str, title: str, timestamp: int) -> bool:
    h = _entry_hash(artist, title, timestamp)
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT 1 FROM submitted_scrobbles WHERE entry_hash=?", (h,)
        ).fetchone()
    return row is not None


def mark_submitted(db_path: Path, artist: str, title: str, timestamp: int) -> None:
    h = _entry_hash(artist, title, timestamp)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as con:
        con.execute(
            """INSERT OR IGNORE INTO submitted_scrobbles
               (entry_hash, artist, title, timestamp, submitted_at)
               VALUES (?,?,?,?,?)""",
            (h, artist, title, timestamp, now),
        )


# ============================================================================
# Log parsing
# ============================================================================

def parse_log(log_path: Path) -> list[dict]:
    """
    Parse .scrobbler.log and return a list of play entry dicts.
    Only entries with rating 'L' (listened) are returned.
    """
    if not log_path.exists():
        return []

    entries = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            artist, album, title, tracknum, duration, rating, timestamp_str, *_rest = parts
            if rating.strip().upper() != "L":
                continue
            try:
                timestamp = int(timestamp_str.strip())
            except ValueError:
                continue
            entries.append({
                "artist": artist.strip(),
                "album": album.strip(),
                "title": title.strip(),
                "tracknum": tracknum.strip(),
                "duration": duration.strip(),
                "timestamp": timestamp,
            })
    return entries


def get_pending_scrobbles(log_path: Path, db_path: Path) -> list[dict]:
    """Return entries from the log that have not yet been submitted."""
    all_entries = parse_log(log_path)
    return [
        e for e in all_entries
        if not is_submitted(db_path, e["artist"], e["title"], e["timestamp"])
    ]


# ============================================================================
# Submission
# ============================================================================

def submit_scrobbles(
    entries: list[dict],
    client,
    db_path: Path,
    search_fn=None,
) -> dict:
    """
    Submit a list of scrobble entries to Navidrome via the Subsonic API.

    Uses the manifest to look up Subsonic track IDs by artist+title when possible.
    Falls back to a search3 call if not found.

    Returns a summary dict with counts.
    """
    submitted = 0
    failed = 0
    not_found = 0

    for entry in entries:
        artist = entry["artist"]
        title = entry["title"]
        timestamp_ms = entry["timestamp"] * 1000

        track_id = _resolve_track_id(client, artist, title)
        if not track_id:
            not_found += 1
            continue

        try:
            client.scrobble(track_id, timestamp_ms=timestamp_ms, submission=True)
            mark_submitted(db_path, artist, title, entry["timestamp"])
            submitted += 1
        except Exception:
            failed += 1

    return {"submitted": submitted, "failed": failed, "not_found": not_found}


def _resolve_track_id(client, artist: str, title: str) -> str | None:
    """Search Navidrome for a track by artist + title and return its ID."""
    try:
        results = client.search(f"{artist} {title}", song_count=5)
        songs = results.get("song", [])
        for song in songs:
            if (
                song.get("title", "").lower() == title.lower()
                and song.get("artist", "").lower() == artist.lower()
            ):
                return song["id"]
        # Looser match: just title
        for song in songs:
            if song.get("title", "").lower() == title.lower():
                return song["id"]
    except Exception:
        pass
    return None
