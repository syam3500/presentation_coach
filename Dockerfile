# Dockerfile — Presentation Coach Agent
# Packages the Flask API + all Python dependencies.
# Ollama runs as a separate service (see docker-compose.yml).

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Whisper (ffmpeg) and MediaPipe (libGL, libglib2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py adk_agents.py mcp_server.py slide_extractor.py AGENTS.md ./
COPY .agents/ .agents/

# Security: run as non-root user
RUN useradd -m -u 1000 coach
USER coach

EXPOSE 5000

# Production server: single worker (Whisper is not thread-safe)
CMD ["python", "-m", "gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
