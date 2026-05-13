"""
Central configuration for the unified video annotation app.

Every value here is ported from the original standalone scripts
(ann_rand2.1.py, cropper.py, concat_videos.py). Change values here
and every tool picks them up. All paths can also be overridden by
environment variables — useful when running on a different machine.
"""

import os
from pathlib import Path

# --------------------------------------------------------------------------
# PATHS  — defaults match your Windows setup; env vars override
# --------------------------------------------------------------------------
# Root containing video subfolders (Charlote_Videos/, Gretas_Videos/, …)
# ann_rand2.1.py used: r"D:\NewFolder(3)\videoProject\Charlote_Videos"
# We use the parent so the app sees every subfolder.
VIDEO_DIR = Path(os.environ.get(
    "VIDEO_DIR", r"D:\NewFolder(3)\videoProject"
))

# Annotations file (ann_rand2.1.py writes it next to the videos)
ANNOTATION_FILE = Path(os.environ.get(
    "ANNOTATION_FILE", str(VIDEO_DIR / "_frame_annotations.txt")
))

# Cropper output parent (mirrors cropper.py OUTPUT_DIR='croppedVideos')
CROPPED_OUTPUT_DIR = Path(os.environ.get(
    "CROPPED_OUTPUT_DIR", str(VIDEO_DIR / "croppedVideos")
))

# Exact ffmpeg/ffprobe paths from your existing scripts
FFMPEG_BIN  = os.environ.get(
    "FFMPEG_BIN",
    r"C:\Users\bergs\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
)
FFPROBE_BIN = os.environ.get(
    "FFPROBE_BIN",
    r"C:\Users\bergs\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
)

# --------------------------------------------------------------------------
# ANNOTATOR CONSTANTS  (ann_rand2.1.py)
# --------------------------------------------------------------------------
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".m4v"}
POS_WEIGHT       = {"yes": 1, "perfect": 20}
VALID_LABELS     = {"yes", "no", "perfect"}

PRELOAD_MAX           = 20
NUM_WORKERS           = 4
MIN_GAP_SEC           = 1.0
FAVOR_SIGMA_SEC       = 1.75
FAVOR_POS_PROB        = 0.7
FAVOR_CORRIDOR_PROB   = 0.55
CORRIDOR_WEIGHT_GAMMA = 0.8
UNSEEN_VIDEO_BONUS    = 1.0
LOW_RATING_BOOST      = 6.0
LOW_RATING_POWER      = 1.75
RATING_SMOOTHING      = 3.0
BASE_VIDEO_WEIGHT     = 1.0
UNDO_LIMIT            = 100

# Browser frame transport
FRAME_JPEG_QUALITY = 88
FRAME_MAX_DIM      = 1280

# --------------------------------------------------------------------------
# CROPPER DEFAULTS  (cropper.py — these are starting values; the UI sends
# whatever the user picked in the form)
# --------------------------------------------------------------------------
CROP_MIN_PERFECTS         = 3
CROP_MAX_GAP_PERFECTS     = 10.0
CROP_PRE_ROLL             = 2.0
CROP_POST_ROLL            = 2.0
CROP_MERGE_GAP            = 3.0
CROP_MAX_SEGMENTS         = 5
CROP_SELECT_TOP_BY        = "most_perfects"   # or "duration"
CROP_REENCODE             = True
CROP_VIDEO_CODEC          = "libx264"
CROP_CRF                  = "18"
CROP_PRESET               = "veryfast"

# --------------------------------------------------------------------------
# CONCAT DEFAULTS  (concat_videos.py)
# --------------------------------------------------------------------------
CONCAT_VIDEO_CODEC = "libx264"
CONCAT_CRF         = "18"
CONCAT_PRESET      = "veryfast"
CONCAT_AUDIO_CODEC = "aac"
CONCAT_AUDIO_RATE  = "48000"
CONCAT_AUDIO_CHAN  = "2"
CONCAT_AUDIO_BR    = "192k"
