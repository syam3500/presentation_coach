"""
MCP Server — Presentation Coach Tools
Concept demonstrated: MCP Server (Day 2)

Exposes two deterministic tools via stdio transport:
  • transcribe_audio  — Whisper speech-to-text
  • analyze_video     — MediaPipe eye contact, posture, gesture, lighting

The actual implementations live in media_tools.py (single source of truth), so
this server and the in-process hot path in adk_agents.py never diverge.

Run standalone: python mcp_server.py
"""
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from media_tools import analyze_video_file, transcribe_media

server = Server("presentation-coach-tools")


# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="transcribe_audio",
            description="Transcribe an audio or video file to text using OpenAI Whisper (runs locally).",
            inputSchema={
                "type": "object",
                "properties": {
                    "audio_path": {
                        "type": "string",
                        "description": "Absolute path to the media file (wav, mp3, webm, ogg, mp4, ...).",
                    }
                },
                "required": ["audio_path"],
            },
        ),
        Tool(
            name="analyze_video",
            description=(
                "Analyze a video file for presentation presence using MediaPipe. "
                "Returns JSON with eye_contact_ratio, posture, gesture, lighting scores and tips."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "video_path": {
                        "type": "string",
                        "description": "Absolute path to the video file (mp4, webm, mov, avi).",
                    }
                },
                "required": ["video_path"],
            },
        ),
    ]


# ── Tool implementations (thin wrappers over media_tools) ─────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "transcribe_audio":
        # CPU-bound; run off the event loop so the stdio server stays responsive.
        transcript = await asyncio.to_thread(transcribe_media, arguments["audio_path"])
        return [TextContent(type="text", text=transcript)]

    if name == "analyze_video":
        result = await asyncio.to_thread(analyze_video_file, arguments["video_path"])
        return [TextContent(type="text", text=json.dumps(result))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    asyncio.run(main())
