"""
SyncSonic Flask webapp.

Run with:
    syncsonic-web
or:
    python -m syncsonic.web.app
"""

import hashlib
import json
import os
import queue
import threading
import webbrowser
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

# Session-only storage for Navidrome password (never written to config.json).
_SESSION_PASSWORD = "navidrome_password"

from syncsonic import config as cfg
from syncsonic.api.subsonic import SubsonicClient
from syncsonic.device import rockbox
from syncsonic.sync import engine, manifest as mf, scrobble as scb


_config_path = cfg._DEFAULT_CONFIG_PATH
_db_path = cfg.db_path(_config_path)


# ============================================================================
# App setup
# ============================================================================

def _get_or_create_secret_key() -> str:
    """
    Load a persistent random secret key from the config dir, generating it on first run.
    This key signs Flask session cookies.  Never hardcode this value.
    """
    key_path = _config_path.parent / "secret_key"
    if key_path.exists():
        return key_path.read_text().strip()
    import secrets as _secrets
    key = _secrets.token_hex(32)
    _config_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key)
    key_path.chmod(0o600)
    return key


app = Flask(__name__)
app.secret_key = _get_or_create_secret_key()


def _get_app_version() -> str:
    """Return installed package version, or a sensible fallback for local runs."""
    try:
        return package_version("syncsonic")
    except PackageNotFoundError:
        return os.environ.get("SYNCSONIC_VERSION", "dev")


@app.context_processor
def inject_app_version() -> dict:
    return {"app_version": _get_app_version()}


# ============================================================================
# Jinja2 template filters
# ============================================================================

@app.template_filter("hash_entry")
def hash_entry_filter(value: str) -> str:
    """Compute the same SHA1 hash that scrobble.py uses for deduplication."""
    return hashlib.sha1(value.encode()).hexdigest()


@app.template_filter("datetimeformat")
def datetimeformat_filter(value: int) -> str:
    try:
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _load_config() -> dict:
    return cfg.load(_config_path)


def _save_config(c: dict) -> None:
    cfg.save(c, _config_path)


def _env_password() -> str:
    """Check NAVIDROME_PASSWORD environment variable."""
    return os.environ.get("NAVIDROME_PASSWORD", "")


def _session_password() -> str:
    """Return password from session, falling back to env var."""
    return session.get(_SESSION_PASSWORD, "") or _env_password()


def _update_session_password_from_form(form) -> None:
    """If the user typed a new password, store it in the session only (not on disk)."""
    pw = form.get("password", "")
    if pw:
        session[_SESSION_PASSWORD] = pw


def _get_client(c: dict | None = None, *, password: str | None = None) -> SubsonicClient:
    if c is None:
        c = _load_config()
    s = c["server"]
    pwd = _session_password() if password is None else password
    return SubsonicClient(s["host"], s["username"], pwd)


def _get_mount(c: dict | None = None) -> Path | None:
    if c is None:
        c = _load_config()
    mp = c["device"].get("mount_path", "")
    return rockbox.get_device(mp if mp else None)


def _ensure_db() -> None:
    mf.init_db(_db_path)
    scb.init_scrobble_table(_db_path)


# ============================================================================
# Dashboard  /
# ============================================================================

@app.route("/")
def dashboard():
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)
    device_info = rockbox.device_info(mount) if mount else None
    stats = mf.get_stats(_db_path)

    server_ok = False
    if c["server"]["host"]:
        try:
            server_ok = _get_client(c).ping()
        except Exception:
            pass

    return render_template(
        "dashboard.html",
        device=device_info,
        stats=stats,
        server_ok=server_ok,
        config=c,
    )


# ============================================================================
# Artists  /artists
# ============================================================================

