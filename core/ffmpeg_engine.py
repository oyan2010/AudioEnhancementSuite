"""
FFmpeg wrapper: audio extraction, chunk splitting, post-processing, merge, export.

Post-processing modes:
  apply_post_processing        – full chain (compressor + EQ + pitch + limiter + normalize)
  apply_post_processing_gentle – gentle mode: normalize only, no EQ/pitch/compression
"""

import subprocess
import shutil
import math
import re
from pathlib import Path
from typing import List, Optional, Callable

from core.config import (
    COMPRESSOR_ATTACK, COMPRESSOR_RELEASE, COMPRESSOR_THRESHOLD, COMPRESSOR_RATIO,
    EQ_LOW_FREQ_START, EQ_LOW_FREQ_END, EQ_LOW_GAIN,
    EQ_HIGH_FREQ_START, EQ_HIGH_FREQ_END, EQ_HIGH_GAIN,
    PITCH_SHIFT_PERCENT, LIMITER_THRESHOLD, NORMALIZE_PEAK,
    EXPORT_BITRATE, EXPORT_CHANNELS, EXPORT_RATE,
    MAX_CHUNK_SECONDS, FFMPEG_RETRY_COUNT,
)
from core.logger import get_logger

log = get_logger(__name__)


def _run(cmd: List[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    log.debug("FFmpeg cmd: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (rc={result.returncode}):\n"
            + result.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    return result


def download_ffmpeg(target_dir: Path) -> Path:
    import urllib.request
    import zipfile
    import tempfile
    
    url = "https://github.com/GyanD/codexffmpeg/releases/download/6.0/ffmpeg-6.0-essentials_build.zip"
    
    log.info("FFmpeg not found. Downloading static build from: %s (approx. 35MB)...", url)
    target_dir.mkdir(parents=True, exist_ok=True)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "ffmpeg.zip"
        
        last_pct = -1.0
        def _reporthook(blocknum, blocksize, totalsize):
            nonlocal last_pct
            readsofar = blocknum * blocksize
            if totalsize > 0:
                percent = round(readsofar * 1e2 / totalsize, 1)
                if percent >= last_pct + 5.0 or readsofar >= totalsize:
                    log.info("Downloading FFmpeg: %.1f%% of %.1f MB", percent, totalsize / (1024*1024))
                    last_pct = percent
            
        urllib.request.urlretrieve(url, zip_path, reporthook=_reporthook)
        log.info("Download completed. Extracting files...")
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(tmpdir)
            
        extracted_bin = None
        for p in Path(tmpdir).rglob("ffmpeg.exe"):
            extracted_bin = p.parent
            break
            
        if not extracted_bin:
            raise FileNotFoundError("Could not find ffmpeg.exe inside the downloaded archive.")
            
        bin_dir = target_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        
        for f in ["ffmpeg.exe", "ffprobe.exe"]:
            src_f = extracted_bin / f
            dst_f = bin_dir / f
            if src_f.exists():
                shutil.copy2(src_f, dst_f)
                log.info("Installed %s to %s", f, dst_f)
                
        return bin_dir / "ffmpeg.exe"


def locate_ffmpeg(configured_path: str = "ffmpeg") -> str:
    """Return usable ffmpeg executable path. Downloads automatically if missing."""
    if configured_path and Path(configured_path).exists():
        return configured_path
    if shutil.which(configured_path):
        return configured_path
        
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\tools\ffmpeg\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
            
    # Check if we already downloaded it to our app directory
    from core.config import APP_DIR
    local_ffmpeg = APP_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"
    if local_ffmpeg.exists():
        return str(local_ffmpeg)
        
    # Trigger auto-installer
    try:
        ffmpeg_bin = download_ffmpeg(APP_DIR / "ffmpeg")
        return str(ffmpeg_bin)
    except Exception as e:
        raise FileNotFoundError(
            f"FFmpeg not found. Automatic download failed: {e}. "
            "Please install FFmpeg manually and add it to your system PATH."
        ) from e


def get_duration_seconds(ffmpeg: str, path: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    cmd = [
        ffmpeg.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def extract_audio(ffmpeg: str, src: Path, dst: Path) -> Path:
    """
    Extract audio from video (or convert audio) → 48kHz stereo PCM WAV.
    """
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "48000",
        "-ac", "1",
        str(dst),
    ]
    _run(cmd)
    log.info("Audio extracted → %s", dst)
    return dst


def detect_silence_points(ffmpeg: str, path: Path, duration: float) -> List[float]:
    """
    Use ffmpeg silencedetect to find good split points near chunk boundaries.
    Returns a sorted list of silence midpoints (seconds).
    """
    silence_thresh = -40
    silence_dur = 0.3

    cmd = [
        ffmpeg, "-y",
        "-i", str(path),
        "-af", f"silencedetect=noise={silence_thresh}dB:d={silence_dur}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    stderr = result.stderr

    starts, ends = [], []
    for line in stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            starts.append(float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m:
            ends.append(float(m.group(1)))

    midpoints = []
    for s, e in zip(starts, ends):
        midpoints.append((s + e) / 2)

    return midpoints


def smart_split(
    ffmpeg: str,
    src: Path,
    output_dir: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> List[Path]:
    """
    Split audio into ≤29-minute chunks, preferring silence points.
    Returns list of chunk paths in order.
    """
    duration = get_duration_seconds(ffmpeg, src)
    if duration == 0:
        raise ValueError("Could not determine audio duration.")

    log.info("Total duration: %.1f s (%.1f min)", duration, duration / 60)

    n_chunks = math.ceil(duration / MAX_CHUNK_SECONDS)
    ideal_points = [i * MAX_CHUNK_SECONDS for i in range(1, n_chunks)]

    if not ideal_points:
        out = output_dir / "chunk_001.wav"
        cmd = [ffmpeg, "-y", "-i", str(src), "-acodec", "copy", str(out)]
        _run(cmd)
        return [out]

    silence_pts = detect_silence_points(ffmpeg, src, duration)
    log.info("Found %d silence points", len(silence_pts))

    SNAP_WINDOW = 60.0
    split_points = []
    for ideal in ideal_points:
        candidates = [
            s for s in silence_pts
            if abs(s - ideal) <= SNAP_WINDOW
        ]
        if candidates:
            best = min(candidates, key=lambda s: abs(s - ideal))
            split_points.append(best)
        else:
            split_points.append(ideal)
    split_points = sorted(set(split_points))

    boundaries = [0.0] + split_points + [duration]
    chunks = []
    total = len(boundaries) - 1

    for i in range(total):
        start = boundaries[i]
        end = boundaries[i + 1]
        seg_dur = end - start
        out_path = output_dir / f"chunk_{i+1:03d}.wav"

        cmd = [
            ffmpeg, "-y",
            "-ss", f"{start:.3f}",
            "-t", f"{seg_dur:.3f}",
            "-i", str(src),
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            str(out_path),
        ]
        _run(cmd)
        chunks.append(out_path)
        log.info("Chunk %d/%d created: %.1f s", i + 1, total, seg_dur)
        if progress_cb:
            progress_cb(i + 1, total)

    return chunks


def get_max_volume(ffmpeg: str, path: Path) -> float:
    """Scan audio file via volumedetect filter to find max volume in dB."""
    cmd = [
        ffmpeg, "-i", str(path),
        "-af", "volumedetect",
        "-f", "null", "-"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        for line in res.stderr.splitlines():
            if "max_volume:" in line:
                parts = line.split("max_volume:")
                if len(parts) > 1:
                    val = parts[1].strip().split()[0]
                    return float(val)
    except Exception as e:
        log.warning("Failed to detect max volume: %s", e)
    return 0.0


def apply_post_processing(ffmpeg: str, src: Path, dst: Path) -> Path:
    """
    Apply post-processing chain step-by-step to match the Audacity macro EXACTLY:
    Pass 1: Compressor (threshold=-41dB, ratio=2:1, attack=200ms, release=1000ms)
    Pass 2: FilterCurve EQ (100Hz/500Hz boost & 4000Hz/5000Hz presence boost)
    Pass 3: Change Pitch (-1%)
    Pass 4: Limiter (HardLimit at -2.1 dB threshold)
    Pass 5: Peak Normalize to -2.0 dB (via volumedetect analysis + volume gain adjustment)
    """
    import tempfile
    tmp_dir = dst.parent / "_postproc_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def _pass(in_path: Path, out_name: str, af: str) -> Path:
        out = tmp_dir / out_name
        cmd = [
            ffmpeg, "-y",
            "-i", str(in_path),
            "-af", af,
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            str(out),
        ]
        _run(cmd)
        log.debug("Post-proc pass done → %s", out.name)
        return out

    try:
        # Pass 1: Compressor
        # threshold = -41 dB -> linear threshold = 10^(-41/20) = 0.00891
        p1 = _pass(
            src, "p1_comp.wav",
            "acompressor=threshold=0.00891:ratio=2:attack=200:release=1000"
        )

        # Pass 2: FilterCurve EQ
        # Boost <100Hz by 9dB, slope to 0dB at 500Hz. Boost >5000Hz by 9dB, slope from 4000Hz.
        eq_curve = (
            "firequalizer=gain='if(lt(f,100),9,"
            "if(lt(f,500),9-9*(f-100)/400,"
            "if(lt(f,4000),0,"
            "if(lt(f,5000),9*(f-4000)/1000,9))))'"
        )
        p2 = _pass(p1, "p2_eq.wav", eq_curve)

        # Pass 3: Change Pitch (-1% -> pitch=0.99)
        p3 = _pass(p2, "p3_pitch.wav", "rubberband=pitch=0.99")

        # Pass 4: Hard Limiter (-2.1 dB threshold -> limit = 10^(-2.1/20) = 0.785)
        p4 = _pass(p3, "p4_limiter.wav", "alimiter=limit=0.785:attack=5:release=50")

        # Pass 5: Peak Normalize to -2.0 dB
        max_vol = get_max_volume(ffmpeg, p4)
        gain = -2.0 - max_vol
        p5 = _pass(p4, "p5_norm.wav", f"volume={gain}dB")

        # Move final result to dst
        import shutil as _sh
        _sh.move(str(p5), str(dst))

        log.info("FFmpeg post-processing done (Audacity macro replica) → %s", dst.name)
        return dst

    finally:
        # Cleanup temp files
        try:
            import shutil as _sh2
            _sh2.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    return dst


def apply_post_processing_gentle(ffmpeg: str, src: Path, dst: Path) -> Path:
    """
    Gentle mode: only loudness normalization, no EQ/compression/pitch.
    Use this when Adobe Podcast has already done heavy processing and you
    just want consistent loudness across chunks.
    """
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-af", "dynaudnorm=p=0.95:m=31:s=5",
        "-acodec", "pcm_s16le",
        "-ar", "48000",
        "-ac", "1",
        str(dst),
    ]
    for attempt in range(FFMPEG_RETRY_COUNT + 1):
        try:
            _run(cmd)
            log.info("Gentle post-processing done → %s", dst.name)
            return dst
        except RuntimeError:
            if attempt < FFMPEG_RETRY_COUNT:
                log.warning("Gentle post-proc failed, retrying…")
            else:
                raise
    return dst


def apply_passthrough(ffmpeg: str, src: Path, dst: Path) -> Path:
    """
    No processing — just copy/re-encode to ensure consistent format.
    Used when post-processing is disabled.
    """
    import shutil as _sh
    _sh.copy2(str(src), str(dst))
    log.info("Passthrough copy → %s", dst.name)
    return dst


def merge_chunks(ffmpeg: str, chunk_paths: List[Path], dst: Path) -> Path:
    """Concatenate processed chunks in order → merged.wav."""
    if len(chunk_paths) == 1:
        import shutil as sh
        sh.copy2(str(chunk_paths[0]), str(dst))
        log.info("Single chunk, copied to %s", dst)
        return dst

    list_file = dst.parent / "_concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in chunk_paths:
            f.write(f"file '{p.as_posix()}'\n")

    cmd = [
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-acodec", "copy",
        str(dst),
    ]
    _run(cmd)
    list_file.unlink(missing_ok=True)
    log.info("Merged %d chunks → %s", len(chunk_paths), dst)
    return dst


def export_mp3(ffmpeg: str, src: Path, dst: Path) -> Path:
    """Export final WAV → MP3 192k stereo 48kHz."""
    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-acodec", "libmp3lame",
        "-b:a", EXPORT_BITRATE,
        "-ac", str(EXPORT_CHANNELS),
        "-ar", str(EXPORT_RATE),
        str(dst),
    ]
    _run(cmd)
    log.info("Exported MP3 → %s", dst)
    return dst
