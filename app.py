"""
Presentation Coach — Flask API
Concepts demonstrated:
  • MCP Server        (Day 2) — tools exposed via mcp_server.py, consumed by ADK agents
  • Multi-Agent ADK   (Day 1) — orchestrator → transcription agent → feedback agent
  • Agent Skills      (Day 3) — .agents/skills/ loaded on demand by each specialist agent
  • Security Features (Day 4) — API key auth, CORS restriction, input sanitization, audit log
"""
# Compatibility shim: aiohttp 3.9+ removed several exception aliases used by litellm
import asyncio as _asyncio
import aiohttp as _aiohttp
_aiohttp_patches = {
    'ClientConnectorDNSError': _aiohttp.ClientConnectorError,
    'ConnectionTimeoutError':  _aiohttp.ServerTimeoutError,
    'SocketTimeoutError':      _aiohttp.ServerTimeoutError,
    'ClientProxyConnectionError': _aiohttp.ClientConnectorError,
}
for _name, _cls in _aiohttp_patches.items():
    if not hasattr(_aiohttp, _name):
        setattr(_aiohttp, _name, _cls)

import json
import logging
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps

_TMPDIR = tempfile.gettempdir()

from flask import Flask, jsonify, request
from flask_cors import CORS

from adk_agents import (
    MODEL_PROVIDER,
    run_coach_sync,
    run_offline_sync,
    run_video_sync,
    sanitize_transcript,
)
from slide_extractor import extract_slides


def _utcnow_iso() -> str:
    """Timezone-aware UTC timestamp in ISO-8601 (e.g. 2026-06-20T05:00:00+00:00)."""
    return datetime.now(timezone.utc).isoformat()

# ── Structured JSON logging (Pillar 6 — Observability) ───────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Security: CORS restricted to localhost only (Pillar 1) ───────────────────
CORS(
    app,
    origins=[
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5001",
        "http://127.0.0.1:5001",
    ],
)

# ── Security: API key authentication (Pillar 5 — IAM) ────────────────────────
DEV_MODE = os.getenv("FLASK_ENV", "production") == "development"

API_KEY = os.getenv("API_KEY", "dev-key-change-in-production")
if not DEV_MODE and API_KEY == "dev-key-change-in-production":
    raise RuntimeError(
        "Refusing to start in production with the default API_KEY. "
        "Set a real API_KEY env var, or use FLASK_ENV=development for local testing."
    )

