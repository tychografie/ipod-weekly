#!/usr/bin/env python3
"""
iPod Shuffle menubar watcher.

Lives in the macOS status bar. Polls /Volumes/ for a Shuffle mount; when
one appears, runs `discover_to_shuffle.py --check` to see whether Spotify
has rotated any known playlist (DW/RR). If so, asks the user whether to
sync; otherwise stays silent. Meant to be launched by a LaunchAgent at
login.

Menu:
    • Sync now
    • Unmount iPod
    • Sources ▶  (lists current Spotify playlists / albums + Manage…)
Hold Option while the menu is open for:
    • Show state file
    • Open log
    • Quit watcher
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import objc
import rumps
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelBorder,
    NSBezierPath,
    NSButton,
    NSColor,
    NSEventModifierFlagOption,
    NSFloatingWindowLevel,
    NSImage,
    NSMakeRect,
    NSMakeSize,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTableViewSelectionHighlightStyleRegular,
    NSTextField,
    NSView,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "discover_to_shuffle.py"
VENV_PY = HERE / ".venv" / "bin" / "python"
STATE_FILE = Path.home() / ".ipod-weekly-state.json"
CONFIG_FILE = Path.home() / ".ipod-weekly-config.json"
LOG_FILE = HERE / ".watcher.log"

POLL_INTERVAL = 3
CHECK_TIMEOUT = 10 * 60
SYNC_TIMEOUT = 30 * 60
ANIM_INTERVAL = 0.1  # seconds per spinner frame (10 fps redraw)
SPINNER_STEPS = 12   # rotation positions for the indeterminate spinner
ICON_SIZE = 18.0     # pt; menubar shrinks to fit the bar height

S_CONNECTED = "✓"
S_ERROR = "⚠"
DEFAULT_MODEL = "iPod"

# Per-source-type glyph shown in the menubar submenu and the Manage window.
# Type is inferred from the URL pattern; the user never picks it manually.
TYPE_GLYPH = {"playlist": "♪", "album": "○"}


def detect_source_type(url: str) -> str:
    """Return 'playlist' or 'album' from a Spotify URL. Defaults to playlist."""
    if "/album/" in url:
        return "album"
    return "playlist"


# Default sources. Shipped with the app; the menubar "Sources" submenu
# reads/writes CONFIG_FILE to override this.
DEFAULT_PLAYLISTS: "dict[str, dict]" = {
    "dw": {
        "name": "Discover Weekly",
        "url": "https://open.spotify.com/playlist/37i9dQZEVXcIbA23Oqj31h",
    },
    "rr": {
        "name": "Release Radar",
        "url": "https://open.spotify.com/playlist/37i9dQZEVXbqd1Ig0YN47j",
    },
}

# Lines from discover_to_shuffle.py we parse for live progress.
_DL_START_RE = re.compile(r"^Downloading (.+?) \((\d+) tracks?\)")
_TRACK_RE = re.compile(r"^\[(\d+)/(\d+)\]")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def detect_ipod() -> "Path | None":
    volumes = Path("/Volumes")
    if not volumes.exists():
        return None
    for v in volumes.iterdir():
        try:
            if (v / "iPod_Control").is_dir():
                return v
        except (PermissionError, OSError):
            continue
    return None


def load_playlists() -> "dict[str, dict]":
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            pls = data.get("playlists")
            if isinstance(pls, dict) and pls:
                return pls
        except (OSError, json.JSONDecodeError) as e:
            log(f"could not read {CONFIG_FILE}: {e!r}; using defaults")
    return dict(DEFAULT_PLAYLISTS)


def save_playlists(playlists: "dict[str, dict]") -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps({"playlists": playlists}, indent=2) + "\n"
        )
    except OSError as e:
        log(f"could not save {CONFIG_FILE}: {e!r}")


def _make_unique_tag(name: str, existing: "dict[str, dict]") -> str:
    """Derive a short filename-prefix tag from a playlist name (dw, rr, nmf, …)."""
    base = re.sub(r"[^a-z0-9]+", "", name.lower())[:6] or "pl"
    tag = base
    i = 1
    while tag in existing:
        i += 1
        tag = f"{base}{i}"
    return tag


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_capacity(mount: Path) -> str:
    try:
        usage = shutil.disk_usage(str(mount))
    except Exception as e:
        log(f"disk_usage({mount}) failed: {e!r}")
        return ""
    used = usage.used
    total = usage.total
    gb = 1024 * 1024 * 1024
    mb = 1024 * 1024
    used_s = f"{used / mb:.0f} MB" if used < gb else f"{used / gb:.1f} GB"
    total_s = f"{total / gb:.1f} GB"
    return f"{used_s} / {total_s}"


def count_songs(mount: Path) -> int:
    music = mount / "iPod_Control" / "Music"
    if not music.exists():
        return 0
    try:
        return sum(1 for _ in music.rglob("*.mp3"))
    except OSError:
        return 0


def _make_alt(item: rumps.MenuItem) -> rumps.MenuItem:
    """Mark a MenuItem as the Option-held alternate of its predecessor.

    NSMenuItem pairs an alternate with the item immediately above it: both
    share a slot, and the alternate is shown instead when Option is held.
    """
    ns = item._menuitem
    ns.setAlternate_(True)
    ns.setKeyEquivalentModifierMask_(NSEventModifierFlagOption)
    return item


class Watcher(rumps.App):
    def __init__(self) -> None:
        super().__init__("iPod Weekly", title=DEFAULT_MODEL, quit_button=None)
        # Menubar-only: stay out of the Dock / Cmd-Tab.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )

        self.status_item = rumps.MenuItem("Waiting for iPod…")
        # Option-held replacement for status_item: shows capacity + song count.
        self.capacity_item = rumps.MenuItem("—")
        _make_alt(self.capacity_item)

        # Sources submenu: rebuilt whenever the user adds / edits / removes
        # a source. Starts empty; _rebuild_sources_submenu() fills it in.
        self.playlists_menu = rumps.MenuItem("Sources")
        self._manage_window: "ManageWindow | None" = None

        self.menu = [
            self.status_item,
            self.capacity_item,
            None,
            # Paired items: the alternate appears in place of the primary
            # when the user holds Option while the menu is open.
            rumps.MenuItem("Sync now", callback=self.on_sync_now),
            _make_alt(rumps.MenuItem("Show state file", callback=self.on_show_state)),
            rumps.MenuItem("Unmount iPod", callback=self.on_unmount),
            _make_alt(rumps.MenuItem("Open log", callback=self.on_open_log)),
            None,
            self.playlists_menu,
            # Dummy primary so Quit has a slot to pair into. An empty
            # NSMenuItem marked hidden takes up no visual space, and its
            # alternate shows up when Option is held.
            _make_hidden_spacer(),
            _make_alt(rumps.MenuItem("Quit watcher", callback=self.on_quit)),
        ]
        self._rebuild_sources_submenu()

        self.connected_path: "Path | None" = None
        self.model: str = DEFAULT_MODEL
        self.checking = False
        self.syncing = False
        self.unmounting = False
        self._pending_result: "dict | None" = None

        # Animation state. `_anim_base`, when non-None, tells the animation
        # timer to render "{base}..." (dots rotating 1→2→3) in the status
        # line and redraw the menubar icon as a rotating arc (spinner) or
        # filled arc (when `_progress_fraction` is set). Worker threads may
        # update either field directly — Python attribute assignment is
        # atomic, so no lock is needed.
        self._anim_base: "str | None" = None
        self._anim_frame = 0
        self._progress_fraction: "float | None" = None
        # Remembered by the sync line parser so "[05/30]" can be rendered as
        # "Downloading Discover Weekly 5/30" instead of a bare "5/30".
        self._sync_current_name: "str | None" = None

        self._set_status("Waiting for iPod…")
        log("watcher started")

        # Spinner / dots animator. Always running; a no-op tick while
        # `_anim_base` is None so idle state stays static.
        self._anim_timer = rumps.Timer(self._tick_anim, ANIM_INTERVAL)
        self._anim_timer.start()

        # Hide the menubar item on launch; first poll will reveal it if an
        # iPod is already mounted. NSStatusItem isn't attached yet in
        # __init__, so we defer to a one-shot timer.
        self._init_timer = rumps.Timer(self._initial_setup, 0.15)
        self._init_timer.start()

    def _initial_setup(self, timer) -> None:
        timer.stop()
        self._set_visible(False)
        self._poll(None)

    def _set_visible(self, visible: bool) -> None:
        try:
            self._nsapp.nsstatusitem.setVisible_(bool(visible))
        except Exception as e:
            log(f"setVisible({visible}) failed: {e!r}")

    def _title_for(self, suffix: str = "") -> str:
        base = self.model or DEFAULT_MODEL
        return f"{base} {suffix}".rstrip()

    def _set_status(self, status: str, suffix: "str | None" = None) -> None:
        # Any explicit status set cancels animation; the caller has decided
        # the UI is static again (idle / error / complete).
        self._stop_busy_anim()
        if suffix is not None:
            self.title = self._title_for(suffix)
        self.status_item.title = status

    # -------- busy animation --------

    def _tick_anim(self, _sender) -> None:
        """Advance the spinner + dots one frame if a busy phase is active.

        Runs on the main thread at ANIM_INTERVAL. Reads `_anim_base` and
        `_progress_fraction`, which worker threads may have updated to
        reflect live progress.
        """
        if self._anim_base is None:
            return
        self._anim_frame = (self._anim_frame + 1) % 10000
        # Title is just the model name; the rotating / filling arc lives in
        # the status item's image, drawn fresh each tick.
        self.title = self._title_for("")
        self._set_icon(self._render_indicator_image(
            self._progress_fraction, self._anim_frame
        ))
        # Throttle dots to ~3 ticks/dot so the text doesn't blur into a strobe.
        dots = "." * (1 + (self._anim_frame // 3) % 3)
        self.status_item.title = f"{self._anim_base}{dots}"

    def _start_busy_anim(self, base_text: str) -> None:
        """Kick off the animated status line and show the first frame now."""
        self._anim_base = base_text
        self._anim_frame = 0
        self._progress_fraction = None
        self._tick_anim(None)

    def _stop_busy_anim(self) -> None:
        self._anim_base = None
        self._progress_fraction = None
        self._set_icon(None)

    def _update_progress(self, base_text: str) -> None:
        """Thread-safe progress bump; the animator picks this up next tick."""
        self._anim_base = base_text

    def _set_progress_fraction(self, fraction: "float | None") -> None:
        """Switch the indicator into determinate (or back to indeterminate) mode."""
        self._progress_fraction = fraction

    # -------- icon rendering --------

    def _set_icon(self, image) -> None:
        """Attach (or clear) an NSImage on the menubar status item.

        Silently no-ops when rumps hasn't wired up `_nsapp` yet -- that
        happens during __init__, where `_set_status` flows through
        `_stop_busy_anim` and lands here before run() has built the
        NSApplication.
        """
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None:
            return
        item = getattr(nsapp, "nsstatusitem", None)
        if item is None:
            return
        try:
            button = item.button() if hasattr(item, "button") else None
            if button is not None:
                button.setImage_(image)
            else:
                item.setImage_(image)
        except Exception as e:
            log(f"_set_icon failed: {e!r}")

    def _render_indicator_image(
        self,
        fraction: "float | None",
        frame: int,
    ) -> "NSImage":
        """Draw the menubar arc. None fraction → rotating spinner; else filled arc.

        Coordinate notes: NSBezierPath uses counterclockwise angles measured
        from +X (3 o'clock). To fill clockwise from 12 o'clock we start at
        90° and end at 90° - sweep with clockwise=True.
        """
        size = ICON_SIZE
        img = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
        img.lockFocus()

        inset = 2.0
        cx = size / 2.0
        cy = size / 2.0
        radius = (size - 2.0 * inset) / 2.0

        track = NSBezierPath.bezierPath()
        track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            (cx, cy), radius, 0.0, 360.0
        )
        NSColor.blackColor().colorWithAlphaComponent_(0.3).set()
        track.setLineWidth_(1.5)
        track.stroke()

        if fraction is None:
            sweep = 110.0
            start = 90.0 - (frame % SPINNER_STEPS) * (360.0 / SPINNER_STEPS)
        else:
            f = max(0.0, min(1.0, fraction))
            sweep = f * 360.0
            start = 90.0

        if sweep > 0:
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                (cx, cy), radius, start, start - sweep, True
            )
            NSColor.blackColor().set()
            arc.setLineWidth_(2.0)
            arc.setLineCapStyle_(1)  # NSLineCapStyleRound
            arc.stroke()

        img.unlockFocus()
        # Template mode lets the menubar tint for dark/light appearance.
        img.setTemplate_(True)
        return img

    def _refresh_capacity_line(self) -> None:
        """Update the Option-held 'capacity' line based on current mount."""
        mount = self.connected_path
        if mount is None or not mount.exists():
            self.capacity_item.title = "—"
            return
        cap = format_capacity(mount)
        songs = count_songs(mount)
        song_word = "song" if songs == 1 else "songs"
        parts = [f"{songs} {song_word}"]
        if cap:
            parts.append(cap)
        self.capacity_item.title = " • ".join(parts)

    # -------- polling + state machine --------

    @rumps.timer(POLL_INTERVAL)
    def _poll(self, _sender) -> None:
        # Drain any worker-thread result on the main thread first.
        if self._pending_result is not None:
            pending = self._pending_result
            self._pending_result = None
            self._handle_pending(pending)
            return

        # Don't mutate connection state while a subprocess is in flight.
        if self.checking or self.syncing or self.unmounting:
            self._set_visible(True)
            return

        mount = detect_ipod()
        if mount and self.connected_path != mount:
            log(f"iPod mounted at {mount}")
            self.connected_path = mount
            # Use the Finder volume name (e.g. "IPOD", "Tycho's iPod") so the
            # menubar matches what the user sees in Finder. Falls back to
            # DEFAULT_MODEL only if the mount somehow has no name.
            self.model = mount.name or DEFAULT_MODEL
            log(f"detected name: {self.model!r}")
            self._refresh_capacity_line()
            self._start_check(mount)
        elif not mount and self.connected_path:
            log(f"iPod ejected from {self.connected_path}")
            self.connected_path = None
            self.model = DEFAULT_MODEL
            self._refresh_capacity_line()
            self._set_status("Waiting for iPod…", suffix="")
            self._set_visible(False)
        else:
            # Keep the capacity line fresh while idle + connected (it can
            # change if something else writes to the volume).
            if self.connected_path is not None:
                self._refresh_capacity_line()
            self._set_visible(self.connected_path is not None)

    # -------- check phase --------

    def _start_check(self, mount: Path) -> None:
        self.checking = True
        self._start_busy_anim("Checking for new tracks")
        self._set_visible(True)
        threading.Thread(target=self._check_thread, args=(mount,), daemon=True).start()

    def _check_thread(self, mount: Path) -> None:
        try:
            log(f"check start ({mount})")
            env = {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "LANG": "en_US.UTF-8",
            }
            proc = subprocess.run(
                [str(VENV_PY), str(SYNC_SCRIPT), "--check"],
                cwd=str(HERE),
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT,
                env=env,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                self._pending_result = {
                    "kind": "check",
                    "ok": False,
                    "error": "\n".join(tail) or f"exit {proc.returncode}",
                    "mount": mount,
                }
            else:
                try:
                    data = json.loads(proc.stdout)
                    self._pending_result = {
                        "kind": "check",
                        "ok": True,
                        "data": data,
                        "mount": mount,
                    }
                except json.JSONDecodeError as e:
                    self._pending_result = {
                        "kind": "check",
                        "ok": False,
                        "error": f"bad JSON from --check: {e}",
                        "mount": mount,
                    }
        except subprocess.TimeoutExpired:
            self._pending_result = {
                "kind": "check",
                "ok": False,
                "error": f"check timed out after {CHECK_TIMEOUT // 60} min",
                "mount": mount,
            }
        except Exception as e:
            self._pending_result = {"kind": "check", "ok": False, "error": str(e), "mount": mount}
        finally:
            self.checking = False
            log("check done")

    def _handle_check_result(self, pending: dict) -> None:
        # If the iPod was ejected during the check, drop the result.
        if self.connected_path is None:
            log("check result arrived after eject; discarding")
            return

        if not pending["ok"]:
            err = pending.get("error", "")
            log(f"check failed: {err}")
            self._set_status("Check failed", suffix=S_ERROR)
            self._notify("Check failed", err)
            return

        data: dict = pending["data"]
        changed = [p for p in data.values() if p.get("changed")]
        if not changed:
            log("check: up to date")
            self._set_status("Up to date", suffix=S_CONNECTED)
            # Silent: no notification when there's nothing to do.
            return

        # Prompt before doing any work. rumps.alert runs on the main thread
        # (we're on the timer tick here), so it blocks until the user acts.
        lines = ["Spotify has new tracks for:"]
        for p in changed:
            lines.append(f"  • {p['name']} ({p['track_count']} tracks)")
        lines.append("")
        lines.append(f"Sync to {self.connected_path.name}?")
        self._start_busy_anim("Waiting for approval")
        response = rumps.alert(
            title=self.model or DEFAULT_MODEL,
            message="\n".join(lines),
            ok="Sync",
            cancel="Not now",
        )
        # Re-check connection: user may have ejected during the dialog.
        if self.connected_path is None:
            log("iPod ejected while dialog was open; not syncing")
            return
        if response == 1:
            log("user approved sync")
            self._start_sync(self.connected_path)
        else:
            log("user declined sync")
            self._set_status(
                "Sync skipped — will ask again on next connect",
                suffix=S_CONNECTED,
            )

    # -------- sync phase --------

    def _start_sync(self, mount: Path) -> None:
        self.syncing = True
        self._sync_current_name = None
        self._start_busy_anim("Starting sync")
        self._set_visible(True)
        threading.Thread(target=self._sync_thread, args=(mount,), daemon=True).start()

    def _sync_thread(self, mount: Path) -> None:
        proc: "subprocess.Popen | None" = None
        sync_log = None
        try:
            log(f"sync start ({mount})")
            # Tee the child's stdout into the watcher log so we can see exactly
            # which tracks started, completed, and which source produced each
            # MP3 -- without that, diagnosing a stuck sync means running pgrep.
            sync_log = LOG_FILE.open("a")
            sync_log.write(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"--- sync start ({mount}) ---\n"
            )
            sync_log.flush()
            env = {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "LANG": "en_US.UTF-8",
                "IPOD_MOUNT": str(mount),
                # Line-buffer the child's stdout so `[05/30]` lines arrive
                # in real time; without this, Python block-buffers when its
                # stdout is a pipe and the menubar only sees progress in
                # big bursts (or not until the process exits).
                "PYTHONUNBUFFERED": "1",
            }
            proc = subprocess.Popen(
                [str(VENV_PY), str(SYNC_SCRIPT)],
                cwd=str(HERE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            stdout_chunks: "list[str]" = []
            deadline = time.monotonic() + SYNC_TIMEOUT
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_chunks.append(line)
                self._parse_sync_line(line)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sync_log.write(f"[{ts}] sync: {line.rstrip()}\n")
                sync_log.flush()
                if time.monotonic() > deadline:
                    proc.kill()
                    raise subprocess.TimeoutExpired(proc.args, SYNC_TIMEOUT)
            rc = proc.wait()
            full_out = "".join(stdout_chunks)
            if rc == 0:
                self._pending_result = {
                    "kind": "sync",
                    "ok": True,
                    "summary": self._summarize(full_out),
                }
            else:
                tail = full_out.strip().splitlines()[-3:]
                self._pending_result = {
                    "kind": "sync",
                    "ok": False,
                    "error": "\n".join(tail) or f"exit {rc}",
                }
        except subprocess.TimeoutExpired:
            self._pending_result = {
                "kind": "sync",
                "ok": False,
                "error": f"sync timed out after {SYNC_TIMEOUT // 60} min",
            }
        except Exception as e:
            self._pending_result = {"kind": "sync", "ok": False, "error": str(e)}
        finally:
            self.syncing = False
            log("sync done")
            if sync_log is not None:
                try:
                    sync_log.write(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"--- sync done ---\n"
                    )
                    sync_log.close()
                except OSError:
                    pass

    def _parse_sync_line(self, line: str) -> None:
        """Update the animated status based on a single stdout line.

        Mirrors the print statements in discover_to_shuffle.py's
        `run_smart_sync`: Checking → Downloading → Copying → Finalizing.
        Runs on the sync worker thread.
        """
        stripped = line.rstrip("\n").strip()
        if not stripped:
            return

        if stripped.startswith("Copying ") and "new track" in stripped:
            self._set_progress_fraction(None)
            self._update_progress("Copying to iPod")
            return
        if stripped.startswith("Rebuilding iPod database"):
            self._set_progress_fraction(None)
            self._update_progress("Finalizing")
            return

        m = _DL_START_RE.match(stripped)
        if m:
            name = _truncate(m.group(1), 22)
            total = m.group(2)
            self._sync_current_name = name
            self._set_progress_fraction(0.0)
            self._update_progress(f"Downloading {name} 0/{total}")
            return

        m = _TRACK_RE.match(stripped)
        if m:
            # "[05/30]" -> 5/30. int() drops the zero-padding used by the
            # sync script's log format.
            n, total = int(m.group(1)), int(m.group(2))
            name = self._sync_current_name
            self._set_progress_fraction(n / total if total else None)
            self._update_progress(
                f"Downloading {name} {n}/{total}" if name else f"Syncing {n}/{total}"
            )
            return

        if stripped.startswith("Checking "):
            # "Checking Discover Weekly..." -> keep the playlist name.
            name = stripped[len("Checking "):].rstrip(".")
            self._set_progress_fraction(None)
            self._update_progress(f"Checking {_truncate(name, 22)}")
            return

    def _handle_sync_result(self, pending: dict) -> None:
        # Capacity / song count changed after a successful sync.
        self._refresh_capacity_line()
        if pending["ok"]:
            self._set_status("Sync complete", suffix=S_CONNECTED)
            self._notify("Sync complete", pending["summary"])
        else:
            self._set_status("Sync failed", suffix=S_ERROR)
            self._notify("Sync failed", pending["error"])

    def _handle_pending(self, pending: dict) -> None:
        kind = pending.get("kind")
        if kind == "check":
            self._handle_check_result(pending)
        elif kind == "sync":
            self._handle_sync_result(pending)
        elif kind == "unmount":
            self._handle_unmount_result(pending)

    # -------- helpers --------

    @staticmethod
    def _summarize(stdout: str) -> str:
        lines = stdout.splitlines()
        for line in lines:
            low = line.lower()
            if "up to date" in low or "nothing to sync" in low:
                return "All playlists up to date"
        picked: list[str] = []
        for line in lines:
            if ":" in line and ("synced" in line.lower() or "failed" in line.lower()):
                picked.append(line.strip())
        return "\n".join(picked[:3]) if picked else "Sync complete"

    def _notify(self, subtitle: str, message: str) -> None:
        try:
            rumps.notification("iPod Weekly", subtitle, message)
            return
        except Exception as e:
            log(f"rumps notify failed: {e!r}; falling back to osascript")
        try:
            body = message.replace('"', "'")
            sub = subtitle.replace('"', "'")
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{body}" with title "iPod Weekly" subtitle "{sub}"',
                ],
                check=False,
                timeout=5,
            )
        except Exception as e:
            log(f"osascript notify failed: {e!r}")

    # -------- menu callbacks --------

    def on_sync_now(self, _sender) -> None:
        if self.checking or self.syncing or self.unmounting:
            self._notify("Busy", "A check, sync, or unmount is already in progress")
            return
        mount = self.connected_path or detect_ipod()
        if not mount:
            self._notify("No iPod", "Nothing mounted under /Volumes/ with iPod_Control/")
            return
        self.connected_path = mount
        self._start_check(mount)

    def on_unmount(self, _sender) -> None:
        mount = self.connected_path or detect_ipod()
        if not mount:
            self._notify("No iPod", "Nothing to unmount")
            return
        if self.checking or self.syncing or self.unmounting:
            self._notify(
                "Busy",
                "Wait for the current operation to finish before unmounting",
            )
            return
        log(f"unmount requested for {mount}")
        self.unmounting = True
        self._start_busy_anim("Unmounting")
        threading.Thread(
            target=self._unmount_thread, args=(mount,), daemon=True
        ).start()

    def _unmount_thread(self, mount: Path) -> None:
        """Try eject, then `unmount force`, verify the volume is actually gone.

        diskutil sometimes returns 0 even when the volume is still mounted
        (e.g. when Finder/Spotlight is briefly holding a handle and the
        command returns before macOS reaps), so we poll the mount path
        after each attempt before declaring success.
        """
        last_err = ""
        try:
            attempts = [
                ("eject", ["diskutil", "eject", str(mount)]),
                ("unmount force", ["diskutil", "unmount", "force", str(mount)]),
            ]
            for label, cmd in attempts:
                log(f"unmount: {label} {mount}")
                try:
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=30
                    )
                except subprocess.TimeoutExpired:
                    log(f"unmount: {label} timed out after 30s")
                    last_err = f"{label} timed out"
                    continue
                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()
                log(
                    f"unmount: {label} rc={proc.returncode} "
                    f"stdout={stdout!r} stderr={stderr!r}"
                )
                if proc.returncode == 0:
                    # diskutil can return before the kernel finishes the
                    # detach -- poll the mount path for up to ~3s.
                    for _ in range(10):
                        if not mount.exists():
                            log(f"unmount: verified gone after {label}")
                            self._pending_result = {
                                "kind": "unmount",
                                "ok": True,
                                "label": label,
                            }
                            return
                        time.sleep(0.3)
                    log(f"unmount: {label} returned 0 but {mount} still present")
                    last_err = (
                        f"{label} reported success but volume is still mounted"
                    )
                    continue
                last_err = stderr or stdout or f"{label} exit {proc.returncode}"
            self._pending_result = {
                "kind": "unmount",
                "ok": False,
                "error": last_err or "unknown error",
            }
        except Exception as e:
            log(f"unmount: unexpected error: {e!r}")
            self._pending_result = {"kind": "unmount", "ok": False, "error": str(e)}
        finally:
            self.unmounting = False
            log("unmount done")

    def _handle_unmount_result(self, pending: dict) -> None:
        if pending["ok"]:
            label = pending.get("label", "")
            self._set_status("Unmounted — safe to unplug", suffix=S_CONNECTED)
            self._notify("Unmounted", f"Safe to unplug the iPod ({label})")
        else:
            err = pending.get("error", "")
            self._set_status(f"Unmount failed: {err[:60]}", suffix=S_ERROR)
            self._notify("Unmount failed", err)

    def on_show_state(self, _sender) -> None:
        if STATE_FILE.exists():
            try:
                pretty = json.dumps(json.loads(STATE_FILE.read_text()), indent=2)
            except json.JSONDecodeError:
                pretty = STATE_FILE.read_text()
            rumps.alert(title="iPod Weekly state", message=pretty)
        else:
            rumps.alert(title="iPod Weekly state", message="(no state file yet)")

    def on_open_log(self, _sender) -> None:
        if not LOG_FILE.exists():
            LOG_FILE.write_text("")
        subprocess.Popen(["open", "-e", str(LOG_FILE)])

    def on_quit(self, _sender) -> None:
        log("quit requested")
        rumps.quit_application()

    # -------- sources submenu + manage window --------

    def _rebuild_sources_submenu(self) -> None:
        """Regenerate the Sources submenu from CONFIG_FILE.

        Items here are visibility-only: a click opens the Manage window so
        the user has one consistent place to edit everything.
        """
        # rumps creates the submenu's NSMenu lazily on first add(), so _menu
        # is None on the very first call — clear() would crash on it.
        if self.playlists_menu._menu is not None:
            self.playlists_menu.clear()
        playlists = load_playlists()
        if not playlists:
            self.playlists_menu.add(rumps.MenuItem("(none)"))
        else:
            for tag, cfg in playlists.items():
                kind = cfg.get("type") or detect_source_type(cfg.get("url", ""))
                glyph = TYPE_GLYPH.get(kind, "•")
                label = f"{glyph}  {cfg.get('name', tag)}"
                # Each row opens the Manage window; bind `tag` eagerly so the
                # selection lands on the right row.
                item = rumps.MenuItem(
                    label,
                    callback=lambda _sender, t=tag: self._open_manage_window(select_tag=t),
                )
                self.playlists_menu.add(item)
        self.playlists_menu.add(None)
        self.playlists_menu.add(
            rumps.MenuItem("Manage…", callback=lambda _s: self._open_manage_window())
        )

    def _open_manage_window(self, select_tag: "str | None" = None) -> None:
        if self._manage_window is None:
            self._manage_window = ManageWindow.alloc().initWithWatcher_(self)
        self._manage_window.show(select_tag)

    def reload_sources(self) -> None:
        """Called by ManageWindow after add/remove/edit so the menu refreshes."""
        self._rebuild_sources_submenu()


def _make_hidden_spacer() -> rumps.MenuItem:
    """Invisible primary item to pair with a trailing Option-only alternate."""
    item = rumps.MenuItem(" ")
    item._menuitem.setHidden_(True)
    return item


# ---------------------------------------------------------------------------
# Manage window (AppKit NSTableView with +/- buttons)
# ---------------------------------------------------------------------------


def _prompt_url(title: str, default: str = "") -> "str | None":
    """Modal text-input prompt; returns the trimmed URL or None on cancel."""
    win = rumps.Window(
        title=title,
        message="Spotify playlist or album URL:",
        default_text=default,
        ok="Save",
        cancel="Cancel",
        dimensions=(420, 24),
    )
    resp = win.run()
    if not resp.clicked:
        return None
    text = resp.text.strip()
    return text or None


def fetch_spotify_source_name(url: str) -> "str | None":
    """One HTTP call to Spotify; returns the album / playlist name or None.

    Used by the add-source flow so the user doesn't have to retype a name
    Spotify already knows.
    """
    try:
        try:
            from spotify_scraper import SpotifyClient  # type: ignore
        except ImportError:
            from spotifyscraper import SpotifyClient  # type: ignore
        client = SpotifyClient()
        if detect_source_type(url) == "album":
            data = client.get_album_info(url)
        else:
            data = client.get_playlist_info(url)
        name = (data.get("name") or "").replace("\xa0", " ").strip()
        return name or None
    except Exception as e:
        log(f"fetch_spotify_source_name({url!r}) failed: {e!r}")
        return None


def _prompt_name(title: str, default: str = "") -> "str | None":
    win = rumps.Window(
        title=title,
        message="Display name (e.g. New Music Friday, Kid A, …):",
        default_text=default,
        ok="Next",
        cancel="Cancel",
        dimensions=(300, 24),
    )
    resp = win.run()
    if not resp.clicked:
        return None
    text = resp.text.strip()
    return text or None


class ManageWindow(NSObject):
    """Floating panel with a table of sources and +/- buttons.

    Owns its NSWindow + NSTableView. Reads/writes CONFIG_FILE via the same
    load_playlists/save_playlists helpers as the rest of the app, then calls
    back into the Watcher to refresh the menubar submenu.
    """

    def initWithWatcher_(self, watcher):  # noqa: N802 — ObjC selector form
        self = objc.super(ManageWindow, self).init()
        if self is None:
            return None
        self._watcher = watcher
        self._tags: "list[str]" = []  # ordered tags, indexed by table row
        self._build_window()
        return self

    # ---------- window construction ----------

    def _build_window(self) -> None:
        rect = NSMakeRect(0, 0, 520, 320)
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
        )
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("iPod Weekly — Sources")
        win.setReleasedWhenClosed_(False)
        win.setMinSize_(NSMakeSize(380, 220))
        win.setLevel_(NSFloatingWindowLevel)

        content = win.contentView()

        # Table sits above a 36-pt strip that holds the +/- buttons.
        button_strip_h = 36.0
        scroll_rect = NSMakeRect(
            0, button_strip_h, rect.size.width, rect.size.height - button_strip_h
        )
        scroll = NSScrollView.alloc().initWithFrame_(scroll_rect)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setSelectionHighlightStyle_(NSTableViewSelectionHighlightStyleRegular)
        table.setAllowsMultipleSelection_(False)
        table.setDoubleAction_(objc.selector(self.editSelected_, signature=b"v@:@"))
        table.setTarget_(self)

        col_type = NSTableColumn.alloc().initWithIdentifier_("type")
        col_type.headerCell().setStringValue_("Type")
        col_type.setWidth_(60.0)
        col_type.setMinWidth_(50.0)
        col_type.setMaxWidth_(80.0)

        col_name = NSTableColumn.alloc().initWithIdentifier_("name")
        col_name.headerCell().setStringValue_("Name")
        col_name.setWidth_(200.0)
        col_name.setMinWidth_(120.0)

        col_url = NSTableColumn.alloc().initWithIdentifier_("url")
        col_url.headerCell().setStringValue_("URL")
        col_url.setWidth_(240.0)
        col_url.setMinWidth_(120.0)

        for c in (col_type, col_name, col_url):
            table.addTableColumn_(c)

        table.setDataSource_(self)
        table.setDelegate_(self)
        scroll.setDocumentView_(table)
        content.addSubview_(scroll)

        # +, −, Edit buttons in the bottom strip.
        plus = NSButton.alloc().initWithFrame_(NSMakeRect(8, 6, 28, 24))
        plus.setTitle_("+")
        plus.setBezelStyle_(1)  # NSBezelStyleRounded
        plus.setTarget_(self)
        plus.setAction_(objc.selector(self.addSource_, signature=b"v@:@"))
        plus.setAutoresizingMask_(NSViewMaxYMargin)
        content.addSubview_(plus)

        minus = NSButton.alloc().initWithFrame_(NSMakeRect(40, 6, 28, 24))
        minus.setTitle_("−")
        minus.setBezelStyle_(1)
        minus.setTarget_(self)
        minus.setAction_(objc.selector(self.removeSelected_, signature=b"v@:@"))
        minus.setAutoresizingMask_(NSViewMaxYMargin)
        content.addSubview_(minus)

        edit = NSButton.alloc().initWithFrame_(NSMakeRect(80, 6, 60, 24))
        edit.setTitle_("Edit")
        edit.setBezelStyle_(1)
        edit.setTarget_(self)
        edit.setAction_(objc.selector(self.editSelected_, signature=b"v@:@"))
        edit.setAutoresizingMask_(NSViewMaxYMargin)
        content.addSubview_(edit)

        hint = NSTextField.alloc().initWithFrame_(
            NSMakeRect(150, 9, rect.size.width - 158, 18)
        )
        hint.setStringValue_("Paste a Spotify playlist or album URL — type is auto-detected.")
        hint.setEditable_(False)
        hint.setBordered_(False)
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setSelectable_(False)
        hint.setTextColor_(hint.textColor().colorWithAlphaComponent_(0.6))
        hint.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(hint)

        self._window = win
        self._table = table

    # ---------- public ----------

    def show(self, select_tag: "str | None" = None) -> None:
        self._reload(select_tag=select_tag)
        self._window.center()
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    # ---------- data ----------

    def _reload(self, select_tag: "str | None" = None) -> None:
        self._tags = list(load_playlists().keys())
        self._table.reloadData()
        if select_tag and select_tag in self._tags:
            row = self._tags.index(select_tag)
            from Foundation import NSIndexSet
            self._table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(row), False
            )
            self._table.scrollRowToVisible_(row)

    # NSTableViewDataSource
    def numberOfRowsInTableView_(self, _table) -> int:  # noqa: N802
        return len(self._tags)

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):  # noqa: N802
        if row < 0 or row >= len(self._tags):
            return ""
        tag = self._tags[row]
        cfg = load_playlists().get(tag, {})
        ident = column.identifier()
        if ident == "type":
            kind = cfg.get("type") or detect_source_type(cfg.get("url", ""))
            return f"{TYPE_GLYPH.get(kind, '•')} {kind}"
        if ident == "name":
            return cfg.get("name", tag)
        if ident == "url":
            return cfg.get("url", "")
        return ""

    # ---------- button actions ----------

    def addSource_(self, _sender) -> None:  # noqa: N802
        url = _prompt_url("Add source")
        if not url:
            return
        kind = detect_source_type(url)
        # Skip the manual prompt and use Spotify's name. On fetch failure
        # (flaky network, bad URL) fall back to asking so we don't dead-end.
        name = fetch_spotify_source_name(url)
        if not name:
            name = _prompt_name(f"Add {kind}")
        if not name:
            return
        playlists = load_playlists()
        tag = _make_unique_tag(name, playlists)
        playlists[tag] = {"name": name, "url": url, "type": kind}
        save_playlists(playlists)
        log(f"source {tag!r} added: {name!r} ({kind})")
        self._watcher.reload_sources()
        self._reload(select_tag=tag)

    def removeSelected_(self, _sender) -> None:  # noqa: N802
        row = self._table.selectedRow()
        if row < 0 or row >= len(self._tags):
            return
        tag = self._tags[row]
        cfg = load_playlists().get(tag, {})
        resp = rumps.alert(
            title=f"Remove {cfg.get('name', tag)}?",
            message=(
                "This stops future syncs from touching this source. Tracks "
                "already on the iPod from it stay until the next sync."
            ),
            ok="Remove",
            cancel="Cancel",
        )
        if resp != 1:
            return
        playlists = load_playlists()
        playlists.pop(tag, None)
        save_playlists(playlists)
        log(f"source {tag!r} removed")
        self._watcher.reload_sources()
        self._reload()

    def editSelected_(self, _sender) -> None:  # noqa: N802
        row = self._table.selectedRow()
        if row < 0 or row >= len(self._tags):
            return
        tag = self._tags[row]
        cfg = load_playlists().get(tag, {})
        new_name = _prompt_name(
            f"Edit {cfg.get('name', tag)}", default=cfg.get("name", "")
        )
        if new_name is None:
            return
        new_url = _prompt_url(
            f"Edit {cfg.get('name', tag)} URL", default=cfg.get("url", "")
        )
        if new_url is None:
            return
        playlists = load_playlists()
        if tag not in playlists:
            return
        playlists[tag] = {
            **playlists[tag],
            "name": new_name,
            "url": new_url,
            "type": detect_source_type(new_url),
        }
        save_playlists(playlists)
        log(f"source {tag!r} edited")
        self._watcher.reload_sources()
        self._reload(select_tag=tag)


if __name__ == "__main__":
    Watcher().run()
