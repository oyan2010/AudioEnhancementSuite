"""
Profile Manager - manages SeleniumBase Chrome profiles stored locally.

Each profile lives at:
  AudioEnhancementSuite/profiles/<profile_name>/
    ├── session/          ← Chrome user data dir (SeleniumBase)
    ├── quota.json        ← persistent daily quota
    └── meta.json         ← profile metadata
"""

import json
import threading
from datetime import date
from pathlib import Path
from typing import List, Optional

from core.logger import get_logger

log = get_logger(__name__)

# Base profiles directory (next to the app)
APP_DIR = Path(__file__).parent.parent
PROFILES_BASE = APP_DIR / "profiles"
PROFILES_BASE.mkdir(parents=True, exist_ok=True)

ADOBE_DAILY_QUOTA = 60.0  # minutes


class Profile:
    """
    Represents one SeleniumBase Chrome profile with persistent quota.
    """

    def __init__(self, name: str):
        self.name = name
        self.root = PROFILES_BASE / name
        self.session_dir = self.root / "session"   # Chrome user_data_dir
        self.quota_file = self.root / "quota.json"
        self.meta_file = self.root / "meta.json"
        self._lock = threading.Lock()

        # Create dirs
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Load persistent data
        self._quota = self._load_quota()
        self._meta = self._load_meta()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def used_minutes(self) -> float:
        return self._quota.get("used_minutes", 0.0)

    @property
    def remaining_minutes(self) -> float:
        return max(0.0, ADOBE_DAILY_QUOTA - self.used_minutes)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_minutes <= 0.0

    @property
    def is_logged_in(self) -> bool:
        """Check if session cookies exist (basic heuristic)."""
        # If session dir has subdirs/files it means Chrome saved something
        return any(self.session_dir.iterdir()) if self.session_dir.exists() else False

    @property
    def status_text(self) -> str:
        if not self.is_logged_in:
            return "⚠ Not logged in"
        if self.is_exhausted:
            return "✕ Quota exhausted"
        return f"✓ {self.remaining_minutes:.1f} min left"

    # ── Quota management ──────────────────────────────────────────────────────

    def consume_minutes(self, minutes: float):
        with self._lock:
            self._reset_if_new_day()
            self._quota["used_minutes"] = self.used_minutes + minutes
            self._save_quota()

    def release_minutes(self, minutes: float):
        with self._lock:
            self._quota["used_minutes"] = max(0.0, self.used_minutes - minutes)
            self._save_quota()

    def reset_quota(self):
        with self._lock:
            self._quota = {"date": str(date.today()), "used_minutes": 0.0}
            self._save_quota()
        log.info("Quota reset for profile '%s'", self.name)

    def _reset_if_new_day(self):
        today = str(date.today())
        if self._quota.get("date") != today:
            self._quota = {"date": today, "used_minutes": 0.0}
            self._save_quota()
            log.info("New day — quota reset for '%s'", self.name)

    def refresh_quota(self):
        """Re-read quota from disk (in case another process updated it)."""
        with self._lock:
            self._quota = self._load_quota()
            self._reset_if_new_day()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_quota(self) -> dict:
        if self.quota_file.exists():
            try:
                data = json.loads(self.quota_file.read_text())
                # Auto-reset if new day
                if data.get("date") != str(date.today()):
                    data = {"date": str(date.today()), "used_minutes": 0.0}
                return data
            except Exception:
                pass
        return {"date": str(date.today()), "used_minutes": 0.0}

    def _save_quota(self):
        self.quota_file.write_text(json.dumps(self._quota, indent=2))

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                return json.loads(self.meta_file.read_text())
            except Exception:
                pass
        return {"name": self.name, "created": str(date.today())}

    def save_meta(self, **kwargs):
        self._meta.update(kwargs)
        self.meta_file.write_text(json.dumps(self._meta, indent=2))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "session_dir": str(self.session_dir),
            "used_minutes": self.used_minutes,
            "remaining_minutes": self.remaining_minutes,
            "is_logged_in": self.is_logged_in,
            "is_exhausted": self.is_exhausted,
        }


class ProfileRegistry:
    """
    Discovers and manages all profiles in the profiles/ directory.
    Singleton-style access via get_registry().
    """

    def __init__(self):
        self._profiles: dict[str, Profile] = {}
        self._lock = threading.Lock()
        self._load_all()

    def _load_all(self):
        """Load all existing profile folders."""
        for d in sorted(PROFILES_BASE.iterdir()):
            if d.is_dir():
                p = Profile(d.name)
                self._profiles[d.name] = p
                log.info("Loaded profile: %s (%.1f min remaining)", d.name, p.remaining_minutes)

    def get_all(self) -> List[Profile]:
        with self._lock:
            return list(self._profiles.values())

    def get(self, name: str) -> Optional[Profile]:
        with self._lock:
            return self._profiles.get(name)

    def create(self, name: str) -> Profile:
        """Create a new profile folder and register it."""
        with self._lock:
            if name in self._profiles:
                return self._profiles[name]
            p = Profile(name)
            self._profiles[name] = p
            p.save_meta(name=name)
            log.info("Created new profile: %s", name)
            return p

    def delete(self, name: str):
        import shutil
        with self._lock:
            if name in self._profiles:
                root = self._profiles[name].root
                del self._profiles[name]
                shutil.rmtree(root, ignore_errors=True)
                log.info("Deleted profile: %s", name)

    def find_available(self, duration_minutes: float) -> Optional[Profile]:
        """Return first profile with enough quota."""
        with self._lock:
            for p in self._profiles.values():
                p.refresh_quota()
                if p.is_logged_in and p.remaining_minutes >= duration_minutes:
                    return p
        return None

    def refresh_all(self):
        for p in self._profiles.values():
            p.refresh_quota()


# Global registry instance
_registry: Optional[ProfileRegistry] = None


def get_registry() -> ProfileRegistry:
    global _registry
    if _registry is None:
        _registry = ProfileRegistry()
    return _registry
