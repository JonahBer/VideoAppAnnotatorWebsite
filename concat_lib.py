"""
concat_lib.py — combine all videos in a folder into one robust output.

Ported from concat_videos.py with fixes:
  1) No hardcoded DEFAULT_INPUT_FOLDER pointing at a stale run name —
     caller passes the folder.
  2) Output always lands under config.CROPPED_OUTPUT_DIR with an absolute
     path (original used "./croppedVideos" relative to CWD, which was
     wrong when the Flask process ran from a different directory).
  3) Silent inputs get a synthesized audio track so the concat demuxer
     doesn't fail when some clips lack audio.
  4) Uses rational fps string when ffprobe returns one (avoids 29.97
     vs 30.000 drift on long concats).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Generator, List, Optional, Tuple

import config


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def has_binary(path: str) -> bool:
    return Path(path).exists() or shutil.which(path) is not None


_natnum = re.compile(r"(\d+)")
def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in _natnum.split(s)]


def find_videos(folder: Path, recursive: bool, exts: set) -> List[Path]:
    if recursive:
        files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    else:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    files.sort(key=lambda p: _natural_key(p.name))
    return files


def ffprobe_props(path: Path):
    """Returns (width, height, fps_str_or_None, has_audio)."""
    if not has_binary(config.FFPROBE_BIN):
        return (None, None, None, False)

    proc = _run([
        config.FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    width = height = fps_str = None
    if proc.returncode == 0:
        lines = [x.strip() for x in proc.stdout.strip().splitlines() if x.strip()]
        if len(lines) >= 3:
            try:
                width = int(lines[0])
                height = int(lines[1])
                fps_str = lines[2]  # keep rational, e.g. "30000/1001"
            except Exception:
                pass

    # Check for audio
    proc_a = _run([
        config.FFPROBE_BIN, "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    has_audio = proc_a.returncode == 0 and "audio" in proc_a.stdout

    return (width, height, fps_str, has_audio)


def normalize_to_tmp(inputs: List[Path], size: Tuple[int, int],
                     fps_str: Optional[str],
                     log: Callable[[str], None]) -> Tuple[List[Path], Path]:
    """Re-encode each input to a uniform size/fps/codec with a guaranteed audio track."""
    tmpdir = Path(tempfile.mkdtemp(prefix="concat_norm_"))
    norm_paths: List[Path] = []
    w, h = size

    for i, src in enumerate(inputs, start=1):
        dst = tmpdir / f"norm_{i:04d}.mp4"
        _, _, _, has_audio = ffprobe_props(src)

        vf_chain = [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
        ]
        if fps_str:
            vf_chain.append(f"fps={fps_str}")

        cmd = [
            config.FFMPEG_BIN,
            "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts",
            "-i", str(src),
        ]
        # If no audio, synthesize a silent track so the concat demuxer doesn't choke
        if not has_audio:
            cmd += ["-f", "lavfi", "-i",
                    f"anullsrc=channel_layout=stereo:sample_rate={config.CONCAT_AUDIO_RATE}",
                    "-shortest"]

        cmd += [
            "-vf", ",".join(vf_chain),
            "-c:v", config.CONCAT_VIDEO_CODEC,
            "-crf", config.CONCAT_CRF,
            "-preset", config.CONCAT_PRESET,
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-c:a", config.CONCAT_AUDIO_CODEC,
            "-ar", config.CONCAT_AUDIO_RATE,
            "-ac", config.CONCAT_AUDIO_CHAN,
            "-b:a", config.CONCAT_AUDIO_BR,
            "-movflags", "+faststart",
            "-y", str(dst),
        ]

        log(f"[norm {i}/{len(inputs)}] {src.name}{' (silent input)' if not has_audio else ''}")
        proc = _run(cmd)
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f"Failed to normalize {src.name}:\n{proc.stderr.strip()}")
        norm_paths.append(dst)

    return norm_paths, tmpdir


def write_concat_list(paths: List[Path], list_file: Path) -> None:
    with list_file.open("w", encoding="utf-8") as f:
        for p in paths:
            ap = str(p.resolve()).replace("'", r"'\''")
            f.write(f"file '{ap}'\n")


def concat_and_reencode(norm_paths: List[Path], out_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        list_file = Path(tmpdir) / "inputs.txt"
        write_concat_list(norm_paths, list_file)
        cmd = [
            config.FFMPEG_BIN,
            "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c:v", config.CONCAT_VIDEO_CODEC,
            "-crf", config.CONCAT_CRF,
            "-preset", config.CONCAT_PRESET,
            "-c:a", config.CONCAT_AUDIO_CODEC,
            "-ar", config.CONCAT_AUDIO_RATE,
            "-ac", config.CONCAT_AUDIO_CHAN,
            "-b:a", config.CONCAT_AUDIO_BR,
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]
        proc = _run(cmd)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(proc.stderr.strip() or "ffmpeg concat failed")


# ----------------------------------------------------------------------
# Public entry point — generator yields log lines
# ----------------------------------------------------------------------
def run_concat(params: dict,
               log: Optional[Callable[[str], None]] = None) -> Generator[str, None, dict]:

    def emit(msg: str):
        if log:
            log(msg)
        return msg

    folder = Path(params.get("folder", "")).expanduser()
    if not folder.is_absolute():
        folder = (config.CROPPED_OUTPUT_DIR / folder).resolve()
    folder = folder.resolve()

    if not folder.is_dir():
        yield emit(f"[ERROR] Folder not found: {folder}")
        return {"ok": False, "error": "folder_missing"}

    if not has_binary(config.FFMPEG_BIN):
        yield emit(f"[ERROR] ffmpeg not found: {config.FFMPEG_BIN}")
        return {"ok": False, "error": "ffmpeg_missing"}
    if not has_binary(config.FFPROBE_BIN):
        yield emit(f"[ERROR] ffprobe not found: {config.FFPROBE_BIN}")
        return {"ok": False, "error": "ffprobe_missing"}

    out_filename = params.get("out", "combined.mp4")
    if not out_filename.lower().endswith(".mp4"):
        out_filename += ".mp4"

    exts_raw = params.get("exts", ".mp4,.mov,.mkv,.m4v")
    exts = {
        (e.lower().strip() if e.strip().startswith(".") else "." + e.lower().strip())
        for e in exts_raw.split(",") if e.strip()
    }
    recursive = bool(params.get("recursive", False))
    custom_size = params.get("size")   # "1920x1080" or None
    custom_fps  = params.get("fps")    # number or None

    inputs = find_videos(folder, recursive, exts)
    if not inputs:
        yield emit(f"[ERROR] No videos found in {folder}")
        return {"ok": False, "error": "no_inputs"}

    yield emit(f"[INFO] Folder: {folder}")
    yield emit(f"[INFO] Found {len(inputs)} videos to combine")

    # Determine target size
    if custom_size:
        try:
            w, h = map(int, custom_size.lower().split("x"))
        except Exception:
            yield emit("[ERROR] Invalid size; use WxH (e.g. 1920x1080)")
            return {"ok": False, "error": "bad_size"}
    else:
        w0, h0, _fps0, _has_audio = ffprobe_props(inputs[0])
        if not w0 or not h0:
            yield emit("[ERROR] Could not detect size from first file; pass size=WxH")
            return {"ok": False, "error": "bad_first_size"}
        w, h = w0, h0
    yield emit(f"[INFO] Target size: {w}x{h}")

    # Determine target fps
    if custom_fps:
        fps_str = str(custom_fps)
    else:
        _, _, fps_str, _ = ffprobe_props(inputs[0])
    yield emit(f"[INFO] Target fps: {fps_str or '(not enforced)'}")

    # Output dir mirrors the original behavior
    out_dir = config.CROPPED_OUTPUT_DIR / f"{folder.name}_cropped"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / Path(out_filename).name).resolve()

    tmpdir = None
    try:
        norm_paths, tmpdir = normalize_to_tmp(inputs, (w, h), fps_str, emit)
        yield emit(f"[INFO] Normalized {len(norm_paths)} clips; concatenating...")
        concat_and_reencode(norm_paths, out_path)
    except Exception as e:
        yield emit(f"[ERROR] {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    yield emit(f"[DONE] {out_path}")
    return {"ok": True, "output": str(out_path)}
