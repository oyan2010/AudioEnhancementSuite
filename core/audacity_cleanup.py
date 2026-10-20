"""
Audacity pre-launch cleanup + background dialog dismisser.

Why this exists
---------------
When Audacity starts, it can show several modal dialogs that block the
`mod-script-pipe` named-pipe commands used by the post-processing stage:

  1. "Welcome to Audacity"        – first run / always-on dialog
  2. "Audacity Recovery"          – after a previous crash/force-close
  3. "Choose your project folder" – missing project folder
  4. "Unsaved Projects"           – on every startup if a session was interrupted

These dialogs enter a modal event loop that completely blocks named-pipe
processing — the script hangs on the first command (see docs/report.md §5).

This module provides two safety nets so the automation never needs the user
to click "Discard" / "OK" manually:

  * `reset_audacity_state()`   – synchronous, called BEFORE launching Audacity.
      – Kills any stuck `audacity.exe` processes (PowerShell / taskkill).
      – Edits `%APPDATA%/audacity/audacity.cfg` to disable the welcome
        dialog and clear the recent files list.
      – Deletes files in `%APPDATA%/audacity/AutoSave/` so the recovery
        dialog has nothing to recover.

  * `dismiss_audacity_dialogs()` – background thread, called AFTER launching.
      – Walks top-level Windows every second for up to 60 s.
      – Closes any window whose title contains blocking keywords
        (recovery / welcome / crash / discard / unsaved / etc.).
      – Skips the main Audacity window (identified by class + title).

Pure stdlib (ctypes + subprocess). No additional dependencies.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────

def get_audacity_appdata_dir() -> Optional[Path]:
    """Return %APPDATA%/audacity or None on non-Windows / missing."""
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "audacity"


# ── Process killer ───────────────────────────────────────────────────────────

def kill_audacity_processes(timeout_sec: float = 8.0) -> int:
    """
    Force-kill any running `audacity.exe` (and child processes via /T).
    Returns the number of processes killed (best-effort).
    """
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "audacity.exe", "/T"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        # taskkill: 0 = success, 128 = no process found
        killed = 0
        if result.returncode == 0:
            # Count "SUCCESS:" lines for reporting
            killed = result.stdout.upper().count("SUCCESS")
            time.sleep(1.5)   # let OS reap
        elif result.returncode == 128:
            pass              # nothing to kill
        else:
            log.debug("taskkill rc=%d: %s", result.returncode, result.stdout.strip()[:200])
        return killed
    except subprocess.TimeoutExpired:
        log.warning("taskkill timed out — Audacity may still be running")
        return 0
    except FileNotFoundError:
        # taskkill not on PATH (extremely rare on Windows)
        log.warning("taskkill.exe not found")
        return 0
    except Exception as e:
        log.warning("kill_audacity_processes failed: %s", e)
        return 0


# ── Config cleaner ───────────────────────────────────────────────────────────

_CFG_DISABLE_WELCOME = "ShowWelcomeDialog=0"
_CFG_CLEAR_RECENT = re.compile(r"^RecentFiles\d* *= *.*$", re.MULTILINE)
_CFG_CLEAR_RECENT_KEPT = re.compile(r"^(RecentFiles\d*) *=.*$", re.MULTILINE)
_CFG_DISABLE_AUTORECOVER = re.compile(r"^RecoveryMode *=.*$", re.MULTILINE)


def clean_audacity_config() -> bool:
    """
    Modify Audacity's INI-style config to suppress dialogs on next launch.

    Edits made:
      - Set  `ShowWelcomeDialog=0`  (disables the welcome screen)
      - Clear all `RecentFiles*=` lines (no project auto-load)
      - Set  `RecoveryMode=Off`     (skip the crash-recovery dialog)

    Returns True if the file was modified successfully.
    """
    appdata = get_audacity_appdata_dir()
    if not appdata or not appdata.exists():
        return False
    cfg_path = appdata / "audacity.cfg"
    if not cfg_path.exists():
        return False

    try:
        original = cfg_path.read_text(encoding="utf-8", errors="ignore")
        content = original

        # 1) Welcome dialog off
        if re.search(r"^ShowWelcomeDialog *=.*$", content, re.MULTILINE):
            content = re.sub(
                r"^ShowWelcomeDialog *=.*$",
                _CFG_DISABLE_WELCOME,
                content,
                flags=re.MULTILINE,
            )
        else:
            # Append into a [GUI] section if present, else at top
            if re.search(r"^\[GUI\]\s*$", content, re.MULTILINE):
                content = re.sub(
                    r"(\[GUI\]\s*\n)",
                    r"\1" + _CFG_DISABLE_WELCOME + "\n",
                    content,
                    count=1,
                )
            else:
                content = "[GUI]\n" + _CFG_DISABLE_WELCOME + "\n" + content

        # 2) Clear recent files (but keep the keys, just empty values)
        def _clear_recent(m: re.Match) -> str:
            key = m.group(1)
            return f"{key}="
        content = _CFG_CLEAR_RECENT_KEPT.sub(_clear_recent, content)

        # 3) Disable crash-recovery prompt
        if _CFG_DISABLE_AUTORECOVER.search(content):
            content = _CFG_DISABLE_AUTORECOVER.sub("RecoveryMode=Off", content)
        else:
            content += "\n[Directories]\nRecoveryMode=Off\n"

        if content != original:
            cfg_path.write_text(content, encoding="utf-8")
            log.info("Audacity config cleaned: welcome off, recent cleared, recovery off")
            return True
        return False
    except PermissionError as e:
        log.warning("Cannot modify audacity.cfg (permission denied): %s", e)
        return False
    except Exception as e:
        log.warning("clean_audacity_config failed: %s", e)
        return False


# ── AutoSave cleaner ─────────────────────────────────────────────────────────

def clean_audacity_autosave() -> int:
    """
    Delete files in %APPDATA%/audacity/AutoSave/.

    These are the projects that the "Crash Recovery" dialog tries to restore.
    Removing them before launch = no recovery dialog.

    Returns the number of items deleted.
    """
    appdata = get_audacity_appdata_dir()
    if not appdata or not appdata.exists():
        return 0
    autosave = appdata / "AutoSave"
    if not autosave.exists():
        return 0

    deleted = 0
    try:
        for f in autosave.iterdir():
            try:
                if f.is_file() or f.is_symlink():
                    f.unlink()
                    deleted += 1
                elif f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                    deleted += 1
            except (PermissionError, OSError) as e:
                log.debug("Could not delete %s: %s", f.name, e)
        if deleted:
            log.info("Cleaned %d AutoSave items (crash-recovery candidates)", deleted)
    except Exception as e:
        log.warning("clean_audacity_autosave failed: %s", e)
    return deleted


# ── One-shot reset (call BEFORE launching) ──────────────────────────────────

def reset_audacity_state(verbose: bool = True) -> bool:
    """
    Full pre-launch reset. Call this from `audacity_engine.apply_audacity_macro`
    before attempting to launch Audacity for the first time.

    1. Force-kill any stuck `audacity.exe` (uses /T to also kill children).
    2. Wait 1.5 s for OS to reap.
    3. Edit `audacity.cfg` to suppress dialogs.
    4. Wipe `AutoSave/` so Crash Recovery has nothing to recover.

    Returns True on success (even if some individual steps were skipped
    because the folders didn't exist).
    """
    if verbose:
        log.info("🔧 Resetting Audacity state for clean launch …")
    killed = kill_audacity_processes()
    if killed:
        if verbose:
            log.info("  killed %d stuck Audacity process(es)", killed)
    cfg_ok = clean_audacity_config()
    asd_count = clean_audacity_autosave()
    if verbose:
        log.info(
            "  config: %s, autosave cleared: %d item(s)",
            "cleaned" if cfg_ok else "skipped",
            asd_count,
        )
    return True


# ── Background dialog dismisser (call AFTER launching) ──────────────────────

# Keywords (lowercase) that mark a window as a blocking dialog we want to close.
_BLOCKING_KEYWORDS = (
    "audacity recovery",
    "recovery",
    "welcome to audacity",
    "welcome",
    "discard",
    "previous project",
    "unsaved project",
    "crash",
    "project not saved",
    "error:",
    "warning:",
    "do you want to save",
)

# Hard upper bound on how long the dismisser will keep running.
_DEFAULT_MAX_DURATION_SEC = 60.0
_TICK_INTERVAL_SEC = 1.0


def _is_likely_main_audacity_window(user32, hwnd: int) -> bool:
    """
    Heuristic: this is the main Audacity window we must NOT close.
    Identified by:
      - Title contains 'audacity'
      - Title does NOT contain any blocking keyword
      - Window is NOT a small modal (we look for "no owner" + decent size)
    """
    try:
        if not user32.IsWindowVisible(hwnd):
            return False
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0 or length > 256:
            return False
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title_l = buff.value.lower()
        if "audacity" not in title_l:
            return False
        if any(kw in title_l for kw in _BLOCKING_KEYWORDS):
            return False
        # It does not have an owner (main windows have no owner in Win32).
        owner = user32.GetWindow(hwnd, 4)  # GW_OWNER
        if owner:
            return False
        return True
    except Exception:
        return False


def _dismiss_one(user32, hwnd: int, title: str) -> None:
    """
    Try to close a dialog window.
    Strategy: send WM_CLOSE. If that doesn't work in a future iteration, we
    can fall back to clicking a 'No' / 'Discard' button via EnumChildWindows.
    """
    WM_CLOSE = 0x0010
    try:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:
        pass


def _enum_and_dismiss(user32, EnumWindowsProc) -> None:
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, lParam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0 or length > 256:
                return True
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
            title_l = title.lower()

            # Skip the main Audacity window
            if _is_likely_main_audacity_window(user32, hwnd):
                return True

            # Match blocking keywords
            if any(kw in title_l for kw in _BLOCKING_KEYWORDS):
                log.info("Dialog dismisser: closing '%s'", title)
                _dismiss_one(user32, hwnd, title)
                return True
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as e:
        log.debug("EnumWindows failed: %s", e)


def dismiss_audacity_dialogs(
    stop_event: threading.Event,
    max_duration_sec: float = _DEFAULT_MAX_DURATION_SEC,
) -> None:
    """
    Background-thread target. Walks top-level windows every second and
    closes any whose title contains a blocking keyword.

    Stops when `stop_event` is set or after `max_duration_sec`.
    """
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
    except Exception as e:
        log.warning("dismiss_audacity_dialogs: ctypes.windll.user32 unavailable: %s", e)
        return

    log.info("Audacity dialog dismisser started (max %.0f s)", max_duration_sec)
    start = time.time()
    while not stop_event.is_set():
        if (time.time() - start) > max_duration_sec:
            break
        _enum_and_dismiss(user32, None)
        # Sleep in 100 ms slices so stop_event reacts quickly
        for _ in range(int(_TICK_INTERVAL_SEC * 10)):
            if stop_event.is_set():
                break
            time.sleep(0.1)
    log.debug("Audacity dialog dismisser stopped")


class DialogDismisser:
    """
    Context manager wrapper for `dismiss_audacity_dialogs` so callers can do:

        with DialogDismisser(max_sec=60):
            # ... launch Audacity and connect ...
    """

    def __init__(self, max_sec: float = _DEFAULT_MAX_DURATION_SEC):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._max_sec = max_sec

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=dismiss_audacity_dialogs,
            args=(self._stop, self._max_sec),
            daemon=True,
            name="audacity-dialog-dismisser",
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            # Don't block forever if thread is stuck
            self._thread.join(timeout=2.0)
        return False
