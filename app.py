"""
app.py — Flask server for the unified video annotation app.

Routes
------
GET  /                           SPA shell (templates/index.html)

# Annotator
GET  /api/annotator/init         Initialize the annotation engine
GET  /api/annotator/next         Get the next frame to label (jpeg)
GET  /api/annotator/current      Get the currently-displayed frame (jpeg)
GET  /api/annotator/meta         Metadata for the current frame (video, ts)
POST /api/annotator/record       Save a label (json body: {label})
POST /api/annotator/undo         Undo the last label
GET  /api/annotator/stats        Live counts + queue depth

# Timeline (re-implements view_timelines.py)
GET  /api/timeline/data          Returns the current annotations file as text

# Cropper
POST /api/crop/start             Body: params dict. Returns {job_id}
GET  /api/crop/stream/<job_id>   Server-sent events: log lines + done event

# Concat
POST /api/concat/start           Body: {folder, out, ...}. Returns {job_id}
GET  /api/concat/stream/<job_id> SSE log stream
GET  /api/concat/list_run_dirs   Browse available cropper run folders

# Consolidator
POST /api/consolidate            multipart: input files + output_name
                                 Returns json summary

GET  /api/status                 Server-side status (paths, ffmpeg present)
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from queue import Queue, Empty
from typing import Dict

from flask import (
    Flask, Response, jsonify, render_template, request,
    send_file, abort, stream_with_context,
)

import config
import annotator
import consolidator
import cropper_lib
import concat_lib


app = Flask(__name__, template_folder="templates", static_folder="static")

# Lazy-init the annotation engine on first request so the server starts
# even if VIDEO_DIR doesn't exist yet (useful when ffmpeg/data aren't on
# this machine but the UI is being inspected).
_engine: annotator.AnnotationEngine | None = None
_engine_lock = threading.Lock()
_engine_err: str | None = None


def get_engine() -> annotator.AnnotationEngine | None:
    global _engine, _engine_err
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            _engine = annotator.AnnotationEngine(
                str(config.VIDEO_DIR), str(config.ANNOTATION_FILE)
            )
            _engine_err = None
        except Exception as e:
            _engine_err = str(e)
            _engine = None
        return _engine


# --------------------------------------------------------------------------
# Job registry for streaming long-running ffmpeg jobs (cropper, concat)
# --------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.log: Queue = Queue()
        self.done = False
        self.result: dict | None = None
        self.thread: threading.Thread | None = None


_jobs: Dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _start_job(target_generator_fn) -> Job:
    """Run a generator-style function in a background thread, piping yields → queue."""
    job = Job()

    def _runner():
        try:
            # The log callback is a no-op — lines come through the yield protocol
            # below. We pass an empty callback so library code that wants to mirror
            # to stdout can do so, but we don't double-write to the queue.
            gen = target_generator_fn(lambda line: None)
            result = None
            try:
                while True:
                    line = next(gen)
                    job.log.put(line)
            except StopIteration as stop:
                result = stop.value or {"ok": True}
            job.result = result if isinstance(result, dict) else {"ok": True}
        except Exception as e:
            job.log.put(f"[FATAL] {e}")
            job.result = {"ok": False, "error": str(e)}
        finally:
            job.done = True
            job.log.put(None)  # sentinel

    job.thread = threading.Thread(target=_runner, daemon=True)
    job.thread.start()
    with _jobs_lock:
        _jobs[job.id] = job
    return job


def _stream_job(job: Job):
    """SSE stream that drains the job's log queue and emits a final 'done' event."""
    def gen():
        while True:
            try:
                line = job.log.get(timeout=30)
            except Empty:
                # heartbeat to keep the connection open
                yield ": keepalive\n\n"
                if job.done and job.log.empty():
                    break
                continue
            if line is None:
                break
            yield f"data: {json.dumps({'log': line})}\n\n"
        payload = job.result or {"ok": False, "error": "no result"}
        yield f"event: done\ndata: {json.dumps(payload)}\n\n"

    return Response(stream_with_context(gen()), mimetype="text/event-stream")


# --------------------------------------------------------------------------
# SPA shell
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    import shutil
    def has_bin(p):
        return Path(p).exists() or shutil.which(p) is not None
    return jsonify({
        "video_dir":        str(config.VIDEO_DIR),
        "video_dir_exists": Path(config.VIDEO_DIR).is_dir(),
        "annotation_file":  str(config.ANNOTATION_FILE),
        "annotation_file_exists": Path(config.ANNOTATION_FILE).is_file(),
        "ffmpeg":           config.FFMPEG_BIN,
        "ffmpeg_present":   has_bin(config.FFMPEG_BIN),
        "ffprobe":          config.FFPROBE_BIN,
        "ffprobe_present":  has_bin(config.FFPROBE_BIN),
        "engine_loaded":    _engine is not None,
        "engine_error":     _engine_err,
    })


# --------------------------------------------------------------------------
# Annotator routes
# --------------------------------------------------------------------------
@app.route("/api/annotator/init")
def api_annotator_init():
    eng = get_engine()
    if eng is None:
        return jsonify({"ok": False, "error": _engine_err or "engine not loaded"}), 500
    return jsonify({
        "ok": True,
        "video_dir": eng.video_dir,
        "annotation_file": eng.annotation_file,
        "videos_total": len(eng.videos),
        "stats": eng.stats(),
    })


