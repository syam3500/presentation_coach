---
name: presentation-feedback
description: |
  Analyzes a speech transcript for presentation quality across four dimensions.
  Use when you have a transcript and need to evaluate pacing, filler words, tone, and content relevance.
  Trigger keywords: feedback, analyze speech, pacing, fillers, tone, presentation quality, coach, review.
  Do NOT use for grammar checking, translation, summarization, or factual Q&A.
---

# Presentation Feedback Skill

## What this skill does
Evaluates a speech transcript and returns structured JSON feedback across four dimensions:
pacing, filler words, tone, and relevance to the current slide.

## How to invoke
Call the `analyze_speech` MCP tool with the transcript and slide context:

```python
analyze_speech(
    transcript="Today we'll cover three main pillars...",
    slide_num=2,
    total_slides=10
)
```

The slide context (`slide_num` / `total_slides`) calibrates the relevance score.

## Output format
```json
{
  "pacing": "Good pace around 140 wpm. Consider pausing after key points.",
  "fillers": "2 instances of 'um', 1 'like'. Try a silent pause instead.",
  "tone": "Confident and clear. Good variation in pitch.",
  "relevance": "Stays on topic for slide 2. Intro connects well to agenda."
}
```

## Scoring rubric
See `references/rubric.md` for full criteria.

## Context required
Always pass `slide_num` and `total_slides`. Without them the relevance score is unreliable.

## Integration
This skill is the second step in the PresentationCoachOrchestrator pipeline.
It reads `{transcript}` from session state (set by AudioTranscriptionAgent)
and stores its output under session state key `feedback`.