def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if DEV_MODE:
            return f(*args, **kwargs)
        key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(key, API_KEY):
            log.warning('"event":"auth_failure","ip":"%s"' % request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Security: Input validation + prompt sanitization (Pillar 4) ──────────────
ALLOWED_AUDIO_MIMES = {
    "audio/wav", "audio/wave", "audio/mpeg",
    "audio/webm", "audio/ogg", "audio/mp4",
}
ALLOWED_VIDEO_MIMES = {
    "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo",
}
# sanitize_transcript is imported from adk_agents — the injection scrub must run
# inside the pipeline (before the LLM sees the transcript), not only here at the
# display boundary. app.py still calls it to sanitize stored/returned text.


def validate_audio(audio_file) -> str | None:
    """Return error string if file is invalid, else None."""
    mime = audio_file.mimetype or ""
    if mime and mime not in ALLOWED_AUDIO_MIMES:
        return f"Unsupported audio type: {mime}"
    return None


def validate_video(video_file) -> str | None:
    """Return error string if file is invalid, else None."""
    mime = video_file.mimetype or ""
    if mime and mime not in ALLOWED_VIDEO_MIMES:
        return f"Unsupported video type: {mime}"
    return None


# ── Audit log (Pillar 7 — Governance) ────────────────────────────────────────
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG", "audit.jsonl")


def write_audit(request_id: str, tool: str, summary: str, latency_ms: float, error: str = ""):
    entry = {
        "request_id": request_id,
        "timestamp": _utcnow_iso(),
        "tool": tool,
        "input_summary": summary[:200],
        "latency_ms": round(latency_ms, 2),
        "error": error,
    }
    try:
        with open(AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.error(f'"event":"audit_write_failed","error":"{e}"')


# ── In-memory state (replace with SQLite for production) ─────────────────────
feedback_history: list[dict] = []
current_slide: int = 1
total_slides: int = 10

# Uploaded slide deck — list of text strings, one per slide (0-indexed)
_slide_texts: list[str] = []


def _get_slide_content(slide_num: int) -> str:
    """Return extracted text for slide N (1-indexed). Empty string if no deck loaded."""
    if _slide_texts and 1 <= slide_num <= len(_slide_texts):
        return _slide_texts[slide_num - 1]
    return ""

# ── Async job store for offline pipeline ──────────────────────────────────────
# job_id → {status: pending|running|done|error, result: dict|None, error: str|None}
# Bounded so long-running processes don't leak memory: insertion-ordered, oldest
# evicted past MAX_JOBS.
_jobs: dict = {}
MAX_JOBS = 100


def _register_job(job_id: str) -> None:
    while len(_jobs) >= MAX_JOBS:
        oldest = next(iter(_jobs))
        del _jobs[oldest]
    _jobs[job_id] = {'status': 'pending', 'result': None, 'error': None}


def _run_offline_job(job_id: str, video_path: str, slide_num: int, total: int, slide_content: str = ""):
    """Background thread: run offline pipeline and store result in _jobs."""
    _jobs[job_id]['status'] = 'running'
    try:
        result = run_offline_sync(video_path, slide_num, total, slide_content)
        transcript = sanitize_transcript(result.get("transcript", ""))
        feedback   = result.get("feedback", {})
        video_report = result.get("video_report", {})

        entry = {
            "slide": slide_num,
            "transcript": transcript,
            "feedback": feedback,
            "video_report": video_report,
            "timestamp": _utcnow_iso(),
        }
        feedback_history.append(entry)

        _jobs[job_id].update({
            'status': 'done',
            'result': {
                'transcript': transcript,
                'feedback':   feedback,
                'video_report': video_report,
                'slide': slide_num,
            }
        })
        log.info(f'"event":"job_done","job_id":"{job_id}"')
    except Exception as e:
        err_msg = str(e)
        if not err_msg or "cancel scope" in err_msg.lower():
            err_msg = (
                f"Pipeline timed out. "
                "Set MODEL_PROVIDER=gemini (or anthropic) and restart Flask — "
                "Ollama on CPU is too slow for offline mode."
            )
        _jobs[job_id].update({'status': 'error', 'error': err_msg})
        log.error(f'"event":"job_error","job_id":"{job_id}","error":"{err_msg}"')
    finally:
        if os.path.exists(video_path):
            os.remove(video_path)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/transcribe", methods=["POST"])
@require_api_key
def transcribe():
    """
    Receives audio, runs the ADK multi-agent pipeline (transcription → feedback),
    returns structured JSON.
    """
    request_id = str(uuid.uuid4())[:8]
    log.info(f'"event":"request","endpoint":"/api/transcribe","id":"{request_id}"')

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    err = validate_audio(audio_file)
    if err:
        return jsonify({"error": err}), 400

    _mime_ext = {
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "audio/webm": ".webm", "audio/ogg": ".ogg",
    }
    _ext = _mime_ext.get(audio_file.mimetype or "", ".wav")
    audio_path = os.path.join(_TMPDIR, f"recording_{request_id}{_ext}")
    audio_file.save(audio_path)

    slide_num = request.form.get("slide", current_slide, type=int)
    start = datetime.now(timezone.utc)

    try:
        # ── ADK multi-agent pipeline ──────────────────────────────────────────
        result = run_coach_sync(audio_path, slide_num, total_slides,
                                slide_content=_get_slide_content(slide_num))

        transcript = sanitize_transcript(result.get("transcript", ""))
        feedback = result.get("feedback", {})
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

        write_audit(
            request_id,
            tool="PresentationCoachOrchestrator",
            summary=f"slide={slide_num}/{total_slides}, transcript_len={len(transcript)}",
            latency_ms=latency_ms,
        )

        entry = {
            "slide": slide_num,
            "transcript": transcript,
            "feedback": feedback,
            "timestamp": _utcnow_iso(),
        }
        feedback_history.append(entry)

        log.info(
            f'"event":"success","id":"{request_id}",'
            f'"slide":{slide_num},"latency_ms":{round(latency_ms)},'
            f'"transcript_len":{len(transcript)}'
        )
        return jsonify({"transcript": transcript, "feedback": feedback, "slide": slide_num})

    except Exception as e:
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        write_audit(request_id, "PresentationCoachOrchestrator", f"slide={slide_num}", latency_ms, str(e))
        log.error(f'"event":"error","id":"{request_id}","error":"{e}"')
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


@app.route("/api/analyze-video", methods=["POST"])
@require_api_key
def analyze_video():
    """
    Accepts a video file, runs the VideoAnalysisAgent pipeline (MediaPipe via MCP),
    returns structured JSON with eye contact, posture, gesture, and lighting scores.
    """
    request_id = str(uuid.uuid4())[:8]
    log.info(f'"event":"request","endpoint":"/api/analyze-video","id":"{request_id}"')

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video_file = request.files["video"]
    err = validate_video(video_file)
    if err:
        return jsonify({"error": err}), 400

    video_path = os.path.join(_TMPDIR, f"video_{request_id}.mp4")
    video_file.save(video_path)

    start = datetime.now(timezone.utc)
    try:
        result = run_video_sync(video_path)
        video_report = result.get("video_report", {})
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

        write_audit(
            request_id,
            tool="VideoAnalysisAgent",
            summary=f"video={video_file.filename}, frames={video_report.get('frames_analysed', '?')}",
            latency_ms=latency_ms,
        )

        log.info(
            f'"event":"success","id":"{request_id}",'
            f'"latency_ms":{round(latency_ms)},'
            f'"score":{video_report.get("partial_score", "?")}'
        )
        return jsonify({"video_report": video_report})

    except Exception as e:
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        write_audit(request_id, "VideoAnalysisAgent", video_file.filename, latency_ms, str(e))
        log.error(f'"event":"error","id":"{request_id}","error":"{e}"')
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(video_path):
            os.remove(video_path)


@app.route("/api/analyze-offline", methods=["POST"])
@require_api_key
def analyze_offline():
    """
    Offline mode: upload a video, start background job, return job_id immediately.
    Client polls /api/job/<job_id> for status and results.
    """
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video_file = request.files["video"]
    err = validate_video(video_file)
    if err:
        return jsonify({"error": err}), 400

    job_id    = str(uuid.uuid4())[:8]
    video_path = os.path.join(_TMPDIR, f"offline_{job_id}.mp4")
    video_file.save(video_path)

    slide_num = request.form.get("slide", current_slide, type=int)
    total     = request.form.get("total", total_slides, type=int)
    slide_content = _get_slide_content(slide_num)

    _register_job(job_id)

    write_audit(job_id, "OfflinePipeline", f"video={video_file.filename}, slide={slide_num}/{total}", 0)
    log.info(f'"event":"job_started","job_id":"{job_id}","video":"{video_file.filename}","has_deck":{bool(slide_content)}')

    t = threading.Thread(
        target=_run_offline_job,
        args=(job_id, video_path, slide_num, total, slide_content),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/job/<job_id>", methods=["GET"])
def get_job(job_id):
    """Poll job status. Returns {status, result, error}. No auth — job_id is the secret."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/upload-slides", methods=["POST"])
@require_api_key
def upload_slides():
    """
    Upload a PDF or PPTX; extract text per slide for relevance analysis.
    Returns {slides: N, preview: [first 3 slide snippets]}.
    """
    global _slide_texts
    if "slides" not in request.files:
        return jsonify({"error": "No slides file provided"}), 400
    f = request.files["slides"]
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in ('.pdf', '.pptx', '.ppt'):
        return jsonify({"error": "Only .pdf and .pptx files are supported"}), 400

    deck_path = os.path.join(_TMPDIR, f"deck_{uuid.uuid4().hex[:8]}{ext}")
    f.save(deck_path)
    try:
        _slide_texts = extract_slides(deck_path)
        preview = [s[:120].replace('\n', ' ') for s in _slide_texts[:3]]
        log.info(f'"event":"slides_uploaded","filename":"{f.filename}","slides":{len(_slide_texts)}')
        return jsonify({"slides": len(_slide_texts), "preview": preview})
    except Exception as e:
        log.error(f'"event":"slides_error","error":"{e}"')
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(deck_path):
            os.remove(deck_path)


@app.route("/api/slide", methods=["POST"])
@require_api_key
def set_slide():
    global current_slide, total_slides
    data = request.get_json(silent=True) or {}
    current_slide = max(1, int(data.get("slide", 1)))
    total_slides = max(1, int(data.get("total", 10)))
    log.info(f'"event":"slide_update","slide":{current_slide},"total":{total_slides}')
    return jsonify({"slide": current_slide, "total": total_slides})


@app.route("/api/history", methods=["GET"])
@require_api_key
def get_history():
    return jsonify(feedback_history)


@app.route("/api/health", methods=["GET"])
def health():
    """
    Health check — no auth required (monitoring-safe). Only the Ollama provider
    has a local dependency to probe; for gemini/anthropic, report the provider
    rather than falsely flagging Ollama as disconnected.
    """
    if MODEL_PROVIDER != "ollama":
        return jsonify({"status": "ok", "provider": MODEL_PROVIDER})
    try:
        import requests as r
        r.get(f'{os.getenv("OLLAMA_URL", "http://localhost:11434")}/api/tags', timeout=5)
        return jsonify({"status": "ok", "provider": "ollama", "ollama": "connected"})
    except Exception:
        return jsonify({"status": "error", "provider": "ollama", "ollama": "disconnected"}), 503


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Never use debug=True in shared or production environments.
    # Use gunicorn for production: gunicorn -w 1 -b 127.0.0.1:5000 app:app
    app.run(host="127.0.0.1", port=5001, debug=False)
