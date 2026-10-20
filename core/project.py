"""
Project workspace management.
Creates and validates the folder structure for a processing run.
"""

import uuid
from pathlib import Path
from datetime import datetime
from core.logger import get_logger

log = get_logger(__name__)


class Project:
    """Encapsulates all paths for a single processing project."""

    SUBDIRS = [
        "input", "chunks", "downloads", "enhanced",
        "processed", "merged", "output", "logs", "temp",
    ]

    def __init__(self, base_dir: str, source_file: Path, output_name: str = ""):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = source_file.stem[:40]
        self.project_id = f"{stem}_{timestamp}"
        self.source_file = source_file
        self.output_name = (output_name.strip() if output_name else source_file.stem) or source_file.stem
        self.output_name = self._sanitize_filename(self.output_name)
        self.root = Path(base_dir) / self.project_id
        self._create_structure()

        # Derived paths
        self.input_dir    = self.root / "input"
        self.chunks_dir   = self.root / "chunks"
        self.downloads_dir = self.root / "downloads"
        self.enhanced_dir = self.root / "enhanced"
        self.processed_dir = self.root / "processed"
        self.merged_dir   = self.root / "merged"
        self.output_dir   = self.root / "output"
        self.logs_dir     = self.root / "logs"
        self.temp_dir     = self.root / "temp"

        # Files
        self.audio_master = self.input_dir / "audio_master.wav"
        self.merged_enhanced = self.merged_dir / "merged_enhanced.wav"
        self.merged_wav   = self.merged_dir / "merged.wav"
        self.final_mp3    = self.output_dir / f"{self.output_name}.mp3"

        log.info("Project workspace created: %s", self.root)

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Strip characters not allowed in Windows filenames."""
        import re
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('.')
        return cleaned[:120] or "output"

    def _create_structure(self):
        for sub in self.SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def chunk_path(self, index: int) -> Path:
        return self.chunks_dir / f"chunk_{index:03d}.wav"

    def enhanced_path(self, index: int) -> Path:
        return self.enhanced_dir / f"chunk_{index:03d}_enhanced.wav"

    def processed_path(self, index: int) -> Path:
        return self.processed_dir / f"chunk_{index:03d}_processed.wav"

    def download_dir_for_profile(self, profile_name: str) -> Path:
        p = self.downloads_dir / profile_name
        p.mkdir(parents=True, exist_ok=True)
        return p
