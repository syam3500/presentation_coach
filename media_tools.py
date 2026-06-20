"""
media_tools.py — deterministic, in-process media processing.

Single source of truth for the two CPU-bound operations:
  • transcribe_media   — Whisper speech-to-text (audio or video)
  • analyze_video_file — MediaPipe eye contact / posture / gesture / lighting

Both functions are plain, deterministic Python — no LLM involved. They are
imported directly by adk_agents.py (the hot path) and re-exposed as MCP tools by
mcp_server.py, so there is exactly one implementation.

Models are cached at module scope, so they load once per process instead of once
per request.
"""
import os
import subprocess
import tempfile

_VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.avi', '.mkv', '.m4v'}

# ── Whisper transcription ─────────────────────────────────────────────────────

_whisper_model = None


def _load_whisper():
    """Load (and cache) the Whisper model for this process."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute_type = "float16" if device == "cuda" else "int8"
        _whisper_model = WhisperModel(
            os.getenv("WHISPER_MODEL", "base"),
            device=device,
            compute_type=compute_type,
            cpu_threads=os.cpu_count() or 4,
        )
    return _whisper_model


def _extract_audio(media_path: str) -> tuple[str, bool]:
    """
    If media_path is a video file, extract audio to a temp WAV with ffmpeg.
    Returns (audio_path, should_delete_after_use).
    Falls back to passing the file directly to Whisper if ffmpeg is unavailable.
    """
    ext = os.path.splitext(media_path)[1].lower()
    if ext not in _VIDEO_EXTS:
        return media_path, False
    # mkstemp (not the deprecated, race-prone mktemp): creates the file securely.
    fd, tmp_wav = tempfile.mkstemp(suffix='_audio.wav')
    os.close(fd)
    try:
        subprocess.run(
            ['ffmpeg', '-i', media_path, '-vn',
             '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
             tmp_wav, '-y'],
            capture_output=True, check=True,
        )
        return tmp_wav, True
    except Exception:
        # ffmpeg not installed or failed — Whisper will call ffmpeg internally.
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)
        return media_path, False


def transcribe_media(media_path: str, hint_text: str = "") -> str:
    """
    Transcribe an audio or video file to text. Deterministic, in-process.
    Returns the transcript, or a bracketed status string on failure.

    hint_text (e.g. the current slide's text) is passed to Whisper as initial_prompt
    to bias decoding toward domain vocabulary. Quality-only — no extra CPU cost.
    """
    audio_path, should_delete = _extract_audio(media_path)
    try:
        model = _load_whisper()
        segments, _ = model.transcribe(
            audio_path, language="en",
            beam_size=1, vad_filter=True,
            # condition_on_previous_text=False stops the decoder looping/hallucinating
            # on silence; initial_prompt biases vocab. Both are free at greedy beam=1.
            condition_on_previous_text=False,
            initial_prompt=(hint_text[:200] or None),
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        transcript = " ".join(seg.text for seg in segments).strip()
        return transcript or "[no speech detected]"
    except Exception as e:
        return f"[transcription_error: {e}]"
    finally:
        if should_delete and os.path.exists(audio_path):
            os.remove(audio_path)


# ── Video presence analysis (MediaPipe) ───────────────────────────────────────

def analyze_video_file(video_path: str) -> dict:
    """
    Analyse presentation presence from a video file using MediaPipe.
    Scores eye contact (iris gaze), posture (shoulder-to-ear), gestures (wrist
    delta), and lighting (frame brightness). Returns dict with scores + tips.
    """
    try:
        import cv2
        import mediapipe as mp
        mp_face_mesh = mp.solutions.face_mesh
        mp_pose_sol  = mp.solutions.pose
    except ImportError as e:
        return {"error": str(e), "tip": "pip install mediapipe opencv-python-headless"}

    LEFT_IRIS  = [474, 475, 476, 477]
    RIGHT_IRIS = [469, 470, 471, 472]

    face_mesh = mp_face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1, refine_landmarks=True)
    pose      = mp_pose_sol.Pose(static_image_mode=False, model_complexity=0)

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    interval = max(1, int(fps * float(os.getenv("VIDEO_SAMPLE_SEC", "2.0"))))  # sample every 2 s

    frames_total = 0
    frames_face  = 0
    gaze_scores, posture_scores, gesture_deltas, brightness_vals = [], [], [], []
    prev_lw = prev_rw = None

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % interval != 0:
            frame_idx += 1
            continue
        frame_idx += 1
        frames_total += 1

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness_vals.append(float(gray.mean()))

        # Eye contact via iris position
        face_result = face_mesh.process(rgb)
        if face_result.multi_face_landmarks:
            frames_face += 1
            lm = face_result.multi_face_landmarks[0].landmark
            def lm_x(idx): return lm[idx].x * w
            left_cx  = sum(lm_x(i) for i in LEFT_IRIS)  / 4
            right_cx = sum(lm_x(i) for i in RIGHT_IRIS) / 4
            face_xs  = [lm_x(i) for i in [10, 234, 454, 152]]
            face_w   = max(max(face_xs) - min(face_xs), 1)
            offset   = abs((left_cx + right_cx) / 2 - (min(face_xs) + max(face_xs)) / 2)
            gaze_scores.append(1.0 if offset / face_w < 0.12 else 0.0)

        # Posture + gestures via pose
        pose_result = pose.process(rgb)
        if pose_result.pose_landmarks:
            lp = pose_result.pose_landmarks.landmark
            PL = mp_pose_sol.PoseLandmark
            sh_y  = (lp[PL.LEFT_SHOULDER].y + lp[PL.RIGHT_SHOULDER].y) / 2
            ear_y = (lp[PL.LEFT_EAR].y      + lp[PL.RIGHT_EAR].y)      / 2
            gap   = sh_y - ear_y
            posture_scores.append(1.0 if gap > 0.15 else (0.5 if gap > 0.05 else 0.0))
            lw = (lp[PL.LEFT_WRIST].x,  lp[PL.LEFT_WRIST].y)
            rw = (lp[PL.RIGHT_WRIST].x, lp[PL.RIGHT_WRIST].y)
            if prev_lw:
                dl = ((lw[0]-prev_lw[0])**2 + (lw[1]-prev_lw[1])**2) ** 0.5
                dr = ((rw[0]-prev_rw[0])**2 + (rw[1]-prev_rw[1])**2) ** 0.5
                gesture_deltas.append(dl + dr)
            prev_lw, prev_rw = lw, rw

    cap.release()
    face_mesh.close()
    pose.close()

    def avg(lst, default=0.0): return round(sum(lst) / len(lst), 3) if lst else default

    eye_ratio   = avg(gaze_scores)
    avg_posture = avg(posture_scores, 0.5)
    avg_gesture = avg(gesture_deltas)
    avg_bright  = avg(brightness_vals)
    face_rate   = round(frames_face / frames_total, 2) if frames_total else 0.0

    eye_score     = 10 if eye_ratio   > 0.6  else (5 if eye_ratio   > 0.2  else 0)
    posture_score = 10 if avg_posture > 0.7  else (5 if avg_posture > 0.4  else 0)
    gesture_score = 10 if 0.02 <= avg_gesture <= 0.10 else (5 if avg_gesture > 0.01 else 0)

    tips = []
    if eye_ratio   < 0.6:  tips.append(f"Eye contact: {int(eye_ratio*100)}% (target >60%). Look into the lens.")
    if avg_posture < 0.7:  tips.append("Posture: shoulders appear low. Sit/stand tall, pull shoulders back.")
    if avg_gesture < 0.01: tips.append("Gestures: very little hand movement. Use deliberate gestures for key points.")
    elif avg_gesture > 0.15: tips.append("Gestures: too much hand movement. Slow down; accent, don't distract.")
    if avg_bright  < 80:   tips.append(f"Lighting: dark frame ({avg_bright:.0f}/255). Add a front-facing light.")
    elif avg_bright > 220: tips.append(f"Lighting: overexposed ({avg_bright:.0f}/255). Reduce backlighting.")
    if face_rate   < 0.7:  tips.append(f"Face visible in {int(face_rate*100)}% of frames. Stay centred in frame.")

    return {
        "eye_contact_ratio":   eye_ratio,
        "avg_posture_score":   avg_posture,
        "avg_gesture_delta":   avg_gesture,
        "avg_brightness":      avg_bright,
        "face_detection_rate": face_rate,
        "frames_analysed":     frames_total,
        "partial_score":       eye_score + posture_score + gesture_score,
        "max_score":           30,
        "tips":                tips,
    }
