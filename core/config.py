"""
Application configuration and constants.
"""

import json
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_NAME = "Audio Enhancement Suite"
APP_VERSION = "1.0.0"

PYTHON_EXE = r"A:\Python-MainEXE\python.exe"

# App root = folder that contains main.py  (core/ is one level below)
APP_DIR = Path(__file__).resolve().parent.parent

# All app data stays inside APP_DIR — nothing written to C: drive
CONFIG_DIR  = APP_DIR / ".config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.json"

# Default folder for project workspaces
DEFAULT_PROJECTS_DIR = APP_DIR / "projects"

# ── Supported Formats ─────────────────────────────────────────────────────────
VIDEO_FORMATS = [".mp4", ".mov", ".m4v"]
AUDIO_FORMATS = [".mp3", ".wav", ".aac", ".flac", ".m4a"]
ALL_FORMATS = VIDEO_FORMATS + AUDIO_FORMATS

# ── Processing Constants ──────────────────────────────────────────────────────
MAX_CHUNK_MINUTES = 29          # Adobe Podcast limit
MAX_CHUNK_SECONDS = MAX_CHUNK_MINUTES * 60
INITIAL_WAIT_SECONDS = 240      # 4 min initial wait after upload
SCAN_INTERVAL_SECONDS = 5       # Poll every 5 s
TIMEOUT_SECONDS = 90 * 60       # 90-minute overall timeout
ADOBE_DAILY_QUOTA_MINUTES = 60  # free tier
UPLOAD_RETRY_COUNT = 3
FFMPEG_RETRY_COUNT = 1

# ── FFmpeg Post-Processing Defaults ──────────────────────────────────────────
COMPRESSOR_ATTACK   = 0.2
COMPRESSOR_RELEASE  = 1.0
COMPRESSOR_THRESHOLD = -41.0   # dB
COMPRESSOR_RATIO    = 2.0

EQ_LOW_FREQ_START   = 100      # Hz
EQ_LOW_FREQ_END     = 500      # Hz
EQ_LOW_GAIN         = 9.0      # dB

EQ_HIGH_FREQ_START  = 4000     # Hz
EQ_HIGH_FREQ_END    = 5000     # Hz
EQ_HIGH_GAIN        = 9.0      # dB

PITCH_SHIFT_PERCENT = -1.0     # %
LIMITER_THRESHOLD   = -2.1     # dB
NORMALIZE_PEAK      = -2.0     # dB

# ── Export Settings ───────────────────────────────────────────────────────────
EXPORT_BITRATE  = "192k"
EXPORT_CHANNELS = 2
EXPORT_RATE     = 48000

# ── Adobe URL ────────────────────────────────────────────────────────────────
ADOBE_ENHANCE_URL = "https://podcast.adobe.com/en/enhance"

# Upload selectors (used via JavaScript document.querySelector — NOT visibility-checked)
# Actual HTML: <input type="file" hidden="" id="enhance-file-upload"
#               accept=".mp3,.wav,.aac,.flac,.oga,.ogg,.m4a,.mp4,.m4v,.mov,.3gpp,.3gp,.webm"
#               multiple="" aria-label="Choose files">
# The input is a light-DOM child of <sp-button data-testid="choose-files-button">
ADOBE_UPLOAD_SELECTORS = [
    "input#enhance-file-upload",                           # primary — id
    "#enhance-file-upload",                                # id shorthand
    "input[aria-label='Choose files']",                    # aria-label
    "sp-button[data-testid='choose-files-button'] input",  # parent testid + input
    "input[type='file'][accept*='.mp3']",                  # accept attribute
    "input[type='file'][multiple]",                        # multiple attribute
    "input[type='file']",                                  # last-resort: any file input
]

# Download selectors (used via SeleniumBase find_element + visibility check is OK here)
# Actual HTML: <button aria-label="Download" data-testid="download-button" class="sc-hKKfEi iDPzfK">
ADOBE_DOWNLOAD_SELECTORS = [
    'button[data-testid="download-button"]',               # primary — testid
    'button[aria-label="Download"]',                       # aria-label
    'button.sc-hKKfEi',                                    # class
    '.sc-hKKfEi[role="button"]',                           # role fallback
]

# Backward-compat aliases
ADOBE_FILE_INPUT_SELECTOR = ADOBE_UPLOAD_SELECTORS[0]
ADOBE_DOWNLOAD_SELECTOR   = ADOBE_DOWNLOAD_SELECTORS[0]


# ── Data Classes ──────────────────────────────────────────────────────────────
@dataclass
class ChromeProfile:
    name: str
    user_data_dir: str
    profile_dir: str = "Default"
    download_dir: str = ""
    daily_quota_minutes: int = ADOBE_DAILY_QUOTA_MINUTES
    used_minutes: float = 0.0
    last_reset_date: str = ""

    @property
    def remaining_minutes(self) -> float:
        return max(0.0, self.daily_quota_minutes - self.used_minutes)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining_minutes <= 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ChromeProfile":
        return ChromeProfile(**d)


@dataclass
class AppConfig:
    profiles: List[ChromeProfile] = field(default_factory=list)
    ffmpeg_path: str = "ffmpeg"
    projects_dir: str = str(DEFAULT_PROJECTS_DIR)
    audacity_path: str = ""
    use_audacity_mode: bool = False
    theme: str = "dark"
    post_processing_mode: str = "gentle"

    def save(self):
        data = {
            "profiles": [p.to_dict() for p in self.profiles],
            "ffmpeg_path": self.ffmpeg_path,
            "projects_dir": self.projects_dir,
            "audacity_path": self.audacity_path,
            "use_audacity_mode": self.use_audacity_mode,
            "theme": self.theme,
            "post_processing_mode": self.post_processing_mode,
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))

    @staticmethod
    def load() -> "AppConfig":
        if not CONFIG_FILE.exists():
            return AppConfig()
        try:
            data = json.loads(CONFIG_FILE.read_text())
            profiles = [ChromeProfile.from_dict(p) for p in data.get("profiles", [])]
            cfg = AppConfig(
                profiles=profiles,
                ffmpeg_path=data.get("ffmpeg_path", "ffmpeg"),
                projects_dir=data.get("projects_dir", str(DEFAULT_PROJECTS_DIR)),
                audacity_path=data.get("audacity_path", ""),
                use_audacity_mode=data.get("use_audacity_mode", False),
                theme=data.get("theme", "dark"),
                post_processing_mode=data.get("post_processing_mode", "gentle"),
            )
            return cfg
        except Exception:
            return AppConfig()
