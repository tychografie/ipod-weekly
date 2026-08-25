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
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config -- edit these if your setup differs
# ---------------------------------------------------------------------------

# Smart-mode sources. Tag is used as a filename prefix (dw_*.mp3, rr_*.mp3)
# so selective wipes can target exactly one source's tracks on the iPod.
# Each entry is a Spotify playlist or album URL; the type is inferred from
# the URL path (/playlist/ vs /album/) -- no manual classification needed.
# Defaults used when no user config exists yet.
DEFAULT_PLAYLISTS = {
    "dw": {
        "name": "Discover Weekly",
        "url": "https://open.spotify.com/playlist/37i9dQZEVXcIbA23Oqj31h",
    },
    "rr": {
        "name": "Release Radar",
        "url": "https://open.spotify.com/playlist/37i9dQZEVXbqd1Ig0YN47j",
    },
}


def detect_source_type(url: str) -> str:
    """Return 'playlist' or 'album' from a Spotify URL. Defaults to playlist."""
    if "/album/" in url:
        return "album"
    return "playlist"

# User-editable playlist config. Written by the menubar watcher when the
# user adds / edits / removes a source; missing or malformed → defaults.
CONFIG_FILE = Path.home() / ".ipod-weekly-config.json"

# Per-playlist snapshot hashes live here. A snapshot changes iff Spotify
# rotated the playlist (different ordered set of track URIs / names).
STATE_FILE = Path.home() / ".ipod-weekly-state.json"

# Enriched fetch results, written by --check and reused by the sync that
# usually follows within a minute or two. Without this, every sync pays the
# full fetch + per-track enrichment fan-out a second time even though --check
# just did the identical work. Entries older than FETCH_CACHE_TTL are ignored
# (Spotify could rotate between a stale check and the sync).
FETCH_CACHE_FILE = Path.home() / ".ipod-weekly-fetch-cache.json"
FETCH_CACHE_TTL = 30 * 60  # seconds


def load_playlists() -> "dict[str, dict]":
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            pls = data.get("playlists")
            if isinstance(pls, dict) and pls:
                return pls
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"warning: could not read {CONFIG_FILE} ({e}); using defaults",
                file=sys.stderr,
            )
    return dict(DEFAULT_PLAYLISTS)


PLAYLISTS = load_playlists()

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
# Parallel yt-dlp workers. Downloads are network + ffmpeg bound, so 8 roughly
# halves wall time vs 4. Past ~8 YouTube's per-IP rate limiter starts kicking
# individual searches over to the SoundCloud fallback, which hurts match
# quality more than the extra parallelism helps.
DOWNLOAD_WORKERS = 8
# Parallel get_track_info calls during playlist enrichment (plain HTTPS to
# Spotify's public endpoints; serial this was the slowest non-download phase).
ENRICH_WORKERS = 8
# Parallel `say` processes when pre-generating VoiceOver wavs. Speech
# synthesis is CPU-light; the ceiling is really the USB write to the iPod.
VOICEOVER_WORKERS = 6
# Hard wall per yt-dlp invocation. Real downloads finish in ~10-30s; anything
# past 2 min is almost always a search that won't resolve (we saw 5+ min
# hangs on YouTube Music for tracks with diacritics / comma-joined artists).
# Kill it and let the worker move to the next source / mark the track as miss.
SOURCE_TIMEOUT = 120

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


def fetch_source(url: str) -> "list[dict]":
    """Dispatch to fetch_playlist or fetch_album based on the URL."""
    if detect_source_type(url) == "album":
        return fetch_album(url)
    return fetch_playlist(url)


