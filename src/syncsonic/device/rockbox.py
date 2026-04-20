"""
Rockbox device detection and filesystem operations for the Surfans F20 (or any
Rockbox USB mass storage device).

Expected device layout:
    <mount>/Music/<Artist>/<Album>/<track>.mp3
    <mount>/Playlists/<name>.m3u8
    <mount>/.rockbox/.scrobbler.log
"""

import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path


# ============================================================================
# Device detection
# ============================================================================

def find_rockbox_devices() -> list[Path]:
    """Return all /Volumes/* mount points that contain a .rockbox directory."""
    volumes = Path("/Volumes")
    if not volumes.exists():
        return []
    return [
        p for p in volumes.iterdir()
        if p.is_dir() and (p / ".rockbox").is_dir()
    ]


def get_device(mount_path: str | None = None) -> Path | None:
    """
    Return the device mount Path.

    If mount_path is given, validate it has a .rockbox directory.
    Otherwise auto-detect the first Rockbox device under /Volumes.
    Returns None if not found / not mounted.
    """
    if mount_path:
        p = Path(mount_path)
        if p.is_dir() and (p / ".rockbox").is_dir():
            return p
        return None
    devices = find_rockbox_devices()
    return devices[0] if devices else None


# ============================================================================
# Path helpers
# ============================================================================

def music_dir(mount: Path) -> Path:
    return mount / "Music"


def playlists_dir(mount: Path) -> Path:
    return mount / "Playlists"


def scrobbler_log_path(mount: Path) -> Path:
    return mount / ".rockbox" / ".scrobbler.log"


def track_path(mount: Path, artist: str, album: str, filename: str) -> Path:
    return music_dir(mount) / sanitize(artist) / sanitize(album) / filename


def sanitize(name: str) -> str:
    """
    Replace characters that are problematic on FAT32 / exFAT / macOS USB drivers.

    Prohibited by the FAT spec: < > : " / \\ | ? *
    Also replaced:
      $  — macOS exFAT/FAT32 driver returns EPERM on mkdir for names containing $
           (e.g. A$AP Rocky).  Technically valid per spec but broken in practice.
      #  — causes issues on some players and shell tools.
    """
    name = re.sub(r'[<>:"/\\|?*$#\x00-\x1f]', "_", name)
    return name.strip(". ")


# ============================================================================
# M3U playlist I/O
# ============================================================================

def write_playlist(mount: Path, playlist_name: str, track_paths: list[Path]) -> Path:
    """
    Write an M3U8 playlist to <mount>/Playlists/<playlist_name>.m3u8.
    Paths are stored relative to the mount root (e.g. /Music/Artist/...).
    """
    dest_dir = playlists_dir(mount)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (sanitize(playlist_name) + ".m3u8")
    with open(dest, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n\n")
        for p in track_paths:
            # Store paths relative to the mount root so they work on device
            try:
                rel = p.relative_to(mount)
                f.write(f"/{rel}\n")
            except ValueError:
                f.write(f"{p}\n")
    return dest


def read_playlist(path: Path) -> list[str]:
    """
    Parse an M3U/M3U8 file and return a list of track path strings
    (comment lines and blank lines are skipped).
    """
    if not path.exists():
        return []
    tracks = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tracks.append(line)
    return tracks


def list_playlists(mount: Path) -> list[Path]:
    """Return all .m3u / .m3u8 files found in <mount>/Playlists/."""
    d = playlists_dir(mount)
    if not d.exists():
        return []
    return sorted(
        [p for p in d.iterdir() if p.suffix.lower() in (".m3u", ".m3u8")],
        key=lambda p: p.stem.lower(),
    )


# ============================================================================
# Device filesystem scan (for manifest rebuild)
# ============================================================================

def scan_music_files(mount: Path) -> list[Path]:
    """
    Walk <mount>/Music and return all audio files found.
    Used to rebuild the sync manifest from what's actually on disk.
    """
    audio_exts = {".mp3", ".ogg", ".flac", ".aac", ".opus", ".m4a", ".wav"}
    found = []
    music = music_dir(mount)
    if not music.exists():
        return found
    for root, _dirs, files in os.walk(music):
        for fname in files:
            if Path(fname).suffix.lower() in audio_exts:
                found.append(Path(root) / fname)
    return found


def check_writable(mount: Path) -> tuple[bool, str]:
    """
    Attempt a small write + delete inside the Music directory to confirm the
    device is actually writable before starting a long sync.

    Returns (True, "") on success or (False, human-readable reason) on failure.
    Covers: read-only mount, macOS TCC/permission denial, full disk, etc.
    """
    test_dir = music_dir(mount)
    try:
        test_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, (
            f"Cannot create Music directory on device: {exc}. "
            "The volume may be read-only or macOS may be blocking access — "
            "check System Settings → Privacy & Security → Files and Folders."
        )

    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".syncsonic_writetest_", dir=test_dir)
        os.close(fd)
        os.unlink(tmp_path)
        return True, ""
    except PermissionError as exc:
        return False, (
            f"Device is mounted but writes are not permitted ({exc}). "
            "On macOS: System Settings → Privacy & Security → Files and Folders — "
            "make sure your terminal / app has write access to external volumes."
        )
    except OSError as exc:
        return False, f"Write test failed: {exc}"


