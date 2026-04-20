"""
SQLite-backed manifest tracking what has been synced to the device.

Schema
------
synced_tracks   - one row per track file on the device
synced_albums   - one row per album (all tracks downloaded)
synced_playlists - one row per playlist written to device
selected_artists - artist IDs the user has checked for sync
selected_playlists - playlist IDs the user has checked for sync
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


# ============================================================================
# DB connection
# ============================================================================

_DEFAULT_PATH = Path.home() / ".config" / "syncsonic" / "manifest.db"


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect(db_path: Path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ============================================================================
# Schema initialisation
# ============================================================================

def init_db(db_path: Path = _DEFAULT_PATH) -> None:
    _ensure_dir(db_path)
    with _connect(db_path) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS synced_tracks (
                id          TEXT PRIMARY KEY,   -- Subsonic track ID
                album_id    TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                album_name  TEXT NOT NULL,
                filename    TEXT NOT NULL,       -- filename on device
                device_path TEXT NOT NULL,       -- full path relative to mount
                synced_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS synced_albums (
                id          TEXT PRIMARY KEY,   -- Subsonic album ID
                artist_id   TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                album_name  TEXT NOT NULL,
                track_count INTEGER NOT NULL DEFAULT 0,
                sync_quality TEXT NOT NULL DEFAULT 'mp3',  -- 'mp3' | 'original'
                synced_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS synced_playlists (
                id              TEXT PRIMARY KEY,   -- Subsonic playlist ID
                name            TEXT NOT NULL,
                track_count     INTEGER NOT NULL DEFAULT 0,
                server_snapshot TEXT,               -- JSON of track IDs at last sync
                device_snapshot TEXT,               -- JSON of device M3U at last sync
                synced_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS selected_artists (
                id   TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lossless INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS selected_playlists (
                id   TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """)
        _migrate_schema(con)


def _column_names(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_schema(con) -> None:
    """Add columns introduced after first release (SQLite has limited ALTER)."""
    cols = _column_names(con, "selected_artists")
    if "lossless" not in cols:
        con.execute(
            "ALTER TABLE selected_artists ADD COLUMN lossless INTEGER NOT NULL DEFAULT 0"
        )

    cols = _column_names(con, "synced_albums")
    if "sync_quality" not in cols:
        con.execute(
            "ALTER TABLE synced_albums ADD COLUMN sync_quality TEXT NOT NULL DEFAULT 'mp3'"
        )


# ============================================================================
# Tracks
# ============================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_track(
    db_path: Path,
    track_id: str,
    album_id: str,
    artist_name: str,
    album_name: str,
    filename: str,
    device_path: str,
) -> None:
    with _connect(db_path) as con:
        con.execute(
            """INSERT INTO synced_tracks
               (id, album_id, artist_name, album_name, filename, device_path, synced_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 device_path=excluded.device_path,
                 synced_at=excluded.synced_at
            """,
            (track_id, album_id, artist_name, album_name, filename, device_path, _now()),
        )


def get_track(db_path: Path, track_id: str) -> dict | None:
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM synced_tracks WHERE id=?", (track_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_tracks_for_album(db_path: Path, album_id: str) -> None:
    with _connect(db_path) as con:
        con.execute("DELETE FROM synced_tracks WHERE album_id=?", (album_id,))


# ============================================================================
# Albums
# ============================================================================

def upsert_album(
    db_path: Path,
    album_id: str,
    artist_id: str,
    artist_name: str,
    album_name: str,
    track_count: int,
    sync_quality: str = "mp3",
) -> None:
    with _connect(db_path) as con:
        con.execute(
            """INSERT INTO synced_albums
               (id, artist_id, artist_name, album_name, track_count, sync_quality, synced_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 track_count=excluded.track_count,
                 sync_quality=excluded.sync_quality,
                 synced_at=excluded.synced_at
            """,
            (album_id, artist_id, artist_name, album_name, track_count, sync_quality, _now()),
        )


def get_synced_album(db_path: Path, album_id: str) -> dict | None:
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM synced_albums WHERE id=?", (album_id,)
        ).fetchone()
    return dict(row) if row else None


def get_synced_album_ids(db_path: Path) -> set[str]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT id FROM synced_albums").fetchall()
    return {r["id"] for r in rows}


def get_synced_albums_for_artist(db_path: Path, artist_id: str) -> list[dict]:
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT * FROM synced_albums WHERE artist_id=?", (artist_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_synced_albums_by_artist(db_path: Path) -> dict[str, int]:
    """Return ``{artist_id: number of albums in manifest}`` for UI summaries."""
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT artist_id, COUNT(*) AS c FROM synced_albums GROUP BY artist_id"
        ).fetchall()
    return {r["artist_id"]: int(r["c"]) for r in rows}


