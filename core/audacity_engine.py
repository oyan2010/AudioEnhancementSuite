"""
Audacity scripting connector via mod-script-pipe.
Applies user's voice_deep_cinematiceffect macro using Audacity.
"""

import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from typing import List, Optional
from core.logger import get_logger
from core.audacity_cleanup import (
    reset_audacity_state,
    DialogDismisser,
)

log = get_logger(__name__)

# Named pipe paths
if sys.platform == 'win32':
    TONAME = r'\\.\pipe\ToSrvPipe'
    FROMNAME = r'\\.\pipe\FromSrvPipe'
    EOL = '\r\n\0'
else:
    TONAME = f'/tmp/audacity_script_pipe.to.{os.getuid()}'
    FROMNAME = f'/tmp/audacity_script_pipe.from.{os.getuid()}'
    EOL = '\n'

# Global lock to serialize Audacity command execution
audacity_lock = threading.Lock()

DEFAULT_AUDACITY_COMMANDS = [
    'Compressor:AttackTime="0.2" NoiseFloor="-35" Normalize="1" Ratio="2" ReleaseTime="1" Threshold="-41" UsePeak="0"',
    'FilterCurve:f0="100" f1="500" FilterLength="8191" InterpolateLin="0" InterpolationMethod="B-spline" v0="9" v1="0"',
    'FilterCurve:f0="4000" f1="5000" FilterLength="8191" InterpolateLin="0" InterpolationMethod="B-spline" v0="0" v1="9"',
    'ChangePitch:Percentage="-1" SBSMS="0"',
    'Limiter:gain-L="0" gain-R="0" hold="10" makeup="No" thresh="-2.1" type="HardLimit"',
    'Normalize:ApplyGain="1" PeakLevel="-2" RemoveDcOffset="1" StereoIndependent="0"'
]

class AudacitySession:
    """Manages a single open connection to Audacity's Named Pipes."""

    def __init__(self):
        self.to_file = None
        self.from_file = None

    def connect(self):
        try:
            self.to_file = open(TONAME, 'w')
            self.from_file = open(FROMNAME, 'r')
        except Exception as e:
            self.close()
            raise RuntimeError(
                "Failed to open Audacity Named Pipes. Verify Audacity is running "
                "and mod-script-pipe is Enabled in Preferences -> Modules."
            ) from e

    def send(self, command: str) -> str:
        log.debug("Audacity Send: %s", command)
        self.to_file.write(command + EOL)
        self.to_file.flush()

        response = ''
        def _read_target():
            nonlocal response
            try:
                while True:
                    line = self.from_file.readline()
                    if not line:
                        break
                    response += line
                    if line.startswith("BatchCommand finished:"):
                        trailing = self.from_file.readline()
                        response += trailing
                        break
            except Exception:
                pass

        t = threading.Thread(target=_read_target, daemon=True)
        t.start()
        t.join(timeout=120.0)

        if t.is_alive():
            raise TimeoutError(
                "Audacity did not respond within 120 seconds. "
                "This usually occurs if Audacity is showing a modal dialog (like 'Crash Recovery' or 'Welcome'). "
                "Please check Audacity, close any open dialogs, and resume the project again."
            )

        log.debug("Audacity Recv: %s", response.strip())
        return response

    def close(self):
        if self.to_file:
            try:
                self.to_file.close()
            except Exception:
                pass
            self.to_file = None
        if self.from_file:
            try:
                self.from_file.close()
            except Exception:
                pass
            self.from_file = None


