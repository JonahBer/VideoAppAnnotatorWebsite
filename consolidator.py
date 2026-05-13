"""
consolidator.py — merge multiple annotation files into one.

Same algorithm as the standalone consolidate_annotations.py we built:
parses every line, infers parent folders for bare filenames, resolves
duplicate (path, timestamp) pairs, and writes a canonical merged file.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import List, Dict, Tuple, Set, Iterable

LABEL_RANK = {"no": 1, "yes": 2, "perfect": 3}


def _clean_path(raw: str) -> str:
    name = raw.strip().lstrip("\ufeff")
    if name.lower().startswith("combined.txt"):
        name = name[len("combined.txt"):]
    return name.replace("\\", "/").strip()


def _split_folder_basename(path: str) -> Tuple[str, str]:
    if "/" in path:
        folder, _, base = path.rpartition("/")
        return folder, base
    return "", path


def _parse_line(line: str):
    line = line.strip().lstrip("\ufeff")
    if not line or "|" not in line:
        return None
    path_part, data_part = line.split("|", 1)
    path = _clean_path(path_part)
    if not path:
        return None
    entries = []
    for chunk in data_part.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        ts, lab = chunk.split("=", 1)
        ts = ts.strip()
        lab = lab.strip().lower()
        if ts and lab:
            entries.append((ts, lab))
    return path, entries


def _build_folder_map(parsed_lines):
    folder_map: Dict[str, str | None] = {}
    for path, _ in parsed_lines:
        folder, base = _split_folder_basename(path)
        if not folder:
            continue
        if base not in folder_map:
            folder_map[base] = folder
        elif folder_map[base] is None:
            continue
        elif folder_map[base] != folder:
            folder_map[base] = None
    return folder_map


def _resolve_path(path, folder_map, ambiguous_seen):
    folder, base = _split_folder_basename(path)
    if folder:
        return path
    inferred = folder_map.get(base)
    if inferred is None:
        if base in folder_map:
            ambiguous_seen.add(base)
        return base
    return f"{inferred}/{base}"


def _pick_label(existing, incoming, mode):
    if mode == "first":
        return existing
    if mode == "last":
        return incoming
    return incoming if LABEL_RANK.get(incoming, 0) > LABEL_RANK.get(existing, 0) else existing


def _ts_sort_key(ts):
    try:
        h, m, s = ts.split(":")
        return (int(h), int(m), float(s))
    except (ValueError, AttributeError):
        return (10**9, 0, 0.0)


def consolidate(input_paths: Iterable[Path],
                output_path: Path,
                conflict_mode: str = "strongest",
                infer_folders: bool = True) -> dict:
    """Merge files. Returns a summary dict the UI can render."""
    parsed_lines = []
    for path in input_paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            p = _parse_line(raw)
            if p is not None:
                parsed_lines.append(p)

    folder_map = _build_folder_map(parsed_lines) if infer_folders else {}

    merged: Dict[str, Dict[str, str]] = defaultdict(dict)
    stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"records": 0, "duplicates": 0, "conflicts": 0}
    )
    ambiguous_seen: Set[str] = set()

    for raw_path, entries in parsed_lines:
        resolved = _resolve_path(raw_path, folder_map, ambiguous_seen)
        bucket = merged[resolved]
        for ts, lab in entries:
            if ts in bucket:
                if bucket[ts] == lab:
                    stats[resolved]["duplicates"] += 1
                else:
                    stats[resolved]["conflicts"] += 1
                    bucket[ts] = _pick_label(bucket[ts], lab, conflict_mode)
            else:
                bucket[ts] = lab
                stats[resolved]["records"] += 1

    # Write canonical output
    out_lines = []
    for path in sorted(merged.keys(), key=str.lower):
        ts_map = merged[path]
        ordered = sorted(ts_map.items(), key=lambda kv: _ts_sort_key(kv[0]))
        out_lines.append(f"{path} | " + ", ".join(f"{ts}={lab}" for ts, lab in ordered))
    Path(output_path).write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    total_records = sum(s["records"] for s in stats.values())
    total_dupes   = sum(s["duplicates"] for s in stats.values())
    total_conf    = sum(s["conflicts"] for s in stats.values())
    inferred      = {b for b, f in folder_map.items() if f is not None}

    per_video = []
    for path in sorted(merged.keys(), key=str.lower):
        s = stats[path]
        per_video.append({
            "video": path,
            "records": len(merged[path]),
            "duplicates": s["duplicates"],
            "conflicts": s["conflicts"],
        })

    return {
        "output_path": str(output_path),
        "videos": len(merged),
        "records": total_records,
        "duplicates_skipped": total_dupes,
        "conflicts_resolved": total_conf,
        "folder_inferred": len(inferred),
        "ambiguous": sorted(ambiguous_seen),
        "per_video": per_video,
    }