# ============================================================================
# Eject / unmount
# ============================================================================

def _darwin_dissent_hint(diskutil_text: str) -> str:
    """Append human context when ``diskutil`` reports a dissenter (macOS)."""
    low = diskutil_text.lower()
    if "dissent" not in low:
        return ""
    if "terminal.app" in low or "/usr/bin/login" in low:
        return (
            "\n\nTerminal (or a login shell) is blocking eject — almost always because the shell’s "
            "current directory is still on that USB volume (e.g. after `cd /Volumes/...`). "
            "Run `cd ~` (or `cd /`) in every Terminal tab/window that was using the player, then "
            "try Eject again. Closing those Terminal windows also works."
        )
    if "crowdstrike" in low or "falcon.agent" in low:
        return (
            "\n\nmacOS reports that security software (CrowdStrike Falcon) blocked the unmount. "
            "SyncSonic only asks the OS to eject. Try: close other apps using the volume, eject from "
            "Finder, or ask IT for a removable-media / path exclusion."
        )
    return (
        "\n\nmacOS reports that another process blocked the unmount. SyncSonic only asks the OS to "
        "eject. Close apps (and terminals) that might be using a folder on the device, then try "
        "again or eject from Finder."
    )


def eject_device(mount: Path) -> tuple[bool, str]:
    """
    Unmount and eject the volume at ``mount`` (argv list — no shell).

    * **macOS:** ``diskutil eject <mount>``
    * **Linux:** ``umount <mount>`` (user must have permission to unmount)
    * **Windows:** not supported from here; message explains using the system tray.
    """
    root = mount.resolve()
    if not root.is_dir():
        return False, "Device path is not available."
    if not (root / ".rockbox").is_dir():
        return False, "Path does not look like a Rockbox device."

    path = str(root)
    sysname = platform.system()

    if sysname == "Darwin":
        cp = subprocess.run(
            ["diskutil", "eject", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        parts = []
        if cp.stdout and cp.stdout.strip():
            parts.append(cp.stdout.strip())
        if cp.stderr and cp.stderr.strip():
            parts.append(cp.stderr.strip())
        msg = "\n".join(parts) if parts else ""
        if cp.returncode == 0:
            return True, msg or "Volume ejected."
        hint = _darwin_dissent_hint(msg)
        return False, (msg + hint).strip() or "diskutil eject failed."

    if sysname == "Linux":
        cp = subprocess.run(
            ["umount", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        msg = (cp.stdout or cp.stderr or "").strip()
        if cp.returncode == 0:
            return True, msg or "Volume unmounted."
        return False, msg or "umount failed (close apps using the device, or run from a session that owns the mount)."

    if sysname == "Windows":
        return (
            False,
            "Automatic eject is not supported on Windows; use “Safely Remove Hardware” in the taskbar.",
        )

    return False, f"Eject is not implemented for {sysname}."


def device_info(mount: Path) -> dict:
    """Return basic stats about a mounted device."""
    stat = os.statvfs(mount)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    used = total - free
    return {
        "mount": str(mount),
        "name": mount.name,
        "total_gb": round(total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "free_gb": round(free / 1024**3, 2),
        "has_scrobbler_log": scrobbler_log_path(mount).exists(),
    }
