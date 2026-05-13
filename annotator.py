"""
annotator.py — pure-Python annotation engine.

Direct port of ann_rand2.1.py with the Tkinter UI removed. The Flask
layer keeps a single instance alive: workers pre-load frames into a
queue, the browser pulls them via next_frame(), labels come back via
record(), and undo() walks history backward.

Frames are returned as JPEG bytes ready to ship over HTTP. Same biasing
math, same atomic file writes, same corridor logic.

Improvements over ann_rand2.1.py:
  - hhmmss_ms_to_seconds() is defensive against corrupted timestamps
    like "22.500#conflict2" (silently strips the junk)
  - read_existing_annotations_canonical() strips junk tokens too
"""

from __future__ import annotations

import io
import os
import queue
import random
import threading
from typing import Dict, List, Optional, Tuple

import cv2
from PIL import Image

import config


# --------------------------------------------------------------------------
# Pure helpers (identical algorithms to ann_rand2.1.py)
# --------------------------------------------------------------------------

def list_videos(video_dir: str) -> List[str]:
    """Recursively list all videos under video_dir."""
    vids: List[str] = []
    for root, _, files in os.walk(video_dir):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in config.VIDEO_EXTENSIONS:
                vids.append(os.path.join(root, fname))
    return vids


def seconds_to_hhmmss_ms(sec: float) -> str:
    ms = int(round((sec - int(sec)) * 1000))
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _clean_numeric(s: str) -> str:
    """Strip everything but digits and dot — defensive against junk tokens."""
    out = []
    for c in s:
        if c.isdigit() or c == ".":
            out.append(c)
        else:
            break  # stop at the first non-numeric to avoid eating something legitimate
    return "".join(out)


def hhmmss_ms_to_seconds(ts: str) -> float:
    """Parse HH:MM:SS.mmm, tolerant of trailing junk like '#conflict2'."""
    ts = ts.strip()
    if not ts:
        return 0.0
    # Strip anything after '#' (conflict markers from merge tools)
    if "#" in ts:
        ts = ts.split("#", 1)[0].strip()
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(_clean_numeric(s) or "0")
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(_clean_numeric(s) or "0")
        return float(_clean_numeric(parts[0]) or "0")
    except (ValueError, TypeError):
        return 0.0


def _normalize_label(lab: str) -> str:
    lab = (lab or "").strip().lower()
    if "#" in lab:
        lab = lab.split("#", 1)[0].strip()
    return lab if lab in config.VALID_LABELS else ""


def read_existing_annotations_canonical(path: str) -> Dict[str, Dict[str, str]]:
    """Parse any mix of legacy formats into {rel_path: {ts_str: label}}."""
    data: Dict[str, Dict[str, str]] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or "|" not in line:
                    continue
                name, rest = line.split("|", 1)
                name = name.strip().replace("\\", "/")
                rest = rest.strip()
                if not rest:
                    continue
                for chunk in [c.strip() for c in rest.split(",")]:
                    if not chunk or "=" not in chunk:
                        continue
                    ts, lab = chunk.split("=", 1)
                    ts = ts.strip()
                    if "#" in ts:
                        ts = ts.split("#", 1)[0].strip()
                    lab = _normalize_label(lab)
                    if not lab or not ts:
                        continue
                    data.setdefault(name, {})[ts] = lab
    except Exception:
        data = {}
    return data


def write_canonical_annotations_atomic(path: str, data: Dict[str, Dict[str, str]]) -> None:
    """Sorted, deduped, atomic-replace write."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for name in sorted(data.keys()):
            ts_map = data[name]
            ts_sorted = sorted(ts_map.keys(), key=hhmmss_ms_to_seconds)
            if not ts_sorted:
                continue
            line = f"{name} | " + ", ".join(f"{ts}={ts_map[ts]}" for ts in ts_sorted)
            f.write(line + "\n")
    os.replace(tmp, path)


def get_video_meta(video_path: str) -> Tuple[int, float]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    cap.release()
    if frame_count <= 0 or fps <= 0:
        raise RuntimeError(f"Invalid video metadata: {video_path}")
    return frame_count, fps


def read_frame_at_second(video_path: str, target_sec: float, jitter_attempts: int = 4):
    """Returns (PIL.Image, timestamp_str, timestamp_seconds)."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if frame_count <= 0 or fps <= 0:
        cap.release()
        raise RuntimeError(f"Invalid video metadata: {video_path}")

    duration = frame_count / fps
    lo = max(0.02 * duration, 0.0)
    hi = max(0.0, 0.98 * duration)
    t = min(max(target_sec, lo), hi)

    for attempt in range(jitter_attempts + 1):
        idx = int(round(t * fps))
        idx = max(0, min(frame_count - 1, idx))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok and frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            ts_seconds = idx / fps
            ts_str = seconds_to_hhmmss_ms(ts_seconds)
            cap.release()
            return pil_img, ts_str, ts_seconds
        t = min(max(t + (0.5 * (1 if attempt % 2 == 0 else -1)), lo), hi)

    cap.release()
    raise RuntimeError(f"Failed to read a specific frame from: {video_path}")


