"""
Sync engine: orchestrates downloading/transcoding from Navidrome to device.

Progress is reported via a generator that yields dicts:
    {"type": "progress", "message": str, "current": int, "total": int}
    {"type": "done", "summary": dict}
    {"type": "error", "message": str}
"""

import json
import threading
from pathlib import Path
from typing import Generator

from syncsonic.api.subsonic import SubsonicClient
from syncsonic.device import rockbox
from syncsonic.sync import manifest as mf


# ============================================================================
# Type alias
# ============================================================================

ProgressEvent = dict


class SyncCancelled(Exception):
    """Raised when the user requests cancellation (see ``cancel_event`` on sync generators)."""


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SyncCancelled


def _event(msg: str, current: int = 0, total: int = 0) -> ProgressEvent:
    return {"type": "progress", "message": msg, "current": current, "total": total}


def _extension_for_transcode_profile(profile: str) -> str:
    """
    Map a Navidrome/Subsonic transcoding profile *name* to a real file extension.

    The API ``format`` parameter selects the profile by name; names are arbitrary
    (e.g. ``mp3v0``) and must not be used as the literal suffix — the stream is
    still standard MP3/Opus/etc. bytes.
    """
    p = profile.lower().strip()
    if not p:
        return "mp3"
    if "opus" in p:
        return "opus"
    if "flac" in p:
        return "flac"
    if "wav" in p:
        return "wav"
    if "aac" in p or "m4a" in p:
        return "m4a"
    if "vorbis" in p or p in ("ogg", "oga"):
        return "ogg"
    if "mp3" in p or p.startswith("mp3"):
        return "mp3"
    # Unknown profile name — bytes are still almost always MP3 in typical Navidrome setups
    return "mp3"


# ============================================================================
# Artist / album sync
# ============================================================================

def sync_artists(
    client: SubsonicClient,
    mount: Path,
    db_path: Path,
    artist_ids: list[str],
    transcode_format: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> Generator[ProgressEvent, None, None]:
    """
    For each selected artist, fetch all their albums and sync every album's
    tracks to the device.

    Per-artist quality: default is transcoded MP3 (``transcode_format`` profile);
    artists marked lossless in the manifest sync the original file from the
    server (e.g. FLAC) via ``download``.

    Skips albums already synced at the same quality; re-syncs if the user
    toggles between MP3 and original.
    """
    lossless_ids = mf.get_artist_lossless_ids(db_path) & set(artist_ids)
    all_artists = {a["id"]: a for a in client.get_artists()}

    albums_to_sync: list[tuple[dict, dict, str]] = []  # artist, album_stub, sync_quality
    for aid in artist_ids:
        artist = all_artists.get(aid)
        if not artist:
            continue
        desired = "original" if aid in lossless_ids else "mp3"
        artist_detail = client.get_artist(aid)
        for album_stub in artist_detail.get("album", []):
            album_id = album_stub["id"]
            prev = mf.get_synced_album(db_path, album_id)
            if prev and prev.get("sync_quality") == desired:
                continue
            albums_to_sync.append((artist, album_stub, desired))

    total = len(albums_to_sync)
    yield _event(f"Found {total} album(s) to sync", 0, total)

    synced = 0
    failed = 0
    for idx, (artist, album_stub, desired) in enumerate(albums_to_sync, 1):
        _check_cancel(cancel_event)
        album_id = album_stub["id"]
        artist_name = artist["name"]
        album_name = album_stub["name"]
        use_original = desired == "original"
        label = "original" if use_original else (transcode_format or "download")
        yield _event(f"Syncing {artist_name} – {album_name} ({label})", idx, total)

        try:
            prev = mf.get_synced_album(db_path, album_id)
            if prev and prev.get("sync_quality") != desired:
                mf.delete_album(db_path, album_id)

            album = client.get_album(album_id)
            tracks = album.get("song", [])
            for track in tracks:
                yield from _sync_track(
                    client, mount, db_path,
                    track, artist_name, album_name, album_id, artist["id"],
                    transcode_format,
                    use_original=use_original,
                    cancel_event=cancel_event,
                )
            mf.upsert_album(
                db_path, album_id, artist["id"],
                artist_name, album_name, len(tracks),
                sync_quality=desired,
            )
            synced += 1
        except SyncCancelled:
            raise
        except PermissionError as exc:
            failed += 1
            yield {
                "type": "error",
                "message": (
                    f"Failed {artist_name} – {album_name}: permission denied on device. "
                    f"Check macOS System Settings → Privacy & Security → Files and Folders "
                    f"and ensure your terminal has access to external volumes. ({exc})"
                ),
            }
        except Exception as exc:
            failed += 1
            yield {"type": "error", "message": f"Failed {artist_name} – {album_name}: {exc}"}

    yield {"type": "done", "summary": {"albums_synced": synced, "albums_failed": failed}}


def _sync_track(
    client: SubsonicClient,
    mount: Path,
    db_path: Path,
    track: dict,
    artist_name: str,
    album_name: str,
    album_id: str,
    artist_id: str,
    transcode_format: str | None,
    *,
    use_original: bool = False,
    cancel_event: threading.Event | None = None,
) -> Generator[ProgressEvent, None, None]:
    _check_cancel(cancel_event)
    track_id = track["id"]

    original_path = Path(track.get("path", "unknown"))
    if use_original:
        filename = original_path.name
    elif transcode_format:
        ext = _extension_for_transcode_profile(transcode_format)
        filename = f"{original_path.stem}.{ext}"
    else:
        filename = original_path.name

    dest = rockbox.track_path(mount, artist_name, album_name, filename)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot create directory {dest.parent}: {exc}. "
            "On macOS check System Settings → Privacy & Security → Files and Folders."
        ) from exc

    if dest.exists():
        # File already on disk; record in manifest if missing
        if not mf.get_track(db_path, track_id):
            mf.upsert_track(
                db_path, track_id, album_id,
                artist_name, album_name, filename,
                str(dest.relative_to(mount)),
            )
        return

    try:
        if use_original:
            _, data = client.download_track(track_id)
        elif transcode_format:
            _, data = client.stream_track(track_id, fmt=transcode_format)
        else:
            _, data = client.download_track(track_id)

        dest.write_bytes(data)
        mf.upsert_track(
            db_path, track_id, album_id,
            artist_name, album_name, filename,
            str(dest.relative_to(mount)),
        )
        yield _event(f"  ✓ {filename}")
    except SyncCancelled:
        raise
    except Exception as exc:
        yield {"type": "error", "message": f"  ✗ {filename}: {exc}"}


