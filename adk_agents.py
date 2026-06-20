"""
ADK Presentation Coach — agent pipeline.

Design (agent-engineering rationale):
    Deterministic media steps (transcription, video presence) are NOT wrapped in
    LLM agents. Calling an LLM just to invoke one tool with a fixed argument adds
    latency, cost, and failure modes (the model can add commentary or skip the
    tool) for zero reasoning value. They are plain in-process calls into
    media_tools.py, with models cached once per process.

    The one place that needs an LLM is qualitative feedback on the transcript —
    that stays an ADK LlmAgent and uses output_schema for structured output, so
    we no longer parse JSON out of free-form prose.

    The MCP server (mcp_server.py) still exposes the same deterministic tools over
    stdio for interop/demonstration; it shares media_tools.py, so there is one
    implementation.

Audio pipeline: run_coach_sync(audio_path, slide_num, total_slides)
Video pipeline: run_video_sync(video_path)
Offline (both): run_offline_sync(video_path, slide_num, total_slides)
"""
import asyncio
import json
import os
import re as _re

import anyio
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

from media_tools import analyze_video_file, transcribe_media

# ── Configuration ─────────────────────────────────────────────────────────────

# MODEL_PROVIDER: "gemini" (default) | "anthropic" | "ollama"
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "gemini")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
HAIKU_MODEL    = "anthropic/claude-haiku-4-5-20251001"
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_URL     = os.getenv("OLLAMA_URL", "http://localhost:11434")

APP_NAME = "presentation_coach"


def _model():
    if MODEL_PROVIDER == "anthropic":
        return LiteLlm(model=HAIKU_MODEL)
    if MODEL_PROVIDER == "ollama":
        return LiteLlm(model=f"ollama_chat/{OLLAMA_MODEL}", api_base=OLLAMA_URL)
    return GEMINI_MODEL


# Shared session service (persists across requests within the same process)
_session_service = InMemorySessionService()


# ── Prompt-injection sanitization (applied BEFORE the transcript reaches the LLM)
# Defined here, not in app.py, because the defense only works if the model never
# sees the raw text. app.py imports sanitize_transcript from this module.
MAX_TRANSCRIPT_CHARS = 3000
_INJECTION_RE = _re.compile(
    r"(ignore\s+previous\s+instructions|forget\s+everything|"
    r"you\s+are\s+now\s+|system\s+prompt|jailbreak|"
    r"<\s*script|PROMPT\s*:)",
    _re.IGNORECASE,
)


def sanitize_transcript(text: str) -> str:
    """Truncate and strip prompt-injection patterns before LLM use."""
    text = (text or "")[:MAX_TRANSCRIPT_CHARS]
    text = _INJECTION_RE.sub("[redacted]", text)
    return text.strip()


# ── Structured feedback contract ──────────────────────────────────────────────

class FillerCount(BaseModel):
    """One filler word and how many times it occurred."""
    word: str = Field(description="The filler word, e.g. 'um', 'like', 'you know'.")
    count: int = Field(description="Number of times it appeared in the transcript.")


class Feedback(BaseModel):
    """Structured presentation feedback returned by the feedback agent."""
    pacing: str = Field(description="Feedback on speed and rhythm.")
    # A list (not a free-text string) so the frontend can chart filler counts.
    fillers: list[FillerCount] = Field(
        default_factory=list,
        description="Each filler word detected with its occurrence count. Empty list if none.",
    )
    tone: str = Field(description="Feedback on confidence and tone.")
    relevance: str = Field(description="How well the speech covers the slide content.")


_EMPTY_FEEDBACK = {"pacing": "", "fillers": [], "tone": "", "relevance": ""}


