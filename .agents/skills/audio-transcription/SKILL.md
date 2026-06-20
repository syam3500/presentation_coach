---
name: audio-transcription
description: |
  Transcribes audio files to text using the Whisper speech recognition model via the MCP server.
  Use when you need to convert spoken audio to written text.
  Trigger keywords: transcribe, audio, speech-to-text, recording, whisper, microphone.
  Do NOT use for video-only files, text-to-speech synthesis, or language translation.
---

# Audio Transcription Skill

## What this skill does
Converts audio files (wav, mp3, webm, ogg) to plain text using OpenAI Whisper running locally
via the MCP `transcribe_audio` tool.

## How to invoke
Call the `transcribe_audio` MCP tool with the absolute path to the saved audio file:

```python
transcribe_audio(audio_path="/tmp/recording_abc123.wav")
```

The MCP server loads Whisper on first call (~2s cold start). Subsequent calls are faster.

## Output
Raw transcript text. No timestamps, no speaker labels, no punctuation correction.

## Constraints
- Supported formats: wav, mp3, webm, ogg, m4a
- Language: English (forced via `language='en'`)
- Model: Whisper `base` (~74M params, ~1GB RAM)
- Max file size: ~25MB

## Error handling
On failure, return empty string and log the error. Do not retry automatically —
surface the error to the orchestrator.

## Integration
This skill is the first step in the PresentationCoachOrchestrator pipeline.
Its output is stored in session state under key `transcript` and passed
automatically to the PresentationFeedbackAgent.