def fetch_album(url: str) -> "list[dict]":
    """Return list of {title, artist, album, cover_url} dicts for a Spotify album.

    Albums are uniform: one artist, one album name, one cover. So we don't need
    the per-track enrichment HTTP fan-out that fetch_playlist does -- a single
    get_album_info call gives us everything.
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
        data = client.get_album_info(url)
    except Exception as e:
        die(f"Failed to scrape album: {e}")

    album_name = _clean(data.get("name") or "") or "Unknown Album"

    artists_raw = data.get("artists") or []
    if artists_raw and isinstance(artists_raw[0], dict):
        names = [_clean(a.get("name", "")) for a in artists_raw if a.get("name")]
        album_artist = ", ".join(n for n in names if n)
    else:
        album_artist = ", ".join(_clean(str(a)) for a in artists_raw)
    album_artist = album_artist or "Unknown Artist"

    images = data.get("images") or []
    cover_url = ""
    if images and isinstance(images[0], dict):
        cover_url = images[0].get("url", "")
    elif images:
        cover_url = str(images[0])

    raw_tracks = data.get("tracks") or []
    tracks: "list[dict]" = []
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        name = _clean(t.get("name") or t.get("title") or "")
        if not name:
            continue
        tracks.append(
            {
                "title": name,
                "artist": album_artist,
                "album": album_name,
                "cover_url": cover_url,
                "uri": t.get("uri") or t.get("id") or "",
            }
        )
    return tracks


def fetch_playlist(url: str) -> "list[dict]":
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

    # First pass: pull what the playlist response already has, and note which
    # entries are sparse enough to need a per-track get_track_info call.
    tracks: "list[dict]" = []
    need_enrich: "list[int]" = []  # indexes into `tracks`
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        inner = t.get("track", t)
        name = (inner.get("name") or inner.get("title") or "").strip()
        if not name:
            continue

        artist = _extract_artist(inner)
        album, cover_url = _extract_album_and_cover(inner)
        uri = inner.get("uri") or inner.get("id") or ""

        tracks.append(
            {
                "title": name,
                "artist": artist,
                "album": album,
                "cover_url": cover_url,
                "uri": uri,
            }
        )
        if uri and (not album or not cover_url):
            need_enrich.append(len(tracks) - 1)

    # Second pass: enrich the sparse entries in parallel. Serial this was
    # ~1 HTTP round-trip per track and the slowest phase besides downloads.
    if need_enrich:
        total = len(need_enrich)
        print(f"  enriching {total} tracks with album art ({ENRICH_WORKERS} parallel fetches)")
        tls = threading.local()

        def _enrich(idx: int) -> None:
            track = tracks[idx]
            cl = getattr(tls, "client", None)
            if cl is None:
                # SpotifyClient's thread-safety is undocumented; one per worker.
                cl = SpotifyClient()
                tls.client = cl
            try:
                detail = cl.get_track_info(track["uri"])
            except Exception as e:
                print(f"    enrich failed for {track['title']!r}: {e}")
                return
            d_album, d_cover = _extract_album_and_cover(detail)
            track["album"] = track["album"] or d_album
            track["cover_url"] = track["cover_url"] or d_cover
            if not track["artist"]:
                track["artist"] = _extract_artist(detail)

        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as ex:
            list(ex.map(_enrich, need_enrich))

    for track in tracks:
        track["artist"] = (track["artist"] or "Unknown Artist").strip()
        track["album"] = (track["album"] or playlist_name).strip()
        track["cover_url"] = track["cover_url"] or playlist_cover

    return tracks


# ---------------------------------------------------------------------------
# 2. Download with fallback chain
# ---------------------------------------------------------------------------

def download_track(
    track: dict, index: int, out_dir: Path, tag: str = ""
) -> "tuple[Path, str] | None":
    """Try YouTube → SoundCloud → YouTube Music until one yields an MP3.

    Each source attempt is bounded by SOURCE_TIMEOUT so a stalled search
    can't hang the worker indefinitely. Returns (path, source_label) on the
    first success, or None if every source missed.

    Silent on purpose: download_all funnels per-track results through a lock
    so concurrent workers don't interleave their progress lines into soup.
    """
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
        cmd = [
            sys.executable, "-m", "yt_dlp",
            target,
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", AUDIO_QUALITY,
            "--match-filter", "duration < 600",
            # No --embed-thumbnail: tag_track() deletes every yt-dlp tag and
            # re-embeds Spotify's cover, so the thumbnail download + extra
            # ffmpeg pass per track was pure waste. --embed-metadata stays as
            # the fallback for the rare case where tag_track() fails.
            "--embed-metadata",
            "--no-warnings",
            "--quiet",
            "--no-progress",
            "-o", out_template,
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=SOURCE_TIMEOUT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # On TimeoutExpired the child has already been killed by run().
            continue

        if expected.exists():
            return expected, label
        # yt-dlp sometimes writes a different extension if mp3 conversion failed
        for alt in out_dir.glob(f"{base}.*"):
            if alt.suffix != ".mp3":
                alt.unlink(missing_ok=True)

    return None


# ---------------------------------------------------------------------------
# 3. Tag metadata + embed Spotify cover art
# ---------------------------------------------------------------------------

# Album-mode tracks (and playlist fallback covers) share one cover URL across
# many tracks; cache so each URL is fetched once per run, not once per track.
_COVER_CACHE: "dict[str, bytes | None]" = {}
_COVER_LOCK = threading.Lock()


def fetch_cover(url: str) -> "bytes | None":
    if not url:
        return None
    with _COVER_LOCK:
        if url in _COVER_CACHE:
            return _COVER_CACHE[url]
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
    except Exception:
        return None  # don't cache failures; the next track gets a fresh try
    with _COVER_LOCK:
        _COVER_CACHE[url] = data
    return data


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

def _load_shuffle_module():
    """Import ipod-shuffle-4g.py as a module so we can drive Shuffler directly.

    The script is import-safe (all argparse / execution lives under
    __main__), except that its functions reference a module-global
    `verboseprint` that only __main__ defines -- so we inject a no-op.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("ipod_shuffle_4g", str(IPOD_SHUFFLE_SCRIPT))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {IPOD_SHUFFLE_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.verboseprint = lambda *a, **k: None
    return mod


def _voiceover_text(mp3_path: Path) -> str:
    """The exact string Track.populate() feeds to text2speech / the dbid hash.

    Mirrors ipod-shuffle-4g.py: "Title - Artist" when both easy-tags exist,
    else the filename without extension. Must stay byte-identical or the
    pre-generated wav lands under a different dbid and is never used.
    """
    import mutagen

    text = mp3_path.stem
    try:
        audio = mutagen.File(str(mp3_path), easy=True)
    except Exception:
        audio = None
    if audio and audio.get("title", "") and audio.get("artist", ""):
        text = " - ".join(audio.get("title", "") + audio.get("artist", ""))
    return text


def pregenerate_voiceovers() -> "None":
    """Synthesize all track VoiceOver wavs with parallel `say` processes.

    ipod-shuffle-4g.py generates these serially, one blocking `say` call per
    track, straight onto the iPod -- for ~140 tracks that alone is minutes.
    Its text2speech() skips any wav that already exists, so seeding
    Speakable/Tracks/ beforehand turns the serial phase into a no-op.
    """
    music_dir = IPOD_MOUNT / "iPod_Control" / "Music"
    speak_dir = IPOD_MOUNT / "iPod_Control" / "Speakable" / "Tracks"
    speak_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(music_dir.rglob("*.mp3"))
    if not files:
        return
    print(f"  pre-generating {len(files)} VoiceOver clips ({VOICEOVER_WORKERS} parallel)")

    def _one(f: Path) -> None:
        text = _voiceover_text(f)
        if re.search("[А-Яа-я]", text):
            return  # Cyrillic goes through RHVoice; leave it to the library
        dbid = hashlib.md5(text.encode("utf-8", "ignore")).digest()[:8]
        out = speak_dir / ("".join(format(x, "02x") for x in reversed(dbid)) + ".wav")
        if out.exists():
            return
        try:
            subprocess.run(
                ["say", "-o", str(out), "--data-format=LEI16", "--file-format=WAVE", "--", text],
                check=False,
                timeout=60,
            )
        except Exception:
            # A failed clip is fine -- populate() regenerates whatever is
            # missing (serially, but only for the stragglers).
            out.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=VOICEOVER_WORKERS) as ex:
        list(ex.map(_one, files))


