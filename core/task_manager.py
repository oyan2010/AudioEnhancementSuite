"""
Queue Manager – assigns chunks to profiles based on remaining quota.
Task Manager  – orchestrates the full pipeline with state persistence.
"""

from __future__ import annotations

import threading
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Callable

from core.logger import get_logger

log = get_logger(__name__)


# ── Chunk states ──────────────────────────────────────────────────────────────

class ChunkStatus(Enum):
    PENDING     = auto()
    UPLOADING   = auto()
    WAITING     = auto()
    DOWNLOADING = auto()
    ENHANCED    = auto()
    POST_PROC   = auto()
    DONE        = auto()
    FAILED      = auto()
    SKIPPED     = auto()


@dataclass
class ChunkJob:
    index: int
    chunk_path: Path
    assigned_profile: Optional[str] = None
    status: ChunkStatus = ChunkStatus.PENDING
    enhanced_path: Optional[Path] = None
    processed_path: Optional[Path] = None
    error: str = ""
    duration_minutes: float = 0.0


# ── Pipeline Stages ───────────────────────────────────────────────────────────

class PipelineStage(Enum):
    IDLE        = "Idle"
    EXTRACTING  = "Extracting Audio"
    SPLITTING   = "Splitting Chunks"
    ENHANCING   = "Adobe Enhancement"
    POST_PROC   = "Post Processing"
    MERGING     = "Merging"
    EXPORTING   = "Exporting"
    DONE        = "Done"
    CANCELLED   = "Cancelled"
    FAILED      = "Failed"


# ── Task Manager ──────────────────────────────────────────────────────────────

