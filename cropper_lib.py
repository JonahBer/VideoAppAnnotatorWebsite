"""
cropper_lib.py — create cropped highlight videos from annotated timestamps.

Ported from cropper.py with fixes:
  1) Uses config.VIDEO_DIR / config.ANNOTATION_FILE (originals hardcoded
     "Charlote_Videos/_frame_annotations.txt" and ".")
  2) Resolves source files by basename when the annotation entry doesn't
     match a path that exists. The originals would silently skip every
     bare-name annotation in our file (109 of 186 lines!).
  3) Defensive timestamp parser handles junk like "22.500#conflict2".
  4) Run params are passed in, not snapshotted at module load.

Run params come from the Flask request; the function yields log lines so
the UI can stream them.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Tuple

import config


TIME_RE = re.compile(r"^\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d{1,3})?)\s*$")


def _clean_ts(tc: str) -> str:
    tc = tc.strip()
    if "#" in tc:
        tc = tc.split("#", 1)[0].strip()
    return tc


def parse_timecode(tc: str) -> float:
    tc = _clean_ts(tc)
    m = TIME_RE.match(tc)
    if not m:
        raise ValueError(f"Bad timecode: {tc!r}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def secs_to_tc(secs: float) -> str:
    if secs < 0:
        secs = 0.0
    ms = int(round((secs - math.floor(secs)) * 1000))
    s = int(secs) % 60
    m = (int(secs) // 60) % 60
    h = int(secs) // 3600
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def slugify(text) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-")


def has_binary(path: str) -> bool:
    return Path(path).exists() or shutil.which(path) is not None


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def get_video_duration(path: Path) -> float:
    if not has_binary(config.FFPROBE_BIN):
        return 0.0
    proc = _run([
        config.FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    if proc.returncode != 0:
        return 0.0
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def parse_data_file(fpath: Path) -> Dict[str, List[Tuple[float, str]]]:
    """{ 'rel/video.mp4': [(time_sec, label), ... sorted by time] }"""
    result: Dict[str, List[Tuple[float, str]]] = {}
    if not fpath.exists():
        raise FileNotFoundError(f"Data file not found: {fpath}")

    with fpath.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            fname, rest = line.split("|", 1)
            fname = fname.strip().replace("\\", "/")
            entries: List[Tuple[float, str]] = []
            for p in [x.strip().strip(",;") for x in rest.split(",")]:
                if not p or "=" not in p:
                    continue
                left, right = p.rsplit("=", 1)
                t_str = _clean_ts(left)
                label = right.strip().lower()
                if "#" in label:
                    label = label.split("#", 1)[0].strip()
                if label not in {"yes", "no", "perfect"}:
                    continue
                try:
                    entries.append((parse_timecode(t_str), label))
                except ValueError:
                    continue
            if entries:
                entries.sort(key=lambda x: x[0])
                result[fname] = entries
    return result


def _build_basename_index(video_root: Path) -> Dict[str, List[Path]]:
    """basename(lower) -> list of full paths under video_root."""
    idx: Dict[str, List[Path]] = {}
    for p in video_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS:
            idx.setdefault(p.name.lower(), []).append(p)
    return idx


def _resolve_source(video_root: Path, fname: str,
                    basename_idx: Dict[str, List[Path]]) -> Optional[Path]:
    """Try direct path under video_root, then fall back to basename lookup."""
    direct = video_root / fname
    if direct.exists():
        return direct
    base = Path(fname).name.lower()
    candidates = basename_idx.get(base, [])
    if len(candidates) == 1:
        return candidates[0]
    # Ambiguous (>1) or unknown — give up
    return None


# ----------------------------------------------------------------------
# Segment builder
# ----------------------------------------------------------------------
def build_segments_for_video(entries, min_perfects, max_gap, pre_roll, post_roll, merge_gap):
    perfect_times = [t for t, lbl in entries if lbl == "perfect"]
    if not perfect_times:
        return []

    clusters: List[List[float]] = []
    cur = [perfect_times[0]]
    for prev, now in zip(perfect_times, perfect_times[1:]):
        if (now - prev) <= max_gap:
            cur.append(now)
        else:
            clusters.append(cur)
            cur = [now]
    clusters.append(cur)

    raw = []
    for c in clusters:
        if len(c) >= min_perfects:
            raw.append((c[0] - pre_roll, c[-1] + post_roll, len(c)))
    if not raw:
        return []

    raw.sort(key=lambda x: x[0])
    merged = []
    cur_s, cur_e, cur_cnt = raw[0]
    for s, e, cnt in raw[1:]:
        if s - cur_e <= merge_gap:
            cur_e = max(cur_e, e)
            cur_cnt += cnt
        else:
            merged.append((cur_s, cur_e, cur_cnt))
            cur_s, cur_e, cur_cnt = s, e, cnt
    merged.append((cur_s, cur_e, cur_cnt))
    return merged


def pick_top_segments(segments, limit, policy):
    if not segments or limit is None or limit <= 0 or limit >= len(segments):
        return segments
    if policy == "duration":
        ranked = sorted(segments, key=lambda x: x[1] - x[0], reverse=True)
    else:
        ranked = sorted(segments, key=lambda x: (x[2], x[1] - x[0]), reverse=True)
    return sorted(ranked[:limit], key=lambda x: x[0])


# ----------------------------------------------------------------------
# FFmpeg
# ----------------------------------------------------------------------
def ffmpeg_crop(input_path: Path, start: float, end: float, out_path: Path,
                reencode: bool) -> Tuple[bool, str]:
    duration = max(0.01, end - start)
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(input_path),
           "-t", f"{duration:.3f}"]
    if reencode:
        cmd += ["-c:v", config.CROP_VIDEO_CODEC, "-crf", config.CROP_CRF,
                "-preset", config.CROP_PRESET, "-c:a", "aac",
                "-movflags", "+faststart"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-y", str(out_path)]

    if not has_binary(config.FFMPEG_BIN):
        return False, f"ffmpeg not found: {config.FFMPEG_BIN}"

    proc = _run(cmd)
    ok = (proc.returncode == 0) and out_path.exists() and out_path.stat().st_size > 0
    msg = proc.stderr.strip() if proc.stderr else "OK"
    return ok, msg


# ----------------------------------------------------------------------
# Main entry — generator yields log lines for streaming
# ----------------------------------------------------------------------
def run_cropper(params: dict,
                video_root: Optional[Path] = None,
                annotation_file: Optional[Path] = None,
                log: Optional[Callable[[str], None]] = None) -> Generator[str, None, dict]:
    """
    Yields log lines as ffmpeg progresses. Returns final summary dict via
    StopIteration.value. `log` is an optional callback used to mirror lines
    somewhere besides the generator (e.g. server console).
    """
    def emit(msg: str):
        if log:
            log(msg)
        return msg

    video_root      = Path(video_root      or config.VIDEO_DIR).resolve()
    annotation_file = Path(annotation_file or config.ANNOTATION_FILE).resolve()

    # Pull params with sane fallbacks to config defaults
    min_perfects   = int(params.get("min_perfects",       config.CROP_MIN_PERFECTS))
    max_gap        = float(params.get("max_gap",          config.CROP_MAX_GAP_PERFECTS))
    pre_roll       = float(params.get("pre_roll",         config.CROP_PRE_ROLL))
    post_roll      = float(params.get("post_roll",        config.CROP_POST_ROLL))
    merge_gap      = float(params.get("merge_gap",        config.CROP_MERGE_GAP))
    max_segments   = int(params.get("max_segments",       config.CROP_MAX_SEGMENTS))
    select_top_by  = params.get("select_top_by",          config.CROP_SELECT_TOP_BY)
    reencode       = bool(params.get("reencode",          config.CROP_REENCODE))
    source_offset  = int(params.get("source_offset",      0))
    source_limit   = int(params.get("source_limit",       -1))
    run_prefix     = params.get("run_prefix",             "run")

    if not video_root.is_dir():
        yield emit(f"[ERROR] Video root does not exist: {video_root}")
        return {"ok": False, "error": "video_root_missing"}

    if not annotation_file.is_file():
        yield emit(f"[ERROR] Annotation file not found: {annotation_file}")
        return {"ok": False, "error": "annotation_file_missing"}

    # Run folder name from descriptors
    descriptors = [
        ("sel", select_top_by), ("minP", min_perfects), ("gap", max_gap),
        ("pre", pre_roll), ("post", post_roll), ("merge", merge_gap),
        ("maxSeg", max_segments if max_segments > 0 else "all"),
        ("renc", int(reencode)), ("crf", config.CROP_CRF if reencode else "NA"),
        ("off", source_offset), ("lim", source_limit),
    ]
    tokens = [slugify(run_prefix)] if run_prefix else []
    tokens += [f"{slugify(k)}={slugify(v)}" for k, v in descriptors]
    tokens.append(datetime.now().strftime("%Y%m%d-%H%M"))
    run_name = "__".join(tokens)

    run_dir = config.CROPPED_OUTPUT_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.txt").write_text(
        f"Run folder: {run_name}\nCreated: {datetime.now().isoformat()}\n",
        encoding="utf-8"
    )
    yield emit(f"[INFO] Run folder: {run_dir}")

    annotations = parse_data_file(annotation_file)
    yield emit(f"[INFO] Loaded {len(annotations)} videos from annotations")

    basename_idx = _build_basename_index(video_root)
    yield emit(f"[INFO] Indexed {sum(len(v) for v in basename_idx.values())} "
               f"video files under {video_root}")

    # Apply source range slice
    video_items = sorted(annotations.items(), key=lambda kv: kv[0].lower())
    start_idx = max(0, source_offset)
    if source_limit == -1:
        video_items = video_items[start_idx:]
    else:
        video_items = video_items[start_idx:start_idx + max(0, source_limit)]
    yield emit(f"[INFO] Processing {len(video_items)} videos "
               f"(offset={source_offset}, limit={source_limit})")

    csv_path = run_dir / "crop_summary.csv"
    csv_fh = csv_path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(csv_fh)
    writer.writerow(["source_video", "segment_index", "start_sec", "end_sec",
                     "duration_sec", "perfect_count", "output_file_rel"])

    created = 0
    skipped_missing = 0
    skipped_no_segments = 0
    failed = 0

    for fname, entries in video_items:
        src = _resolve_source(video_root, fname, basename_idx)
        if src is None:
            yield emit(f"[WARN] Missing source video (no basename match): {fname}")
            skipped_missing += 1
            continue

        dur = get_video_duration(src)
        segments = build_segments_for_video(
            entries, min_perfects, max_gap, pre_roll, post_roll, merge_gap
        )
        if not segments:
            yield emit(f"[INFO] No qualifying segments in {fname}")
            skipped_no_segments += 1
            continue

        if max_segments > 0:
            segments = pick_top_segments(segments, max_segments, select_top_by)

        base = src.stem
        for i, (s, e, pcnt) in enumerate(segments, start=1):
            if dur > 0:
                s_c = max(0.0, min(s, max(0.0, dur - 0.01)))
                e_c = max(s_c + 0.01, min(e, dur))
            else:
                s_c, e_c = max(0.0, s), max(0.01, e)

            start_tag = secs_to_tc(s_c).replace(":", "-").replace(".", "_")
            end_tag   = secs_to_tc(e_c).replace(":", "-").replace(".", "_")
            out_name  = f"{base}_seg{i}_{start_tag}-{end_tag}.mp4"
            out_path  = run_dir / out_name

            ok, msg = ffmpeg_crop(src, s_c, e_c, out_path, reencode)
            duration = max(0.0, e_c - s_c)
            status = "CREATED" if ok else f"FAILED ({msg})"
            yield emit(f"[{status}] {fname} -> {out_name}  "
                       f"[{s_c:.3f}–{e_c:.3f}s, {duration:.2f}s, perfects={pcnt}]")

            if ok:
                created += 1
            else:
                failed += 1

            writer.writerow([fname, i, f"{s_c:.3f}", f"{e_c:.3f}",
                             f"{duration:.3f}", pcnt, out_name])

    csv_fh.close()
    summary = {
        "ok": True,
        "run_dir": str(run_dir),
        "summary_csv": str(csv_path),
        "created": created,
        "failed": failed,
        "skipped_missing": skipped_missing,
        "skipped_no_segments": skipped_no_segments,
    }
    yield emit(f"[DONE] Created {created}, failed {failed}, "
               f"skipped {skipped_missing + skipped_no_segments}. "
               f"Summary: {csv_path}")
    return summary