def count_synced_tracks_by_artist(db_path: Path) -> dict[str, int]:
    """Return ``{artist_id: number of tracks on device}`` using manifest joins."""
    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT sa.artist_id, COUNT(st.id) AS c
            FROM synced_tracks st
            JOIN synced_albums sa ON sa.id = st.album_id
            GROUP BY sa.artist_id
            """
        ).fetchall()
    return {r["artist_id"]: int(r["c"]) for r in rows}


def delete_album(db_path: Path, album_id: str) -> None:
    with _connect(db_path) as con:
        con.execute("DELETE FROM synced_albums WHERE id=?", (album_id,))
        con.execute("DELETE FROM synced_tracks WHERE album_id=?", (album_id,))


# ============================================================================
# Playlists
# ============================================================================

def upsert_playlist(
    db_path: Path,
    playlist_id: str,
    name: str,
    track_count: int,
    server_snapshot: str,
    device_snapshot: str,
) -> None:
    with _connect(db_path) as con:
        con.execute(
            """INSERT INTO synced_playlists
               (id, name, track_count, server_snapshot, device_snapshot, synced_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 track_count=excluded.track_count,
                 server_snapshot=excluded.server_snapshot,
                 device_snapshot=excluded.device_snapshot,
                 synced_at=excluded.synced_at
            """,
            (playlist_id, name, track_count, server_snapshot, device_snapshot, _now()),
        )


def get_playlist_snapshot(db_path: Path, playlist_id: str) -> dict | None:
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM synced_playlists WHERE id=?", (playlist_id,)
        ).fetchone()
    return dict(row) if row else None


def get_synced_playlist_ids(db_path: Path) -> set[str]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT id FROM synced_playlists").fetchall()
    return {r["id"] for r in rows}


# ============================================================================
# Artist / playlist selection (user preferences)
# ============================================================================

def set_selected_artists(db_path: Path, artists: list[dict]) -> None:
    """Replace selection. Each item: {id, name, lossless: bool}."""
    with _connect(db_path) as con:
        con.execute("DELETE FROM selected_artists")
        con.executemany(
            "INSERT INTO selected_artists (id, name, lossless) VALUES (?,?,?)",
            [
                (a["id"], a["name"], 1 if a.get("lossless") else 0)
                for a in artists
            ],
        )


def get_artist_lossless_ids(db_path: Path) -> set[str]:
    """Artist IDs marked for original-file sync (FLAC etc.) instead of transcoded MP3."""
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT id FROM selected_artists WHERE lossless=1"
        ).fetchall()
    return {r["id"] for r in rows}


def get_selected_artist_ids(db_path: Path) -> set[str]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT id FROM selected_artists").fetchall()
    return {r["id"] for r in rows}


def set_selected_playlists(db_path: Path, playlists: list[dict]) -> None:
    """Replace the full playlist selection with the provided list of {id, name}."""
    with _connect(db_path) as con:
        con.execute("DELETE FROM selected_playlists")
        con.executemany(
            "INSERT INTO selected_playlists (id, name) VALUES (?,?)",
            [(p["id"], p["name"]) for p in playlists],
        )


def get_selected_playlist_ids(db_path: Path) -> set[str]:
    with _connect(db_path) as con:
        rows = con.execute("SELECT id FROM selected_playlists").fetchall()
    return {r["id"] for r in rows}


# ============================================================================
# Manifest rebuild (scan device, reconcile with manifest)
# ============================================================================

def rebuild_from_device(db_path: Path, device_paths_on_disk: set[str]) -> dict:
    """
    Compare synced_tracks.device_path against the actual files on disk.
    Remove any manifest entries whose files are no longer present.
    Also remove the parent album entry if all its tracks are gone.

    Returns a summary dict with counts.
    """
    with _connect(db_path) as con:
        all_tracks = con.execute("SELECT id, album_id, device_path FROM synced_tracks").fetchall()

    removed_tracks = 0
    albums_to_check: set[str] = set()

    with _connect(db_path) as con:
        for row in all_tracks:
            if row["device_path"] not in device_paths_on_disk:
                con.execute("DELETE FROM synced_tracks WHERE id=?", (row["id"],))
                albums_to_check.add(row["album_id"])
                removed_tracks += 1

    removed_albums = 0
    with _connect(db_path) as con:
        for album_id in albums_to_check:
            count = con.execute(
                "SELECT COUNT(*) as c FROM synced_tracks WHERE album_id=?", (album_id,)
            ).fetchone()["c"]
            if count == 0:
                con.execute("DELETE FROM synced_albums WHERE id=?", (album_id,))
                removed_albums += 1

    return {"removed_tracks": removed_tracks, "removed_albums": removed_albums}


def get_stats(db_path: Path) -> dict:
    with _connect(db_path) as con:
        track_count = con.execute("SELECT COUNT(*) as c FROM synced_tracks").fetchone()["c"]
        album_count = con.execute("SELECT COUNT(*) as c FROM synced_albums").fetchone()["c"]
        playlist_count = con.execute("SELECT COUNT(*) as c FROM synced_playlists").fetchone()["c"]
        sel_artist_count = con.execute("SELECT COUNT(*) as c FROM selected_artists").fetchone()["c"]
        sel_playlist_count = con.execute("SELECT COUNT(*) as c FROM selected_playlists").fetchone()["c"]
    return {
        "tracks": track_count,
        "albums": album_count,
        "playlists": playlist_count,
        "selected_artists": sel_artist_count,
        "selected_playlists": sel_playlist_count,
    }
