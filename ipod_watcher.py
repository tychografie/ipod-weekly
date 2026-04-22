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
Hold Option while the menu is open for:
    • Show state file
    • Open log
    • Quit watcher
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import rumps
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSEventModifierFlagOption,
)

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "discover_to_shuffle.py"
VENV_PY = HERE / ".venv" / "bin" / "python"
STATE_FILE = Path.home() / ".ipod-weekly-state.json"
LOG_FILE = HERE / ".watcher.log"

POLL_INTERVAL = 3
CHECK_TIMEOUT = 10 * 60
SYNC_TIMEOUT = 30 * 60

S_CONNECTED = "✓"
S_CHECKING = "⋯"
S_SYNCING = "↻"
S_ERROR = "⚠"
DEFAULT_MODEL = "iPod"


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


def _walk_plist(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_plist(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_plist(v)


def get_ipod_model() -> "str | None":
    """Query USB metadata for the attached iPod's marketing name.

    Example returns: "iPod shuffle", "iPod nano", "iPod". Returns None if
    system_profiler doesn't find a device that looks like an iPod.
    """
    try:
        proc = subprocess.run(
            ["system_profiler", "-json", "SPUSBDataType"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except Exception as e:
        log(f"system_profiler failed: {e!r}")
        return None
    for node in _walk_plist(data):
        name = node.get("_name", "") if isinstance(node, dict) else ""
        if name and "ipod" in name.lower():
            return name
    return None


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
            # Dummy primary so Quit has a slot to pair into. An empty
            # NSMenuItem marked hidden takes up no visual space, and its
            # alternate shows up when Option is held.
            _make_hidden_spacer(),
            _make_alt(rumps.MenuItem("Quit watcher", callback=self.on_quit)),
        ]

        self.connected_path: "Path | None" = None
        self.model: str = DEFAULT_MODEL
        self.checking = False
        self.syncing = False
        self._pending_result: "dict | None" = None

        self._set_status("Waiting for iPod…")
        log("watcher started")

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
        if suffix is not None:
            self.title = self._title_for(suffix)
        self.status_item.title = status

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
        if self.checking or self.syncing:
            self._set_visible(True)
            return

        mount = detect_ipod()
        if mount and self.connected_path != mount:
            log(f"iPod mounted at {mount}")
            self.connected_path = mount
            self.model = get_ipod_model() or DEFAULT_MODEL
            log(f"detected model: {self.model!r}")
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
        self._set_status("Checking for new tracks…", suffix=S_CHECKING)
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
        self._set_status("Waiting for approval…", suffix=S_CHECKING)
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
        self._set_status("Syncing…", suffix=S_SYNCING)
        self._set_visible(True)
        threading.Thread(target=self._sync_thread, args=(mount,), daemon=True).start()

    def _sync_thread(self, mount: Path) -> None:
        try:
            log(f"sync start ({mount})")
            env = {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "LANG": "en_US.UTF-8",
                "IPOD_MOUNT": str(mount),
            }
            proc = subprocess.run(
                [str(VENV_PY), str(SYNC_SCRIPT)],
                cwd=str(HERE),
                capture_output=True,
                text=True,
                timeout=SYNC_TIMEOUT,
                env=env,
            )
            if proc.returncode == 0:
                self._pending_result = {
                    "kind": "sync",
                    "ok": True,
                    "summary": self._summarize(proc.stdout),
                }
            else:
                tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
                self._pending_result = {
                    "kind": "sync",
                    "ok": False,
                    "error": "\n".join(tail) or f"exit {proc.returncode}",
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
        if self.checking or self.syncing:
            self._notify("Busy", "A check or sync is already in progress")
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
        if self.checking or self.syncing:
            self._notify("Busy", "Wait for the current operation to finish before unmounting")
            return
        log(f"unmount requested for {mount}")
        try:
            proc = subprocess.run(
                ["diskutil", "eject", str(mount)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                # Next _poll will notice the absence and hide the menubar.
                self._set_status("Unmounted — safe to unplug", suffix=S_CONNECTED)
                self._notify("Unmounted", "Safe to unplug the iPod")
            else:
                self._notify("Unmount failed", (proc.stderr or proc.stdout).strip())
        except Exception as e:
            self._notify("Unmount failed", str(e))

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


def _make_hidden_spacer() -> rumps.MenuItem:
    """Invisible primary item to pair with a trailing Option-only alternate."""
    item = rumps.MenuItem(" ")
    item._menuitem.setHidden_(True)
    return item


if __name__ == "__main__":
    Watcher().run()
