"""
Subsonic-compatible HTTP client.

Spec: https://subsonic.org/pages/api.jsp

- Uses protocol version 1.16.1 (``v``), JSON (``f=json``), and client id (``c``).
- Authentication follows the 1.13+ recommendation: per-request random ``s``
  (salt) and ``t`` = md5(password + salt), not cleartext ``p`` (still allowed by
  the spec for testing only). LDAP-only users that cannot use token auth must
  use a server/client that sends ``p`` instead — see Subsonic error code 41.
"""

import hashlib
import re
import secrets
from datetime import datetime

import requests
from json import JSONDecodeError
from urllib.parse import urljoin


# ============================================================================
# Subsonic REST API client
# ============================================================================

class SubsonicError(Exception):
    pass


class SubsonicClient:
    API_VERSION = "1.16.1"
    CLIENT_NAME = "syncsonic"

    def __init__(self, host: str, username: str, password: str):
        self.host_base = host.rstrip("/") if host else ""
        self.base_url = urljoin(self.host_base + "/", "rest/") if self.host_base else ""
        self._username = username
        self._password = password

    # -------------------------------------------------------------------------
    # Low-level helpers
    # -------------------------------------------------------------------------

    def _params(self, extra: dict | None = None) -> dict:
        """Build query params including auth. Salt + token are fresh each call (per spec)."""
        salt = secrets.token_hex(4)
        token = hashlib.md5(
            (self._password + salt).encode("utf-8")
        ).hexdigest()
        p = {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": self.API_VERSION,
            "c": self.CLIENT_NAME,
            "f": "json",
        }
        if extra:
            p.update(extra)
        return p

    def get_json(self, endpoint: str, params: dict | None = None) -> dict:
        resp = requests.get(
            self.base_url + endpoint,
            params=self._params(params),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        sr = data.get("subsonic-response", {})
        if sr.get("status") != "ok":
            err = sr.get("error", {})
            raise SubsonicError(f"Subsonic error {err.get('code')}: {err.get('message')}")
        return sr

    def get_binary(self, endpoint: str, params: dict | None = None) -> tuple[str, bytes]:
        """Return (filename, raw_bytes). Raises SubsonicError if server returns JSON error."""
        resp = requests.get(
            self.base_url + endpoint,
            params=self._params(params),
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            try:
                sr = resp.json().get("subsonic-response", {})
                err = sr.get("error", {})
                raise SubsonicError(f"Subsonic error {err.get('code')}: {err.get('message')}")
            except Exception:
                raise SubsonicError("Server returned JSON instead of binary data")

        cd = resp.headers.get("Content-Disposition", "")
        match = re.findall(r'filename="([^"]+)"', cd)
        filename = match[0] if match else "unknown"
        return filename, resp.content

    # -------------------------------------------------------------------------
    # Server info
    # -------------------------------------------------------------------------

    def check_connection(self) -> tuple[bool, str]:
        """
        Return (True, "") on success, or (False, human-readable reason).
        Use this for diagnostics; ``ping()`` only returns a boolean.
        """
        if not self.host_base.strip():
            return False, "Server URL is empty."
        try:
            self.get_json("ping")
            return True, ""
        except requests.exceptions.SSLError as e:
            return False, f"TLS/SSL error: {e}"
        except requests.exceptions.ConnectionError as e:
            return (
                False,
                f"Could not reach the server (nothing may appear in Navidrome logs): {e}",
            )
        except requests.exceptions.Timeout:
            return False, "Connection timed out — host may be wrong or unreachable."
        except requests.exceptions.HTTPError as e:
            return False, f"HTTP error: {e.response.status_code if e.response else e}"
        except SubsonicError as e:
            return False, f"Navidrome/Subsonic rejected the request: {e}"
        except JSONDecodeError:
            return (
                False,
                "Response was not JSON — check the URL is your Navidrome base URL "
                "(e.g. https://music.example.com) with no extra path.",
            )
        except Exception as e:
            return False, str(e)

    def ping(self) -> bool:
        ok, _ = self.check_connection()
        return ok

    # -------------------------------------------------------------------------
    # Library browsing
    # -------------------------------------------------------------------------

    def get_artists(self) -> list[dict]:
        """Return flat list of {id, name, albumCount} dicts."""
        sr = self.get_json("getArtists")
        artists = []
        for index in sr["artists"]["index"]:
            for artist in index["artist"]:
                artists.append(artist)
        return artists

    def get_recently_added_artists(self, limit: int = 5) -> list[dict]:
        """
        Return up to `limit` unique recently added artists.

        Uses newest albums to infer artist recency, then returns the final
        subset sorted alphabetically by artist name for display.
        """
        if limit <= 0:
            return []

        sr = self.get_json(
            "getAlbumList2",
            {"type": "newest", "size": max(25, limit * 10), "offset": 0},
        )
        albums = sr.get("albumList2", {}).get("album", [])

        recent_by_id: dict[str, dict] = {}
        for album in albums:
            artist_id = album.get("artistId")
            artist_name = (album.get("artist") or "").strip()
            if not artist_id or not artist_name:
                continue

            created_raw = album.get("created", "")
            try:
                created_ts = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created_ts = datetime.min

            current = recent_by_id.get(artist_id)
            if current is None or created_ts > current["created_ts"]:
                recent_by_id[artist_id] = {
                    "id": artist_id,
                    "name": artist_name,
                    "created_ts": created_ts,
                }

        newest_first = sorted(
            recent_by_id.values(),
            key=lambda a: a["created_ts"],
            reverse=True,
        )
        top_recent = newest_first[:limit]
        return sorted(top_recent, key=lambda a: a["name"].upper())

    def get_artist(self, artist_id: str) -> dict:
        """Return artist dict including 'album' list."""
        sr = self.get_json("getArtist", {"id": artist_id})
        return sr["artist"]

    def get_album(self, album_id: str) -> dict:
        """Return album dict including 'song' list."""
        sr = self.get_json("getAlbum", {"id": album_id})
        return sr["album"]

    def get_playlists(self) -> list[dict]:
        sr = self.get_json("getPlaylists")
        return sr["playlists"].get("playlist", [])

    def get_playlist(self, playlist_id: str) -> dict:
        sr = self.get_json("getPlaylist", {"id": playlist_id})
        return sr["playlist"]

    def search(self, query: str, song_count: int = 20) -> dict:
        sr = self.get_json("search3", {
            "query": query,
            "songCount": song_count,
            "albumCount": 0,
            "artistCount": 0,
        })
        return sr.get("searchResult3", {})

    # -------------------------------------------------------------------------
    # Streaming / downloading
    # -------------------------------------------------------------------------

    def stream_track(
        self,
        track_id: str,
        fmt: str | None = None,
        max_bitrate: int | None = None,
    ) -> tuple[str, bytes]:
        params: dict = {"id": track_id}
        if fmt:
            params["format"] = fmt
        if max_bitrate:
            params["maxBitRate"] = max_bitrate
        return self.get_binary("stream", params)

    def download_track(self, track_id: str) -> tuple[str, bytes]:
        return self.get_binary("download", {"id": track_id})

    # -------------------------------------------------------------------------
    # Annotations: starred / scrobble
    # -------------------------------------------------------------------------

    def get_starred(self) -> dict:
        """Return dict with keys 'song', 'album', 'artist' (each a list)."""
        sr = self.get_json("getStarred2")
        starred = sr.get("starred2", {})
        return {
            "song": starred.get("song", []),
            "album": starred.get("album", []),
            "artist": starred.get("artist", []),
        }

    def star(self, song_ids: list[str] | None = None, album_ids: list[str] | None = None) -> None:
        params: dict = {}
        if song_ids:
            params["id"] = song_ids
        if album_ids:
            params["albumId"] = album_ids
        if params:
            self.get_json("star", params)

    def unstar(self, song_ids: list[str] | None = None, album_ids: list[str] | None = None) -> None:
        params: dict = {}
        if song_ids:
            params["id"] = song_ids
        if album_ids:
            params["albumId"] = album_ids
        if params:
            self.get_json("unstar", params)

    def scrobble(self, track_id: str, timestamp_ms: int | None = None, submission: bool = True) -> None:
        params: dict = {"id": track_id, "submission": str(submission).lower()}
        if timestamp_ms is not None:
            params["time"] = timestamp_ms
        self.get_json("scrobble", params)

    # -------------------------------------------------------------------------
    # Playlist management
    # -------------------------------------------------------------------------

    def create_playlist(self, name: str, song_ids: list[str]) -> dict:
        params: dict = {"name": name}
        if song_ids:
            params["songId"] = song_ids
        sr = self.get_json("createPlaylist", params)
        return sr.get("playlist", {})

    def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        song_ids_to_add: list[str] | None = None,
        song_indexes_to_remove: list[int] | None = None,
    ) -> None:
        params: dict = {"playlistId": playlist_id}
        if name:
            params["name"] = name
        if song_ids_to_add:
            params["songIdToAdd"] = song_ids_to_add
        if song_indexes_to_remove:
            params["songIndexToRemove"] = song_indexes_to_remove
        self.get_json("updatePlaylist", params)

    def replace_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        """Replace all tracks in a playlist with a new ordered list."""
        existing = self.get_playlist(playlist_id)
        current_count = len(existing.get("entry", []))

        # Add new tracks first, then remove old ones by index
        params: dict = {"playlistId": playlist_id}
        if song_ids:
            params["songIdToAdd"] = song_ids
        if current_count > 0:
            params["songIndexToRemove"] = list(range(current_count))
        self.get_json("updatePlaylist", params)
