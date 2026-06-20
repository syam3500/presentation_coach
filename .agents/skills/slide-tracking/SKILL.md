---
name: slide-tracking
description: |
  Manages the current slide number and total slide count for a live presentation rehearsal session.
  Use when you need to update, retrieve, or validate slide context during a session.
  Trigger keywords: slide, next slide, previous slide, slide number, total slides, current slide, advance.
  Do NOT use for content analysis, audio processing, or feedback generation.
---

# Slide Tracking Skill

## What this skill does
Maintains the presentation state: which slide the speaker is currently on and the total
number of slides. This context is injected into the feedback prompt to calibrate
relevance scoring.

## API
```
POST /api/slide
Body: { "slide": 3, "total": 12 }
Response: { "slide": 3, "total": 12 }
```

## Rules
- `slide_num` must be an integer between 1 and `total_slides`
- `total_slides` defaults to 10 if not explicitly set
- State is in-memory per process — resets on server restart
- Slide updates are broadcast to the feedback pipeline automatically

## Usage in the pipeline
The orchestrator reads `current_slide` and `total_slides` from server state
and passes them to `PresentationFeedbackAgent` as `slide_num` and `total_slides`.

## Future improvement
Replace in-memory state with a session store (Redis / SQLite) to support
multi-user and persistent rehearsal sessions.