def rebuild_db() -> "None":
    # Per-track VoiceOver (-t equivalent): synthesizes a .wav announcement
    # per track so the Shuffle can read the title/artist aloud (the only way
    # to know what's playing on a screenless device). Playlist VoiceOver is
    # skipped because this sync produces no playlists.
    #
    # Fast path: drive the Shuffler class in-process so the parallel wav
    # pre-generation can run between initialize() (which wipes Speakable/)
    # and populate() (which regenerates serially whatever is missing).
    try:
        mod = _load_shuffle_module()
        mod.Text2Speech.check_support()
        shuffle = mod.Shuffler(str(IPOD_MOUNT), track_voiceover=True)
        shuffle.initialize()
        if shutil.which("say"):
            pregenerate_voiceovers()
        shuffle.populate()
        shuffle.write_database()
        return
    except SystemExit:
        raise
    except Exception as e:
        print(f"  in-process rebuild failed ({e}); falling back to ipod-shuffle-4g.py")

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


def load_fetch_cache() -> dict:
    if not FETCH_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(FETCH_CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_fetch_cache(cache: dict) -> None:
    try:
        FETCH_CACHE_FILE.write_text(json.dumps(cache) + "\n")
    except OSError as e:
        print(f"warning: could not save {FETCH_CACHE_FILE}: {e}", file=sys.stderr)


def cached_tracks(cache: dict, tag: str, url: str) -> "list[dict] | None":
    """Return the enriched track list --check stored, if fresh and for this URL."""
    entry = cache.get(tag)
    if not isinstance(entry, dict) or entry.get("url") != url:
        return None
    if time.time() - float(entry.get("fetched_at") or 0) > FETCH_CACHE_TTL:
        return None
    tracks = entry.get("tracks")
    return tracks if isinstance(tracks, list) and tracks else None


def compute_snapshot(tracks: "list[dict]") -> str:
    """Fingerprint of the source -- changes iff Spotify rotated the tracks.

    Prefer hashing the ordered list of Spotify track URIs (stable IDs that
    don't drift). We saw real-world false positives when hashing (artist,
    title) strings -- Spotify silently re-tags titles ("- Remix" gaining a
    suffix, featured-artist ordering shifting) so the same playlist would
    snapshot differently on consecutive fetches even with no rotation.

    Fall back to (artist, title) only if URIs are missing for all tracks
    (very old enriched outputs, or a backend that stops returning them).
    """
    uris = [(t.get("uri") or "").strip() for t in tracks]
    if any(uris):
        sig = "\n".join(uris)
    else:
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

def _run_download_jobs(
    jobs: "list[tuple[str, int, dict]]",
) -> "dict[str, tuple[list[Path], list[dict]]]":
    """Download + tag every (tag, index, track) job through one shared pool.

    Returns {tag: (downloaded, failed)}. One pool for ALL playlists: with
    per-playlist pools, playlist 2 couldn't start until playlist 1's slowest
    straggler (worst case a full SOURCE_TIMEOUT chain) drained the pool.

    Workers run yt-dlp concurrently (network-bound, so the GIL isn't a real
    cost). Output is serialized through a lock:
      • a `[start ]` line when a worker picks up a track (so postmortem
        diff of starts-vs-completions identifies stuck tracks),
      • a `[N/total]` line on completion -- N is the monotonic completion
        count across every playlist, which drives the menubar progress arc,
        and includes which source produced the MP3.
    """
    total = len(jobs)
    results: "dict[str, tuple[list[Path], list[dict]]]" = {
        tag: ([], []) for tag, _, _ in jobs
    }
    if total == 0:
        return results

    state = {"done": 0}
    lock = threading.Lock()

    def _job(job: "tuple[str, int, dict]") -> None:
        tag, index, track = job
        label = tag or "pl"
        with lock:
            print(
                f"[start  ] {label}#{index:02d}  "
                f"{track['artist']} -- {track['title']}",
                flush=True,
            )
        result = download_track(track, index, TMP_DIR, tag=tag)
        tag_err = ""
        if result is not None:
            mp3, _source = result
            try:
                tag_track(mp3, track)
            except Exception as e:
                tag_err = str(e)
        with lock:
            state["done"] += 1
            n = state["done"]
            if result is not None:
                mp3, source = result
                size = human_mb(mp3.stat().st_size)
                print(
                    f"[{n:02d}/{total}] ok    {label}#{index:02d}  "
                    f"{track['artist']} -- {track['title']}  ({source}, {size})",
                    flush=True,
                )
                if tag_err:
                    print(f"  tagging failed ({tag_err}); keeping yt-dlp tags", flush=True)
                results[tag][0].append(mp3)
            else:
                print(
                    f"[{n:02d}/{total}] miss  {label}#{index:02d}  "
                    f"{track['artist']} -- {track['title']}",
                    flush=True,
                )
                results[tag][1].append(track)

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        # list() forces the iterator so exceptions from workers propagate.
        list(ex.map(_job, jobs))

    return results


def download_all(tracks: "list[dict]", tag: str = "") -> "tuple[list[Path], list[dict]]":
    """Single-playlist wrapper around _run_download_jobs (legacy URL mode)."""
    jobs = [(tag, i, t) for i, t in enumerate(tracks, 1)]
    return _run_download_jobs(jobs)[tag]


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
    # A recent --check already did this exact fetch + enrichment; reuse its
    # cache instead of paying the whole fan-out a second time.
    fetch_cache = load_fetch_cache()
    plans: "list[tuple[str, dict, list[dict], str, str | None]]" = []
    for tag, cfg in PLAYLISTS.items():
        print(f"\nChecking {cfg['name']}...")
        tracks = cached_tracks(fetch_cache, tag, cfg["url"])
        if tracks:
            print(f"  reusing {len(tracks)} tracks fetched by the last --check")
        else:
            tracks = fetch_source(cfg["url"])
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

    # --- Phase 2: download everything to temp before touching the iPod.
    # One shared worker pool across all changed playlists, so playlist 2's
    # downloads start immediately instead of waiting for playlist 1's
    # slowest straggler to drain the pool.
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    jobs: "list[tuple[str, int, dict]]" = [
        (tag, i, t)
        for tag, _cfg, tracks, _snapshot, _prev in plans
        for i, t in enumerate(tracks, 1)
    ]
    names = " + ".join(cfg["name"] for _, cfg, _, _, _ in plans)
    print(f"\nDownloading {names} ({len(jobs)} tracks)...")
    per_tag = _run_download_jobs(jobs)
    results: "list[tuple[str, dict, str, list[Path], list[dict]]]" = [
        (tag, cfg, snapshot, *per_tag.get(tag, ([], [])))
        for tag, cfg, _tracks, snapshot, _prev in plans
    ]

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


def run_check() -> None:
    """Report playlist change status to stdout as JSON. No side effects.

    Shape:
        {
          "dw": {"name": "...", "changed": bool, "snapshot": "...",
                 "prev_snapshot": "...", "track_count": N, "synced_at": "..."},
          "rr": { ... }
        }

    fetch_playlist() prints progress lines to stdout; we redirect those to
    stderr so the only thing on stdout is the JSON document (the watcher
    parses it directly).
    """
    state = load_state()
    result: dict = {}
    fetch_cache: dict = {}
    for tag, cfg in PLAYLISTS.items():
        with contextlib.redirect_stdout(sys.stderr):
            tracks = fetch_source(cfg["url"])
        if not tracks:
            result[tag] = {
                "name": cfg["name"],
                "changed": False,
                "error": "playlist returned no tracks",
            }
            continue
        # Stash the enriched fetch so the sync that usually follows this
        # check can skip re-fetching the identical data.
        fetch_cache[tag] = {
            "url": cfg["url"],
            "fetched_at": time.time(),
            "tracks": tracks,
        }
        snapshot = compute_snapshot(tracks)
        prev = state.get(tag, {})
        result[tag] = {
            "name": cfg["name"],
            "changed": prev.get("snapshot") != snapshot,
            "snapshot": snapshot,
            "prev_snapshot": prev.get("snapshot"),
            "track_count": len(tracks),
            "synced_at": prev.get("synced_at"),
        }
    save_fetch_cache(fetch_cache)
    print(json.dumps(result))


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
    p.add_argument(
        "--check",
        action="store_true",
        help="Check-only: emit JSON describing which known playlists changed. No writes.",
    )
    return p.parse_args()


def main() -> "None":
    args = parse_args()
    if args.check:
        if args.url or args.add or args.force or args.reset:
            die("--check is standalone; combine with no other flags.")
        run_check()
        return
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