# ============================================================================
# Playlist sync (server → device)
# ============================================================================

def sync_playlists(
    client: SubsonicClient,
    mount: Path,
    db_path: Path,
    playlist_ids: list[str],
    transcode_format: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> Generator[ProgressEvent, None, None]:
    """
    Write selected playlists as M3U8 files on device, downloading any
    tracks not already present.
    """
    total = len(playlist_ids)
    yield _event(f"Syncing {total} playlist(s)", 0, total)

    for idx, pid in enumerate(playlist_ids, 1):
        _check_cancel(cancel_event)
        try:
            playlist = client.get_playlist(pid)
            name = playlist["name"]
            yield _event(f"Playlist: {name}", idx, total)
            track_paths = []

            for track in playlist.get("entry", []):
                _check_cancel(cancel_event)
                track_id = track["id"]
                artist_name = track.get("artist", "Unknown Artist")
                album_name = track.get("album", "Unknown Album")
                album_id = track.get("albumId", "")
                original_path = Path(track.get("path", "unknown"))

                if transcode_format:
                    ext = _extension_for_transcode_profile(transcode_format)
                    filename = f"{original_path.stem}.{ext}"
                else:
                    filename = original_path.name

                dest = rockbox.track_path(mount, artist_name, album_name, filename)
                dest.parent.mkdir(parents=True, exist_ok=True)

                if not dest.exists():
                    try:
                        if transcode_format:
                            _, data = client.stream_track(track_id, fmt=transcode_format)
                        else:
                            _, data = client.download_track(track_id)
                        dest.write_bytes(data)
                        mf.upsert_track(
                            db_path, track_id, album_id,
                            artist_name, album_name, filename,
                            str(dest.relative_to(mount)),
                        )
                    except SyncCancelled:
                        raise
                    except Exception as exc:
                        yield {"type": "error", "message": f"  ✗ {filename}: {exc}"}
                        continue

                track_paths.append(dest)

            _check_cancel(cancel_event)
            rockbox.write_playlist(mount, name, track_paths)

            server_snapshot = json.dumps([t["id"] for t in playlist.get("entry", [])])
            device_snapshot = json.dumps([str(p.relative_to(mount)) for p in track_paths])
            mf.upsert_playlist(db_path, pid, name, len(track_paths), server_snapshot, device_snapshot)

        except SyncCancelled:
            raise
        except Exception as exc:
            yield {"type": "error", "message": f"Failed playlist {pid}: {exc}"}

    yield {"type": "done", "summary": {"playlists_synced": total}}


# ============================================================================
# Starred tracks → special playlist on device
# ============================================================================

def sync_starred(
    client: SubsonicClient,
    mount: Path,
    db_path: Path,
    transcode_format: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> Generator[ProgressEvent, None, None]:
    yield _event("Fetching starred tracks from Navidrome…")

    try:
        starred = client.get_starred()
        songs = starred.get("song", [])
        yield _event(f"Found {len(songs)} starred track(s)", 0, len(songs))

        track_paths = []
        for idx, track in enumerate(songs, 1):
            _check_cancel(cancel_event)
            track_id = track["id"]
            artist_name = track.get("artist", "Unknown Artist")
            album_name = track.get("album", "Unknown Album")
            album_id = track.get("albumId", "")
            original_path = Path(track.get("path", "unknown"))

            if transcode_format:
                ext = _extension_for_transcode_profile(transcode_format)
                filename = f"{original_path.stem}.{ext}"
            else:
                filename = original_path.name

            dest = rockbox.track_path(mount, artist_name, album_name, filename)
            dest.parent.mkdir(parents=True, exist_ok=True)
            yield _event(f"Starred: {track.get('title', filename)}", idx, len(songs))

            if not dest.exists():
                try:
                    if transcode_format:
                        _, data = client.stream_track(track_id, fmt=transcode_format)
                    else:
                        _, data = client.download_track(track_id)
                    dest.write_bytes(data)
                    mf.upsert_track(
                        db_path, track_id, album_id,
                        artist_name, album_name, filename,
                        str(dest.relative_to(mount)),
                    )
                except SyncCancelled:
                    raise
                except Exception as exc:
                    yield {"type": "error", "message": f"  ✗ {filename}: {exc}"}
                    continue

            track_paths.append(dest)

        _check_cancel(cancel_event)
        rockbox.write_playlist(mount, "Navidrome Starred", track_paths)
        yield {"type": "done", "summary": {"starred_synced": len(track_paths)}}

    except SyncCancelled:
        raise
    except Exception as exc:
        yield {"type": "error", "message": f"Starred sync failed: {exc}"}


# ============================================================================
# Manifest rebuild from device filesystem
# ============================================================================

def rebuild_manifest(mount: Path, db_path: Path) -> dict:
    """
    Scan device filesystem and remove manifest entries for files no longer present.
    Returns a summary dict.
    """
    files_on_disk = {
        str(p.relative_to(mount))
        for p in rockbox.scan_music_files(mount)
    }
    return mf.rebuild_from_device(db_path, files_on_disk)


# ============================================================================
# Playlist conflict detection
# ============================================================================

def detect_playlist_conflicts(
    client: SubsonicClient,
    mount: Path,
    db_path: Path,
) -> list[dict]:
    """
    For each playlist in the manifest, check if the server version or the
    device M3U has changed since the last sync.  Returns a list of conflict dicts.
    """
    conflicts = []
    synced_ids = mf.get_synced_playlist_ids(db_path)

    for pid in synced_ids:
        snap = mf.get_playlist_snapshot(db_path, pid)
        if not snap:
            continue

        # Current server state
        try:
            server_playlist = client.get_playlist(pid)
            current_server = json.dumps([t["id"] for t in server_playlist.get("entry", [])])
        except Exception:
            continue

        # Current device state
        playlist_file = rockbox.playlists_dir(mount) / (
            rockbox.sanitize(snap["name"]) + ".m3u8"
        )
        device_tracks = rockbox.read_playlist(playlist_file)
        current_device = json.dumps(device_tracks)

        server_changed = current_server != snap["server_snapshot"]
        device_changed = current_device != snap["device_snapshot"]

        if server_changed and device_changed:
            conflicts.append({
                "id": pid,
                "name": snap["name"],
                "server_tracks": server_playlist.get("entry", []),
                "device_tracks": device_tracks,
                "server_snapshot": snap["server_snapshot"],
                "device_snapshot": snap["device_snapshot"],
            })

    return conflicts