@app.route("/artists")
def artists():
    _ensure_db()
    c = _load_config()
    try:
        client = _get_client(c)
        all_artists = client.get_artists()
        recent_artists = client.get_recently_added_artists(limit=5)
    except Exception as exc:
        flash(f"Could not connect to server: {exc}", "error")
        return redirect(url_for("settings"))

    default_quality = c["sync"].get("default_quality", "transcode")
    if default_quality not in {"transcode", "original"}:
        default_quality = "transcode"

    selected_ids = mf.get_selected_artist_ids(_db_path)
    lossless_ids = mf.get_artist_lossless_ids(_db_path)
    synced_album_counts = mf.count_synced_albums_by_artist(_db_path)
    synced_track_counts = mf.count_synced_tracks_by_artist(_db_path)

    enriched_artists = []

    # Group by first letter
    groups: dict[str, list] = {}
    for a in sorted(all_artists, key=lambda x: x["name"].upper()):
        letter = a["name"][0].upper() if a["name"] else "#"
        if not letter.isalpha():
            letter = "#"
        server_albums = int(a.get("albumCount") or 0)
        on_device = synced_album_counts.get(a["id"], 0)
        sync_status = "none"
        if server_albums > 0:
            if on_device >= server_albums:
                sync_status = "complete"
            elif on_device > 0:
                sync_status = "partial"
        elif on_device > 0:
            sync_status = "partial"
        enriched = {
            **a,
            "selected": a["id"] in selected_ids,
            "lossless": a["id"] in lossless_ids,
            "server_albums": server_albums,
            "synced_albums": on_device,
            "synced_tracks": synced_track_counts.get(a["id"], 0),
            "sync_status": sync_status,
        }
        enriched_artists.append(enriched)
        groups.setdefault(letter, []).append(enriched)

    artists_by_id = {a["id"]: a for a in enriched_artists}
    recent_groups = []
    for recent_artist in recent_artists:
        enriched = artists_by_id.get(recent_artist["id"])
        if enriched:
            recent_groups.append(enriched)

    return render_template(
        "artists.html",
        groups=groups,
        recent_artists=recent_groups,
        selected_ids=selected_ids,
        default_quality=default_quality,
    )


@app.route("/artists/save", methods=["POST"])
def artists_save():
    _ensure_db()
    c = _load_config()
    checked_ids = set(request.form.getlist("artist_ids"))
    lossless_ids = set(request.form.getlist("lossless_ids"))

    try:
        client = _get_client(c)
        all_artists = {a["id"]: a["name"] for a in client.get_artists()}
    except Exception as exc:
        flash(f"Could not connect to server: {exc}", "error")
        return redirect(url_for("artists"))

    selected = [
        {
            "id": aid,
            "name": all_artists.get(aid, aid),
            "lossless": aid in lossless_ids,
        }
        for aid in checked_ids
    ]
    mf.set_selected_artists(_db_path, selected)
    flash(f"Saved selection: {len(selected)} artist(s)", "success")
    return redirect(url_for("artists"))


# ============================================================================
# Playlists  /playlists
# ============================================================================

@app.route("/playlists")
def playlists():
    _ensure_db()
    c = _load_config()
    try:
        client = _get_client(c)
        all_playlists = client.get_playlists()
    except Exception as exc:
        flash(f"Could not connect to server: {exc}", "error")
        return redirect(url_for("settings"))

    selected_ids = mf.get_selected_playlist_ids(_db_path)
    mount = _get_mount(c)

    conflict_ids: set[str] = set()
    if mount:
        try:
            conflicts = engine.detect_playlist_conflicts(client, mount, _db_path)
            conflict_ids = {co["id"] for co in conflicts}
        except Exception:
            pass

    enriched = [
        {
            **p,
            "selected": p["id"] in selected_ids,
            "conflict": p["id"] in conflict_ids,
        }
        for p in all_playlists
    ]

    return render_template("playlists.html", playlists=enriched, selected_ids=selected_ids)


@app.route("/playlists/save", methods=["POST"])
def playlists_save():
    _ensure_db()
    c = _load_config()
    checked_ids = set(request.form.getlist("playlist_ids"))

    try:
        client = _get_client(c)
        all_playlists = {p["id"]: p["name"] for p in client.get_playlists()}
    except Exception as exc:
        flash(f"Could not connect to server: {exc}", "error")
        return redirect(url_for("playlists"))

    selected = [{"id": pid, "name": all_playlists.get(pid, pid)} for pid in checked_ids]
    mf.set_selected_playlists(_db_path, selected)
    flash(f"Saved selection: {len(selected)} playlist(s)", "success")
    return redirect(url_for("playlists"))