def get_random_frame(video_path: str, max_attempts: int = 5):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if frame_count <= 0 or fps <= 0:
        cap.release()
        raise RuntimeError(f"Invalid video metadata: {video_path}")

    for _ in range(max_attempts):
        idx = random.randint(max(0, int(0.02 * frame_count)),
                             max(0, int(0.98 * frame_count)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok and frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            ts_seconds = idx / fps
            ts_str = seconds_to_hhmmss_ms(ts_seconds)
            cap.release()
            return pil_img, ts_str, ts_seconds

    cap.release()
    raise RuntimeError(f"Failed to read a random frame from: {video_path}")


def pil_to_jpeg_bytes(img: Image.Image, max_dim: int = None) -> bytes:
    max_dim = max_dim or config.FRAME_MAX_DIM
    w, h = img.size
    scale = min(max_dim / w, max_dim / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=config.FRAME_JPEG_QUALITY, optimize=False)
    return buf.getvalue()


# --------------------------------------------------------------------------
# The engine — long-lived singleton owned by app.py
# --------------------------------------------------------------------------

class AnnotationEngine:
    def __init__(self, video_dir: str, annotation_file: str):
        self.video_dir       = os.path.abspath(video_dir)
        self.annotation_file = str(annotation_file)

        # ---- Cache videos (recursive)
        self.videos: List[str] = list_videos(self.video_dir)
        if not self.videos:
            raise RuntimeError(f"No video files found under: {self.video_dir}")

        def _relkey(p: str) -> str:
            return os.path.relpath(p, self.video_dir).replace("\\", "/")

        self.video_name_by_path: Dict[str, str] = {p: _relkey(p) for p in self.videos}
        self.video_path_by_name: Dict[str, str] = {v: p for p, v in self.video_name_by_path.items()}

        # ---- Canonical annotations: {rel_path: {ts_str: label}}
        self.ann_map: Dict[str, Dict[str, str]] = \
            read_existing_annotations_canonical(self.annotation_file)
        self._migrate_basename_keys_if_unambiguous()

        # Atomic rewrite on startup (dedupe + sort + clean any junk)
        try:
            write_canonical_annotations_atomic(self.annotation_file, self.ann_map)
        except Exception:
            pass

        # Derived caches
        self.annotations_sec: Dict[str, List[Tuple[float, str]]] = {}
        self._rebuild_secondary_caches()

        self.ann_lock = threading.Lock()

        # Live label counts
        self.label_counts: Dict[str, int] = {"yes": 0, "no": 0, "perfect": 0}
        self._recount_labels()

        # Undo: (vid, ts_str, prev_label_or_None)
        self.undo_stack: List[Tuple[str, str, Optional[str]]] = []

        # Pending "perfect" follow-up
        self._pending_followup: Optional[Tuple[str, str]] = None

        # Pre-loader infra
        self.frame_queue: "queue.Queue[dict]" = queue.Queue(maxsize=config.PRELOAD_MAX)
        self.stop_event = threading.Event()
        self.workers: List[threading.Thread] = []

        # Currently-displayed frame, set by next_frame()
        self.current: Optional[dict] = None
        self.current_lock = threading.Lock()

        for i in range(config.NUM_WORKERS):
            t = threading.Thread(target=self._preload_worker,
                                 name=f"preloader-{i}", daemon=True)
            t.start()
            self.workers.append(t)

    # ------------------------------------------------------------------
    # Migration helper (legacy basename-only keys -> rel paths)
    # ------------------------------------------------------------------
    def _migrate_basename_keys_if_unambiguous(self):
        basename_index: Dict[str, List[str]] = {}
        for rel_key in self.video_path_by_name.keys():
            base = os.path.basename(rel_key)
            basename_index.setdefault(base, []).append(rel_key)

        remaps: List[Tuple[str, str]] = []
        for key in list(self.ann_map.keys()):
            if key in self.video_path_by_name:
                continue
            base = os.path.basename(key)
            candidates = basename_index.get(base, [])
            if len(candidates) == 1:
                remaps.append((key, candidates[0]))

        for old, new in remaps:
            existing = self.ann_map.pop(old, {})
            tgt = self.ann_map.setdefault(new, {})
            tgt.update(existing)

    # ------------------------------------------------------------------
    # Caches & weights
    # ------------------------------------------------------------------
    def _rebuild_secondary_caches(self):
        self.annotations_sec.clear()
        for vid_name, ts_map in self.ann_map.items():
            pairs = [(hhmmss_ms_to_seconds(ts), lab) for ts, lab in ts_map.items()]
            pairs.sort(key=lambda x: x[0])
            self.annotations_sec[vid_name] = pairs

    def _recount_labels(self):
        counts = {"yes": 0, "no": 0, "perfect": 0}
        for ts_map in self.ann_map.values():
            for lab in ts_map.values():
                if lab in counts:
                    counts[lab] += 1
        self.label_counts = counts

    def _video_rating_0to1(self, vid_name: str) -> float:
        ts_map = self.ann_map.get(vid_name, {})
        if not ts_map:
            return 0.5
        pos_units = 0.0
        no_count = 0.0
        for lab in ts_map.values():
            if lab == "no":
                no_count += 1.0
            else:
                pos_units += float(config.POS_WEIGHT.get(lab, 0))
        denom = pos_units + no_count + float(config.RATING_SMOOTHING)
        if denom <= 0:
            return 0.5
        return max(0.0, min(1.0, pos_units / denom))

    def _video_pick_weight(self, vid_name: str) -> float:
        ts_map = self.ann_map.get(vid_name, {})
        if not ts_map:
            return float(config.BASE_VIDEO_WEIGHT + config.UNSEEN_VIDEO_BONUS)
        rating = self._video_rating_0to1(vid_name)
        low_factor = (1.0 - rating) ** float(config.LOW_RATING_POWER)
        return float(config.BASE_VIDEO_WEIGHT + config.LOW_RATING_BOOST * low_factor)

    def _get_event_lists(self, vid_name: str):
        pairs = list(self.annotations_sec.get(vid_name, []))
        pairs.sort(key=lambda x: x[0])
        pos      = [(s, config.POS_WEIGHT[l]) for s, l in pairs if l in config.POS_WEIGHT]
        no_secs  = [s for s, l in pairs if l == "no"]
        all_secs = [s for s, _ in pairs]
        return pos, no_secs, all_secs, pairs

    def _build_pos_corridors(self, vid_name: str):
        _, _, _, events = self._get_event_lists(vid_name)
        corridors = []
        pos_indices = [i for i, (_, lab) in enumerate(events) if lab in config.POS_WEIGHT]
        for a, b in zip(pos_indices, pos_indices[1:]):
            if any(lab == "no" for _, lab in events[a+1:b]):
                continue
            start, end = events[a][0], events[b][0]
            if end > start:
                w_a = config.POS_WEIGHT[events[a][1]]
                w_b = config.POS_WEIGHT[events[b][1]]
                corridors.append((start, end, (w_a + w_b) / 2.0))
        return corridors

    # ------------------------------------------------------------------
    # Sampling (corridor → near-positive → uniform)
    # ------------------------------------------------------------------
    def _uniform_sec_away_from_events(self, avoid_secs, duration, rnd, tries=50):
        lo = max(0.02 * duration, 0.0)
        hi = max(0.0, 0.98 * duration)
        for _ in range(tries):
            t = lo + rnd.random() * (hi - lo)
            if not avoid_secs or min(abs(t - s) for s in avoid_secs) >= config.MIN_GAP_SEC:
                return t
        return lo + rnd.random() * (hi - lo)

    def _pick_biased_second(self, vid_name, frame_count, fps, rnd):
        duration = frame_count / max(fps, 1e-9)
        pos_list, _, all_secs, _ = self._get_event_lists(vid_name)

        # 1) Corridor
        corridors = self._build_pos_corridors(vid_name)
        if corridors and rnd.random() < config.FAVOR_CORRIDOR_PROB:
            lengths = [(b - a) for (a, b, _) in corridors]
            weights = [max(L, 1e-3) ** config.CORRIDOR_WEIGHT_GAMMA * max(ep, 0.5)
                       for L, (_, _, ep) in zip(lengths, corridors)]
            (a, b, ep) = rnd.choices(corridors, weights=weights, k=1)[0]
            lo_c = a + config.MIN_GAP_SEC
            hi_c = b - config.MIN_GAP_SEC
            if hi_c > lo_c:
                for _ in range(24):
                    t = lo_c + rnd.random() * (hi_c - lo_c)
                    nearest = min(abs(t - s) for s in all_secs) if all_secs else float("inf")
                    if nearest >= config.MIN_GAP_SEC:
                        return t

        # 2) Around a positive label
        if pos_list and rnd.random() < config.FAVOR_POS_PROB:
            secs = [s for s, _ in pos_list]
            wts  = [max(w, 1e-3) for _, w in pos_list]
            lo = max(0.02 * duration, 0.0)
            hi = max(0.0, 0.98 * duration)
            for _ in range(48):
                center = rnd.choices(secs, weights=wts, k=1)[0]
                t = center + rnd.gauss(0.0, config.FAVOR_SIGMA_SEC)
                t = min(max(t, lo), hi)
                nearest = min(abs(t - s) for s in all_secs) if all_secs else float("inf")
                if nearest >= config.MIN_GAP_SEC:
                    return t

        # 3) Uniform
        return self._uniform_sec_away_from_events(all_secs, duration, rnd)

    def _choose_video_with_weights(self, rnd):
        weights = []
        for p in self.videos:
            name = self.video_name_by_path[p]
            weights.append(max(self._video_pick_weight(name), 0.001))
        return rnd.choices(self.videos, weights=weights, k=1)[0]

    # ------------------------------------------------------------------
    # Preloader worker
    # ------------------------------------------------------------------
    def _preload_worker(self):
        rnd = random.Random()
        while not self.stop_event.is_set():
            try:
                if self.frame_queue.full():
                    self.stop_event.wait(0.05)
                    continue

                with self.ann_lock:
                    vid_path = self._choose_video_with_weights(rnd)
                    vid_name = self.video_name_by_path[vid_path]
                    all_secs_snapshot = [s for s, _ in self.annotations_sec.get(vid_name, [])]

                try:
                    frame_count, fps = get_video_meta(vid_path)
                except Exception:
                    continue

                pil_img = None
                ts_str = ""
                for _ in range(10):
                    target_sec = self._pick_biased_second(vid_name, frame_count, fps, rnd)
                    try:
                        img, ts_cand, ts_sec = read_frame_at_second(vid_path, target_sec)
                    except Exception:
                        continue
                    nearest = min(abs(ts_sec - s) for s in all_secs_snapshot) if all_secs_snapshot else float("inf")
                    if nearest >= config.MIN_GAP_SEC:
                        pil_img = img
                        ts_str = ts_cand
                        break

                if pil_img is None:
                    try:
                        pil_img, ts_str, _ = get_random_frame(vid_path)
                    except Exception:
                        continue

                payload = {
                    "video_path": vid_path,
                    "video_name": vid_name,
                    "ts_str": ts_str,
                    "pil_img": pil_img,
                }
                try:
                    self.frame_queue.put(payload, timeout=0.1)
                except queue.Full:
                    pass
            except Exception:
                continue

    def _try_local_followup(self, vid_name: str, center_ts_str: str) -> Optional[dict]:
        """Try to grab a frame near `center_ts_str` after a 'perfect', respecting MIN_GAP."""
        vid_path = self.video_path_by_name.get(vid_name)
        if not vid_path:
            return None
        try:
            frame_count, fps = get_video_meta(vid_path)
        except Exception:
            return None

        duration = frame_count / max(fps, 1e-9)
        lo = max(0.02 * duration, 0.0)
        hi = max(0.0, 0.98 * duration)
        center = hhmmss_ms_to_seconds(center_ts_str)

        with self.ann_lock:
            all_secs = [s for s, _ in self.annotations_sec.get(vid_name, [])]

        radii = [config.MIN_GAP_SEC * 1.10,
                 max(0.75, config.MIN_GAP_SEC * 1.50),
                 max(1.25, config.MIN_GAP_SEC * 2.00),
                 2.50, 3.00]
        candidates = []
        for r in radii:
            candidates.append(min(max(center + r, lo), hi))
            candidates.append(min(max(center - r, lo), hi))

        for t in candidates:
            try:
                img, ts_str, ts_sec = read_frame_at_second(vid_path, t)
            except Exception:
                continue
            nearest = min(abs(ts_sec - s) for s in all_secs) if all_secs else float("inf")
            if nearest < config.MIN_GAP_SEC:
                continue
            return {
                "video_path": vid_path,
                "video_name": vid_name,
                "ts_str": ts_str,
                "pil_img": img,
            }
        return None

    # ------------------------------------------------------------------
    # Public API (called by Flask)
    # ------------------------------------------------------------------
    def next_frame(self, timeout: float = 10.0) -> Optional[dict]:
        """
        Returns the next frame to label. Honors a pending follow-up
        from the last 'perfect' if there was one. Blocks up to `timeout`
        seconds waiting for the preloader queue.
        """
        # One-shot follow-up after 'perfect'
        if self._pending_followup is not None:
            vid_name, center_ts = self._pending_followup
            self._pending_followup = None
            local = self._try_local_followup(vid_name, center_ts)
            if local is not None:
                with self.current_lock:
                    self.current = local
                return local

        try:
            item = self.frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self.current_lock:
            self.current = item
        return item

    def current_frame(self) -> Optional[dict]:
        """Return the currently-displayed frame without advancing."""
        with self.current_lock:
            return self.current

    def record(self, label: str) -> dict:
        """
        Save a label for the currently-displayed frame. Returns a dict
        describing what happened: {ok, changed, prev, video, ts}.
        """
        with self.current_lock:
            cur = self.current
        if cur is None:
            return {"ok": False, "reason": "no_current_frame"}

        lab = _normalize_label(label)
        if not lab:
            return {"ok": False, "reason": "invalid_label"}

        vid_name = cur["video_name"]
        ts_str   = cur["ts_str"]

        with self.ann_lock:
            prev = self.ann_map.setdefault(vid_name, {}).get(ts_str)
            if prev == lab:
                return {"ok": True, "changed": False, "prev": prev,
                        "video": vid_name, "ts": ts_str}

            # Push undo entry BEFORE mutating
            self.undo_stack.append((vid_name, ts_str, prev))
            if len(self.undo_stack) > config.UNDO_LIMIT:
                self.undo_stack.pop(0)

            self.ann_map[vid_name][ts_str] = lab

            # Incremental count update
            if prev in self.label_counts:
                self.label_counts[prev] -= 1
            if lab in self.label_counts:
                self.label_counts[lab] += 1

            self._rebuild_secondary_caches()
            try:
                write_canonical_annotations_atomic(self.annotation_file, self.ann_map)
            except Exception as e:
                return {"ok": False, "reason": f"save_failed: {e}"}

        if lab == "perfect":
            self._pending_followup = (vid_name, ts_str)

        return {"ok": True, "changed": True, "prev": prev,
                "video": vid_name, "ts": ts_str}

    def undo(self) -> dict:
        """Pop one entry off the undo stack and restore."""
        if not self.undo_stack:
            return {"ok": False, "reason": "empty"}

        vid_name, ts_str, prev_label = self.undo_stack.pop()

        with self.ann_lock:
            ts_map = self.ann_map.get(vid_name, {})
            current_lab = ts_map.get(ts_str)

            if prev_label is None:
                ts_map.pop(ts_str, None)
                if not ts_map:
                    self.ann_map.pop(vid_name, None)
            else:
                ts_map[ts_str] = prev_label
                self.ann_map[vid_name] = ts_map

            # Incremental count update
            if current_lab in self.label_counts:
                self.label_counts[current_lab] -= 1
            if prev_label in self.label_counts:
                self.label_counts[prev_label] += 1

            self._rebuild_secondary_caches()
            try:
                write_canonical_annotations_atomic(self.annotation_file, self.ann_map)
            except Exception as e:
                return {"ok": False, "reason": f"save_failed: {e}"}

        self._pending_followup = None

        # Re-show the just-undone frame so user can re-label
        local = self._try_local_followup(vid_name, ts_str)
        if local is not None:
            with self.current_lock:
                self.current = local

        return {
            "ok": True, "video": vid_name, "ts": ts_str,
            "restored_to": prev_label, "was": current_lab,
            "reshown": local is not None,
        }

    def stats(self) -> dict:
        """Return the data the status bar needs."""
        n_videos = len(self.videos)
        n_with_anns = sum(1 for ts_map in self.ann_map.values() if ts_map)
        c = self.label_counts
        total = c["yes"] + c["no"] + c["perfect"]
        return {
            "counts": c,
            "total": total,
            "videos_total": n_videos,
            "videos_annotated": n_with_anns,
            "undo_depth": len(self.undo_stack),
            "queue_size": self.frame_queue.qsize(),
            "queue_max": config.PRELOAD_MAX,
        }

    def shutdown(self):
        self.stop_event.set()
        for t in self.workers:
            t.join(timeout=0.2)
