"""
Pipeline state persistence - save/resume chunk processing state.

State file: <project_root>/pipeline_state.json
"""

import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

from core.logger import get_logger

log = get_logger(__name__)


class PipelineState:
    """
    Saves and restores the pipeline state so processing can be resumed
    after an interruption.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.state_file = project_root / "pipeline_state.json"

    def save(
        self,
        source_file: str,
        stage: str,
        jobs: list,
        completed_chunks: List[int],
        failed_chunks: List[int],
    ):
        state: Dict[str, Any] = {
            "version": 1,
            "saved_at": datetime.now().isoformat(),
            "source_file": source_file,
            "current_stage": stage,
            "completed_chunks": completed_chunks,
            "failed_chunks": failed_chunks,
            "jobs": [
                {
                    "index": j.index,
                    "chunk_path": str(j.chunk_path),
                    "assigned_profile": j.assigned_profile,
                    "status": j.status.name,
                    "enhanced_path": str(j.enhanced_path) if j.enhanced_path else None,
                    "processed_path": str(j.processed_path) if j.processed_path else None,
                    "error": j.error,
                    "duration_minutes": j.duration_minutes,
                }
                for j in jobs
            ],
        }
        self.state_file.write_text(json.dumps(state, indent=2))
        log.debug("Pipeline state saved → %s", self.state_file)

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.state_file.exists():
            return None
        try:
            return json.loads(self.state_file.read_text())
        except Exception as e:
            log.warning("Failed to load pipeline state: %s", e)
            return None

    def exists(self) -> bool:
        return self.state_file.exists()

    def clear(self):
        if self.state_file.exists():
            self.state_file.unlink()

    @staticmethod
    def find_resumable_projects(projects_dir: str) -> List[Dict[str, Any]]:
        """
        Scan projects directory for any incomplete pipeline_state.json files.
        Returns list of resumable project info dicts.
        """
        resumable = []
        base = Path(projects_dir)
        if not base.exists():
            return resumable

        for proj_dir in sorted(base.iterdir(), reverse=True):
            if not proj_dir.is_dir():
                continue
            state_file = proj_dir / "pipeline_state.json"
            if not state_file.exists():
                # Recoverable project fallback
                chunks_dir = proj_dir / "chunks"
                if chunks_dir.exists() and any(chunks_dir.glob("chunk_*.wav")):
                    wav_chunks = list(chunks_dir.glob("chunk_*.wav"))
                    mtime = datetime.fromtimestamp(proj_dir.stat().st_mtime).isoformat()
                    resumable.append({
                        "project_dir": str(proj_dir),
                        "project_name": f"{proj_dir.name} (Recovered)",
                        "source_file": str(proj_dir / "input" / "audio_master.wav"),
                        "stage": "POST_PROC",
                        "saved_at": mtime,
                        "completed": 0,
                        "total": len(wav_chunks),
                    })
                continue
            try:
                state = json.loads(state_file.read_text())
                stage = state.get("current_stage", "")
                # Only show truly incomplete projects
                if stage not in ("DONE", ""):
                    resumable.append({
                        "project_dir": str(proj_dir),
                        "project_name": proj_dir.name,
                        "source_file": state.get("source_file", "?"),
                        "stage": stage,
                        "saved_at": state.get("saved_at", "?"),
                        "completed": len(state.get("completed_chunks", [])),
                        "total": len(state.get("jobs", [])),
                    })
            except Exception:
                continue
        return resumable