def parse_macro_file(macro_path: Path) -> List[str]:
    """Parse commands from Audacity macro text file."""
    commands = []
    if not macro_path.exists():
        return []
    try:
        with open(macro_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                commands.append(line)
    except Exception as e:
        log.warning("Failed to read macro file %s: %s", macro_path, e)
    return commands


def apply_audacity_macro(src: Path, dst: Path, config) -> Path:
    """
    Import file into Audacity, apply the voice_deep_cinematiceffect macro, and export.
    Uses audacity_lock to serialize requests across threads.

    Pre-launch behavior (new):
      - If Audacity is NOT running: run a one-shot state reset
        (kill stuck processes, clear AutoSave, edit cfg to disable
        Welcome / Recovery / Recent-Files dialogs), then launch.
      - In parallel, a background thread (DialogDismisser) walks top-level
        Windows every second for up to 60 s and auto-closes any
        Audacity modal dialog that still appears (e.g. if the user has
        a custom startup screen).
    """
    with audacity_lock:
        # ── Pre-launch state reset (only if nothing is running) ───────────
        if not _is_audacity_running():
            log.info("Audacity not running — resetting state before launch")
            reset_audacity_state(verbose=True)
        else:
            log.info("Audacity already running — skipping state reset")

        # ── Launch + dialog dismisser ──────────────────────────────────────
        session = AudacitySession()
        connected = False
        last_err = None

        with DialogDismisser(max_sec=60):
            for attempt in range(30):
                try:
                    session.connect()
                    connected = True
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        # First failure: try launching Audacity
                        if config.audacity_path and Path(config.audacity_path).exists():
                            log.info("Launching Audacity from: %s", config.audacity_path)
                            subprocess.Popen([config.audacity_path])
                        else:
                            log.warning(
                                "Audacity is not running and no executable path is configured. "
                                "Please start Audacity manually."
                            )
                    elif attempt == 10:
                        # After 10 s of trying, the launch may have failed
                        log.warning("Still not connected after 10 s — Audacity may be blocked by a dialog")
                    time.sleep(1)

        if not connected:
            raise RuntimeError(
                "Audacity named pipes not found after 30 s. Possible causes:\n"
                "  - Audacity did not start (check the path in Settings)\n"
                "  - A modal dialog is still blocking (e.g. Crash Recovery, Welcome)\n"
                "  - mod-script-pipe is not Enabled (Edit → Preferences → Modules)\n"
                "Try manually opening Audacity once, dismissing any dialogs, then resuming."
            ) from last_err

        # Load commands from user path, or fall back to default list
        macro_file = Path(r"C:\Users\Muhammad Ayyan\AppData\Roaming\audacity\Macros\voice_deep_cinematiceffect.txt")
        commands = parse_macro_file(macro_file)
        if not commands:
            log.info("Using default voice_deep_cinematiceffect macro settings")
            commands = DEFAULT_AUDACITY_COMMANDS
        else:
            log.info("Loaded macro settings from %s", macro_file)

        try:
            # 1. Clean workspace to start fresh
            session.send("Select: All=1")
            session.send("RemoveTracks:")

            # 2. Import file
            src_posix = src.resolve().as_posix()
            res = session.send(f'Import2: Filename="{src_posix}"')
            if "failed" in res.lower():
                raise RuntimeError(f"Audacity failed to import source file: {res.strip()}")

            # 3. Ensure track is selected
            session.send("Select: All=1")

            # 4. Apply macro commands step by step
            for cmd in commands:
                res = session.send(cmd)
                if "failed" in res.lower():
                    log.warning("Audacity command warning/failure: %s -> %s", cmd, res.strip())

            # 5. Export processed file
            dst_posix = dst.resolve().as_posix()
            res = session.send(f'Export2: Filename="{dst_posix}" NumChannels=2')
            if "failed" in res.lower():
                raise RuntimeError(f"Audacity failed to export track: {res.strip()}")

            # 6. Clean workspace
            session.send("Select: All=1")
            session.send("RemoveTracks:")

            log.info("Audacity macro processing complete -> %s", dst.name)
            return dst

        except Exception as e:
            # Attempt to clean up even on failure
            try:
                session.send("Select: All=1")
                session.send("RemoveTracks:")
            except Exception:
                pass
            raise e
        finally:
            session.close()


def _is_audacity_running() -> bool:
    """Cheap check: is any `audacity.exe` currently running?"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq audacity.exe", "/NH"],
            capture_output=True, text=True, timeout=4,
        )
        return "audacity.exe" in out.stdout.lower()
    except Exception:
        return False