# ============================================================================
# Playlist conflict resolution  /playlists/<id>/resolve
# ============================================================================

@app.route("/playlists/<playlist_id>/resolve")
def resolve_conflict(playlist_id: str):
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    if not mount:
        flash("No device connected.", "error")
        return redirect(url_for("playlists"))

    try:
        client = _get_client(c)
        conflicts = engine.detect_playlist_conflicts(client, mount, _db_path)
        conflict = next((co for co in conflicts if co["id"] == playlist_id), None)
    except Exception as exc:
        flash(f"Error detecting conflicts: {exc}", "error")
        return redirect(url_for("playlists"))

    if not conflict:
        flash("No conflict detected for this playlist.", "info")
        return redirect(url_for("playlists"))

    return render_template("resolve.html", conflict=conflict)


@app.route("/playlists/<playlist_id>/resolve", methods=["POST"])
def resolve_conflict_submit(playlist_id: str):
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    if not mount:
        flash("No device connected.", "error")
        return redirect(url_for("playlists"))

    # User submits the merged track list: ordered song IDs from the server side
    merged_ids = request.form.getlist("merged_track_ids")
    playlist_name = request.form.get("playlist_name", "")

    try:
        client = _get_client(c)
        client.replace_playlist(playlist_id, merged_ids)

        # Re-fetch and write to device
        playlist = client.get_playlist(playlist_id)
        track_paths = []
        fmt = c["sync"].get("transcode_format")
        for track in playlist.get("entry", []):
            artist_name = track.get("artist", "Unknown Artist")
            album_name = track.get("album", "Unknown Album")
            original_path = Path(track.get("path", "unknown"))
            filename = original_path.stem + f".{fmt}" if fmt else original_path.name
            dest = rockbox.track_path(mount, artist_name, album_name, filename)
            if dest.exists():
                track_paths.append(dest)

        rockbox.write_playlist(mount, playlist_name or playlist["name"], track_paths)

        server_snapshot = json.dumps(merged_ids)
        device_snapshot = json.dumps([str(p.relative_to(mount)) for p in track_paths])
        mf.upsert_playlist(_db_path, playlist_id, playlist_name or playlist["name"],
                           len(track_paths), server_snapshot, device_snapshot)

        flash(f'Conflict resolved for "{playlist_name}".', "success")
    except Exception as exc:
        flash(f"Failed to resolve conflict: {exc}", "error")

    return redirect(url_for("playlists"))


# ============================================================================
# Sync  /sync  (SSE streaming progress)
# ============================================================================

# Background sync state
_sync_queue: queue.Queue = queue.Queue()
_sync_running = threading.Event()
_sync_cancel_event = threading.Event()


def _run_sync_background(
    c: dict,
    mount: Path,
    artist_ids: list[str],
    playlist_ids: list[str],
    password: str,
) -> None:
    fmt = c["sync"].get("transcode_format") or None
    client = _get_client(c, password=password)

    def push(event: dict) -> None:
        _sync_queue.put(event)

    try:
        # Pre-flight: verify the device is actually writable before attempting anything.
        push({"type": "progress", "message": "Checking device write access…", "current": 0, "total": 0})
        ok, err = rockbox.check_writable(mount)
        if not ok:
            push({"type": "error", "message": f"Write test failed — aborting sync. {err}"})
            return

        push({"type": "progress", "message": "Device write test passed.", "current": 0, "total": 0})

        for ev in engine.sync_artists(
            client, mount, _db_path, artist_ids, fmt, cancel_event=_sync_cancel_event
        ):
            push(ev)
        for ev in engine.sync_playlists(
            client, mount, _db_path, playlist_ids, fmt, cancel_event=_sync_cancel_event
        ):
            push(ev)
        if c["sync"].get("sync_starred", True):
            for ev in engine.sync_starred(
                client, mount, _db_path, fmt, cancel_event=_sync_cancel_event
            ):
                push(ev)
    except engine.SyncCancelled:
        push({"type": "cancelled", "message": "Sync stopped by user."})
    except Exception as exc:
        push({"type": "error", "message": str(exc)})
    finally:
        push({"type": "finished"})
        _sync_running.clear()


