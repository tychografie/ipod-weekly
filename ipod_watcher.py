#!/usr/bin/env python3
"""
iPod Shuffle menubar watcher.

Lives in the macOS status bar. Polls /Volumes/ for an iPod Shuffle mount;
when one appears, runs discover_to_shuffle.py (smart mode) in the
background and reports result via the menubar title + a native
notification. Meant to be launched by a LaunchAgent at login.
"""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

HERE = Path(__file__).resolve().parent
SYNC_SCRIPT = HERE / "discover_to_shuffle.py"
VENV_PY = HERE / ".venv" / "bin" / "python"
STATE_FILE = Path.home() / ".ipod-weekly-state.json"
LOG_FILE = HERE / ".watcher.log"

POLL_INTERVAL = 3
SYNC_TIMEOUT = 30 * 60

T_IDLE = "iPod 💤"
T_CONNECTED = "iPod ▶"
T_SYNCING = "iPod ⟳"
T_ERROR = "iPod ⚠"


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


class Watcher(rumps.App):
    def __init__(self) -> None:
        super().__init__("iPod Weekly", title=T_IDLE, quit_button=None)
        # Menubar-only: keep the python process out of the Dock / Cmd-Tab.
        # NSApplicationActivationPolicyAccessory (=1) means "agent app with
        # UI (status item), no Dock presence". Must be set after rumps has
        # created the NSApplication.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        self.status_item = rumps.MenuItem("Waiting for iPod…")
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Sync now", callback=self.on_sync_now),
            rumps.MenuItem("Show state file", callback=self.on_show_state),
            rumps.MenuItem("Open log", callback=self.on_open_log),
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]
        self.connected_path: "Path | None" = None
        self.syncing = False
        self._refresh_status()
        log("watcher started")

        # Hide the menubar item on launch -- it should only appear while an
        # iPod is mounted (or a sync is in flight). The NSStatusItem isn't
        # created until rumps finishes launching the NSApplication, so we
        # defer to a one-shot timer that fires shortly after the event loop
        # is up.
        self._init_timer = rumps.Timer(self._initial_setup, 0.15)
        self._init_timer.start()

    def _initial_setup(self, timer) -> None:
        timer.stop()
        self._set_visible(False)
        # Immediate first poll so plugged-in iPods don't wait for the 3s tick.
        self._poll(None)

    def _set_visible(self, visible: bool) -> None:
        try:
            self._nsapp.nsstatusitem.setVisible_(bool(visible))
        except Exception as e:
            log(f"setVisible({visible}) failed: {e!r}")

    def _refresh_status(self) -> None:
        if self.syncing:
            self.status_item.title = "Syncing…"
        elif self.connected_path:
            self.status_item.title = f"iPod mounted: {self.connected_path.name}"
        else:
            self.status_item.title = "Waiting for iPod…"

    @rumps.timer(POLL_INTERVAL)
    def _poll(self, _sender) -> None:
        if self.syncing:
            self._set_visible(True)
            return
        mount = detect_ipod()
        if mount and self.connected_path != mount:
            log(f"iPod mounted at {mount}")
            self.connected_path = mount
            self.title = T_CONNECTED
            self._set_visible(True)
            self._refresh_status()
            self._start_sync(mount)
        elif not mount and self.connected_path:
            log(f"iPod ejected from {self.connected_path}")
            self.connected_path = None
            self.title = T_IDLE
            self._refresh_status()
            self._set_visible(False)
        else:
            # No edge transition; reassert visibility to match current state.
            self._set_visible(self.connected_path is not None)

    def _start_sync(self, mount: Path) -> None:
        self.syncing = True
        self.title = T_SYNCING
        self._refresh_status()
        threading.Thread(target=self._run_sync, args=(mount,), daemon=True).start()

    def _run_sync(self, mount: Path) -> None:
        try:
            log(f"sync start ({mount})")
            # LaunchAgents get a minimal env -- bake in a sane PATH so yt-dlp
            # can find ffmpeg, and forward IPOD_MOUNT so the sync script skips
            # its auto-detect (in case of multiple mounts).
            env = {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
                "IPOD_MOUNT": str(mount),
                "LANG": "en_US.UTF-8",
            }
            result = subprocess.run(
                [str(VENV_PY), str(SYNC_SCRIPT)],
                cwd=str(HERE),
                capture_output=True,
                text=True,
                timeout=SYNC_TIMEOUT,
                env=env,
            )
            if result.returncode == 0:
                log("sync ok")
                self.title = T_CONNECTED
                self._notify("Sync complete", self._summarize(result.stdout))
            else:
                log(f"sync failed exit={result.returncode}")
                self.title = T_ERROR
                tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
                self._notify("Sync failed", "\n".join(tail) or f"exit {result.returncode}")
        except subprocess.TimeoutExpired:
            log("sync timeout")
            self.title = T_ERROR
            self._notify("Sync timed out", f"> {SYNC_TIMEOUT // 60} minutes")
        except Exception as e:
            log(f"sync crashed: {e!r}")
            self.title = T_ERROR
            self._notify("Sync crashed", str(e))
        finally:
            self.syncing = False
            self._refresh_status()

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
        # rumps.notification wants the app to be a bundled .app to deliver
        # reliably; fall back to osascript which works from plain scripts too.
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

    def on_sync_now(self, _sender) -> None:
        if self.syncing:
            self._notify("Busy", "Sync already in progress")
            return
        mount = self.connected_path or detect_ipod()
        if not mount:
            self._notify("No iPod", "Nothing mounted under /Volumes/ with iPod_Control/")
            return
        self.connected_path = mount
        self._start_sync(mount)

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


if __name__ == "__main__":
    Watcher().run()
