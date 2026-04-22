# discover-to-shuffle

One-shot Python pipeline that takes Spotify's Discover Weekly playlist and
loads it onto an iPod Shuffle 3rd gen — no iTunes, no Spotify API keys.

## What it does

1. Scrapes the Discover Weekly playlist with `spotifyscraper` (no auth needed).
2. For each track, fetches album art + metadata via `get_track_info(uri)`.
3. Downloads each track as MP3 via `yt-dlp`, falling back
   YouTube → SoundCloud → YouTube Music.
4. Retags the MP3s with Spotify-sourced title/artist/album + embeds album art
   via `mutagen` (so VoiceOver reads the right title/artist, not yt-dlp's
   messy YouTube title).
5. Wipes `/Volumes/IPOD SHUFFL/iPod_Control/Music/` and copies the new tracks.
6. Rebuilds `iTunesSD` using `nims11/IPod-Shuffle-4g` so the Shuffle plays
   without ever touching iTunes.

## Setup

This repo ships with everything already installed. If you're setting it up
fresh somewhere else:

```bash
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
git clone https://github.com/nims11/IPod-Shuffle-4g.git
```

Expected tree:

```
ipod-weekly/
├── .venv/                    # python 3.12 venv with spotifyscraper, yt-dlp, mutagen
├── discover_to_shuffle.py
├── requirements.txt
├── README.md
└── IPod-Shuffle-4g/
    └── ipod-shuffle-4g.py
```

## Run

Plug in the iPod (it auto-detects `/Volumes/IPOD` or similar), then:

```bash
# Smart mode: checks Discover Weekly + Release Radar and re-syncs only the
# one(s) Spotify has rotated since last run. Untouched playlist stays intact.
.venv/bin/python discover_to_shuffle.py

# Force a re-sync even if snapshots match:
.venv/bin/python discover_to_shuffle.py --force

# Full wipe + fresh install of both (useful on the first run, or to clean
# up untagged legacy files from before smart mode):
.venv/bin/python discover_to_shuffle.py --reset

# One-off sync of an arbitrary playlist, wipes the iPod:
.venv/bin/python discover_to_shuffle.py <playlist-url>

# Additive one-off (keeps existing tracks):
.venv/bin/python discover_to_shuffle.py --add <playlist-url>
```

The script does preflight checks first, so if the iPod isn't mounted or
ffmpeg is missing, it exits with a clear message before touching anything.

Smart-mode state lives at `~/.ipod-weekly-state.json` (per-playlist snapshot
hash). Smart-mode filenames are prefixed with the playlist tag (`dw_*.mp3`,
`rr_*.mp3`) so a selective re-sync only touches that playlist's files.

## Menubar app (auto-sync on connect)

`ipod_watcher.py` is a tiny Python menubar app (built on `rumps` →
`NSStatusItem`). It polls `/Volumes/` for a Shuffle mount and, when one
appears, runs the smart sync in the background. Status, notifications, and
a "Sync now" menu item live in the menubar.

Install it as a LaunchAgent so it starts on login:

```bash
./install_watcher.sh
```

The script writes `~/Library/LaunchAgents/nl.tycholitjens.ipod-weekly.plist`
and loads it. Output goes to `.watcher.log`. Uninstall with:

```bash
launchctl unload ~/Library/LaunchAgents/nl.tycholitjens.ipod-weekly.plist
rm ~/Library/LaunchAgents/nl.tycholitjens.ipod-weekly.plist
```

When it's done, eject:

```bash
diskutil eject "/Volumes/IPOD SHUFFL"
```

## Caveat about "Discover Weekly"

Because we scrape anonymously (no Spotify OAuth), what we get is Spotify's
logged-out/editorial version of Discover Weekly, **not your personalized
one** — personalization happens server-side based on your auth cookie. It's
still a fresh weekly playlist of new music, just curator-picked rather than
tailored to your history. If you want your actual personalized version, you'd
need to swap `fetch_playlist()` for something that goes through the real
Spotify Web API with OAuth.

## Config

All knobs live at the top of `discover_to_shuffle.py`:

- `PLAYLIST_URL` — Discover Weekly URL (same for every user, personalized server-side)
- `IPOD_MOUNT` — mount point of the Shuffle
- `TMP_DIR` — where MP3s land before being copied to the iPod
- `IPOD_SHUFFLE_SCRIPT` — path to `ipod-shuffle-4g.py`
- `AUDIO_QUALITY` — yt-dlp VBR scale (0 best, 9 worst; default 5 ≈ 130 kbps)

## Notes

- The script skips any track yt-dlp can't find on YouTube, SoundCloud, or
  YT Music and reports the skips at the end.
- `--match-filter "duration < 600"` protects against hour-long fan mixes
  masquerading as singles.
- If the iPod runs out of space mid-copy, the sync stops gracefully and
  rebuilds the DB with whatever made it across.
- The script **deletes every file** under `iPod_Control/Music/` *and* every
  stray `.mp3` elsewhere on the volume (root, old genre folders) before
  copying new tracks — the old Shuffle contents are not preserved. The
  sweep is required because `ipod-shuffle-4g.py` indexes the whole volume,
  so anything left behind would show up in the rebuilt `iTunesSD`.