class TaskManager:
    """
    Orchestrates the full pipeline in a background thread.
    - Uses ProfileRegistry for quota management
    - Saves state periodically for resume capability
    """

    # Post-processing mode constants
    POST_PROC_FULL       = "full"        # 5-pass chain (compressor+EQ+limit+norm)
    POST_PROC_GENTLE     = "gentle"      # normalize only
    POST_PROC_PASSTHROUGH = "passthrough" # copy as-is
    POST_PROC_AUDACITY    = "audacity"    # Audacity macro

    def __init__(
        self,
        config,
        source_file: Path,
        on_stage:           Callable[[PipelineStage], None]  = lambda s: None,
        on_job_update:      Callable[[ChunkJob], None]       = lambda j: None,
        on_progress:        Callable[[int, int], None]       = lambda c, t: None,
        on_done:            Callable[[bool, str], None]      = lambda ok, msg: None,
        resume_from:        Optional[Path]                   = None,
        post_processing_mode: str                            = "gentle",
        post_proc_threads:  int                              = 3,
        start_from_stage:   Optional[str]                    = None,
        output_name:        str                              = "",
    ):
        self.config                = config
        self.source_file           = source_file
        self.on_stage              = on_stage
        self.on_job_update         = on_job_update
        self.on_progress           = on_progress
        self.on_done               = on_done
        self.resume_from           = resume_from
        self.post_processing_mode  = post_processing_mode
        self.post_proc_threads     = max(1, post_proc_threads)
        self.start_from_stage      = start_from_stage
        self.output_name           = output_name

        self._cancel_event = threading.Event()
        self._pause_event  = threading.Event()
        self._pause_event.set()

        self._thread: Optional[threading.Thread] = None
        self.project  = None
        self.jobs: List[ChunkJob] = []
        self._state_mgr = None

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self):
        self._cancel_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(target=self._run_pipeline, daemon=True)
        self._thread.start()

    def pause(self):
        self._pause_event.clear()
        log.info("Pipeline paused.")

    def resume(self):
        self._pause_event.set()
        log.info("Pipeline resumed.")

    def cancel(self):
        self._cancel_event.set()
        self._pause_event.set()
        log.info("Pipeline cancel requested.")

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _check(self):
        self._pause_event.wait()
        if self._cancel_event.is_set():
            raise InterruptedError("Pipeline cancelled by user.")

    def _save_state(self, stage: PipelineStage):
        if not self._state_mgr or not self.project:
            return
        completed = [j.index for j in self.jobs if j.status == ChunkStatus.DONE]
        failed    = [j.index for j in self.jobs if j.status == ChunkStatus.FAILED]
        self._state_mgr.save(
            source_file=str(self.source_file),
            stage=stage.name,
            jobs=self.jobs,
            completed_chunks=completed,
            failed_chunks=failed,
        )

    def _find_existing_enhanced(self, chunk_index: int) -> Optional[Path]:
        """
        Scan downloads/ and enhanced/ directories for an already-downloaded
        enhanced file that belongs to chunk_index.

        Matches any file whose name contains the zero-padded chunk number,
        e.g. chunk_001, chunk_002 … regardless of profile subdirectory.

        This lets users manually copy enhanced files into the project and
        have the pipeline skip the Adobe upload step automatically.
        """
        if not self.project:
            return None

        tag = f"chunk_{chunk_index:03d}"   # e.g. "chunk_001"
        AUDIO_EXTS = {".wav", ".mp3", ".aac", ".flac", ".m4a", ".ogg", ".oga"}

        search_roots = [
            self.project.downloads_dir,   # downloads/<profile>/
            self.project.enhanced_dir,    # enhanced/
        ]

        for root in search_roots:
            if not root.exists():
                continue
            # Recurse into profile subdirs inside downloads/
            for f in root.rglob("*"):
                if (
                    f.is_file()
                    and tag in f.name
                    and f.suffix.lower() in AUDIO_EXTS
                ):
                    return f

        return None


    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _run_pipeline(self):
        try:
            from core.ffmpeg_engine import (
                locate_ffmpeg, extract_audio, smart_split,
                get_duration_seconds, apply_post_processing,
                apply_post_processing_gentle, apply_passthrough,
                merge_chunks, export_mp3,
            )
            from core.adobe_worker import AdobeWorker
            from core.project import Project
            from core.pipeline_state import PipelineState
            from core.profile_manager import get_registry
            from core.config import VIDEO_FORMATS

            ffmpeg   = locate_ffmpeg(self.config.ffmpeg_path)
            registry = get_registry()

            # ── Create or restore project ─────────────────────────────────────
            if self.resume_from:
                from core.project import Project as _Proj
                # Restore project paths from existing directory
                self.project = _Proj.__new__(_Proj)
                self.project.root          = Path(self.resume_from)
                self.project.input_dir     = self.project.root / "input"
                self.project.chunks_dir    = self.project.root / "chunks"
                self.project.downloads_dir = self.project.root / "downloads"
                self.project.enhanced_dir  = self.project.root / "enhanced"
                self.project.processed_dir = self.project.root / "processed"
                self.project.merged_dir    = self.project.root / "merged"
                self.project.output_dir    = self.project.root / "output"
                self.project.logs_dir      = self.project.root / "logs"
                self.project.temp_dir      = self.project.root / "temp"
                self.project.audio_master  = self.project.input_dir / "audio_master.wav"
                self.project.merged_enhanced = self.project.merged_dir / "merged_enhanced.wav"
                self.project.merged_wav    = self.project.merged_dir / "merged.wav"
                self.project.final_mp3     = self.project.output_dir / "final_output.mp3"
                self.project.project_id    = self.project.root.name
                # Ensure dirs exist
                for d in [self.project.input_dir, self.project.chunks_dir,
                          self.project.downloads_dir, self.project.enhanced_dir,
                          self.project.processed_dir, self.project.merged_dir,
                          self.project.output_dir, self.project.logs_dir,
                          self.project.temp_dir]:
                    d.mkdir(parents=True, exist_ok=True)
                # Add helper methods dynamically
                def chunk_path(i, s=self): return s.project.chunks_dir / f"chunk_{i:03d}.wav"
                def enhanced_path(i, s=self): return s.project.enhanced_dir / f"chunk_{i:03d}_enhanced.wav"
                def processed_path(i, s=self): return s.project.processed_dir / f"chunk_{i:03d}_processed.wav"
                def download_dir_for_profile(name, s=self):
                    p = s.project.downloads_dir / name; p.mkdir(parents=True, exist_ok=True); return p
                import types
                self.project.chunk_path               = types.MethodType(lambda self, i: self.chunks_dir / f"chunk_{i:03d}.wav", self.project)
                self.project.enhanced_path            = types.MethodType(lambda self, i: self.enhanced_dir / f"chunk_{i:03d}_enhanced.wav", self.project)
                self.project.processed_path           = types.MethodType(lambda self, i: self.processed_dir / f"chunk_{i:03d}_processed.wav", self.project)
                self.project.download_dir_for_profile = types.MethodType(lambda self, n: (self.downloads_dir / n).__class__(self.downloads_dir / n).mkdir(parents=True, exist_ok=True) or self.downloads_dir / n, self.project)
                log.info("Resuming project: %s", self.project.root)
            else:
                self.project = Project(self.config.projects_dir, self.source_file, self.output_name)

            self._state_mgr = PipelineState(self.project.root)

            # ── Load existing state (for resume) ──────────────────────────────
            prior_state = self._state_mgr.load() if self.resume_from else None
            skip_to_stage = self.start_from_stage
            prior_done_indices = set()
            prior_jobs_by_index = {}

            if prior_state:
                if not skip_to_stage:
                    skip_to_stage = prior_state.get("current_stage")
                prior_done_indices = set(prior_state.get("completed_chunks", []))
                for pj in prior_state.get("jobs", []):
                    prior_jobs_by_index[pj["index"]] = pj
                log.info(
                    "Resuming with stage '%s', %d chunks already done",
                    skip_to_stage, len(prior_done_indices),
                )
            elif skip_to_stage:
                log.info("Starting fresh with starting stage override: '%s'", skip_to_stage)

            # If we are starting from or before POST_PROC, reset DONE/POST_PROC status
            # and clear processed_path so they get re-processed!
            is_post_proc_or_earlier = skip_to_stage in ("EXTRACTING", "SPLITTING", "ENHANCING", "MERGING", "POST_PROC", None)
            skip_adobe = skip_to_stage in ("POST_PROC", "MERGING", "EXPORTING")
            skip_post_proc = skip_to_stage in ("EXPORTING",)

            if is_post_proc_or_earlier:
                # Force re-postprocessing by removing previous output files
                if hasattr(self.project, "merged_wav") and self.project.merged_wav.exists():
                    try:
                        self.project.merged_wav.unlink(missing_ok=True)
                    except Exception:
                        pass
                if hasattr(self.project, "final_mp3") and self.project.final_mp3.exists():
                    try:
                        self.project.final_mp3.unlink(missing_ok=True)
                    except Exception:
                        pass

            # ── Stage 1: Extract ──────────────────────────────────────────────
            need_extract = (
                not self.project.audio_master.exists()
                or skip_to_stage in (None, "EXTRACTING")
            )
            if need_extract:
                self._check()
                self.on_stage(PipelineStage.EXTRACTING)
                extract_audio(ffmpeg, self.source_file, self.project.audio_master)
                self._save_state(PipelineStage.EXTRACTING)
            else:
                log.info("Skipping extraction (already done)")

            # ── Stage 2: Smart Split ──────────────────────────────────────────
            existing_chunks = sorted(self.project.chunks_dir.glob("chunk_*.wav"))
            need_split = (
                len(existing_chunks) == 0
                or skip_to_stage in (None, "EXTRACTING", "SPLITTING")
            )

            if need_split:
                self._check()
                self.on_stage(PipelineStage.SPLITTING)
                chunk_paths = smart_split(
                    ffmpeg,
                    self.project.audio_master,
                    self.project.chunks_dir,
                    progress_cb=lambda d, t: self.on_progress(d, t),
                )
                self._save_state(PipelineStage.SPLITTING)
            else:
                chunk_paths = existing_chunks
                log.info("Skipping split (found %d existing chunks)", len(chunk_paths))

            # ── Build jobs ────────────────────────────────────────────────────
            self.jobs = []
            for i, cp in enumerate(sorted(chunk_paths)):
                dur_s = get_duration_seconds(ffmpeg, cp)
                dur_m = dur_s / 60.0

                job = ChunkJob(
                    index=i + 1,
                    chunk_path=cp,
                    duration_minutes=dur_m,
                )

                # Restore state from prior run (pipeline_state.json)
                if (i + 1) in prior_done_indices:
                    pj = prior_jobs_by_index.get(i + 1, {})
                    job.assigned_profile = pj.get("assigned_profile")
                    ep = pj.get("enhanced_path")
                    pp = pj.get("processed_path")
                    if ep and Path(ep).exists():
                        job.enhanced_path = Path(ep)
                    
                    if is_post_proc_or_earlier:
                        job.status = ChunkStatus.ENHANCED if job.enhanced_path else ChunkStatus.PENDING
                        job.processed_path = None
                    else:
                        job.status = ChunkStatus.DONE
                        if pp and Path(pp).exists():
                            job.processed_path = Path(pp)

                # ── Auto-detect enhanced files already in downloads/enhanced ──
                # Covers: manually placed files, or files moved by adobe_worker
                # after app was closed mid-run.
                if job.status == ChunkStatus.PENDING:
                    enhanced = self._find_existing_enhanced(job.index)
                    if enhanced:
                        log.info(
                            "Chunk %d: found existing enhanced file → %s",
                            job.index, enhanced.name,
                        )
                        job.enhanced_path = enhanced
                        job.status = ChunkStatus.ENHANCED

                self.jobs.append(job)
                self.on_job_update(job)

            total_jobs = len(self.jobs)
            self._card_chunks_count = total_jobs

            # ── Stage 3: Adobe Enhancement ────────────────────────────────────
            self._check()
            self.on_stage(PipelineStage.ENHANCING)

            completed = len([j for j in self.jobs if j.status == ChunkStatus.DONE])

            for job in self.jobs:
                self._check()

                # Already done from a previous run?
                if job.status == ChunkStatus.DONE and job.processed_path and job.processed_path.exists():
                    log.info("Chunk %d already fully done — skipping Adobe", job.index)
                    completed += 1
                    self.on_progress(completed, total_jobs)
                    continue

                # Should we skip Adobe upload?
                if skip_adobe:
                    if job.status != ChunkStatus.DONE:
                        if not job.enhanced_path or not job.enhanced_path.exists():
                            enhanced = self._find_existing_enhanced(job.index)
                            job.enhanced_path = enhanced if enhanced else job.chunk_path
                        job.status = ChunkStatus.ENHANCED
                        self.on_job_update(job)
                        log.info("Chunk %d skipped Adobe stage by override request", job.index)
                    completed += 1
                    self.on_progress(completed, total_jobs)
                    continue

                # Already enhanced (file exists in downloads or enhanced dir)?
                if job.status == ChunkStatus.ENHANCED and job.enhanced_path and job.enhanced_path.exists():
                    log.info("Chunk %d already enhanced — skipping Adobe upload", job.index)
                    completed += 1
                    self.on_progress(completed, total_jobs)
                    # Will be picked up by post-processing stage below
                    continue

                # Find a profile with quota
                profile = registry.find_available(job.duration_minutes)
                if profile is None:
                    job.status = ChunkStatus.SKIPPED
                    job.error  = "No profile has remaining quota"
                    self.on_job_update(job)
                    log.warning("Chunk %d skipped — no quota", job.index)
                    continue

                # Consume quota optimistically
                profile.consume_minutes(job.duration_minutes)
                job.assigned_profile = profile.name

                job.status = ChunkStatus.UPLOADING
                self.on_job_update(job)
                self._save_state(PipelineStage.ENHANCING)

                # Download dir
                dl_dir = self.project.downloads_dir / profile.name
                dl_dir.mkdir(parents=True, exist_ok=True)

                def _status_cb(msg, j=job):
                    msg_l = msg.lower()
                    if "wait" in msg_l or "scan" in msg_l or "still" in msg_l:
                        j.status = ChunkStatus.WAITING
                    elif "download" in msg_l:
                        j.status = ChunkStatus.DOWNLOADING
                    self.on_job_update(j)

                from core.adobe_worker import AdobeWorker
                worker = AdobeWorker(
                    profile_name = profile.name,
                    session_dir  = str(profile.session_dir),
                    chunk_path   = job.chunk_path,
                    download_dir = dl_dir,
                    status_cb    = _status_cb,
                    cancel_flag  = self.is_cancelled,
                )

                downloaded = worker.run()

                if downloaded is None:
                    job.status = ChunkStatus.FAILED
                    job.error  = "Adobe enhancement failed after retries"
                    profile.release_minutes(job.duration_minutes)
                    self.on_job_update(job)
                    self._save_state(PipelineStage.ENHANCING)
                    log.error("Chunk %d FAILED", job.index)
                    continue

                # Move to enhanced/
                enhanced_dst = self.project.enhanced_dir / f"chunk_{job.index:03d}_enhanced.wav"
                shutil.move(str(downloaded), str(enhanced_dst))
                job.enhanced_path = enhanced_dst
                job.status = ChunkStatus.ENHANCED
                self.on_job_update(job)

                completed += 1
                self.on_progress(completed, total_jobs)
                self._save_state(PipelineStage.ENHANCING)

            # ── Stage 4: Merge Enhanced Chunks ────────────────────────────────
            self._check()
            self.on_stage(PipelineStage.MERGING)
            self._save_state(PipelineStage.MERGING)

            # Sort and collect enhanced chunk paths
            sorted_enhanced = []
            for job in sorted(self.jobs, key=lambda j: j.index):
                if not job.enhanced_path or not job.enhanced_path.exists():
                    enhanced = self._find_existing_enhanced(job.index)
                    job.enhanced_path = enhanced if enhanced else job.chunk_path
                    self.on_job_update(job)
                sorted_enhanced.append(job.enhanced_path)

            need_merge = (
                not self.project.merged_enhanced.exists()
                or skip_to_stage in (None, "EXTRACTING", "SPLITTING", "ENHANCING", "MERGING")
            )

            if need_merge:
                log.info("Merging %d enhanced chunks...", len(sorted_enhanced))
                merge_chunks(ffmpeg, sorted_enhanced, self.project.merged_enhanced)
            else:
                log.info("Skipping merging (already merged)")

            # ── Stage 5: Post Processing (Applied to Merged File) ──────────────
            self._check()
            self.on_stage(PipelineStage.POST_PROC)
            self._save_state(PipelineStage.POST_PROC)

            mode = self.post_processing_mode
            if mode == TaskManager.POST_PROC_FULL:
                proc_fn = lambda s, d: apply_post_processing(ffmpeg, s, d)
                mode_label = "Full (5-pass)"
            elif mode == TaskManager.POST_PROC_GENTLE:
                proc_fn = lambda s, d: apply_post_processing_gentle(ffmpeg, s, d)
                mode_label = "Gentle (normalize only)"
            elif mode == TaskManager.POST_PROC_AUDACITY:
                from core.audacity_engine import apply_audacity_macro
                proc_fn = lambda s, d: apply_audacity_macro(s, d, self.config)
                mode_label = "Audacity Macro"
            else:
                proc_fn = lambda s, d: apply_passthrough(ffmpeg, s, d)
                mode_label = "Passthrough (copy)"

            need_post_proc = (
                not self.project.merged_wav.exists()
                or skip_to_stage in (None, "EXTRACTING", "SPLITTING", "ENHANCING", "MERGING", "POST_PROC")
            )

            if need_post_proc:
                log.info("Applying post-processing to merged enhanced audio [%s]...", mode_label)
                # Update status of all jobs to POST_PROC for visual feedback
                for job in self.jobs:
                    job.status = ChunkStatus.POST_PROC
                    self.on_job_update(job)
                
                try:
                    proc_fn(self.project.merged_enhanced, self.project.merged_wav)
                    log.info("Merged post-processing done.")
                except Exception as e:
                    log.error("Merged post-processing failed: %s. Using fallback...", e)
                    shutil.copy2(str(self.project.merged_enhanced), str(self.project.merged_wav))
                
                # Update status of all jobs to DONE
                for job in self.jobs:
                    job.status = ChunkStatus.DONE
                    job.processed_path = self.project.merged_wav
                    self.on_job_update(job)
                self._save_state(PipelineStage.POST_PROC)
            else:
                log.info("Skipping post-processing (already processed)")
                for job in self.jobs:
                    job.status = ChunkStatus.DONE
                    job.processed_path = self.project.merged_wav
                    self.on_job_update(job)

            # ── Stage 6: Export ───────────────────────────────────────────────
            self._check()
            self.on_stage(PipelineStage.EXPORTING)
            self._save_state(PipelineStage.EXPORTING)
            
            log.info("Exporting post-processed merged audio to MP3...")
            export_mp3(ffmpeg, self.project.merged_wav, self.project.final_mp3)

            # ── Done ──────────────────────────────────────────────────────────
            self._save_state(PipelineStage.DONE)
            self._state_mgr.clear()
            self.on_stage(PipelineStage.DONE)
            self.on_done(True, str(self.project.final_mp3))

        except InterruptedError:
            if self._state_mgr:
                self._save_state(PipelineStage.CANCELLED)
            self.on_stage(PipelineStage.CANCELLED)
            self.on_done(False, "Cancelled by user. You can resume this project later.")

        except Exception as e:
            log.exception("Pipeline error: %s", e)
            if self._state_mgr:
                self._save_state(PipelineStage.FAILED)
            self.on_stage(PipelineStage.FAILED)
            self.on_done(False, str(e))

