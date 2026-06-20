# AGENTS.md — Presentation Coach Agent

## Identity
**Name**: PresentationCoachOrchestrator / VideoCoachOrchestrator
**Role**: Multi-agent system for real-time presentation rehearsal feedback — speech and on-camera presence
**Stack**: Python 3.11+, Flask, Google ADK, MCP (stdio), Whisper (local), Ollama/Mistral (local), MediaPipe (local)

## Architecture
```
Flask API (app.py)
    │
    ├── /api/transcribe     → ADK SequentialAgent: PresentationCoachOrchestrator
    │                               ├── AudioTranscriptionAgent   [output_key: transcript]
    │                               │       └── MCP tool: transcribe_audio  → Whisper
    │                               └── PresentationFeedbackAgent [output_key: feedback]
    │                                       └── MCP tool: analyze_speech    → Ollama/Mistral
    │
    └── /api/analyze-video  → ADK SequentialAgent: VideoCoachOrchestrator
                                    └── VideoAnalysisAgent        [output_key: video_report]
                                            └── MCP tool: analyze_video     → MediaPipe
```

## Agent Conventions
- Think before coding: state assumptions explicitly, surface tradeoffs, halt on ambiguity
- Write minimum code that solves the problem — no speculative features
- Make surgical changes: touch only what the request requires
- Define success criteria before starting; loop until verified

## Coding Standards
- Python 3.11+; type hints on all function signatures
- Structured JSON logging only — no plain print() in production paths
- All temp files deleted in finally blocks
- All external calls wrapped in try/except with error logging

## Security Rules (Day 4 — 7 Pillars)
- API key required (X-API-Key header) on all mutating endpoints
- CORS restricted to localhost origins only
- Audio MIME type validated before saving to disk
- Video MIME type validated before saving to disk
- Transcripts sanitized for prompt injection before LLM processing (max 3000 chars)
- Temp audio and video files deleted immediately after processing
- Every tool invocation written to audit.jsonl
- Never use debug=True in production; use gunicorn

## Context Engineering (Day 1)
- Static context: these rules, skill descriptions in SKILL.md metadata
- Dynamic context: SKILL.md body loaded on demand when skill triggers
- Session state keys: `transcript` (after transcription), `feedback` (after speech analysis), `video_report` (after video analysis)

## Skills Catalog (Day 3)
Load from `.agents/skills/`:

| Skill | Trigger keywords | Output |
|-------|-----------------|--------|
| audio-transcription | transcribe, audio, speech-to-text, recording | Raw text transcript |
| presentation-feedback | feedback, pacing, fillers, tone, relevance, analyze speech | JSON {pacing, fillers, tone, relevance} |
| slide-tracking | slide, next slide, current slide, total slides | Slide state update |
| video-analysis | eye contact, posture, gesture, lighting, on-camera, presence, video | JSON {eye_contact_ratio, avg_posture_score, avg_gesture_delta, avg_brightness, partial_score, tips} |

## Environment Variables
```
OLLAMA_URL=http://localhost:11434   # Ollama server URL
OLLAMA_MODEL=mistral                # Model to use
API_KEY=dev-key-change-in-production  # API key (change before any shared use)
AUDIT_LOG=audit.jsonl               # Audit log output path
```

## MCP Tools (mcp_server.py — spawned automatically as subprocess)

| Tool | Input | Backend |
|------|-------|---------|
| `transcribe_audio` | `audio_path: str` | Whisper base model |
| `analyze_speech` | `transcript: str, slide_num: int, total_slides: int` | Ollama/Mistral |
| `analyze_video` | `video_path: str` | MediaPipe FaceMesh + Pose |

`analyze_video` returns: `eye_contact_ratio`, `avg_posture_score`, `avg_gesture_delta`, `avg_brightness`, `face_detection_rate`, `frames_analysed`, `partial_score` (out of 30), `tips`.

## Running
```bash
# Development (single worker, localhost only)
python app.py

# Production
gunicorn -w 1 -b 127.0.0.1:5000 app:app

# MCP server (started automatically by ADK agents as subprocess)
python mcp_server.py
```