@app.route("/api/annotator/next")
def api_annotator_next():
    eng = get_engine()
    if eng is None:
        abort(503)
    item = eng.next_frame(timeout=15.0)
    if item is None:
        return ("", 204)
    jpeg = annotator.pil_to_jpeg_bytes(item["pil_img"])
    resp = Response(jpeg, mimetype="image/jpeg")
    resp.headers["X-Video-Name"] = item["video_name"]
    resp.headers["X-Timestamp"]  = item["ts_str"]
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/annotator/current")
def api_annotator_current():
    eng = get_engine()
    if eng is None:
        abort(503)
    item = eng.current_frame()
    if item is None:
        return ("", 204)
    jpeg = annotator.pil_to_jpeg_bytes(item["pil_img"])
    resp = Response(jpeg, mimetype="image/jpeg")
    resp.headers["X-Video-Name"] = item["video_name"]
    resp.headers["X-Timestamp"]  = item["ts_str"]
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/annotator/meta")
def api_annotator_meta():
    eng = get_engine()
    if eng is None:
        return jsonify({"ok": False}), 503
    item = eng.current_frame()
    if item is None:
        return jsonify({"ok": True, "current": None, "stats": eng.stats()})
    return jsonify({
        "ok": True,
        "current": {"video": item["video_name"], "ts": item["ts_str"]},
        "stats": eng.stats(),
    })


@app.route("/api/annotator/record", methods=["POST"])
def api_annotator_record():
    eng = get_engine()
    if eng is None:
        return jsonify({"ok": False}), 503
    label = (request.json or {}).get("label", "")
    result = eng.record(label)
    result["stats"] = eng.stats()
    return jsonify(result)


@app.route("/api/annotator/undo", methods=["POST"])
def api_annotator_undo():
    eng = get_engine()
    if eng is None:
        return jsonify({"ok": False}), 503
    result = eng.undo()
    result["stats"] = eng.stats()
    return jsonify(result)


@app.route("/api/annotator/stats")
def api_annotator_stats():
    eng = get_engine()
    if eng is None:
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True, "stats": eng.stats()})


# --------------------------------------------------------------------------
# Timeline route
# --------------------------------------------------------------------------
@app.route("/api/timeline/data")
def api_timeline_data():
    path = Path(config.ANNOTATION_FILE)
    if not path.is_file():
        return Response("", mimetype="text/plain")
    return Response(
        path.read_text(encoding="utf-8", errors="replace"),
        mimetype="text/plain",
    )


# --------------------------------------------------------------------------
# Cropper
# --------------------------------------------------------------------------
@app.route("/api/crop/start", methods=["POST"])
def api_crop_start():
    params = request.json or {}

    def target(log_cb):
        return cropper_lib.run_cropper(params, log=log_cb)

    job = _start_job(target)
    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/crop/stream/<job_id>")
def api_crop_stream(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        abort(404)
    return _stream_job(job)


# --------------------------------------------------------------------------
# Concat
# --------------------------------------------------------------------------
@app.route("/api/concat/start", methods=["POST"])
def api_concat_start():
    params = request.json or {}

    def target(log_cb):
        return concat_lib.run_concat(params, log=log_cb)

    job = _start_job(target)
    return jsonify({"ok": True, "job_id": job.id})


@app.route("/api/concat/stream/<job_id>")
def api_concat_stream(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        abort(404)
    return _stream_job(job)


@app.route("/api/concat/list_run_dirs")
def api_concat_list_run_dirs():
    """List cropper run folders + the top-level video subdirs as concat targets."""
    out = []
    if config.CROPPED_OUTPUT_DIR.is_dir():
        for p in sorted(config.CROPPED_OUTPUT_DIR.iterdir()):
            if p.is_dir():
                out.append({"path": str(p), "name": p.name, "kind": "cropper_run"})
    # also include top-level subdirs of VIDEO_DIR
    if config.VIDEO_DIR.is_dir():
        for p in sorted(config.VIDEO_DIR.iterdir()):
            if p.is_dir() and p.name != "croppedVideos":
                out.append({"path": str(p), "name": p.name, "kind": "video_subdir"})
    return jsonify({"folders": out})


# --------------------------------------------------------------------------
# Consolidator
# --------------------------------------------------------------------------
@app.route("/api/consolidate", methods=["POST"])
def api_consolidate():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "no files uploaded"}), 400

    output_name = request.form.get("output_name", "consolidated_annotations.txt")
    conflict_mode = request.form.get("conflict_mode", "strongest")
    infer_folders = request.form.get("infer_folders", "1") == "1"

    import tempfile, os
    tmpdir = Path(tempfile.mkdtemp(prefix="consolidate_"))
    saved = []
    try:
        for f in files:
            dst = tmpdir / (f.filename or "input.txt")
            f.save(dst)
            saved.append(dst)
        out_path = tmpdir / output_name
        summary = consolidator.consolidate(
            saved, out_path, conflict_mode=conflict_mode, infer_folders=infer_folders
        )
        summary["download_token"] = uuid.uuid4().hex[:12]
        _consolidated_files[summary["download_token"]] = out_path
        return jsonify({"ok": True, "summary": summary})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_consolidated_files: Dict[str, Path] = {}


@app.route("/api/consolidate/download/<token>")
def api_consolidate_download(token: str):
    path = _consolidated_files.get(token)
    if not path or not path.exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=path.name, mimetype="text/plain")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    host = "127.0.0.1"
    port = 5000
    if "--host" in sys.argv:
        host = sys.argv[sys.argv.index("--host") + 1]
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    # threaded=True is essential — the annotator's preloader runs in worker
    # threads and SSE streams need to coexist with normal requests.
    app.run(host=host, port=port, threaded=True, debug=False)