@app.route("/sync", methods=["POST"])
def sync_start():
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    if not mount:
        flash("No Rockbox device detected. Connect your device and try again.", "error")
        return redirect(url_for("dashboard"))

    if _sync_running.is_set():
        flash("Sync already in progress.", "info")
        return redirect(url_for("sync_progress"))

    artist_ids = list(mf.get_selected_artist_ids(_db_path))
    playlist_ids = list(mf.get_selected_playlist_ids(_db_path))

    if not artist_ids and not playlist_ids:
        flash("No artists or playlists selected. Choose what to sync first.", "info")
        return redirect(url_for("artists"))

    password = _session_password()
    if not password:
        flash(
            "Enter your Navidrome password in Settings (it is kept in this browser session only, not on disk).",
            "error",
        )
        return redirect(url_for("settings"))

    _sync_cancel_event.clear()
    _sync_running.set()
    t = threading.Thread(
        target=_run_sync_background,
        args=(c, mount, artist_ids, playlist_ids, password),
        daemon=True,
    )
    t.start()
    return redirect(url_for("sync_progress"))


@app.route("/sync/cancel", methods=["POST"])
def sync_cancel():
    if _sync_running.is_set():
        _sync_cancel_event.set()
        flash("Stopping sync…", "info")
    else:
        flash("No sync is running.", "info")
    return redirect(url_for("sync_progress"))


@app.route("/sync/progress")
def sync_progress():
    return render_template("sync_progress.html", running=_sync_running.is_set())


@app.route("/sync/stream")
def sync_stream():
    """Server-Sent Events endpoint for real-time sync progress."""

    @stream_with_context
    def generate():
        while True:
            try:
                event = _sync_queue.get(timeout=30)
            except queue.Empty:
                yield "data: {\"type\": \"heartbeat\"}\n\n"
                continue

            yield f"data: {json.dumps(event)}\n\n"

            if event.get("type") == "finished":
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============================================================================
# Manifest rebuild  /rebuild-manifest
# ============================================================================

@app.route("/rebuild-manifest", methods=["POST"])
def rebuild_manifest_route():
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    if not mount:
        flash("No Rockbox device detected.", "error")
        return redirect(url_for("dashboard"))

    result = engine.rebuild_manifest(mount, _db_path)

    cleanup_note = ""
    if c["server"].get("host"):
        try:
            client = _get_client(c)
            cleaned = engine.prune_tracks_missing_on_server(client, mount, _db_path)
            cleanup_note = (
                f" Pruned missing-server tracks: checked {cleaned['checked_tracks']}, "
                f"missing {cleaned['missing_on_server']}, removed files {cleaned['removed_files']}, "
                f"removed manifest tracks {cleaned['removed_manifest_tracks']}, "
                f"removed empty albums {cleaned['removed_empty_albums']}."
            )
            if cleaned["failed_file_deletes"] > 0:
                flash(
                    f"Could not delete {cleaned['failed_file_deletes']} local file(s) due to file permissions or locks.",
                    "warning",
                )
        except Exception as exc:
            flash(f"Server prune skipped: {exc}", "warning")

    flash(
        f"Library reconciled: removed {result['removed_tracks']} track(s) "
        f"and {result['removed_albums']} album row(s) no longer on device.{cleanup_note}",
        "success",
    )
    return redirect(url_for("dashboard"))


@app.route("/device/eject", methods=["POST"])
def device_eject():
    """Unmount / eject the detected Rockbox volume (macOS: diskutil)."""
    if _sync_running.is_set():
        flash("Wait for the current sync to finish before ejecting.", "error")
        return redirect(url_for("dashboard"))
    c = _load_config()
    mount = _get_mount(c)
    if not mount:
        flash("No Rockbox device is connected.", "error")
        return redirect(url_for("dashboard"))
    ok, msg = rockbox.eject_device(mount)
    if ok:
        flash(msg, "success")
    else:
        flash(msg, "error")
    return redirect(url_for("dashboard"))


# ============================================================================
# Scrobble  /scrobble
# ============================================================================

