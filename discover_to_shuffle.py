#!/usr/bin/env python3
"""
Spotify weekly playlists -> iPod Shuffle 3G sync pipeline.

Default (no args): smart mode. Check Discover Weekly + Release Radar, and only
re-sync whichever one Spotify has rotated since the last run. State lives in
~/.ipod-weekly-state.json (per-playlist snapshot hash).

With a URL arg: legacy single-playlist mode (unchanged). Downloads each track
as MP3 via yt-dlp (YouTube -> SoundCloud -> YouTube Music fallback), tags
metadata + cover art, wipes the iPod (or --add to keep existing), copies new
files, and rebuilds the iTunesSD database via nims11/IPod-Shuffle-4g.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config -- edit these if your setup differs
# ---------------------------------------------------------------------------
PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZEVXcIbA23Oqj31h"

# Smart-mode playlists. Tag is used as a filename prefix (dw_*.mp3, rr_*.mp3)
# so selective wipes can target exactly one playlist's tracks on the iPod.
PLAYLISTS = {
    "dw": {"name": "Discover Weekly", "url": PLAYLIST_URL},
    "rr": {
        "name": "Release Radar",
        "url": "https://open.spotify.com/playlist/37i9dQZEVXbqd1Ig0YN47j",
    },
}

# Per-playlist snapshot hashes live here. A snapshot changes iff Spotify
# rotated the playlist (different ordered set of track URIs / names).
STATE_FILE = Path.home() / ".ipod-weekly-state.json"

# We used to hardcode "/Volumes/IPOD SHUFFL" but the FAT32 volume label is
# fragile -- after firmware resets or reformat recoveries it comes back as
# just "IPOD". Auto-detect by scanning /Volumes for any mounted FAT volume
# that contains iPod_Control/. Override this env var to force a specific path.
def _detect_ipod_mount() -> "Path":
    override = os.environ.get("IPOD_MOUNT")
    if override:
        return Path(override)
    volumes = Path("/Volumes")
    if not volumes.exists():
        return Path("/Volumes/IPOD")  # fallback; check_environment will error clearly
    candidates = []
    for v in volumes.iterdir():
        try:
            if (v / "iPod_Control").is_dir():
                candidates.append(v)
        except (PermissionError, OSError):
            continue
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer anything that looks like an iPod
        for c in candidates:
            if "ipod" in c.name.lower():
                return c
        return candidates[0]
    return Path("/Volumes/IPOD")


IPOD_MOUNT = _detect_ipod_mount()
TMP_DIR = Path.home() / "discover-weekly-tmp"
IPOD_SHUFFLE_SCRIPT = Path(__file__).resolve().parent / "IPod-Shuffle-4g" / "ipod-shuffle-4g.py"
AUDIO_QUALITY = "5"  # yt-dlp VBR scale: 0 best, 9 worst; 5 ~ 130 kbps VBR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def sanitize(s: str) -> str:
    """FAT32-safe, lowercase, underscore-joined slug, max 50 chars."""
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip()
    s = re.sub(r"[\s_]+", "_", s)
    return s.lower()[:50] or "unknown"


def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------

def check_environment() -> "None":
    if not IPOD_MOUNT.exists():
        die(f"iPod not mounted at {IPOD_MOUNT}. Plug it in and try again.")
    if not (IPOD_MOUNT / "iPod_Control").exists():
        die(f"{IPOD_MOUNT} has no iPod_Control directory -- is this really an iPod Shuffle?")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        die("yt-dlp not installed in the current interpreter. Run: pip install yt-dlp")
    if shutil.which("ffmpeg") is None:
        die("ffmpeg not on PATH. Install with: brew install ffmpeg")
    if not IPOD_SHUFFLE_SCRIPT.exists():
        die(
            f"Missing {IPOD_SHUFFLE_SCRIPT}.\n"
            f"  Clone it alongside this script:\n"
            f"    git clone https://github.com/nims11/IPod-Shuffle-4g.git "
            f"{IPOD_SHUFFLE_SCRIPT.parent}"
        )


# ---------------------------------------------------------------------------
# 1. Read Discover Weekly
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    # spotifyscraper leaves HTML non-breaking spaces in scraped names
    return s.replace("\xa0", " ").strip() if s else ""


def _extract_artist(inner: dict) -> str:
    artists_raw = inner.get("artists") or []
    if artists_raw and isinstance(artists_raw[0], dict):
        names = [_clean(a.get("name", "")) for a in artists_raw if a.get("name")]
        return ", ".join(n for n in names if n)
    if artists_raw:
        return ", ".join(_clean(str(a)) for a in artists_raw)
    return _clean(inner.get("artist", "") or "")


def _extract_album_and_cover(inner: dict) -> "tuple[str, str]":
    album_raw = inner.get("album") or {}
    if isinstance(album_raw, dict):
        album = album_raw.get("name", "")
        images = album_raw.get("images") or []
    else:
        album = str(album_raw)
        images = inner.get("images") or []

    cover_url = ""
    if images and isinstance(images[0], dict):
        # images are typically sorted largest-first; take the first we find
        cover_url = images[0].get("url", "")
    elif images:
        cover_url = str(images[0])
    return album, cover_url


def fetch_playlist(url: str = PLAYLIST_URL) -> "list[dict]":
    """Return list of {title, artist, album, cover_url} dicts.

    SpotifyScraper's get_playlist_info returns only name+artists per track --
    no album name and no cover art. We fetch get_track_info(uri) per track to
    enrich with album metadata + cover URL (needed so VoiceOver on the Shuffle
    reads the right album and the ID3 APIC frame has real art).
    """
    try:
        from spotify_scraper import SpotifyClient  # type: ignore
    except ImportError:
        try:
            from spotifyscraper import SpotifyClient  # type: ignore
        except ImportError:
            die("spotifyscraper not installed. Run: pip install spotifyscraper")

    client = SpotifyClient()

    try:
        data = client.get_playlist_info(url)
    except Exception as e:
        die(f"Failed to scrape playlist: {e}")

    raw_tracks = data.get("tracks") or data.get("items") or []

    # Fallback playlist-level cover + name. spotifyscraper's anonymous response
    # returns empty string for album.name, so we default to the playlist name
    # (e.g. "Discover Weekly") -- gives the Shuffle something meaningful to
    # group under and to read via VoiceOver.
    playlist_images = data.get("images") or []
    playlist_cover = ""
    if playlist_images and isinstance(playlist_images[0], dict):
        playlist_cover = playlist_images[0].get("url", "")
    playlist_name = (data.get("name") or "").strip() or "Discover Weekly"

    tracks: "list[dict]" = []
    total = len(raw_tracks)
    print(f"  enriching {total} tracks with album art (this takes ~{total} HTTP calls)")

    for i, t in enumerate(raw_tracks, 1):
        if not isinstance(t, dict):
            continue
        inner = t.get("track", t)
        name = (inner.get("name") or inner.get("title") or "").strip()
        if not name:
            continue

        artist = _extract_artist(inner)
        album, cover_url = _extract_album_and_cover(inner)

        # Enrich via get_track_info if the playlist entry was sparse
        uri = inner.get("uri") or inner.get("id") or ""
        if uri and (not album or not cover_url):
            try:
                detail = client.get_track_info(uri)
                d_album, d_cover = _extract_album_and_cover(detail)
                album = album or d_album
                cover_url = cover_url or d_cover
                # Also backfill artist if the sparse entry lacked one
                if not artist:
                    artist = _extract_artist(detail)
            except Exception as e:
                print(f"    [{i}/{total}] enrich failed for {name!r}: {e}")

        tracks.append(
            {
                "title": name,
                "artist": (artist or "Unknown Artist").strip(),
                "album": (album or playlist_name).strip(),
                "cover_url": cover_url or playlist_cover,
            }
        )

    return tracks


# ---------------------------------------------------------------------------
# 2. Download with fallback chain
# ---------------------------------------------------------------------------

def download_track(track: dict, index: int, out_dir: Path, tag: str = "") -> "Path | None":
    prefix = f"{tag}_" if tag else ""
    base = f"{prefix}{index:02d}_{sanitize(track['artist'])}_{sanitize(track['title'])}"
    out_template = str(out_dir / f"{base}.%(ext)s")
    expected = out_dir / f"{base}.mp3"

    if expected.exists():
        expected.unlink()

    query = f"{track['artist']} {track['title']}"
    sources = [
        ("YouTube", f"ytsearch1:{query}"),
        ("SoundCloud", f"scsearch1:{query}"),
        ("YouTube Music", f"https://music.youtube.com/search?q={urllib.parse.quote_plus(query)}"),
    ]

    for label, target in sources:
        print(f"  -> {label}...", end=" ", flush=True)
        cmd = [
            sys.executable, "-m", "yt_dlp",
            target,
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", AUDIO_QUALITY,
            "--match-filter", "duration < 600",
            "--embed-thumbnail",
            "--embed-metadata",
            "--no-warnings",
            "--quiet",
            "--no-progress",
            "-o", out_template,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            print("miss")
            continue

        if expected.exists():
            print(f"ok ({human_mb(expected.stat().st_size)})")
            return expected
        # yt-dlp sometimes writes a different extension if mp3 conversion failed
        for alt in out_dir.glob(f"{base}.*"):
            if alt.suffix != ".mp3":
                alt.unlink(missing_ok=True)
        print("miss")

    return None


# ---------------------------------------------------------------------------
# 3. Tag metadata + embed Spotify cover art
# ---------------------------------------------------------------------------

def fetch_cover(url: str) -> "bytes | None":
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read()
    except Exception:
        return None


def tag_track(mp3_path: Path, track: dict) -> "None":
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError

    try:
        tags = ID3(mp3_path)
        tags.delete()  # nuke whatever yt-dlp embedded -- Spotify data is authoritative
    except ID3NoHeaderError:
        tags = ID3()

    tags.add(TIT2(encoding=3, text=track["title"]))
    tags.add(TPE1(encoding=3, text=track["artist"]))
    if track.get("album"):
        tags.add(TALB(encoding=3, text=track["album"]))

    cover = fetch_cover(track.get("cover_url", ""))
    if cover:
        tags.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # front cover
                desc="Cover",
                data=cover,
            )
        )

    tags.save(mp3_path, v2_version=3)  # v2.3 for maximum old-device compatibility


# ---------------------------------------------------------------------------
# 4. Sync to iPod
# ---------------------------------------------------------------------------

def wipe_ipod_music() -> "None":
    music_dir = IPOD_MOUNT / "iPod_Control" / "Music"
    if music_dir.exists():
        for root, _, files in os.walk(music_dir):
            for f in files:
                p = Path(root) / f
                try:
                    p.unlink()
                except OSError as e:
                    print(f"  could not remove {p}: {e}")
    else:
        music_dir.mkdir(parents=True, exist_ok=True)

    # ipod-shuffle-4g.py indexes the entire volume, so any stray MP3 at the
    # root or in old genre folders (e.g. Rock/, Charts/ from a prior owner's
    # library) ends up in the rebuilt iTunesSD alongside Discover Weekly.
    # Sweep every .mp3 outside Music/ so the DB reflects only what we copy.
    stray = 0
    for root, _, files in os.walk(IPOD_MOUNT):
        root_path = Path(root)
        if root_path == music_dir or music_dir in root_path.parents:
            continue
        for f in files:
            if not f.lower().endswith(".mp3"):
                continue
            p = root_path / f
            try:
                p.unlink()
                stray += 1
            except OSError as e:
                print(f"  could not remove {p}: {e}")
    if stray:
        print(f"  removed {stray} stray MP3 file(s) outside iPod_Control/Music/")

    # Reset Shuffle play-state files. ipod-shuffle-4g.py only rewrites
    # iTunesSD, but the Shuffle firmware also reads iTunesPState (current
    # track index + playback offset) and iTunesStats (play counts). If those
    # still reference offsets from the old track layout after we rewrite the
    # DB, the device tries to resume into nonsense and won't play. Deleting
    # them is safe -- the firmware recreates them on next power-on.
    itunes_dir = IPOD_MOUNT / "iPod_Control" / "iTunes"
    for stale in ("iTunesPState", "iTunesStats"):
        p = itunes_dir / stale
        if p.exists():
            try:
                p.unlink()
                print(f"  reset {stale}")
            except OSError as e:
                print(f"  could not remove {p}: {e}")


def sync_to_ipod(mp3_files: "list[Path]") -> "list[Path]":
    music_dir = IPOD_MOUNT / "iPod_Control" / "Music"
    music_dir.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(IPOD_MOUNT).free
    total = sum(f.stat().st_size for f in mp3_files)
    print(f"  {len(mp3_files)} files, {human_mb(total)} to copy; iPod has {human_mb(free)} free")

    copied: "list[Path]" = []
    for f in mp3_files:
        dest = music_dir / f.name
        size = f.stat().st_size
        if size > shutil.disk_usage(IPOD_MOUNT).free:
            print(f"  out of space before {f.name}; stopping at {len(copied)} files")
            break
        try:
            shutil.copy2(f, dest)
            copied.append(dest)
        except OSError as e:
            print(f"  copy failed for {f.name}: {e}")
            break
    return copied


# ---------------------------------------------------------------------------
# 5. Rebuild iTunesSD database
# ---------------------------------------------------------------------------

def rebuild_db() -> "None":
    # nims11/IPod-Shuffle-4g expects the mount path as its positional arg.
    # -t enables per-track VoiceOver: synthesizes a .wav announcement per
    # track via macOS `say` so the Shuffle can read the title/artist aloud
    # (the only way to know what's playing on a screenless device).
    # -p (playlist VoiceOver) is skipped because this sync produces no
    # playlists -- everything lands flat under Music/.
    cmd = [sys.executable, str(IPOD_SHUFFLE_SCRIPT), "-t", str(IPOD_MOUNT)]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        die(f"ipod-shuffle-4g.py failed with exit {e.returncode}")


# ---------------------------------------------------------------------------
# 6. Smart-mode state + selective wipe
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: could not read {STATE_FILE} ({e}); treating as empty")
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        print(f"warning: could not save {STATE_FILE}: {e}")


def compute_snapshot(tracks: "list[dict]") -> str:
    """Fingerprint of the playlist -- changes iff Spotify rotated the tracks.

    Hashing (artist, title) pairs in order. We don't have stable Spotify URIs
    in the enriched output, so (artist, title) is the cheapest reliable proxy;
    collisions require Spotify to replace a track with a different song that
    happens to share artist *and* title -- vanishingly rare for DW/RR.
    """
    sig = "\n".join(f"{t['artist']}\t{t['title']}" for t in tracks)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


def wipe_playlist_tracks(tag: str) -> int:
    """Delete only MP3s whose filename starts with `{tag}_` from Music/."""
    music_dir = IPOD_MOUNT / "iPod_Control" / "Music"
    if not music_dir.exists():
        return 0
    count = 0
    for p in music_dir.rglob(f"{tag}_*.mp3"):
        try:
            p.unlink()
            count += 1
        except OSError as e:
            print(f"  could not remove {p}: {e}")
    return count


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 7. Pipeline runners
# ---------------------------------------------------------------------------

def download_all(tracks: "list[dict]", tag: str = "") -> "tuple[list[Path], list[dict]]":
    """Download + tag every track. Returns (downloaded_mp3s, failed_tracks)."""
    downloaded: "list[Path]" = []
    failed: "list[dict]" = []
    for i, track in enumerate(tracks, 1):
        print(f"[{i:02d}/{len(tracks)}] {track['artist']} -- {track['title']}")
        mp3 = download_track(track, i, TMP_DIR, tag=tag)
        if mp3 is None:
            failed.append(track)
            continue
        try:
            tag_track(mp3, track)
        except Exception as e:
            print(f"  tagging failed ({e}); keeping file with yt-dlp tags")
        downloaded.append(mp3)
    return downloaded, failed


def run_single_playlist(url: str, add: bool) -> None:
    """Legacy one-off mode: fetch a single URL, wipe (or --add), copy, rebuild."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching playlist from {url}")
    tracks = fetch_playlist(url)
    if not tracks:
        die("No tracks found in playlist response.")
    print(f"Got {len(tracks)} tracks")

    downloaded, failed = download_all(tracks)

    print()
    print(f"Downloaded {len(downloaded)}/{len(tracks)} tracks")
    if not downloaded:
        die("Nothing downloaded -- aborting before touching the iPod.")

    if add:
        print("Additive sync: keeping existing iPod tracks.")
    else:
        print(f"Wiping {IPOD_MOUNT / 'iPod_Control' / 'Music'}")
        wipe_ipod_music()

    print("Copying new tracks...")
    copied = sync_to_ipod(downloaded)

    print("Rebuilding iPod database...")
    rebuild_db()

    total_bytes = sum(f.stat().st_size for f in copied)
    print()
    print("=" * 60)
    print(f"Synced:      {len(copied)} tracks ({human_mb(total_bytes)})")
    print(f"Failed:      {len(failed)}")
    for t in failed:
        print(f"             - {t['artist']} -- {t['title']}")
    print("=" * 60)

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print(f'\nDone. Eject with:\n  diskutil eject "{IPOD_MOUNT}"')


