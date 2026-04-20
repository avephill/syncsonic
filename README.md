# Capsella

Capsella is a fresh project focused on syncing music from Navidrome/Subsonic servers to local devices (currently Rockbox/F20 workflows), with a small web UI for selection, sync, and scrobble flows.

It started from ideas and early code in the original SyncSonic project

## Current scope

- Connect to a Subsonic-compatible server (tested with Navidrome)
- Pick artists/playlists in a web UI
- Sync to device storage and maintain a local manifest
- Submit scrobbles back to the server

## Run

If `syncsonic-web` is installed in your active Python environment:

```bash
syncsonic-web
```

Typical local dev setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
syncsonic-web
```

You can also run directly from the repo root:

```bash
python3 -m syncsonic.web.app
```

Python 3.9+.