@app.route("/scrobble")
def scrobble():
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    pending = []
    if mount:
        log_path = rockbox.scrobbler_log_path(mount)
        pending = scb.get_pending_scrobbles(log_path, _db_path)

    return render_template("scrobble.html", pending=pending, device=mount)


@app.route("/scrobble/submit", methods=["POST"])
def scrobble_submit():
    _ensure_db()
    c = _load_config()
    mount = _get_mount(c)

    if not mount:
        flash("No device connected.", "error")
        return redirect(url_for("scrobble"))

    selected_hashes = set(request.form.getlist("entry_hashes"))
    log_path = rockbox.scrobbler_log_path(mount)
    all_pending = scb.get_pending_scrobbles(log_path, _db_path)

    to_submit = [
        e for e in all_pending
        if scb._entry_hash(e["artist"], e["title"], e["timestamp"]) in selected_hashes
    ]

    try:
        client = _get_client(c)
        result = scb.submit_scrobbles(to_submit, client, _db_path)
        flash(
            f"Submitted {result['submitted']} scrobble(s). "
            f"Not found: {result['not_found']}. Failed: {result['failed']}.",
            "success" if result["failed"] == 0 else "warning",
        )
    except Exception as exc:
        flash(f"Scrobble submission failed: {exc}", "error")

    return redirect(url_for("scrobble"))


# ============================================================================
# Settings  /settings
# ============================================================================

def _apply_settings_form(form) -> dict:
    """Merge submitted form into config for **disk** persistence (no password field)."""
    c = _load_config()
    c["server"]["host"] = form.get("host", "").rstrip("/")
    c["server"]["username"] = form.get("username", "")
    c["device"]["mount_path"] = form.get("mount_path", "")
    c["sync"]["transcode_format"] = form.get("transcode_format", "mp3")
    default_quality = form.get("default_quality", "transcode")
    c["sync"]["default_quality"] = (
        default_quality if default_quality in {"transcode", "original"} else "transcode"
    )
    c["sync"]["sync_starred"] = "sync_starred" in form
    return c


@app.route("/settings")
def settings():
    c = _load_config()
    devices = [str(d) for d in rockbox.find_rockbox_devices()]
    env_pw = _env_password()
    session_pw = session.get(_SESSION_PASSWORD, "")
    return render_template(
        "settings.html",
        config=c,
        detected_devices=devices,
        has_session_password=bool(session_pw),
        has_env_password=bool(env_pw),
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    c = _apply_settings_form(request.form)
    _update_session_password_from_form(request.form)
    _save_config(c)
    flash("Settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/clear-session-password", methods=["POST"])
def clear_session_password():
    session.pop(_SESSION_PASSWORD, None)
    flash("Removed password from this browser session.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/test-connection", methods=["GET", "POST"])
def test_connection():
    """
    POST: save non-secret settings to disk; store password in session only; then ping.
    GET: ping using saved host/username from disk and password from session.
    """
    if request.method == "POST":
        c = _apply_settings_form(request.form)
        _update_session_password_from_form(request.form)
        _save_config(c)
        host = c["server"]["host"]
        username = c["server"]["username"]
        password = request.form.get("password", "") or _session_password()
    else:
        c = _load_config()
        host = (c["server"].get("host") or "").rstrip("/")
        username = c["server"].get("username") or ""
        password = _session_password()

    if not host:
        flash(
            "Enter your Navidrome base URL first (e.g. https://navidrome.example.com). "
            "Use the Test Connection button below the fields so unsaved values are tested.",
            "error",
        )
        return redirect(url_for("settings"))

    if not password:
        flash(
            "Enter your password above (it is stored in this browser session only, not on disk).",
            "error",
        )
        return redirect(url_for("settings"))

    client = SubsonicClient(host, username, password)
    ok, err = client.check_connection()
    if ok:
        flash("Connection successful — Navidrome accepted ping with these credentials.", "success")
    else:
        flash(err, "error")
    return redirect(url_for("settings"))


# ============================================================================
# Entry point
# ============================================================================

def main():
    _ensure_db()
    host = "127.0.0.1"
    port = 5000

    # Open browser automatically for local CLI launches (opt out with env var).
    if os.environ.get("SYNCSONIC_NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
        url = f"http://{host}:{port}/"
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