def run_smart_sync(force: bool, reset: bool) -> None:
    """Default mode: check each known playlist, re-sync only changed ones."""
    state = load_state()
    first_run = not state or reset

    # --- Phase 1: check each playlist (fetches + enriches, needed for snapshot)
    plans: "list[tuple[str, dict, list[dict], str, str | None]]" = []
    for tag, cfg in PLAYLISTS.items():
        print(f"\nChecking {cfg['name']}...")
        tracks = fetch_playlist(cfg["url"])
        if not tracks:
            print(f"  no tracks returned; skipping {cfg['name']}")
            continue
        snapshot = compute_snapshot(tracks)
        prev = state.get(tag, {}).get("snapshot")
        if not force and prev == snapshot:
            print(f"  unchanged (snapshot {snapshot[:8]}); skipping")
            continue
        prev_str = prev[:8] if prev else "none"
        print(f"  snapshot {prev_str} -> {snapshot[:8]} ({len(tracks)} tracks)")
        plans.append((tag, cfg, tracks, snapshot, prev))

    if not plans:
        print("\nAll playlists up to date. Nothing to sync.")
        return

    # --- Phase 2: download everything to temp before touching the iPod
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    results: "list[tuple[str, dict, str, list[Path], list[dict]]]" = []
    for tag, cfg, tracks, snapshot, _prev in plans:
        print(f"\nDownloading {cfg['name']} ({len(tracks)} tracks)...")
        downloaded, failed = download_all(tracks, tag=tag)
        results.append((tag, cfg, snapshot, downloaded, failed))

    if not any(r[3] for r in results):
        die("Nothing downloaded across any changed playlist -- aborting.")

    # --- Phase 3: wipe the right scope on the iPod
    if first_run:
        reason = "--reset" if reset else "no prior state"
        print(f"\nFull wipe ({reason}): removing all tracks from iPod_Control/Music/")
        wipe_ipod_music()
    else:
        for tag, cfg, _snapshot, _dl, _fail in results:
            n = wipe_playlist_tracks(tag)
            print(f"\nRemoved {n} existing {cfg['name']} track(s) from iPod")

    # --- Phase 4: copy + rebuild DB once for everything
    all_mp3s = [f for _, _, _, files, _ in results for f in files]
    print(f"\nCopying {len(all_mp3s)} new track(s)...")
    copied = sync_to_ipod(all_mp3s)
    copied_names = {p.name for p in copied}

    print("Rebuilding iPod database...")
    rebuild_db()

    # --- Phase 5: persist state for everything that actually landed
    for tag, cfg, snapshot, downloaded, failed in results:
        landed = [f for f in downloaded if f.name in copied_names]
        if not landed:
            print(f"note: nothing from {cfg['name']} made it to disk; not updating state")
            continue
        state[tag] = {
            "name": cfg["name"],
            "url": cfg["url"],
            "snapshot": snapshot,
            "synced_at": now_iso(),
            "track_count": len(landed),
        }
    save_state(state)

    # --- Summary
    total_bytes = sum(f.stat().st_size for f in copied)
    print()
    print("=" * 60)
    for tag, cfg, _snapshot, downloaded, failed in results:
        landed = sum(1 for f in downloaded if f.name in copied_names)
        print(f"{cfg['name']}: {landed} synced, {len(failed)} failed")
        for t in failed:
            print(f"             - {t['artist']} -- {t['title']}")
    print(f"Total on iPod from this run: {len(copied)} files ({human_mb(total_bytes)})")
    print("=" * 60)

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print(f'\nDone. Eject with:\n  diskutil eject "{IPOD_MOUNT}"')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sync Spotify weekly playlists to an iPod Shuffle 3G. "
            "With no args, checks Discover Weekly + Release Radar and syncs only "
            "whichever changed since last run. Pass a URL for one-off sync."
        ),
    )
    p.add_argument(
        "url",
        nargs="?",
        help="Spotify playlist URL. If omitted, smart mode runs on the known playlists.",
    )
    p.add_argument(
        "--add",
        action="store_true",
        help="(URL mode) keep existing iPod tracks instead of wiping.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="(Smart mode) re-sync all known playlists regardless of snapshot.",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="(Smart mode) full wipe before syncing (also clears untagged legacy files).",
    )
    return p.parse_args()


def main() -> "None":
    args = parse_args()
    check_environment()
    if args.url:
        if args.force or args.reset:
            die("--force / --reset are smart-mode flags; drop the URL or drop the flag.")
        run_single_playlist(args.url, add=args.add)
    else:
        if args.add:
            die("--add is only valid with a URL; smart mode decides additively per playlist.")
        run_smart_sync(force=args.force, reset=args.reset)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