def _parse_feedback(raw) -> dict:
    """
    Coerce the feedback agent's output into a dict. With output_schema set, this is
    already structured; we keep a small fallback for providers that still return a
    JSON string or wrap it lightly.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            m = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
    return {**_EMPTY_FEEDBACK, "pacing": str(raw)}


# ── Feedback agent (the only LLM step) ────────────────────────────────────────

async def _run_feedback(transcript: str, slide_num: int, total_slides: int,
                        slide_content: str = "") -> dict:
    slide_ctx = (
        f"\n\nSlide {slide_num} content from the uploaded deck:\n---\n{slide_content}\n---"
        if slide_content else ""
    )
    feedback_agent = LlmAgent(
        name="PresentationFeedbackAgent",
        model=_model(),
        description="Specialist: analyzes a speech transcript for presentation quality.",
        instruction=(
            f"You are a presentation coach. Analyze this transcript from "
            f"slide {slide_num} of {total_slides}.{slide_ctx}\n\n"
            f"Transcript:\n{transcript}\n\n"
            f"Provide concise, specific feedback on pacing, tone, and relevance to the "
            f"slide content. For fillers, list each filler word you find (um, uh, like, "
            f"you know, so, actually, ...) with its exact occurrence count; empty list if none."
        ),
        output_schema=Feedback,
        output_key="feedback",
    )

    runner = Runner(
        agent=feedback_agent,
        app_name=APP_NAME,
        session_service=_session_service,
    )
    session = await _session_service.create_session(
        app_name=APP_NAME, user_id="user", state={},
    )
    events = runner.run_async(
        user_id="user",
        session_id=session.id,
        new_message=Content(role="user", parts=[Part(text="Provide the feedback.")]),
    )
    async for _ in events:
        pass

    updated = await _session_service.get_session(
        app_name=APP_NAME, user_id="user", session_id=session.id,
    )
    state = updated.state if updated else {}
    return _parse_feedback(state.get("feedback", _EMPTY_FEEDBACK))


# ── Public pipelines ──────────────────────────────────────────────────────────

async def run_coach(audio_path: str, slide_num: int, total_slides: int,
                    slide_content: str = "") -> dict:
    """
    Transcribe one audio/video segment, then get structured feedback.
    Returns {"transcript": str, "feedback": dict}.
    """
    transcript = await asyncio.to_thread(transcribe_media, audio_path, slide_content)
    safe_transcript = sanitize_transcript(transcript)  # sanitize BEFORE the LLM
    feedback = await _run_feedback(safe_transcript, slide_num, total_slides, slide_content)
    return {"transcript": transcript, "feedback": feedback}


def run_video_sync(video_path: str) -> dict:
    """
    Analyse one video for presentation presence. Deterministic, in-process — no
    LLM, no subprocess. Returns {"video_report": dict}.
    """
    return {"video_report": analyze_video_file(video_path)}


# ── Sync wrappers for Flask ───────────────────────────────────────────────────

def _anyio_run(coro_fn, *args):
    """Run an async function, converting anyio cancel-scope cleanup errors into TimeoutError."""
    try:
        return anyio.run(coro_fn, *args)
    except RuntimeError as e:
        if "cancel scope" in str(e).lower():
            raise TimeoutError(
                "Feedback agent run failed during async cleanup. "
                "Check MODEL_PROVIDER / API key configuration."
            ) from None
        raise


def run_coach_sync(audio_path: str, slide_num: int, total_slides: int,
                   slide_content: str = "") -> dict:
    """Blocking wrapper — safe to call from Flask route handlers."""
    return _anyio_run(run_coach, audio_path, slide_num, total_slides, slide_content)


def run_offline_sync(video_path: str, slide_num: int, total_slides: int,
                     slide_content: str = "") -> dict:
    """
    Offline mode: speech coaching + video analysis on one video file.
    Whisper handles video→audio extraction internally via ffmpeg.
    Returns {"transcript": str, "feedback": dict, "video_report": dict}.
    """
    coach_result = run_coach_sync(video_path, slide_num, total_slides, slide_content)
    video_result = run_video_sync(video_path)
    return {
        "transcript":   coach_result.get("transcript", ""),
        "feedback":     coach_result.get("feedback", {}),
        "video_report": video_result.get("video_report", {}),
    }
